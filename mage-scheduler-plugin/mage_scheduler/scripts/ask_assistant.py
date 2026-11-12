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
#   3 -> could not deliver: nobody is home (HTTP 503), the app is unreachable,
#        or the app refused to start the turn (HTTP 400 session_turn_rejected,
#        e.g. it could not renew the gateway sign-in while the screen was
#        locked); deferrable — the scheduler parks/retries rather than failing
#   4 -> sent, unconfirmed: the app took the request but had not answered when
#        the wait ran out. Mage 0.13+ answers only after it has STARTED the chat
#        turn, which can take a while; the receipt (ack) decides whether it landed.
#   1 -> real error (bad config, unexpected HTTP error)
EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
EXIT_UNDELIVERABLE = 3
EXIT_UNCONFIRMED = 4


def is_turn_rejection(body: str) -> bool:
    """True if the app refused to START the turn (Mage 0.13+ ``session_turn_rejected``).

    Since 0.13 the app admits the chat turn before answering /ask_assistant, and
    answers HTTP 400 with ``{"detail": {"type": "session_turn_rejected", ...}}``
    when admission fails — most often ``code=gateway_authentication`` because it
    could not renew the gateway sign-in (the desktop UI must answer a refresh
    request, which it does not do while the screen is locked). The message
    never entered a turn, so nobody received it: that is undeliverable, not a
    configuration error, and the scheduler should park and re-deliver it.
    """
    try:
        detail = json.loads(body).get("detail")
    except (ValueError, AttributeError):
        return False
    return isinstance(detail, dict) and detail.get("type") == "session_turn_rejected"


ENDPOINT = os.environ.get("MAGE_ASK_ASSISTANT_URL", "http://127.0.0.1:11115/ask_assistant")
# How long to wait for the app's answer. It was 10 s when the app only queued
# the message; since 0.13 it starts the turn (session, MCP servers) first.
TIMEOUT = float(os.environ.get("MAGE_ASK_ASSISTANT_TIMEOUT", "60"))

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
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        body = resp.read().decode()
        print(body)
except urllib.error.HTTPError as exc:
    body = exc.read().decode()
    print(f"ERROR: HTTP {exc.code} from ask_assistant: {body}", file=sys.stderr)
    # 503 = "No active frontend websocket connection" — nobody is home to
    # receive it. Deferrable rather than a hard failure.
    if exc.code == 503 or is_turn_rejection(body):
        sys.exit(EXIT_UNDELIVERABLE)
    sys.exit(EXIT_CONFIG_ERROR)
except urllib.error.URLError as exc:
    # App backend unreachable (e.g. Mage not running, or no connection within
    # the wait) — never reached it, so deferrable.
    print(f"ERROR: {exc}", file=sys.stderr)
    sys.exit(EXIT_UNDELIVERABLE)
except TimeoutError:
    # Connected and sent, but no answer in time: it may have landed.
    print(f"ERROR: no answer within {TIMEOUT:g}s from ask_assistant (sent; the receipt decides "
          f"whether it was received)", file=sys.stderr)
    sys.exit(EXIT_UNCONFIRMED)
except Exception as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    sys.exit(EXIT_CONFIG_ERROR)
