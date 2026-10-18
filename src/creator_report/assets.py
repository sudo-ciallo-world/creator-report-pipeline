from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from creator_report.models import ChannelProfile, ChannelReport


class AssetLocalizationResult(BaseModel):
    downloaded_count: int
    failed_count: int
    failures: list[str]


async def localize_channel_avatar(
    channel: ChannelProfile,
    output_dir: Path,
    *,
    client: httpx.AsyncClient | None = None,
) -> AssetLocalizationResult:
    if not channel.avatar_url or channel.avatar_path:
        return AssetLocalizationResult(
            downloaded_count=0,
            failed_count=0,
            failures=[],
        )
    media_dir = output_dir / "assets" / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    owned_client = client is None
    active_client = client or httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers={"User-Agent": "CreatorReport/0.2"},
    )
    try:
        avatar = await _download_image(
            active_client,
            channel.avatar_url,
            media_dir,
            "avatar",
        )
        channel.avatar_path = avatar.relative_to(output_dir).as_posix()
        return AssetLocalizationResult(
            downloaded_count=1,
            failed_count=0,
            failures=[],
        )
    except Exception as exc:  # noqa: BLE001
        return AssetLocalizationResult(
            downloaded_count=0,
            failed_count=1,
            failures=[f"avatar: {exc}"],
        )
    finally:
        if owned_client:
            await active_client.aclose()


async def localize_report_assets(
    report: ChannelReport,
    output_dir: Path,
    *,
    client: httpx.AsyncClient | None = None,
) -> AssetLocalizationResult:
    media_dir = output_dir / "assets" / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    owned_client = client is None
    active_client = client or httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers={"User-Agent": "CreatorReport/0.2"},
    )
    downloaded = 0
    failures: list[str] = []
    try:
        avatar_result = await localize_channel_avatar(
            report.channel,
            output_dir,
            client=active_client,
        )
        downloaded += avatar_result.downloaded_count
        failures.extend(avatar_result.failures)

        for video in report.videos:
            if not video.thumbnail_url:
                continue
            safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", video.video_id)[:100] or "video"
            try:
                thumbnail = await _download_image(
                    active_client,
                    video.thumbnail_url,
                    media_dir,
                    safe_id,
                )
                video.thumbnail_path = thumbnail.relative_to(output_dir).as_posix()
                downloaded += 1
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{video.video_id}: {exc}")
    finally:
        if owned_client:
            await active_client.aclose()
    return AssetLocalizationResult(
        downloaded_count=downloaded,
        failed_count=len(failures),
        failures=failures,
    )


async def _download_image(
    client: httpx.AsyncClient,
    url: str,
    destination_dir: Path,
    stem: str,
) -> Path:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("unsupported asset URL")
    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            response = await client.get(url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";")[0].lower()
            if content_type not in {
                "image/jpeg",
                "image/png",
                "image/webp",
                "image/gif",
            }:
                raise ValueError(f"unsupported image type: {content_type or 'unknown'}")
            if len(response.content) > 15 * 1024 * 1024:
                raise ValueError("image exceeds 15 MB")
            extension = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/webp": ".webp",
                "image/gif": ".gif",
            }[content_type]
            destination = destination_dir / f"{stem}{extension}"
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            temporary.write_bytes(response.content)
            temporary.replace(destination)
            return destination
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    assert last_error is not None
    raise last_error
