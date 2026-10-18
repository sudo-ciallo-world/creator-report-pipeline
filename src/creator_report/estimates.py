from __future__ import annotations

from pydantic import BaseModel

from creator_report.models import ChannelPreview


DEFAULT_UNKNOWN_DURATION_SECONDS = 600
GEMINI_AUDIO_TOKENS_PER_SECOND = 32
GEMINI_INPUT_USD_PER_MILLION_TOKENS = 1.50
GEMINI_OUTPUT_USD_PER_MILLION_TOKENS = 7.50


class UsageEstimate(BaseModel):
    video_count: int
    duration_seconds: int
    cached_video_count: int
    gemini_audio_seconds: int
    gemini_cost_usd_low: float
    gemini_cost_usd_high: float
    deepseek_call_count: int
    estimated_minutes_low: int
    estimated_minutes_high: int
    requires_confirmation: bool
    channel: ChannelPreview | None = None


def estimate_usage(
    *,
    durations_seconds: list[int | None],
    cached_indexes: set[int],
    transcription_mode: str,
    gemini_available: bool,
) -> UsageEstimate:
    durations = [
        max(0, int(duration)) if duration is not None else DEFAULT_UNKNOWN_DURATION_SECONDS
        for duration in durations_seconds
    ]
    total_seconds = sum(durations)
    cached_seconds = sum(
        duration for index, duration in enumerate(durations) if index in cached_indexes
    )
    uses_gemini = transcription_mode == "gemini" or (
        transcription_mode == "auto" and gemini_available
    )
    gemini_seconds = max(0, total_seconds - cached_seconds) if uses_gemini else 0

    input_cost = (
        gemini_seconds
        * GEMINI_AUDIO_TOKENS_PER_SECOND
        * GEMINI_INPUT_USD_PER_MILLION_TOKENS
        / 1_000_000
    )
    output_cost_low = (
        gemini_seconds * 4 * GEMINI_OUTPUT_USD_PER_MILLION_TOKENS / 1_000_000
    )
    output_cost_high = (
        gemini_seconds * 8 * GEMINI_OUTPUT_USD_PER_MILLION_TOKENS / 1_000_000
    )
    uncached_seconds = max(0, total_seconds - cached_seconds)
    if transcription_mode == "local" or not uses_gemini:
        estimated_low = max(1, round(uncached_seconds / 60 * 0.35))
        estimated_high = max(2, round(uncached_seconds / 60 * 1.2))
    else:
        estimated_low = max(1, round(uncached_seconds / 60 * 0.2))
        estimated_high = max(2, round(uncached_seconds / 60 * 0.65))

    return UsageEstimate(
        video_count=len(durations),
        duration_seconds=total_seconds,
        cached_video_count=len(cached_indexes),
        gemini_audio_seconds=gemini_seconds,
        gemini_cost_usd_low=round(input_cost + output_cost_low, 5),
        gemini_cost_usd_high=round(input_cost + output_cost_high, 5),
        deepseek_call_count=len(durations) + (1 if durations else 0),
        estimated_minutes_low=estimated_low,
        estimated_minutes_high=estimated_high,
        requires_confirmation=len(durations) > 10,
    )
