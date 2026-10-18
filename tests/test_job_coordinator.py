from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from creator_report.config import get_settings
from creator_report.jobs import JobCoordinator
from creator_report.models import JobRequest
from creator_report.storage import Storage


async def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 2.0,
) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_coordinator_runs_at_most_two_jobs_and_leaves_the_rest_queued(tmp_path: Path) -> None:
    storage = Storage(get_settings(tmp_path))
    jobs = [
        storage.create_job(JobRequest(source_url=f"https://www.youtube.com/@demo{index}/videos"))
        for index in range(3)
    ]
    release = asyncio.Event()
    started: list[str] = []
    active = 0
    max_active = 0

    async def runner(job_id: str, request: JobRequest) -> None:
        nonlocal active, max_active
        del request
        started.append(job_id)
        active += 1
        max_active = max(max_active, active)
        storage.update_job(job_id, status="running", message="running")
        await release.wait()
        active -= 1
        storage.update_job(job_id, status="completed", message="done")

    coordinator = JobCoordinator(storage, runner, max_workers=2)
    await coordinator.start()
    try:
        await _wait_until(lambda: len(started) == 2)
        assert max_active == 2
        assert storage.get_job(jobs[2].job_id).status == "queued"  # type: ignore[union-attr]

        release.set()
        await _wait_until(
            lambda: all(storage.get_job(job.job_id).status == "completed" for job in jobs)  # type: ignore[union-attr]
        )
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_cancelling_a_queued_job_prevents_it_from_running(tmp_path: Path) -> None:
    storage = Storage(get_settings(tmp_path))
    first = storage.create_job(JobRequest(source_url="https://www.youtube.com/@first/videos"))
    second = storage.create_job(JobRequest(source_url="https://www.youtube.com/@second/videos"))
    release = asyncio.Event()
    started: list[str] = []

    async def runner(job_id: str, request: JobRequest) -> None:
        del request
        started.append(job_id)
        storage.update_job(job_id, status="running", message="running")
        await release.wait()
        storage.update_job(job_id, status="completed", message="done")

    coordinator = JobCoordinator(storage, runner, max_workers=1)
    await coordinator.start()
    try:
        await _wait_until(lambda: started == [first.job_id])
        cancelled = coordinator.cancel(second.job_id)
        assert cancelled.status == "cancelled"

        release.set()
        await _wait_until(
            lambda: storage.get_job(first.job_id).status == "completed"  # type: ignore[union-attr]
        )
        await asyncio.sleep(0.05)
        assert second.job_id not in started
    finally:
        await coordinator.stop()
