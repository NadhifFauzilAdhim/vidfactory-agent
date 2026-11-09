"""Tests for the unified TTS services: chunking, configuration inheritance, fallback,
generation records, usage accounting and the Studio endpoints.
"""

from __future__ import annotations

import asyncio
import io
import wave

import httpx
import pytest
from sqlalchemy import select
from test_credentials_tts_providers import FakeTransport

from app.adapters import tts_cartesia, tts_elevenlabs
from app.core.crypto import get_vault
from app.models import AiProvider, Workspace
from app.ports.tts import TTSError
from app.services import tts as tts_service
from app.services.chunking import split_text
from app.services.credentials import invalidate_all

# --------------------------------------------------------------------------- chunking


def test_short_text_is_one_chunk():
    chunks = split_text("A tiny line.", 100)
    assert len(chunks) == 1
    assert chunks[0].text == "A tiny line."


def test_long_text_splits_on_sentence_boundaries():
    text = " ".join(f"Sentence number {i} is here." for i in range(60))
    chunks = split_text(text, 120)
    assert len(chunks) > 1
    assert all(len(c.text) <= 120 for c in chunks)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert all(c.text.rstrip().endswith(".") for c in chunks)
    assert " ".join(c.text for c in chunks).split() == text.split()


def test_single_huge_sentence_falls_back_to_clauses_then_words():
    sentence = ("This clause is long, " * 40) + "and it ends here."
    chunks = split_text(sentence, 200)
    assert all(len(c.text) <= 200 for c in chunks)
    assert chunks[0].text.endswith(",")


def test_paragraphs_are_respected():
    text = ("First paragraph sentence one. Sentence two.\n\n"
            "Second paragraph sentence one. Sentence two.")
    assert len(split_text(text, 60)) >= 2


def test_chunk_limit_validation():
    from app.ports.tts import TTSInvalidRequestError

    with pytest.raises(TTSInvalidRequestError):
        split_text("x", 10)


# --------------------------------------------------------------------------- helpers


def _wav(seconds: float = 0.25) -> bytes:
    buffer = io.BytesIO()
    rate = 22050
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


def _db():
    from app.db.session import SessionFactory

    return SessionFactory()


async def _workspace_id(session):  # type: ignore[no-untyped-def]
    return (await session.execute(select(Workspace.id).limit(1))).scalar_one()


async def _attach_fake_keys(session, ws, slugs=("cartesia", "elevenlabs")):  # type: ignore[no-untyped-def]
    """Seal a placeholder key on the provider rows so adapters can be constructed."""
    vault = get_vault()
    for slug in slugs:
        row = await session.scalar(
            select(AiProvider).where(
                AiProvider.workspace_id == ws, AiProvider.kind == "tts", AiProvider.slug == slug
            )
        )
        if row is None:
            continue
        row.credential_ciphertext = vault.seal(
            {"api_key": "sk_car_" + "x" * 24}, aad=f"provider:{ws}:{row.id}"
        )
        row.credential_status = "active"
        row.credential_version = int(row.credential_version or 0) + 1
    await session.flush()
    invalidate_all()


@pytest.fixture()
def wire_patch(monkeypatch):
    """Route every httpx client the TTS adapters create through a fake transport."""
    state: dict = {}

    def install(transport):  # type: ignore[no-untyped-def]
        state["transport"] = transport

    original = httpx.AsyncClient

    def factory(*args, **kwargs):  # type: ignore[no-untyped-def]
        if "transport" in state:
            kwargs["transport"] = state["transport"]
        return original(*args, **kwargs)

    monkeypatch.setattr(tts_cartesia.httpx, "AsyncClient", factory)
    monkeypatch.setattr(tts_elevenlabs.httpx, "AsyncClient", factory)
    monkeypatch.setattr("app.adapters.tts_http.asyncio.sleep", _noop)
    yield install
    invalidate_all()


async def _noop(*_args, **_kwargs):
    return None


# --------------------------------------------------------------------------- inheritance


