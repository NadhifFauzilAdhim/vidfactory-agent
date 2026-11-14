"""Content library: projects, storyboards, assets, metadata edits, regeneration, downloads."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage import LocalStorage, get_storage
from app.agents.media import VisualRequest, VisualSceneRequest
from app.api.deps import Principal, get_principal, require_role
from app.container import ProviderFactory
from app.core.errors import AppError, NotFoundError
from app.db.session import get_session
from app.models import (
    ApprovalRequest,
    AudioAsset,
    Channel,
    ChannelSetting,
    ContentProject,
    MediaAsset,
    ProductionWorkflow,
    QualityReport,
    Scene,
    Script,
    Storyboard,
    VideoRender,
)
from app.schemas import (
    AssetOut,
    MetadataUpdate,
    ProjectDetailOut,
    ProjectOut,
    RegenerateRequest,
    SceneOut,
)
from app.workflow.pipeline import WorkflowRepository

router = APIRouter(prefix="/content", tags=["content"])


def _url(key: str | None) -> str | None:
    return get_storage().url_for(key) if key else None


async def _project_out(session: AsyncSession, project: ContentProject) -> ProjectOut:
    channel = await session.get(Channel, project.channel_id)
    qc = await session.scalar(
        select(QualityReport.score)
        .where(QualityReport.project_id == project.id)
        .order_by(QualityReport.created_at.desc())
        .limit(1)
    )
    thumb = None
    if project.thumbnail_asset_id:
        asset = await session.get(MediaAsset, project.thumbnail_asset_id)
        thumb = _url(asset.storage_key) if asset else None
    return ProjectOut(
        id=project.id,
        channel_id=project.channel_id,
        channel_name=channel.name if channel else "—",
        title=project.title,
        status=project.status,
        language=project.language,
        format=project.format,
        estimated_duration_s=project.estimated_duration_s,
        total_cost_usd=float(project.total_cost_usd or 0),
        qc_score=float(qc) if qc is not None else None,
        thumbnail_url=thumb,
        created_at=project.created_at,
        published_at=project.published_at,
    )


@router.get("", response_model=list[ProjectOut])
async def list_projects(
    channel_id: uuid.UUID | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = Query(default=50, le=200),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> list[ProjectOut]:
    query = (
        select(ContentProject)
        .where(
            ContentProject.workspace_id == principal.workspace.id,
            ContentProject.deleted_at.is_(None),
        )
        .order_by(ContentProject.created_at.desc())
        .limit(limit)
    )
    if channel_id:
        query = query.where(ContentProject.channel_id == channel_id)
    if status and status != "all":
        query = query.where(ContentProject.status == status)
    if q:
        query = query.where(ContentProject.title.ilike(f"%{q}%"))
    rows = list((await session.execute(query)).scalars())
    return [await _project_out(session, p) for p in rows]


@router.get("/{project_id}", response_model=ProjectDetailOut)
async def project_detail(
    project_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> ProjectDetailOut:
    project = await _get(session, principal, project_id)
    base = await _project_out(session, project)
    script = await session.scalar(
        select(Script).where(Script.project_id == project.id, Script.is_current.is_(True))
    )
    board = None
    scenes: list[SceneOut] = []
    if script:
        board = await session.scalar(
            select(Storyboard).where(Storyboard.script_id == script.id, Storyboard.is_current.is_(True))
        )
    if board:
        scene_rows = list(
            (
                await session.execute(
                    select(Scene).where(Scene.storyboard_id == board.id).order_by(Scene.index)
                )
            ).scalars()
        )
        images = {
            a.scene_id: a
            for a in (
                await session.execute(
                    select(MediaAsset).where(
                        MediaAsset.project_id == project.id,
                        MediaAsset.kind == "image",
                        MediaAsset.deleted_at.is_(None),
                    )
                )
            ).scalars()
        }
        audio = {
            a.scene_id: a
            for a in (
                await session.execute(
                    select(AudioAsset).where(
                        AudioAsset.project_id == project.id, AudioAsset.kind == "narration"
                    )
                )
            ).scalars()
        }
        code = {
            a.scene_id: a
            for a in (
                await session.execute(
                    select(MediaAsset).where(
                        MediaAsset.project_id == project.id,
                        MediaAsset.kind == "code",
                        MediaAsset.deleted_at.is_(None),
                    )
                )
            ).scalars()
        }
        for s in scene_rows:
            img = images.get(s.id)
            aud = audio.get(s.id)
            scenes.append(
                SceneOut(
                    id=s.id,
                    index=s.index,
                    title=s.title,
                    narration=s.narration,
                    dialogue=list(s.dialogue or []),
                    camera=s.camera,
                    composition=s.composition,
                    characters=list(s.characters or []),
                    background=s.background,
                    action=s.action,
                    image_prompt=s.image_prompt,
                    sfx=list(s.sfx or []),
                    duration_s=float(s.duration_s or 0),
                    transition=s.transition,
                    image_url=_url(img.storage_key) if img else None,
                    audio_url=_url(aud.storage_key) if aud else None,
                    code_url=f"/api/v1/content/{project.id}/scenes/{s.index}/code"
                    if s.id in code
                    else None,
                    audio_duration_s=float(aud.duration_s) if aud else None,
                    audio_provider=aud.provider if aud else None,
                    audio_model=aud.model_id if aud else None,
                    audio_voice_id=aud.voice_id if aud else None,
                )
            )
    assets = list(
        (
            await session.execute(
                select(MediaAsset)
                .where(MediaAsset.project_id == project.id, MediaAsset.deleted_at.is_(None))
                .order_by(MediaAsset.created_at)
            )
        ).scalars()
    )
    renders = list(
        (
            await session.execute(
                select(VideoRender)
                .where(VideoRender.project_id == project.id)
                .order_by(VideoRender.created_at.desc())
            )
        ).scalars()
    )
    qc = await session.scalar(
        select(QualityReport)
        .where(QualityReport.project_id == project.id)
        .order_by(QualityReport.created_at.desc())
        .limit(1)
    )
    workflow = await session.scalar(
        select(ProductionWorkflow)
        .where(ProductionWorkflow.project_id == project.id)
        .order_by(ProductionWorkflow.created_at.desc())
        .limit(1)
    )
    approval = await session.scalar(
        select(ApprovalRequest)
        .where(ApprovalRequest.project_id == project.id)
        .order_by(ApprovalRequest.created_at.desc())
        .limit(1)
    )
    return ProjectDetailOut(
        **base.model_dump(),
        metadata=dict(project.metadata_json or {}),
        script=(
            {
                "id": str(script.id),
                "title": script.title,
                "description": script.description,
                "hook": script.hook,
                "narration": script.narration,
                "word_count": script.word_count,
                "estimated_duration_s": script.estimated_duration_s,
                "version": script.version,
            }
            if script
            else None
        ),
        scenes=scenes,
        assets=[
            AssetOut(
                id=a.id, kind=a.kind, url=get_storage().url_for(a.storage_key),
                width=a.width, height=a.height, bytes=a.bytes, provider=a.provider,
                created_at=a.created_at,
            )
            for a in assets
        ],
        renders=[
            {
                "id": str(r.id),
                "status": r.status,
                "url": _url(r.storage_key),
                "duration_s": float(r.duration_s or 0),
                "bytes": r.bytes,
                "resolution": r.resolution,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in renders
        ],
        qc=(
            {
                "score": float(qc.score),
                "status": qc.approval_status,
                "checks": qc.checks,
                "issues": qc.issues,
                "warnings": qc.warnings,
                "recommendations": qc.recommendations,
                "regenerate": qc.regenerate,
                "created_at": qc.created_at.isoformat() if qc.created_at else None,
            }
            if qc
            else None
        ),
        workflow_id=workflow.id if workflow else None,
        approval_id=approval.id if approval else None,
    )


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_metadata(
    project_id: uuid.UUID,
    payload: MetadataUpdate,
    principal: Principal = Depends(require_role("editor")),
    session: AsyncSession = Depends(get_session),
) -> ProjectOut:
    project = await _get(session, principal, project_id)
    if payload.title:
        project.title = payload.title
    if payload.seo is not None:
        project.metadata_json = {**(project.metadata_json or {}), "seo": payload.seo}
    await session.commit()
    return await _project_out(session, project)


@router.post("/{project_id}/regenerate", response_model=ProjectOut)
async def regenerate(
    project_id: uuid.UUID,
    payload: RegenerateRequest,
    principal: Principal = Depends(require_role("editor")),
    session: AsyncSession = Depends(get_session),
) -> ProjectOut:
    project = await _get(session, principal, project_id)
    # Script or storyboard regeneration needs a fresh pipeline run on the channel so every downstream
    # asset and ID stays consistent; scene-level regeneration has its own endpoint.
    channel = await session.get(Channel, project.channel_id)
    if channel is None:
        raise NotFoundError("Channel not found")
    repo = WorkflowRepository(session)
    wf = await repo.create(
        workspace_id=principal.workspace.id, channel_id=channel.id,
        trigger="regenerate", force_approval=True,
    )
    await repo.log(wf.id, f"Regeneration for “{project.title}” ({payload.target})")
    await session.commit()
    from app.workflow.pipeline import runner

    runner.submit(wf.id)
    return await _project_out(session, project)


@router.post("/{project_id}/scenes/{scene_index}/regenerate", response_model=ProjectOut)
async def regenerate_scene(
    project_id: uuid.UUID,
    scene_index: int,
    target: str = Query(default="image", pattern="^(image|audio)$"),
    principal: Principal = Depends(require_role("editor")),
    session: AsyncSession = Depends(get_session),
) -> ProjectOut:
    project = await _get(session, principal, project_id)
    channel = await session.get(Channel, project.channel_id)
    if channel is None:
        raise NotFoundError("Channel not found")
    scene = await session.scalar(
        select(Scene)
        .join(Storyboard, Storyboard.id == Scene.storyboard_id)
        .join(Script, Script.id == Storyboard.script_id)
        .where(Script.project_id == project.id, Scene.index == scene_index)
    )
    if scene is None:
        raise NotFoundError("Scene not found")
    if target == "audio":
        from app.agents.media import VoiceGenerationAgent, VoiceRequest, VoiceSceneRequest
        from app.models import AudioAsset

        factory = ProviderFactory(session)
        ctx = await factory.build_context(
            workspace_id=principal.workspace.id, agent_key="voice_generation", channel=channel,
            project_id=project.id, request_key=f"regen-audio-{scene.id}",
        )
        agent = VoiceGenerationAgent()
        result = await agent.run(
            ctx,
            VoiceRequest(language=project.language,
                         scenes=[VoiceSceneRequest(scene_id=str(scene.id), index=scene.index,
                                                   text=scene.narration)]),
        )
        vr = result.output.scenes[0]
        existing = await session.scalar(
            select(AudioAsset).where(AudioAsset.scene_id == scene.id, AudioAsset.kind == "narration")
        )
        if existing:
            existing.storage_key = vr.key
            existing.duration_s = vr.duration_s
            existing.provider = vr.provider
        else:
            session.add(
                AudioAsset(
                    workspace_id=principal.workspace.id, project_id=project.id, scene_id=scene.id,
                    kind="narration", storage_key=vr.key, duration_s=vr.duration_s,
                    provider=vr.provider, voice_id=vr.voice_id, text=scene.narration,
                )
            )
    else:
        factory = ProviderFactory(session)
        setting = await session.scalar(
            select(ChannelSetting).where(ChannelSetting.channel_id == channel.id)
        )
        ctx = await factory.build_context(
            workspace_id=principal.workspace.id, agent_key="visual_generation", channel=channel,
            project_id=project.id, request_key=f"regen-image-{scene.id}",
        )
        from app.agents.media import VisualGenerationAgent

        agent = VisualGenerationAgent()
        result = await agent.run(
            ctx,
            VisualRequest(
                aspect_ratio=channel.video_format,
                style_suffix=setting.visual_style if setting else "",
                scenes=[VisualSceneRequest(scene_id=str(scene.id), index=scene.index,
                                           prompt=scene.image_prompt,
                                           negative_prompt=scene.negative_prompt)],
            ),
        )
        vr = result.output.scenes[0]
        existing = await session.scalar(
            select(MediaAsset).where(
                MediaAsset.scene_id == scene.id, MediaAsset.kind == "image",
                MediaAsset.deleted_at.is_(None),
            )
        )
        if existing:
            existing.deleted_at = datetime.now(UTC)
        session.add(
            MediaAsset(
                workspace_id=principal.workspace.id, project_id=project.id, scene_id=scene.id,
                kind="image", storage_key=vr.key, width=vr.width, height=vr.height, bytes=vr.bytes,
                checksum_sha256=vr.checksum, provider=vr.provider, model=vr.model,
                prompt=scene.image_prompt,
            )
        )
    await session.commit()
    return await _project_out(session, project)


@router.get("/{project_id}/scenes/{scene_index}/code")
async def scene_code(
    project_id: uuid.UUID,
    scene_index: int,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The scene programme the LLM wrote (HTML/CSS/GSAP), so it can be reviewed before publishing.

    Read-only: the operator sees exactly what the renderer runs. Editing goes through a regenerate.
    """
    project = await _get(session, principal, project_id)
    board = project.storyboard
    if board is None:
        raise NotFoundError("No storyboard for this project")
    scene = await session.scalar(
        select(Scene).where(Scene.storyboard_id == board.id, Scene.index == scene_index)
    )
    if scene is None:
        raise NotFoundError(f"Scene {scene_index} not found")
    asset = await session.scalar(
        select(MediaAsset).where(
            MediaAsset.scene_id == scene.id,
            MediaAsset.kind == "code",
            MediaAsset.deleted_at.is_(None),
        )
    )
    if asset is None:
        raise NotFoundError(f"Scene {scene_index} has no code")
    raw = await get_storage().get_bytes(asset.storage_key)
    try:
        code = json.loads(raw)
    except ValueError as exc:
        raise AppError("Stored scene code is not valid JSON") from exc
    return {
        "scene_index": scene_index,
        "runtime": code.get("runtime"),
        "html": code.get("html", ""),
        "css": code.get("css", ""),
        "js": code.get("js", ""),
        "model": asset.model,
        "provider": asset.provider,
    }


