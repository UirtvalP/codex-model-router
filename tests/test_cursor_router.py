import json
import os
import sys
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_model_router.cursor_router import (
    DEFAULT_TIMEOUT_SECONDS, ENV_GUARD, ConfigurationError, build_hook_entries,
    handle_hook, handle_observe, install_hooks, load_config, run_evaluator,
)


class Result:
    returncode = 0
    stdout = json.dumps({"result": json.dumps({"model": "cursor-small", "reason": "simple task"})})


def enabled_config():
    return {"enabled": True, "evaluator_model": "composer-2.5", "agent_executable": "/usr/local/bin/cursor-agent", "candidates": ["cursor-small", "cursor-large"], "timeout_seconds": 10}


class CursorRouterTests(unittest.TestCase):
    def test_task_replaces_only_model(self):
        payload = {"tool_name": "Task", "tool_input": {"description": "d", "prompt": "p", "subagent_type": "general", "model": "old", "name": "child", "mode": "ask", "environment": {"x": 1}}}
        calls = []
        def runner(*args, **kwargs):
            calls.append((args, kwargs)); return Result()
        with tempfile.TemporaryDirectory() as directory:
            result = handle_hook(payload, enabled_config(), runner, Path(directory) / "decisions.jsonl")
        self.assertEqual(result["permission"], "allow")
        self.assertEqual(result["updated_input"], {**payload["tool_input"], "model": "cursor-small"})
        self.assertEqual(calls[0][0][0][0], "/usr/local/bin/cursor-agent")
        self.assertIn("composer-2.5", calls[0][0][0])
        self.assertEqual(calls[0][1]["env"][ENV_GUARD], "1")

    def test_invalid_candidate_fails_open_and_logs_error(self):
        payload = {"tool_name": "Task", "tool_input": {"description": "d", "prompt": "p", "model": "old"}}
        class Bad(Result):
            stdout = json.dumps({"result": json.dumps({"model": "unknown", "reason": "x"})})
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "decisions.jsonl"
            result = handle_hook(payload, enabled_config(), lambda *a, **k: Bad(), log)
            record = json.loads(log.read_text(encoding="utf-8"))
        self.assertEqual(result["updated_input"], payload["tool_input"])
        self.assertIn("error:", record["reason"])

    def test_evaluator_sends_task_text_over_stdin_not_argv(self):
        task = {"description": "description", "prompt": "private task prompt"}
        calls = []
        def runner(*args, **kwargs):
            calls.append((args, kwargs))
            return Result()
        self.assertEqual(run_evaluator(task, enabled_config(), runner)[0], "cursor-small")
        command, options = calls[0][0][0], calls[0][1]
        self.assertNotIn("private task prompt", command)
        self.assertIn("private task prompt", options["input"])

    def test_success_log_uses_fixed_reason_summary(self):
        payload = {"tool_name": "Task", "tool_input": {"description": "d", "prompt": "private task prompt", "model": "old"}}
        class Verbose(Result):
            stdout = json.dumps({"result": json.dumps({"model": "cursor-small", "reason": "private task prompt"})})
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "decisions.jsonl"
            handle_hook(payload, enabled_config(), lambda *a, **k: Verbose(), log)
            record = json.loads(log.read_text(encoding="utf-8"))
        self.assertEqual(record["reason"], "evaluated by composer-2.5")
        self.assertNotIn("private task prompt", json.dumps(record))

    def test_evaluator_accepts_json_code_block(self):
        payload = {"tool_name": "Task", "tool_input": {"description": "d", "prompt": "p", "model": "old"}}
        class Fenced(Result):
            stdout = json.dumps({"result": "```json\n{\"model\": \"cursor-small\", \"reason\": \"short\"}\n```"})
        with tempfile.TemporaryDirectory() as directory:
            result = handle_hook(payload, enabled_config(), lambda *a, **k: Fenced(), Path(directory) / "log")
        self.assertEqual(result["updated_input"]["model"], "cursor-small")

    def test_recursive_and_resumed_task_are_unchanged(self):
        payload = {"tool_name": "Task", "tool_input": {"prompt": "p", "model": "old", "resume": True}}
        self.assertEqual(handle_hook(payload, enabled_config())["updated_input"], payload["tool_input"])
        payload["tool_input"].pop("resume")
        with patch.dict(os.environ, {ENV_GUARD: "1"}):
            self.assertEqual(handle_hook(payload, enabled_config())["updated_input"], payload["tool_input"])

    def test_observe_uses_prior_tool_use_id_decision_not_parent_model(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "decisions.jsonl"
            payload = {"tool_name": "Task", "tool_use_id": "call-1", "tool_input": {"description": "d", "prompt": "p", "model": "parent-model"}}
            handle_hook(payload, enabled_config(), lambda *a, **k: Result(), log)
            handle_observe({"event": "subagentStart", "tool_call_id": "call-1", "model": "parent-model", "subagent_model": "cursor-small"}, log)
            record = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(record["id"], "call-1")
        self.assertEqual(record["requested_model"], "cursor-small")
        self.assertEqual(record["actual_model"], "cursor-small")
        self.assertIn("True", record["reason"])

    def test_observe_without_prior_request_never_matches_none(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "decisions.jsonl"
            handle_observe({"event": "subagentStart", "tool_call_id": "unseen"}, log)
            record = json.loads(log.read_text(encoding="utf-8"))
        self.assertIn("matches_requested=False", record["reason"])

    def test_timeout_fails_open_without_logging_prompt(self):
        payload = {"tool_name": "Task", "tool_input": {"description": "d", "prompt": "secret task text", "model": "old"}}
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "decisions.jsonl"
            def timeout(*args, **kwargs):
                raise __import__("subprocess").TimeoutExpired(args[0], 1)
            result = handle_hook(payload, enabled_config(), timeout, log)
            record = json.loads(log.read_text(encoding="utf-8"))
        self.assertEqual(result["updated_input"], payload["tool_input"])
        self.assertIn("timed out", record["reason"])
        self.assertNotIn("secret task text", record["reason"])

    def test_config_excludes_other_models(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = enabled_config()
            config["agent_executable"] = sys.executable
            config["candidates"] = ["composer-2.5", "gpt-5.6-sol-high"]
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(path)
            config["candidates"] = ["composer-2.5", "cursor-grok-4.6-high"]
            path.write_text(json.dumps(config), encoding="utf-8")
            self.assertEqual(load_config(path)["candidates"], config["candidates"])

    def test_enabled_config_requires_explicit_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"enabled": True, "agent_executable": "/x", "candidates": []}), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(path)

    def test_install_is_idempotent_and_preserves_existing_hooks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.json"
            original = {"hooks": {"postToolUse": [{"command": "existing"}]}}
            path.write_text(json.dumps(original), encoding="utf-8")
            _, installed, backup = install_hooks(path, "/usr/bin/python3")
            first = path.read_text(encoding="utf-8")
            _, repeated, repeated_backup = install_hooks(path, "/usr/bin/python3")
            backup_exists = backup is not None and backup.exists()
            after = path.read_text(encoding="utf-8")
        self.assertTrue(installed)
        self.assertTrue(backup_exists)
        self.assertFalse(repeated)
        self.assertIsNone(repeated_backup)
        self.assertEqual(first, after)

    def test_install_quotes_interpreter_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.json"
            install_hooks(path, "/tmp/Cursor Router/python3")
            payload = json.loads(path.read_text(encoding="utf-8"))
        command = payload["hooks"]["preToolUse"][0]["command"]
        self.assertEqual(shlex.split(command)[0], os.path.abspath("/tmp/Cursor Router/python3"))
        self.assertIn(" -I -m codex_model_router.cursor_router", command)

    def test_default_timeout_and_hook_timeout_are_deliberate(self):
        self.assertEqual(DEFAULT_TIMEOUT_SECONDS, 60)
        self.assertEqual(build_hook_entries()["preToolUse"][0]["timeout"], 125)
