# Testing code scenes

Manual walkthrough for `visual_mode="code"`, written for the Docker local stack.

## 0 · Rebuild and start

The API image contains the engine, GSAP and the code-scene components, so it must be rebuilt after
any change in `services/media-engine/remotion` or `apps/api`.

```bash
docker compose --profile full build api
docker compose --profile full up -d
```

The build runs `node scripts/code-scene-check.mjs`; if it goes green, headless Chromium can mount a
code scene and seek GSAP inside the image.

### Smoke test (no UI, no LLM)

```bash
docker compose --profile full exec api python /app/infrastructure/docker/container_check.py
```

Renders three scenes — two plain, one code — through `RemotionRenderer`, then validates the MP4
(H.264, AAC, A/V sync, faststart). This is the fastest "is it wired correctly" check.

### Real LLM check (one scene, ~1 minute)

The smoke test uses a hand-written scene. To confirm the LLM path, run a production with a code-mode
channel and read the workflow log; a healthy run shows
`Visuals: code mode — N/N scenes written by <model>`.

### Retrying a failed run

A retry resumes from the first incomplete stage (see `docs/11-code-scenes.md` §5b). To inspect it
without the UI:

```bash
docker compose --profile full exec api python /app/scripts/retry_workflow.py <WORKFLOW_ID>
```

Expected log lines on a run that failed at `visual_generation`:

```
planning reused · <title> (already produced by an earlier attempt)
script reused · v1 (already produced by an earlier attempt)
storyboard reused · v1 (already produced by an earlier attempt)
voice reused · 11 scenes (already produced by an earlier attempt)
```

If `planning` is **not** reused, the workflow has no `project_id` yet — it failed before planning
finished, and there is nothing to save.

## 1 · Create a channel in code mode

1. Open <http://localhost:3000> and sign in with the bootstrap admin from `.env`.
2. **Channels → New channel**.
3. Set **visuals** to **Written by AI (HTML/CSS/GSAP)**.
4. Fill the rest as usual (16:9 for a first try) and save.

The channel stores `visual_mode="code"`; the image provider is never called for this channel.

## 2 · Run the workflow

Start a production for that channel. The render pipeline now has a `code_scene` stage instead of
image generation:

| What to expect | Where |
| --- | --- |
| an LLM call, no image call | workflow log line `Visuals: code mode — N/N scenes written by <model>` |
| cost recorded as LLM tokens | Costs page |
| fallback notice | `..., K fall back to generative` when the validator rejected a scene twice |

If the log shows `0/N written`, the LLM output failed validation twice. Open the workflow log — the
warning lists the rejected rules. The usual culprits are `fetch`, `Math.random`, `setTimeout`, or a
missing `window.__timelines.push(...)`.

## 3 · Inspect the generated scene code

Open the project (`/content/<id>`). Under each scene that has code there is a **scene code** toggle.
It expands to three sections:

* `html` — the markup for the stage;
* `css` — the stylesheet;
* `gsap` — the timeline code.

This is exactly what the renderer runs; it is read-only and changes go through a regenerate.

## 4 · Render and watch the result

1. Render the project from the project page.
2. Confirm in the render metadata: `H.264 + AAC`, the channel's resolution, `faststart`.
3. Play the MP4. Code scenes must show their own composition with no white template border or
   scene index on top — the editorial template drops its chrome for `mode: "code"`.

Failures look like this:

| Symptom | Meaning |
| --- | --- |
| scene looks like layered gradients, no custom type | the scene fell back to `generative` (validation failed) |
| scene is blank/white | check the container log for `browser-error`; the timeline probably threw |
| GSAP animation missing but layout correct | `vendor/gsap.min.js` was not staged — rebuild the image |

## 5 · Studio preview (optional, no Docker)

```bash
cd services/media-engine/remotion
npm install
node -e "import('./assets.mjs').then(m => m.stageVendor('public', process.cwd()))"   # stages GSAP
npm run studio
```

Open `youtube-editorial` and go to scene **04 · Code scene**. Edit the `code` object in the Data
panel to try your own markup — the same props the agent produces.

## 6 · What is still unverified

* The dashboard `<Player>` preview of a code scene (9:16 and 16:9). Rendering is verified; the player
  is not.
* Scenes whose timeline throws at runtime still record into `window.__codeErrors`, but nothing reads
  that list yet, so the render can pass with a blank scene.
