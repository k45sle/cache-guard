"""Unit tests for tools/cache_audit.py on synthetic transcripts. Standard library only."""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hooks"))
sys.path.insert(0, str(ROOT / "tools"))
import cache_audit as ca  # noqa: E402
import guard  # noqa: E402

T0 = 1_700_000_000.0


def iso(offset):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(T0 + offset, tz=timezone.utc).isoformat()


def planner(idx, t, model="claude-opus", inp=1000, cr=9000, tools=None):
    return {
        "type": "PLANNER_RESPONSE",
        "step_index": idx,
        "created_at": iso(t),
        "model": model,
        "input_tokens": inp,
        "cache_read_tokens": cr,
        "tool_calls": tools or [],
    }


def user(idx, t=0):
    return {"type": "USER_INPUT", "step_index": idx, "created_at": iso(t), "content": "hi"}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="cg-audit-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.tmp = Path(os.path.realpath(tmp))
        self.home = self.tmp / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("ANTIGRAVITY_APP_DATA_DIR", None)
        self.assertEqual(str(guard.home_dir()), str(self.home))

    def transcript(self, conv, steps):
        d = self.tmp / "brain" / conv / ".system_generated" / "logs"
        d.mkdir(parents=True)
        p = d / "transcript_full.jsonl"
        p.write_text("\n".join(json.dumps(s) for s in steps) + "\n", encoding="utf-8")
        return p

    def pairs(self, steps, conv="conv-a", **kw):
        return ca.build_pairs([self.transcript(conv, steps)], **kw)


class BuildPairsTests(Base):
    def test_hit_and_lost_math(self):
        pairs, _, stats = self.pairs(
            [
                planner(1, 0, inp=1000, cr=9000),  # live 10000
                planner(2, 10, inp=500, cr=9000),  # reads 9000 of 10000
                planner(3, 20, inp=50000, cr=0),  # reads nothing of 9500
            ]
        )
        self.assertEqual(stats["calls"], 3)
        a, b = pairs
        self.assertEqual((a.prefix, a.lost), (10000, 1000))
        self.assertAlmostEqual(a.hit, 0.9)
        self.assertEqual(a.outcome, "hit")
        self.assertEqual((b.prefix, b.lost), (9500, 9500))
        self.assertEqual(b.hit, 0.0)
        self.assertEqual(b.outcome, "total")
        self.assertTrue(b.is_miss)

    def test_cache_read_above_prefix_loses_nothing(self):
        pairs, _, _ = self.pairs([planner(1, 0, inp=100, cr=9900), planner(2, 5, inp=0, cr=20000)])
        self.assertEqual(pairs[0].lost, 0)
        self.assertEqual(pairs[0].hit, 1.0)

    def test_small_prefix_is_not_a_miss(self):
        pairs, _, _ = self.pairs([planner(1, 0, inp=100, cr=100), planner(2, 5, inp=100, cr=0)])
        self.assertEqual(pairs[0].outcome, "small")
        self.assertFalse(pairs[0].is_miss)

    def test_no_pair_across_chat_reset(self):
        pairs, _, stats = self.pairs(
            [planner(1, 0), planner(2, 5), planner(3, 10), planner(0, 15, cr=0), planner(1, 20, cr=0)]
        )
        self.assertEqual(stats["resets"], 1)
        self.assertEqual(len(pairs), 3)  # 1-2, 2-3, 0-1; none from 3 to 0
        self.assertFalse(any(p.prev.step == 3 and p.cur.step == 0 for p in pairs))
        self.assertEqual({p.cur.segment for p in pairs}, {0, 1})

    def test_turn_start_after_user_input(self):
        pairs, _, _ = self.pairs(
            [planner(1, 0), planner(2, 5), user(3, 8), planner(4, 10), planner(5, 15)]
        )
        self.assertEqual([p.cur.turn_start for p in pairs], [False, True, False])
        self.assertTrue(pairs[0].prev.turn_start)  # a chat's first call starts a turn

    def test_exclude_list(self):
        p1 = self.transcript("keep", [planner(1, 0), planner(2, 5)])
        p2 = self.transcript("skip", [planner(1, 0), planner(2, 5)])
        pairs, _, stats = ca.build_pairs([p1, p2], exclude=["skip"])
        self.assertEqual(stats["excluded"], 1)
        self.assertEqual(stats["transcripts"], 1)
        self.assertEqual({p.conv for p in pairs}, {"keep"})

    def test_zero_token_and_bad_lines_are_skipped(self):
        p = self.transcript("c", [planner(1, 0), planner(2, 5, inp=0, cr=0), planner(3, 9)])
        with p.open("a", encoding="utf-8") as fh:
            fh.write("not json\n\n[1]\n")
        pairs, _, stats = ca.build_pairs([p])
        self.assertEqual(stats["calls"], 2)
        self.assertEqual(len(pairs), 1)


