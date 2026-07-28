# Codex Model Router

An experimental, local-first router that chooses a Codex model, reasoning
effort, and safe delegation policy before a task runs.

It uses a small `gpt-5.6-luna` / `low` classification call, validates the
answer against the model catalog exposed by the installed Codex CLI, then
applies deterministic policy floors. High-consequence work is never allowed to
stay on a weak route, and Ultra is reserved for safe root tasks with genuinely
independent workstreams.

```text
prompt -> Luna/low classifier -> deterministic policy -> selected Codex route
```

This is an MVP, not an official OpenAI project.

## Routes

| Family | Intended role |
| --- | --- |
| Luna | Classification, formatting, bounded answers, tiny deterministic work |
| Terra | Normal implementation, debugging, review, and moderate research |
| Sol | Ambiguous, cross-cutting, high-consequence, or exceptionally hard work |
| Terra or Sol / Ultra | Safe root tasks that benefit from automatic parallel delegation |

Safety-sensitive work involving external writes, deletion, money, credentials,
security, legal or medical decisions, persisted data, or public deployment is
forced to Sol/high or stronger and single-agent execution.

## Requirements

- Python 3.9 or newer.
- Codex CLI 0.145.0 or newer, logged in with ChatGPT.
- Account access to the Luna, Terra, and Sol model families.

The CLI, package, and hook surfaces were validated against `codex-cli 0.145.0`.
Model availability is still account- and release-dependent.

## Quick Start

```bash
git clone https://github.com/Saadfk/codex-model-router.git
cd codex-model-router
python -m pip install .
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

Only unpinned `spawn_agent` calls are rewritten. If the parent provides a model
or reasoning effort, the router leaves the call unchanged. Spawned agents are
never assigned Ultra, avoiding recursive delegation.

## Feedback

Every decision has a `decision_id`. Label the outcome after inspecting the
result:

```bash
python -I -m codex_model_router --feedback DECISION_ID --rating good
```

Ratings are `good`, `underpowered`, `overkill`, and `failed`. Exact-task
feedback is applied immediately; similar structured tasks require at least
three labels and a two-thirds majority. Safety policy is reapplied after every
learned adjustment.

## Privacy And Cost

The default route is a two-call flow: up to 12,000 characters of the task are
sent to a separate Luna classifier before the original prompt is sent to the
selected model. That classifier call consumes Codex usage. Use
`--heuristic-only` when you do not want the extra model call.

The classifier runs ephemerally in an empty, read-only directory with the shell
tool, multi-agent delegation, web search, project rules, and session persistence
disabled. API key environment variables are removed so the saved ChatGPT login
is used.

Local decision logs do not contain raw prompts, classifier prose, or checkout
paths. They contain hashes, structured task features, the chosen route,
timings, and exit status. The default path is
`%LOCALAPPDATA%\CodexModelRouter\decisions.jsonl` on Windows and
`~/.codex/router/decisions.jsonl` elsewhere.

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
