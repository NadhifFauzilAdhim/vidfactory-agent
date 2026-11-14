# 11 · Code scenes (LLM-authored HTML/CSS/GSAP)

Status: **implemented** — `visual_mode="code"` ships alongside `image` and `generative`.

## 1 · What this adds

| `visual_mode` | Per-scene art | Provider call | Where it lives today |
| --- | --- | --- | --- |
| `image` | one AI image per scene | image provider | `app/agents/media.py::VisualGenerationAgent`, `scene.tsx::Backdrop` |
| `generative` | procedural CSS from the prompt hash | none | `src/generative.tsx`, `scene.tsx:48-60` |
| **`code` (new)** | **HTML/CSS/GSAP written by the LLM** | **LLM** | new `src/code-scene.tsx`, new agent |

The LLM writes a self-contained scene. Nothing is sent to an image provider. The scene is rendered
inside Remotion's existing Chromium pass, so the output is **still one complete MP4** — H.264 +
AAC, narration, music, SFX, transitions, captions, intro/outro — validated by the same
`RemotionRenderer.validate()` (`app/adapters/video.py:324`).

### Non-goals

* No second encoding pipeline, no frame stitching, no Playwright screenshots. Remotion stays the
  only composer and encoder (`docs/10-video-engine.md`).
* No arbitrary package installation by the LLM. Allowed globals are whitelisted; GSAP is served
  from the bundle, not a CDN.
* No network access from the scene. The iframe is sandboxed.

---

## 2 · Data contract

### 2.1 Remotion props (`services/media-engine/remotion/src/types.ts`)

```ts
export type SceneCode = {
  /** Body markup only — the iframe shell provides <html>, <head>, the stage and the runtime. */
  html: string;
  /** Scoped stylesheet. Selectors are wrapped in the iframe document, so no escaping needed. */
  css: string;
  /** Optional GSAP timeline code. Must register one paused timeline `window.__timelines[0]`. */
  js?: string | null;
  /** Contract version, so the runtime can refuse code generated for an older spec. */
  runtime: 1;
};

export type StoryboardScene = {
  // …existing…
  mode?: "asset" | "generative" | "code";
  /** Present when mode === "code". Staged as an asset, never inlined in the render job. */
  codeKey?: string | null;
  code?: SceneCode | null;
};
```

`code` is resolved by `render.mjs` from `codeKey` the same way `assetKey`/`narrationKey` resolve
today (`render.mjs:76-88`): the file is staged to `public/jobs/<renderId>/` and the JSON pointer is
rewritten to `"codeSrc": "jobs/<renderId>/scene-03.code.json"`. The composition fetches it.

**Why a file, not inline props:** scene code is ~2–8 KB of HTML/CSS/JS per scene; inlining bloats
`inputProps` (passed through `selectComposition`/`renderMedia` twice) and makes the render job row
large. Staging also gives content-addressed caching for free.

### 2.2 Runtime contract (what the LLM must produce)

The iframe shell (`src/code-runtime.ts`, a template string) provides:

```html
<!doctype html>
<html>
  <head><style>/* reset + stage */</style><style>{{css}}</style></head>
  <body>
    <div id="stage">{{html}}</div>
    <script src="{{gsapUrl}}"></script>
    <script>{{js}}</script>
    <script>/* seek runtime — injected, not authored by the LLM */</script>
  </body>
</html>
```

Injected seek runtime:

```js
window.__timelines = window.__timelines || [];
window.__seek = (frame, fps) => {
  const t = frame / fps;
  for (const tl of window.__timelines) tl.pause().seek(t, false);
  // CSS-only animations: the shell sets --frame and toggles the paused class
  document.documentElement.style.setProperty("--t", t + "s");
};
window.__ready = true;
```

