"""Tests for the missed-task API surface.

  - GET  /api/tasks/missed              — lists only 'missed' tasks
  - POST /api/tasks/{id}/resolve_missed — run / skip, with guards
  - POST /settings                      — persists missed policy + grace
"""
from __future__ import annotations

import pytest

from tests.conftest import make_task


@pytest.fixture
def no_dispatch(monkeypatch):
    """Stop resolve('run') from touching a real APScheduler instance."""
    import dispatch
    calls = []
    monkeypatch.setattr(
        dispatch, "schedule_command",
        lambda task_id, command, run_at: calls.append(task_id) or f"job-{task_id}",
    )
    return calls


class TestListMissed:
    def test_lists_only_missed(self, api_client):
        client, Factory = api_client
        s = Factory()
        make_task(s, status="missed", command="echo a")
        make_task(s, status="scheduled", command="echo b")
        make_task(s, status="missed", command="echo c")
        s.commit()
        s.close()

        resp = client.get("/api/tasks/missed")
        assert resp.status_code == 200
        statuses = {t["status"] for t in resp.json()}
        assert statuses == {"missed"}
        assert len(resp.json()) == 2


class TestResolveMissed:
    def test_run_redispatches(self, api_client, no_dispatch):
        client, Factory = api_client
        s = Factory()
        task = make_task(s, status="missed")
        s.commit()
        tid = task.id
        s.close()

        resp = client.post(f"/api/tasks/{tid}/resolve_missed", json={"action": "run"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "scheduled", "task_id": tid}
        assert no_dispatch == [tid]

        from models import TaskRequest
        s2 = Factory()
        assert s2.get(TaskRequest, tid).status == "scheduled"
        s2.close()

    def test_skip_cancels_and_cascades(self, api_client, no_dispatch):
        from models import TaskDependency, TaskRequest

        client, Factory = api_client
        s = Factory()
        upstream = make_task(s, status="missed")
        dependent = make_task(s, status="waiting")
        s.flush()
        s.add(TaskDependency(task_id=dependent.id, depends_on_task_id=upstream.id))
        s.commit()
        up_id, dep_id = upstream.id, dependent.id
        s.close()

        resp = client.post(f"/api/tasks/{up_id}/resolve_missed", json={"action": "skip"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "cancelled", "task_id": up_id}

        s2 = Factory()
        assert s2.get(TaskRequest, up_id).status == "cancelled"
        assert s2.get(TaskRequest, dep_id).status == "failed"
        s2.close()

    def test_reject_non_missed(self, api_client):
        client, Factory = api_client
        s = Factory()
        task = make_task(s, status="scheduled")
        s.commit()
        tid = task.id
        s.close()

        resp = client.post(f"/api/tasks/{tid}/resolve_missed", json={"action": "run"})
        assert resp.status_code == 400

    def test_not_found(self, api_client):
        client, _ = api_client
        resp = client.post("/api/tasks/99999/resolve_missed", json={"action": "run"})
        assert resp.status_code == 404

    def test_bad_action(self, api_client):
        client, Factory = api_client
        s = Factory()
        task = make_task(s, status="missed")
        s.commit()
        tid = task.id
        s.close()

        resp = client.post(f"/api/tasks/{tid}/resolve_missed", json={"action": "nope"})
        assert resp.status_code == 400


class TestMissedSettings:
    def test_policy_and_grace_saved(self, api_client):
        from models import Settings

        client, Factory = api_client
        resp = client.post(
            "/settings",
            data={"missed_task_policy": "auto_run", "missed_grace_seconds": "120"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        s = Factory()
        settings = s.execute(__import__("sqlalchemy").select(Settings)).scalar_one()
        assert settings.missed_task_policy == "auto_run"
        assert settings.missed_grace_seconds == 120
        s.close()

    def test_invalid_policy_ignored(self, api_client):
        from models import Settings

        client, Factory = api_client
        client.post("/settings", data={"missed_task_policy": "bogus"})

        s = Factory()
        settings = s.execute(__import__("sqlalchemy").select(Settings)).scalar_one_or_none()
        # default remains 'always_ask' (either no row written or unchanged)
        assert settings is None or settings.missed_task_policy == "always_ask"
        s.close()


class TestNeedsDecisionDashboard:
    def test_block_shown_with_run_and_skip_buttons(self, api_client):
        client, Factory = api_client
        s = Factory()
        task = make_task(s, status="missed", command="echo report")
        s.commit()
        tid = task.id
        s.close()

        html = client.get("/").text
        assert "Needs Your Decision" in html
        assert f"/tasks/{tid}/run_missed" in html
        assert f"/tasks/{tid}/skip_missed" in html

    def test_block_hidden_when_no_missed(self, api_client):
        client, Factory = api_client
        s = Factory()
        make_task(s, status="scheduled")
        s.commit()
        s.close()

        assert "Needs Your Decision" not in client.get("/").text

    def test_run_missed_form_redirects_and_dispatches(self, api_client, no_dispatch):
        client, Factory = api_client
        s = Factory()
        task = make_task(s, status="missed")
        s.commit()
        tid = task.id
        s.close()

        resp = client.post(f"/tasks/{tid}/run_missed", follow_redirects=False)
        assert resp.status_code == 303
        assert no_dispatch == [tid]
        s2 = Factory()
        from models import TaskRequest
        assert s2.get(TaskRequest, tid).status == "scheduled"
        s2.close()

    def test_skip_missed_form_cancels(self, api_client):
        client, Factory = api_client
        s = Factory()
        task = make_task(s, status="missed")
        s.commit()
        tid = task.id
        s.close()

        resp = client.post(f"/tasks/{tid}/skip_missed", follow_redirects=False)
        assert resp.status_code == 303
        s2 = Factory()
        from models import TaskRequest
        assert s2.get(TaskRequest, tid).status == "cancelled"
        s2.close()

    def test_run_missed_form_ignores_non_missed(self, api_client, no_dispatch):
        client, Factory = api_client
        s = Factory()
        task = make_task(s, status="scheduled")
        s.commit()
        tid = task.id
        s.close()

        resp = client.post(f"/tasks/{tid}/run_missed", follow_redirects=False)
        assert resp.status_code == 303
        assert no_dispatch == []  # not re-dispatched — it wasn't parked


class TestDashboardButtonFeedback:
    def test_instant_feedback_script_ships(self, api_client):
        client, _ = api_client
        html = client.get("/").text
        # The submit handler that reflects a "Working…" state on POST actions.
        assert 'form[method="post"]' in html
        assert "Working" in html

    def test_decision_buttons_native_post(self, api_client):
        client, Factory = api_client
        s = Factory()
        task = make_task(s, status="missed")
        s.commit()
        tid = task.id
        s.close()
        html = client.get("/").text
        # Native form POST (works in the webview). Must NOT rely on fetch/XHR,
        # and must NOT re-submit via JS (rAF is throttled in a bg iframe → stall).
        assert f"/tasks/{tid}/skip_missed" in html
        assert "fetch(" not in html
        assert "form.submit(" not in html
        assert "requestAnimationFrame" not in html

    def test_dashboard_no_store(self, api_client):
        client, _ = api_client
        resp = client.get("/")
        # Live state — the webview must not serve a stale cached copy.
        assert "no-store" in resp.headers.get("cache-control", "")

    def test_auto_refresh_recurring_10s(self, api_client):
        client, _ = api_client
        html = client.get("/").text
        # Recurring 10s refresh that re-evaluates each tick — not the old
        # one-shot 30s that latched off forever on any field interaction.
        assert "setInterval" in html and "10000" in html
        assert "_touched" not in html
        assert "10&nbsp;seconds" in html

    def test_sandbox_safe_confirm(self, api_client):
        client, Factory = api_client
        s = Factory()
        make_task(s, status="missed")
        s.commit()
        s.close()
        html = client.get("/").text
        # Native confirm() modals are blocked in the in-app cross-origin sandbox,
        # so destructive buttons use a two-click data-confirm handled globally.
        assert 'onclick="return confirm' not in html
        assert "data-confirm=" in html
        assert "button[data-confirm]" in html
