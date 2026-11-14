"""Video render service.

Owns the lifecycle of a Remotion render:

    queued → bundling → rendering → encoding → validating → succeeded | failed | cancelled

* Runs in a background task (never inside an HTTP request).
* Bounded concurrency (`RENDER_CONCURRENCY`, default 1) so two renders do not fight over the CPU.
* Automatic retry for transient infrastructure failures (browser crash, OOM, engine restart), not
  for invalid input or missing assets.
* Cancellation through Remotion's cancel signal, propagated to the job record.
* Progress persisted so the dashboard can poll it.
* Output validation (dimensions, fps, duration, audio, A/V sync, file integrity) before publishing.
* Temporary staging is deleted by the adapter; failures keep a short diagnostic excerpt instead.

The service knows nothing about Remotion internals — it talks to the renderer port.
"""

# Reading a finished temp artifact (size, bytes for upload) is a local file operation.
# ruff: noqa: ASYNC240

from __future__ import annotations

import asyncio
import hashlib
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage import LocalStorage, get_storage
from app.adapters.video import RemotionRenderer, RenderCancelled, RenderError, engine_health
from app.core.errors import ConflictError, ValidationAppError
from app.core.logging import get_logger
from app.core.settings import get_settings
from app.db.base import elapsed_seconds
from app.db.session import SessionFactory
from app.models import (
    ContentProject,
    RenderCostLog,
    RenderJob,
    RenderValidation,
    VideoRender,
)
from app.ports.interfaces import RenderProgress, StoryboardRenderSpec
from app.services import storyboard as sb

log = get_logger("factory.render")

#: Failures worth retrying (infrastructure / transient). Data problems are not retried.
RETRYABLE_CODES = {"render_failed", "engine_unavailable", "render_timeout"}

#: Statuses that mean "this job is computing". Shared with the pipeline's duplicate-render guard.
ACTIVE_RENDER_STATES_FOR_JOB = ("queued", "bundling", "rendering", "encoding", "validating")


@dataclass(slots=True)
class EnqueueResult:
    job: RenderJob
    reused: bool = False


