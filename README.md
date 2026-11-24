# cache-guard

A Google Antigravity plugin (lifecycle hooks + agent rule) that stops and warns **before** actions that trigger a total or massive prompt-cache miss on large conversations.

Zero third-party dependencies — pure Python 3 standard library + POSIX `sh`, fail-open by design.

## Why?

On long Antigravity sessions (60k+ tokens), prompt caching dramatically cuts latency and token cost. A single accidental edit to a rule/skill/plugin file, a mid-session `/model` switch, an idle TTL timeout, or a mid-session `define_subagent` call invalidates up to ~100% of the cached prefix on the next turn.

`cache-guard` catches those events **before** the expensive model call or tool mutation happens, tells you how many cached tokens are at risk, and lets you approve, undo, `/compact`, or defer to a fresh session.

## What It Guards (when live context >= `60,000` tokens)

1. **Mid-session prompt customization edits** (`PreToolUse` -> `force_ask`, plus `PreInvocation` disk-drift detection -> `terminate` with 1-retry confirm):
   - **Rules**: `GEMINI.md`, `AGENTS.md`, `~/.gemini/config/rules/*`, `.agents/rules/*`, `rules.json`
   - **Skills**: `SKILL.md`, `skills.json`
   - **Plugins & MCP**: `~/.gemini/config/plugins/*` manifests/rules/skills/MCP/hooks, `~/.gemini/config/config.json`, `mcp_config.json`, `hooks.json`, `agy plugin ...`, `agy mcp ...`
   - **CLI settings**: `~/.gemini/antigravity-cli/settings.json` (hashes cache-affecting keys only to avoid UI-noise false positives)
   - Approved agent edits refresh the baseline fingerprint in `PostToolUse` so `PreInvocation` does not double-warn on the same change.
2. **Mid-session model / effort switches** (`PreInvocation` -> `terminate` before the API call; re-send within 3 min to confirm, or switch back with `/model`).
3. **Idle cache TTL expiry** (`PreInvocation` -> `terminate` when `>= 5 min` elapsed since the last model call, letting you confirm, `/compact`, or start fresh).
4. **Mid-session `define_subagent`** (`PreToolUse` -> `force_ask`).

Additionally, `rules/AGENTS.md` instructs the agent itself to pause and explain the cache impact before attempting any of these changes in a large session.

## Installation

### Option 1: Clone directly into your global Antigravity plugins directory

```bash
git clone https://github.com/k45sle/cache-guard.git ~/.gemini/config/plugins/cache-guard
```

### Option 2: Install via `agy plugin install` from a local clone

```bash
git clone https://github.com/k45sle/cache-guard.git
agy plugin install ./cache-guard
```

### Option 3: Project-scoped installation

Clone or copy into `.agents/plugins/cache-guard` at your repository root to share with your team.

### Verify installation

```bash
agy plugin list
agy plugin validate ~/.gemini/config/plugins/cache-guard --strict
```

## Configuration

Auto-created on first run at:

```text
~/.gemini/antigravity/plugin_data/cache-guard/config.json
```

(Or `<surface>/plugin_data/cache-guard/config.json` when `PLUGIN_DATA` or `ANTIGRAVITY_APP_DATA_DIR` is set.)

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

| Option | Default | Description |
| :--- | :--- | :--- |
| `enabled` | `true` | Master toggle for all hooks. |
| `min_tokens` | `60000` | Minimum live context tokens (`input_tokens + cache_read_tokens`) before warnings fire. |
| `check_customization_edits` | `true` | Guard tool edits, shell mutations, and external disk drift on customization files. |
| `check_model_switch` | `true` | Guard mid-session model changes in `PreInvocation`. |
| `check_idle_ttl` | `true` | Guard likely cache TTL expiry after idle gaps. |
| `idle_ttl_seconds` | `300` | Idle gap threshold in seconds (minimum `30`). |
| `check_define_subagent` | `true` | Guard mid-session `define_subagent` tool calls. |
| `pretool_decision` | `"force_ask"` | `PreToolUse` decision: `"force_ask"`, `"ask"`, or `"deny"`. |
| `retry_confirm_window_seconds` | `180` | Seconds a retry confirms and bypasses a `PreInvocation` stop (minimum `15`). |

## Enable / Disable / Uninstall

```bash
agy plugin disable cache-guard
agy plugin enable cache-guard
agy plugin uninstall cache-guard
```

## Requirements

- Google Antigravity CLI / IDE / Desktop with plugin hooks support
- Python 3.9+ (`python3` on `PATH` or `/usr/bin/python3`)
- POSIX `sh`

## Development & Tests

Run the full unit and end-to-end test suite:

```bash
python3 -m unittest discover -s hooks -v
```

Validate the plugin manifest and layout:

```bash
agy plugin validate . --strict
```

## License

[MIT](LICENSE)
