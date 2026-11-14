"""Production workflow engine.

A single, backend-agnostic implementation of the ten-stage production pipeline. The same
``ProductionPipeline`` class is driven either by:

* ``LocalWorkflowRunner`` — durable in-process executor (Postgres state, resumable), or
* ``TemporalWorker`` — Temporal activities (``services/workflow-engine``).

State lives in ``production_workflows`` / ``workflow_tasks`` / ``workflow_logs`` so any process can
observe, resume or retry a run. Never execute production inside an HTTP request.

All stages are idempotent: asset keys derive from content hashes, and usage rows are keyed by
``request_key``.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.contracts import (
    PlannerInput,
    ScriptBrief,
    ScriptScene,
    SeoInput,
    StoryboardInput,
)
from app.agents.generation import word_count
from app.agents.media import (
    VisualRequest,
    VisualSceneRequest,
    VoiceRequest,
    VoiceSceneRequest,
)
from app.agents.quality import PublishRequest, QcInput
from app.agents.registry import REGISTRY
from app.container import ProviderFactory
from app.core.errors import AppError, BudgetExceededError, NotFoundError
from app.core.logging import get_logger
from app.core.settings import get_settings
from app.db.base import elapsed_seconds
from app.db.session import session_scope
from app.models import (
    AiUsageLog,
    ApprovalRequest,
    AudioAsset,
    Channel,
    ChannelSetting,
    ContentIdea,
    ContentProject,
    MediaAsset,
    ProductionWorkflow,
    QualityReport,
    Scene,
    Script,
    Storyboard,
    VideoRender,
    WorkflowLog,
    WorkflowTask,
    Workspace,
)
from app.ports.interfaces import Usage as _Usage
from app.services.youtube_oauth import refresh_access_token

log = get_logger("factory.workflow")

#: A render job in any of these states is still being computed. A second job for the same project
#: must wait for it instead of starting a duplicate — the failure that produced two renders of
#: "Kancing Kayu Danu" at 74% and 72%, both burning CPU and tokens for the same frames.
ACTIVE_RENDER_STATES = ("queued", "bundling", "rendering", "encoding", "validating")

STAGES = [
    "planning",
    "script_generation",
    "storyboarding",
    "voice_generation",
    "visual_generation",
    "rendering",
    "quality_checking",
    "seo",
    "awaiting_approval",
    "publishing",
    "published",
]
STAGE_PROGRESS = {stage: int((i + 1) / len(STAGES) * 100) for i, stage in enumerate(STAGES)}
STAGE_AGENT = {
    "planning": "content_planner",
    "script_generation": "script_writer",
    "storyboarding": "storyboard",
    "voice_generation": "voice_generation",
    "visual_generation": "visual_generation",
    "rendering": "video_production",
    "quality_checking": "quality_control",
    "seo": "seo_metadata",
    "publishing": "publishing",
}


class WorkflowCancelled(Exception):
    pass


class ApprovalPending(Exception):
    """Raised when the run must pause for a human decision (not an error)."""


@dataclass(slots=True)
class RunState:
    workflow_id: uuid.UUID
    project_id: uuid.UUID
    channel_id: uuid.UUID
    workspace_id: uuid.UUID
    force_approval: bool = False
    regenerate_targets: list[str] = field(default_factory=list)


class WorkflowRepository:
    """Persistence helpers shared by HTTP services and workers."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        channel_id: uuid.UUID,
        trigger: str = "manual",
        force_approval: bool = False,
    ) -> ProductionWorkflow:
        workflow = ProductionWorkflow(
            workspace_id=workspace_id,
            channel_id=channel_id,
            workflow_key=f"prod-{uuid.uuid4().hex[:12]}",
            trigger=trigger,
            status="pending",
            stage="planning",
            progress=0,
            input_json={"force_approval": force_approval},
        )
        self.session.add(workflow)
        await self.session.flush()
        return workflow

    async def log(
        self,
        workflow_id: uuid.UUID,
        message: str,
        *,
        level: str = "info",
        task_id: uuid.UUID | None = None,
        **context: Any,
    ) -> None:
        self.session.add(
            WorkflowLog(
                workflow_id=workflow_id,
                task_id=task_id,
                level=level,
                message=message[:4000],
                context=context,
            )
        )

    async def set_stage(self, workflow: ProductionWorkflow, stage: str, agent_key: str | None = None) -> None:
        workflow.stage = stage
        workflow.current_agent_key = agent_key
        workflow.progress = STAGE_PROGRESS.get(stage, workflow.progress)
        workflow.heartbeat_at = datetime.now(UTC)
        if stage in STAGE_PROGRESS and STAGE_PROGRESS[stage] <= 80 and workflow.status != "pending":
            workflow.status = _status_for_stage(stage)
        elif workflow.status == "pending":
            workflow.status = _status_for_stage(stage)
        await self.session.flush()

    async def start_task(
        self, workflow: ProductionWorkflow, agent_key: str, step_index: int, payload: dict
    ) -> WorkflowTask:
        attempt = (
            await self.session.execute(
                select(func.coalesce(func.max(WorkflowTask.attempt), 0)).where(
                    WorkflowTask.workflow_id == workflow.id,
                    WorkflowTask.step_index == step_index,
                )
            )
        ).scalar_one() + 1
        task = WorkflowTask(
            workflow_id=workflow.id,
            step_index=step_index,
            attempt=int(attempt),
            agent_key=agent_key,
            name=REGISTRY.get(agent_key).name,
            status="running",
            input_json=_shrink(payload),
            started_at=datetime.now(UTC),
        )
        self.session.add(task)
        await self.session.flush()
        return task

    async def finish_task(
        self,
        task: WorkflowTask,
        *,
        output: dict[str, Any] | None = None,
        usage: Any = None,
        error: str | None = None,
        provider: str = "",
        model: str = "",
    ) -> None:
        task.completed_at = datetime.now(UTC)
        task.status = "failed" if error else "succeeded"
        task.output_json = _shrink(output or {})
        task.error = error
        task.provider = provider or getattr(usage, "provider", None)
        task.model = model or getattr(usage, "model", None)
        if usage:
            task.cost_usd = float(getattr(usage, "cost_usd", 0) or 0)
            task.tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
            task.tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
        if task.started_at:
            task.duration_ms = int(elapsed_seconds(task.started_at, task.completed_at) * 1000)
        await self.session.flush()


def _status_for_stage(stage: str) -> str:
    return {
        "planning": "planning",
        "script_generation": "script_generation",
        "storyboarding": "storyboarding",
        "voice_generation": "voice_generation",
        "visual_generation": "visual_generation",
        "rendering": "rendering",
        "quality_checking": "quality_checking",
        "seo": "quality_checking",
        "awaiting_approval": "awaiting_approval",
        "publishing": "publishing",
        "published": "published",
    }.get(stage, "pending")


def _shrink(data: dict[str, Any], limit: int = 20000) -> dict[str, Any]:
    import json

    text = json.dumps(data, default=str)
    if len(text) <= limit:
        return data
    return {"_truncated": True, "preview": text[:limit]}


# --------------------------------------------------------------------------- spend guard


async def month_spend(session: AsyncSession, workspace_id: uuid.UUID) -> float:
    start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = (
        await session.execute(
            select(func.coalesce(func.sum(AiUsageLog.cost_usd), 0)).where(
                AiUsageLog.workspace_id == workspace_id, AiUsageLog.created_at >= start
            )
        )
    ).scalar_one()
    return float(total or 0)


# --------------------------------------------------------------------------- pipeline


