from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime

from creator_report.models import (
    ChannelProfile,
    ChannelReport,
    ContentDistributionItem,
    TopicCluster,
    VideoFailure,
    VideoRecord,
)


def _date_range_label(videos: list[VideoRecord]) -> str:
    dates = [video.published_at.date().isoformat() for video in videos if video.published_at]
    if not dates:
        return "Unknown"
    return f"{min(dates)} to {max(dates)}"


def _recurring_formats(videos: list[VideoRecord]) -> list[str]:
    formats: Counter[str] = Counter()
    for video in videos:
        duration = video.duration_seconds or 0
        if video.video_type == "short":
            formats["Short-form clips"] += 1
        elif duration >= 1200:
            formats["Long-form deep dives"] += 1
        elif duration >= 300:
            formats["Mid-length explainers"] += 1
        else:
            formats["Quick takes"] += 1
    return [label for label, _ in formats.most_common(3)]


def build_channel_report(
    channel: ChannelProfile,
    videos: list[VideoRecord],
    topic_prompt: str | None,
) -> ChannelReport:
    processed = [video for video in videos if video.summary]
    failures = [
        VideoFailure(video_id=video.video_id, title=video.title, reason=video.processing_error or "unknown")
        for video in videos
        if video.processing_error
    ]
    topic_to_video_ids: dict[str, list[str]] = defaultdict(list)
    topic_to_points: dict[str, list[str]] = defaultdict(list)
    topic_display_labels: dict[str, str] = {}
    viewpoints: Counter[str] = Counter()

    for video in processed:
        summary = video.summary
        assert summary is not None
        for viewpoint in summary.viewpoints:
            viewpoints[viewpoint] += 1
        for topic in summary.topics:
            normalized = topic.strip().lower()
            if not normalized:
                continue
            topic_display_labels.setdefault(normalized, topic.strip())
            topic_to_video_ids[normalized].append(video.video_id)
            topic_to_points[normalized].extend(summary.key_points)

    topic_clusters: list[TopicCluster] = []
    for topic, ids in sorted(topic_to_video_ids.items(), key=lambda item: (-len(item[1]), item[0])):
        unique_ids = list(dict.fromkeys(ids))[:5]
        points = topic_to_points[topic][:3]
        summary = "；".join(points) if points else f"{channel.title} repeatedly talks about {topic}."
        topic_clusters.append(
            TopicCluster(
                label=topic_display_labels.get(topic, topic),
                summary=summary,
                representative_video_ids=unique_ids,
                video_count=len(set(ids)),
            )
        )

    video_count = len(videos)
    processed_count = len(processed)
    failed_count = len(failures)
    high_level_summary = (
        f"{channel.title} 在 {processed_count} 个已处理视频里，持续输出 "
        f"{'、'.join(cluster.label for cluster in topic_clusters[:5]) or '稳定主题内容'}。"
    )
    if topic_prompt:
        high_level_summary += f" 本次任务额外围绕主题“{topic_prompt}”进行筛选。"

    return ChannelReport(
        channel=channel,
        generated_at=datetime.now(UTC),
        video_count=video_count,
        processed_video_count=processed_count,
        failed_video_count=failed_count,
        date_range_label=_date_range_label(videos),
        high_level_summary=high_level_summary,
        repeated_viewpoints=[text for text, _ in viewpoints.most_common(8)],
        recurring_formats=_recurring_formats(videos),
        topic_clusters=topic_clusters,
        videos=videos,
        failures=failures,
        topic_prompt=topic_prompt,
        channel_theme=" / ".join(cluster.label for cluster in topic_clusters[:4])
        or channel.description[:120]
        or "综合内容",
        viewpoint_thread=[text for text, _ in viewpoints.most_common(6)],
        content_distribution=[
            ContentDistributionItem(
                label=cluster.label,
                video_count=cluster.video_count,
                share_percent=round(
                    cluster.video_count / max(processed_count, 1) * 100,
                    1,
                ),
            )
            for cluster in topic_clusters[:8]
        ],
        recommended_video_ids=[video.video_id for video in processed[:5]],
    )
