from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlmodel import Field, Session, SQLModel, create_engine, select

from creator_report.config import Settings
from creator_report.models import JobRequest, JobState


TERMINAL_JOB_STATUSES = {
    "completed",
    "partial",
    "failed",
    "cancelled",
    "interrupted",
}
ALL_JOB_STATUSES = (
    "queued",
    "running",
    "completed",
    "partial",
    "failed",
    "cancelled",
    "interrupted",
)
SCHEMA_VERSION = 3


class JobRecord(SQLModel, table=True):
    job_id: str = Field(primary_key=True)
    source_url: str
    mode: str
    status: str
    message: str
    report_relpath: str | None = None
    request_json: str = "{}"
    progress: float = 0.0
    current_step: str = "queued"
    retry_of: str | None = None
    cancel_requested: bool = False
    error_details: str | None = None
    config_json: str = "{}"
    channel_id: str | None = None
    channel_name: str | None = None
    channel_avatar_path: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancelled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class Storage:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = create_engine(
            settings.database_url,
            connect_args={"check_same_thread": False},
        )
        self._ensure_dirs()
        SQLModel.metadata.create_all(self.engine)
        self._migrate_schema()
        self._backfill_channel_metadata()

    def _ensure_dirs(self) -> None:
        for path in (
            self.settings.base_dir / "data",
            self.settings.runs_dir,
            self.settings.cache_dir,
            self.settings.reports_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def _migrate_schema(self) -> None:
        column_definitions = {
            "request_json": "TEXT NOT NULL DEFAULT '{}'",
            "progress": "FLOAT NOT NULL DEFAULT 0",
            "current_step": "TEXT NOT NULL DEFAULT 'queued'",
            "retry_of": "TEXT",
            "cancel_requested": "BOOLEAN NOT NULL DEFAULT 0",
            "error_details": "TEXT",
            "config_json": "TEXT NOT NULL DEFAULT '{}'",
            "channel_id": "TEXT",
            "channel_name": "TEXT",
            "channel_avatar_path": "TEXT",
            "started_at": "DATETIME",
            "finished_at": "DATETIME",
            "cancelled_at": "DATETIME",
        }
        with self.engine.begin() as connection:
            existing_columns = {
                str(row[1])
                for row in connection.execute(text("PRAGMA table_info(jobrecord)")).all()
            }
            for name, definition in column_definitions.items():
                if name not in existing_columns:
                    connection.execute(
                        text(f'ALTER TABLE jobrecord ADD COLUMN "{name}" {definition}')
                    )
            connection.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS creator_report_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO creator_report_meta (key, value)
                    VALUES ('schema_version', :version)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """
                ),
                {"version": str(SCHEMA_VERSION)},
            )

    def schema_version(self) -> int:
        with self.engine.connect() as connection:
            value = connection.execute(
                text(
                    "SELECT value FROM creator_report_meta WHERE key = 'schema_version'"
                )
            ).scalar_one()
        return int(value)

    def _backfill_channel_metadata(self) -> None:
        for state in self.list_jobs(limit=10_000):
            if state.channel_id and state.channel_name:
                continue
            report_path = self.settings.reports_dir / state.job_id / "report.json"
            payload = self.read_json(report_path)
            channel = payload.get("channel") if isinstance(payload, dict) else None
            if not isinstance(channel, dict):
                continue
            source_id = str(channel.get("source_id") or "").strip()
            changes: dict[str, str] = {}
            if not state.channel_id and source_id:
                changes["channel_id"] = _display_channel_id(source_id)
            if not state.channel_name and channel.get("title"):
                changes["channel_name"] = str(channel["title"])
            if not state.channel_avatar_path and channel.get("avatar_path"):
                changes["channel_avatar_path"] = str(channel["avatar_path"])
            if changes:
                self.update_job(state.job_id, **changes)

    def create_job(
        self,
        request: JobRequest,
        *,
        status: str = "queued",
        retry_of: str | None = None,
    ) -> JobState:
        now = datetime.now(UTC)
        messages = {
            "queued": "任务已创建，正在排队。",
            "failed": "任务已记录为失败。",
        }
        state = JobState(
            job_id=uuid.uuid4().hex[:12],
            source_url=request.source_url,
            mode=request.mode,
            status=status,
            message=messages.get(status, status),
            request_json=request.model_dump_json(),
            current_step=status,
            retry_of=retry_of,
            created_at=now,
            updated_at=now,
            started_at=now if status == "running" else None,
            finished_at=now if status in TERMINAL_JOB_STATUSES else None,
        )
        self.save_job(state)
        return state

    def save_job(self, state: JobState) -> None:
        record = JobRecord(**state.model_dump())
        with Session(self.engine) as session:
            session.merge(record)
            session.commit()

    def get_job(self, job_id: str) -> JobState | None:
        with Session(self.engine) as session:
            record = session.exec(
                select(JobRecord).where(JobRecord.job_id == job_id)
            ).first()
        if not record:
            return None
        return JobState.model_validate(record.model_dump())

    def list_jobs(
        self,
        *,
        statuses: set[str] | None = None,
        limit: int = 100,
        oldest_first: bool = False,
    ) -> list[JobState]:
        statement = select(JobRecord)
        if statuses:
            statement = statement.where(JobRecord.status.in_(statuses))
        order = JobRecord.created_at.asc() if oldest_first else JobRecord.created_at.desc()
        statement = statement.order_by(order).limit(limit)
        with Session(self.engine) as session:
            records = list(session.exec(statement))
        return [JobState.model_validate(record.model_dump()) for record in records]

    def status_counts(self) -> dict[str, int]:
        counts = {status: 0 for status in ALL_JOB_STATUSES}
        with Session(self.engine) as session:
            records = list(session.exec(select(JobRecord.status)))
        for status in records:
            value = str(status)
            counts[value] = counts.get(value, 0) + 1
        return counts

    def request_for_job(self, job_id: str) -> JobRequest:
        state = self.get_job(job_id)
        if state is None:
            raise KeyError(job_id)
        try:
            payload = json.loads(state.request_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        if not payload:
            payload = {
                "source_url": state.source_url,
                "mode": state.mode,
                "recent_limit": 10,
            }
        return JobRequest.model_validate(payload)

    def retry_job(self, job_id: str) -> JobState:
        original = self.get_job(job_id)
        if original is None:
            raise KeyError(job_id)
        request = self.request_for_job(job_id)
        retried = self.create_job(request, retry_of=job_id)
        return self.update_job(
            retried.job_id,
            channel_id=original.channel_id,
            channel_name=original.channel_name,
            channel_avatar_path=original.channel_avatar_path,
        )

    def request_cancel(self, job_id: str) -> JobState:
        state = self.get_job(job_id)
        if state is None:
            raise KeyError(job_id)
        if state.status == "queued":
            return self.update_job(
                job_id,
                status="cancelled",
                message="任务已在队列中取消。",
                current_step="cancelled",
                cancel_requested=True,
                cancelled_at=datetime.now(UTC),
            )
        if state.status == "running":
            return self.update_job(
                job_id,
                message="取消请求已收到，将在当前安全步骤结束后停止。",
                cancel_requested=True,
            )
        return state

    def is_cancel_requested(self, job_id: str) -> bool:
        state = self.get_job(job_id)
        return bool(state and state.cancel_requested)

    def mark_running_jobs_interrupted(self) -> int:
        running = self.list_jobs(statuses={"running"}, limit=10_000)
        for state in running:
            self.update_job(
                state.job_id,
                status="interrupted",
                message="服务重启时任务仍在运行，已标记为中断。",
                current_step="interrupted",
                error_details="Process ended before the job reached a terminal state.",
            )
        return len(running)

    def update_job(self, job_id: str, **changes: Any) -> JobState:
        current = self.get_job(job_id)
        if current is None:
            raise KeyError(job_id)
        now = datetime.now(UTC)
        allowed = set(JobState.model_fields) - {"job_id", "created_at", "updated_at"}
        for name, value in changes.items():
            if name not in allowed:
                raise ValueError(f"Unsupported job field: {name}")
            setattr(current, name, value)
        if changes.get("status") == "running" and current.started_at is None:
            current.started_at = now
        if changes.get("status") in TERMINAL_JOB_STATUSES:
            current.finished_at = current.finished_at or now
        current.updated_at = now
        self.save_job(current)
        return current

    def write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)

    def read_json(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))


def _display_channel_id(source_id: str) -> str:
    return source_id.split(":", 1)[-1] if ":" in source_id else source_id
