import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stderr
from unittest.mock import patch

from codex_model_router import cli as cli_module
from codex_model_router.router import (
    CODEX_EVALUATOR_FAST,
    CODEX_EVALUATOR_GUARD,
    CODEX_EVALUATOR_EFFORT,
    CODEX_EVALUATOR_MODEL,
    CODEX_MODEL_ROUTER_CONFIG,
    FALLBACK_MODELS,
    ModelCatalog,
    RouteChoice,
    _choice_from_payload,
    _evaluator_observability,
    append_decision_log,
    apply_policy,
    build_classifier_command,
    build_codex_command,
    model_fast_setting,
    load_evaluator_config,
    classifier_environment,
    classify_with_codex,
    route_task,
)


def catalog():
    return ModelCatalog(models=dict(FALLBACK_MODELS), source="test")


def valid_payload(**overrides):
    payload = {
        "model": "gpt-5.6-luna",
        "effort": "low",
        "orchestration": "single",
        "task_type": "answer",
        "risk": "low",
        "parallelizable": False,
        "confidence": 0.95,
        "reason": "The task is bounded and deterministic.",
        "safety": {
            "external_write": False,
            "destructive": False,
            "money": False,
            "security": False,
            "legal_or_medical": False,
            "persisted_data": False,
            "public_deployment": False,
            "credentials": False,
        },
    }
    payload.update(overrides)
    return payload


class SuccessfulProcess:
    def __init__(self, command, payload, event_stdout, **kwargs):
        self.command = command
        self.payload = payload
        self.event_stdout = event_stdout
        self.returncode = 0
        self.pid = 4321
        self.output_path = Path(
            command[command.index("--output-last-message") + 1]
        )

    def communicate(self, input, timeout):
        self.output_path.write_text(json.dumps(self.payload), encoding="utf-8")
        return self.event_stdout, ""

    def poll(self):
        return self.returncode