@router.delete("/{project_id}", status_code=204)
async def archive_project(
    project_id: uuid.UUID,
    principal: Principal = Depends(require_role("editor")),
    session: AsyncSession = Depends(get_session),
) -> None:
    project = await _get(session, principal, project_id)
    project.deleted_at = datetime.now(UTC)
    project.status = "archived"
    await session.commit()


@router.get("/{project_id}/download")
async def download_render(
    project_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    await _get(session, principal, project_id)
    render = await session.scalar(
        select(VideoRender)
        .where(VideoRender.project_id == project_id, VideoRender.status == "ready")
        .order_by(VideoRender.created_at.desc())
        .limit(1)
    )
    if render is None or not render.storage_key:
        raise NotFoundError("No rendered video for this project")
    storage = get_storage()
    if not isinstance(storage, LocalStorage):
        raise AppError("Downloads are served directly from R2 in this deployment")
    safe_name = "".join(c for c in render.id.hex[:8])
    return FileResponse(
        storage.root / render.storage_key,
        media_type="video/mp4",
        filename=f"{safe_name}.mp4",
        headers={"Cache-Control": "private, no-store"},
    )


async def _get(session: AsyncSession, principal: Principal, project_id: uuid.UUID) -> ContentProject:
    project = await session.scalar(
        select(ContentProject).where(
            ContentProject.id == project_id,
            ContentProject.workspace_id == principal.workspace.id,
            ContentProject.deleted_at.is_(None),
        )
    )
    if project is None:
        raise NotFoundError("Project not found")
    return project


__all__ = ["router"]
