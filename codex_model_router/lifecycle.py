"""Local, reviewed model comparisons and managed native v1 catalog refresh.

No model calls, remote document execution or inference from version numbers.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import time
import uuid
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def native_model(model: str) -> bool:
    match = re.fullmatch(r"gpt-(\d+)(?:\.\d+)?-(luna|sol|astra)", model)
    return bool(match and int(match.group(1)) >= 6)


def family(model: str) -> str:
    return model.rsplit("-", 1)[-1]


def settings(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    value = payload.get("lifecycle", {})
    if not isinstance(value, Mapping):
        raise ValueError("lifecycle must be an object")
    if value.get("cost_basis", "subscription") not in ("subscription", "api_standard"):
        raise ValueError("Unknown lifecycle cost basis")
    value = dict(value)
    registry = value.get("registry", {})
    if isinstance(registry, Mapping) and registry.get("cache_path"):
        try:
            registry_payload = json.loads(Path(registry["cache_path"]).read_text(encoding="utf-8"))
            validate_registry(registry_payload)
            if registry_payload.get("schema_version") == 1 and isinstance(registry_payload.get("comparisons"), list):
                value["comparisons"] = registry_payload["comparisons"] + value.get("comparisons", [])
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    return value


def refresh_registry(payload: Mapping[str, Any]) -> bool:
    """Public reviewed JSON only; called by maintenance, never the spawn hook."""
    registry = payload.get("lifecycle", {}).get("registry", {})
    if not isinstance(registry, Mapping) or registry.get("enabled") is not True:
        return False
    url = registry.get("url", "")
    if url != "https://raw.githubusercontent.com/UirtvalP/codex-model-router/main/docs/model-comparisons.json":
        raise ValueError("Registry must be the explicitly trusted repository's HTTPS data file")
    cache = Path(registry["cache_path"])
    attempt = cache.with_name(cache.name + ".attempt")
    if time.time() - (attempt.stat().st_mtime if attempt.exists() else 0) < 86400:
        return False
    atomic_json(attempt, {"attempted_at": time.time()})
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            if response.geturl() != url:
                raise ValueError("Registry redirect is not trusted")
            raw = response.read(262145)
        if len(raw) > 262144:
            raise ValueError("Registry exceeds size limit")
        data = json.loads(raw)
        if (not isinstance(data, Mapping) or data.get("schema_version") != 1
                or not isinstance(data.get("comparisons"), list) or len(data["comparisons"]) > 256):
            raise ValueError("Invalid registry schema")
        validate_registry(data)
        try:
            old = json.loads(cache.read_text(encoding="utf-8"))
            if data["revision"] < old.get("revision", 0) or (data["revision"] == old.get("revision") and data != old):
                raise ValueError("Registry rollback rejected")
        except (OSError, json.JSONDecodeError):
            pass
        atomic_json(cache, data)
        return True
    except (OSError, ValueError, TypeError):
        return False


def validate_registry(data: Any) -> None:
    if (not isinstance(data, dict) or set(data) != {"schema_version", "revision", "comparisons"}
            or data["schema_version"] != 1 or type(data["revision"]) is not int or data["revision"] < 1
            or not isinstance(data["comparisons"], list) or len(data["comparisons"]) > 256):
        raise ValueError("Invalid registry schema")
    for row in data["comparisons"]:
        required = {"old", "new", "tier", "capability", "prices"}
        if not isinstance(row, dict) or not required.issubset(row) or set(row) - required - {"release"}:
            raise ValueError("Invalid comparison schema")
        release = row.get("release")
        if release is not None:
            if (not isinstance(release, dict) or set(release) != {"date", "date_note", "compared_models", "dimensions", "price_scope"}
                    or not all(isinstance(release[k], str) for k in ("date", "date_note", "price_scope"))
                    or not all(isinstance(release[k], list) and 0 < len(release[k]) <= 16
                               and all(isinstance(v, str) for v in release[k]) for k in ("compared_models", "dimensions"))):
                raise ValueError("Invalid release provenance")
        if not all(isinstance(row[k], str) for k in ("old", "new", "tier")):
            raise ValueError("Invalid comparison IDs")
        if not isinstance(row["capability"], dict) or not isinstance(row["prices"], dict):
            raise ValueError("Invalid evidence schema")
        allowed = {"verdict", "reviewed_by", "source", "checked_at", "valid_until"}
        if set(row["capability"]) != allowed or not all(isinstance(v, str) for v in row["capability"].values()):
            raise ValueError("Invalid capability schema")
        prices = row["prices"]
        if prices.get("basis") == "unknown":
            if set(prices) != {"basis", "reason"} or not isinstance(prices["reason"], str):
                raise ValueError("Invalid unknown prices")
        elif (set(prices) != {"basis", "currency", "source", "reviewed_by", "checked_at", "valid_until", "scope", "old", "new"}
              or not all(isinstance(prices[k], str) for k in prices if k not in ("old", "new"))
              or not all(isinstance(prices[k], dict) for k in ("old", "new"))):
            raise ValueError("Invalid prices schema")


def runtime_version(executable: str) -> str:
    result = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=8, check=False)
    match = re.search(r"\b(\d+\.\d+)\.\d+", result.stdout)
    if result.returncode or not match:
        raise ValueError("Native runtime version unavailable")
    return match.group(1)


def replacements(models: Mapping[str, Any], payload: Mapping[str, Any]):
    """Only strict, same-tier, available, enabled Pareto improvements retire IDs."""
    config = settings(payload)
    evidence = config.get("comparisons", [])
    if not isinstance(evidence, list):
        raise ValueError("lifecycle comparisons must be a list")
    result: Dict[str, str] = {}
    stronger = []
    for row in evidence:
        if not isinstance(row, Mapping):
            continue
        old, new = row.get("old"), row.get("new")
        if not isinstance(old, str) or not isinstance(new, str):
            continue
        if (old == new or old not in models or new not in models
                or not native_model(old) or not native_model(new)
                or family(old) != family(new) or row.get("tier") != family(old)):
            continue
        if not set(models[old]).issubset(models[new]):
            continue
        proof = row.get("capability", {})
        if not isinstance(proof, Mapping):
            continue
        try:
            current = date.fromisoformat(str(proof["checked_at"])) <= date.today() <= date.fromisoformat(str(proof["valid_until"]))
        except (ValueError, KeyError):
            continue
        if not (current and isinstance(proof.get("reviewed_by"), str) and proof["reviewed_by"]
                and isinstance(proof.get("source"), str) and proof["source"].startswith("https://")
                and proof.get("verdict") == "strictly_better"):
            continue
        stronger.append("{0} has reviewed stronger capability than {1}; cost is separate".format(new, old))
        prices = row.get("prices", {})
        if not isinstance(prices, Mapping):
            continue
        try:
            if not date.fromisoformat(str(prices["checked_at"])) <= date.today() <= date.fromisoformat(str(prices["valid_until"])):
                continue
        except (ValueError, KeyError):
            continue
        basis = config.get("cost_basis", "subscription")
        keys = ("input", "cached_input", "output") if basis == "api_standard" else ("usage_units",)
        if (prices.get("basis") != basis or not prices.get("reviewed_by")
                or not isinstance(prices.get("source"), str) or not prices["source"].startswith("https://")
                or prices.get("currency") != ("USD_per_million_tokens" if basis == "api_standard" else "subscription_units")
                or not isinstance(prices.get("valid_until"), str) or prices["valid_until"] < date.today().isoformat()
                or not isinstance(prices.get("scope"), str) or not prices["scope"]):
            continue
        if basis == "subscription" and prices["scope"] != config.get("subscription_scope"):
            continue
        previous, next_prices = prices.get("old", {}), prices.get("new", {})
        if not isinstance(previous, Mapping) or not isinstance(next_prices, Mapping):
            continue
        numbers = [values.get(key) for values in (previous, next_prices) for key in keys]
        if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) or n < 0 for n in numbers):
            continue
        if all(next_prices[k] <= previous[k] for k in keys) and any(next_prices[k] < previous[k] for k in keys):
            result.setdefault(old, new)
    # 循环证据无效；保守保留所有参与循环的旧模型。
    for old in list(result):
        seen = set()
        current = old
        while current in result and current not in seen:
            seen.add(current)
            current = result[current]
        if current in seen:
            for model in seen:
                result.pop(model, None)
    for old in result:
        current = result[old]
        while current in result:
            current = result[current]
        result[old] = current
    return result, tuple(stronger), digest(config)


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        descriptor = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(str(temporary), str(path))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def stamp(path: Path):
    try:
        info = path.stat()
        return [str(path.resolve()), info.st_size, info.st_mtime_ns]
    except OSError:
        return [str(path), None]


def validate_snapshot(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("models"), list):
        raise ValueError("Native snapshot must contain models")
    rows = payload["models"]
    seen = set()
    present = set()
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("slug"), str) or row["slug"] in seen:
            raise ValueError("Invalid/duplicate native model")
        seen.add(row["slug"])
        if native_model(row["slug"]):
            levels = row.get("supported_reasoning_levels")
            if (not isinstance(levels, list) or not levels or not row.get("model_messages")
                    or not row.get("input_modalities") or not row.get("context_window")):
                raise ValueError("Incomplete native capability metadata")
            present.add(family(row["slug"]))
    if present != {"luna", "sol", "astra"}:
        raise ValueError("Native snapshot lacks required families")
    return {"models": rows}


def sync_native_catalog(executable: str, payload: Mapping[str, Any], *, force: bool = False) -> Optional[Dict[str, Any]]:
    config = settings(payload).get("native_refresh")
    if not isinstance(config, Mapping) or config.get("enabled") is not True:
        return None
    target = Path(config["catalog_path"])
    lock = target.with_name(target.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()
    except FileExistsError:
        try:
            if time.time() - lock.stat().st_mtime > 120:
                lock.rmdir()
                lock.mkdir()
            else:
                return None
        except OSError:
            return None
    try:
        return _sync_native_locked(executable, payload, force=force)
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass


def _sync_native_locked(executable: str, payload: Mapping[str, Any], *, force: bool = False) -> Optional[Dict[str, Any]]:
    """Cheap stat checks; refresh only on changed native sources, never per-spawn web evals.

    Authorized IDs come from the account's native cache. Bundled metadata alone
    never introduces an unconfirmed account model. Config paths are opt-in.
    """
    config = settings(payload).get("native_refresh")
    if not isinstance(config, Mapping) or config.get("enabled") is not True:
        return None
    target = Path(config["catalog_path"])
    source = Path(config["source_cache"])
    state_path = Path(config["state_path"])
    if not all(p.is_absolute() for p in (target, source, state_path)) or len({target, source, state_path}) != 3:
        raise ValueError("Native refresh needs distinct absolute paths")
    native = Path(executable).resolve().parent.parent / "CodexCLI.app/Contents/MacOS/codex"
    runtime = stamp(native if native.is_file() else Path(executable))
    fingerprint = {"runtime": runtime, "source": stamp(source), "target": stamp(target)}
    try:
        previous = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    if not isinstance(previous, Mapping):
        previous = {}
    if not isinstance(previous.get("failed_at", 0), (int, float)):
        previous = {}
    if (not force and previous.get("fingerprint") == fingerprint
            and time.time() - previous.get("failed_at", 0) > 60):
        return None
    if not force and previous.get("failed_fingerprint") == fingerprint and time.time() - previous.get("failed_at", 0) < 60:
        return None
    try:
        prior_fingerprint = previous.get("fingerprint", {})
        changed = not isinstance(prior_fingerprint, Mapping) or prior_fingerprint.get("runtime") != runtime
        version = runtime_version(executable) if changed else previous.get("runtime_version")
        raw = json.loads(source.read_text(encoding="utf-8"))
        source_version = raw.get("client_version", "") if isinstance(raw, Mapping) else ""
        if not isinstance(source_version, str) or not re.match(r"^\d+\.\d+\.\d+", source_version):
            raise ValueError("Native cache version unavailable")
        if not isinstance(source_version, str) or not source_version.startswith(str(version) + "."):
            snapshot = validate_snapshot(json.loads(target.read_text(encoding="utf-8")))
        else:
            snapshot = validate_snapshot(raw)
        if changed:
            # --bundled 离线读取匹配新二进制的完整能力字段，不调用模型或扩展授权目录。
            completed = subprocess.run([executable, "debug", "models", "--bundled"], capture_output=True,
                                       text=True, encoding="utf-8", errors="replace", timeout=8, check=False)
            if completed.returncode:
                raise ValueError("Bundled native catalog unavailable")
            bundled = validate_snapshot(json.loads(completed.stdout))
            by_id = {row["slug"]: row for row in bundled["models"]}
            snapshot = {"models": [by_id.get(row["slug"], row) for row in snapshot["models"]]}
        snapshot = {"models": [dict(row, multi_agent_version="v1") for row in snapshot["models"]]}
        validate_snapshot(snapshot)
        if stamp(source) != fingerprint["source"] or stamp(native if native.is_file() else Path(executable)) != runtime:
            raise ValueError("Native sources changed during refresh")
        atomic_json(target, snapshot)
        fingerprint["target"] = stamp(target)
        atomic_json(state_path, {"fingerprint": fingerprint, "runtime_version": version, "updated_at": time.time(),
                                 "model_ids": [row["slug"] for row in snapshot["models"]],
                                 "snapshot_digest": digest(snapshot)})
        return snapshot
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        # 写入失败状态，保留最后可用目录；不在每个 spawn 重试失败源。
        try:
            atomic_json(state_path, dict(previous, failed_fingerprint=fingerprint, failed_at=time.time(),
                                         error=type(exc).__name__))
        except OSError:
            pass
        return None
