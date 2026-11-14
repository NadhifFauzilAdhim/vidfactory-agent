"""Code scenes — the LLM writes HTML/CSS/GSAP instead of an image being generated.

The agent is a sibling of ``VisualGenerationAgent``: same input contract, same per-scene result, but
the artefact is a small JSON programme rather than a PNG. Validation happens here (one repair round)
because an invalid scene must never reach a browser, and a scene that still fails is flagged so the
pipeline can fall back to the generative backdrop instead of aborting the whole render.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re

from pydantic import BaseModel, Field

from app.agents.base import AgentContext, AgentResult
from app.agents.contracts import Contract
from app.agents.generation import usage_zero
from app.agents.runner import StructuredOutputError, StructuredRunner
from app.ports.interfaces import ProviderError, Usage
from app.services.scene_code import (
    CODE_RUNTIME_VERSION,
    normalise_scene_code,
    validate_scene_code,
)

CODE_SCENE_SYSTEM = """You are the Scene Coder — art director, illustrator and motion designer — for a
{niche} video channel ({audience}).
For each storyboard scene you write ONE self-contained scene as HTML + CSS + JavaScript.
There is no image and no footage: your markup and your motion ARE the shot.
You choose the visual idea, the composition, what moves and how. Aim for something that looks
designed and alive, not like a wireframe that fades in.

Canvas: {width}x{height} px, {fps} fps, scene lasts {duration_s} seconds.

Hard rules (violating any of them rejects the scene):
- html: body markup for a full-bleed stage. No <html>, <head>, <body>, <style>, <script>, <title>,
  <img>, <iframe>, <link>. Illustrate with CSS shapes, gradients, type and inline <svg>.
- css: stylesheet for that markup. No @import and no url() except data: URIs.
- js: optional JavaScript. Register a paused GSAP timeline (or use per-frame hooks, see below):
      const tl = gsap.timeline({{paused: true}}); /* ... */ window.__timelines.push(tl);
  Timeline time 0 = scene start; the engine seeks it per frame. Never animate with CSS
  @keyframes, requestAnimationFrame, setTimeout or transitions triggered by timers.
- Never use Math.random, Date.now, new Date, performance.now, fetch, import, eval, localStorage,
  parent/top/window.opener. The same frame must always look the same.
- Only https-free, network-free assets: inline SVG paths and CSS gradients only.
- Fonts: use the system stacks already allowed — Georgia/"Times New Roman",serif for editorial,
  system-ui/-apple-system,sans-serif for UI, ui-monospace,monospace for data.
- Palette: background {background}, ink {ink}, accent {accent}. Keep text inside 5%% of the edges.
- Keep total output under {budget} characters. Leave room for a lower-third caption at the bottom.

Composition notes:
- Fill the frame: use scale, overlap, depth and one focal subject. Empty middle-of-frame is a fail.
- The scene title is the visual idea; the caption is spoken narration and must NOT be repeated
  in the scene as large text.

MOTION IS THE PRODUCT — a scene that only fades things in is a rejected draft:
- Every scene needs at least ONE looping motion that runs for the whole scene (repeat: -1), plus
  whatever entrance choreography the shot calls for. Repeats are exact here: the engine seeks the
  timeline by time, so frame N always looks the same.
- You are the motion designer. Decide what moves from what the scene actually says — a crowd
  surging, leaves shredding in wind, a wave crawling up a hull, a clock hand stuttering, grains
  tumbling through a chute, a shadow stretching, a door swinging, a crowd turning to look. Do not
  reach for a generic "character breathing idle" when the line describes something more specific.
- Animate whatever carries the story: subjects, props, light, weather, camera. A camera move
  (slow push-in, drift, tilt) on a wrapper element counts as motion and often sells the shot better
  than a limb wiggling.
- Give animatable parts a class or id, and set transformOrigin on whatever rotates from a joint,
  hinge or pivot. Rotating a limb from its own centre looks broken.
- Beyond GSAP tweens you may use anything the scene needs, as long as it is a pure function of
  scene time:
    * nested/nested-repeating timelines, keyframes arrays, stagger, onRepeat callbacks,
      gsap.utils interpolation, motion paths, SVG attr morphing, clip-path and filter animation;
    * per-frame hooks for motion a tween cannot express (per-particle maths, canvas drawing,
      physics-ish arcs):
          window.__frameFns.push(function (t, frame) {{ /* t = seconds into the scene */ }});
  The runtime calls every hook after seeking the timelines, once per rendered frame.
- Never move anything from a clock or a random source; `t` is the only time you get.
- Layer motion: a base layer that never stops, a beat layer tied to the narration, and accents.
  Aim for 8-16 tweens; at least one must loop and at least one should start after 1 s so the shot
  keeps evolving instead of settling.
