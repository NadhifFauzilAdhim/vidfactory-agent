/** Shared contract types mirrored from the FastAPI schemas (`app/schemas.py`). */

export type MetricValue = {
  value: number;
  previous?: number | null;
  label: string;
  caption: string;
};

export type Role = "owner" | "admin" | "editor" | "viewer";

export type Session = {
  user: { id: string; email: string; name: string; avatar_color: string; last_login_at?: string | null };
  workspace: { id: string; name: string; slug: string; role: Role };
  workspaces: { id: string; name: string; slug: string; role: Role }[];
  expires_at: string;
};

export type ChannelSettings = {
  visual_style: string;
  palette: Record<string, string>;
  content_guidelines?: string | null;
  banned_topics: string[];
  made_for_kids: boolean;
  target_duration_seconds: number;
  cost_ceiling_usd: number;
  approval_mode: "always" | "below_score" | "never";
  min_quality_score: number;
  voice_provider?: string | null;
  voice_id?: string | null;
  thumbnail_style?: string | null;
};

export type Character = {
  id?: string;
  name: string;
  role: string;
  description: string;
  visual_prompt: string;
  voice_id?: string | null;
  color: string;
};

export type Channel = {
  id: string;
  name: string;
  handle: string;
  description: string | null;
  niche: string;
  audience: string;
  language: string;
  video_format: "16:9" | "9:16" | "1:1";
  accent_color: string;
  /** "image" = one generated image per scene · "generative" = Remotion composes scenes from prompts. */
  visual_mode?: "image" | "generative" | "code";
  is_active: boolean;
  youtube_channel_id: string | null;
  youtube_channel_title: string | null;
  youtube_connected_at: string | null;
  created_at: string;
  settings: ChannelSettings | null;
  characters: Character[];
  production_count: number;
  published_count: number;
  next_run_at: string | null;
};

export type Schedule = {
  id: string;
  channel_id: string;
  name: string;
  timezone: string;
  days_of_week: string[];
  production_count: number;
  start_time: string;
  publish_time: string;
  max_concurrent: number;
  require_approval: boolean;
  auto_publish: boolean;
  is_enabled: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
};

export type Provider = {
  id: string;
  kind: "llm" | "tts" | "image" | "video" | "youtube";
  slug: string;
  name: string;
  vendor: string;
  base_url: string | null;
  models: {
    id: string;
    name?: string;
    input_cost_per_million?: number;
    output_cost_per_million?: number;
    unit_cost?: number;
    unit?: string;
  }[];
  credential_fingerprint: string | null;
  has_credential: boolean;
  credential_status: "unset" | "active" | "disabled";
  credential_version: number;
  credential_updated_at: string | null;
  is_enabled: boolean;
  is_mock: boolean;
  priority: number;
  health: "unknown" | "ok" | "degraded" | "error";
  health_detail: string | null;
  health_checked_at: string | null;
  extra_config: Record<string, string | null>;
  created_at: string;
};

// --------------------------------------------------------------------------- tts

export type TTSProviderRow = {
  slug: string;
  label: string;
  mock: boolean;
  enabled: boolean;
  has_credential: boolean;
  credential_version: number;
  fingerprint: string | null;
  default_model: string | null;
  default_voice: string | null;
  default_output_format: string | null;
  base_url: string | null;
  docs: string | null;
  key_hint: string | null;
  usable: boolean;
  encryption_ready?: boolean;
  encryption_problem?: string | null;
};

export type TTSParam = {
  key: string;
  label: string;
  kind: "number" | "enum" | "boolean";
  minimum: number | null;
  maximum: number | null;
  default: unknown;
  options: { value: string; label: string }[];
  help: string | null;
};

export type TTSOutputFormat = {
  id: string;
  label: string;
  container: string;
  mime: string;
  sample_rate: number;
  bit_rate: number | null;
};

export type TTSModel = {
  id: string;
  name: string;
  description: string | null;
  max_chars: number | null;
  languages: string[];
  supports_dialogue: boolean;
  deprecated: boolean;
  params: TTSParam[];
  output_formats: TTSOutputFormat[];
};

export type TTSVoice = {
  voice_id: string;
  name: string;
  language: string | null;
  locale: string | null;
  gender: string | null;
  category: string | null;
  description: string | null;
  labels: Record<string, string>;
  own_voice: boolean;
  has_preview: boolean;
  preview_endpoint: string | null;
};

