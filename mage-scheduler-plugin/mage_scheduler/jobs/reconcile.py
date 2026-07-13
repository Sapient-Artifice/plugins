"""
reconcile — durability layer for one-off tasks.

APScheduler's default job store is in-memory, so every one-off ``date`` job is
lost when the backend process stops. The durable record lives in SQLite
(``TaskRequest``), but nothing re-creates the in-memory jobs on restart. This
module closes that gap:

  * **Rehydrate** — on startup, re-register the ``date`` job for any task that
    is still ``scheduled`` with a ``run_at`` in the future, so a plain restart
    no longer silently drops future tasks.

  * **Missed detection** — a task whose ``run_at`` passed while the scheduler
    was offline (overdue by more than the configured grace window) is surfaced
    for a human decision. Depending on the global ``missed_task_policy`` it is
    parked in the ``missed`` status (``always_ask``), re-dispatched
    (``auto_run``), or cancelled (``auto_skip``).

``reconcile_scheduled_tasks`` runs once at startup (full logic) and again on a
60s beat (missed-only), and posts a single summary notification to the
assistant when anything needed attention.
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone

from sqlalchemy import select

from db import SessionLocal, init_db
from models import Settings, TaskDependency, TaskRequest

DEFAULT_GRACE_SECONDS = 300
ASK_ASSISTANT_ENDPOINT = os.getenv(
    "MAGE_ASK_ASSISTANT_URL", "http://127.0.0.1:11115/ask_assistant"
)

_VALID_POLICIES = {"always_ask", "auto_run", "auto_skip"}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def reconcile_scheduled_tasks(startup: bool = False) -> dict:
    """Reconcile durable ``scheduled`` tasks against the live scheduler.

    On ``startup`` the in-memory job store is empty, so this rehydrates
    future-dated jobs and catches up jobs that were only briefly missed, in
    addition to detecting genuinely missed tasks. On the periodic beat it only
    detects genuinely missed tasks (overdue beyond the grace window) — the
    grace window keeps it from racing with a job that is firing right now.

    Returns a summary dict describing what was done (also used for the
    notification and for tests).
    """
    init_db()
    now = datetime.now(timezone.utc)
    summary: dict[str, list] = {
        "rehydrated": [],
        "caught_up": [],
        "missed": [],
        "auto_run": [],
        "auto_skip": [],
    }

    with SessionLocal() as session:
        policy, grace = _load_policy(session)
        tasks = session.execute(
            select(TaskRequest).where(TaskRequest.status == "scheduled")
        ).scalars().all()

        for task in tasks:
            if _has_live_job(task.job_id):
                continue  # a live job will fire it; nothing to reconcile

            run_at = _as_aware(task.run_at)
            if run_at is None:
                continue
            overdue = (now - run_at).total_seconds()

            if run_at > now:
                # Future task with no live job — only possible after a restart.
                if startup:
                    _rehydrate(session, task, run_at)
                    summary["rehydrated"].append(_brief(task))
                continue

            if overdue <= grace:
                # Effectively on-time. On startup, dispatch the brief-miss now;
                # during steady state APScheduler's own misfire grace covers it.
                if startup:
                    _dispatch_now(session, task, now)
                    summary["caught_up"].append(_brief(task))
                continue

            # Genuinely missed while the scheduler was offline.
            if policy == "auto_run":
                _dispatch_now(session, task, now)
                summary["auto_run"].append(_brief(task))
            elif policy == "auto_skip":
                skip_missed_task(
                    session,
                    task,
                    "Skipped by policy: task missed its scheduled run while the "
                    "scheduler was offline.",
                )
                summary["auto_skip"].append(_brief(task))
            elif task.status != "missed":
                task.status = "missed"
                summary["missed"].append(_brief(task))

        _expire_awaiting_ack(session, now, summary)

        session.commit()

    _notify(summary, policy)
    return summary


def _expire_awaiting_ack(session, now: datetime, summary: dict) -> None:
    """Park delivered-but-unacknowledged tasks whose ack window has closed.

    A require_ack task sits in 'awaiting_ack' until a live assistant confirms
    receipt. If the deadline passes with no ack, nobody received it — treat it
    like a missed task so it can be re-delivered later.
    """
    pending = session.execute(
        select(TaskRequest).where(TaskRequest.status == "awaiting_ack")
    ).scalars().all()
    for task in pending:
        deadline = _as_aware(task.ack_deadline)
        if deadline is not None and deadline <= now:
            task.status = "missed"
            task.error = (
                "Delivered but no receipt confirmation within the ack window; "
                "nobody received it. Parked as missed for re-delivery."
            )
            summary["missed"].append(_brief(task))


# ---------------------------------------------------------------------------
# Resolution helpers — shared by reconcile and the resolve_missed API endpoint
# ---------------------------------------------------------------------------

def run_missed_task(session, task: TaskRequest) -> None:
    """Re-dispatch a task immediately, preserving its identity and dependents."""
    _dispatch_now(session, task, datetime.now(timezone.utc))


def skip_missed_task(session, task: TaskRequest, reason: str) -> None:
    """Cancel a task and cascade-fail anything waiting on it."""
    task.status = "cancelled"
    task.error = reason
    session.flush()
    _cascade_fail_dependents(
        session, task.id, f"Dependency task {task.id} failed or was cancelled."
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_policy(session) -> tuple[str, int]:
    settings = session.execute(select(Settings)).scalars().first()
    if settings is None:
        return "always_ask", DEFAULT_GRACE_SECONDS
    policy = settings.missed_task_policy or "always_ask"
    if policy not in _VALID_POLICIES:
        policy = "always_ask"
    grace = settings.missed_grace_seconds
    if grace is None or grace < 0:
        grace = DEFAULT_GRACE_SECONDS
    return policy, grace


def _has_live_job(job_id: str | None) -> bool:
    if not job_id:
        return False
    from scheduler import get_scheduler

    try:
        return get_scheduler().get_job(job_id) is not None
    except Exception:
        return False


def _rehydrate(session, task: TaskRequest, run_at: datetime) -> None:
    from dispatch import schedule_command

    job_id = schedule_command(task.id, task.command, run_at)
    task.job_id = job_id


def _dispatch_now(session, task: TaskRequest, now: datetime) -> None:
    from dispatch import schedule_command

    task.status = "scheduled"
    task.error = None
    session.flush()  # ensure the row is visible before the worker reads it
    job_id = schedule_command(task.id, task.command, now)
    task.job_id = job_id


def _cascade_fail_dependents(session, task_id: int, reason: str) -> None:
    """Mark all waiting tasks that depend on task_id as failed.

    Mirrors api._cascade_fail_dependents; duplicated here to keep this module
    free of a dependency on the FastAPI app module.
    """
    dep_rows = session.execute(
        select(TaskDependency).where(TaskDependency.depends_on_task_id == task_id)
    ).scalars().all()
    candidate_ids = [r.task_id for r in dep_rows]
    if not candidate_ids:
        return
    waiting = session.execute(
        select(TaskRequest).where(
            TaskRequest.id.in_(candidate_ids),
            TaskRequest.status == "waiting",
        )
    ).scalars().all()
    for wt in waiting:
        wt.status = "failed"
        wt.error = reason


def _as_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _brief(task: TaskRequest) -> dict:
    run_at = _as_aware(task.run_at)
    return {
        "id": task.id,
        "description": task.description,
        "run_at": run_at.strftime("%Y-%m-%dT%H:%M:%SZ") if run_at else None,
        "action_name": task.action_name,
    }


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

def _notify(summary: dict, policy: str) -> None:
    """POST a single summary of reconcile actions to the assistant.

    Best-effort — a delivery failure must never affect reconciliation. Only
    sends when something actually needed attention.
    """
    missed = summary["missed"]
    auto_run = summary["auto_run"]
    auto_skip = summary["auto_skip"]
    if not (missed or auto_run or auto_skip):
        return

    message = _build_notification(summary, policy)
    payload = json.dumps({"message": message}).encode()
    req = urllib.request.Request(
        ASK_ASSISTANT_ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # Notification failure must never affect reconciliation


def _build_notification(summary: dict, policy: str) -> str:
    missed = summary["missed"]
    auto_run = summary["auto_run"]
    auto_skip = summary["auto_skip"]

    lines = ["[MAGE SCHEDULER — MISSED TASK NOTIFICATION]"]
    total = len(missed) + len(auto_run) + len(auto_skip)
    lines.append(
        f"{total} scheduled task(s) missed their run time while the scheduler "
        f"was offline. Active policy: {policy}."
    )

    if missed:
        lines.append("")
        lines.append(f"Awaiting your decision ({len(missed)}):")
        lines.extend(_format_task_line(t) for t in missed)
        lines.append(
            "Reply with which to run now vs skip. Assistant: use "
            "scheduler_list_missed and scheduler_resolve_missed(task_id, "
            "action='run'|'skip')."
        )
    if auto_run:
        lines.append("")
        lines.append(f"Auto-run (policy=auto_run) — re-dispatched now ({len(auto_run)}):")
        lines.extend(_format_task_line(t) for t in auto_run)
    if auto_skip:
        lines.append("")
        lines.append(f"Auto-skipped (policy=auto_skip) — cancelled ({len(auto_skip)}):")
        lines.extend(_format_task_line(t) for t in auto_skip)

    return "\n".join(lines)


def _format_task_line(task: dict) -> str:
    action = task.get("action_name") or "custom command"
    return (
        f"  - Task {task['id']} | {task.get('description')} | "
        f"was due {task.get('run_at')} | {action}"
    )
