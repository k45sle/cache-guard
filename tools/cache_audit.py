#!/usr/bin/env python3
"""
cache_audit: measure real prompt-cache misses in Antigravity transcripts and
backtest cache-guard's rules against them.

Ground truth comes from the provider's own token accounting that Antigravity
writes on every model call (PLANNER_RESPONSE.input_tokens = uncached input,
PLANNER_RESPONSE.cache_read_tokens = input served from cache). For two
consecutive model calls in one conversation, everything the earlier call sent
is a cacheable prefix of the later call, so

    lost = max(0, prev.input + prev.cache_read - cur.cache_read)

is how many already-seen tokens were billed again at the full rate.

Usage:
    python3 tools/cache_audit.py                  # scan every local transcript
    python3 tools/cache_audit.py --min-tokens 60000 --list-misses
    python3 tools/cache_audit.py --json > audit.json
    python3 tools/cache_audit.py path/to/transcript_full.jsonl ...

Stdlib only; Python 3.9+.
"""

from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "hooks"))
import guard  # noqa: E402  (shared path / command classifiers)

EFFORT_SUFFIX_RE = re.compile(r"-(?:minimal|low|medium|high|xhigh|max)$", re.IGNORECASE)
GAP_BUCKETS = [(0, 60), (60, 180), (180, 300), (300, 600), (600, 1800), (1800, 3600), (3600, 10**9)]
MIN_PREFIX = 4096  # ignore tiny prefixes: providers do not cache below a few thousand tokens
LAB_LIST = HERE / "lab-chats.txt"
V01_REV = "f2940cf"  # last v0.1 commit; its guard is replayed for the before/after backtest
# A partial SKILL.md edit whose snippets hold none of these lines changes only the body.
SKILL_FRONTMATTER_LINE = re.compile(r"^(?:---\s*$|name\s*:|description\s*:)", re.MULTILINE)
CACHE_GUARD_RE = re.compile(r"cache[- ]?guard", re.IGNORECASE)  # v0.1 "CACHE-GUARD", v0.2 "Cache Guard"


def parse_ts(val: Any) -> Optional[float]:
    if not isinstance(val, str) or not val:
        return None
    s = val.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def base_model(model: str) -> str:
    return EFFORT_SUFFIX_RE.sub("", model.lower())


def model_family(model: str) -> str:
    m = model.lower()
    for fam in ("claude", "gemini", "gpt"):
        if fam in m:
            return fam
    return "other"


@dataclass
class Call:
    conv: str
    step: int
    ts: Optional[float]
    model: str
    inp: int
    cr: int
    kind: str  # planner | checkpoint
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    turn_start: bool = False  # first model call after a user message (invocationNum 0)
    pos: int = 0  # line number in the transcript (file order is time order)
    segment: int = 0  # bumps when step numbers restart (chat reset): no pair across it

    @property
    def live(self) -> int:
        return self.inp + self.cr


@dataclass
class Edit:
    conv: str
    ts: float
    category: str
    target: str
    prompt: bool = True  # v0.2: the change reaches the prompt
    v01: bool = False  # v0.1 would have flagged it
    live: int = 0  # size of the chat that made it, when it made it (PreToolUse threshold)


@dataclass
class Pair:
    conv: str
    prev: Call
    cur: Call
    gap: Optional[float]
    between: List[Dict[str, Any]]
    prefix: int = 0
    lost: int = 0
    hit: float = 1.0
    causes: List[str] = field(default_factory=list)
    own_edits: List[Edit] = field(default_factory=list)  # made by prev's tool calls
    cross_edits: List[Edit] = field(default_factory=list)  # other chats, between prev and cur
    # Edits by anyone since this chat's previous turn start. The prompt is rebuilt
    # only at a turn start, so these are what can make a turn-start call miss.
    window_edits: List[Edit] = field(default_factory=list)
    cur_model_last: Optional[Call] = None  # last earlier call on cur's model (warm switch-back)
    # The chat's next turn-start call (cur or later) missed with no model switch, idle
    # gap or compaction to explain it, so an edit made before it is the likely cause.
    next_turn_miss: bool = False
    # An Orchestrator switch the guard lets through: the Planner's exit_plan_mode hands
    # the plan to the Executor, or the Executor hands the chat back to that Planner.
    orchestrator: bool = False

    @property
    def outcome(self) -> str:
        if self.prefix < MIN_PREFIX:
            return "small"
        if self.hit < 0.1:
            return "total"
        if self.hit < 0.5:
            return "major"
        if self.hit < 0.9:
            return "partial"
        return "hit"

    @property
    def is_miss(self) -> bool:
        return self.outcome in ("total", "major")