def test_configuration_inheritance_precedence(app):
    async def scenario():
        async with _db() as session:
            from app.models import Channel, ChannelSetting

            ws = await _workspace_id(session)
            service = tts_service.TTSConfigurationService(session)
            await service.set_global_config(
                ws, provider="mock", model="mock-tts", voice_id="global-voice",
                language="en", output_format="wav/22050/pcm_s16le",
                voice_settings={"speed": 1.0},
            )
            channel = Channel(workspace_id=ws, name="Inherit", handle="inh", language="de")
            session.add(channel)
            await session.flush()
            session.add(ChannelSetting(
                channel_id=channel.id,
                voice_provider="elevenlabs",
                voice_id="channel-voice",
                voice_model="eleven_v4",
                voice_config={"stability": 0.4},
            ))
            await session.flush()

            resolved = await service.resolve(
                workspace_id=ws,
                channel=channel,
                workflow_override=tts_service.TTSOverride(voice_id="workflow-voice"),
                scene_override=tts_service.TTSOverride(voice_settings={"speed": 1.2}),
            )
            assert resolved.provider == "elevenlabs"
            assert resolved.model == "eleven_v4"
            assert resolved.voice_id == "workflow-voice"  # most specific wins
            assert resolved.sources["provider"] == "channel"
            assert resolved.sources["model"] == "channel"
            assert resolved.language == "en"  # global language; channel override has none
            assert resolved.voice_settings["stability"] == 0.4
            assert resolved.voice_settings["speed"] == 1.2

            # A provider equal to the field default (here "mock") is still a real override and
            # must not be dropped by exclude_defaults semantics.
            global_only = await service.resolve(workspace_id=ws)
            assert global_only.provider == "mock"
            assert global_only.voice_id == "global-voice"
            assert global_only.sources["voice_id"] == "global"
            await session.rollback()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- fallback


def test_no_fallback_on_configuration_error(app, wire_patch):
    wire_patch(FakeTransport(lambda request, body: httpx.Response(
        401, json={"message": "bad key"})))

    async def scenario():
        async with _db() as session:
            ws = await _workspace_id(session)
            await _attach_fake_keys(session, ws)
            service = tts_service.TTSGenerationService(session)
            config = tts_service.ResolvedTTSConfig(
                provider="cartesia", fallback_provider="elevenlabs", model="sonic-3.6",
                voice_id="voice-1", language="en", output_format="wav/44100/pcm_s16le",
                voice_settings={}, fallback_on_transient=True,
            )
            with pytest.raises(TTSError) as exc:
                await service.generate(workspace_id=ws, text="Hello there.", config=config)
            assert exc.value.code == "invalid_credentials"
            await session.rollback()

    asyncio.run(scenario())


def test_fallback_on_transient_uses_second_provider(app, wire_patch):
    state = {"cartesia": 0, "eleven": 0}

    def handler(request, body):
        if request.url.host == "api.cartesia.ai":
            state["cartesia"] += 1
            return httpx.Response(503, json={"message": "upstream down"})
        state["eleven"] += 1
        return httpx.Response(200, content=_wav(), headers={"content-type": "audio/wav"})

    wire_patch(FakeTransport(handler))

    async def scenario():
        async with _db() as session:
            ws = await _workspace_id(session)
            await _attach_fake_keys(session, ws)
            service = tts_service.TTSGenerationService(session)
            config = tts_service.ResolvedTTSConfig(
                provider="cartesia", fallback_provider="elevenlabs", model="sonic-3.6",
                voice_id="voice-1", language="en", output_format="wav/44100/pcm_s16le",
                voice_settings={}, fallback_on_transient=True,
            )
            generated = await service.generate(
                workspace_id=ws, text="Fallback please. It works.", config=config)
            assert generated.provider == "elevenlabs"
            assert generated.fallback_used is True
            assert generated.fallback_reason
            assert generated.result.voice_id  # fallback uses the provider default voice
            await session.rollback()

    asyncio.run(scenario())
    assert state["cartesia"] >= 3  # retried before falling back
    assert state["eleven"] >= 1


def test_fallback_can_be_disabled(app, wire_patch):
    wire_patch(FakeTransport(lambda request, body: httpx.Response(503, json={})))

    async def scenario():
        async with _db() as session:
            ws = await _workspace_id(session)
            await _attach_fake_keys(session, ws)
            service = tts_service.TTSGenerationService(session)
            config = tts_service.ResolvedTTSConfig(
                provider="cartesia", fallback_provider="elevenlabs", model="sonic-3.6",
                voice_id="voice-1", language="en", output_format="wav/44100/pcm_s16le",
                voice_settings={}, fallback_on_transient=False,
            )
            with pytest.raises(TTSError):
                await service.generate(workspace_id=ws, text="No fallback.", config=config)
            await session.rollback()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- studio endpoints


