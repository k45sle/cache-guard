#!/usr/bin/env python3
"""
cache-guard: an Antigravity hook that pauses before something makes a long chat
lose its prompt cache, which would make the model re-read the whole chat at the
full price.

The rules come from measurements on agy 1.5.x (see docs/EVIDENCE.md):

- Antigravity builds the prompt at the start of each turn (PreInvocation with
  invocationNum 0). Rule text, skill names and descriptions (SKILL.md
  frontmatter), MCP config and the list of enabled plugins are part of it.
  hooks.json and skill bodies are not, so editing those costs nothing.
- Each model id has its own cache, and the effort level is part of the id.
  Going back to a model used a few minutes ago reuses that model's cache, but an
  Orchestrator hand-off from Planner to Executor never does. Those hand-offs
  are how the Orchestrator works, so they are let through.
- A cache expires after an idle gap that depends on the provider.

Events:
  SessionStart   save a fingerprint of the prompt-relevant content.
  PreToolUse     ask before a tool call that would change that content.
  PostToolUse    remember an approved change, so the next turn is not stopped again.
  PreInvocation  stop before the model call when the model changed, or (at turn
                 start) the prompt content changed or the cache expired. Sending
                 the same message again goes ahead.

Python 3.9+, standard library only. Any internal error fails open (no output).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

VERSION = "0.2.0"

# How long a model keeps an unused prompt cache, in seconds: the longest idle wait
# the cache was seen to survive (see docs/EVIDENCE.md). A key matches when it is part
# of the model id; the longest matching key wins and "default" covers everything else.
DEFAULT_IDLE_TTL: Dict[str, int] = {
    "claude": 300,  # alive at 4.1 min, gone at 6.2 (lab)
    "gemini": 450,  # Gemini 3.8 Flash: alive at 7.6 min, gone at 10.1 (lab)
    "gemini-pro": 300,  # gemini-pro-orchestrator-g1-pro: alive at 5.1 min, gone from 5.6 (logs)
    "gemini-4-neon": 900,  # alive at 13.1 and 15 min (lab); no clean "gone" point yet
    "orchestrator-neon": 900,  # warm after 12.4 min (logs); assumed to follow Gemini 4 Neon
    "gpt": 300,  # not measured
    "default": 300,  # not measured
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "min_tokens": 60000,
    "check_customization_edits": True,
    "check_model_switch": True,
    "check_idle_ttl": True,
    "idle_ttl_seconds": dict(DEFAULT_IDLE_TTL),
    "check_define_subagent": True,
    "pretool_decision": "force_ask",
    "retry_confirm_window_seconds": 180,
}

# The file v0.1 wrote on first run. A config that still equals it was never
# edited, so it is replaced with the current defaults.
V01_SEEDED_CONFIG: Dict[str, Any] = {
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

# The agy-mesh bundle of v0.1 also seeded its Orchestrator Edit Guard keys, with
# these values (that guard now lives in agy-mesh). A file that differs from
# V01_SEEDED_CONFIG only by these exact values was never edited either.
V01_BUNDLE_SEEDED_EXTRA: Dict[str, Any] = {
    "check_orchestrator_edits": True,
    "max_orchestrator_files": 2,
    "max_orchestrator_edits": 4,
    "orchestrator_edit_decision": "force_ask",
}

FILE_WRITE_TOOLS = {"write_file", "edit_file", "write_to_file", "replace_file_content", "multi_replace_file_content"}
SHELL_TOOLS = {"run_command", "Bash"}
GUARDED_TOOLS = FILE_WRITE_TOOLS | SHELL_TOOLS | {"define_subagent"}

RULE_NAMES = ("GEMINI.md", "AGENTS.md")
AGENT_DIR_NAMES = (".agents", ".agent", "_agents", "_agent")
LIST_NAMES = ("skills.json", "plugins.json", "rules.json")
# settings.json keys that can change the prompt. "model" is left out on purpose:
# a model change is judged by the model check, which knows about warm caches.
SETTINGS_PROMPT_KEYS = ("agentMode", "mcp", "mcpServers", "plugins")
# plugin.json keys that feed the UI, hooks or other platforms, never the prompt.
PLUGIN_NON_PROMPT_KEYS = {
    "version",
    "description",
    "displayName",
    "author",
    "homepage",
    "repository",
    "logo",
    "keywords",
    "license",
    "category",
    "suggestedPrompts",
    "hooks",
    "extensions",
}
MAX_TEXT_BYTES = 2 * 1024 * 1024
STATE_MAX_AGE_SECONDS = 30 * 24 * 3600

KIND_LABELS = {
    "rule": "rule",
    "skill": "skill name or description",
    "mcp": "MCP servers",
    "plugins": "plugin list",
    "manifest": "plugin manifest",
    "list": "skill or plugin list",
    "settings": "Antigravity settings",
    "folder": "folder with rules, skills or plugins",
    "agy": "plugin or MCP install",
    "script": "script that writes prompt files",
    "shell": "shell command that writes prompt files",
    "subagent": "new subagent",
}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def home_dir() -> Path:
    return Path(os.path.expanduser("~"))


def real(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path


def abs_path(raw: str, cwd: str) -> Path:
    """Absolute, normalised path for a tool argument (no symlink resolution)."""
    p = os.path.expanduser(raw.strip())
    if p.startswith("$HOME/") or p == "$HOME":
        p = str(home_dir()) + p[len("$HOME") :]
    elif p.startswith("${HOME}"):
        p = str(home_dir()) + p[len("${HOME}") :]
    if not os.path.isabs(p):
        p = os.path.join(cwd or os.getcwd(), p)
    return Path(os.path.normpath(p))


def is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def read_text(path: Path) -> Optional[str]:
    try:
        if not path.is_file() or path.stat().st_size > MAX_TEXT_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def shorten_path(path: Any) -> str:
    s = str(path)
    h = str(home_dir())
    if s == h:
        return "~"
    if s.startswith(h + os.sep):
        return "~" + s[len(h) :]
    return s


def fmt_tokens(n: int) -> str:
    if n >= 1000:
        return f"about {round(n, -2):,} tokens"
    return f"{n:,} tokens"


def fmt_gap(sec: float) -> str:
    s = int(max(0, round(sec)))
    if s < 90:
        return f"{s} s"
    mins = round(s / 60)
    if mins < 120:
        return f"{mins} min"
    return f"{mins // 60} h {mins % 60} min"


def norm_model(name: Any) -> str:
    """'Gemini 4 Neon (High)' and 'gemini-4-neon-high' compare equal."""
    s = str(name or "").strip().lower()
    s = re.sub(r"[()]", " ", s)
    s = re.sub(r"[\s_]+", "-", s.strip())
    return re.sub(r"-{2,}", "-", s).strip("-")


def real_model(name: str) -> bool:
    return bool(name) and name != "auto" and not name.startswith("model-placeholder")


def model_family(name: str) -> str:
    m = name.lower()
    for fam in ("claude", "gemini", "gpt"):
        if fam in m:
            return fam
    return "default"


def parse_iso_ts(val: Any) -> Optional[float]:
    if not isinstance(val, str) or not val:
        return None
    s = val.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Python 3.9 fromisoformat accepts at most 6 fractional digits.
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# --------------------------------------------------------------------------- #
# Config and per-conversation state
# --------------------------------------------------------------------------- #


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


def clean_ttl(value: Any) -> Any:
    """idle_ttl_seconds is one number for every model, or {model key: seconds}."""
    if isinstance(value, bool):
        return dict(DEFAULT_IDLE_TTL)
    if isinstance(value, (int, float, str)):
        try:
            return max(30, int(value))
        except (TypeError, ValueError):
            return dict(DEFAULT_IDLE_TTL)
    if isinstance(value, dict):
        out = dict(DEFAULT_IDLE_TTL)
        for k, v in value.items():
            key = norm_model(k)
            if not key or isinstance(v, bool):
                continue
            try:
                out[key] = max(30, int(v))
            except (TypeError, ValueError):
                continue
        return out
    return dict(DEFAULT_IDLE_TTL)


def ttl_for(cfg: Dict[str, Any], model: str) -> int:
    ttl = cfg.get("idle_ttl_seconds")
    if isinstance(ttl, int):
        return ttl
    if not isinstance(ttl, dict):
        ttl = DEFAULT_IDLE_TTL
    m = norm_model(model)
    best_key = ""
    for key in ttl:
        if key != "default" and key in m and len(key) > len(best_key):
            best_key = key
    return int(ttl[best_key]) if best_key else int(ttl.get("default", DEFAULT_IDLE_TTL["default"]))


def is_v01_seeded(user_cfg: Dict[str, Any]) -> bool:
    """True for a file exactly as v0.1, or its agy-mesh bundle, wrote it on first run."""
    rest = dict(user_cfg)
    for key, value in V01_BUNDLE_SEEDED_EXTRA.items():
        if key in rest and type(rest[key]) is type(value) and rest[key] == value:
            del rest[key]
    return rest == V01_SEEDED_CONFIG


def load_config(data_dir: Path) -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    own = data_dir / "config.json"
    candidates = [own]
    # A set PLUGIN_DATA (a test or lab copy) reads only its own folder, never the
    # main install's file.
    if not os.environ.get("PLUGIN_DATA", "").strip():
        canon = canonical_data_dir() / "config.json"
        if canon != own:
            candidates.append(canon)

    loaded = False
    for path in candidates:
        if not path.is_file():
            continue
        try:
            user_cfg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(user_cfg, dict):
            continue
        if is_v01_seeded(user_cfg):
            try:
                atomic_write_json(path, DEFAULT_CONFIG)
            except OSError:
                pass
            user_cfg = dict(DEFAULT_CONFIG)
        cfg.update(user_cfg)
        loaded = True
        break

    # Seed the folder this app uses, but never replace a file that is there and
    # unreadable: it holds the user's edits.
    if not loaded and not own.exists():
        try:
            atomic_write_json(own, DEFAULT_CONFIG)
        except OSError:
            pass

    try:
        cfg["min_tokens"] = max(0, int(cfg.get("min_tokens", DEFAULT_CONFIG["min_tokens"])))
    except (TypeError, ValueError):
        cfg["min_tokens"] = DEFAULT_CONFIG["min_tokens"]
    cfg["idle_ttl_seconds"] = clean_ttl(cfg.get("idle_ttl_seconds"))
    try:
        cfg["retry_confirm_window_seconds"] = max(15, int(cfg.get("retry_confirm_window_seconds", 180)))
    except (TypeError, ValueError):
        cfg["retry_confirm_window_seconds"] = DEFAULT_CONFIG["retry_confirm_window_seconds"]
    if cfg.get("pretool_decision") not in ("force_ask", "ask", "deny"):
        cfg["pretool_decision"] = "force_ask"
    return cfg


def safe_conv_id(payload: Dict[str, Any]) -> str:
    raw = str(payload.get("conversationId") or "").strip()
    if re.fullmatch(r"[0-9A-Za-z_-]{6,128}", raw):
        return raw
    return "default"


def load_state(data_dir: Path, conv_id: str) -> Dict[str, Any]:
    path = data_dir / "state" / f"{conv_id}.json"
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(obj, dict):
            return obj
    except (OSError, ValueError):
        pass
    return {}


def save_state(data_dir: Path, conv_id: str, state: Dict[str, Any]) -> None:
    state["updated_at"] = time.time()
    state["version"] = VERSION
    try:
        atomic_write_json(data_dir / "state" / f"{conv_id}.json", state)
    except OSError:
        pass


def prune_state(data_dir: Path) -> None:
    cutoff = time.time() - STATE_MAX_AGE_SECONDS
    try:
        entries = list((data_dir / "state").glob("*.json"))[:500]
    except OSError:
        return
    for f in entries:
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            continue


# --------------------------------------------------------------------------- #
# Transcript: the provider's own token counts for every model call
# --------------------------------------------------------------------------- #


def _transcript_file(transcript_path: str) -> Optional[Path]:
    if not transcript_path:
        return None
    first = Path(transcript_path)
    candidates = [first]
    if first.name == "transcript_full.jsonl":
        candidates.append(first.with_name("transcript.jsonl"))
    elif first.name == "transcript.jsonl":
        candidates.append(first.with_name("transcript_full.jsonl"))
    for c in candidates:
        if c.is_file():
            return c
    return None


def _tail_lines(path: Path, tail_bytes: int) -> List[str]:
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > tail_bytes:
            fh.seek(size - tail_bytes)
            fh.readline()
        raw = fh.read()
    return raw.decode("utf-8", errors="replace").splitlines()


def _planner_calls_backwards(transcript_path: str, max_bytes: int = 16 * 1024 * 1024) -> Iterable[Dict[str, Any]]:
    """Yield PLANNER_RESPONSE steps with token counts, newest first."""
    target = _transcript_file(transcript_path)
    if target is None:
        return
    seen_upto = 0
    for limit in (512 * 1024, 4 * 1024 * 1024, max_bytes):
        try:
            lines = _tail_lines(target, limit)
            size = target.stat().st_size
        except OSError:
            return
        for i, line in enumerate(reversed(lines)):
            if i < seen_upto or '"PLANNER_RESPONSE"' not in line:
                continue
            try:
                step = json.loads(line)
            except ValueError:
                continue
            if step.get("type") != "PLANNER_RESPONSE":
                continue
            inp = int(step.get("input_tokens") or 0)
            cr = int(step.get("cache_read_tokens") or 0)
            if inp + cr <= 0:
                continue
            yield step
        seen_upto = len(lines)
        if size <= limit:
            return


def _call_info(step: Dict[str, Any]) -> Dict[str, Any]:
    inp = int(step.get("input_tokens") or 0)
    cr = int(step.get("cache_read_tokens") or 0)
    tools = []
    for tc in step.get("tool_calls") or []:
        if isinstance(tc, dict) and tc.get("name"):
            tools.append(str(tc.get("name")))
    return {
        "live": inp + cr,
        "model": str(step.get("model") or "").strip(),
        "ts": parse_iso_ts(step.get("created_at")),
        "step": step.get("step_index"),
        "tools": tools,
    }


def read_last_call(transcript_path: str) -> Dict[str, Any]:
    for step in _planner_calls_backwards(transcript_path):
        return _call_info(step)
    return {"live": 0, "model": "", "ts": None, "step": None, "tools": []}


def find_model_last_use(transcript_path: str, model: str) -> Optional[Dict[str, Any]]:
    want = norm_model(model)
    for step in _planner_calls_backwards(transcript_path):
        info = _call_info(step)
        if norm_model(info["model"]) == want:
            return info
    return None


def handoff_pairs(transcript_path: str) -> Set[Tuple[str, str]]:
    """(planner, executor) for each Orchestrator hand-off in this chat: a call that ran
    exit_plan_mode, followed by a call on another model."""
    pairs: Set[Tuple[str, str]] = set()
    newer = ""
    for step in _planner_calls_backwards(transcript_path):
        info = _call_info(step)
        model = norm_model(info["model"])
        if "exit_plan_mode" in info["tools"] and real_model(model) and real_model(newer) and model != newer:
            pairs.add((model, newer))
        newer = model
    return pairs


# --------------------------------------------------------------------------- #
# Scope: which files feed this conversation's prompt
# --------------------------------------------------------------------------- #


def _json_or_text(text: str) -> str:
    try:
        return json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"))
    except ValueError:
        return text


def skill_frontmatter(text: str) -> str:
    """The YAML block between the leading '---' lines; the whole file if none."""
    body = text.lstrip("﻿")
    lines = body.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(line.rstrip() for line in lines[1:i])
    return body


def project_text(kind: str, text: Optional[str]) -> Optional[str]:
    """The part of a customization file that ends up in the prompt."""
    if text is None:
        return None
    if kind == "rule":
        return "rule:" + text
    if kind == "skill":
        return "skill:" + skill_frontmatter(text)
    if kind in ("mcp", "list"):
        return f"{kind}:" + _json_or_text(text)
    try:
        obj = json.loads(text)
    except ValueError:
        return f"{kind}:" + text
    if not isinstance(obj, dict):
        return f"{kind}:" + _json_or_text(text)
    if kind == "plugins":  # ~/.gemini/config/config.json
        plugins = obj.get("plugins") if isinstance(obj.get("plugins"), dict) else {}
        enabled = {}
        for name, val in plugins.items():
            enabled[name] = bool(val.get("enabled", True)) if isinstance(val, dict) else bool(val)
        return "plugins:" + json.dumps(enabled, sort_keys=True)
    if kind == "manifest":
        subset = {k: v for k, v in obj.items() if k not in PLUGIN_NON_PROMPT_KEYS}
        return "manifest:" + json.dumps(subset, sort_keys=True)
    if kind == "settings":
        subset = {k: obj.get(k) for k in SETTINGS_PROMPT_KEYS if k in obj}
        return "settings:" + json.dumps(subset, sort_keys=True)
    return f"{kind}:" + _json_or_text(text)


def _rel_kind(rtype: str, parts: Tuple[str, ...]) -> Optional[str]:
    """Kind of prompt file at this path inside a root, or None."""
    n = len(parts)
    if n == 0:
        return None
    name = parts[-1]
    if rtype == "workspace":
        if n == 1 and name in RULE_NAMES:
            return "rule"
        if parts[0] in AGENT_DIR_NAMES:
            return _rel_kind("agent", parts[1:])
        return None
    if rtype == "skills":
        return "skill" if n == 2 and name == "SKILL.md" else None
    if n == 1:
        if rtype == "global":
            if name in RULE_NAMES:
                return "rule"
            if name == "config.json":
                return "plugins"
        if rtype == "plugin" and name == "plugin.json":
            return "manifest"
        if name == "mcp_config.json":
            return "mcp"
        if name in LIST_NAMES and rtype in ("global", "agent"):
            return "list"
        return None
    head = parts[0]
    if head == "rules":
        return "rule" if name.endswith(".md") else None
    if head == "skills":
        return "skill" if n == 3 and name == "SKILL.md" else None
    if head == "plugins" and rtype in ("global", "agent") and n > 2:
        return _rel_kind("plugin", parts[2:])
    return None


def _rel_container(rtype: str, parts: Tuple[str, ...]) -> bool:
    """True when a folder at this path inside a root can hold prompt files."""
    n = len(parts)
    if rtype == "workspace":
        return n >= 1 and parts[0] in AGENT_DIR_NAMES and _rel_container("agent", parts[1:])
    if rtype == "skills":
        return n <= 1
    if n == 0:
        return True
    head = parts[0]
    if head == "rules":
        return True
    if head == "skills":
        return n <= 2
    if head == "plugins" and rtype in ("global", "agent"):
        return n <= 2 or _rel_container("plugin", parts[2:])
    return False


class Scope:
    """The customization roots and files that feed one conversation's prompt."""

    def __init__(self, workspace_paths: Iterable[Any], cwd: str):
        self.home = home_dir()
        self.cwd = cwd or str(self.home)
        self.roots: List[Tuple[Path, str, bool]] = []  # (root, type, came from a workspace)
        self.extra: Dict[Path, str] = {}
        self._root_seen: Set[Tuple[str, str]] = set()
        self._files: Optional[Dict[str, Tuple[Path, str]]] = None
        self._from_ws = False
        g = self.home / ".gemini" / "config"
        self.global_dir = g
        self.disabled_plugins = self._disabled_plugins(g / "config.json")
        self._add_root(g, "global")
        self._add_plugins(g / "plugins")
        for ad in AGENT_DIR_NAMES:
            self._add_root(self.home / ad, "agent")
            self._add_plugins(self.home / ad / "plugins")
        self._add_skill_registry(g / "skills.json")
        for settings in (
            self.home / ".gemini" / "antigravity-cli" / "settings.json",
            self.home / ".gemini" / "antigravity" / "settings.json",
        ):
            self.extra[settings] = "settings"
        self.extra[self.home / ".gemini" / "GEMINI.md"] = "rule"
        self._from_ws = True
        for d in self._workspace_dirs(workspace_paths):
            self._add_root(d, "workspace")
            for ad in AGENT_DIR_NAMES:
                if (d / ad).is_dir():
                    self._add_root(d / ad, "agent")
                    self._add_plugins(d / ad / "plugins")

    # -- building ---------------------------------------------------------- #

    def _add_root(self, path: Path, rtype: str) -> None:
        for p in (path, real(path)):
            key = (str(p), rtype)
            if key not in self._root_seen:
                self._root_seen.add(key)
                self.roots.append((p, rtype, self._from_ws))

    @staticmethod
    def _disabled_plugins(config_json: Path) -> Set[str]:
        text = read_text(config_json)
        if not text:
            return set()
        try:
            obj = json.loads(text)
        except ValueError:
            return set()
        plugins = obj.get("plugins") if isinstance(obj, dict) else None
        if not isinstance(plugins, dict):
            return set()
        return {str(k) for k, v in plugins.items() if isinstance(v, dict) and v.get("enabled") is False}

    def _add_plugins(self, plugins_dir: Path) -> None:
        try:
            entries = sorted(plugins_dir.iterdir()) if plugins_dir.is_dir() else []
        except OSError:
            return
        for pdir in entries:
            if not pdir.is_dir() or pdir.name.startswith("."):
                continue
            manifest_text = read_text(pdir / "plugin.json")
            manifest: Dict[str, Any] = {}
            if manifest_text:
                try:
                    obj = json.loads(manifest_text)
                    manifest = obj if isinstance(obj, dict) else {}
                except ValueError:
                    manifest = {}
            names = {pdir.name, str(manifest.get("name") or pdir.name)}
            if names & self.disabled_plugins:
                continue
            self._add_root(pdir, "plugin")
            skills = manifest.get("skills")
            for rel in [skills] if isinstance(skills, str) else (skills if isinstance(skills, list) else []):
                if isinstance(rel, str) and rel.strip():
                    self._add_root(Path(os.path.normpath(str(pdir / rel))), "skills")
            mcp = manifest.get("mcpServers")
            if isinstance(mcp, str) and mcp.strip():
                self.extra[Path(os.path.normpath(str(pdir / mcp)))] = "mcp"

    def _add_skill_registry(self, skills_json: Path) -> None:
        text = read_text(skills_json)
        if not text:
            return
        try:
            obj = json.loads(text)
        except ValueError:
            return
        entries = obj.get("entries") if isinstance(obj, dict) else None
        for e in entries if isinstance(entries, list) else []:
            raw = e.get("path") if isinstance(e, dict) else e
            if isinstance(raw, str) and raw.strip():
                self._add_root(abs_path(raw, str(self.global_dir)), "skills")

    def _workspace_dirs(self, workspace_paths: Iterable[Any]) -> List[Path]:
        """Each workspace root and cwd, walking up to the repo root or home."""
        starts: List[Path] = []
        for p in list(workspace_paths or []) + [self.cwd]:
            if isinstance(p, str) and p.strip():
                starts.append(abs_path(p, str(self.home)))
        out: List[Path] = []
        seen: Set[Path] = set()
        for start in starts:
            cur = start
            for _ in range(16):
                if cur in seen or not cur.is_dir():
                    break
                seen.add(cur)
                out.append(cur)
                if (cur / ".git").exists() or cur == self.home or cur.parent == cur:
                    break
                cur = cur.parent
        return out

    # -- queries ----------------------------------------------------------- #

    def files(self) -> Dict[str, Tuple[Path, str]]:
        """Existing prompt files: str(path) -> (path, kind). One entry per real file."""
        if self._files is not None:
            return self._files
        found: Dict[str, Tuple[Path, str]] = {}
        seen_real: Set[str] = set()

        def add(path: Path, kind: str) -> None:
            if not path.is_file():
                return
            r = str(real(path))
            if r in seen_real:
                return
            seen_real.add(r)
            found[str(path)] = (path, kind)

        for root, rtype, _ws in self.roots:
            if not root.is_dir():
                continue
            if rtype == "workspace":
                for n in RULE_NAMES:
                    add(root / n, "rule")
                continue
            if rtype == "skills":
                for sf in sorted(root.glob("*/SKILL.md")):
                    add(sf, "skill")
                continue
            if rtype == "global":
                for n in RULE_NAMES:
                    add(root / n, "rule")
                add(root / "config.json", "plugins")
            add(root / "mcp_config.json", "mcp")
            if rtype == "plugin":
                add(root / "plugin.json", "manifest")
            else:
                for n in LIST_NAMES:
                    add(root / n, "list")
            rules = root / "rules"
            if rules.is_dir():
                for rf in sorted(rules.rglob("*.md")):
                    add(rf, "rule")
            skills = root / "skills"
            if skills.is_dir():
                for sf in sorted(skills.glob("*/SKILL.md")):
                    add(sf, "skill")
        for path, kind in self.extra.items():
            add(path, kind)
        self._files = found
        return found

    def projection(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for key, (path, kind) in self.files().items():
            proj = project_text(kind, read_text(path))
            if proj is not None:
                out[key] = f"{kind}:{digest(proj)}"
        return out

    def _parts_in(self, cand: Path, root: Path, rtype: str) -> Optional[Tuple[str, ...]]:
        """Path parts below root, or None when outside it or inside a disabled plugin."""
        if not is_under(cand, root):
            return None
        parts = cand.relative_to(root).parts
        if rtype in ("global", "agent") and len(parts) > 1 and parts[0] == "plugins":
            if parts[1] in self.disabled_plugins:
                return None
        if rtype == "plugin" and root.name in self.disabled_plugins:
            return None
        return parts

    def classify_file(self, path: Path) -> Optional[str]:
        """Kind of prompt file this path is (or would be if created), else None."""
        cands = (path, real(path))
        for cand in cands:
            if cand in self.extra:
                return self.extra[cand]
        for cand in cands:
            for root, rtype, _ws in self.roots:
                parts = self._parts_in(cand, root, rtype)
                if parts is not None:
                    kind = _rel_kind(rtype, parts)
                    if kind:
                        return kind
        return None

    def folder_affects_prompt(self, path: Path, include_workspace: bool = True) -> bool:
        """True when changing this folder's contents can change the prompt.

        include_workspace=False ignores roots found through the workspace: used for
        git, where a pull in a project repo rarely touches its rule files and the
        turn-start check still catches it.
        """
        cands = {path, real(path)}
        for cand in cands:
            for root, rtype, from_ws in self.roots:
                if from_ws and not include_workspace:
                    continue
                parts = self._parts_in(cand, root, rtype)
                if parts is not None and _rel_container(rtype, parts):
                    return True
        for fpath, _kind in self.files().values():
            if not include_workspace and self._only_workspace(fpath):
                continue
            for target in cands:
                if is_under(fpath, target) or is_under(real(fpath), target):
                    return True
        return False

    def _only_workspace(self, fpath: Path) -> bool:
        for cand in (fpath, real(fpath)):
            for root, _rtype, from_ws in self.roots:
                if not from_ws and is_under(cand, root):
                    return False
        return True

    def affected(self, path: Path, folder_ok: bool = True) -> Optional[Tuple[str, Path]]:
        kind = self.classify_file(path)
        if kind:
            return kind, path
        if folder_ok and self.folder_affects_prompt(path):
            return "folder", path
        return None


def diff_projection(old: Dict[str, str], new: Dict[str, str]) -> List[Tuple[str, str, str]]:
    out: List[Tuple[str, str, str]] = []
    for key in sorted(set(old) | set(new)):
        o, n = old.get(key), new.get(key)
        if o == n:
            continue
        kind = (n or o or "rule:").split(":", 1)[0]
        change = "added" if o is None else "removed" if n is None else "changed"
        out.append((key, kind, change))
    return out


def projection_hash(proj: Dict[str, str]) -> str:
    return digest(json.dumps(proj, sort_keys=True))


# --------------------------------------------------------------------------- #
# Tool calls: would this change the prompt?
# --------------------------------------------------------------------------- #


def extract_tool_path(args: Dict[str, Any], cwd: str) -> Optional[Path]:
    for key in ("Path", "TargetFile", "path", "file_path", "FilePath", "AbsolutePath"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return abs_path(val, cwd)
    return None


def extract_tool_command(args: Dict[str, Any]) -> str:
    for key in ("CommandLine", "command", "Command"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _replace(text: str, target: Any, repl: Any, allow_multiple: Any = False) -> Optional[str]:
    if not isinstance(target, str) or not isinstance(repl, str) or not target:
        return None
    if target not in text:
        return text  # the edit will fail, so nothing changes
    return text.replace(target, repl) if allow_multiple else text.replace(target, repl, 1)


def simulate_file_tool(name: str, args: Dict[str, Any], old: Optional[str]) -> Optional[str]:
    """File text after the tool runs; None when it cannot be worked out."""
    if name == "write_file":
        content = args.get("Content")
        return content if isinstance(content, str) else None
    if name == "write_to_file":
        content = args.get("CodeContent", args.get("Content"))
        return content if isinstance(content, str) else None
    if old is None:
        return None
    if name == "edit_file":
        return _replace(old, args.get("Target"), args.get("Replacement"), args.get("AllowMultiple"))
    if name == "replace_file_content":
        return _replace(old, args.get("TargetContent"), args.get("ReplacementContent"), args.get("AllowMultiple"))
    if name == "multi_replace_file_content":
        chunks = args.get("ReplacementChunks")
        if not isinstance(chunks, list):
            return None
        text = old
        for ch in chunks:
            if not isinstance(ch, dict):
                return None
            nxt = _replace(text, ch.get("TargetContent"), ch.get("ReplacementContent"), ch.get("AllowMultiple"))
            if nxt is None:
                return None
            text = nxt
        return text
    return None


def file_tool_findings(name: str, args: Dict[str, Any], cwd: str, scope: Scope) -> List[Tuple[str, Path]]:
    path = extract_tool_path(args, cwd)
    if path is None:
        return []
    kind = scope.classify_file(path)
    if not kind:
        return []
    old = read_text(path)
    new = simulate_file_tool(name, args, old)
    if new is not None and project_text(kind, old) == project_text(kind, new):
        return []
    return [(kind, path)]


# ---- shell ------------------------------------------------------------------ #

WRITE_REDIRS = {">", ">>", ">|", "&>", "&>>", "<>"}
SEPARATORS = {";", ";;", "&&", "||", "|", "|&", "&", "(", ")", "{", "}"}
SHELL_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "fi", "done", "esac", "function"}
PREFIX_CMDS = {"sudo", "command", "builtin", "exec", "nohup", "time", "nice", "env", "stdbuf", "timeout", "doas"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
SCRIPT_LANGS = re.compile(r"^(?:python[0-9.]*|pypy[0-9.]*|node|nodejs|deno|bun|ruby|php)$")
ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
GIT_MUTATING = {
    "pull",
    "checkout",
    "switch",
    "reset",
    "merge",
    "rebase",
    "restore",
    "stash",
    "apply",
    "am",
    "cherry-pick",
    "revert",
    "clone",
    "rm",
    "mv",
    "clean",
}
AGY_PLUGIN_VERBS = {"install", "uninstall", "remove", "rm", "enable", "disable", "update", "add"}
PROMPT_HINT_RE = re.compile(
    r"(?:GEMINI\.md|AGENTS\.md|SKILL\.md|mcp_config\.json|skills\.json|plugins\.json|rules\.json|plugin\.json"
    r"|\.gemini/config|antigravity(?:-cli)?/settings\.json|(?:^|[/\s'\"])(?:\.agents|\.agent|_agents|_agent)/)"
)
SCRIPT_WRITE_RE = re.compile(
    r"open\([^)]*['\"][wax]|write_text|write_bytes|\.write\(|shutil\.|os\.(?:remove|unlink|rename|replace)|"
    r"\.unlink\(|rmtree|\.rename\(|writeFile|appendFile|rmSync|unlinkSync|renameSync|copyFile|cpSync|"
    r"File\.(?:write|delete|rename)|IO\.write|unlink\s|rename\s"
)
STRING_LIT_RE = re.compile(r"\"([^\"\n]{2,400})\"|'([^'\n]{2,400})'")
HEREDOC_RE = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")


def _split_heredocs(cmd: str) -> Tuple[str, List[str]]:
    """Remove heredoc bodies from a command; return (command, bodies in order)."""
    lines = cmd.split("\n")
    out: List[str] = []
    bodies: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for m in HEREDOC_RE.finditer(line):
            if line[max(0, m.start() - 1) : m.start()] == "<":
                continue  # here-string <<<
            strip_tabs, delim = m.group(1) == "-", m.group(3)
            body: List[str] = []
            while i < len(lines):
                cand = lines[i].lstrip("\t") if strip_tabs else lines[i]
                i += 1
                if cand.strip() == delim:
                    break
                body.append(lines[i - 1])
            bodies.append("\n".join(body))
    return "\n".join(out), bodies


def _newlines_to_semicolons(cmd: str) -> str:
    """Drop unquoted comments and turn unquoted newlines into ';' for shlex."""
    out: List[str] = []
    quote = ""
    escaped = False
    in_comment = False
    prev = "\n"
    for ch in cmd:
        if in_comment:
            if ch != "\n":
                continue
            in_comment = False
        if not quote and not escaped and ch == "#" and prev in " \t\n;&|(":
            in_comment = True
            continue
        prev = ch
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\" and quote != "'":
            escaped = True
            out.append(ch)
            continue
        if quote:
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "\n":
            out.append(" ; ")
            continue
        out.append(ch)
    return "".join(out)


def _tokenize(cmd: str) -> Optional[List[str]]:
    lex = shlex.shlex(_newlines_to_semicolons(cmd), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:
        return None


def _simple_commands(tokens: List[str]) -> List[List[str]]:
    cmds: List[List[str]] = []
    cur: List[str] = []
    for tok in tokens:
        if tok in SEPARATORS:
            if cur:
                cmds.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        cmds.append(cur)
    return cmds


def _positional(args: List[str], value_opts: Iterable[str] = ()) -> Tuple[List[str], Dict[str, str]]:
    """Split args into positional words and {option: value} for options that take one."""
    value_opts = set(value_opts)
    pos: List[str] = []
    opts: Dict[str, str] = {}
    i = 0
    end_of_opts = False
    while i < len(args):
        a = args[i]
        if not end_of_opts and a == "--":
            end_of_opts = True
        elif not end_of_opts and a.startswith("--") and "=" in a:
            k, v = a.split("=", 1)
            opts[k] = v
        elif not end_of_opts and a in value_opts and i + 1 < len(args):
            opts[a] = args[i + 1]
            i += 1
        elif not end_of_opts and a.startswith("-") and len(a) > 1:
            opts[a] = ""
        else:
            pos.append(a)
        i += 1
    return pos, opts


class ShellInspector:
    """Finds the files a shell command may write or delete, and judges them."""

    def __init__(self, scope: Scope, cwd: str):
        self.scope = scope
        self.cwd = cwd or str(scope.home)
        self.findings: List[Tuple[str, Path]] = []
        self.depth = 0
        self.vars: Dict[str, str] = {"HOME": str(scope.home)}
        self.handlers = {
            "cd": self.cmd_cd,
            "pushd": self.cmd_cd,
            "rm": self.cmd_rm,
            "unlink": self.cmd_rm,
            "rmdir": self.cmd_rm,
            "shred": self.cmd_rm,
            "trash": self.cmd_rm,
            "touch": self.cmd_touch,
            "truncate": self.cmd_truncate,
            "tee": self.cmd_tee,
            "cp": self.cmd_cp,
            "mv": self.cmd_mv,
            "ln": self.cmd_ln,
            "install": self.cmd_install,
            "rsync": self.cmd_rsync,
            "scp": self.cmd_rsync,
            "sed": self.cmd_sed,
            "gsed": self.cmd_sed,
            "perl": self.cmd_perl,
            "dd": self.cmd_dd,
            "tar": self.cmd_tar,
            "gtar": self.cmd_tar,
            "unzip": self.cmd_unzip,
            "git": self.cmd_git,
            "agy": self.cmd_agy,
            "find": self.cmd_find,
            "xargs": self.cmd_xargs,
            "eval": self.cmd_eval,
            "export": self.cmd_export,
        }

    def add(self, hit: Optional[Tuple[str, Path]]) -> None:
        if hit and hit not in self.findings:
            self.findings.append(hit)

    def expand(self, raw: str) -> str:
        return VAR_RE.sub(lambda m: self.vars.get(m.group(1) or m.group(2), m.group(0)), raw)

    def path(self, raw: str) -> Path:
        return abs_path(self.expand(raw), self.cwd)

    def check_file(self, raw: str) -> None:
        if raw and raw != "-" and not raw.startswith("/dev/"):
            self.add(self.scope.affected(self.path(raw), folder_ok=False))

    def check_any(self, raw: str) -> None:
        if raw and raw != "-" and not raw.startswith("/dev/"):
            self.add(self.scope.affected(self.path(raw)))

    def run(self, cmd: str) -> List[Tuple[str, Path]]:
        if self.depth > 3 or not cmd.strip():
            return self.findings
        self.depth += 1
        try:
            text, bodies = _split_heredocs(cmd)
            tokens = _tokenize(text)
            if tokens is None:
                self.fallback(cmd)
                return self.findings
            for words in _simple_commands(tokens):
                self.simple(words, bodies)
        finally:
            self.depth -= 1
        return self.findings

    def fallback(self, cmd: str) -> None:
        """Unparseable command: keep the old, conservative text check."""
        cleaned = re.sub(r"\d*>&\d+|&>>?\s*/dev/null|\d*>>?\s*/dev/null", " ", cmd)
        if PROMPT_HINT_RE.search(cleaned) and re.search(
            r">|\b(?:tee|cp|mv|rm|unlink|ln|rsync|truncate|install)\b|\bsed\s+-\S*i|\bperl\s+-\S*i", cleaned
        ):
            self.add(("shell", Path(self.cwd)))

    def simple(self, words: List[str], bodies: List[str]) -> None:
        argv: List[str] = []
        i = 0
        heredoc_body: Optional[str] = None
        while i < len(words):
            w = words[i]
            if w in WRITE_REDIRS or w in (">&", "<", "<<<", "<&", "<<", "<<-"):
                if argv and argv[-1].isdigit():
                    argv.pop()  # the '2' of '2>'
            if w in WRITE_REDIRS:
                if i + 1 < len(words):
                    self.check_file(words[i + 1])
                i += 2
                continue
            if w == ">&":  # >&2 duplicates a descriptor; >&file writes a file
                if i + 1 < len(words) and not words[i + 1].isdigit() and words[i + 1] != "-":
                    self.check_file(words[i + 1])
                i += 2
                continue
            if w in ("<", "<<<", "<&"):
                i += 2
                continue
            if w in ("<<", "<<-"):
                heredoc_body = bodies.pop(0) if bodies else None
                i += 2
                continue
            argv.append(w)
            i += 1
        assigns = [ASSIGN_RE.match(a) for a in argv]
        if argv and all(assigns):
            for m in assigns:
                self.vars[m.group(1)] = self.expand(m.group(2))
            return
        while argv and (ASSIGN_RE.match(argv[0]) or argv[0] in SHELL_KEYWORDS):
            argv = argv[1:]
        while argv and os.path.basename(argv[0]) in PREFIX_CMDS:
            argv = argv[1:]
            while argv and (argv[0].startswith("-") or ASSIGN_RE.match(argv[0])):
                argv = argv[1:]
            if argv and re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", argv[0]):
                argv = argv[1:]  # timeout DURATION
        if not argv:
            return
        prog = os.path.basename(argv[0])
        args = argv[1:]
        if prog in SHELLS:
            self.cmd_shell(args, heredoc_body)
        elif SCRIPT_LANGS.match(prog):
            self.cmd_script(args, heredoc_body)
        elif prog in self.handlers:
            self.handlers[prog](args)

    # -- command handlers ---------------------------------------------------- #

    def cmd_cd(self, args: List[str]) -> None:
        pos, _ = _positional(args)
        self.cwd = str(self.path(pos[0])) if pos else str(self.scope.home)

    def cmd_rm(self, args: List[str]) -> None:
        for p in _positional(args)[0]:
            self.check_any(p)

    def cmd_touch(self, args: List[str]) -> None:
        for p in _positional(args, ("-r", "-t", "-d"))[0]:
            self.check_file(p)

    def cmd_truncate(self, args: List[str]) -> None:
        for p in _positional(args, ("-s", "-r", "--size", "--reference"))[0]:
            self.check_file(p)

    def cmd_tee(self, args: List[str]) -> None:
        for p in _positional(args)[0]:
            self.check_file(p)

    def _copy_like(self, args: List[str], value_opts: Iterable[str], removes_sources: bool) -> None:
        pos, opts = _positional(args, value_opts)
        target_dir = opts.get("-t") or opts.get("--target-directory")
        if target_dir:
            dests = [str(Path(target_dir) / os.path.basename(s.rstrip("/"))) for s in pos]
            srcs = pos
        elif len(pos) >= 2:
            dest, srcs = pos[-1], pos[:-1]
            dest_path = self.path(dest)
            if dest.endswith("/") or dest_path.is_dir():
                dests = [str(dest_path / os.path.basename(s.rstrip("/"))) for s in srcs]
            else:
                dests = [dest]
        else:
            return
        for d in dests:
            self.check_any(d)
        if removes_sources:
            for s in srcs:
                self.check_any(s)

    def cmd_cp(self, args: List[str]) -> None:
        self._copy_like(args, ("-t", "-S", "--suffix"), removes_sources=False)

    def cmd_mv(self, args: List[str]) -> None:
        self._copy_like(args, ("-t", "-S", "--suffix"), removes_sources=True)

    def cmd_ln(self, args: List[str]) -> None:
        self._copy_like(args, ("-t", "-S", "--suffix"), removes_sources=False)

    def cmd_install(self, args: List[str]) -> None:
        self._copy_like(args, ("-t", "-m", "-o", "-g", "-S", "--mode", "--owner", "--group"), removes_sources=False)

    def cmd_rsync(self, args: List[str]) -> None:
        pos, _ = _positional(args, ("-e", "--rsh", "--exclude", "--include", "--filter", "-f"))
        if len(pos) < 2 or re.match(r"^[^/]*:", pos[-1]):
            return
        dest = pos[-1]
        self.check_any(dest)
        for s in pos[:-1]:
            if not s.endswith("/"):
                self.check_any(str(Path(dest) / os.path.basename(s)))

    def cmd_sed(self, args: List[str]) -> None:
        in_place = any(a.startswith("--in-place") or re.fullmatch(r"-[a-hj-zA-HJ-Z]*i\S*", a) for a in args)
        if not in_place:
            return
        pos, opts = _positional(args, ("-e", "-f", "--expression", "--file"))
        has_script_opt = any(k in opts for k in ("-e", "-f", "--expression", "--file"))
        for f in pos if has_script_opt else pos[1:]:
            self.check_file(f)

    def cmd_perl(self, args: List[str]) -> None:
        if any(re.fullmatch(r"-[a-hj-zA-HJ-Z]*i[\w.]*", a) and not a.startswith(("-M", "-I", "-m")) for a in args):
            pos, _ = _positional(args, ("-e", "-E", "-M", "-I"))
            for f in pos:
                self.check_file(f)
            return
        self.cmd_script(args, None)

    def cmd_dd(self, args: List[str]) -> None:
        for a in args:
            if a.startswith("of="):
                self.check_file(a[3:])

    def cmd_tar(self, args: List[str]) -> None:
        clusters = [a[1:] for a in args if a.startswith("-") and not a.startswith("--")]
        if args and not args[0].startswith("-"):
            clusters.append(args[0])
        if not (any("x" in c for c in clusters) or "--extract" in args or "--get" in args):
            return
        _, opts = _positional(args, ("-C", "--directory", "-f", "--file"))
        self.check_any(opts.get("-C") or opts.get("--directory") or self.cwd)

    def cmd_unzip(self, args: List[str]) -> None:
        _, opts = _positional(args, ("-d", "-x"))
        self.check_any(opts.get("-d") or self.cwd)

    def cmd_git(self, args: List[str]) -> None:
        repo = self.cwd
        i = 0
        while i < len(args) and args[i].startswith("-"):
            if args[i] == "-C" and i + 1 < len(args):
                repo = str(abs_path(args[i + 1], repo))
                i += 2
                continue
            if args[i] in ("-c", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(args):
                i += 2
                continue
            i += 1
        if i >= len(args):
            return
        sub, rest = args[i], args[i + 1 :]
        if sub not in GIT_MUTATING:
            return
        if sub == "stash" and rest and rest[0] in ("list", "show", "drop", "clear", "create", "store"):
            return
        if sub == "clone":
            pos, _ = _positional(rest, ("-b", "--branch", "--depth", "-o", "--origin", "--reference"))
            if not pos:
                return
            dest = pos[1] if len(pos) > 1 else re.sub(r"\.git$", "", pos[0].rstrip("/").split("/")[-1].split(":")[-1])
            target = abs_path(dest, repo)
        else:
            target = Path(repo)
        if self.scope.folder_affects_prompt(target, include_workspace=False):
            self.add(("folder", target))

    def cmd_agy(self, args: List[str]) -> None:
        pos, _ = _positional(args)
        if len(pos) >= 2 and pos[0] in ("plugin", "plugins", "mcp") and pos[1] in AGY_PLUGIN_VERBS:
            self.add(("agy", Path(" ".join(["agy"] + pos[:3]))))

    def cmd_find(self, args: List[str]) -> None:
        if not any(a in ("-delete", "-exec", "-execdir", "-ok") for a in args):
            return
        roots: List[str] = []
        for a in args:
            if a.startswith("-") or a in ("(", "!"):
                break
            roots.append(a)
        names = [args[j + 1] for j, a in enumerate(args[:-1]) if a in ("-name", "-iname")]
        for r in roots or ["."]:
            root = self.path(r)
            for key, (fpath, kind) in self.scope.files().items():
                if not (is_under(fpath, root) or is_under(real(fpath), root)):
                    continue
                if names and not any(fnmatch.fnmatch(fpath.name, pat) for pat in names):
                    continue
                self.add((kind, fpath))

    def cmd_xargs(self, args: List[str]) -> None:
        pos, _ = _positional(args, ("-I", "-n", "-P", "-L", "-d", "-E", "-s"))
        if pos and os.path.basename(pos[0]) in ("rm", "mv", "cp", "tee", "unlink", "trash", "sed", "truncate"):
            self.add(("shell", Path(self.cwd)))

    def cmd_shell(self, args: List[str], heredoc_body: Optional[str]) -> None:
        for j, a in enumerate(args):
            if a.startswith("-") and "c" in a[1:] and not a.startswith("--") and j + 1 < len(args):
                self.run(args[j + 1])
                return
        if heredoc_body:
            self.run(heredoc_body)

    def cmd_export(self, args: List[str]) -> None:
        for a in args:
            m = ASSIGN_RE.match(a)
            if m:
                self.vars[m.group(1)] = self.expand(m.group(2))

    def cmd_eval(self, args: List[str]) -> None:
        self.run(" ".join(args))

    def cmd_script(self, args: List[str], heredoc_body: Optional[str]) -> None:
        code = heredoc_body or ""
        for j, a in enumerate(args):
            if a in ("-c", "-e", "-E", "--eval", "-p", "--print", "-r") and j + 1 < len(args):
                code = args[j + 1]
                break
        if code and SCRIPT_WRITE_RE.search(code):
            self.script_findings(code)

    def script_findings(self, code: str) -> None:
        literal_hits = 0
        loose_hint = False  # a prompt file or folder named by a relative path or a fragment
        for m in STRING_LIT_RE.finditer(code):
            lit = m.group(1) or m.group(2) or ""
            if "/" not in lit and not PROMPT_HINT_RE.search(lit):
                continue
            # A literal is a full path (spaces allowed) or text with paths in it, such
            # as a shell command or a prompt for another agent.
            if _full_path(lit):
                parts = [lit]
            else:
                parts = [t.lstrip("([{<`").rstrip(":,;.!?)]}>`") for t in re.sub(r"\\[nrt]", " ", lit).split()]
            for part in parts:
                if _full_path(part):
                    hit = self.scope.affected(self.path(part))
                    if hit:
                        self.add(("script", hit[1]))
                        literal_hits += 1
                elif PROMPT_HINT_RE.search(part):
                    loose_hint = True
        # Full paths were all checked above, so a script that only names other files in
        # a plugin folder (its server, its tests) is left alone. Flag it when it names a
        # prompt file in a way that cannot be resolved here, such as a relative path or a
        # piece joined at run time, or names one with no full path in the script at all.
        if literal_hits == 0 and PROMPT_HINT_RE.search(code):
            if loose_hint or not re.search(r"['\"](?:~|/|\$HOME)[^'\"]*['\"]", code):
                self.add(("script", Path(self.cwd)))


def _full_path(text: str) -> bool:
    """'/Users/me/x', '~/x' and '$HOME/x' are full paths. '/plugin.json' and
    '/.gemini/config/' are pieces a script joins to another path at run time: their
    first part is not a folder at /."""
    if text.startswith(("~", "$HOME", "${HOME}")):
        return True
    if not text.startswith("/"):
        return False
    first = text.lstrip("/").split("/", 1)[0]
    return bool(first) and os.path.isdir("/" + first)


def shell_findings(cmd: str, cwd: str, scope: Scope) -> List[Tuple[str, Path]]:
    return ShellInspector(scope, cwd).run(cmd)


# --------------------------------------------------------------------------- #
# Messages (plain words; shown to the person, not the model)
# --------------------------------------------------------------------------- #


def describe(kind: str, path: Path) -> str:
    label = KIND_LABELS.get(kind, kind)
    if kind == "agy":
        return f"{label} (`{path}`)"
    if kind in ("subagent",):
        return f"{label} ({path})"
    return f"{label}: {shorten_path(path)}"


def pretool_message(findings: List[Tuple[str, Path]], live: int) -> str:
    shown = "\n".join(f"- {describe(k, p)}" for k, p in findings[:5])
    more = f"\n- and {len(findings) - 5} more" if len(findings) > 5 else ""
    return (
        "Cache Guard: this step changes what goes into the prompt.\n"
        f"{shown}{more}\n"
        f"Your next message will then re-read {fmt_tokens(live)} at full price.\n"
        "Approve to do it now. Reject to keep the cache and make this change in a new chat."
    )


def stop_message(reasons: List[str], hints: List[str], live: int, window: int) -> str:
    lines = [
        "Cache Guard paused this message before it was sent.",
        f"Sending it now re-reads {fmt_tokens(live)} at full price, because:",
    ]
    lines += [f"- {r}" for r in reasons]
    lines.append("What you can do:")
    lines.append(f"- Send the same message again within {max(1, round(window / 60))} min to go ahead.")
    lines += [f"- {h}" for h in hints]
    lines.append("- Or start a new chat, which only pays for a short prompt.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Event handlers
# --------------------------------------------------------------------------- #


def _workspace(payload: Dict[str, Any]) -> Tuple[List[Any], str]:
    ws = payload.get("workspacePaths") if isinstance(payload.get("workspacePaths"), list) else []
    return ws, str(payload.get("cwd") or "")


def handle_session_start(payload: Dict[str, Any], data_dir: Path, conv_id: str) -> None:
    ws, cwd = _workspace(payload)
    state = load_state(data_dir, conv_id)
    state["baseline"] = Scope(ws, cwd).projection()
    state.pop("fingerprint", None)
    save_state(data_dir, conv_id, state)
    prune_state(data_dir)


def handle_pre_tool_use(payload: Dict[str, Any], cfg: Dict[str, Any], data_dir: Path, conv_id: str) -> Optional[Dict[str, Any]]:
    tc = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
    name = str(tc.get("name") or "")
    args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
    if name not in GUARDED_TOOLS:
        return None
    if name == "define_subagent" and not cfg.get("check_define_subagent", True):
        return None
    if name != "define_subagent" and not cfg.get("check_customization_edits", True):
        return None
    live = read_last_call(str(payload.get("transcriptPath") or ""))["live"]
    if live < cfg["min_tokens"]:
        return None

    ws, cwd = _workspace(payload)
    findings: List[Tuple[str, Path]] = []
    if name == "define_subagent":
        findings = [("subagent", Path(str(args.get("Name") or "subagent")))]
    else:
        scope = Scope(ws, cwd)
        if name in FILE_WRITE_TOOLS:
            findings = file_tool_findings(name, args, cwd, scope)
        else:
            findings = shell_findings(extract_tool_command(args), cwd, scope)
    if not findings:
        return None

    call_id = str(tc.get("id") or "")
    if call_id:
        state = load_state(data_dir, conv_id)
        warned = [c for c in state.get("warned_calls") or [] if isinstance(c, str)][-20:]
        warned.append(call_id)
        state["warned_calls"] = warned
        save_state(data_dir, conv_id, state)
    return {"decision": cfg["pretool_decision"], "reason": pretool_message(findings, live)}


def handle_post_tool_use(payload: Dict[str, Any], data_dir: Path, conv_id: str) -> None:
    """After an approved prompt change, accept the new content as the next baseline."""
    if payload.get("error"):
        return
    tc = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
    call_id = str(tc.get("id") or "")
    state = load_state(data_dir, conv_id)
    warned = state.get("warned_calls") or []
    if not call_id or call_id not in warned:
        return
    ws, cwd = _workspace(payload)
    state["ack"] = projection_hash(Scope(ws, cwd).projection())
    state["warned_calls"] = [c for c in warned if c != call_id]
    save_state(data_dir, conv_id, state)


def _model_credit(state: Dict[str, Any], transcript: str, model: str) -> Optional[Dict[str, Any]]:
    rec = (state.get("models") or {}).get(norm_model(model))
    if isinstance(rec, dict) and rec.get("ts"):
        return rec
    return find_model_last_use(transcript, model)


def handle_pre_invocation(payload: Dict[str, Any], cfg: Dict[str, Any], data_dir: Path, conv_id: str) -> Optional[Dict[str, Any]]:
    now = time.time()
    ws, cwd = _workspace(payload)
    transcript = str(payload.get("transcriptPath") or "")
    inv = payload.get("invocationNum")
    turn_start = not isinstance(inv, int) or isinstance(inv, bool) or inv == 0
    state = load_state(data_dir, conv_id)
    last = read_last_call(transcript)
    live = last["live"]

    # Remember when each model was last used: a model's cache stays warm for its TTL.
    if last["model"] and last["ts"]:
        models = state.get("models") if isinstance(state.get("models"), dict) else {}
        models[norm_model(last["model"])] = {"ts": last["ts"], "live": live}
        state["models"] = dict(sorted(models.items(), key=lambda kv: kv[1].get("ts") or 0)[-12:])

    proj: Optional[Dict[str, str]] = None
    if turn_start and cfg.get("check_customization_edits", True):
        proj = Scope(ws, cwd).projection()

    def accept() -> None:
        if proj is not None:
            state["baseline"] = proj
            state.pop("ack", None)
        state.pop("pending", None)
        save_state(data_dir, conv_id, state)

    if live < cfg["min_tokens"]:
        accept()
        return None

    reasons: List[str] = []
    hints: List[str] = []
    sig: List[Any] = [last["step"]]
    cur_model = str(payload.get("modelName") or "").strip()
    cur_n, prev_n = norm_model(cur_model), norm_model(last["model"])

    # 1. Model switch (any invocation). The Orchestrator's own switches go through: its
    # Planner hands each plan to the Executor (exit_plan_mode) and takes the chat back
    # at the next message. Both re-read the chat (all 11 hand-offs in the audit missed),
    # but a stop cannot prevent that and would hit every plan; only giving both roles
    # the same model and effort can, which agy 1.5.17 runs as a single agent.
    switched = real_model(cur_n) and real_model(prev_n) and cur_n != prev_n
    if cfg.get("check_model_switch", True) and switched and "exit_plan_mode" not in last["tools"]:
        pairs = handoff_pairs(transcript)
        at_risk = 0 if (cur_n, prev_n) in pairs else live  # 0: the Executor hands back
        credit = _model_credit(state, transcript, cur_model) if at_risk else None
        if credit and credit.get("ts") and now - float(credit["ts"]) < ttl_for(cfg, cur_n):
            at_risk = max(0, live - int(credit.get("live") or 0))
        if at_risk >= cfg["min_tokens"]:
            reasons.append(f"The model changed from {last['model']} to {cur_model}. Each model keeps its own cache.")
            if any(executor == prev_n for _planner, executor in pairs):
                hints.append("Or switch back to the Orchestrator with /model.")
            else:
                hints.append(f"Or switch back to {last['model']} with /model.")
            sig.append(("model", prev_n, cur_n))

    # 2. Prompt content changed since the last turn start.
    baseline = state.get("baseline") if isinstance(state.get("baseline"), dict) else None
    if proj is not None and baseline is not None:
        changes = diff_projection(baseline, proj)
        if changes and state.get("ack") != projection_hash(proj):
            shown = ", ".join(f"{shorten_path(p)} ({KIND_LABELS.get(k, k)}, {c})" for p, k, c in changes[:4])
            more = f" and {len(changes) - 4} more" if len(changes) > 4 else ""
            reasons.append(f"Prompt files changed: {shown}{more}.")
            hints.append("Or undo that change, then send again.")
            sig.append(("drift", projection_hash(proj)))
    elif proj is not None and baseline is None:
        state["baseline"] = proj

    # 3. The cache expired while the chat sat idle (turn start only).
    if turn_start and cfg.get("check_idle_ttl", True) and last["ts"]:
        idle = now - float(last["ts"])
        ttl = ttl_for(cfg, cur_n or prev_n)
        if idle >= ttl:
            fam = model_family(cur_n or prev_n)
            who = {"claude": "Claude", "gemini": "Gemini", "gpt": "GPT"}.get(fam, "This model")
            reasons.append(f"This chat sat idle for {fmt_gap(idle)}. {who} keeps its cache for about {fmt_gap(ttl)}.")
            sig.append(("idle", last["step"]))

    if not reasons:
        accept()
        return None

    sig_hash = digest(json.dumps(sig, sort_keys=True, default=str))
    window = cfg["retry_confirm_window_seconds"]
    pending = state.get("pending") if isinstance(state.get("pending"), dict) else None
    if pending and pending.get("sig") == sig_hash and now - float(pending.get("ts") or 0) <= window:
        accept()
        return None

    state["pending"] = {"sig": sig_hash, "ts": now}
    save_state(data_dir, conv_id, state)
    return {
        "injectSteps": [{"errorMessage": {"userMessage": stop_message(reasons, hints, live, window)}}],
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
        handle_session_start(payload, data_dir, conv_id)
        return None
    if event == "PreToolUse":
        return handle_pre_tool_use(payload, cfg, data_dir, conv_id)
    if event == "PostToolUse":
        handle_post_tool_use(payload, data_dir, conv_id)
        return None
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
    except Exception as exc:  # fail open: never block the agent on a guard bug
        sys.stderr.write(f"[cache-guard] error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