def default_transcripts() -> List[Path]:
    home = Path(os.path.expanduser("~"))
    out: List[Path] = []
    for logs in sorted(glob.glob(str(home / ".gemini" / "antigravity*" / "brain" / "*" / ".system_generated" / "logs"))):
        full = Path(logs) / "transcript_full.jsonl"
        lite = Path(logs) / "transcript.jsonl"
        if full.is_file():
            out.append(full)
        elif lite.is_file():
            out.append(lite)
    return out


def conv_id_for(path: Path) -> str:
    parts = path.parts
    if "brain" in parts:
        idx = parts.index("brain")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return path.stem


def load_steps(path: Path) -> List[Dict[str, Any]]:
    steps: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                steps.append(obj)
    return steps


def edit_category(target: str) -> str:
    name = os.path.basename(target)
    low = target.replace("\\", "/")
    if name in ("GEMINI.md", "AGENTS.md") or "/rules/" in low:
        return "rule"
    if name == "SKILL.md" or "/skills/" in low:
        return "skill"
    if name == "mcp_config.json":
        return "mcp"
    if name == "hooks.json":
        return "hooks"
    if name in ("plugin.json", "plugins.json"):
        return "plugin-manifest"
    if name == "settings.json":
        return "settings"
    if name == "config.json":
        return "config"
    return "other"


def load_lab_ids(path: Path = LAB_LIST) -> Set[str]:
    ids: Set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ids
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if line:
            ids.add(line)
    return ids


