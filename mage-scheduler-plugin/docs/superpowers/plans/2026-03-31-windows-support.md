# Windows Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the mage-scheduler plugin fully cross-platform (Windows, macOS, Linux) by isolating all platform-specific operations, replacing Unix-only tools with `psutil`, updating the entry point to `uv run`, and resolving bare command names at schedule time.

**Architecture:** A new `mcp_server/platform_compat.py` module holds all platform-conditional logic. `backend.py`, `tools.py` import from it. `db.py` uses a 3-line inline conditional (cross-package import is not safe in the uvicorn subprocess context). Command resolution via `shutil.which()` is added to `_validate_command()` in `api.py`, which now returns the resolved command string.

**Tech Stack:** Python 3.11+, psutil 5.9+, shutil (stdlib), uv, FastAPI, SQLAlchemy, APScheduler

**Spec:** `docs/superpowers/specs/2026-03-31-windows-support-design.md`

---

## File Map

| File | Action | What changes |
|------|--------|-------------|
| `pyproject.toml` | Modify | Add `psutil>=5.9.0` to dependencies |
| `mcp_server/platform_compat.py` | **Create** | All platform abstractions |
| `mcp_server/backend.py` | Modify | Import from `platform_compat`; remove `signal` |
| `mcp_server/tools.py` | Modify | `open_browser()` via `platform_compat` |
| `mage_scheduler/db.py` | Modify | Inline Windows venv path check |
| `mage_scheduler/api.py` | Modify | `_validate_command` returns resolved str; `shutil.which()` resolution |
| `.mcp.json` | Modify | `uv run python -m mcp_server` |
| `bin/start_mcp.py` | **Delete** | |
| `tests/test_platform_compat.py` | **Create** | Unit tests for compat module |
| `tests/test_backend_restart.py` | Modify | Retarget patches to `platform_compat` |
| `tests/test_command_resolution.py` | **Create** | Command resolution tests |

---

## Task 1: Add psutil dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add psutil to dependencies**

Edit `pyproject.toml`. In the `dependencies` list, add after `"python-multipart>=0.0.9"`:

```toml
dependencies = [
    "apscheduler>=3.10.0",
    "croniter>=1.4.0",
    "dateparser>=1.2.0",
    "fastapi>=0.115.0",
    "httpx>=0.27.0",
    "jinja2>=3.1.3",
    "mcp>=1.0.0",
    "psutil>=5.9.0",
    "python-multipart>=0.0.9",
    "sqlalchemy>=2.0.0",
    "uvicorn>=0.30.0",
    "tzdata>=2024.1; sys_platform == 'win32'",
]
```

- [ ] **Step 2: Sync the venv**

```bash
cd mage-scheduler-plugin
uv sync
```

Expected: `uv` resolves and installs psutil alongside existing deps. No errors.

- [ ] **Step 3: Verify psutil is importable**

```bash
uv run python -c "import psutil; print(psutil.__version__)"
```

Expected: prints a version string like `5.9.x`.

- [ ] **Step 4: Commit**

```bash
git add mage-scheduler-plugin/pyproject.toml mage-scheduler-plugin/uv.lock
git commit -m "feat: add psutil dependency for cross-platform process management"
```

---

## Task 2: Create `mcp_server/platform_compat.py`

**Files:**
- Create: `mcp_server/platform_compat.py`
- Create: `tests/test_platform_compat.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_platform_compat.py`:

