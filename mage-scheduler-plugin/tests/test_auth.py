"""Control-plane auth gate — exercised with anon DISABLED (the production posture).

The rest of the suite runs with SCHEDULER_ALLOW_ANON=1 (conftest); these tests clear it so the
real fail-closed behavior is tested: unauthenticated requests are rejected, the key file / bearer
/ cookie all work, and health/login/static stay open.
"""
from __future__ import annotations

import importlib
import os
import sys

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    # isolated key dir, anon OFF, module reloaded so the middleware binds to this config
    monkeypatch.setenv("SCHEDULER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SCHEDULER_ALLOW_ANON", raising=False)
    monkeypatch.delenv("SCHEDULER_API_KEY", raising=False)
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mage_scheduler"))
    import auth
    importlib.reload(auth)
    key = auth.get_or_create_key()
    import api
    importlib.reload(api)
    # These tests assert the GATE's decision (admit vs reject), not handler success — a request
    # the gate admits may still 500 on the reloaded module's DB binding, which is out of scope.
    return TestClient(api.app, raise_server_exceptions=False), key


def _admitted(resp):
    """The gate let the request through (didn't 401 or bounce to /login)."""
    if resp.status_code == 401:
        return False
    if resp.status_code == 303 and resp.headers.get("location") == "/login":
        return False
    return True


def test_health_and_login_are_exempt(client):
    c, _ = client
    assert c.get("/health").status_code == 200
    assert c.get("/login").status_code == 200           # form, no key
    # a static path is exempt by prefix even if the file 404s — not a 401
    assert c.get("/static/nope.css").status_code != 401


def test_unauthenticated_api_is_rejected(client):
    c, _ = client
    assert c.get("/api/tasks").status_code == 401
    assert c.post("/api/actions", json={"name": "x", "command": "/bin/true"}).status_code == 401


def test_unauthenticated_browser_get_redirects_to_login(client):
    c, _ = client
    r = c.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_bearer_token_grants_api_access(client):
    c, key = client
    assert _admitted(c.get("/api/tasks", headers={"Authorization": f"Bearer {key}"}))


def test_wrong_bearer_rejected(client):
    c, _ = client
    assert c.get("/api/tasks", headers={"Authorization": "Bearer not-the-key"}).status_code == 401


def test_login_sets_cookie_and_cookie_grants_access(client):
    c, key = client
    r = c.get(f"/login?key={key}", follow_redirects=False)
    assert r.status_code == 303
    assert c.cookies.get("mage_scheduler_key") == key
    # subsequent request rides the cookie the client now holds
    assert _admitted(c.get("/api/tasks"))


def test_login_wrong_key_no_cookie(client):
    c, _ = client
    r = c.get("/login?key=wrong", follow_redirects=False)
    assert r.status_code == 401
    assert "mage_scheduler_key" not in r.cookies


def test_key_file_is_0600(client, tmp_path):
    _, _ = client
    keyfile = tmp_path / "api.key"
    assert keyfile.exists()
    assert (keyfile.stat().st_mode & 0o777) == 0o600


def test_anon_env_opens_the_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHEDULER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCHEDULER_ALLOW_ANON", "1")
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mage_scheduler"))
    import auth, api
    importlib.reload(auth)
    importlib.reload(api)
    c = TestClient(api.app, raise_server_exceptions=False)
    assert _admitted(c.get("/api/tasks"))   # no creds, but anon on
