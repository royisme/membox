# Plan 06 — Phase F: Distill Workflows

> **Status**: Draft for owner review — do not execute until accepted. · **Spec source**: `docs/spec/spec_02_memory_lifecycle.md` Phase F (deliverables + acceptance are normative there; this plan only sequences and bounds the work).
> **Predecessor**: Phase E (plan_05) complete — merged to main `f274d27` (query-side memory fusion, strict eval gate, 544 tests, offline 24/26 + Gemini 26/26 re-verified).

## Goal

`membox distill --dry-run`: identify repeated workflows in a project's memory units that are worth packaging, and report candidates with evidence, frequency, and a recommended form — **without creating anything**. Per spec: "No automatic skill or command creation unless separately approved" and "The command can return 'created nothing' as a successful result."

## What "distill" is and is not (scope fence)

- **Is**: a read-only analysis pass over active `procedure`/`learning` units (and crystals) that groups recurrences of the same workflow, counts independent evidence, inventories existing assets, and prints a candidate report.
- **Is not**: a writer. No new tables, no new unit types, no skill/command file generation, no `--apply` in this phase. The only conceivable write is candidate bookkeeping, and this plan explicitly does without it — re-derive on each run, exactly like Phase D's stateless conflict surfacing (same precedent, same revisit condition: only if re-announcement becomes noise).
- **Is not** the cross-project global-scope distillation from roadmap Future Tracks. Phase F is per-project (`--project X`, same `_default_project` resolution as the other memory commands). The global track builds on this later and owns its own spec chapter.

## Hard regression contract (F0 — holds for every milestone)

- Phase F adds a read-only command. **No existing behavior may change**: query output byte-stable (offline eval exactly 24/26 @ 4000 via `--expect-hits 24`), all 544 existing tests untouched and green.
- **No schema change** — migration head stays 8. Candidate grouping is computed at read time from existing `memory_units` + `memory_unit_sources`. If a milestone seems to need DDL, stop and escalate.
- No LLM in the read path (spec §3.7 unchanged). Grouping is deterministic: FTS/token similarity over unit content, not model judgment. An LLM-assisted "describe this workflow" enrichment is explicitly out of scope (recorded under Deferred).

## Current state (verify before execution)

- `memory_units` already carries everything distill needs: `unit_type` (closed taxonomy; `procedure`/`learning` are the distill sources), `status`, `importance_score`/`confidence_score`, `recall_count`/`last_recalled_at` (Phase E bookkeeping — distill is the first consumer that may *read* them as a frequency signal; they still feed no ranking in `query`), sources with the Phase D session-level independence rule (`count_independent_sources_for_units`, already batched).
- `core/consolidate.py` has the reusable pure-policy pattern (dataclass results + deterministic plan builder) — `core/distill.py` mirrors it.
- The c5 fixture (`eval/lifecycle/history/c5_repeated_failure.jsonl`, expectation `unit_type: procedure`) is the seed acceptance case; the fixture corpus has 9 expectation entries to draw negatives from (c2 chatter, c8 tool noise must never become candidates).

## Milestones

### F1 — Candidate model + grouping policy (pure domain, `core/distill.py`)

- `DistillCandidate` dataclass: the grouped units (ids + titles), evidence summary (independent source count across the group, session-level rule reused), frequency (group size + summed `recall_count`), recommended form, and a deterministic explain line per candidate (same explain-line discipline as consolidation transitions).
- Grouping: active `procedure` and `learning` units (+ crystals of those types) within `--since` window, grouped by content similarity — deterministic token/claim overlap (reuse `_claim_tokens` from consolidate), NOT embeddings, NOT LLM. Threshold a named constant (`DISTILL_GROUP_MIN_OVERLAP`), owner-calibration target like the D1/E1 constants.
- Candidate gate (spec: "A candidate must have repeated evidence or explicit user approval"): a group qualifies only with ≥2 units from independent sessions OR one unit with `user_intent=explicit` provenance. Named constant `DISTILL_MIN_INDEPENDENT_EVIDENCE = 2`.
- Recommended form: closed mapping from unit composition — `procedure`-dominant → "command/script candidate"; `learning`-dominant → "convention/doc candidate"; mixed → "skill-file candidate". No free-text invention; the form vocabulary is a closed set like the label taxonomy.