```python
"""Unit tests for mcp_server/platform_compat.py."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import pytest

import mcp_server.platform_compat as compat


# ---------------------------------------------------------------------------
# venv_python_path
# ---------------------------------------------------------------------------

class TestVenvPythonPath:
    def test_unix_path(self):
        with patch("sys.platform", "linux"):
            result = compat.venv_python_path(Path("/plugin"))
        assert result == Path("/plugin/.venv/bin/python")

    def test_macos_path(self):
        with patch("sys.platform", "darwin"):
            result = compat.venv_python_path(Path("/plugin"))
        assert result == Path("/plugin/.venv/bin/python")

    def test_windows_path(self):
        with patch("sys.platform", "win32"):
            result = compat.venv_python_path(Path("C:/plugin"))
        assert result == Path("C:/plugin/.venv/Scripts/python.exe")


# ---------------------------------------------------------------------------
# detached_popen_kwargs
# ---------------------------------------------------------------------------

class TestDetachedPopenKwargs:
    def test_unix_returns_start_new_session(self):
        with patch("sys.platform", "linux"):
            result = compat.detached_popen_kwargs()
        assert result == {"start_new_session": True}

    def test_windows_returns_creationflags(self):
        with patch("sys.platform", "win32"):
            result = compat.detached_popen_kwargs()
        assert "creationflags" in result
        expected = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        assert result["creationflags"] == expected


# ---------------------------------------------------------------------------
# find_pid_on_port
# ---------------------------------------------------------------------------

class TestFindPidOnPort:
    def _make_conn(self, port: int, status: str, pid: int):
        conn = MagicMock()
        conn.laddr.port = port
        conn.status = status
        conn.pid = pid
        return conn

    def test_returns_pid_for_listening_connection(self):
        conn = self._make_conn(8012, "LISTEN", 1234)
        with patch.object(compat.psutil, "net_connections", return_value=[conn]):
            assert compat.find_pid_on_port(8012) == 1234

    def test_returns_none_when_port_not_matched(self):
        conn = self._make_conn(9999, "LISTEN", 1234)
        with patch.object(compat.psutil, "net_connections", return_value=[conn]):
            assert compat.find_pid_on_port(8012) is None

    def test_ignores_non_listening_connections(self):
        conn = self._make_conn(8012, "ESTABLISHED", 1234)
        with patch.object(compat.psutil, "net_connections", return_value=[conn]):
            assert compat.find_pid_on_port(8012) is None

    def test_returns_none_on_access_denied(self):
        with patch.object(compat.psutil, "net_connections",
                          side_effect=psutil.AccessDenied(0)):
            assert compat.find_pid_on_port(8012) is None

    def test_returns_none_on_exception(self):
        with patch.object(compat.psutil, "net_connections",
                          side_effect=OSError("unexpected")):
            assert compat.find_pid_on_port(8012) is None


# ---------------------------------------------------------------------------
# terminate_process
# ---------------------------------------------------------------------------

class TestTerminateProcess:
    def test_terminates_gracefully(self):
        mock_proc = MagicMock()
        mock_proc.wait.return_value = None
        with patch.object(compat.psutil, "Process", return_value=mock_proc):
            compat.terminate_process(1234)
        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_not_called()

    def test_force_kills_on_timeout(self):
        mock_proc = MagicMock()
        mock_proc.wait.side_effect = psutil.TimeoutExpired(5)
        with patch.object(compat.psutil, "Process", return_value=mock_proc):
            compat.terminate_process(1234)
        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()

    def test_swallows_no_such_process(self):
        with patch.object(compat.psutil, "Process",
                          side_effect=psutil.NoSuchProcess(1234)):
            compat.terminate_process(1234)  # must not raise

    def test_swallows_access_denied(self):
        with patch.object(compat.psutil, "Process",
                          side_effect=psutil.AccessDenied(1234)):
            compat.terminate_process(1234)  # must not raise


# ---------------------------------------------------------------------------
# open_browser
# ---------------------------------------------------------------------------

class TestOpenBrowser:
    def test_macos_uses_open(self):
        with patch("sys.platform", "darwin"), \
             patch.object(compat.subprocess, "Popen") as mock_popen:
            compat.open_browser("http://127.0.0.1:8012")
        mock_popen.assert_called_once_with(["open", "http://127.0.0.1:8012"])

    def test_linux_uses_xdg_open(self):
        with patch("sys.platform", "linux"), \
             patch.object(compat.subprocess, "Popen") as mock_popen:
            compat.open_browser("http://127.0.0.1:8012")
        mock_popen.assert_called_once_with(["xdg-open", "http://127.0.0.1:8012"])

    def test_windows_uses_cmd_start(self):
        with patch("sys.platform", "win32"), \
             patch.object(compat.subprocess, "Popen") as mock_popen:
            compat.open_browser("http://127.0.0.1:8012")
        mock_popen.assert_called_once_with(["cmd", "/c", "start", "http://127.0.0.1:8012"])
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd mage-scheduler-plugin
uv run pytest tests/test_platform_compat.py -v
```

