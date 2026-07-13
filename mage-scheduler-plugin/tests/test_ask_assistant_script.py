"""Integration tests for scripts/ask_assistant.py exit codes and ack wiring.

The script runs as a subprocess and makes real HTTP calls, so we point it at a
throwaway local server (never at Mage). Exit codes are the contract run_command
relies on: 0 delivered, 3 undeliverable/deferrable, 1 real error.
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


def _make_server(status: int):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            _last_body["raw"] = self.rfile.read(length).decode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"success","message":"queued"}')

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
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
