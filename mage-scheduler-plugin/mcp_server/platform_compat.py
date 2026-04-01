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
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)


def venv_python_path(plugin_dir: Path) -> Path:
    """Return the venv Python executable path for the current platform."""
    if sys.platform == "win32":
        return plugin_dir / ".venv" / "Scripts" / "python.exe"
    return plugin_dir / ".venv" / "bin" / "python"


def detached_popen_kwargs() -> dict:
    """Return Popen kwargs for spawning a detached background process."""
    if sys.platform == "win32":
        return {
            "creationflags": _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
        }
    return {"start_new_session": True}


def find_pid_on_port(port: int) -> int | None:
    """Return the PID of the process listening on port, or None."""
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr.port == port and conn.status == "LISTEN":
                return conn.pid
    except Exception:
        pass
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
