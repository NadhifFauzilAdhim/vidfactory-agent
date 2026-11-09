"""Integration tests over the real API: auth, RBAC, channels, workflows, approvals, costs."""

from __future__ import annotations

import time


def test_health_endpoints(client):
    assert client.get("/api/v1/healthz").json()["status"] == "ok"
    assert client.get("/api/v1/readyz").json()["ready"] is True


def test_requires_authentication(client):
    resp = client.get("/api/v1/overview")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


def test_login_rejects_bad_password(client):
    resp = client.post("/api/v1/auth/login", json={"email": "owner@factory.app", "password": "nope"})
    assert resp.status_code == 401


def test_seeded_agents_and_providers(client, auth):
    agents = client.get("/api/v1/agents").json()
    assert len(agents) == 10
    assert {a["key"] for a in agents} >= {"content_planner", "quality_control", "publishing"}
    providers = client.get("/api/v1/providers").json()
    kinds = {p["kind"] for p in providers}
    assert {"llm", "tts", "image", "youtube"} <= kinds
    # A fresh install is clean: the catalogue lists real adapters only, so nothing can produce
    # placeholder content before an operator adds a key.
    assert not [p for p in providers if p["is_mock"]], providers
    assert all("credential_ciphertext" not in p and "api_key" not in p for p in providers)


def test_channel_crud_and_settings(client, auth):
    resp = client.post(
        "/api/v1/channels",
        json={"name": "Test Channel", "handle": "testch", "niche": "kids-stories"},
    )
    assert resp.status_code == 201, resp.text
    channel = resp.json()
    assert channel["settings"]["approval_mode"] == "always"
    patched = client.patch(
        f"/api/v1/channels/{channel['id']}",
        json={"audience": "Ages 4-6", "settings": {**channel["settings"], "made_for_kids": True,
                                                    "min_quality_score": 90}},
    )
    assert patched.status_code == 200
    assert patched.json()["audience"] == "Ages 4-6"
    assert patched.json()["settings"]["min_quality_score"] == 90


def test_provider_credential_is_encrypted_and_never_returned(client, auth):
    resp = client.post(
        "/api/v1/providers",
        json={
            "kind": "llm",
            "slug": "openai",
            "name": "OpenAI test",
            "vendor": "OpenAI",
            "api_key": "sk-test-secret-value-123456",
            "models": [{"id": "gpt-4.1-mini"}],
        },
    )
    if resp.status_code == 409:  # already created by another test run
        return
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["has_credential"] is True
    assert "sk-test-secret" not in resp.text
    assert body["credential_fingerprint"].endswith("3456")


def test_unknown_provider_adapter_rejected(client, auth):
    resp = client.post(
        "/api/v1/providers",
        json={"kind": "llm", "slug": "not-a-provider", "name": "X", "vendor": "X"},
    )
    assert resp.status_code == 422


def test_overview_metrics_are_real(client, auth):
    data = client.get("/api/v1/overview").json()
    assert data["totals"]["channels"]["value"] >= 1
    assert isinstance(data["production_activity"], list)
    assert {"agent_activity", "providers", "system"} <= set(data)


def test_schedule_crud(client, auth):
    channel_id = auth["channel"]["id"]
    resp = client.post(
        f"/api/v1/channels/{channel_id}/schedules",
        json={"name": "Evening run", "timezone": "Europe/Amsterdam", "production_count": 2},
    )
    assert resp.status_code == 201, resp.text
    schedule = resp.json()
    assert schedule["next_run_at"] is not None
    bad = client.post(
        f"/api/v1/channels/{channel_id}/schedules",
        json={"timezone": "Mars/Olympus"},
    )
    assert bad.status_code == 409
    assert client.delete(f"/api/v1/schedules/{schedule['id']}").status_code == 204


def _add_mock_providers(client) -> None:
    """Bind mock adapters explicitly, the way an operator would for a dry run.

    Bootstrap seeds real adapters only, and a mock is never an implicit fallback, so a workflow
    test has to add one *and* bind it to the agent — which is exactly the behaviour being asserted.
    """
    providers = {p["kind"]: p for p in client.get("/api/v1/providers").json() if p["kind"] in
                 ("llm", "tts", "image")}
    for kind in ("llm", "tts", "image"):
        created = client.post(
            "/api/v1/providers",
            json={"kind": kind, "slug": "mock", "name": f"Mock {kind}", "vendor": "Factory"},
        )
        assert created.status_code == 201, created.text
        providers[kind] = created.json()
    # Every agent that talks to an external provider is bound to the mock for that kind.
    for agent in client.get("/api/v1/agents").json():
        provider = providers.get(agent["port"])
        if provider is None:
            continue
        bound = client.put(
            f"/api/v1/agents/{agent['key']}/config", json={"provider_id": provider["id"]}
        )
        assert bound.status_code == 200, bound.text


