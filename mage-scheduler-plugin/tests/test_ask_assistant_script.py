"""Integration tests for scripts/ask_assistant.py exit codes and ack wiring.

The script runs as a subprocess and makes real HTTP calls, so we point it at a
throwaway local server (never at Mage). Exit codes are the contract run_command
relies on: 0 delivered, 3 undeliverable/deferrable, 4 sent but unconfirmed
(Mage took the request and had not answered when the wait ran out), 1 real error.
"""
from __future__ import annotations

import http.server
import os
import subprocess
import sys
import threading
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "mage_scheduler" / "scripts" / "ask_assistant.py"

_last_body: dict = {}


def _make_server(status: int, delay: float = 0.0,
                 body: bytes = b'{"status":"success","message":"queued"}'):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            import time
            time.sleep(delay)
            length = int(self.headers.get("Content-Length", 0))
            _last_body["raw"] = self.rfile.read(length).decode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _run(env_extra: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, **env_extra}
    return subprocess.run(
        [sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=20
    )


def test_missing_message_is_config_error():
    r = _run({"MESSAGE": ""})
    assert r.returncode == 1


def test_200_delivers_ok():
    srv = _make_server(200)
    try:
        port = srv.server_address[1]
        r = _run({"MESSAGE": "hi", "MAGE_ASK_ASSISTANT_URL": f"http://127.0.0.1:{port}/ask"})
        assert r.returncode == 0
    finally:
        srv.shutdown()


def test_503_is_undeliverable():
    srv = _make_server(503)
    try:
        port = srv.server_address[1]
        r = _run({"MESSAGE": "hi", "MAGE_ASK_ASSISTANT_URL": f"http://127.0.0.1:{port}/ask"})
        assert r.returncode == 3
    finally:
        srv.shutdown()


# Mage 0.13 admits the chat turn before answering /ask_assistant and answers
# HTTP 400 {"detail": {"type": "session_turn_rejected", ...}} when it cannot.
# The common case is code=gateway_authentication: the backend asked the desktop
# UI to renew the gateway sign-in and got no answer because the screen was
# locked. The message never entered a turn, so nobody received it. That has
# to be deferrable (parked as missed → dashboard decision + re-delivery), not
# the hard failure that silently lost the 5 AM reports for a week.

_TURN_REJECTED = (b'{"detail":{"type":"session_turn_rejected","ok":false,'
                  b'"code":"gateway_authentication","error":"Mage could not renew '
                  b'your Mage Lab sign-in. Sign in again and retry.","session_id":1,'
                  b'"chat_id":1704,"client_request_id":"legacy-abc"}}')


def test_turn_rejected_400_is_undeliverable():
    srv = _make_server(400, body=_TURN_REJECTED)
    try:
        port = srv.server_address[1]
        r = _run({"MESSAGE": "hi", "MAGE_ASK_ASSISTANT_URL": f"http://127.0.0.1:{port}/ask"})
        assert r.returncode == 3, (r.returncode, r.stderr)
        assert "session_turn_rejected" in r.stderr  # the reason is kept for the task's error
    finally:
        srv.shutdown()


def test_turn_rejected_with_another_code_is_still_undeliverable():
    # Any admission refusal means the message never entered a turn.
    body = _TURN_REJECTED.replace(b"gateway_authentication", b"gateway_session_changed")
    srv = _make_server(400, body=body)
    try:
        port = srv.server_address[1]
        r = _run({"MESSAGE": "hi", "MAGE_ASK_ASSISTANT_URL": f"http://127.0.0.1:{port}/ask"})
        assert r.returncode == 3
    finally:
        srv.shutdown()


def test_other_400_is_still_a_config_error():
    # e.g. a malformed request: retrying later will not help.
    srv = _make_server(400, body=b'{"detail":"Message cannot be empty"}')
    try:
        port = srv.server_address[1]
        r = _run({"MESSAGE": "hi", "MAGE_ASK_ASSISTANT_URL": f"http://127.0.0.1:{port}/ask"})
        assert r.returncode == 1
    finally:
        srv.shutdown()


def test_non_json_error_body_is_a_config_error():
    srv = _make_server(500, body=b"<html>Internal Server Error</html>")
    try:
        port = srv.server_address[1]
        r = _run({"MESSAGE": "hi", "MAGE_ASK_ASSISTANT_URL": f"http://127.0.0.1:{port}/ask"})
        assert r.returncode == 1
    finally:
        srv.shutdown()


def test_unreachable_backend_is_undeliverable():
    # Port 1 is not listenable → connection refused → URLError → deferrable.
    r = _run({"MESSAGE": "hi", "MAGE_ASK_ASSISTANT_URL": "http://127.0.0.1:1/ask"})
    assert r.returncode == 3


def test_ack_instruction_included_in_message():
    srv = _make_server(200)
    try:
        port = srv.server_address[1]
        r = _run({
            "MESSAGE": "do the scan",
            "MAGE_ASK_ASSISTANT_URL": f"http://127.0.0.1:{port}/ask",
            "SCHEDULER_TASK_ID": "77",
            "SCHEDULER_ACK_REQUIRED": "1",
            "SCHEDULER_ACK_TOKEN": "abc123",
        })
        assert r.returncode == 0
        body = _last_body["raw"]
        assert "scheduler_ack_task" in body
        assert "77" in body and "abc123" in body
    finally:
        srv.shutdown()


# Mage 0.13 answers /ask_assistant only after it has STARTED the chat turn
# (it used to queue and answer at once). When that takes longer than the
# script waits, the message was sent; whether it landed is what the receipt
# (ack) is for. Calling that a hard failure is what lost the 5 AM tasks.

def test_a_slow_answer_is_sent_but_unconfirmed_not_failed():
    srv = _make_server(200, delay=3)
    try:
        port = srv.server_address[1]
        r = _run({"MESSAGE": "hi", "MAGE_ASK_ASSISTANT_URL": f"http://127.0.0.1:{port}/ask",
                  "MAGE_ASK_ASSISTANT_TIMEOUT": "1"})
        assert r.returncode == 4, (r.returncode, r.stderr)
        assert "no answer within 1" in r.stderr
    finally:
        srv.shutdown()


def test_the_default_wait_outlasts_a_slow_turn_start():
    # 10 s used to be the whole wait; a 0.13 turn start can take longer.
    srv = _make_server(200, delay=11)
    try:
        port = srv.server_address[1]
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=40,
                           env={**os.environ, "MESSAGE": "hi", "MAGE_ASK_ASSISTANT_URL": f"http://127.0.0.1:{port}/ask"})
        assert r.returncode == 0, (r.returncode, r.stderr)
    finally:
        srv.shutdown()
