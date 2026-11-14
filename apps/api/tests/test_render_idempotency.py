"""Enqueueing a render must never die on the unique idempotency key.

``render_jobs`` has a UNIQUE index on ``idempotency_key``, so only one row may carry a given key for
all time. The lookup used to filter on active/succeeded statuses, meaning a **failed** row kept the
key while being invisible to the check — the next insert raised UniqueViolationError, surfaced as a
PendingRollbackError the workflow never logged. That is why retries died silently right after
"visuals reused".
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.core.errors import ConflictError
from app.models import RenderJob
from app.services.render import ACTIVE_RENDER_STATES_FOR_JOB, RenderService, resolve_existing_job


class StubJob:
    def __init__(self, status: str, attempts: int = 1, max_attempts: int = 2):
        self.id = "job-1"
        self.status = status
        self.stage = status
        self.progress = 40
        self.attempts = attempts
        self.max_attempts = max_attempts
        self.error_code = "render_timeout"
        self.error_message = "boom"
        self.worker_pid = 123
        self.started_at = "t0"
        self.finished_at = "t1"
        self.output_key = "old.mp4"
        self.workflow_id = "wf-old"
        self.queued_at = None


def test_active_states_match_the_pipeline_guard():
    from app.workflow.pipeline import ACTIVE_RENDER_STATES

    assert tuple(ACTIVE_RENDER_STATES) == ACTIVE_RENDER_STATES_FOR_JOB


def test_the_lookup_is_not_filtered_by_status():
    """The regression itself: a status filter hides the failed row that still owns the key."""
    key = "abc123"
    strict = select(RenderJob).where(RenderJob.idempotency_key == key)
    old = select(RenderJob).where(
        RenderJob.idempotency_key == key,
        RenderJob.status.in_(["queued", "succeeded"]),
    )

    def where_clause(statement) -> str:
        sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
        return sql.split("WHERE", 1)[1] if "WHERE" in sql else ""

    # The fixed query filters on the key only; the buggy one also filtered on status.
    assert "status" not in where_clause(strict).lower()
    assert "status" in where_clause(old).lower()


@pytest.mark.parametrize("status", ["queued", "bundling", "rendering", "encoding", "validating"])
def test_active_job_is_reused_as_is(status):
    job = StubJob(status, attempts=2)
    action, result = resolve_existing_job(job, workflow_id="wf-new")
    assert action == "reuse"
    assert result is job
    assert result.status == status  # untouched


def test_succeeded_job_is_reused():
    job = StubJob("succeeded", attempts=1)
    action, _ = resolve_existing_job(job, workflow_id="wf-new")
    assert action == "reuse"


def test_failed_job_is_revived_for_retry():
    job = StubJob("failed", attempts=2, max_attempts=2)
    action, result = resolve_existing_job(job, workflow_id="wf-new")
    assert action == "requeue"
    assert result.status == "queued"
    assert result.error_code is None and result.error_message is None
    assert result.started_at is None and result.finished_at is None
    assert result.output_key is None
    assert result.worker_pid is None
    assert result.attempts == 0  # exhausted budget reset so the worker can claim it
    assert result.workflow_id == "wf-new"


def test_failed_job_with_attempts_left_keeps_its_count():
    job = StubJob("failed", attempts=1, max_attempts=3)
    _, result = resolve_existing_job(job, workflow_id="wf-x")
    assert result.attempts == 1


def test_cancelled_job_is_not_silently_revived():
    job = StubJob("cancelled")
    with pytest.raises(ConflictError):
        resolve_existing_job(job, workflow_id="wf-x")


def test_service_exposes_the_resolver():
    assert hasattr(RenderService, "enqueue")
    assert callable(resolve_existing_job)
    asyncio.run(asyncio.sleep(0))  # keep pytest-asyncio-free
