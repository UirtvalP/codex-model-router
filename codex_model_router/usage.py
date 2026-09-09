"""Read local Codex usage metadata without exposing conversation content."""
from __future__ import annotations

import json
from bisect import bisect_left
import threading
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Mapping

FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens',
          'reasoning_output_tokens', 'cache_write_input_tokens')
_LOCK = threading.Lock()
_CACHE: Dict[str, Any] = {}


def _counts(value):
    if not isinstance(value, Mapping):
        return None
    if 'input_tokens' not in value or 'output_tokens' not in value:
        return None
    return {k: max(0, value.get(k, 0)) if isinstance(value.get(k, 0), int)
            and not isinstance(value.get(k, 0), bool) else 0 for k in FIELDS}


def _stamp(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone()
    except (ValueError, TypeError, OverflowError):
        return None


def _metrics(value):
    result = {k: value.get(k, 0) for k in FIELDS}
    result['uncached_input_tokens'] = max(0, result['input_tokens'] - result['cached_input_tokens'])
    result['total_tokens'] = result['input_tokens'] + result['output_tokens']
    result['cache_hit_rate'] = result['cached_input_tokens'] / result['input_tokens'] if result['input_tokens'] else 0
    return result


def _parse_session(path):
    records = []
    session = path.stem
    model = 'unknown'
    previous = None
    errors = 0
    has_usage = set()
    fork = False
    modern_times = defaultdict(list)
    parent = None
    created = None
    with path.open(encoding='utf-8', errors='replace') as stream:
        for line in stream:
            if not any(marker in line for marker in ('session_meta', 'turn_context', 'token_usage_record', 'token_count')):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                errors += 1
                continue
            if not isinstance(event, dict):
                continue
            payload = event.get('payload')
            if not isinstance(payload, dict):
                continue
            kind = event.get('type')
            if kind == 'session_meta':
                session = str(payload.get('id') or session)
                parent = payload.get('forked_from_id') or payload.get('parent_thread_id')
                fork = bool(parent)
                created = _stamp(payload.get('timestamp'))
                continue
            if kind == 'turn_context':
                model = str(payload.get('model') or 'unknown')
                continue
            stamp = _stamp(event.get('timestamp'))
            if not stamp:
                continue
            usage = None
            key = None
            owner = session
            if kind == 'token_usage_record':
                usage = _counts(payload.get('usage'))
                owner = str(payload.get('thread_id') or session)
                response = payload.get('response_id')
                key = ('response', str(response)) if response else ('new', owner, event.get('ordinal'), str(event.get('timestamp')))
                if usage is not None:
                    modern_times[(owner, tuple(usage[k] for k in FIELDS))].append(stamp.timestamp())
            elif kind == 'event_msg' and payload.get('type') == 'token_count':
                info = payload.get('info')
                if not isinstance(info, dict):
                    continue
                total = _counts(info.get('total_token_usage'))
                last = _counts(info.get('last_token_usage'))
                if total is None:
                    continue
                if previous == total:
                    continue
                if previous is None:
                    usage = last if fork and last is not None else total
                elif any(total[k] < previous[k] for k in ('input_tokens', 'output_tokens')):
                    usage = last or total
                else:
                    usage = {k: max(0, total[k] - previous[k]) for k in FIELDS}
                previous = total
                if parent and created and stamp < created:
                    owner = str(parent)
                key = ('legacy', owner, str(event.get('timestamp')), tuple(total[k] for k in FIELDS),
                       tuple((last or usage)[k] for k in FIELDS))
            if usage is not None and key is not None:
                has_usage.add(owner)
                if usage['input_tokens'] or usage['output_tokens']:
                    records.append((key, stamp.isoformat(), owner, model, 'subagent' if fork else 'session', usage))
    for times in modern_times.values():
        times.sort()
    def is_dual_write(record):
        if record[0][0] != 'legacy':
            return False
        times = modern_times.get((record[2], record[0][4]), [])
        when = datetime.fromisoformat(record[1]).timestamp()
        index = bisect_left(times, when - 2)
        if index < len(times) and times[index] <= when + 2:
            times.pop(index)
            return True
        return False
    records = [r for r in records if not is_dual_write(r)]
    return records, errors, has_usage


def _parse_router(path):
    rows, errors = [], 0
    with path.open(encoding='utf-8', errors='replace') as stream:
        for line in stream:
            try:
                item = json.loads(line)
            except ValueError:
                errors += 1
                continue
            if not isinstance(item, dict):
                continue
            evaluator = item.get('evaluator')
            if not isinstance(evaluator, dict):
                continue
            usage = _counts(evaluator.get('usage'))
            stamp = _stamp(item.get('timestamp'))
            if not usage or not stamp:
                continue
            thread = evaluator.get('thread_id')
            decision = item.get('decision_id')
            if not thread and not decision:
                continue
            owner = str(thread or 'evaluator:' + str(decision))
            key = ('evaluator', str(thread or decision))
            rows.append((key, stamp.isoformat(), owner, str(evaluator.get('model') or 'unknown'), 'evaluator', usage))
    return rows, errors, set()


def _cached(path, router=False):
    key = str(path.resolve())
    try:
        stat = path.stat()
        fingerprint = (stat.st_mtime_ns, stat.st_size, stat.st_ino, router)
        cached = _CACHE.get(key)
        if cached and cached[0] == fingerprint:
            return cached[1]
        result = _parse_router(path) if router else _parse_session(path)
        _CACHE[key] = (fingerprint, result)
        return result
    except OSError:
        return [], 1, set()


def _session_titles(home):
    titles = {}
    index = Path(home) / 'session_index.jsonl'
    try:
        with index.open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict) and row.get('id') and row.get('thread_name'):
                        titles[str(row['id'])] = str(row['thread_name'])
                except ValueError:
                    continue
    except OSError:
        pass
    for database in sorted(Path(home).glob('state_*.sqlite'), key=lambda p: int(p.stem.split('_')[-1]) if p.stem.split('_')[-1].isdigit() else 0):
        connection = None
        try:
            connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.5)
            columns = {r[1] for r in connection.execute('pragma table_info(threads)')}
            if 'id' not in columns or 'title' not in columns:
                continue
            query = 'select id, title, name from threads' if 'name' in columns else 'select id, title, NULL from threads'
            for thread, title, name in connection.execute(query):
                value = name or titles.get(str(thread)) or title
                if value:
                    titles[str(thread)] = str(value)
        except (sqlite3.Error, OSError):
            continue
        finally:
            if connection is not None:
                connection.close()
    return titles