- Unrelated loops must not fight: give each its own target element or its own tween.
- Stagger repeated elements so a group never moves in lockstep unless that is the joke.

Return ONLY JSON matching the schema: {{"scene_id", "index", "html", "css", "js"}}."""

CODE_SCENE_REPAIR = """Your scene was rejected by the validator. Rewrite it and return ONLY the corrected
JSON object (same schema). Problems:
{issues}

Rules you missed most often: no network, no timers, no randomness, exactly one paused timeline in
window.__timelines, inline SVG/CSS only, no document-level tags, and at least one looping
(repeat: -1, yoyo: true) tween so the character keeps moving."""

_FENCE = re.compile(r"^```(?:json|html|css|javascript|js)?\s*|\s*```$", re.MULTILINE)

log = logging.getLogger("factory.agents.code_scene")


class CodeSceneRequest(Contract):
    scene_id: str
    index: int
    title: str
    prompt: str
    narration: str = ""
    duration_s: float = 5.0
    width: int = 1920
    height: int = 1080
    fps: int = 30
    accent: str = "#D6FF45"
    background: str = "#FFFFFF"
    ink: str = "#111111"
    palette: list[str] = Field(default_factory=list)
    #: The lines from the neighbouring scenes, so the shot can set up or resolve the action.
    previous_narration: str = ""
    next_narration: str = ""


class CodeSceneRequestBatch(Contract):
    niche: str = "children's stories"
    audience: str = "Children 3-7"
    style_suffix: str = ""
    scenes: list[CodeSceneRequest] = Field(min_length=1)


class CodeSceneDraft(BaseModel):
    scene_id: str
    index: int
    html: str = ""
    css: str = ""
    js: str = ""


class CodeSceneResult(BaseModel):
    scene_id: str
    index: int
    code: dict | None
    issues: list[str] = Field(default_factory=list)
    fallback: bool = False
    bytes: int = 0
    checksum: str = ""


class CodeSceneBatchResult(BaseModel):
    scenes: list[CodeSceneResult]
    provider: str = ""
    model: str = ""


def _clean(text: str) -> str:
    return _FENCE.sub("", (text or "").strip()).strip()


class CodeSceneAgent:
    key = "code_scene"
    name = "Code Scene"
    InputModel = CodeSceneRequestBatch
    OutputModel = CodeSceneBatchResult

    #: A scene is 3–8 KB of JSON. Reasoning models spend output tokens on hidden reasoning *before*
    #: emitting text, so a small budget comes back empty. This is only a floor for the case where the
    #: operator never configured the agent; an explicit `max_tokens` in the agent settings always
    #: wins, because the operator knows the model's real ceiling.
    MIN_OUTPUT_TOKENS = 8_192

    async def run(
        self, ctx: AgentContext, payload: CodeSceneRequestBatch
    ) -> AgentResult[CodeSceneBatchResult]:
        provider = ctx.require_llm()
        runner = StructuredRunner(provider)
        concurrency = int(ctx.params.get("concurrency", 2))
        semaphore = asyncio.Semaphore(concurrency)
        warnings: list[str] = []
        budget = max(int(ctx.max_tokens or 0), self.MIN_OUTPUT_TOKENS)

        async def attempt(
            system: str, user: str, *, temperature: float
        ) -> tuple[CodeSceneDraft | None, Usage, list[str]]:
            """One structured call with transport retries. Schema/content failures are not retried."""
            attempts = max(1, int(ctx.params.get("max_attempts", 3)))
            total = usage_zero()
            last_issue = ""
            for attempt in range(1, attempts + 1):
                try:
                    draft, usage = await runner.run(
                        model=ctx.model or "default",
                        system=system,
                        user=user,
                        output_model=CodeSceneDraft,
                        temperature=temperature,
                        max_tokens=budget,
                        timeout_s=240.0,
                        repair=False,
                    )
                    return draft, _merge(total, usage), []
                except StructuredOutputError as exc:
                    # One scene must never fail the whole video: the caller falls back to generative.
                    # Tokens already spent are still billed, so keep the usage when it is carried.
                    # Retrying content here would just burn tokens on the same answer.
                    return None, _merge(total, exc.usage or usage_zero()), [f"[structured_output] {exc}"]
                except ProviderError as exc:
                    # Gateway 5xx / timeout / rate limit: transient. Back off and try again before
                    # giving up on the scene, otherwise a single blip costs a whole scene's design.
                    last_issue = f"[provider] {exc}"
                    log.warning("code_scene.attempt_failed scene_user=%s attempt=%s/%s: %s",
                                user.splitlines()[0][:60] if user else "", attempt, attempts, exc)
                    if attempt < attempts:
                        await asyncio.sleep(min(2 ** attempt, 8))
            return None, total, [last_issue]

        async def write(scene: CodeSceneRequest) -> tuple[CodeSceneResult, Usage]:
            system = CODE_SCENE_SYSTEM.format(
                niche=ctx.params.get("niche", payload.niche),
                audience=ctx.params.get("audience", payload.audience),
                width=scene.width,
                height=scene.height,
                fps=scene.fps,
                duration_s=scene.duration_s,
                accent=scene.accent,
                background=scene.background,
                ink=scene.ink,
                budget=32_000,
            )
            user = "\n".join(
                [
                    f"Scene {scene.index}: {scene.title}",
                    f"Visual idea: {scene.prompt}",
                    f"Narration (spoken, do not set as large text): {scene.narration}",
                    f"Previous line: {scene.previous_narration or '(this is the first scene)'}",
                    f"Next line: {scene.next_narration or '(this is the last scene)'}",
                    f"Palette hints: {', '.join(scene.palette) or scene.accent}",
                    f"Style direction: {payload.style_suffix or 'clean editorial'}",
                ]
            )
            async with semaphore:
                draft, usage, issues = await attempt(system, user, temperature=ctx.temperature)
                if draft is not None:
                    html = _clean(draft.html)
                    css = _clean(draft.css)
                    js = _clean(draft.js)
                    issues = validate_scene_code(html, css, js)
                else:
                    html = css = js = ""

                if issues:
                    detail = "\n".join(f"- {issue}" for issue in issues)
                    repaired, repair_usage, repair_issues = await attempt(
                        system,
                        f"{user}\n\n{CODE_SCENE_REPAIR.format(issues=detail)}",
                        temperature=0.1,
                    )
                    usage = _merge(usage, repair_usage)
                    if repaired is not None:
                        html = _clean(repaired.html)
                        css = _clean(repaired.css)
                        js = _clean(repaired.js)
                        issues = validate_scene_code(html, css, js)
                    else:
                        issues = repair_issues

            if issues:
                return (
                    CodeSceneResult(
                        scene_id=scene.scene_id,
                        index=scene.index,
                        code=None,
                        issues=[str(i) for i in issues],
                        fallback=True,
                    ),
                    usage,
                )

            code = normalise_scene_code(html, css, js)
            blob = json.dumps(code.to_payload(), ensure_ascii=False).encode("utf-8")
            return (
                CodeSceneResult(
                    scene_id=scene.scene_id,
                    index=scene.index,
                    code=code.to_payload(),
                    bytes=len(blob),
                    checksum=hashlib.sha256(blob).hexdigest(),
                ),
                usage,
            )

        results = await asyncio.gather(*(write(scene) for scene in payload.scenes))
        usage = _merge_all([u for _, u in results])
        fallbacks = [r.index for r, _ in results if r.fallback]
        if fallbacks:
            warnings.append(
                f"Scene(s) {fallbacks} could not be coded; they fall back to the generative backdrop"
            )
        if usage.provider == "mock":
            warnings.append("Scene code was generated by a mock provider — configure a real LLM.")
        # Report the model that was asked, not the usage of the last attempt: when every scene fails
        # the transport layer the merged usage is the zero usage ("rules"/"deterministic"), and the
        # workflow log would then claim the scenes were written by a deterministic provider.
        reported_provider = usage.provider if usage.provider not in ("", "rules") else provider.name
        reported_model = usage.model if usage.model not in ("", "deterministic") else (ctx.model or "")
        return AgentResult(
            output=CodeSceneBatchResult(
                scenes=[r for r, _ in results], provider=reported_provider, model=reported_model
            ),
            usage=usage,
            provider=reported_provider,
            model=reported_model,
            artifacts={
                f"scene:{r.index}:code": str(r.checksum) for r, _ in results if not r.fallback
            },
            warnings=warnings,
        )


def _merge(a: Usage, b: Usage) -> Usage:
    return Usage(
        provider=b.provider or a.provider,
        model=b.model or a.model,
        operation="llm_chat",
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        units=a.units + b.units,
        unit_label="tokens",
        latency_ms=a.latency_ms + b.latency_ms,
    )


def _merge_all(usages: list[Usage]) -> Usage:
    if not usages:
        return usage_zero()
    total = usages[0]
    for usage in usages[1:]:
        total = _merge(total, usage)
    return total


__all__ = [
    "CODE_RUNTIME_VERSION",
    "CodeSceneAgent",
    "CodeSceneBatchResult",
    "CodeSceneDraft",
    "CodeSceneRequest",
    "CodeSceneRequestBatch",
    "CodeSceneResult",
]