def _wait_for_workflow(api, workflow_id: str, timeout: float) -> dict:
    """Poll a workflow by *file* state only — the workflow runs on a different loop (a live server),
    so a blocking sleep here is safe and never freezes it."""
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        resp = api.get(f"/api/v1/workflows/{workflow_id}")
        detail = resp.json()
        if resp.status_code != 200 or "status" not in detail:
            raise AssertionError(f"workflow lookup failed: {resp.status_code} {resp.text[:400]}")
        if detail["status"] in ("awaiting_approval", "failed", "published"):
            return detail
        time.sleep(1.5)
    return detail


def test_workflow_run_and_approval_gate(live_server, client, auth, remotion_ready):
    channel_id = auth["channel"]["id"]
    _add_mock_providers(client)
    start = client.post(f"/api/v1/workflows?channel_id={channel_id}&force_approval=true")
    assert start.status_code == 201, start.text
    workflow_id = start.json()["id"]

    # Workflows and renders run as background tasks on the server's event loop. With TestClient that
    # loop is the same one the test blocks in, so the long stages would be frozen; use a real server
    # on its own loop instead (same database, same storage).
    detail = _wait_for_workflow(live_server, workflow_id, 600 if remotion_ready else 120)

    # Without the Remotion engine the render stage still queues a durable job — that is the contract
    # we can observe on a machine without Node/Chromium.
    if not remotion_ready:
        assert detail["stage"] in ("rendering", "awaiting_approval"), detail.get("error_message")
        return

    assert detail["status"] == "awaiting_approval", (
        f"stage={detail.get('stage')} status={detail.get('status')} "
        f"error={detail.get('error_message')} "
        f"tasks={[(t['agent_key'], t['status']) for t in detail.get('tasks', [])]} "
        f"logs={[str(row.get('message')) for row in detail.get('logs', [])][-6:]}"
    )
    assert detail["stage"] == "awaiting_approval"
    assert detail["project_id"]
    assert any(t["agent_key"] == "quality_control" for t in detail["tasks"])

    project = client.get(f"/api/v1/content/{detail['project_id']}").json()
    assert project["qc"] is not None
    assert project["scenes"] and project["scenes"][0]["image_url"]
    assert project["renders"] and project["renders"][0]["status"] == "ready"
    assert project["metadata"]["seo"]["title"]

    jobs = client.get(f"/api/v1/video/projects/{detail['project_id']}/renders").json()
    assert jobs and jobs[0]["status"] == "succeeded"
    validation = client.get(f"/api/v1/video/renders/{jobs[0]['id']}/validation").json()
    assert validation["available"] and validation["passed"], validation
    # The renderer inspects the encoded file: dimensions, frame rate, duration, audio and A/V sync.
    ids = {check["id"] for check in validation["checks"]}
    assert {"dimensions", "frame_rate", "duration", "audio_stream"} <= ids
    # QC consumes that inspection as its media check.
    media = next(c for c in project["qc"]["checks"] if c["id"] == "media_integrity")
    assert media["passed"]

    approvals = client.get("/api/v1/approvals").json()
    approval = next(a for a in approvals if a["project_id"] == detail["project_id"])
    decided = client.post(
        f"/api/v1/approvals/{approval['id']}/decision",
        json={"decision": "approve", "note": "looks good"},
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "approved"


def test_cost_dashboard_returns_ledger_shape(client, auth):
    data = client.get("/api/v1/costs").json()
    assert "month_usd" in data and "daily" in data
    assert len(data["daily"]) == 14


def test_analytics_reports_setup_state_when_not_configured(client, auth):
    data = client.get("/api/v1/analytics").json()
    assert data["configured"] is False
    assert "OAuth" in data["detail"] or "snapshots" in data["detail"]


def test_system_status_lists_services(client, auth):
    data = client.get("/api/v1/system/status").json()
    names = {s["name"] for s in data["services"]}
    assert {"api", "database", "ffmpeg", "youtube"} <= names
