# Changelog

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
