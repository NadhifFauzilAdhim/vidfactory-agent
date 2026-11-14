"""Builds the Remotion storyboard spec from stored content.

This is the seam between the AI pipeline and the video engine: agents produce structured scenes and
assets; this module turns them into the JSON a composition consumes. It contains no rendering logic
and no prompt text — only references to generated assets (storage keys), measured durations and
presentation choices that come from the channel and the template.

Every field maps 1:1 to `services/media-engine/remotion/src/types.ts`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AudioAsset,
    Channel,
    ChannelSetting,
    ContentProject,
    MediaAsset,
    Scene,
    Storyboard,
)

#: Template ids implemented by the Remotion project (`src/catalogue.tsx`).
TEMPLATES: dict[str, dict[str, Any]] = {
    "youtube-editorial": {
        "label": "YouTube · Editorial",
        "formats": ("16:9",),
        "width": 1920,
        "height": 1080,
        "accent": "#D6FF45",
        "background": "#FFFFFF",
        "ink": "#111111",
        "caption_mode": "lower-third",
        "transition": "fade",
        "transition_s": 0.6,
    },
    "childrens-story": {
        "label": "Children's story",
        "formats": ("16:9",),
        "width": 1920,
        "height": 1080,
        "accent": "#FFB4A2",
        "background": "#FFF6EC",
        "ink": "#2B2B2B",
        "caption_mode": "lower-third",
        "transition": "zoom",
        "transition_s": 0.75,
    },
    "educational": {
        "label": "Educational",
        "formats": ("16:9",),
        "width": 1920,
        "height": 1080,
        "accent": "#7FD1FF",
        "background": "#0E1B2A",
        "ink": "#F4F7FA",
        "caption_mode": "subtitle",
        "transition": "wipe",
        "transition_s": 0.5,
    },
    "storytelling": {
        "label": "Storytelling",
        "formats": ("16:9",),
        "width": 1920,
        "height": 1080,
        "accent": "#E8C36A",
        "background": "#0B0B0B",
        "ink": "#F5F1E8",
        "caption_mode": "overlay-title",
        "transition": "fade",
        "transition_s": 1.1,
    },
    "shorts": {
        "label": "Short-form · vertical",
        "formats": ("9:16",),
        "width": 1080,
        "height": 1920,
        "accent": "#FF4D2E",
        "background": "#111111",
        "ink": "#FFFFFF",
        "caption_mode": "subtitle",
        "transition": "slide-up",
        "transition_s": 0.35,
    },
    "youtube-shorts": {
        "label": "YouTube Shorts",
        "formats": ("9:16",),
        "width": 1080,
        "height": 1920,
        "accent": "#D6FF45",
        "background": "#111111",
        "ink": "#FFFFFF",
        "caption_mode": "subtitle",
        "transition": "slide-up",
        "transition_s": 0.4,
    },
}

DEFAULT_TEMPLATE = "youtube-editorial"
TRANSITIONS = ("fade", "cut", "slide-left", "slide-up", "wipe", "iris", "zoom")
KEN_BURNS = ("in", "out", "pan-left", "pan-right", "none")
ENTRANCES = ("fade", "rise", "slide", "scale", "typewriter", "none")


@dataclass(slots=True)
class RenderScene:
    scene: Scene
    image: MediaAsset | None
    audio: AudioAsset | None
    #: LLM-authored scene programme (HTML/CSS/GSAP) when the channel runs in ``code`` mode.
    code: MediaAsset | None = None

    @property
    def duration_s(self) -> float:
        """Narration length wins over the storyboard estimate so speech is never clipped."""
        if self.audio is not None and float(self.audio.duration_s or 0) > 0:
            return round(float(self.audio.duration_s) + 0.35, 3)
        return round(float(self.scene.duration_s or 5), 3)


@dataclass(slots=True)
class StoryboardPlan:
    spec: dict[str, Any]
    assets: list[dict[str, str]]
    scene_keys: list[str]
    duration_s: float
    width: int
    height: int
    fps: int
    template_id: str


def template_for(channel_format: str, requested: str | None = None) -> str:
    """Pick a template that matches the channel's aspect ratio unless one was requested."""
    if requested and requested in TEMPLATES:
        return requested
    if channel_format in ("9:16", "1:1"):
        return "youtube-shorts"
    return DEFAULT_TEMPLATE


