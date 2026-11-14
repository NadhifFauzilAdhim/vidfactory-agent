# 10 · Video engine (Remotion)

Remotion is the **only** composition and rendering engine. Scenes, transitions, captions, music,
sound effects, intro/outro and the final encode are all produced by Remotion's React renderer, driven
by `renderMedia()` from `@remotion/renderer`. There is no second pipeline: no MoviePy, no OpenCV, no
hand-written FFmpeg filter graphs. FFmpeg exists only inside Remotion (its encoder) plus `ffprobe`
for **read-only** inspection of a finished file.

---

## 1 · Where things live

| Path | Responsibility |
| --- | --- |
| `services/media-engine/remotion/src/` | compositions, templates, scene components, timeline maths |
| `services/media-engine/remotion/render.mjs` | server-side CLI: `bundle → selectComposition → renderMedia` |
| `apps/api/app/services/storyboard.py` | turns stored scenes + assets into the render spec (JSON) |
| `apps/api/app/services/render.py` | durable job queue: claim, execute, retry, cancel, validate, store |
| `apps/api/app/adapters/video.py` | `RemotionRenderer` — runs the Node CLI, streams progress, validates |
| `apps/api/app/api/v1/video.py` | REST: queue/list/status/cancel/retry/spec/validation |
| `apps/web/src/components/domain/video-player.tsx` | `<Player>` preview bound to the same composition |
| `apps/api/alembic/versions/c4f9a2d13e57_render_jobs.py` | `render_jobs`, `render_validations`, `render_cost_logs` |

## 2 · Composition model

One composition (`StoryboardVideo`) renders every template. It owns:

```
intro card → scene 1 → … → scene N → outro card
                ↑ overlapping transitions
narration (per scene, absolute timeline) · sound effects · music bed with ducking
captions (React components, not a subtitle file)
```

A **template** supplies only the look — title card, outro card and how a scene is framed:

| id | Format | Identity |
| --- | --- | --- |
| `youtube-editorial` | 16:9 | white paper, ink rules, serif lower thirds, lime accent |
| `childrens-story` | 16:9 | pastel ground, rounded vignette, sticker badge |
| `educational` | 16:9 | dark split frame, didactic side panel, high-contrast subtitles |
| `storytelling` | 16:9 | cinematic letterbox, slow Ken Burns, serif overlay titles |
| `shorts` | 9:16 | full-bleed, stacked title block, chunky subtitles |
| `youtube-shorts` | 9:16 | the editorial identity in vertical |

A scene is drawn one of three ways (`StoryboardScene.mode`):

| mode | source | notes |
| --- | --- | --- |
| `asset` (default) | one generated image per scene | Ken Burns / video clip |
| `generative` | procedural CSS from the prompt hash | no provider call |
| `code` | HTML/CSS/GSAP written by the LLM | sandboxed iframe, see `docs/11-code-scenes.md` |

Code scenes render inside `<CodeScene>` (`src/code-scene.tsx`): the document is mounted once with
`srcDoc`, `sandbox="allow-scripts"`, and each frame is delivered by `postMessage` to the runtime,
which seeks every paused GSAP timeline. `render.mjs` inlines the staged scene programme into the
props and stages `vendor/gsap.min.js` via `assets.mjs`.

Adding a template = one file in `src/templates/` + one entry in `src/catalogue.tsx` + one entry in
`apps/api/app/services/storyboard.py::TEMPLATES`. The renderer, timeline, audio graph and player
code are untouched.

### Timeline

`src/timeline.ts::buildTimeline()` is the single source of truth and is shared by the composition,
the dashboard player and the API's duration validation:

* a scene lasts `max(0.5s, narration)` so speech is never clipped (or the storyboard estimate when a
  scene has no narration);
* scene N+1 starts `transitionFrames` before scene N ends (crossfade/slide/wipe are overlaps, so
  narration stays continuous);
* an overlap can never exceed a third of either neighbour;
* intro/outro frames are added around the body.

`storyboard.py::actual_duration()` mirrors this maths so QC and the renderer agree before a single
frame is encoded.

## 3 · Storyboard input contract

Everything the composition needs is plain JSON — no prompts, no database access:

```jsonc
{
  "spec": {
    "version": 1,
    "title": "The Lantern Path",
    "brand": "FACTORY",
    "templateId": "youtube-editorial",
    "width": 1920, "height": 1080, "fps": 30,
    "accent": "#D6FF45", "background": "#FFFFFF", "ink": "#111111",
    "captions": {"enabled": true, "mode": "lower-third"},
    "defaultTransition": "fade", "transitionDurationS": 0.6,
    "intro": {"enabled": true, "durationS": 2.4, "title": "…", "subtitle": "…"},
    "outro": {"enabled": true, "durationS": 3, "headline": "The end.", "subline": "…", "cta": "…"},
    "music": {"url": null, "volume": 0.12, "duckUnderNarration": true, "fadeInS": 1.2, "fadeOutS": 2},
    "scenes": [
      {
        "id": "…", "index": 1, "title": "Setting out",
        "assetKey": "a-<asset-uuid>",        // → imageSrc by the CLI
        "narrationKey": "a-<asset-uuid>",    // → narrationSrc by the CLI
        "durationS": 5.3, "transition": "fade",
        "caption": "The sun went down behind the hill…",
        "animation": {"kenBurns": "in", "entrance": "rise"},
        "elements": [{"kind": "progress", "position": "bottom"}],
        "sfx": []
      }
    ]
  }
}
```

