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

from datetime import datetime, timezone

from sqlalchemy import select

from db import SessionLocal, init_db
from models import Settings, TaskDependency, TaskRequest
from notify import os_notify, post_to_assistant

DEFAULT_GRACE_SECONDS = 300

# A parked (always_ask) task re-nudges the user this often until resolved, so a
# single alert fired at 5am into a sleeping house is no longer the only chance.
RENUDGE_INTERVAL_SECONDS = 1800  # 30 min
# An auto_run interactive task re-delivers this often until an ack lands.
REDELIVER_BACKOFF_SECONDS = 900  # 15 min

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
                cascaded = skip_missed_task(
                    session,
                    task,
                    "Skipped by policy: task missed its scheduled run while the "
                    "scheduler was offline.",
                )
                summary["auto_skip"].append(_brief(task, cascaded=cascaded))
            elif task.status != "missed":
                task.status = "missed"
                task.last_notified_at = None  # → the policy pass acts immediately

        _expire_awaiting_ack(session, now)
        # Make freshly-parked rows visible to the policy pass's SELECT even when
        # the session isn't autoflushing.
        session.flush()
        _apply_missed_policy(session, now, summary, policy)

        session.commit()

    _notify(summary, policy)
    return summary


def _expire_awaiting_ack(session, now: datetime) -> None:
    """Park delivered-but-unacknowledged tasks whose ack window has closed.

    A require_ack task sits in 'awaiting_ack' until a live assistant confirms
    receipt. If the deadline passes with no ack, nobody received it — park it as
    'missed'. What happens next (re-nudge / re-deliver / skip) is decided by
    ``_apply_missed_policy``, which sees it on this same beat. Resetting
    ``last_notified_at`` makes that pass act immediately.
    """
    pending = session.execute(
        select(TaskRequest).where(TaskRequest.status == "awaiting_ack")
    ).scalars().all()
    for task in pending:
        deadline = _as_aware(task.ack_deadline)
        if deadline is not None and deadline <= now:
            task.status = "missed"
            task.ack_token = None
            task.last_notified_at = None
            task.error = (
                "Delivered but no receipt confirmation within the ack window; "
                "nobody received it. Parked as missed for re-delivery."
            )


def _apply_missed_policy(session, now: datetime, summary: dict, policy: str) -> None:
    """Handle every currently-parked ('missed') task per the active policy.

    This is the single authority for what happens to a parked task over time:

      * ``always_ask`` — leave it parked and **re-nudge** the user on a throttle
        (``RENUDGE_INTERVAL_SECONDS``) until they run or skip it.
      * ``auto_run``   — **re-deliver** interactive tasks on a backoff
        (``REDELIVER_BACKOFF_SECONDS``) until an ack lands; re-dispatch a
        deterministic one straight away.
      * ``auto_skip``  — cancel any straggler (e.g. an on-time ack that timed
        out under this policy) and cascade-fail its dependents.

    A null ``last_notified_at`` means "act now" (first sighting), so a freshly
    parked task is surfaced on the very next beat rather than after a full
    interval.
    """
    parked = session.execute(
        select(TaskRequest).where(TaskRequest.status == "missed")
    ).scalars().all()

    for task in parked:
        if policy == "auto_skip":
            cascaded = skip_missed_task(
                session,
                task,
                "Skipped by policy: delivered/scheduled run could not reach you.",
            )
            summary["auto_skip"].append(_brief(task, cascaded=cascaded))
            continue

        if policy == "auto_run" and _is_interactive(task):
            if _throttle_elapsed(task.last_notified_at, now, REDELIVER_BACKOFF_SECONDS):
                _dispatch_now(session, task, now)
                task.last_notified_at = now.replace(tzinfo=None)
                summary["auto_run"].append(_brief(task))
            continue

        if policy == "auto_run":  # deterministic missed task — just run it
            _dispatch_now(session, task, now)
            task.last_notified_at = now.replace(tzinfo=None)
            summary["auto_run"].append(_brief(task))
            continue

        # always_ask (or any unknown policy) — keep parked, re-nudge on throttle.
        if _throttle_elapsed(task.last_notified_at, now, RENUDGE_INTERVAL_SECONDS):
            task.last_notified_at = now.replace(tzinfo=None)
            summary["missed"].append(_brief(task))


def _is_interactive(task: TaskRequest) -> bool:
    """A task that needs a live human/assistant to receive it (ask_assistant)."""
    return bool(task.require_ack) or task.action_name == "ask_assistant"


def _throttle_elapsed(last, now: datetime, interval: int) -> bool:
    """True if ``interval`` seconds have passed since ``last`` (None → True)."""
    if last is None:
        return True
    return (now - _as_aware(last)).total_seconds() >= interval


