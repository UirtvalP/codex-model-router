"""Local, read-only web dashboard for Codex model-router decisions."""

from __future__ import annotations

import argparse
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter, deque
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Deque, Dict, List, Mapping, Optional

from .router import default_log_path
from .usage import build_usage_payload


DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 8765
DASHBOARD_NAME = "codex-model-router-dashboard"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _model_family(model: Any) -> str:
    lowered = str(model or "").lower()
    for family in ("astra", "sol", "terra", "luna"):
        if lowered.endswith("-" + family) or lowered == family:
            return family
    return "other"


def _route_view(record: Mapping[str, Any], feedback: Mapping[str, str]) -> Dict[str, Any]:
    codex = _mapping(record.get("codex"))
    notdiamond = _mapping(record.get("notdiamond"))
    evaluator = _mapping(record.get("evaluator"))
    cache = _mapping(record.get("cache"))
    fallback = _mapping(record.get("fallback"))
    route = _mapping(record.get("route"))
    model = codex.get("model") or route.get("model") or "unknown"
    effort = codex.get("reasoning_effort") or route.get("effort") or "unknown"
    source = route.get("source") or record.get("source") or "unknown"
    decision_id = str(record.get("decision_id") or "")
    task_name = record.get("task_name")
    task_summary = record.get("task_summary") or "No task summary"
    return {
        "decision_id": decision_id,
        "timestamp": record.get("timestamp"),
        "task": str(task_name or task_summary),
        "task_summary": str(task_summary),
        "proxy_model": notdiamond.get("proxy_model") or route.get("nd_proxy_model") or "none",
        "evaluator_model": evaluator.get("model") or "",
        "selector_model": evaluator.get("model") or notdiamond.get("proxy_model") or route.get("nd_proxy_model") or "none",
        "reason": str(evaluator.get("summary") or ""),
        "session_id": evaluator.get("thread_id") or notdiamond.get("session_id") or route.get("nd_session_id") or "none",
        "request_ms": evaluator.get("request_ms") if evaluator.get("request_ms") is not None else (notdiamond.get("request_ms") if notdiamond.get("request_ms") is not None else route.get("classifier_ms")),
        "model": str(model),
        "family": _model_family(model),
        "effort": str(effort),
        "surface": codex.get("surface") or route.get("surface") or "unknown",
        "source": source,
        "cache_hit": bool(cache.get("hit", route.get("cache_hit", False))),
        "cache_samples": cache.get("sample_count", route.get("cache_sample_count", 0)),
        "cache_similarity": cache.get("similarity", route.get("cache_similarity")),
        "fallback_used": bool(fallback.get("used", str(source).endswith("-fallback"))),
        "fallback_error": fallback.get("error") or evaluator.get("error") or route.get("nd_error"),
        "outcome": record.get("outcome") or "unknown",
        "execution_ms": record.get("execution_ms"),
        "rating": feedback.get(decision_id),
        "display": record.get("display") or "",
    }


