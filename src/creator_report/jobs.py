from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from creator_report.adapters import BilibiliAdapter, YouTubeAdapter
from creator_report.adapters.bilibili import ChargingVideoError
from creator_report.assets import localize_channel_avatar, localize_report_assets
from creator_report.asr import (
    AsrProvider,
    FasterWhisperProvider,
    GeminiAsrProvider,
    NullAsrProvider,
)
from creator_report.config import Settings
from creator_report.estimates import UsageEstimate, estimate_usage
from creator_report.llm import DeepSeekClient, MockDeepSeekClient
from creator_report.models import (
    ChannelPreview,
    JobRequest,
    JobState,
    TranscriptChunk,
    VideoRecord,
)
from creator_report.pipeline import build_channel_report
from creator_report.reports import render_report_bundle
from creator_report.sources import resolve_source
from creator_report.storage import Storage, TERMINAL_JOB_STATUSES

JobRunner = Callable[[str, JobRequest], Awaitable[None]]
EstimateProgress = Callable[[dict[str, Any]], Awaitable[None]]

# 防挂死：每个步骤的硬超时。本地 whisper / yt-dlp / Gemini 的某个原生调用可能
# 永久阻塞（本机已多次出现），超时后任务跳过该步骤继续，而不是整体冻结。
PLATFORM_SUBTITLE_TIMEOUT_SECONDS = 300
ASR_PREPARE_TIMEOUT_SECONDS = 900
ASR_TRANSCRIBE_TIMEOUT_SECONDS = 5400
SUMMARY_TIMEOUT_SECONDS = 900


class JobCancelled(RuntimeError):
    pass


