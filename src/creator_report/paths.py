from __future__ import annotations

import re
from pathlib import Path, PurePosixPath


class UnsafeGeneratedPath(ValueError):
    pass


def resolve_generated_path(
    reports_dir: Path,
    job_id: str,
    relative_path: str,
) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
        raise UnsafeGeneratedPath("Invalid job id.")
    posix_path = PurePosixPath(relative_path.replace("\\", "/"))
    if posix_path.is_absolute() or ".." in posix_path.parts:
        raise UnsafeGeneratedPath("Generated path leaves the job directory.")

    job_dir = (reports_dir / job_id).resolve()
    candidate = job_dir.joinpath(*posix_path.parts).resolve()
    if not candidate.is_relative_to(job_dir):
        raise UnsafeGeneratedPath("Generated path leaves the job directory.")
    return candidate
