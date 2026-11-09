"""CLI helpers: key generation, bootstrap, agent sync, scheduler tick.

Usage (from apps/api):
    python -m app.cli gen-key
    python -m app.cli bootstrap [--if-empty]
    python -m app.cli sync-agents
    python -m app.cli tick
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import func, select

from app.agents.registry import REGISTRY
from app.core.crypto import CredentialVault, master_key_status
from app.core.security import hash_password
from app.core.settings import get_settings
from app.db.session import engine, init_models, session_scope
from app.models import (
    Agent,
    AiProvider,
    User,
    Workspace,
    WorkspaceMember,
)

#: Provider catalogue seeded on a brand-new installation. Real adapters only: a fresh workspace
#: starts with unconfigured entries that ask for a key, never with mock adapters that would
#: silently produce placeholder content. Mock adapters stay available in code for tests and for an
#: operator who explicitly adds one from the providers screen.
DEFAULT_PROVIDERS = [
    {
        "kind": "llm",
        "slug": "openai",
        "name": "OpenAI",
        "vendor": "OpenAI",
        "is_mock": False,
        "models": [
            {"id": "gpt-4.1-mini", "name": "GPT-4.1 mini", "input_cost_per_million": 0.40,
             "output_cost_per_million": 1.60},
            {"id": "gpt-4.1", "name": "GPT-4.1", "input_cost_per_million": 2.00,
             "output_cost_per_million": 8.00},
        ],
    },
    {
        "kind": "llm",
        "slug": "gemini",
        "name": "Google Gemini",
        "vendor": "Google",
        "is_mock": False,
        "models": [
            {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash", "input_cost_per_million": 0.30,
             "output_cost_per_million": 2.50},
            {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro", "input_cost_per_million": 1.25,
             "output_cost_per_million": 10.00},
        ],
    },
    {
        "kind": "tts",
        "slug": "cartesia",
        "name": "Cartesia",
        "vendor": "Cartesia",
        "is_mock": False,
        "base_url": "https://api.cartesia.ai",
        "extra_config": {
            "default_model": "sonic-3.6",
            "default_voice_id": "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4",
            "default_output_format": "wav/44100/pcm_s16le",
        },
        "models": [
            {"id": "sonic-3.6", "name": "Sonic 3.6", "unit_cost": 0.000065, "unit": "char"},
            {"id": "sonic-3.5", "name": "Sonic 3.5", "unit_cost": 0.000065, "unit": "char"},
            {"id": "sonic-3", "name": "Sonic 3", "unit_cost": 0.000065, "unit": "char"},
            {"id": "sonic-latest", "name": "Sonic latest", "unit_cost": 0.000065, "unit": "char"},
        ],
    },
    {
        "kind": "tts",
        "slug": "elevenlabs",
        "name": "ElevenLabs",
        "vendor": "ElevenLabs",
        "is_mock": False,
        "base_url": "https://api.elevenlabs.io",
        "extra_config": {
            "default_model": "eleven_multilingual_v2",
            "default_voice_id": "21m00Tcm4TlvDq8ikWAM",
            "default_output_format": "mp3_44100_128",
        },
        "models": [
            {"id": "eleven_multilingual_v2", "name": "Multilingual v2", "unit_cost": 0.00012,
             "unit": "char"},
            {"id": "eleven_v4", "name": "Eleven v4", "unit_cost": 0.00012, "unit": "char"},
            {"id": "eleven_v4_turbo", "name": "Eleven v4 Turbo", "unit_cost": 0.00006,
             "unit": "char"},
            {"id": "eleven_v3", "name": "Eleven v3", "unit_cost": 0.00012, "unit": "char"},
            {"id": "eleven_flash_v2_5", "name": "Flash v2.5", "unit_cost": 0.00006,
             "unit": "char"},
        ],
    },
    {
        "kind": "image",
        "slug": "openai",
        "name": "OpenAI Images",
        "vendor": "OpenAI",
        "is_mock": False,
        "models": [{"id": "gpt-image-1", "name": "GPT Image 1", "unit_cost": 0.040, "unit": "image"}],
    },
    {
        "kind": "image",
        "slug": "gemini",
        "name": "Gemini Images",
        "vendor": "Google",
        "is_mock": False,
        "models": [{"id": "gemini-2.5-flash-image", "name": "Gemini Flash Image", "unit_cost": 0.039,
                    "unit": "image"}],
    },
    {
        "kind": "youtube",
        "slug": "google",
        "name": "YouTube (Google)",
        "vendor": "Google",
        "is_mock": False,
        "models": [{"id": "v3", "name": "Data API v3"}],
    },
]


async def sync_agents() -> int:
    async with session_scope() as session:
        for spec in REGISTRY.all():
            agent = await session.scalar(select(Agent).where(Agent.key == spec.key))
            data = {
                "name": spec.name,
                "role": spec.role,
                "description": spec.description,
                "category": spec.category,
                "stage": spec.stage,
                "port": spec.port,
                "default_system_prompt": spec.system_prompt,
                "input_schema": spec.input_model.model_json_schema(),
                "output_schema": spec.output_model.model_json_schema(),
            }
            if agent is None:
                session.add(Agent(key=spec.key, **data))
            else:
                for key, value in data.items():
                    setattr(agent, key, value)
        return len(REGISTRY.all())


async def sync_providers(workspace_id) -> int:  # type: ignore[no-untyped-def]
    async with session_scope() as session:
        existing = {
            (p.kind, p.slug)
            for p in (
                await session.execute(select(AiProvider).where(AiProvider.workspace_id == workspace_id))
            ).scalars()
        }
        added = 0
        for spec in DEFAULT_PROVIDERS:
            if (spec["kind"], spec["slug"]) in existing:
                continue
            session.add(AiProvider(workspace_id=workspace_id, **spec))
            added += 1
        return added


async def refresh_tts_providers(workspace_id) -> int:  # type: ignore[no-untyped-def]
    """Keep the catalogue of TTS models current without touching user-chosen defaults.

    Model lists are code-owned (they mirror the providers' published catalogues); operator
    choices such as ``default_model`` / ``default_voice_id`` are preserved. Credentials are never
    read or modified here.
    """
    specs = {s["slug"]: s for s in DEFAULT_PROVIDERS if s["kind"] == "tts"}
    changed = 0
    async with session_scope() as session:
        rows = list((await session.execute(
            select(AiProvider).where(
                AiProvider.workspace_id == workspace_id, AiProvider.kind == "tts"
            )
        )).scalars())
        for row in rows:
            spec = specs.get(row.slug)
            if spec is None:
                continue
            if row.models != spec["models"]:
                row.models = spec["models"]
                changed += 1
            extra = dict(row.extra_config or {})
            defaults = spec.get("extra_config", {})
            for key, value in defaults.items():
                extra.setdefault(key, value)
            if row.slug in ("cartesia", "elevenlabs") and not extra.get("default_model"):
                extra["default_model"] = defaults.get("default_model")
            if extra != (row.extra_config or {}):
                row.extra_config = extra
                changed += 1
    return changed


async def bootstrap(if_empty: bool = False) -> None:
    settings = get_settings()
    await init_models()
    async with session_scope() as session:
        users = int(await session.scalar(select(func.count(User.id))) or 0)
        if users and if_empty:
            # Still ensure the code registry is materialized; the schema may be newer than the data.
            workspace_id = await session.scalar(select(Workspace.id).limit(1))
            print("bootstrap: existing data found, refreshing agent registry")
            await session.commit()
            await sync_agents()
            if workspace_id:
                # Only seed the catalogue when the operator has no providers yet. Re-seeding on every
                # start resurrected providers that were deliberately deleted (and re-created mocks).
                provider_count = int(
                    await session.scalar(
                        select(func.count(AiProvider.id)).where(
                            AiProvider.workspace_id == workspace_id
                        )
                    )
                    or 0
                )
                if provider_count == 0:
                    added = await sync_providers(workspace_id)
                    print(f"bootstrap: seeded {added} providers")
                await refresh_tts_providers(workspace_id)
            return
        admin = await session.scalar(select(User).where(User.email == settings.bootstrap_admin_email.lower()))
        if admin is None:
            admin = User(
                email=settings.bootstrap_admin_email.lower(),
                name=settings.bootstrap_admin_name,
                password_hash=hash_password(settings.bootstrap_admin_password),
            )
            session.add(admin)
            await session.flush()
        workspace = await session.scalar(select(Workspace).limit(1))
        if workspace is None:
            workspace = Workspace(name="Studio", slug="studio", plan="studio", monthly_budget_usd=250)
            session.add(workspace)
            await session.flush()
        member = await session.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.user_id == admin.id, WorkspaceMember.workspace_id == workspace.id
            )
        )
        if member is None:
            session.add(WorkspaceMember(user_id=admin.id, workspace_id=workspace.id, role="owner"))

        # No demo channel, characters or schedule. A fresh workspace starts empty so the first
        # thing an operator sees is onboarding, not someone else's story: sample content made it
        # look like real work was already set up.
        workspace_id = workspace.id
        print(f"bootstrap: admin={admin.email} workspace={workspace.slug} (no channel yet)")
    await sync_agents()
    await sync_providers(workspace_id)
    await refresh_tts_providers(workspace_id)
    print("bootstrap: agents and providers registered")


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="factory")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("gen-key", help="Generate a 32-byte AES-256-GCM master key for APP_ENCRYPTION_KEY")
    boot = sub.add_parser("bootstrap", help="Create tables, admin, workspace, demo channel")
    boot.add_argument("--if-empty", action="store_true")
    sub.add_parser("sync-agents", help="Register/refresh agents from the code registry")
    sub.add_parser("tick", help="Run one scheduler tick (materialize due schedules)")
    args = parser.parse_args(argv)

    if args.command == "gen-key":
        print(CredentialVault.generate_key())
        return 0
    if args.command == "bootstrap":
        ready, problem = master_key_status()
        if not ready:
            print(f"warning: {problem}")
        await bootstrap(args.if_empty)
        return 0
    if args.command == "sync-agents":
        await init_models()
        n = await sync_agents()
        print(f"synced {n} agents")
        return 0
    if args.command == "tick":
        from app.workflow.scheduler import scheduler_tick

        await init_models()
        created = await scheduler_tick()
        print(f"tick: {created} workflows created")
        return 0
    return 1


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1:]))
    finally:
        asyncio.run(engine.dispose())
