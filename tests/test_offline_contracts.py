from pathlib import Path

import pytest

from creator_report.config import get_settings
from creator_report.jobs import JobService
from creator_report.models import JobRequest, VideoRecord, VideoSummary
from creator_report.storage import Storage


def test_loaded_module_is_the_staged_source_not_an_editable_original():
    import creator_report.jobs
    assert Path(creator_report.jobs.__file__).resolve().is_relative_to(
        Path(__file__).resolve().parents[1] / "src"
    )


@pytest.mark.asyncio
async def test_transcript_and_summary_cache_avoid_provider_calls(tmp_path: Path):
    storage = Storage(get_settings(tmp_path))
    service = JobService(storage.settings, storage)
    service.adapters = {}
    service.gemini_asr = service.local_asr = service.llm = object()
    cache = storage.settings.cache_dir / "youtube"
    storage.write_json(cache / "synthetic-cache" / "transcript.json", {
        "source": "synthetic-cache",
        "chunks": [{"start": 0, "end": 1, "text": "SYNTHETIC transcript"}],
    })
    storage.write_json(cache / "synthetic-cache" / "summary.json",
                       VideoSummary(overview="SYNTHETIC summary").model_dump())
    video = VideoRecord(
        video_id="synthetic-cache", platform="youtube",
        source_url="https://example.invalid/video", title="Synthetic fixture",
    )
    await service._process_video(video, cache, JobRequest(source_url="https://example.invalid/channel"))
    assert video.subtitle_source == "synthetic-cache"
    assert video.summary.overview == "SYNTHETIC summary"
