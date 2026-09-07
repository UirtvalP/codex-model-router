import base64
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from codex_model_router.router import (
    FALLBACK_MODELS,
    PRESERVE_MODEL_MARKER,
    ModelCatalog,
    RouteChoice,
    RoutingDecision,
    _catalog_from_payload,
    _proxy_to_choice,
    append_decision_log,
    append_feedback,
    apply_policy,
    build_classifier_command,
    build_codex_command,
    calibrate_with_feedback,
    classifier_environment,
    classify_heuristically,
    classify_with_notdiamond,
    discover_catalog,
    dispatch_codex,
    hook_updated_input,
    normalize_spawn_task_name,
    run_hook,
    route_task,
)
from codex_model_router.user_hook import build_user_hook_group, install_user_hook
from codex_model_router.dashboard import DASHBOARD_HTML, build_dashboard_payload
from codex_model_router import cli as cli_module


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


class DashboardTests(unittest.TestCase):
    def test_dashboard_payload_aggregates_routes_and_feedback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "decisions.jsonl"
            events = [
                {
                    "event": "route_decision",
                    "timestamp": "2099-01-01T00:00:00+00:00",
                    "decision_id": "decision-a",
                    "task_summary": "4 words; type=answer; risk=low",
                    "notdiamond": {"proxy_model": "claude-haiku-4-5", "session_id": "session-a", "request_ms": 120},
                    "codex": {"model": "gpt-5.6-luna", "reasoning_effort": "low", "surface": "agent"},
                    "cache": {"hit": False, "sample_count": 0},
                    "fallback": {"used": False, "error": None},
                    "route": {"source": "notdiamond"},
                    "outcome": "agent_hook",
                },
                {
                    "event": "route_decision",
                    "timestamp": "2099-01-01T00:01:00+00:00",
                    "decision_id": "decision-b",
                    "task_name": "Cached test run",
                    "task_summary": "3 words; type=implement; risk=low",
                    "notdiamond": {"proxy_model": "claude-sonnet-4.6", "session_id": None, "request_ms": 80},
                    "codex": {"model": "gpt-5.6-terra", "reasoning_effort": "medium", "surface": "agent"},
                    "cache": {"hit": True, "sample_count": 5},
                    "fallback": {"used": True, "error": "timeout"},
                    "route": {"source": "notdiamond-fallback"},
                    "outcome": "agent_hook",
                },
                {"event": "route_feedback", "decision_id": "decision-b", "rating": "good"},
            ]
            lines = [json.dumps(events[0]), "not-json"] + [json.dumps(item) for item in events[1:]]
            path.write_text("\n".join(lines), encoding="utf-8")

            payload = build_dashboard_payload(path)

        self.assertEqual(payload["stats"]["total"], 2)
        self.assertEqual(payload["stats"]["cache_hits"], 1)
        self.assertEqual(payload["stats"]["fallbacks"], 1)
        self.assertEqual(payload["stats"]["avg_request_ms"], 100)
        self.assertEqual(payload["stats"]["models"], {"luna": 1, "terra": 1})
        self.assertEqual(payload["log"]["invalid_lines"], 1)
        self.assertEqual(payload["routes"][0]["task"], "Cached test run")
        self.assertEqual(payload["routes"][0]["rating"], "good")

    def test_dashboard_payload_handles_missing_log(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = build_dashboard_payload(Path(temp_dir) / "missing.jsonl")
        self.assertFalse(payload["log"]["exists"])
        self.assertEqual(payload["stats"]["total"], 0)
        self.assertEqual(payload["routes"], [])

    def test_dashboard_recognizes_legacy_fallback_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "decisions.jsonl"
            path.write_text(json.dumps({
                "event": "route_decision",
                "decision_id": "legacy-fallback",
                "codex": {"model": "gpt-5.6-terra", "reasoning_effort": "medium"},
                "route": {"source": "notdiamond-fallback", "nd_error": "timeout"},
            }), encoding="utf-8")
            payload = build_dashboard_payload(path)
        self.assertEqual(payload["stats"]["fallbacks"], 1)
        self.assertTrue(payload["routes"][0]["fallback_used"])
        self.assertEqual(payload["routes"][0]["fallback_error"], "timeout")

    def test_dashboard_is_self_contained_and_refreshes(self):
        self.assertIn("Codex 路由面板", DASHBOARD_HTML)
        self.assertIn("/api/routes?limit=1000", DASHBOARD_HTML)
        self.assertIn("setInterval(load,3000)", DASHBOARD_HTML)
        self.assertNotIn("https://", DASHBOARD_HTML)


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
        self.assertIn("multi_agent_v1__spawn_agent", group["matcher"])
        self.assertIn("functions\\.collaboration\\.spawn_agent", group["matcher"])
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
            r"& 'C:\Program Files\O''Brien Python\python.exe' -I -c",
            script,
        )
        self.assertIn("codex_model_router.cli", script)

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
    def test_spawn_task_name_is_normalized_before_hook_output(self):
        decision = apply_policy(choice(), "Review the local API", catalog(), surface="agent")
        payload = {
            "tool_input": {
                "task_name": "Route-Smoke Test",
                "message": "Review the local API",
            }
        }

        updated = hook_updated_input(payload, decision)

        self.assertEqual(updated["task_name"], "route_smoke_test")

    def test_spawn_task_name_falls_back_when_no_ascii_name_remains(self):
        self.assertEqual(normalize_spawn_task_name("路由测试"), "routed_task")

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

    def test_hook_overrides_parent_supplied_model_and_effort(self):
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
        updated = hook_updated_input(payload, decision)
        self.assertEqual(updated["model"], decision.model)
        self.assertEqual(updated["reasoning_effort"], decision.effort)

    def test_unmarked_parent_model_does_not_opt_agent_out_of_routing(self):
        decision = apply_policy(
            choice(model="gpt-5.6-sol", effort="ultra", task_type="research"),
            "Research several independent options",
            catalog(),
            surface="agent",
        )
        payload = {
            "tool_input": {
                "message": "Research several independent options",
                "model": "custom-model",
            }
        }
        updated = hook_updated_input(payload, decision)
        self.assertEqual(updated["model"], decision.model)
        self.assertEqual(updated["reasoning_effort"], decision.effort)

    def test_unmarked_parent_reasoning_effort_is_overridden_by_route(self):
        payload = {
            "tool_input": {
                "message": "Research several independent options",
                "reasoning_effort": "ultra",
            }
        }
        decision = apply_policy(choice(model="gpt-5.6-sol", effort="high"), payload["tool_input"]["message"], catalog(), surface="agent")
        updated = hook_updated_input(payload, decision)
        self.assertEqual(updated["model"], decision.model)
        self.assertEqual(updated["reasoning_effort"], decision.effort)

    def test_desktop_spawn_names_match_and_route(self):
        import re
        from codex_model_router.user_hook import build_user_hook_group
        matcher = build_user_hook_group()["matcher"]
        for name in ("collaborationspawn_agent", "collaboration.spawn_agent"):
            with self.subTest(name=name), patch("codex_model_router.router.discover_catalog", return_value=catalog()):
                self.assertIsNotNone(re.fullmatch(matcher, name))
                result = run_hook({"tool_name": name, "tool_input": {"message": "Compute 2+2"}}, heuristic_only=True, no_log=True)
                self.assertIn("model", result["hookSpecificOutput"]["updatedInput"])
        self.assertIsNone(re.fullmatch(matcher, "exec_command"))

    def test_macos_empty_ca_uses_system_bundle_and_respects_overrides(self):
        from unittest.mock import MagicMock
        cases = [("darwin", 0, {}, True), ("darwin", 2, {}, False),
                 ("linux", 0, {}, False), ("darwin", 0, {"SSL_CERT_FILE": "/custom.pem"}, False),
                 ("darwin", 0, {"SSL_CERT_DIR": "/custom"}, False)]
        for platform, roots, overrides, expected in cases:
            context = MagicMock()
            context.cert_store_stats.return_value = {"x509_ca": roots}
            response = MagicMock()
            response.__enter__.return_value.read.return_value = b'{"providers":[{"model":"claude-sonnet-4.6"}]}'
            with self.subTest(platform=platform, roots=roots, overrides=overrides), patch.dict(os.environ, {"NOTDIAMOND_API_KEY": "test", **overrides}, clear=True), patch("codex_model_router.router.sys.platform", platform), patch("codex_model_router.router.ssl.create_default_context", return_value=context), patch("codex_model_router.router.Path.is_file", return_value=True), patch("codex_model_router.router.urllib.request.urlopen", return_value=response) as request:
                classify_with_notdiamond("Compute 2+2", catalog(), "agent")
                self.assertEqual(context.load_verify_locations.called, expected)
                self.assertIs(request.call_args.kwargs["context"], context)
                if expected:
                    context.load_verify_locations.assert_called_once_with(cafile="/etc/ssl/cert.pem")

    def test_notdiamond_maps_four_claude_capability_proxies(self):
        from unittest.mock import Mock
        proxies = {
            "claude-haiku-4-5": "gpt-5.6-luna",
            "claude-sonnet-4.6": "gpt-5.6-terra",
            "claude-opus-4.7": "gpt-5.6-sol",
            "claude-sonnet-5": "gpt-6-astra",
        }
        for proxy, expected in proxies.items():
            response = Mock()
            response.__enter__ = lambda self: self
            response.__exit__ = lambda *args: None
            response.read.return_value = json.dumps({
                "providers": [{"provider": "openai", "model": proxy}],
                "session_id": "session-1",
            }).encode("utf-8")
            with self.subTest(proxy=proxy), patch.dict(os.environ, {"NOTDIAMOND_API_KEY": "test-key"}), patch("codex_model_router.router.urllib.request.urlopen", return_value=response):
                selected = classify_with_notdiamond("Implement this task", catalog(), "agent")
            self.assertEqual(selected.model, expected)
            self.assertEqual(selected.nd_proxy_model, proxy)
            self.assertEqual(selected.nd_session_id, "session-1")

    def test_opus_normally_maps_to_sol(self):
        task = "Review this implementation"
        decision = apply_policy(
            choice(
                model="gpt-5.6-sol",
                effort="high",
                task_type="review",
                source="notdiamond",
                nd_proxy_model="claude-opus-4.7",
            ),
            task,
            catalog(),
            surface="agent",
        )
        self.assertEqual(decision.model, "gpt-5.6-sol")

    def test_extreme_opus_task_still_maps_to_sol(self):
        task = "Analyze a difficult cross-service concurrency and consistency root cause"
        decision = apply_policy(
            choice(
                model="gpt-5.6-sol",
                effort="high",
                task_type="debug",
                source="notdiamond",
                nd_proxy_model="claude-opus-4.7",
            ),
            task,
            catalog(),
            surface="agent",
        )
        self.assertEqual((decision.model, decision.effort), ("gpt-5.6-sol", "high"))

    def test_fable_proxy_maps_to_astra_when_notdiamond_supports_it(self):
        selected = _proxy_to_choice(
            "claude-fable-5-1",
            "Analyze a difficult architecture problem",
            catalog(),
            12,
            "session-fable",
        )
        self.assertEqual((selected.model, selected.effort), ("gpt-6-astra", "high"))

    def test_notdiamond_failure_falls_back_to_terra_medium(self):
        with patch.dict(os.environ, {"NOTDIAMOND_API_KEY": "test-key"}), patch("codex_model_router.router.classify_with_notdiamond", side_effect=RuntimeError("timeout")):
            decision = route_task("Implement a small helper", catalog=catalog(), surface="agent", use_feedback=False, use_cache=False)
        self.assertEqual((decision.model, decision.effort), ("gpt-5.6-terra", "medium"))
        self.assertEqual(decision.source, "notdiamond-fallback")
        self.assertIn("timeout", decision.nd_error)

    def test_similarity_cache_requires_enough_successful_samples(self):
        task = "Run the parser unit tests"
        live_choice = choice(
            model="gpt-5.6-terra",
            effort="medium",
            task_type="answer",
            source="notdiamond",
            nd_proxy_model="claude-sonnet-4.6",
        )
        live_decision = apply_policy(live_choice, task, catalog(), surface="agent")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "decisions.jsonl"
            for _ in range(4):
                append_decision_log(task, live_decision, "agent_hook", log_path=path)
            with patch("codex_model_router.router.classify_with_notdiamond", return_value=live_choice) as router:
                decision = route_task(
                    task,
                    catalog=catalog(),
                    surface="agent",
                    feedback_log_path=path,
                    use_feedback=False,
                )
        router.assert_called_once()
        self.assertEqual(decision.source, "notdiamond")
        self.assertFalse(decision.cache_hit)

    def test_similarity_cache_skips_notdiamond_after_stable_sample_threshold(self):
        task = "Run the parser unit tests"
        live_decision = apply_policy(
            choice(
                model="gpt-5.6-terra",
                effort="medium",
                task_type="answer",
                source="notdiamond",
                nd_proxy_model="claude-sonnet-4.6",
            ),
            task,
            catalog(),
            surface="agent",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "decisions.jsonl"
            for _ in range(5):
                append_decision_log(task, live_decision, "agent_hook", log_path=path)
            with patch("codex_model_router.router.classify_with_notdiamond") as router:
                decision = route_task(
                    "Run parser unit tests",
                    catalog=catalog(),
                    surface="agent",
                    feedback_log_path=path,
                    use_feedback=False,
                )
        router.assert_not_called()
        self.assertEqual(decision.source, "similarity-cache")
        self.assertTrue(decision.cache_hit)
        self.assertEqual(decision.cache_sample_count, 5)
        self.assertEqual((decision.model, decision.effort), ("gpt-5.6-terra", "medium"))

    def test_recursive_spawn_calls_route_for_each_hook_invocation(self):
        payload = {"tool_name": "spawn_agent", "tool_input": {"message": "Inspect the module"}}
        first = apply_policy(choice(model="gpt-5.6-luna"), "Inspect the module", catalog(), surface="agent")
        second = apply_policy(choice(model="gpt-5.6-sol", effort="high"), "Inspect the module", catalog(), surface="agent")
        with patch("codex_model_router.router.discover_catalog", return_value=catalog()), patch("codex_model_router.router.classify_with_notdiamond", side_effect=[first, second]) as route:
            self.assertIsNotNone(run_hook(payload, no_log=True))
            self.assertIsNotNone(run_hook(payload, no_log=True))
        self.assertEqual(route.call_count, 2)

    def test_parent_supplied_model_is_still_routed(self):
        payload = {
            "tool_name": "Agent",
            "tool_input": {
                "message": "Research several independent options",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "high",
            },
        }
        routed = apply_policy(
            choice(model="gpt-5.6-luna", effort="low"),
            payload["tool_input"]["message"],
            catalog(),
            surface="agent",
        )
        with patch("codex_model_router.router.discover_catalog", return_value=catalog()), patch("codex_model_router.router.route_task", return_value=routed) as route:
            output = run_hook(payload, heuristic_only=False, no_log=True)
        route.assert_called_once()
        updated = output["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(updated["model"], "gpt-5.6-luna")
        self.assertEqual(updated["reasoning_effort"], "low")

    def test_full_history_fork_is_changed_for_routed_model(self):
        payload = {
            "tool_name": "multi_agent_v1__spawn_agent",
            "tool_input": {
                "message": "Audit the verification provider contract",
                "fork_turns": "all",
            },
        }
        routed = apply_policy(
            choice(model="gpt-5.6-terra", effort="medium"),
            payload["tool_input"]["message"],
            catalog(),
            surface="agent",
        )
        with patch("codex_model_router.router.discover_catalog", return_value=catalog()), patch("codex_model_router.router.route_task", return_value=routed):
            output = run_hook(payload, no_log=True)
        updated = output["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(updated["fork_turns"], "none")
        self.assertEqual(updated["model"], "gpt-5.6-terra")

    def test_user_preserve_marker_keeps_explicit_model_and_strips_marker(self):
        payload = {
            "tool_name": "spawn_agent",
            "tool_input": {
                "task_name": "user-pinned-review",
                "message": PRESERVE_MODEL_MARKER + " Review this patch",
                "model": "gpt-6-astra",
                "reasoning_effort": "high",
            },
        }
        with patch("codex_model_router.router.discover_catalog") as discover, patch("codex_model_router.router.route_task") as route, patch("codex_model_router.router.append_decision_log") as log:
            output = run_hook(payload, no_log=False)
        discover.assert_not_called()
        route.assert_not_called()
        log.assert_called_once()
        self.assertEqual(log.call_args.args[1].source, "explicit-user-model")
        self.assertEqual(log.call_args.kwargs["outcome"], "agent_hook_user_pinned")
        updated = output["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(updated["message"], "Review this patch")
        self.assertEqual(updated["model"], "gpt-6-astra")
        self.assertEqual(updated["reasoning_effort"], "high")

    def test_preserve_marker_without_model_still_routes_and_is_stripped(self):
        payload = {
            "tool_name": "spawn_agent",
            "tool_input": {
                "message": PRESERVE_MODEL_MARKER + " Inspect the module",
            },
        }
        routed = apply_policy(
            choice(model="gpt-5.6-terra", effort="medium"),
            "Inspect the module",
            catalog(),
            surface="agent",
        )
        with patch("codex_model_router.router.discover_catalog", return_value=catalog()), patch("codex_model_router.router.route_task", return_value=routed):
            output = run_hook(payload, no_log=True)
        updated = output["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(updated["message"], "Inspect the module")
        self.assertEqual(updated["model"], "gpt-5.6-terra")

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
                codex_session_id="parent-session",
            )
            content = path.read_text(encoding="utf-8")
            payload = json.loads(content)
        self.assertNotIn(raw_prompt, content)
        self.assertNotIn("reason", payload["route"])
        self.assertNotIn("cwd_name", payload)
        self.assertEqual(len(payload["task_sha256"]), 64)
        self.assertEqual(payload["features"]["words"], 8)
        self.assertEqual(payload["codex"]["model"], decision.model)
        self.assertEqual(payload["codex"]["reasoning_effort"], decision.effort)
        self.assertEqual(payload["codex_session_id"], "parent-session")
        self.assertIn("Codex {0} ({1})".format(decision.model, decision.effort), payload["display"])
        self.assertFalse(payload["fallback"]["used"])


class CliSpawnRouteTests(unittest.TestCase):
    def test_spawn_route_outputs_native_spawn_arguments_and_logs(self):
        routed = apply_policy(
            choice(model="gpt-5.6-sol", effort="high", task_type="review"),
            "Review the provider boundary",
            catalog(),
            surface="agent",
        )
        output = io.StringIO()
        with patch.object(cli_module, "find_codex_executable", return_value="codex"), patch.object(
            cli_module, "route_task", return_value=routed
        ) as route, patch.object(cli_module, "_try_log_decision") as log, redirect_stdout(output):
            exit_code = cli_module.main(
                [
                    "--spawn-route",
                    "--prompt",
                    "Review the provider boundary",
                    "--task-name",
                    "provider-review",
                    "--session-id",
                    "parent-session",
                ]
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            payload["spawn_input"],
            {
                "model": "gpt-5.6-sol",
                "reasoning_effort": "high",
                "fork_turns": "none",
                "task_name": "provider_review",
            },
        )
        self.assertEqual(route.call_args.kwargs["surface"], "agent")
        self.assertEqual(log.call_args.kwargs["outcome"], "agent_pre_spawn")
        self.assertEqual(log.call_args.kwargs["task_name"], "provider_review")
        self.assertEqual(log.call_args.kwargs["codex_session_id"], "parent-session")

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
