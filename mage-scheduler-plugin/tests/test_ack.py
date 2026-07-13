"""Tests for the receipt-acknowledgment gate (require_ack / awaiting_ack).

Covers run_command outcome mapping, the ack endpoint, the timeout sweep in
reconcile, and the seeded default on the ask_assistant action.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tests.conftest import make_task, make_action


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _patch_subprocess(monkeypatch, returncode, stdout="", stderr="", capture=None):
    import jobs.run_command as rc

    def fake_run(command, **kwargs):
        if capture is not None:
            capture.update(kwargs)
        m = MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        m.stderr = stderr
        return m

    monkeypatch.setattr(rc, "subprocess", MagicMock(run=fake_run))


# ---------------------------------------------------------------------------
# run_command outcome mapping
# ---------------------------------------------------------------------------

class TestRunCommandAck:
    def test_delivered_goes_awaiting_ack(self, nt_mem_db, monkeypatch):
        from jobs.run_command import run_command
        from models import TaskRequest

        _patch_subprocess(monkeypatch, 0, stdout='{"status":"success"}')
        s = nt_mem_db()
        task = make_task(s, status="scheduled", command="echo hi")
        task.require_ack = 1
        s.commit()
        tid = task.id
        s.close()

        result = run_command(tid, "echo hi")

        assert result.get("awaiting_ack") is True
        s2 = nt_mem_db()
        t = s2.get(TaskRequest, tid)
        assert t.status == "awaiting_ack"
        assert t.ack_token and t.ack_deadline is not None
        s2.close()

    def test_undeliverable_goes_missed(self, nt_mem_db, monkeypatch):
        from jobs.run_command import run_command, EXIT_UNDELIVERABLE
        from models import TaskRequest

        _patch_subprocess(monkeypatch, EXIT_UNDELIVERABLE, stderr="HTTP 503")
        s = nt_mem_db()
        task = make_task(s, status="scheduled")
        task.require_ack = 1
        s.commit()
        tid = task.id
        s.close()

        run_command(tid, "echo hi")

        s2 = nt_mem_db()
        t = s2.get(TaskRequest, tid)
        assert t.status == "missed"
        assert t.ack_token is None
        s2.close()

    def test_config_error_fails(self, nt_mem_db, monkeypatch):
        from jobs.run_command import run_command
        from models import TaskRequest

        _patch_subprocess(monkeypatch, 1, stderr="MESSAGE required")
        s = nt_mem_db()
        task = make_task(s, status="scheduled")
        task.require_ack = 1
        s.commit()
        tid = task.id
        s.close()

        run_command(tid, "echo hi")

        s2 = nt_mem_db()
        assert s2.get(TaskRequest, tid).status == "failed"
        s2.close()

    def test_require_ack_resolved_from_action(self, nt_mem_db, monkeypatch):
        """A task whose own flag is 0 still gates if its action requires ack."""
        from jobs.run_command import run_command
        from models import TaskRequest

        _patch_subprocess(monkeypatch, 0)
        s = nt_mem_db()
        action = make_action(s, name="ask_assistant", command="echo hi")
        action.require_ack = 1
        task = make_task(s, status="scheduled")
        task.action_name = "ask_assistant"
        task.require_ack = 0
        s.commit()
        tid = task.id
        s.close()

        run_command(tid, "echo hi")

        s2 = nt_mem_db()
        assert s2.get(TaskRequest, tid).status == "awaiting_ack"
        s2.close()

    def test_non_ack_task_unaffected(self, nt_mem_db, monkeypatch):
        from jobs.run_command import run_command
        from models import TaskRequest

        _patch_subprocess(monkeypatch, 0, stdout="ok")
        s = nt_mem_db()
        task = make_task(s, status="scheduled")
        s.commit()
        tid = task.id
        s.close()

        run_command(tid, "echo hi")

        s2 = nt_mem_db()
        assert s2.get(TaskRequest, tid).status == "success"
        s2.close()

    def test_ack_env_injected(self, nt_mem_db, monkeypatch):
        from jobs.run_command import run_command

        captured: dict = {}
        _patch_subprocess(monkeypatch, 0, capture=captured)
        s = nt_mem_db()
        task = make_task(s, status="scheduled")
        task.require_ack = 1
        s.commit()
        tid = task.id
        s.close()

        run_command(tid, "echo hi")

        env = captured["env"]
        assert env["SCHEDULER_ACK_REQUIRED"] == "1"
        assert env["SCHEDULER_ACK_TOKEN"]


# ---------------------------------------------------------------------------
# Ack endpoint
# ---------------------------------------------------------------------------

class TestAckEndpoint:
    def _awaiting(self, Factory, token="tok123", status="awaiting_ack"):
        s = Factory()
        t = make_task(s, status=status)
        t.ack_token = token
        s.commit()
        tid = t.id
        s.close()
        return tid

    def test_valid_ack_marks_success(self, api_client):
        from models import TaskRequest
        client, Factory = api_client
        tid = self._awaiting(Factory)

        resp = client.post(f"/api/tasks/{tid}/ack", json={"token": "tok123"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "success", "task_id": tid}

        s = Factory()
        t = s.get(TaskRequest, tid)
        assert t.status == "success"
        assert t.ack_token is None and t.ack_deadline is None
        s.close()

    def test_bad_token_rejected(self, api_client):
        client, Factory = api_client
        tid = self._awaiting(Factory)
        resp = client.post(f"/api/tasks/{tid}/ack", json={"token": "wrong"})
        assert resp.status_code == 403

    def test_wrong_status_rejected(self, api_client):
        client, Factory = api_client
        s = Factory()
        t = make_task(s, status="scheduled")
        t.ack_token = "tok123"
        s.commit()
        tid = t.id
        s.close()
        resp = client.post(f"/api/tasks/{tid}/ack", json={"token": "tok123"})
        assert resp.status_code == 400

    def test_not_found(self, api_client):
        client, _ = api_client
        resp = client.post("/api/tasks/99999/ack", json={"token": "x"})
        assert resp.status_code == 404

    def test_late_ack_on_missed_succeeds(self, api_client):
        """A valid token still acks a task that already timed out to missed."""
        from models import TaskRequest
        client, Factory = api_client
        tid = self._awaiting(Factory, status="missed")

        resp = client.post(f"/api/tasks/{tid}/ack", json={"token": "tok123"})
        assert resp.status_code == 200
        s = Factory()
        assert s.get(TaskRequest, tid).status == "success"
        s.close()


# ---------------------------------------------------------------------------
# Reconcile: ack-timeout sweep
# ---------------------------------------------------------------------------

class TestExpireAwaitingAck:
    def test_past_deadline_parked_missed(self, db_session):
        from jobs.reconcile import _expire_awaiting_ack
        from models import TaskRequest

        now = datetime.now(timezone.utc)
        t = TaskRequest(
            description="x", command="c", run_at=_now_naive(),
            status="awaiting_ack",
            ack_deadline=(now - timedelta(minutes=1)).replace(tzinfo=None),
        )
        db_session.add(t)
        db_session.commit()

        summary = {"missed": []}
        _expire_awaiting_ack(db_session, now, summary)

        assert t.status == "missed"
        assert len(summary["missed"]) == 1

    def test_before_deadline_unchanged(self, db_session):
        from jobs.reconcile import _expire_awaiting_ack
        from models import TaskRequest

        now = datetime.now(timezone.utc)
        t = TaskRequest(
            description="x", command="c", run_at=_now_naive(),
            status="awaiting_ack",
            ack_deadline=(now + timedelta(minutes=10)).replace(tzinfo=None),
        )
        db_session.add(t)
        db_session.commit()

        summary = {"missed": []}
        _expire_awaiting_ack(db_session, now, summary)

        assert t.status == "awaiting_ack"
        assert summary["missed"] == []


# ---------------------------------------------------------------------------
# Seed default
# ---------------------------------------------------------------------------

def test_ask_assistant_action_seeded_with_require_ack(monkeypatch):
    import db as db_mod
    from models import Action

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db_mod.Base.metadata.create_all(bind=engine)
    Factory = sessionmaker(bind=engine)
    monkeypatch.setattr(db_mod, "SessionLocal", Factory)

    db_mod._seed_default_actions()

    s = Factory()
    action = s.execute(select(Action).where(Action.name == "ask_assistant")).scalar_one()
    assert action.require_ack == 1
    s.close()
