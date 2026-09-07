#!/usr/bin/env python3
"""Route a Codex task, launch it, or handle a spawn-agent hook."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from .router import (
    CODEX_EVALUATOR_DEFAULT_TIMEOUT_SECONDS,
    append_decision_log,
    append_feedback,
    default_log_path,
    dispatch_codex,
    find_codex_executable,
    normalize_spawn_task_name,
    route_task,
    run_hook,
)
from .user_hook import install_user_hook


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Use an isolated Codex evaluator plus deterministic policy to choose "
            "the Codex model and reasoning effort."
        )
    )
    parser.add_argument("task", nargs="*", help="Task text; stdin is used when omitted")
    parser.add_argument("--prompt", help="Task text as a named argument")
    parser.add_argument(
        "--route-only",
        action="store_true",
        help="Print the policy-checked route without launching the real task",
    )
    parser.add_argument(
        "--spawn-route",
        action="store_true",
        help=(
            "Choose and log model arguments for a Codex/Work spawn_agent call "
            "without launching the child"
        ),
    )
    parser.add_argument(
        "--task-name",
        help=(
            "Optional spawn_agent task name; normalized to lowercase letters, "
            "digits, and underscores, returned in spawn_input, and logged"
        ),
    )
    parser.add_argument(
        "--session-id",
        help="Optional parent Codex session id recorded in the route log",
    )
    parser.add_argument(
        "--heuristic-only",
        action="store_true",
        help="Skip the Codex evaluator and use the offline local policy",
    )
    parser.add_argument(
        "--hook",
        action="store_true",
        help="Read one Codex PreToolUse hook payload from stdin",
    )
    parser.add_argument(
        "--cd",
        default=os.getcwd(),
        help="Working directory for the real Codex task",
    )
    parser.add_argument(
        "--sandbox",
        choices=("read-only", "workspace-write", "danger-full-access"),
        help="Explicit sandbox override for the real task",
    )
    parser.add_argument("--resume", help="Resume a Codex task by session id or name")
    parser.add_argument(
        "--resume-last",
        action="store_true",
        help="Resume the latest Codex task in the current directory",
    )
    parser.add_argument(
        "--json-events",
        action="store_true",
        help="Stream the real Codex task as JSONL events",
    )
    parser.add_argument(
        "--classifier-timeout",
        type=float,
        default=CODEX_EVALUATOR_DEFAULT_TIMEOUT_SECONDS,
        help="Seconds before using the Terra/medium fallback",
    )
    parser.add_argument(
        "--no-log",
        action="store_true",
        help="Do not write a prompt-free routing record",
    )
    parser.add_argument(
        "--log-path",
        type=Path,
        help="Override the prompt-free decision log path",
    )
    parser.add_argument(
        "--feedback",
        metavar="DECISION_ID",
        help="Append a feedback label for an earlier decision",
    )
    parser.add_argument(
        "--rating",
        choices=("good", "underpowered", "overkill", "failed"),
        help="Feedback label used with --feedback",
    )
    parser.add_argument(
        "--install-user-hook",
        action="store_true",
        help="Merge agent routing into the current user's Codex hooks.json",
    )
    return parser


def _read_task(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str:
    if args.prompt is not None and args.task:
        parser.error("Use either --prompt or positional task text, not both")
    if args.prompt is not None:
        task = args.prompt
    elif args.task:
        task = " ".join(args.task)
    elif not sys.stdin.isatty():
        task = sys.stdin.read()
    else:
        parser.error("Provide task text or pipe it on stdin")
    if not task.strip():
        parser.error("Task text cannot be empty")
    return task


def _run_hook(args: argparse.Namespace) -> int:
    try:
        payload = json.load(sys.stdin)
        result = run_hook(
            payload,
            heuristic_only=args.heuristic_only,
            no_log=args.no_log,
            classifier_timeout_seconds=args.classifier_timeout,
        )
        if result is not None:
            sys.stdout.write(json.dumps(result))
        return 0
    except Exception:
        # A router outage must never prevent the original agent call.
        return 0


def _try_log_decision(*args, **kwargs) -> None:
    try:
        append_decision_log(*args, **kwargs)
    except OSError as exc:
        print("Codex router warning: decision log unavailable: {0}".format(exc), file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.hook:
        return _run_hook(args)

    if args.install_user_hook:
        if args.prompt is not None or args.task:
            parser.error("--install-user-hook does not accept task text")
        try:
            path, installed, backup = install_user_hook()
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            parser.error("Could not install user hook: {0}".format(exc))
        if installed:
            print("Installed Codex model-router hook in {0}".format(path))
            if backup is not None:
                print("Existing hooks backed up to {0}".format(backup))
        else:
            print("Codex model-router hook is already installed in {0}".format(path))
        return 0

    log_path = args.log_path or default_log_path()
    if args.feedback:
        if not args.rating:
            parser.error("--feedback requires --rating")
        path = append_feedback(args.feedback, args.rating, log_path)
        print("Feedback recorded in {0}".format(path))
        return 0
    if args.rating:
        parser.error("--rating requires --feedback")
    if args.resume and args.resume_last:
        parser.error("Use either --resume or --resume-last")

    task = _read_task(args, parser)
    codex_executable = find_codex_executable()
    decision = route_task(
        task,
        surface="agent" if args.spawn_route else "root",
        heuristic_only=args.heuristic_only,
        codex_executable=codex_executable,
        classifier_timeout_seconds=args.classifier_timeout,
        feedback_log_path=log_path,
        feedback_cwd=args.cd,
        use_feedback=not args.no_log,
    )

    if args.route_only or args.spawn_route:
        output = decision.as_dict()
        task_name = normalize_spawn_task_name(args.task_name) if args.task_name else None
        if args.spawn_route:
            output["spawn_input"] = {
                "model": decision.model,
                "reasoning_effort": decision.effort,
                "fork_turns": "none",
            }
            if task_name:
                output["spawn_input"]["task_name"] = task_name
        print(json.dumps(output, indent=2, sort_keys=True))
        if not args.no_log:
            _try_log_decision(
                task,
                decision,
                outcome="agent_pre_spawn" if args.spawn_route else "route_only",
                log_path=log_path,
                cwd=args.cd,
                task_name=task_name,
                codex_session_id=args.session_id,
            )
        return 0

    print(
        "Codex router: {0} / {1} / {2} (decision {3})".format(
            decision.model,
            decision.effort,
            decision.orchestration,
            decision.decision_id,
        ),
        file=sys.stderr,
    )
    exit_code, execution_ms = dispatch_codex(
        task,
        decision,
        codex_executable=codex_executable,
        cwd=args.cd,
        sandbox=args.sandbox,
        resume=args.resume,
        resume_last=args.resume_last,
        json_events=args.json_events,
    )
    if not args.no_log:
        _try_log_decision(
            task,
            decision,
            outcome="completed" if exit_code == 0 else "failed",
            log_path=log_path,
            cwd=args.cd,
            exit_code=exit_code,
            execution_ms=execution_ms,
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