def _transition(value: str | None, fallback: str) -> str:
    return value if value in TRANSITIONS else fallback


def _ken_burns(index: int, strength: str) -> str:
    if strength == "none":
        return "none"
    return ["in", "pan-right", "out", "pan-left"][(index - 1) % 4]


def _elements(index: int, template: str, scene: Scene) -> list[dict[str, Any]]:
    """Reusable visual elements — composition rules per template, still plain data."""
    elements: list[dict[str, Any]] = []
    if template in ("youtube-editorial", "educational"):
        elements.append({"kind": "progress", "position": "bottom"})
    if template == "childrens-story":
        elements.append({"kind": "badge", "text": f"story · {index:02d}", "position": "top-left"})
    if template == "educational" and scene.characters:
        elements.append({"kind": "badge", "text": ", ".join(map(str, scene.characters))[:60],
                         "position": "top-left"})
    return elements


def _asset_url(asset: MediaAsset | AudioAsset | None) -> dict[str, str] | None:
    """Asset reference: a stable key plus the storage key (turned into a local path later).

    The staged file keeps its extension so Remotion's image/audio loaders recognise the format.
    """
    if asset is None:
        return None
    name = Path(asset.storage_key).name
    return {"key": f"a-{asset.id}", "source": asset.storage_key, "name": f"a-{asset.id}-{name}"}


async def load_scenes(
    session: AsyncSession, project: ContentProject, storyboard: Storyboard
) -> list[RenderScene]:
    scenes = list(
        (
            await session.execute(
                select(Scene).where(Scene.storyboard_id == storyboard.id).order_by(Scene.index)
            )
        ).scalars()
    )
    images = {
        a.scene_id: a
        for a in (
            await session.execute(
                select(MediaAsset).where(
                    MediaAsset.project_id == project.id, MediaAsset.kind == "image"
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
                    MediaAsset.project_id == project.id, MediaAsset.kind == "code"
                )
            )
        ).scalars()
    }
    return [
        RenderScene(scene=s, image=images.get(s.id), audio=audio.get(s.id), code=code.get(s.id))
        for s in scenes
    ]


async def load_storyboard(session: AsyncSession, project: ContentProject) -> Storyboard | None:
    """Current storyboard of a project's latest script version."""
    from app.models import Script

    script = await session.scalar(
        select(Script)
        .where(Script.project_id == project.id, Script.is_current.is_(True))
        .order_by(Script.version.desc())
    )
    if script is None:
        return None
    return await session.scalar(
        select(Storyboard)
        .where(Storyboard.script_id == script.id, Storyboard.is_current.is_(True))
        .order_by(Storyboard.version.desc())
    )


