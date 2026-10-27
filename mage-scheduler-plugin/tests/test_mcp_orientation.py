"""Registered MCP orientation tools cross the real API authentication gate."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from tests.conftest import make_action, make_task
from mcp_server import tools


def call_tool(name: str) -> dict | list:
    content, structured = asyncio.run(tools.mcp.call_tool(name, {}))
    assert content
    return json.loads(structured["result"])


def api_get(client, url: str, **kwargs):
    # TestClient has no socket deadline; production requests keep their timeout.
    kwargs.pop("timeout", None)
    return client.get(url, **kwargs)


@pytest.fixture
def authenticated_api(request, monkeypatch, tmp_path):
    monkeypatch.setenv("SCHEDULER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCHEDULER_ALLOW_ANON", "0")
    monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
    # Request the shared API fixture after configuring real authentication.
    client, factory = request.getfixturevalue("api_client")
    import scheduler
    from models import Settings

    monkeypatch.setattr(scheduler, "get_scheduler", lambda: SimpleNamespace(running=True))
    monkeypatch.setattr(tools, "BASE_URL", "http://testserver")
    # Replace only network transport. The MCP handler, auth headers, API routes,
    # middleware, and database queries are real; no backend process is started.
    monkeypatch.setattr(tools.httpx, "get", lambda url, **kwargs: api_get(client, url, **kwargs))
    with factory() as session:
        make_action(session, name="probe_action", allowed_env_json='["MESSAGE"]')
        missed = make_task(session, status="missed")
        session.add(Settings(
            id=1,
            allowed_command_dirs_json='["/probe/bin"]',
            allowed_cwd_dirs_json='["/probe/workspace"]',
        ))
        session.commit()
        missed_id = missed.id
    return client, factory, missed_id


@pytest.mark.parametrize("key_source", ["file", "environment"])
def test_orientation_reads_authenticated_data(authenticated_api, monkeypatch, key_source):
    client, _factory, missed_id = authenticated_api
    if key_source == "environment":
        monkeypatch.setenv("SCHEDULER_API_KEY", "synthetic-scheduler-orientation-key")
    # Prove this test did not accidentally retain the broad suite's anonymous gate.
    assert client.get("/api/actions").status_code == 401
    listed = call_tool("scheduler_list_actions")
    context = call_tool("scheduler_context")
    status = call_tool("scheduler_status")

    assert context["service"]["data_ready"] is True
    assert context["service"]["worker_ready"] is True
    assert {item["name"] for item in context["actions"]} == {
        item["name"] for item in listed
    } == {"probe_action"}
    assert context["actions"][0]["allowed_env"] == ["MESSAGE"]
    assert context["stats"] == {"total": 1, "by_status": {"missed": 1}}
    assert context["recent_tasks"][0]["id"] == missed_id
    assert context["validation"] == {
        "allowed_command_dirs": ["/probe/bin"],
        "allowed_cwd_dirs": ["/probe/workspace"],
    }
    assert "errors" not in context
    assert status["ready"] is True
    assert status["missed_task_count"] == 1
    assert status["missed_task_ids"] == [missed_id]
    assert "errors" not in status


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer synthetic-wrong-key"}])
def test_authentication_failure_is_not_an_empty_scheduler(authenticated_api, monkeypatch, headers):
    monkeypatch.setattr(tools, "_auth_headers", lambda: headers)
    context = call_tool("scheduler_context")
    status = call_tool("scheduler_status")

    assert context["service"]["api_ready"] is True
    assert context["service"]["worker_ready"] is True
    assert context["service"]["data_ready"] is False
    sections = {"actions", "stats", "recent_tasks", "validation"}
    assert set(context["errors"]) == sections
    for section in sections:
        assert context[section] is None
        assert "401" in context["errors"][section]
    assert status["api_alive"] is True
    assert status["worker_alive"] is True
    assert status["ready"] is False
    assert status["missed_task_count"] is None
    assert "401" in status["errors"]["missed_tasks"]
    assert "synthetic-wrong-key" not in json.dumps([context, status])


@pytest.mark.parametrize("failure,detail", [
    ("status", "HTTP 503"),
    ("connection", "request failed"),
    ("json", "invalid JSON"),
    ("shape", "unexpected response format"),
])
def test_context_preserves_good_sections_and_reports_failed_reads(
    authenticated_api, monkeypatch, failure, detail
):
    client, _factory, missed_id = authenticated_api

    def get(url, **kwargs):
        if url.endswith("/api/tasks/stats"):
            if failure == "connection":
                raise httpx.ConnectError("synthetic-private-transport-detail")
            if failure == "status":
                return httpx.Response(503, text="synthetic-private-backend-detail")
            if failure == "json":
                return httpx.Response(200, text="synthetic-private-invalid-json")
            return httpx.Response(200, json=[])
        return api_get(client, url, **kwargs)

    monkeypatch.setattr(tools.httpx, "get", get)
    context = call_tool("scheduler_context")
    assert context["service"]["data_ready"] is False
    assert context["actions"][0]["name"] == "probe_action"
    assert context["recent_tasks"][0]["id"] == missed_id
    assert context["validation"]["allowed_cwd_dirs"] == ["/probe/workspace"]
    assert context["stats"] is None
    assert set(context["errors"]) == {"stats"}
    assert detail in context["errors"]["stats"]
    assert "synthetic-private" not in json.dumps(context)


def test_empty_missed_list_is_a_known_zero(authenticated_api):
    from models import TaskRequest

    _client, factory, _missed_id = authenticated_api
    with factory() as session:
        session.query(TaskRequest).delete()
        session.commit()
    status = call_tool("scheduler_status")
    assert status["ready"] is True
    assert status["missed_task_count"] == 0
    assert "errors" not in status


def test_malformed_missed_list_is_unknown(authenticated_api, monkeypatch):
    client, _factory, _missed_id = authenticated_api

    def get(url, **kwargs):
        if url.endswith("/api/tasks/missed"):
            return httpx.Response(200, json=["not a task record"])
        return api_get(client, url, **kwargs)

    monkeypatch.setattr(tools.httpx, "get", get)
    status = call_tool("scheduler_status")
    assert status["ready"] is False
    assert status["missed_task_count"] is None
    assert "unexpected response format" in status["errors"]["missed_tasks"]
