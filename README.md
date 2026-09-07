# Codex Model Router

An experimental, local-first router that chooses a Codex model, reasoning
effort, and safe delegation policy before a task runs.

By default, a fixed GPT-5.3 Codex Spark/low CLI evaluator chooses directly
among Luna, Terra, Sol, and Astra, then the selected Codex subscription model
runs the task. The evaluator uses the existing ChatGPT login. If evaluation
fails, the hook falls back to Terra/medium and records the failure.

```text
prompt -> Codex Spark evaluator (low) -> selected Codex model + reasoning effort
```

This is an MVP, not an official OpenAI project.

## Routes

| Family | Intended role |
| --- | --- |
| Luna | Classification, formatting, bounded answers, tiny deterministic work |
| Terra | Normal implementation, debugging, review, and moderate research |
| Sol | Ambiguous, cross-cutting, high-consequence, or exceptionally hard work |
| Astra | Most demanding reasoning and complex tasks |

The evaluator chooses the model and reasoning effort. Local validation checks
availability and supported effort without task-content-based model upgrades.
Safety metadata can restrict delegation; it does not force a stronger model.

## Requirements

- Python 3.9 or newer.
- Codex CLI 0.145.0 or newer, logged in with ChatGPT.
- Account access to the Luna, Terra, Sol, and Astra model families.
- The evaluator uses the existing ChatGPT login; no separate routing API key is needed.

The CLI, package, and hook surfaces were validated against `codex-cli 0.145.0`.
Model availability is still account- and release-dependent.

## Quick Start

```bash
git clone https://github.com/UirtvalP/codex-model-router.git
cd codex-model-router
python -m pip install .
codex login status
```

Windows PowerShell:

```powershell
git clone https://github.com/UirtvalP/codex-model-router.git
Set-Location codex-model-router
python -m pip install .
codex login status
```

macOS/Linux:

```bash
python3 -m pip install .
codex login status
```

Inspect a decision without launching the real task:

```bash
python -I -m codex_model_router --route-only --prompt "Summarize these notes in three bullets."
```

Run the routed task in another checkout:

```bash
python -I -m codex_model_router --cd /path/to/project --prompt "Review the current changes for real bugs."
```

Use the no-extra-model fallback:

```bash
python -I -m codex_model_router --route-only --heuristic-only --prompt "Rename one local variable."
```

Windows users can also run the source-checkout convenience launcher:

```powershell
.\Start-Codex-Routed.ps1 -RouteOnly "Summarize these notes in three bullets."
```

Package installation also exposes `codex-model-router` and `codex-route`
aliases when your Python scripts directory is on `PATH`. On Windows,
`py -3 -I -m codex_model_router` is the reliable module form.

## Route Subagents Globally

Codex currently has two subagent execution paths. The standard local-function
path can be rewritten by `PreToolUse`; the Codex/Work multi-agent path may be a
specialized path that does not run tool hooks. Full-history
`fork_turns: "all"` calls also must inherit the parent model. For reliable
Codex/Work routing, select the model immediately before `spawn_agent`:

```powershell
py -3.12 -I -m codex_model_router --spawn-route --task-name "provider_review" --prompt "Review the provider boundary"
```

```bash
python3 -I -m codex_model_router --spawn-route --task-name "provider_review" --prompt "Review the provider boundary"
```

Use the returned `spawn_input` fields in the native `spawn_agent` call. They
include the normalized `task_name`, routed `model`, `reasoning_effort`, and
`fork_turns: "none"` so the child can use a different model from its parent.
Task names are normalized to lowercase letters, digits, and underscores before
the native call. The global
`agent-orchestration` Skill automates this pre-spawn step and falls back to
Terra/medium if the command fails.

After installing the package, merge the router into your user-level Codex
hooks:

```bash
python -I -m codex_model_router --install-user-hook
```