def build_spec(
    *,
    project: ContentProject,
    channel: Channel,
    setting: ChannelSetting | None,
    scenes: list[RenderScene],
    template_id: str,
    brand: str = "FACTORY",
    fps: int = 30,
    speed: float = 1.0,
    captions_enabled: bool | None = None,
    visual_mode: str = "image",
    include_intro: bool = True,
    include_outro: bool = True,
    music: dict[str, Any] | None = None,
    width: int | None = None,
    height: int | None = None,
) -> StoryboardPlan:
    template = TEMPLATES.get(template_id, TEMPLATES[DEFAULT_TEMPLATE])
    template_id = template_id if template_id in TEMPLATES else DEFAULT_TEMPLATE
    w = width or template["width"]
    h = height or template["height"]

    assets: list[dict[str, str]] = []
    spec_scenes: list[dict[str, Any]] = []
    for item in scenes:
        s = item.scene
        image_asset = _asset_url(item.image)
        audio_asset = _asset_url(item.audio)
        # Code scenes stage their programme as a JSON asset and are played by the CodeScene
        # component; a scene without stored code keeps the prompt so the renderer can fall back.
        code_asset = _asset_url(item.code) if visual_mode == "code" else None
        if image_asset:
            assets.append(image_asset)
        if code_asset:
            assets.append(code_asset)
        if audio_asset:
            assets.append(audio_asset)
        entry: dict[str, Any] = {
            "id": str(s.id),
            "index": s.index,
            "title": s.title,
            "durationS": item.duration_s,
            "transition": _transition(s.transition, template["transition"]),
            "caption": s.narration or "",
            # Generative scenes need no image: the visual prompt is the scene's script for Remotion.
            "mode": visual_mode,
            "prompt": s.image_prompt or None,
            "animation": {
                "kenBurns": _ken_burns(s.index, "in"),
                "entrance": ENTRANCES[(s.index - 1) % 3],
            },
            "elements": _elements(s.index, template_id, s),
            "sfx": [],
        }
        if image_asset:
            entry["assetKey"] = image_asset["key"]
        if code_asset:
            entry["codeKey"] = code_asset["key"]
        if audio_asset:
            entry["narrationKey"] = audio_asset["key"]
            entry["narrationDurationS"] = round(float(item.audio.duration_s or 0), 3) if item.audio else 0
        spec_scenes.append(entry)


    intro_s = 2.4
    outro_s = 3.0
    spec: dict[str, Any] = {
        "version": 1,
        "title": project.title,
        "brand": brand,
        "templateId": template_id,
        "width": w,
        "height": h,
        "fps": fps,
        "accent": template["accent"],
        "background": template["background"],
        "ink": template["ink"],
        "speed": speed,
        "captions": {
            "enabled": captions_enabled if captions_enabled is not None else True,
            "mode": template["caption_mode"],
            "language": project.language,
        },
        "defaultTransition": template["transition"],
        "transitionDurationS": template["transition_s"],
        "intro": (
            {
                "enabled": True,
                "durationS": intro_s,
                "title": project.title,
                "subtitle": channel.audience,
            }
            if include_intro
            else {"enabled": False, "durationS": 0, "title": project.title}
        ),
        "outro": (
            {
                "enabled": True,
                "durationS": outro_s,
                "headline": "The end.",
                "subline": f"Thanks for watching {channel.name}.",
                "cta": "New story every week",
            }
            if include_outro
            else {"enabled": False, "durationS": 0, "headline": ""}
        ),
        "scenes": spec_scenes,
        "music": music or {"url": None, "volume": 0.12, "duckUnderNarration": True,
                           "fadeInS": 1.2, "fadeOutS": 2.0},
    }




    return StoryboardPlan(
        spec=spec,
        assets=assets,
        scene_keys=[str(i.scene.id) for i in scenes],
        duration_s=actual_duration(spec),
        width=w,
        height=h,
        fps=fps,
        template_id=template_id,
    )


def actual_duration(spec: dict[str, Any]) -> float:
    """Duration implied by the spec's own scenes (frame-accurate, mirrors the engine timeline).

    Mirrors `buildTimeline()` in the Remotion project: overlapping transitions shorten the total.
    """
    fps = int(spec.get("fps") or 30)
    speed = float(spec.get("speed") or 1.0)
    transition_frames = max(0, round(float(spec.get("transitionDurationS") or 0) * fps))
    scenes = spec.get("scenes") or []
    if not scenes:
        return 0.0
    total_frames = 0
    for i, scene in enumerate(scenes):
        frames = max(12, round(max(float(scene.get("durationS") or 0.5), 0.5) / max(speed, 0.1) * fps))
        overlap = 0
        if i > 0 and (scene.get("transition") or spec.get("defaultTransition")) != "cut":
            overlap = min(transition_frames, frames // 3)
        total_frames += frames - overlap
    intro = spec.get("intro") or {}
    outro = spec.get("outro") or {}
    if intro.get("enabled"):
        total_frames += max(1, round(float(intro.get("durationS") or 0) * fps))
    if outro.get("enabled"):
        total_frames += max(1, round(float(outro.get("durationS") or 0) * fps))
    return round(total_frames / fps, 2)


def uuid_key(project_id: uuid.UUID, render_id: uuid.UUID) -> str:
    return f"{project_id}:{render_id}"