Expected: `ModuleNotFoundError: No module named 'mcp_server.platform_compat'`

- [ ] **Step 3: Create `mcp_server/platform_compat.py`**

```python
"""Platform abstraction layer for the Mage Scheduler MCP server.

All platform-specific operations are isolated here. Other modules
import from this module rather than calling OS APIs directly.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import psutil


def venv_python_path(plugin_dir: Path) -> Path:
    """Return the venv Python executable path for the current platform."""
    if sys.platform == "win32":
        return plugin_dir / ".venv" / "Scripts" / "python.exe"
    return plugin_dir / ".venv" / "bin" / "python"


def detached_popen_kwargs() -> dict:
    """Return Popen kwargs for spawning a detached background process."""
    if sys.platform == "win32":
        return {
            "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
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
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_platform_compat.py -v
```

Expected: all tests PASS. Note: `subprocess.DETACHED_PROCESS` and `subprocess.CREATE_NEW_PROCESS_GROUP` are Windows-only and absent on Linux/macOS. Use `getattr(subprocess, "DETACHED_PROCESS", 0x00000008)` and `getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)` in the implementation and tests.

- [ ] **Step 5: Run full test suite to confirm no regressions**

```bash
uv run pytest tests/ -v
```

Expected: all existing tests pass. New tests pass.

- [ ] **Step 6: Commit**

```bash
git add mage-scheduler-plugin/mcp_server/platform_compat.py \
        mage-scheduler-plugin/tests/test_platform_compat.py
git commit -m "feat: add platform_compat module with cross-platform process management"
```

---

## Task 3: Update `mcp_server/backend.py`

**Files:**
- Modify: `mcp_server/backend.py`
- Modify: `tests/test_backend_restart.py`

- [ ] **Step 1: Write updated tests that target the new interface**

Replace the entire content of `tests/test_backend_restart.py` with:

```python
"""Unit tests for mcp_server/backend.py — process management helpers."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from mcp_server.backend import _find_backend_pid, _is_ready, restart_backend, PORT


# ---------------------------------------------------------------------------
# _is_ready
# ---------------------------------------------------------------------------

class TestIsReady:
    def _mock_urlopen(self, status: int):
        resp = MagicMock()
        resp.status = status
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_returns_true_on_200(self):
        with patch("mcp_server.backend.urllib.request.urlopen",
                   return_value=self._mock_urlopen(200)):
            assert _is_ready() is True

    def test_returns_false_on_connection_error(self):
        with patch("mcp_server.backend.urllib.request.urlopen",
                   side_effect=OSError("connection refused")):
            assert _is_ready() is False

    def test_returns_false_on_non_200(self):
        with patch("mcp_server.backend.urllib.request.urlopen",
                   return_value=self._mock_urlopen(503)):
            assert _is_ready() is False


# ---------------------------------------------------------------------------
# _find_backend_pid
# ---------------------------------------------------------------------------

class TestFindBackendPid:
    def test_delegates_to_find_pid_on_port(self):
        with patch("mcp_server.backend.find_pid_on_port", return_value=1234) as mock:
            result = _find_backend_pid()
        mock.assert_called_once_with(PORT)
        assert result == 1234

    def test_returns_none_when_not_found(self):
        with patch("mcp_server.backend.find_pid_on_port", return_value=None):
            assert _find_backend_pid() is None


# ---------------------------------------------------------------------------
# restart_backend
# ---------------------------------------------------------------------------

class TestRestartBackend:
    def test_starts_fresh_when_no_existing_backend(self):
        with patch("mcp_server.backend._find_backend_pid", return_value=None), \
             patch("mcp_server.backend._start_backend") as mock_start, \
             patch("mcp_server.backend._wait_for_ready", return_value=True):
            success, msg = restart_backend(timeout_secs=1)

        assert success is True
        assert "restarted" in msg.lower()
        mock_start.assert_called_once()

    def test_terminates_existing_pid_before_restart(self):
        with patch("mcp_server.backend._find_backend_pid", return_value=9999), \
             patch("mcp_server.backend.terminate_process") as mock_terminate, \
             patch("mcp_server.backend._is_ready", return_value=False), \
             patch("mcp_server.backend.time.sleep"), \
             patch("mcp_server.backend.time.monotonic", side_effect=[0.0, 10.0]), \
             patch("mcp_server.backend._start_backend"), \
             patch("mcp_server.backend._wait_for_ready", return_value=True):
            success, msg = restart_backend(timeout_secs=1)

        mock_terminate.assert_called_once_with(9999)
        assert success is True

    def test_returns_false_when_backend_never_ready(self):
        with patch("mcp_server.backend._find_backend_pid", return_value=None), \
             patch("mcp_server.backend._start_backend"), \
             patch("mcp_server.backend._wait_for_ready", return_value=False):
            success, msg = restart_backend(timeout_secs=1)

        assert success is False
        assert "did not become ready" in msg
```

- [ ] **Step 2: Run to confirm tests fail**

```bash
uv run pytest tests/test_backend_restart.py -v
```

Expected: `TestFindBackendPid` tests fail (function still uses `ss`), `TestRestartBackend::test_terminates_existing_pid_before_restart` fails (still uses `os.kill`).

- [ ] **Step 3: Rewrite `mcp_server/backend.py`**

Replace the entire file with:

```python
"""
Mage Scheduler — backend process management
=============================================
Shared by __main__.py (startup) and tools.py (scheduler_restart_backend).
"""
from __future__ import annotations

import os
import time
import urllib.request
from pathlib import Path

from mcp_server.platform_compat import (
    detached_popen_kwargs,
    find_pid_on_port,
    terminate_process,
    venv_python_path,
)

import subprocess

PLUGIN_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = PLUGIN_DIR / "mage_scheduler"

PORT = int(os.environ.get("SCHEDULER_PORT", "8012"))
HOST = os.environ.get("SCHEDULER_HOST", "127.0.0.1")
BASE_URL = f"http://{HOST}:{PORT}"

DATA_DIR = Path(os.environ.get("SCHEDULER_DATA_DIR", Path.home() / ".mage_scheduler"))


def _is_ready(timeout: float = 1.5) -> bool:
    try:
        req = urllib.request.Request(f"{BASE_URL}/health")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _start_backend() -> subprocess.Popen:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_path = DATA_DIR / "scheduler.log"
    log_file = open(log_path, "a", encoding="utf-8")  # noqa: SIM115

    python = venv_python_path(PLUGIN_DIR)

    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT")}
    env["SCHEDULER_DATA_DIR"] = str(DATA_DIR)

    return subprocess.Popen(
        [str(python), "-m", "uvicorn", "api:app",
         "--host", HOST, "--port", str(PORT), "--log-level", "warning"],
        cwd=str(BACKEND_DIR),
        stdout=log_file,
        stderr=log_file,
        env=env,
        **detached_popen_kwargs(),
    )


def _wait_for_ready(timeout_secs: int = 15) -> bool:
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        if _is_ready():
            return True
        time.sleep(0.4)
    return False


def _find_backend_pid() -> int | None:
    """Return the PID of the process listening on PORT, or None."""
    return find_pid_on_port(PORT)


def restart_backend(timeout_secs: int = 15) -> tuple[bool, str]:
    """Kill the running backend (if any) and start a fresh one.

    Returns (success, message).
    """
    pid = _find_backend_pid()
    if pid:
        terminate_process(pid)
        # Wait for the port to be released before starting fresh.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not _is_ready(timeout=0.4):
                break
            time.sleep(0.3)

    _start_backend()

    if _wait_for_ready(timeout_secs=timeout_secs):
        return True, f"Backend restarted successfully on {BASE_URL}."
    return False, (
        f"Backend process started but did not become ready within {timeout_secs}s. "
        f"Check logs at {DATA_DIR / 'scheduler.log'}."
    )
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_backend_restart.py -v
```

