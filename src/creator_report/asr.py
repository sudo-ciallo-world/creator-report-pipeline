from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from yt_dlp import DownloadError, YoutubeDL

from creator_report.models import TranscriptChunk, VideoRecord

ProgressCallback = Callable[[str, float | None], Any] | None
GEMINI_TRANSIENT_RETRY_ROUNDS = 4
GEMINI_TRANSIENT_BACKOFF_SECONDS = (5, 15, 45)
GeminiClientFactory = Callable[[str], Any]
WhisperModelLoader = Callable[[str, str, str], Any]

LOGGER = logging.getLogger(__name__)
_WHISPER_MODELS: dict[tuple[str, str, str, int], Any] = {}
_WHISPER_MODELS_LOCK = threading.Lock()
_WHISPER_GPU_SEMAPHORE = threading.Semaphore(1)


class GeminiKeysExhausted(RuntimeError):
    pass


def _find_ffmpeg() -> str | None:
    explicit = os.environ.get("FFMPEG_LOCATION")
    if explicit and Path(explicit).exists():
        path = Path(explicit)
        return str(path.parent if path.is_file() else path)

    found = shutil.which("ffmpeg")
    if found:
        return str(Path(found).parent)

    winget_base = (
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Microsoft"
        / "WinGet"
        / "Packages"
    )
    if winget_base.exists():
        candidates = list(winget_base.glob("Gyan.FFmpeg_*/**/bin/ffmpeg.exe"))
        if candidates:
            return str(candidates[0].parent)
    return None


def _ffmpeg_executable(ffmpeg_location: str | None = None) -> str | None:
    if ffmpeg_location:
        candidate = Path(ffmpeg_location)
        if candidate.is_file():
            return str(candidate)
        executable = candidate / "ffmpeg.exe"
        if executable.exists():
            return str(executable)
        executable = candidate / "ffmpeg"
        if executable.exists():
            return str(executable)
    return shutil.which("ffmpeg")


