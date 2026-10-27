---
description: "Open the Mage Scheduler dashboard, or schedule a task from natural language."
argument-hint: "[open | status | <natural language scheduling request>]"
allowed-tools: [mcp__mage-scheduler__scheduler_context, mcp__mage-scheduler__scheduler_open_dashboard, mcp__mage-scheduler__scheduler_schedule_intent, mcp__mage-scheduler__scheduler_preview_intent]
---

Handle the user's scheduler request based on $ARGUMENTS:

- If $ARGUMENTS is empty or "open" → call `scheduler_open_dashboard` and confirm it opened.
- If $ARGUMENTS is "status" → call `scheduler_context` and summarize: running status, task counts, and the 5 most recent tasks.
- Otherwise, treat $ARGUMENTS as a natural language scheduling request:
  1. Call `scheduler_context` to get available actions and allowed directories.
  2. Parse the request into an intent object (use `scheduler_preview_intent({"intent": ...})` if timing is ambiguous).
  3. Confirm the intent with the user if the command or timing is significant.
  4. Call `scheduler_schedule_intent({"intent": ...})` to create the task. Pass the intent as an object, not an encoded JSON string.
  5. Report back: task ID, scheduled time (in user's timezone), and what will run.

If context reports `errors` or `service.data_ready: false`, explain the unavailable sections before scheduling. A null section does not mean no actions or tasks exist.
