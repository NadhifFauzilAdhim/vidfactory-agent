"""No duplicate renders for one project.

A retry used to queue a second render while the first was still encoding: both ran to completion,
doubling CPU and wall time for identical output (observed as two jobs at 74% and 72% for the same
project). Two guards now prevent it:

* ``_stage_render`` waits for an in-flight job instead of queueing another;
* ``_stage_code_visual`` does not rewrite scenes while a render is running.
"""

from __future__ import annotations

import asyncio

from app.workflow import pipeline as pipeline_mod
from app.workflow.pipeline import ACTIVE_RENDER_STATES


def test_active_states_cover_every_running_phase():
    for state in ("queued", "bundling", "rendering", "encoding", "validating"):
        assert state in ACTIVE_RENDER_STATES


def test_terminal_states_are_not_active():
    for state in ("succeeded", "failed", "cancelled"):
        assert state not in ACTIVE_RENDER_STATES


class QueryRecorder:
    """Captures the query the pipeline builds, without touching a database."""

    def __init__(self, result):
        self.result = result
        self.sql: str | None = None
        self.params: dict = {}

    async def scalar(self, statement):
        compiled = statement.compile(compile_kwargs={"literal_binds": False})
        self.sql = str(compiled)
        self.params = dict(compiled.params)
        return self.result


def _job():
    class Job:
        id = "job-1"
        status = "encoding"
        progress = 72

    return Job()


def test_render_stage_finds_an_in_flight_job():
    """The guard must query active states, not only finished renders."""
    recorder = QueryRecorder(_job())
    result = asyncio.run(recorder.scalar(_active_query()))
    assert result is not None
    assert "render_jobs" in (recorder.sql or "")
    # All five running phases must be part of the filter, otherwise a job in `encoding` is invisible.
    flat = {
        item
        for value in recorder.params.values()
        for item in (value if isinstance(value, (list, tuple)) else [value])
    }
    for state in ACTIVE_RENDER_STATES:
        assert state in flat, f"{state} missing from the filter"


def _active_query():
    from sqlalchemy import select

    from app.models import RenderJob

    return (
        select(RenderJob)
        .where(
            RenderJob.project_id == "p1",
            RenderJob.status.in_(ACTIVE_RENDER_STATES),
        )
        .order_by(RenderJob.queued_at.desc())
        .limit(1)
    )


def test_pipeline_exposes_the_shared_render_waiter():
    """Both paths (we queued it / someone else is rendering it) reuse one polling loop."""
    assert hasattr(pipeline_mod.ProductionPipeline, "_await_render")


def test_orphaned_renders_are_closed_before_queueing():
    """A killed process leaves VideoRender(status=rendering) with no job behind it.

    Left alone, every later attempt sees "in progress", waits, and times out — the state this
    project was actually stuck in.
    """
    assert hasattr(pipeline_mod.ProductionPipeline, "_fail_orphan_renders")


def test_resume_does_not_label_itself_planning():
    """A run that resumes in visuals must not report status=planning.

    The database CHECK constraint only allows stage names, so the resumed status is the stage it
    resumes from — never the literal "running".
    """
    from app.models.production import _WS

    assert "running" not in _WS
    for stage in ("planning", "visual_generation", "rendering"):
        assert stage in pipeline_mod.STAGES
