"""
notify — delivery channels for scheduler notifications.

Two channels, both best-effort (a delivery failure must never affect task
state or reconciliation):

  * ``post_to_assistant`` — the primary channel: POST a message to Mage's
    ask_assistant endpoint. Returns whether it was accepted, so callers can
    decide whether to keep retrying (a returning user is reached the first time
    the frontend is actually connected).

  * ``os_notify`` — an OS-level backstop that does NOT depend on Mage being
    alive (native notification center / toast / libnotify). This is what makes
    a parked task "loud" even when the assistant channel is down.

The OS command is built by the pure, testable ``os_notify_command`` so the
per-platform argv can be asserted without spawning anything.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request

ASK_ASSISTANT_ENDPOINT = os.getenv(
    "MAGE_ASK_ASSISTANT_URL", "http://127.0.0.1:11115/ask_assistant"
)

# Windows: suppress the console window when shelling out to PowerShell. Mirrors
# mcp_server.platform_compat.detached_popen_kwargs; duplicated here to keep this
# module free of a cross-package import (jobs run with mage_scheduler/ on path).
_CREATE_NO_WINDOW = 0x08000000

_NOTIFY_TITLE = "Mage Scheduler"


def post_to_assistant(message: str, *, timeout: int = 10) -> bool:
    """POST a message to ask_assistant. Return True iff it was accepted (2xx).

    Never raises — a False return simply means "not delivered right now", which
    the caller uses to keep the notification pending until the user is back.
    """
    payload = json.dumps({"message": message}).encode()
    req = urllib.request.Request(
        ASK_ASSISTANT_ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception:
        return False


def _osa_escape(text: str) -> str:
    """Escape a string for embedding in an AppleScript double-quoted literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _ps_escape(text: str) -> str:
    """Escape a string for a PowerShell single-quoted literal."""
    return text.replace("'", "''")


def _powershell_toast(title: str, body: str) -> str:
    """A self-contained PowerShell toast (Win10+), silent if the API is absent."""
    t = _ps_escape(title)
    b = _ps_escape(body)
    return (
        "$ErrorActionPreference='SilentlyContinue';"
        "[void][Windows.UI.Notifications.ToastNotificationManager,"
        "Windows.UI.Notifications,ContentType=WindowsRuntime];"
        "$x=[Windows.UI.Notifications.ToastNotificationManager]::"
        "GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$n=$x.GetElementsByTagName('text');"
        f"[void]$n.Item(0).AppendChild($x.CreateTextNode('{t}'));"
        f"[void]$n.Item(1).AppendChild($x.CreateTextNode('{b}'));"
        "$toast=[Windows.UI.Notifications.ToastNotification]::new($x);"
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        f"CreateToastNotifier('{_NOTIFY_TITLE}').Show($toast);"
    )


def os_notify_command(
    title: str, body: str, *, platform: str | None = None
) -> list[str] | None:
    """Return the argv for a native notification, or None if unsupported.

    Pure and side-effect-free so tests can assert the per-platform command.
    """
    plat = platform if platform is not None else sys.platform
    if plat == "darwin":
        script = (
            f'display notification "{_osa_escape(body)}" '
            f'with title "{_osa_escape(title)}"'
        )
        return ["osascript", "-e", script]
    if plat.startswith("linux"):
        # notify-send is the de-facto libnotify CLI; absent on headless boxes.
        from shutil import which

        if which("notify-send"):
            return ["notify-send", title, body]
        return None
    if plat.startswith("win"):
        return [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            _powershell_toast(title, body),
        ]
    return None


def os_notify(title: str, body: str) -> None:
    """Best-effort native OS notification. Never raises."""
    try:
        cmd = os_notify_command(title, body)
        if not cmd:
            return
        kwargs: dict = {"timeout": 10, "capture_output": True}
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        subprocess.run(cmd, **kwargs)
    except Exception:
        pass  # A backstop that fails must never disturb the caller.
