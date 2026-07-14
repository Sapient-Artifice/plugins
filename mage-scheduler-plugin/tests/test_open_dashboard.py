"""The dashboard opens as an in-app Mage Lab tab by default.

Mage Lab's in-app HTML tab is a cross-origin sandbox where native form POST
works (the dashboard is built to that constraint), so the dashboard defaults to
the in-app tab. SCHEDULER_DASHBOARD_IN_BROWSER=1 forces the system browser.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

tools = pytest.importorskip("mcp_server.tools")


def test_defaults_to_in_app(monkeypatch):
    monkeypatch.delenv("SCHEDULER_DASHBOARD_IN_BROWSER", raising=False)
    posts = []

    class _Resp:
        status_code = 200

    monkeypatch.setattr(tools.Path, "mkdir", lambda self, **k: None)
    monkeypatch.setattr(tools.Path, "write_text", lambda self, *a, **k: None)
    monkeypatch.setattr(tools.httpx, "post", lambda *a, **k: posts.append(1) or _Resp())
    monkeypatch.setattr(
        tools, "open_browser",
        lambda url: pytest.fail("default must be the in-app tab, not the browser"),
    )

    tools._open_in_app("http://127.0.0.1:8012/", "dashboard")

    assert posts == [1]  # attempted the in-app open_file tab


@pytest.mark.parametrize("val", ["1", "true", "YES"])
def test_in_browser_opt_in(monkeypatch, val):
    monkeypatch.setenv("SCHEDULER_DASHBOARD_IN_BROWSER", val)
    browser, posts = [], []
    monkeypatch.setattr(tools, "open_browser", lambda url: browser.append(url))
    monkeypatch.setattr(tools.httpx, "post", lambda *a, **k: posts.append(1))

    tools._open_in_app("http://127.0.0.1:8012/", "dashboard")

    assert browser == ["http://127.0.0.1:8012/"]
    assert posts == []  # forced browser → never touches the in-app path
