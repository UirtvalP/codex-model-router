# Codex Model Router

An experimental, local-first router that chooses a Codex model, reasoning
effort, and safe delegation policy before a task runs.

It uses Not Diamond's route-only `/v2/modelRouter/modelSelect` endpoint to
choose a capability tier, then uses the selected Codex subscription model for
the real work. Not Diamond never executes the task. If the route request fails,
the hook falls back to Terra/medium.

```text
prompt -> Not Diamond modelSelect -> capability mapping -> Codex subscription
```

This is an MVP, not an official OpenAI project.

## Routes

| Family | Intended role |
| --- | --- |
| Luna | Classification, formatting, bounded answers, tiny deterministic work |
| Terra | Normal implementation, debugging, review, and moderate research |
| Sol | Ambiguous, cross-cutting, high-consequence, or exceptionally hard work |
| Astra | Frontier proxy for the hardest tasks |

Safety-sensitive work involving external writes, deletion, money, credentials,
security, legal or medical decisions, persisted data, or public deployment is
forced to Sol/high or stronger and single-agent execution.

## Requirements

- Python 3.9 or newer.
- Codex CLI 0.145.0 or newer, logged in with ChatGPT.
- Account access to the Luna, Terra, Sol, and Astra model families.
- A Not Diamond API key in `NOTDIAMOND_API_KEY`.

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
$env:NOTDIAMOND_API_KEY = "your-key"
codex login status
```

macOS/Linux:

```bash
export NOTDIAMOND_API_KEY="your-key"
python3 -m pip install .
codex login status
```

Use a persistent user environment variable only if desired (`setx
NOTDIAMOND_API_KEY "your-key"` on Windows, or your shell profile on
macOS/Linux). Never put the key in this repository.

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

The capability proxies are Haiku 4.5 → Luna, Sonnet 4.6 → Terra, Opus 4.7 →
Sol, and the highest Claude proxy currently exposed by Not Diamond → Astra.
At present that frontier proxy is `claude-sonnet-5`. The router also recognizes
`claude-fable-5` and `claude-fable-5-1` as Astra proxies; when Not Diamond adds
one to your model catalog, select it with
`NOTDIAMOND_FRONTIER_PROXY=claude-fable-5-1`. Opus always maps to Sol and is
never promoted to Astra by local keywords.
`NOTDIAMOND_COST_QUALITY_TRADEOFF` defaults to `1` and accepts `0`–`10`;
`NOTDIAMOND_TIMEOUT_SECONDS` defaults to `8`.

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

The default route sends up to 12,000 characters to Not Diamond's route-only
endpoint with `hash_content=true`, then sends the unchanged original prompt to
the selected Codex model. The Not Diamond response contains a `session_id` for
diagnostics and feedback. Use `--heuristic-only` to avoid the network route.

The classifier runs ephemerally in an empty, read-only directory with the shell
tool, multi-agent delegation, web search, project rules, and session persistence
disabled. API key environment variables are removed so the saved ChatGPT login
is used.

Local decision logs do not contain raw prompts, classifier prose, or checkout
paths. They contain a non-sensitive task summary, hashes, structured task
features, the Not Diamond proxy and session ID, the mapped route, fallback
errors, timings, and exit status. The default path is
`%LOCALAPPDATA%\CodexModelRouter\decisions.jsonl` on Windows and
`~/.codex/router/decisions.jsonl` elsewhere.

Each JSONL record also includes a top-level `display` line plus explicit
`notdiamond`, `codex`, and `fallback` objects, so the selected proxy, actual
Codex model, reasoning effort, session ID, and failure reason are visible
without decoding the nested policy record.

## Similar-task route cache

Repeated short tasks can reuse a stable local routing decision instead of
calling Not Diamond every time. By default, a cache hit requires at least five
successful live Not Diamond routes from the last seven days, task similarity of
at least `0.78`, and an `0.80` majority for the same proxy/model/effort route.
Fallback decisions and high-consequence tasks are never cached, and cached
decisions are not allowed to train themselves.

Configuration environment variables:

```text
CODEX_ROUTER_CACHE_ENABLED=1
CODEX_ROUTER_CACHE_MIN_SAMPLES=5
CODEX_ROUTER_CACHE_SIMILARITY=0.78
CODEX_ROUTER_CACHE_MAJORITY=0.80
CODEX_ROUTER_CACHE_MAX_AGE_DAYS=7
```

The log `display` field reports `source notdiamond`, `source similarity-cache`,
or `source notdiamond-fallback`, plus cache hit/miss and sample count.

## Local routing dashboard

Open the read-only dashboard, which refreshes every three seconds:

```powershell
py -3 -m codex_model_router.dashboard
```

```bash
python3 -m codex_model_router.dashboard
```

The dashboard listens only on `127.0.0.1:8765`. It shows total and 24-hour
routing volume, Codex model distribution, cache and fallback rates, Not Diamond
latency, and the latest proxy-to-Codex mapping for each task. It does not change
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