Expected: all 8 tests PASS.

- [ ] **Step 5: Run full test suite**

```bash
uv run pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add mage-scheduler-plugin/mcp_server/backend.py \
        mage-scheduler-plugin/tests/test_backend_restart.py
git commit -m "feat: replace signal/ss-based process management with psutil in backend.py"
```

---

## Task 4: Update `mcp_server/tools.py`

**Files:**
- Modify: `mcp_server/tools.py:112-115`

- [ ] **Step 1: Replace inline browser open with `open_browser` from `platform_compat`**

In `mcp_server/tools.py`, find the imports at the top. Remove these two lines:
```python
import platform
import subprocess
```

Add this import (alongside the existing `mcp_server` imports, after the stdlib block):
```python
from mcp_server.platform_compat import open_browser
```

Then find the fallback block (around line 112):
```python
        if platform.system() == "Darwin":
            subprocess.Popen(["open", url])
        else:
            subprocess.Popen(["xdg-open", url])
```

Replace with:
```python
        open_browser(url)
```

- [ ] **Step 2: Run full test suite**

```bash
uv run pytest tests/ -v
```

Expected: all tests pass. (tools.py has no direct unit tests — the change is verified by no import errors and the suite passing.)

- [ ] **Step 3: Commit**

```bash
git add mage-scheduler-plugin/mcp_server/tools.py
git commit -m "feat: use platform_compat.open_browser in tools.py for Windows support"
```

---

## Task 5: Update `mage_scheduler/db.py`

**Files:**
- Modify: `mage_scheduler/db.py:42-51`

- [ ] **Step 1: Replace Unix-only venv path with inline platform check**

In `mage_scheduler/db.py`, add `import sys` to the existing imports at the top of the file (alongside `import os`):

```python
import sys
```

Then replace the `_ask_assistant_command` function (lines 42-51) with:

```python
def _ask_assistant_command() -> str:
    """Return the canonical command for the ask_assistant action."""
    script = Path(__file__).resolve().parent / "scripts" / "ask_assistant.py"
    plugin_dir = Path(__file__).resolve().parent.parent
    if sys.platform == "win32":
        venv_python = plugin_dir / ".venv" / "Scripts" / "python.exe"
    else:
        venv_python = plugin_dir / ".venv" / "bin" / "python"
    return f"{venv_python} {script}"
```

- [ ] **Step 2: Run full test suite**

```bash
uv run pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add mage-scheduler-plugin/mage_scheduler/db.py
git commit -m "feat: fix Windows venv path in db.py ask_assistant command"
```

---

## Task 6: Update entry points

**Files:**
- Modify: `.mcp.json`
- Delete: `bin/start_mcp.py`

- [ ] **Step 1: Update `.mcp.json`**

Replace the entire content of `mage-scheduler-plugin/.mcp.json` with:

```json
{
  "mcpServers": {
    "mage-scheduler": {
      "command": "uv",
      "args": ["run", "python", "-m", "mcp_server"]
    }
  }
}
```

- [ ] **Step 2: Delete `bin/start_mcp.py`**

```bash
rm mage-scheduler-plugin/bin/start_mcp.py
```

