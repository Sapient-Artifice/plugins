# Windows Support Design — Mage Scheduler Plugin

**Date:** 2026-03-31  
**Status:** Approved  
**Scope:** Full cross-platform support — Windows, macOS, Linux — including scheduling arbitrary platform-native shell commands.

---

## Background

The plugin currently runs only on Unix (Linux/macOS). Windows compatibility is a strict pre-release requirement. The plugin must work reliably for users of widely varying technical skill across all three platforms, with no manual configuration required.

---

## Problem Inventory

| # | Problem | Location | Root Cause |
|---|---------|----------|------------|
| 1 | Hardcoded Unix venv path | `backend.py:43`, `db.py:49` | `.venv/bin/python` doesn't exist on Windows (`.venv\Scripts\python.exe`) |
| 2 | `os.execv` for venv re-exec | `bin/start_mcp.py:34` | Unreliable on Windows; not needed with `uv run` |
| 3 | `start_new_session=True` | `backend.py:60` | Not available on Windows; needs `creationflags` instead |
| 4 | Port→PID via `ss` command | `backend.py:76-87` | Linux/macOS only; `ss` doesn't exist on Windows |
| 5 | `signal.SIGTERM`/`SIGKILL` | `backend.py:98,110` | `SIGKILL` doesn't exist on Windows; `SIGTERM` via `os.kill` unreliable |
| 6 | `python3` in `.mcp.json` | `.mcp.json:4` | Typically `python` on Windows; also bypasses venv entirely |
| 7 | No command path resolution | `task_manager.py`, API | LLMs don't know absolute paths; users schedule bare names like `python3` |
| 8 | Browser open missing Windows | `tools.py:112-115` | Only handles Mac (`open`) and Linux (`xdg-open`); no `start` for Windows |

---

## Design

### Approach: Platform Compat Module (Approach B)

All platform-conditional logic is consolidated in a single new file: `mcp_server/platform_compat.py`. No platform branching is scattered across other files. Other modules import clean functions with no awareness of the underlying OS.

---

### 1. Entry Point & `.mcp.json`

**`.mcp.json`** changes from `python3 bin/start_mcp.py` to:

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

`uv run` handles venv creation and activation on all platforms before invoking Python, making explicit venv bootstrap logic unnecessary. Using `-m mcp_server` targets `mcp_server/__main__.py` directly — the canonical entry point.

**`bin/start_mcp.py`** is deleted. Its two responsibilities (venv bootstrap, `os.execv` re-exec) are fully superseded by `uv run`.

**`pyproject.toml`** adds `psutil>=5.9.0` to `dependencies`.

---

### 2. New Module: `mcp_server/platform_compat.py`

Single file containing all platform-specific abstractions. No dependencies on the rest of the plugin. Imports only: `psutil`, `sys`, `os`, `subprocess`, `pathlib`, `time`.

#### `venv_python_path(plugin_dir: Path) -> Path`

Returns the venv Python executable path for the current platform:

- **Unix:** `plugin_dir / ".venv" / "bin" / "python"`
- **Windows:** `plugin_dir / ".venv" / "Scripts" / "python.exe"`

#### `detached_popen_kwargs() -> dict`

Returns `subprocess.Popen` keyword arguments for spawning a detached background process:

- **Unix:** `{"start_new_session": True}`
- **Windows:** `{"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}`

#### `find_pid_on_port(port: int) -> int | None`

Returns the PID of the process listening on `port`, or `None`. Uses `psutil.net_connections()` — cross-platform, replaces the `ss`-based implementation entirely.

```python
for conn in psutil.net_connections(kind="inet"):
    if conn.laddr.port == port and conn.status == "LISTEN":
        return conn.pid
return None
```

#### `terminate_process(pid: int) -> None`

Gracefully terminates a process, escalating to force-kill on timeout. Replaces direct `os.kill` + signal usage:

1. `psutil.Process(pid).terminate()` — sends SIGTERM on Unix, `TerminateProcess` on Windows
2. Wait up to 5 seconds for exit
3. If still running: `psutil.Process(pid).kill()` — sends SIGKILL on Unix, `TerminateProcess` on Windows
4. `psutil.NoSuchProcess` and `psutil.AccessDenied` are silently swallowed (process already gone or unowned)

#### `open_browser(url: str) -> None`

Opens a URL in the default browser using the platform-appropriate command:

- **macOS:** `subprocess.Popen(["open", url])`
- **Linux:** `subprocess.Popen(["xdg-open", url])`
- **Windows:** `subprocess.Popen(["cmd", "/c", "start", url])`

---

### 3. `mcp_server/backend.py` Changes

All platform-specific imports (`signal`) and logic are removed. Three targeted substitutions:

| Before | After |
|--------|-------|
| `python = PLUGIN_DIR / ".venv" / "bin" / "python"` | `python = venv_python_path(PLUGIN_DIR)` |
| `start_new_session=True` | `**detached_popen_kwargs()` |
| `ss`-based `_find_backend_pid()` body | `return find_pid_on_port(PORT)` |
| `os.kill(pid, SIGTERM)` + wait loop + `os.kill(pid, SIGKILL)` | `terminate_process(pid)` |