def build_dashboard_payload(log_path: Path, limit: int = 1000, start: str = "", end: str = "") -> Dict[str, Any]:
    """Read routing JSONL and return a browser-safe dashboard payload."""

    first = date.fromisoformat(start) if start else None
    last = date.fromisoformat(end) if end else None
    if first and last and first > last:
        raise ValueError("start must not exceed end")
    filtered_models: Dict[str, Counter[str]] = {"codex-evaluator": Counter()}
    path = Path(log_path)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    recent_records: Deque[Mapping[str, Any]] = deque(maxlen=max(1, min(limit, 5000)))
    feedback: Dict[str, str] = {}
    model_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    models_by_source: Dict[str, Counter[str]] = {}
    total = last_24h = cache_hits = fallbacks = invalid_lines = 0
    latency_total = latency_count = 0
    latest_at: Optional[str] = None

    if path.exists():
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    invalid_lines += 1
                    continue
                if not isinstance(event, Mapping):
                    invalid_lines += 1
                    continue
                if event.get("event") == "route_feedback":
                    decision_id = event.get("decision_id")
                    rating = event.get("rating")
                    if decision_id and rating:
                        feedback[str(decision_id)] = str(rating)
                    continue
                if event.get("event") != "route_decision":
                    continue

                total += 1
                recent_records.append(event)
                view = _route_view(event, {})
                model_counts[view["family"]] += 1
                source_counts[str(view["source"])] += 1
                models_by_source.setdefault(str(view["source"]), Counter())[view["family"]] += 1
                cache_hits += int(view["cache_hit"])
                fallbacks += int(view["fallback_used"])
                timestamp = _parse_timestamp(view["timestamp"])
                local_day = timestamp.astimezone().date() if timestamp else None
                if (not first and not last) or (local_day is not None and
                        (first is None or local_day >= first) and (last is None or local_day <= last)):
                    filtered_models.setdefault(str(view["source"]), Counter())[view["family"]] += 1
                if timestamp is not None and timestamp >= cutoff:
                    last_24h += 1
                if timestamp is not None and (latest_at is None or str(view["timestamp"]) > latest_at):
                    latest_at = str(view["timestamp"])
                request_ms = view.get("request_ms")
                if isinstance(request_ms, (int, float)) and request_ms >= 0:
                    latency_total += float(request_ms)
                    latency_count += 1

    routes = [_route_view(record, feedback) for record in reversed(recent_records)]
    stat = path.stat() if path.exists() else None
    return {
        "name": DASHBOARD_NAME,
        "generated_at": now.isoformat(),
        "distribution_range": {"start": start, "end": end},
        "log": {
            "path": str(path.resolve()),
            "exists": path.exists(),
            "bytes": stat.st_size if stat else 0,
            "invalid_lines": invalid_lines,
            "showing": len(routes),
            "truncated": total > len(routes),
        },
        "stats": {
            "total": total,
            "last_24h": last_24h,
            "cache_hits": cache_hits,
            "cache_rate": round(cache_hits * 100.0 / total, 1) if total else 0.0,
            "fallbacks": fallbacks,
            "fallback_rate": round(fallbacks * 100.0 / total, 1) if total else 0.0,
            "avg_request_ms": round(latency_total / latency_count) if latency_count else None,
            "latest_at": latest_at,
            "models": dict(model_counts),
            "sources": dict(source_counts),
            "filtered_models_by_source": {source: dict(counts) for source, counts in filtered_models.items()},
            "models_by_source": {source: dict(counts) for source, counts in models_by_source.items()},
        },
        "routes": routes,
    }


DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Codex 用量与路由面板</title>
  <style>
    :root{--bg:#080b12;--panel:#111722;--panel2:#151d2b;--line:#263247;--text:#edf3ff;--muted:#91a0b8;--accent:#75a7ff;--luna:#65d6ad;--terra:#65b9ff;--sol:#b58cff;--astra:#ffbd66;--bad:#ff6f7d;--good:#66dda0}
    *{box-sizing:border-box} body{margin:0;background:radial-gradient(circle at 15% 0,#18233a 0,transparent 28%),var(--bg);color:var(--text);font:14px/1.45 Inter,Segoe UI,Arial,sans-serif}
    .shell{max-width:1500px;margin:auto;padding:28px}.top{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:24px}.brand h1{font-size:26px;margin:0 0 6px;letter-spacing:-.4px}.subtitle,.muted{color:var(--muted)}
    .status{display:flex;align-items:center;gap:10px;background:#111927cc;border:1px solid var(--line);padding:10px 14px;border-radius:12px}.dot{width:9px;height:9px;border-radius:50%;background:var(--good);box-shadow:0 0 12px var(--good)}
    button,select,input{font:inherit;color:var(--text);background:#0d1420;border:1px solid var(--line);border-radius:9px;padding:9px 11px;outline:none}button{cursor:pointer}button:hover{border-color:var(--accent)}
    .cards{display:grid;grid-template-columns:repeat(5,minmax(150px,1fr));gap:14px;margin-bottom:18px}.card,.panel{background:linear-gradient(145deg,#141c2a,#0f1520);border:1px solid var(--line);border-radius:15px;box-shadow:0 16px 40px #0003}.card{padding:18px}.card .label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}.card .value{font-size:28px;font-weight:730;margin-top:8px}.card .hint{font-size:12px;color:var(--muted);margin-top:3px}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:18px}.panel{padding:18px}.panel h2{font-size:15px;margin:0 0 16px}.bars{display:grid;gap:12px}.bar-row{display:grid;grid-template-columns:66px 1fr 110px;align-items:center;gap:10px}.bar-track{height:9px;border-radius:10px;background:#090d14;overflow:hidden}.bar-fill{height:100%;border-radius:10px}.bar-count{text-align:right;color:var(--muted)}
    .grid>article{min-width:0}
    .token-unit{font-size:12px;font-weight:400;color:var(--muted);margin-left:5px}.chart-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr) minmax(0,1.5fr);gap:16px;margin:20px 0}.chart-box{border:1px solid var(--line);border-radius:12px;padding:16px;min-width:0}.trend{display:flex;gap:12px;height:215px;align-items:stretch;overflow-x:auto}.trend-col{flex:1;min-width:62px;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;gap:8px}.trend-bar{width:65%;max-width:55px;border-radius:5px 5px 0 0;background:var(--accent);min-height:2px}.chart-label{font-size:11px;color:var(--muted);white-space:nowrap}.model-chart-row{display:grid;grid-template-columns:150px 1fr 110px;align-items:center;gap:12px;margin:14px 0}.donut{width:142px;height:142px;border-radius:50%;display:grid;place-items:center;margin:12px auto}.donut-center{width:112px;height:112px;border-radius:50%;background:var(--panel);display:flex;flex-direction:column;align-items:center;justify-content:center;font-size:23px;font-weight:700}.legend{display:flex;gap:12px;justify-content:center;flex-wrap:wrap;font-size:12px;color:var(--muted)}.legend-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px}
    @media(max-width:1100px){.chart-grid{grid-template-columns:1fr}.chart-grid .panel{height:100%}}
    @media(max-width:800px){.model-chart-row{grid-template-columns:110px 1fr 95px}}
    .filters{display:grid;grid-template-columns:minmax(220px,1fr) repeat(4,150px) auto;gap:10px;margin-bottom:13px}.table-panel{padding:0;overflow:hidden}.table-head{padding:18px 18px 0}.scroll{overflow:auto;max-height:56vh}table{width:100%;border-collapse:collapse;min-width:1100px}th{position:sticky;top:0;background:#111925;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em;text-align:left;padding:11px 13px;border-bottom:1px solid var(--line);z-index:1}td{padding:12px 13px;border-bottom:1px solid #202a3a;vertical-align:top}tbody tr:hover{background:#172131}.task{max-width:280px}.task strong{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.small{font-size:12px;color:var(--muted)}
    .pill{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--line);background:#0b111b;border-radius:99px;padding:4px 8px;font-size:12px;white-space:nowrap}.family-luna{color:var(--luna)}.family-terra{color:var(--terra)}.family-sol{color:var(--sol)}.family-astra{color:var(--astra)}.bad{color:var(--bad)}.good{color:var(--good)}
    .empty{padding:50px;text-align:center;color:var(--muted)}.footer{display:flex;justify-content:space-between;gap:14px;margin-top:12px;color:var(--muted);font-size:12px;word-break:break-all}.error{margin:0 0 14px;background:#341720;border:1px solid #71313d;color:#ffadb5;padding:12px;border-radius:10px;display:none}
    @media(max-width:1050px){.cards{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:1fr}.filters{grid-template-columns:1fr 1fr}.top{flex-direction:column}}@media(max-width:600px){.shell{padding:16px}.cards{grid-template-columns:1fr}.filters{grid-template-columns:1fr}}
  </style>
</head>
<body>
<main class="shell">
  <header class="top"><div class="brand"><h1>Codex 用量与路由面板</h1><div class="subtitle">本机任务与子 agent 的 Token 用量，以及模型路由记录</div></div><div class="status"><span class="dot" id="dot"></span><span id="status">正在连接</span><button id="refresh">立即刷新</button></div></header>
  <div id="error" class="error"></div>
  <section class="panel" style="margin-bottom:20px">
    <h2>本机 Codex · Token 用量</h2>
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px">
      <button data-period="today">今天</button><button data-period="week">最近 7 天</button><button data-period="all">全部记录</button>
      <label>从 <input type="date" id="usageStart"></label><label>至 <input type="date" id="usageEnd"></label><button id="usageApply">查询</button>
    </div>
    <div id="usageStatus" class="muted" role="status">正在读取本机会话用量…</div>
    <div class="cards" id="usageCards" style="margin-top:16px"></div>
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px"><h2 style="margin:0">用量图表</h2><select id="usageMetric" aria-label="图表指标"><option value="total_tokens">总用量（输入 + 输出）</option><option value="input_tokens">输入 Token</option><option value="output_tokens">输出 Token</option><option value="uncached_input_tokens">未缓存输入</option></select></div>
    <div class="chart-grid"><article class="chart-box"><h2>每日用量趋势</h2><div id="usageTrend" class="trend"></div><div id="usageTrendLegend" class="legend" style="margin-top:14px"></div><div id="usageDayDetail" class="small" style="margin-top:14px" aria-live="polite"></div></article><article class="chart-box"><h2>输入缓存占比</h2><div id="usageCacheChart"></div></article><div id="usageRouteOverview"></div></div>
    <article class="chart-box" style="margin-bottom:18px"><h2>模型用量对比</h2><div id="usageModelChart"></div></article>
    <details><summary>查看精确数据（单位：Token）</summary><div class="grid" style="margin-top:16px"><article><h2>按模型</h2><div id="usageModels" class="scroll"></div></article><article><h2>按日期</h2><div id="usageDays" class="scroll"></div></article></div></details>
    <article class="chart-box" style="margin:18px 0"><h2>最近 7 天 · 用量最高的 3 条会话</h2><div class="small" id="usageTopRange"></div><div id="usageTopSessions"></div></article><details><summary>按对话查看</summary><div id="usageSessions" class="scroll" style="margin-top:12px"></div></details>
    <p class="small">缓存命中包含在输入中，推理包含在输出中，不重复相加。仅统计本机保留的日志，不等同于账号额度或费用。</p>
  </section>
  <h2 style="font-size:18px">模型路由</h2>
  <section class="cards">
    <article class="card"><div class="label">全部路由</div><div class="value" id="total">—</div><div class="hint" id="shown">读取日志中</div></article>
    <article class="card"><div class="label">最近 24 小时</div><div class="value" id="day">—</div><div class="hint">每次 spawn 独立计数</div></article>
    <article class="card"><div class="label">缓存命中率</div><div class="value" id="cacheRate">—</div><div class="hint" id="cacheCount">稳定相似任务</div></article>
    <article class="card"><div class="label">回退率</div><div class="value" id="fallbackRate">—</div><div class="hint" id="fallbackCount">评估失败回退 Terra</div></article>
    <article class="card"><div class="label">选模平均延迟</div><div class="value" id="latency">—</div><div class="hint">不含 Codex 执行时间</div></article>
  </section>
  <section class="grid" id="sourceModelPanels" aria-label="各来源模型分布"></section>
  <section class="panel table-panel">
    <div class="table-head"><div class="filters"><input id="search" placeholder="搜索任务、模型、session"><select id="family"><option value="">全部模型</option></select><select id="source"><option value="">全部来源</option></select><select id="cache"><option value="">全部缓存</option><option value="hit">缓存命中</option><option value="miss">实时路由</option></select><select id="fallback"><option value="">全部状态</option><option value="yes">仅回退</option><option value="no">无回退</option></select><button id="clear">清除筛选</button></div></div>
    <div class="scroll"><table><thead><tr><th>时间 / 任务</th><th>评估器 / 历史代理</th><th>Codex 执行模型</th><th>来源</th><th>缓存</th><th>回退</th><th>选模延迟</th><th>评估摘要 / Session</th><th>结果</th></tr></thead><tbody id="rows"></tbody></table><div id="empty" class="empty" hidden>没有符合筛选条件的路由记录</div></div>
  </section>
  <footer class="footer"><span id="logPath"></span><span id="updated"></span></footer>
</main>
<script>
let payload={routes:[],stats:{},log:{}};
const $=id=>document.getElementById(id); const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pct=v=>`${Number(v||0).toFixed(1)}%`; const number=v=>new Intl.NumberFormat('zh-CN').format(v||0);
function familyName(v){return ({luna:'Luna',terra:'Terra',sol:'Sol',astra:'Astra',other:'Other'})[v]||v}
function localTime(v){if(!v)return '—'; const d=new Date(v); return Number.isNaN(d.getTime())?'—':d.toLocaleString('zh-CN',{hour12:false})}
function renderModelDistributions(groups,range={}){const entries=Object.entries(groups||{}).sort((a,b)=>a[0].localeCompare(b[0])), names={'codex-evaluator':'Codex 评估器','notdiamond':'Not Diamond'}; $('sourceModelPanels').innerHTML=entries.map(([source,counts])=>{const total=Object.values(counts).reduce((sum,v)=>sum+v,0), families=['luna','terra','sol','astra',...Object.keys(counts).filter(k=>!['luna','terra','sol','astra'].includes(k))];return `<article class="panel" data-source="${esc(source)}"><h2>${esc(names[source]||source)} · 模型分布</h2><div class="small" style="margin-bottom:16px">${esc(range.start||range.end?`${range.start||'最早'} 至 ${range.end||'最新'}`:'全部日期')} · 共 ${number(total)} 次</div><div class="bars">${families.map(k=>{const v=counts[k]||0, share=total?v/total*100:0;return `<div class="bar-row"><span class="family-${esc(k)}">${esc(familyName(k))}</span><div class="bar-track"><div class="bar-fill" style="width:${share}%;background:var(--${['luna','terra','sol','astra'].includes(k)?k:'accent'})"></div></div><span class="bar-count">${number(v)} · ${pct(share)}</span></div>`}).join('')}</div></article>`}).join('')||'<article class="panel muted">暂无模型分布数据</article>';const evaluatorPanel=[...$('sourceModelPanels').children].find(el=>el.dataset.source==='codex-evaluator');$('usageRouteOverview').replaceChildren();if(evaluatorPanel)$('usageRouteOverview').appendChild(evaluatorPanel)}
function optionValues(id,values,label){const select=$(id), old=select.value, unique=[...new Set(values.filter(Boolean))].sort(); select.innerHTML=`<option value="">${label}</option>`+unique.map(v=>`<option value="${esc(v)}">${esc(id==='family'?familyName(v):v)}</option>`).join(''); select.value=old}
function render(){const s=payload.stats||{}, log=payload.log||{}; $('total').textContent=number(s.total); $('day').textContent=number(s.last_24h); $('cacheRate').textContent=pct(s.cache_rate); $('cacheCount').textContent=`${number(s.cache_hits)} 次命中`; $('fallbackRate').textContent=pct(s.fallback_rate); $('fallbackCount').textContent=`${number(s.fallbacks)} 次回退`; $('latency').textContent=s.avg_request_ms==null?'—':`${number(s.avg_request_ms)} ms`; $('shown').textContent=`显示最近 ${number(log.showing)} 条`; $('logPath').textContent=`日志：${log.path||'未找到'}`; $('updated').textContent=`更新：${localTime(payload.generated_at)}`;
renderModelDistributions(s.filtered_models_by_source||s.models_by_source,payload.distribution_range); optionValues('family',payload.routes.map(r=>r.family),'全部模型'); optionValues('source',payload.routes.map(r=>r.source),'全部来源'); renderRows()}
function renderRows(){const q=$('search').value.trim().toLowerCase(), family=$('family').value, source=$('source').value, cache=$('cache').value, fallback=$('fallback').value; const routes=payload.routes.filter(r=>{const hay=[r.task,r.task_summary,r.selector_model,r.reason,r.model,r.session_id,r.decision_id].join(' ').toLowerCase(); return(!q||hay.includes(q))&&(!family||r.family===family)&&(!source||r.source===source)&&(!cache||(cache==='hit')===r.cache_hit)&&(!fallback||(fallback==='yes')===r.fallback_used)}); $('empty').hidden=routes.length>0; $('rows').innerHTML=routes.map(r=>`<tr><td class="task"><strong title="${esc(r.task)}">${esc(r.task)}</strong><span class="small">${esc(localTime(r.timestamp))}</span></td><td><span class="pill">${esc(r.selector_model)}</span></td><td><span class="pill family-${esc(r.family)}">${esc(r.model)}</span><div class="small">${esc(r.effort)}</div></td><td>${esc(r.source)}</td><td class="${r.cache_hit?'good':''}">${r.cache_hit?`命中 · ${number(r.cache_samples)} 样本`:'实时'}</td><td class="${r.fallback_used?'bad':'good'}" title="${esc(r.fallback_error||'')}">${r.fallback_used?'是':'否'}</td><td>${r.request_ms==null?'—':`${number(r.request_ms)} ms`}</td><td><div class="small">${esc(r.reason)}</div><span class="small" title="${esc(r.session_id)}">${esc(r.session_id==='none'?'—':r.session_id.slice(0,12)+'…')}</span></td><td>${esc(r.rating||r.outcome||'—')}</td></tr>`).join('')}
let routeSeq=0;async function load(){const seq=++routeSeq;const query=new URLSearchParams({limit:'1000',start:$('usageStart').value,end:$('usageEnd').value});try{const res=await fetch(`api/routes?${query}`,{cache:'no-store'});if(!res.ok)throw new Error(`HTTP ${res.status}`);const next=await res.json();if(seq!==routeSeq)return;payload=next;$('error').style.display='none';$('dot').style.background='var(--good)';$('status').textContent='实时 · 每 3 秒刷新';render()}catch(e){if(seq!==routeSeq)return;$('error').textContent=`面板读取失败：${e.message}`;$('error').style.display='block';$('dot').style.background='var(--bad)';$('status').textContent='连接异常'}}
['search','family','source','cache','fallback'].forEach(id=>$(id).addEventListener(id==='search'?'input':'change',renderRows));$('clear').onclick=()=>{['search','family','source','cache','fallback'].forEach(id=>$(id).value='');renderRows()};$('refresh').onclick=load;load();setInterval(load,3000);

let usageSeq=0, usageData=null, selectedUsageDay=null;
function tokenNumber(value){const n=Number(value)||0;return n>=1e8?`${(n/1e8).toFixed(2)} 亿`:n>=1e4?`${(n/1e4).toFixed(2)} 万`:number(n)}
function modelColor(model){const family=String(model).split('-').pop(),colors={luna:'#65d6ad',terra:'#65b9ff',sol:'#b58cff',astra:'#ffbd66',spark:'#ff7e9d'};if(colors[family])return colors[family];let hash=0;for(const c of String(model))hash=(hash*31+c.charCodeAt(0))>>>0;return `hsl(${hash%360} 60% 65%)`}
function showUsageDay(day){selectedUsageDay=day;const r=(usageData?.by_day||[]).find(r=>r.date===day),k=$('usageMetric').value;if(!r){$('usageDayDetail').textContent='';return}$('usageDayDetail').innerHTML=`<strong>${esc(day)} · ${esc($('usageMetric').selectedOptions[0].textContent)}</strong>`+(r.models||[]).map(m=>`<div style="display:flex;justify-content:space-between;gap:12px;margin-top:6px"><span><i class="legend-dot" style="background:${modelColor(m.model)}"></i>${esc(m.model)}</span><span title="${number(m[k])} Token">${tokenNumber(m[k])} Token</span></div>`).join('')}
function renderUsageCharts(){if(!usageData)return;const d=usageData,k=$('usageMetric').value,days=[...(d.by_day||[])].sort((a,b)=>a.date.localeCompare(b.date)),models=[...(d.by_model||[])].sort((a,b)=>(b[k]||0)-(a[k]||0));const max=Math.max(1,...days.map(r=>r[k]||0));$('usageTrend').innerHTML=days.map(r=>{const breakdown=[...(r.models||[])].sort((a,b)=>a.model.localeCompare(b.model));return `<div class="trend-col" role="button" tabindex="0" data-day="${esc(r.date)}" aria-label="查看 ${esc(r.date)} 各模型用量" title="${esc(r.date)}：${number(r[k])} Token"><span class="chart-label">${tokenNumber(r[k])}</span><div class="trend-bar" style="height:${Math.max(0,(r[k]||0)/max)*145}px;display:flex;flex-direction:column-reverse;overflow:hidden;background:transparent">${breakdown.map(m=>`<div style="height:${r[k]?Math.max(0,(m[k]||0)/r[k])*100:0}%;background:${modelColor(m.model)};flex-shrink:0" title="${esc(m.model)}：${number(m[k])} Token"></div>`).join('')}</div><span class="chart-label">${esc(r.date.slice(5))}</span></div>`}).join('')||'<span class="muted">所选时间内没有记录</span>';$('usageTrendLegend').innerHTML=[...models].sort((a,b)=>a.model.localeCompare(b.model)).map(m=>`<span><i class="legend-dot" style="background:${modelColor(m.model)}"></i>${esc(m.model)}</span>`).join('');$('usageTrend').querySelectorAll('[data-day]').forEach(el=>{el.onclick=()=>showUsageDay(el.dataset.day);el.onmouseenter=()=>showUsageDay(el.dataset.day);el.onfocus=()=>showUsageDay(el.dataset.day);el.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();showUsageDay(el.dataset.day)}}});showUsageDay(days.some(r=>r.date===selectedUsageDay)?selectedUsageDay:days.at(-1)?.date);
const modelMax=Math.max(1,...models.map(r=>r[k]||0));$('usageModelChart').innerHTML=models.map(r=>`<div class="model-chart-row" title="${esc(r.model)}：${number(r[k])} Token"><span class="small">${esc(r.model)}</span><div class="bar-track"><div class="bar-fill" style="width:${Math.max(0,(r[k]||0)/modelMax)*100}%;background:${modelColor(r.model)}"></div></div><span class="chart-label" style="text-align:right">${tokenNumber(r[k])} Token</span></div>`).join('')||'<span class="muted">所选时间内没有记录</span>';const a=d.summary||{},rate=Math.max(0,Math.min(100,(a.cache_hit_rate||0)*100));$('usageCacheChart').innerHTML=`<div class="donut" style="background:conic-gradient(var(--luna) ${rate}%,var(--line) 0)" title="缓存命中 ${number(a.cached_input_tokens)} / 输入 ${number(a.input_tokens)} Token"><div class="donut-center">${a.input_tokens?pct(rate):'—'}<span class="small">输入缓存命中率</span></div></div><div class="legend"><span><i class="legend-dot" style="background:var(--luna)"></i>命中 ${tokenNumber(a.cached_input_tokens)}</span><span><i class="legend-dot" style="background:var(--line)"></i>未缓存 ${tokenNumber(a.uncached_input_tokens)}</span></div>`}

function usageTable(rows,key){return `<table style="min-width:550px"><thead><tr><th>${key==='model'?'模型':key==='date'?'日期':'对话标题'}</th><th>输入</th><th>缓存命中</th><th>输出</th><th>命中率</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${esc(key==='session_id'?(r.title||'未命名对话'):(r[key]||'unknown'))}</td><td>${number(r.input_tokens)}</td><td>${number(r.cached_input_tokens)}</td><td>${number(r.output_tokens)}</td><td>${pct((r.cache_hit_rate||0)*100)}</td></tr>`).join('')||'<tr><td colspan="5">所选时间内没有记录</td></tr>'}</tbody></table>`}
async function loadUsage(){load();const seq=++usageSeq;const q=new URLSearchParams({start:$('usageStart').value,end:$('usageEnd').value});$('usageStatus').textContent='正在统计本地日志…';try{const res=await fetch(`api/usage?${q}`,{cache:'no-store'});if(!res.ok)throw new Error(`HTTP ${res.status}`);const d=await res.json();if(seq!==usageSeq)return;usageData=d;renderUsageCharts();const s=d.summary||{};const cards=[['输入 Token',s.input_tokens,'含缓存命中输入'],['缓存命中',s.cached_input_tokens,`命中率 ${pct((s.cache_hit_rate||0)*100)}`],['未缓存输入',s.uncached_input_tokens,'输入减去缓存命中'],['输出 Token',s.output_tokens,'含推理输出'],['推理 Token',s.reasoning_output_tokens,'输出中的推理部分']];$('usageCards').innerHTML=cards.map(([label,value,hint])=>`<article class="card"><div class="label">${label}</div><div class="value" style="font-size:26px" title="${number(value)} Token">${tokenNumber(value)}<span class="token-unit">Token</span></div><div class="hint">${hint}</div></article>`).join('');$('usageModels').innerHTML=usageTable(d.by_model||[],'model');$('usageDays').innerHTML=usageTable(d.by_day||[],'date');$('usageSessions').innerHTML=usageTable(d.by_session||[],'session_id');$('usageTopRange').textContent=`${d.week_range?.start||''} 至 ${d.week_range?.end||''} · 按输入 + 输出排序，不受上方日期筛选影响`;$('usageTopSessions').innerHTML=(d.top_week_sessions||[]).map((r,i)=>`<div style="display:flex;align-items:center;gap:12px;padding:14px 0;border-bottom:1px solid var(--line)"><span class="pill">${i+1}</span><div style="flex:1;min-width:0"><strong style="overflow-wrap:anywhere">${esc(r.title||'未命名对话')}</strong><div class="small">输入 ${tokenNumber(r.input_tokens)} · 输出 ${tokenNumber(r.output_tokens)} Token</div></div><span title="${number(r.total_tokens)} Token" style="white-space:nowrap">${tokenNumber(r.total_tokens)} Token</span></div>`).join('')||'<p class="muted">最近 7 天没有记录</p>';$('usageStatus').textContent=`输入 + 输出：${tokenNumber(s.total_tokens)} Token · 缓存写入：${tokenNumber(s.cache_write_input_tokens)} Token · ${d.coverage?.note||'按请求增量统计，已去除重复记录'}${d.coverage?.errors? ' · 存在无法读取或解析的记录，请留意统计可能不完整':''}`;}catch(e){if(seq===usageSeq)$('usageStatus').textContent=`用量读取失败：${e.message}`}}
function localDate(d){return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`}
function usagePeriod(p){let today=new Date(),start=new Date();if(p==='week')start.setDate(start.getDate()-6);$('usageStart').value=p==='all'?'':localDate(start);$('usageEnd').value=p==='all'?'':localDate(today);loadUsage()}
$('usageMetric').onchange=renderUsageCharts;document.querySelectorAll('[data-period]').forEach(b=>b.onclick=()=>usagePeriod(b.dataset.period));$('usageApply').onclick=loadUsage;usagePeriod('today');setInterval(loadUsage,30000);
</script></body></html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    log_path = default_log_path()

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self._send(200, "text/html; charset=utf-8", DASHBOARD_HTML.encode("utf-8"))
            return
        if parsed.path == "/healthz":
            body = json.dumps({"name": DASHBOARD_NAME, "ok": True}).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
            return
        if parsed.path == "/api/usage":
            query = urllib.parse.parse_qs(parsed.query)
            try:
                payload = build_usage_payload(
                    Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"),
                    self.log_path,
                    start=query.get("start", [""])[0], end=query.get("end", [""])[0],
                )
                self._send(200, "application/json; charset=utf-8",
                           json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            except ValueError:
                self._send(400, "application/json; charset=utf-8", b'{"error":"Invalid date range"}')
            except OSError:
                self._send(500, "application/json; charset=utf-8", b'{"error":"Cannot read local usage logs"}')
            return
        if parsed.path == "/api/routes":
            query = urllib.parse.parse_qs(parsed.query)
            try:
                limit = max(1, min(5000, int(query.get("limit", ["1000"])[0])))
            except ValueError:
                limit = 1000
            try:
                payload = build_dashboard_payload(self.log_path, limit=limit,
                    start=query.get("start", [""])[0], end=query.get("end", [""])[0])
            except ValueError:
                self._send(400, "application/json; charset=utf-8", b'{"error":"Invalid date range"}')
                return
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
            return
        self._send(404, "text/plain; charset=utf-8", "Not found".encode("utf-8"))

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def dashboard_is_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            "http://{0}:{1}/healthz".format(DASHBOARD_HOST, port), timeout=0.7
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("name") == DASHBOARD_NAME and payload.get("ok") is True
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False


def serve_dashboard(log_path: Path, port: int = DASHBOARD_PORT, open_browser: bool = True) -> None:
    url = "http://{0}:{1}/".format(DASHBOARD_HOST, port)
    if dashboard_is_running(port):
        if open_browser:
            webbrowser.open(url)
        return
    DashboardHandler.log_path = Path(log_path)
    server = ThreadingHTTPServer((DASHBOARD_HOST, port), DashboardHandler)
    if open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Open the local Codex routing dashboard.")
    parser.add_argument("--log-path", type=Path, default=default_log_path())
    parser.add_argument("--port", type=int, default=DASHBOARD_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    serve_dashboard(args.log_path, port=args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
