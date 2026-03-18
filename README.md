# Mage Plugins

A collection of plugins for [Mage Lab](https://github.com/bardware/mage-lab) — drop any plugin folder into `~/Mage/Skills/` and activate it from **Settings → Skills & Plugins**.

Skills in Mage combine LLM instructions with tools, letting you activate and deactivate capabilities in a way that keeps the model's context focused and lean. Plugins extend this further. On top of everything a skill offers, a plugin adds dependency handling so required packages and resources are managed automatically, slash command support for user-invocable shortcuts, bash command support for direct shell execution, and lifecycle hooks that let the plugin respond to events like session start, tool use, and session end. Sub-agent support is on the way. Plugins are the right choice when a capability needs to reach outside the conversation — managing its own environment, triggering on events, or giving users direct control through commands.

---

## Plugins

| Plugin | Description |
|mage-scheduler-plugin|allows LLM / user scheduled tasks, including recurrance and dependencies|
| *(more coming soon)* | |

---

## Installation

1. Copy the plugin folder to `~/Mage/Skills/`:
   ```bash
   cp -r my-plugin ~/Mage/Skills/
   ```
2. Open **Settings → Skills & Plugins**
3. Find the plugin in the **Available** section and click **Activate**

---

## Plugin Format

Plugins extend skills with hooks, slash commands, sub-agents, script tools, and MCP servers. A plugin is a folder containing a `.claude-plugin/plugin.json` manifest:

```
my-plugin/
├── .claude-plugin/
│   └── plugin.json       # Required — name and description
├── SKILL.md              # LLM instructions (optional)
├── commands/
│   └── my-command.md     # Slash commands (/my-command)
├── agents/
│   └── my-agent.md       # LLM-callable sub-agents
├── hooks/
│   └── hooks.json        # Lifecycle hooks (SessionStart, PreToolUse, etc.)
├── scripts/
│   └── my-tool.sh        # Script tools (auto-registered as LLM-callable)
└── .mcp.json             # MCP server definitions
```

Only `plugin.json` is required — include only what your plugin needs.

### plugin.json

```json
{
  "name": "my-plugin",
  "description": "What this plugin does"
}
```

See the [Mage Lab docs](https://github.com/bardware/mage-lab) for the full plugin specification.
