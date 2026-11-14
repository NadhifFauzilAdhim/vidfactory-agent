"""Request/response schemas for the public API."""

from __future__ import annotations

import uuid
from datetime import datetime, time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# --------------------------------------------------------------------------- auth


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class WorkspaceBrief(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    role: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: str
    name: str
    avatar_color: str
    last_login_at: datetime | None = None


class SessionOut(BaseModel):
    user: UserOut
    workspace: WorkspaceBrief
    workspaces: list[WorkspaceBrief]
    expires_at: datetime


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10, max_length=200)


class InviteMemberRequest(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=120)
    role: Literal["admin", "editor", "viewer"] = "editor"
    password: str | None = Field(default=None, min_length=10, max_length=200)


class MemberOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    email: str
    name: str
    role: str
    created_at: datetime
    last_login_at: datetime | None = None


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    action: str
    actor_id: uuid.UUID | None
    target_type: str | None
    target_id: str | None
    created_at: datetime


# --------------------------------------------------------------------------- channels


class CharacterIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    role: str = "protagonist"
    description: str = ""
    visual_prompt: str = ""
    voice_id: str | None = None
    color: str = "#111111"


class CharacterOut(CharacterIn):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID


class ChannelSettingsIn(BaseModel):
    visual_style: str = "Soft gouache storybook illustration"
    palette: dict[str, str] = Field(default_factory=lambda: {"primary": "#D6FF45"})
    content_guidelines: str | None = None
    banned_topics: list[str] = Field(default_factory=list)
    made_for_kids: bool = True
    target_duration_seconds: int = Field(default=240, ge=60, le=1800)
    cost_ceiling_usd: float = Field(default=4.5, ge=0, le=500)
    approval_mode: Literal["always", "below_score", "never"] = "always"
    min_quality_score: float = Field(default=85, ge=0, le=100)
    voice_provider: str | None = None
    voice_id: str | None = None
    thumbnail_style: str | None = None


class ChannelCreate(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    handle: str = Field(min_length=2, max_length=80)
    description: str | None = None
    niche: str = "kids-stories"
    audience: str = "Children 3-7"
    language: str = "en"
    video_format: Literal["16:9", "9:16", "1:1"] = "16:9"
    accent_color: str = "#D6FF45"
    visual_mode: Literal["image", "generative", "code"] = "image"
    settings: ChannelSettingsIn | None = None


class ChannelUpdate(BaseModel):
    name: str | None = None
    handle: str | None = None
    description: str | None = None
    niche: str | None = None
    audience: str | None = None
    language: str | None = None
    video_format: Literal["16:9", "9:16", "1:1"] | None = None
    accent_color: str | None = None
    visual_mode: Literal["image", "generative", "code"] | None = None
    is_active: bool | None = None
    settings: ChannelSettingsIn | None = None


class ChannelOut(BaseModel):
    id: uuid.UUID
    name: str
    handle: str
    description: str | None
    niche: str
    audience: str
    language: str
    video_format: str
    accent_color: str
    visual_mode: str = "image"
    is_active: bool
    youtube_channel_id: str | None
    youtube_channel_title: str | None
    youtube_connected_at: datetime | None
    created_at: datetime
    settings: ChannelSettingsIn | None = None
    characters: list[CharacterOut] = Field(default_factory=list)
    production_count: int = 0
    published_count: int = 0
    next_run_at: datetime | None = None


class ScheduleIn(BaseModel):
    name: str = "Daily production"
    timezone: str = "UTC"
    days_of_week: list[str] = Field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
    production_count: int = Field(default=1, ge=1, le=5)
    start_time: time = time(6, 0)
    publish_time: time = time(17, 0)
    max_concurrent: int = Field(default=1, ge=1, le=3)
    require_approval: bool = True
    auto_publish: bool = False
    is_enabled: bool = True


class ScheduleOut(ScheduleIn):
    id: uuid.UUID
    channel_id: uuid.UUID
    next_run_at: datetime | None
    last_run_at: datetime | None


# --------------------------------------------------------------------------- providers / agents


class ProviderModel(BaseModel):
    id: str
    name: str | None = None
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None
    unit_cost: float | None = None
    unit: str | None = None


class ProviderCreate(BaseModel):
    kind: Literal["llm", "tts", "image", "video", "youtube"]
    slug: str
    name: str
    vendor: str
    base_url: str | None = None
    api_key: str | None = Field(default=None, max_length=500)
    models: list[ProviderModel] = Field(default_factory=list)
    extra_config: dict[str, Any] = Field(default_factory=dict)
    priority: int = 100


class ProviderUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    models: list[ProviderModel] | None = None
    api_key: str | None = Field(default=None, max_length=500)
    is_enabled: bool | None = None
    priority: int | None = None


class ProviderOut(BaseModel):
    id: uuid.UUID
    kind: str
    slug: str
    name: str
    vendor: str
    base_url: str | None
    models: list[ProviderModel]
    credential_fingerprint: str | None
    has_credential: bool
    credential_status: str
    credential_version: int
    credential_updated_at: datetime | None
    is_enabled: bool
    is_mock: bool
    priority: int
    health: str
    health_detail: str | None
    health_checked_at: datetime | None
    extra_config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ProviderTestResult(BaseModel):
    ok: bool
    detail: str
    checked_at: datetime
    checks: list[dict[str, Any]] = Field(default_factory=list)


class AgentConfigIn(BaseModel):
    provider_id: uuid.UUID | None = None
    fallback_provider_id: uuid.UUID | None = None
    model: str | None = None
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_tokens: int = Field(default=4096, ge=256, le=128000)
    system_prompt: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    is_enabled: bool = True


class AgentOut(BaseModel):
    id: uuid.UUID
    key: str
    name: str
    role: str
    description: str
    category: str
    stage: str
    port: str
    is_enabled: bool
    provider_id: uuid.UUID | None
    provider_name: str | None
    model: str | None
    temperature: float
    max_tokens: int
    system_prompt: str | None
    default_system_prompt: str | None
    params: dict[str, Any]
    executions: int
    success_rate: float | None
    avg_duration_ms: int | None
    total_cost_usd: float
    last_run_at: datetime | None
    status: Literal["active", "paused", "unconfigured"]


class AgentRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    workflow_id: uuid.UUID
    agent_key: str
    status: str
    provider: str | None
    model: str | None
    duration_ms: int | None
    cost_usd: float
    error: str | None
    started_at: datetime | None
    created_at: datetime


# --------------------------------------------------------------------------- content


class SceneOut(BaseModel):
    id: uuid.UUID
    index: int
    title: str
    narration: str
    dialogue: list[dict] = Field(default_factory=list)
    camera: str | None
    composition: str | None
    characters: list = Field(default_factory=list)
    background: str | None
    action: str | None
    image_prompt: str
    sfx: list = Field(default_factory=list)
    duration_s: float
    transition: str
    image_url: str | None = None
    audio_url: str | None = None
    #: Scene programme written by the LLM (visual_mode="code"); served by /scenes/{index}/code.
    code_url: str | None = None
    audio_duration_s: float | None = None
    audio_provider: str | None = None
    audio_model: str | None = None
    audio_voice_id: str | None = None


class AssetOut(BaseModel):
    id: uuid.UUID
    kind: str
    url: str
    width: int | None
    height: int | None
    bytes: int | None
    provider: str | None
    created_at: datetime


class ProjectOut(BaseModel):
    id: uuid.UUID
    channel_id: uuid.UUID
    channel_name: str
    title: str
    status: str
    language: str
    format: str
    estimated_duration_s: int | None
    total_cost_usd: float
    qc_score: float | None
    thumbnail_url: str | None
    created_at: datetime
    published_at: datetime | None


class ProjectDetailOut(ProjectOut):
    metadata: dict[str, Any]
    script: dict[str, Any] | None
    scenes: list[SceneOut]
    assets: list[AssetOut]
    renders: list[dict[str, Any]]
    qc: dict[str, Any] | None
    workflow_id: uuid.UUID | None
    approval_id: uuid.UUID | None


class MetadataUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=240)
    seo: dict[str, Any] | None = None


