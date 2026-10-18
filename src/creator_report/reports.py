from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from creator_report.models import ChannelReport, VideoRecord


@dataclass
class RenderBundle:
    output_dir: Path
    index_html: str


def _get_environment(template_dir: Path) -> Environment:
    return Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(["html", "xml"]),
    )


def render_report_bundle(report: ChannelReport, output_dir: Path, template_dir: Path | None = None) -> RenderBundle:
    root = Path(__file__).resolve().parents[2]
    templates = template_dir or root / "templates"
    env = _get_environment(templates)

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "videos").mkdir(parents=True, exist_ok=True)
    _copy_report_assets(root, output_dir)

    index_html = env.get_template("report_index.html").render(report=report)
    (output_dir / "index.html").write_text(index_html, encoding="utf-8")

    detail_template = env.get_template("video_detail.html")
    for video in report.videos:
        detail_html = detail_template.render(report=report, video=video)
        (output_dir / "videos" / f"{video.video_id}.html").write_text(detail_html, encoding="utf-8")

    _write_creator_corpus(report, output_dir)
    _write_aggregated_transcript(report, output_dir)
    _write_channel_summary(report, output_dir)
    return RenderBundle(output_dir=output_dir, index_html=index_html)


def _copy_report_assets(root: Path, output_dir: Path) -> None:
    static_dir = root / "static"
    for relative in (
        Path("fonts") / "NotoSansSC.woff2",
        Path("fonts") / "NotoSerifSC.woff2",
        Path("fonts") / "OFL.txt",
        Path("vendor") / "lucide.min.js",
        Path("vendor") / "LUCIDE-LICENSE.txt",
    ):
        source = static_dir / relative
        if not source.is_file():
            continue
        destination = output_dir / "assets" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _write_creator_corpus(report: ChannelReport, output_dir: Path) -> None:
    payload = {
        "channel": {
            "source_id": report.channel.source_id,
            "platform": report.channel.platform,
            "title": report.channel.title,
            "description": report.channel.description,
            "source_url": report.channel.source_url,
            "avatar_url": report.channel.avatar_url,
        },
        "generated_at": report.generated_at.isoformat(),
        "video_count": report.video_count,
        "processed_video_count": report.processed_video_count,
        "failed_video_count": report.failed_video_count,
        "date_range_label": report.date_range_label,
        "videos": [_video_corpus_item(video) for video in report.videos],
    }
    (output_dir / "creator_corpus.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _video_corpus_item(video: VideoRecord) -> dict:
    transcript_text = "\n".join(chunk.text for chunk in video.transcript)
    return {
        "video_id": video.video_id,
        "platform": video.platform,
        "title": video.title,
        "url": video.source_url,
        "published_at": video.published_at.isoformat() if video.published_at else None,
        "duration_seconds": video.duration_seconds,
        "description": video.description,
        "transcript_status": video.transcript_status,
        "subtitle_source": video.subtitle_source,
        "processing_error": video.processing_error,
        "full_transcript": transcript_text,
    }


def _write_aggregated_transcript(report: ChannelReport, output_dir: Path) -> None:
    """Write pure transcript aggregation — NO AI summaries, NO LLM content.

    Output: output_dir / full_transcripts.md
    Format: Markdown with each video as a ## section containing raw transcript chunks.
    """
    lines: list[str] = []
    lines.append(f"# {report.channel.title} — 完整视频文稿\n\n")
    lines.append(f"**平台**: {report.channel.platform}\n\n")
    lines.append(f"**频道链接**: {report.channel.source_url}\n\n")
    lines.append(f"**已处理视频数**: {report.processed_video_count} / {report.video_count}\n\n")
    lines.append(f"**时间跨度**: {report.date_range_label}\n\n")
    lines.append(f"**生成时间**: {report.generated_at.isoformat()}\n\n")
    lines.append("---\n\n")

    written = 0
    for video in report.videos:
        if not video.transcript:
            continue
        lines.append(f"## {video.title}\n\n")
        lines.append(f"- 链接: {video.source_url}\n")
        lines.append(f"- 字幕来源: {video.subtitle_source or 'N/A'}\n")
        if video.published_at:
            lines.append(f"- 发布日期: {video.published_at.strftime('%Y-%m-%d')}\n")
        lines.append("\n")
        for chunk in video.transcript:
            lines.append(chunk.text + "\n")
        lines.append("\n---\n\n")
        written += 1

    if written == 0:
        lines.append("\n*此频道没有可用的视频文稿。*\n")

    (output_dir / "full_transcripts.md").write_text("".join(lines), encoding="utf-8")


def _write_channel_summary(report: ChannelReport, output_dir: Path) -> None:
    """Write channel-level + per-video AI summaries.

    Output: output_dir / channel_summary.txt
    Contains AI-generated content: high_level_summary, video summaries, key points, viewpoints.
    """
    lines: list[str] = []
    lines.append(f"# {report.channel.title} — 频道总结\n\n")
    lines.append(f"平台: {report.channel.platform}\n")
    lines.append(f"主页: {report.channel.source_url}\n")
    lines.append(f"简介: {report.channel.description}\n\n")
    lines.append(f"已处理视频: {report.processed_video_count} / {report.video_count}\n")
    if report.failed_video_count:
        lines.append(f"失败视频: {report.failed_video_count}\n")
    lines.append(f"时间跨度: {report.date_range_label}\n")
    lines.append(f"内容类型: {', '.join(report.recurring_formats)}\n\n")
    lines.append("---\n\n")

    lines.append("## 整体结论\n\n")
    lines.append(report.high_level_summary + "\n\n")

    if report.repeated_viewpoints:
        lines.append("## 核心观点\n\n")
        for item in report.repeated_viewpoints:
            lines.append(f"- {item}\n")
        lines.append("\n")

    if report.topic_clusters:
        lines.append("## 主题聚类\n\n")
        for cluster in report.topic_clusters:
            lines.append(f"### {cluster.label}\n")
            lines.append(f"{cluster.summary}\n\n")
            vid_links = [
                f"https://www.bilibili.com/video/{vid}/"
                if report.channel.platform == "bilibili"
                else f"https://www.youtube.com/watch?v={vid}"
                for vid in cluster.representative_video_ids
            ]
            lines.append(f"代表视频: {', '.join(vid_links)}\n\n")

    lines.append("---\n\n")
    lines.append("## 各视频摘要\n\n")

    for video in report.videos:
        lines.append(f"### {video.title}\n\n")
        lines.append(f"链接: {video.source_url}\n")
        if video.published_at:
            lines.append(f"日期: {video.published_at.strftime('%Y-%m-%d')}\n")
        lines.append(f"时长: {(video.duration_seconds or 0) // 60} 分钟\n")
        lines.append(f"字幕状态: {video.transcript_status}\n")

        if video.processing_error:
            lines.append(f"处理错误: {video.processing_error}\n")

        if video.summary:
            lines.append(f"\n**摘要**: {video.summary.overview}\n")
            if video.summary.key_points:
                lines.append("\n**关键要点**:\n")
                for kp in video.summary.key_points:
                    lines.append(f"  - {kp}\n")
            if video.summary.viewpoints:
                lines.append("\n**观点**:\n")
                for vp in video.summary.viewpoints:
                    lines.append(f"  - {vp}\n")
            if video.summary.topics:
                lines.append(f"\n**主题**: {' / '.join(video.summary.topics)}\n")
        else:
            lines.append("\n*暂无 AI 摘要*\n")

        lines.append("\n---\n\n")

    (output_dir / "channel_summary.txt").write_text("".join(lines), encoding="utf-8")
