"""Tests for _ask_assistant_command in db.py.

Verifies that the returned command double-quotes both the Python executable
and the script path, so that paths containing spaces work correctly on both
Windows (cmd.exe) and Unix (sh), and so that shlex.split preserves Windows
backslash path separators (which are eaten as escape characters when unquoted).
"""
from __future__ import annotations

import shlex
import sys
from pathlib import Path


def _make_venv(tmp_path: Path) -> Path:
    """Create the venv Python binary expected by _ask_assistant_command."""
    if sys.platform == "win32":
        python = tmp_path / ".venv" / "Scripts" / "python.exe"
    else:
        python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    return python


def _make_script(tmp_path: Path) -> Path:
    """Create the ask_assistant.py script expected by _ask_assistant_command."""
    script = tmp_path / "mage_scheduler" / "scripts" / "ask_assistant.py"
    script.parent.mkdir(parents=True)
    script.touch()
    return script


def test_command_double_quotes_both_paths(tmp_path, monkeypatch):
    """_ask_assistant_command must double-quote both paths."""
    import db

    python = _make_venv(tmp_path)
    _make_script(tmp_path)
    monkeypatch.setattr(db, "__file__", str(tmp_path / "mage_scheduler" / "db.py"))

    result = db._ask_assistant_command()

    assert result.startswith('"'), "executable must be double-quoted"
    tokens = shlex.split(result)
    assert len(tokens) == 2
    assert tokens[0] == str(python)


def test_command_with_spaces_in_path(tmp_path, monkeypatch):
    """Paths containing spaces must survive shlex.split and shell execution."""
    import db

    # Use a tmp_path that contains a space-like segment by renaming after creation
    spaced = tmp_path / "my plugin"
    spaced.mkdir()

    if sys.platform == "win32":
        python = spaced / ".venv" / "Scripts" / "python.exe"
    else:
        python = spaced / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()

    script = spaced / "mage_scheduler" / "scripts" / "ask_assistant.py"
    script.parent.mkdir(parents=True)
    script.touch()

    monkeypatch.setattr(db, "__file__", str(spaced / "mage_scheduler" / "db.py"))

    result = db._ask_assistant_command()

    tokens = shlex.split(result)
    assert len(tokens) == 2, (
        f"path with space split into {len(tokens)} tokens instead of 2: {result!r}"
    )
    assert tokens[0] == str(python)
    assert tokens[1] == str(script)


def test_windows_backslashes_survive_shlex_split():
    """Windows backslash paths inside double quotes must not be eaten by shlex.

    This is a property test of the quoting format: verifies that the double-quote
    wrapping used by _ask_assistant_command preserves backslashes correctly when
    the resulting command string is later parsed by shlex.split (in _validate_command).
    """
    python_path = r"C:\Users\My User\plugin\.venv\Scripts\python.exe"
    script_path = r"C:\Users\My User\plugin\mage_scheduler\scripts\ask_assistant.py"

    command = f'"{python_path}" "{script_path}"'

    tokens = shlex.split(command)
    assert tokens[0] == python_path, (
        f"backslashes in Windows python path eaten by shlex: got {tokens[0]!r}"
    )
    assert tokens[1] == script_path, (
        f"backslashes in Windows script path eaten by shlex: got {tokens[1]!r}"
    )
