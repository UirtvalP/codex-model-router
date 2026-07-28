# Codex Model Router MVP

This MVP puts a small, isolated `gpt-5.6-luna` call in front of a Codex task.
It selects a model, reasoning effort, and whether the root task warrants Ultra
delegation. A deterministic policy then raises weak or unsafe recommendations
before the original prompt is sent unchanged to the selected Codex model.

This is a two-call path: up to 12,000 characters of the task are sent to the
classifier first, and that call consumes Codex usage. `--heuristic-only` keeps
routing entirely local when that extra transmission or latency is undesirable.

The installed Codex CLI is queried for its Luna, Terra, and Sol catalog, so the
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

Install the user-level hook to intercept `spawn_agent` at the `PreToolUse`
boundary in every repository:

```powershell
py -3 -I -m codex_model_router --install-user-hook
```

For a fully unpinned subagent, it fills in `model` and `reasoning_effort`. If
the parent explicitly supplies either field, that spawn opts out of automatic
routing and proceeds unchanged. An automatically routed spawned agent is never
assigned Ultra, which avoids recursive delegation.

Codex asks you to review and trust new hooks before they run. Start a new task
and use `/hooks` after installation. The installer merges rather than replacing
existing user hooks, pins the active Python interpreter, uses isolated mode to
prevent target-repository import shadowing, and backs up an existing file before
changing it. A plugin bundle is also available under `.codex-plugin/plugin.json` and
`hooks/hooks.json`; plugin hook paths resolve through `PLUGIN_ROOT`.

The hook does not expose or alter private chain-of-thought. Its intervention
point is the agent boundary: the subagent task description, model, and reasoning
effort.

Codex requires a `PreToolUse` rewrite to return `permissionDecision: "allow"`.
That allows the `spawn_agent` call whose arguments were rewritten; it does not
relax the child agent's sandbox, approvals, or downstream tool permissions.

## Safety And Privacy

- The classifier runs in a disposable empty directory with read-only sandbox,
  the shell tool, multi-agent delegation, and web search disabled, no project
  rules, no persisted session, and ChatGPT login forced. API-key environment
  variables are removed from that child process.
- The classifier receives up to 12,000 characters of the task in a separate
  Codex request. Use `--heuristic-only` to avoid that extra request.
- The real task retains normal Codex configuration, approval rules, hooks, and
  sandbox. The router only passes a sandbox when you explicitly provide one.
- Raw prompts and the classifier's free-text reason are never written to the router log. The default log is
  `%LOCALAPPDATA%\CodexModelRouter\decisions.jsonl` on Windows and
  `~/.codex/router/decisions.jsonl` elsewhere; it contains hashes, structured
  features, the decision, timings, and exit status.
- If the subagent hook fails, the original `spawn_agent` call proceeds unchanged.

High-consequence work involving external writes, deletion, money, credentials,
security, legal or medical decisions, persisted data, or public deployment is
forced to Sol/high or stronger and single-agent execution. Ultra is reserved
for safe root tasks with genuinely independent workstreams.

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
user hook is installed and trusted, unpinned spawned agents are routed across
repositories.
