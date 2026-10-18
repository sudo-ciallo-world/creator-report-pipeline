from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class TranscriptChunk(BaseModel):
    start: float
    end: float
    text: str


class VideoSummary(BaseModel):
    overview: str
    key_points: list[str] = Field(default_factory=list)
    viewpoints: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    snippets: list[str] = Field(default_factory=list)


class VideoFailure(BaseModel):
    video_id: str
    title: str
    reason: str


class VideoRecord(BaseModel):
    video_id: str
    platform: str
    source_url: str
    title: str
    description: str = ""
    published_at: datetime | None = None
    duration_seconds: int | None = None
    transcript: list[TranscriptChunk] = Field(default_factory=list)
    summary: VideoSummary | None = None
    video_type: str = "video"
    subtitle_source: str | None = None
    transcript_status: str = "pending"
    processing_error: str | None = None
    thumbnail_url: str | None = None
    thumbnail_path: str | None = None


class ChannelProfile(BaseModel):
    source_id: str
    platform: str
    title: str
    description: str = ""
    source_url: str
    avatar_url: str | None = None
    avatar_path: str | None = None


class ChannelPreview(BaseModel):
    channel_id: str
    platform: str
    name: str
    source_url: str
    avatar_url: str | None = None


class TopicCluster(BaseModel):
    label: str
    summary: str
    representative_video_ids: list[str] = Field(default_factory=list)
    video_count: int = 0


class ContentDistributionItem(BaseModel):
    label: str
    video_count: int
    share_percent: float


class ChannelReport(BaseModel):
    channel: ChannelProfile
    generated_at: datetime
    video_count: int
    processed_video_count: int
    failed_video_count: int
    date_range_label: str
    high_level_summary: str
    repeated_viewpoints: list[str] = Field(default_factory=list)
    recurring_formats: list[str] = Field(default_factory=list)
    topic_clusters: list[TopicCluster] = Field(default_factory=list)
    videos: list[VideoRecord] = Field(default_factory=list)
    failures: list[VideoFailure] = Field(default_factory=list)
    topic_prompt: str | None = None
    channel_theme: str = ""
    viewpoint_thread: list[str] = Field(default_factory=list)
    content_distribution: list[ContentDistributionItem] = Field(default_factory=list)
    recommended_video_ids: list[str] = Field(default_factory=list)


class SourceResolution(BaseModel):
    platform: str
    normalized_url: str


class JobRequest(BaseModel):
    source_url: str
    mode: Literal["all", "recent", "topic"] = "recent"
    recent_limit: int | None = Field(default=10, ge=1, le=50)
    topic_prompt: str | None = None
    output_language: str = "zh-CN"
    force_refresh: bool = False
    transcription_mode: Literal["auto", "gemini", "local"] = "auto"
    cost_confirmed: bool = False


class EstimateRequest(BaseModel):
    source_url: str
    recent_limit: int = Field(default=10, ge=1, le=50)
    transcription_mode: Literal["auto", "gemini", "local"] = "auto"


class JobState(BaseModel):
    job_id: str
    source_url: str
    mode: str
    status: str
    message: str
    report_relpath: str | None = None
    request_json: str = "{}"
    progress: float = 0.0
    current_step: str = "queued"
    retry_of: str | None = None
    cancel_requested: bool = False
    error_details: str | None = None
    config_json: str = "{}"
    channel_id: str | None = None
    channel_name: str | None = None
    channel_avatar_path: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancelled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ViewpointRecord(BaseModel):
    creator: str
    platform: str
    topic: str
    viewpoint: str
    evidence_snippet: str
    video_id: str
    video_title: str
    video_url: str