def _audio_duration_seconds(audio_path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        ffmpeg_dir = _find_ffmpeg()
        if ffmpeg_dir:
            candidate = Path(ffmpeg_dir) / "ffprobe.exe"
            ffprobe = str(candidate) if candidate.exists() else None
    if not ffprobe:
        return 0.0
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "quiet",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(audio_path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return float(result.stdout.strip()) if result.stdout else 0.0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


class AsrProvider:
    async def prepare_audio(
        self,
        video: VideoRecord,
        work_dir: Path,
        progress: ProgressCallback = None,
    ) -> Path:
        raise NotImplementedError

    async def transcribe(
        self,
        audio_path: Path,
        progress: ProgressCallback = None,
    ) -> list[TranscriptChunk]:
        raise NotImplementedError


class NullAsrProvider(AsrProvider):
    async def prepare_audio(
        self,
        video: VideoRecord,
        work_dir: Path,
        progress: ProgressCallback = None,
    ) -> Path:
        del video, work_dir, progress
        raise RuntimeError("ASR fallback is not configured.")

    async def transcribe(
        self,
        audio_path: Path,
        progress: ProgressCallback = None,
    ) -> list[TranscriptChunk]:
        del audio_path, progress
        raise RuntimeError("ASR fallback is not configured.")


class GeminiAsrProvider(AsrProvider):
    def __init__(
        self,
        *,
        api_keys: list[str],
        model_name: str = "gemini-3.6-flash",
        client_factory: GeminiClientFactory | None = None,
        cookiefile: str | None = None,
        ffmpeg_location: str | None = None,
    ) -> None:
        self.api_keys = [key for key in api_keys if key]
        self.model_name = model_name
        self.client_factory = client_factory or self._default_client_factory
        self.cookiefile = cookiefile
        self.ffmpeg_location = ffmpeg_location or _find_ffmpeg()

    async def prepare_audio(
        self,
        video: VideoRecord,
        work_dir: Path,
        progress: ProgressCallback = None,
    ) -> Path:
        source = await _download_audio(
            video,
            work_dir,
            cookiefile=self.cookiefile,
            ffmpeg_location=self.ffmpeg_location,
            progress=progress,
        )
        return await _transcode_for_gemini(
            source,
            work_dir / "gemini-16k-mono.mp3",
            ffmpeg_location=self.ffmpeg_location,
            progress=progress,
        )

    async def transcribe(
        self,
        audio_path: Path,
        progress: ProgressCallback = None,
    ) -> list[TranscriptChunk]:
        if not self.api_keys:
            raise GeminiKeysExhausted("No Gemini API key is configured.")
        await _report(progress, "Gemini 正在转写音频...", 0)
        chunks = await asyncio.to_thread(self._transcribe_sync, audio_path)
        await _report(progress, f"Gemini 转写完成，共 {len(chunks)} 段。", 100)
        return chunks

    @staticmethod
    def _default_client_factory(api_key: str):
        from google import genai

        return genai.Client(api_key=api_key)

    def _transcribe_sync(self, audio_path: Path) -> list[TranscriptChunk]:
        # 瞬时错误（429 限流 / 503 高负载）自动多轮退避重试，避免一次 503 就
        # 掉进慢速本地 ASR。
        transient_errors: list[str] = []
        for round_index in range(GEMINI_TRANSIENT_RETRY_ROUNDS):
            quota_errors: list[str] = []
            for index, api_key in enumerate(self.api_keys, start=1):
                client = self.client_factory(api_key)
                uploaded = None
                try:
                    LOGGER.info("Gemini transcription attempt using configured key #%s.", index)
                    uploaded = client.files.upload(file=str(audio_path))
                    response = client.models.generate_content(
                        model=self.model_name,
                        contents=[
                            (
                                "Transcribe the speech in its original language. Return coarse "
                                "timestamped segments as JSON with exactly one key named segments. "
                                "Each segment must have start_seconds, end_seconds, and text. "
                                "Do not translate, summarize, or add commentary."
                            ),
                            uploaded,
                        ],
                        config=self._generation_config(),
                    )
                    return _parse_gemini_transcript(
                        str(getattr(response, "text", "") or ""),
                        duration_seconds=_audio_duration_seconds(audio_path),
                    )
                except Exception as exc:  # noqa: BLE001
                    if not _is_gemini_quota_error(exc):
                        raise
                    transient_errors.append(type(exc).__name__)
                    quota_errors.append(type(exc).__name__)
                    LOGGER.warning(
                        "Gemini key #%s was rate limited or transiently unavailable; "
                        "trying the next key.",
                        index,
                    )
                finally:
                    if uploaded is not None:
                        try:
                            client.files.delete(name=uploaded.name)
                        except Exception as cleanup_error:  # noqa: BLE001
                            LOGGER.warning(
                                "Could not delete Gemini temporary file for key #%s: %s",
                                index,
                                type(cleanup_error).__name__,
                            )
            if transient_errors and round_index + 1 < GEMINI_TRANSIENT_RETRY_ROUNDS:
                delay = GEMINI_TRANSIENT_BACKOFF_SECONDS[
                    min(round_index, len(GEMINI_TRANSIENT_BACKOFF_SECONDS) - 1)
                ]
                LOGGER.warning(
                    "Gemini transient errors so far (%s); retrying in %ss.",
                    ", ".join(sorted(set(transient_errors))),
                    delay,
                )
                time.sleep(delay)
        raise GeminiKeysExhausted(
            "All configured Gemini API keys were rate limited or exhausted "
            f"or transiently unavailable ({len(quota_errors)} attempts across "
            f"{len(transient_errors)} rounds)."
        )

    @staticmethod
    def _generation_config():
        from google.genai import types

        return types.GenerateContentConfig(response_mime_type="application/json")


class FasterWhisperProvider(AsrProvider):
    def __init__(
        self,
        model_size: str = "turbo",
        device: str = "auto",
        cookiefile: str | None = None,
        ffmpeg_location: str | None = None,
        model_loader: WhisperModelLoader | None = None,
    ) -> None:
        self.model_size = model_size
        self.requested_device = device
        self.cookiefile = cookiefile
        self.ffmpeg_location = ffmpeg_location or _find_ffmpeg()
        self.model_loader = model_loader or _default_whisper_loader
        self._resolved_device: str | None = None
        self._compute_type: str | None = None

    async def prepare_audio(
        self,
        video: VideoRecord,
        work_dir: Path,
        progress: ProgressCallback = None,
    ) -> Path:
        return await _download_audio(
            video,
            work_dir,
            cookiefile=self.cookiefile,
            ffmpeg_location=self.ffmpeg_location,
            progress=progress,
        )

    async def transcribe(
        self,
        audio_path: Path,
        progress: ProgressCallback = None,
    ) -> list[TranscriptChunk]:
        duration = _audio_duration_seconds(audio_path)
        await _report(
            progress,
            f"本地 Whisper 开始转写（{duration / 60:.1f} 分钟）。",
            0,
        )
        chunks = await asyncio.to_thread(self._transcribe_sync, audio_path)
        await _report(progress, f"本地转写完成，共 {len(chunks)} 段。", 100)
        return chunks

    def _resolve_runtime(self) -> tuple[str, str]:
        if self._resolved_device and self._compute_type:
            return self._resolved_device, self._compute_type
        device = _resolve_whisper_device(self.requested_device)
        compute_type = "int8"
        if device == "cuda":
            supported = _supported_whisper_compute_types(device)
            compute_type = next(
                (
                    candidate
                    for candidate in ("int8_float16", "int8", "int8_float32", "float32")
                    if candidate in supported
                ),
                "int8_float16",
            )
        self._resolved_device = device
        self._compute_type = compute_type
        return device, compute_type

    def _model(self) -> tuple[Any, str]:
        device, compute_type = self._resolve_runtime()
        loader_id = id(self.model_loader)
        key = (self.model_size, device, compute_type, loader_id)
        with _WHISPER_MODELS_LOCK:
            if key in _WHISPER_MODELS:
                return _WHISPER_MODELS[key], device
            try:
                model = self.model_loader(self.model_size, device, compute_type)
            except (RuntimeError, ValueError) as exc:
                if device != "cuda":
                    raise
                LOGGER.warning(
                    "CUDA Whisper initialization failed (%s); falling back to CPU int8.",
                    type(exc).__name__,
                )
                device = "cpu"
                compute_type = "int8"
                self._resolved_device = device
                self._compute_type = compute_type
                key = (self.model_size, device, compute_type, loader_id)
                if key in _WHISPER_MODELS:
                    return _WHISPER_MODELS[key], device
                model = self.model_loader(self.model_size, device, compute_type)
            _WHISPER_MODELS[key] = model
            return model, device

    def _transcribe_sync(self, audio_path: Path) -> list[TranscriptChunk]:
        model, device = self._model()
        gate = _WHISPER_GPU_SEMAPHORE if device == "cuda" else nullcontext()
        with gate:
            segments, _info = model.transcribe(
                str(audio_path),
                beam_size=5,
                vad_filter=True,
                language=None,
            )
            return [
                TranscriptChunk(
                    start=round(float(segment.start), 2),
                    end=round(float(segment.end), 2),
                    text=str(segment.text or "").strip(),
                )
                for segment in segments
                if str(segment.text or "").strip()
            ]


def _default_whisper_loader(model_size: str, device: str, compute_type: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper is not installed. Install creator-report[asr]."
        ) from exc
    return WhisperModel(model_size, device=device, compute_type=compute_type)


def _resolve_whisper_device(requested: str) -> str:
    normalized = requested.lower().strip()
    if normalized in {"cuda", "cpu"}:
        return normalized
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
    except Exception:  # noqa: BLE001
        pass
    LOGGER.warning("CUDA is unavailable; Faster Whisper will run on CPU int8.")
    return "cpu"


def _supported_whisper_compute_types(device: str) -> set[str]:
    try:
        import ctranslate2

        return set(ctranslate2.get_supported_compute_types(device))
    except Exception:  # noqa: BLE001
        return set()


def _is_gemini_quota_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status_code in (429, 503) or str(status_code) in ("429", "503"):
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "429",
            "503",
            "resource_exhausted",
            "quota",
            "rate limit",
            "too many requests",
            "unavailable",
            "high demand",
        )
    )


