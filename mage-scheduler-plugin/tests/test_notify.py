"""Tests for the notify module — assistant POST + cross-platform OS backstop.

The OS command is asserted per-platform via the pure ``os_notify_command`` so no
real notification is ever spawned in CI.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mage_scheduler"))

import notify  # noqa: E402


# --------------------------------------------------------------------------
# post_to_assistant
# --------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_post_to_assistant_true_on_2xx(monkeypatch):
    monkeypatch.setattr(notify.urllib.request, "urlopen", lambda *a, **k: _FakeResp(200))
    assert notify.post_to_assistant("hi") is True


def test_post_to_assistant_false_on_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    # Undeliverable must be reported as False, never raised.
    assert notify.post_to_assistant("hi") is False


# --------------------------------------------------------------------------
# os_notify_command — per platform
# --------------------------------------------------------------------------

def test_macos_uses_osascript():
    cmd = notify.os_notify_command("Title", "Body", platform="darwin")
    assert cmd[0] == "osascript" and cmd[1] == "-e"
    assert "display notification" in cmd[2]
    assert '"Body"' in cmd[2] and '"Title"' in cmd[2]


def test_macos_escapes_quotes():
    cmd = notify.os_notify_command('T', 'say "hi" \\ok', platform="darwin")
    # Embedded double-quotes and backslashes are escaped so AppleScript parses.
    assert '\\"hi\\"' in cmd[2] and "\\\\ok" in cmd[2]


def test_linux_uses_notify_send_when_present(monkeypatch):
    monkeypatch.setattr(notify, "sys", notify.sys)  # keep sys
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/notify-send")
    cmd = notify.os_notify_command("Title", "Body", platform="linux")
    assert cmd == ["notify-send", "Title", "Body"]


def test_linux_none_when_notify_send_absent(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert notify.os_notify_command("T", "B", platform="linux") is None


def test_windows_uses_powershell_toast():
    cmd = notify.os_notify_command("Title", "Body", platform="win32")
    assert cmd[0] == "powershell" and "-Command" in cmd
    script = cmd[-1]
    assert "ToastNotificationManager" in script
    assert "'Title'" in script and "'Body'" in script


def test_windows_escapes_single_quotes():
    cmd = notify.os_notify_command("T", "it's here", platform="win32")
    assert "it''s here" in cmd[-1]  # PowerShell single-quote escaping


def test_unknown_platform_returns_none():
    assert notify.os_notify_command("T", "B", platform="sunos") is None


def test_os_notify_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no notifier")

    monkeypatch.setattr(notify.subprocess, "run", boom)
    # Backstop failure must be swallowed.
    notify.os_notify("T", "B")
