import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_model_router.router import (
    FALLBACK_MODELS,
    ModelCatalog,
    RouteChoice,
    RoutingDecision,
    _catalog_from_payload,
    append_decision_log,
    append_feedback,
    apply_policy,
    build_classifier_command,
    build_codex_command,
    calibrate_with_feedback,
    classifier_environment,
    classify_heuristically,
    discover_catalog,
    dispatch_codex,
    hook_updated_input,
    run_hook,
)
from codex_model_router.user_hook import build_user_hook_group, install_user_hook


def catalog():
    return ModelCatalog(models=dict(FALLBACK_MODELS), source="test")


def choice(**overrides):
    values = {
        "model": "gpt-5.6-luna",
        "effort": "low",
        "orchestration": "single",
        "task_type": "answer",
        "risk": "low",
        "parallelizable": False,
        "confidence": 0.9,
        "reason": "test",
        "safety": {},
        "source": "test",
    }
    values.update(overrides)
    return RouteChoice(**values)


class UserHookInstallerTests(unittest.TestCase):
    def test_installer_preserves_existing_hooks_and_creates_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "hooks.json"
            original = {
                "hooks": {
                    "PostToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": "review"}]}
                    ]
                }
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            target, installed, backup = install_user_hook(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            backup_exists = backup is not None and backup.exists()
            backup_payload = (
                json.loads(backup.read_text(encoding="utf-8"))
                if backup is not None
                else None
            )

        self.assertEqual(target, path)
        self.assertTrue(installed)
        self.assertIsNotNone(backup)
        self.assertTrue(backup_exists)
        self.assertEqual(backup_payload, original)
        self.assertEqual(payload["hooks"]["PostToolUse"], original["hooks"]["PostToolUse"])
        self.assertIn(build_user_hook_group(), payload["hooks"]["PreToolUse"])

    def test_installer_pins_the_active_python_interpreter(self):
        group = build_user_hook_group()
        handler = group["hooks"][0]
        active_command = (
            handler["commandWindows"] if os.name == "nt" else handler["command"]
        )
        if os.name == "nt":
            encoded_script = active_command.rsplit(" ", 1)[1]
            active_script = base64.b64decode(encoded_script).decode("utf-16-le")
        else:
            active_script = active_command
        self.assertIn(os.path.abspath(sys.executable), active_script)
        self.assertIn(" -I ", active_script)

    def test_windows_hook_handles_spaces_and_apostrophes_in_python_path(self):
        group = build_user_hook_group(
            python_executable=r"C:\Program Files\O'Brien Python\python.exe",
            platform_name="nt",
            windows_directory=r"C:\Windows",
        )
        command = group["hooks"][0]["commandWindows"]
        self.assertTrue(
            command.startswith(
                r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe "
            )
        )
        encoded_script = command.rsplit(" ", 1)[1]
        script = base64.b64decode(encoded_script).decode("utf-16-le")
        self.assertIn(
            r"& 'C:\Program Files\O''Brien Python\python.exe' -I "
            r"-m codex_model_router --hook",
            script,
        )

    def test_windows_hook_rejects_shell_unsafe_system_directory(self):
        with self.assertRaises(ValueError):
            build_user_hook_group(
                python_executable=r"C:\Python\python.exe",
                platform_name="nt",
                windows_directory=r"C:\Unsafe Windows",
            )

    def test_installer_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "hooks.json"
            install_user_hook(path)
            before = path.read_text(encoding="utf-8")
            _, installed, backup = install_user_hook(path)
            after = path.read_text(encoding="utf-8")

        self.assertFalse(installed)
        self.assertIsNone(backup)
        self.assertEqual(after, before)

    def test_invalid_existing_json_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "hooks.json"
            path.write_text("not-json", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                install_user_hook(path)
            content = path.read_text(encoding="utf-8")

        self.assertEqual(content, "not-json")


class CatalogTests(unittest.TestCase):
    def test_catalog_parses_supported_efforts(self):
        parsed = _catalog_from_payload(
            {
                "models": [
                    {
                        "slug": "gpt-5.6-luna",
                        "supported_reasoning_levels": [{"effort": "low"}],
                    },
                    {
                        "slug": "gpt-5.6-terra",
                        "supported_reasoning_levels": [{"effort": "medium"}],
                    },
                    {
                        "slug": "gpt-5.6-sol",
                        "supported_reasoning_levels": [
                            {"effort": "low"},
                            {"effort": "ultra"},
                        ],
                    },
                    {
                        "slug": "unrelated-model",
                        "supported_reasoning_levels": [{"effort": "low"}],
                    },
                ]
            }
        )
        self.assertEqual(parsed.models["gpt-5.6-sol"], ("low", "ultra"))
        self.assertNotIn("unrelated-model", parsed.models)

    def test_fresh_catalog_cache_avoids_requerying_cli(self):
        payload = {
            "models": [
                {
                    "slug": "gpt-5.6-luna",
                    "supported_reasoning_levels": [{"effort": "low"}],
                },
                {
                    "slug": "gpt-5.6-terra",
                    "supported_reasoning_levels": [{"effort": "medium"}],
                },
                {
                    "slug": "gpt-5.6-sol",
                    "supported_reasoning_levels": [{"effort": "high"}],
                },
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "catalog.json"
            with patch("codex_model_router.router.subprocess.run") as runner:
                runner.return_value.returncode = 0
                runner.return_value.stdout = json.dumps(payload)
                first = discover_catalog("codex", cache_path=cache_path)
            with patch("codex_model_router.router.subprocess.run") as runner:
                second = discover_catalog("codex", cache_path=cache_path)
                runner.assert_not_called()
        self.assertEqual(first.source, "codex-cli")
        self.assertEqual(second.source, "codex-cli-cache")

    def test_incomplete_catalog_is_rejected(self):
        with self.assertRaises(ValueError):
            _catalog_from_payload(
                {
                    "models": [
                        {
                            "slug": "gpt-5.6-luna",
                            "supported_reasoning_levels": [{"effort": "low"}],
                        }
                    ]
                }
            )

    def test_fresh_incomplete_cache_falls_through_to_cli(self):
        complete_payload = {
            "models": [
                {
                    "slug": "gpt-5.6-luna",
                    "supported_reasoning_levels": [{"effort": "low"}],
                },
                {
                    "slug": "gpt-5.6-terra",
                    "supported_reasoning_levels": [{"effort": "medium"}],
                },
                {
                    "slug": "gpt-5.6-sol",
                    "supported_reasoning_levels": [{"effort": "high"}],
                },
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "catalog.json"
            cache_path.write_text(
                json.dumps({"models": {"gpt-5.6-luna": ["low"]}}),
                encoding="utf-8",
            )
            with patch("codex_model_router.router.subprocess.run") as runner:
                runner.return_value.returncode = 0
                runner.return_value.stdout = json.dumps(complete_payload)
                discovered = discover_catalog("codex", cache_path=cache_path)
                runner.assert_called_once()
        self.assertEqual(discovered.source, "codex-cli")


class PolicyTests(unittest.TestCase):
    def test_low_risk_formatting_stays_luna_low(self):
        decision = apply_policy(
            choice(task_type="explain"),
            "Format this sentence as a title",
            catalog(),
        )
        self.assertEqual((decision.model, decision.effort), ("gpt-5.6-luna", "low"))

    def test_luna_implementation_is_raised_to_terra_medium(self):
        decision = apply_policy(
            choice(task_type="implement"),
            "Implement a small local parser helper",
            catalog(),
        )
        self.assertEqual((decision.model, decision.effort), ("gpt-5.6-terra", "medium"))

    def test_public_deploy_is_sol_high_single(self):
        decision = apply_policy(
            choice(
                orchestration="multi_agent",
                parallelizable=True,
                task_type="deploy",
            ),
            "Deploy the public service to production",
            catalog(),
        )
        self.assertEqual(decision.model, "gpt-5.6-sol")
        self.assertGreaterEqual(
            ("low", "medium", "high", "xhigh", "max", "ultra").index(decision.effort),
            2,
        )
        self.assertEqual(decision.orchestration, "single")

    def test_external_calendar_write_is_sol_high_single(self):
        decision = apply_policy(
            choice(task_type="external_action"),
            "Create a Google Calendar event for tomorrow",
            catalog(),
        )
        self.assertEqual((decision.model, decision.effort), ("gpt-5.6-sol", "high"))
        self.assertTrue(decision.safety["external_write"])
        self.assertEqual(decision.orchestration, "single")

    def test_safe_parallel_root_enables_ultra(self):
        decision = apply_policy(
            choice(
                model="gpt-5.6-sol",
                effort="xhigh",
                orchestration="multi_agent",
                task_type="review",
                parallelizable=True,
            ),
            "Review several independent local modules in parallel",
            catalog(),
        )
        self.assertEqual(decision.effort, "ultra")
        self.assertEqual(decision.orchestration, "multi_agent")

    def test_spawned_agent_ultra_is_downgraded(self):
        decision = apply_policy(
            choice(
                model="gpt-5.6-sol",
                effort="ultra",
                orchestration="multi_agent",
                task_type="research",
                parallelizable=True,
            ),
            "Research two independent implementation options",
            catalog(),
            surface="agent",
        )
        self.assertEqual(decision.effort, "xhigh")
        self.assertEqual(decision.orchestration, "single")

    def test_ambiguous_task_cannot_auto_delegate(self):
        decision = apply_policy(
            choice(
                model="gpt-5.6-sol",
                effort="ultra",
                orchestration="multi_agent",
                parallelizable=True,
            ),
            "do it all",
            catalog(),
        )
        self.assertEqual(decision.orchestration, "single")
        self.assertNotEqual(decision.effort, "ultra")

    def test_clear_three_word_task_is_not_treated_as_ambiguous(self):
        decision = apply_policy(
            choice(task_type="explain"),
            "Summarize these notes",
            catalog(),
        )
        self.assertEqual((decision.model, decision.effort), ("gpt-5.6-luna", "low"))

    def test_heuristic_routes_simple_summary_to_luna(self):
        selected = classify_heuristically(
            "Summarize this paragraph in three bullets",
            catalog(),
        )
        self.assertEqual((selected.model, selected.effort), ("gpt-5.6-luna", "low"))

    def test_informational_risk_words_do_not_trigger_action_floor(self):
        decision = apply_policy(
            choice(task_type="explain"),
            "Summarize this invoice and explain the production OAuth incident",
            catalog(),
        )
        self.assertEqual((decision.model, decision.effort), ("gpt-5.6-luna", "low"))
        self.assertFalse(any(decision.safety.values()))

        heuristic = classify_heuristically(
            "Summarize this invoice and explain the production OAuth incident",
            catalog(),
        )
        self.assertEqual(heuristic.task_type, "explain")


class CommandTests(unittest.TestCase):
    def test_classifier_is_isolated_and_uses_chatgpt_login(self):
        command = build_classifier_command(
            "codex",
            "gpt-5.6-luna",
            "empty-dir",
            "schema.json",
            "decision.json",
        )
        joined = " ".join(command)
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--sandbox read-only", joined)
        self.assertIn("--disable shell_tool", joined)
        self.assertIn("--disable multi_agent", joined)
        self.assertIn('forced_login_method="chatgpt"', command)

    def test_classifier_environment_strips_api_credentials(self):
        result = classifier_environment(
            {
                "PATH": "safe",
                "OPENAI_API_KEY": "secret",
                "codex_access_token": "secret-too",
                "CODEX_HOME": "keep-this",
            }
        )
        self.assertEqual(result["PATH"], "safe")
        self.assertEqual(result["CODEX_HOME"], "keep-this")
        self.assertNotIn("OPENAI_API_KEY", result)
        self.assertNotIn("codex_access_token", result)

    def test_real_dispatch_preserves_normal_policy_by_default(self):
        decision = RoutingDecision(
            decision_id="id",
            model="gpt-5.6-terra",
            effort="medium",
            orchestration="single",
            task_type="implement",
            risk="low",
            parallelizable=False,
            confidence=0.9,
            reason="test",
            source="test",
            surface="root",
            catalog_source="test",
            classifier_ms=None,
            safety={},
        )
        command = build_codex_command("codex", decision, cwd="repo")
        self.assertEqual(command[-1], "-")
        self.assertIn(str(Path("repo").resolve()), command)
        self.assertNotIn("--sandbox", command)
        self.assertNotIn("--ignore-user-config", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_explicit_sandbox_is_forwarded(self):
        decision = apply_policy(
            choice(task_type="explain"),
            "Explain these four lines of code",
            catalog(),
        )
        command = build_codex_command(
            "codex",
            decision,
            cwd="repo",
            sandbox="workspace-write",
        )
        self.assertIn("--sandbox", command)
        self.assertIn("workspace-write", command)

    def test_resume_last_runs_in_requested_working_directory(self):
        decision = apply_policy(
            choice(task_type="explain"),
            "Continue the analysis with one more example",
            catalog(),
        )
        with patch("codex_model_router.router.subprocess.run") as runner:
            runner.return_value.returncode = 0
            exit_code, _ = dispatch_codex(
                "Continue the analysis with one more example",
                decision,
                codex_executable="codex",
                cwd="target-repository",
                resume_last=True,
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            runner.call_args.kwargs["cwd"],
            str(Path("target-repository").resolve()),
        )


class HookAndLogTests(unittest.TestCase):
    def test_hook_preserves_all_args_when_adding_route(self):
        decision = apply_policy(
            choice(
                model="gpt-5.6-sol",
                effort="high",
                task_type="research",
            ),
            "Research the local API",
            catalog(),
            surface="agent",
        )
        payload = {
            "tool_input": {
                "task_name": "research",
                "message": "Research the local API",
            }
        }
        updated = hook_updated_input(payload, decision)
        self.assertEqual(updated["task_name"], "research")
        self.assertEqual(updated["message"], "Research the local API")
        self.assertEqual(updated["model"], decision.model)
        self.assertEqual(updated["reasoning_effort"], decision.effort)

    def test_hook_noops_when_parent_pins_both_fields(self):
        decision = apply_policy(
            choice(),
            "Summarize this text",
            catalog(),
            surface="agent",
        )
        payload = {
            "tool_input": {
                "message": "Summarize this text",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "low",
            }
        }
        self.assertIsNone(hook_updated_input(payload, decision))

    def test_either_partial_pin_opts_agent_out_of_routing(self):
        decision = apply_policy(
            choice(model="gpt-5.6-sol", effort="ultra", task_type="research"),
            "Research several independent options",
            catalog(),
            surface="agent",
        )
        for pinned in (
            {"model": "custom-model"},
            {"reasoning_effort": "ultra"},
        ):
            payload = {
                "tool_input": dict(
                    {"message": "Research several independent options"},
                    **pinned,
                )
            }
            with self.subTest(pinned=pinned):
                self.assertIsNone(hook_updated_input(payload, decision))

    def test_pinned_agent_skips_catalog_and_classifier(self):
        payload = {
            "tool_name": "Agent",
            "tool_input": {
                "message": "Research several independent options",
                "model": "gpt-5.6-sol",
            },
        }
        with patch("codex_model_router.router.discover_catalog") as discover:
            self.assertIsNone(run_hook(payload, heuristic_only=False, no_log=True))
        discover.assert_not_called()

    def test_log_has_hash_and_features_but_not_raw_prompt(self):
        raw_prompt = "private unique prompt that must not be logged"
        decision = apply_policy(
            choice(task_type="explain", reason=raw_prompt),
            raw_prompt,
            catalog(),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "decisions.jsonl"
            append_decision_log(
                raw_prompt,
                decision,
                outcome="route_only",
                log_path=path,
                cwd=temp_dir,
            )
            content = path.read_text(encoding="utf-8")
            payload = json.loads(content)
        self.assertNotIn(raw_prompt, content)
        self.assertNotIn("reason", payload["route"])
        self.assertNotIn("cwd_name", payload)
        self.assertEqual(len(payload["task_sha256"]), 64)
        self.assertEqual(payload["features"]["words"], 8)

    def test_underpowered_feedback_upgrades_the_same_task(self):
        task = "Summarize this paragraph into three bullets"
        prior = apply_policy(
            choice(task_type="explain"),
            task,
            catalog(),
        )
        current = apply_policy(
            choice(task_type="explain"),
            task,
            catalog(),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "decisions.jsonl"
            append_decision_log(task, prior, "completed", log_path=path, cwd=temp_dir)
            append_feedback(prior.decision_id, "underpowered", log_path=path)
            calibrated = calibrate_with_feedback(
                task,
                current,
                catalog(),
                log_path=path,
                cwd=temp_dir,
            )
        self.assertEqual((calibrated.model, calibrated.effort), ("gpt-5.6-terra", "medium"))
        self.assertTrue(any("personal feedback applied" in item for item in calibrated.overrides))

    def test_similar_task_majority_uses_a_matching_feedback_route(self):
        examples = (
            ("Summarize the first short memo", choice(task_type="explain"), "underpowered"),
            ("Summarize the second short memo", choice(task_type="explain"), "underpowered"),
            (
                "Summarize the third short memo",
                choice(model="gpt-5.6-sol", effort="high", task_type="explain"),
                "overkill",
            ),
        )
        current_task = "Summarize the fourth short memo"
        current = apply_policy(choice(task_type="explain"), current_task, catalog())
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "decisions.jsonl"
            for task, selected_choice, rating in examples:
                prior = apply_policy(selected_choice, task, catalog())
                append_decision_log(task, prior, "completed", log_path=path, cwd=temp_dir)
                append_feedback(prior.decision_id, rating, log_path=path)
            calibrated = calibrate_with_feedback(
                current_task,
                current,
                catalog(),
                log_path=path,
                cwd=temp_dir,
            )
        self.assertEqual((calibrated.model, calibrated.effort), ("gpt-5.6-terra", "medium"))

    def test_feedback_never_reverses_direction_from_fresh_route(self):
        task = "Summarize this stable routing prompt"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "decisions.jsonl"

            historical_low = apply_policy(choice(task_type="explain"), task, catalog())
            append_decision_log(task, historical_low, "completed", log_path=path, cwd=temp_dir)
            append_feedback(historical_low.decision_id, "underpowered", log_path=path)
            fresh_high = apply_policy(
                choice(model="gpt-5.6-sol", effort="high", task_type="explain"),
                task,
                catalog(),
            )
            calibrated_high = calibrate_with_feedback(
                task,
                fresh_high,
                catalog(),
                log_path=path,
                cwd=temp_dir,
            )
            self.assertEqual(
                (calibrated_high.model, calibrated_high.effort),
                (fresh_high.model, fresh_high.effort),
            )

            second_path = Path(temp_dir) / "overkill.jsonl"
            historical_high = apply_policy(
                choice(model="gpt-5.6-sol", effort="high", task_type="explain"),
                task,
                catalog(),
            )
            append_decision_log(task, historical_high, "completed", log_path=second_path, cwd=temp_dir)
            append_feedback(historical_high.decision_id, "overkill", log_path=second_path)
            fresh_low = apply_policy(choice(task_type="explain"), task, catalog())
            calibrated_low = calibrate_with_feedback(
                task,
                fresh_low,
                catalog(),
                log_path=second_path,
                cwd=temp_dir,
            )
            self.assertEqual(
                (calibrated_low.model, calibrated_low.effort),
                (fresh_low.model, fresh_low.effort),
            )

    def test_feedback_is_scoped_to_repository_and_surface(self):
        task = "Summarize the current changes"
        prior = apply_policy(choice(task_type="explain"), task, catalog())
        current = apply_policy(choice(task_type="explain"), task, catalog())
        with tempfile.TemporaryDirectory() as temp_dir:
            first_repo = str(Path(temp_dir) / "first")
            second_repo = str(Path(temp_dir) / "second")
            path = Path(temp_dir) / "decisions.jsonl"
            append_decision_log(task, prior, "completed", log_path=path, cwd=first_repo)
            append_feedback(prior.decision_id, "underpowered", log_path=path)
            other_repo = calibrate_with_feedback(
                task,
                current,
                catalog(),
                log_path=path,
                cwd=second_repo,
            )
            agent_decision = apply_policy(
                choice(task_type="explain"),
                task,
                catalog(),
                surface="agent",
            )
            other_surface = calibrate_with_feedback(
                task,
                agent_decision,
                catalog(),
                log_path=path,
                cwd=first_repo,
            )
        self.assertEqual((other_repo.model, other_repo.effort), (current.model, current.effort))
        self.assertEqual(
            (other_surface.model, other_surface.effort),
            (agent_decision.model, agent_decision.effort),
        )


if __name__ == "__main__":
    unittest.main()
