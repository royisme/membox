# Plan 06 — Phase F: Distill Workflows

> **Status**: Accepted 2026-06-12 · **Spec source**: `docs/spec/spec_02_memory_lifecycle.md` Phase F (deliverables + acceptance are normative there; this plan only sequences and bounds the work).
> **Predecessor**: Phase E (plan_05) complete — merged to main `f274d27` (query-side memory fusion, strict eval gate, 544 tests, offline 24/26 + Gemini 26/26 re-verified).

## Goal

`membox distill --dry-run`: identify repeated workflows in a project's memory units that are worth packaging, and report candidates with evidence, frequency, and a recommended form — **without creating anything**. Per spec: "No automatic skill or command creation unless separately approved" and "The command can return 'created nothing' as a successful result."

## What "distill" is and is not (scope fence)

- **Is**: a read-only analysis pass over `procedure`/`learning` units in statuses `{active_unit, crystal_candidate, crystal}` (the same read-side status set as Phase E's memory pool; `{unit_candidate, superseded, archived, retracted}` excluded) that groups recurrences of the same workflow, counts independent evidence, inventories existing assets, and prints a candidate report. Including `crystal_candidate` is load-bearing: the c5 fixture's Phase D outcome IS `crystal_candidate`, so an active-only scan would exclude the seed acceptance case.
- **Is not**: a writer. No new tables, no new unit types, no skill/command file generation, no `--apply` in this phase. The only conceivable write is candidate bookkeeping, and this plan explicitly does without it — re-derive on each run, exactly like Phase D's stateless conflict surfacing (same precedent, same revisit condition: only if re-announcement becomes noise).
- **Is not** the cross-project global-scope distillation from roadmap Future Tracks. Phase F is per-project (`--project X`, same `_default_project` resolution as the other memory commands). The global track builds on this later and owns its own spec chapter.

## Hard regression contract (F0 — holds for every milestone)

- Phase F adds a read-only command. **No existing behavior may change**: query output byte-stable (offline eval exactly 24/26 @ 4000 via `--expect-hits 24`), all 544 existing tests untouched and green.
- **No schema change** — migration head stays 8. Candidate grouping is computed at read time from existing `memory_units` + `memory_unit_sources`. If a milestone seems to need DDL, stop and escalate.
- No LLM in the read path (spec §3.7 unchanged). Grouping is deterministic: FTS/token similarity over unit content, not model judgment. An LLM-assisted "describe this workflow" enrichment is explicitly out of scope (recorded under Deferred).

## Current state (verify before execution)

- `memory_units` already carries everything distill needs: `unit_type` (closed taxonomy; `procedure`/`learning` are the distill sources), `status`, `importance_score`/`confidence_score`, `recall_count`/`last_recalled_at` (Phase E bookkeeping — distill is the first consumer that may *read* them as a frequency signal; they still feed no ranking in `query`), sources with the Phase D session-level independence rule (`count_independent_sources_for_units`, already batched).
- `core/consolidate.py` has the reusable pure-policy pattern (dataclass results + deterministic plan builder) — `core/distill.py` mirrors it.
- The c5 fixture (`eval/lifecycle/history/c5_repeated_failure.jsonl`, expectation `unit_type: procedure`, Phase D status `crystal_candidate`) is the seed material — but it is a **single session**, so by itself it must NOT pass the candidate gate; F4 adds a second-session fixture `c5_b` to form the positive case. The corpus has 9 expectation entries to draw negatives from (c2 chatter, c8 tool noise must never become candidates).

## Milestones

### F1 — Candidate model + grouping policy (pure domain, `core/distill.py`)

- `DistillCandidate` dataclass: the grouped units (ids + titles), evidence summary (independent source count across the group, session-level rule reused), frequency fields (`unit_count` + displayed-only `summed_recall_count`), recommended form, and a deterministic explain line per candidate (same explain-line discipline as consolidation transitions). `recall_count` is never used for candidate qualification, ordering, or form selection.
- Grouping: `procedure` and `learning` units in the status set above, within the `--since` window when provided (default: all eligible units), grouped by content similarity — deterministic token/claim overlap (reuse `_claim_tokens` from consolidate), NOT embeddings, NOT LLM. Threshold a named constant (`DISTILL_GROUP_MIN_OVERLAP`), owner-calibration target like the D1/E1 constants.
- Candidate gate (spec: "A candidate must have repeated evidence or explicit user approval"): a group qualifies only with evidence from ≥2 **independent sessions** (Phase D session-level independence rule via `count_independent_sources_for_units` — a message+event pair from one session counts once) OR explicit user approval, defined by the existing Phase D predicate `has_explicit_user_confirmation` (a `MemorySourceKind.MANUAL` source or confirmation phrasing). No new vocabulary: the model's `user_intent` enum is `manual | auto` and `MemoryUnitRecord` does not hydrate it — this plan does not introduce `explicit` as a value nor add the field. Named constant `DISTILL_MIN_INDEPENDENT_EVIDENCE = 2`.
- Recommended form: closed mapping from unit composition and asset shape. Machine values are `{command, script, convention_doc, skill_file}`. `command` maps to command assets such as `.claude/commands/`, `script` maps to `scripts/` or Makefile/justfile-style executable workflows, `convention_doc` maps to documentation of durable conventions, and `skill_file` maps to a reusable skill-style workflow. No free-text invention; the form vocabulary is a closed set like the label taxonomy.

### F2 — Asset inventory (spec: "Existing assets are inventoried before proposing a new one")

- Deterministic scan of conventional asset locations under an explicit filesystem root (`scripts/`, `.claude/commands/`, `skills/`, `Makefile`/`justfile` targets) — names+paths only, no content parsing beyond titles. Injectable as a small `AssetInventory` protocol so tests use a fake (project rule: mock only at I/O boundaries).
- **Root resolution**: `--project X` is a DB scope, not a filesystem location — the two must not be conflated. CLI gains `--root PATH` (default: cwd); the report always prints `scanned_root: <path>` so a run from the wrong directory is visible instead of silently marking candidates `covered_by` another project's assets. If the root does not exist, error out.
- A candidate whose recommended form already has a matching asset (token overlap between candidate title and asset name, same named-constant discipline) is reported as `covered_by: <path>` instead of suppressed — honest reporting over silent filtering, same principle as the truncation footer.

### F3 — CLI (`membox distill --project X --dry-run`)

- New `cli/commands/distill.py` (presentation only; assembly in core, same layering as `memory consolidate`). `--dry-run` required in this phase; passing `--apply` errors with "not implemented in Phase F" (explicitly reserving the flag). `--root PATH` per F2 (default cwd, printed as `scanned_root`).
- Output: one block per candidate (form, evidence count, frequency as `evidence_sessions=N, units=N, recalls=N`, member units by id/title, `covered_by` when applicable, explain line), plus the effective window (`window: all` or `window: since 30d`) and an honest empty result: "no distill candidates found" with the scanned-unit count — "created nothing" is a successful result per spec acceptance.
- Read-only ⇒ no lease (`lifecycle_lease` is for mutating applies only); document this in the command docstring.

### F4 — Acceptance: fixture harness + regression

- **New fixture `c5_b` (separate session)**: the existing c5 is a SINGLE session (`requires_multi_session: false`, one message + one tool event — under the session-level independence rule that is 1 source, not 2), so c5 alone must NOT qualify. c5_b is a second, independent session recording a recurrence of the same verify-migration-head workflow (same anonymization discipline as c8/c9). c5+c5_b together form the genuine cross-session repeated workflow.
- Extend `tests/test_lifecycle_acceptance.py`: full pipeline (import → triage → extract → consolidate → distill) asserting (a) with c5 only, zero candidates (single-session repeat does not qualify — the independence rule as a test, not a claim); (b) with c5+c5_b, exactly one candidate with the expected form and 2-session evidence; (c) c2/c8/c9 noise produces zero candidates; (d) `covered_by` fires against a planted fake asset under an explicit `--root`.
- `expectations.yaml` gains a `distill` key per entry (c5 expects `none` standalone; the c5+c5_b pair expects one candidate; all others `none`) — same pattern as the `query_inclusion` matrix written at C1 time for Phase E. c5's existing Phase C/D expectations are untouched (c5_b gets its own entry).
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

## Owner decisions (resolved 2026-06-12, at plan review)

1. **Recommended-form vocabulary: `{command, script, convention_doc, skill_file}`.** Use machine-safe closed values, not slash-bearing labels such as `command/script`. `command` and `script` are separate because their asset locations and later `--apply` semantics differ (`.claude/commands/` vs `scripts/` or Makefile/justfile-style executable workflows). `convention_doc` covers durable documentation, and `skill_file` covers reusable skill-style workflows.
2. **Frequency signal: display `unit_count + summed recall_count`, but never decide from `recall_count`.** The report should show frequency as `evidence_sessions=N, units=N, recalls=N`. `recall_count` is a read-side explanatory field only; it must not influence candidate qualification, candidate ordering, or recommended-form selection. This preserves Phase E's bookkeeping-only decision while making hot workflows visible.
3. **`--since` default: no window.** By default, scan all eligible active units/crystal candidates/crystals. The help text should recommend `--since 30d` as a common narrowing option, and the output must print the effective window (`window: all` or `window: since 30d`) so scope is never implicit. Rationale: unit volume is still small, and a 30-day default can miss low-frequency long-lived workflows.
4. **"Explicit user approval" mapping: reuse Phase D's `has_explicit_user_confirmation`.** The spec phrase maps to a `MemorySourceKind.MANUAL` source or confirmation phrasing in title/content/context. Do not mint a new `user_intent=explicit` value (`MemoryUserIntent` is `manual | auto`), and do not expand `MemoryUnitRecord` just for Phase F to hydrate `memory_units.user_intent`. This keeps Phase F read-only and aligned with Phase D's existing user-confirmation semantics.