async def retry_async(
    operation: Callable[[], Awaitable[Any]],
    *,
    attempts: int = 2,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            return await operation()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(0)
    assert last_error is not None
    raise last_error


class JobCoordinator:
    def __init__(
        self,
        storage: Storage,
        runner: JobRunner,
        *,
        max_workers: int = 2,
    ) -> None:
        self.storage = storage
        self.runner = runner
        self.max_workers = max(1, max_workers)
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._pending: set[str] = set()

    async def start(self) -> None:
        if self._workers:
            return
        self.storage.mark_running_jobs_interrupted()
        self._workers = [
            asyncio.create_task(
                self._worker_loop(),
                name=f"creator-report-worker-{index + 1}",
            )
            for index in range(self.max_workers)
        ]
        for state in self.storage.list_jobs(
            statuses={"queued"},
            limit=10_000,
            oldest_first=True,
        ):
            self.enqueue(state.job_id)

    async def stop(self) -> None:
        if not self._workers:
            return
        for _ in self._workers:
            await self._queue.put(None)
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._pending.clear()

    def enqueue(self, job_id: str) -> None:
        if job_id in self._pending:
            return
        self._pending.add(job_id)
        self._queue.put_nowait(job_id)

    def cancel(self, job_id: str) -> JobState:
        return self.storage.request_cancel(job_id)

    @property
    def queue_size(self) -> int:
        return len(self._pending)

    async def _worker_loop(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                if job_id is None:
                    return
                self._pending.discard(job_id)
                state = self.storage.get_job(job_id)
                if state is None or state.status != "queued":
                    continue
                request = self.storage.request_for_job(job_id)
                try:
                    await self.runner(job_id, request)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    latest = self.storage.get_job(job_id)
                    if latest and latest.status not in TERMINAL_JOB_STATUSES:
                        self.storage.update_job(
                            job_id,
                            status="failed",
                            message=f"任务失败：{exc}",
                            current_step="failed",
                            error_details=str(exc),
                        )
            finally:
                self._queue.task_done()


class JobService:
    def __init__(self, settings: Settings, storage: Storage) -> None:
        self.settings = settings
        self.storage = storage
        self.adapters = {
            "youtube": YouTubeAdapter(),
            "bilibili": BilibiliAdapter(settings),
        }
        cookiefile = self._cookiefile(settings)
        self.gemini_asr: AsrProvider | None = (
            GeminiAsrProvider(
                api_keys=settings.gemini_api_keys,
                model_name=settings.gemini_model,
                cookiefile=cookiefile,
            )
            if settings.gemini_api_keys
            else None
        )
        self.local_asr: AsrProvider = FasterWhisperProvider(
            model_size=settings.asr_model_size,
            device=settings.asr_device,
            cookiefile=cookiefile,
        )
        self.asr = self.local_asr
        self.llm = (
            DeepSeekClient(settings)
            if settings.deepseek_api_key
            else MockDeepSeekClient(settings)
        )

    def create_job(self, request: JobRequest) -> JobState:
        return self.storage.create_job(request)

    async def estimate(
        self,
        *,
        source_url: str,
        recent_limit: int,
        transcription_mode: str,
        progress: EstimateProgress | None = None,
    ) -> UsageEstimate:
        await _emit_estimate_progress(
            progress,
            progress_value=0.05,
            message="正在识别频道链接",
        )
        resolution = resolve_source(source_url)
        adapter = self.adapters[resolution.platform]
        await _emit_estimate_progress(
            progress,
            progress_value=0.15,
            message="正在读取频道信息",
        )
        channel = await retry_async(
            lambda: adapter.resolve_channel(resolution.normalized_url)
        )
        preview = ChannelPreview(
            channel_id=_display_channel_id(channel.source_id),
            platform=channel.platform,
            name=channel.title,
            source_url=channel.source_url,
            avatar_url=channel.avatar_url,
        )
        if progress:
            await progress(
                {
                    "type": "channel",
                    "progress": 0.28,
                    "message": "频道信息已读取",
                    "channel": preview.model_dump(mode="json"),
                }
            )
        await _emit_estimate_progress(
            progress,
            progress_value=0.34,
            message="正在读取视频列表",
        )
        videos = await retry_async(
            lambda: _list_adapter_videos(
                adapter,
                resolution.normalized_url,
                recent_limit,
            )
        )
        request = JobRequest(
            source_url=source_url,
            mode="recent",
            recent_limit=recent_limit,
            transcription_mode=transcription_mode,
        )
        selected = adapter.filter_mode(videos, request)[:recent_limit]
        selected = await self._hydrate_estimate_videos(
            adapter,
            selected,
            progress,
        )
        await _emit_estimate_progress(
            progress,
            progress_value=0.94,
            message="正在计算缓存与费用",
        )
        cached_indexes = {
            index
            for index, video in enumerate(selected)
            if (
                self.settings.cache_dir
                / video.platform
                / video.video_id
                / "transcript.json"
            ).is_file()
        }
        result = estimate_usage(
            durations_seconds=[video.duration_seconds for video in selected],
            cached_indexes=cached_indexes,
            transcription_mode=transcription_mode,
            gemini_available=bool(self.settings.gemini_api_keys),
        )
        result.channel = preview
        return result

    async def _hydrate_estimate_videos(
        self,
        adapter: Any,
        videos: list[VideoRecord],
        progress: EstimateProgress | None,
    ) -> list[VideoRecord]:
        hydrate = getattr(adapter, "hydrate_video", None)
        if not callable(hydrate) or not videos:
            return videos

        semaphore = asyncio.Semaphore(4)
        completed = 0
        completed_lock = asyncio.Lock()

        async def hydrate_one(video: VideoRecord) -> VideoRecord:
            nonlocal completed
            async with semaphore:
                try:
                    hydrated = await retry_async(lambda: hydrate(video))
                except Exception:  # noqa: BLE001
                    hydrated = video
            async with completed_lock:
                completed += 1
                fraction = completed / len(videos)
                await _emit_estimate_progress(
                    progress,
                    progress_value=0.48 + fraction * 0.42,
                    message=f"正在读取视频详情（{completed}/{len(videos)}）",
                )
            return hydrated

        return list(await asyncio.gather(*(hydrate_one(video) for video in videos)))

    async def run_job(self, job_id: str, request: JobRequest) -> None:
        try:
            state = self.storage.get_job(job_id)
            if state is None or state.status == "cancelled":
                return
            self.storage.update_job(
                job_id,
                status="running",
                message="[1/4] 正在解析频道...",
                current_step="resolve",
                progress=0.02,
            )
            self._check_cancel(job_id)
            resolution = resolve_source(request.source_url)
            adapter = self.adapters[resolution.platform]
            channel = await retry_async(
                lambda: adapter.resolve_channel(resolution.normalized_url)
            )
            report_dir = self.settings.reports_dir / job_id
            await localize_channel_avatar(channel, report_dir)
            self.storage.update_job(
                job_id,
                channel_id=_display_channel_id(channel.source_id),
                channel_name=channel.title,
                channel_avatar_path=channel.avatar_path,
            )
            listing_limit = (
                request.recent_limit
                if request.mode == "recent" and request.recent_limit
                else 50
            )
            videos = await retry_async(
                lambda: _list_adapter_videos(
                    adapter,
                    resolution.normalized_url,
                    listing_limit,
                )
            )
            videos = adapter.filter_mode(videos, request)[:50]
            if request.mode == "topic" and request.topic_prompt:
                candidate_ids = await retry_async(
                    lambda: self.llm.filter_relevance(videos, request.topic_prompt)
                )
                id_set = set(candidate_ids)
                videos = [video for video in videos if video.video_id in id_set]

            total = len(videos)
            self.storage.update_job(
                job_id,
                message=f"[2/4] 准备处理 {total} 个视频...",
                current_step="videos",
                progress=0.08,
            )
            processed_videos: list[VideoRecord] = []
            cache_dir = self.settings.cache_dir / channel.platform

            for index, video in enumerate(videos, start=1):
                self._check_cancel(job_id)

                async def progress(message: str, percent: float | None = None) -> None:
                    local_fraction = (percent or 0) / 100
                    overall = 0.08 + ((index - 1 + local_fraction) / max(total, 1)) * 0.62
                    self.storage.update_job(
                        job_id,
                        message=f"[2/4] [{index}/{total}] {video.title[:48]}：{message}",
                        current_step="videos",
                        progress=min(overall, 0.70),
                    )

                try:
                    hydrate = getattr(adapter, "hydrate_video", None)
                    if callable(hydrate):
                        try:
                            video = await retry_async(lambda: hydrate(video))
                        except Exception as exc:  # noqa: BLE001
                            video.processing_error = _append_error(
                                video.processing_error,
                                f"视频元数据读取失败: {exc}",
                            )
                    await self._process_video(
                        video,
                        cache_dir,
                        request,
                        progress,
                        job_id=job_id,
                    )
                except JobCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    video.processing_error = _append_error(
                        video.processing_error,
                        str(exc),
                    )
                    if video.transcript_status == "pending":
                        video.transcript_status = "failed"
                processed_videos.append(video)
                self._check_cancel(job_id)

            successful = [video for video in processed_videos if video.summary]
            if not successful:
                errors = [
                    f"{video.title}: {video.processing_error or 'unknown error'}"
                    for video in processed_videos
                ]
                self.storage.update_job(
                    job_id,
                    status="failed",
                    message="没有视频成功完成，未生成报告。",
                    current_step="failed",
                    progress=1.0,
                    error_details="\n".join(errors) or "No videos were available.",
                )
                return

            self.storage.update_job(
                job_id,
                message="[3/4] 正在生成频道级总结...",
                current_step="synthesis",
                progress=0.75,
            )
            report = build_channel_report(
                channel=channel,
                videos=processed_videos,
                topic_prompt=request.topic_prompt,
            )
            asset_result = await localize_report_assets(report, report_dir)
            synthesis_error: str | None = None
            try:
                synthesized = await retry_async(
                    lambda: self.llm.synthesize_channel_summary(
                        channel,
                        successful,
                        request.output_language,
                    )
                )
                if synthesized:
                    report.high_level_summary = synthesized
            except Exception as exc:  # noqa: BLE001
                synthesis_error = str(exc)

            self._check_cancel(job_id)
            self.storage.update_job(
                job_id,
                message="[4/4] 正在生成本地报告...",
                current_step="render",
                progress=0.90,
            )
            render_report_bundle(report, report_dir, self.settings.templates_dir)
            self.storage.write_json(
                report_dir / "report.json",
                report.model_dump(mode="json"),
            )
            failure_messages = [
                f"{video.title}: {video.processing_error}"
                for video in processed_videos
                if video.processing_error
            ]
            if synthesis_error:
                failure_messages.append(f"频道总结: {synthesis_error}")
            failure_messages.extend(
                f"本地资源: {failure}" for failure in asset_result.failures
            )
            final_status = "partial" if failure_messages else "completed"
            self.storage.update_job(
                job_id,
                status=final_status,
                message=(
                    "报告已生成，部分视频处理失败。"
                    if final_status == "partial"
                    else "报告已生成。"
                ),
                current_step=final_status,
                progress=1.0,
                report_relpath=(Path("reports") / job_id / "index.html").as_posix(),
                channel_avatar_path=report.channel.avatar_path,
                error_details="\n".join(failure_messages) or None,
            )
        except JobCancelled:
            self.storage.update_job(
                job_id,
                status="cancelled",
                message="任务已取消，已完成的缓存予以保留。",
                current_step="cancelled",
                progress=self.storage.get_job(job_id).progress,  # type: ignore[union-attr]
                cancel_requested=True,
                cancelled_at=datetime.now(UTC),
            )
        except Exception as exc:  # noqa: BLE001
            latest = self.storage.get_job(job_id)
            if latest and latest.status not in TERMINAL_JOB_STATUSES:
                self.storage.update_job(
                    job_id,
                    status="failed",
                    message=f"任务失败：{exc}",
                    current_step="failed",
                    error_details=str(exc),
                )

    async def _process_video(
        self,
        video: VideoRecord,
        cache_dir: Path,
        request: JobRequest,
        progress: Any = None,
        *,
        job_id: str | None = None,
    ) -> None:
        transcript_cache = cache_dir / video.video_id / "transcript.json"
        summary_cache = cache_dir / video.video_id / "summary.json"

        if not request.force_refresh:
            cached_transcript = self.storage.read_json(transcript_cache)
            if cached_transcript:
                video.transcript = [
                    TranscriptChunk.model_validate(item)
                    for item in cached_transcript.get("chunks", [])
                ]
                video.subtitle_source = str(cached_transcript.get("source") or "cache")
                video.transcript_status = "ready"
            cached_summary = self.storage.read_json(summary_cache)
            if cached_summary:
                from creator_report.models import VideoSummary

                video.summary = VideoSummary.model_validate(cached_summary)

        self._check_cancel(job_id)
        if not video.transcript:
            adapter = self.adapters[video.platform]
            platform_error: str | None = None
            try:
                chunks, source = await asyncio.wait_for(
                    retry_async(
                        lambda: adapter.fetch_transcript(
                            video,
                            cache_dir / video.video_id,
                        )
                    ),
                    timeout=PLATFORM_SUBTITLE_TIMEOUT_SECONDS,
                )
            except ChargingVideoError as exc:
                # Charging videos are skipped, not failed: no subtitle/audio is
                # accessible, so ASR would be pointless and would only waste time.
                video.transcript_status = "skipped"
                video.processing_error = str(exc)
                return
            except Exception as exc:  # noqa: BLE001
                platform_error = str(exc)
                chunks, source, fallback_errors = await self._fallback_transcribe(
                    video,
                    cache_dir / video.video_id,
                    request.transcription_mode,
                    progress,
                    job_id=job_id,
                )
                if not chunks:
                    video.transcript_status = "metadata_only"
                    video.processing_error = (
                        f"平台字幕失败: {platform_error}; "
                        f"ASR 失败: {'; '.join(fallback_errors)}"
                    )
            if chunks:
                video.transcript = chunks
                video.subtitle_source = source
                video.transcript_status = "ready"
                self.storage.write_json(
                    transcript_cache,
                    {
                        "source": source,
                        "chunks": [chunk.model_dump() for chunk in chunks],
                    },
                )

        self._check_cancel(job_id)
        if not video.summary:
            video.summary = await asyncio.wait_for(
                retry_async(
                    lambda: self.llm.summarize_video(video, request.output_language)
                ),
                timeout=SUMMARY_TIMEOUT_SECONDS,
            )
            self.storage.write_json(summary_cache, video.summary.model_dump())

    async def _fallback_transcribe(
        self,
        video: VideoRecord,
        work_dir: Path,
        transcription_mode: str,
        progress: Any,
        *,
        job_id: str | None,
    ) -> tuple[list[TranscriptChunk], str, list[str]]:
        providers: list[tuple[str, AsrProvider]] = []
        if transcription_mode in {"auto", "gemini"} and self.gemini_asr is not None:
            providers.append(("gemini", self.gemini_asr))
        if transcription_mode in {"auto", "local"}:
            providers.append(("local", self.local_asr))
        if transcription_mode == "gemini" and self.gemini_asr is None:
            return [], "metadata_only", ["Gemini API key is not configured."]

        errors: list[str] = []
        for source, provider in providers:
            self._check_cancel(job_id)
            try:
                audio_path = await asyncio.wait_for(
                    retry_async(
                        lambda provider=provider: provider.prepare_audio(
                            video,
                            work_dir,
                            progress=progress,
                        )
                    ),
                    timeout=ASR_PREPARE_TIMEOUT_SECONDS,
                )
                chunks = await asyncio.wait_for(
                    retry_async(
                        lambda provider=provider, audio_path=audio_path: provider.transcribe(
                            audio_path,
                            progress=progress,
                        )
                    ),
                    timeout=ASR_TRANSCRIBE_TIMEOUT_SECONDS,
                )
                if not chunks:
                    raise RuntimeError("The transcription provider returned no text.")
                return chunks, source, errors
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{source}: {exc}")
        return [], "metadata_only", errors

    def _check_cancel(self, job_id: str | None) -> None:
        if job_id and self.storage.is_cancel_requested(job_id):
            raise JobCancelled(job_id)

    @staticmethod
    def _cookiefile(settings: Settings) -> str | None:
        if not settings.bilibili_cookie:
            return None
        adapter = BilibiliAdapter(settings)
        path = adapter._cookiefile_path()
        return str(path) if path else None

    @staticmethod
    def _build_asr(settings: Settings) -> AsrProvider:
        if settings.asr_provider == "none":
            return NullAsrProvider()
        return FasterWhisperProvider(
            model_size=settings.asr_model_size,
            device=settings.asr_device,
        )


def _append_error(existing: str | None, new_error: str) -> str:
    return f"{existing}; {new_error}" if existing else new_error


async def _list_adapter_videos(
    adapter: Any,
    source_url: str,
    limit: int,
) -> list[VideoRecord]:
    parameters = inspect.signature(adapter.list_videos).parameters
    if "limit" in parameters:
        return await adapter.list_videos(source_url, limit=limit)
    return await adapter.list_videos(source_url)


async def _emit_estimate_progress(
    callback: EstimateProgress | None,
    *,
    progress_value: float,
    message: str,
) -> None:
    if callback:
        await callback(
            {
                "type": "progress",
                "progress": progress_value,
                "message": message,
            }
        )


def _display_channel_id(source_id: str) -> str:
    return source_id.split(":", 1)[-1] if ":" in source_id else source_id


def run_job_in_background(
    service: JobService,
    job_id: str,
    request: JobRequest,
) -> None:
    asyncio.run(service.run_job(job_id, request))
