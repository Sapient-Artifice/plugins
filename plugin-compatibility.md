# Claude Code Plugin Compatibility

Mage Lab supports [Claude Code plugins](https://docs.anthropic.com/en/docs/claude-code) with near-full compatibility.

## Quick Start

Clone a compatible plugin into your Skills directory:

```bash
cd ~/Mage/Skills/
git clone https://github.com/obra/episodic-memory
```

Restart Mage Lab. The plugin will be discovered and available in **Settings > Skills & Plugins**.

## Supported Features

| Feature | Support | Notes |
|---------|---------|-------|
| Plugin manifest (`.claude-plugin/plugin.json`) | Full | |
| Commands (`commands/*.md`) | Full | Exposed as `/command` slash commands |
| Agents (`agents/*.md`) | Full | Spawnable as sub-agents |
| Hooks (`hooks/hooks.json`) | Full | `command` type only; string and dict matchers supported |
| Hook JSON stdin/stdout | Partial | Stdin fields fully supported; some universal stdout fields (`continue`, `systemMessage`, `suppressOutput`, `stopReason`) not supported |
| Hook decisions | Full | PreToolUse: `deny`, `allow`, `updatedInput`; PostToolUse: `block`, `additionalContext`; Stop: `block`; UserPromptSubmit: `block` |
| `hookSpecificOutput` envelope | Full | Both top-level and nested `hookSpecificOutput` format accepted |
| `once` hook field | Full | Hooks with `"once": true` run once per session. Applied to all hooks (Claude Code limits to skills only). |
| Exit code 2 blocking | Full | Non-zero exit code 2 treated as blocking error; stderr used as reason |
| MCP Servers (`.mcp.json`) | Partial | `stdio` transport only; no automatic dependency installation |
| Skills (`skills/*.md` or root `SKILL.md`) | Full | |

## Unsupported Features

- **SSE/HTTP MCP transport** — Only `stdio` supported
- **Non-command hook types** — Only `"type": "command"` supported; `"prompt"` and `"url"` types are skipped with a warning
- **`statusMessage` hook field** — Custom spinner messages not shown
- **`additionalContext` response field** — Supported for PostToolUse only (appends to tool output). Not implemented for other events (SessionStart, UserPromptSubmit, PreToolUse, Stop, Notification).
- **`continue: false` response field** — Universal stop field not implemented. Use event-specific `decision: "block"` instead.
- **`systemMessage` response field** — Hooks cannot inject system messages into the conversation
- **`stopReason` / `suppressOutput` response fields** — Universal response fields not implemented
- **`CLAUDE_ENV_FILE`** — SessionStart hooks cannot persist env vars via file. Mage Lab's tool execution model does not share a persistent environment across tool calls.
- **`updatedMCPToolOutput`** — PostToolUse field for replacing MCP tool output not implemented. Use `additionalContext` to append instead.
- **`agent_type` in SessionStart** — No equivalent concept in Mage Lab (Claude Code passes `--agent <name>`)
- **`agent_transcript_path` in SubagentStop** — Subagent transcripts are not stored as separate files
- **`permission_mode` values** — Only `"default"` and `"bypassPermissions"` supported. Claude Code modes `"plan"`, `"acceptEdits"`, `"dontAsk"` always map to `"default"`.
- **PostToolUse `tool_response` format** — Always a string containing raw tool output text, not a structured JSON object as in Claude Code. Plugins that parse `tool_response` as JSON will receive a string instead.
- **Notification types** — Mage Lab emits its own notification types (`error`, `hook`, `tool_error`). Claude Code types (`permission_prompt`, `idle_prompt`, `auth_success`, `elicitation_dialog`) are never emitted. Notification matchers using Claude Code types will not match.
- **SessionEnd `reason` values** — Only `"deactivated"` is emitted. Claude Code values (`clear`, `logout`, `prompt_input_exit`, `bypass_permissions_disabled`, `other`) are not used.

## Unsupported Events

These Claude Code events are accepted in `hooks.json` without error but are never fired:

| Event | Reason |
|-------|--------|
| `PreCompact` | No context compaction in Mage Lab |
| `PostToolUseFailure` | Tool errors go through PostToolUse (error text appears in `tool_response`) |
| `PermissionRequest` | No permission dialog hook point |
| `SubagentStart` | Not implemented |
| `TeammateIdle` | No agent team support |
| `TaskCompleted` | No task system hook point |

## Hook Events

Claude Code hook events are used directly — no translation layer:

| Event | Support | When |
|-------|---------|------|
| `SessionStart` | Yes | Plugin activates |
| `SessionEnd` | Yes | Plugin deactivates or session closes |
| `PreToolUse` | Yes | Before a tool executes (can deny, allow, or modify input) |
| `PostToolUse` | Yes | After a tool executes (can block or append to output) |
| `UserPromptSubmit` | Yes | Before processing a user message (can block; reason shown to user) |
| `Stop` | Yes | Agent turn ends (always fires; `stop_hook_active` lets hook decide) |
| `Notification` | Yes | When a notification is sent to the user |
| `SubagentStop` | Yes | When a background subagent completes |

**Mage Lab extension events** (desktop only): `AppFocus`, `AppBlur`, `Idle`, `HealthCheck`.

## Hook JSON Protocol

Hooks receive a JSON object on **stdin** with event-specific fields. All events include these common fields:

| Field | Description |
|-------|-------------|
| `hook_event_name` | Event name (e.g., `"PreToolUse"`) |
| `cwd` | Current working directory (POSIX path) |
| `session_id` | Current session identifier |
| `transcript_path` | Path to conversation JSON file |
| `permission_mode` | Permission mode: `"default"` or `"bypassPermissions"` only (Claude Code modes `"plan"`, `"acceptEdits"`, `"dontAsk"` not supported) |

**Environment variables** available to all hook scripts:

| Variable | Description |
|----------|-------------|
| `SKILL_PATH` | Absolute path to plugin directory |
| `CLAUDE_PLUGIN_ROOT` | Absolute path to plugin directory (Claude Code name) |
| `CLAUDE_PROJECT_DIR` | Current working directory |

Additional event-specific env vars are documented in [SKILLS.md](../docs/SKILLS.md).

If a hook writes valid JSON to **stdout**, the response is parsed and acted on. Both top-level fields and the `hookSpecificOutput` envelope are supported.

**Exit codes:** Exit 0 = success (parse JSON from stdout). Exit 2 = blocking error (stderr used as reason, stdout ignored). Other non-zero = non-blocking error (logged and continued). **Note:** PostToolUse cannot block — exit code 2 appends stderr as feedback to the tool result instead of replacing it.

### PreToolUse stdin

```json
{
  "hook_event_name": "PreToolUse",
  "cwd": "/path/to/project",
  "session_id": "abc123",
  "transcript_path": "/path/to/chat_abc123.json",
  "permission_mode": "default",
  "tool_name": "run_bash",
  "tool_input": { "script": "rm -rf /tmp/cache" },
  "tool_use_id": "call_abc123"
}
```

### PreToolUse stdout (optional)

Deny a tool call:

```json
{ "decision": "deny", "reason": "Destructive command blocked" }
```

Or using the `hookSpecificOutput` envelope (Claude Code standard format):

```json
{
  "hookSpecificOutput": {
    "permissionDecision": "deny",
    "permissionDecisionReason": "Destructive command blocked"
  }
}
```

Allow a tool call (bypasses user confirmation):

```json
{ "hookSpecificOutput": { "permissionDecision": "allow" } }
```

Modify the input:

```json
{ "updatedInput": { "script": "echo 'modified by hook'" } }
```

**Decision precedence:** `deny` always wins over `allow` regardless of hook ordering.

### PostToolUse stdin

```json
{
  "hook_event_name": "PostToolUse",
  "cwd": "/path/to/project",
  "session_id": "abc123",
  "transcript_path": "/path/to/chat_abc123.json",
  "permission_mode": "default",
  "tool_name": "read_file",
  "tool_input": { "file_path": "/etc/hosts" },
  "tool_response": "127.0.0.1 localhost\n...",
  "tool_response_truncated": false,
  "tool_use_id": "call_abc123"
}
```

### PostToolUse stdout (optional)

Block the tool output (replaces result shown to LLM):

```json
{ "decision": "block", "reason": "Sensitive file contents redacted" }
```

Or append context to the output (Claude Code standard):

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "Additional information for the LLM"
  }
}
```

Note: PostToolUse uses **top-level** `decision` and `reason` fields (not inside `hookSpecificOutput`). Tool errors from failed tool calls appear in the `tool_response` field (there is no separate `PostToolUseFailure` event).

### Stop stdin

```json
{
  "hook_event_name": "Stop",
  "cwd": "/path/to/project",
  "session_id": "abc123",
  "transcript_path": "/path/to/chat_abc123.json",
  "permission_mode": "default",
  "stop_hook_active": false,
  "last_assistant_message": "Here is the result..."
}
```

### Stop stdout (optional)

Block the stop to force the agent to continue:

```json
{ "decision": "block", "reason": "Task not complete, continue working" }
```

Stop hooks **always fire**, even after a previous block. The `stop_hook_active` field is `true` when the agent is continuing from a prior Stop hook block. Hooks should check this to avoid infinite loops.

### Other events

| Event | Additional stdin fields |
|-------|-------------|
| `UserPromptSubmit` | `prompt` |
| `Notification` | `title`, `message`, `notification_type` |
| `SubagentStop` | `agent_id`, `agent_type`, `status`, `stop_hook_active`, `last_assistant_message`, `output_truncated`, `elapsed_seconds`, `tool_calls_made`, `llm_calls_made`, `error` |
| `SessionStart` | `source` (`"startup"` or `"resume"`), `model` |
| `SessionEnd` | `reason` (`"deactivated"`) |

### SubagentStop fields

SubagentStop uses Claude Code field names for plugin compatibility:

| Field | Description |
|-------|-------------|
| `agent_id` | Subagent task identifier |
| `agent_type` | Subagent task name |
| `status` | Completion status (`completed`, `failed`, `cancelled`) |
| `stop_hook_active` | Always `false` (reserved for future use) |
| `last_assistant_message` | Final output (truncated to 5000 chars) |
| `output_truncated` | `true` if output was truncated |
| `elapsed_seconds` | Task duration |
| `tool_calls_made` | Number of tool calls |
| `llm_calls_made` | Number of LLM calls |
| `error` | Error message if failed |

Note: `agent_transcript_path` from the Claude Code spec is omitted (subagent transcripts are not stored as separate files).

## Hook Matcher Semantics

String matchers filter hooks by event-specific fields, matching the Claude Code spec:

| Event | Matcher filters on | Example matchers |
|-------|-------------------|-----------------|
| `PreToolUse` | Tool name | `run_bash`, `Edit\|Write`, `mcp__.*` |
| `PostToolUse` | Tool name | `run_bash`, `read_file` |
| `Notification` | Notification type | `error`, `hook`, `tool_error` (Mage Lab values; differs from Claude Code) |
| `SessionStart` | Session source | `startup`, `resume` |
| `SessionEnd` | End reason | `deactivated` (Mage Lab value; Claude Code uses `clear`, `logout`, etc.) |
| `SubagentStop` | Agent type | Task name |
| `Stop` | *(no matcher support)* | Always fires |
| `UserPromptSubmit` | *(no matcher support)* | Always fires |

String matchers use `re.fullmatch()` (anchored regex). The special matcher `"*"` matches any value.

Dict matchers are a **Mage Lab extension** for key-value filtering against environment variables (e.g., `{"activationType": "llm"}`). They are not applied to `Stop` or `UserPromptSubmit` events.

## Plugin Namespace

Plugins are assigned the `plugin` namespace to distinguish them from native skills:

- Plugin: `plugin:episodic-memory`
- Native skill: `my-custom-skill`

## Tool Name Mapping

Plugin allowed-tools use [agentskills.io](https://agentskills.io/) names, mapped to Mage Lab tools:

| Plugin Tool | Mage Lab Tool(s) |
|-------------|-----------------|
| `Read` | `read_file` |
| `Write` | `write_file` |
| `Edit` | `write_file` |
| `Bash` | `run_bash` |
| `Glob` | `search_files`, `search_folders` |
| `Grep` | `search_files` |
| `WebFetch` | `read_website` |
| `WebSearch` | `search_web`, `search_images` |
| `Python` | `run_python` |

## Mage Lab Extensions

These features go beyond the Claude Code plugin spec:

| Extension | Description |
|-----------|-------------|
| `SKILL.md` in plugin root | agentskills.io body instructions injected into LLM context |
| `scripts/` auto-discovery | Shell scripts registered as callable LLM tools |
| Desktop lifecycle events | `AppFocus`, `AppBlur`, `Idle`, `HealthCheck` hook events |
| Dict matchers | Key-value filtering against environment variables on hook events |

## Troubleshooting

### Plugin not discovered

1. Check that `.claude-plugin/plugin.json` exists and is valid JSON
2. Verify the plugin directory is inside a skills discovery path (`~/Mage/Skills/` or `SKILLS_PATH`)
3. Check backend logs for parsing errors

### Commands not working

1. Ensure the plugin is **activated** first
2. Type `/command-name` in chat (with the `/` prefix)
3. Check that `commands/*.md` files have valid frontmatter

### Hooks not running

1. Verify `hooks/hooks.json` is valid JSON with the correct nested structure
2. Only `"type": "command"` hooks are supported
3. Commands are resolved relative to the plugin directory

### MCP server not connecting

1. Verify `.mcp.json` is valid JSON
2. Check that the server command is installed and works in terminal
3. Ensure `MCP_ENABLED=true` in configuration
4. **Dependencies are not auto-installed.** Use self-installing commands (`npx -y`, `uvx`) or install dependencies manually before activating the plugin. `package.json` and `requirements.txt` are not processed automatically.
