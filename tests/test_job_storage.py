from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from creator_report.config import get_settings
from creator_report.housekeeping import cleanup_old_audio
from creator_report.models import JobRequest
from creator_report.storage import Storage


def _create_legacy_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE jobrecord (
                job_id VARCHAR NOT NULL PRIMARY KEY,
                source_url VARCHAR NOT NULL,
                mode VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                message VARCHAR NOT NULL,
                report_relpath VARCHAR,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO jobrecord (
                job_id, source_url, mode, status, message,
                report_relpath, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-running",
                "https://www.youtube.com/@demo/videos",
                "recent",
                "running",
                "processing",
                None,
                "2026-06-01T10:00:00+00:00",
                "2026-06-01T10:01:00+00:00",
            ),
        )


def test_storage_migrates_legacy_jobs_without_losing_history(tmp_path: Path) -> None:
    database_path = tmp_path / "data" / "creator_report.db"
    _create_legacy_database(database_path)

    storage = Storage(get_settings(tmp_path))
    legacy = storage.get_job("legacy-running")

    assert legacy is not None
    assert legacy.source_url == "https://www.youtube.com/@demo/videos"
    assert legacy.request_json == "{}"
    assert legacy.channel_id is None
    assert legacy.channel_name is None
    assert legacy.channel_avatar_path is None
    assert storage.schema_version() >= 3


def test_startup_marks_only_running_jobs_as_interrupted(tmp_path: Path) -> None:
    storage = Storage(get_settings(tmp_path))
    running = storage.create_job(
        JobRequest(source_url="https://www.youtube.com/@running/videos"),
        status="running",
    )
    queued = storage.create_job(
        JobRequest(source_url="https://www.youtube.com/@queued/videos"),
        status="queued",
    )

    changed = storage.mark_running_jobs_interrupted()

    assert changed == 1
    assert storage.get_job(running.job_id).status == "interrupted"  # type: ignore[union-attr]
    assert storage.get_job(queued.job_id).status == "queued"  # type: ignore[union-attr]


def test_retry_creates_a_linked_job_and_preserves_original(tmp_path: Path) -> None:
    storage = Storage(get_settings(tmp_path))
    request = JobRequest(
        source_url="https://www.youtube.com/@demo/videos",
        mode="recent",
        recent_limit=7,
        transcription_mode="local",
    )
    original = storage.create_job(request, status="failed")
    original = storage.update_job(
        original.job_id,
        channel_id="UC-demo",
        channel_name="Demo Creator",
        channel_avatar_path="assets/media/avatar.jpg",
    )

    retried = storage.retry_job(original.job_id)

    assert retried.job_id != original.job_id
    assert retried.retry_of == original.job_id
    assert json.loads(retried.request_json)["recent_limit"] == 7
    assert retried.channel_id == "UC-demo"
    assert retried.channel_name == "Demo Creator"
    assert retried.channel_avatar_path == "assets/media/avatar.jpg"
    assert storage.get_job(original.job_id).status == "failed"  # type: ignore[union-attr]


def test_storage_backfills_channel_identity_from_existing_report_json(
    tmp_path: Path,
) -> None:
    settings = get_settings(tmp_path)
    storage = Storage(settings)
    job = storage.create_job(
        JobRequest(source_url="https://space.bilibili.com/synthetic-channel"),
        status="failed",
    )
    report_path = settings.reports_dir / job.job_id / "report.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(
        json.dumps(
            {
                "channel": {
                    "source_id": "bilibili:synthetic-channel",
                    "title": "测试博主",
                    "avatar_path": "assets/media/avatar.jpg",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    reloaded = Storage(settings)
    backfilled = reloaded.get_job(job.job_id)

    assert backfilled is not None
    assert backfilled.channel_id == "synthetic-channel"
    assert backfilled.channel_name == "测试博主"
    assert backfilled.channel_avatar_path == "assets/media/avatar.jpg"


def test_cleanup_removes_only_expired_audio_files(tmp_path: Path) -> None:
    cache_dir = tmp_path / "data" / "cache"
    video_dir = cache_dir / "youtube" / "video-1"
    video_dir.mkdir(parents=True)
    old_audio = video_dir / "audio.mp3"
    old_transcript = video_dir / "transcript.json"
    fresh_audio = video_dir / "gemini-16k-mono.mp3"
    old_audio.write_bytes(b"old audio")
    old_transcript.write_text("{}", encoding="utf-8")
    fresh_audio.write_bytes(b"fresh audio")

    now = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    old_timestamp = (now - timedelta(days=31)).timestamp()
    fresh_timestamp = (now - timedelta(days=5)).timestamp()
    os.utime(old_audio, (old_timestamp, old_timestamp))
    os.utime(old_transcript, (old_timestamp, old_timestamp))
    os.utime(fresh_audio, (fresh_timestamp, fresh_timestamp))

    result = cleanup_old_audio(cache_dir, retention_days=30, now=now)

    assert result.removed_count == 1
    assert not old_audio.exists()
    assert old_transcript.exists()
    assert fresh_audio.exists()
