# Mage Scheduler Plugin

A task scheduler plugin for [Mage Lab](https://magelab.ai). Schedule one-off commands, set up cron-driven recurring tasks, chain tasks with dependency graphs, and get completion notifications — all without any external services.

Drop the directory into `~/Mage/Skills/` and it works.

---

## Features

- **Zero external dependencies** — APScheduler runs in-process.
- **One-off tasks** — schedule a command to run at a specific time or after a delay (`run_at`, `run_in`).
- **Recurring tasks** — cron-driven schedules (`0 9 * * 1` = every Monday at 9am) with per-timezone support.
- **Dependency chains** — `depends_on: [task_id, ...]` holds a task as `waiting` until its upstream tasks complete or cascade-fail it.
- **Actions** — reusable, vetted command templates. Register once, schedule many times. Restrict allowed env keys and working directories per action.
- **Retries** — configurable `max_retries` and `retry_delay` per task or action.
- **Completion notifications** — opt-in per task; posts a structured result back to the assistant when the task finishes.
- **Missed-task recovery** — a run missed while the backend was offline (or an ack-required message nobody received) is surfaced *loudly* — on the assistant **and** a native OS notification — and *durably*, re-nudging or re-delivering per a configurable policy until you handle it. Never silently lost.
- **Receipt acknowledgment** — ack-required deliveries (e.g. `ask_assistant`) count as done only once a live assistant confirms receipt; otherwise they park as missed for re-delivery.
- **Auto-cleanup** — configurable retention policy deletes old terminal tasks automatically.
- **Web dashboard** — Jinja2-rendered HTML UI at `http://127.0.0.1:8012` for task/action/settings management.
- **25 MCP tools** — full scheduling, inspection, and management surface exposed to the LLM via MCP stdio.

- **`/scheduler` slash command** — natural language scheduling or dashboard access in one keystroke.

---

## Installation

```bash
cp -r mage-scheduler-plugin ~/Mage/Skills/
```

Then activate it from **Settings → Skills & Plugins**. The plugin activates the next time Mage Lab starts (or when you reload plugins). The backend server starts on first use and persists between sessions — scheduled tasks continue firing even when mage lab is closed.

### Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (used to run the plugin in an isolated venv)

All Python dependencies are managed by `pyproject.toml` and installed automatically by `uv run`.

---

## How It Works

```
Mage Lab
    │
    ├── MCP stdio  ──►  mcp_server/__main__.py
    │                        │ starts (if not running)
    │                        ▼
    │                   uvicorn → mage_scheduler/api.py  (FastAPI)
    │                                   │
    │                                   ├── APScheduler (in-process)
    │                                   │     ├── beat_recurring  (every 60s)
    │                                   │     ├── beat_dependency (every 60s)
    │                                   │     ├── beat_reconcile  (every 60s + startup)
    │                                   │     └── beat_cleanup    (every 24h)
    │                                   │
    │                                   └── SQLite  (~/.mage_scheduler/scheduler.db)
    │
    └── /scheduler  ──►  commands/scheduler.md  (slash command)
```

**MCP server startup:** When mage lab activates the plugin, `mcp_server/__main__.py` delegates to `mcp_server/backend.py` to check if the FastAPI backend is already running on the configured port. If not, `backend.py` spawns a `uvicorn` subprocess with `start_new_session=True` (detached from the MCP process), waits up to 15 seconds for it to become healthy, then the MCP stdio server starts. On subsequent activations the backend is already running and the health check passes immediately. The `scheduler_restart_backend` MCP tool uses the same `backend.py` logic to kill and respawn the backend on demand.

**Task execution:** Each task is stored as a `TaskRequest` row with `status = "scheduled"`. APScheduler fires `run_command(task_id, command)` at the scheduled time. The job reads the row, runs the command as a subprocess, writes stdout/stderr back to the row, and updates the status to `success` or `failed`.

**Durability & missed tasks:** APScheduler's job store is in-memory, so one-off `date` jobs do not survive a backend restart. The `SQLite` `TaskRequest` rows are the durable record; `beat_reconcile` (run once at startup and every 60s thereafter) reconciles them against the live scheduler. On startup it **rehydrates** the jobs for tasks still due in the future, so a plain restart never drops them. A task whose `run_at` passed while the backend was offline (overdue by more than the configurable grace window) is a **missed** task: depending on the global `missed_task_policy` it is parked in the `missed` status for a run/skip decision (`always_ask`, the default), re-dispatched (`auto_run`), or cancelled (`auto_skip`). Recurring occurrences that come due while offline follow the same policy instead of firing late. When tasks are missed, the scheduler alerts you on both the assistant and a native OS notification, and keeps nudging (or re-delivering) until they're resolved. See [Missed Tasks](#missed-tasks).

---

## Core Concepts

### Actions

An **Action** is a named, reusable command template stored in the database.

```
Action: "backup_home"
  command:         /usr/local/bin/backup.sh
  default_cwd:     /home/user
  allowed_env:     ["DEST_PATH", "COMPRESSION"]
  max_retries:     2
  retry_delay:     300
```

Actions act as a security boundary: only whitelisted env keys can be passed in at schedule time, and allowed directory restrictions can be set per action. Create an action once; schedule it many times without repeating the command path.

### Tasks

A **Task** (`TaskRequest`) is a single scheduled execution. Fields:

| Field | Description |
|---|---|
| `id` | Integer primary key |
| `description` | Human-readable label |
| `command` | Shell command to run |
| `run_at` | UTC datetime to execute |
| `status` | `scheduled` → `running` → `success` / `failed` / `cancelled`; a run missed while offline becomes `missed`; a delivery awaiting receipt confirmation is `awaiting_ack` |
| `job_id` | APScheduler job ID (used for cancellation) |
| `result` | Captured stdout (last 16000 chars retained if longer) |
| `error` | Captured stderr or failure reason |
| `action_name` | Source action if scheduled via an action |
| `env_json` | JSON dict of env vars injected into the subprocess |
| `cwd` | Working directory override |
| `notify_on_complete` | Post completion notification to assistant |
| `max_retries` / `retry_count` / `retry_delay` | Retry configuration |
| `retain_result` | Exempt from automatic cleanup |
| `recurring_task_id` | Link back to the parent `RecurringTask` |

### Recurring Tasks

A **RecurringTask** holds a cron schedule and spawns a new `TaskRequest` each time it fires. The beat job (`check_recurring_tasks`) runs every 60 seconds, finds tasks whose `next_run_at <= now`, spawns the task, and advances `next_run_at` to the next occurrence.

```
RecurringTask: "weekly_report"
  cron:      0 9 * * 1          ← Monday 09:00
  timezone:  America/New_York
  action:    generate_report
  enabled:   true
  next_run_at: 2026-03-16T14:00:00Z
```

### Dependency Chains

Set `depends_on: [task_id, ...]` to hold a task as `waiting` until all upstream tasks complete:

- If all upstream tasks reach `success` → the waiting task is scheduled.
- If any upstream task reaches `failed`, `cancelled`, or `blocked` → the waiting task is cascade-failed.
- A `cancelled` parent propagates `failed` (not `cancelled`) to dependents, so the error is surfaced.

The dependency beat job (`check_waiting_tasks`) re-evaluates all waiting tasks every 60 seconds.

### Missed Tasks

A scheduled run can be missed two ways: the backend was **offline** (machine asleep, app closed, crash) when its `run_at` arrived, or the task was **delivered but never received** — an ack-required message (see [Receipt Acknowledgment](#receipt-acknowledgment)) whose confirmation window closed because nobody was there. Either way the run is surfaced *loudly and durably* instead of being silently lost.

**What counts as missed:** a `scheduled` task with no live scheduler job, overdue by more than `missed_grace_seconds` (default 300s) — a shorter delay is treated as effectively on-time and simply runs (covering the 60s beat latency and brief sleeps) — or an ack-required task whose confirmation window closed.

**Two notification channels.** When a task is missed, the scheduler alerts you on **both** the assistant (via `ask_assistant`) **and** a native OS notification (macOS / Linux / Windows) — so a run that misses at 5 a.m. isn't lost just because the assistant channel happened to be down.

**What happens** is governed by the global `missed_task_policy` setting, and it is applied *continuously* (every 60s beat), not just once — so a parked task keeps knocking until it's handled:

- `always_ask` (default) — the task is parked in the `missed` status and **re-nudges** you on a throttle (~30 min) until you run or skip it.
- `auto_run` — the task is re-dispatched. A deterministic command runs immediately; an **interactive** (ack-required) task **re-delivers on a backoff (~15 min) until an assistant actually acknowledges it**, so it never records a false `success`.
- `auto_skip` — the task is cancelled and its dependents cascade-fail; the notification names the dependent tasks it also cancelled.

Recurring occurrences that come due while offline follow the same policy rather than firing late. A newer occurrence of a recurring task **supersedes** any still-unresolved earlier one, so a job that keeps missing can't stack up.

**Resolving a parked (`missed`) task** — from the dashboard, the assistant, or the API:

- **Dashboard** — the **"Needs Your Decision"** block lists every parked task with **Run now** / **Skip** buttons.
- `scheduler_list_missed` — list everything awaiting a decision.
- `scheduler_resolve_missed(task_id, "run")` — run it now (preserves the task ID and any dependents).
- `scheduler_resolve_missed(task_id, "skip")` — cancel it and free the queue.

`scheduler_status` also reports a `missed_task_count` as a safety net in case a notification is dropped. Configure the policy, grace window, and ack timeout under [Settings](#settings).

### Receipt Acknowledgment

Some deliveries succeed at the transport layer without actually being received. The clearest example is the built-in **`ask_assistant`** action: its HTTP `200` only means the message was *queued to the frontend*, not that a live assistant received or acted on it. A task that fires while the machine is asleep (or the UI is idle) would otherwise be recorded as a silent `success`.

To close that gap, an action or task can set **`require_ack`** (on by default for `ask_assistant`). For such tasks:

1. Delivery moves the task to **`awaiting_ack`** — not `success` — with a one-time token and a deadline (`ack_timeout_seconds`, default 900s).
2. The delivered message instructs the receiving assistant to confirm receipt by calling **`scheduler_ack_task(task_id, token)`**. A valid ack marks the task `success` and releases any dependents.
3. If the ack window passes with no confirmation, the task is parked as **`missed`** — so it rides the missed-task flow above and can be re-delivered when someone is actually available (e.g. after the machine wakes). A late ack with a matching token is still accepted.
4. If delivery can't happen at all (no frontend connected → `503`, or the app is unreachable), the task is parked as `missed` immediately rather than recorded as failed.

This keeps all logic in the plugin — no dependence on the app reporting connection state — and measures the thing that matters: whether an assistant actually received the message.

### Settings

Global settings live in the `settings` table and are editable from the dashboard **Settings** page or via `GET` / `PUT` `/api/settings`:

| Setting | Default | Description |
|---|---|---|
| `missed_task_policy` | `always_ask` | How a missed run is handled — `always_ask`, `auto_run`, or `auto_skip` (see [Missed Tasks](#missed-tasks)). |
| `missed_grace_seconds` | `300` | A run overdue by less than this is treated as on-time and simply runs; beyond it, the missed-task policy applies. |
| `ack_timeout_seconds` | `900` | How long an ack-required task waits in `awaiting_ack` for a receipt confirmation before it is parked as `missed`. |
| `cleanup_enabled` | `false` | Whether the daily cleanup beat auto-deletes old terminal tasks. |
| `task_retention_days` | `30` | Age after which terminal tasks become eligible for cleanup (when enabled). |
| allowed command / cwd dirs | (unset) | Optional allow-lists restricting where scheduled commands and working directories may live. |

---

## Dashboard

A Jinja2-rendered web UI at `http://127.0.0.1:8012` for managing tasks, actions, recurring schedules, and settings. Highlights:

- **"Needs Your Decision"** block — parked (`missed`) tasks with one-click **Run now** / **Skip**.
- **Auto-refresh** every 10 s (paused while you're mid-edit) so state stays live; served with `Cache-Control: no-store`.
- Create one-off or recurring tasks from a form, browse history, and edit settings.

**Runs as an in-app Mage Lab tab by default.** That tab is a cross-origin sandbox — native form navigation works, but `fetch`/XHR to the backend and native `confirm()` / `alert()` modals are blocked. The dashboard is built to that constraint: buttons submit via plain form navigation (no XHR), and destructive actions (Skip, Delete) use a **two-click "click again to confirm"** instead of a modal. Set `SCHEDULER_DASHBOARD_IN_BROWSER=1` to open it in your system browser instead.

---

## MCP Tools

All 25 tools are available via the `scheduler` MCP server. The naming convention is `scheduler_<action>`.

### Orientation
| Tool | Description |
|---|---|
| `scheduler_context` | Bootstrap call: service status, all actions, recent tasks, counts, validation rules |
| `scheduler_status` | Lightweight liveness check |

### Scheduling
| Tool | Description |
|---|---|
| `scheduler_schedule_intent(intent_json)` | Primary scheduling tool — one-off, recurring, and chained tasks |
| `scheduler_preview_intent(intent_json)` | Validate and preview timing without creating anything |
| `scheduler_run_now(task_json)` | Dispatch a command for immediate execution |

### Task Inspection & Management
| Tool | Description |
|---|---|
| `scheduler_list_tasks(limit, status)` | List tasks; filter by status e.g. `"scheduled,running"` |
| `scheduler_get_task(task_id)` | Full task detail: output, error, retry count, dependencies |
| `scheduler_get_dependencies(task_id)` | Dependency graph: `depends_on` + `blocking` lists |
| `scheduler_cancel_task(task_id)` | Cancel a scheduled/running/waiting task |
| `scheduler_list_missed` | List tasks parked in the `missed` state awaiting a decision |
| `scheduler_resolve_missed(task_id, action)` | Resolve a missed task — `action` is `"run"` or `"skip"` |
| `scheduler_ack_task(task_id, token)` | Confirm receipt of an ack-required scheduled message (marks it `success`) |
| `scheduler_cleanup` | Delete all terminal tasks now |

### Recurring Tasks
| Tool | Description |
|---|---|
| `scheduler_list_recurring` | List all recurring tasks with schedule and next run |
| `scheduler_toggle_recurring(recurring_id)` | Enable or disable a recurring task |
| `scheduler_delete_recurring(recurring_id)` | Permanently delete a recurring task |

### Actions
| Tool | Description |
|---|---|
| `scheduler_list_actions` | List all registered actions |
| `scheduler_create_action(action_json)` | Register a new action |
| `scheduler_update_action(action_id, action_json)` | Update an action |
| `scheduler_delete_action(action_id)` | Delete an action |

### Validation & Dashboard
| Tool | Description |
|---|---|
| `scheduler_get_validation` | Get allowed command/cwd directories |
| `scheduler_open_dashboard` | Open task dashboard in browser |
| `scheduler_open_actions` | Open actions management page |
| `scheduler_open_settings` | Open settings page |

### Backend Management
| Tool | Description |
|---|---|
| `scheduler_restart_backend` | Kill the running backend (if any) and start a fresh one; waits up to 15 s for readiness |

---

## Slash Command

Type `/scheduler` in mage lab to open the dashboard or schedule from natural language:

```
/scheduler open                          → opens dashboard
/scheduler status                        → service health + recent tasks
/scheduler remind me to run the backup in 2 hours
/scheduler run echo hello every weekday at 9am Pacific
```

---

## Intent Schema (v1)

All scheduling goes through the `POST /api/tasks/intent` endpoint (or the `scheduler_schedule_intent` MCP tool), which accepts a structured intent object:

```json
{
  "intent_version": "v1",
  "task": {
    "description": "Weekly database backup",
    "action_name": "backup_db",
    "command": "/usr/local/bin/backup.sh",
    "run_at": "2026-03-10T18:00:00",
    "run_in": "2h",
    "cron": "0 2 * * 0",
    "timezone": "America/New_York",
    "cwd": "/var/backups",
    "env": { "DEST": "/mnt/nas/backups" },
    "depends_on": [42, 43],
    "notify_on_complete": true,
    "max_retries": 2,
    "retry_delay": 300,
    "retain_result": false,
    "replace_existing": false
  },
  "replace_existing": false,
  "meta": {
    "source": "mage-lab-llm",
    "user_confirmed": true
  }
}
```

**Scheduling rules:**
- Use `action_name` when a matching action exists; fall back to `command` for ad-hoc tasks.
- Provide exactly one of `run_at`, `run_in`, or `cron`. Do not combine them.
- `run_in` accepts natural duration strings: `"30m"`, `"2h"`, `"1d"`, `"1w"`.
- `command` accepts bare names (`python3`, `ffmpeg`) or absolute paths. Bare names are resolved via `PATH` at schedule time and stored as absolute paths. If the name is not found on `PATH`, the request is blocked with `command_not_found`.
- `env` is only allowed when `action_name` is set, and keys must be whitelisted by the action.
- `timezone` defaults to the server's local system timezone (set `SCHEDULER_TIMEZONE=America/New_York` to override). Affects cron scheduling and response display only — storage is always UTC.
- `cron` creates a `RecurringTask`. Incompatible with `run_at`, `run_in`, and `depends_on`.
- `replace_existing: true` cancels any existing `scheduled` or `waiting` tasks with the same description before creating the new one.

**Response statuses:**
- `"scheduled"` — task created successfully.
- `"recurring_scheduled"` — recurring task registered.
- `"blocked"` — validation failed; see `error` or `errors[]` for codes.

---

## Common Patterns

### Schedule a one-off task in 2 hours

```json
{
  "intent_version": "v1",
  "task": {
    "description": "Run database vacuum",
    "action_name": "db_vacuum",
    "run_in": "2h"
  }
}
```

### Set up a cron job in a specific timezone

```json
{
  "intent_version": "v1",
  "task": {
    "description": "Morning sync",
    "action_name": "sync_files",
    "cron": "0 8 * * 1-5",
    "timezone": "Europe/London"
  }
}
```

### Chain tasks — run step 2 after step 1

```json
{
  "intent_version": "v1",
  "task": {
    "description": "Deploy step 2",
    "action_name": "deploy_backend",
    "run_in": "5m",
    "depends_on": [101]
  }
}
```

### Ask the assistant to follow up after a long task

```json
{
  "intent_version": "v1",
  "task": {
    "description": "Post-training review",
    "action_name": "ask_assistant",
    "env": { "MESSAGE": "Training finished. Please review the validation metrics." },
    "run_in": "4h",
    "notify_on_complete": true
  }
}
```

### Replace the previous backup job with a new one

```json
{
  "intent_version": "v1",
  "task": {
    "description": "nightly backup",
    "action_name": "backup_home",
    "cron": "0 3 * * *"
  },
  "replace_existing": true
}
```

---

## Environment Variables

Set in `.claude-plugin/plugin.json` under `mcpServers.env`, or export before starting.

| Variable | Default | Description |
|---|---|---|
| `SCHEDULER_DATA_DIR` | `~/.mage_scheduler` | Directory for the SQLite database and log file |
| `SCHEDULER_PORT` | `8012` | Port the FastAPI backend listens on |
| `SCHEDULER_HOST` | `127.0.0.1` | Bind address for the FastAPI backend |
| `SCHEDULER_TIMEZONE` | system local tz | Default timezone for scheduling (IANA name, e.g. `America/New_York`). Auto-detected on macOS/Linux (from `TZ` or `/etc/localtime`); set explicitly on Windows. |
| `SCHEDULER_DASHBOARD_IN_BROWSER` | `0` | Open the dashboard in the system browser instead of the default in-app Mage Lab tab (see [Dashboard](#dashboard)). |
| `MAGE_ASK_ASSISTANT_URL` | `http://127.0.0.1:11115/ask_assistant` | Endpoint the scheduler posts assistant notifications and ack-required messages to. |

The backend log is written to `$SCHEDULER_DATA_DIR/scheduler.log`. If the backend fails to start, check there first.

---

## REST API

The FastAPI backend is also directly accessible. Base URL: `http://127.0.0.1:8012`.

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check; returns `{"status":"ok","uptime_seconds":N}` |
| `GET` | `/api/tasks` | List tasks (`?status=scheduled,running` to filter) |
| `GET` | `/api/tasks/{id}` | Get a single task |
| `POST` | `/api/tasks` | Create a task directly (JSON `TaskCreate` body) |
| `POST` | `/api/tasks/run_now` | Schedule a task for immediate execution |
| `POST` | `/api/tasks/intent` | Schedule via intent object (recommended) |
| `POST` | `/api/tasks/{id}/cancel` | Cancel a task |
| `GET` | `/api/tasks/missed` | List tasks parked in the `missed` state |
| `POST` | `/api/tasks/{id}/resolve_missed` | Resolve a missed task (JSON body `{"action":"run"\|"skip"}`) |
| `POST` | `/api/tasks/{id}/ack` | Confirm receipt of an ack-required task (JSON body `{"token":"..."}`) |
| `GET` | `/api/tasks/{id}/dependencies` | Get dependency graph for a task |
| `GET` | `/api/tasks/stats` | Count tasks by status |
| `POST` | `/api/tasks/cleanup` | Manually trigger cleanup |
| `GET` | `/api/parse` | Parse a natural language string to an intent |
| `GET` | `/api/actions` | List actions |
| `POST` | `/api/actions` | Create an action |
| `PUT` | `/api/actions/{id}` | Update an action |
| `DELETE` | `/api/actions/{id}` | Delete an action |
| `GET` | `/api/recurring` | List recurring tasks |
| `POST` | `/api/recurring` | Create a recurring task directly |
| `PUT` | `/api/recurring/{id}` | Update a recurring task |
| `DELETE` | `/api/recurring/{id}` | Delete a recurring task |
| `POST` | `/api/recurring/{id}/toggle` | Enable/disable a recurring task |
| `GET` | `/api/settings` | Get global settings |
| `PUT` | `/api/settings` | Update global settings |
| `GET` | `/api/validation` | Get allowed command/cwd directory rules |

Interactive docs are available at `http://127.0.0.1:8012/docs`.

---

## Data Storage

All data lives in `$SCHEDULER_DATA_DIR` (default `~/.mage_scheduler`):

```
~/.mage_scheduler/
├── scheduler.db      ← SQLite database
└── scheduler.log     ← Backend stdout/stderr
```

**Tables:**

| Table | Description |
|---|---|
| `task_requests` | All scheduled and historical tasks |
| `actions` | Registered action templates |
| `recurring_tasks` | Cron schedules |
| `task_dependencies` | Dependency edges between tasks |
| `settings` | Global configuration (allowed dirs, cleanup policy) |

The database is created automatically on first run. No migrations are required for the current schema version.

---

## Subprocess Environment

Every task subprocess receives these environment variables in addition to the system environment:

| Variable | Value |
|---|---|
| `SCHEDULER_TASK_ID` | Integer task ID |
| `SCHEDULER_TRIGGERED_AT` | ISO 8601 UTC timestamp of when the job fired |
| `SCHEDULER_ACTION_NAME` | Action name, or empty string |

Plus any keys from the task's `env_json`.

---

## Project Structure

```
mage_scheduler_plugin/
├── .claude-plugin/
│   └── plugin.json              ← Plugin manifest (MCP server declaration)
├── commands/
│   └── scheduler.md             ← /scheduler slash command
├── SKILL.md                     ← MCP tool reference for the LLM
├── pyproject.toml               ← Dependencies and pytest config
│
├── mage_scheduler/              ← FastAPI application (sys.path root)
│   ├── api.py                   ← FastAPI routes + lifespan handler
│   ├── task_manager.py          ← TaskManager class (intent → DB row + dispatch)
│   ├── scheduler.py             ← APScheduler singleton + beat job registration
│   ├── dispatch.py              ← schedule_command / cancel_command shim
│   ├── db.py                    ← SQLAlchemy engine + SessionLocal factory
│   ├── models.py                ← ORM models
│   ├── schemas.py               ← Pydantic request/response schemas
│   ├── nl_parser.py             ← Natural language → ParsedRequest
│   ├── notify.py                ← Assistant + OS-notification delivery (missed-task alerts)
│   ├── jobs/
│   │   ├── run_command.py       ← Task executor + dependency helpers + notify
│   │   ├── dependency_check.py  ← Beat job: unblock waiting tasks
│   │   ├── recurring_check.py   ← Beat job: spawn recurring task instances
│   │   ├── reconcile.py         ← Beat job: rehydrate + detect missed tasks
│   │   └── cleanup.py           ← Beat job: delete old terminal tasks
│   └── templates/               ← Jinja2 HTML templates (dashboard, actions, settings)
│
├── mcp_server/
│   ├── __main__.py              ← Entry point: start backend → serve MCP stdio
│   ├── backend.py               ← Backend process management (start, health-check, restart)
│   └── tools.py                 ← 25 FastMCP tool definitions (httpx → REST API)
│
└── tests/                       ← 32 files, 543 tests — in-memory SQLite, no backend needed
    ├── conftest.py              ← Pytest fixtures (in-memory DB, mocked scheduler)
    └── test_*.py                ← API + intent, recurring, dependencies, reconcile/missed,
                                    ack, notify, timezone, cleanup, cross-platform, dashboard
```

---

## Development

### Running Tests

```bash
cd mage_scheduler_plugin
uv run pytest tests/ -v
```

All 543 tests run in a few seconds against an in-memory SQLite database. No backend needs to be running.

### Test Architecture

Tests use two isolation strategies:

**`db_session` fixture** — a fresh in-memory SQLite session per test. Used for unit tests of functions that accept a session directly.

**Module-patching fixtures** — for functions that call `SessionLocal()` internally, the fixture monkeypatches the module attribute before the test:

```python
# conftest.py pattern
monkeypatch.setattr(jobs.run_command, "SessionLocal", Factory)
```

Available fixtures:
- `db_session` — bare SQLAlchemy session
- `nt_mem_db` — patches `jobs.run_command.SessionLocal`
- `dep_mem_db` — patches `jobs.dependency_check.SessionLocal`
- `rec_mem_db` — patches `jobs.recurring_check.SessionLocal`
- `cln_mem_db` — patches `jobs.cleanup.SessionLocal`
- `api_client` — full FastAPI `TestClient` with StaticPool shared DB; mocks APScheduler lifecycle and dispatch

APScheduler is never started during tests — `scheduler.start_scheduler` and `scheduler.stop_scheduler` are patched to no-ops, and `dispatch.schedule_command` returns a fake job ID string.

### Running the Backend Standalone

```bash
cd mage_scheduler_plugin/mage_scheduler
SCHEDULER_DATA_DIR=/tmp/sched_dev uv run uvicorn api:app --port 8012 --reload
```

The dashboard will be available at `http://127.0.0.1:8012`.

---

## Architecture Notes

### Backend Persistence

The uvicorn backend runs as a detached subprocess. It survives past the MCP server process and continues firing scheduled tasks between sessions. On next activation, the MCP server's health check detects it and skips the startup step.

### Platform Support

The plugin runs on Windows, macOS, and Linux. Platform-specific behaviour is isolated in `mcp_server/platform_compat.py`:

- **Process management** uses `psutil` — no Unix-only `ss` command or `SIGKILL`.
- **Detached subprocess** uses `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` on Windows, `start_new_session=True` on Unix.
- **Browser open** uses `cmd /c start` on Windows, `open` on macOS, `xdg-open` on Linux.
- **Venv path** resolves to `.venv\Scripts\python.exe` on Windows, `.venv/bin/python` on Unix.

Users on Windows schedule Windows commands; users on Unix schedule Unix commands. The plugin is a scheduler, not a shell abstraction layer.
