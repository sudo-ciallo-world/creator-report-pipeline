from datetime import datetime, timezone

from creator_report.models import (
    ChannelProfile,
    TranscriptChunk,
    VideoRecord,
    VideoSummary,
)
from creator_report.pipeline import build_channel_report


def test_build_channel_report_groups_topics_and_picks_representative_videos() -> None:
    channel = ChannelProfile(
        source_id="yt:@demo",
        platform="youtube",
        title="Demo Creator",
        description="Focus on AI and automation",
        source_url="https://www.youtube.com/@demo/videos",
    )
    videos = [
        VideoRecord(
            video_id="v1",
            platform="youtube",
            source_url="https://youtu.be/v1",
            title="AI agents for coding",
            description="Why coding agents matter",
            published_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            duration_seconds=600,
            transcript=[TranscriptChunk(start=0, end=12, text="Agents save time for coding teams.")],
            summary=VideoSummary(
                overview="This video explains coding agents.",
                key_points=["Agents reduce repetitive work", "Verification still matters"],
                viewpoints=["Coding agents are leverage tools"],
                topics=["AI agents", "automation"],
                snippets=["Agents save time for coding teams."],
            ),
        ),
        VideoRecord(
            video_id="v2",
            platform="youtube",
            source_url="https://youtu.be/v2",
            title="Automation systems",
            description="Batch workflows",
            published_at=datetime(2026, 6, 2, tzinfo=timezone.utc),
            duration_seconds=720,
            transcript=[TranscriptChunk(start=0, end=12, text="Automation turns repeat tasks into systems.")],
            summary=VideoSummary(
                overview="This video explains automation systems.",
                key_points=["Systems compound output"],
                viewpoints=["Automation should replace repeated work"],
                topics=["automation"],
                snippets=["Automation turns repeat tasks into systems."],
            ),
        ),
    ]

    report = build_channel_report(channel=channel, videos=videos, topic_prompt=None)

    assert report.channel.title == "Demo Creator"
    assert report.video_count == 2
    assert report.topic_clusters[0].label == "automation"
    assert report.topic_clusters[0].representative_video_ids == ["v1", "v2"]
    assert "Coding agents are leverage tools" in report.repeated_viewpoints
