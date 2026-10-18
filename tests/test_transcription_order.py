from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from creator_report import jobs as jobs_module
from creator_report.adapters.bilibili import ChargingVideoError
from creator_report.config import get_settings
from creator_report.jobs import JobService
from creator_report.models import JobRequest, TranscriptChunk, VideoRecord
from creator_report.storage import Storage


class _PlatformAdapter:
    def __init__(self, *, succeeds: bool) -> None:
        self.succeeds = succeeds
        self.calls = 0

    async def fetch_transcript(self, video: VideoRecord, work_dir: Path):
        del video, work_dir
        self.calls += 1
        if not self.succeeds:
            raise RuntimeError("no platform subtitles")
        return [TranscriptChunk(start=0, end=1, text="platform")], "platform"


class _ChargingAdapter:
    async def fetch_transcript(self, video: VideoRecord, work_dir: Path):
        del video, work_dir
        raise ChargingVideoError("Bilibili charging video (充电专属), skipped")


class _HangingAdapter:
    async def fetch_transcript(self, video: VideoRecord, work_dir: Path):
        del video, work_dir
        await asyncio.sleep(3600)


class _Asr:
    def __init__(
        self,
        name: str,
        events: list[str],
        *,
        succeeds: bool = True,
        hang_prepare: bool = False,
    ) -> None:
        self.name = name
        self.events = events
        self.succeeds = succeeds
        self.hang_prepare = hang_prepare

    async def prepare_audio(self, video: VideoRecord, work_dir: Path, progress=None):
        del video, progress
        self.events.append(f"{self.name}:prepare")
        if self.hang_prepare:
            await asyncio.sleep(3600)
        work_dir.mkdir(parents=True, exist_ok=True)
        path = work_dir / f"{self.name}.mp3"
        path.write_bytes(b"audio")
        return path

    async def transcribe(self, audio_path: Path, progress=None):
        del audio_path, progress
        self.events.append(f"{self.name}:transcribe")
        if not self.succeeds:
            raise RuntimeError(f"{self.name} unavailable")
        return [TranscriptChunk(start=0, end=1, text=self.name)]


class _Summary:
    async def summarize_video(self, video, output_language):
        from creator_report.models import VideoSummary

        del output_language
        return VideoSummary(
            overview=video.transcript[0].text,
            key_points=[],
            viewpoints=[],
            topics=[],
            snippets=[],
        )


def _video() -> VideoRecord:
    return VideoRecord(
        video_id="video-1",
        platform="youtube",
        source_url="https://www.youtube.com/watch?v=video-1",
        title="Video",
    )


@pytest.mark.asyncio
async def test_platform_subtitles_are_used_before_gemini_or_local_asr(
    tmp_path: Path,
) -> None:
    storage = Storage(get_settings(tmp_path))
    service = JobService(storage.settings, storage)
    events: list[str] = []
    adapter = _PlatformAdapter(succeeds=True)
    service.adapters = {"youtube": adapter}
    service.gemini_asr = _Asr("gemini", events)
    service.local_asr = _Asr("local", events)
    service.llm = _Summary()
    video = _video()

    await service._process_video(
        video,
        storage.settings.cache_dir / "youtube",
        JobRequest(source_url="https://www.youtube.com/@demo/videos"),
    )

    assert video.subtitle_source == "platform"
    assert events == []


@pytest.mark.asyncio
async def test_auto_mode_falls_from_gemini_to_local_after_platform_failure(
    tmp_path: Path,
) -> None:
    storage = Storage(get_settings(tmp_path))
    service = JobService(storage.settings, storage)
    events: list[str] = []
    service.adapters = {"youtube": _PlatformAdapter(succeeds=False)}
    service.gemini_asr = _Asr("gemini", events, succeeds=False)
    service.local_asr = _Asr("local", events)
    service.llm = _Summary()
    video = _video()

    await service._process_video(
        video,
        storage.settings.cache_dir / "youtube",
        JobRequest(
            source_url="https://www.youtube.com/@demo/videos",
            transcription_mode="auto",
        ),
    )

    assert events == [
        "gemini:prepare",
        "gemini:transcribe",
        "gemini:transcribe",
        "local:prepare",
        "local:transcribe",
    ]
    assert video.subtitle_source == "local"


@pytest.mark.asyncio
async def test_charging_video_is_skipped_without_asr_or_summary(
    tmp_path: Path,
) -> None:
    storage = Storage(get_settings(tmp_path))
    service = JobService(storage.settings, storage)
    events: list[str] = []
    service.adapters = {"youtube": _ChargingAdapter()}
    service.gemini_asr = _Asr("gemini", events)
    service.local_asr = _Asr("local", events)
    service.llm = _Summary()
    video = _video()

    await service._process_video(
        video,
        storage.settings.cache_dir / "youtube",
        JobRequest(source_url="https://www.youtube.com/@demo/videos"),
    )

    assert video.transcript_status == "skipped"
    assert video.transcript == []
    assert video.summary is None
    assert events == []


@pytest.mark.asyncio
async def test_hanging_subtitle_fetch_times_out_and_falls_to_asr(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(jobs_module, "PLATFORM_SUBTITLE_TIMEOUT_SECONDS", 0.1)
    storage = Storage(get_settings(tmp_path))
    service = JobService(storage.settings, storage)
    events: list[str] = []
    service.adapters = {"youtube": _HangingAdapter()}
    service.gemini_asr = _Asr("gemini", events)
    service.local_asr = _Asr("local", events)
    service.llm = _Summary()
    video = _video()

    await service._process_video(
        video,
        storage.settings.cache_dir / "youtube",
        JobRequest(source_url="https://www.youtube.com/@demo/videos"),
    )

    assert video.transcript_status == "ready"
    assert video.subtitle_source == "gemini"
    assert "gemini:prepare" in events


@pytest.mark.asyncio
async def test_hanging_asr_prepare_times_out_and_tries_next_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(jobs_module, "ASR_PREPARE_TIMEOUT_SECONDS", 0.1)
    storage = Storage(get_settings(tmp_path))
    service = JobService(storage.settings, storage)
    events: list[str] = []
    service.adapters = {"youtube": _PlatformAdapter(succeeds=False)}
    service.gemini_asr = _Asr("gemini", events, hang_prepare=True)
    service.local_asr = _Asr("local", events)
    service.llm = _Summary()
    video = _video()

    await service._process_video(
        video,
        storage.settings.cache_dir / "youtube",
        JobRequest(source_url="https://www.youtube.com/@demo/videos"),
    )

    assert video.transcript_status == "ready"
    assert video.subtitle_source == "local"
    assert events == [
        "gemini:prepare",
        "local:prepare",
        "local:transcribe",
    ]
