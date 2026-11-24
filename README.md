# cache-guard

An Antigravity plugin that stops and warns you before something wipes your prompt cache in a long chat.

Long Antigravity chats stay fast and cheap because the model reuses cached context from earlier turns. A few easy-to-forget actions wipe that cache and force the model to re-read the whole conversation at full price. This plugin catches them before the call goes out.

## What it catches

Once a chat reaches 60,000 tokens (configurable), `cache-guard` pauses and warns you before:

- **Editing rules, skills, plugins, MCP, or settings** (`GEMINI.md`, `AGENTS.md`, `SKILL.md`, `hooks.json`, `mcp_config.json`, `plugin.json`, `settings.json`, or `agy plugin` / `agy mcp` commands). Catches both agent edits and files changed outside the chat.
- **Switching models mid-chat** (`/model`). Caches are per model, so switching forces a full re-read.
- **Coming back after an idle gap** (default: 5 minutes). The cache has likely expired, so you can continue, run `/compact`, or start fresh.
- **Creating a new subagent mid-chat** (`define_subagent`), which changes the system prompt.

When it stops you, it shows what triggered it and how many tokens are at risk. Send your message again within 3 minutes (or approve the tool prompt) to go ahead anyway.

## Install

Clone it into your global Antigravity plugins folder:

```bash
git clone https://github.com/k45sle/cache-guard.git ~/.gemini/config/plugins/cache-guard
```

Or install from a local copy:

```bash
git clone https://github.com/k45sle/cache-guard.git
agy plugin install ./cache-guard
```

Turn it off or back on anytime:

```bash
agy plugin disable cache-guard
agy plugin enable cache-guard
```

Needs Python 3.9+ and `sh`.

## Settings

Created automatically on first run at:

```text
~/.gemini/antigravity/plugin_data/cache-guard/config.json
```

Defaults:

```json
{
  "enabled": true,
  "min_tokens": 60000,
  "check_customization_edits": true,
  "check_model_switch": true,
  "check_idle_ttl": true,
  "idle_ttl_seconds": 300,
  "check_define_subagent": true,
  "pretool_decision": "force_ask",
  "retry_confirm_window_seconds": 180
}
```

## Tests

```bash
python3 -m unittest discover -s hooks -v
```

MIT License.
