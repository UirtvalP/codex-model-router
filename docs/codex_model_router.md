# Codex Model Router MVP

The online routing path uses a fixed Terra/low Codex CLI evaluator with Fast enabled with the existing
ChatGPT login. It chooses Luna, Terra, Sol, or Astra directly and returns a
model, reasoning effort, and short reason. Evaluation failure falls back to
Terra/medium and is visible in the log. No local task-content rule upgrades
the selected model.

There is no external router backend or proxy-model mapping. `--heuristic-only`
provides explicit offline evaluation. The installed CLI catalog supplies the
allowed model/effort pairs and is cached for six hours; routing decisions are
not reused from the removed external-router cache.

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
model/effort, skips evaluation, and still writes an `explicit-user-model`
record so the dashboard shows the requested child route.

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

- The default evaluator receives a bounded task excerpt in an ephemeral,
  read-only Codex process with hooks, shell, delegation, plugins, and rules
  disabled. It consumes subscription usage and adds a model-call delay.
  Use `--heuristic-only` to avoid the network call.
- The real task retains normal Codex configuration, approval rules, hooks, and
  sandbox. The router only passes a sandbox when you explicitly provide one.
- Raw prompts are not written to the router log; only a controlled evaluation summary is stored. The default log is
  `%LOCALAPPDATA%\CodexModelRouter\decisions.jsonl` on Windows and
  `~/.codex/router/decisions.jsonl` elsewhere; it contains a non-sensitive task
  summary, hashes, structured features, evaluator/session information, the decision,
  fallback errors, timings, and exit status.
- If the subagent hook fails, the original `spawn_agent` call proceeds unchanged.

Safety metadata can restrict orchestration and unsupported effort values are
normalized. These checks do not impose content-based model upgrades.

## Routing dashboard

Run `py -3 -m codex_model_router.dashboard` on Windows or
`python3 -m codex_model_router.dashboard` on macOS/Linux. The local-only page at
`http://127.0.0.1:8765/` refreshes every three seconds and summarizes model
distribution, cache hits, fallbacks, evaluation latency and controlled summaries, and individual route
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
without prompt text. Automatic feedback adjustments apply only to explicit
heuristic-only mode, never to live evaluator decisions. An execution
failure alone does not prove the selected model was underpowered.

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