### F2 — Asset inventory (spec: "Existing assets are inventoried before proposing a new one")

- Deterministic scan of conventional asset locations in the project working tree (`scripts/`, `.claude/commands/`, `skills/`, `Makefile`/`justfile` targets) — names+paths only, no content parsing beyond titles. Injectable as a small `AssetInventory` protocol so tests use a fake (project rule: mock only at I/O boundaries).
- A candidate whose recommended form already has a matching asset (token overlap between candidate title and asset name, same named-constant discipline) is reported as `covered_by: <path>` instead of suppressed — honest reporting over silent filtering, same principle as the truncation footer.

### F3 — CLI (`membox distill --project X --since 30d --dry-run`)

- New `cli/commands/distill.py` (presentation only; assembly in core, same layering as `memory consolidate`). `--dry-run` required in this phase; passing `--apply` errors with "not implemented in Phase F" (explicitly reserving the flag).
- Output: one block per candidate (form, evidence count, frequency, member units by id/title, `covered_by` when applicable, explain line), and an honest empty result: "no distill candidates found" with the scanned-unit count — "created nothing" is a successful result per spec acceptance.
- Read-only ⇒ no lease (`lifecycle_lease` is for mutating applies only); document this in the command docstring.

### F4 — Acceptance: fixture harness + regression

- Extend `tests/test_lifecycle_acceptance.py`: full pipeline (import → triage → extract → consolidate → distill) asserting (a) the c5 repeated-failure group produces exactly one candidate with the expected form and ≥2-session evidence; (b) c2/c8/c9 noise produces zero candidates; (c) a fabricated single-session repeat does NOT qualify (independence rule); (d) `covered_by` fires against a planted fake asset.
- `expectations.yaml` gains a `distill` key per entry (only c5 expects a candidate; all others `none`) — same pattern as the `query_inclusion` matrix written at C1 time for Phase E.
- F0 contract on every milestone commit: 544 existing tests + new ones green, ruff + strict mypy, coverage ≥ 80%, offline eval `--expect-hits 24` @ 4000 (local/agent-run — CI cannot, corpus is gitignored). No Gemini re-verification needed at merge unless retrieval code is touched (it must not be).

## Deferred / out of scope (recorded, not forgotten)

- `distill --apply` (asset generation) — separate owner approval per spec.
- LLM-assisted candidate description/enrichment (injectable Protocol, like the conflict comparator deferral).
- Cross-project/global-scope distillation (roadmap Future Tracks; depends on this phase).
- Candidate persistence/dismissal bookkeeping ("don't re-propose dismissed candidates") — revisit if re-announcement becomes noise, same as D2 conflict persistence.
- Existing deferrals unchanged: atomic apply batching, FTS conflict pairing, LLM conflict comparator, gate v4.

## Acceptance criteria (spec_02 Phase F, verbatim)

- A candidate must have repeated evidence or explicit user approval.
- Existing assets are inventoried before proposing a new one.
- The command can return "created nothing" as a successful result.

## Constraints for all dispatched subagents

- All Phase F work on `feature/phase-f-distill` (single branch, milestone-ordered commits). Never commit to main/develop.
- No schema changes (head stays 8); no LLM in the read path; thresholds/vocabularies are named constants = owner calibration targets.
- Gates per milestone: `uv run pytest` + ruff + strict mypy green; coverage ≥ 80%; offline eval exactly 24/26 @ 4000 via `--expect-hits`.
- Stop and escalate on judgment calls beyond the brief — notably: any grouping threshold change, any new recommended-form value, anything that looks like it needs persistence or DDL.

## Owner decisions needed before execution

1. **Recommended-form vocabulary**: proposed closed set is `{command/script, convention/doc, skill-file}`. Confirm or amend — this is the distill counterpart of the closed label taxonomy.
2. **Frequency signal**: F1 proposes group size + summed `recall_count` as reported frequency (first read-side consumer of the E4 bookkeeping counters; they still influence nothing in `query`). Confirm that reading them here does not violate the E4 "bookkeeping-only" decision's intent, or strike `recall_count` from the report.
3. **`--since` default**: spec example shows `--since 30d`. Propose defaulting to no window (all active units) with `30d` in the help text as the recommended scan, since unit volume is still tiny. Confirm or require the literal 30d default.