class CodexEvaluatorTests(unittest.TestCase):
    def test_model_speed_policy_and_execution_commands(self):
        payload = {"model_speed": {"default": True, "sol": False, "astra": False}}
        self.config_path.write_text(json.dumps(payload))
        for model, expected in [("gpt-5.6-luna", True), ("gpt-5.6-terra", True),
                                ("gpt-5.6-sol", False), ("gpt-6-astra", False),
                                ("gpt-5.3-codex-spark", True)]:
            self.assertEqual(model_fast_setting(model, payload), expected)
            decision = route_task("Reply hello", catalog(), heuristic_only=True, use_feedback=False)
            decision.model = model
            for resume in (None, "test-session"):
                command = build_codex_command("codex", decision, resume=resume)
                self.assertIn('service_tier="{0}"'.format("fast" if expected else "default"), command)
                index = command.index("fast_mode")
                self.assertEqual(command[index - 1], "--enable" if expected else "--disable")
        self.assertTrue(load_evaluator_config().fast)
        self.config_path.write_text("{")
        command = build_codex_command("codex", decision)
        self.assertIn('service_tier="default"', command)
        payload["model_speed"]["gpt-5.6-sol"] = True
        self.assertTrue(model_fast_setting("gpt-5.6-sol", payload))
        self.assertIsNone(model_fast_setting("gpt-5.6-sol", {}))
        with self.assertRaises(ValueError):
            model_fast_setting("gpt-5.6-sol", {"model_speed": {"sol": "false"}})

    def test_skill_budget_notice_is_not_a_disabled_tool(self):
        notice = {"type": "item.completed", "item": {
            "type": "error", "message": "Skill descriptions were shortened to fit the skills context budget. Codex can still see every skill."
        }}
        completed = {"type": "turn.completed", "usage": {"input_tokens": 10}}
        self.assertEqual(
            _evaluator_observability(json.dumps(notice) + "\n" + json.dumps(completed)),
            (None, {"input_tokens": 10}),
        )
        for item in [
            {"type": "error", "message": "Unexpected evaluator failure"},
            {"type": "command_execution", "command": "pwd"},
        ]:
            with self.assertRaises(RuntimeError):
                _evaluator_observability(json.dumps({"type": "item.completed", "item": item}))


    def setUp(self):
        self.config_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.config_dir.cleanup)
        self.config_path = Path(self.config_dir.name) / "config.json"
        config_environment = patch.dict(
            os.environ,
            {CODEX_MODEL_ROUTER_CONFIG: str(self.config_path)},
        )
        config_environment.start()
        self.addCleanup(config_environment.stop)

    def test_classifier_command_uses_default_isolated_spark_evaluator(self):
        command = build_classifier_command(
            "codex", CODEX_EVALUATOR_MODEL, "empty", "schema.json", "output.json"
        )
        joined = " ".join(command)
        self.assertIn(CODEX_EVALUATOR_MODEL, command)
        self.assertIn('model_reasoning_effort="low"', command)
        self.assertEqual(CODEX_EVALUATOR_MODEL, "gpt-5.3-codex-spark")
        self.assertIn('service_tier="default"', command)
        self.assertNotIn("--enable fast_mode", joined)
        self.assertIn("--disable fast_mode", joined)
        for feature in (
            "shell_tool",
            "multi_agent",
            "hooks",
            "apps",
            "plugins",
            "browser_use",
            "computer_use",
            "image_generation",
            "memories",
            "skill_search",
        ):
            self.assertIn("--disable {0}".format(feature), joined)
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--strict-config", command)
        self.assertIn('forced_login_method="chatgpt"', command)

    def test_classifier_command_applies_configured_effort_and_fast(self):
        command = build_classifier_command(
            "codex",
            "gpt-5.6-terra",
            "empty",
            "schema.json",
            "output.json",
            evaluator_reasoning_effort="high",
            evaluator_fast=True,
        )
        joined = " ".join(command)
        self.assertIn("gpt-5.6-terra", command)
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertIn('service_tier="fast"', command)
        self.assertIn("--enable fast_mode", joined)
        self.assertNotIn("--disable fast_mode", joined)

    def test_classifier_environment_sets_recursion_guard_and_strips_keys(self):
        environment = classifier_environment(
            {"PATH": "safe", "OPENAI_API_KEY": "secret", "CODEX_HOME": "auth"}
        )
        self.assertEqual(environment[CODEX_EVALUATOR_GUARD], "1")
        self.assertEqual(environment["CODEX_HOME"], "auth")
        self.assertNotIn("OPENAI_API_KEY", environment)

    def test_success_records_thread_usage_latency_and_reason(self):
        stdout = "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "{}"},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 120, "output_tokens": 40},
                    }
                ),
            )
        )

        def process_factory(command, **kwargs):
            return SuccessfulProcess(command, valid_payload(), stdout, **kwargs)

        with patch("codex_model_router.router.subprocess.Popen", side_effect=process_factory):
            selected = classify_with_codex(
                "Compute 2+2", catalog(), "agent", codex_executable="codex"
            )
        self.assertEqual((selected.model, selected.effort), ("gpt-5.6-luna", "low"))
        self.assertEqual(selected.source, "codex-evaluator")
        self.assertEqual(selected.evaluator_model, CODEX_EVALUATOR_MODEL)
        self.assertEqual(selected.evaluator_reasoning_effort, CODEX_EVALUATOR_EFFORT)
        self.assertEqual(selected.evaluator_fast, CODEX_EVALUATOR_FAST)
        self.assertEqual(selected.evaluator_thread_id, "thread-1")
        self.assertEqual(selected.evaluator_usage["input_tokens"], 120)
        self.assertEqual(selected.evaluator_reason, valid_payload()["reason"])
        self.assertGreaterEqual(selected.classifier_ms, 0)

    def test_direct_classify_reloads_config_between_calls(self):
        stdout = json.dumps({"type": "thread.started", "thread_id": "thread-1"})
        commands = []

        def process_factory(command, **kwargs):
            commands.append(command)
            return SuccessfulProcess(command, valid_payload(), stdout, **kwargs)

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            with patch.dict(os.environ, {CODEX_MODEL_ROUTER_CONFIG: str(config_path)}), patch(
                "codex_model_router.router.subprocess.Popen", side_effect=process_factory
            ):
                config_path.write_text(
                    json.dumps({"evaluator": {"model": "gpt-5.6-terra", "reasoning_effort": "high", "fast": True}}),
                    encoding="utf-8",
                )
                first = classify_with_codex(
                    "Classify this", catalog(), "agent", codex_executable="codex"
                )
                config_path.write_text(
                    json.dumps({"evaluator": {"model": "gpt-5.6-sol", "reasoning_effort": "medium", "fast": False}}),
                    encoding="utf-8",
                )
                second = classify_with_codex(
                    "Classify this", catalog(), "agent", codex_executable="codex"
                )
        self.assertEqual((first.evaluator_model, first.evaluator_reasoning_effort, first.evaluator_fast), ("gpt-5.6-terra", "high", True))
        self.assertEqual((second.evaluator_model, second.evaluator_reasoning_effort, second.evaluator_fast), ("gpt-5.6-sol", "medium", False))
        self.assertIn('service_tier="fast"', commands[0])
        self.assertIn("--enable", commands[0])
        self.assertIn('service_tier="default"', commands[1])
        self.assertIn("--disable", commands[1])

    def test_malformed_config_falls_back_with_default_evaluator_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text('{"evaluator": {"fast": "true"}}', encoding="utf-8")
            with patch.dict(os.environ, {CODEX_MODEL_ROUTER_CONFIG: str(config_path)}), patch(
                "codex_model_router.router.classify_with_codex"
            ) as evaluator:
                decision = route_task("Implement a helper", catalog=catalog())
        evaluator.assert_not_called()
        self.assertEqual(decision.source, "codex-evaluator-fallback")
        self.assertEqual(decision.evaluator_model, CODEX_EVALUATOR_MODEL)
        self.assertEqual(decision.evaluator_reasoning_effort, CODEX_EVALUATOR_EFFORT)
        self.assertEqual(decision.evaluator_fast, CODEX_EVALUATOR_FAST)
        self.assertIn("config fast must be boolean", decision.evaluator_error)

    def test_strict_payload_rejects_string_boolean_and_nonfinite_confidence(self):
        malformed = valid_payload(parallelizable="false")
        with self.assertRaises(TypeError):
            _choice_from_payload(malformed, 1)
        malformed = valid_payload(confidence=float("nan"))
        with self.assertRaises(ValueError):
            _choice_from_payload(malformed, 1)

    def test_unknown_model_is_rejected_even_after_schema_output(self):
        def process_factory(command, **kwargs):
            return SuccessfulProcess(
                command, valid_payload(model="gpt-unknown"), "", **kwargs
            )

        with patch("codex_model_router.router.subprocess.Popen", side_effect=process_factory):
            with self.assertRaisesRegex(ValueError, "unavailable model"):
                classify_with_codex(
                    "Classify this", catalog(), "agent", codex_executable="codex"
                )

    def test_tool_event_is_rejected(self):
        stdout = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "pwd"},
            }
        )

        def process_factory(command, **kwargs):
            return SuccessfulProcess(command, valid_payload(), stdout, **kwargs)

        with patch("codex_model_router.router.subprocess.Popen", side_effect=process_factory):
            with self.assertRaisesRegex(RuntimeError, "disabled tool item"):
                classify_with_codex(
                    "Classify this", catalog(), "agent", codex_executable="codex"
                )

    def test_timeout_and_interrupt_clean_up_process_group(self):
        class InterruptedProcess:
            returncode = None
            pid = 4321

            def __init__(self, exception):
                self.exception = exception

            def communicate(self, input, timeout):
                raise self.exception

        for exception, expected in (
            (subprocess.TimeoutExpired("codex", 1), RuntimeError),
            (KeyboardInterrupt(), KeyboardInterrupt),
        ):
            process = InterruptedProcess(exception)
            with self.subTest(exception=type(exception).__name__), patch(
                "codex_model_router.router.subprocess.Popen", return_value=process
            ), patch("codex_model_router.router._terminate_classifier_process") as terminate:
                with self.assertRaises(expected):
                    classify_with_codex(
                        "Classify this",
                        catalog(),
                        "agent",
                        codex_executable="codex",
                        timeout_seconds=1,
                    )
                terminate.assert_called_once_with(process)

    def test_route_task_uses_codex_evaluator(self):
        selected = RouteChoice(
            model="gpt-5.6-luna",
            effort="low",
            orchestration="single",
            task_type="answer",
            risk="low",
            parallelizable=False,
            confidence=0.9,
            reason="bounded",
            safety={},
            source="codex-evaluator",
            evaluator_model=CODEX_EVALUATOR_MODEL,
            evaluator_reason="bounded",
        )
        with patch(
            "codex_model_router.router.classify_with_codex", return_value=selected
        ) as evaluator:
            decision = route_task("Compute 2+2", catalog=catalog())
        evaluator.assert_called_once()
        self.assertEqual(decision.source, "codex-evaluator")

    def test_failure_falls_back_to_terra_medium_with_latency(self):
        with patch(
            "codex_model_router.router.classify_with_codex",
            side_effect=RuntimeError("login unavailable"),
        ), patch(
            "codex_model_router.router.time.perf_counter", side_effect=(10.0, 10.125)
        ):
            decision = route_task("Implement a helper", catalog=catalog())
        self.assertEqual((decision.model, decision.effort), ("gpt-5.6-terra", "medium"))
        self.assertEqual(decision.source, "codex-evaluator-fallback")
        self.assertEqual(decision.classifier_ms, 125)
        self.assertIn("login unavailable", decision.evaluator_error)

    def test_evaluator_log_keeps_observability_without_removed_backend_fields(self):
        choice = RouteChoice(
            model="gpt-5.6-luna",
            effort="low",
            orchestration="single",
            task_type="answer",
            risk="low",
            parallelizable=False,
            confidence=0.9,
            reason="bounded",
            safety={},
            source="codex-evaluator",
            classifier_ms=123,
            evaluator_model=CODEX_EVALUATOR_MODEL,
            evaluator_reasoning_effort=CODEX_EVALUATOR_EFFORT,
            evaluator_fast=CODEX_EVALUATOR_FAST,
            evaluator_reason="private-token-test-123",
            evaluator_thread_id="thread-1",
            evaluator_usage={"input_tokens": 120, "output_tokens": 40},
        )
        decision = apply_policy(choice, "Compute 2+2", catalog())
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "decisions.jsonl"
            append_decision_log("Compute 2+2", decision, "route_only", log_path=path)
            record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["evaluator"]["model"], CODEX_EVALUATOR_MODEL)
        self.assertEqual(record["evaluator"]["reasoning_effort"], CODEX_EVALUATOR_EFFORT)
        self.assertFalse(record["evaluator"]["fast"])
        self.assertEqual(record["evaluator"]["request_ms"], 123)
        self.assertEqual(record["evaluator"]["summary"], "type=answer; risk=low; confidence=0.90")
        self.assertNotIn("reason", record["evaluator"])
        self.assertNotIn("evaluator_reason", record["route"])
        self.assertNotIn("private-token-test-123", json.dumps(record))
        self.assertEqual(record["evaluator"]["thread_id"], "thread-1")
        self.assertEqual(record["evaluator"]["usage"]["input_tokens"], 120)
        self.assertNotIn("notdiamond", record)
        self.assertNotIn("cache", record)
        self.assertFalse(any(key.startswith("nd_") for key in record["route"]))

    def test_cli_rejects_removed_backend_option(self):
        parser = cli_module.build_parser()
        parsed = parser.parse_args(["task"])
        self.assertFalse(hasattr(parsed, "backend"))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["--backend", "notdiamond", "task"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
