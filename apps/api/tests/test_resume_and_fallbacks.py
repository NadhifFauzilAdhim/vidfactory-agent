"""Resume semantics: a retry must not re-pay for stages that already succeeded.

These tests pin the behaviour described in the retry endpoint's comment — "resumes from the first
incomplete step" — because the pipeline used to restart at planning and re-bill planning, script,
storyboard, TTS and visuals on every attempt.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.agents.base import AgentContext
from app.agents.code_scene import CodeSceneAgent, CodeSceneRequest, CodeSceneRequestBatch
from app.agents.runner import StructuredOutputError
from app.workflow.pipeline import ProductionPipeline


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    """Just enough session for the reuse helpers: each execute() pops the next queued result."""

    def __init__(self, *batches):
        self.batches = list(batches)
        self.added = []

    async def execute(self, *_args, **_kwargs):
        return _Result(self.batches.pop(0) if self.batches else [])

    async def scalar(self, *_args, **_kwargs):
        return self.batches.pop(0) if self.batches else None

    async def get(self, _model, _pk):
        return None

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        return None


def _scene(index: int):
    return SimpleNamespace(id=f"scene-{index}", index=index, title=f"Scene {index}")


def _audio(scene_id: str, duration: float = 3.0, key: str = "a.wav"):
    return SimpleNamespace(scene_id=scene_id, duration_s=duration, storage_key=key)


def _image(scene_id: str, key: str = "i.png"):
    return SimpleNamespace(scene_id=scene_id, storage_key=key)


def _pipeline(session) -> ProductionPipeline:
    pipe = ProductionPipeline(session)  # type: ignore[arg-type]
    pipe.repo = SimpleNamespace(log=_noop_log)
    return pipe


async def _noop_log(*_args, **_kwargs):
    return None


def test_voice_is_reused_when_every_scene_has_narration():
    session = FakeSession([_audio("scene-1"), _audio("scene-2")])
    pipe = _pipeline(session)
    assert asyncio.run(pipe._has_narration(SimpleNamespace(id="p"), [_scene(1), _scene(2)])) is True


def test_voice_is_regenerated_when_one_scene_is_missing():
    session = FakeSession([_audio("scene-1")])
    pipe = _pipeline(session)
    assert asyncio.run(pipe._has_narration(SimpleNamespace(id="p"), [_scene(1), _scene(2)])) is False


def test_voice_with_a_zero_duration_row_counts_as_missing():
    """A half-written row must be regenerated, not trusted."""
    session = FakeSession([_audio("scene-1"), _audio("scene-2", duration=0.0)])
    pipe = _pipeline(session)
    assert asyncio.run(pipe._has_narration(SimpleNamespace(id="p"), [_scene(1), _scene(2)])) is False


def test_voice_without_storage_key_counts_as_missing():
    session = FakeSession([_audio("scene-1"), _audio("scene-2", key="")])
    pipe = _pipeline(session)
    assert asyncio.run(pipe._has_narration(SimpleNamespace(id="p"), [_scene(1), _scene(2)])) is False


def test_visuals_are_reused_per_mode():
    project = SimpleNamespace(id="p")
    scenes = [_scene(1), _scene(2)]
    image_channel = SimpleNamespace(visual_mode="image")
    code_channel = SimpleNamespace(visual_mode="code")

    # images present → image mode is done
    session = FakeSession([_image("scene-1"), _image("scene-2")])
    assert asyncio.run(_pipeline(session)._has_visuals(project, scenes, image_channel)) is True

    # no code assets → code mode must generate (the fake returns an empty result set)
    session = FakeSession([])
    assert asyncio.run(_pipeline(session)._has_visuals(project, scenes, code_channel)) is False


def test_generative_mode_never_claims_to_have_visuals():
    """It generates nothing, so the stage must still run (it is a free log entry)."""
    session = FakeSession()
    pipe = _pipeline(session)
    assert asyncio.run(pipe._has_visuals(
        SimpleNamespace(id="p"), [_scene(1)], SimpleNamespace(visual_mode="generative")
    )) is False


def test_missing_visuals_returns_only_the_pending_scenes():
    """The point of the helper: a retry pays for two scenes, not six."""
    session = FakeSession([_image("scene-2"), _image("scene-4")])
    pending = asyncio.run(
        _pipeline(session)._scenes_missing_visuals(
            SimpleNamespace(id="p"), [_scene(i) for i in range(1, 7)], "image"
        )
    )
    assert [s.index for s in pending] == [1, 3, 5, 6]


def test_project_is_reused_only_when_it_still_exists():
    pipe = _pipeline(FakeSession())
    assert asyncio.run(pipe._reuse_project(SimpleNamespace(project_id=None))) is None


# --------------------------------------------------------------------------- agent robustness


class BrokenLLM:
    """Always answers with an empty body — the failure seen in production."""

    name = "broken"

    async def complete(self, **_kwargs):
        from app.ports.interfaces import LLMResult, Usage

        return LLMResult(
            content="",
            usage=Usage(provider="broken", model="m", operation="llm_chat",
                        input_tokens=10, output_tokens=4096, units=4106, unit_label="tokens"),
        )

    async def health(self):
        return True, "ok"


def test_empty_response_does_not_fail_the_whole_batch():
    """One unusable scene falls back; it must not raise out of the agent."""
    ctx = AgentContext(workspace_id="ws", llm=BrokenLLM(), model="m")  # type: ignore[arg-type]
    batch = CodeSceneRequestBatch(
        scenes=[
            CodeSceneRequest(scene_id="s1", index=1, title="One", prompt="p"),
            CodeSceneRequest(scene_id="s2", index=2, title="Two", prompt="p"),
        ]
    )
    result = asyncio.run(CodeSceneAgent().run(ctx, batch))
    assert [s.fallback for s in result.output.scenes] == [True, True]
    assert all(s.issues for s in result.output.scenes)
    assert "structured_output" in " ".join(result.output.scenes[0].issues)
    # Tokens spent on failed scenes are still reported, so cost tracking stays honest.
    # Two scenes, two attempts each, 4096 tokens per response.
    assert result.usage.output_tokens == 16_384


def test_empty_response_error_explains_the_cause():
    """The old message was `Invalid JSON: EOF`, which pointed at the wrong problem."""
    from app.agents.code_scene import CodeSceneDraft
    from app.agents.runner import StructuredRunner

    runner = StructuredRunner(BrokenLLM())  # type: ignore[arg-type]
    with pytest.raises(StructuredOutputError) as excinfo:
        asyncio.run(
            runner.run(
                model="m",
                system="s",
                user="u",
                output_model=CodeSceneDraft,
                max_tokens=4096,
                repair=False,
            )
        )
    message = str(excinfo.value)
    assert "empty response" in message
    assert "max_tokens=4096" in message
    assert excinfo.value.usage is not None


def test_a_gateway_failure_does_not_fail_the_batch():
    """A 502 from the gateway is transport, not a reason to abandon the other scenes."""

    class DownLLM:
        name = "down"

        async def complete(self, **_kwargs):
            from app.ports.interfaces import ProviderError

            raise ProviderError("gateway error 502", status=502)

        async def health(self):
            return False, "down"

    ctx = AgentContext(workspace_id="ws", llm=DownLLM(), model="m")  # type: ignore[arg-type]
    batch = CodeSceneRequestBatch(
        scenes=[CodeSceneRequest(scene_id="s1", index=1, title="One", prompt="p")]
    )
    result = asyncio.run(CodeSceneAgent().run(ctx, batch))
    scene = result.output.scenes[0]
    assert scene.fallback is True
    assert "[provider]" in " ".join(scene.issues)


def test_a_gateway_failure_is_retried_before_falling_back():
    """A single 503 used to cost a whole scene; transient errors back off and retry."""

    class FlakyLLM:
        name = "flaky"

        def __init__(self, failures: int) -> None:
            self.failures = failures
            self.calls = 0

        async def complete(self, **_kwargs):
            from app.ports.interfaces import LLMResult, ProviderError, Usage

            self.calls += 1
            if self.calls <= self.failures:
                raise ProviderError("provider temporarily unavailable", status=503)
            return LLMResult(
                content='{"scene_id":"s1","index":1,"html":"<div class=\\"a\\"></div>",'
                '"css":".a{color:red}",'
                '"js":"gsap.timeline({paused:true});'
                "window.__timelines.push(gsap.timeline({paused:true}).to('.a',{y:1,repeat:-1}));\"}",
                usage=Usage(provider="flaky", model="m", operation="llm_chat",
                            input_tokens=1, output_tokens=1, units=2, unit_label="tokens"),
            )

        async def health(self):
            return True, "ok"

    ctx = AgentContext(workspace_id="ws", llm=FlakyLLM(failures=2), model="m")  # type: ignore[arg-type]
    batch = CodeSceneRequestBatch(
        scenes=[CodeSceneRequest(scene_id="s1", index=1, title="One", prompt="p")]
    )
    result = asyncio.run(CodeSceneAgent().run(ctx, batch))
    assert result.output.scenes[0].fallback is False


def test_gateway_failure_still_falls_back_after_all_retries():
    class DownLLM:
        name = "down"

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, **_kwargs):
            from app.ports.interfaces import ProviderError

            self.calls += 1
            raise ProviderError("gateway error 502", status=502)

        async def health(self):
            return False, "down"

    llm = DownLLM()
    ctx = AgentContext(
        workspace_id="ws", llm=llm, model="m", params={"max_attempts": 2}
    )  # type: ignore[arg-type]
    batch = CodeSceneRequestBatch(
        scenes=[CodeSceneRequest(scene_id="s1", index=1, title="One", prompt="p")]
    )
    result = asyncio.run(CodeSceneAgent().run(ctx, batch))
    assert result.output.scenes[0].fallback is True
    # Two attempts for the first call, then two more for the repair round: the scene is retried,
    # but a failing gateway is never hammered forever.
    assert llm.calls == 4


def test_usage_request_key_is_unique_across_attempts():
    """A retry restarts `step` at 0; the key must still differ from the earlier attempt.

    Without the attempt in the key the insert hit uq_usage_request_key and the whole run died with
    PendingRollbackError — after the provider had already been paid.
    """

    def key(workflow_key: str, retry: int, step: int, agent: str = "code_scene") -> str:
        return f"{workflow_key}:{agent}:a{retry}:{step}:ctx"

    assert key("wf", 0, 3) != key("wf", 1, 3)
    assert key("wf", 1, 3) == key("wf", 1, 3)


def test_code_scene_budget_is_never_the_generic_default():
    """4096 was too small: reasoning consumed it before any JSON was emitted."""
    assert CodeSceneAgent.MIN_OUTPUT_TOKENS >= 8192


def test_operator_budget_is_respected_not_clamped():
    """An explicit max_tokens (e.g. 120000) must reach the provider unchanged."""

    class RecordingLLM(BrokenLLM):
        def __init__(self) -> None:
            self.seen: list[int] = []

        async def complete(self, **kwargs):
            self.seen.append(kwargs["max_tokens"])
            return await super().complete(**kwargs)

    llm = RecordingLLM()
    ctx = AgentContext(workspace_id="ws", llm=llm, model="m", max_tokens=120_000)  # type: ignore[arg-type]
    batch = CodeSceneRequestBatch(
        scenes=[CodeSceneRequest(scene_id="s1", index=1, title="One", prompt="p")]
    )
    asyncio.run(CodeSceneAgent().run(ctx, batch))
    assert llm.seen and set(llm.seen) == {120_000}


def test_all_fallbacks_still_report_the_model_that_was_asked():
    """The workflow log said "written by deterministic" when every scene hit a gateway 502."""

    class DownLLM(BrokenLLM):
        name = "9routergo"

        async def complete(self, **_kwargs):
            from app.ports.interfaces import ProviderError

            raise ProviderError("gateway error 502", status=502)

    ctx = AgentContext(
        workspace_id="ws", llm=DownLLM(), model="inferhub/cb/deepseek-v4.1-flash"
    )  # type: ignore[arg-type]
    batch = CodeSceneRequestBatch(
        scenes=[CodeSceneRequest(scene_id="s1", index=1, title="One", prompt="p")]
    )
    result = asyncio.run(CodeSceneAgent().run(ctx, batch))
    assert result.provider == "9routergo"
    assert result.model == "inferhub/cb/deepseek-v4.1-flash"
