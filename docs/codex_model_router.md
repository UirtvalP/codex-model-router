# Codex Model Router MVP

This MVP calls Not Diamond's route-only `modelSelect` endpoint in front of a
Codex task. It selects a capability proxy, maps that proxy to a Codex model and
reasoning effort, and sends the original prompt unchanged to Codex. Not
Diamond never executes the task.

The default capability mapping is Haiku 4.5 → Luna, Sonnet 4.6 → Terra, Opus
4.7 → Sol, and Not Diamond's currently available Claude frontier proxy
(`claude-sonnet-5`) → Astra. The router recognizes `claude-fable-5` and
`claude-fable-5-1` as Astra proxies for when Not Diamond exposes them; set
`NOTDIAMOND_FRONTIER_PROXY` to the supported Fable model ID at that point.
Opus always maps to Sol and is never promoted by local keyword matching.

Up to 12,000 characters are sent to Not Diamond with content hashing enabled.
`--heuristic-only` keeps routing entirely local. A Not Diamond timeout or API
failure always falls back to Terra/medium.

The installed Codex CLI is queried for its Luna, Terra, Sol, and Astra catalog, so the
router only dispatches supported model/effort pairs. The non-sensitive catalog
is cached for six hours to keep agent routing lean. If classification or model
discovery fails, a conservative local heuristic takes over.

## Try It

Install an editable checkout to expose the cross-platform command:

```powershell
py -3 -m pip install -e .
```

Inspect a route without running the task:

```powershell
.\Start-Codex-Routed.ps1 -RouteOnly "Summarize these notes in three bullets."
```

Run a routed task:

```powershell
.\Start-Codex-Routed.ps1 "Review the current changes and identify real bugs."
```

Pass a sandbox explicitly when you want to override the normal Codex setting:

```powershell
.\Start-Codex-Routed.ps1 -Sandbox workspace-write "Fix the parser failure."
```

Route a follow-up onto the latest saved task:

```powershell
.\Start-Codex-Routed.ps1 -ResumeLast "Continue with the smallest useful test."
```

Use the zero-cost local fallback:

```powershell
.\Start-Codex-Routed.ps1 -RouteOnly -HeuristicOnly "Rename one local variable."
```

## Subagents

Codex/Work may execute its dedicated multi-agent tool through a specialized
path that does not run `PreToolUse`. In addition, `fork_turns: "all"` requires
the child to inherit the parent's model. Route immediately before the native
spawn on that path:

```powershell
py -3.12 -I -m codex_model_router --spawn-route --task-name "provider_review" --prompt "Review the provider boundary"
```

The JSON `spawn_input` object is ready to copy into `spawn_agent`; it contains
the normalized `task_name`, selected `model`, `reasoning_effort`, and
`fork_turns: "none"`. Task names are restricted to lowercase letters, digits,
and underscores, with incompatible characters normalized before spawning. This
route is logged as `agent_pre_spawn`. The user-level `agent-orchestration` Skill makes
this call automatically before each spawn and uses Terra/medium if routing
fails.

Install the user-level hook to intercept `spawn_agent` at the `PreToolUse`
boundary in every repository:

```powershell
py -3 -I -m codex_model_router --install-user-hook
```

On hook-capable execution paths it replaces `model` and `reasoning_effort` on
every spawn and changes a full-history fork to `fork_turns: "none"`. Values
filled by a parent Agent are treated as suggestions, so they cannot silently
bypass routing. Each invocation is routed independently, including recursive
child-agent spawns.

If the user personally names a child model, the parent adds
`[codex-router:preserve-model]` at the start of the child message and passes the
requested route fields. The hook strips that marker, preserves the requested
model/effort, skips Not Diamond, and still writes an `explicit-user-model`
record so the dashboard shows what the child actually runs.

