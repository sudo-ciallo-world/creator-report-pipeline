from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from creator_report.models import SourceResolution


def detect_platform(url: str) -> str:
    lowered = url.lower()
    if "youtube.com" in lowered or "youtu.be" in lowered:
        return "youtube"
    if "bilibili.com" in lowered:
        return "bilibili"
    raise ValueError(f"Unsupported source URL: {url}")


def normalize_source_url(url: str) -> str:
    parts = urlsplit(url.strip())
    clean_path = parts.path.rstrip("/") or "/"
    normalized = urlunsplit((parts.scheme, parts.netloc, clean_path, "", ""))
    if normalized.endswith("/"):
        normalized = normalized[:-1]
    return normalized


def resolve_source(url: str) -> SourceResolution:
    normalized = normalize_source_url(url)
    return SourceResolution(platform=detect_platform(normalized), normalized_url=normalized)