Author rules (enforced by the validator, stated in the agent's system prompt):

1. Every animation is driven by GSAP timelines pushed to `window.__timelines`, **not**
   `requestAnimationFrame` or timers.
2. `gsap.ticker.lagSmoothing(0)`; no `Math.random()`, `Date.now()`, `performance.now()`, `fetch`.
3. Stage is exactly `100vw × 100vh`, `overflow: hidden`, transparent background by default.
4. All assets must be inline SVG/CSS. No `<img src>`, no `@import`, no external fonts (only the
   three fonts already in the bundle).

---

## 3 · Video engine changes

### 3.1 `src/code-scene.tsx` — what actually shipped

The mount strategy is the part the plan got wrong, and it cost a spike to find out. Findings from
`scripts/mount-spike.mjs` and later render probes:

| Strategy | Rendered by Remotion? |
| --- | --- |
| Inline markup in the composition | yes |
| `<iframe srcDoc>` | yes |
| `<iframe>` + `contentDocument.write()` | **no** — the written document is never captured |
| `<iframe src={blob:}>` | yes (used, because it is same-origin) |

Two further traps were found only by rendering:

1. **`postMessage` alone is not enough for a same-origin document, but it is required for a
   sandboxed one.** A message is delivered on a later task; Remotion wraps `remotion_setFrame` in
   `delayRender()` and continues after a `requestAnimationFrame`, so a message posted from
   `useLayoutEffect` *does* arrive before the screenshot. The component posts the frame every render.
2. **`fetch("/vendor/gsap.min.js")` 404s during a render.** Remotion serves the bundle from a nested
   path, so an absolute root URL misses. The scene silently ran without GSAP — every timeline was
   dead, which looked exactly like "the animation does nothing". `staticFile(GSAP_SRC)` is required.

`scripts/loop-motion-check.mjs` renders a scene with `repeat: -1` and asserts that frames 60→75 and
75→90 differ; `scripts/code-scene-determinism.mjs` asserts identical bytes across runs.

### 3.0 Planned shape (kept for reference)

```tsx
export const CodeScene: React.FC<{src: string; frames: number}> = ({src, frames}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const ref = useRef<HTMLIFrameElement>(null);
  const [ready, setReady] = useState(false);

  // Mount once per scene: srcdoc is written to a ref, never re-rendered per frame.
  useEffect(() => {
    const doc = ref.current?.contentDocument;
    if (!doc) return;
    doc.open();
    doc.write(shellHtml);
    doc.close();
    setReady(true);
  }, [shellHtml]);

  // Frame-accurate seek. GSAP .seek() is synchronous, so no awaits inside the render pass.
  useLayoutEffect(() => {
    if (!ready) return;
    (ref.current?.contentWindow as any)?.__seek?.(frame, fps);
  }, [frame, ready, fps]);

  return <iframe ref={ref} sandbox="allow-scripts" style={{border: 0, width: '100%', height: '100%'}} />;
};
```

Key decisions:

* **`srcdoc` is not used as a JSX prop.** Writing via `contentDocument.write()` once avoids a
  remount every frame (the failure mode called out in the review).
* **`sandbox="allow-scripts"` only** — no `allow-same-origin`, so the scene cannot reach the
  Remotion page, its cookies or parent storage. This breaks `contentDocument` access, so the shell
  is injected by Remotion *before* sandboxing would matter — **use a `Blob` URL + `sandbox` if
  `contentDocument` access proves unreliable**; keep `allow-scripts` and add
  `allow-same-origin` only if the renderer test shows it is required (document the trade-off).
* **Readiness**: `renderStill`/`renderMedia` take the frame immediately after React commits. A
  scene that is not ready yields an empty frame. Mitigation: the `useEffect` write happens on mount,
  before the first paint of frame 0; GSAP is inlined (not fetched) so there is no async load.
  Regression test: render a scene whose frame 0 must be non-blank.

### 3.2 `src/scene.tsx::Backdrop`

Add a branch before the generative branch:

```tsx
if (scene.mode === "code" && scene.code?.runtime === 1) {
  return <AbsoluteFill><CodeScene src={scene.code.html} ... /></AbsoluteFill>;
}
```

If `mode === "code"` but the code is missing or a different runtime, fall back to
`GenerativeScene` and let `validate()` flag it — failures stay visible, matching the existing
"image unavailable" rule.

### 3.3 `render.mjs`

* Stage `codeKey` assets (kind `code`) next to images/audio; name them `<scene>-code.json`.
* Rewrite `"codeKey":"…"` → `"codeSrc":"jobs/<renderId>/…"` in `inputProps`.
* Set `timeoutInMilliseconds` for code scenes from a job field (`job.codeSceneTimeoutMs`,
  default 120 s) — a heavy GSAP timeline must not silently fail at 60 s.

### 3.4 Dependencies

* Add `gsap` to `services/media-engine/remotion/package.json`, and copy
  `node_modules/gsap/dist/gsap.min.js` into the staged public dir at render time. **Never a CDN** —
  renders must work offline and in the Docker image.
* GSAP core is free; premium plugins (SplitText, MorphSVG) are **not** to be used — the validator
  rejects them by name.

---

## 4 · API changes

### 4.1 Model and schema

| File | Change |
| --- | --- |
| `app/models/channel.py:53` | comment + allowed values — `code` needs no column change (`String(16)`) |
| `app/schemas.py:115,128` | `Literal["image", "generative", "code"]` |
| `alembic/versions/…` | no schema change required; add a **data** guard only if a check constraint exists |

No migration needed for the column itself. Add one only if the team wants a DB-level `CHECK`.

### 4.2 New agent: `code_scene`

`app/agents/media.py`

```python
class CodeSceneRequest(Contract):
    scene_id: str
    index: int
    title: str
    prompt: str
    narration: str
    duration_s: float
    duration_in_frames: int
    fps: int
    width: int
    height: int
    palette: list[str]
    brand: str

class CodeSceneCode(BaseModel):
    html: str = Field(min_length=32, max_length=24_000)
    css: str = Field(default="", max_length=16_000)
    js: str | None = Field(default=None, max_length=16_000)

class CodeSceneResult(BaseModel):
    scene_id: str
    index: int
    code: CodeSceneCode
    fallback: bool = False
```

* `InputModel = CodeSceneRequest`, `OutputModel = CodeSceneResult`, `port="llm"`.
* Uses `StructuredRunner` like every other LLM agent (`app/agents/generation.py:106-116`).
* Scene code is then run through `app/services/scene_code.py::validate(code) -> list[str]`.
* On validation failure: one repair round (feed the errors back), then `fallback=True` with an
  empty code so the renderer uses `GenerativeScene`.
* Register in `app/agents/registry.py` next to `visual_generation`, `category="generation"`,
  `stage="visual"`, `port="llm"`.

### 4.3 `app/services/scene_code.py` (new) — the safety gate

This is the part that makes LLM-authored JS acceptable. It returns a list of problems; any
problem means the code is not rendered.

**Static checks (regex/AST-free, fast):**

| Rule | Reject |
| --- | --- |
| Forbidden APIs | `fetch(`, `XMLHttpRequest`, `import(`, `new Function`, `eval(`, `localStorage`, `indexedDB`, `document.cookie`, `top.`, `parent.`, `opener` |
| Non-determinism | `Math.random`, `Date.now`, `new Date`, `performance.now`, `setTimeout`, `setInterval`, `requestAnimationFrame` |
| External loads | `http://`, `https://`, `//`, `@import`, `<img`, `url(` outside `data:`, `<link`, `<script src` |
| Size | `len(html+css+js) > 48_000` |
| Structure | `#stage` referenced or a single root wrapper; balanced `<div>`/`</div>` count sanity |
| Contract | no `window.__seek`, no `window.__timelines` reassignment to a non-array |
| `url()` | target must start with `data:` or `#` (inline SVG paint servers are legitimate) |

`url()` is checked by extracting every reference and inspecting its target, not by a regex
lookahead. The regex version rejected `url(#grad)` — a normal SVG gradient reference — and reported
an empty "found" value, which made the repair round useless. Both are covered by regression tests.

**Strip, don't reject:** `<!doctype>`, `<html>`, `<head>`, `<body>`, `<script>` tags around `js`,
`<style>` tags around `css` — models drift, and stripping is safe.

**Runtime checks (defence in depth, optional phase 2):** render frame 0 and the last frame as
stills in CI; if either is blank/near-blank, flag the scene. This is the only reliable way to catch
a timeline that throws.

### 4.4 `app/workflow/pipeline.py::_stage_visual`

Third branch beside `generative` (line 829):

```
if visual_mode == "code":
    # build CodeSceneRequest per scene from the storyboard row + channel palette
    ctx, result = await self._run_agent(workflow, channel, "code_scene", payload, stage="visual")
    for each scene:
        storage.put_bytes(f"p/{project}/code/scene-{i:02d}-{sha[:10]}.json", json_bytes)
        MediaAsset(kind="code", key=…, meta={"fallback": …})
    log "Visuals: code mode — N scenes written by <model>"
```

Cost is recorded automatically by `_record_usage` (LLM tokens), so `costs.png` keeps working.

### 4.5 `app/services/storyboard.py`

* `_asset_url` already maps any `MediaAsset`; add `kind == "code"` → `codeKey` instead of `assetKey`.
* `build_spec` emits `"mode": visual_mode` and `"codeKey": …` when present.
* `actual_duration` is unchanged — duration still comes from narration.

### 4.6 `app/services/render.py`

* Line 268: treat `code` like `generative` for the "missing image" check.
* `_spec_for` passes the resolved code asset path through `assets` like images.

### 4.7 Preview / QC / API surface

| Surface | Change |
| --- | --- |
| `video.py::validate` | unchanged — MP4 is MP4 |
| `video.py::preview` (stills) | works; add a still at frame 0 for code scenes |
| `api/v1/video.py` | no change |
| `scripts/compare_visual_modes.py` | add a `D-code` case so the operator can compare A/B/C/D |

---

## 5 · Web changes

Shipped:

| File | Change |
| --- | --- |
| `channels/new-channel.tsx` | third option `Written by AI (HTML/CSS/GSAP)` with a hint |
| `channels/[id]/channel-forms.tsx` | same option, widened union |
| `content/[id]/scene-code.tsx` | "scene code" panel: HTML/CSS/GSAP of a scene, read from the API |
| `content/[id]/page.tsx` | renders that panel under scenes that have code |

Not verified: the deposit of `CodeScene` inside the dashboard `<Player>`. The composition is the
same one the renderer uses, but the player runs in the browser at a different size; check the
9:16 and 16:9 channels manually before relying on preview for code scenes.

---

## 5b · Resume and retry

A retry resumes; it does not restart. `ProductionPipeline.run()` resolves each stage's output from
the database and only executes a stage whose output is missing:| Stage | Reused when |
| --- | --- |
| planning | `workflow.project_id` points at a live project |
| script | the project has a current `Script` |
| storyboard | the current script has a current `Storyboard` |
| voice | every scene has narration audio with a positive duration and a storage key |
| visuals (`image`) | every scene has a `MediaAsset(kind="image")` |
| visuals (`code`) | every scene has a `MediaAsset(kind="code")` |
| render | a `VideoRender(status="ready")` exists for the project |
| QC | the latest report judged **this** render (`report.render_id == render.id`) |
| SEO | `metadata_json["seo"]` exists and a thumbnail was produced |

Each skipped stage writes `<stage> reused · <detail>` to the workflow log, so the log explains where
the money went.

Partial work is regenerated **per scene**, not per stage: a run that coded four of six scenes pays
for two, and TTS re-synthesises only scenes without usable audio. This matters most in code mode,
where every scene is a separate LLM call.

To start genuinely fresh, start a new workflow — do not retry a failed one.

### Empty responses from reasoning models

A reasoning model can spend its whole `max_tokens` budget on hidden reasoning and return an empty
`content` block. `StructuredRunner` now reports "got an empty response … likely exhausted
max_tokens" instead of `Invalid JSON: EOF`, and `CodeSceneAgent` raises the output budget to at least
8192 tokens.

Both failure kinds are contained per scene:

| Failure | Before | Now |
| --- | --- | --- |
| empty/unparseable response | raised, killed the workflow | scene falls back to generative |
| gateway 5xx / timeout / rate limit | raised, killed the workflow | scene falls back to generative |

The workflow log keeps the reason (`[structured_output] …` or `[provider] …`) on the scene, and tokens
already spent are still billed.

One more defect surfaced while testing this on a real failed run: storage was injected only for the
`image`/`tts`/`video`/`youtube` ports, so the code-scene stage raised "Storage is not configured"
**after** the LLM had already been paid for. `build_context` now injects storage for every agent,
because storage is not a provider — it is always constructible and has no credential of its own.

## Motion

Motion is the product, and it is **open-ended**: the model is the motion designer, not a recipe
follower.

* `validate_scene_code` rejects a scene that goes still after its entrance (`missing_loop`). Any of
  these satisfy it: `repeat: -1`, a nested repeating timeline, or a function pushed onto
  `window.__frameFns`.
* `window.__frameFns` is the escape hatch for motion a tween cannot express — per-particle maths,
  canvas drawing, physics-like arcs. The runtime calls every hook with `(t, frame)` after seeking the
  timelines, once per rendered frame: pure time, no clock. `scripts/frame-fns-check.mjs` proves the
  hook actually receives the scene time.
* The prompt deliberately carries **no list of approved animations**. It states the goal,
  encourages a camera move or subject motion that fits the line, and leaves the choice to the model.
  Each scene also receives the previous and next line so a shot can set up what comes next.

The earlier version of this doc described a fixed vocabulary (breathing, blinking, arm rotation).
That was removed: it made every scene look the same and prevented the model from inventing the motion
the scene actually needed.

### Render jobs and the idempotency key

`render_jobs.idempotency_key` is **UNIQUE**, so exactly one row may ever carry a given key. The
enqueue lookup must therefore see *every* status, not just active ones — a `failed` row still owns
the key. Looking only for active/succeeded rows made the next insert raise
`UniqueViolationError`, which surfaced as `PendingRollbackError` **outside** `_stage_render`'s
handlers: the workflow stopped with no `error_message`, right after `visuals reused`. That is the
hardest class of bug here, because nothing appears in the log.

`resolve_existing_job()` now decides for one row:

| Existing status | Action |
| --- | --- |
| `queued` … `validating` | reuse — a job is computing |
| `succeeded` | reuse — the video already exists |
| `failed` | requeue — reset the same row (attempt count reset when exhausted) |
| `cancelled` | raise `ConflictError` — never silently revive an operator's cancellation |

Two more guards keep one project from rendering twice: `_stage_render` waits on an in-flight job
instead of queueing another, and `_fail_orphan_renders()` closes `VideoRender` rows whose process
died (status `rendering` with no job behind it), which otherwise make every later attempt wait.

## Editing the token budget`code_scene` is the agent that writes the scene programmes, and it takes its budget from the agent's
**max tokens** setting (`Agents → Code Scene → max tokens`). An explicit value always wins; the 8192
floor only applies when the agent was never configured. The number must fit the model's real output
ceiling — a reasoning model that returns an empty body has usually spent the whole budget before
emitting any text.

The `visual_generation` (image) agent has no token budget: image providers do not take one, so its
field stays read-only.

---

## 6 · Testing
Shipped:

| Layer | Test |
| --- | --- |
| Unit (Python) | `tests/test_scene_code.py` — 20 cases covering every validator rule, stripping and the renderable guard |
| Unit (Python) | `tests/test_code_mode.py` — spec carries `codeKey`; image mode never stages code; agent stores, repairs once, falls back, requires an LLM |
| Unit (Python) | `tests/test_resume_and_fallbacks.py` — per-stage reuse, per-scene regeneration, empty-response fallback and the explanatory error |
| Engine | `scripts/code-scene-check.mjs` — renders a code scene, asserts three frames have contrast (a flat frame is a blank scene) |
| Engine | `scripts/code-scene-determinism.mjs` — same frame twice → identical SHA-256 |
| Renderer | `apps/api/scripts/check_code_scene.py` — full `RemotionRenderer.render` path, then `validate()` (h264, aac, A/V sync, faststart) |
| Suite | `apps/api` pytest: 140 passed |

Still manual: create a channel with `visual_mode=code`, run the workflow, inspect the "scene code"
panel, preview and download the MP4.

---

## 7 · Rollout order

1. **Engine proof** — `CodeScene` + `code-runtime.ts` + sample scene. Done.
2. **Validator** — `scene_code.py` + tests. Done.
3. **Agent** — `code_scene` (own module `agents/code_scene.py`) + registry. Done.
4. **Pipeline** — `_stage_visual` branch, `storyboard.py`, `render.py`, `render.mjs` staging and
   `assets.mjs` vendor staging. Done.
5. **Schema + UI** — `Literal` widening, channel forms, content detail panel. Done.
6. **Docs** — this file, `docs/10-video-engine.md`, `docs/04-agents-and-workflows.md`. Done.

### Rollback

Steps 1–3 are additive and dead code if unused. Step 4 is behind a `visual_mode` value; reverting
the pipeline branch leaves any `code` channel falling back to `generative` without a migration.

---

## 8 · Risks and open questions

Resolved by the spike / shipped behaviour:

| Risk | Outcome |
| --- | --- |
| iframe + sandbox blocks `contentDocument` | Confirmed for `write()` and for `allow-scripts`-only sandboxes. Shipped `srcDoc` + `postMessage`. |
| Non-deterministic code → shimmering frames | Validator bans `Math.random`/timers/`new Date`; determinism check renders a frame twice. |
| LLM writes JS that throws → black scene | One repair round, then `GenerativeScene` fallback flagged in the workflow log. |

Still open:

| Risk | Mitigation |
| --- | --- |
| A timeline that throws but passes static validation | The per-frame try/catch records into `window.__codeErrors`; nothing reads it yet. Wire it to a browser log handler in `render.mjs` and fail the scene. |
| Render time on long code videos | Measured: 168-frame two-scene check ≈ 700 KB output; a full production-length benchmark is still missing. |
| Fonts | The prompt restricts the model to Georgia / system-ui / ui-monospace; the sandbox has no web fonts. |
| GSAP licence | Core only. Premium plugins are not installed, so they cannot be used. |
| Dashboard `<Player>` preview of code scenes | Not manually verified at 9:16 and 16:9. |

Open questions for the reviewer:

1. Should `code` be a channel-level mode, or per-scene so a channel can mix AI images with code
   scenes? The schema supports per-scene; the pipeline is channel-level. **Recommendation:** ship
   channel-level, allow per-scene overrides later.
2. Should the generated code be editable (not just readable) in the dashboard? Currently read-only;
   changes go through a regenerate. **Recommendation:** keep it read-only until the validator runs
   on user edits too.