Codex asks you to review and trust new hooks before they run. Start a new task
and use `/hooks` after installation. The installer merges rather than replacing
existing user hooks, pins the active Python interpreter, uses isolated mode to
prevent target-repository import shadowing, and backs up an existing file before
changing it. On Windows it also pins the system PowerShell executable and uses
an encoded inner command, preventing a target checkout from shadowing either
launcher. Static hook and plugin-hook configs are deliberately not shipped
because they cannot pin the user's Python interpreter at install time.

The hook does not expose or alter private chain-of-thought. Its intervention
point is the agent boundary: the subagent task description, model, and reasoning
effort.

Codex requires a `PreToolUse` rewrite to return `permissionDecision: "allow"`.
That allows the `spawn_agent` call whose arguments were rewritten; it does not
relax the child agent's sandbox, approvals, or downstream tool permissions.

## Safety And Privacy

- The route-only request receives up to 12,000 characters and uses
  `NOTDIAMOND_API_KEY`, content hashing, and a bounded timeout. It never
  executes Codex or another model itself. Use `--heuristic-only` to avoid the
  network request.
- The real task retains normal Codex configuration, approval rules, hooks, and
  sandbox. The router only passes a sandbox when you explicitly provide one.
- Raw prompts and free-text route explanations are never written to the router log. The default log is
  `%LOCALAPPDATA%\CodexModelRouter\decisions.jsonl` on Windows and
  `~/.codex/router/decisions.jsonl` elsewhere; it contains a non-sensitive task
  summary, hashes, structured features, proxy/session information, the decision,
  fallback errors, timings, and exit status.
- If the subagent hook fails, the original `spawn_agent` call proceeds unchanged.

High-consequence work involving external writes, deletion, money, credentials,
security, legal or medical decisions, persisted data, or public deployment is
forced to Sol/high or stronger and single-agent execution. Ultra is reserved
for safe root tasks with genuinely independent workstreams.

## Routing dashboard

Run `py -3 -m codex_model_router.dashboard` on Windows or
`python3 -m codex_model_router.dashboard` on macOS/Linux. The local-only page at
`http://127.0.0.1:8765/` refreshes every three seconds and summarizes model
distribution, cache hits, fallbacks, Not Diamond latency, and individual route
records. The server is read-only and never binds to a LAN interface.

## Existing Codex History

Codex installations may have local history under `~/.codex/history.jsonl`,
`~/.codex/sessions`, and `~/.codex/archived_sessions`. The MVP does not import
those files or treat the model that happened to run historically as the correct
label. Those records are useful for a later replay set, but need a judge or user
rating before they can answer the counterfactual question of which other model
would have been better.

## Feedback And Learning

Each decision prints or returns a `decision_id`. Label it after seeing the
result:

```powershell
py -3 .\tools\codex_route.py `
  --feedback DECISION_ID --rating good
```

Ratings are `good`, `underpowered`, `overkill`, and `failed`. They are appended
without prompt text. On the next route, an exact task reuses or adjusts its
previous route immediately: `underpowered` moves up, `overkill` moves down, and
`good` anchors the prior choice. For similar structured task categories, the
router waits for at least three labels and a two-thirds majority before
adjusting. Feedback is scoped to the same working directory and the same root/agent
surface, and it can never reverse direction relative to the fresh classifier
route. Safety floors are reapplied after every learned adjustment.
`failed` is retained for evaluation but does not automatically spend more,
because an execution failure is not necessarily a model-capability failure.

This is deliberately feedback learning, not fictional counterfactual learning:
old Codex transcripts say which model ran, but do not prove which untried model
would have been best. Existing history can become useful after replay/judge
evaluation adds those missing labels.

## Current Boundary

Root routing applies to tasks launched with `Start-Codex-Routed.ps1` or
`python -I -m codex_model_router` (also installed as `codex-model-router` when the
Python scripts directory is on `PATH`). A prompt already submitted in an open
Codex Desktop task cannot be retroactively moved to another model. Once the
user hook is installed and trusted, hook-capable spawned agents are routed
across repositories. Codex/Work specialized multi-agent calls use the
documented `--spawn-route` pre-spawn path instead.
