"""Platform abstraction layer for the Mage Scheduler MCP server.

All platform-specific operations are isolated here. Other modules
import from this module rather than calling OS APIs directly.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import psutil

# Windows-only subprocess constants; define fallback values on other platforms
# so that the module can be imported and tested cross-platform.
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)


def venv_python_path(plugin_dir: Path) -> Path:
    """Return the venv Python executable path for the current platform."""
    if sys.platform == "win32":
        return plugin_dir / ".venv" / "Scripts" / "python.exe"
    return plugin_dir / ".venv" / "bin" / "python"


def detached_popen_kwargs() -> dict:
    """Return Popen kwargs for spawning a detached background process."""
    if sys.platform == "win32":
        # CREATE_NO_WINDOW unconditionally suppresses the console window, unlike
        # DETACHED_PROCESS which fails when the parent has no console (e.g. Tauri).
        return {
            "creationflags": _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
        }
    return {"start_new_session": True}


def find_pid_on_port(port: int) -> int | None:
    """Return the PID of the scheduler backend serving ``port``, or None.

    Tries the direct listener lookup first (fast, works without privileges on
    Linux and Windows). On macOS ``psutil.net_connections`` raises
    ``AccessDenied`` for a non-root user, so fall back to matching our own
    uvicorn process by command line — which is permission-safe for a
    same-user process on every platform.
    """
    try:
        for conn in psutil.net_connections(kind="inet"):
            laddr = getattr(conn, "laddr", None)
            if laddr and laddr.port == port and conn.status == "LISTEN" and conn.pid:
                return conn.pid
    except (psutil.AccessDenied, PermissionError):
        pass  # macOS: system-wide connection table needs root — use the fallback
    except Exception:
        pass
    return _find_uvicorn_pid(port)


def _find_uvicorn_pid(port: int) -> int | None:
    """Find the backend by matching the uvicorn command line for ``port``.

    Identifies the process the scheduler spawns in backend._start_backend
    (``python -m uvicorn api:app ... --port <port>``). Reading another user's
    cmdline can raise, so per-process errors are skipped.
    """
    port_token = str(port)
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if not cmdline:
            continue
        # Require the port as a discrete argv token so we don't match a
        # different uvicorn (or the port as a substring of another number).
        if port_token in cmdline and "uvicorn" in " ".join(cmdline) and "api:app" in cmdline:
            return proc.pid
    return None


def terminate_process(pid: int) -> None:
    """Terminate a process gracefully, escalating to force-kill after 5 seconds."""
    try:
        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except psutil.TimeoutExpired:
            proc.kill()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass


def open_browser(url: str) -> None:
    """Open url in the default browser using the platform-appropriate command."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", url])
    elif sys.platform == "win32":
        subprocess.Popen(["cmd", "/c", "start", url])
    else:
        subprocess.Popen(["xdg-open", url])
