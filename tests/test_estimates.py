from __future__ import annotations

import pytest

from creator_report.estimates import estimate_usage


def test_gemini_estimate_excludes_cached_audio_and_counts_deepseek_calls() -> None:
    estimate = estimate_usage(
        durations_seconds=[60, 120, None],
        cached_indexes={1},
        transcription_mode="auto",
        gemini_available=True,
    )

    assert estimate.video_count == 3
    assert estimate.duration_seconds == 780
    assert estimate.cached_video_count == 1
    assert estimate.gemini_audio_seconds == 660
    assert estimate.deepseek_call_count == 4
    assert estimate.gemini_cost_usd_low == pytest.approx(0.05148, abs=0.00001)
    assert estimate.gemini_cost_usd_high == pytest.approx(0.07128, abs=0.00001)


def test_local_mode_has_no_gemini_cost_and_large_jobs_require_confirmation() -> None:
    estimate = estimate_usage(
        durations_seconds=[60] * 11,
        cached_indexes=set(),
        transcription_mode="local",
        gemini_available=True,
    )

    assert estimate.gemini_audio_seconds == 0
    assert estimate.gemini_cost_usd_low == 0
    assert estimate.gemini_cost_usd_high == 0
    assert estimate.requires_confirmation is True
