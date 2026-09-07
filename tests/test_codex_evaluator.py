import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stderr
from unittest.mock import patch

from codex_model_router import cli as cli_module
from codex_model_router.router import (
    CODEX_EVALUATOR_GUARD,
    CODEX_EVALUATOR_MODEL,
    FALLBACK_MODELS,
    ModelCatalog,
    RouteChoice,
    _choice_from_payload,
    append_decision_log,
    apply_policy,
    build_classifier_command,
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
    def test_classifier_command_uses_fixed_isolated_spark_evaluator(self):
        command = build_classifier_command(
            "codex", CODEX_EVALUATOR_MODEL, "empty", "schema.json", "output.json"
        )
        joined = " ".join(command)
        self.assertIn(CODEX_EVALUATOR_MODEL, command)
        self.assertIn('model_reasoning_effort="low"', command)
        self.assertEqual(CODEX_EVALUATOR_MODEL, "gpt-5.3-codex-spark")
        self.assertNotIn('service_tier="fast"', command)
        self.assertNotIn("--enable fast_mode", joined)
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
        self.assertEqual(selected.evaluator_thread_id, "thread-1")
        self.assertEqual(selected.evaluator_usage["input_tokens"], 120)
        self.assertEqual(selected.evaluator_reason, valid_payload()["reason"])
        self.assertGreaterEqual(selected.classifier_ms, 0)

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
