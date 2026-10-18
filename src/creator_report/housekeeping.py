from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel


class CleanupResult(BaseModel):
    removed_count: int
    removed_bytes: int
    removed_paths: list[str]


def _is_audio_artifact(path: Path) -> bool:
    name = path.name.lower()
    return name.startswith("audio.") or name.startswith("gemini-16k-mono.")


def cleanup_old_audio(
    cache_dir: Path,
    *,
    retention_days: int = 30,
    now: datetime | None = None,
) -> CleanupResult:
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    removed_paths: list[str] = []
    removed_bytes = 0
    if not cache_dir.exists():
        return CleanupResult(removed_count=0, removed_bytes=0, removed_paths=[])

    for path in cache_dir.rglob("*"):
        if not path.is_file() or not _is_audio_artifact(path):
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        if modified >= cutoff:
            continue
        size = path.stat().st_size
        path.unlink()
        removed_bytes += size
        removed_paths.append(str(path))

    return CleanupResult(
        removed_count=len(removed_paths),
        removed_bytes=removed_bytes,
        removed_paths=removed_paths,
    )
