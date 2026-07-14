"""The dashboard must open in the system browser by default.

The mage-lab in-app HTML tab is sandboxed without allow-same-origin (app commit
f88d4462), which makes the dashboard a null origin and blocks its fetch/forms/
buttons. So _open_in_app must default to the real browser, and must NOT attempt
the sandboxed in-app tab unless explicitly opted in.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

tools = pytest.importorskip("mcp_server.tools")


def test_defaults_to_browser(monkeypatch):
    monkeypatch.delenv("SCHEDULER_DASHBOARD_IN_APP", raising=False)
    browser, posts = [], []
    monkeypatch.setattr(tools, "open_browser", lambda url: browser.append(url))
    monkeypatch.setattr(tools.httpx, "post", lambda *a, **k: posts.append(1))

    tools._open_in_app("http://127.0.0.1:8012/", "dashboard")

    assert browser == ["http://127.0.0.1:8012/"]
    assert posts == []  # never touches the sandboxed in-app open_file path


@pytest.mark.parametrize("val", ["1", "true", "YES"])
def test_in_app_opt_in_uses_app_tab(monkeypatch, val):
    monkeypatch.setenv("SCHEDULER_DASHBOARD_IN_APP", val)
    posts = []

    class _Resp:
        status_code = 200

    monkeypatch.setattr(tools.Path, "mkdir", lambda self, **k: None)
    monkeypatch.setattr(tools.Path, "write_text", lambda self, *a, **k: None)
    monkeypatch.setattr(tools.httpx, "post", lambda *a, **k: posts.append(1) or _Resp())
    monkeypatch.setattr(
        tools, "open_browser",
        lambda url: pytest.fail("must not fall back to browser when opted in"),
    )

    tools._open_in_app("http://127.0.0.1:8012/", "dashboard")

    assert posts == [1]  # opted in → attempted the in-app open_file tab