class ProductionPipeline:
    """Executes one run to completion (or the next blocking stage)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = WorkflowRepository(session)
        self.factory = ProviderFactory(session)
        self.step = 0

    async def resume_until(self, workflow: ProductionWorkflow, target_stage: str) -> None:
        """Resume the pipeline and stop once ``target_stage`` completes.

        Used by Temporal activities so each stage is its own retryable unit. Completed stages are
        no-ops because their outputs are already persisted (assets are content-addressed).
        """
        channel = await self.session.get(Channel, workflow.channel_id)
        if channel is None:
            raise NotFoundError("Channel not found")
        setting = await self.session.scalar(
            select(ChannelSetting).where(ChannelSetting.channel_id == channel.id)
        )
        project = await self.session.get(ContentProject, workflow.project_id) if workflow.project_id else None
        if project is None:
            project = await self._stage_planning(workflow, channel, setting)  # type: ignore[arg-type]
        if target_stage == "planning":
            return
        script = await self.session.scalar(
            select(Script).where(Script.project_id == project.id, Script.is_current.is_(True))
        )
        if script is None:
            script = await self._stage_script(workflow, channel, project)
        if target_stage == "script_generation":
            return
        storyboard = await self.session.scalar(
            select(Storyboard).where(Storyboard.script_id == script.id, Storyboard.is_current.is_(True))
        )
        if storyboard is None:
            storyboard = await self._stage_storyboard(workflow, channel, project, script, setting)  # type: ignore[arg-type]
        if target_stage == "storyboarding":
            return
        scenes = list(
            (
                await self.session.execute(
                    select(Scene).where(Scene.storyboard_id == storyboard.id).order_by(Scene.index)
                )
            ).scalars()
        )
        if target_stage == "voice_generation":
            await self._stage_voice(workflow, channel, project, scenes)
            return
        if target_stage == "visual_generation":
            await self._stage_visual(workflow, channel, project, scenes, setting)  # type: ignore[arg-type]
            return
        render = await self.session.scalar(
            select(VideoRender)
            .where(VideoRender.project_id == project.id, VideoRender.status == "ready")
            .order_by(VideoRender.created_at.desc())
            .limit(1)
        )
        if target_stage == "rendering":
            if render is None:
                await self._stage_render(workflow, channel, project, storyboard)
            return
        if render is None:
            render = await self._stage_render(workflow, channel, project, storyboard)
        if target_stage == "quality_checking":
            await self._stage_qc(workflow, channel, project, setting, scenes, render)  # type: ignore[arg-type]
            return
        if target_stage == "seo":
            await self._stage_seo(workflow, channel, project, setting)  # type: ignore[arg-type]
            await self._stage_approval(
                workflow, channel, setting, project,  # type: ignore[arg-type]
                await self.session.scalar(
                    select(QualityReport)
                    .where(QualityReport.project_id == project.id)
                    .order_by(QualityReport.created_at.desc())
                    .limit(1)
                ),  # type: ignore[arg-type]
                stop=True,
            )
            raise ApprovalPending()

    async def resume_after_approval(self, workflow_id: uuid.UUID) -> str:
        """Publish an approved workflow using its stored render and metadata."""
        workflow = await self.session.get(ProductionWorkflow, workflow_id)
        if workflow is None:
            raise NotFoundError("Workflow not found")
        if workflow.status == "cancelled":
            raise WorkflowCancelled("Workflow was cancelled")
        if workflow.project_id is None:
            raise NotFoundError("Workflow has no project")
        channel = await self.session.get(Channel, workflow.channel_id)
        project = await self.session.get(ContentProject, workflow.project_id)
        render = await self.session.scalar(
            select(VideoRender)
            .where(VideoRender.project_id == workflow.project_id, VideoRender.status == "ready")
            .order_by(VideoRender.created_at.desc())
            .limit(1)
        )
        if channel is None or project is None or render is None:
            raise NotFoundError("Workflow cannot be resumed: render or project missing")
        try:
            await self._stage_publish(workflow, channel, project, (project.metadata_json or {}).get("seo", {}), render)
            workflow.status = "published"
            workflow.stage = "published"
            workflow.progress = 100
            workflow.current_agent_key = "publishing"
            workflow.completed_at = datetime.now(UTC)
            await self.session.commit()
            return "published"
        except Exception as exc:
            workflow.status = "failed"
            workflow.error_message = f"{type(exc).__name__}: {exc}"[:2000]
            workflow.completed_at = datetime.now(UTC)
            await self.repo.log(workflow.id, f"Publishing failed: {exc}", level="error")
            await self.session.commit()
            raise

    async def ensure_budget(self, workspace_id: uuid.UUID) -> None:
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace and workspace.monthly_budget_usd is not None:
            spent = await month_spend(self.session, workspace_id)
            if spent >= float(workspace.monthly_budget_usd):
                raise BudgetExceededError(
                    f"Monthly AI budget reached (${spent:.2f} of ${float(workspace.monthly_budget_usd):.2f}). "
                    "Raise the budget in Costs → Budget or wait for the next cycle."
                )

    # -- helpers ---------------------------------------------------------

    async def _run_agent(
        self,
        workflow: ProductionWorkflow,
        channel: Channel,
        key: str,
        payload: Any,
        *,
        stage: str,
        extra_params: dict[str, Any] | None = None,
        allow_missing_provider: bool = False,
    ):
        await self.repo.set_stage(workflow, stage, key)
        await self.repo.log(workflow.id, f"{REGISTRY.get(key).name} started", agent=key)
        await self.session.commit()

        ctx = await self.factory.build_context(
            workspace_id=workflow.workspace_id,
            agent_key=key,
            channel=channel,
            project_id=workflow.project_id,
            workflow_id=workflow.id,
            request_key=f"{workflow.workflow_key}:{key}:{self.step}",
            extra_params=extra_params,
        )
        agent = REGISTRY.instance(key)
        result = await agent.run(ctx, payload)
        await self._record_usage(workflow, channel, ctx, result, key, stage)
        return ctx, result

    async def _record_usage(self, workflow, channel, ctx, result, agent_key: str, operation: str) -> None:
        usage = result.usage
        # `step` restarts at 0 on every attempt, so the key must also carry the attempt number;
        # otherwise a retry reuses a key an earlier attempt already committed and the flush dies on
        # uq_usage_request_key. The lookup alone cannot see pending rows in this session, which is
        # exactly how the duplicate reached the database.
        attempt = int(getattr(workflow, "retry_count", 0) or 0)
        request_key = (
            f"{workflow.workflow_key}:{agent_key}:a{attempt}:{self.step}:"
            f"{getattr(ctx, 'request_key', '')}"
        )
        cost = float(usage.cost_usd or 0)
        # Cost is only attributed when provider pricing is configured (per model), otherwise 0.
        existing = await self.session.scalar(
            select(AiUsageLog.id).where(AiUsageLog.request_key == request_key[:200])
        )
        if existing is None:
            self.session.add(
                AiUsageLog(
                    workspace_id=workflow.workspace_id,
                    provider_id=ctx.provider_id,
                    channel_id=channel.id,
                    workflow_id=workflow.id,
                    project_id=workflow.project_id,
                    agent_key=agent_key,
                    provider_slug=usage.provider or "",
                    model=usage.model or "",
                    operation=usage.operation or operation,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    units=float(usage.units or 0),
                    unit_label=usage.unit_label or "request",
                    cost_usd=cost,
                    latency_ms=usage.latency_ms,
                    status="ok",
                    request_key=request_key[:200],
                )
            )
        workflow.cost_usd = float(workflow.cost_usd or 0) + cost
        if workflow.project_id:
            project = await self.session.get(ContentProject, workflow.project_id)
            if project:
                project.total_cost_usd = float(project.total_cost_usd or 0) + cost
        await self.session.flush()

    # -- resume helpers ---------------------------------------------------

    async def _log_reused(self, workflow, stage: str, detail: str) -> None:
        """Say out loud that a stage was skipped, so the log explains where the tokens went."""
        await self.repo.log(
            workflow.id,
            f"{stage} reused · {detail} (already produced by an earlier attempt)",
        )
        await self.session.commit()

    async def _reuse_project(self, workflow) -> ContentProject | None:
        """The project a previous attempt already planned, if it is still usable."""
        if not workflow.project_id:
            return None
        project = await self.session.get(ContentProject, workflow.project_id)
        if project is None or project.deleted_at is not None:
            return None
        return project

    async def _reuse_script(self, project: ContentProject) -> Script | None:
        return await self.session.scalar(
            select(Script).where(Script.project_id == project.id, Script.is_current.is_(True))
        )

    async def _reuse_storyboard(self, script: Script) -> Storyboard | None:
        return await self.session.scalar(
            select(Storyboard).where(
                Storyboard.script_id == script.id, Storyboard.is_current.is_(True)
            )
        )

    async def _has_narration(self, project: ContentProject, scenes: list[Scene]) -> bool:
        """True when every scene already has usable narration audio.

        TTS is billed per character, so re-synthesising on every retry is the single most expensive
        mistake a resume can make. A scene counts as done only with a positive duration: a row that
        failed mid-write must be regenerated.
        """
        if not scenes:
            return False
        rows = list(
            (
                await self.session.execute(
                    select(AudioAsset).where(
                        AudioAsset.project_id == project.id, AudioAsset.kind == "narration"
                    )
                )
            ).scalars()
        )
        have = {a.scene_id for a in rows if float(a.duration_s or 0) > 0 and a.storage_key}
        return all(s.id in have for s in scenes)

    async def _has_visuals(self, project: ContentProject, scenes: list[Scene], channel) -> bool:
        """True when every scene already has the asset its visual mode needs."""
        mode = getattr(channel, "visual_mode", "image")
        if mode == "generative":
            # Nothing is generated for this mode; the stage is a log entry, so re-running is free.
            return False
        if not scenes:
            return False
        kind = "code" if mode == "code" else "image"
        return not await self._scenes_missing_visuals(project, scenes, kind)

    async def _scenes_missing_visuals(
        self, project: ContentProject, scenes: list[Scene], kind: str
    ) -> list[Scene]:
        """Scenes without an asset of ``kind`` — the only ones a retry should pay to generate.

        Regenerating a scene that already succeeded would re-bill an LLM/image call for work that is
        already on disk, which is exactly the waste a resume is supposed to avoid.
        """
        if not scenes:
            return []
        rows = list(
            (
                await self.session.execute(
                    select(MediaAsset).where(
                        MediaAsset.project_id == project.id,
                        MediaAsset.kind == kind,
                        MediaAsset.deleted_at.is_(None),
                    )
                )
            ).scalars()
        )
        have = {a.scene_id for a in rows if a.storage_key}
        return [s for s in scenes if s.id not in have]

    async def _reuse_qc(self, project: ContentProject, render: VideoRender) -> QualityReport | None:
        """A report is reusable only if it judged this exact render."""
        report = await self.session.scalar(
            select(QualityReport)
            .where(QualityReport.project_id == project.id)
            .order_by(QualityReport.created_at.desc())
            .limit(1)
        )
        if report is None:
            return None
        if report.render_id is not None and report.render_id == render.id:
            return report
        # Legacy rows without a render_id: fall back to comparing timestamps.
        rendered_at = render.completed_at or render.created_at
        if rendered_at is None or report.created_at is None:
            return None
        return report if report.created_at >= rendered_at else None

    async def _reuse_seo(self, project: ContentProject) -> dict | None:
        """Metadata is reusable when it exists and its thumbnail was produced."""
        seo = (project.metadata_json or {}).get("seo")
        if not seo or project.thumbnail_asset_id is None:
            return None
        return seo

    # -- stages ----------------------------------------------------------

    async def run(self, workflow_id: uuid.UUID, *, stop_at_approval: bool = True) -> str:
        workflow = await self.session.get(ProductionWorkflow, workflow_id)
        if workflow is None:
            raise NotFoundError("Workflow not found")
        if workflow.status in ("cancelled",):
            raise WorkflowCancelled("Workflow was cancelled")
        channel = await self.session.get(Channel, workflow.channel_id)
        if channel is None:
            raise NotFoundError("Channel not found")
        setting = (
            await self.session.execute(select(ChannelSetting).where(ChannelSetting.channel_id == channel.id))
        ).scalar_one_or_none()
        if setting is None:
            setting = ChannelSetting(channel_id=channel.id)
            self.session.add(setting)
            await self.session.flush()

        # `pending` means "queued, not started". A resumed workflow is already past planning, so
        # labelling it `planning` made a run that died in visuals look like it never left planning.
        # The CHECK constraint allows only stage names, so start it at the stage it resumes from.
        if workflow.status == "pending":
            workflow.status = workflow.stage if workflow.stage in STAGES else "planning"
        workflow.started_at = workflow.started_at or datetime.now(UTC)
        await self.ensure_budget(workflow.workspace_id)

        try:
            # Resume semantics: every stage is skipped when its output is already persisted, so a
            # retry pays only for the work that actually failed. Without this, a retry re-bills
            # planning, script, storyboard, TTS and visuals — the most expensive stages.
            project = await self._reuse_project(workflow)
            if project is None:
                project = await self._stage_planning(workflow, channel, setting)
            else:
                await self._log_reused(workflow, "planning", project.title)

            script = await self._reuse_script(project)
            if script is None:
                script = await self._stage_script(workflow, channel, project)
            else:
                await self._log_reused(workflow, "script", f"v{script.version}")

            storyboard = await self._reuse_storyboard(script)
            if storyboard is None:
                storyboard = await self._stage_storyboard(workflow, channel, project, script, setting)
            else:
                await self._log_reused(workflow, "storyboard", f"v{storyboard.version}")

            scenes = list(
                (
                    await self.session.execute(
                        select(Scene).where(Scene.storyboard_id == storyboard.id).order_by(Scene.index)
                    )
                ).scalars()
            )
            if not await self._has_narration(project, scenes):
                await self._stage_voice(workflow, channel, project, scenes)
            else:
                await self._log_reused(workflow, "voice", f"{len(scenes)} scenes")

            if not await self._has_visuals(project, scenes, channel):
                await self._stage_visual(workflow, channel, project, scenes, setting)
            else:
                await self._log_reused(workflow, "visuals", f"{len(scenes)} scenes")

            render = await self._stage_render(workflow, channel, project, storyboard)

            report = await self._reuse_qc(project, render)
            if report is None:
                report = await self._stage_qc(workflow, channel, project, setting, scenes, render)
            else:
                await self._log_reused(workflow, "quality control", f"score {report.score:.0f}")

            metadata = await self._reuse_seo(project)
            if metadata is None:
                metadata = await self._stage_seo(workflow, channel, project, setting)
            else:
                await self._log_reused(workflow, "metadata", "already generated")

            await self._stage_approval(workflow, channel, setting, project, report, stop_at_approval)
            if stop_at_approval:
                raise ApprovalPending()
            await self._stage_publish(workflow, channel, project, metadata, render)
            workflow.status = "published"
            workflow.stage = "published"
            workflow.progress = 100
            await self.repo.log(workflow.id, "Workflow finished: published")
        except ApprovalPending:
            await self.session.commit()
            return "awaiting_approval"
        except WorkflowCancelled:
            workflow.status = "cancelled"
            workflow.completed_at = datetime.now(UTC)
            await self.session.commit()
            return "cancelled"
        except Exception as exc:
            workflow.status = "failed"
            workflow.error_message = f"{type(exc).__name__}: {exc}"[:2000]
            workflow.completed_at = datetime.now(UTC)
            await self.repo.log(workflow.id, f"Workflow failed: {exc}", level="error")
            if workflow.project_id:
                project = await self.session.get(ContentProject, workflow.project_id)
                if project:
                    project.status = "failed"
            await self.session.commit()
            log.exception("workflow failed %s", workflow.workflow_key)
            raise

        workflow.completed_at = datetime.now(UTC)
        await self.session.commit()
        return "completed"

    async def _performance_notes(self, workspace_id: uuid.UUID, channel_id: uuid.UUID) -> list[str]:
        """Feed recent analytics back into planning (only when measured data exists)."""
        from app.models import AnalyticsSnapshot, YouTubeVideo

        rows = (
            await self.session.execute(
                select(YouTubeVideo.title, AnalyticsSnapshot.views)
                .join(AnalyticsSnapshot, AnalyticsSnapshot.youtube_video_id == YouTubeVideo.youtube_video_id)
                .where(YouTubeVideo.workspace_id == workspace_id, YouTubeVideo.channel_id == channel_id)
                .order_by(AnalyticsSnapshot.captured_at.desc())
                .limit(5)
            )
        ).all()
        return [f"{title} — {views} views" for title, views in rows]

    async def _check_cancel(self, workflow: ProductionWorkflow) -> None:
        status = await self.session.scalar(
            select(ProductionWorkflow.status).where(ProductionWorkflow.id == workflow.id)
        )
        if status == "cancelled":
            raise WorkflowCancelled("Workflow cancelled by operator")

    async def _stage_planning(self, workflow, channel, setting) -> ContentProject:
        await self._check_cancel(workflow)
        await self.ensure_budget(workflow.workspace_id)
        await self.repo.set_stage(workflow, "planning", "content_planner")
        await self.session.commit()

        recent = list(
            (
                await self.session.execute(
                    select(ContentProject.title)
                    .where(ContentProject.channel_id == channel.id, ContentProject.deleted_at.is_(None))
                    .order_by(ContentProject.created_at.desc())
                    .limit(12)
                )
            ).scalars()
        )
        payload = PlannerInput(
            channel_name=channel.name,
            niche=channel.niche,
            audience=channel.audience,
            language=channel.language,
            recent_titles=recent,
            target_duration_s=int(setting.target_duration_seconds or 240),
            banned_topics=list(setting.banned_topics or []),
            performance_notes=await self._performance_notes(workflow.workspace_id, channel.id),
        )
        self.step += 1
        ctx, result = await self._run_agent(
            workflow, channel, "content_planner", payload, stage="planning"
        )
        brief = result.output
        task = await self.repo.start_task(workflow, "content_planner", self.step, payload.model_dump())
        await self.repo.finish_task(task, output=brief.model_dump(), usage=result.usage,
                                    provider=result.provider, model=result.model)

        idea = ContentIdea(
            workspace_id=workflow.workspace_id,
            channel_id=channel.id,
            title=brief.title,
            topic=brief.topic,
            angle=brief.angle,
            audience=brief.audience,
            brief=brief.brief,
            rationale=brief.rationale,
            category=brief.category,
            estimated_duration_s=brief.estimated_duration_s,
            score=88,
            source="agent",
            selected=True,
        )
        self.session.add(idea)
        await self.session.flush()
        project = ContentProject(
            workspace_id=workflow.workspace_id,
            channel_id=channel.id,
            idea_id=idea.id,
            title=brief.title,
            status="in_production",
            language=channel.language,
            format=channel.video_format,
            estimated_duration_s=brief.estimated_duration_s,
            metadata_json={"brief": brief.model_dump()},
        )
        self.session.add(project)
        await self.session.flush()
        workflow.project_id = project.id
        workflow.input_json = {**(workflow.input_json or {}), "brief": brief.model_dump()}
        await self.repo.log(workflow.id, f"Planned: “{brief.title}”", task_id=task.id)
        await self.session.commit()
        return project

    async def _stage_script(self, workflow, channel, project) -> Script:
        await self._check_cancel(workflow)
        brief = (workflow.input_json or {}).get("brief", {})
        setting = await self.session.scalar(
            select(ChannelSetting).where(ChannelSetting.channel_id == channel.id)
        )
        chars = await self._characters(channel)
        payload = ScriptBrief(
            title=brief.get("title", project.title),
            topic=brief.get("topic", project.title),
            brief=brief.get("brief", ""),
            creative_direction=brief.get("creative_direction", ""),
            audience=channel.audience,
            language=channel.language,
            target_duration_s=int(project.estimated_duration_s or 240),
            character_names=[c.name for c in chars],
            guidelines=setting.content_guidelines if setting else None,
        )
        self.step += 1
        ctx, result = await self._run_agent(
            workflow, channel, "script_writer", payload, stage="script_generation"
        )
        task = await self.repo.start_task(workflow, "script_writer", self.step, payload.model_dump())
        draft = result.output
        narration = draft.narration
        words = word_count(narration)
        script = Script(
            project_id=project.id,
            version=1,
            title=draft.title,
            description=draft.description,
            narration=narration,
            hook=draft.hook,
            word_count=words,
            estimated_duration_s=int(words / 2.6),
            is_current=True,
            generated_by="script_writer",
            raw_output=draft.model_dump(),
        )
        self.session.add(script)
        await self.session.flush()
        await self.repo.finish_task(task, output=draft.model_dump(), usage=result.usage,
                                    provider=result.provider, model=result.model)
        await self.repo.log(workflow.id, f"Script written: {len(draft.scenes)} scenes · {words} words",
                            task_id=task.id)
        await self.session.commit()
        return script

    async def _characters(self, channel: Channel):  # type: ignore[no-untyped-def]
        from app.models import ChannelCharacter

        return list(
            (
                await self.session.execute(
                    select(ChannelCharacter).where(ChannelCharacter.channel_id == channel.id)
                )
            ).scalars()
        )

    async def _stage_storyboard(self, workflow, channel, project, script: Script, setting) -> Storyboard:
        await self._check_cancel(workflow)
        chars = await self._characters(channel)
        scenes = [ScriptScene.model_validate(s) for s in (script.raw_output or {}).get("scenes", [])]
        if not scenes:
            from app.agents.contracts import ScriptDraft

            scenes = ScriptDraft.model_validate(script.raw_output).scenes
        payload = StoryboardInput(
            title=script.title,
            style=setting.visual_style,
            aspect_ratio=channel.video_format,
            characters=[
                {"name": c.name, "visual_prompt": c.visual_prompt, "role": c.role} for c in chars
            ],
            palette=list((setting.palette or {}).values()),
            scenes=scenes,
        )
        self.step += 1
        ctx, result = await self._run_agent(
            workflow, channel, "storyboard", payload, stage="storyboarding"
        )
        task = await self.repo.start_task(workflow, "storyboard", self.step, payload.model_dump())
        draft = result.output
        board = Storyboard(script_id=script.id, version=1, style_guide=draft.style_guide, is_current=True)
        self.session.add(board)
        await self.session.flush()
        for sb, script_scene in zip(draft.scenes, scenes, strict=False):
            self.session.add(
                Scene(
                    storyboard_id=board.id,
                    index=sb.index,
                    title=script_scene.title,
                    narration=script_scene.narration,
                    dialogue=[d.model_dump() for d in script_scene.dialogue],
                    camera=sb.camera,
                    composition=sb.composition,
                    characters=sb.characters,
                    background=sb.background,
                    action=sb.action,
                    image_prompt=sb.image_prompt,
                    negative_prompt=sb.negative_prompt,
                    sfx=sb.sfx,
                    duration_s=sb.duration_s,
                    transition=sb.transition,
                )
            )
        await self.session.flush()
        await self.repo.finish_task(task, output=draft.model_dump(), usage=result.usage,
                                    provider=result.provider, model=result.model)
        await self.repo.log(workflow.id, f"Storyboard: {len(draft.scenes)} shots planned", task_id=task.id)
        await self.session.commit()
        return board

    async def _stage_voice(self, workflow, channel, project, scenes: list[Scene]) -> None:
        await self._check_cancel(workflow)
        # Only scenes without usable narration: TTS is billed per character, so a retry must not
        # re-synthesise audio that already exists on disk.
        existing = {
            a.scene_id
            for a in (
                await self.session.execute(
                    select(AudioAsset).where(
                        AudioAsset.project_id == project.id, AudioAsset.kind == "narration"
                    )
                )
            ).scalars()
            if float(a.duration_s or 0) > 0 and a.storage_key
        }
        pending = [s for s in scenes if s.id not in existing]
        if not pending:
            await self._log_reused(workflow, "voice", f"{len(scenes)} scenes")
            return
        if len(pending) < len(scenes):
            await self.repo.log(
                workflow.id,
                f"Narration: synthesising {len(pending)} of {len(scenes)} scenes "
                f"({len(scenes) - len(pending)} already present)",
            )
        reqs = [
            VoiceSceneRequest(scene_id=str(s.id), index=s.index, text=s.narration)
            for s in pending
        ]
        voice_override = (workflow.input_json or {}).get("voice") or None
        characters = sum(len(s.narration or "") for s in pending)
        self.step += 1
        ctx, result = await self._run_agent(
            workflow, channel, "voice_generation",
            VoiceRequest(language=project.language, scenes=reqs, voice=voice_override),
            stage="voice_generation",
            extra_params={"project_id": project.id},
        )
        task = await self.repo.start_task(workflow, "voice_generation", self.step,
                                          {"scenes": len(reqs), "characters": characters})
        by_index = {s.index: s for s in scenes}
        from app.models.tts_generation import TTSGeneration

        for vr in result.output.scenes:
            scene = by_index.get(vr.index)
            self.session.add(
                AudioAsset(
                    workspace_id=workflow.workspace_id,
                    project_id=project.id,
                    scene_id=scene.id if scene else None,
                    kind="narration",
                    voice_id=vr.voice_id,
                    model_id=vr.model or None,
                    voice_settings=vr.voice_settings,
                    output_format=vr.output_format or None,
                    provider=vr.provider,
                    storage_key=vr.key,
                    duration_s=vr.duration_s,
                    sample_rate=44100,
                    bytes=vr.bytes,
                    text=scene.narration if scene else None,
                )
            )
            # Production narration is part of the TTS history too — marked as production, never test.
            self.session.add(
                TTSGeneration(
                    workspace_id=workflow.workspace_id,
                    provider=vr.provider,
                    model=vr.model or "",
                    voice_id=vr.voice_id or None,
                    language=project.language,
                    input_text=scene.narration if scene else "",
                    character_count=len(scene.narration or "") if scene else 0,
                    audio_path=vr.key,
                    audio_format=vr.output_format or None,
                    mime_type=vr.mime,
                    audio_duration=vr.duration_s,
                    generation_latency=0,
                    estimated_cost=0,
                    status="succeeded",
                    trigger="production",
                    is_test=False,
                    mode="single",
                    chunk_count=vr.chunk_count,
                    settings=vr.voice_settings,
                    workflow_id=workflow.id,
                    channel_id=channel.id,
                    content_id=project.id,
                    # The workflow key is stable across resumes, so the attempt number must be part
                    # of the key; otherwise the second pass collides with the rows it already wrote
                    # (UNIQUE constraint on tts_generations.request_key).
                    request_key=(
                        f"tts-prod:{workflow.workflow_key}:a{workflow.retry_count or 0}:{vr.index}"
                    ),
                )
            )
        await self.repo.finish_task(task, output=result.output.model_dump(), usage=result.usage,
                                    provider=result.provider, model=result.model)
        await self.repo.log(
            workflow.id,
            f"Narration generated · {result.output.total_duration_s:.1f}s across {len(reqs)} scenes "
            f"({characters:,} characters · {result.provider or 'tts'})",
            task_id=task.id,
            warnings=result.warnings,
        )
        await self.session.commit()

    async def _stage_visual(self, workflow, channel, project, scenes: list[Scene], setting) -> None:
        await self._check_cancel(workflow)
        mode = getattr(channel, "visual_mode", "image")
        if mode == "generative":
            # No image provider call: Remotion composes every scene from its visual prompt, so the
            # stage completes immediately and records why no assets exist.
            task = await self.repo.start_task(
                workflow, "visual_generation", self.step + 1, {"mode": "generative"}
            )
            self.step += 1
            from app.ports.interfaces import Usage as _U

            await self.repo.finish_task(
                task,
                output={"mode": "generative", "scenes": len(scenes)},
                usage=_U(provider="remotion", model="generative", operation="visual", units=len(scenes),
                         unit_label="scene"),
                provider="remotion",
                model="generative",
            )
            await self.repo.log(
                workflow.id,
                f"Visuals: generative mode — Remotion composes {len(scenes)} scenes from prompts",
                task_id=task.id,
            )
            await self.session.commit()
            return
        if mode == "code":
            await self._stage_code_visual(workflow, channel, project, scenes, setting)
            return
        # Only scenes without an image: a retry must not pay for art it already has.
        pending = await self._scenes_missing_visuals(project, scenes, "image")
        if not pending:
            await self._log_reused(workflow, "visuals", f"{len(scenes)} scenes")
            return
        if len(pending) < len(scenes):
            await self.repo.log(
                workflow.id,
                f"Visuals: generating {len(pending)} of {len(scenes)} scenes "
                f"({len(scenes) - len(pending)} already present)",
            )
        reqs = [
            VisualSceneRequest(scene_id=str(s.id), index=s.index, prompt=s.image_prompt,
                               negative_prompt=s.negative_prompt)
            for s in pending
        ]
        self.step += 1
        ctx, result = await self._run_agent(
            workflow, channel, "visual_generation",
            VisualRequest(aspect_ratio=channel.video_format, style_suffix=setting.visual_style, scenes=reqs),
            stage="visual_generation",
        )
        task = await self.repo.start_task(workflow, "visual_generation", self.step, {"scenes": len(reqs)})
        by_index = {s.index: s for s in scenes}
        for vr in result.output.scenes:
            scene = by_index.get(vr.index)
            self.session.add(
                MediaAsset(
                    workspace_id=workflow.workspace_id,
                    project_id=project.id,
                    scene_id=scene.id if scene else None,
                    kind="image",
                    storage_key=vr.key,
                    mime_type="image/png",
                    width=vr.width,
                    height=vr.height,
                    bytes=vr.bytes,
                    checksum_sha256=vr.checksum,
                    provider=vr.provider,
                    model=vr.model,
                    prompt=scene.image_prompt if scene else None,
                )
            )
        await self.repo.finish_task(task, output=result.output.model_dump(), usage=result.usage,
                                    provider=result.provider, model=result.model)
        await self.repo.log(workflow.id, f"Visuals generated · {len(reqs)} assets", task_id=task.id,
                            warnings=result.warnings)
        await self.session.commit()

    async def _stage_code_visual(self, workflow, channel, project, scenes, setting) -> None:
        """Write one HTML/CSS/GSAP scene per storyboard scene with the LLM (``visual_mode="code"``).

        Each accepted scene is stored as a small JSON programme in object storage and recorded as a
        ``MediaAsset(kind="code")``; scenes that fail validation after the repair round are stored
        with ``status="fallback"`` and no asset, so the renderer uses the generative backdrop for
        them instead of failing the whole video.
        """
        import json as _json
        import uuid as _uuid

        from app.agents.code_scene import CodeSceneRequest, CodeSceneRequestBatch
        from app.models import RenderJob
        from app.services.scene_code import is_renderable

        width, height = _resolution(channel.video_format)
        template_id = (workflow.input_json or {}).get("template") or "youtube-editorial"
        from app.services import storyboard as _sb
        from app.services.storyboard import TEMPLATES

        template = TEMPLATES.get(template_id, TEMPLATES[_sb.DEFAULT_TEMPLATE])
        fps = int((workflow.input_json or {}).get("fps") or 30)
        # Only scenes without code: a retry must not re-bill the LLM for scenes already written.
        pending = await self._scenes_missing_visuals(project, scenes, "code")
        if not pending:
            await self._log_reused(workflow, "visuals", f"{len(scenes)} scenes")
            return
        # A render already in flight is the real answer: another workflow is mid-compute on exactly
        # these scenes. Rewriting them would pay the LLM again and change the frames under a running
        # encoder. Wait for that render instead.
        active = await self.session.scalar(
            select(RenderJob)
            .where(
                RenderJob.project_id == project.id,
                RenderJob.status.in_(ACTIVE_RENDER_STATES),
            )
            .order_by(RenderJob.queued_at.desc())
            .limit(1)
        )
        if active is not None:
            await self.repo.log(
                workflow.id,
                f"Visuals: skipped — a render is already in progress ({active.status} "
                f"{active.progress}%), so these scenes are already written",
            )
            await self.session.commit()
            return
        if len(pending) < len(scenes):
            await self.repo.log(
                workflow.id,
                f"Visuals: writing {len(pending)} of {len(scenes)} scenes "
                f"({len(scenes) - len(pending)} already coded)",
            )
        # Neighbouring lines give the shot a job: set up what the next line pays off.
        ordered = sorted(scenes, key=lambda s: s.index)
        previous = {
            s.index: (ordered[i - 1].narration or "") for i, s in enumerate(ordered) if i > 0
        }
        following = {
            s.index: (ordered[i + 1].narration or "")
            for i, s in enumerate(ordered)
            if i + 1 < len(ordered)
        }
        reqs = [
            CodeSceneRequest(
                scene_id=str(s.id),
                index=s.index,
                title=s.title or f"Scene {s.index}",
                prompt=s.image_prompt or s.narration or "",
                narration=s.narration or "",
                duration_s=float(s.duration_s or 5),
                width=width,
                height=height,
                fps=fps,
                accent=getattr(channel, "accent_color", None) or template["accent"],
                background=template["background"],
                ink=template["ink"],
                palette=[template["accent"], template["background"], template["ink"]],
                previous_narration=previous.get(s.index, ""),
                next_narration=following.get(s.index, ""),
            )
            for s in pending
        ]
        self.step += 1
        ctx, result = await self._run_agent(
            workflow,
            channel,
            "code_scene",
            CodeSceneRequestBatch(
                niche=getattr(channel, "niche", "") or "stories",
                audience=getattr(channel, "audience", "") or "general",
                style_suffix=getattr(setting, "visual_style", "") or "",
                scenes=reqs,
            ),
            stage="visual_generation",
            extra_params={"fps": fps, "template": template_id},
        )
        storage = ctx.require_storage()
        task = await self.repo.start_task(
            workflow, "visual_generation", self.step, {"mode": "code", "scenes": len(reqs)}
        )
        by_index = {s.index: s for s in scenes}
        written = 0
        fallbacks: list[int] = []
        for scene_result in result.output.scenes:
            scene = by_index.get(scene_result.index)
            if not is_renderable(scene_result.code):
                fallbacks.append(scene_result.index)
                continue
            key = (
                f"p/{project.id}/code/scene-{scene_result.index:02d}-"
                f"{scene_result.checksum[:12] or _uuid.uuid4().hex}.json"
            )
            stored = await storage.put_bytes(
                key,
                _json.dumps(scene_result.code, ensure_ascii=False).encode("utf-8"),
                "application/json",
            )
            self.session.add(
                MediaAsset(
                    workspace_id=workflow.workspace_id,
                    project_id=project.id,
                    scene_id=scene.id if scene else None,
                    kind="code",
                    storage_key=stored.key,
                    mime_type="application/json",
                    bytes=stored.bytes,
                    checksum_sha256=stored.checksum_sha256,
                    provider=result.provider,
                    model=result.model,
                    license="ai-generated",
                    prompt=scene.image_prompt if scene else None,
                )
            )
            written += 1
        await self.repo.finish_task(
            task,
            output={**result.output.model_dump(), "written": written, "fallback": fallbacks},
            usage=result.usage,
            provider=result.provider,
            model=result.model,
        )
        warnings = list(result.warnings)
        await self.repo.log(
            workflow.id,
            f"Visuals: code mode — {written}/{len(reqs)} scenes written by {result.model or 'LLM'}"
            + (f", {len(fallbacks)} fall back to generative" if fallbacks else ""),
            task_id=task.id,
            warnings=warnings,
        )
        await self.session.commit()

    async def _stage_render(self, workflow, channel, project, storyboard: Storyboard) -> VideoRender:
        """Queue a Remotion render and wait for the worker to finish it.

        The API never runs Remotion: this stage writes a durable ``RenderJob`` (structured scenes and
        asset references), commits, then polls the record so workflow progress mirrors the render.
        The worker (``python -m app.worker``) executes it through ``renderMedia()``.
        """
        await self._check_cancel(workflow)
        from app.models import RenderJob
        from app.services import render as render_svc
        from app.services.render import engine_status

        # Resume path: a previous pass already produced this project's video. Reuse it instead of
        # rendering again — otherwise every resume would pay the full render cost and could then
        # fail on a stale job row from the earlier attempt.
        existing = await self.session.scalar(
            select(VideoRender)
            .where(
                VideoRender.project_id == project.id,
                VideoRender.status == "ready",
                VideoRender.storage_key.is_not(None),
            )
            .order_by(VideoRender.completed_at.desc())
            .limit(1)
        )
        if existing is not None and existing.storage_key:
            await self.repo.log(
                workflow.id,
                f"Render reused · {existing.resolution} · {existing.duration_s or 0:.1f}s "
                "(already produced by an earlier attempt)",
            )
            await self.session.commit()
            return existing

        # A render already in flight for this project is not a reason to start another one. Before
        # this guard, a retry queued a second job while the first was still encoding: both ran to
        # completion, doubling CPU and wall time for identical output.
        active = await self.session.scalar(
            select(RenderJob)
            .where(
                RenderJob.project_id == project.id,
                RenderJob.status.in_(ACTIVE_RENDER_STATES),
            )
            .order_by(RenderJob.queued_at.desc())
            .limit(1)
        )
        if active is not None:
            await self.repo.log(
                workflow.id,
                f"Render already in progress · {active.status} {active.progress}% — waiting for it "
                "instead of queueing a duplicate",
            )
            await self.session.commit()
            return await self._await_render(workflow, active.id, project)

        # A VideoRender can outlive its job when the process dies mid-render: the row still says
        # "rendering" while no job is computing anything. Left alone it makes later runs think a
        # render is in flight and wait forever. Close the orphans before queueing.
        await self._fail_orphan_renders(workflow, project)

        engine = await asyncio.to_thread(engine_status)
        if not engine["ok"]:
            raise AppError(
                f"The Remotion engine is not available: {engine['detail']}. "
                "Install Node 18+ and run `npm install` in services/media-engine/remotion, "
                "then start the worker with `python -m app.worker`."
            )

        width, height = _resolution(channel.video_format)
        render = VideoRender(
            workspace_id=workflow.workspace_id,
            project_id=project.id,
            template=(workflow.input_json or {}).get("template") or "youtube-editorial",
            resolution=f"{width}x{height}",
            fps=30,
            status="rendering",
            started_at=datetime.now(UTC),
            progress=2,
        )
        self.session.add(render)
        await self.session.flush()

        self.step += 1
        task = await self.repo.start_task(workflow, "video_production", self.step,
                                          {"render_id": str(render.id)})
        setting = await self.session.scalar(
            select(ChannelSetting).where(ChannelSetting.channel_id == channel.id)
        )
        enqueued = await render_svc.render_service().enqueue(
            self.session,
            project=project,
            template_id=(workflow.input_json or {}).get("template"),
            workflow_id=workflow.id,
            kind="full",
            channel=channel,
            setting=setting,
        )
        job_id = enqueued.job.id
        render.template = enqueued.job.template_id
        await self.session.commit()
        await self.repo.log(
            workflow.id,
            f"Remotion render queued · {enqueued.job.template_id} · "
            f"{enqueued.job.width}x{enqueued.job.height}",
            task_id=task.id,
        )
        await self.session.commit()

        return await self._await_render(workflow, job_id, project, task=task, render=render)

    async def _fail_orphan_renders(self, workflow, project) -> None:
        """Mark ``VideoRender`` rows that no live job backs as failed.

        A render process killed mid-flight leaves ``status="rendering"`` behind. Without this, every
        later attempt sees an "in progress" render, waits, and eventually times out — which is exactly
        how this project ended up with two permanent ``rendering`` rows and no working job.
        """
        from app.models import RenderJob

        stuck = list(
            (
                await self.session.execute(
                    select(VideoRender).where(
                        VideoRender.project_id == project.id,
                        VideoRender.status.in_(("queued", "rendering")),
                    )
                )
            ).scalars()
        )
        if not stuck:
            return
        live_job = await self.session.scalar(
            select(RenderJob.id)
            .where(
                RenderJob.project_id == project.id,
                RenderJob.status.in_(ACTIVE_RENDER_STATES),
            )
            .limit(1)
        )
        if live_job is not None:
            return  # a real job is running; do not touch its render row
        for orphan in stuck:
            orphan.status = "failed"
            orphan.error = "Render process stopped before finishing this video."
        await self.session.commit()
        await self.repo.log(
            workflow.id,
            f"Closed {len(stuck)} orphaned render record(s) left by a stopped process",
        )
        await self.session.commit()

    async def _await_render(
        self, workflow, job_id, project, *, task=None, render=None
    ) -> VideoRender:
        """Poll a render job until it finishes; attach the result to this workflow's task if any.

        Shared by the "we queued it" and "someone else is already rendering it" paths, so a resume
        waits on the existing job instead of starting a second one.
        """
        from app.models import RenderJob

        workflow_pk = workflow.id
        task_pk = task.id if task is not None else None
        if render is None:
            render = await self.session.scalar(
                select(VideoRender)
                .where(VideoRender.project_id == project.id, VideoRender.status == "rendering")
                .order_by(VideoRender.created_at.desc())
                .limit(1)
            )
        render_pk = render.id if render is not None else None

        deadline = datetime.now(UTC).timestamp() + get_settings().remotion_render_timeout_s + 300
        last_reported = -1
        while datetime.now(UTC).timestamp() < deadline:
            await asyncio.sleep(3)
            # A new transaction (not a rollback, which would expire every loaded object) makes the
            # render worker's commits visible; `populate_existing` refreshes the cached rows.
            await self.session.commit()
            workflow_row = await self.session.get(
                ProductionWorkflow, workflow_pk, populate_existing=True
            )
            if workflow_row is not None and workflow_row.status == "cancelled":
                raise WorkflowCancelled("Workflow cancelled by operator")
            job = await self.session.get(RenderJob, job_id, populate_existing=True)
            render = (
                await self.session.get(VideoRender, render_pk, populate_existing=True)
                if render_pk
                else None
            )
            task = (
                await self.session.get(WorkflowTask, task_pk, populate_existing=True)
                if task_pk
                else None
            )
            if job is None:
                raise AppError("Render job disappeared")
            if job.status == "succeeded":
                if render is not None:
                    render.storage_key = job.output_key
                    render.duration_s = job.duration_s
                    render.bytes = job.bytes
                    render.status = "ready"
                    render.progress = 100
                    render.completed_at = datetime.now(UTC)
                usage = _Usage(provider="remotion", model=job.template_id, operation="render",
                               units=1, unit_label="render",
                               latency_ms=int((job.duration_s or 0) * 1000))
                if task is not None:
                    await self.repo.finish_task(task, output={
                        "job_id": str(job.id), "key": job.output_key, "bytes": job.bytes,
                        "duration_s": job.duration_s, "width": job.width, "height": job.height,
                    }, usage=usage, provider="remotion", model=job.template_id)
                await self.repo.log(
                    workflow_pk,
                    f"Render ready · {job.duration_s:.1f}s · {(job.bytes or 0) // 1024} KB · "
                    f"{job.width}x{job.height}",
                    task_id=task_pk,
                )
                await self.session.commit()
                if render is not None:
                    return render
                completed = await self.session.scalar(
                    select(VideoRender)
                    .where(VideoRender.project_id == project.id, VideoRender.status == "ready")
                    .order_by(VideoRender.completed_at.desc())
                    .limit(1)
                )
                if completed is None:
                    raise AppError("Render finished but no video row was recorded")
                return completed
            if job.status in ("failed", "cancelled"):
                if render is not None:
                    render.status = "failed"
                    render.error = job.error_message or job.error_code
                if task is not None:
                    await self.repo.finish_task(task, error=f"{job.error_code}: {job.error_message}")
                await self.session.commit()
                raise AppError(f"Render {job.status}: {job.error_message or job.error_code}")
            if job.progress - last_reported >= 20:
                last_reported = job.progress
                if render is not None:
                    render.progress = job.progress
                await self.repo.log(workflow_pk, f"Rendering {job.progress}% · {job.stage}",
                                    task_id=task_pk)
                await self.session.commit()
        raise AppError(
            "Render timed out waiting for the render worker. Is `python -m app.worker` running?"
        )

    async def _render_validation(self, render: VideoRender) -> dict:
        """Latest output validation for this project's video, if the renderer recorded one."""
        from app.models import RenderJob, RenderValidation

        job = await self.session.scalar(
            select(RenderJob)
            .where(RenderJob.project_id == render.project_id, RenderJob.output_key.is_not(None))
            .order_by(RenderJob.finished_at.desc())
            .limit(1)
        )
        if job is None:
            return {}
        row = await self.session.scalar(
            select(RenderValidation)
            .where(RenderValidation.job_id == job.id)
            .order_by(RenderValidation.created_at.desc())
            .limit(1)
        )
        if row is None:
            return {}
        return {
            "passed": bool(row.passed),
            "checks": list(row.checks or []),
            "issues": list(row.issues or []),
            "width": row.width,
            "height": row.height,
            "fps": row.fps,
            "duration_s": row.duration_s,
            "video_codec": row.video_codec,
            "audio_codec": row.audio_codec,
        }

    async def _stage_qc(self, workflow, channel, project, setting, scenes: list[Scene],
                        render: VideoRender) -> QualityReport:
        await self._check_cancel(workflow)
        images = list(
            (
                await self.session.execute(
                    select(MediaAsset).where(MediaAsset.project_id == project.id, MediaAsset.kind == "image")
                )
            ).scalars()
        )
        audio = list(
            (
                await self.session.execute(
                    select(AudioAsset).where(
                        AudioAsset.project_id == project.id, AudioAsset.kind == "narration"
                    )
                )
            ).scalars()
        )
        img_by_scene = {a.scene_id: a for a in images}
        audio_by_scene = {a.scene_id: a for a in audio}
        script = await self.session.scalar(
            select(Script).where(Script.project_id == project.id, Script.is_current.is_(True))
        )
        payload = QcInput(
            title=project.title,
            audience=channel.audience,            language=project.language,
            made_for_kids=bool(setting.made_for_kids),
            min_score=float(setting.min_quality_score or 85),
            script_text=script.narration if script else "",
            scene_count=len(scenes),
            scenes=[{"index": s.index, "title": s.title} for s in scenes],
            image_checks=[
                {"index": s.index, "present": s.id in img_by_scene,
                 "bytes": img_by_scene[s.id].bytes if s.id in img_by_scene else 0,
                 "width": img_by_scene[s.id].width if s.id in img_by_scene else 0,
                 "height": img_by_scene[s.id].height if s.id in img_by_scene else 0}
                for s in scenes
            ],
            audio_checks=[
                {"index": s.index, "present": s.id in audio_by_scene,
                 "duration_s": float(audio_by_scene[s.id].duration_s) if s.id in audio_by_scene else 0}
                for s in scenes
            ],
            render={"present": bool(render.storage_key), "duration_s": float(render.duration_s or 0),
                    "bytes": render.bytes or 0, "width": 0, "height": 0},
            render_validation=await self._render_validation(render),
            subtitle_chars=len(script.narration) if script else 0,
            word_count=script.word_count if script else 0,
        )
        self.step += 1
        ctx, result = await self._run_agent(
            workflow, channel, "quality_control", payload, stage="quality_checking"
        )
        task = await self.repo.start_task(workflow, "quality_control", self.step, {"scenes": len(scenes)})
        report = result.output
        row = QualityReport(
            workspace_id=workflow.workspace_id,
            workflow_id=workflow.id,
            project_id=project.id,
            render_id=render.id,
            score=report.score,
            approval_status=report.approval_status,
            checks=[c.model_dump() for c in report.checks],
            issues=[i.model_dump() for i in report.issues],
            warnings=report.warnings,
            recommendations=report.recommendations,
            regenerate=report.regenerate,
        )
        self.session.add(row)
        await self.repo.finish_task(task, output=report.model_dump(), usage=result.usage,
                                    provider=result.provider, model=result.model)
        await self.repo.log(workflow.id, f"QC {report.approval_status.upper()} · score {report.score}",
                            task_id=task.id, level="warning" if report.approval_status != "pass" else "info")
        await self.session.commit()
        return row

    async def _stage_seo(self, workflow, channel, project, setting) -> dict:
        await self._check_cancel(workflow)
        script = await self.session.scalar(
            select(Script).where(Script.project_id == project.id, Script.is_current.is_(True))
        )
        brief = (workflow.input_json or {}).get("brief", {})
        payload = SeoInput(
            title=project.title,
            description=script.description if script else "",
            narration=script.narration if script else "",
            audience=channel.audience,
            language=project.language,
            channel_name=channel.name,
            niche=channel.niche,
            made_for_kids=bool(setting.made_for_kids),
        )
        self.step += 1
        ctx, result = await self._run_agent(workflow, channel, "seo_metadata", payload, stage="seo")
        task = await self.repo.start_task(workflow, "seo_metadata", self.step, {"title": project.title})
        meta = result.output
        project.metadata_json = {
            **(project.metadata_json or {}),
            "seo": meta.model_dump(),
            "brief": brief,
        }
        await self.repo.finish_task(task, output=meta.model_dump(), usage=result.usage,
                                    provider=result.provider, model=result.model)
        await self.repo.log(workflow.id, "Metadata & thumbnail concept ready", task_id=task.id)

        # render thumbnail from concept with the image provider
        try:
            from app.agents.media import VisualRequest as VR
            from app.agents.media import VisualSceneRequest as VSR

            self.step += 1
            vctx, vres = await self._run_agent(
                workflow, channel, "visual_generation",
                VR(aspect_ratio="16:9", style_suffix="", scenes=[
                    VSR(scene_id="thumbnail", index=0,
                        prompt=f"Thumbnail: {meta.thumbnail_concept}. Text does not appear in image.")
                ]),
                stage="seo",
            )
            vt = await self.repo.start_task(workflow, "visual_generation", self.step,
                                            {"kind": "thumbnail"})
            thumb = vres.output.scenes[0]
            asset = MediaAsset(
                workspace_id=workflow.workspace_id, project_id=project.id, kind="thumbnail",
                storage_key=thumb.key, mime_type="image/png", width=thumb.width, height=thumb.height,
                bytes=thumb.bytes, checksum_sha256=thumb.checksum, provider=thumb.provider,
                model=thumb.model, prompt=meta.thumbnail_concept,
            )
            self.session.add(asset)
            await self.session.flush()
            project.thumbnail_asset_id = asset.id
            await self.repo.finish_task(vt, output=thumb.model_dump(), usage=vres.usage)
        except Exception as exc:  # noqa: BLE001 - thumbnail is non-blocking
            await self.repo.log(workflow.id, f"Thumbnail generation skipped: {exc}", level="warning")
        await self.session.commit()
        return meta.model_dump()

    async def _stage_approval(self, workflow, channel, setting, project, report: QualityReport,
                              stop: bool) -> None:
        workflow.status = "awaiting_approval"
        workflow.stage = "awaiting_approval"
        workflow.progress = STAGE_PROGRESS["awaiting_approval"]
        needs = setting.approval_mode == "always"
        if setting.approval_mode == "below_score":
            needs = report.score < float(setting.min_quality_score or 85)
        if bool((workflow.input_json or {}).get("force_approval")):
            needs = True
        project.status = "awaiting_approval" if needs else "approved"
        existing = await self.session.scalar(
            select(ApprovalRequest.id).where(
                ApprovalRequest.workflow_id == workflow.id, ApprovalRequest.status == "pending"
            )
        )
        if needs and existing is None:
            render = await self.session.scalar(
                select(VideoRender).where(VideoRender.project_id == project.id)
                .order_by(VideoRender.created_at.desc()).limit(1)
            )
            self.session.add(
                ApprovalRequest(
                    workspace_id=workflow.workspace_id,
                    project_id=project.id,
                    workflow_id=workflow.id,
                    kind="publish",
                    requested_by_agent="quality_control",
                    status="pending",
                    summary={
                        "title": project.title,
                        "qc_score": float(report.score),
                        "qc_status": report.approval_status,
                        "duration_s": float(render.duration_s) if render else None,
                        "cost_usd": float(workflow.cost_usd or 0),
                        "metadata": (project.metadata_json or {}).get("seo", {}),
                    },
                    expires_at=datetime.now(UTC) + timedelta(days=7),
                )
            )
            await self.repo.log(workflow.id, "Awaiting human approval")
        elif not needs:
            await self.repo.log(workflow.id, "Approval not required by channel policy — proceeding")
        await self.session.commit()

    async def _stage_publish(self, workflow, channel, project, metadata: dict, render: VideoRender) -> None:
        await self._check_cancel(workflow)
        privacy = "private"
        scheduled_for = None
        if metadata:
            meta = metadata
        else:
            meta = (project.metadata_json or {}).get("seo", {})
        thumb = None
        if project.thumbnail_asset_id:
            thumb = await self.session.get(MediaAsset, project.thumbnail_asset_id)
        try:
            access_token = await refresh_access_token(self.session, channel)
        except AppError:
            access_token = None
        payload = PublishRequest(
            title=meta.get("title", project.title),
            description=meta.get("description", ""),
            tags=list(meta.get("tags", [])),
            privacy=privacy,
            made_for_kids=bool(meta.get("made_for_kids", True)),
            scheduled_for=scheduled_for,
            video_key=render.storage_key or "",
            thumbnail_key=thumb.storage_key if thumb else None,
        )
        self.step += 1
        ctx, result = await self._run_agent(
            workflow, channel, "publishing", payload, stage="publishing",
            extra_params={"access_token": access_token},
        )
        task = await self.repo.start_task(workflow, "publishing", self.step, {"title": payload.title})
        project.status = "published"
        project.published_at = datetime.now(UTC)
        await self.repo.finish_task(task, output=result.output.model_dump(), usage=result.usage,
                                    provider=result.provider, model=result.model)
        await self.repo.log(workflow.id, f"Published {result.output.url}", task_id=task.id)
        await self.session.commit()


