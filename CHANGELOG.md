# Changelog

## 0.2.0 (2026-11-25)

Rebuilt on measurements from real Antigravity logs and live tests. See
[docs/EVIDENCE.md](docs/EVIDENCE.md) and [docs/ASSESSMENT.md](docs/ASSESSMENT.md).

Changed

- Prompt changes are found by fingerprinting only what reaches the prompt: rule
  text, each skill's name and description, MCP config, the plugin list and the
  prompt-related keys of `settings.json`. The check runs at the start of each
  turn. Edits to `hooks.json` and to skill bodies no longer stop you.
- An agent's file edit is worked out in advance and held for approval only when
  it changes the prompt.
- Switching back to a model used within its cache lifetime goes through. So do
  the Orchestrator's own switches, from Planner to Executor and back, which cost
  a re-read whatever you answer.
- The idle check runs only at the start of a turn and uses a timing per model.
  `idle_ttl_seconds` can be one number or a map. Defaults: Claude 300 s, Gemini
  450 s, Gemini Pro orchestrator 300 s, Gemini 4 Neon 900 s.
- Shell commands are read with `shlex`. `2>/dev/null` and `2>&1` no longer count
  as writes, and a script is judged by the full paths it names.
- A config file left exactly as v0.1, or the agy-mesh bundle of v0.1, wrote it
  is upgraded to the new defaults. A new config file is created in the data
  folder of the app in use, and a set `PLUGIN_DATA` reads only its own folder.
- Messages are in plain words.

Added

- `tools/cache_audit.py` measures cache misses in local logs and backtests the
  guard against them.
- `tests/` with recorded hook payloads, GitHub Actions CI, `ruff` and
  `shellcheck`.

Removed

- `hooks/test_guard.py`, replaced by `tests/`.

## 0.1.0 (2026-11-23)

First release. Fixes on 2026-11-24 covered the Termux `PATH`, `plugin_data` and
git exemptions, and agent folders in the home directory.
