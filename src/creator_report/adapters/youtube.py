from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)
from yt_dlp import YoutubeDL

from creator_report.adapters.base import CreatorAdapter
from creator_report.models import ChannelProfile, TranscriptChunk, VideoRecord


class YouTubeAdapter(CreatorAdapter):
    platform = "youtube"

    async def resolve_channel(self, source_url: str) -> ChannelProfile:
        info = await asyncio.to_thread(
            self._extract_info,
            source_url,
            extract_flat=True,
            limit=1,
        )
        title = info.get("channel") or info.get("title") or "YouTube Creator"
        source_id = info.get("channel_id") or info.get("id") or source_url
        description = info.get("description") or ""
        avatar = info.get("thumbnails", [{}])[-1].get("url") if info.get("thumbnails") else None
        return ChannelProfile(
            source_id=f"youtube:{source_id}",
            platform=self.platform,
            title=title,
            description=description,
            source_url=source_url,
            avatar_url=avatar,
        )

    async def list_videos(
        self,
        source_url: str,
        *,
        limit: int = 50,
    ) -> list[VideoRecord]:
        info = await asyncio.to_thread(
            self._extract_info,
            source_url,
            extract_flat=True,
            limit=limit,
        )
        entries = info.get("entries") or []
        videos: list[VideoRecord] = []
        for entry in entries:
            video_id = entry.get("id")
            if not video_id:
                continue
            timestamp = entry.get("timestamp")
            published_at = datetime.fromtimestamp(timestamp, tz=UTC) if timestamp else None
            duration = entry.get("duration")
            videos.append(
                VideoRecord(
                    video_id=video_id,
                    platform=self.platform,
                    source_url=entry.get("url") or f"https://www.youtube.com/watch?v={video_id}",
                    title=entry.get("title") or video_id,
                    description=entry.get("description") or "",
                    published_at=published_at,
                    duration_seconds=duration,
                    video_type="short" if (duration or 0) and (duration or 0) <= 90 else "video",
                    thumbnail_url=(
                        entry.get("thumbnails", [{}])[-1].get("url")
                        if entry.get("thumbnails")
                        else None
                    ),
                )
            )
        return videos

    async def fetch_transcript(self, video: VideoRecord, work_dir: Path) -> tuple[list[TranscriptChunk], str]:
        """Fetch transcript for a YouTube video. Tries multiple strategies in order:

        1. youtube-transcript-api (fast, direct API access)
        2. yt-dlp subtitle download (more reliable, handles IP blocks better)
        """
        chunks: list[TranscriptChunk] = []
        errors: list[str] = []

        # Strategy 1: youtube-transcript-api with language fallback
        try:
            chunks = self._fetch_via_transcript_api(video.video_id)
            if chunks:
                return chunks, "youtube_transcript_api"
        except Exception as exc:
            errors.append(f"transcript_api: {exc}")

        # Strategy 2: yt-dlp subtitle extraction
        try:
            chunks = self._fetch_via_ytdlp(video, work_dir)
            if chunks:
                return chunks, "ytdlp_youtube"
        except Exception as exc:
            errors.append(f"ytdlp: {exc}")

        raise RuntimeError(
            f"All YouTube transcript strategies failed: {'; '.join(errors)}"
        )

    @staticmethod
    def _fetch_via_transcript_api(video_id: str) -> list[TranscriptChunk]:
        """Strategy 1: Use youtube-transcript-api with proper language fallback.

        Tries auto-generated captions first (most common), then manual captions.
        Supports multiple common languages.
        """
        api = YouTubeTranscriptApi()

        # Get available transcripts first to make smart choices
        try:
            transcript_list = api.list(video_id)
        except (VideoUnavailable, TranscriptsDisabled) as exc:
            raise RuntimeError(f"YouTube transcript unavailable for {video_id}: {exc}") from exc
        except Exception as exc:
            raise RuntimeError(f"YouTube transcript list failed for {video_id}: {exc}") from exc

        # Try languages in order: English, Chinese, Japanese, Korean, auto-detect
        language_priorities = ["en", "zh", "zh-Hans", "zh-Hant", "ja", "ko", "es", "fr", "de"]

        # Try auto-generated first (most YouTube videos have these)
        transcript = None
        for lang in language_priorities:
            try:
                transcript = transcript_list.find_generated_transcript([lang])
                break
            except NoTranscriptFound:
                continue

        # If no auto-generated, try manual captions
        if transcript is None:
            for lang in language_priorities:
                try:
                    transcript = transcript_list.find_manually_created_transcript([lang])
                    break
                except NoTranscriptFound:
                    continue

        # Fall back to any available transcript
        if transcript is None:
            try:
                # Just take whatever is available
                available = list(transcript_list)
                if available:
                    transcript = available[0]
            except Exception:
                pass

        if transcript is None:
            raise RuntimeError(f"No transcript found for video {video_id} in any language.")

        fetched = transcript.fetch()
        return [
            TranscriptChunk(
                start=round(snippet.start, 2),
                end=round(snippet.start + snippet.duration, 2),
                text=snippet.text.strip(),
            )
            for snippet in fetched.snippets
            if snippet.text.strip()
        ]

    def _fetch_via_ytdlp(self, video: VideoRecord, work_dir: Path) -> list[TranscriptChunk]:
        """Strategy 2: Use yt-dlp to download subtitles without downloading the video.

        yt-dlp handles IP blocks, rate limiting, and client fallbacks better
        than youtube-transcript-api.
        """
        work_dir.mkdir(parents=True, exist_ok=True)
        outtmpl = str(work_dir / "%(id)s.%(ext)s")

        opts = {
            "quiet": True,
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en.*", "zh.*", "ja.*", "ko.*", "-live_chat"],
            "subtitlesformat": "json3",
            "outtmpl": outtmpl,
            "no_warnings": True,
            # Use multiple client fallbacks for better reliability
            "extractor_args": {"youtube": {"player_client": ["ios", "web"]}},
        }

        with YoutubeDL(opts) as ydl:
            ydl.download([video.source_url])

        # Find and parse the downloaded subtitle file
        for path in sorted(work_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if path.suffix in (".json3", ".vtt", ".srt") and path.stem != "audio":
                return self._parse_subtitle_file(path)

        raise RuntimeError("yt-dlp did not produce any subtitle files for YouTube video.")

    @staticmethod
    def _parse_subtitle_file(path: Path) -> list[TranscriptChunk]:
        """Parse a subtitle file into TranscriptChunks."""
        text = path.read_text(encoding="utf-8")

        if path.suffix == ".json3":
            return YouTubeAdapter._parse_json3(text)
        elif path.suffix == ".vtt":
            return YouTubeAdapter._parse_vtt(text)
        elif path.suffix == ".srt":
            return YouTubeAdapter._parse_srt(text)
        return []

    @staticmethod
    def _parse_json3(text: str) -> list[TranscriptChunk]:
        """Parse YouTube json3 subtitle format."""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        events = data.get("events") or []
        chunks: list[TranscriptChunk] = []
        for event in events:
            t_start_ms = event.get("tStartMs", 0)
            d_duration_ms = event.get("dDurationMs", 0)
            t_start = t_start_ms / 1000.0
            t_end = (t_start_ms + max(d_duration_ms, 100)) / 1000.0
            segs = event.get("segs") or []
            text_parts = []
            for seg in segs:
                utf8 = seg.get("utf8", "")
                if utf8:
                    text_parts.append(utf8)
            line = "".join(text_parts).strip()
            if line:
                chunks.append(TranscriptChunk(start=round(t_start, 2), end=round(t_end, 2), text=line))
        return chunks

    @staticmethod
    def _parse_vtt(text: str) -> list[TranscriptChunk]:
        """Parse WebVTT subtitle format."""
        chunks: list[TranscriptChunk] = []
        lines = text.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if "-->" in line:
                times = line.split("-->")
                start = YouTubeAdapter._parse_vtt_time(times[0].strip())
                end = (
                    YouTubeAdapter._parse_vtt_time(times[1].strip())
                    if len(times) > 1
                    else start + 5
                )
                i += 1
                text_lines = []
                while i < len(lines) and lines[i].strip() and "-->" not in lines[i]:
                    text_lines.append(lines[i].strip())
                    i += 1
                text_str = " ".join(text_lines).strip()
                if text_str:
                    chunks.append(
                        TranscriptChunk(start=round(start, 2), end=round(end, 2), text=text_str)
                    )
            else:
                i += 1
        return chunks

    @staticmethod
    def _parse_vtt_time(time_str: str) -> float:
        """Parse VTT/SRT timestamp like '00:01:23.456' or '00:01:23,456' to seconds."""
        time_str = time_str.split()[0] if time_str else "0"
        parts = time_str.replace(",", ".").split(":")
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        elif len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s)
        return float(parts[0]) if parts else 0.0

    @staticmethod
    def _parse_srt(text: str) -> list[TranscriptChunk]:
        """Parse SRT subtitle format."""
        chunks: list[TranscriptChunk] = []
        blocks = text.strip().split("\n\n")
        for block in blocks:
            lines = block.strip().split("\n")
            if len(lines) >= 3:
                times = lines[1].split("-->")
                if len(times) == 2:
                    start = YouTubeAdapter._parse_vtt_time(times[0].strip())
                    end = YouTubeAdapter._parse_vtt_time(times[1].strip())
                    text_str = " ".join(line.strip() for line in lines[2:])
                    if text_str:
                        chunks.append(
                            TranscriptChunk(start=round(start, 2), end=round(end, 2), text=text_str)
                        )
        return chunks

    def _extract_info(
        self,
        url: str,
        *,
        extract_flat: bool,
        limit: int = 50,
    ) -> dict:
        opts = {
            "quiet": True,
            "extract_flat": extract_flat,
            "skip_download": True,
            "playlistend": max(1, min(50, limit)),
        }
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