- [ ] **Step 3: Run full test suite**

```bash
uv run pytest tests/ -v
```

Expected: all tests pass. (No tests reference `bin/start_mcp.py`.)

- [ ] **Step 4: Commit**

```bash
git add mage-scheduler-plugin/.mcp.json
git rm mage-scheduler-plugin/bin/start_mcp.py
git commit -m "feat: switch entry point to uv run, remove redundant bin/start_mcp.py"
```

---

## Task 7: Add command resolution to `api.py`

**Files:**
- Create: `tests/test_command_resolution.py`
- Modify: `mage_scheduler/api.py`

The goal: `_validate_command()` now accepts bare names, resolves them via `shutil.which()`, hard-blocks if not found, and returns the (possibly resolved) command string. All callers are updated to use the return value.

- [ ] **Step 1: Write failing tests**

Create `tests/test_command_resolution.py`:

```python
"""Tests for command resolution in the intent API.

_validate_command now accepts bare names (e.g. "python3"), resolves them
to absolute paths via shutil.which(), and returns the resolved command.
Absolute paths are passed through unchanged.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

# api.py adds itself to sys.path via pytest pythonpath config
from api import _validate_command
from fastapi import HTTPException


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

    def test_bare_command_resolved_and_scheduled(self, api_client):
        with patch("api.shutil.which", return_value="/usr/bin/python3"), \
             patch("api.os.path.exists", return_value=True), \
             patch("api.os.access", return_value=True):
            resp = api_client.post("/api/tasks/intent", json={
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

    def test_missing_command_returns_blocked(self, api_client):
        with patch("api.shutil.which", return_value=None):
            resp = api_client.post("/api/tasks/intent", json={
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
```

- [ ] **Step 2: Run to confirm tests fail**

```bash
uv run pytest tests/test_command_resolution.py -v
```

Expected: `test_bare_name_resolved_to_absolute` and similar fail because `_validate_command` still raises `command_must_be_absolute` for bare names. `TestIntentEndpointResolution` tests fail with blocked status where scheduled is expected.

- [ ] **Step 3: Add `import shutil` to `api.py`**

In `mage_scheduler/api.py`, find the imports block (around line 7). Add `import shutil` after `import re`:

```python
import re
import shutil
import shlex
```

- [ ] **Step 4: Replace `_validate_command` in `api.py`**

Find `_validate_command` (line 228) and replace it entirely:

```python
def _validate_command(command: str, allowed_dirs: list[str] | None = None) -> str:
    """Validate command, resolving bare executable names via PATH.

    Returns the resolved command string (absolute path substituted for bare name).
    Raises HTTPException on validation failure.
    """
    if not command:
        raise HTTPException(status_code=400, detail="command_required")
    try:
        tokens = shlex.split(command)
    except ValueError:
        raise HTTPException(status_code=400, detail="command_invalid")
    if not tokens:
        raise HTTPException(status_code=400, detail="command_invalid")
    executable = tokens[0]
    if not os.path.isabs(executable):
        resolved = shutil.which(executable)
        if resolved is None:
            raise HTTPException(status_code=400, detail="command_not_found")
        command = resolved + command[len(executable):]
        executable = resolved
    if not os.path.exists(executable):
        raise HTTPException(status_code=400, detail="command_not_found")
    if not os.access(executable, os.X_OK):
        raise HTTPException(status_code=400, detail="command_not_executable")
    if allowed_dirs:
        if not _is_path_allowed(executable, allowed_dirs):
            raise HTTPException(status_code=400, detail="command_dir_not_allowed")
    return command
```

- [ ] **Step 5: Update `_validate_action_payload` to return the resolved command**

Find `_validate_action_payload` (line 369). Make these three targeted changes — do not rewrite the function body:

**Change 1:** Update the return type annotation from `tuple[list[str] | None, list[str] | None]` to `str`.