def load_v01(rev: str = V01_REV) -> Any:
    """The v0.1 guard module from git history, or None (no git, shallow clone...)."""
    try:
        src = subprocess.run(
            ["git", "-C", str(HERE.parent), "show", f"{rev}:hooks/guard.py"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "guard_v01.py"
            path.write_text(src, encoding="utf-8")
            spec = importlib.util.spec_from_file_location("guard_v01", path)
            if spec is None or spec.loader is None:
                return None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
    except Exception:  # an unreadable old revision only disables the comparison
        return None
    needed = ("FILE_MUTATION_TOOLS", "is_customization_path", "inspect_shell_command")
    return mod if all(hasattr(mod, n) for n in needed) else None


def _partial_edit_snippets(name: str, args: Dict[str, Any]) -> Optional[List[Any]]:
    """Old and new text of a partial file edit; None for a whole-file write."""
    if name == "edit_file":
        return [args.get("Target"), args.get("Replacement")]
    if name == "replace_file_content":
        return [args.get("TargetContent"), args.get("ReplacementContent")]
    if name == "multi_replace_file_content":
        out: List[Any] = []
        for ch in args.get("ReplacementChunks") or []:
            if isinstance(ch, dict):
                out += [ch.get("TargetContent"), ch.get("ReplacementContent")]
        return out
    return None


def skill_edit_hits_frontmatter(name: str, args: Dict[str, Any]) -> bool:
    """Transcripts keep no file snapshots, so the live guard's before/after compare is
    not possible here. A whole-file write counts as a frontmatter change; a partial edit
    counts only when its snippets contain a '---', name: or description: line."""
    snippets = _partial_edit_snippets(name, args)
    if snippets is None:
        return True
    return any(isinstance(t, str) and SKILL_FRONTMATTER_LINE.search(t) for t in snippets)


def tool_call_edits(call: Call, scope: "guard.Scope", cwd: str, v01: Any = None) -> List[Edit]:
    """Customization edits made by the tool calls of one model response.

    prompt: v0.2's classifiers say the edit reaches the prompt. v01: v0.1's
    classifiers would have flagged it. Edits that are neither are dropped, except
    skill-body edits, which are kept as evidence that they cost nothing.
    """
    edits: List[Edit] = []
    if call.ts is None:
        return edits
    v01_file_tools = set(getattr(v01, "FILE_MUTATION_TOOLS", ()) or ())
    for tc in call.tool_calls:
        name = str(tc.get("name") or "")
        args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
        if name in guard.FILE_WRITE_TOOLS or name in v01_file_tools:
            path = guard.extract_tool_path(args, cwd)
            if path is None:
                continue
            kind = scope.classify_file(path) if name in guard.FILE_WRITE_TOOLS else None
            body_only = kind == "skill" and not skill_edit_hits_frontmatter(name, args)
            prompt = bool(kind) and not body_only
            flagged = False
            if v01 is not None and name in v01_file_tools:
                try:
                    flagged = bool(v01.is_customization_path(str(path))[0])
                except Exception:
                    flagged = False
            if prompt:
                category = str(kind)
            elif body_only:
                category = "not-prompt:skill-body"
            else:
                category = "not-prompt:" + edit_category(str(path))
            if prompt or flagged or body_only:
                edits.append(Edit(call.conv, call.ts, category, guard.shorten_path(path), prompt, flagged))
        elif name in guard.SHELL_TOOLS:
            cmd = guard.extract_tool_command(args)
            if not cmd:
                continue
            findings = guard.shell_findings(cmd, cwd, scope)
            flagged = False
            if v01 is not None:
                try:
                    flagged = bool(v01.inspect_shell_command(cmd)[0])
                except Exception:
                    flagged = False
            seen: Set[Tuple[str, str]] = set()
            for kind, path in findings:
                key = (kind, str(path))
                if key not in seen:
                    seen.add(key)
                    edits.append(Edit(call.conv, call.ts, kind, guard.shorten_path(path), True, flagged))
            if not findings and flagged:
                edits.append(Edit(call.conv, call.ts, "not-prompt:shell", cmd[:80], False, True))
    return edits


def _step_index(s: Dict[str, Any]) -> int:
    try:
        return int(s.get("step_index") or 0)
    except (TypeError, ValueError):
        return 0


def build_pairs(
    paths: Iterable[Path], exclude: Iterable[str] = (), v01: Any = None
) -> Tuple[List[Pair], List[Edit], Dict[str, int]]:
    skip = set(exclude)
    convs: Dict[str, Tuple[List[Call], List[Dict[str, Any]]]] = {}
    stats = {"transcripts": 0, "calls": 0, "excluded": 0, "resets": 0}
    for path in paths:
        conv = conv_id_for(path)
        if conv in skip:
            stats["excluded"] += 1
            continue
        try:
            # Keep file order: it is time order, and step numbers start again at 0 when
            # a chat is reset, so sorting by step would interleave unrelated calls.
            steps = load_steps(path)
        except OSError:
            continue
        stats["transcripts"] += 1
        calls: List[Call] = []
        user_since_call = True  # a chat's first call starts a turn
        segment, last_idx = 0, -1
        for pos, s in enumerate(steps):
            idx = _step_index(s)
            if idx < last_idx:
                segment += 1
                user_since_call = True
            last_idx = idx
            typ = s.get("type")
            if typ == "USER_INPUT":
                user_since_call = True
                continue
            if typ not in ("PLANNER_RESPONSE", "CHECKPOINT"):
                continue
            inp = int(s.get("input_tokens") or 0)
            cr = int(s.get("cache_read_tokens") or 0)
            if inp + cr <= 0:
                continue
            planner = typ == "PLANNER_RESPONSE"
            calls.append(
                Call(
                    conv=conv,
                    step=_step_index(s),
                    ts=parse_ts(s.get("created_at")),
                    model=str(s.get("model") or ("<checkpoint>" if not planner else "")),
                    inp=inp,
                    cr=cr,
                    kind="planner" if planner else "checkpoint",
                    tool_calls=[tc for tc in (s.get("tool_calls") or []) if isinstance(tc, dict)],
                    turn_start=planner and user_since_call,
                    pos=pos,
                    segment=segment,
                )
            )
            if planner:
                user_since_call = False
        stats["calls"] += len(calls)
        convs[conv] = (calls, steps)

    # Every customization edit any agent made, with its time: the natural experiment.
    # Transcripts do not record workspaces; agy runs with cwd = home, so home is the
    # workspace here and rule files inside other project folders are not attributed.
    home = str(guard.home_dir())
    scope = guard.Scope([home], home)
    own: Dict[Tuple[str, int], List[Edit]] = {}
    all_edits: List[Edit] = []
    for conv, (calls, _steps) in convs.items():
        for c in calls:
            found = tool_call_edits(c, scope, home, v01)
            for e in found:
                e.live = c.live
            own[(conv, c.pos)] = found
            all_edits.extend(found)
    all_edits.sort(key=lambda e: e.ts)

    pairs: List[Pair] = []
    for conv, (calls, steps) in convs.items():
        last_turn_start: Optional[Call] = None
        last_by_model: Dict[str, Call] = {}
        handoffs: Set[Tuple[str, str]] = set()  # (planner, executor), as guard.handoff_pairs
        for i, cur in enumerate(calls):
            if i > 0 and calls[i - 1].segment != cur.segment:
                stats["resets"] += 1
                last_turn_start, last_by_model = None, {}
            elif i > 0:
                prev = calls[i - 1]
                between = steps[prev.pos + 1 : cur.pos]
                gap = (cur.ts - prev.ts) if (cur.ts is not None and prev.ts is not None) else None
                p = Pair(conv=conv, prev=prev, cur=cur, gap=gap, between=between)
                p.prefix = prev.live
                p.lost = max(0, prev.live - cur.cr)
                p.hit = (min(cur.cr, prev.live) / prev.live) if prev.live else 1.0
                p.own_edits = own.get((conv, prev.pos), [])
                if prev.ts is not None and cur.ts is not None:
                    p.cross_edits = [e for e in all_edits if e.conv != conv and prev.ts < e.ts <= cur.ts]
                if cur.turn_start and last_turn_start is not None and last_turn_start.ts is not None and cur.ts is not None:
                    lo, hi = last_turn_start.ts, cur.ts
                    p.window_edits = [
                        e for e in all_edits if lo <= e.ts <= hi and not (e.conv == conv and e.ts >= hi)
                    ]
                p.cur_model_last = last_by_model.get(guard.norm_model(cur.model))
                pm, cm = guard.norm_model(prev.model), guard.norm_model(cur.model)
                if prev.kind == "planner" and cur.kind == "planner" and pm != cm:
                    if any(tc.get("name") == "exit_plan_mode" for tc in prev.tool_calls):
                        p.orchestrator = guard.real_model(pm) and guard.real_model(cm)
                        if p.orchestrator:
                            handoffs.add((pm, cm))
                    else:
                        p.orchestrator = (cm, pm) in handoffs
                p.causes = classify(p)
                pairs.append(p)
            if cur.kind == "planner":
                last_by_model[guard.norm_model(cur.model)] = cur
                if cur.turn_start:
                    last_turn_start = cur
    # An edit made mid-turn reaches the prompt at the next turn start: remember, for
    # every pair, whether that turn-start call missed (walk each chat backwards).
    nxt: Dict[Tuple[str, int], bool] = {}
    for p in reversed(pairs):
        key = (p.conv, p.cur.segment)
        if p.cur.turn_start:
            nxt[key] = p.is_miss and not (set(p.causes) & OTHER_CAUSES)
        p.next_turn_miss = nxt.get(key, False)
    return pairs, all_edits, stats


def classify(p: Pair) -> List[str]:
    causes: List[str] = []
    pm, cm = p.prev.model.lower(), p.cur.model.lower()
    texts = " ".join(str(s.get("content") or s.get("error") or "") for s in p.between)
    if p.cur.kind == "checkpoint" or p.prev.kind == "checkpoint" or any(s.get("type") == "CHECKPOINT" for s in p.between):
        causes.append("compaction")
    if pm and cm and pm != cm and p.cur.kind == "planner" and p.prev.kind == "planner":
        handoff = "Plan mode is off" in texts or any(tc.get("name") == "exit_plan_mode" for tc in p.prev.tool_calls)
        if handoff:
            causes.append("plan_handoff_model_switch")
        elif p.orchestrator:
            causes.append("orchestrator_return")
        elif base_model(pm) == base_model(cm):
            causes.append("effort_switch")
        else:
            causes.append("model_switch")
    prompt_edits = [e for e in p.window_edits if e.prompt]
    if any(e.conv == p.conv for e in prompt_edits):
        causes.append("own_prompt_edit")
    if any(e.conv != p.conv for e in prompt_edits):
        causes.append("other_session_prompt_edit")
    if p.window_edits and not prompt_edits:
        causes.append("not_prompt_edit_only")
    if any(
        s.get("type") == "ERROR_MESSAGE" and CACHE_GUARD_RE.search(str(s.get("error") or s.get("content") or ""))
        for s in p.between
    ):
        causes.append("after_cache_guard_stop")
    if p.gap is not None and p.gap >= 300:
        causes.append("idle_5m_plus")
    return causes


V02_CFG: Dict[str, Any] = {"idle_ttl_seconds": dict(guard.DEFAULT_IDLE_TTL)}


@dataclass
class Verdict:
    fired: Set[str]  # checks that interrupt right before cur (or before prev's tool calls)
    warned_earlier: bool = False  # an earlier ask in this chat already covered this miss


def v02_rules(p: Pair, min_tokens: int, cfg: Optional[Dict[str, Any]] = None) -> Verdict:
    """What v0.2 would have done: ask before a prompt-changing tool call, stop before
    cur on a costly model switch, or at a turn start on prompt drift or an idle cache."""
    cfg = cfg or V02_CFG
    fired: Set[str] = set()
    pm, cm = guard.norm_model(p.prev.model), guard.norm_model(p.cur.model)
    planners = p.prev.kind == "planner" and p.cur.kind == "planner"
    if planners and guard.real_model(pm) and guard.real_model(cm) and pm != cm and not p.orchestrator:
        at_risk = p.prev.live
        warm = p.cur_model_last
        if warm is not None and warm.ts is not None and p.cur.ts is not None:
            if p.cur.ts - warm.ts < guard.ttl_for(cfg, cm):
                at_risk = max(0, p.prev.live - warm.live)
        if at_risk >= min_tokens:
            fired.add("model_switch")
    if any(e.prompt for e in p.own_edits) and p.prev.live >= min_tokens:
        fired.add("edit_ask")  # PreToolUse, before prev's own tool call
    warned = False
    if p.cur.turn_start:
        asked = [e for e in p.window_edits if e.prompt and e.conv == p.conv and e.live >= min_tokens]
        unasked = [e for e in p.window_edits if e.prompt and not (e.conv == p.conv and e.live >= min_tokens)]
        if unasked:
            fired.add("drift")  # another chat (or a below-threshold edit) changed the prompt
        warned = bool(asked)  # approved at the ask, so no second stop here
        if p.gap is not None and p.gap >= guard.ttl_for(cfg, cm or pm):
            fired.add("idle")
    return Verdict(fired, warned)


def v01_rules(p: Pair, idle_ttl: int, have_v01: bool) -> Verdict:
    """v0.1 checked every model call: any model change, idle >= one TTL, and asked
    before (or stopped after) any edit its path list flagged."""
    fired: Set[str] = set()
    pm, cm = p.prev.model.lower(), p.cur.model.lower()
    if pm and cm and pm != cm and p.prev.kind == "planner" and p.cur.kind == "planner":
        fired.add("model_switch")
    if p.gap is not None and p.gap >= idle_ttl:
        fired.add("idle")
    if have_v01 and any(e.v01 for e in p.own_edits + p.cross_edits):
        fired.add("customization_edit")
    return Verdict(fired)


EDIT_RULES = {"edit_ask", "customization_edit"}
OTHER_CAUSES = {
    "model_switch",
    "effort_switch",
    "plan_handoff_model_switch",
    "orchestrator_return",
    "compaction",
    "idle_5m_plus",
}


def _useful(p: Pair, rule: str) -> bool:
    """A stop was worth it when a miss followed: at cur, or for an edit, at the next turn start."""
    return p.is_miss or (rule in EDIT_RULES and p.next_turn_miss)


def _backtest(elig: List[Pair], verdicts: Dict[int, Verdict], names: Iterable[str]) -> Dict[str, Any]:
    """Per rule and overall: stops, stops followed by a real miss, and misses missed."""
    misses = [p for p in elig if p.is_miss]
    out: Dict[str, Any] = {"eligible_pairs": len(elig), "misses": len(misses)}
    rules: Dict[str, Any] = {}
    for name in names:
        fired = [p for p in elig if name in verdicts[id(p)].fired]
        tp = [p for p in fired if _useful(p, name)]
        rules[name] = {
            "fired": len(fired),
            "true_positive": len(tp),
            "false_positive": len(fired) - len(tp),
            "precision": round(len(tp) / len(fired), 3) if fired else None,
        }
    out["rules"] = rules
    stops = [p for p in elig if verdicts[id(p)].fired]
    useful = [p for p in stops if any(_useful(p, r) for r in verdicts[id(p)].fired)]

    def covered(p: Pair) -> bool:
        v = verdicts[id(p)]
        return bool(v.fired) or v.warned_earlier

    caught = [p for p in misses if covered(p)]
    out["stops"] = len(stops)
    out["false_stops"] = len(stops) - len(useful)
    out["precision"] = round(len(useful) / len(stops), 3) if stops else None
    out["recall"] = round(len(caught) / len(misses), 3) if misses else None
    out["tokens_in_caught_misses"] = sum(p.lost for p in caught)
    out["tokens_in_uncovered_misses"] = sum(p.lost for p in misses if not covered(p))
    # The Orchestrator's own switches cost a re-read whatever the user does, so judge
    # a guard on the misses a stop could have prevented.
    rest = [p for p in misses if not p.orchestrator]
    out["orchestrator_misses"] = len(misses) - len(rest)
    out["tokens_in_orchestrator_misses"] = sum(p.lost for p in misses if p.orchestrator)
    out["misses_without_orchestrator"] = len(rest)
    out["caught_without_orchestrator"] = sum(1 for p in rest if covered(p))
    out["tokens_without_orchestrator"] = sum(p.lost for p in rest)
    out["tokens_caught_without_orchestrator"] = sum(p.lost for p in rest if covered(p))
    return out


def bucket_label(lo: int, hi: int) -> str:
    def f(x: int) -> str:
        return f"{x // 60}m" if x >= 60 else f"{x}s"

    return f"{f(lo)}+" if hi >= 10**9 else f"{f(lo)}-{f(hi)}"


def summarize(
    pairs: List[Pair],
    all_edits: List[Edit],
    stats: Dict[str, int],
    min_tokens: int,
    idle_ttl: int,
    have_v01: bool = False,
) -> Dict[str, Any]:
    big = [p for p in pairs if p.prefix >= MIN_PREFIX]
    total_input = sum(p.cur.live for p in pairs)
    total_cached = sum(p.cur.cr for p in pairs)
    misses = [p for p in big if p.is_miss]
    out: Dict[str, Any] = {
        "transcripts": stats["transcripts"],
        "lab_transcripts_excluded": stats.get("excluded", 0),
        "model_calls": stats["calls"],
        "call_pairs": len(pairs),
        "input_tokens_total": total_input,
        "cache_read_share": round(total_cached / total_input, 4) if total_input else 0.0,
        "miss_events": len(misses),
        "tokens_rebilled_by_misses": sum(p.lost for p in misses),
    }

    # Misses by primary cause (first cause wins; unexplained if none).
    by_cause: Dict[str, Dict[str, int]] = {}
    for p in misses:
        key = p.causes[0] if p.causes else "unexplained"
        d = by_cause.setdefault(key, {"events": 0, "tokens_rebilled": 0})
        d["events"] += 1
        d["tokens_rebilled"] += p.lost
    out["misses_by_primary_cause"] = dict(sorted(by_cause.items(), key=lambda kv: -kv[1]["tokens_rebilled"]))

    # How often does each signal actually precede a miss? (precision of the signal)
    signal_rows: Dict[str, Dict[str, Any]] = {}
    for sig in (
        "model_switch",
        "effort_switch",
        "plan_handoff_model_switch",
        "orchestrator_return",
        "compaction",
        "own_prompt_edit",
        "other_session_prompt_edit",
        "not_prompt_edit_only",
        "after_cache_guard_stop",
        "idle_5m_plus",
    ):
        rows = [p for p in big if sig in p.causes]
        if not rows:
            continue
        signal_rows[sig] = {
            "pairs": len(rows),
            "followed_by_miss": sum(1 for p in rows if p.is_miss),
            "mean_hit": round(statistics.mean(p.hit for p in rows), 3),
        }
    out["signals"] = signal_rows

    # Idle curve: same model, nothing else going on.
    curve: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for p in big:
        if p.gap is None or [c for c in p.causes if c != "idle_5m_plus"]:
            continue
        fam = model_family(p.cur.model)
        for lo, hi in GAP_BUCKETS:
            if lo <= p.gap < hi:
                row = curve.setdefault(fam, {}).setdefault(bucket_label(lo, hi), {"pairs": 0, "misses": 0, "_hits": []})
                row["pairs"] += 1
                row["misses"] += int(p.is_miss)
                row["_hits"].append(p.hit)
                break
    for fam_rows in curve.values():
        for row in fam_rows.values():
            hits = row.pop("_hits")
            row["mean_hit"] = round(statistics.mean(hits), 3) if hits else None
    out["idle_curve"] = curve

    # Edits since the previous turn start, by category: did the turn-start call miss?
    # Only turn-start calls count: the prompt is rebuilt there and nowhere else.
    cat_rows: Dict[str, Dict[str, int]] = {}
    for p in big:
        if not p.cur.turn_start or p.cur.kind != "planner":
            continue
        for key in sorted({f"{'own' if e.conv == p.conv else 'other'}:{e.category}" for e in p.window_edits}):
            row = cat_rows.setdefault(key, {"pairs": 0, "followed_by_miss": 0})
            row["pairs"] += 1
            row["followed_by_miss"] += int(p.is_miss)
    out["customization_edits_by_category"] = dict(sorted(cat_rows.items()))
    out["customization_edits_seen"] = len(all_edits)
    out["prompt_edits_seen"] = sum(1 for e in all_edits if e.prompt)

    # Backtest both guard versions at the same threshold.
    elig = [p for p in big if p.prefix >= min_tokens]
    v02 = {id(p): v02_rules(p, min_tokens) for p in elig}
    out["guard_backtest_v02"] = _backtest(elig, v02, ("model_switch", "edit_ask", "drift", "idle"))
    out["guard_backtest_v02"]["idle_ttl_seconds"] = dict(guard.DEFAULT_IDLE_TTL)
    v01 = {id(p): v01_rules(p, idle_ttl, have_v01) for p in elig}
    v01_names = ("model_switch", "idle", "customization_edit") if have_v01 else ("model_switch", "idle")
    out["guard_backtest_v01"] = _backtest(elig, v01, v01_names)
    out["guard_backtest_v01"]["idle_ttl_seconds"] = idle_ttl
    out["guard_backtest_v01"]["edit_rule_replayed"] = have_v01
    return out


def fmt_k(n: int) -> str:
    return f"{n / 1000:,.1f}k" if n >= 1000 else str(n)


def _render_backtest(title: str, bt: Dict[str, Any]) -> List[str]:
    lines = [title]
    for name, d in bt["rules"].items():
        prec = "n/a" if d["precision"] is None else f"{d['precision']:.0%}"
        lines.append(
            f"  {name:20s} fired {d['fired']:4d}  TP {d['true_positive']:3d}  FP {d['false_positive']:4d}  precision {prec}"
        )
    prec = "n/a" if bt["precision"] is None else f"{bt['precision']:.0%}"
    rec = "n/a" if bt["recall"] is None else f"{bt['recall']:.0%}"
    lines.append(
        f"  all rules: {bt['stops']} stops, {bt['false_stops']} with no miss after (precision {prec}); "
        f"caught {rec} of misses ({fmt_k(bt['tokens_in_caught_misses'])} tokens), "
        f"missed {fmt_k(bt['tokens_in_uncovered_misses'])} tokens"
    )
    lines.append(
        f"  leaving out the Orchestrator's own switches ({bt['orchestrator_misses']} misses, "
        f"{fmt_k(bt['tokens_in_orchestrator_misses'])} tokens): caught {bt['caught_without_orchestrator']} of "
        f"{bt['misses_without_orchestrator']} misses, {fmt_k(bt['tokens_caught_without_orchestrator'])} of "
        f"{fmt_k(bt['tokens_without_orchestrator'])} tokens"
    )
    return lines


def render(summary: Dict[str, Any], pairs: List[Pair], list_misses: bool, min_tokens: int) -> str:
    s = summary
    lines = [
        f"Transcripts: {s['transcripts']} (lab chats left out: {s['lab_transcripts_excluded']})  "
        f"model calls: {s['model_calls']}  pairs: {s['call_pairs']}",
        f"Input tokens: {fmt_k(s['input_tokens_total'])}  served from cache: {s['cache_read_share']:.1%}",
        f"Miss events (hit < 50% of a >= {MIN_PREFIX} token prefix): {s['miss_events']}, "
        f"re-billed {fmt_k(s['tokens_rebilled_by_misses'])} tokens",
        "",
        "Misses by primary cause:",
    ]
    for cause, d in s["misses_by_primary_cause"].items():
        lines.append(f"  {cause:36s} {d['events']:4d} events  {fmt_k(d['tokens_rebilled']):>9s} tokens")
    lines += ["", "Signals (how often each was followed by a miss):"]
    for sig, d in s["signals"].items():
        lines.append(f"  {sig:36s} {d['followed_by_miss']:4d}/{d['pairs']:<4d} mean hit {d['mean_hit']:.2f}")
    lines += ["", "Idle gap vs cache hit (same model, no other cause):"]
    for fam, rows in s["idle_curve"].items():
        for label, d in rows.items():
            lines.append(f"  {fam:7s} {label:9s} pairs {d['pairs']:4d}  misses {d['misses']:3d}  mean hit {d['mean_hit']:.2f}")
    lines += ["", "Edits since the chat's previous turn start (scope:category -> turn-start call missed?):"]
    for key, d in s["customization_edits_by_category"].items():
        lines.append(f"  {key:34s} {d['followed_by_miss']:3d}/{d['pairs']}")
    v02, v01 = s["guard_backtest_v02"], s["guard_backtest_v01"]
    lines += ["", f"Guard backtest at >= {fmt_k(min_tokens)} tokens: {v02['eligible_pairs']} pairs, {v02['misses']} misses"]
    ttl = ", ".join(f"{k} {v}s" for k, v in v02["idle_ttl_seconds"].items())
    lines += _render_backtest(f" v0.2 (idle TTL: {ttl}):", v02)
    note = "" if v01["edit_rule_replayed"] else "; edit rule not replayed (v0.1 code not found in git)"
    lines += _render_backtest(f" v0.1 (idle TTL {v01['idle_ttl_seconds']}s{note}):", v01)
    if list_misses:
        lines += ["", "Miss list (conv step model prefix hit gap causes | v0.2 rules that fire):"]
        for p in pairs:
            if p.is_miss and p.prefix >= min_tokens:
                gap = "?" if p.gap is None else f"{p.gap:.0f}s"
                v = v02_rules(p, min_tokens)
                fired = ",".join(sorted(v.fired)) or ("asked earlier" if v.warned_earlier else "-")
                lines.append(
                    f"  {p.conv[:8]} {p.cur.step:5d} {p.prev.model}->{p.cur.model} {fmt_k(p.prefix)} "
                    f"{p.hit:.2f} {gap} {','.join(p.causes) or 'unexplained'} | {fired}"
                )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="*", help="transcript_full.jsonl files (default: all local transcripts)")
    ap.add_argument("--min-tokens", type=int, default=60000, help="guard threshold to backtest (default 60000)")
    ap.add_argument("--idle-ttl", type=int, default=300, help="v0.1 idle rule to backtest, seconds (default 300)")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    ap.add_argument("--list-misses", action="store_true", help="list every miss at or above --min-tokens")
    ap.add_argument("--exclude", action="append", default=[], metavar="CONV_ID", help="leave out a conversation")
    ap.add_argument("--include-lab", action="store_true", help=f"keep the lab chats listed in {LAB_LIST.name}")
    ap.add_argument("--v01-rev", default=V01_REV, help=f"git revision of the v0.1 guard to replay (default {V01_REV})")
    args = ap.parse_args(argv)

    paths = [Path(p) for p in args.paths] if args.paths else default_transcripts()
    exclude = set(args.exclude) | (set() if args.include_lab else load_lab_ids())
    v01 = load_v01(args.v01_rev)
    pairs, edits, stats = build_pairs(paths, exclude, v01)
    summary = summarize(pairs, edits, stats, args.min_tokens, args.idle_ttl, have_v01=v01 is not None)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(render(summary, pairs, args.list_misses, args.min_tokens))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
