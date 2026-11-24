#!/usr/bin/env python3
"""Unit and end-to-end tests for cache-guard."""

import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import guard


def make_transcript(path: Path, *, tokens_in: int, tokens_cached: int, model: str, age_seconds: int = 5, step_index: int = 10) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    step = {
        "step_index": step_index,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "created_at": ts,
        "model": model,
        "input_tokens": tokens_in,
        "cache_read_tokens": tokens_cached,
        "output_tokens": 200,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(step) + "\n", encoding="utf-8")


class CacheGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.fake_home = self.root / "home"
        self.fake_home.mkdir()
        (self.fake_home / ".gemini" / "config" / "rules").mkdir(parents=True)
        (self.fake_home / ".gemini" / "config" / "plugins" / "demo").mkdir(parents=True)
        self.ws = self.root / "repo"
        self.ws.mkdir()
        (self.ws / ".git").mkdir()
        self.data_dir = self.root / "plugin_data"
        self.transcript = self.root / "brain" / "conv-123456" / ".system_generated" / "logs" / "transcript_full.jsonl"

        self.env_patch = mock.patch.dict(
            os.environ,
            {"HOME": str(self.fake_home), "PLUGIN_DATA": str(self.data_dir)},
            clear=False,
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.tmp.cleanup()

    def _base_payload(self, event: str, model: str = "gemini-4-neon-high") -> dict:
        return {
            "conversationId": "conv-123456",
            "workspacePaths": [str(self.ws)],
            "cwd": str(self.ws),
            "transcriptPath": str(self.transcript),
            "modelName": model,
            "hookEventName": event,
        }

    def test_below_threshold_is_noop(self) -> None:
        make_transcript(self.transcript, tokens_in=5000, tokens_cached=30000, model="gemini-4-neon-high")
        payload = self._base_payload("PreToolUse")
        payload["toolCall"] = {
            "name": "write_file",
            "args": {"Path": str(self.ws / "GEMINI.md")},
        }
        self.assertIsNone(guard.dispatch(payload))

    def test_pretool_blocks_customization_file_at_60k(self) -> None:
        make_transcript(self.transcript, tokens_in=4000, tokens_cached=56000, model="gemini-4-neon-high")
        for target in [
            self.ws / "GEMINI.md",
            self.ws / "AGENTS.md",
            self.fake_home / ".gemini" / "config" / "rules" / "new.md",
            self.fake_home / ".gemini" / "config" / "mcp_config.json",
            self.fake_home / ".gemini" / "config" / "plugins" / "demo" / "plugin.json",
        ]:
            payload = self._base_payload("PreToolUse")
            payload["toolCall"] = {"name": "edit_file", "args": {"Path": str(target)}}
            res = guard.dispatch(payload)
            self.assertIsNotNone(res, f"Expected stop for {target}")
            self.assertEqual(res["decision"], "force_ask")
            self.assertIn("60.0k tokens", res["reason"])

    def test_pretool_ignores_normal_source_and_plugin_data(self) -> None:
        make_transcript(self.transcript, tokens_in=10000, tokens_cached=80000, model="gemini-4-neon-high")
        for target in [
            self.ws / "src" / "main.py",
            self.fake_home / ".gemini" / "antigravity" / "plugin_data" / "cache-guard" / "config.json",
            self.fake_home / ".gemini" / "config" / "plugins" / "demo" / "hooks" / "helper.py",
        ]:
            payload = self._base_payload("PreToolUse")
            payload["toolCall"] = {"name": "write_file", "args": {"Path": str(target)}}
            self.assertIsNone(guard.dispatch(payload), f"Did not expect stop for {target}")

    def test_pretool_shell_command_mutating_vs_readonly(self) -> None:
        make_transcript(self.transcript, tokens_in=10000, tokens_cached=70000, model="gemini-4-neon-high")
        # Read-only commands should pass
        for cmd in [
            "cat GEMINI.md",
            "ls -la ~/.gemini/config/rules",
            "agy plugin list",
            "grep -n foo ~/.gemini/config/config.json",
        ]:
            p = self._base_payload("PreToolUse")
            p["toolCall"] = {"name": "run_command", "args": {"CommandLine": cmd}}
            self.assertIsNone(guard.dispatch(p), f"Read-only command flagged: {cmd}")

        # Mutating commands should stop
        for cmd in [
            "agy plugin install cool-plugin",
            "agy plugin disable context7",
            "echo '# rule' >> GEMINI.md",
            "cp /tmp/foo.md ~/.gemini/config/rules/foo.md",
            "sed -i 's/a/b/' AGENTS.md",
        ]:
            p = self._base_payload("PreToolUse")
            p["toolCall"] = {"name": "run_command", "args": {"CommandLine": cmd}}
            res = guard.dispatch(p)
            self.assertIsNotNone(res, f"Mutating command missed: {cmd}")
            self.assertEqual(res["decision"], "force_ask")

    def test_pretool_define_subagent(self) -> None:
        make_transcript(self.transcript, tokens_in=5000, tokens_cached=75000, model="gemini-4-neon-high")
        p = self._base_payload("PreToolUse")
        p["toolCall"] = {"name": "define_subagent", "args": {"Name": "reviewer"}}
        res = guard.dispatch(p)
        self.assertIsNotNone(res)
        self.assertEqual(res["decision"], "force_ask")
        self.assertIn("define_subagent", res["reason"])

    def test_preinvocation_model_switch_and_retry_confirm(self) -> None:
        make_transcript(self.transcript, tokens_in=5000, tokens_cached=75000, model="gemini-4-neon-high", age_seconds=10)
        # Baseline
        guard.dispatch(self._base_payload("SessionStart"))
        # Switch to another model
        p = self._base_payload("PreInvocation", model="claude-sonnet-4-6")
        res1 = guard.dispatch(p)
        self.assertIsNotNone(res1)
        self.assertEqual(res1["terminationBehavior"], "terminate")
        msg = res1["injectSteps"][0]["errorMessage"]["userMessage"]
        self.assertIn("gemini-4-neon-high", msg)
        self.assertIn("claude-sonnet-4-6", msg)

        # Retry confirms and proceeds
        res2 = guard.dispatch(p)
        self.assertIsNone(res2)

    def test_preinvocation_external_customization_drift_and_posttool_dedup(self) -> None:
        rule_file = self.fake_home / ".gemini" / "config" / "rules" / "custom.md"
        rule_file.write_text("# v1\n", encoding="utf-8")
        make_transcript(self.transcript, tokens_in=5000, tokens_cached=75000, model="gemini-4-neon-high", age_seconds=10)
        guard.dispatch(self._base_payload("SessionStart"))

        # External edit
        time.sleep(0.01)
        rule_file.write_text("# v2 updated externally\n", encoding="utf-8")
        res1 = guard.dispatch(self._base_payload("PreInvocation"))
        self.assertIsNotNone(res1)
        self.assertEqual(res1["terminationBehavior"], "terminate")
        self.assertIn("custom.md", res1["injectSteps"][0]["errorMessage"]["userMessage"])

        # Retry confirms
        self.assertIsNone(guard.dispatch(self._base_payload("PreInvocation")))

        # Approved edit via agent tool -> PostToolUse refreshes baseline -> no PreInvocation double-stop
        time.sleep(0.01)
        rule_file.write_text("# v3 approved via force_ask\n", encoding="utf-8")
        post = self._base_payload("PostToolUse")
        post["toolCall"] = {"name": "write_file", "args": {"Path": str(rule_file)}}
        self.assertIsNone(guard.dispatch(post))
        self.assertIsNone(guard.dispatch(self._base_payload("PreInvocation")))

    def test_preinvocation_idle_ttl_and_retry(self) -> None:
        make_transcript(self.transcript, tokens_in=5000, tokens_cached=85000, model="gemini-4-neon-high", age_seconds=420)
        guard.dispatch(self._base_payload("SessionStart"))
        p = self._base_payload("PreInvocation")
        res1 = guard.dispatch(p)
        self.assertIsNotNone(res1)
        self.assertEqual(res1["terminationBehavior"], "terminate")
        self.assertIn("Idle cache TTL", res1["injectSteps"][0]["errorMessage"]["userMessage"])

        # Retry confirms
        self.assertIsNone(guard.dispatch(p))

    def test_fingerprint_covers_global_rules_and_home_agent_skills(self) -> None:
        global_agents = self.fake_home / ".gemini" / "config" / "AGENTS.md"
        global_gemini = self.fake_home / ".gemini" / "config" / "GEMINI.md"
        skill = self.fake_home / ".agents" / "skills" / "demo" / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        global_agents.write_text("# global agents\n", encoding="utf-8")
        global_gemini.write_text("# global gemini\n", encoding="utf-8")
        skill.write_text("# skill\n", encoding="utf-8")

        fp = guard.compute_customization_fingerprint([str(self.ws)], str(self.ws))
        for path in (global_agents, global_gemini, skill):
            self.assertIn(str(path.resolve()), fp, f"Missing fingerprint entry for {path}")

    def test_is_customization_path_covers_global_rules_and_agent_skills(self) -> None:
        global_agents = self.fake_home / ".gemini" / "config" / "AGENTS.md"
        global_gemini = self.fake_home / ".gemini" / "config" / "GEMINI.md"
        skill = self.fake_home / ".agents" / "skills" / "demo" / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        for path in (global_agents, global_gemini, skill):
            path.write_text("# rule\n", encoding="utf-8")
            matched, category = guard.is_customization_path(str(path))
            self.assertTrue(matched, f"Expected customization match for {path}")
            self.assertTrue(category)

        plugin_state = self.fake_home / ".gemini" / "antigravity" / "plugin_data" / "cache-guard" / "config.json"
        plugin_state.parent.mkdir(parents=True, exist_ok=True)
        plugin_state.write_text("{}\n", encoding="utf-8")
        self.assertFalse(guard.is_customization_path(str(plugin_state))[0])

    def test_inspect_shell_command_exempts_plugin_data_and_external_git(self) -> None:
        # plugin_data is runtime state: never a prompt customization hit.
        for cmd in [
            "cp /tmp/quota.json ~/.gemini/antigravity/plugin_data/cache-guard/config.json",
            "rm -f ~/.gemini/antigravity/plugin_data/context-meter/server.pid",
            "python3 -c 'open(\"/home/x/.gemini/antigravity/plugin_data/cache-guard/state.json\", \"w\").write(\"{}\")'",
        ]:
            self.assertEqual(guard.inspect_shell_command(cmd), (False, ""), f"plugin_data flagged: {cmd}")

        # External git inspection / checkout outside ~/.gemini/config and ~/.agents.
        for cmd in [
            "git status",
            "git -C ~/src/other-repo checkout -- main",
            "git diff HEAD~1 > /tmp/patch.diff",
            "git -C ~/src/other-repo restore src/app.py",
        ]:
            self.assertEqual(guard.inspect_shell_command(cmd), (False, ""), f"external git flagged: {cmd}")

        # Real customization mutations are still detected.
        for cmd in [
            "echo '# rule' >> ~/.gemini/config/AGENTS.md",
            "cp /tmp/skill.md ~/.agents/skills/demo/SKILL.md",
            "git checkout -- AGENTS.md && echo '# rule' >> GEMINI.md",
            "rm ~/.gemini/config/GEMINI.md",
        ]:
            matched, category = guard.inspect_shell_command(cmd)
            self.assertTrue(matched, f"Missed customization mutation: {cmd}")
            self.assertTrue(category)

    def test_pretool_shell_plugin_data_and_external_git_are_noop(self) -> None:
        make_transcript(self.transcript, tokens_in=5000, tokens_cached=75000, model="gemini-4-neon-high")
        for cmd in [
            "cp /tmp/quota.json ~/.gemini/antigravity/plugin_data/context-meter/statusline-quota.json",
            "git -C ~/src/other-repo status",
            "git -C ~/src/other-repo diff --stat",
        ]:
            p = self._base_payload("PreToolUse")
            p["toolCall"] = {"name": "run_command", "args": {"CommandLine": cmd}}
            self.assertIsNone(guard.dispatch(p), f"Did not expect stop for: {cmd}")

    def test_shell_wrapper_emits_empty_stdout_on_noop_and_json_on_stop(self) -> None:
        wrapper = Path(__file__).with_name("cache-guard.sh")
        env = dict(os.environ)

        # No-op (< 60k) -> strictly empty stdout
        make_transcript(self.transcript, tokens_in=2000, tokens_cached=10000, model="gemini-4-neon-high")
        noop_payload = self._base_payload("PreToolUse")
        noop_payload["toolCall"] = {"name": "write_file", "args": {"Path": str(self.ws / "GEMINI.md")}}
        proc = subprocess.run(
            ["sh", str(wrapper)],
            input=json.dumps(noop_payload),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

        # Triggered (>= 60k) -> valid JSON with force_ask
        make_transcript(self.transcript, tokens_in=5000, tokens_cached=70000, model="gemini-4-neon-high")
        proc2 = subprocess.run(
            ["sh", str(wrapper)],
            input=json.dumps(noop_payload),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(proc2.returncode, 0)
        parsed = json.loads(proc2.stdout)
        self.assertEqual(parsed["decision"], "force_ask")


if __name__ == "__main__":
    unittest.main()