**Change 2:** Change this line:
```python
    _validate_command(payload.command, settings.allowed_command_dirs)
```
to:
```python
    resolved_command = _validate_command(payload.command, settings.allowed_command_dirs)
```

**Change 3:** Change the `_get_executable` call from:
```python
        executable = _get_executable(payload.command)
```
to:
```python
        executable = _get_executable(resolved_command)
```

**Change 4:** At the very end of the function body (after all validation), add:
```python
    return resolved_command
```

- [ ] **Step 6: Update `create_action` to use the resolved command**

Find `create_action` (line 1035). Change the call from `_validate_action_payload(payload, settings)` to capture the return, and use it when creating the Action:

```python
@app.post("/api/actions", response_model=ActionRead)
def create_action(payload: ActionCreate, db: Session = Depends(get_db)):
    settings = _get_settings(db)
    resolved_command = _validate_action_payload(payload, settings)
    existing = db.execute(select(Action).where(Action.name == payload.name)).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="action_name_exists")
    action = Action(
        name=payload.name,
        command=resolved_command,
        description=payload.description,
        default_cwd=payload.default_cwd,
        allowed_env_json=json.dumps(payload.allowed_env) if payload.allowed_env else None,
        allowed_command_dirs_json=(
            json.dumps(payload.allowed_command_dirs) if payload.allowed_command_dirs else None
        ),
        allowed_cwd_dirs_json=(
            json.dumps(payload.allowed_cwd_dirs) if payload.allowed_cwd_dirs else None
        ),
        max_retries=max(0, payload.max_retries),
        retry_delay=max(1, payload.retry_delay),
        retain_result=1 if payload.retain_result else 0,
    )
    db.add(action)
    db.commit()
    db.refresh(action)
    return action
```

- [ ] **Step 7: Update `update_action` to use the resolved command**

Find `update_action` (line 1064). Change similarly:

```python
@app.put("/api/actions/{action_id}", response_model=ActionRead)
def update_action(action_id: int, payload: ActionUpdate, db: Session = Depends(get_db)):
    action = db.get(Action, action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="action_not_found")
    settings = _get_settings(db)
    resolved_command = _validate_action_payload(payload, settings)
    existing = db.execute(
        select(Action).where(Action.name == payload.name, Action.id != action_id)
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="action_name_exists")
    action.name = payload.name
    action.command = resolved_command
    action.description = payload.description
    action.default_cwd = payload.default_cwd
    action.allowed_env_json = json.dumps(payload.allowed_env) if payload.allowed_env else None
    action.allowed_command_dirs_json = (
        json.dumps(payload.allowed_command_dirs) if payload.allowed_command_dirs else None
    )
    action.allowed_cwd_dirs_json = (
        json.dumps(payload.allowed_cwd_dirs) if payload.allowed_cwd_dirs else None
    )
    action.max_retries = max(0, payload.max_retries)
    action.retry_delay = max(1, payload.retry_delay)
    action.retain_result = 1 if payload.retain_result else 0
    db.commit()
    db.refresh(action)
    return action
```

- [ ] **Step 8: Update HTML form action endpoints to use the resolved command**

Find the `actions_create` HTML form handler (around line 863). Change:

```python
        try:
            _validate_action_payload(
                ActionCreate(...),
                settings,
            )
        except HTTPException as exc:
            return templates.TemplateResponse(...)
```

To:

```python
        try:
            resolved_command = _validate_action_payload(
                ActionCreate(
                    name=name,
                    description=description,
                    command=command,
                    default_cwd=default_cwd,
                    allowed_env=_parse_allowed_env(allowed_env),
                    allowed_command_dirs=allowed_command_dirs_list,
                    allowed_cwd_dirs=allowed_cwd_dirs_list,
                ),
                settings,
            )
        except HTTPException as exc:
            return templates.TemplateResponse(...)
```

And then wherever the Action is created in that function, replace `command=command` with `command=resolved_command`.

