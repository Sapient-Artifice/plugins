#!/usr/bin/env python3
"""Send a scheduled message to the ask_assistant endpoint.

Reads the MESSAGE environment variable and POSTs it as JSON to
http://127.0.0.1:11115/ask_assistant with a structured disclosure header so
the receiving LLM can clearly identify this as an automated scheduler message.

Optional metadata env vars (injected automatically by the task runner):
    SCHEDULER_TASK_ID        - ID of the triggering task
    SCHEDULER_TRIGGERED_AT   - UTC ISO timestamp of execution
    SCHEDULER_ACTION_NAME    - Action name (default: ask_assistant)
    SCHEDULER_DESCRIPTION    - Task description

Usage (via action):
    env MESSAGE="your message here" python3 ask_assistant.py
"""
import json
import os
import sys
import urllib.request

# Exit codes are meaningful to the scheduler's run_command:
#   0 -> delivered (HTTP 200)
#   3 -> could not deliver because nobody is home (HTTP 503) or the app is
#        unreachable; deferrable — the scheduler parks/retries rather than failing
#   1 -> real error (bad config, unexpected HTTP error)
EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
EXIT_UNDELIVERABLE = 3

ENDPOINT = os.environ.get("MAGE_ASK_ASSISTANT_URL", "http://127.0.0.1:11115/ask_assistant")

message = os.environ.get("MESSAGE", "").strip()
if not message:
    print("ERROR: MESSAGE environment variable is required and must not be empty", file=sys.stderr)
    sys.exit(EXIT_CONFIG_ERROR)

task_id = os.environ.get("SCHEDULER_TASK_ID", "")
triggered_at = os.environ.get("SCHEDULER_TRIGGERED_AT", "")
action_name = os.environ.get("SCHEDULER_ACTION_NAME", "ask_assistant")
description = os.environ.get("SCHEDULER_DESCRIPTION", "")
ack_required = os.environ.get("SCHEDULER_ACK_REQUIRED", "") == "1"
ack_token = os.environ.get("SCHEDULER_ACK_TOKEN", "")

meta_parts = []
if task_id:
    meta_parts.append(f"Task ID: {task_id}")
meta_parts.append(f"Action: {action_name}")
if triggered_at:
    meta_parts.append(f"Triggered: {triggered_at}")

header_lines = ["[MAGE SCHEDULER — AUTOMATED MESSAGE]"]
if meta_parts:
    header_lines.append(" | ".join(meta_parts))
if description:
    header_lines.append(f"Description: {description}")

# When the scheduler requires a receipt acknowledgment, instruct the receiving
# assistant to confirm first. Without this ack the task will not be marked
# successful — it is parked as 'missed' so it can be re-delivered when someone
# is actually available (e.g. after the machine wakes).
if ack_required and task_id and ack_token:
    header_lines.append(
        f"ACTION REQUIRED — confirm receipt before anything else by calling the "
        f"MCP tool scheduler_ack_task(task_id={task_id}, token=\"{ack_token}\"). "
        f"If you do not, this scheduled task will be recorded as not received."
    )
header_lines.append("---")

full_message = "\n".join(header_lines) + "\n" + message

payload = json.dumps({"message": full_message}).encode()
req = urllib.request.Request(
    ENDPOINT,
    data=payload,
    headers={"Content-Type": "application/json"},
)

try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode()
        print(body)
except urllib.error.HTTPError as exc:
    body = exc.read().decode()
    print(f"ERROR: HTTP {exc.code} from ask_assistant: {body}", file=sys.stderr)
    # 503 = "No active frontend websocket connection" — nobody is home to
    # receive it. Deferrable rather than a hard failure.
    sys.exit(EXIT_UNDELIVERABLE if exc.code == 503 else EXIT_CONFIG_ERROR)
except urllib.error.URLError as exc:
    # App backend unreachable (e.g. Mage not running) — also deferrable.
    print(f"ERROR: {exc}", file=sys.stderr)
    sys.exit(EXIT_UNDELIVERABLE)
except Exception as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    sys.exit(EXIT_CONFIG_ERROR)
