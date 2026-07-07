"""
recurring_check — APScheduler beat job for cron-driven recurring tasks.

Replaces tasks/recurring_task.py. Identical business logic; Celery
decorator replaced with a plain function called by APScheduler interval job.
"""
from __future__ import annotations

from datetime import datetime, timezone

from croniter import croniter
from sqlalchemy import select

from db import SessionLocal, init_db
from models import RecurringTask, TaskRequest


def check_recurring_tasks() -> None:
    """Beat job: fire any recurring tasks that are due, then advance next_run_at."""
    init_db()
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

    from jobs.reconcile import _load_policy, _notify

    summary: dict[str, list] = {
        "rehydrated": [],
        "caught_up": [],
        "missed": [],
        "auto_run": [],
        "auto_skip": [],
    }

    with SessionLocal() as session:
        policy, grace = _load_policy(session)
        due = session.execute(
            select(RecurringTask).where(
                RecurringTask.enabled == 1,
                RecurringTask.next_run_at <= now_utc,
            )
        ).scalars().all()

        for rt in due:
            overdue = (
                (now_utc - rt.next_run_at).total_seconds()
                if rt.next_run_at is not None
                else 0
            )
            if overdue <= grace:
                # On-time (or within beat latency): spawn and dispatch as usual.
                _spawn_task(session, rt, now_utc)
            else:
                # This occurrence was missed while the scheduler was offline.
                bucket = _spawn_missed_occurrence(session, rt, now_utc, policy)
                if bucket is not None:
                    summary[bucket[0]].append(bucket[1])

        session.commit()

    _notify(summary, policy)


def _compute_next_run(cron: str, tz_name: str, from_dt: datetime) -> datetime:
    """Return the next UTC-naive datetime after from_dt for the given cron+tz."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc

    from_aware = from_dt.replace(tzinfo=timezone.utc).astimezone(tz)
    it = croniter(cron, from_aware)
    next_local = it.get_next(datetime)
    return next_local.astimezone(timezone.utc).replace(tzinfo=None)


def compute_initial_next_run(cron: str, tz_name: str) -> datetime:
    """Compute the first next_run_at for a newly created recurring task."""
    return _compute_next_run(cron, tz_name, datetime.now(timezone.utc).replace(tzinfo=None))


def _resolve_command(session, rt: RecurringTask) -> str:
    """Resolve the command a recurring task should run (via its action if set)."""
    command = rt.command or ""
    if rt.action_name and not command:
        from models import Action
        action = session.execute(
            select(Action).where(Action.name == rt.action_name)
        ).scalar_one_or_none()
        if action is not None:
            command = action.command
    return command


def _spawn_task(session, rt: RecurringTask, now_utc: datetime) -> None:
    """Create a TaskRequest from a RecurringTask and schedule it immediately."""
    command = _resolve_command(session, rt)

    if not command:
        rt.last_run_at = now_utc
        rt.next_run_at = _compute_next_run(rt.cron, rt.timezone, now_utc)
        return

    task_request = TaskRequest(
        description=rt.description or rt.name,
        command=command,
        run_at=now_utc,
        status="scheduled",
        action_name=rt.action_name,
        cwd=rt.cwd,
        env_json=rt.env_json,
        notify_on_complete=rt.notify_on_complete,
        max_retries=rt.max_retries,
        retry_delay=rt.retry_delay,
        recurring_task_id=rt.id,
    )
    session.add(task_request)
    session.flush()
    # Commit before dispatch so the worker sees the row
    session.commit()

    from dispatch import schedule_command
    now_aware = now_utc.replace(tzinfo=timezone.utc)
    job_id = schedule_command(task_request.id, command, now_aware)
    task_request.job_id = job_id

    rt.last_run_at = now_utc
    rt.next_run_at = _compute_next_run(rt.cron, rt.timezone, now_utc)


def _spawn_missed_occurrence(
    session, rt: RecurringTask, now_utc: datetime, policy: str
) -> tuple[str, dict] | None:
    """Handle a recurring occurrence that was missed while offline.

    Creates a TaskRequest recording the missed occurrence and, per the global
    policy, parks it (``always_ask`` → status ``missed``), re-dispatches it
    (``auto_run``), or cancels it (``auto_skip``). Always advances the recurring
    schedule so it does not re-fire every beat. Returns ``(bucket, brief)`` for
    the notification summary, or ``None`` if there was nothing to run.
    """
    from jobs.reconcile import _brief

    command = _resolve_command(session, rt)
    if not command:
        rt.last_run_at = now_utc
        rt.next_run_at = _compute_next_run(rt.cron, rt.timezone, now_utc)
        return None

    intended = rt.next_run_at or now_utc
    task_request = TaskRequest(
        description=rt.description or rt.name,
        command=command,
        run_at=intended,  # the time it was supposed to run
        status="missed",
        action_name=rt.action_name,
        cwd=rt.cwd,
        env_json=rt.env_json,
        notify_on_complete=rt.notify_on_complete,
        max_retries=rt.max_retries,
        retry_delay=rt.retry_delay,
        recurring_task_id=rt.id,
    )
    session.add(task_request)
    session.flush()

    if policy == "auto_run":
        task_request.status = "scheduled"
        session.commit()  # worker must see the row before dispatch
        from dispatch import schedule_command
        now_aware = now_utc.replace(tzinfo=timezone.utc)
        task_request.job_id = schedule_command(task_request.id, command, now_aware)
        bucket = "auto_run"
    elif policy == "auto_skip":
        task_request.status = "cancelled"
        task_request.error = (
            "Skipped by policy: recurring occurrence missed while the "
            "scheduler was offline."
        )
        bucket = "auto_skip"
    else:  # always_ask
        bucket = "missed"

    brief = _brief(task_request)
    rt.last_run_at = now_utc
    rt.next_run_at = _compute_next_run(rt.cron, rt.timezone, now_utc)
    return bucket, brief