def _parse_gemini_transcript(
    raw_text: str,
    *,
    duration_seconds: float = 0,
) -> list[TranscriptChunk]:
    stripped = raw_text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        payload = None

    if isinstance(payload, dict) and isinstance(payload.get("segments"), list):
        chunks = []
        for segment in payload["segments"]:
            if not isinstance(segment, dict):
                continue
            text_value = str(segment.get("text") or "").strip()
            if not text_value:
                continue
            start = _coerce_seconds(
                segment.get("start_seconds", segment.get("start", 0))
            )
            end = _coerce_seconds(
                segment.get("end_seconds", segment.get("end", start))
            )
            chunks.append(
                TranscriptChunk(
                    start=round(start, 2),
                    end=round(max(start, end), 2),
                    text=text_value,
                )
            )
        if chunks:
            return chunks

    timestamped = _parse_timestamped_lines(stripped)
    if timestamped:
        return timestamped
    if stripped:
        return [
            TranscriptChunk(
                start=0,
                end=round(max(0, duration_seconds), 2),
                text=stripped,
            )
        ]
    raise RuntimeError("Gemini returned an empty transcript.")


def _coerce_seconds(value: Any) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    text_value = str(value or "0").strip()
    pieces = text_value.split(":")
    try:
        if len(pieces) == 3:
            return max(
                0.0,
                int(pieces[0]) * 3600 + int(pieces[1]) * 60 + float(pieces[2]),
            )
        if len(pieces) == 2:
            return max(0.0, int(pieces[0]) * 60 + float(pieces[1]))
        return max(0.0, float(pieces[0]))
    except (TypeError, ValueError):
        return 0.0


