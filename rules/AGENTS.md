# Cache-Miss Guard

When the active conversation already has large context (60k+ tokens or many turns), **STOP and warn the user BEFORE taking any action that will invalidate the prompt cache prefix (total / massive cache miss)**:

1. **Prompt customization edits** (invalidates ~100% of cached prefix on the next turn):
   - Creating, editing, deleting, or renaming `GEMINI.md`, `AGENTS.md`, rules (`~/.gemini/config/rules/*`, `.agents/rules/*`, `rules.json`), skills (adding or removing a `SKILL.md`, changing its `name` or `description`, `skills.json`), plugins (`~/.gemini/config/plugins/*`, `plugin.json`, `plugins.json`, `~/.gemini/config/config.json`), MCP configs (`mcp_config.json`), or CLI `settings.json`.
   - Running `agy plugin install/enable/disable/uninstall` or `agy mcp` mutations.
   - Editing `hooks.json` or the body of a skill below its frontmatter does not change the prompt.
2. **Mid-session model / effort switches or `/compact`**:
   - Switching models or effort (e.g., `/model`) causes a 100% cache miss, except when switching back to a model used in the last few minutes; `/compact` rewrites history and causes a 100% cache miss on the next turn (only worthwhile when shrinking a bloated session).
3. **Mid-session `define_subagent`** (alters `<subagents>` in the system prompt).

**Required behavior when any of the above is requested or planned in a large session:**
- Pause before executing the mutating tool call.
- State clearly: (a) **what** action/file will break the cache, (b) **why** (system prompt / tool prefix mutation or per-model cache miss), and (c) safer options (do it in a fresh low-context session, defer and batch all customization edits at the end of the session, or proceed now if the user confirms).
- Only proceed once the user explicitly confirms.