export type TTSGeneration = {
  id: string;
  provider: string;
  model: string;
  voice_id: string | null;
  language: string | null;
  text: string;
  characters: number;
  audio_url: string | null;
  audio_format: string | null;
  mime: string | null;
  duration_s: number;
  latency_ms: number;
  estimated_cost_usd: number;
  actual_cost_usd: number | null;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  error_code: string | null;
  error_message: string | null;
  trigger: string;
  is_test: boolean;
  mode: string;
  chunk_count: number;
  fallback_used: boolean;
  request_id: string | null;
  channel_id: string | null;
  workflow_id: string | null;
  content_id: string | null;
  created_at: string;
  config?: TTSResolvedConfig;
  chunk_manifest?: { index: number; characters: number; duration_s: number; latency_ms: number; preview: string }[];
  warnings?: string[];
};

export type TTSResolvedConfig = {
  provider: string;
  fallback_provider: string | null;
  model: string | null;
  voice_id: string | null;
  language: string;
  output_format: string | null;
  voice_settings: Record<string, unknown>;
  sources: Record<string, string>;
  fallback_on_transient: boolean;
  chain: { level: string; values: Record<string, unknown> }[];
};

export type TTSUsage = {
  window_days: number;
  totals: {
    generations: number;
    succeeded: number;
    failed: number;
    characters: number;
    audio_seconds: number;
    estimated_cost_usd: number;
    actual_cost_usd: number;
    avg_latency_ms: number;
  };
  by_provider: Record<string, TTSUsageBucket>;
  by_model: Record<string, TTSUsageBucket>;
  by_day: Record<string, TTSUsageBucket>;
};

export type TTSUsageBucket = {
  generations: number;
  characters: number;
  seconds: number;
  cost_usd: number;
  failed: number;
  avg_latency_ms: number;
};

export type Agent = {
  id: string;
  key: string;
  name: string;
  role: string;
  description: string;
  category: string;
  stage: string;
  port: "llm" | "tts" | "image" | "video" | "rules" | "youtube";
  is_enabled: boolean;
  provider_id: string | null;
  provider_name: string | null;
  model: string | null;
  temperature: number;
  max_tokens: number;
  system_prompt: string | null;
  default_system_prompt: string | null;
  params: Record<string, unknown>;
  executions: number;
  success_rate: number | null;
  avg_duration_ms: number | null;
  total_cost_usd: number;
  last_run_at: string | null;
  status: "active" | "paused" | "unconfigured";
};

export type WorkflowStatus =
  | "pending"
  | "planning"
  | "script_generation"
  | "storyboarding"
  | "voice_generation"
  | "visual_generation"
  | "rendering"
  | "quality_checking"
  | "awaiting_approval"
  | "publishing"
  | "published"
  | "failed"
  | "cancelled";

export type Workflow = {
  id: string;
  workflow_key: string;
  channel_id: string;
  channel_name: string;
  project_id: string | null;
  project_title: string | null;
  trigger: string;
  status: WorkflowStatus;
  stage: string;
  current_agent_key: string | null;
  progress: number;
  retry_count: number;
  cost_usd: number;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
};

export type WorkflowTask = {
  id: string;
  step_index: number;
  attempt: number;
  agent_key: string;
  name: string;
  status: "pending" | "running" | "succeeded" | "failed" | "skipped";
  provider: string | null;
  model: string | null;
  duration_ms: number | null;
  cost_usd: number;
  tokens_in: number;
  tokens_out: number;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
};

export type WorkflowLog = {
  id: string;
  level: string;
  message: string;
  context: Record<string, unknown>;
  at: string;
};

export type WorkflowDetail = Workflow & {
  tasks: WorkflowTask[];
  logs: WorkflowLog[];
  input: Record<string, unknown>;
};

export type ProjectStatus =
  | "draft"
  | "in_production"
  | "awaiting_approval"
  | "approved"
  | "rejected"
  | "scheduled"
  | "published"
  | "failed"
  | "archived";

export type Project = {
  id: string;
  channel_id: string;
  channel_name: string;
  title: string;
  status: ProjectStatus;
  language: string;
  format: string;
  estimated_duration_s: number | null;
  total_cost_usd: number;
  qc_score: number | null;
  thumbnail_url: string | null;
  created_at: string;
  published_at: string | null;
};

export type Scene = {
  id: string;
  index: number;
  title: string;
  narration: string;
  dialogue: { character: string; line: string; emotion?: string | null }[];
  camera: string | null;
  composition: string | null;
  characters: string[];
  background: string | null;
  action: string | null;
  image_prompt: string;
  sfx: string[];
  duration_s: number;
  transition: string;
  image_url: string | null;
  audio_url: string | null;
  audio_duration_s: number | null;
  code_url: string | null;
};

