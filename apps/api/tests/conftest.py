"""Pytest fixtures: isolated SQLite DB, seeded workspace, authenticated client."""

from __future__ import annotations

import asyncio
import os
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

TMP = Path(tempfile.mkdtemp(prefix="factory-tests-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TMP / 'test.db'}"
os.environ["STORAGE_ROOT"] = str(TMP / "storage")
os.environ["STORAGE_BACKEND"] = "local"
os.environ["SCHEDULER_ENABLED"] = "false"
# The workflow test starts a real uvicorn server for long background work (renders, workflows); the
# API process must not also run the render queue or two loops would share one engine.
os.environ["RENDER_IN_API"] = "false"
# The worker loop must not tick the production scheduler while a test drives workflows by hand.
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["JWT_SECRET"] = "test-secret-that-is-long-enough-for-hs256"
os.environ["BOOTSTRAP_ADMIN_EMAIL"] = "owner@factory.app"
os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "test-password-123"
from app.core.crypto import CredentialVault  # noqa: E402

os.environ.setdefault("CREDENTIAL_KEY", CredentialVault.generate_key())

from app.core.settings import get_settings  # noqa: E402

get_settings.cache_clear()

from fastapi.testclient import TestClient  # noqa: E402

from app.cli import bootstrap  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def app():
    asyncio.run(bootstrap(if_empty=False))
    return create_app()


@pytest.fixture(scope="session")
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def auth(client):
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@factory.app", "password": "test-password-123"},
    )
    assert resp.status_code == 200, resp.text
    session = resp.json()
    # A fresh workspace has no channel: bootstrap seeds none, so tests create one the way an
    # operator would rather than relying on demo content.
    created = client.post(
        "/api/v1/channels",
        json={"name": "Test Channel", "handle": "fixture", "niche": "kids-stories"},
    )
    assert created.status_code == 201, created.text
    return {"headers": {}, "session": session, "channel": created.json()}


@pytest.fixture(scope="session", autouse=True)
def _dispose():
    yield
    asyncio.run(engine.dispose())


@pytest.fixture(scope="session")
def remotion_ready() -> bool:
    """True when the Remotion engine (Node + installed packages) can actually render."""
    from app.services.render import engine_status

    return bool(engine_status()["ok"])


@contextmanager
def _live_server(app, cookies: dict[str, str] | None = None):
    """Run the API under uvicorn on its own event loop, against the same test database.

    Long background work (workflows, Remotion renders) must not share the loop a test blocks in.
    """
    import uvicorn

    settings = get_settings()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    )
    # The server's lifespan runs the workflow runner and (optionally) the render queue. Those tasks
    # are bound to its loop; when it exits it disposes the engine, so no other loop may use it.
    thread = threading.Thread(target=server.run, name="test-live-server", daemon=True)
    thread.start()

    # Background work needs both loops: the API runs workflows, the worker runs the render queue.
    from app.worker import main as worker_main

    def run_worker() -> None:
        asyncio.run(worker_main([]))

    worker_thread = threading.Thread(
        target=run_worker, name="test-worker", daemon=True
    )
    worker_thread.start()
    base_url = f"http://127.0.0.1:{port}"
    api = httpx.Client(
        base_url=base_url,
        headers={"Origin": settings.web_origin},
        timeout=60.0,
    )
    # Reuse the TestClient's session: the access token cookie is scoped to the client's host.
    if cookies:
        api.cookies.update(cookies)
    deadline = time.time() + 120
    ready = False
    while time.time() < deadline and thread.is_alive() and not server.should_exit:
        try:
            if api.get("/api/v1/health").status_code < 500:
                ready = True
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    assert ready, "live test server did not become healthy"
    try:
        yield api
    finally:
        api.close()
        # uvicorn runs the lifespan shutdown, stopping the runner and background tasks.
        server.should_exit = True
        thread.join(timeout=60)
        if thread.is_alive():
            raise RuntimeError("live test server did not stop")


@pytest.fixture(scope="session")
def live_server(app, auth):
    # Log in against the live server itself: the session cookie is host-scoped (TestClient's
    # "testserver" cookie would not be sent to 127.0.0.1).
    with _live_server(app, None) as api:
        login = api.post(
            "/api/v1/auth/login",
            json={"email": "owner@factory.app", "password": "test-password-123"},
        )
        assert login.status_code == 200, login.text
        yield api
