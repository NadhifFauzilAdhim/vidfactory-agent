"""Runtime dependency container.

Resolves agents → their configured provider → an adapter instance. This is the only place in the
codebase where adapter classes are constructed; services and agents stay provider-agnostic.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.images import build_image_provider
from app.adapters.llm import build_llm_provider
from app.adapters.media import ffmpeg_available  # noqa: F401 - re-exported for status views
from app.adapters.storage import get_storage
from app.adapters.youtube import build_youtube_service
from app.agents.base import AgentContext
from app.agents.registry import REGISTRY
from app.core.crypto import CredentialError
from app.core.errors import NotFoundError, ProviderNotConfiguredError
from app.models import Agent, AgentConfiguration, AiProvider, Channel, ChannelSetting
from app.services.credentials import CredentialService
from app.services.tts import TTSConfigurationService
from app.services.tts import TTSProviderFactory as TTSServiceFactory

# Default models for providers when a new provider row is created / the agent has no model set.
DEFAULT_MODELS: dict[str, dict[str, str]] = {
    "openai": {"llm": "gpt-4.1-mini", "image": "gpt-image-1"},
    "gemini": {"llm": "gemini-2.5-flash", "image": "gemini-2.5-flash-image"},
    "openai-compatible": {"llm": ""},
    "anthropic-compatible": {"llm": ""},
    "cartesia": {"tts": "sonic-3.6"},
    "elevenlabs": {"tts": "eleven_multilingual_v2"},
    "mock": {"llm": "mock-structured", "tts": "mock-tts", "image": "mock-image"},
}


@dataclass(slots=True)
class ResolvedAgent:
    key: str
    spec: Any
    provider: AiProvider | None
    config: AgentConfiguration | None
    model: str
    temperature: float
    max_tokens: int
    system_prompt: str | None
    params: dict[str, Any]
    enabled: bool
    retry_policy: dict[str, Any] = field(default_factory=dict)


class ProviderFactory:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.credentials = CredentialService(session)

    async def _secret(self, provider: AiProvider) -> str | None:
        """Decrypt a provider's stored key.

        Always goes through ``CredentialService``: credentials are sealed with an AAD that binds them
        to their provider row, so opening them requires that same scope. Decrypting with the vault's
        bare ``decrypt()`` (AAD "default") fails authentication for every provider — that was the
        cause of "The stored credential could not be authenticated" on LLM/image tests.
        """
        if provider.is_mock or not provider.credential_ciphertext:
            return None
        try:
            return await self.credentials.get_secret(provider)
        except CredentialError as exc:
            raise ProviderNotConfiguredError(str(exc)) from exc

    async def build(self, provider: AiProvider):  # type: ignore[no-untyped-def]
        key = await self._secret(provider)
        kind = provider.kind
        if kind == "llm":
            return build_llm_provider(
                provider.slug, key, provider.base_url,
                label=provider.name, models=list(provider.models or []),
            )
        if kind == "tts":
            if provider.slug == "mock":
                from app.adapters.tts_registry import build as build_tts

                extra = provider.extra_config or {}
                return build_tts("mock", api_key=None, model=extra.get("default_model"),
                                 voice_id=extra.get("default_voice_id"),
                                 output_format=extra.get("default_output_format"))
            handle = await TTSServiceFactory(self.session).build(
                provider.workspace_id,
                provider.slug,
                model=(provider.extra_config or {}).get("default_model"),
                voice_id=(provider.extra_config or {}).get("default_voice_id"),
                output_format=(provider.extra_config or {}).get("default_output_format"),
            )
            return handle.provider
        if kind == "image":
            extra = provider.extra_config or {}
            return build_image_provider(
                provider.slug,
                key,
                provider.base_url,
                options={k: str(v) for k, v in extra.items() if k in
                         ("size", "quality", "background", "image_detail", "output_format")},
                label=provider.name,
            )
        if kind == "youtube":
            return build_youtube_service(provider.slug)
        raise ProviderNotConfiguredError(f"No adapter for provider kind '{kind}'")

    async def resolve_for_agent(
        self, *, workspace_id: uuid.UUID, agent_key: str, channel: Channel | None = None
    ) -> ResolvedAgent:
        spec = REGISTRY.get(agent_key)
        agent_row = (
            await self.session.execute(select(Agent).where(Agent.key == agent_key))
        ).scalar_one_or_none()
        configs: list[AgentConfiguration] = []
        if agent_row is not None:
            result = await self.session.execute(
                select(AgentConfiguration).where(
                    AgentConfiguration.agent_id == agent_row.id,
                    AgentConfiguration.channel_id.in_(
                        [c for c in [channel.id if channel else None] if c][:1] or [None]
                    ),
                )
            )
            configs = list(result.scalars())
            if not configs:
                result = await self.session.execute(
                    select(AgentConfiguration).where(
                        AgentConfiguration.agent_id == agent_row.id,
                        AgentConfiguration.channel_id.is_(None),
                    )
                )
                configs = list(result.scalars())
        config = configs[0] if configs else None

        provider: AiProvider | None = None
        if config and config.provider_id:
            bound = await self.session.get(AiProvider, config.provider_id)
            # A disabled provider must not be used; fall through to the normal selection order.
            if bound is not None and bound.is_enabled:
                provider = bound
        if provider is None and spec.port == "tts":
            # Voice provider precedence: global TTS configuration → first usable provider.
            global_cfg = await TTSConfigurationService(self.session).global_config(workspace_id)
            if global_cfg is not None:
                provider = await self.session.scalar(
                    select(AiProvider).where(
                        AiProvider.workspace_id == workspace_id,
                        AiProvider.kind == "tts",
                        AiProvider.slug == global_cfg.provider_slug,
                        AiProvider.is_enabled.is_(True),
                    )
                )
        if provider is None:
            wanted = "youtube" if spec.port == "youtube" else spec.port
            result = await self.session.execute(
                select(AiProvider)
                .where(
                    AiProvider.workspace_id == workspace_id,
                    AiProvider.kind == wanted,
                    AiProvider.is_enabled.is_(True),
                )
                # Real providers first, then the operator's priority (lower wins), then a stable
                # tiebreak so the choice is deterministic across runs.
                .order_by(
                    AiProvider.is_mock.asc(),
                    AiProvider.priority.asc(),
                    AiProvider.created_at.asc(),
                    AiProvider.slug.asc(),
                )
            )
            candidates = list(result.scalars())
            real = [c for c in candidates if not c.is_mock and c.credential_ciphertext]
            # Mock adapters are only used when an operator adds one explicitly and binds it to the
            # agent — never as an automatic fallback, which would quietly produce placeholder work.
            mocks = [c for c in candidates if c.is_mock and _bound_to(config, c)]
            provider = real[0] if real else (mocks[0] if mocks else None)

        # The configured model only applies to the provider it was configured for. When selection
        # falls through to a different provider (bound one disabled, none bound), that provider's
        # own first model wins — otherwise a gateway receives another vendor's model id and 404s.
        bound_model = (
            config.model
            if config and config.model and config.provider_id and provider
            and str(config.provider_id) == str(provider.id)
            else None
        )
        model = bound_model or (provider and _first_model(provider)) or ""
        return ResolvedAgent(
            key=agent_key,
            spec=spec,
            provider=provider,
            config=config,
            model=model,
            temperature=float(config.temperature) if config and config.temperature is not None else 0.7,
            max_tokens=int(config.max_tokens) if config and config.max_tokens else 4096,
            system_prompt=config.system_prompt if config else None,
            params=dict(config.params or {}) if config else {},
            enabled=bool(config.is_enabled) if config else True,
            retry_policy=dict(config.retry_policy or {}) if config else {},
        )

    async def build_context(
        self,
        *,
        workspace_id: uuid.UUID,
        agent_key: str,
        channel: Channel | None = None,
        project_id: uuid.UUID | None = None,
        workflow_id: uuid.UUID | None = None,
        request_key: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> AgentContext:
        resolved = await self.resolve_for_agent(
            workspace_id=workspace_id, agent_key=agent_key, channel=channel
        )
        spec = resolved.spec
        port_name = spec.port
        ctx = AgentContext(
            workspace_id=workspace_id,
            project_id=project_id,
            workflow_id=workflow_id,
            channel_id=channel.id if channel else None,
            provider_id=resolved.provider.id if resolved.provider else None,
            model=resolved.model,
            temperature=resolved.temperature,
            max_tokens=resolved.max_tokens,
            system_prompt=resolved.system_prompt,
            params={**resolved.params, **(extra_params or {})},
            request_key=request_key or uuid.uuid4().hex,
        )
        needs_external = port_name in ("llm", "tts", "image", "youtube")
        if needs_external and (resolved.provider is None or not resolved.enabled):
            raise ProviderNotConfiguredError(
                f"No enabled {port_name.upper()} provider is configured for agent '{agent_key}'. "
                "Add one under Providers."
            )
        if resolved.provider is not None and needs_external:
            try:
                adapter = await self.build(resolved.provider)
            except Exception as exc:  # noqa: BLE001 - surfaced as configuration error
                raise ProviderNotConfiguredError(
                    f"Provider '{resolved.provider.name}' is not usable: {exc}"
                ) from exc
            if port_name == "llm":
                ctx.llm = adapter
            elif port_name == "tts":
                ctx.tts = adapter
                # Resolution order: channel → workspace → provider default (see TTSConfigurationService).
                # ``tts_config`` is the single source of truth; the flat key is kept for agents that
                # read it directly.
                try:
                    resolved_tts = await TTSConfigurationService(self.session).resolve(
                        workspace_id=workspace_id, channel=channel
                    )
                    ctx.params["tts_config"] = resolved_tts.as_dict()
                    ctx.params["tts_credential_version"] = int(
                        resolved.provider.credential_version or 0
                    )
                    if resolved_tts.voice_id and not ctx.params.get("default_voice_id"):
                        ctx.params["default_voice_id"] = resolved_tts.voice_id
                except Exception as exc:  # noqa: BLE001 - surfaced when the agent runs
                    ctx.params.setdefault(
                        "default_voice_id", await _default_voice(self.session, channel)
                    )
                    ctx.params["tts_resolution_error"] = str(exc)
            elif port_name == "image":
                ctx.image = adapter
            elif port_name == "youtube":
                ctx.youtube = adapter

        if port_name in ("image", "tts", "video", "youtube"):
            ctx.storage = get_storage()
        return ctx


def _first_model(provider: AiProvider) -> str:
    models = provider.models or []
    if models and isinstance(models[0], dict):
        return models[0].get("id", "")
    if models:
        return str(models[0])
    return DEFAULT_MODELS.get(provider.slug, {}).get(provider.kind, "")


def _bound_to(config: AgentConfiguration | None, provider: AiProvider) -> bool:
    """True when the agent's configuration explicitly names this provider."""
    return bool(config and config.provider_id and str(config.provider_id) == str(provider.id))


async def _default_voice(session: AsyncSession, channel: Channel | None) -> str:
    if channel is not None:
        setting = (
            await session.execute(select(ChannelSetting).where(ChannelSetting.channel_id == channel.id))
        ).scalar_one_or_none()
        if setting and setting.voice_id:
            return setting.voice_id
    return ""


async def load_channel(session: AsyncSession, channel_id: uuid.UUID) -> Channel:
    channel = await session.get(Channel, channel_id)
    if channel is None or channel.deleted_at is not None:
        raise NotFoundError("Channel not found")
    return channel