class ClassifyTests(Base):
    def causes(self, steps):
        pairs, _, _ = self.pairs(steps)
        return pairs[0].causes

    def test_model_switch(self):
        self.assertEqual(self.causes([planner(1, 0, "claude-opus"), planner(2, 5, "gemini-flash")]), ["model_switch"])

    def test_effort_only_switch(self):
        c = self.causes([planner(1, 0, "gemini-4-neon-low"), planner(2, 5, "gemini-4-neon-high")])
        self.assertEqual(c, ["effort_switch"])

    def test_plan_handoff(self):
        tools = [{"name": "exit_plan_mode", "args": {}}]
        c = self.causes([planner(1, 0, "claude-opus", tools=tools), planner(2, 5, "gemini-flash")])
        self.assertEqual(c, ["plan_handoff_model_switch"])

    def test_idle_gap(self):
        self.assertEqual(self.causes([planner(1, 0), planner(2, 300)]), ["idle_5m_plus"])
        self.assertEqual(self.causes_fresh([planner(1, 0), planner(2, 299)]), [])

    def causes_fresh(self, steps):
        pairs, _, _ = self.pairs(steps, conv="conv-b")
        return pairs[0].causes

    def test_checkpoint_is_compaction(self):
        cp = {"type": "CHECKPOINT", "step_index": 2, "created_at": iso(5), "input_tokens": 2000, "cache_read_tokens": 0}
        self.assertIn("compaction", self.causes([planner(1, 0), cp]))


class V02RulesTests(Base):
    def switch_pairs(self, handoff=False, back_at=120):
        tools = [{"name": "exit_plan_mode", "args": {}}] if handoff else []
        pairs, _, _ = self.pairs(
            [
                planner(1, 0, "claude-opus", inp=1000, cr=69000),  # live 70000
                planner(2, 60, "gemini-flash", inp=100000, cr=0, tools=tools),  # live 100000
                planner(3, back_at, "claude-opus", inp=1000, cr=69000),
            ]
        )
        return pairs

    def test_first_switch_fires(self):
        p = self.switch_pairs()[0]
        self.assertIn("model_switch", ca.v02_rules(p, 60000).fired)

    def test_warm_switch_back_gets_credit(self):
        p = self.switch_pairs()[1]
        self.assertIsNotNone(p.cur_model_last)
        # 100000 live - 70000 still warm = 30000 at risk, under the 60000 threshold
        self.assertNotIn("model_switch", ca.v02_rules(p, 60000).fired)

    def test_no_credit_after_ttl(self):
        p = self.switch_pairs(back_at=60 + 400)[1]
        self.assertIn("model_switch", ca.v02_rules(p, 60000).fired)

    def test_orchestrator_handoff_does_not_fire(self):
        p = self.switch_pairs(handoff=True)[1]
        self.assertTrue(p.orchestrator)
        self.assertNotIn("model_switch", ca.v02_rules(p, 60000).fired)

    def orchestrator_pairs(self, last_model):
        exit_tools = [{"name": "exit_plan_mode", "args": {}}]
        pairs, _, _ = self.pairs(
            [
                planner(1, 0, "gemini-pro-orchestrator-neon", inp=1000, cr=99000, tools=exit_tools),
                planner(2, 30, "gemini-flash-worker-agy", inp=100000, cr=0),
                user(3, 900),
                planner(4, 1000, last_model, inp=110000, cr=0),
            ]
        )
        return pairs

    def test_executor_hand_back_does_not_fire(self):
        p = self.orchestrator_pairs("gemini-pro-orchestrator-neon")[1]
        self.assertTrue(p.orchestrator)
        self.assertEqual(p.causes[0], "orchestrator_return")
        self.assertNotIn("model_switch", ca.v02_rules(p, 60000).fired)

    def test_executor_to_another_model_fires(self):
        p = self.orchestrator_pairs("claude-opus")[1]
        self.assertFalse(p.orchestrator)
        self.assertIn("model_switch", ca.v02_rules(p, 60000).fired)

    def test_idle_fires_at_turn_start(self):
        pairs, _, _ = self.pairs([planner(1, 0, inp=1000, cr=99000), user(2, 5), planner(3, 700, cr=0)])
        self.assertIn("idle", ca.v02_rules(pairs[0], 60000).fired)


