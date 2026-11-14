"""Agent registry.

Explicit registration — no import magic. Every agent must declare its contracts, which are
exported to the database so the UI can display them and administrators can inspect schemas.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from app.agents.code_scene import CodeSceneAgent
from app.agents.generation import (
    PLANNER_SYSTEM,
    SCRIPT_SYSTEM,
    SEO_SYSTEM,
    STORYBOARD_SYSTEM,
    ContentPlannerAgent,
    ScriptWriterAgent,
    SeoAgent,
    StoryboardAgent,
)
from app.agents.media import (
    VideoProductionAgent,
    VisualGenerationAgent,
    VoiceGenerationAgent,
)
from app.agents.quality import AnalyticsAgent, PublishingAgent, QualityControlAgent


@dataclass(frozen=True, slots=True)
class AgentSpec:
    key: str
    name: str
    role: str
    description: str
    category: str
    stage: str
    port: str
    agent: type
    system_prompt: str | None = None

    @property
    def input_model(self) -> type[BaseModel]:
        return self.agent.InputModel  # type: ignore[attr-defined]

    @property
    def output_model(self) -> type[BaseModel]:
        return self.agent.OutputModel  # type: ignore[attr-defined]


_SPECS: list[AgentSpec] = [
    AgentSpec(
        key="content_planner",
        name="Content Planner",
        role="Plans what to make next",
        description=(
            "Researches the niche context, avoids repeating recent titles and produces a content "
            "brief with audience, angle, creative direction and runtime."
        ),
        category="planning",
        stage="planning",
        port="llm",
        agent=ContentPlannerAgent,
        system_prompt=PLANNER_SYSTEM,
    ),
    AgentSpec(
        key="script_writer",
        name="Script Writer",
        role="Writes the original script",
        description=(
            "Turns a brief into an original, age-appropriate script: hook, story beats, dialogue, "
            "scene durations, visual and sound-effect instructions."
        ),
        category="generation",
        stage="script",
        port="llm",
        agent=ScriptWriterAgent,
        system_prompt=SCRIPT_SYSTEM,
    ),
    AgentSpec(
        key="storyboard",
        name="Storyboard",
        role="Directs the shots",
        description=(
            "Converts script scenes into camera, composition, character and background direction with "
            "self-contained image prompts that keep character appearance consistent."
        ),
        category="generation",
        stage="storyboard",
        port="llm",
        agent=StoryboardAgent,
        system_prompt=STORYBOARD_SYSTEM,
    ),
    AgentSpec(
        key="voice_generation",
        name="Voice Generation",
        role="Voices narration & dialogue",
        description=(
            "Per-scene TTS with voice selection, speed and emotion. Returns audio duration and "
            "metadata for subtitle timing and mixing."
        ),
        category="generation",
        stage="voice",
        port="tts",
        agent=VoiceGenerationAgent,
    ),
    AgentSpec(
        key="visual_generation",
        name="Visual Generation",
        role="Renders scene artwork",
        description=(
            "Generates one image per scene from the storyboard prompt, stores it content-addressed "
            "and records provider, model, licence and cost per asset."
        ),
        category="generation",
        stage="visual",
        port="image",
        agent=VisualGenerationAgent,
    ),
    AgentSpec(
        key="code_scene",
        name="Code Scene",
        role="Writes scene visuals as code",
        description=(
            "Writes one self-contained HTML/CSS/GSAP scene per storyboard scene instead of generating "
            "an image. Every scene is validated for determinism, sandbox safety and size; scenes that "
            "fail after one repair round fall back to the generative backdrop."
        ),
        category="generation",
        stage="visual",
        port="llm",
        agent=CodeSceneAgent,
    ),
    AgentSpec(
        key="video_production",
        name="Video Production",
        role="Assembles the film",
        description=(
            "Composites scene images, narration, transitions, Ken Burns motion, burned-in subtitles "
            "and music bed into the final MP4 using FFmpeg (Remotion templates available)."
        ),
        category="production",
        stage="render",
        port="video",
        agent=VideoProductionAgent,
    ),
    AgentSpec(
        key="quality_control",
        name="Quality Control",
        role="Reviews before approval",
        description=(
            "Weighted checks for script readability, scene consistency, asset completeness, audio, "
            "subtitles, media integrity, age appropriateness and originality; returns regenerate targets."
        ),
        category="quality",
        stage="qc",
        port="rules",
        agent=QualityControlAgent,
    ),
    AgentSpec(
        key="seo_metadata",
        name="SEO & Metadata",
        role="Packages for discovery",
        description=(
            "Titles, descriptions, tags, playlist suggestion and thumbnail concept/text constrained "
            "to honest, policy-safe metadata."
        ),
        category="distribution",
        stage="seo",
        port="llm",
        agent=SeoAgent,
        system_prompt=SEO_SYSTEM,
    ),
    AgentSpec(
        key="publishing",
        name="Publishing",
        role="Delivers to YouTube",
        description=(
            "Uploads the approved render and thumbnail through YouTube Data API v3 with Made for Kids "
            "classification, privacy and scheduling, and records the resulting video URL."
        ),
        category="distribution",
        stage="publish",
        port="youtube",
        agent=PublishingAgent,
    ),
    AgentSpec(
        key="analytics",
        name="Analytics",
        role="Learns from performance",
        description=(
            "Collects views, watch time, average view duration and subscriber growth, ranks videos "
            "and feeds insights back into planning."
        ),
        category="insight",
        stage="analytics",
        port="rules",
        agent=AnalyticsAgent,
    ),
]


class AgentRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, AgentSpec] = {spec.key: spec for spec in _SPECS}

    def all(self) -> list[AgentSpec]:
        return list(self._specs.values())

    def get(self, key: str) -> AgentSpec:
        try:
            return self._specs[key]
        except KeyError as exc:
            raise KeyError(f"Unknown agent '{key}'") from exc

    def instance(self, key: str):  # type: ignore[no-untyped-def]
        return self.get(key).agent()


REGISTRY = AgentRegistry()
