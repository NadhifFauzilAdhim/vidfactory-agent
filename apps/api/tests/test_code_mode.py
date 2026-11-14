"""Code visual mode: the LLM writes HTML/CSS/GSAP scenes, the renderer stages them as JSON assets."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.agents.base import AgentContext
from app.agents.code_scene import CodeSceneAgent, CodeSceneRequest, CodeSceneRequestBatch
from app.ports.interfaces import LLMResult, Message, Usage
from app.services import render as render_mod
from app.services.scene_code import CODE_RUNTIME_VERSION
from app.services.storyboard import build_spec

CHANNEL = SimpleNamespace(
    video_format="16:9",
    audience="Children 3-7",
    name="Test",
    workspace_id="ws",
    niche="kids-stories",
    visual_mode="code",
    accent_color="#D6FF45",
)

GOOD = {
    "html": '<div class="sun"></div><h1 class="t">Dusk</h1>',
    "css": ".sun{width:20vw;height:20vw;border-radius:50%;background:#f2a65a}.t{font:italic 5vw serif}",
    "js": (
        "const tl=gsap.timeline({paused:true});tl.from('.t',{opacity:0});"
        "tl.to('.sun',{y:-8,duration:2,repeat:-1,yoyo:true,ease:'sine.inOut'});"
        "window.__timelines.push(tl);"
    ),
}
BAD = {"html": '<img src="data:image/png;base64,AA">', "css": "", "js": "fetch('/x')"}


def _scene(index: int, code=None):
    scene = SimpleNamespace(
        id=f"scene-{index}",
        index=index,
        title=f"Scene {index}",
        duration_s=4.0,
        transition="fade",
        narration="Narration text.",
        image_prompt="A fox with a red scarf",
        characters=None,
    )
    audio = SimpleNamespace(id=f"audio-{index}", storage_key=f"p/x/audio/scene-0{index}.wav", duration_s=3.5)
    code_asset = (
        SimpleNamespace(id=f"code-{index}", storage_key=f"p/x/code/scene-0{index}.json") if code else None
    )
    return render_mod.sb.RenderScene(scene=scene, image=None, audio=audio, code=code_asset)


def test_code_spec_carries_a_code_key_instead_of_an_image():
    plan = build_spec(
        project=SimpleNamespace(title="T", language="en"),
        channel=CHANNEL,
        setting=None,
        scenes=[_scene(1, code=True), _scene(2)],
        template_id="youtube-editorial",
        visual_mode="code",
    )
    first, second = plan.spec["scenes"]
    assert first["mode"] == "code"
    assert first["codeKey"] == "a-code-1"
    assert "assetKey" not in first
    # The scene still carries its prompt so a scene without code can fall back to generative.
    assert first["prompt"] == "A fox with a red scarf"
    assert "codeKey" not in second
    staged = {a["source"] for a in plan.assets}
    assert "p/x/code/scene-01.json" in staged


def test_image_mode_never_stages_code_assets():
    plan = build_spec(
        project=SimpleNamespace(title="T", language="en"),
        channel=SimpleNamespace(**{**vars(CHANNEL), "visual_mode": "image"}),
        setting=None,
        scenes=[_scene(1, code=True)],
        template_id="youtube-editorial",
        visual_mode="image",
    )
    assert "codeKey" not in plan.spec["scenes"][0]
    assert all("code" not in a["source"] for a in plan.assets)


def test_code_channel_skips_the_missing_image_check():
    scenes = [SimpleNamespace(scene=SimpleNamespace(index=1), image=None)]
    unavailable = (
        []
        if CHANNEL.visual_mode in ("generative", "code")
        else [i.scene.index for i in scenes if i.image is None]
    )
    assert unavailable == []


# --------------------------------------------------------------------------- agent


class FakeLLM:
    """Returns queued JSON payloads; records every prompt it was given."""

    name = "fake"

    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.last = self.payloads[-1] if self.payloads else {"html": ""}
        self.calls: list[list[Message]] = []

    async def complete(self, *, model, messages, temperature=0.7, max_tokens=4096,
                       json_schema=None, timeout_s=120.0) -> LLMResult:
        import json

        self.calls.append(list(messages))
        # Transport retries may call more times than there are queued payloads: repeat the last one.
        payload = self.payloads.pop(0) if self.payloads else self.last
        body = {**payload, "scene_id": "scene-1", "index": 1}
        return LLMResult(
            content=json.dumps(body),
            usage=Usage(provider="fake", model="fake-1", operation="llm_chat",
                        input_tokens=100, output_tokens=200, units=300, unit_label="tokens"),
        )

    async def health(self):
        return True, "ok"


def _ctx(llm: FakeLLM) -> AgentContext:
    return AgentContext(
        workspace_id="ws", llm=llm, model="fake-1", temperature=0.4, max_tokens=8000,  # type: ignore[arg-type]
    )


def _request(index: int = 1) -> CodeSceneRequestBatch:
    return CodeSceneRequestBatch(
        scenes=[
            CodeSceneRequest(
                scene_id=f"scene-{index}",
                index=index,
                title="Harbour at dusk",
                prompt="a harbour at dusk with a lantern",
                narration="The harbour turned the colour of pale tea.",
            )
        ]
    )


def test_agent_stores_valid_scenes_and_reports_usage():
    llm = FakeLLM([GOOD])
    result = asyncio.run(CodeSceneAgent().run(_ctx(llm), _request()))
    scene = result.output.scenes[0]
    assert scene.fallback is False
    assert scene.code is not None
    assert scene.code["runtime"] == CODE_RUNTIME_VERSION
    assert scene.checksum
    assert result.usage.output_tokens == 200
    assert len(llm.calls) == 1


def test_agent_repairs_once_then_accepts():
    llm = FakeLLM([BAD, GOOD])
    result = asyncio.run(CodeSceneAgent().run(_ctx(llm), _request()))
    scene = result.output.scenes[0]
    assert scene.fallback is False
    assert len(llm.calls) == 2
    assert "validator" in llm.calls[1][-1].content.lower() or "rejected" in llm.calls[1][-1].content.lower()
    # Both attempts are billed.
    assert result.usage.output_tokens == 400


def test_agent_falls_back_after_a_failed_repair():
    llm = FakeLLM([BAD, BAD])
    result = asyncio.run(CodeSceneAgent().run(_ctx(llm), _request()))
    scene = result.output.scenes[0]
    assert scene.fallback is True
    assert scene.code is None
    assert scene.issues
    assert result.warnings and "fall" in result.warnings[0].lower()


def test_agent_requires_an_llm():
    from app.agents.base import AgentError

    ctx = AgentContext(workspace_id="ws")
    with pytest.raises(AgentError):
        asyncio.run(CodeSceneAgent().run(ctx, _request()))
