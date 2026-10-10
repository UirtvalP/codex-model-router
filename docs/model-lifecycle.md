# Native model updates and reviewed replacements

0.10 discovers every available native Luna/Sol/Astra model from generation6
onward. All discovered models become evaluator candidates; version numbers do
not establish strength. Explicit per-family preferred_models remain candidate
pins. Same-backend enforcement, explicit exclusions and family speed policy
remain. A model missing from the account's native cache is not enabled solely
because it exists in the shipped bundle. No new provider or credential is added.

Managed refresh is opt-in under `lifecycle.native_refresh`: enabled,
catalog_path, source_cache and state_path (distinct absolute local paths). It
checks runtime executable and local cache file stamps, validates complete model
capabilities, and atomically writes the entire v1 catalog. Binary changes read
`codex debug models --bundled` once offline to update capabilities for known
account IDs. Only a cache matching the current CLI major/minor line adds available IDs. An older CLI overwriting the shared cache cannot remove the verified roster or add its IDs; the last complete v1 snapshot supplies that roster instead. Concurrent
writers are excluded. Bad/offline sources retain the last usable directory and
failures back off. Hooks only read the last atomic snapshot; even changed or offline sources do not launch maintenance processes or network requests on the spawn path. A dedicated maintenance command, `codex-model-router --refresh-models`,
can run from a local file watcher; it makes no model call.

Custom catalogs are startup-loaded. New desktop tasks after a normal restart
were verified; an already running app may cache the previous catalog. Updating
the file does not forcibly migrate old sessions or restart an app. Bundled
unknown-account models and missing official local roster updates remain unknown
rather than silently granting access. The watcher cannot invent an account
roster that the native client has not supplied.

## Capability and price evidence

The native catalog contains supported efforts/capabilities, not comparative
benchmarks or model-specific subscription prices. Capability retirement uses a
reviewed comparison registry, not version order or downloaded prose. The public
registry [model-comparisons.json](model-comparisons.json) records source,
reviewer, date and expiry. An unambiguous official release declaration is sufficient capability evidence; no independent benchmark is required. Claims are limited to the release's stated comparison objects and dimensions, never inferred from vague marketing. Release provenance records date (explicitly unknown when absent), comparison models, dimensions and price scope. Maintainers transcribe/review official declarations and publish the
JSON through normal PR/CI. Users need not edit per-model rules to receive newly
published comparisons, but **evidence curation is not automated**. If no reviewed
comparison is published, new available models remain candidates and older ones
are not automatically retired.

`lifecycle.registry` opts into that exact repository's HTTPS JSON URL, cache_path
and enabled=true. ONLY the maintenance command fetches it, at most once per day,
with a size/time limit and atomic cache; hooks read the cached JSON only. The URL is fixed to the controlled main/docs/model-comparisons.json file. Redirects, unknown fields and decreasing revisions (or changed content at an equal revision) are rejected. The reviewer field is provenance only; trust comes from the controlled repository release process. Invalid
downloads retain previous evidence. Expired rows are ignored. No remote text is
executed or treated as instructions. Explicit local `lifecycle.comparisons`
may add reviewed rows. `--refresh-models` reports evidence and effective retired
IDs; this is policy status, not a paid evaluator.

Each row needs old,new,tier, and capability with verdict=strictly_better,
reviewed_by, HTTPS source, checked_at and valid_until. Tier must match the native
family and both IDs must be present/enabled; successor efforts must cover the
predecessor. A stronger but dearer model is eligible and described to the
evaluator, while bounded cheaper tasks can still use the older candidate.

Default cost_basis is **subscription**. For retirement it needs reviewed
subscription_units prices for the same explicitly configured subscription_scope
and finite nonnegative usage_units. API token rates never imply subscription
unit costs. `api_standard` uses currency=USD_per_million_tokens and input,
cached_input,output values for both models. All three rates must be no greater,
at least one lower; mixed increases/decreases, equal rates, missing/future/stale
prices, differing scope or NaN are not cheaper. Prices need checked_at,
valid_until, scope, reviewer and HTTPS source. Fast/context-specific/batch rates
cannot be mixed with standard rates.

The initial reviewed capability row is6 Sol ->6.1 Sol, based on
https://openai.com/index/introducing-gpt-6-1-sol/ . Complete subscription price
evidence is unavailable, so its price basis isunknown and it does not retire
6 Sol by itself. The explicit Mac exclusion of6 Sol remains independent.

## Automatic evaluator, exclusions and caches

`evaluator.selection="auto"` makes the evaluator follow a valid retired-model
replacement while retaining its reasoning effort and applying successor family
speed policy. Existing `evaluator.model` settings default to `selection="fixed"`
for compatibility; fixed user choices are never rewritten. Direct explicitly
pinned agent choices also remain user choices, not automatic routing.

If a successor is explicitly excluded it is not a replacement and the old model
remains unless separately excluded. If the old evaluator is excluded in auto
mode, another enabled same-family candidate is required; no disabled ID is
re-enabled. An empty required family causes existing safe skip/fallback behavior,
not an unauthorized model/backend switch. Exact fixed selections take precedence
over automatic retirement and need an explicit change to join automatic mode.

Raw catalog cache stores order. Comparison changes affect policy_revision and
invalidate bound preselection reuse. Retirement runs after configured additions
and exclusions, so a static routing.models entry or raw stale cache cannot
reintroduce a retired ID while its valid replacement remains available. Offline
availability uncertainty is handled conservatively, never by inventing a new
entitlement. Lifecycle does not edit history, remove models from user data or
alter global manually chosen root models.

Tests use explicitly synthetic future IDs/prices to verify future updates,
strict dominance, unknown preservation, tier boundaries, exclusions, fixed/auto
evaluator selection, ordered caches, policy invalidation, version/cache refresh,
offline preservation and v1 capability retention. They are not benchmarks of
unreleased models.