export type QualityReport = {
  score: number;
  status: "pass" | "review" | "fail";
  checks: { id: string; label: string; passed: boolean; weight: number; note: string }[];
  issues: { severity: string; area: string; message: string; scene_index?: number | null; regenerate: string[] }[];
  warnings: string[];
  recommendations: string[];
  regenerate: string[];
  created_at: string;
};

export type ProjectDetail = Project & {
  metadata: Record<string, unknown>;
  script: {
    id: string;
    title: string;
    description: string;
    hook: string | null;
    narration: string;
    word_count: number;
    estimated_duration_s: number;
    version: number;
  } | null;
  scenes: Scene[];
  assets: { id: string; kind: string; url: string; width: number | null; height: number | null; bytes: number | null; provider: string | null; created_at: string }[];
  renders: { id: string; status: string; url: string | null; duration_s: number; bytes: number | null; resolution: string; created_at: string | null }[];
  qc: QualityReport | null;
  workflow_id: string | null;
  approval_id: string | null;
};

export type Approval = {
  id: string;
  project_id: string;
  project_title: string;
  channel_name: string;
  workflow_id: string | null;
  kind: string;
  status: "pending" | "approved" | "rejected" | "expired";
  summary: {
    title?: string;
    qc_score?: number;
    qc_status?: string;
    duration_s?: number | null;
    cost_usd?: number;
    metadata?: {
      title?: string;
      description?: string;
      tags?: string[];
      thumbnail_text?: string;
      thumbnail_concept?: string;
      playlist?: string;
    };
  };
  created_at: string;
  decided_at: string | null;
  decision_note: string | null;
};

export type Overview = {
  totals: Record<
    "channels" | "generated" | "published" | "pending_approvals" | "active_workflows" | "failed" | "spend" | "success_rate",
    MetricValue
  >;
  production_activity: { date: string; created: number; published: number }[];
  agent_activity: {
    key: string;
    name: string;
    stage: string;
    executions: number;
    success_rate: number;
    avg_duration_ms: number;
    cost_usd: number;
  }[];
  channels: { id: string; name: string; handle: string; runs: number; is_active: boolean }[];
  recent_projects: Project[];
  recent_workflows: Workflow[];
  system: Record<string, { status: string; detail: string }>;
  providers: { id: string; name: string; kind: string; health: string; detail: string | null; is_mock: boolean; is_enabled: boolean }[];
};

export type Costs = {
  month_usd: number;
  previous_month_usd: number;
  budget_usd: number | null;
  daily: { date: string; cost_usd: number }[];
  by_provider: { provider: string; cost_usd: number; calls: number; tokens: number; share: number }[];
  by_agent: { agent: string; cost_usd: number; calls: number; tokens: number; share: number }[];
  by_model: { model: string; cost_usd: number; calls: number; tokens: number; share: number }[];
  by_channel: { channel_id: string; cost_usd: number; calls: number; tokens: number; share: number }[];
  per_minute_usd: number | null;
  budgets: { monthly: number | null; channel_ceiling: number | null };
};

export type Analytics = {
  configured: boolean;
  detail: string;
  totals: { views: number; watch_time_minutes: number; avg_view_duration_s: number; subscribers_gained: number; ctr: number };
  trend: { date: string; views: number; watch_time_minutes: number; subscribers: number }[];
  videos: {
    id: string;
    youtube_video_id: string;
    title: string;
    url: string;
    published_at: string | null;
    views: number | null;
    watch_time_minutes: number | null;
    avg_view_duration_s: number | null;
    subscribers_gained: number | null;
    is_mock: boolean;
  }[];
};

export type SystemStatus = {
  time: string;
  services: { name: string; status: string; detail: string }[];
  providers: { id: string; name: string; kind: string; health: string; detail: string | null; is_mock: boolean; is_enabled: boolean }[];
  scheduler: { enabled: boolean; next_run_at: string | null; active_schedules: number };
};

export type IntegrationConfig = {
  environment: string;
  google_oauth_configured: boolean;
  storage_backend: string;
  workflow_backend: string;
  ffmpeg_available: boolean;
  video_engine: {
    name: string;
    ok: boolean;
    version: string | null;
    node: string | null;
    detail: string;
  };
  credential_vault_ready: boolean;
  scheduler_enabled: boolean;
};

export type Member = {
  id: string;
  user_id: string;
  email: string;
  name: string;
  role: Role;
  created_at: string;
  last_login_at: string | null;
};

export type AuditLog = {
  id: string;
  action: string;
  actor_id: string | null;
  target_type: string | null;
  target_id: string | null;
  created_at: string;
};

export type ApiKey = {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  created_at: string;
  last_used_at: string | null;
};
