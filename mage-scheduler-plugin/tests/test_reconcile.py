"""Tests for jobs.reconcile — durability and missed-task detection.

Covers rehydration of future one-off jobs on startup, brief-miss catch-up,
genuinely-missed detection under each policy, the live-job guard, the shared
run/skip resolution helpers, and the notification builder.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tests.conftest import make_task


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def rec_db(monkeypatch):
    """In-memory DB wired into jobs.reconcile, with dispatch and notify stubbed.

    Yields (SessionFactory, dispatched_list). ``dispatched_list`` records every
    (task_id, command, run_at) passed to the patched dispatch.schedule_command.
    No live APScheduler jobs exist unless a test overrides _has_live_job.
    """
    from db import Base
    import models  # noqa: F401
    import jobs.reconcile as rec
    import dispatch

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    Factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    monkeypatch.setattr(rec, "SessionLocal", Factory)
    monkeypatch.setattr(rec, "init_db", lambda: None)
    monkeypatch.setattr(rec, "_has_live_job", lambda job_id: False)
    monkeypatch.setattr(rec, "_notify", lambda summary, policy: None)

    dispatched: list = []

    def fake_dispatch(task_id, command, run_at):
        dispatched.append((task_id, command, run_at))
        return f"job-{task_id}"

    monkeypatch.setattr(dispatch, "schedule_command", fake_dispatch)

    yield Factory, dispatched

    Base.metadata.drop_all(bind=engine)


def _set_policy(factory, policy, grace=300):
    from models import Settings

    s = factory()
    s.add(Settings(missed_task_policy=policy, missed_grace_seconds=grace))
    s.commit()
    s.close()


# ---------------------------------------------------------------------------
# Rehydration (future tasks)
# ---------------------------------------------------------------------------

class TestRehydration:
    def test_future_task_rehydrated_on_startup(self, rec_db):
        from jobs.reconcile import reconcile_scheduled_tasks
        from models import TaskRequest

        Factory, dispatched = rec_db
        s = Factory()
        task = make_task(s, status="scheduled")
        task.run_at = _now() + timedelta(hours=2)
        task.job_id = "stale-job"
        s.commit()
        tid = task.id
        s.close()

        summary = reconcile_scheduled_tasks(startup=True)

        assert [d[0] for d in dispatched] == [tid]
        assert [b["id"] for b in summary["rehydrated"]] == [tid]
        s2 = Factory()
        assert s2.get(TaskRequest, tid).job_id == f"job-{tid}"
        assert s2.get(TaskRequest, tid).status == "scheduled"
        s2.close()

    def test_future_task_not_rehydrated_on_beat(self, rec_db):
        from jobs.reconcile import reconcile_scheduled_tasks

        Factory, dispatched = rec_db
        s = Factory()
        task = make_task(s, status="scheduled")
        task.run_at = _now() + timedelta(hours=2)
        s.commit()
        s.close()

        summary = reconcile_scheduled_tasks(startup=False)

        assert dispatched == []
        assert summary["rehydrated"] == []

    def test_live_job_is_left_alone(self, rec_db, monkeypatch):
        import jobs.reconcile as rec
        from jobs.reconcile import reconcile_scheduled_tasks

        Factory, dispatched = rec_db
        monkeypatch.setattr(rec, "_has_live_job", lambda job_id: True)
        s = Factory()
        task = make_task(s, status="scheduled")
        task.run_at = _now() - timedelta(hours=5)  # overdue, but job is "live"
        task.job_id = "live-job"
        s.commit()
        s.close()

        summary = reconcile_scheduled_tasks(startup=True)

        assert dispatched == []
        assert summary["missed"] == [] and summary["caught_up"] == []


# ---------------------------------------------------------------------------
# Brief miss (within grace) — catch-up
# ---------------------------------------------------------------------------

class TestCatchUp:
    def test_within_grace_caught_up_on_startup(self, rec_db):
        from jobs.reconcile import reconcile_scheduled_tasks

        Factory, dispatched = rec_db
        s = Factory()
        task = make_task(s, status="scheduled")
        task.run_at = _now() - timedelta(seconds=60)  # < 300s grace
        s.commit()
        tid = task.id
        s.close()

        summary = reconcile_scheduled_tasks(startup=True)

        assert [d[0] for d in dispatched] == [tid]
        assert [b["id"] for b in summary["caught_up"]] == [tid]

    def test_within_grace_ignored_on_beat(self, rec_db):
        from jobs.reconcile import reconcile_scheduled_tasks

        Factory, dispatched = rec_db
        s = Factory()
        task = make_task(s, status="scheduled")
        task.run_at = _now() - timedelta(seconds=60)
        s.commit()
        s.close()

        summary = reconcile_scheduled_tasks(startup=False)

        assert dispatched == []
        assert summary["caught_up"] == []


# ---------------------------------------------------------------------------
# Genuinely missed (beyond grace)
# ---------------------------------------------------------------------------

class TestMissedPolicies:
    def test_always_ask_parks_as_missed(self, rec_db):
        from jobs.reconcile import reconcile_scheduled_tasks
        from models import TaskRequest

        Factory, dispatched = rec_db
        s = Factory()
        task = make_task(s, status="scheduled")
        task.run_at = _now() - timedelta(hours=1)
        s.commit()
        tid = task.id
        s.close()

        summary = reconcile_scheduled_tasks(startup=True)

        assert dispatched == []
        assert [b["id"] for b in summary["missed"]] == [tid]
        s2 = Factory()
        assert s2.get(TaskRequest, tid).status == "missed"
        s2.close()

    def test_missed_detected_on_beat_too(self, rec_db):
        from jobs.reconcile import reconcile_scheduled_tasks
        from models import TaskRequest

        Factory, dispatched = rec_db
        s = Factory()
        task = make_task(s, status="scheduled")
        task.run_at = _now() - timedelta(hours=1)
        s.commit()
        tid = task.id
        s.close()

        reconcile_scheduled_tasks(startup=False)

        s2 = Factory()
        assert s2.get(TaskRequest, tid).status == "missed"
        s2.close()

    def test_auto_run_redispatches(self, rec_db):
        from jobs.reconcile import reconcile_scheduled_tasks
        from models import TaskRequest

        Factory, dispatched = rec_db
        _set_policy(Factory, "auto_run")
        s = Factory()
        task = make_task(s, status="scheduled")
        task.run_at = _now() - timedelta(hours=1)
        s.commit()
        tid = task.id
        s.close()

        summary = reconcile_scheduled_tasks(startup=True)

        assert [d[0] for d in dispatched] == [tid]
        assert [b["id"] for b in summary["auto_run"]] == [tid]
        s2 = Factory()
        assert s2.get(TaskRequest, tid).status == "scheduled"
        s2.close()

    def test_auto_skip_cancels_and_cascades(self, rec_db):
        from jobs.reconcile import reconcile_scheduled_tasks
        from models import TaskDependency, TaskRequest

        Factory, dispatched = rec_db
        _set_policy(Factory, "auto_skip")
        s = Factory()
        upstream = make_task(s, status="scheduled")
        upstream.run_at = _now() - timedelta(hours=1)
        dependent = make_task(s, status="waiting")
        s.flush()
        s.add(TaskDependency(task_id=dependent.id, depends_on_task_id=upstream.id))
        s.commit()
        up_id, dep_id = upstream.id, dependent.id
        s.close()

        summary = reconcile_scheduled_tasks(startup=True)

        assert dispatched == []
        assert [b["id"] for b in summary["auto_skip"]] == [up_id]
        s2 = Factory()
        assert s2.get(TaskRequest, up_id).status == "cancelled"
        assert s2.get(TaskRequest, dep_id).status == "failed"
        s2.close()


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------

class TestResolutionHelpers:
    def test_run_missed_task_dispatches(self, rec_db):
        from jobs.reconcile import run_missed_task
        from models import TaskRequest

        Factory, dispatched = rec_db
        s = Factory()
        task = make_task(s, status="missed")
        s.commit()

        run_missed_task(s, task)
        s.commit()

        assert [d[0] for d in dispatched] == [task.id]
        assert task.status == "scheduled"
        s.close()

    def test_skip_missed_task_cancels_and_cascades(self, rec_db):
        from jobs.reconcile import skip_missed_task
        from models import TaskDependency, TaskRequest

        Factory, _ = rec_db
        s = Factory()
        upstream = make_task(s, status="missed")
        dependent = make_task(s, status="waiting")
        s.flush()
        s.add(TaskDependency(task_id=dependent.id, depends_on_task_id=upstream.id))
        s.commit()

        skip_missed_task(s, upstream, "skipped")
        s.commit()

        assert upstream.status == "cancelled"
        assert upstream.error == "skipped"
        dep = s.get(TaskRequest, dependent.id)
        assert dep.status == "failed"
        s.close()


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

class TestNotification:
    def test_no_notification_when_nothing_actionable(self, monkeypatch):
        import jobs.reconcile as rec

        posts, os_calls = [], []
        monkeypatch.setattr(rec, "post_to_assistant", lambda m: posts.append(m) or True)
        monkeypatch.setattr(rec, "os_notify", lambda t, b: os_calls.append((t, b)))
        rec._notify(
            {"rehydrated": [{"id": 1}], "caught_up": [], "missed": [],
             "auto_run": [], "auto_skip": []},
            "always_ask",
        )
        assert posts == [] and os_calls == []

    def test_notification_sent_for_missed(self, monkeypatch):
        import jobs.reconcile as rec

        posts, os_calls = [], []
        monkeypatch.setattr(rec, "post_to_assistant", lambda m: posts.append(m) or True)
        monkeypatch.setattr(rec, "os_notify", lambda t, b: os_calls.append((t, b)))
        rec._notify(
            {"rehydrated": [], "caught_up": [],
             "missed": [{"id": 7, "description": "d", "run_at": "t", "action_name": None}],
             "auto_run": [], "auto_skip": []},
            "always_ask",
        )
        # Both channels fire: assistant (primary) AND OS backstop.
        assert len(posts) == 1 and len(os_calls) == 1

    def test_build_notification_mentions_tasks_and_policy(self):
        from jobs.reconcile import _build_notification

        msg = _build_notification(
            {"rehydrated": [], "caught_up": [],
             "missed": [{"id": 7, "description": "nightly", "run_at": "2026-07-06T09:00:00Z", "action_name": "backup"}],
             "auto_run": [], "auto_skip": []},
            "always_ask",
        )
        assert "always_ask" in msg
        assert "Task 7" in msg
        assert "scheduler_resolve_missed" in msg


# ---------------------------------------------------------------------------
# Ongoing policy handling of already-parked ('missed') tasks
# ---------------------------------------------------------------------------

def _park_missed(session, *, interactive=False, last_notified_at=None):
    """Create a task already parked in 'missed' (as after an ack timeout)."""
    from models import TaskRequest

    t = TaskRequest(
        description="parked", command="echo hi",
        run_at=_now() - timedelta(hours=1), status="missed",
        action_name="ask_assistant" if interactive else None,
        require_ack=1 if interactive else 0,
        last_notified_at=last_notified_at,
    )
    session.add(t)
    session.flush()
    return t


class TestMissedPolicyOngoing:
    def test_auto_run_redelivers_interactive_parked_task(self, rec_db):
        from jobs.reconcile import reconcile_scheduled_tasks
        from models import TaskRequest

        Factory, dispatched = rec_db
        _set_policy(Factory, "auto_run")
        s = Factory()
        tid = _park_missed(s, interactive=True).id
        s.commit(); s.close()

        summary = reconcile_scheduled_tasks(startup=False)

        # Re-dispatched (kept knocking) and reported under auto_run.
        assert [d[0] for d in dispatched] == [tid]
        assert [b["id"] for b in summary["auto_run"]] == [tid]
        s2 = Factory()
        t = s2.get(TaskRequest, tid)
        assert t.status == "scheduled" and t.last_notified_at is not None
        s2.close()

    def test_auto_run_respects_redeliver_backoff(self, rec_db):
        from jobs.reconcile import reconcile_scheduled_tasks

        Factory, dispatched = rec_db
        _set_policy(Factory, "auto_run")
        s = Factory()
        # Re-delivered 5 min ago — inside the 15 min backoff → must wait.
        _park_missed(s, interactive=True, last_notified_at=_now() - timedelta(minutes=5))
        s.commit(); s.close()

        summary = reconcile_scheduled_tasks(startup=False)

        assert dispatched == []
        assert summary["auto_run"] == []

    def test_always_ask_renudges_when_stale(self, rec_db):
        from jobs.reconcile import reconcile_scheduled_tasks

        Factory, _ = rec_db
        _set_policy(Factory, "always_ask")
        s = Factory()
        tid = _park_missed(s, last_notified_at=_now() - timedelta(minutes=40)).id
        s.commit(); s.close()

        summary = reconcile_scheduled_tasks(startup=False)

        # 40 min > 30 min throttle → re-nudged.
        assert [b["id"] for b in summary["missed"]] == [tid]

    def test_always_ask_throttles_recent_nudge(self, rec_db):
        from jobs.reconcile import reconcile_scheduled_tasks

        Factory, _ = rec_db
        _set_policy(Factory, "always_ask")
        s = Factory()
        _park_missed(s, last_notified_at=_now() - timedelta(minutes=5))
        s.commit(); s.close()

        summary = reconcile_scheduled_tasks(startup=False)

        # 5 min < 30 min throttle → stays quiet, still parked.
        assert summary["missed"] == []

    def test_auto_skip_cancels_straggler_and_reports_cascade(self, rec_db):
        from jobs.reconcile import reconcile_scheduled_tasks
        from models import TaskDependency, TaskRequest

        Factory, _ = rec_db
        _set_policy(Factory, "auto_skip")
        s = Factory()
        parked = _park_missed(s)
        dependent = make_task(s, status="waiting")
        s.add(TaskDependency(task_id=dependent.id, depends_on_task_id=parked.id))
        s.commit()
        pid, did = parked.id, dependent.id
        s.close()

        summary = reconcile_scheduled_tasks(startup=False)

        assert [b["id"] for b in summary["auto_skip"]] == [pid]
        assert summary["auto_skip"][0]["cascaded"] == 1
        s2 = Factory()
        assert s2.get(TaskRequest, pid).status == "cancelled"
        assert s2.get(TaskRequest, did).status == "failed"
        s2.close()
