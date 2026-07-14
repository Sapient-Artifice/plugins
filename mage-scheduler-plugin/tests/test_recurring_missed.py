"""Tests for recurring catch-up handling in jobs.recurring_check.

An occurrence that comes due within the grace window spawns and dispatches as
usual; one that is overdue beyond the grace window (i.e. missed while offline)
is routed through the missed-task policy instead of firing late.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from tests.conftest import make_recurring


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _no_notify(monkeypatch):
    import jobs.reconcile as rec
    monkeypatch.setattr(rec, "_notify", lambda summary, policy: None)


def _mock_dispatch(monkeypatch):
    import dispatch
    calls = []
    monkeypatch.setattr(
        dispatch, "schedule_command",
        lambda task_id, command, run_at: calls.append(task_id) or "job-id",
    )
    return calls


def _set_policy(factory, policy):
    from models import Settings
    s = factory()
    s.add(Settings(missed_task_policy=policy, missed_grace_seconds=300))
    s.commit()
    s.close()


class TestRecurringCatchUp:
    def test_ontime_occurrence_dispatched(self, rec_mem_db, monkeypatch):
        from jobs.recurring_check import check_recurring_tasks
        from models import TaskRequest

        calls = _mock_dispatch(monkeypatch)
        s = rec_mem_db()
        rt = make_recurring(s, command="echo ontime")
        rt.next_run_at = _now() - timedelta(seconds=30)  # within grace
        s.commit()
        s.close()

        check_recurring_tasks()

        s2 = rec_mem_db()
        tasks = s2.execute(select(TaskRequest)).scalars().all()
        assert len(tasks) == 1
        assert tasks[0].status == "scheduled"
        assert calls == [tasks[0].id]
        s2.close()

    def test_overdue_occurrence_parked_as_missed(self, rec_mem_db, monkeypatch):
        from jobs.recurring_check import check_recurring_tasks
        from models import TaskRequest

        calls = _mock_dispatch(monkeypatch)
        s = rec_mem_db()
        rt = make_recurring(s, command="echo missed")
        rt.next_run_at = _now() - timedelta(hours=3)  # beyond grace
        s.commit()
        s.close()

        check_recurring_tasks()

        s2 = rec_mem_db()
        tasks = s2.execute(select(TaskRequest)).scalars().all()
        assert len(tasks) == 1
        assert tasks[0].status == "missed"
        assert calls == []  # not dispatched
        # schedule advanced so it does not re-fire every beat
        rt2 = s2.execute(select(__import__("models").RecurringTask)).scalar_one()
        assert rt2.next_run_at > _now()
        s2.close()

    def test_overdue_occurrence_records_intended_run_at(self, rec_mem_db, monkeypatch):
        from jobs.recurring_check import check_recurring_tasks
        from models import TaskRequest

        _mock_dispatch(monkeypatch)
        intended = _now() - timedelta(hours=3)
        s = rec_mem_db()
        rt = make_recurring(s, command="echo missed")
        rt.next_run_at = intended
        s.commit()
        s.close()

        check_recurring_tasks()

        s2 = rec_mem_db()
        task = s2.execute(select(TaskRequest)).scalar_one()
        # run_at reflects the missed occurrence time, not "now"
        assert abs((task.run_at - intended).total_seconds()) < 1
        s2.close()

    def test_overdue_auto_run(self, rec_mem_db, monkeypatch):
        from jobs.recurring_check import check_recurring_tasks
        from models import TaskRequest

        _set_policy(rec_mem_db, "auto_run")
        calls = _mock_dispatch(monkeypatch)
        s = rec_mem_db()
        rt = make_recurring(s, command="echo autorun")
        rt.next_run_at = _now() - timedelta(hours=3)
        s.commit()
        s.close()

        check_recurring_tasks()

        s2 = rec_mem_db()
        task = s2.execute(select(TaskRequest)).scalar_one()
        assert task.status == "scheduled"
        assert calls == [task.id]
        s2.close()

    def test_overdue_auto_skip(self, rec_mem_db, monkeypatch):
        from jobs.recurring_check import check_recurring_tasks
        from models import TaskRequest

        _set_policy(rec_mem_db, "auto_skip")
        calls = _mock_dispatch(monkeypatch)
        s = rec_mem_db()
        rt = make_recurring(s, command="echo autoskip")
        rt.next_run_at = _now() - timedelta(hours=3)
        s.commit()
        s.close()

        check_recurring_tasks()

        s2 = rec_mem_db()
        task = s2.execute(select(TaskRequest)).scalar_one()
        assert task.status == "cancelled"
        assert calls == []
        s2.close()


class TestSupersede:
    def test_new_occurrence_supersedes_prior_unresolved(self, rec_mem_db, monkeypatch):
        """A newer occurrence collapses an earlier still-parked one (no stacking)."""
        from jobs.recurring_check import check_recurring_tasks
        from models import TaskRequest

        _mock_dispatch(monkeypatch)
        s = rec_mem_db()
        rt = make_recurring(s, command="echo weekly")
        # An earlier occurrence still parked awaiting a decision.
        prior = TaskRequest(
            description="last week", command="echo weekly",
            run_at=_now() - timedelta(days=7), status="missed",
            recurring_task_id=rt.id,
        )
        s.add(prior)
        rt.next_run_at = _now() - timedelta(seconds=30)  # new one due, on time
        s.commit()
        prior_id = prior.id
        s.close()

        check_recurring_tasks()

        s2 = rec_mem_db()
        assert s2.get(TaskRequest, prior_id).status == "cancelled"
        assert "Superseded" in (s2.get(TaskRequest, prior_id).error or "")
        # Exactly one live (scheduled) occurrence remains.
        live = s2.execute(
            select(TaskRequest).where(TaskRequest.status == "scheduled")
        ).scalars().all()
        assert len(live) == 1
        s2.close()

    def test_awaiting_ack_prior_is_superseded(self, rec_mem_db, monkeypatch):
        from jobs.recurring_check import check_recurring_tasks
        from models import TaskRequest

        _mock_dispatch(monkeypatch)
        s = rec_mem_db()
        rt = make_recurring(s, command="echo weekly")
        prior = TaskRequest(
            description="in flight", command="echo weekly",
            run_at=_now() - timedelta(days=7), status="awaiting_ack",
            recurring_task_id=rt.id, ack_token="tok",
        )
        s.add(prior)
        rt.next_run_at = _now() - timedelta(seconds=30)
        s.commit()
        prior_id = prior.id
        s.close()

        check_recurring_tasks()

        s2 = rec_mem_db()
        superseded = s2.get(TaskRequest, prior_id)
        assert superseded.status == "cancelled" and superseded.ack_token is None
        s2.close()
