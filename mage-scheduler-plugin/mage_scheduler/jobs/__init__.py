"""Scheduler jobs. Also home to one tiny helper both run_command and reconcile need."""
from __future__ import annotations

ASK_ASSISTANT_SCRIPT = "ask_assistant.py"


def is_assistant_delivery(action_name: str | None, command: str | None) -> bool:
    """True if this task is a message to the assistant (the ask_assistant script).

    Such a task needs a live human/assistant on the other end, so an exit code
    saying "nobody could receive it" means *park and re-deliver*, not *failed*.
    Recognised by the ``ask_assistant`` action OR by a custom command that runs
    the bundled script directly (a one-off created without an action), so both
    get the same missed-task handling.
    """
    if action_name == "ask_assistant":
        return True
    return bool(command) and ASK_ASSISTANT_SCRIPT in command
