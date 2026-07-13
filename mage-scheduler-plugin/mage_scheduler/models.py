from __future__ import annotations

from datetime import datetime, timezone
import json
from sqlalchemy import Column, DateTime, Integer, Text
from db import Base


class Action(Base):
    __tablename__ = "actions"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(Text, nullable=False, unique=True)
    description = Column(Text, nullable=True)
    command = Column(Text, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), nullable=False)
    default_cwd = Column(Text, nullable=True)
    allowed_env_json = Column(Text, nullable=True)
    allowed_command_dirs_json = Column(Text, nullable=True)
    allowed_cwd_dirs_json = Column(Text, nullable=True)
    max_retries = Column(Integer, default=0, nullable=False)
    retry_delay = Column(Integer, default=60, nullable=False)
    retain_result = Column(Integer, default=0, nullable=False)
    # Require an end-to-end receipt acknowledgment before a task counts as
    # successful. On by default for the seeded ask_assistant action, whose
    # HTTP 200 only means "queued to the frontend", not "received".
    require_ack = Column(Integer, default=0, nullable=False)

    @property
    def allowed_env(self) -> list[str] | None:
        if not self.allowed_env_json:
            return None
        try:
            return json.loads(self.allowed_env_json)
        except json.JSONDecodeError:
            return None

    @property
    def allowed_command_dirs(self) -> list[str] | None:
        if not self.allowed_command_dirs_json:
            return None
        try:
            return json.loads(self.allowed_command_dirs_json)
        except json.JSONDecodeError:
            return None

    @property
    def allowed_cwd_dirs(self) -> list[str] | None:
        if not self.allowed_cwd_dirs_json:
            return None
        try:
            return json.loads(self.allowed_cwd_dirs_json)
        except json.JSONDecodeError:
            return None


class Settings(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, index=True)
    allowed_command_dirs_json = Column(Text, nullable=True)
    allowed_cwd_dirs_json = Column(Text, nullable=True)
    cleanup_enabled = Column(Integer, default=0, nullable=False)
    task_retention_days = Column(Integer, default=30, nullable=False)
    # How to handle a task whose scheduled run was missed while the scheduler
    # was offline (overdue by more than missed_grace_seconds):
    #   "always_ask" — park the task in the "missed" status and wait for a
    #                  human run/skip decision (default).
    #   "auto_run"   — re-dispatch the task immediately on detection.
    #   "auto_skip"  — cancel the task and cascade-fail its dependents.
    missed_task_policy = Column(Text, default="always_ask", nullable=False)
    # Overdue threshold in seconds. A task overdue by <= this is treated as
    # effectively on-time and simply runs (covers the 60s beat latency and
    # brief sleeps); overdue by more than this triggers missed_task_policy.
    missed_grace_seconds = Column(Integer, default=300, nullable=False)
    # How long a require_ack task waits in 'awaiting_ack' for a receipt
    # confirmation before it is parked as 'missed'.
    ack_timeout_seconds = Column(Integer, default=900, nullable=False)

    @property
    def allowed_command_dirs(self) -> list[str] | None:
        if not self.allowed_command_dirs_json:
            return None
        try:
            return json.loads(self.allowed_command_dirs_json)
        except json.JSONDecodeError:
            return None

    @property
    def allowed_cwd_dirs(self) -> list[str] | None:
        if not self.allowed_cwd_dirs_json:
            return None
        try:
            return json.loads(self.allowed_cwd_dirs_json)
        except json.JSONDecodeError:
            return None


class TaskRequest(Base):
    __tablename__ = "task_requests"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), nullable=False)
    description = Column(Text, nullable=False)
    command = Column(Text, nullable=False)
    run_at = Column(DateTime, nullable=False)
    status = Column(Text, default="scheduled", nullable=False)
    job_id = Column(Text, nullable=True)
    result = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    intent_version = Column(Text, nullable=True)
    source = Column(Text, nullable=True)
    action_id = Column(Integer, nullable=True)
    action_name = Column(Text, nullable=True)
    cwd = Column(Text, nullable=True)
    env_json = Column(Text, nullable=True)
    notify_on_complete = Column(Integer, default=0, nullable=False)
    max_retries = Column(Integer, default=0, nullable=False)
    retry_delay = Column(Integer, default=60, nullable=False)
    retry_count = Column(Integer, default=0, nullable=False)
    recurring_task_id = Column(Integer, nullable=True)
    retain_result = Column(Integer, default=0, nullable=False)
    # Receipt-acknowledgment gate (see Action.require_ack). When set, a
    # successful delivery moves the task to 'awaiting_ack' with a one-time
    # token and deadline instead of 'success'; a matching ack confirms it.
    require_ack = Column(Integer, default=0, nullable=False)
    ack_token = Column(Text, nullable=True)
    ack_deadline = Column(DateTime, nullable=True)

    @property
    def env_keys(self) -> list[str] | None:
        if not self.env_json:
            return None
        try:
            data = json.loads(self.env_json)
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict):
            return list(data.keys())
        return None


class TaskDependency(Base):
    __tablename__ = "task_dependencies"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, nullable=False, index=True)
    depends_on_task_id = Column(Integer, nullable=False, index=True)


class RecurringTask(Base):
    __tablename__ = "recurring_tasks"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(Text, nullable=False, unique=True)
    description = Column(Text, nullable=True)
    cron = Column(Text, nullable=False)
    timezone = Column(Text, nullable=False, default="UTC")
    action_name = Column(Text, nullable=True)
    command = Column(Text, nullable=True)
    cwd = Column(Text, nullable=True)
    env_json = Column(Text, nullable=True)
    notify_on_complete = Column(Integer, default=0, nullable=False)
    max_retries = Column(Integer, default=0, nullable=False)
    retry_delay = Column(Integer, default=60, nullable=False)
    enabled = Column(Integer, default=1, nullable=False)
    require_ack = Column(Integer, default=0, nullable=False)
    next_run_at = Column(DateTime, nullable=True)
    last_run_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), nullable=False)

    @property
    def env_keys(self) -> list[str] | None:
        if not self.env_json:
            return None
        try:
            data = json.loads(self.env_json)
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict):
            return list(data.keys())
        return None
