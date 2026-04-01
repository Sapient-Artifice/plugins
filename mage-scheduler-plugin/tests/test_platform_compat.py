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
        _DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        _CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        expected = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
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