# ---------------------------------------------------------------------------
# Resolution helpers — shared by reconcile and the resolve_missed API endpoint
# ---------------------------------------------------------------------------

def run_missed_task(session, task: TaskRequest) -> None:
    """Re-dispatch a task immediately, preserving its identity and dependents."""
    _dispatch_now(session, task, datetime.now(timezone.utc))


def skip_missed_task(session, task: TaskRequest, reason: str) -> int:
    """Cancel a task and cascade-fail anything waiting on it.

    Returns the number of dependent tasks that were cascade-cancelled, so the
    notification can tell the user a skip also killed a chain.
    """
    task.status = "cancelled"
    task.error = reason
    task.ack_token = None
    session.flush()
    return _cascade_fail_dependents(
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


def _cascade_fail_dependents(session, task_id: int, reason: str) -> int:
    """Mark all waiting tasks that depend on task_id as failed. Return the count.

    Mirrors api._cascade_fail_dependents; duplicated here to keep this module
    free of a dependency on the FastAPI app module.
    """
    dep_rows = session.execute(
        select(TaskDependency).where(TaskDependency.depends_on_task_id == task_id)
    ).scalars().all()
    candidate_ids = [r.task_id for r in dep_rows]
    if not candidate_ids:
        return 0
    waiting = session.execute(
        select(TaskRequest).where(
            TaskRequest.id.in_(candidate_ids),
            TaskRequest.status == "waiting",
        )
    ).scalars().all()
    for wt in waiting:
        wt.status = "failed"
        wt.error = reason
    return len(waiting)


def _as_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _brief(task: TaskRequest, *, cascaded: int = 0) -> dict:
    run_at = _as_aware(task.run_at)
    return {
        "id": task.id,
        "description": task.description,
        "run_at": run_at.strftime("%Y-%m-%dT%H:%M:%SZ") if run_at else None,
        "action_name": task.action_name,
        "cascaded": cascaded,
    }


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

def _notify(summary: dict, policy: str) -> None:
    """Announce reconcile actions over both channels.

    Best-effort — a delivery failure must never affect reconciliation. Only
    fires when something actually needed attention. Sends to the assistant
    (primary) AND an OS-level notification (backstop that survives Mage being
    down), so a parked task is loud even at 5am into a sleeping house.
    """
    missed = summary["missed"]
    auto_run = summary["auto_run"]
    auto_skip = summary["auto_skip"]
    if not (missed or auto_run or auto_skip):
        return

    message = _build_notification(summary, policy)
    post_to_assistant(message)
    os_notify("Mage Scheduler", _os_summary_line(summary))


def _os_summary_line(summary: dict) -> str:
    """A short one-line summary for the OS notification (no room for detail)."""
    missed = len(summary["missed"])
    auto_run = len(summary["auto_run"])
    auto_skip = len(summary["auto_skip"])
    parts = []
    if missed:
        parts.append(f"{missed} awaiting your decision")
    if auto_run:
        parts.append(f"{auto_run} auto-running")
    if auto_skip:
        parts.append(f"{auto_skip} auto-skipped")
    return "Missed task(s): " + ", ".join(parts) + ". Open the dashboard to act."


def _build_notification(summary: dict, policy: str) -> str:
    missed = summary["missed"]
    auto_run = summary["auto_run"]
    auto_skip = summary["auto_skip"]

    lines = ["[MAGE SCHEDULER — MISSED TASK NOTIFICATION]"]
    total = len(missed) + len(auto_run) + len(auto_skip)
    lines.append(
        f"{total} scheduled task(s) couldn't complete when due — missed while "
        f"the scheduler was offline, or delivered but not received. "
        f"Active policy: {policy}."
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
        lines.append(
            f"Auto-run (policy=auto_run) — running a previously missed job "
            f"({len(auto_run)}); interactive ones keep re-delivering until you "
            f"receive them:"
        )
        lines.extend(_format_task_line(t) for t in auto_run)
    if auto_skip:
        lines.append("")
        lines.append(
            f"Auto-skipped (policy=auto_skip) — cancelled a missed job "
            f"({len(auto_skip)}):"
        )
        lines.extend(_format_task_line(t) for t in auto_skip)

    return "\n".join(lines)


def _format_task_line(task: dict) -> str:
    action = task.get("action_name") or "custom command"
    line = (
        f"  - Task {task['id']} | {task.get('description')} | "
        f"was due {task.get('run_at')} | {action}"
    )
    cascaded = task.get("cascaded") or 0
    if cascaded:
        line += f" | also cancelled {cascaded} dependent task(s)"
    return line
