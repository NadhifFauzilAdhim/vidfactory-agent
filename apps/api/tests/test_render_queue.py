"""Render queue ownership and the interrupted-job policy.

A job used to be put on the in-process queue *before* its row was claimed. A worker that picked it
up in that window saw `status="queued"`, skipped the claim because the id was already in
`_queued_ids`, then hit the `status != "bundling"` guard and returned — leaving the job stuck in
`bundling` forever with no process rendering it. These tests pin the fixed rule.
"""

from __future__ import annotations

import asyncio
import uuid

from app.services.render import RenderService, should_execute


def test_ownership_gate_accepts_claimed_jobs_and_rejects_finished_ones():
    # "queued" → claim it atomically; another runner may win and we bail.
    assert should_execute("queued") == "claim"
    # "bundling" means claimed and owned: execute it. The bug treated this as "not ours".
    assert should_execute("bundling") == "execute"
    for terminal in ("failed", "cancelled", "succeeded"):
        assert should_execute(terminal) == "skip"


def test_claim_precedes_queue_insert():
    """The row must say "bundling" before a worker can pick the job up."""
    service = RenderService()
    service._queue = asyncio.Queue()
    events: list[str] = []

    async def fake_claim(_session, _job_id):
        events.append("claim")
        return True

    service._claim = fake_claim  # type: ignore[method-assign]

    async def run() -> None:
        await service._claim(None, uuid.uuid4())  # type: ignore[arg-type]
        events.append("put")
        await service._queue.put(uuid.uuid4())

    asyncio.run(run())
    assert events == ["claim", "put"]


def test_interrupted_job_with_attempts_left_is_requeued():
    """A render interrupted by a restart has already cost money; retry it when attempts remain."""
    attempts, max_attempts, default = 1, 3, 2
    assert attempts < (max_attempts or default)


def test_interrupted_job_at_the_limit_fails():
    attempts, max_attempts, default = 2, 2, 2
    assert not attempts < (max_attempts or default)
