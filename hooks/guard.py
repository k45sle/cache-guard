#!/usr/bin/env python3
"""
cache-guard: Antigravity lifecycle hook that stops and warns BEFORE actions
that cause a total or massive prompt-cache miss.

Events handled:
  - SessionStart: baseline fingerprint of active customization files.
  - PreToolUse:   returns force_ask (configurable) when the agent is about to
                  mutate customization files or define_subagent on a session
                  with >= min_tokens live context.
  - PostToolUse:  refreshes fingerprint after an approved customization edit
                  so PreInvocation does not double-warn on the same change.
  - PreInvocation: terminates BEFORE the model API call (with 1-retry confirm)
                  when >= min_tokens and any of:
                    1. Mid-session model / effort switch
                    2. External customization file edits on disk
                    3. Idle cache TTL expiration
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "min_tokens": 60000,
    "check_customization_edits": True,
    "check_model_switch": True,
    "check_idle_ttl": True,
    "idle_ttl_seconds": 300,
    "check_define_subagent": True,
    "pretool_decision": "force_ask",
    "retry_confirm_window_seconds": 180,
}

FILE_MUTATION_TOOLS = {
    "write_file",
    "edit_file",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
}
SHELL_TOOLS = {"run_command", "Bash"}
AGENT_DIR_NAMES = (".agents", ".agent", "_agents", "_agent")
TOP_RULE_FILES = {"GEMINI.md", "AGENTS.md"}
CONFIG_MANIFEST_FILES = {
    "config.json",
    "mcp_config.json",
    "hooks.json",
    "rules.json",
    "skills.json",
    "plugins.json",
    "plugin.json",
}
PLUGIN_PROMPT_FILES = {"plugin.json", "mcp_config.json", "hooks.json", "AGENTS.md", "GEMINI.md", "SKILL.md"}
SETTINGS_CACHE_KEYS = ("model", "agentMode", "mcp", "mcpServers", "plugins")

# Shell mutation detection patterns
AGY_MUTATE_RE = re.compile(
    r"\bagy\s+(?:plugin\s+(?:install|uninstall|remove|enable|disable|update)|mcp\s+(?:add|remove|enable|disable))\b",
    re.IGNORECASE,
)
SHELL_MUTATE_OP_RE = re.compile(
    r"(?:>>?|\b(?:tee|cp|mv|rm|unlink|mkdir|touch|ln|tar|unzip|rsync|scp|install|truncate)\b"
    r"|\bsed\s+-[^\n]*i\b|\bperl\s+-[^\n]*i\b"
    r"|\bpython[23]?\b[^\n]*(?:open\(|write_text|write_bytes|shutil\.|os\.remove|os\.unlink|os\.rename)"
    r"|\bnode\b[^\n]*(?:writeFile|appendFile|rmSync|unlinkSync|renameSync))",
    re.IGNORECASE,
)
CUSTOM_PATH_HINT_RE = re.compile(
    r"(?:\.gemini/config(?:/|\b)|\.gemini/antigravity(?:-[a-z]+)?/settings\.json\b"
    r"|(?:^|[/\s'\"`])(?:GEMINI\.md|AGENTS\.md|SKILL\.md|mcp_config\.json|hooks\.json|plugin\.json|plugins\.json|skills\.json|rules\.json)(?:$|[/\s'\"`])"
    r"|(?:^|[/\s'\"`])(?:\.agents|\.agent|_agents|_agent)/(?:rules|skills|plugins|hooks\.json|mcp_config\.json|rules\.json|skills\.json|plugins\.json))",
    re.IGNORECASE,
)


def home_dir() -> Path:
    return Path(os.path.expanduser("~")).resolve()


def canonical_data_dir() -> Path:
    return home_dir() / ".gemini" / "antigravity" / "plugin_data" / "cache-guard"


def resolve_data_dir(payload: Dict[str, Any]) -> Path:
    env_pd = os.environ.get("PLUGIN_DATA", "").strip()
    if env_pd:
        return Path(os.path.expanduser(env_pd))
    tp = str(payload.get("transcriptPath") or "")
    marker = f"{os.sep}brain{os.sep}"
    idx = tp.find(marker)
    if idx > 0:
        return Path(tp[:idx]) / "plugin_data" / "cache-guard"
    return canonical_data_dir()


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".tmp.{os.getpid()}.{time.time_ns()}"
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_config(data_dir: Path) -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    candidates = [data_dir / "config.json"]
    canon = canonical_data_dir() / "config.json"
    if canon != candidates[0]:
        candidates.append(canon)

    loaded = False
    for path in candidates:
        if path.is_file():
            try:
                user_cfg = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(user_cfg, dict):
                    cfg.update(user_cfg)
                    loaded = True
                    break
            except Exception:
                pass

    if not loaded:
        # Seed canonical config so the user has a discoverable file to edit.
        try:
            atomic_write_json(canon, DEFAULT_CONFIG)
        except Exception:
            pass

    # Sanitize numeric / enum fields
    try:
        cfg["min_tokens"] = max(0, int(cfg.get("min_tokens", DEFAULT_CONFIG["min_tokens"])))
    except Exception:
        cfg["min_tokens"] = DEFAULT_CONFIG["min_tokens"]
    try:
        cfg["idle_ttl_seconds"] = max(30, int(cfg.get("idle_ttl_seconds", DEFAULT_CONFIG["idle_ttl_seconds"])))
    except Exception:
        cfg["idle_ttl_seconds"] = DEFAULT_CONFIG["idle_ttl_seconds"]
    try:
        cfg["retry_confirm_window_seconds"] = max(
            15, int(cfg.get("retry_confirm_window_seconds", DEFAULT_CONFIG["retry_confirm_window_seconds"]))
        )
    except Exception:
        cfg["retry_confirm_window_seconds"] = DEFAULT_CONFIG["retry_confirm_window_seconds"]
    if cfg.get("pretool_decision") not in ("force_ask", "ask", "deny"):
        cfg["pretool_decision"] = "force_ask"
    return cfg


def safe_conv_id(payload: Dict[str, Any]) -> str:
    raw = str(payload.get("conversationId") or "").strip()
    if re.fullmatch(r"[0-9A-Za-z_-]{6,128}", raw):
        return raw
    return "default"


def load_session_state(data_dir: Path, conv_id: str) -> Dict[str, Any]:
    path = data_dir / "state" / f"{conv_id}.json"
    if path.is_file():
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return {}


def save_session_state(data_dir: Path, conv_id: str, state: Dict[str, Any]) -> None:
    try:
        atomic_write_json(data_dir / "state" / f"{conv_id}.json", state)
    except Exception:
        pass


def parse_iso_ts(val: Any) -> Optional[float]:
    if not isinstance(val, str) or not val:
        return None
    s = val.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def read_transcript_tail_lines(path: Path, tail_bytes: int = 524288) -> List[str]:
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > tail_bytes:
            fh.seek(size - tail_bytes)
            fh.readline()  # discard partial first line
        raw = fh.read()
    return raw.decode("utf-8", errors="replace").splitlines()


def read_transcript_state(transcript_path: str) -> Dict[str, Any]:
    """Return latest PLANNER_RESPONSE metrics from transcript_full.jsonl / transcript.jsonl."""
    result: Dict[str, Any] = {
        "live_tokens": 0,
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "prev_model": None,
        "last_ts": None,
        "last_step_index": None,
    }
    if not transcript_path:
        return result

    candidates = [Path(transcript_path)]
    if candidates[0].name == "transcript_full.jsonl":
        candidates.append(candidates[0].with_name("transcript.jsonl"))
    elif candidates[0].name == "transcript.jsonl":
        candidates.append(candidates[0].with_name("transcript_full.jsonl"))

    target: Optional[Path] = None
    for c in candidates:
        if c.is_file():
            target = c
            break
    if not target:
        return result

    # Try fast tail scan first; fall back to full file if no PLANNER_RESPONSE found in tail.
    for tail_limit in (524288, 4 * 1024 * 1024, 10**12):
        try:
            lines = read_transcript_tail_lines(target, tail_bytes=tail_limit)
        except Exception:
            return result
        for line in reversed(lines):
            if '"PLANNER_RESPONSE"' not in line:
                continue
            try:
                step = json.loads(line)
            except Exception:
                continue
            if step.get("type") != "PLANNER_RESPONSE":
                continue
            inp = int(step.get("input_tokens") or 0)
            cr = int(step.get("cache_read_tokens") or 0)
            tot = inp + cr
            if tot <= 0:
                continue
            result["live_tokens"] = tot
            result["input_tokens"] = inp
            result["cache_read_tokens"] = cr
            result["prev_model"] = str(step.get("model") or "").strip() or None
            result["last_ts"] = parse_iso_ts(step.get("created_at"))
            result["last_step_index"] = step.get("step_index")
            return result
        if target.stat().st_size <= tail_limit:
            break
    return result


def collect_workspace_ancestors(workspace_paths: List[str], cwd: str) -> List[Path]:
    """Walk up from cwd / workspace_paths to repo root (containing .git) or workspace root."""
    dirs: List[Path] = []
    seen: set[Path] = set()

    roots: List[Path] = []
    for p in workspace_paths:
        if isinstance(p, str) and p.strip():
            try:
                roots.append(Path(os.path.expanduser(p)).resolve())
            except Exception:
                pass
    if cwd:
        try:
            roots.append(Path(os.path.expanduser(cwd)).resolve())
        except Exception:
            pass

    for start in roots:
        cur = start
        for _ in range(16):
            if cur in seen or not cur.is_dir():
                break
            seen.add(cur)
            dirs.append(cur)
            if (cur / ".git").exists():
                break
            parent = cur.parent
            if parent == cur:
                break
            # Stop climbing once we reach home or filesystem root
            if cur == home_dir():
                break
            cur = parent
    return dirs


def stat_sig(path: Path) -> Optional[str]:
    try:
        st = path.stat()
        return f"{st.st_mtime_ns}:{st.st_size}"
    except Exception:
        return None


def settings_cache_sig(path: Path) -> Optional[str]:
    """Hash only cache-affecting keys in settings.json to avoid UI-noise false positives."""
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(obj, dict):
            return stat_sig(path)
        subset = {k: obj.get(k) for k in SETTINGS_CACHE_KEYS if k in obj}
        digest = hashlib.sha256(json.dumps(subset, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        return f"keys:{digest}"
    except Exception:
        return stat_sig(path)


def add_plugin_dir_files(plugins_root: Path, fp: Dict[str, str]) -> None:
    if not plugins_root.is_dir():
        return
    try:
        entries = sorted(plugins_root.iterdir())
    except Exception:
        return
    for pdir in entries:
        if not pdir.is_dir() or pdir.name.startswith("."):
            continue
        for fname in ("plugin.json", "mcp_config.json", "hooks.json"):
            f = pdir / fname
            sig = stat_sig(f)
            if sig:
                fp[str(f)] = sig
        rules_dir = pdir / "rules"
        if rules_dir.is_dir():
            for rf in sorted(rules_dir.rglob("*.md")):
                sig = stat_sig(rf)
                if sig:
                    fp[str(rf)] = sig
        skills_dir = pdir / "skills"
        if skills_dir.is_dir():
            for sf in sorted(skills_dir.glob("*/SKILL.md")):
                sig = stat_sig(sf)
                if sig:
                    fp[str(sf)] = sig


def add_agent_dir_files(agent_root: Path, fp: Dict[str, str]) -> None:
    if not agent_root.is_dir():
        return
    for fname in ("hooks.json", "mcp_config.json", "rules.json", "skills.json", "plugins.json"):
        f = agent_root / fname
        sig = stat_sig(f)
        if sig:
            fp[str(f)] = sig
    rules_dir = agent_root / "rules"
    if rules_dir.is_dir():
        for rf in sorted(rules_dir.rglob("*.md")):
            sig = stat_sig(rf)
            if sig:
                fp[str(rf)] = sig
    skills_dir = agent_root / "skills"
    if skills_dir.is_dir():
        for sf in sorted(skills_dir.glob("*/SKILL.md")):
            sig = stat_sig(sf)
            if sig:
                fp[str(sf)] = sig
    add_plugin_dir_files(agent_root / "plugins", fp)


def compute_customization_fingerprint(workspace_paths: List[str], cwd: str) -> Dict[str, str]:
    """Snapshot prompt-affecting customization files across global config and workspace."""
    fp: Dict[str, str] = {}
    h = home_dir()
    global_cfg = h / ".gemini" / "config"
    if global_cfg.is_dir():
        for fname in ("config.json", "mcp_config.json", "hooks.json", "rules.json", "skills.json", "plugins.json"):
            f = global_cfg / fname
            sig = stat_sig(f)
            if sig:
                fp[str(f)] = sig
        rules_dir = global_cfg / "rules"
        if rules_dir.is_dir():
            for rf in sorted(rules_dir.rglob("*.md")):
                sig = stat_sig(rf)
                if sig:
                    fp[str(rf)] = sig
        skills_dir = global_cfg / "skills"
        if skills_dir.is_dir():
            for sf in sorted(skills_dir.glob("*/SKILL.md")):
                sig = stat_sig(sf)
                if sig:
                    fp[str(sf)] = sig
        add_plugin_dir_files(global_cfg / "plugins", fp)

    cli_settings = h / ".gemini" / "antigravity-cli" / "settings.json"
    if cli_settings.is_file():
        sig = settings_cache_sig(cli_settings)
        if sig:
            fp[str(cli_settings)] = sig

    for d in collect_workspace_ancestors(workspace_paths, cwd):
        for fname in TOP_RULE_FILES:
            f = d / fname
            sig = stat_sig(f)
            if sig:
                fp[str(f)] = sig
        for ad in AGENT_DIR_NAMES:
            add_agent_dir_files(d / ad, fp)

    return fp


def diff_fingerprints(old_fp: Dict[str, str], new_fp: Dict[str, str]) -> List[Tuple[str, str]]:
    changes: List[Tuple[str, str]] = []
    all_keys = sorted(set(old_fp.keys()) | set(new_fp.keys()))
    for k in all_keys:
        o = old_fp.get(k)
        n = new_fp.get(k)
        if o is None and n is not None:
            changes.append((k, "added"))
        elif o is not None and n is None:
            changes.append((k, "removed"))
        elif o != n:
            changes.append((k, "modified"))
    return changes


def shorten_path(path_str: str) -> str:
    h = str(home_dir())
    if path_str == h:
        return "~"
    if path_str.startswith(h + os.sep):
        return "~" + path_str[len(h) :]
    return path_str


def fmt_tokens(n: int) -> str:
    if n >= 1000:
        return f"~{n / 1000:.1f}k tokens ({n:,})"
    return f"{n:,} tokens"


def fmt_duration(sec: float) -> str:
    s = int(max(0, round(sec)))
    mins, rem = divmod(s, 60)
    hrs, mins = divmod(mins, 60)
    if hrs:
        return f"{hrs}h {mins}m {rem}s"
    if mins:
        return f"{mins}m {rem}s"
    return f"{rem}s"


def extract_tool_path(args: Dict[str, Any], cwd: str) -> Optional[str]:
    for key in ("Path", "TargetFile", "path", "file_path", "FilePath"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            p = os.path.expanduser(val.strip())
            if not os.path.isabs(p) and cwd:
                p = os.path.join(cwd, p)
            try:
                return str(Path(p).resolve())
            except Exception:
                return os.path.abspath(p)
    return None


def extract_tool_command(args: Dict[str, Any]) -> str:
    for key in ("CommandLine", "command", "Command"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def is_customization_path(abs_path: str) -> Tuple[bool, str]:
    """Return (is_match, human_category) if mutating abs_path invalidates prompt cache."""
    try:
        p = Path(abs_path).resolve()
    except Exception:
        p = Path(abs_path)

    name = p.name
    parts = p.parts
    h = home_dir()
    global_cfg = h / ".gemini" / "config"
    cli_settings = h / ".gemini" / "antigravity-cli" / "settings.json"

    # Never flag plugin_data state/config writes
    if "plugin_data" in parts:
        return False, ""

    if p == cli_settings:
        return True, "CLI settings (settings.json)"

    # Global ~/.gemini/config/**
    try:
        rel = p.relative_to(global_cfg)
        rel_parts = rel.parts
        if len(rel_parts) == 1 and name in CONFIG_MANIFEST_FILES:
            return True, f"global config ({name})"
        if rel_parts and rel_parts[0] == "rules":
            return True, "global rule (~/.gemini/config/rules)"
        if rel_parts and rel_parts[0] == "skills":
            return True, "global skill (~/.gemini/config/skills)"
        if rel_parts and rel_parts[0] == "plugins":
            # Only flag prompt-affecting plugin files (manifest, rules, skills, mcp, hooks)
            if name in PLUGIN_PROMPT_FILES or "rules" in rel_parts[2:] or "skills" in rel_parts[2:]:
                return True, f"global plugin customization ({name})"
            return False, ""
    except ValueError:
        pass

    # Workspace top-level GEMINI.md / AGENTS.md
    if name in TOP_RULE_FILES:
        return True, f"workspace rule ({name})"

    # Workspace .agents / .agent / _agents / _agent
    for ad in AGENT_DIR_NAMES:
        if ad in parts:
            idx = parts.index(ad)
            sub = parts[idx + 1 :]
            if len(sub) == 1 and name in CONFIG_MANIFEST_FILES:
                return True, f"workspace customization ({ad}/{name})"
            if sub and sub[0] in ("rules", "skills"):
                return True, f"workspace {sub[0]} ({ad}/{sub[0]})"
            if sub and sub[0] == "plugins":
                if name in PLUGIN_PROMPT_FILES or "rules" in sub[2:] or "skills" in sub[2:]:
                    return True, f"workspace plugin customization ({name})"
                return False, ""

    return False, ""


def inspect_shell_command(cmd: str) -> Tuple[bool, str]:
    """Detect shell commands that mutate prompt customizations."""
    if not cmd:
        return False, ""
    if AGY_MUTATE_RE.search(cmd):
        return True, "CLI plugin/MCP mutation (`agy plugin`/`agy mcp`)"
    if CUSTOM_PATH_HINT_RE.search(cmd) and SHELL_MUTATE_OP_RE.search(cmd):
        return True, "shell command modifying prompt customization files"
    return False, ""


def handle_session_start(payload: Dict[str, Any], data_dir: Path, conv_id: str) -> Optional[Dict[str, Any]]:
    ws = payload.get("workspacePaths") if isinstance(payload.get("workspacePaths"), list) else []
    cwd = str(payload.get("cwd") or "")
    state = load_session_state(data_dir, conv_id)
    state["fingerprint"] = compute_customization_fingerprint(ws, cwd)
    state["updated_at"] = time.time()
    save_session_state(data_dir, conv_id, state)
    return None


def handle_pre_tool_use(
    payload: Dict[str, Any], cfg: Dict[str, Any], data_dir: Path, conv_id: str
) -> Optional[Dict[str, Any]]:
    tstate = read_transcript_state(str(payload.get("transcriptPath") or ""))
    live_tokens = tstate["live_tokens"]
    if live_tokens < cfg["min_tokens"]:
        return None

    tc = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
    tool_name = str(tc.get("name") or "")
    args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
    cwd = str(payload.get("cwd") or "")

    hit = False
    what = ""
    why = ""

    if tool_name in FILE_MUTATION_TOOLS and cfg.get("check_customization_edits", True):
        fp = extract_tool_path(args, cwd)
        if fp:
            matched, category = is_customization_path(fp)
            if matched:
                hit = True
                what = f"`{tool_name}` on {shorten_path(fp)} ({category})"
                why = "Mutating prompt customization files invalidates ~100% of the cached system-prompt prefix on the next turn."

    elif tool_name in SHELL_TOOLS and cfg.get("check_customization_edits", True):
        cmd = extract_tool_command(args)
        matched, category = inspect_shell_command(cmd)
        if matched:
            hit = True
            snippet = cmd if len(cmd) <= 120 else cmd[:117] + "..."
            what = f"`{tool_name}`: `{snippet}` ({category})"
            why = "Modifying rules, skills, plugins, MCP, or settings invalidates ~100% of the cached prefix on the next turn."

    elif tool_name == "define_subagent" and cfg.get("check_define_subagent", True):
        hit = True
        sub_name = str(args.get("Name") or "subagent")
        what = f"`define_subagent` (`{sub_name}`)"
        why = "Defining a new subagent mid-session can alter the `<subagents>` system-prompt prefix on the next turn."

    if not hit:
        return None

    reason = (
        f"[CACHE-GUARD] Massive cache miss warning!\n"
        f"About to run: {what}\n"
        f"At risk: {fmt_tokens(live_tokens)} cached context.\n"
        f"Why: {why}\n"
        f"Approve to proceed anyway, or Reject to defer this change to a fresh session."
    )
    return {
        "decision": cfg["pretool_decision"],
        "reason": reason,
    }


def handle_post_tool_use(payload: Dict[str, Any], data_dir: Path, conv_id: str) -> Optional[Dict[str, Any]]:
    # If the user approved a customization edit in PreToolUse and it succeeded,
    # refresh the baseline fingerprint so PreInvocation doesn't double-stop on the same edit.
    if payload.get("error"):
        return None
    tc = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
    tool_name = str(tc.get("name") or "")
    args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
    cwd = str(payload.get("cwd") or "")

    should_refresh = False
    if tool_name in FILE_MUTATION_TOOLS:
        fp = extract_tool_path(args, cwd)
        if fp and is_customization_path(fp)[0]:
            should_refresh = True
    elif tool_name in SHELL_TOOLS:
        if inspect_shell_command(extract_tool_command(args))[0]:
            should_refresh = True

    if should_refresh:
        ws = payload.get("workspacePaths") if isinstance(payload.get("workspacePaths"), list) else []
        state = load_session_state(data_dir, conv_id)
        state["fingerprint"] = compute_customization_fingerprint(ws, cwd)
        state["updated_at"] = time.time()
        save_session_state(data_dir, conv_id, state)
    return None


def handle_pre_invocation(
    payload: Dict[str, Any], cfg: Dict[str, Any], data_dir: Path, conv_id: str
) -> Optional[Dict[str, Any]]:
    now = time.time()
    ws = payload.get("workspacePaths") if isinstance(payload.get("workspacePaths"), list) else []
    cwd = str(payload.get("cwd") or "")
    state = load_session_state(data_dir, conv_id)

    curr_fp = compute_customization_fingerprint(ws, cwd)
    old_fp = state.get("fingerprint") if isinstance(state.get("fingerprint"), dict) else None

    tstate = read_transcript_state(str(payload.get("transcriptPath") or ""))
    live_tokens = tstate["live_tokens"]
    prev_model = tstate["prev_model"]
    last_ts = tstate["last_ts"]
    last_step = tstate["last_step_index"]

    # Below threshold: silently keep baseline fingerprint current and exit.
    if live_tokens < cfg["min_tokens"]:
        state["fingerprint"] = curr_fp
        state["updated_at"] = now
        save_session_state(data_dir, conv_id, state)
        return None

    reasons: List[str] = []
    sig_parts: List[Any] = [last_step]

    # 1. Model / effort switch
    curr_model = str(payload.get("modelName") or "").strip()
    if (
        cfg.get("check_model_switch", True)
        and curr_model
        and curr_model.lower() != "auto"
        and prev_model
        and curr_model.lower() != prev_model.lower()
    ):
        reasons.append(
            f"• Model switched mid-session: `{prev_model}` → `{curr_model}` "
            f"(100% cache miss; prompt caches are per-model)."
        )
        sig_parts.append(("model_switch", prev_model.lower(), curr_model.lower()))

    # 2. External customization file drift on disk
    drift_changes: List[Tuple[str, str]] = []
    if cfg.get("check_customization_edits", True) and old_fp is not None:
        drift_changes = diff_fingerprints(old_fp, curr_fp)
        if drift_changes:
            shown = drift_changes[:6]
            file_lines = "\n".join(f"    - {shorten_path(p)} ({kind})" for p, kind in shown)
            extra = f"\n    - ...and {len(drift_changes) - len(shown)} more" if len(drift_changes) > len(shown) else ""
            reasons.append(
                f"• Prompt customization files changed on disk (~100% prefix invalidation):\n{file_lines}{extra}"
            )
            sig_parts.append(("custom_drift", [(p, k, curr_fp.get(p)) for p, k in drift_changes]))

    # 3. Idle cache TTL expiration
    if cfg.get("check_idle_ttl", True) and last_ts is not None:
        idle_sec = now - last_ts
        if idle_sec >= cfg["idle_ttl_seconds"]:
            reasons.append(
                f"• Idle cache TTL likely expired: {fmt_duration(idle_sec)} since last model call "
                f"(threshold: {fmt_duration(cfg['idle_ttl_seconds'])})."
            )
            sig_parts.append(("idle_ttl", last_step))

    if not reasons:
        if old_fp is None:
            state["fingerprint"] = curr_fp
            state["updated_at"] = now
            save_session_state(data_dir, conv_id, state)
        return None

    sig_hash = hashlib.sha256(json.dumps(sig_parts, sort_keys=True).encode("utf-8")).hexdigest()[:20]

    # Check if user already confirmed this exact warning on retry
    pending = state.get("pending_confirm") if isinstance(state.get("pending_confirm"), dict) else None
    window = cfg["retry_confirm_window_seconds"]
    if pending and pending.get("sig") == sig_hash and (now - float(pending.get("ts") or 0)) <= window:
        state["fingerprint"] = curr_fp
        state["pending_confirm"] = None
        state["last_confirmed_sig"] = sig_hash
        state["updated_at"] = now
        save_session_state(data_dir, conv_id, state)
        return None

    # Also skip if already confirmed on this exact step + sig (e.g. multiple PreInvocation in same turn)
    if state.get("last_confirmed_sig") == sig_hash:
        return None

    # Record pending confirmation and terminate BEFORE calling the model API
    state["pending_confirm"] = {"sig": sig_hash, "ts": now, "step": last_step}
    if old_fp is None:
        state["fingerprint"] = curr_fp
    state["updated_at"] = now
    save_session_state(data_dir, conv_id, state)

    window_min = max(1, round(window / 60))
    msg = (
        f"CACHE-GUARD STOP — Massive prompt cache miss about to happen\n\n"
        f"At risk: {fmt_tokens(live_tokens)} live context.\n"
        f"Cause:\n"
        + "\n".join(reasons)
        + f"\n\nNext steps:\n"
        f"  • Send your message again within {window_min} min to confirm and proceed anyway.\n"
        f"  • Or undo the change (e.g. switch back with `/model`), run `/compact`, or start a fresh session."
    )
    return {
        "injectSteps": [
            {
                "errorMessage": {
                    "userMessage": msg,
                }
            }
        ],
        "terminationBehavior": "terminate",
    }


def dispatch(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    data_dir = resolve_data_dir(payload)
    cfg = load_config(data_dir)
    if not cfg.get("enabled", True):
        return None

    event = str(payload.get("hookEventName") or "")
    conv_id = safe_conv_id(payload)

    if event == "SessionStart":
        return handle_session_start(payload, data_dir, conv_id)
    if event == "PreToolUse":
        return handle_pre_tool_use(payload, cfg, data_dir, conv_id)
    if event == "PostToolUse":
        return handle_post_tool_use(payload, data_dir, conv_id)
    if event == "PreInvocation":
        return handle_pre_invocation(payload, cfg, data_dir, conv_id)
    return None


def main() -> int:
    try:
        raw = sys.stdin.read().strip()
        if not raw:
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        out = dispatch(payload)
        if out:
            sys.stdout.write(json.dumps(out) + "\n")
    except Exception as exc:
        sys.stderr.write(f"[cache-guard] error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