Do the same for the `actions_update` HTML form handler (around line 954): capture `resolved_command = _validate_action_payload(...)` and use `action.command = resolved_command`.

- [ ] **Step 9: Update `create_task_from_intent` — action path**

Find lines 1327-1331 in `create_task_from_intent`:

```python
            try:
                _validate_command(action.command, allowed_command_dirs)
            except HTTPException as exc:
                return _blocked(str(exc.detail), action.command)
            resolved_command = action.command
```

Replace with:

```python
            try:
                resolved_command = _validate_command(action.command, allowed_command_dirs)
            except HTTPException as exc:
                return _blocked(str(exc.detail), action.command)
```

- [ ] **Step 10: Update `create_task_from_intent` — ad-hoc command path**

Find lines 1350-1353:

```python
            try:
                _validate_command(resolved_command, allowed_command_dirs)
            except HTTPException as exc:
                return _blocked(str(exc.detail), resolved_command)
```

Replace with:

```python
            try:
                resolved_command = _validate_command(resolved_command, allowed_command_dirs)
            except HTTPException as exc:
                return _blocked(str(exc.detail), resolved_command)
```

- [ ] **Step 11: Update `_handle_recurring_intent` — action path**

Find lines 1173-1177 in `_handle_recurring_intent`:

```python
        try:
            _validate_command(action.command, allowed_command_dirs)
        except HTTPException as exc:
            return _blocked_recurring(str(exc.detail), action.command)
        resolved_command = action.command
```

Replace with:

```python
        try:
            resolved_command = _validate_command(action.command, allowed_command_dirs)
        except HTTPException as exc:
            return _blocked_recurring(str(exc.detail), action.command)
```

- [ ] **Step 12: Update `_handle_recurring_intent` — ad-hoc command path**

Find lines 1198-1201:

```python
        try:
            _validate_command(resolved_command, allowed_command_dirs)
        except HTTPException as exc:
            return _blocked_recurring(str(exc.detail), resolved_command)
```

Replace with:

```python
        try:
            resolved_command = _validate_command(resolved_command, allowed_command_dirs)
        except HTTPException as exc:
            return _blocked_recurring(str(exc.detail), resolved_command)
```

- [ ] **Step 13: Update `command_not_found` hint in `INTENT_ERROR_HINTS`**

Find line 109:

```python
    "command_not_found": "Verify the command path exists on the host.",
```

Replace with:

```python
    "command_not_found": "Install the command or provide the full absolute path.",
```

- [ ] **Step 14: Run the new tests to confirm they pass**

```bash
uv run pytest tests/test_command_resolution.py -v
```

Expected: all tests PASS.

- [ ] **Step 15: Run full test suite**

```bash
uv run pytest tests/ -v
```

Expected: all tests pass. If any `test_api_action_endpoints.py` tests fail, they're likely testing `command_must_be_absolute` behavior — update those assertions to expect `command_not_found` instead, since that error code now covers both the "bare name not found" and "absolute path not found" cases.

- [ ] **Step 16: Commit**

```bash
git add mage-scheduler-plugin/mage_scheduler/api.py \
        mage-scheduler-plugin/tests/test_command_resolution.py
git commit -m "feat: resolve bare command names via shutil.which at schedule time"
```

---

## Final verification

- [ ] **Run the complete test suite one last time**

```bash
cd mage-scheduler-plugin
uv run pytest tests/ -v --tb=short
```

Expected: 420+ tests, all passing. The count is 412 original + new tests from Tasks 2 and 7.

- [ ] **Confirm deleted files are gone**

```bash
ls mage-scheduler-plugin/bin/
```

Expected: directory is empty or does not exist.

- [ ] **Confirm `.mcp.json` is correct**

```bash
cat mage-scheduler-plugin/.mcp.json
```

Expected:
```json
{
  "mcpServers": {
    "mage-scheduler": {
      "command": "uv",
      "args": ["run", "python", "-m", "mcp_server"]
    }
  }
}
```
