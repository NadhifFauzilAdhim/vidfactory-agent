"""Text-to-speech services.

Layered so each concern is testable on its own:

``TTSConfigurationService``  resolves the effective configuration by inheritance
                            (scene → workflow → channel → workspace → provider default) and
                            records which level supplied each field.
``TTSProviderFactory``       turns a provider slug (+ optional model) into a configured adapter,
                            using the credential service; also resolves fallbacks.
``TTSGenerationService``     the only place that calls an adapter: chunking, per-chunk retry,
                            fallback, assembly, validation, persistence and usage accounting.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import audio as audio_tools
from app.adapters import tts_registry
from app.adapters.storage import get_storage
from app.core.crypto import CredentialError
from app.core.errors import ValidationAppError
from app.core.logging import get_logger
from app.core.settings import get_settings
from app.models import AiProvider, AiUsageLog, Channel, ChannelSetting, TTSConfiguration
from app.ports.tts import (
    SynthesisResult,
    TTSConfig,
    TTSConfigError,
    TTSError,
    TTSInvalidRequestError,
)
from app.ports.tts_provider import TTSProvider
from app.services import pricing
from app.services.chunking import split_text
from app.services.credentials import CredentialService

log = get_logger("factory.tts")

SCOPE_ORDER = ("scene", "workflow", "channel", "workspace", "provider")
_OVERRIDE_KEYS = ("model", "voice_id", "language", "output_format")


# --------------------------------------------------------------------------- config inheritance


class TTSOverride(BaseModel):
    """A partial configuration at one inheritance level (all fields optional)."""

    model_config = {"extra": "forbid"}
    provider: str | None = None
    fallback_provider: str | None = None
    model: str | None = None
    voice_id: str | None = None
    language: str | None = None
    output_format: str | None = None
    voice_settings: dict[str, Any] = Field(default_factory=dict)
    fallback_on_transient: bool = True

    def values(self) -> dict[str, Any]:
        """Fields actually provided at this level.

        ``exclude_defaults`` is deliberately avoided: a value equal to the field default (for
        example ``provider="mock"``) is still a real override and must win over less specific
        levels. Unset fields stay ``None`` and therefore never overwrite.
        """
        data = {key: value for key, value in self.model_dump().items()
                if value is not None and value != ""}
        if not data.get("voice_settings"):
            data.pop("voice_settings", None)
        return data

    def meaningful(self) -> bool:
        data = self.values()
        return bool(data.get("provider") or data.get("fallback_provider") or data.get("model")
                    or data.get("voice_id") or data.get("language") or data.get("output_format")
                    or data.get("voice_settings"))


@dataclass(slots=True)
class ResolvedTTSConfig:
    provider: str
    fallback_provider: str | None
    model: str | None
    voice_id: str | None
    language: str
    output_format: str | None
    voice_settings: dict[str, Any]
    sources: dict[str, str] = field(default_factory=dict)
    fallback_on_transient: bool = True
    chain: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "fallback_provider": self.fallback_provider,
            "model": self.model,
            "voice_id": self.voice_id,
            "language": self.language,
            "output_format": self.output_format,
            "voice_settings": self.voice_settings,
            "sources": self.sources,
            "fallback_on_transient": self.fallback_on_transient,
            "chain": self.chain,
        }


class TTSConfigurationService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def global_config(self, workspace_id: uuid.UUID) -> TTSConfiguration | None:
        return await self.session.scalar(
            select(TTSConfiguration).where(TTSConfiguration.workspace_id == workspace_id)
        )

    async def set_global_config(
        self,
        workspace_id: uuid.UUID,
        *,
        provider: str,
        actor_id: uuid.UUID | None = None,
        fallback_provider: str | None = None,
        model: str | None = None,
        voice_id: str | None = None,
        language: str | None = None,
        output_format: str | None = None,
        voice_settings: dict[str, Any] | None = None,
    ) -> TTSConfiguration:
        for slug in filter(None, [provider, fallback_provider]):
            if slug not in tts_registry.ADAPTERS:
                raise ValidationAppError(f"Unknown TTS provider '{slug}'")
        if fallback_provider and fallback_provider == provider:
            raise ValidationAppError("The fallback provider must differ from the primary provider")
        row = await self.global_config(workspace_id)
        if row is None:
            row = TTSConfiguration(workspace_id=workspace_id, provider_slug=provider)
            self.session.add(row)
        row.provider_slug = provider
        row.fallback_provider_slug = fallback_provider or None
        row.model = model or None
        row.voice_id = voice_id or None
        row.language = language or None
        row.output_format = output_format or None
        row.voice_settings = {k: v for k, v in (voice_settings or {}).items() if v is not None}
        row.updated_by = actor_id
        row.last_verified_at = datetime.now(UTC)
        await self.session.flush()
        return row

    async def channel_override(self, channel_id: uuid.UUID | None) -> TTSOverride:
        if channel_id is None:
            return TTSOverride()
        setting = await self.session.scalar(
            select(ChannelSetting).where(ChannelSetting.channel_id == channel_id)
        )
        if setting is None:
            return TTSOverride()
        config = dict(setting.voice_config or {})
        return TTSOverride(
            provider=setting.voice_provider or config.get("provider"),
            model=setting.voice_model or config.get("model"),
            voice_id=setting.voice_id or config.get("voice_id"),
            language=config.get("language"),
            output_format=config.get("output_format"),
            voice_settings={k: v for k, v in config.items()
                            if k not in {"provider", "model", "voice_id", "language", "output_format"}},
            fallback_provider=config.get("fallback_provider"),
            fallback_on_transient=bool(config.get("fallback_on_transient", True)),
            # Any voice setting on a channel counts as an override even if only settings changed.
        )

    async def first_provider(self, workspace_id: uuid.UUID) -> AiProvider | None:
        """The provider a missing voice config inherits from: only one holding a real key.

        A mock row is never inherited implicitly. Seeding used to create a mock TTS provider, so
        falling back to it was harmless; now that a fresh workspace has none, a silent fallback
        would mean placeholder audio pretending to be narration.
        """
        rows = list((await self.session.execute(
            select(AiProvider).where(
                AiProvider.workspace_id == workspace_id,
                AiProvider.kind == "tts",
                AiProvider.is_enabled.is_(True),
            ).order_by(AiProvider.is_mock.asc(), AiProvider.priority.asc())
        )).scalars())
        real = [r for r in rows if not r.is_mock and r.credential_ciphertext]
        return real[0] if real else None

    async def resolve(
        self,
        *,
        workspace_id: uuid.UUID,
        channel: Channel | None = None,
        workflow_override: TTSOverride | None = None,
        scene_override: TTSOverride | None = None,
        explicit: TTSOverride | None = None,
    ) -> ResolvedTTSConfig:
        """Apply overrides from least to most specific; later levels win."""
        global_row = await self.global_config(workspace_id)
        channel_ov = await self.channel_override(channel.id if channel else None)
        levels: list[tuple[str, TTSOverride]] = [
            ("global", TTSOverride(
                provider=global_row.provider_slug if global_row else None,
                fallback_provider=global_row.fallback_provider_slug if global_row else None,
                model=global_row.model if global_row else None,
                voice_id=global_row.voice_id if global_row else None,
                language=global_row.language if global_row else None,
                output_format=global_row.output_format if global_row else None,
                voice_settings=dict(global_row.voice_settings or {}) if global_row else {},
            )),
            ("channel", channel_ov),
            ("workflow", workflow_override or TTSOverride()),
            ("scene", scene_override or TTSOverride()),
            ("request", explicit or TTSOverride()),
        ]
        resolved = {
            "provider": None, "fallback_provider": None, "model": None, "voice_id": None,
            "language": None, "output_format": None,
        }
        sources: dict[str, str] = {}
        settings: dict[str, Any] = {}
        fallback_on_transient = True
        chain: list[dict[str, Any]] = []
        for name, level in levels:
            data = level.values()
            if not level.meaningful():
                continue
            chain.append({"level": name, "values": {
                key: value for key, value in data.items() if key != "fallback_on_transient"
            }})
            for key in ("provider", "fallback_provider", "model", "voice_id", "language",
                        "output_format"):
                value = data.get(key)
                if value not in (None, ""):
                    resolved[key] = value
                    sources[key] = name
            settings.update({k: v for k, v in (level.voice_settings or {}).items() if v is not None})
            if isinstance(data.get("fallback_on_transient"), bool):
                fallback_on_transient = bool(data["fallback_on_transient"])

        provider = resolved["provider"]
        if not provider:
            first = await self.first_provider(workspace_id)
            if first is None:
                raise TTSConfigError(
                    "No text-to-speech provider is configured for this workspace. "
                    "Add Cartesia or ElevenLabs under Providers."
                )
            provider = first.slug
            sources["provider"] = "provider"
        if channel and not resolved["language"]:
            resolved["language"] = channel.language
            sources["language"] = "channel.default"
        model = resolved["model"]
        if not model:
            adapter_meta = tts_registry.CATALOG.get(provider, {})
            model = adapter_meta.get("default_model")
            sources["model"] = "provider.default"
        if not resolved["voice_id"]:
            voice = await self._first_voice(provider, workspace_id)
            if voice:
                resolved["voice_id"] = voice
                sources["voice_id"] = "provider.default"
        voice_settings: dict[str, Any] = {}
        if provider == "elevenlabs":
            voice_settings = {
                "stability": settings.pop("stability", 0.5),
                "similarity_boost": settings.pop("similarity_boost", 0.75),
                "speed": settings.pop("speed", 1.0),
                "style": settings.pop("style", 0.0),
                "use_speaker_boost": settings.pop("use_speaker_boost", True),
            }
            voice_settings.update(settings)
        else:
            voice_settings = {"speed": 1.0, "volume": 1.0, **settings}
        return ResolvedTTSConfig(
            provider=provider,
            fallback_provider=resolved["fallback_provider"],
            model=model,
            voice_id=resolved["voice_id"],
            language=(resolved["language"] or "en"),
            output_format=resolved["output_format"],
            voice_settings={k: v for k, v in voice_settings.items() if v is not None},
            sources=sources,
            fallback_on_transient=fallback_on_transient,
            chain=chain,
        )

    async def _first_voice(self, provider: str, workspace_id: uuid.UUID) -> str | None:
        if provider == "mock":
            return "mock-narrator"
        row = await self.session.scalar(
            select(AiProvider).where(
                AiProvider.workspace_id == workspace_id,
                AiProvider.kind == "tts",
                AiProvider.slug == provider,
            )
        )
        if row is None:
            return None
        return row.extra_config.get("default_voice_id") or tts_registry.CATALOG.get(provider, {}).get(
            "default_voice"
        )


# --------------------------------------------------------------------------- provider factory


@dataclass(slots=True)
class ProviderHandle:
    slug: str
    provider: TTSProvider
    row: AiProvider | None
    credential_version: int
    configured: bool = True


class TTSProviderFactory:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.credentials = CredentialService(session)

    async def row_for(self, workspace_id: uuid.UUID, slug: str) -> AiProvider | None:
        return await self.session.scalar(
            select(AiProvider).where(
                AiProvider.workspace_id == workspace_id,
                AiProvider.kind == "tts",
                AiProvider.slug == slug,
            )
        )

    async def build(
        self,
        workspace_id: uuid.UUID,
        slug: str,
        *,
        model: str | None = None,
        voice_id: str | None = None,
        output_format: str | None = None,
        expected_version: int | None = None,
    ) -> ProviderHandle:
        if slug not in tts_registry.ADAPTERS:
            raise TTSConfigError(f"Unknown TTS provider '{slug}'")
        row = await self.row_for(workspace_id, slug)
        if slug == "mock":
            return ProviderHandle(slug, tts_registry.build("mock", api_key=None, model=model,
                                                          voice_id=voice_id,
                                                          output_format=output_format), row, 0)
        if row is None:
            raise TTSConfigError(
                f"{slug} is not registered for this workspace. Add it under Providers."
            )
        if not row.is_enabled:
            raise TTSConfigError(f"{row.name} is disabled")
        if not row.credential_ciphertext:
            raise TTSConfigError(f"{row.name} has no API key configured")
        try:
            key = await self.credentials.get_secret(row, expected_version=expected_version)
        except CredentialError as exc:
            raise TTSConfigError(str(exc)) from exc
        if not key:
            raise TTSConfigError(f"{row.name} has no API key configured")
        provider = tts_registry.build(
            slug,
            api_key=key,
            base_url=row.base_url,
            model=model or (row.extra_config or {}).get("default_model"),
            voice_id=voice_id or (row.extra_config or {}).get("default_voice_id"),
            output_format=output_format or (row.extra_config or {}).get("default_output_format"),
        )
        return ProviderHandle(slug, provider, row, int(row.credential_version or 0))

    async def list_configured(self, workspace_id: uuid.UUID) -> list[dict[str, Any]]:
        rows = list((await self.session.execute(
            select(AiProvider).where(
                AiProvider.workspace_id == workspace_id, AiProvider.kind == "tts"
            ).order_by(AiProvider.is_mock.asc(), AiProvider.priority.asc())
        )).scalars())
        out = []
        for row in rows:
            meta = tts_registry.CATALOG.get(row.slug, {})
            out.append({
                "slug": row.slug,
                "label": row.name or meta.get("label", row.slug),
                "mock": row.is_mock,
                "enabled": row.is_enabled,
                "has_credential": bool(row.credential_ciphertext),
                "credential_version": int(row.credential_version or 0),
                "fingerprint": row.credential_fingerprint,
                "default_model": (row.extra_config or {}).get("default_model")
                or meta.get("default_model"),
                "default_voice": (row.extra_config or {}).get("default_voice_id")
                or meta.get("default_voice"),
                "default_output_format": (row.extra_config or {}).get("default_output_format")
                or meta.get("default_format"),
                "base_url": row.base_url,
                "docs": meta.get("docs"),
                "key_hint": meta.get("key_hint"),
                "usable": row.is_enabled and (row.is_mock or bool(row.credential_ciphertext)),
            })
        return out


# --------------------------------------------------------------------------- generation


@dataclass(slots=True)
class GeneratedAudio:
    result: SynthesisResult
    audio: bytes
    mime: str
    duration_s: float
    chunk_results: list[dict[str, Any]] = field(default_factory=list)
    fallback_used: bool = False
    fallback_reason: str | None = None
    provider: str = ""
    model: str = ""
    voice_id: str = ""
    output_format: str = ""
    estimated_cost_usd: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def as_summary(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "voice_id": self.voice_id,
            "output_format": self.output_format,
            "duration_s": self.duration_s,
            "bytes": len(self.audio),
            "characters": self.result.character_count,
            "latency_ms": self.result.latency_ms,
            "estimated_cost_usd": self.estimated_cost_usd,
            "actual_cost_usd": self.result.actual_cost_usd,
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
            "chunks": self.chunk_results,
            "warnings": self.warnings,
        }


class TTSGenerationService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.factory = TTSProviderFactory(session)
        self.config_service = TTSConfigurationService(session)
        self.storage = get_storage()

    # -- chunk sizing ------------------------------------------------------
    async def chunk_limit(self, handle: ProviderHandle, model: str) -> int:
        if handle.provider.is_mock:
            return get_settings().voice_max_chars
        try:
            models = await handle.provider.list_models()
        except TTSError:
            models = []
        for info in models:
            if info.id == model and info.max_chars:
                return int(info.max_chars)
        return get_settings().voice_max_chars

    # -- single call with per-chunk retries --------------------------------
    async def _synthesize_text(
        self,
        handle: ProviderHandle,
        config: ResolvedTTSConfig,
        text: str,
        *,
        seed: int | None = None,
        prior_text: str | None = None,
        next_text: str | None = None,
    ) -> SynthesisResult:
        req = TTSConfig(
            text=text,
            voice_id=config.voice_id or "",
            model=config.model or "",
            language=config.language,
            output_format=config.output_format,
            voice_settings=config.voice_settings,
            seed=seed,
            speed=config.voice_settings.get("speed"),
            emotion=config.voice_settings.get("emotion"),
            prior_text=prior_text,
            next_text=next_text,
        )
        # Single attempt: the adapter is the retry owner (exponential backoff with jitter for
        # timeouts, 5xx and 429). Chunk-level retries live in the voice agent so a failed chunk
        # can be regenerated without repeating the whole narration; nesting the two would
        # multiply requests against a rate-limited provider.
        return await handle.provider.generate_speech(req)

    async def _generate_with(
        self,
        handle: ProviderHandle,
        config: ResolvedTTSConfig,
        text: str,
        *,
        before: str = "Hello, this is a narration range check.",
    ) -> GeneratedAudio:
        limit = await self.chunk_limit(handle, config.model or "")
        chunks = split_text(text, limit)
        if not chunks:
            raise TTSInvalidRequestError("Nothing to synthesise")
        audios: list[bytes] = []
        metas: list[dict[str, Any]] = []
        cost = 0.0
        latency = 0
        mime = "audio/wav"
        rate = 44100
        actual_total: float | None = 0.0
        warnings: list[str] = []
        for i, chunk in enumerate(chunks):
            started = time.perf_counter()
            prior = chunks[i - 1].text[-400:] if i > 0 else before[:200]
            upcoming = chunks[i + 1].text[:400] if i + 1 < len(chunks) else None
            result = await self._synthesize_text(handle, config, chunk.text, seed=None,
                                                 prior_text=prior, next_text=upcoming)
            audios.append(result.audio)
            mime, rate = result.mime, result.sample_rate
            cost += result.estimated_cost_usd
            latency += result.latency_ms
            if actual_total is not None:
                actual_total = (actual_total or 0.0) + (result.actual_cost_usd or 0.0) if (
                    result.actual_cost_usd is not None) else None
            warnings.extend(w for w in result.warnings if w not in warnings)
            metas.append({
                "index": chunk.index,
                "characters": len(chunk.text),
                "bytes": len(result.audio),
                "duration_s": result.duration_s,
                "latency_ms": result.latency_ms,
                "preview": chunk.text[:90] + ("…" if len(chunk.text) > 90 else ""),
                "generated_at": datetime.now(UTC).isoformat(),
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
            })
            log.info("tts.chunk provider=%s model=%s chunk=%s/%s chars=%s ms=%s",
                     handle.slug, config.model, i + 1, len(chunks), len(chunk.text), result.latency_ms)

        if len(audios) > 1:
            started = time.perf_counter()
            if mime != "audio/wav":
                audios = [audio_tools.resample_to_wav(a) for a in audios]
            combined = await audio_tools.combine(audios, mime="audio/wav", output_format="wav")
            latency += int((time.perf_counter() - started) * 1000)
            mime, rate = "audio/wav", 44100
            warnings.append(f"Assembled {len(audios)} chunks with 220 ms joins and loudness matching.")
        else:
            combined = audios[0]

        audio_tools.validate_audio(combined, mime=mime, provider=handle.slug)
        if mime == "audio/wav":
            audio_tools.validate_wav(combined, provider=handle.slug)
        duration = audio_tools.duration_of(combined)
        if duration <= 0:
            raise TTSError("Generated audio has no measurable duration")
        summary = SynthesisResult(
            audio=combined,
            mime=mime,
            duration_s=duration,
            provider=handle.slug,
            model=config.model or "",
            voice_id=config.voice_id or "",
            output_format=config.output_format or "provider-default",
            sample_rate=rate,
            character_count=len(text),
            latency_ms=latency,
            estimated_cost_usd=round(cost, 6),
            actual_cost_usd=round(actual_total, 6) if actual_total else None,
        )
        if not actual_total:
            summary.actual_cost_usd = None
        return GeneratedAudio(
            result=summary,
            audio=combined,
            mime=mime,
            duration_s=duration,
            chunk_results=metas,
            provider=handle.slug,
            model=config.model or "",
            voice_id=config.voice_id or "",
            output_format=config.output_format or "",
            estimated_cost_usd=summary.estimated_cost_usd,
            warnings=warnings,
        )

    async def generate(
        self,
        *,
        workspace_id: uuid.UUID,
        text: str,
        config: ResolvedTTSConfig,
        expected_version: int | None = None,
        before: str = "Hello, this is a narration range check.",
        allow_fallback: bool = True,
    ) -> GeneratedAudio:
        if not text.strip():
            raise TTSInvalidRequestError("Enter some text to synthesise")
        handle = await self.factory.build(
            workspace_id, config.provider, model=config.model, voice_id=config.voice_id,
            output_format=config.output_format, expected_version=expected_version,
        )
        if config.voice_id:
            await self._assert_model_supports(handle, config)
        try:
            result = await self._generate_with(handle, config, text, before=before)
        except TTSError as exc:
            if not (exc.transient and config.fallback_on_transient and allow_fallback
                    and config.fallback_provider and config.fallback_provider != config.provider):
                raise
            log.warning("tts.fallback primary=%s fallback=%s code=%s",
                        config.provider, config.fallback_provider, exc.code)
            fallback_cfg = ResolvedTTSConfig(
                provider=config.fallback_provider,
                fallback_provider=None,
                model=None,
                voice_id=await self.config_service._first_voice(config.fallback_provider,
                                                                 workspace_id),
                language=config.language,
                output_format=None,
                voice_settings={},
                sources={**config.sources, "provider": "fallback"},
                fallback_on_transient=False,
                chain=[*config.chain, {"level": "fallback", "values": {
                    "provider": config.fallback_provider, "reason": exc.code}}],
            )
            fallback_handle = await self.factory.build(
                workspace_id, config.fallback_provider, model=fallback_cfg.model,
                voice_id=fallback_cfg.voice_id, output_format=None,
            )
            fallback_cfg.model = fallback_handle.provider.default_model
            result = await self._generate_with(fallback_handle, fallback_cfg, text, before=before)
            result.fallback_used = True
            result.fallback_reason = exc.message
            result.warnings = [*result.warnings,
                               f"Primary provider {config.provider} failed ({exc.code}); "
                               f"{config.fallback_provider} produced this audio."]
        self._cost_estimate_check(config, result, await self.factory.row_for(workspace_id,
                                                                           result.provider))
        return result

    async def _fallback_voice_settings(self) -> dict[str, Any]:
        return {}

    def _cost_estimate_check(self, config: ResolvedTTSConfig, result: GeneratedAudio,
                             row: AiProvider | None = None) -> None:
        # Adapter estimates are public list prices; prefer configured contract rates when present.
        estimate = pricing.estimate(provider=result.provider, model=result.model,
                                    characters=result.result.character_count,
                                    models=list(row.models or []) if row else None)
        result.estimated_cost_usd = estimate.estimated_usd
        result.result.estimated_cost_usd = estimate.estimated_usd
        result.result.actual_cost_usd = None

    async def _assert_model_supports(self, handle: ProviderHandle, config: ResolvedTTSConfig) -> None:
        if not config.model:
            return
        try:
            formats = handle.provider.output_formats(config.model)
        except Exception:  # noqa: BLE001
            return
        if not formats:
            return
        if config.output_format and not any(f.id == config.output_format for f in formats):
            raise TTSInvalidRequestError(
                f"Output format '{config.output_format}' is not supported by "
                f"{handle.slug} {config.model}"
            )
        if not handle.provider.language_supported(config.model, config.language):
            raise TTSInvalidRequestError(
                f"{handle.slug} {config.model} does not support the language '{config.language}'"
            )

    # -- persistence -------------------------------------------------------
    async def persist(
        self,
        *,
        generated: GeneratedAudio,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID | None,
        text: str,
        request: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
        store_chunks: bool = False,
        channel_id: uuid.UUID | None = None,
        workflow_id: uuid.UUID | None = None,
        content_id: uuid.UUID | None = None,
        trigger: str = "studio",
        request_key: str | None = None,
    ) -> dict[str, Any]:
        # The audio and the small DB writes run sequentially: concurrent compare() sides share one
        # session, and AsyncSession cannot be used from two coroutines at once.
        return await self._persist(
            generated=generated, workspace_id=workspace_id, actor_id=actor_id, text=text,
            request=request, config=config, store_chunks=store_chunks, channel_id=channel_id,
            workflow_id=workflow_id, content_id=content_id, trigger=trigger,
            request_key=request_key,
        )

    async def _persist(self, **kwargs: Any) -> dict[str, Any]:
        generated: GeneratedAudio = kwargs["generated"]
        workspace_id: uuid.UUID = kwargs["workspace_id"]
        actor_id: uuid.UUID | None = kwargs["actor_id"]
        text: str = kwargs["text"]
        request = kwargs.get("request")
        config = kwargs.get("config")
        channel_id = kwargs.get("channel_id")
        workflow_id = kwargs.get("workflow_id")
        content_id = kwargs.get("content_id")
        trigger: str = kwargs.get("trigger") or "studio"
        request_key = kwargs.get("request_key")
        digest = hashlib.sha256(
            f"{generated.provider}:{generated.model}:{generated.voice_id}:{text}".encode()
        ).hexdigest()[:16]
        ext = "wav" if generated.mime == "audio/wav" else (
            "mp3" if generated.mime == "audio/mpeg" else "audio"
        )
        key = f"tts/{workspace_id}/{digest}.{ext}"
        content_type = "audio/wav" if ext == "wav" else generated.mime
        await self.storage.put_bytes(key, generated.audio, content_type)
        stored = {"key": key, "bytes": len(generated.audio), "mime": content_type}

        if kwargs.get("store_chunks") and len(generated.chunk_results) > 1:
            stored["chunk_keys"] = [
                f"tts/{workspace_id}/{digest}-part-{i:03d}.{ext}"
                for i in range(len(generated.chunk_results))
            ]            # Chunk audio is not retained separately by default to save storage; the manifest keeps
            # per-chunk metadata so a single failed chunk can be regenerated by index.

        from app.models.tts_generation import TTSGeneration  # local import avoids cycles

        generation = TTSGeneration(
            workspace_id=workspace_id,
            provider=generated.provider,
            model=generated.model,
            voice_id=generated.voice_id or None,
            language=(config or {}).get("language"),
            input_text=text,
            character_count=generated.result.character_count,
            audio_path=key,
            audio_format=generated.output_format or (config or {}).get("output_format"),
            mime_type=content_type,
            audio_duration=generated.duration_s,
            generation_latency=generated.result.latency_ms,
            estimated_cost=generated.estimated_cost_usd,
            actual_cost=generated.result.actual_cost_usd,
            status="succeeded",
            provider_request_id=generated.result.request_id,
            fallback_used=generated.fallback_used,
            fallback_from=None if not generated.fallback_used else (config or {}).get("provider"),
            trigger=trigger,
            is_test=trigger == "studio",
            mode="single",
            chunk_count=max(1, len(generated.chunk_results)),
            chunk_manifest=generated.chunk_results,
            settings={**(config or {}), **(request or {})},
            workflow_id=workflow_id,
            channel_id=channel_id,
            content_id=content_id,
            created_by=actor_id,
            request_key=request_key or self._idempotency_key(workspace_id, text, config),
        )
        # Idempotency: the same request key (a resumed workflow re-synthesising a scene) must not
        # violate the UNIQUE constraint. Reuse the existing row instead of inserting a duplicate.
        from sqlalchemy import select as _select

        existing = None
        if generation.request_key:
            existing = await self.session.scalar(
                _select(TTSGeneration).where(
                    TTSGeneration.workspace_id == workspace_id,
                    TTSGeneration.request_key == generation.request_key,
                )
            )
        if existing is not None:
            out = generation_summary(existing)
            out["stored"] = stored
            out["chunks"] = generated.chunk_results
            out["warnings"] = [*generated.warnings, "Reused the existing generation for this request"]
            return out

        self.session.add(generation)
        await self.session.flush()
        await self._record_usage(            generation,
            workspace_id=workspace_id,
            channel_id=channel_id,
            workflow_id=workflow_id,
            content_id=content_id,
            operation="tts_speech",
        )
        await self.session.flush()
        out = generation_summary(generation)
        out["stored"] = stored
        out["chunks"] = generated.chunk_results
        out["warnings"] = generated.warnings
        if generated.fallback_used:
            out["fallback_reason"] = generated.fallback_reason
        return out

    @staticmethod
    def _idempotency_key(workspace_id: uuid.UUID, text: str, config: dict[str, Any] | None) -> str:
        """Content hash + nonce.

        The hash identifies identical inputs (useful for analytics and for a future
        "return the cached clip" mode); the nonce keeps every user-initiated generation distinct so
        a deliberate repeat is not mistaken for a duplicate request.
        """
        cfg = {k: (config or {}).get(k) for k in ("provider", "model", "voice_id", "output_format",
                                                  "language")}
        payload = f"{workspace_id}|{text}|{sorted(cfg.items())}|{uuid.uuid4()}"
        return "tts:" + hashlib.sha256(payload.encode()).hexdigest()[:40]

    async def _record_usage(self, generation, *, workspace_id: uuid.UUID, channel_id, workflow_id,
                            content_id, operation: str) -> None:
        from app.models.tts_generation import TTSUsageLog

        self.session.add(TTSUsageLog(
            workspace_id=workspace_id,
            generation_id=generation.id,
            provider=generation.provider,
            model=generation.model,
            operation=operation,
            usage_quantity=float(generation.character_count),
            usage_unit="characters",
            estimated_cost=generation.estimated_cost,
            actual_cost=generation.actual_cost,
            latency=generation.generation_latency,
            status=generation.status,
            channel_id=channel_id,
            workflow_id=workflow_id,
            content_id=content_id,
        ))
        request_key = f"tts:{generation.id}"
        exists = await self.session.scalar(
            select(AiUsageLog.id).where(AiUsageLog.request_key == request_key)
        )
        provider_row = await self.factory.row_for(workspace_id, generation.provider)
        if not exists:
            self.session.add(AiUsageLog(
                workspace_id=workspace_id,
                provider_id=provider_row.id if provider_row else None,
                channel_id=channel_id,
                workflow_id=workflow_id,
                project_id=content_id,
                agent_key="audio_studio" if generation.trigger == "studio" else "voice_generation",
                provider_slug=generation.provider,
                model=generation.model,
                operation="tts",
                units=float(generation.character_count),
                unit_label="chars",
                cost_usd=generation.estimated_cost,
                latency_ms=generation.generation_latency,
                status=generation.status,
                request_key=request_key,
            ))


def generation_summary(g: Any) -> dict[str, Any]:
    return {
        "id": str(g.id),
        "provider": g.provider,
        "model": g.model,
        "voice_id": g.voice_id,
        "language": g.language,
        "text": g.input_text,
        "characters": g.character_count,
        "audio_url": f"/files/{g.audio_path}" if g.audio_path else None,
        "audio_format": g.audio_format,
        "mime": g.mime_type,
        "duration_s": float(g.audio_duration or 0),
        "latency_ms": g.generation_latency,
        "estimated_cost_usd": float(g.estimated_cost or 0),
        "actual_cost_usd": float(g.actual_cost) if g.actual_cost is not None else None,
        "status": g.status,
        "error_code": g.error_code,
        "error_message": g.error_message,
        "trigger": g.trigger,
        "is_test": g.is_test,
        "mode": g.mode,
        "chunk_count": g.chunk_count,
        "fallback_used": g.fallback_used,
        "request_id": g.provider_request_id,
        "channel_id": str(g.channel_id) if g.channel_id else None,
        "workflow_id": str(g.workflow_id) if g.workflow_id else None,
        "content_id": str(g.content_id) if g.content_id else None,
        "created_at": g.created_at.isoformat() if g.created_at else None,
    }


# --------------------------------------------------------------------------- comparison


@dataclass(slots=True)
class ComparisonSide:
    provider: str
    model: str | None
    voice_id: str | None
    output_format: str | None
    voice_settings: dict[str, Any] = field(default_factory=dict)


async def compare(
    service: TTSGenerationService,
    *,
    workspace_id: uuid.UUID,
    text: str,
    language: str,
    left: ComparisonSide,
    right: ComparisonSide,
) -> dict[str, Any]:
    """Generate the same text with two configurations and report side-by-side metrics.

    Generation runs concurrently (two independent HTTP calls). Persistence is serialised because
    both sides share one database session.
    """
    ready: dict[str, tuple[dict[str, Any], Any, ResolvedTTSConfig]] = {}
    lock = asyncio.Lock()

    async def run(side: ComparisonSide, key: str) -> None:
        started = time.perf_counter()
        try:
            cfg = await service.config_service.resolve(
                workspace_id=workspace_id,
                explicit=TTSOverride(
                    provider=side.provider, model=side.model, voice_id=side.voice_id,
                    output_format=side.output_format, language=language,
                    voice_settings=side.voice_settings,
                ),
            )
            generated = await service.generate(workspace_id=workspace_id, text=text, config=cfg,
                                               allow_fallback=False)
            async with lock:
                saved = await service.persist(
                    generated=generated, workspace_id=workspace_id, actor_id=None, text=text,
                    config=cfg.as_dict(), trigger="compare",
                )
            ready[key] = ({
                "ok": True,
                "provider": generated.provider,
                "model": generated.model,
                "voice_id": generated.voice_id,
                "output_format": generated.output_format,
                "duration_s": generated.duration_s,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "estimated_cost_usd": generated.estimated_cost_usd,
                "characters": generated.result.character_count,
                "audio_url": saved["audio_url"],
                "generation_id": saved["id"],
                "warnings": generated.warnings,
            }, generated, cfg)
        except TTSError as exc:
            ready[key] = ({
                "ok": False,
                "provider": side.provider,
                "model": side.model,
                "voice_id": side.voice_id,
                "error": exc.message,
                "error_code": exc.code,
                "latency_ms": int((time.perf_counter() - started) * 1000),
            }, None, None)  # type: ignore[assignment]

    await asyncio.gather(run(left, "left"), run(right, "right"))
    left_result = ready["left"][0]
    right_result = ready["right"][0]
    verdict = _verdict(left_result, right_result)
    return {"left": left_result, "right": right_result, "verdict": verdict}


def _verdict(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    if left.get("ok") and not right.get("ok"):
        return {"winner": "left", "reason": "The right-hand configuration failed."}
    if right.get("ok") and not left.get("ok"):
        return {"winner": "right", "reason": "The left-hand configuration failed."}
    if not (left.get("ok") and right.get("ok")):
        return {"winner": None, "reason": "Both configurations failed."}
    l_cost, r_cost = left["estimated_cost_usd"], right["estimated_cost_usd"]
    l_lat, r_lat = left["latency_ms"], right["latency_ms"]
    cheaper = "left" if l_cost < r_cost else "right" if r_cost < l_cost else "tie"
    faster = "left" if l_lat < r_lat else "right" if r_lat < l_lat else "tie"
    # Cost and latency are objective; voice quality is not, so a winner is only declared when one
    # side is no worse on both and strictly better on at least one.
    winner: str | None = None
    if cheaper == faster and cheaper != "tie":
        winner = cheaper
        reason = (
            "Cheaper and faster on this text. Still listen before deciding — voice quality is "
            "subjective."
        )
    else:
        reason = "Listen to both clips — quality is subjective, cost and latency disagree."
    return {
        "winner": winner,
        "reason": reason,
        "cheaper": cheaper,
        "faster": faster,
        "cost_delta_usd": round(r_cost - l_cost, 6),
        "latency_delta_ms": r_lat - l_lat,
    }


async def usage_summary(session: AsyncSession, workspace_id: uuid.UUID, days: int = 30) -> dict[str, Any]:
    from app.models.tts_generation import TTSGeneration

    since = datetime.now(UTC).timestamp() - days * 86400
    since_dt = datetime.fromtimestamp(since, UTC)
    rows = list((await session.execute(
        select(TTSGeneration).where(
            TTSGeneration.workspace_id == workspace_id,
            TTSGeneration.created_at >= since_dt,
        ).order_by(TTSGeneration.created_at)
    )).scalars())
    succeeded = [r for r in rows if r.status == "succeeded"]
    failed = [r for r in rows if r.status == "failed"]
    by_provider: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    by_day: dict[str, dict[str, Any]] = {}
    for row in rows:
        for bucket, key in (
            (by_provider, row.provider),
            (by_model, f"{row.provider}/{row.model}"),
            (by_day, row.created_at.strftime("%Y-%m-%d")),
        ):
            entry = bucket.setdefault(key, {"generations": 0, "characters": 0, "seconds": 0.0,
                                            "cost_usd": 0.0, "failed": 0, "latency_ms": 0,
                                            "latency_samples": 0})
            entry["generations"] += 1
            entry["characters"] += row.character_count or 0
            entry["seconds"] += float(row.audio_duration or 0)
            entry["cost_usd"] += float(row.estimated_cost or 0)
            entry["failed"] += 1 if row.status == "failed" else 0
            if row.generation_latency:
                entry["latency_ms"] += row.generation_latency
                entry["latency_samples"] += 1
    for bucket in (by_provider, by_model, by_day):
        for entry in bucket.values():
            samples = entry.pop("latency_samples") or 1
            entry["avg_latency_ms"] = int(entry.pop("latency_ms") / samples)
            entry["cost_usd"] = round(entry["cost_usd"], 6)
            entry["seconds"] = round(entry["seconds"], 2)
    total_chars = sum(r.character_count or 0 for r in succeeded)
    total_seconds = sum(float(r.audio_duration or 0) for r in succeeded)
    avg_latency = int(sum(r.generation_latency or 0 for r in succeeded) / len(succeeded)) if succeeded else 0
    return {
        "window_days": days,
        "totals": {
            "generations": len(rows),
            "succeeded": len(succeeded),
            "failed": len(failed),
            "characters": total_chars,
            "audio_seconds": round(total_seconds, 2),
            "estimated_cost_usd": round(sum(float(r.estimated_cost or 0) for r in rows), 6),
            "actual_cost_usd": round(sum(float(r.actual_cost) for r in rows if r.actual_cost is not None), 6),
            "avg_latency_ms": avg_latency,
        },
        "by_provider": by_provider,
        "by_model": by_model,
        "by_day": by_day,
    }
