"""Tests for bounded task-output storage (#6)."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from sqlalchemy import select

from tests.conftest import make_task


class TestTruncateHelper:
    def test_none_passthrough(self):
        from jobs.run_command import _truncate_output
        assert _truncate_output(None) is None

    def test_short_unchanged(self):
        from jobs.run_command import _truncate_output
        assert _truncate_output("hello") == "hello"

    def test_at_limit_unchanged(self):
        from jobs.run_command import _truncate_output, RESULT_STORAGE_MAX
        text = "x" * RESULT_STORAGE_MAX
        assert _truncate_output(text) == text

    def test_over_limit_keeps_tail_with_marker(self):
        from jobs.run_command import _truncate_output, RESULT_STORAGE_MAX
        text = "A" + ("x" * (RESULT_STORAGE_MAX + 5000))
        out = _truncate_output(text)
        assert out.startswith("[... truncated earlier output ...]\n")
        assert out.endswith("x" * 100)
        assert "A" not in out  # the head was dropped
        assert len(out) <= RESULT_STORAGE_MAX + 40  # cap + short marker


class TestRunCommandTruncation:
    def _make_subprocess(self, monkeypatch, *, stdout="", stderr="", returncode=0):
        import jobs.run_command as rc
        fake = MagicMock()
        fake.returncode = returncode
        fake.stdout = stdout
        fake.stderr = stderr
        monkeypatch.setattr(rc, "subprocess", MagicMock(run=MagicMock(return_value=fake)))

    def test_stored_output_is_capped(self, nt_mem_db, monkeypatch):
        from jobs.run_command import run_command, RESULT_STORAGE_MAX
        from models import TaskRequest

        big = "y" * (RESULT_STORAGE_MAX + 10000)
        self._make_subprocess(monkeypatch, stdout=big, stderr=big)

        s = nt_mem_db()
        task = make_task(s, status="scheduled", command="echo big")
        s.commit()
        tid = task.id
        s.close()

        run_command(tid, "echo big")

        s2 = nt_mem_db()
        stored = s2.get(TaskRequest, tid)
        assert len(stored.result) <= RESULT_STORAGE_MAX + 40
        assert len(stored.error) <= RESULT_STORAGE_MAX + 40
        assert stored.result.startswith("[... truncated earlier output ...]")
        s2.close()
