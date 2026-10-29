# Changelog

## Unreleased

### Added
- **MCP 2026-07-28 (stateless) alongside 2025-11-25.** `/mcp` picks the era per request: a request carrying `_meta["io.modelcontextprotocol/protocolVersion"]` or `MCP-Protocol-Version: 2026-07-28` gets `server/discover`, `resultType`, `ttlMs`/`cacheScope` on `tools/list`, server identity in `_meta`, and the spec's header checks (`MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name`, base64 sentinel decoded) with HTTP 400 and codes -32602, -32020, -32022; unknown methods (including `initialize`, `ping`) give 404. Legacy clients see no change.
- **Proof bench** `reason bench`: rules against a hostile decider, indirect injection against a gullible one, provider failures (timeouts, 429, non-JSON, wrong schema), concurrency on one idempotency key, `SIGKILL` during decisions then restart, canary lesson and fuzzed namespaces; failures and a 95 % upper bound per suite, exit code 1 on failure. `--namespace` adds baselines on recorded outcomes, `--live N` measures the configured decider's hijack rate.
- **Shadow mode**: `config.shadow` maps namespace globs to challenger model overrides; model-made decisions are re-decided by the challenger as a background dry run and compared in `shadow.jsonl`. `reason shadow <ns>` reports agreement, escalation rates, paired wins/losses with a sign test.
- **Rule simulation**: `reason rules <ns> simulate --candidate FILE` replays candidate rules on journaled requests without model calls and reports changed decisions, errors avoided or introduced, and escalations added or automated.
- **Claude Code hook: human answers, `defer`, `updatedInput`.** `POST /hooks/claude/answer` (new `approve` scope, distinct from `outcome`) records allow/deny with an optional `updated_input` for an escalated tool call; it applies to the same decision and to the same call in the same session (SHA-256 of tool name and full input, signed with `data_dir/hook.key` so decisions forged through `/decide` are refused), the latest answer wins, `deny` overrides any decision, `allow` only settles an escalation. `REASON_HOOK_DEFER=1` returns `defer` for unanswered escalations (`claude -p` only). A concurrent decision for the same call returns `ask` with HTTP 200 instead of 409.
- MCP tool annotations (`title`, `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) on the five tools.
- **External estimates**: `POST /estimate/record` (MCP `record_estimate`, CLI `estimate-record`) records a probability, distribution or quantiles produced outside Reason, without a model call, under a free `estimator` channel; one snapshot per (`namespace`, `estimator`, `correlation_key`). Calibration and `calibrate` reports group by `estimator`, falling back to `source`. `/estimate` also accepts `estimator`, `correlation_key` and `p_outcome`.
- **Poller settles estimates**: a source with `"settles": "estimates"` resolves every pending estimate of a `correlation_key` from one event (`fields.winning`, label or index; `void`/`no_outcome` annuls), so untraded markets get scored too.
- **Read-only chat** (`config.chat`, role `chat` else `executor`): questions in plain language answered by any connected LLM (OpenAI-compatible or Claude, not Jev) through structured output, with read-only built-in tools (`recent`, `decision`, `pending`, `value`, `calibration`, `outcomes`, `lessons`, `namespaces`) and host project tools declared as fixed `GET` URLs. On Telegram (text messages, dedicated bot), `POST /chat`, `Client.chat` and `reason chat`.
- Chat: `chat.style` sets the tone of replies; Telegram keeps the typing indicator on until the reply is sent; the tool field is a plain string enum (`"aucun"` for the final answer), since Claude structured outputs reject an enum holding null.
- **Telegram connector** (`config.telegram`, stdlib): escalations to a human are sent with one button per action playable without parameters, degraded fallbacks for information, a reached budget once per day; quiet hours send silently. Dedicated bot (`poll: true`, read by `reason serve`) or shared bot (`poll: false`: the application that reads the bot forwards `rsn:` callbacks to `POST /telegram/callback`). `reason telegram-test` checks the setup.
- **Human answers**: `POST /decisions/answer` (scope `approve`) records the answer to an escalated decision, first one wins; `GET /decisions/<id>` returns decision, answer and outcome. A later outcome without `human_feedback.correct_action` takes the answer as its correction. Client: `answer`, `decision`, `telegram_callback`.
- **Cost per decision**: decisions that called a model carry `cost` (`tokens_in`, `tokens_out`, `usd`, `by_tier`); models declare `price_in` / `price_out` (USD per million tokens).
- **Daily budget** per namespace (`budgets`: glob -> `usd_per_day` / `tokens_per_day`, UTC day, rebuilt from the journal): once reached, no model is called and the decision carries `budget_exhausted`.
- **Declared fallback**: when no model answered (provider down, budget reached), `fallback` (namespace glob) or `constraints.on_model_failure` returns that action with `tier: "fallback"`, `degraded: true`; otherwise the escalation carries `degraded`.
- **`reason value <ns>`**: spend, outcomes and reported `value` (mean, 90 % CI, sum) per tier, decision point, day and lesson. Value is measured, never used to weight lesson credit.
- **`reason.client.Client`**: stdlib Python client with timeout, retries (idempotent routes only after a timeout; `decide` gets a generated `idempotency_key`) and an optional safe fallback when the service is unreachable.
- README: rule examples for fresh information and available cash.
- **Long markets and multi-outcome reports**: estimate settlement ignores `match_ttl_hours` (it only bounds how long an unmatched event waits), and `calibrate` reports `sources_multi` per channel for categorical and ordinal estimates (multi-class Brier, skill vs uniform, RPS); score rows carry `brier_uniform`.
- **`"web": true` on a model**: native web search per provider (`web_search` tool on Claude, `web` plugin on OpenRouter, `web_search_options` elsewhere), capped by `web_max_uses`. On Claude the schema moves into the prompt (citations exclude `output_config.format`) and only the text after the last search is parsed. Estimates from a web model are recorded under the `web:<model>` channel.
- **Native Claude support**: `"provider": "anthropic"` on a model calls the Anthropic Messages API (still stdlib only) for any role. `reasoning_effort` maps to `output_config.effort`, decision schemas use structured outputs, `temperature` is never sent, responses are streamed (SSE) so long generations do not hit `timeout`, and `max_tokens` truncation or a `refusal` is an error. Jev stays optional.

### Fixed
- `Content-Length` is validated (digits only, single value, no `Transfer-Encoding`), else 400 and the connection is closed.
- A concurrent request with the same `idempotency_key` still in progress gets HTTP 409.
- `estimate` tolerates failed samples and reports `n_requested`; `estimate_outcome` and namespace reads refresh from disk first; `prune` evicts removed decisions from memory in other processes.

## 0.3.0 (2026-10-10)

Driven by three independent reviews and a research pass on trading models: measure, do not assert.

### Added
- **Calibrated decision confidence.** Outcomes of model-made decisions are recorded as (confidence, correct) in `decision_scores.jsonl`. `reason calibrate <ns> --decisions` fits Platt per tier on a chronological 70/30 split, reports AUROC and test-set AURC, and learns an escalation threshold per decision point on the older 70 %: the lowest calibrated confidence whose Wilson lower bound reaches the target accuracy, with coverage and accuracy reported on the unseen recent 30 %. A learned threshold never goes below the configured one (unless `allow_threshold_below_default`) and expires after `calibration_max_age_days`. Escalated proposals are scored when a human answers, so thresholds can come back down. Every trusted revision writes a new label. Decisions carry `confidence` (calibrated), `confidence_raw`, `threshold`, `calibrated`.
- **Sample agreement.** `decide.n_samples` (or `constraints.n_samples`) samples the decider several times; majority action, `agreement` (over the samples requested), `n_ok`, effective confidence capped by agreement. Deterministic deciders (Jev) are called once.
- **`intent_key`** on every executable decision: a per-decision key to pass downstream as `client_order_id`, `Idempotency-Key` or `ClOrdID`; send `idempotency_key` to get the same decision, hence the same key, after a crash.
- **Lesson ablation**: `reason eval <ns> --compare-lessons [wrong.json] --after <date>` replays the same held-out cases with lessons on, off and deliberately wrong, with paired wins/losses and an exact sign test; pairs where either arm hit a provider failure are excluded and counted. `--lessons on|off|file` for single runs. Replays are dry runs and every run is logged to `trials.jsonl`.
- **`decide(..., dry_run=True)`**: nothing journaled, indexed or counted.
- **Scoped tokens**: `config.tokens` maps secrets to namespace globs and scopes (`decide`, `outcome`, `execute`, `estimate`, `read`), enforced on HTTP, MCP and hooks; the admin `REASON_TOKEN` keeps full access.
- **Retention and redaction**: `redact_paths` (under `event`, `state`, `history`, `context`, `ruling_text`, `input`) masks sensitive fields in everything persisted; free text is not masked. `reason prune <ns> --days N` removes old journal events except pending decisions (including horizon-expired ones) and unresolved estimates; other processes detect the rewritten file by inode.

### Changed
- **Breaking**: the server refuses to start without authentication; `serve --insecure` is allowed on a loopback address only (and then checks the `Host` header). Only one `reason serve` per `data_dir` (file lock). Token comparison works on bytes (non-ASCII headers give 401, not a crash). Authorization failures raise a dedicated error, distinct from filesystem permission errors.
- Playbook writes, journal appends and dataset/score appends are file-locked across processes (no lost counter updates or interleaved lines between `serve`, `poll` and the CLI). Calibration files are written atomically and merged.
- Example reasoner budget raised to 16 000 tokens (reasoning tokens count against the limit).
- README: sources for the "Why" table, a "Proving it works" section, duplicate layout entry removed.

An independent review of this release found 34 issues (7 high); all were fixed and covered by tests before release.

### Removed
- **The weekly maintenance workflow.** It had no data to read on CI, and the tests it ran imported the code it had just modified, so a patch could have rigged them. `scripts/check_diff.py` stays as a standalone CI check.

## 0.2.1 (2026-10-10)

### Fixed
- **Poller survivorship bias**: an event type that is neither a valid result, nor in `result_map`, nor in the new `ignore` list used to become `unknown`, which silently excluded unmapped losses from every score. It is now held in the poller state, counted under `unmapped` in the tick report, warned about on stderr, and reprocessed with the current config at each tick, so fixing `result_map` recovers it.
- Examples: removed the `no-fallback-clause` rule, which encoded a domain opinion (a missing fallback clause can be an edge, not a reason to refuse); the example `result_map` now maps a loss event and ignores trade events.

## 0.2.0 (2026-10-10)

Driven by an external review from a prediction-market trader (see the research notes): from "which action" to "how likely, scored at settlement".

### Added
- **Typed estimates** `POST /estimate` and `POST /estimate/outcome`: binary, categorical, ordinal and quantity estimates with strict schema, `source` (fact | model | opinion), evidence, base rate, K-sample aggregation in log-odds space, per-(namespace, source) Platt recalibration, Brier / log-loss / RPS scores in `estimate_scores.jsonl`, lesson credit from scores. `reason calibrate` reports per source (Brier, skill vs base rate and market, Murphy decomposition) and per lesson (observational and counterfactual deltas); `--fit` learns Platt on a temporal 70/30 split and keeps it only if the test Brier improves.
- **Outcome v2**: extended results (`partial`, `void`, `no_outcome`), `result_detail`, `value`, `payout_fraction`, `settled_at`, `observed_at`, `source`, `source_ref` (idempotency), `label_confidence`, versioned revisions that undo the previous version's lesson counters, `resolution_text` kept as untrusted quoted data. One markdown post-mortem per settled decision, rewritten with a version history on revision.
- **Poller** `reason poll`: reads settlements from a local JSONL, an HTTP JSON endpoint or a GraphQL subgraph from a persisted watermark, matches them to pending decisions by `correlation_key` and time window, posts outcomes idempotently, and marks decisions past their horizon as `unknown` with zero label confidence. `GET /pending/<ns>` lists what is waiting.
- **Rules v2**: path-vs-path comparisons (`path2`), `all` / `any` / `not`, date operators (`before`, `after`, `hours_until_gte`, `hours_until_lte`, `within_hours`) on a `now` injected in the request and copied into the decision (replayable), bounded regex on untrusted text (`norm`, pattern length cap, nested-quantifier lint, text truncation), `reason` interpolation with `{match}` and `{path}`, `reason rules lint|test` with coverage and contradiction detection. A forced `decide` is now checked against `forbid` rules.
- **Lessons v2**: `tags`, `decision_points`, `valid_from`, `valid_until`, `status`, provenance, calibration counters, Wilson lower bound ordering, injection capped at 6 lessons, `reason lessons review` (promotion and review candidates). **Markdown vault**: with `vault_dir`, Reason reads `doctrine/` and `postmortems/` (frontmatter `kind: lesson`), derives the effective status from frontmatter or folder, quarantines lessons without provenance, picks up approvals made in Obsidian or git without restart, and writes proposals as new files only. It never modifies an existing vault file.
- `reason outcomes`: success rate on the mature cohort only, label coverage, poller lag.

### Changed
- `estimator` role (falls back to `decider`, then `executor`). `max_lessons` and `label_confidence_min` thresholds.
- Binary estimates report no interval "coverage": an interval on p cannot cover a 0/1 outcome; coverage is computed for quantities only.

### Security and robustness (independent review, 35 findings fixed)
- Vault files are written with single-line, quoted frontmatter values: a third-party `result_detail`, `source` or `decision_point` can no longer inject keys and self-approve a lesson. `decision_point` is validated; vault paths are checked.
- Regex patterns can only come from the rule (`path2` refused for `regex`); the ReDoS lint is a stack-based scanner that catches `(a+)+`, `(a|aa)+` and `((\w+)\s?)+`; rules that fail the lint are never executed.
- The engine re-reads journals incrementally, so `reason poll` and `reason serve` in separate processes see each other's decisions and outcomes (no double counting).
- The poller retries unmatched settlements until their TTL, compares numeric cursors numerically, detects a replaced JSONL file, caps HTTP responses, and applies late settlements after a horizon expiry as a revision.
- Fixed: RPS formula, mature-cohort filtering in `reason outcomes`, starvation of unscored vault doctrine (prior 1/1), archived state for vault lessons, id collisions between local and vault lessons, inclusive date-only `valid_until`, numeric overflow in sigmoid and epoch parsing, malformed `all`/`any` conditions, estimate outcome validation before marking resolved, counterfactual replay now a dry run credited once per pair, reflector treats outcome fields as untrusted data.

### Compatibility
- v0.1 requests, outcomes, playbooks and journals keep working unchanged; a v0.1 outcome replayed in the v0.1 format is still a duplicate, and a v0.2 revision correctly undoes the counters a v0.1 outcome had applied.

## 0.1.0 (2026-10-10)

Initial release: four-tier decision engine, configurable model roles, Jev adapter, lesson playbook, outcome feedback, regression replay, model fingerprint, HTTP / MCP / Claude Code hook facades, maintenance workflow.