`import signal` is removed from `backend.py`. The file loses ~20 lines and all OS-specific logic.

---

### 4. Command Resolution

**Problem:** LLMs and non-technical users provide bare command names (`python3`, `ffmpeg`, `git`). The current absolute-path requirement is unworkable for them, but storing bare names verbatim breaks reproducibility.

**Solution:** Resolve at schedule time using `shutil.which()`. Accept bare names in input; store resolved absolute paths in the DB.

**Resolution logic** (applied at the API layer before any DB write):

```
"python3 script.py"        → which("python3") → "/usr/bin/python3 script.py"  stored
"/usr/bin/python3 ..."     → already absolute  → stored as-is
"C:\Python311\python.exe"  → already absolute  → stored as-is
"ffmpeg -i in out"         → which("ffmpeg") returns None → HARD BLOCK
```

Absolute path detection uses `os.path.isabs()` — correctly handles both Unix and Windows paths with no branching.

**Hard block error response:**
```json
{
  "status": "blocked",
  "error": "command_not_found",
  "message": "\"ffmpeg\" was not found on PATH. Install it or provide the full path."
}
```

**Scope:** Resolution runs at two points:
- Intent endpoint (`POST /api/tasks/intent`) — before task/recurring task creation
- Action create/update endpoints — so stored Actions always have absolute paths

**`db.py` — `_ask_assistant_command()`:** The venv Python path construction is updated with an inline 3-line platform conditional. Importing from `mcp_server.platform_compat` is not used here because `db.py` runs inside the `mage_scheduler/` package context (uvicorn CWD is `mage_scheduler/`, and `backend.py` strips `PYTHONPATH` from the subprocess env), making cross-package imports unreliable. The logic is trivial enough that duplication is the right call:

```python
if sys.platform == "win32":
    venv_python = PLUGIN_DIR / ".venv" / "Scripts" / "python.exe"
else:
    venv_python = PLUGIN_DIR / ".venv" / "bin" / "python"
```

The Unix-only fallback to `/usr/bin/python3` is removed — a missing venv means a broken installation, not something to silently route around.

---

### 5. `mcp_server/tools.py` — Browser Open

The inline platform check is replaced with a call to `open_browser(url)` from `platform_compat`. `import platform` and `import subprocess` are removed from `tools.py` — they are used only for the browser open block.

---

### 6. Testing

**No existing tests change behavior.** All 412 existing tests continue to pass.

**New: `tests/test_platform_compat.py`**

Unit tests for each function in `platform_compat.py`. Platform branches tested by mocking `sys.platform` — no real Windows machine required:

- `venv_python_path()` — correct suffix on `win32` vs posix
- `detached_popen_kwargs()` — `start_new_session` on posix, `creationflags` on win32
- `find_pid_on_port()` — mock `psutil.net_connections()`, assert PID extraction
- `terminate_process()` — mock `psutil.Process`, assert terminate→kill escalation sequence
- `open_browser()` — assert correct subprocess command per platform

**Updates to `tests/test_backend_restart.py`**

Patches targeting `mcp_server.backend.subprocess.run` and `mcp_server.backend.os.kill` are retargeted to `mcp_server.platform_compat`. Test assertions (graceful terminate, force-kill on timeout, `ProcessLookupError` suppressed) are preserved.

**New: `tests/test_command_resolution.py`**

- Bare name resolved correctly via mocked `shutil.which`
- Already-absolute Unix path passed through unchanged
- Already-absolute Windows path (`C:\...`) passed through unchanged
- `which()` returning `None` → 400 response with `command_not_found`

---

## Files Changed

| File | Change |
|------|--------|
| `.mcp.json` | `python3 bin/start_mcp.py` → `uv run python -m mcp_server` |
| `bin/start_mcp.py` | **Deleted** |
| `pyproject.toml` | Add `psutil>=5.9.0` to dependencies |
| `mcp_server/platform_compat.py` | **New file** — all platform abstractions |
| `mcp_server/backend.py` | Import from `platform_compat`; remove `signal` |
| `mcp_server/tools.py` | `open_browser()` via `platform_compat` |
| `mage_scheduler/db.py` | `venv_python_path()` via `platform_compat` |
| `mage_scheduler/api.py` | Command resolution via `shutil.which()` at intent/action endpoints |
| `tests/test_platform_compat.py` | **New file** — unit tests for compat module |
| `tests/test_command_resolution.py` | **New file** — command resolution unit tests |
| `tests/test_backend_restart.py` | Retarget patches to `platform_compat` |

---

## Non-Goals

- Shell syntax translation (bash → cmd.exe). Users on Windows schedule Windows commands; users on Unix schedule Unix commands. The plugin is a scheduler, not a shell abstraction layer.
- Automatic installation of missing commands.
- Backwards-compatibility shims for the old `bin/start_mcp.py` entry point.