def build_usage_payload(codex_home: Path, log_path: Path, start: str = '', end: str = '') -> dict:
    """Aggregate requests by local event date, model and actual thread identity."""
    first = date.fromisoformat(start) if start else None
    last = date.fromisoformat(end) if end else None
    if first and last and first > last:
        raise ValueError('start must not exceed end')
    with _LOCK:
        files = sorted(set(p for directory in ('sessions', 'archived_sessions')
                           for p in (Path(codex_home) / directory).rglob('*.jsonl')))
        rows, covered, errors = [], set(), 0
        paths = {str(p.resolve()) for p in files}
        for path in files:
            parsed, count, owners = _cached(path)
            rows.extend(parsed)
            covered.update(owners)
            errors += count
        if Path(log_path).is_file():
            paths.add(str(Path(log_path).resolve()))
            supplement, count, _ = _cached(Path(log_path), router=True)
            errors += count
            rows.extend(r for r in supplement if r[2] not in covered)
        for key in list(_CACHE):
            if key not in paths:
                del _CACHE[key]
        seen = set()
        summary = defaultdict(int)
        models, days, sessions, day_models = {}, {}, {}, {}
        week_sessions = {}
        today = datetime.now().astimezone().date()
        week_start = today - timedelta(days=6)
        titles = _session_titles(codex_home)
        duplicates = 0
        for key, timestamp, session, model, source, usage in sorted(rows, key=lambda r: r[1]):
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            day = datetime.fromisoformat(timestamp).date()
            if week_start <= day <= today:
                weekly = week_sessions.setdefault(session, defaultdict(int))
                for field in FIELDS:
                    weekly[field] += usage[field]
            if (first and day < first) or (last and day > last):
                continue
            by_model = models.setdefault(model, defaultdict(int))
            by_day = days.setdefault(day.isoformat(), defaultdict(int))
            by_day_model = day_models.setdefault(day.isoformat(), {}).setdefault(model, defaultdict(int))
            by_session = sessions.setdefault(session, {'usage': defaultdict(int), 'models': set(), 'source': source})
            by_session['models'].add(model)
            for field in FIELDS:
                for target in (summary, by_model, by_day, by_day_model, by_session['usage']):
                    target[field] += usage[field]
        return {
            'summary': _metrics(summary),
            'by_model': sorted([dict(model=k, **_metrics(v)) for k, v in models.items()], key=lambda r: -r['total_tokens']),
            'by_day': [dict(date=k, models=[dict(model=m, **_metrics(c)) for m, c in sorted(day_models[k].items())], **_metrics(v)) for k, v in sorted(days.items(), reverse=True)],
            'by_session': sorted([dict(session_id=k, title=titles.get(k) or ('路由评估' if v['source'] == 'evaluator' else '未命名对话'), models=sorted(v['models']), source=v['source'], **_metrics(v['usage'])) for k, v in sessions.items()], key=lambda r: -r['total_tokens']),
            'top_week_sessions': sorted([dict(session_id=k, title=titles.get(k) or '未命名对话', **_metrics(v)) for k, v in week_sessions.items()], key=lambda r: (-r['total_tokens'], r['session_id']))[:3],
            'week_range': {'start': week_start.isoformat(), 'end': today.isoformat()},
            'coverage': {'files': len(files), 'errors': errors, 'duplicates_removed': duplicates,
                         'note': '本机保留日志；按事件日期统计，旧格式缺失历史、未知模型及未落盘调用可能不完整'},
        }