Asset keys are resolved by the render CLI to bundle-relative paths after copying the files into a
temporary public directory (browsers cannot load `file://`). The staged copy is deleted afterwards.

## 4 · Render pipeline

```
POST /video/projects/{id}/renders          →  202 + job (status=queued)
   │                                             (rendering never blocks the request)
   ▼
RenderService worker (claim: queued → bundling, atomic UPDATE)
   │
   ├─ storyboard spec rebuilt from the DB (scene re-render picks up new images only)
   ├─ node render.mjs --job job.json
   │     bundle()          → progress 0–5
   │     selectComposition → duration/fps/size
   │     renderStill()     → preview poster
   │     renderMedia()     → rendering 5–70 → encoding 70–95 → muxing 95–99
   ├─ validate()           → ffprobe: dimensions, fps, duration, audio, A/V sync, faststart
   ├─ storage.put_bytes()  → p/<project>/renders/<job>.mp4 + poster
   └─ succeeded → VideoRender becomes the project's current video
```

Status values: `queued → bundling → rendering → encoding → validating → succeeded | failed | cancelled`.

* **Progress** is persisted on every ≥2 % change; `GET /video/renders/{id}/status` adds an ETA.
* **Cancellation** writes a sentinel file the CLI polls every 500 ms; it then calls Remotion's own
  cancel signal. The job becomes `cancelled` and the partial file is discarded.
* **Retries** are automatic only for transient infrastructure failures (browser crash, engine
  restart, timeout). Invalid input, missing assets and failed validation are not retried.
* **Concurrency** is bounded by `RENDER_CONCURRENCY` (each render is already multi-threaded by
  Remotion via `REMOTION_CONCURRENCY`).
* **Ownership**: `queued → bundling` is a conditional `UPDATE`, so the API and a separate worker can
  both watch the queue without ever rendering the same job twice.
* **Cleanup**: staged assets, the raw render and preview stills live in a temp directory that is
  deleted after upload.

### Where the queue runs

`RENDER_IN_API=true` (default) runs it in the API process — a single-process install renders with no
extra setup. For heavy installs set `RENDER_IN_API=false` and run
`python -m app.worker --render-only` on a render machine. Both read the same `render_jobs` table.

## 5 · Quality control

`renderer.validate()` inspects the finished MP4 with `ffprobe` (never re-encodes) and records a
`render_validations` row:

| check | what it proves |
| --- | --- |
| `file_integrity` | file exists and is larger than 50 KB |
| `video_stream` | an H.264 video stream is present |
| `dimensions` | width/height match the storyboard |
| `frame_rate` | fps matches the storyboard (within 0.01) |
| `duration` | within 6 % (or 0.75 s) of the composed duration |
| `audio_stream` | an AAC audio track exists (narration/music) |
| `av_sync` | audio and video end together (AAC priming tolerance 0.35 s) |
| `faststart` | `moov` precedes `mdat`, so streaming starts immediately |

The QC agent consumes this row as its `media_integrity` check and warns loudly when a render was
never validated, instead of silently trusting the file size.

Subtitle timing is evaluated inside the composition: cues are split on sentence boundaries and timed
by character weight across the scene, so they track narration without a separate timing pass.

## 6 · Dashboard

* `/content/[id]` embeds the rendered MP4.
* `VideoPlayer` mounts Remotion's `<Player>` with the **same** composition code the renderer uses.
  Props come from `GET /video/renders/{id}/spec`, so the preview matches the export.
* Scene chips seek to exact frame boundaries; transport controls, timeline readout and the current
  scene are derived from the shared `buildTimeline()`.
* The studio (`npm run studio` in `services/media-engine/remotion`) edits the same compositions with
  sample props.

## 7 · Setup

```bash
# 1 · Remotion project (Node 18+; Remotion 4.0.541, all @remotion/* packages pinned equal)
cd services/media-engine/remotion
npm install
npx remotion compositions src/index.tsx     # sanity check: six compositions

# 2 · API
cd apps/api
alembic upgrade head
python -m uvicorn app.main:app --port 8001  # RENDER_IN_API=true renders in-process
```

### Docker

The API image bakes the engine in, including GSAP for code scenes. `assets.mjs` stages
`vendor/gsap.min.js` into the render's public dir, and the image build runs
`node scripts/code-scene-check.mjs` so a broken sandbox or a missing dependency fails the build.

```bash
docker compose --profile full build api
docker compose --profile full up -d
# smoke test: renders a code scene through the real adapter, then validates the MP4
docker compose --profile full exec api python /app/infrastructure/docker/container_check.py
```

`GET /api/v1/video/engine` reports Node version, Remotion version, whether `node_modules` is present
and the available templates/transitions.

## 8 · Licensing

Remotion is source-available, not OSI-open-source. The free licence covers individuals, teams of up
to 3, non-profits and evaluation. Companies with more than 3 people need a **Company Licence**
(https://www.remotion.dev/docs/license). Set `REMOTION_LICENSE_KEY` and
`REMOTION_LICENSE_ACKNOWLEDGED=true` after obtaining one. This must be verified before any
production deployment.

## 9 · Known limitations

* Rendering requires Node 18+ and Chromium (Remotion downloads a headless shell on first run).
* The renderer needs **local** storage (`STORAGE_BACKEND=local`) because Remotion reads files from
  disk; remote object storage must be staged to local disk first.
* One render at a time per process by default; long videos are not split across machines.
* Word-level karaoke captions rely on synthetic timing (no forced alignment yet).
* Scene-only re-render produces a clip, not a patched master; the full render remains authoritative.