def test_generation_is_persisted_with_usage_and_history(client, auth):
    resp = client.post("/api/v1/tts/generate", json={
        "text": "The little lighthouse blinked twice. Then the harbour was quiet.",
        "provider": "mock", "voice_id": "mock-narrator", "output_format": "wav/22050/pcm_s16le",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["provider"] == "mock"
    assert body["audio_url"].startswith("/files/tts/")
    assert body["characters"] > 0
    assert body["chunk_count"] >= 1
    generation_id = body["id"]

    audio = client.get(f"/api/v1/files/{body['audio_url'][len('/files/'):]}")
    assert audio.status_code == 200
    assert audio.content[:4] == b"RIFF"

    detail = client.get(f"/api/v1/tts/generations/{generation_id}").json()
    assert detail["mode"] == "single"
    assert detail["is_test"] is True

    history = client.get("/api/v1/tts/generations", params={"provider": "mock"}).json()
    assert history["total"] >= 1
    assert any(item["id"] == generation_id for item in history["items"])

    usage = client.get("/api/v1/tts/usage").json()
    assert usage["totals"]["generations"] >= 1
    assert "mock" in usage["by_provider"]


def test_failed_generation_is_recorded(client, auth):
    resp = client.post("/api/v1/tts/generate", json={
        "text": "This will fail because no key is stored for Cartesia.",
        "provider": "cartesia", "voice_id": "voice-1",
    })
    assert resp.status_code in (409, 422, 502)
    history = client.get("/api/v1/tts/generations",
                         params={"status": "failed", "limit": 10}).json()
    assert history["total"] >= 1
    failed = history["items"][0]
    assert failed["status"] == "failed"
    assert failed["error_code"]


def test_provider_catalogue_has_no_secrets(client, auth):
    # Read before any test adds a mock: a fresh workspace lists real adapters only.
    body = client.get("/api/v1/tts/providers").json()
    assert {"cartesia", "elevenlabs"} <= {row["slug"] for row in body}
    import re

    text = client.get("/api/v1/tts/providers").text
    # Only the documented key *prefix hints* (e.g. sk_car_…) may appear, never a value.
    assert not re.search(r"sk_car_[A-Za-z0-9]{6,}|xi-[A-Za-z0-9]{16,}", text)


def test_resolved_config_endpoint_explains_sources(client, auth):
    saved = client.put("/api/v1/tts/settings", json={
        "provider": "mock", "model": "mock-tts", "voice_id": "mock-child", "language": "en",
        "output_format": "wav/22050/pcm_s16le",
    })
    assert saved.status_code == 200, saved.text
    resolved = client.get("/api/v1/tts/debug/resolved").json()
    assert resolved["provider"] == "mock"
    assert resolved["sources"]["provider"] in ("global", "provider")
    assert resolved["chain"]


def test_settings_are_audited(client, auth):
    resp = client.put("/api/v1/tts/settings", json={
        "provider": "mock", "fallback_provider": None, "model": "mock-tts",
    })
    assert resp.status_code == 200
    audit = client.get("/api/v1/workspace/audit", params={"limit": 20}).json()
    assert any(row["action"] == "tts.settings.updated" for row in audit)


def test_compare_runs_both_sides(client, auth):
    resp = client.post("/api/v1/tts/compare", json={
        "text": "Compare these two narrators.",
        "language": "en",
        "left": {"provider": "mock", "voice_id": "mock-narrator"},
        "right": {"provider": "mock", "voice_id": "mock-child"},
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["left"]["ok"] is True and body["right"]["ok"] is True
    assert body["left"]["audio_url"].startswith("/files/")
    assert "cheaper" in body["verdict"]


def test_channel_voice_override(client, auth):
    channel_id = auth["channel"]["id"]
    resp = client.put(f"/api/v1/tts/channels/{channel_id}", json={
        "provider": "mock", "voice_id": "mock-guest", "language": "en",
        "voice_settings": {"speed": 1.1},
    })
    assert resp.status_code == 200, resp.text
    override = resp.json()["override"]
    assert override["provider"] == "mock"
    assert override["voice_id"] == "mock-guest"
    assert resp.json()["resolved"]["voice_id"] == "mock-guest"
    cleared = client.put(f"/api/v1/tts/channels/{channel_id}", json={"clear": True}).json()
    assert cleared["override"] is None


def test_dialogue_endpoint_with_mock(client, auth):
    resp = client.post("/api/v1/tts/generate-dialogue", json={
        "provider": "mock",
        "lines": [{"text": "Hello there.", "voice_id": "mock-narrator"},
                  {"text": "Hello back.", "voice_id": "mock-child"}],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["mode"] == "dialogue"


def test_estimate_endpoint(client, auth):
    body = client.get("/api/v1/tts/estimate",
                      params={"text_length": 1000, "provider": "elevenlabs"}).json()
    assert body["characters"] == 1000
    assert body["estimated_usd"] >= 0
