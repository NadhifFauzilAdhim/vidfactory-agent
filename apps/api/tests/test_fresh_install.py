"""A brand-new installation starts empty: no mock providers, no demo channel.

Seeding mock adapters meant a fresh workspace could run the whole pipeline and produce placeholder
content that looked like real work. Bootstrap now registers real adapters only (unconfigured, so
they ask for a key) and creates no channel — onboarding is the first screen, not someone's story.

These tests run the real bootstrap against a *separate* SQLite file in their own subprocess, so
they neither depend on nor disturb the shared test database.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

#: Run in a child process: a fresh database and a settings cache that has never seen another URL.
SCRIPT = textwrap.dedent(
    """
    import asyncio, json, os, sys
    from sqlalchemy import func, select

    os.environ["DATABASE_URL"] = sys.argv[1]
    os.environ["STORAGE_ROOT"] = sys.argv[2]
    os.environ["SCHEDULER_ENABLED"] = "false"
    os.environ["RENDER_IN_API"] = "false"

    from app.core.crypto import CredentialVault
    os.environ.setdefault("CREDENTIAL_KEY", CredentialVault.generate_key())
    from app.core.settings import get_settings
    get_settings.cache_clear()

    from app.cli import bootstrap
    from app.container import ProviderFactory
    from app.db.session import SessionFactory
    from app.models import (
        AiProvider, Channel, ChannelCharacter, Schedule, User, Workspace,
    )

    async def main():
        await bootstrap(if_empty=False)
        async with SessionFactory() as session:
            providers = list(
                (await session.execute(select(AiProvider))).scalars()
            )
            counts = {}
            for name, model in (
                ("channels", Channel), ("characters", ChannelCharacter),
                ("schedules", Schedule), ("users", User), ("workspaces", Workspace),
            ):
                counts[name] = int(await session.scalar(select(func.count(model.id))) or 0)

            # Only a mock exists, and nothing binds it: resolution must still find nothing.
            workspace_id = await session.scalar(select(AiProvider.workspace_id))
            session.add(AiProvider(
                workspace_id=workspace_id, kind="llm", slug="mock", name="Mock LLM",
                vendor="Factory", is_mock=True, is_enabled=True, health="ok", models=[],
            ))
            await session.commit()
            resolved = await ProviderFactory(session).resolve_for_agent(
                workspace_id=workspace_id, agent_key="content_planner", channel=None
            )

        print(json.dumps({
            "providers": sorted(
                [p.kind, p.slug, p.is_mock, bool(p.credential_ciphertext)] for p in providers
            ),
            "counts": counts,
            "unbound_mock_resolves_to": resolved.provider.slug if resolved.provider else None,
        }))

    asyncio.run(main())
    """
)


def _run_fresh(db_path: Path, storage: Path) -> dict:
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT, f"sqlite+aiosqlite:///{db_path}", str(storage)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_bootstrap_on_an_empty_database_is_clean(tmp_path):
    state = _run_fresh(tmp_path / "fresh.db", tmp_path / "storage")

    # Real adapters only, one set per kind, none of them a mock, none holding a key.
    assert state["providers"] == [
        ["image", "gemini", False, False],
        ["image", "openai", False, False],
        ["llm", "gemini", False, False],
        ["llm", "openai", False, False],
        ["tts", "cartesia", False, False],
        ["tts", "elevenlabs", False, False],
        ["youtube", "google", False, False],
    ]

    # The login the operator needs exists...
    assert state["counts"]["users"] >= 1
    assert state["counts"]["workspaces"] == 1
    # ...but no sample content was created.
    assert state["counts"]["channels"] == 0
    assert state["counts"]["characters"] == 0
    assert state["counts"]["schedules"] == 0

    # A mock provider nobody bound is never picked automatically.
    assert state["unbound_mock_resolves_to"] is None
