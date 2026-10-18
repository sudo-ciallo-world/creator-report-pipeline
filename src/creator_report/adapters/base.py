from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from creator_report.models import ChannelProfile, JobRequest, TranscriptChunk, VideoRecord


class CreatorAdapter(ABC):
    platform: str

    @abstractmethod
    async def resolve_channel(self, source_url: str) -> ChannelProfile:
        raise NotImplementedError

    @abstractmethod
    async def list_videos(
        self,
        source_url: str,
        *,
        limit: int = 50,
    ) -> list[VideoRecord]:
        raise NotImplementedError

    @abstractmethod
    async def fetch_transcript(self, video: VideoRecord, work_dir: Path) -> tuple[list[TranscriptChunk], str]:
        raise NotImplementedError

    async def hydrate_video(self, video: VideoRecord) -> VideoRecord:
        return video

    def filter_mode(self, videos: list[VideoRecord], request: JobRequest) -> list[VideoRecord]:
        if request.mode == "recent" and request.recent_limit:
            return videos[: request.recent_limit]
        return videos
