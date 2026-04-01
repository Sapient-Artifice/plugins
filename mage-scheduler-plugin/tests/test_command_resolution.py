"""Tests for command resolution in the intent API.

_validate_command now accepts bare names (e.g. "python3"), resolves them
to absolute paths via shutil.which(), and returns the resolved command.
Absolute paths are passed through unchanged.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# api.py adds itself to sys.path via pytest pythonpath config
from api import _validate_command
from fastapi import HTTPException


@pytest.fixture
def resolve_client(monkeypatch):
    """TestClient with a fully isolated in-memory DB that does NOT mock _validate_command.

    This allows tests to exercise the real command-resolution logic through
    the full intent endpoint.
    """
    from fastapi.testclient import TestClient
    from db import Base
    import models  # noqa: F401
    import api
    import task_manager as tm
    import scheduler
    import dispatch

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    monkeypatch.setattr(api, "SessionLocal", Factory)
    monkeypatch.setattr(tm, "SessionLocal", Factory)
    monkeypatch.setattr(tm, "init_db", lambda: None)

    # Suppress APScheduler lifecycle in the FastAPI lifespan handler
    monkeypatch.setattr(scheduler, "start_scheduler", lambda: None)
    monkeypatch.setattr(scheduler, "stop_scheduler", lambda: None)

    # Mock cwd validation — filesystem not relevant here
    monkeypatch.setattr(api, "_validate_cwd", lambda *a, **kw: None)

    # Mock dispatch — no real APScheduler jobs during tests
    monkeypatch.setattr(tm, "schedule_command", lambda *a, **kw: "fake-job-id")
    monkeypatch.setattr(api, "cancel_command", lambda *a, **kw: None)

    with TestClient(api.app, raise_server_exceptions=True) as client:
        yield client

    Base.metadata.drop_all(bind=engine)


class TestValidateCommandResolution:
    def test_bare_name_resolved_to_absolute(self):
        with patch("api.shutil.which", return_value="/usr/bin/python3"), \
             patch("api.os.path.exists", return_value=True), \
             patch("api.os.access", return_value=True):
            result = _validate_command("python3 script.py")
        assert result == "/usr/bin/python3 script.py"

    def test_bare_name_only_resolved(self):
        with patch("api.shutil.which", return_value="/usr/bin/git"), \
             patch("api.os.path.exists", return_value=True), \
             patch("api.os.access", return_value=True):
            result = _validate_command("git")
        assert result == "/usr/bin/git"

    def test_absolute_path_passed_through_unchanged(self):
        with patch("api.os.path.exists", return_value=True), \
             patch("api.os.access", return_value=True):
            result = _validate_command("/usr/bin/python3 script.py")
        assert result == "/usr/bin/python3 script.py"

    def test_windows_absolute_path_passed_through_unchanged(self):
        # os.path.isabs handles C:\ paths correctly on all platforms
        with patch("api.os.path.isabs", return_value=True), \
             patch("api.os.path.exists", return_value=True), \
             patch("api.os.access", return_value=True):
            result = _validate_command(r"C:\Python311\python.exe script.py")
        assert r"C:\Python311\python.exe" in result

    def test_bare_name_not_on_path_raises_command_not_found(self):
        with patch("api.shutil.which", return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                _validate_command("ffmpeg -i in.mp4 out.avi")
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "command_not_found"

    def test_empty_command_raises_command_required(self):
        with pytest.raises(HTTPException) as exc_info:
            _validate_command("")
        assert exc_info.value.detail == "command_required"


class TestIntentEndpointResolution:
    """Integration tests through the full intent endpoint."""

    def test_bare_command_resolved_and_scheduled(self, resolve_client):
        with patch("api.shutil.which", return_value="/usr/bin/python3"), \
             patch("api.os.path.exists", return_value=True), \
             patch("api.os.access", return_value=True):
            resp = resolve_client.post("/api/tasks/intent", json={
                "intent_version": "v1",
                "task": {
                    "description": "test bare name resolution",
                    "command": "python3 /tmp/script.py",
                    "run_in": "1h",
                },
            })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "scheduled"
        assert data["command"] == "/usr/bin/python3 /tmp/script.py"

    def test_missing_command_returns_blocked(self, resolve_client):
        with patch("api.shutil.which", return_value=None):
            resp = resolve_client.post("/api/tasks/intent", json={
                "intent_version": "v1",
                "task": {
                    "description": "test missing command",
                    "command": "ffmpeg -i in.mp4 out.avi",
                    "run_in": "1h",
                },
            })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "blocked"
        assert any(e["code"] == "command_not_found" for e in data["errors"])
