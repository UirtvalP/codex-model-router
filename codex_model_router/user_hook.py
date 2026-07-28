"""Safe, idempotent installation of the user-level Codex agent hook."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


ROUTER_STATUS_MESSAGE = "Choosing the subagent model"


def build_user_hook_group(
    python_executable: Optional[str] = None,
    platform_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Pin the hook to the interpreter containing this installed package."""

    executable = os.path.abspath(python_executable or sys.executable)
    platform = platform_name or os.name
    if platform == "nt":
        command = "codex-model-router --hook"
        powershell_executable = executable.replace("'", "''")
        command_windows = (
            'powershell.exe -NoLogo -NoProfile -NonInteractive -Command '
            '"& \'{0}\' -I -m codex_model_router --hook"'
        ).format(powershell_executable)
    else:
        command = "{0} -I -m codex_model_router --hook".format(
            shlex.quote(executable)
        )
        command_windows = "codex-model-router.exe --hook"
    return {
        "matcher": "^(Agent|spawn_agent)$",
        "hooks": [
            {
                "type": "command",
                "command": command,
                "commandWindows": command_windows,
                "timeout": 55,
                "statusMessage": ROUTER_STATUS_MESSAGE,
            }
        ],
    }


def _is_router_hook_group(group: Any) -> bool:
    if not isinstance(group, dict):
        return False
    handlers = group.get("hooks")
    if not isinstance(handlers, list):
        return False
    for handler in handlers:
        if not isinstance(handler, dict):
            continue
        commands = (handler.get("command"), handler.get("commandWindows"))
        if (
            handler.get("statusMessage") == ROUTER_STATUS_MESSAGE
            and any(
                isinstance(command, str)
                and "--hook" in command
                and (
                    "codex_model_router" in command
                    or "codex_router" in command
                    or "codex-model-router" in command
                )
                for command in commands
            )
        ):
            return True
    return False


def default_user_hooks_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home) / "hooks.json"
    return Path.home() / ".codex" / "hooks.json"


def _atomic_json_write(path: Path, payload: Dict[str, Any]) -> None:
    temporary = path.with_name(".{0}.{1}.tmp".format(path.name, uuid.uuid4().hex))
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        os.replace(str(temporary), str(path))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def install_user_hook(
    path: Optional[Path] = None,
) -> Tuple[Path, bool, Optional[Path]]:
    """Merge the router hook without discarding any existing user hooks."""

    target = path or default_user_hooks_path()
    existed = target.exists()
    if existed:
        payload = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Existing hooks.json must contain a JSON object")
    else:
        payload = {
            "description": "User-level hooks installed by Codex Model Router.",
            "hooks": {},
        }

    hooks = payload.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Existing hooks.json field 'hooks' must be an object")
    groups = hooks.setdefault("PreToolUse", [])
    if not isinstance(groups, list):
        raise ValueError("Existing PreToolUse hooks must be an array")
    user_hook_group = build_user_hook_group()
    changed = False
    for index, group in enumerate(groups):
        if group == user_hook_group:
            return target, False, None
        if _is_router_hook_group(group):
            groups[index] = user_hook_group
            changed = True
            break
    if not changed:
        groups.append(user_hook_group)
    target.parent.mkdir(parents=True, exist_ok=True)
    backup: Optional[Path] = None
    if existed:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = target.with_name("{0}.bak.{1}".format(target.name, stamp))
        if backup.exists():
            backup = target.with_name(
                "{0}.bak.{1}.{2}".format(target.name, stamp, uuid.uuid4().hex[:8])
            )
        shutil.copy2(str(target), str(backup))
    _atomic_json_write(target, payload)
    return target, True, backup
