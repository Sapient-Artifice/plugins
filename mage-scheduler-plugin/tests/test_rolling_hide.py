"""Rolling hide: a recurring task can keep only its newest N successful runs
visible, so a frequent job (a 5-minute tick) does not bury everything else.

The rule, stated as what is RIGHT:
  - a run is hidden only if it SUCCEEDED, belongs to a recurring task with
    keep_visible = N, and is not among that task's newest N successful runs;
  - anything unsuccessful (failed, blocked, cancelled, missed, awaiting ack,
    still scheduled or running) is always visible;
  - nothing is deleted: include_hidden=true returns every run;
  - keep_visible unset (the default) changes nothing.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine

T0 = datetime(2026, 10, 29, 8, 0, 0)


def _rec(session, name, keep_visible=None):
    from models import RecurringTask
    rt = RecurringTask(name=name, cron="*/5 * * * *", command="echo ok", timezone="UTC",
                       enabled=1, keep_visible=keep_visible)
    session.add(rt)
    session.flush()
    return rt


def _run(session, desc, status, minute, recurring=None):
    from models import TaskRequest
    t = TaskRequest(description=desc, command="echo ok", run_at=T0 + timedelta(minutes=minute),
                    created_at=T0 + timedelta(minutes=minute), status=status,
                    recurring_task_id=recurring.id if recurring else None)
    session.add(t)
    session.flush()
    return t


def _seed(factory):
    """A 5-minute tick keeping 3, with old failures mixed in; a recurring task
    with no setting; and a one-off. Returns {description: id}."""
    s = factory()
    tick, plain = _rec(s, "tick", keep_visible=3), _rec(s, "plain")
    rows = [("tick-ok-0", "success", 0, tick), ("tick-fail-1", "failed", 5, tick),
            ("tick-ok-2", "success", 10, tick), ("tick-blocked-3", "blocked", 15, tick),
            ("tick-ok-4", "success", 20, tick), ("tick-cancelled-5", "cancelled", 25, tick),
            ("tick-ok-6", "success", 30, tick), ("tick-ok-7", "success", 35, tick),
            ("tick-ok-8", "success", 40, tick), ("tick-running-9", "running", 45, tick),
            ("plain-ok-0", "success", 1, plain), ("plain-ok-1", "success", 2, plain),
            ("plain-ok-2", "success", 3, plain), ("plain-ok-3", "success", 4, plain),
            ("oneoff-ok", "success", 6, None)]
    ids = {d: _run(s, d, st, m, r).id for d, st, m, r in rows}
    s.commit()
    s.close()
    return ids


HIDDEN = {"tick-ok-0", "tick-ok-2", "tick-ok-4"}       # successes older than the newest 3


def _descs(client, query=""):
    r = client.get("/api/tasks" + query)
    assert r.status_code == 200, r.text
    return [t["description"] for t in r.json()]


def test_only_the_newest_successes_of_a_kept_recurring_task_stay_listed(api_client):
    client, factory = api_client
    ids = _seed(factory)
    listed = _descs(client)
    assert set(listed) == set(ids) - HIDDEN
    # every unsuccessful run survives, however old
    for d in ("tick-fail-1", "tick-blocked-3", "tick-cancelled-5", "tick-running-9"):
        assert d in listed
    # untouched: a recurring task with no setting, and a one-off
    assert {"plain-ok-0", "plain-ok-1", "plain-ok-2", "plain-ok-3", "oneoff-ok"} <= set(listed)
    assert listed[:3] == ["tick-running-9", "tick-ok-8", "tick-ok-7"]          # still newest first


def test_hidden_runs_are_kept_and_can_be_asked_for(api_client):
    client, factory = api_client
    ids = _seed(factory)
    assert set(_descs(client, "?include_hidden=true")) == set(ids)
    for d in HIDDEN:                                     # still addressable directly
        assert client.get(f"/api/tasks/{ids[d]}").status_code == 200


def test_a_status_filter_still_hides(api_client):
    client, factory = api_client
    _seed(factory)
    ok = _descs(client, "?status=success")
    assert [d for d in ok if d.startswith("tick-")] == ["tick-ok-8", "tick-ok-7", "tick-ok-6"]


def test_the_dashboard_leaves_hidden_runs_out_and_says_so(api_client):
    client, factory = api_client
    ids = _seed(factory)
    page = client.get("/").text
    for d in HIDDEN:
        assert f'href="/tasks/{ids[d]}"' not in page
    for d in ("tick-ok-8", "tick-fail-1", "plain-ok-0"):
        assert f'href="/tasks/{ids[d]}"' in page
    assert "3 older successful runs hidden" in page
    shown = client.get("/?show_hidden=1").text
    for d in HIDDEN:
        assert f'href="/tasks/{ids[d]}"' in shown


def test_keep_visible_is_set_read_and_validated_through_the_api(api_client):
    client, _ = api_client
    body = {"name": "tick", "cron": "*/5 * * * *", "command": "echo ok", "keep_visible": 3}
    r = client.post("/api/recurring", json=body)
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    assert r.json()["keep_visible"] == 3
    for bad in (0, -1):
        assert client.post("/api/recurring", json={**body, "name": f"b{bad}", "keep_visible": bad}).status_code == 422
    # a PUT that does not mention keep_visible leaves it alone (callers that
    # predate the field must not silently switch it off)...
    upd = {"name": "tick", "cron": "*/10 * * * *", "command": "echo ok"}
    assert client.put(f"/api/recurring/{rid}", json=upd).json()["keep_visible"] == 3
    # ...and one that does, sets it; null switches it off
    assert client.put(f"/api/recurring/{rid}", json={**upd, "keep_visible": 5}).json()["keep_visible"] == 5
    assert client.put(f"/api/recurring/{rid}", json={**upd, "keep_visible": None}).json()["keep_visible"] is None


def test_the_schema_migration_adds_keep_visible_to_an_existing_install(monkeypatch):
    import db
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as c:
        c.exec_driver_sql("CREATE TABLE task_requests (id INTEGER PRIMARY KEY, description TEXT)")
        c.exec_driver_sql("CREATE TABLE recurring_tasks (id INTEGER PRIMARY KEY, name TEXT, cron TEXT)")
        c.exec_driver_sql("INSERT INTO recurring_tasks (id, name, cron) VALUES (1, 'old', '* * * * *')")
    monkeypatch.setattr(db, "engine", engine)
    db._migrate_schema()
    db._migrate_schema()                                 # idempotent
    with engine.begin() as c:
        cols = {r[1] for r in c.exec_driver_sql("PRAGMA table_info(recurring_tasks)").fetchall()}
        assert "keep_visible" in cols
        assert c.exec_driver_sql("SELECT keep_visible FROM recurring_tasks WHERE id = 1").scalar() is None


def test_editing_a_recurring_task_in_the_dashboard_keeps_its_setting(api_client):
    client, factory = api_client
    s = factory(); rid = _rec(s, "tick", keep_visible=3).id; s.commit(); s.close()
    r = client.post(f"/recurring/{rid}/edit", data={"name": "tick", "cron": "*/10 * * * *",
                                                     "command": "echo ok", "enabled": "on"},
                    follow_redirects=False)
    assert r.status_code in (200, 303), r.text
    assert client.get("/api/recurring").json()[0]["keep_visible"] == 3


def test_each_recurring_task_keeps_its_own_newest_runs(api_client):
    client, factory = api_client
    s = factory()
    a, b = _rec(s, "a", keep_visible=2), _rec(s, "b", keep_visible=2)
    for m in range(4):
        _run(s, f"a-{m}", "success", m, a)                 # a's runs are all OLDER than b's
        _run(s, f"b-{m}", "success", 10 + m, b)
    s.commit(); s.close()
    assert sorted(_descs(client)) == ["a-2", "a-3", "b-2", "b-3"]
