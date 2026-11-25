# cache-guard

An Antigravity plugin that pauses a long chat just before something makes it lose
its prompt cache.

A long chat stays cheap while the provider keeps the earlier context in its prompt
cache. Some actions throw that cache away, and the next message then pays full
price to read the whole chat again. In the author's logs from November 2026 that
happened 40 times and re-billed 7.7 million tokens. Once a chat
passes 60,000 tokens, cache-guard stops before those actions.

## What it stops

- A model switch mid-chat, including a change of effort level, because each model
  and effort keeps its own cache. Switching back to a model you used within its
  cache lifetime goes through, since that cache is still warm.
- A change to what goes into the prompt: rule text (`GEMINI.md`, `AGENTS.md`, rule
  folders), a skill's name or description, MCP config, the plugin list, the
  prompt-related keys of `settings.json`, `agy plugin` and `agy mcp` changes, and
  new subagents. The agent's tool call waits for your approval. A change made
  outside the chat is caught at the start of the next turn.
- Coming back after the cache has expired: after 5 minutes for Claude, 7.5 for
  Gemini Flash and 15 for Gemini 4 Neon.

It ignores changes that never reach the prompt, such as `hooks.json`, the body of
a skill, or a script that only touches other files.

It also lets the Orchestrator's own model switches through. When the Orchestrator
uses different models for Planner and Executor, each plan goes to the Executor and
your next message goes back to the Planner. Both steps re-read the whole chat (16
of the misses in the author's logs, 4.2 million tokens), but stopping there would
interrupt every plan and save nothing. To avoid those re-reads, pick the same
model and effort for Planner and Executor in the `Orchestrator` row of `/model`:
agy 1.5.17 then runs it as one agent on that model.

When it stops a message, send the same message again within 3 minutes to go
ahead. When it holds a tool call, approve it to go ahead, or reject it and make
the change in a new chat.

The measurements behind each rule are in [docs/EVIDENCE.md](docs/EVIDENCE.md).
[docs/ASSESSMENT.md](docs/ASSESSMENT.md) reviews the project and explains the
v0.2 rebuild.

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

Turn it off or back on at any time:

```bash
agy plugin disable cache-guard
agy plugin enable cache-guard
```

Needs Python 3.9 or newer and `sh`.

## Is it worth it for you?

```bash
python3 tools/cache_audit.py
```

This reads your local Antigravity logs, lists the cache misses with their likely
causes, and replays the guard's rules over them: how often it would have stopped
you, how often for nothing, and how many of the lost tokens it would have caught.
Run it now and again in a few weeks.

## Settings

The file is created on first run in the data folder of the app you use, the
folder that holds its chats:

```text
~/.gemini/antigravity/plugin_data/cache-guard/config.json
~/.gemini/antigravity-cli/plugin_data/cache-guard/config.json
```

The first is Antigravity's, the second the agy CLI's. An app with no file of its
own reads Antigravity's when it exists. When `PLUGIN_DATA` is set, only
`$PLUGIN_DATA/config.json` counts.

Defaults:

```json
{
  "enabled": true,
  "min_tokens": 60000,
  "check_customization_edits": true,
  "check_model_switch": true,
  "check_idle_ttl": true,
  "idle_ttl_seconds": {
    "claude": 300,
    "gemini": 450,
    "gemini-pro": 300,
    "gemini-4-neon": 900,
    "orchestrator-neon": 900,
    "gpt": 300,
    "default": 300
  },
  "check_define_subagent": true,
  "pretool_decision": "force_ask",
  "retry_confirm_window_seconds": 180
}
```

`idle_ttl_seconds` takes one number for every model, or a map from part of a model
name to seconds. When several parts match a model, the longest wins, and `default`
covers the rest. A config file still exactly as v0.1, or the agy-mesh bundle of
v0.1, wrote it is upgraded to these defaults.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

MIT License.