def _parse_timestamped_lines(text_value: str) -> list[TranscriptChunk]:
    rows: list[tuple[float, str]] = []
    pattern = re.compile(
        r"^\s*\[?(?P<time>\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)\]?\s*[-–:]?\s*(?P<text>.+)$"
    )
    for line in text_value.splitlines():
        match = pattern.match(line)
        if match:
            rows.append((_coerce_seconds(match.group("time")), match.group("text").strip()))
    chunks = []
    for index, (start, line) in enumerate(rows):
        end = rows[index + 1][0] if index + 1 < len(rows) else start
        chunks.append(TranscriptChunk(start=start, end=max(start, end), text=line))
    return chunks


async def _report(
    progress: ProgressCallback,
    message: str,
    percent: float | None = None,
) -> None:
    if progress is None:
        LOGGER.info(message)
        return
    try:
        result = progress(message, percent)
        if asyncio.iscoroutine(result):
            await result
    except Exception:  # noqa: BLE001
        LOGGER.debug("Progress callback failed.", exc_info=True)


async def _download_audio(
    video: VideoRecord,
    work_dir: Path,
    *,
    cookiefile: str | None = None,
    ffmpeg_location: str | None = None,
    progress: ProgressCallback = None,
) -> Path:
    output_path = work_dir / "audio.mp3"
    if output_path.exists() and output_path.stat().st_size > 1000:
        await _report(progress, "音频缓存命中。", 100)
        return output_path

    work_dir.mkdir(parents=True, exist_ok=True)
    for candidate in work_dir.glob("audio.*"):
        if candidate.name != "audio.mp3" or candidate.stat().st_size < 1000:
            candidate.unlink(missing_ok=True)
    await _report(progress, "正在下载音频...", 0)

    options: dict[str, Any] = {
        "quiet": True,
        # Audio-only strictly: never fall back to full video downloads.
        "format": "bestaudio",
        "outtmpl": str(work_dir / "audio.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }
        ],
        "noplaylist": True,
        "no_warnings": True,
        "retries": 1,
        "fragment_retries": 1,
        "extractor_args": {
            "bilibili": {"player_client": ["android", "ios"]},
        },
    }
    if cookiefile:
        options["cookiefile"] = cookiefile
    if ffmpeg_location:
        options["ffmpeg_location"] = ffmpeg_location

    def _run(opts: dict[str, Any]) -> None:
        with YoutubeDL(opts) as ydl:
            ydl.download([video.source_url])

    try:
        await asyncio.to_thread(_run, options)
    except DownloadError as exc:
        if cookiefile and "412" in str(exc):
            # A stale/flagged cookie session can 412-block downloads;
            # retry once without cookies (public audio streams).
            anonymous_options = {key: value for key, value in options.items() if key != "cookiefile"}
            await asyncio.to_thread(_run, anonymous_options)
        else:
            raise
    if output_path.exists() and output_path.stat().st_size > 1000:
        await _report(progress, "音频下载完成。", 100)
        return output_path
    for extension in ("m4a", "opus", "webm", "aac", "ogg"):
        candidate = work_dir / f"audio.{extension}"
        if candidate.exists() and candidate.stat().st_size > 1000:
            candidate.replace(output_path)
            await _report(progress, "音频下载完成。", 100)
            return output_path
    raise RuntimeError(f"Audio download did not create a usable file in {work_dir}.")


async def _transcode_for_gemini(
    source_path: Path,
    output_path: Path,
    *,
    ffmpeg_location: str | None,
    progress: ProgressCallback = None,
) -> Path:
    if output_path.exists() and output_path.stat().st_size > 1000:
        await _report(progress, "Gemini 低码率音频缓存命中。", 100)
        return output_path
    ffmpeg = _ffmpeg_executable(ffmpeg_location)
    if not ffmpeg:
        raise RuntimeError("ffmpeg was not found; Gemini audio preprocessing is unavailable.")
    await _report(progress, "正在生成 16kHz 单声道 Gemini 音频...", 0)

    def run() -> None:
        result = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(source_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-b:a",
                "48k",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if result.returncode != 0:
            output_path.unlink(missing_ok=True)
            raise RuntimeError(
                "ffmpeg Gemini preprocessing failed: "
                + (result.stderr[-800:] if result.stderr else "unknown error")
            )

    await asyncio.to_thread(run)
    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise RuntimeError("Gemini audio preprocessing produced no output.")
    await _report(progress, "Gemini 音频预处理完成。", 100)
    return output_path
