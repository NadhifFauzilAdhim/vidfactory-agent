# 04 · Agent contracts & workflow pipeline

## Agent contract

Every agent is a class implementing `Agent` (see `services/ai-engine/factory_ai/base.py`):

```python
class Agent(Protocol):
    key: ClassVar[str]                       # stable registry key, e.g. "script_writer"
    InputModel: type[BaseModel]
    OutputModel: type[BaseModel]
    async def run(self, ctx: AgentContext, payload: InputModel) -> AgentResult[OutputModel]
```

`AgentResult` carries `output`, `usage` (tokens, cost, latency), `artifacts` (storage keys),
`warnings`, `model`, `provider`, and is persisted into `workflow_tasks.output_json`.

LLM-backed agents are *not* separate hand-written prompts scattered in code. They are declarative:

```python
LLMAgent(
    key="script_writer",
    input_model=ScriptBrief,
    output_model=ScriptDraft,
    system_prompt=SCRIPT_SYSTEM,
    temperature=0.7,
)
```

`LLMStructuredRunner` instructs the provider with the output JSON schema, parses the response with
Pydantic, and on validation failure performs **one repair round** that includes the error text, then
raises `StructuredOutputError`.

## Registry

| key | agent | stage | input → output |
|-----|-------|-------|----------------|
| `content_planner` | Content Planner | planning | `PlannerInput` → `ContentBrief` |
| `script_writer` | Script Writer | script | `ScriptBrief` → `ScriptDraft` |
| `storyboard` | Storyboard | storyboard | `StoryboardInput` → `StoryboardDraft` |
| `voice_generation` | Voice Generation | voice | `VoiceRequest` → `VoiceResult` (TTS port + storage) |
| `visual_generation` | Visual Generation | visual | `VisualRequest` → `VisualResult` (image port + storage) |
| `code_scene` | Code Scene | visual | `CodeSceneRequestBatch` → `CodeSceneBatchResult` (LLM writes HTML/CSS/GSAP per scene; see `docs/11-code-scenes.md`) |
| `video_production` | Video Production | rendering | `RenderRequest` → `RenderResult` (renderer port) |
| `quality_control` | Quality Control | qc | `QcInput` → `QualityReport` (rules + LLM) |
| `seo_metadata` | SEO & Metadata | seo | `SeoInput` → `MetadataDraft` (+ thumbnail) |
| `publishing` | Publishing | publishing | `PublishRequest` → `PublishResult` (YouTube port) |
| `analytics` | Analytics | analytics | `AnalyticsWindow` → `PerformanceInsight` |

Registration is explicit (no import magic): `REGISTRY.register(...)` in `factory_agents/registry.py`.

## Structured output highlights

`ContentBrief` — title, topic, angle, audience, brief, creative_direction, category,
`estimated_duration_s`, `safety_notes[]`, `rationale`.
`ScriptDraft` — title, description, narration, `scenes[]` each with index, title, narration,
`dialogue[{character, line, emotion}]`, `duration_s`, `visual_instruction`, `sfx[]`, `transition`.
`StoryboardDraft` — `scenes[]` with camera, composition, characters, background, action,
`image_prompt`, `negative_prompt`, sfx, duration.
`QualityReport` — `score 0–100`, `approval_status`, `checks[{id, passed, weight, note}]`,
`issues[{severity, area, message, regenerate[]}]`, `warnings`, `recommendations`.

## Pipeline (LangGraph-compatible, Temporal-durable)

```
Scheduler ─▶ ProductionWorkflow(project_id)
   planning      content_planner         ── cost guard: monthly budget
   script        script_writer           ── schema validation, word budget
   storyboard    storyboard
   voice         voice_generation        ── per-scene audio → R2
   visual        visual_generation       ── per-scene image → R2  (visual_mode=image)
                 code_scene              ── per-scene HTML/CSS/GSAP → R2  (visual_mode=code)
   render        video_production        ── FFmpeg/Remotion → mp4
   qc            quality_control         ── score; may loop back to visual/voice (max N)
   seo           seo_metadata            ── metadata + thumbnail concept
   approval      approval_requests row   ── wait for signal (human in the loop)
   publish       publishing              ── YouTube upload/schedule
   analytics     analytics               ── deferred snapshot collection
```

Each node is a Temporal activity with its own retry policy and heartbeat. The workflow updates
`production_workflows.stage/progress` after each activity so the monitor is a projection of state.

## Regeneration

QC returns `regenerate: ["scene:3.image", "scene:5.audio"]`. The workflow re-runs only the targeted
activities, increments `retry_count`, and re-runs QC once the new render is ready. If QC fails after
`max_retries`, status becomes `FAILED` and an approval of type `review` is raised.

## Cost accounting

Every port call returns `Usage(cost_usd, tokens/units, latency)`. The runner writes one
`ai_usage_logs` row per call keyed by `request_key = f"{workflow_key}:{stage}:{scene}:{attempt}"`.
Dashboard spend is `sum(cost_usd)` over `ai_usage_logs` — no estimate tables.

## Provider ports

| Port | Real adapters | Dev adapter |
|------|---------------|-------------|
| `LLMProvider` | OpenAI (`/v1/chat/completions` json_schema), Google Gemini (`generateContent`, `responseSchema`) | `MockLLM` (deterministic schema-valid fixtures) |
| `TTSProvider` | Cartesia (`/tts/bytes`), ElevenLabs (`/v1/text-to-speech`) | `MockTTS` (silent WAV with true duration) |
| `ImageProvider` | OpenAI Images, Gemini `imagen` / Replicate | `MockImage` (Pillow-generated scene cards) |
| `VideoRenderer` | FFmpeg + Remotion | same (local FFmpeg required) |
| `Storage` | Cloudflare R2 (S3-compatible) | local filesystem under `storage/` |
| `YouTubeService` | YouTube Data API v3 resumable upload + Analytics API | `MockYouTube` (records job, no upload) |
