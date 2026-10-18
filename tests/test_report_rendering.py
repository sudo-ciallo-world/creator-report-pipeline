from datetime import datetime, timezone
from pathlib import Path

from creator_report.models import (
    ChannelProfile,
    ChannelReport,
    TopicCluster,
    TranscriptChunk,
    VideoRecord,
    VideoSummary,
)
from creator_report.reports import render_report_bundle


def test_render_report_bundle_writes_index_and_video_detail_pages(tmp_path: Path) -> None:
    channel = ChannelProfile(
        source_id="bili:synthetic-channel",
        platform="bilibili",
        title="Demo UP",
        description="Talks about tools",
        source_url="https://space.bilibili.com/synthetic-channel",
        avatar_path="assets/media/avatar.jpg",
    )
    video = VideoRecord(
        video_id="BV1xx",
        platform="bilibili",
        source_url="https://www.bilibili.com/video/BV1xx",
        title="Tool thinking",
        description="How tools help output",
        published_at=datetime(2026, 6, 4, tzinfo=timezone.utc),
        duration_seconds=480,
        transcript=[TranscriptChunk(start=0, end=10, text="Tools make people more scalable.")],
        summary=VideoSummary(
            overview="A summary",
            key_points=["Tooling compounds output"],
            viewpoints=["Scale comes from reusable systems"],
            topics=["tools"],
            snippets=["Tools make people more scalable."],
        ),
    )
    report = ChannelReport(
        channel=channel,
        generated_at=datetime(2026, 6, 15, tzinfo=timezone.utc),
        video_count=1,
        processed_video_count=1,
        failed_video_count=0,
        date_range_label="2026-06-04 to 2026-06-04",
        high_level_summary="Creator focuses on tools.",
        repeated_viewpoints=["Scale comes from reusable systems"],
        recurring_formats=["Long-form explainers"],
        topic_clusters=[TopicCluster(label="tools", summary="Tools are central.", representative_video_ids=["BV1xx"])],
        videos=[video],
        failures=[],
        topic_prompt=None,
    )

    bundle = render_report_bundle(report=report, output_dir=tmp_path)

    assert (tmp_path / "index.html").exists()
    assert (tmp_path / "videos" / "BV1xx.html").exists()
    assert (tmp_path / "creator_corpus.json").exists()
    assert "BV1xx.html" in bundle.index_html
    detail_text = (tmp_path / "videos" / "BV1xx.html").read_text(encoding="utf-8")
    assert 'class="channel-identity"' in detail_text
    assert "Demo UP" in detail_text
    assert "博主 ID synthetic-channel" in detail_text
    assert detail_text.index("短评") < detail_text.index("原视频简介")
    assert "<details" in detail_text

    # Verify downloadable artifacts
    assert (tmp_path / "full_transcripts.md").exists()
    transcript_text = (tmp_path / "full_transcripts.md").read_text(encoding="utf-8")
    assert "## Tool thinking" in transcript_text
    assert "Tools make people more scalable." in transcript_text
    # Verify NO AI summary content leaked into transcript file
    assert "整体结论" not in transcript_text
    assert "A summary" not in transcript_text

    assert (tmp_path / "channel_summary.txt").exists()
    summary_text = (tmp_path / "channel_summary.txt").read_text(encoding="utf-8")
    assert "整体结论" in summary_text
    assert "A summary" in summary_text
    assert "Creator focuses on tools." in summary_text
