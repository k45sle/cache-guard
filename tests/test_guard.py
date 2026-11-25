"""Unit tests for cache-guard v0.2 (hooks/guard.py). Standard library only."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hooks"))
import guard  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "payloads"
CONV = "conv-test-0001"
SKILL_X = "---\nname: x\ndescription: does x\n---\nbody text\n"


def write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class Base(unittest.TestCase):
    """Fresh temp HOME and PLUGIN_DATA for every test; environment restored after."""

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="cg-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.tmp = Path(os.path.realpath(tmp))
        self.home = self.tmp / "home"
        self.data = self.tmp / "data"
        self.ws = self.home / "proj"
        for d in (self.home, self.data, self.ws):
            d.mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "PLUGIN_DATA": str(self.data)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("ANTIGRAVITY_APP_DATA_DIR", None)
        self.assertEqual(str(guard.home_dir()), str(self.home))
        self.cfg_dir = self.home / ".gemini" / "config"
        self.transcript = (
            self.home / ".gemini" / "antigravity-cli" / "brain" / CONV / ".system_generated" / "logs" / "transcript_full.jsonl"
        )

    # -- world ------------------------------------------------------------- #

    def build_world(self):
        c = self.cfg_dir
        write(c / "AGENTS.md", "# rules\nbe nice\n")
        write(c / "rules" / "fleet-routing.md", "route things\n")
        pd = c / "plugins" / "cache-guard"
        write(pd / "plugin.json", json.dumps({"name": "cache-guard", "version": "0.2.0", "description": "d", "logo": "l.svg"}))
        write(pd / "hooks.json", "{}")
        write(pd / "hooks" / "guard.py", "# code\n")
        write(pd / "hooks" / "__pycache__" / "guard.pyc", "bytes")
        write(self.home / ".agents" / "skills" / "x" / "SKILL.md", SKILL_X)
        write(self.home / ".agents" / "skills" / "old" / "SKILL.md", SKILL_X.replace("name: x", "name: old"))
        write(c / "mcp_config.json", json.dumps({"mcpServers": {"a": {"command": "a"}}}))
        write(c / "config.json", json.dumps({"plugins": {"cache-guard": {"enabled": True}}}))
        write(self.home / ".gemini" / "antigravity-cli" / "settings.json", json.dumps({"model": "m", "theme": "dark", "agentMode": "plan"}))
        write(self.ws / "AGENTS.md", "workspace rules\n")

    def scope(self):
        return guard.Scope([str(self.home), str(self.ws)], str(self.home))

    def proj(self):
        return self.scope().projection()

    # -- transcript and payloads ------------------------------------------ #

    def set_transcript(self, steps):
        """steps: oldest first; dict(model, live, age seconds ago, tools)."""
        now = time.time()
        lines = []
        for i, s in enumerate(steps):
            ts = datetime.fromtimestamp(now - s.get("age", 5), timezone.utc).isoformat()
            live = s.get("live", 100000)
            lines.append(
                json.dumps(
                    {
                        "type": "PLANNER_RESPONSE",
                        "step_index": i + 1,
                        "created_at": ts,
                        "model": s.get("model", "gemini-3.8-flash-low"),
                        "input_tokens": live // 10,
                        "cache_read_tokens": live - live // 10,
                        "tool_calls": [{"name": t} for t in s.get("tools", [])],
                    }
                )
            )
        write(self.transcript, "\n".join(lines) + "\n")

    def payload(self, fixture, **over):
        text = (FIXTURES / (fixture + ".json")).read_text(encoding="utf-8")
        text = text.replace("{HOME}", str(self.home)).replace("{WS}", str(self.ws)).replace("{CONV}", CONV)
        obj = json.loads(text)
        obj.update(over)
        return obj

    def pre(self, inv=0, model="gemini-3.8-flash-low"):
        return guard.dispatch(self.payload("PreInvocation-0", invocationNum=inv, modelName=model))

    def session_start(self):
        guard.dispatch(self.payload("SessionStart"))

    def tool(self, event, name, args, call_id="call_0001", **over):
        fx = "PreToolUse-run_command" if event == "PreToolUse" else "PostToolUse-run_command"
        return guard.dispatch(self.payload(fx, toolCall={"id": call_id, "name": name, "args": args}, **over))

    def state(self):
        return json.loads((self.data / "state" / (CONV + ".json")).read_text())

    def write_state(self, st):
        (self.data / "state" / (CONV + ".json")).write_text(json.dumps(st))

    def set_cfg(self, **kw):
        write(self.data / "config.json", json.dumps(kw))

    def assertStopped(self, out, *needles):
        self.assertIsNotNone(out)
        self.assertEqual(out["terminationBehavior"], "terminate")
        msg = out["injectSteps"][0]["errorMessage"]["userMessage"]
        for n in needles:
            self.assertIn(n, msg)
        return msg


# --------------------------------------------------------------------------- #
# 1. Config
# --------------------------------------------------------------------------- #


class ConfigTests(Base):
    def load(self):
        return guard.load_config(self.data)

    def test_v01_seeded_config_rewritten_to_defaults(self):
        write(self.data / "config.json", json.dumps(guard.V01_SEEDED_CONFIG))
        cfg = self.load()
        self.assertEqual(cfg, guard.DEFAULT_CONFIG)
        self.assertEqual(json.loads((self.data / "config.json").read_text()), guard.DEFAULT_CONFIG)

    def test_edited_v01_config_kept_and_int_ttl_applies_to_all_models(self):
        edited = dict(guard.V01_SEEDED_CONFIG, min_tokens=50000, idle_ttl_seconds=400)
        write(self.data / "config.json", json.dumps(edited))
        cfg = self.load()
        self.assertEqual(cfg["min_tokens"], 50000)
        self.assertEqual(cfg["idle_ttl_seconds"], 400)
        for m in ("claude-x", "gemini-4-neon-high", "gpt-5", "whatever"):
            self.assertEqual(guard.ttl_for(cfg, m), 400)
        self.assertEqual(json.loads((self.data / "config.json").read_text())["min_tokens"], 50000)

    def test_bad_values_cleaned(self):
        self.set_cfg(min_tokens="abc", pretool_decision="bogus", idle_ttl_seconds=True)
        cfg = self.load()
        self.assertEqual(cfg["min_tokens"], guard.DEFAULT_CONFIG["min_tokens"])
        self.assertEqual(cfg["pretool_decision"], "force_ask")
        self.assertEqual(cfg["idle_ttl_seconds"], guard.DEFAULT_IDLE_TTL)

    def test_small_ttl_raised_to_30(self):
        self.set_cfg(idle_ttl_seconds=5)
        self.assertEqual(self.load()["idle_ttl_seconds"], 30)

    def test_pretool_decision_values_kept(self):
        for v in ("ask", "deny", "force_ask"):
            self.set_cfg(pretool_decision=v)
            self.assertEqual(self.load()["pretool_decision"], v)

    def test_v01_bundle_seeded_config_rewritten_to_defaults(self):
        seeded = dict(guard.V01_SEEDED_CONFIG, **guard.V01_BUNDLE_SEEDED_EXTRA)
        write(self.data / "config.json", json.dumps(seeded))
        cfg = self.load()
        self.assertEqual(cfg, guard.DEFAULT_CONFIG)
        self.assertEqual(json.loads((self.data / "config.json").read_text()), guard.DEFAULT_CONFIG)

    def test_v01_bundle_config_with_any_orchestrator_edit_kept(self):
        for change in (
            {"max_orchestrator_files": 3},
            {"max_orchestrator_edits": 8},
            {"orchestrator_edit_decision": "ask"},
            {"check_orchestrator_edits": False},
            {"check_orchestrator_edits": 1},
            {"max_orchestrator_files": 2.0},
        ):
            with self.subTest(change=change):
                edited = dict(guard.V01_SEEDED_CONFIG, **guard.V01_BUNDLE_SEEDED_EXTRA)
                edited.update(change)
                write(self.data / "config.json", json.dumps(edited))
                self.load()
                self.assertEqual(json.loads((self.data / "config.json").read_text()), edited)

    def canon_cfg(self):
        return self.home / ".gemini" / "antigravity" / "plugin_data" / "cache-guard" / "config.json"

    def test_no_config_writes_defaults_to_data_dir(self):
        cfg = self.load()
        self.assertEqual(cfg, guard.DEFAULT_CONFIG)
        self.assertEqual(json.loads((self.data / "config.json").read_text()), guard.DEFAULT_CONFIG)
        self.assertFalse(self.canon_cfg().exists())

    def test_plugin_data_set_ignores_canonical_config(self):
        write(self.canon_cfg(), json.dumps({"min_tokens": 1234}))
        self.assertEqual(self.load()["min_tokens"], guard.DEFAULT_CONFIG["min_tokens"])
        self.assertEqual(json.loads((self.data / "config.json").read_text()), guard.DEFAULT_CONFIG)

    def test_without_plugin_data_canonical_config_is_the_fallback(self):
        os.environ.pop("PLUGIN_DATA")
        cli_dir = self.home / ".gemini" / "antigravity-cli" / "plugin_data" / "cache-guard"
        write(self.canon_cfg(), json.dumps({"min_tokens": 1234}))
        self.assertEqual(guard.load_config(cli_dir)["min_tokens"], 1234)
        self.assertFalse((cli_dir / "config.json").exists())

    def test_without_plugin_data_and_no_config_seeds_app_dir(self):
        os.environ.pop("PLUGIN_DATA")
        cli_dir = self.home / ".gemini" / "antigravity-cli" / "plugin_data" / "cache-guard"
        self.assertEqual(guard.load_config(cli_dir), guard.DEFAULT_CONFIG)
        self.assertEqual(json.loads((cli_dir / "config.json").read_text()), guard.DEFAULT_CONFIG)
        self.assertFalse(self.canon_cfg().exists())

    def test_unreadable_config_left_alone(self):
        write(self.data / "config.json", "{not json")
        self.assertEqual(self.load(), guard.DEFAULT_CONFIG)
        self.assertEqual((self.data / "config.json").read_text(), "{not json")

    def test_ttl_longest_key_wins(self):
        self.set_cfg(idle_ttl_seconds={"gemini": 100, "gemini-4-neon": 900})
        cfg = self.load()
        self.assertEqual(guard.ttl_for(cfg, "gemini-4-neon-high"), 900)
        self.assertEqual(guard.ttl_for(cfg, "gemini-3.8-flash-low"), 100)

    def test_ttl_default_key_and_merge_over_defaults(self):
        self.set_cfg(idle_ttl_seconds={"default": 77, "claude": 123})
        cfg = self.load()
        self.assertEqual(guard.ttl_for(cfg, "mystery-model"), 77)
        self.assertEqual(guard.ttl_for(cfg, "claude-sonnet-x"), 123)
        # keys the user did not mention keep their defaults
        self.assertEqual(guard.ttl_for(cfg, "gpt-5"), guard.DEFAULT_IDLE_TTL["gpt"])
        self.assertEqual(guard.ttl_for(cfg, "gemini-3.8-flash-low"), guard.DEFAULT_IDLE_TTL["gemini"])

    def test_ttl_display_names_normalised(self):
        self.assertEqual(guard.ttl_for(guard.DEFAULT_CONFIG, "Gemini 4 Neon (High)"), guard.DEFAULT_IDLE_TTL["gemini-4-neon"])

    def test_default_ttls_for_models_seen_in_logs(self):
        # Measured values (docs/EVIDENCE.md); the longest matching key decides.
        cfg = guard.DEFAULT_CONFIG
        for model, ttl in (
            ("claude-sonnet-5-5-low", 300),
            ("gemini-3.8-flash-low", 450),
            ("gemini-flash-worker-agy", 450),
            ("gemini-pro-orchestrator-g1-pro", 300),
            ("gemini-pro-orchestrator-neon", 900),
            ("Gemini 4 Neon (High)", 900),
            ("gemini-4-neon-xhigh", 900),
            ("mystery-model", 300),
        ):
            with self.subTest(model=model):
                self.assertEqual(guard.ttl_for(cfg, model), ttl)


# --------------------------------------------------------------------------- #
# 2. Fingerprint
# --------------------------------------------------------------------------- #


class FingerprintTests(Base):
    def setUp(self):
        super().setUp()
        self.build_world()

    def changed(self, mutate):
        before = self.proj()
        mutate()
        after = self.proj()
        return bool(guard.diff_projection(before, after))

    def reset_world(self):
        shutil.rmtree(self.home / ".gemini", ignore_errors=True)
        shutil.rmtree(self.home / ".agents", ignore_errors=True)
        self.build_world()

    def test_baseline_is_stable(self):
        self.assertTrue(self.proj())
        self.assertEqual(self.proj(), self.proj())

    def test_changes_that_alter_projection(self):
        c, h = self.cfg_dir, self.home
        pj = c / "plugins" / "cache-guard" / "plugin.json"

        def plugin_key():
            obj = json.loads(pj.read_text())
            obj["contextFileName"] = "X.md"
            write(pj, json.dumps(obj))

        def disable_plugin():
            write(c / "config.json", json.dumps({"plugins": {"cache-guard": {"enabled": False}}}))

        def remove_skill():
            shutil.rmtree(h / ".agents" / "skills" / "old")

        cases = {
            "AGENTS.md text": lambda: write(c / "AGENTS.md", "# rules\nbe rude\n"),
            "GEMINI.md added": lambda: write(c / "GEMINI.md", "new rules\n"),
            "home GEMINI.md": lambda: write(h / ".gemini" / "GEMINI.md", "new rules\n"),
            "workspace AGENTS.md": lambda: write(self.ws / "AGENTS.md", "changed\n"),
            "rules file": lambda: write(c / "rules" / "fleet-routing.md", "other\n"),
            "rules file added": lambda: write(c / "rules" / "extra.md", "other\n"),
            "skill description": lambda: write(h / ".agents" / "skills" / "x" / "SKILL.md", SKILL_X.replace("does x", "does y")),
            "skill name": lambda: write(h / ".agents" / "skills" / "x" / "SKILL.md", SKILL_X.replace("name: x", "name: z")),
            "skill added": lambda: write(h / ".agents" / "skills" / "new" / "SKILL.md", SKILL_X.replace("name: x", "name: new")),
            "skill removed": remove_skill,
            "mcp_config": lambda: write(c / "mcp_config.json", json.dumps({"mcpServers": {"b": {"command": "b"}}})),
            "plugin disabled": disable_plugin,
            "plugin.json prompt key": plugin_key,
            "settings agentMode": lambda: write(
                h / ".gemini" / "antigravity-cli" / "settings.json", json.dumps({"model": "m", "theme": "dark", "agentMode": "fast"})
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                self.reset_world()
                self.assertTrue(self.changed(mutate))

    def test_plugin_enable_changes_projection(self):
        c = self.cfg_dir
        write(c / "config.json", json.dumps({"plugins": {"cache-guard": {"enabled": False}}}))
        before = self.proj()
        write(c / "config.json", json.dumps({"plugins": {"cache-guard": {"enabled": True}}}))
        self.assertTrue(guard.diff_projection(before, self.proj()))

    def test_changes_that_keep_projection(self):
        c, h = self.cfg_dir, self.home
        pd = c / "plugins" / "cache-guard"
        pj = pd / "plugin.json"

        def ui_keys():
            obj = json.loads(pj.read_text())
            for k in guard.PLUGIN_NON_PROMPT_KEYS - {"hooks", "extensions"}:
                obj[k] = "changed-" + k
            write(pj, json.dumps(obj))

        def reformat_json():
            obj = json.loads((c / "mcp_config.json").read_text())
            write(c / "mcp_config.json", json.dumps(obj, indent=4, sort_keys=False) + "\n\n")

        cases = {
            "hooks.json": lambda: write(pd / "hooks.json", '{"hooks": {"x": 1}}'),
            "skill body": lambda: write(h / ".agents" / "skills" / "x" / "SKILL.md", SKILL_X.replace("body text", "totally new body")),
            "plugin.json UI keys": ui_keys,
            "settings model/theme": lambda: write(
                h / ".gemini" / "antigravity-cli" / "settings.json", json.dumps({"model": "other", "theme": "light", "agentMode": "plan"})
            ),
            "__pycache__ file": lambda: write(pd / "hooks" / "__pycache__" / "other.pyc", "zzz"),
            "plugin code": lambda: write(pd / "hooks" / "guard.py", "# different\n"),
            "mcp json reformat": reformat_json,
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                self.reset_world()
                self.assertFalse(self.changed(mutate))


# --------------------------------------------------------------------------- #
# 3. Drift only at turn start
# --------------------------------------------------------------------------- #


class DriftTests(Base):
    def setUp(self):
        super().setUp()
        self.build_world()
        self.set_transcript([{"live": 100000}])
        self.session_start()

    def edit_rules(self, text="# rules\nbe rude\n"):
        write(self.cfg_dir / "AGENTS.md", text)

    def test_no_change_no_stop(self):
        self.assertIsNone(self.pre(0))

    def test_drift_ignored_mid_turn(self):
        self.edit_rules()
        self.assertIsNone(self.pre(1))
        self.assertIsNone(self.pre(2))

    def test_drift_stops_at_turn_start(self):
        self.edit_rules()
        self.assertIsNone(self.pre(1))
        self.assertStopped(self.pre(0), "Prompt files changed")

    def test_undo_before_turn_start_does_not_stop(self):
        self.edit_rules()
        self.assertIsNone(self.pre(1))
        self.edit_rules("# rules\nbe nice\n")
        self.assertIsNone(self.pre(0))

    def test_after_confirmed_resend_next_turn_does_not_stop(self):
        self.edit_rules()
        self.assertStopped(self.pre(0))
        self.assertIsNone(self.pre(0))  # confirmed resend
        self.assertIsNone(self.pre(0))  # baseline moved


# --------------------------------------------------------------------------- #
# 4. Resend confirm
# --------------------------------------------------------------------------- #


class ResendTests(Base):
    def setUp(self):
        super().setUp()
        self.build_world()
        self.set_transcript([{"live": 100000}])
        self.session_start()
        write(self.cfg_dir / "AGENTS.md", "changed\n")

    def test_identical_resend_goes_ahead(self):
        self.assertStopped(self.pre(0))
        self.assertIsNone(self.pre(0))

    def test_resend_after_window_stops_again(self):
        self.assertStopped(self.pre(0))
        st = self.state()
        window = guard.DEFAULT_CONFIG["retry_confirm_window_seconds"]
        st["pending"]["ts"] -= window + 10
        self.write_state(st)
        self.assertStopped(self.pre(0))

    def test_resend_inside_configured_window(self):
        self.set_cfg(retry_confirm_window_seconds=600)
        self.assertStopped(self.pre(0))
        st = self.state()
        st["pending"]["ts"] -= 400
        self.write_state(st)
        self.assertIsNone(self.pre(0))

    def test_different_reason_stops_again(self):
        self.assertStopped(self.pre(0))
        write(self.cfg_dir / "AGENTS.md", "changed again, differently\n")
        self.assertStopped(self.pre(0))


# --------------------------------------------------------------------------- #
# 5. PreToolUse / PostToolUse
# --------------------------------------------------------------------------- #


class ToolTests(Base):
    def setUp(self):
        super().setUp()
        self.build_world()
        self.set_transcript([{"live": 100000}])
        self.session_start()
        self.agents = self.cfg_dir / "AGENTS.md"
        self.cmd = {"CommandLine": "echo hi >> %s" % self.agents}

    def test_shell_write_asks_and_records_call(self):
        out = self.tool("PreToolUse", "run_command", self.cmd)
        self.assertEqual(out["decision"], "force_ask")
        self.assertTrue(out["reason"])
        self.assertIn("call_0001", self.state()["warned_calls"])

    def test_file_write_asks(self):
        out = self.tool("PreToolUse", "write_file", {"Path": str(self.agents), "Content": "new\n"})
        self.assertEqual(out["decision"], "force_ask")

    def test_harmless_command_silent(self):
        self.assertIsNone(self.tool("PreToolUse", "run_command", {"CommandLine": "echo hi > note.txt"}))

    def test_below_min_tokens_silent(self):
        self.set_transcript([{"live": 1000}])
        self.assertIsNone(self.tool("PreToolUse", "run_command", self.cmd))

    def test_pretool_decision_passthrough(self):
        for v in ("ask", "deny"):
            self.set_cfg(pretool_decision=v)
            out = self.tool("PreToolUse", "run_command", self.cmd)
            self.assertEqual(out["decision"], v)

    def test_define_subagent_asks_and_can_be_disabled(self):
        out = self.tool("PreToolUse", "define_subagent", {"Name": "helper"})
        self.assertEqual(out["decision"], "force_ask")
        self.set_cfg(check_define_subagent=False)
        self.assertIsNone(self.tool("PreToolUse", "define_subagent", {"Name": "helper"}))

    def test_customization_check_disabled(self):
        self.set_cfg(check_customization_edits=False)
        self.assertIsNone(self.tool("PreToolUse", "run_command", self.cmd))

    def test_ack_after_change_prevents_drift_stop(self):
        self.assertIsNotNone(self.tool("PreToolUse", "run_command", self.cmd))
        with open(str(self.agents), "a") as fh:
            fh.write("hi\n")
        self.tool("PostToolUse", "run_command", self.cmd, error="")
        st = self.state()
        self.assertTrue(st.get("ack"))
        self.assertNotIn("call_0001", st.get("warned_calls", []))
        self.assertIsNone(self.pre(0))

    def test_post_tool_with_error_stores_no_ack(self):
        self.tool("PreToolUse", "run_command", self.cmd)
        with open(str(self.agents), "a") as fh:
            fh.write("hi\n")
        self.tool("PostToolUse", "run_command", self.cmd, error="boom")
        self.assertNotIn("ack", self.state())
        self.assertStopped(self.pre(0))

    def test_post_tool_for_unwarned_call_stores_no_ack(self):
        with open(str(self.agents), "a") as fh:
            fh.write("hi\n")
        self.tool("PostToolUse", "run_command", self.cmd, call_id="call_other")
        st = self.data / "state" / (CONV + ".json")
        self.assertTrue(not st.exists() or "ack" not in json.loads(st.read_text()))
        self.assertStopped(self.pre(0))


# --------------------------------------------------------------------------- #
# 6. Model switch
# --------------------------------------------------------------------------- #

A = "gemini-3.8-flash-low"
B = "claude-sonnet-x"
PLANNER = "gemini-pro-orchestrator-neon"  # model ids seen in the 21 Nov Orchestrator chats
EXECUTOR = "gemini-flash-worker-agy"


class ModelSwitchTests(Base):
    def test_switch_stops_and_names_both(self):
        self.set_transcript([{"model": A, "live": 100000}])
        self.assertStopped(self.pre(2, B), A, B)

    def test_switch_at_any_invocation(self):
        for inv in (0, 1, 2):
            with self.subTest(inv=inv):
                self.set_transcript([{"model": A, "live": 100000}])
                shutil.rmtree(str(self.data / "state"), ignore_errors=True)
                self.assertStopped(self.pre(inv, B), A, B)

    def test_below_min_tokens_no_stop(self):
        self.set_transcript([{"model": A, "live": 1000}])
        self.assertIsNone(self.pre(2, B))

    def test_same_model_no_stop(self):
        self.set_transcript([{"model": A, "live": 100000}])
        self.assertIsNone(self.pre(2, A))

    # The Orchestrator: its Planner hands each plan to the Executor (exit_plan_mode)
    # and takes the chat back at the next message. Both switches go through.
    def test_orchestrator_handoff_goes_through(self):
        for inv in (0, 1, 2):
            with self.subTest(inv=inv):
                self.set_transcript([{"model": PLANNER, "live": 400000, "tools": ["exit_plan_mode"]}])
                shutil.rmtree(str(self.data / "state"), ignore_errors=True)
                self.assertIsNone(self.pre(inv, EXECUTOR))

    def test_handoff_ignores_an_old_executor_call(self):
        self.set_transcript(
            [
                {"model": EXECUTOR, "live": 90000, "age": 3000},
                {"model": PLANNER, "live": 400000, "age": 5, "tools": ["exit_plan_mode"]},
            ]
        )
        self.assertIsNone(self.pre(2, EXECUTOR))

    def test_executor_hands_back_to_planner(self):
        ttl = guard.ttl_for(guard.DEFAULT_CONFIG, PLANNER)
        self.set_transcript(
            [
                {"model": PLANNER, "live": 100000, "age": ttl + 600, "tools": ["exit_plan_mode"]},
                {"model": EXECUTOR, "live": 300000, "age": ttl + 500},
                {"model": EXECUTOR, "live": 400000, "age": 30},
            ]
        )
        self.assertIsNone(self.pre(0, PLANNER))

    def test_switch_into_the_orchestrator_stops(self):
        self.set_transcript([{"model": A, "live": 100000}])
        msg = self.assertStopped(self.pre(0, PLANNER), A, PLANNER)
        self.assertIn(f"switch back to {A}", msg)

    def test_leaving_the_orchestrator_stops_and_names_it(self):
        self.set_transcript(
            [{"model": PLANNER, "live": 90000, "tools": ["exit_plan_mode"]}, {"model": EXECUTOR, "live": 100000}]
        )
        msg = self.assertStopped(self.pre(0, A), EXECUTOR, A)
        self.assertIn("switch back to the Orchestrator", msg)

    def test_executor_to_another_model_is_not_a_hand_back(self):
        self.set_transcript(
            [{"model": PLANNER, "live": 90000, "tools": ["exit_plan_mode"]}, {"model": EXECUTOR, "live": 100000}]
        )
        self.assertStopped(self.pre(0, B))

    def test_exit_plan_mode_on_one_model_is_no_hand_off(self):
        # Plan mode without the Orchestrator keeps the model: nothing to let through later.
        self.set_transcript([{"model": A, "live": 90000, "tools": ["exit_plan_mode"]}, {"model": A, "live": 100000}])
        self.assertStopped(self.pre(0, B))
        self.assertEqual(guard.handoff_pairs(str(self.transcript)), set())

    def test_handoff_pairs_from_transcript(self):
        self.set_transcript(
            [
                {"model": PLANNER, "live": 90000, "tools": ["exit_plan_mode"]},
                {"model": EXECUTOR, "live": 100000},
                {"model": "Orchestrator (Carbon)", "live": 110000, "tools": ["exit_plan_mode"]},
                {"model": "model-placeholder-1", "live": 120000},
            ]
        )
        self.assertEqual(guard.handoff_pairs(str(self.transcript)), {(PLANNER, EXECUTOR)})

    def test_placeholder_names_never_count(self):
        self.set_transcript([{"model": A, "live": 100000}])
        self.assertIsNone(self.pre(2, "auto"))
        self.assertIsNone(self.pre(2, "model-placeholder-1"))
        self.set_transcript([{"model": "model-placeholder-1", "live": 100000}])
        self.assertIsNone(self.pre(2, B))

    def test_effort_suffix_is_a_different_model(self):
        self.set_transcript([{"model": "gemini-3.8-flash-low", "live": 100000}])
        self.assertStopped(self.pre(2, "gemini-3.8-flash-high"))

    def test_display_name_equals_id(self):
        self.set_transcript([{"model": "gemini-4-neon-high", "live": 100000}])
        self.assertIsNone(self.pre(2, "Gemini 4 Neon (High)"))

    # credit from the transcript
    def test_credit_small_delta_goes_ahead(self):
        self.set_transcript([{"model": B, "live": 95000, "age": 100}, {"model": A, "live": 100000, "age": 5}])
        self.assertIsNone(self.pre(2, B))

    def test_credit_large_delta_stops(self):
        self.set_transcript([{"model": B, "live": 20000, "age": 100}, {"model": A, "live": 100000, "age": 5}])
        self.assertStopped(self.pre(2, B), A, B)

    def test_credit_expired_stops(self):
        ttl = guard.ttl_for(guard.DEFAULT_CONFIG, B)
        self.set_transcript([{"model": B, "live": 99000, "age": ttl + 120}, {"model": A, "live": 100000, "age": 5}])
        self.assertStopped(self.pre(2, B))

    # credit from saved state (the old call is no longer in the transcript)
    def test_state_credit_small_delta_goes_ahead(self):
        self.set_transcript([{"model": B, "live": 95000, "age": 100}])
        self.assertIsNone(self.pre(2, B))
        self.set_transcript([{"model": A, "live": 100000, "age": 5}])
        self.assertIsNone(self.pre(2, B))

    def test_state_credit_large_delta_stops(self):
        self.set_transcript([{"model": B, "live": 20000, "age": 100}])
        self.assertIsNone(self.pre(2, B))
        self.set_transcript([{"model": A, "live": 100000, "age": 5}])
        self.assertStopped(self.pre(2, B))

    def test_state_credit_expired_stops(self):
        ttl = guard.ttl_for(guard.DEFAULT_CONFIG, B)
        self.set_transcript([{"model": B, "live": 95000, "age": ttl + 120}])
        self.assertIsNone(self.pre(2, B))
        self.set_transcript([{"model": A, "live": 100000, "age": 5}])
        self.assertStopped(self.pre(2, B))

    def test_check_model_switch_disabled(self):
        self.set_cfg(check_model_switch=False)
        self.set_transcript([{"model": A, "live": 100000}])
        self.assertIsNone(self.pre(2, B))


# --------------------------------------------------------------------------- #
# 7. Idle per model
# --------------------------------------------------------------------------- #

NEON = "gemini-4-neon-high"
FLASH = "gemini-3.8-flash-low"


class IdleTests(Base):
    def setUp(self):
        super().setUp()
        self.set_cfg(idle_ttl_seconds={"gemini": 300, "gemini-4-neon": 900})

    def test_older_than_ttl_stops_with_idle_reason(self):
        self.set_transcript([{"model": FLASH, "live": 100000, "age": 400}])
        self.assertStopped(self.pre(0, FLASH), "idle")

    def test_younger_than_ttl_does_not_stop(self):
        self.set_transcript([{"model": FLASH, "live": 100000, "age": 200}])
        self.assertIsNone(self.pre(0, FLASH))

    def test_mid_turn_never_idle(self):
        self.set_transcript([{"model": FLASH, "live": 100000, "age": 4000}])
        self.assertIsNone(self.pre(1, FLASH))
        self.assertIsNone(self.pre(2, FLASH))

    def test_longer_ttl_model_not_stopped_at_same_gap(self):
        self.set_transcript([{"model": NEON, "live": 100000, "age": 400}])
        self.assertIsNone(self.pre(0, NEON))
        self.set_transcript([{"model": FLASH, "live": 100000, "age": 400}])
        self.assertStopped(self.pre(0, FLASH), "idle")

    def test_neon_stops_past_its_own_ttl(self):
        self.set_transcript([{"model": NEON, "live": 100000, "age": 1000}])
        self.assertStopped(self.pre(0, NEON), "idle")

    def test_idle_check_disabled(self):
        self.set_cfg(check_idle_ttl=False)
        self.set_transcript([{"model": FLASH, "live": 100000, "age": 4000}])
        self.assertIsNone(self.pre(0, FLASH))


# --------------------------------------------------------------------------- #
# 8. Below min_tokens
# --------------------------------------------------------------------------- #


class BelowMinTests(Base):
    def test_nothing_fires_and_baseline_accepted(self):
        self.build_world()
        self.set_transcript([{"model": A, "live": 1000, "age": 5000}])
        self.session_start()
        write(self.cfg_dir / "AGENTS.md", "changed\n")
        self.assertIsNone(self.pre(0, B))  # model switch, idle and drift all ignored
        self.assertEqual(self.state()["baseline"], self.proj())
        self.set_transcript([{"model": A, "live": 100000, "age": 5}])
        self.assertIsNone(self.pre(0, A))  # no stale drift later

    def test_min_tokens_is_configurable(self):
        self.set_cfg(min_tokens=500)
        self.set_transcript([{"model": A, "live": 1000}])
        self.assertStopped(self.pre(2, B))


# --------------------------------------------------------------------------- #
# 9. Edit simulation
# --------------------------------------------------------------------------- #


class EditSimulationTests(Base):
    def setUp(self):
        super().setUp()
        self.build_world()
        self.skill = self.home / ".agents" / "skills" / "x" / "SKILL.md"
        self.agents = self.cfg_dir / "AGENTS.md"
        self.hooks = self.cfg_dir / "plugins" / "cache-guard" / "hooks.json"

    def find(self, name, args):
        return guard.file_tool_findings(name, args, str(self.home), self.scope())

    def test_write_file(self):
        p = str(self.skill)
        self.assertEqual(self.find("write_file", {"Path": p, "Content": SKILL_X.replace("body text", "other")}), [])
        self.assertTrue(self.find("write_file", {"Path": p, "Content": SKILL_X.replace("does x", "does z")}))

    def test_write_to_file(self):
        p = str(self.skill)
        self.assertEqual(self.find("write_to_file", {"TargetFile": p, "CodeContent": SKILL_X.replace("body text", "o")}), [])
        self.assertTrue(self.find("write_to_file", {"TargetFile": p, "CodeContent": SKILL_X.replace("name: x", "name: q")}))

    def test_edit_file(self):
        p = str(self.skill)
        self.assertEqual(self.find("edit_file", {"Path": p, "Target": "body text", "Replacement": "new body"}), [])
        self.assertTrue(self.find("edit_file", {"Path": p, "Target": "does x", "Replacement": "does y"}))

    def test_replace_file_content(self):
        p = str(self.skill)
        args = {"TargetFile": p, "TargetContent": "body text", "ReplacementContent": "new body"}
        self.assertEqual(self.find("replace_file_content", args), [])
        args = {"TargetFile": p, "TargetContent": "does x", "ReplacementContent": "does y"}
        self.assertTrue(self.find("replace_file_content", args))

    def test_multi_replace_file_content(self):
        p = str(self.skill)
        body = [{"TargetContent": "body text", "ReplacementContent": "b2"}]
        self.assertEqual(self.find("multi_replace_file_content", {"TargetFile": p, "ReplacementChunks": body}), [])
        both = body + [{"TargetContent": "does x", "ReplacementContent": "does y"}]
        self.assertTrue(self.find("multi_replace_file_content", {"TargetFile": p, "ReplacementChunks": both}))

    def test_edit_whose_target_is_missing_changes_nothing(self):
        args = {"Path": str(self.agents), "Target": "not in file", "Replacement": "x"}
        self.assertEqual(self.find("edit_file", args), [])

    def test_agents_md_any_change(self):
        p = str(self.agents)
        for text in ("# rules\nbe nice\n\n", "# rules\nbe nice \n", "different\n"):
            with self.subTest(text=text):
                self.assertTrue(self.find("write_file", {"Path": p, "Content": text}))
        self.assertTrue(self.find("edit_file", {"Path": p, "Target": "nice", "Replacement": "kind"}))

    def test_hooks_json_never_a_finding(self):
        self.assertEqual(self.find("write_file", {"Path": str(self.hooks), "Content": '{"x": 1}'}), [])

    def test_identical_content_no_finding(self):
        self.assertEqual(self.find("write_file", {"Path": str(self.agents), "Content": self.agents.read_text()}), [])

    def test_new_skill_file_is_a_finding(self):
        p = self.home / ".agents" / "skills" / "fresh" / "SKILL.md"
        self.assertTrue(self.find("write_file", {"Path": str(p), "Content": SKILL_X}))

    def test_unrelated_file_no_finding(self):
        self.assertEqual(self.find("write_file", {"Path": str(self.home / "notes.txt"), "Content": "x"}), [])

    def test_simulate_returns_none_when_unknown(self):
        self.assertIsNone(guard.simulate_file_tool("edit_file", {"Target": "a", "Replacement": "b"}, None))
        self.assertIsNone(guard.simulate_file_tool("nope", {}, "text"))
        self.assertEqual(guard.simulate_file_tool("edit_file", {"Target": "a", "Replacement": "b"}, "aaa"), "baa")
        self.assertEqual(
            guard.simulate_file_tool("edit_file", {"Target": "a", "Replacement": "b", "AllowMultiple": True}, "aaa"), "bbb"
        )


# --------------------------------------------------------------------------- #
# 10. Shell command table
# --------------------------------------------------------------------------- #

SHELL_TABLE = [
    ("ok", "ls -la ~/.gemini/config 2>/dev/null"),
    ("ok", "cat ~/.gemini/config/AGENTS.md 2>&1 | head"),
    ("HIT", "echo hi >> ~/.gemini/config/AGENTS.md"),
    ("ok", "echo hi > /tmp/x.txt 2>&1"),
    ("ok", "rm -rf ~/.gemini/config/plugins/cache-guard/hooks/__pycache__"),
    ("HIT", "rm -rf ~/.gemini/config/plugins/cache-guard"),
    ("HIT", "cp -r /Users/me/cache-guard ~/.gemini/config/plugins/"),
    ("ok", "cp /Users/me/cache-guard/hooks/guard.py ~/.gemini/config/plugins/cache-guard/hooks/guard.py"),
    ("HIT", "cd ~/.gemini/config && git pull"),
    ("ok", "git -C /Users/me/cache-guard checkout -b feature"),
    ("ok", "git -C /Users/me/cache-guard pull"),
    ("HIT", "sed -i '' 's/a/b/' ~/.agents/skills/x/SKILL.md"),
    ("ok", "sed -n '1,5p' ~/.agents/skills/x/SKILL.md"),
    ("HIT", "agy plugin install ./cache-guard"),
    ("ok", "agy plugin list"),
    ("HIT", "python3 -c \"open('/Users/me/.gemini/config/rules/x.md','w').write('x')\""),
    ("ok", "python3 -c \"print(open('/Users/me/.gemini/config/rules/fleet-routing.md').read())\""),
    ("HIT", "F=~/.gemini/config/AGENTS.md; echo x > $F"),
    ("HIT", "if [ -f x ]; then rm ~/.agents/skills/old/SKILL.md; fi"),
    ("ok", "echo '# not a comment' > /tmp/y # comment > ~/.gemini/config/AGENTS.md"),
    ("HIT", "cat > ~/.agents/skills/new/SKILL.md <<'EOF'\n---\nname: new\n---\nbody\nEOF"),
    ("HIT", "python3 - <<'PY'\nfrom pathlib import Path\nPath('/Users/me/.gemini/config/AGENTS.md').write_text('x')\nPY"),
    ("ok", "touch ~/.gemini/config/plugins/cache-guard/hooks.json"),
    ("HIT", "mv ~/.agents/skills/x ~/.agents/skills-old/x"),
    ("ok", "find ~/.agents/skills -name '*.pyc' -delete"),
    ("HIT", "find ~/.agents/skills -name SKILL.md -delete"),
    ("ok", "tar -czf /tmp/out.tgz --exclude=x ~/.gemini/config"),
    ("HIT", "tar -xzf /tmp/out.tgz -C ~/.gemini/config/plugins/"),
    ("HIT", "bash -c 'echo x >> ~/.gemini/config/rules/a.md'"),
    ("ok", "npm test 2>&1 | tail -5"),
    ("ok", 'echo "unbalanced'),
    # Scripts that only name other files in a plugin folder (seen in real chats).
    (
        "ok",
        "node -e \"const c=require('child_process').spawn('node',['/Users/me/.gemini/config/plugins/cm/server/web.mjs']);"
        " c.stdout.on('data',d=>process.stdout.write(d))\"",
    ),
    (
        "ok",
        "python3 - <<'PY'\nimport json\nspec = {'cwd': '/Users/me/.gemini/config/plugins/cm/lib',"
        " 'prompt': 'Apply 2 fixes to /Users/me/.gemini/config/plugins/cm/lib/cli.mjs:\\n'}\n"
        "open('/tmp/spec.json', 'w').write(json.dumps(spec))\nPY",
    ),
    # Paths built at run time from pieces still count.
    (
        "HIT",
        "python3 - <<'PY'\nimport os\nbase = os.path.expanduser('~') + '/.gemini/config/plugins/x'\n"
        "open(base + '/plugin.json', 'w').write('{}')\nPY",
    ),
    ("HIT", "python3 -c \"open('AGENTS.md', 'w').write('x')\""),
    (
        "HIT",
        "python3 - <<'PY'\nimport os\nos.system('cp /tmp/a.md /Users/me/.gemini/config/AGENTS.md')\n"
        "open('/tmp/log', 'w').write('done')\nPY",
    ),
]


class ShellTests(Base):
    def setUp(self):
        super().setUp()
        self.build_world()

    def findings(self, cmd):
        home = str(self.home)
        return guard.shell_findings(cmd, home, guard.Scope([home], home))

    def test_table(self):
        for expect, cmd in SHELL_TABLE:
            with self.subTest(cmd=cmd):
                found = self.findings(cmd.replace("/Users/me", str(self.home)))
                if expect == "HIT":
                    self.assertTrue(found, "expected a finding")
                else:
                    self.assertEqual(found, [])

    def test_stderr_redirects_alone_are_not_writes(self):
        for cmd in (
            "ls ~/.gemini/config/AGENTS.md 2>/dev/null",
            "cat ~/.agents/skills/x/SKILL.md 2>&1",
            "cat ~/.gemini/config/AGENTS.md 2>/dev/null 1>&2",
            "ls ~/.gemini/config/rules >/dev/null 2>&1",
            "cat ~/.gemini/config/AGENTS.md &>/dev/null",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.findings(cmd), [])

    def test_unbalanced_quote_with_prompt_write_is_conservative(self):
        self.assertTrue(self.findings('echo "oops >> ~/.gemini/config/AGENTS.md'))


# --------------------------------------------------------------------------- #
# 11. Wrapper and main()
# --------------------------------------------------------------------------- #


class WrapperTests(Base):
    def run_wrapper(self, stdin):
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        env["PLUGIN_DATA"] = str(self.data)
        for k in ("PLUGIN_ROOT", "ANTIGRAVITY_APP_DATA_DIR"):
            env.pop(k, None)
        return subprocess.run(
            ["sh", str(ROOT / "hooks" / "cache-guard.sh")],
            input=stdin,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=30,
        )

    def test_noop_payload_is_silent(self):
        r = self.run_wrapper(json.dumps(self.payload("PostInvocation")))
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_stopping_payload_emits_one_json_line(self):
        self.set_transcript([{"model": FLASH, "live": 100000, "age": 5000}])
        r = self.run_wrapper(json.dumps(self.payload("PreInvocation-0", invocationNum=0, modelName=FLASH)))
        self.assertEqual(r.returncode, 0)
        lines = r.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["terminationBehavior"], "terminate")

    def test_invalid_json_is_silent(self):
        r = self.run_wrapper("this is { not json")
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_empty_stdin_is_silent(self):
        r = self.run_wrapper("")
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_main_with_garbage_stdin(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO("garbage{")), mock.patch.object(sys, "stdout", out), mock.patch.object(
            sys, "stderr", err
        ):
            rc = guard.main()
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "")

    def test_main_prints_one_line_for_stop(self):
        self.set_transcript([{"model": FLASH, "live": 100000, "age": 5000}])
        out = io.StringIO()
        raw = json.dumps(self.payload("PreInvocation-0", invocationNum=0, modelName=FLASH))
        with mock.patch.object(sys, "stdin", io.StringIO(raw)), mock.patch.object(sys, "stdout", out):
            self.assertEqual(guard.main(), 0)
        self.assertEqual(len(out.getvalue().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
