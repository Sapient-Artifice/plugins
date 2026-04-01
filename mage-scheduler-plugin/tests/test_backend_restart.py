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
