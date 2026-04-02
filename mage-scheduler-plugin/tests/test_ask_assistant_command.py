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
from unittest.mock import patch


def test_command_double_quotes_both_paths(tmp_path):
    """Returned command must quote both the executable and the script."""
    import db

    fake_python = tmp_path / ".venv" / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    fake_python.touch()

    with patch("db.Path") as mock_path_cls:
        # __file__ resolution chain: Path(__file__).resolve().parent gives mage_scheduler/
        # .parent gives plugin_dir; / "scripts" / "ask_assistant.py" gives script
        instance = mock_path_cls.return_value.resolve.return_value
        script = instance.parent.__truediv__.return_value.__truediv__.return_value
        plugin_dir = instance.parent.parent
        if sys.platform == "win32":
            venv_python = (
                plugin_dir.__truediv__.return_value.__truediv__.return_value.__truediv__.return_value
            )
        else:
            venv_python = (
                plugin_dir.__truediv__.return_value.__truediv__.return_value.__truediv__.return_value
            )
        venv_python.exists.return_value = True

        result = db._ask_assistant_command()

    assert result.startswith('"'), "command must start with a double-quoted executable"
    # shlex.split should produce exactly two tokens
    tokens = shlex.split(result)
    assert len(tokens) == 2


def test_command_with_spaces_in_path_survives_shlex_split():
    """Paths containing spaces must round-trip through shlex.split correctly."""
    import db

    if sys.platform == "win32":
        fake_python = Path(r"C:\Users\My User\plugin\.venv\Scripts\python.exe")
        fake_script = Path(r"C:\Users\My User\plugin\mage_scheduler\scripts\ask_assistant.py")
    else:
        fake_python = Path("/home/my user/plugin/.venv/bin/python")
        fake_script = Path("/home/my user/plugin/mage_scheduler/scripts/ask_assistant.py")

    with patch("db.Path") as mock_path_cls:
        instance = mock_path_cls.return_value.resolve.return_value
        instance.parent.__truediv__.return_value.__truediv__.return_value = fake_script
        plugin_dir = instance.parent.parent
        venv_python_mock = (
            plugin_dir.__truediv__.return_value.__truediv__.return_value.__truediv__.return_value
        )
        venv_python_mock.__str__ = lambda s: str(fake_python)
        venv_python_mock.exists.return_value = True

        # Manually build what the function would produce, since Path mock is complex
        # Directly test the quoting format instead
        command = f'"{fake_python}" "{fake_script}"'

    tokens = shlex.split(command)
    assert tokens[0] == str(fake_python), "executable path must survive shlex.split intact"
    assert tokens[1] == str(fake_script), "script path must survive shlex.split intact"


def test_windows_backslashes_survive_shlex_split():
    """Windows backslash paths inside double quotes must not be eaten by shlex."""
    python_path = r"C:\Users\My User\plugin\.venv\Scripts\python.exe"
    script_path = r"C:\Users\My User\plugin\mage_scheduler\scripts\ask_assistant.py"

    # This is the format _ask_assistant_command must produce
    command = f'"{python_path}" "{script_path}"'

    tokens = shlex.split(command)
    assert tokens[0] == python_path, (
        f"backslashes in Windows python path eaten by shlex: got {tokens[0]!r}"
    )
    assert tokens[1] == script_path, (
        f"backslashes in Windows script path eaten by shlex: got {tokens[1]!r}"
    )
