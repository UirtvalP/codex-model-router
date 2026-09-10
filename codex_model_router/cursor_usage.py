"""Read Cursor official usage-export snapshots without conversation content."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

CSV_COLUMNS = ("Date", "Kind", "Model", "Input (w/ Cache Write)", "Input (w/o Cache Write)", "Cache Read", "Output Tokens", "Total Tokens")


def _parse_date(value: str) -> Optional[date]:
    return date.fromisoformat(value) if value else None


def _local_date(value: Any) -> Optional[date]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone().date() if parsed.tzinfo else None


def _integer(row: Mapping[str, Any], column: str) -> Optional[int]:
    try:
        value = int(str(row.get(column, "")).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _empty_metrics() -> Dict[str, None]:
    return {"input_tokens": None, "cached_input_tokens": None, "cache_write_input_tokens": None, "output_tokens": None, "reasoning_output_tokens": None, "uncached_input_tokens": None, "total_tokens": None, "cache_hit_rate": None}


def _metrics(value: Mapping[str, int]) -> Dict[str, Any]:
    input_tokens = sum(value.get(field, 0) for field in ("cache_write", "input", "cache_read"))
    return {"input_tokens": input_tokens, "cached_input_tokens": value.get("cache_read", 0), "cache_write_input_tokens": value.get("cache_write", 0), "output_tokens": value.get("output", 0), "reasoning_output_tokens": None, "uncached_input_tokens": value.get("cache_write", 0) + value.get("input", 0), "total_tokens": input_tokens + value.get("output", 0), "cache_hit_rate": value.get("cache_read", 0) / input_tokens if input_tokens else None}


def _routes(path: Path, first: Optional[date], last: Optional[date]) -> Dict[str, Any]:
    proposals: Dict[str, Mapping[str, Any]] = {}
    paired = []
    errors = orphans = 0
    if not path.is_file():
        return {"routes": [], "route_summary": {"total": 0, "verified": 0, "matched": 0, "failed": 0, "errors": 0}}
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                errors += 1
                continue
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                errors += 1
                continue
            if row.get("actual_model") is None:
                proposals[row["id"]] = row
                continue
            proposal = proposals.pop(row["id"], None)
            if proposal is None:
                orphans += 1
            else:
                paired.append((proposal, row))
    paired.extend((proposal, None) for proposal in proposals.values())
    routes = []
    for proposal, observation in paired:
        stamp = _local_date(proposal.get("timestamp"))
        if stamp is None:
            errors += 1
            continue
        if (first and stamp < first) or (last and stamp > last):
            continue
        actual = observation.get("actual_model") if observation else None
        routes.append({"id": proposal["id"], "timestamp": proposal.get("timestamp"), "original_model": proposal.get("original_model"), "requested_model": proposal.get("requested_model"), "actual_model": actual, "reason": proposal.get("reason") or (observation.get("reason") if observation else "")})
    routes.sort(key=lambda row: str(row["timestamp"]), reverse=True)
    verified = sum(row["actual_model"] is not None for row in routes)
    matched = sum(row["actual_model"] is not None and row["actual_model"] == row["requested_model"] for row in routes)
    failed = sum(
        (row["actual_model"] is not None and row["actual_model"] != row["requested_model"])
        or str(row["reason"] or "").lower().startswith("error:")
        for row in routes
    )
    return {"routes": routes, "route_summary": {"total": len(routes), "verified": verified, "matched": matched, "failed": failed, "errors": errors + orphans}}


def _official_csv_payload(path: Path, first: Optional[date], last: Optional[date]) -> Optional[Dict[str, Any]]:
    summary: Dict[str, int] = defaultdict(int)
    days: Dict[str, Dict[str, int]] = {}
    models: Dict[str, Dict[str, int]] = {}
    day_models: Dict[str, Dict[str, Dict[str, int]]] = {}
    rows = accepted = invalid = skipped_api_key = mismatches = 0
    event_dates = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames or any(column not in reader.fieldnames for column in CSV_COLUMNS):
                return None
            for row in reader:
                rows += 1
                if str(row.get("Kind") or "").strip().lower() == "user api key":
                    skipped_api_key += 1
                    continue
                stamp = _local_date(row.get("Date"))
                values = [_integer(row, column) for column in CSV_COLUMNS[3:7]]
                reported_total = _integer(row, "Total Tokens")
                if stamp is None or any(value is None for value in values) or reported_total is None:
                    invalid += 1
                    continue
                counters = dict(zip(("cache_write", "input", "cache_read", "output"), values))
                if sum(counters.values()) != reported_total:
                    mismatches += 1
                    invalid += 1
                    continue
                event_dates.append(stamp.isoformat())
                if (first and stamp < first) or (last and stamp > last):
                    continue
                accepted += 1
                model = str(row.get("Model") or "unknown")
                targets = (summary, days.setdefault(stamp.isoformat(), defaultdict(int)), models.setdefault(model, defaultdict(int)), day_models.setdefault(stamp.isoformat(), {}).setdefault(model, defaultdict(int)))
                for target in targets:
                    for field, value in counters.items():
                        target[field] += value
    except OSError:
        return None
    return {"summary": _metrics(summary), "by_day": [dict(date=stamp, models=[dict(model=model, **_metrics(values)) for model, values in sorted(day_models[stamp].items())], **_metrics(values)) for stamp, values in sorted(days.items(), reverse=True)], "by_model": sorted([dict(model=model, **_metrics(values)) for model, values in models.items()], key=lambda row: -row["total_tokens"]), "by_session": [], "coverage": {"source": "Cursor 官方导出 CSV", "imported_at": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(), "event_range": {"start": min(event_dates) if event_dates else None, "end": max(event_dates) if event_dates else None}, "rows": rows, "accepted_rows": accepted, "skipped_api_key_rows": skipped_api_key, "invalid_rows": invalid, "total_mismatches": mismatches, "note": "官方导出快照，非实时数据；已跳过 User API Key 和缺失或校验不通过的行。CSV 不含会话映射，无法按会话汇总；未提供推理 token。"}}


def build_cursor_payload(db_path: Path, route_log: Path, start: str = "", end: str = "", csv_path: Optional[Path] = None) -> Dict[str, Any]:
    """Build usage from an official export; db_path remains for API compatibility."""
    first, last = _parse_date(start), _parse_date(end)
    if first and last and first > last:
        raise ValueError("start must not exceed end")
    official_path = Path(csv_path) if csv_path else Path.home() / ".cursor" / "model-router" / "usage-events.csv"
    usage = _official_csv_payload(official_path, first, last) if official_path.is_file() else None
    route_data = _routes(Path(route_log), first, last)
    if usage is None:
        usage = {"summary": _empty_metrics(), "by_day": [], "by_model": [], "by_session": [], "coverage": {"source": "无官方导出 CSV", "note": "未导入 Cursor 官方用量快照，无法可靠统计 token。"}}
    usage.update(route_data)
    return usage