class EditTests(Base):
    def own_edits(self, tool_call):
        pairs, edits, _ = self.pairs([planner(1, 0, tools=[tool_call]), planner(2, 5)])
        return pairs[0].own_edits, edits

    def test_rule_file_write_counts_as_prompt_edit(self):
        path = str(self.home / ".gemini" / "config" / "AGENTS.md")
        own, edits = self.own_edits({"name": "write_file", "args": {"TargetFile": path}})
        self.assertEqual([(e.category, e.prompt) for e in own], [("rule", True)])
        self.assertEqual(len(edits), 1)
        self.assertIn("edit_ask", ca.v02_rules(self.pairs_with_edit(path), 60000).fired)

    def pairs_with_edit(self, path):
        steps = [
            planner(1, 0, inp=1000, cr=99000, tools=[{"name": "write_file", "args": {"TargetFile": path}}]),
            planner(2, 5),
        ]
        return self.pairs(steps, conv="conv-edit")[0][0]

    def test_shell_rule_write_counts(self):
        cmd = "echo hi >> ~/.gemini/config/AGENTS.md"
        own, _ = self.own_edits({"name": "run_command", "args": {"CommandLine": cmd}})
        self.assertEqual([(e.category, e.prompt) for e in own], [("rule", True)])

    def test_redirections_are_not_writes(self):
        for cmd in ("ls foo 2>/dev/null", "ls 2>&1 | head", "cat AGENTS.md 2>&1"):
            with self.subTest(cmd=cmd):
                steps = [planner(1, 0, tools=[{"name": "run_command", "args": {"CommandLine": cmd}}]), planner(2, 5)]
                pairs, edits, _ = ca.build_pairs([self.transcript("c" + str(abs(hash(cmd))), steps)])
                self.assertEqual(edits, [])
                self.assertEqual(pairs[0].own_edits, [])


class MainTests(Base):
    def run_main(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ca.main(argv)
        return rc, buf.getvalue()

    def test_json_output_parses(self):
        p = self.transcript("conv-main", [planner(1, 0), planner(2, 5, cr=0), planner(3, 10)])
        rc, out = self.run_main([str(p), "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["transcripts"], 1)
        self.assertEqual(data["call_pairs"], 2)
        self.assertEqual(data["miss_events"], 1)
        self.assertIn("guard_backtest_v02", data)

    def test_text_output_and_list_misses(self):
        p = self.transcript("conv-text", [planner(1, 0), planner(2, 5, cr=0)])
        rc, out = self.run_main([str(p), "--list-misses", "--min-tokens", "1000"])
        self.assertEqual(rc, 0)
        self.assertIn("Miss list", out)

    def test_missing_git_rev_does_not_crash(self):
        self.assertIsNone(ca.load_v01("no-such-rev-0000000"))
        p = self.transcript("conv-norev", [planner(1, 0), planner(2, 5)])
        rc, out = self.run_main([str(p), "--json", "--v01-rev", "no-such-rev-0000000"])
        self.assertEqual(rc, 0)
        self.assertFalse(json.loads(out)["guard_backtest_v01"]["edit_rule_replayed"])

    def test_git_unavailable_does_not_crash(self):
        with mock.patch.object(ca.subprocess, "run", side_effect=OSError("no git")):
            self.assertIsNone(ca.load_v01())

    def test_default_scan_reads_only_temp_home(self):
        rc, out = self.run_main(["--json"])  # no paths: scans HOME/.gemini, which is empty here
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["transcripts"], 0)


if __name__ == "__main__":
    unittest.main()
