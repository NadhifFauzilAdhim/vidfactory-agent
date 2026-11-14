"""Channels, their settings, cast and production schedules."""

from __future__ import annotations

import uuid
from datetime import datetime, time

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDMixin

VIDEO_FORMATS = ("16:9", "9:16", "1:1")
APPROVAL_MODES = ("always", "below_score", "never")


class Channel(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "channels"
    __table_args__ = (
        CheckConstraint(
            "video_format IN ('16:9', '9:16', '1:1')", name="video_format_valid"
        ),
        Index("ix_channels_workspace_active", "workspace_id", "is_active"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    handle: Mapped[str] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(Text)
    niche: Mapped[str] = mapped_column(String(80), default="kids-stories")
    audience: Mapped[str] = mapped_column(String(160), default="Children 3-7")
    language: Mapped[str] = mapped_column(String(12), default="en")
    video_format: Mapped[str] = mapped_column(String(8), default="16:9")
    accent_color: Mapped[str] = mapped_column(String(16), default="#D6FF45")
    # How scenes are illustrated:
    #   "image"       — generate one image per scene with the image provider (default)
    #   "generative"  — no image provider: Remotion composes each scene from its visual prompt
    #   "code"        — no image provider: the LLM writes each scene as HTML/CSS/GSAP, rendered
    #                   in a sandboxed iframe (see docs/11-code-scenes.md)
    visual_mode: Mapped[str] = mapped_column(String(16), default="image", server_default="image")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # YouTube connection state (credentials live encrypted in youtube_connections)
    youtube_channel_id: Mapped[str | None] = mapped_column(String(64))
    youtube_channel_title: Mapped[str | None] = mapped_column(String(160))
    youtube_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    settings: Mapped[ChannelSetting | None] = relationship(
        back_populates="channel", uselist=False, cascade="all, delete-orphan"
    )
    characters: Mapped[list[ChannelCharacter]] = relationship(
        back_populates="channel", cascade="all, delete-orphan", order_by="ChannelCharacter.name"
    )
    schedules: Mapped[list[Schedule]] = relationship(
        back_populates="channel", cascade="all, delete-orphan"
    )


class ChannelSetting(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "channel_settings"

    channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), unique=True
    )
    visual_style: Mapped[str] = mapped_column(
        String(160), default="Soft gouache storybook illustration"
    )
    palette: Mapped[dict] = mapped_column(default=lambda: {"primary": "#D6FF45"})
    content_guidelines: Mapped[str | None] = mapped_column(Text)
    banned_topics: Mapped[list] = mapped_column(default=list)
    made_for_kids: Mapped[bool] = mapped_column(Boolean, default=True)
    target_duration_seconds: Mapped[int] = mapped_column(Integer, default=240)
    cost_ceiling_usd: Mapped[float] = mapped_column(Numeric(10, 4), default=4.5)
    approval_mode: Mapped[str] = mapped_column(String(16), default="always")
    min_quality_score: Mapped[float] = mapped_column(Numeric(5, 2), default=85)
    voice_provider: Mapped[str | None] = mapped_column(String(40))
    voice_id: Mapped[str | None] = mapped_column(String(120))
    voice_model: Mapped[str | None] = mapped_column(String(120))
    # Partial override — only the keys set here win over the global TTS configuration
    # (language, output_format, stability, speed, …).
    voice_config: Mapped[dict] = mapped_column(default=dict)
    thumbnail_style: Mapped[str | None] = mapped_column(String(240))

    channel: Mapped[Channel] = relationship(back_populates="settings")


class ChannelCharacter(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "channel_characters"

    channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(80))
    role: Mapped[str] = mapped_column(String(80), default="protagonist")
    description: Mapped[str] = mapped_column(Text)
    visual_prompt: Mapped[str] = mapped_column(Text)
    voice_id: Mapped[str | None] = mapped_column(String(120))
    color: Mapped[str] = mapped_column(String(16), default="#111111")

    channel: Mapped[Channel] = relationship(back_populates="characters")


class YouTubeOAuthConnection(UUIDMixin, TimestampMixin, Base):
    """Encrypted OAuth tokens for a channel's YouTube account."""

    __tablename__ = "youtube_oauth_connections"
    __table_args__ = (UniqueConstraint("channel_id", name="uq_oauth_channel"),)

    channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    youtube_channel_id: Mapped[str | None] = mapped_column(String(64))
    channel_title: Mapped[str | None] = mapped_column(String(160))
    refresh_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    token_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    access_token: Mapped[str | None] = mapped_column(Text)  # short-lived, renewed from refresh token
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[str | None] = mapped_column(Text)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class Schedule(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "schedules"
    __table_args__ = (Index("ix_schedules_channel_enabled", "channel_id", "is_enabled"),)

    channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120), default="Daily production")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    days_of_week: Mapped[list] = mapped_column(
        default=lambda: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    )
    production_count: Mapped[int] = mapped_column(Integer, default=1)
    start_time: Mapped[time] = mapped_column(Time, default=lambda: time(6, 0))
    publish_time: Mapped[time] = mapped_column(Time, default=lambda: time(17, 0))
    max_concurrent: Mapped[int] = mapped_column(Integer, default=1)
    require_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_publish: Mapped[bool] = mapped_column(Boolean, default=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    channel: Mapped[Channel] = relationship(back_populates="schedules")