class RenderService:
    def __init__(self) -> None:
        self.storage = get_storage()
        self.renderer = RemotionRenderer(self.storage, workdir=None)
        self._queue: asyncio.Queue[uuid.UUID] = asyncio.Queue()
        self._known: set[uuid.UUID] = set()  # ids this process has seen
        self._queued_ids: set[uuid.UUID] = set()  # ids currently on this process's queue
        self._workers: list[asyncio.Task] = []
        self._semaphore = asyncio.Semaphore(max(1, get_settings().render_concurrency))
        self._started = False

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        """Recover after a restart, then start the worker(s) and keep them running."""
        if self._started:
            return
        self._started = True
        await self.reconcile_schema()
        await self.recover()
        self._ensure_workers(max(1, get_settings().render_concurrency))

    async def reconcile_schema(self) -> None:
        """Reconcile the schema on SQLite only.

        SQLite installs are self-creating (``create_all`` plus the additive reconciler), so a code
        update picks up new columns without a migration step. PostgreSQL is owned by Alembic — the
        API runs ``alembic upgrade head`` before it starts, and two processes racing ``create_all``
        against a half-migrated database is how you get duplicate-relation errors.
        """
        if not get_settings().database_url.startswith("sqlite"):
            return
        from app.db.session import init_models

        try:
            await init_models()
        except Exception as exc:  # noqa: BLE001 - never block rendering on a schema pass
            log.warning("render.schema_reconcile_failed %s: %s", type(exc).__name__, exc)

    async def recover(self) -> None:
        """Mark jobs interrupted by a previous process as failed; re-queue the ones that never ran.

        Safe to call from more than one process: a job another runner already claimed is left alone.
        """
        async with SessionFactory() as session:
            settings = get_settings()
            stale = list(
                (
                    await session.execute(
                        select(RenderJob).where(
                            RenderJob.status.in_(
                                ["bundling", "rendering", "encoding", "validating"]
                            )
                        )
                    )
                ).scalars()
            )
            for job in stale:
                if job.attempts < (job.max_attempts or settings.render_max_attempts):
                    # Nothing is rendering it (we just started, so no worker owns it yet). Re-queue
                    # instead of failing: a job interrupted mid-render has already been paid for.
                    job.status = "queued"
                    job.stage = "queued"
                    job.progress = 0
                    job.error_message = "Re-queued after an interrupted attempt."
                else:
                    job.status = "failed"
                    job.stage = "failed"
                    job.error_code = "interrupted"
                    job.error_message = "The render worker restarted before this job finished."
                    job.finished_at = datetime.now(UTC)
            if stale:
                await session.commit()
                log.warning("render.recovered_stale jobs=%s", len(stale))

            queued = list(
                (await session.execute(select(RenderJob.id).where(RenderJob.status == "queued"))).scalars()
            )
        for job_id in queued:
            # Claim before queueing so a worker in another process cannot race us for it.
            async with SessionFactory() as session:
                await self._claim(session, job_id)
            self._known.add(job_id)
            self._queued_ids.add(job_id)
            await self._queue.put(job_id)
        if queued:
            log.info("render.requeued jobs=%s", len(queued))

    async def shutdown(self) -> None:
        for task in self._workers:
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._started = False

    def _ensure_workers(self, count: int) -> None:
        # Workers are created lazily so an enqueue is valid whether or not `start()` ran.
        while len(self._workers) < count:
            self._workers.append(asyncio.create_task(self._worker(len(self._workers))))

    async def _worker(self, index: int) -> None:
        log.info("render.worker_started index=%s", index)
        while True:
            job_id = None
            try:
                job_id = await asyncio.wait_for(self._queue.get(), timeout=2.0)
            except TimeoutError:
                job_id = await self._next_foreign_job()
                if job_id is None:
                    continue
            # Trace the handoff: a job that is claimed but never reaches the renderer is the
            # difference between "queued" and "stuck".
            log.info("render.worker_pickup job=%s source=%s", job_id,
                     "queue" if job_id in self._queued_ids else "poll")
            try:
                async with self._semaphore:
                    await self._execute(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a worker must never die
                log.exception("render.worker_crashed job=%s", job_id)
            finally:
                if job_id in self._queued_ids:
                    self._queued_ids.discard(job_id)
                    self._queue.task_done()

    async def _next_foreign_job(self) -> uuid.UUID | None:
        """Jobs queued by another process (API and worker share a database, not a queue)."""
        if not get_settings().render_cross_process_poll:
            return None
        async with SessionFactory() as session:
            pending = list(
                (
                    await session.execute(
                        select(RenderJob.id)
                        .where(RenderJob.status == "queued")
                        .order_by(RenderJob.queued_at)
                        .limit(20)
                    )
                ).scalars()
            )
        for job_id in pending:
            if job_id not in self._known:
                self._known.add(job_id)
                return job_id
        return None

    async def _claim(self, session: AsyncSession, job_id: uuid.UUID) -> bool:
        """Atomically move queued → bundling; returns False when another runner got it first."""
        result = await session.execute(
            RenderJob.__table__.update()
            .where(RenderJob.id == job_id, RenderJob.status == "queued")
            .values(status="bundling", stage="bundling", attempts=RenderJob.attempts + 1,
                    started_at=datetime.now(UTC))
        )
        await session.commit()
        return bool(result.rowcount)

    async def _heartbeat(self, job_id: uuid.UUID) -> None:
        """Keep the job row fresh so a supervisor can tell a live render from an abandoned one.

        ``updated_at`` is maintained by the ORM mixin on every commit the progress callback makes,
        so rendering already heartbeats; this exists for long silent phases (bundling, muxing).
        """
        interval = 30
        while True:
            await asyncio.sleep(interval)
            try:
                async with SessionFactory() as session:
                    job = await session.get(RenderJob, job_id)
                    if job is None or job.status in ("succeeded", "failed", "cancelled"):
                        return
                    job.updated_at = datetime.now(UTC)
                    await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a missed beat must never fail a render
                log.debug("render.heartbeat_failed job=%s", job_id)

    # ------------------------------------------------------------------ enqueue
    async def enqueue(
        self,
        session: AsyncSession,
        *,
        project: ContentProject,
        template_id: str | None = None,
        kind: str = "full",
        scene_indexes: list[int] | None = None,
        workflow_id: uuid.UUID | None = None,
        requested_by: uuid.UUID | None = None,
        fps: int = 30,
        speed: float = 1.0,
        include_intro: bool | None = None,
        include_outro: bool | None = None,
        idempotency_key: str | None = None,
        max_attempts: int | None = None,
        channel=None,
        setting=None,
    ) -> EnqueueResult:
        storyboard = await sb.load_storyboard(session, project)
        if storyboard is None:
            raise ValidationAppError("This project has no storyboard yet — run the pipeline first")
        scenes = await sb.load_scenes(session, project, storyboard)
        if not scenes:
            raise ValidationAppError("The storyboard has no scenes")
        if scene_indexes:
            wanted = set(scene_indexes)
            scenes = [s for s in scenes if s.scene.index in wanted]
            missing = wanted - {s.scene.index for s in scenes}
            if missing:
                raise ValidationAppError(f"Unknown scene indexes: {sorted(missing)}")
        # Code scenes are rendered from their stored programme, like generative ones are rendered
        # from the prompt; neither needs an image asset.
        unavailable = (
            []
            if getattr(channel, "visual_mode", "image") in ("generative", "code")
            else [i.scene.index for i in scenes if i.image is None]
        )
        if unavailable and kind == "full":
            raise ValidationAppError(
                f"Scene {unavailable} has no image yet; generate assets before rendering"
            )

        if channel is None:
            from app.models import Channel

            channel = await session.get(Channel, project.channel_id)
        if setting is None:
            from app.models import ChannelSetting

            setting = await session.scalar(
                select(ChannelSetting).where(ChannelSetting.channel_id == project.channel_id)
            )

        template = sb.template_for(channel.video_format if channel else "16:9", template_id)
        plan = sb.build_spec(
            project=project,
            channel=channel,  # type: ignore[arg-type]
            setting=setting,
            scenes=scenes,
            template_id=template,
            fps=fps,
            speed=speed,
            include_intro=True if include_intro is None else include_intro,
            include_outro=True if include_outro is None else include_outro,
            visual_mode=getattr(channel, "visual_mode", "image"),
            width=None if kind != "scene" else None,
        )

        key = idempotency_key or _fingerprint(plan, kind, scene_indexes)
        # The table has a UNIQUE index on idempotency_key, so only ONE row may ever carry it —
        # including a failed row. Looking only for active/succeeded rows let a failed job keep the
        # key, and the next insert died on uq_render_job_idempotency (reported as
        # PendingRollbackError, invisible in the workflow log). A failed job is retryable; a
        # cancelled one is the operator's decision and must not be silently revived.
        existing = await session.scalar(
            select(RenderJob)
            .where(
                RenderJob.project_id == project.id,
                RenderJob.idempotency_key == key,
            )
            .order_by(RenderJob.queued_at.desc())
            .limit(1)
        )
        settings = get_settings()
        if existing is not None:
            action, existing = resolve_existing_job(existing, workflow_id=workflow_id)
            if action == "reuse":
                return EnqueueResult(job=existing, reused=True)
            # A revived row must be committed before the worker can claim it.
            await session.commit()
            log.info("render.requeued_failed job=%s", existing.id)
            self._ensure_workers(settings.render_concurrency)
            self._known.add(existing.id)
            self._queued_ids.add(existing.id)
            await self._queue.put(existing.id)
            return EnqueueResult(job=existing, reused=False)

        job = RenderJob(
            workspace_id=project.workspace_id,
            project_id=project.id,
            workflow_id=workflow_id,
            kind=kind,
            template_id=plan.template_id,
            composition_id=plan.template_id,
            status="queued",
            stage="queued",
            width=plan.width,
            height=plan.height,
            fps=plan.fps,
            duration_s=plan.duration_s,
            spec=_public_spec(plan.spec),
            scene_indexes=scene_indexes or [],
            max_attempts=max_attempts or settings.render_max_attempts,
            idempotency_key=key,
            requested_by=requested_by,
        )
        session.add(job)
        await session.flush()
        job.worker_pid = None
        await session.commit()
        self._ensure_workers(settings.render_concurrency)
        self._known.add(job.id)
        # Claim before queueing: the row must say "bundling" before any worker can pick it up, so a
        # second runner skips it and a worker never sees a job it is not allowed to render.
        await self._claim(session, job.id)
        self._queued_ids.add(job.id)
        await self._queue.put(job.id)
        log.info("render.enqueued job=%s template=%s scenes=%s", job.id, plan.template_id, len(scenes))
        return EnqueueResult(job=job, reused=False)

    # ------------------------------------------------------------------ execute
    async def _execute(self, job_id: uuid.UUID) -> None:
        log.info("render.execute_begin job=%s", job_id)
        try:
            await self._execute_inner(job_id)
        except BaseException as exc:
            log.exception("render.execute_crashed job=%s type=%s", job_id, type(exc).__name__)
            raise

    async def _execute_inner(self, job_id: uuid.UUID) -> None:
        async with SessionFactory() as session:
            job = await session.get(RenderJob, job_id)
            if job is None or job.cancel_requested:
                log.info("render.execute_skipped job=%s reason=%s", job_id,
                         "missing" if job is None else "cancel_requested")
                return
            # Ownership: claim atomically so the API and a separate worker never render the same job
            # twice. A job already in "bundling" was claimed by enqueue in this process (or by a
            # previous pickup), so it is ours to execute. The old check keyed off `_queued_ids`,
            # which raced: a worker that picked the job up between `queue.put()` and `_claim()`
            # saw status "queued", skipped the claim, then bailed on the "bundling" check — leaving
            # the job stuck in bundling forever with no process rendering it.
            decision = should_execute(job.status)
            if decision == "skip":
                return
            if decision == "claim" and not await self._claim(session, job_id):
                return
            spec = await self._spec_for(session, job)
            if spec is None:
                job.status = "failed"
                job.error_code = "invalid_spec"
                job.error_message = "The job describes scenes that no longer exist."
                job.finished_at = datetime.now(UTC)
                await session.commit()
                return
            try:
                await self._run(session, job, spec)
            except RenderCancelled:
                job.status = "cancelled"
                job.stage = "cancelled"
                job.finished_at = datetime.now(UTC)
                await session.commit()
            except RenderError as exc:
                await self._fail(session, job, exc)
            except Exception as exc:  # noqa: BLE001 - any crash is a job failure
                await self._fail(session, job, RenderError(f"{type(exc).__name__}: {exc}"))

    async def _run(self, session: AsyncSession, job: RenderJob, spec: StoryboardRenderSpec) -> None:
        async def progress(p: RenderProgress) -> None:
            mapped = _map_progress(p)
            if mapped["progress"] - job.progress >= 2 or mapped["stage"] != job.stage:
                job.progress = mapped["progress"]
                job.stage = mapped["stage"]
                job.rendered_frames = p.rendered_frames
                job.status = _status_for(p.stage)
                await session.commit()

        artifact, meta = await self.renderer.render(spec, on_progress=progress)
        job.status = "validating"
        job.stage = "validating"
        composition = meta.get("composition") or {}
        # Validate against what Remotion actually composed: its frame count is authoritative and
        # already accounts for narration-adjusted scene durations and transition overlaps.
        if composition.get("durationInFrames") and composition.get("fps"):
            job.duration_s = round(
                float(composition["durationInFrames"]) / float(composition["fps"]), 3
            )
        job.frames = int(composition.get("durationInFrames") or meta.get("frame_count") or 0)
        job.rendered_frames = int(meta.get("frame_count") or 0)
        log.info("render.composed job=%s duration=%.2fs frames=%s", job.id, job.duration_s, job.frames)
        await session.commit()

        path = Path(artifact.output_path)
        log.info("render.artifact job=%s path=%s bytes=%s", job.id, path, artifact.bytes)
        passed, checks, issues, meta_info = await self.renderer.validate(
            path,
            expected={"width": job.width, "height": job.height, "fps": job.fps,
                      "duration_s": job.duration_s},
        )
        session.add(RenderValidation(
            job_id=job.id,
            workspace_id=job.workspace_id,
            passed=passed,
            checks=checks,
            issues=issues,
            video_codec=meta_info.get("video_codec"),
            audio_codec=meta_info.get("audio_codec"),
            width=int(meta_info.get("width") or 0),
            height=int(meta_info.get("height") or 0),
            fps=float(meta_info.get("fps") or 0),
            duration_s=float(meta_info.get("duration_s") or 0),
            bitrate_kbps=meta_info.get("bitrate_kbps"),
        ))
        if not passed:
            await self._fail(session, job, RenderError("; ".join(issues) or "Output validation failed",
                                                       code="validation_failed"))

        data = path.read_bytes()
        video_key = f"p/{job.project_id}/renders/{job.id}.mp4"
        stored = await self.storage.put_bytes(video_key, data, "video/mp4")

        stills = meta.get("stills") or []
        poster_key = None
        if stills:
            poster = Path(stills[0])
            if poster.exists():
                poster_key = f"p/{job.project_id}/renders/{job.id}-poster.jpg"
                await self.storage.put_bytes(poster_key, poster.read_bytes(), "image/jpeg")

        job.output_key = stored.key
        job.bytes = stored.bytes
        job.poster_key = poster_key
        job.width = int(meta_info.get("width") or job.width)
        job.height = int(meta_info.get("height") or job.height)
        job.duration_s = float(meta_info.get("duration_s") or job.duration_s)
        job.frames = int(meta_info.get("frames") or job.rendered_frames or 0)
        job.progress = 100
        job.status = "succeeded"
        job.stage = "succeeded"
        job.finished_at = datetime.now(UTC)

        # Point the project's current video at this render (full renders only).
        if job.kind == "full":
            render = await session.get(VideoRender, job.render_id) if job.render_id else None
            if render is None:
                render = VideoRender(
                    workspace_id=job.workspace_id,
                    project_id=job.project_id,
                    started_at=job.started_at,
                )
                session.add(render)
                await session.flush()
                job.render_id = render.id
            render.template = job.template_id
            render.resolution = f"{job.width}x{job.height}"
            render.fps = job.fps
            render.storage_key = stored.key
            render.duration_s = job.duration_s
            render.bytes = stored.bytes
            render.status = "ready"
            render.progress = 100
            render.completed_at = datetime.now(UTC)
            render.error = None

        elapsed = elapsed_seconds(job.started_at)
        session.add(RenderCostLog(
            job_id=job.id,
            workspace_id=job.workspace_id,
            template_id=job.template_id,
            duration_s=job.duration_s,
            render_seconds=round(elapsed, 2),
            frames=job.frames,
            estimated_cost_usd=0,
        ))
        await session.commit()
        # Temporary artifacts (staged assets, preview stills, the raw output) are never kept.
        await asyncio.to_thread(_cleanup, path.parent)
        log.info("render.succeeded job=%s %sx%s %.1fs %s KB in %.1fs",
                 job.id, job.width, job.height, job.duration_s, stored.bytes // 1024, elapsed)

    async def _fail(self, session: AsyncSession, job: RenderJob, exc: RenderError) -> None:
        retryable = exc.code in RETRYABLE_CODES and job.attempts < job.max_attempts
        job.error_code = exc.code
        job.error_message = str(exc)[:2000]
        if retryable:
            job.status = "queued"
            job.stage = "retry"
            job.progress = 0
            await session.commit()
            log.warning("render.retry job=%s attempt=%s/%s code=%s",
                        job.id, job.attempts, job.max_attempts, exc.code)
            await asyncio.sleep(min(30, 2 ** job.attempts))
            await self._claim(session, job.id)
            self._queued_ids.add(job.id)
            await self._queue.put(job.id)
            return
        job.status = "failed"
        job.stage = "failed"
        job.finished_at = datetime.now(UTC)
        render = await session.get(VideoRender, job.render_id) if job.render_id else None
        if render is not None and render.status != "ready":
            render.status = "failed"
            render.error = str(exc)[:1000]
        await session.commit()
        log.error("render.failed job=%s code=%s message=%s", job.id, exc.code, str(exc)[:400])

    async def _spec_for(self, session: AsyncSession, job: RenderJob) -> StoryboardRenderSpec | None:
        project = await session.get(ContentProject, job.project_id)
        if project is None:
            return None
        from app.models import Channel, ChannelSetting

        channel = await session.get(Channel, project.channel_id)
        setting = await session.scalar(
            select(ChannelSetting).where(ChannelSetting.channel_id == project.channel_id)
        )
        storyboard = await sb.load_storyboard(session, project)
        if storyboard is None or channel is None:
            return None
        scenes = await sb.load_scenes(session, project, storyboard)
        if job.scene_indexes:
            scenes = [s for s in scenes if s.scene.index in set(job.scene_indexes)]
        if not scenes:
            return None
        # The stored spec is the snapshot of intent; asset references are re-resolved so a scene
        # re-render picks up regenerated images while everything else stays byte-identical.
        plan = sb.build_spec(
            project=project,
            channel=channel,
            setting=setting,
            scenes=scenes,
            template_id=job.template_id,
            fps=job.fps,
            visual_mode=getattr(channel, "visual_mode", "image"),
            width=job.width,
            height=job.height,
        )
        spec = {**plan.spec, **_presentation(job.spec)}
        # Validate against the duration the composition will actually have (frame-accurate).
        job.duration_s = sb.actual_duration(spec)
        return StoryboardRenderSpec(
            render_id=str(job.id),
            project_id=str(job.project_id),
            composition_id=job.composition_id,
            spec=spec,
            assets=[{**a, "source": _local_path(a["source"])} for a in plan.assets],
            stills=[],
            cancel_token=str(job.id),
        )

    # ------------------------------------------------------------------ controls
    async def cancel(self, session: AsyncSession, job: RenderJob) -> RenderJob:
        if job.status in ("succeeded", "failed", "cancelled"):
            raise ConflictError(f"This render already finished ({job.status})")
        job.cancel_requested = True
        await session.commit()
        await self.renderer.cancel(str(job.id))
        await session.refresh(job)
        return job

    async def progress_of(self, job_id: uuid.UUID) -> dict[str, Any] | None:
        async with SessionFactory() as session:
            job = await session.get(RenderJob, job_id)
            if job is None:
                return None
            return {
                "id": str(job.id),
                "status": job.status,
                "stage": job.stage,
                "progress": job.progress,
                "rendered_frames": job.rendered_frames,
            }


# --------------------------------------------------------------------------- helpers


def _map_progress(p: RenderProgress) -> dict[str, Any]:
    """Buckets: bundling 0-5, rendering 5-70, encoding 70-95, muxing 95-99."""
    stage = p.stage or "rendering"
    frac = max(0.0, min(1.0, float(p.progress or 0)))
    if stage == "bundling":
        pct = 2 + frac * 3
        label = "bundling"
    elif stage == "muxing":
        pct = 95 + frac * 4
        label = "encoding"
    elif stage == "encoding":
        pct = 70 + frac * 25
        label = "encoding"
    else:
        pct = 5 + frac * 65
        label = "rendering"
    return {"progress": int(pct), "stage": label}


def _status_for(stage: str) -> str:
    return {
        "bundling": "bundling",
        "rendering": "rendering",
        "encoding": "encoding",
        "validating": "validating",
    }.get(stage, "rendering")


def _public_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Keep the snapshot JSON-only (asset keys are resolved at execution time)."""
    import json

    return json.loads(json.dumps(spec, default=str))


def _presentation(spec: dict[str, Any]) -> dict[str, Any]:
    """The parts of the snapshot that describe intent rather than assets."""
    keys = ("title", "brand", "templateId", "width", "height", "fps", "accent", "background",
            "ink", "captions", "intro", "outro", "defaultTransition", "transitionDurationS",
            "music", "speed")
    return {k: spec[k] for k in keys if k in spec}


def _local_path(key: str) -> str:
    """Storage key → local file path (the renderer needs files, not URLs)."""
    storage = get_storage()
    if isinstance(storage, LocalStorage):
        return str(storage._path(key))  # validated, inside the storage root
    raise RenderError(
        "Remote object storage is configured; stage assets locally before rendering "
        "(STORAGE_BACKEND=local is required by the renderer today).",
        code="storage_unsupported",
    )


def _cleanup(directory: Path) -> None:
    """Remove a temporary artifact directory (never the storage root)."""
    name = directory.name
    if directory.is_dir() and (name.startswith("factory-artifacts-")
                               or name.startswith("factory-remotion-")):
        shutil.rmtree(directory, ignore_errors=True)


def _fingerprint(plan: sb.StoryboardPlan, kind: str, scene_indexes: list[int] | None) -> str:
    payload = repr((
        plan.template_id,
        plan.width,
        plan.height,
        plan.fps,
        plan.duration_s,
        [s["durationS"] for s in plan.spec["scenes"]],
        [a["source"] for a in plan.assets],
        kind,
        sorted(scene_indexes or []),
    ))
    return hashlib.sha256(payload.encode()).hexdigest()[:48]


_SERVICE: RenderService | None = None


def should_execute(status: str) -> str:
    """What to do with a picked-up job: ``claim``, ``execute`` or ``skip``.

    Kept as a pure function because the rule is subtle and once wrong: "bundling" is not a reason to
    skip a job, it is the proof that someone already claimed it.
    """
    if status == "queued":
        return "claim"
    if status == "bundling":
        return "execute"
    return "skip"


def resolve_existing_job(
    job: RenderJob, *, workflow_id: str | None
) -> tuple[str, RenderJob]:
    """Decide what to do with a row that already owns this idempotency key.

    Returns ``("reuse", job)`` when the job is still meaningful, ``("requeue", job)`` when a failed
    job is being retried (the row is reset in place), and raises for cancelled work — reviving an
    operator's cancellation would render a video they stopped.
    """
    if job.status in ACTIVE_RENDER_STATES_FOR_JOB or job.status == "succeeded":
        return "reuse", job
    if job.status == "failed":
        # Reuse the row: it owns the UNIQUE key, so inserting another one is impossible.
        job.status = "queued"
        job.stage = "queued"
        job.progress = 0
        job.error_code = None
        job.error_message = None
        job.worker_pid = None
        job.queued_at = datetime.now(UTC)
        job.started_at = None
        job.finished_at = None
        job.output_key = None
        if job.attempts >= job.max_attempts:
            job.attempts = 0
        if workflow_id:
            job.workflow_id = workflow_id
        return "requeue", job
    raise ConflictError(
        f"This render was {job.status}; start a new attempt instead of reusing it"
    )


def render_service() -> RenderService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = RenderService()
    return _SERVICE


def engine_status() -> dict[str, Any]:
    health = engine_health()
    return {
        "ok": health.ok,
        "detail": health.detail,
        "node": health.node_version,
        "remotion": health.remotion_version,
        "installed": health.bundler_installed,
        "engine": "remotion",
    }


__all__ = [
    "EnqueueResult",
    "RenderService",
    "engine_status",
    "render_service",
]