def _resolution(fmt: str) -> tuple[int, int]:
    return {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)}.get(fmt, (1920, 1080))


# --------------------------------------------------------------------------- local runner


class LocalWorkflowRunner:
    """Durable local executor.

    Runs pipelines concurrently up to ``max_concurrent_workflows``. State lives in Postgres;
    a crashed run leaves ``running`` tasks visible and can be resumed with ``retry``.
    """

    def __init__(self, max_concurrency: int = 3) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._tasks: dict[uuid.UUID, asyncio.Task] = {}

    def is_running(self, workflow_id: uuid.UUID) -> bool:
        task = self._tasks.get(workflow_id)
        return task is not None and not task.done()

    def submit(self, workflow_id: uuid.UUID) -> None:
        if self.is_running(workflow_id):
            return
        from app.core.settings import get_settings

        if get_settings().workflow_backend == "temporal":
            from app.workflow.temporal_adapter import submit_temporal

            async def _route() -> None:
                handle_id = await submit_temporal(str(workflow_id))
                if handle_id is None:  # degrade gracefully rather than losing the run
                    await self._run(workflow_id)
                    return
                async with session_scope() as session:
                    row = await session.get(ProductionWorkflow, workflow_id)
                    if row is not None:
                        row.run_id = handle_id
                    await session.commit()

            self._tasks[workflow_id] = asyncio.create_task(_route())
            return
        self._tasks[workflow_id] = asyncio.create_task(self._run(workflow_id))

    def submit_publish(self, workflow_id: uuid.UUID) -> None:
        """Publish an approved workflow (called after an approval decision is committed)."""
        if self.is_running(workflow_id):
            return
        self._tasks[workflow_id] = asyncio.create_task(self._publish(workflow_id))

    async def _publish(self, workflow_id: uuid.UUID) -> None:
        async with self._semaphore:
            async with session_scope() as session:
                pipeline = ProductionPipeline(session)
                try:
                    await pipeline.resume_after_approval(workflow_id)
                except WorkflowCancelled:
                    pass
                except Exception:  # noqa: BLE001 - failure recorded on the workflow row
                    log.exception("publish failed for %s", workflow_id)

    async def _run(self, workflow_id: uuid.UUID) -> None:
        async with self._semaphore:
            async with session_scope() as session:
                pipeline = ProductionPipeline(session)
                try:
                    await pipeline.run(workflow_id, stop_at_approval=True)
                except AppError:
                    pass  # state already persisted
                except Exception:  # noqa: BLE001 - recorded on the workflow row
                    log.exception("local workflow run failed")

    async def resume(self, workflow_id: uuid.UUID) -> None:
        """Continue after approval: publishes using stored render + metadata."""
        self.submit(workflow_id)

    async def shutdown(self) -> None:
        tasks = [t for t in self._tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


runner = LocalWorkflowRunner()