class RegenerateRequest(BaseModel):
    target: Literal["scene_image", "scene_audio", "script", "storyboard"]


# --------------------------------------------------------------------------- workflows


class WorkflowOut(BaseModel):
    id: uuid.UUID
    workflow_key: str
    channel_id: uuid.UUID
    channel_name: str
    project_id: uuid.UUID | None
    project_title: str | None
    trigger: str
    status: str
    stage: str
    current_agent_key: str | None
    progress: int
    retry_count: int
    cost_usd: float
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class WorkflowTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    step_index: int
    attempt: int
    agent_key: str
    name: str
    status: str
    provider: str | None
    model: str | None
    duration_ms: int | None
    cost_usd: float
    tokens_in: int
    tokens_out: int
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None


class WorkflowLogOut(BaseModel):
    id: uuid.UUID
    level: str
    message: str
    context: dict[str, Any]
    at: datetime


class WorkflowDetailOut(WorkflowOut):
    tasks: list[WorkflowTaskOut]
    logs: list[WorkflowLogOut]
    input: dict[str, Any]


# --------------------------------------------------------------------------- approvals


class ApprovalOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    project_title: str
    channel_name: str
    workflow_id: uuid.UUID | None
    kind: str
    status: str
    summary: dict[str, Any]
    created_at: datetime
    decided_at: datetime | None
    decision_note: str | None


class ApprovalDecision(BaseModel):
    decision: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=1000)


# --------------------------------------------------------------------------- overview / costs / analytics


class MetricValue(BaseModel):
    value: float
    previous: float | None = None
    label: str
    caption: str


class OverviewOut(BaseModel):
    totals: dict[str, MetricValue]
    production_activity: list[dict[str, Any]]
    agent_activity: list[dict[str, Any]]
    channels: list[dict[str, Any]] = Field(default_factory=list)
    recent_projects: list[ProjectOut]
    recent_workflows: list[WorkflowOut]
    system: dict[str, Any]
    providers: list[dict[str, Any]]


class CostBreakdown(BaseModel):
    dimension: str
    rows: list[dict[str, Any]]
    total_usd: float


class CostOverviewOut(BaseModel):
    month_usd: float
    previous_month_usd: float
    budget_usd: float | None
    daily: list[dict[str, Any]]
    by_provider: list[dict[str, Any]]
    by_agent: list[dict[str, Any]]
    by_model: list[dict[str, Any]]
    by_channel: list[dict[str, Any]]
    per_minute_usd: float | None
    budgets: dict[str, float | None]


class BudgetUpdate(BaseModel):
    monthly_budget_usd: float | None = Field(default=None, ge=0)
    channel_ceiling_usd: float | None = Field(default=None, ge=0)
    channel_id: uuid.UUID | None = None


class AnalyticsOut(BaseModel):
    configured: bool
    detail: str
    totals: dict[str, float]
    trend: list[dict[str, Any]]
    videos: list[dict[str, Any]]


class SystemStatusOut(BaseModel):
    time: datetime
    services: list[dict[str, Any]]
    providers: list[dict[str, Any]]
    scheduler: dict[str, Any]


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)


class ApiKeyOut(BaseModel):
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None


class ApiKeyCreatedOut(ApiKeyOut):
    token: str
