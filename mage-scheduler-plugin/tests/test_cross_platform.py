"""Cross-platform correctness tests for command tokenization and path allowlist.

These exercise the Windows code paths on any host by toggling the module-level
`_IS_WINDOWS` flag / the normcase seam, so the POSIX CI machine still covers the
Windows behavior. POSIX behavior is asserted to be byte-identical to the prior
shlex.split / shlex.join implementation.
"""
from __future__ import annotations

import os
import shlex

import pytest

import api


# ---------------------------------------------------------------------------
# _split_command / _join_command  (#2 — Windows backslash / quoting)
# ---------------------------------------------------------------------------

class TestCommandTokenization:
    def test_posix_split_matches_shlex(self):
        assert api._split_command("/usr/bin/python script.py --flag") == \
            shlex.split("/usr/bin/python script.py --flag")

    def test_posix_join_matches_shlex(self):
        tokens = ["/opt/a b/tool", "--dest", "/tmp/out dir"]
        assert api._join_command(tokens) == shlex.join(tokens)

    def test_windows_split_preserves_backslashes(self, monkeypatch):
        monkeypatch.setattr(api, "_IS_WINDOWS", True)
        result = api._split_command(r"C:\Users\me\tools\backup.exe --dest D:\out")
        assert result == [r"C:\Users\me\tools\backup.exe", "--dest", r"D:\out"]

    def test_windows_split_strips_surrounding_quotes(self, monkeypatch):
        monkeypatch.setattr(api, "_IS_WINDOWS", True)
        result = api._split_command(r'"C:\Program Files\app\tool.exe" arg1')
        assert result == [r"C:\Program Files\app\tool.exe", "arg1"]

    def test_windows_join_quotes_only_when_needed(self, monkeypatch):
        monkeypatch.setattr(api, "_IS_WINDOWS", True)
        # No spaces → backslashes kept literal, no quoting (cmd.exe-safe).
        assert api._join_command([r"C:\Users\me\python.exe", "script.py"]) == \
            r"C:\Users\me\python.exe script.py"
        # Spaces → double-quoted (NOT single-quoted like shlex would do).
        assert api._join_command([r"C:\Program Files\app.exe", "a"]) == \
            r'"C:\Program Files\app.exe" a'

    def test_windows_bare_name_resolution_is_cmd_safe(self, monkeypatch):
        """Regression for the shlex.join single-quote bug on Windows.

        Simulates _validate_command's rewrite branch: a resolved absolute path
        must serialize into something cmd.exe can execute, not `'C:\\...'`.
        """
        monkeypatch.setattr(api, "_IS_WINDOWS", True)
        resolved = r"C:\Users\me\.venv\Scripts\python.exe"
        rewritten = api._join_command([resolved, "job.py"])
        assert "'" not in rewritten
        assert rewritten == rf"{resolved} job.py"


# ---------------------------------------------------------------------------
# _is_path_allowed  (#3 — case-(in)sensitivity)
# ---------------------------------------------------------------------------

class TestPathAllowlist:
    def test_posix_is_case_sensitive(self):
        # On POSIX a case mismatch must NOT be allowed (no over-permissiveness).
        assert api._is_path_allowed("/App/bin", ["/app"]) is False

    def test_exact_match_allowed(self):
        assert api._is_path_allowed("/opt/tools", ["/opt/tools"]) is True

    def test_subdir_allowed(self):
        assert api._is_path_allowed("/opt/tools/sub/x", ["/opt/tools"]) is True

    def test_outside_dir_denied(self):
        assert api._is_path_allowed("/opt/other/x", ["/opt/tools"]) is False

    def test_case_insensitive_when_fs_is(self, monkeypatch):
        """Simulate Windows / case-insensitive macOS: normcase folds case."""
        monkeypatch.setattr(os.path, "realpath", lambda p: p)
        monkeypatch.setattr(os.path, "normcase", str.lower)
        # Exact match differing only by case.
        assert api._is_path_allowed("/App", ["/APP"]) is True
        # Subdir with differing case on the allowed prefix.
        assert api._is_path_allowed("/Tools/Sub/x", ["/TOOLS"]) is True
        # Genuinely outside is still denied.
        assert api._is_path_allowed("/Other/x", ["/TOOLS"]) is False
