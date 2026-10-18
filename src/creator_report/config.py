from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field


class Settings(BaseModel):
    base_dir: Path
    database_url: str
    runs_dir: Path
    cache_dir: Path
    reports_dir: Path
    templates_dir: Path
    static_dir: Path
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8768)
    service_id: str = Field(default="creator-report")
    deepseek_api_key: str | None = Field(default=None)
    deepseek_base_url: str = Field(default="https://opencode.ai/zen/go/v1")
    deepseek_model: str = Field(default="deepseek-v4-flash")
    gemini_api_keys: list[str] = Field(default_factory=list)
    gemini_model: str = Field(default="gemini-3.6-flash")
    output_language: str = Field(default="zh-CN")
    max_concurrency: int = Field(default=2)
    max_job_workers: int = Field(default=2)
    default_recent_limit: int = Field(default=10)
    default_transcription_mode: str = Field(default="auto")
    audio_retention_days: int = Field(default=30)
    asr_provider: str = Field(default="whisper")
    asr_model_size: str = Field(default="turbo")
    asr_device: str = Field(default="auto")
    bilibili_cookie: str | None = Field(default=None)
    bilibili_cookie_file: Path | None = Field(default=None)


class LocalPreferences(BaseModel):
    default_recent_limit: int = Field(default=10, ge=1, le=50)
    default_transcription_mode: Literal["auto", "gemini", "local"] = "auto"
    gemini_key_labels: dict[str, str] = Field(default_factory=dict)


class GeminiKeyDraft(BaseModel):
    key_id: str | None = None
    label: str = Field(default="", max_length=60)
    value: str = Field(default="", max_length=512)


class SettingsUpdate(LocalPreferences):
    gemini_keys: list[GeminiKeyDraft] | None = Field(default=None, max_length=20)


def _api_keys_from_environment() -> list[str]:
    raw = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY") or ""
    normalized = raw.replace("\r", "\n").replace(";", ",").replace("\n", ",")
    return list(dict.fromkeys(part.strip() for part in normalized.split(",") if part.strip()))


def get_settings(base_dir: Path | None = None) -> Settings:
    root = Path(base_dir or os.environ.get("CREATOR_REPORT_BASE_DIR") or Path.cwd()).resolve()
    load_dotenv(root / ".env", override=False)
    data_dir = root / "data"
    package_root = Path(__file__).resolve().parents[2]
    templates_dir = root / "templates"
    static_dir = root / "static"
    bilibili_cookie_file = Path(
        os.environ.get("BILIBILI_COOKIE_FILE", root / "data" / "bilibili_cookie.txt")
    ).resolve()

    bilibili_cookie = os.environ.get("BILIBILI_COOKIE")
    if bilibili_cookie is None and bilibili_cookie_file.exists():
        bilibili_cookie = bilibili_cookie_file.read_text(encoding="utf-8").strip() or None

    return Settings(
        base_dir=root,
        database_url=f"sqlite:///{(data_dir / 'creator_report.db').as_posix()}",
        runs_dir=root / "runs",
        cache_dir=data_dir / "cache",
        reports_dir=root / "reports",
        templates_dir=templates_dir if templates_dir.exists() else package_root / "templates",
        static_dir=static_dir if static_dir.exists() else package_root / "static",
        host=os.environ.get("CREATOR_REPORT_HOST", "127.0.0.1"),
        port=int(os.environ.get("CREATOR_REPORT_PORT", "8768")),
        deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY_YOUTUBE_REPORT") or os.environ.get("DEEPSEEK_API_KEY"),
        deepseek_base_url=os.environ.get("DEEPSEEK_BASE_URL_YOUTUBE_REPORT") or os.environ.get("DEEPSEEK_BASE_URL", "https://opencode.ai/zen/go/v1"),
        deepseek_model=os.environ.get("DEEPSEEK_MODEL_YOUTUBE_REPORT") or os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        gemini_api_keys=_api_keys_from_environment(),
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
        output_language=os.environ.get("OUTPUT_LANGUAGE", "zh-CN"),
        max_concurrency=int(os.environ.get("MAX_CONCURRENCY", "2")),
        max_job_workers=int(os.environ.get("MAX_JOB_WORKERS", "2")),
        default_recent_limit=int(os.environ.get("DEFAULT_RECENT_LIMIT", "10")),
        default_transcription_mode=os.environ.get("DEFAULT_TRANSCRIPTION_MODE", "auto"),
        audio_retention_days=int(os.environ.get("AUDIO_RETENTION_DAYS", "30")),
        asr_provider=os.environ.get("ASR_PROVIDER", "whisper"),
        asr_model_size=os.environ.get("ASR_MODEL_SIZE", "turbo"),
        asr_device=os.environ.get("ASR_DEVICE", "auto"),
        bilibili_cookie=bilibili_cookie,
        bilibili_cookie_file=bilibili_cookie_file,
    )


def load_local_preferences(settings: Settings) -> LocalPreferences:
    path = settings.base_dir / "local_settings.json"
    defaults = LocalPreferences(
        default_recent_limit=settings.default_recent_limit,
        default_transcription_mode=settings.default_transcription_mode,
    )
    if not path.exists():
        return defaults
    try:
        return LocalPreferences.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaults


def save_local_preferences(
    settings: Settings,
    preferences: LocalPreferences,
) -> LocalPreferences:
    path = settings.base_dir / "local_settings.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(preferences.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
    return preferences


def gemini_key_id(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def resolve_gemini_key_drafts(
    current_keys: list[str],
    drafts: list[GeminiKeyDraft],
    current_labels: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    current_by_id = {gemini_key_id(api_key): api_key for api_key in current_keys}
    known_labels = current_labels or {}
    resolved: list[str] = []
    labels: dict[str, str] = {}
    for draft in drafts:
        value = draft.value.strip()
        if value:
            if len(value) < 8 or re.search(r"[\s,;\r\n]", value):
                raise ValueError("Gemini API Key 格式无效。")
            api_key = value
        elif draft.key_id:
            api_key = current_by_id.get(draft.key_id, "")
            if not api_key:
                raise ValueError("Gemini API Key 标识已失效，请刷新设置页。")
        else:
            continue
        if api_key not in resolved:
            resolved.append(api_key)
            resolved_id = gemini_key_id(api_key)
            label = draft.label.strip()
            if not label and draft.key_id:
                label = known_labels.get(draft.key_id, "")
            labels[resolved_id] = label or f"Key {len(resolved)}"
    return resolved, labels


def save_gemini_api_keys(settings: Settings, api_keys: list[str]) -> None:
    path = settings.base_dir / ".env"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    assignment = re.compile(
        r"^\s*(?:export\s+)?GEMINI_API_KEYS?\s*=",
        flags=re.IGNORECASE,
    )
    lines = [
        line
        for line in existing.splitlines()
        if not assignment.match(line)
    ]
    if api_keys:
        lines.append(f"GEMINI_API_KEYS={','.join(api_keys)}")
    rendered = "\n".join(lines)
    if rendered:
        rendered += "\n"
    temporary = path.with_name(".env.tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(path)