The installer preserves all existing hooks, pins the hook to the Python
interpreter containing the package, and enables Python isolated mode so a
target repository cannot shadow the installed router module. It creates a
timestamped backup before changing an existing file and does nothing when the
exact router hook is already present. Start a new Codex task, open `/hooks`, and
review/trust the new command hook before it can run. Codex officially supports user hooks at
`~/.codex/hooks.json`; see the [Codex Hooks documentation](https://learn.chatgpt.com/docs/hooks).

The MVP deliberately does not ship a static hook or plugin hook: only the
installer can pin the exact Python and Windows PowerShell executables and avoid
launcher shadowing by an untrusted target checkout.

On the standard hook-capable path, every `spawn_agent` call is rewritten by
default, even when the parent Agent pre-filled `model` or `reasoning_effort`.
The hook also changes `fork_turns: "all"` to `"none"` when applying a routed
model. Every hook invocation routes independently, including recursive spawns.
The pre-spawn command provides the same independent routing on Codex/Work's
specialized multi-agent path.

When the user personally requests a particular child model, put
`[codex-router:preserve-model]` at the start of the child message and pass the
requested `model`/`reasoning_effort`. The hook preserves those fields, removes
the internal marker before the child sees the message, and records the route as
`explicit-user-model`. Other task arguments are never changed.

For the second-layer safety default, add this to `~/.codex/config.toml` (or the
equivalent `%USERPROFILE%/.codex/config.toml` path on Windows):

```toml
[agents]
default_subagent_model = "gpt-5.6-terra"
default_subagent_reasoning_effort = "medium"
```

The online evaluator is Codex only and keeps GPT-5.3 Codex Spark/low fixed.
Spark is used directly without `fast_mode` or `service_tier="fast"`, which are
not advertised for this model. `--heuristic-only` remains available for
explicit offline routing. No external router, proxy-model mapping, or
external-router cache is used.

## Feedback

Every decision has a `decision_id`. Label the outcome after inspecting the
result:

```bash
python -I -m codex_model_router --feedback DECISION_ID --rating good
```

Ratings are `good`, `underpowered`, `overkill`, and `failed`. Feedback-based
route adjustments apply only in explicit heuristic-only mode. Live evaluator
decisions are not silently upgraded using local feedback.

## Privacy And Cost

The default route sends a bounded task excerpt to a separate Codex evaluation
turn using the existing ChatGPT login. This consumes subscription usage and
adds model-call latency before the worker starts. No separate routing service receives the task.

The evaluator runs ephemerally in an empty, read-only directory. Hooks,
delegation, shell access, web search, plugins, and project rules are disabled.
API-key environment variables are removed. The evaluator is instructed only
to classify the task, and its response is validated before use.

Decision logs contain a controlled evaluation summary, selected route, evaluator
metadata, fallback errors, timings, task hashes, and structured task features.
Full prompts are not logged. The default path is
`%LOCALAPPDATA%\CodexModelRouter\decisions.jsonl` on Windows and
`~/.codex/router/decisions.jsonl` elsewhere.

New records expose an `evaluator` object alongside `codex` and `fallback`.
Historical `notdiamond` records remain readable. A selected model in the route
log is the requested execution model; it is not independent evidence of the
upstream model's identity.

## Local routing dashboard

Open the read-only dashboard, which refreshes every three seconds:

```powershell
py -3 -m codex_model_router.dashboard
```

```bash
python3 -m codex_model_router.dashboard
```

The dashboard listens only on `127.0.0.1:8765`. It shows total and 24-hour
routing volume, separate model distributions for each source, fallback rates,
selection latency, and evaluator-to-execution model choices. Historical cache
statistics remain readable for old records. It does not change
routing decisions or upload log data. Use `--no-browser`, `--port`, or
`--log-path` to customize startup.

## Boundaries

- Root routing works for tasks launched through this CLI or the PowerShell
  launcher. A prompt already submitted in Codex Desktop cannot be moved to a
  different root model retroactively.
- The hook intervenes at the agent boundary. It does not read, expose, or alter
  private chain-of-thought.
- Historical Codex transcripts show which model ran, not which untried model
  would have been best. Replay/judge evaluation is still needed for true
  counterfactual training labels.
- Model discovery currently uses `codex debug models --bundled`, with a cached
  catalog and a conservative fallback. That CLI surface may evolve.

More implementation detail is in [docs/codex_model_router.md](docs/codex_model_router.md).

## Development

```bash
python -m pip install -e .
python -m unittest discover -s tests -p "test_*.py" -v
python -I -m codex_model_router --help
```

CI covers Python 3.9 and 3.14 on Windows and Linux.

## License

No open-source license has been selected for this MVP yet.
