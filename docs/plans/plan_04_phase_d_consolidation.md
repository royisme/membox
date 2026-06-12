# Plan 04 — Phase D: Memory Consolidation (crystals, conflicts, decay)

> **Status**: Implementation exists on `feature/phase-d-consolidation` (D1–D5 + review-fix pass, 2026-06-12); the D0 exit gate was NOT met by the first real-trace run (see sign-off line below) and a v3 tuning round was run the same day: c8/c9 noise fixtures committed, gate at heuristic-v3, real-trace re-run 11 extract / 289 reject with both false-positive families eliminated. **Merge waits on the owner's final OK over the v3 results** (the exit gate's "v3 tuning round closed the gaps" path — results recorded in the sign-off package below). · **Spec source**: `docs/spec/spec_02_memory_lifecycle.md` Phase D (deliverables + acceptance are normative there; this plan only sequences and bounds the work).
> **Predecessor**: Phase C (plan_01) implemented — migration 8, heuristic-v2 gate, `membox memory triage/extract/supersede/retract/restore/search`, C5 harness at precision/recall/type-accuracy 1.00 on the C1 fixtures.

## Goal

Consolidate active units into crystals with strict provenance: crystal promotion under the conservative default policy, conflict detection that surfaces (never merges), unit-level supersession driven by consolidation, the validator, score evolution, and decay — all behind `membox memory consolidate --dry-run/--apply`.

## Hard entry condition (D0) — real-trace validation of the Phase C gate

**Do not start D1+ before D0 is reviewed.** spec_02 names its own most fragile assumption: "that a cheap triage gate reaches usable precision on real agent trace." The 1.00 C5 metrics are measured on 7 synthetic fixtures the gate was tuned against; they say nothing about real trace yet.

D0 procedure (cheap: dry-run only, no API cost, no writes):

1. `membox history import` 2–3 real local session logs (codex-jsonl adapter is live; pick sessions with known decisions/corrections in them).
2. `membox memory triage --dry-run` over the imported trace; sample ≥ 30 decisions across roles and review by hand.
3. Classify misses: false-positive chatter, missed rules, wrong types. Each real disagreement becomes a new committed fixture entry in `eval/lifecycle/` (anonymize/redact before committing — fixtures are public, real trace is not; reuse the import-time redaction scrubber and strip project-identifying content by hand).
4. If gate quality is materially below the fixture metrics, tune to heuristic-v3 against the grown corpus before D1. Record the real-trace sample size and miss counts in the PR/HANDOFF.

Exit gate for D0: owner has seen the dry-run sample summary and signed off that gate quality is sufficient to build consolidation on, OR a v3 tuning round closed the gaps. (Consolidation amplifies gate errors: a misclassified unit that gains sources becomes a wrong crystal.)

### D0 sign-off package (2026-06-12 — awaiting owner decision)

**Real-trace dry-run summary** (temporary import, nothing persisted): 20 local Codex session logs → 931 messages / 2844 events imported; 300 triage rows sampled; gate v2 decided **113 extract / 187 reject**. Per the D0 procedure, the owner reviews this sample (or a re-run of it) and signs off — or requests a v3 tuning round against any real disagreements, each of which becomes a committed anonymized fixture in `eval/lifecycle/`.

**Boundary declaration for the conflict detector (D2 first cut)** — accept explicitly, not silently: conflict detection is a deterministic word-list signal (contrast terms + ≥3 shared claim tokens + correction-term short-circuit routing correction pairs to supersession). It classifies the lifecycle fixtures correctly (c6 → conflict review; c4/c7 → supersession), but **real-trace conflict recall is uncalibrated and expected to be low** — this is the intentionally conservative first cut. The LLM-backed comparator (injectable Protocol, same pattern as the gate) remains the planned follow-up, as this plan's D2 already sketches. Conflict persistence stayed stateless (re-surfacing each run); migration 9 was not needed.

**Sign-off line**: `D0 signed off: NEEDS v3 TUNING — 2026-06-12 — re-run over 20 newest Codex logs (300 rows: 167 extract / 133 reject) exposed two systematic false-positive families: (1) raw tool_result/exec_command bodies (exit codes, "HELD" echoes, JSON cmd payloads) hit failure_fix_or_procedure — 119/119 extracted events were raw tool noise; (2) a harness wake-up template repeated across sessions was extracted per session — the exact cross-session-repetition shape the >=3-independent-sources rule would auto-crystallize. Both become anonymized eval/lifecycle/ fixtures; gate goes to heuristic-v3 before the Phase D branch merges.`

**v3 tuning round — done 2026-06-12 (closes the sign-off line's requirement):**

- Fixtures committed: `eval/lifecycle/history/c8_tool_noise.jsonl` (family 1) and `c9_wakeup_template.jsonl` + `c9_wakeup_template_b.jsonl` (family 2, two sessions because cross-session repetition is the dangerous shape), with reject-shaped `expectations.yaml` entries.
- Gate at `heuristic-v3`: events never extract on bare procedure signals (corrections and fix+resolution combos still can); message-side procedure requires failure/durable/explicit context; `resolved`/`decision` keyword matches are word-boundary (the template leaked through "unresolved"/"flow decisions" substrings); declared plans about durable topics extract as plan units (restores the c3 plan_to_decision path that the tightening had cut — `_looks_like_replacement` also now allows the plan→decision supersession pairing).
- Real-trace re-run (same 300 rows): **11 extract / 289 reject** (was 167/133). Extracted events: 2 (one repeated `--help`-style introspection dump surviving via correction words in help text — known residual, v4 candidate, accepted for now). Template messages extracted: 0. The remaining 9 extracted messages are instruction blobs/assistant rationale hitting `explicit_decision_or_rule` — borderline but not noise families.
- D5 harness extended to the full plan scope: c3 supersedes_plan, c1/c2 not_applicable, c8/c9 rejected, crystal-precision line printed (1.00 on fixtures). Full suite 535 passed; ruff + strict mypy clean.

**Residual risks accepted with this round** (owner is signing off on these, explicitly): the `--help`-dump event family (2 rows) still extracts; instruction-blob messages extract as borderline `explicit_decision_or_rule`; and the C5 harness type-accuracy semantics changed to "any passing ref predicts the gold type" (an entry with plan+decision refs is type-correct if the decision ref matches) — flag if you want gold-type tied to the superseding ref instead.

## Current state (verify before execution)

- Migration head is **8** (`latest_version() == 8`); Phase D adds entry 9 only on that base. If the worktree head is not 8, stop and rebase/re-plan instead of auto-renumbering (session-10 lesson).
- `memory_units` already has everything spec_02 sketches for units: status enum incl. `crystal_candidate`/`crystal`, `superseded_by` self-FK, `valid_from`/`valid_to`, `recall_count`/`last_recalled_at`, `content_hash` UNIQUE, sources/labels/status-log side tables, FTS + trigram sidecars. **Expect migration 9 to be small or empty** — likely additions are limited to whatever conflict bookkeeping needs persistence (see D2); verify against the spec data model before writing any DDL.
- Storage ops live in `core/store/memory_units.py` (`transition_memory_unit` with status-guard + log write, `restore_memory_unit` from status log, `find_unit_covering_sources`); CLI in `cli/commands/memory.py` with `lifecycle_lease:<project>` on every mutating apply.
- C5 harness `tests/test_lifecycle_acceptance.py` runs the C1 corpus end-to-end; the expectations already carry `phase_d_status` per entry (`supersedes_plan`, `superseded`, `crystal_candidate`, `conflict_review`, `retracted_or_superseded`) — recorded at C1 time precisely so D5 can assert them.
- Do not trust historical test counts; re-run gates in the actual worktree.

## Milestones

### D1 — Crystal policy + promotion/demotion (pure domain logic first)

New pure functions in `core/triage.py` or a new `core/consolidate.py` (decide at implementation; no I/O either way):

- Independent-source counting: "independent" = distinct `history_sessions.id`; non-trace sources count by distinct `source_ref`; repetition within one session is one source (spec verbatim).
- Crystal promotion predicate, spec-verbatim default policy:
  `explicit_user_confirmation OR independent_source_count >= 3 OR (unit_type = decision AND confidence_score >= 0.90 AND importance_score >= 0.80)`.
  Note the intentional conservatism: heuristic-gate confidence caps at 0.85, so the decision branch is reachable only via score evolution — do not "fix" this.
- Score evolution: +0.05 confidence per newly attached independent source, capped at 0.95; this is the ONLY automatic post-extraction score change.
- Demotion: `crystal_candidate → active_unit` on policy rejection, audited, re-candidacy allowed.

Storage side: `count_independent_sources(unit_id)`, attach-source-with-score-evolution, and the two status transitions through the existing guarded `transition_memory_unit`.

### D2 — Conflict detection (surface, never merge)

Scope to what C1's c6 fixture exercises: two same-type, same-project, overlapping-label active units whose contents disagree. First implementation is deterministic and cheap:

- Candidate pairing via FTS/label/type overlap among active units + crystals (no LLM in the default path; an LLM-backed comparator can follow the same injectable-Protocol pattern as the gate, later). **As-built deviation (first cut)**: pairing is pairwise in-memory over the consolidation unit list — project + label intersection + ≥3 shared claim tokens + contrast-term signal, with correction terms short-circuiting pairs to supersession; it does not use FTS and does not require same unit_type. FTS-based candidate pairing becomes relevant when unit counts outgrow the in-memory scan (revisit at Phase E alongside the LLM comparator).
- A surfaced conflict appears in consolidate dry-run AND apply output with both unit ids, titles, and sources; neither unit is modified automatically. Spec acceptance: "Conflicts are surfaced, not silently overwritten."
- Persistence question for the implementer to resolve against the spec data model *before* writing DDL: does a surfaced conflict need a table (so repeated runs don't re-announce acknowledged conflicts), or is stateless re-surfacing acceptable for the first cut? If a table is needed, that is migration 9's content; escalate to the owner with the tradeoff rather than deciding silently.

### D3 — Validator + consolidation-driven supersession

- Validator checks (spec list): source coverage (no source → no crystal, ever), content length bounds, duplicate titles within project, stale `source_ref` paths (the referenced upstream file no longer exists — report, don't auto-retract), unsupported-claim heuristic (content sentences with no source overlap; first cut can be conservative/coarse).
- Unit supersession via consolidation: when a newer unit covers the same subject with newer sources, mark the older one `superseded` with `superseded_by` set — reuse the zero-rowcount-guard transition; never delete; c4/c7 fixture semantics are the acceptance reference.

### D4 — `membox memory consolidate` CLI + decay

- `memory consolidate --dry-run/--apply` (mutually exclusive, same pattern as triage/extract). Apply takes `lifecycle_lease:<project>`; dry-run takes nothing and writes nothing (assert both, as C4 does).
- Dry-run output explains every would-be action: promotions (with which policy branch fired), demotions, conflicts, supersessions, validator rejections, decay candidates.
- Decay runs inside consolidate --apply (spec: no daemon, consolidate owns expiry): archive units past `valid_to`; surface (not archive) `plan`/`context` units past review horizon. Decay only archives — never retracts/deletes — and every transition is status-logged.
- CLI stays presentation-only; all logic in core/store layers.

### D5 — Acceptance run against C1 `phase_d_status` expectations

Extend `tests/test_lifecycle_acceptance.py` (or a sibling `test_lifecycle_consolidation.py`): run triage → extract → consolidate over the C1 corpus and assert the recorded `phase_d_status` per entry — c3 `supersedes_plan`, c4 `superseded`, c5 `crystal_candidate`, c6 `conflict_review`, c7 `retracted_or_superseded`, c1/c2 `not_applicable`. Also assert the spec's Phase D acceptance verbatim: no source → no crystal; conflicts surfaced not overwritten; user-confirmed units crystal with one source; automatic crystals require the threshold. Record crystal precision in the harness output alongside the C5 metrics. Query regression: `scripts/eval_memory.py --offline --budget 4000` unchanged (memory features stay out of default query until Phase E).

## Acceptance criteria (spec_02 Phase D, verbatim)

- No source means no crystal.
- Conflicts are surfaced, not silently overwritten.
- User-confirmed units can become crystals with one source.
- Automatic crystals require the approved threshold.

## Constraints for all dispatched subagents

- ~~Branch per milestone~~ — superseded by events: D1–D5 landed together on `feature/phase-d-consolidation`; all follow-up Phase D work continues on that branch until merge. Never commit to main/develop.
- **Pin the expected migration head (8) in every brief and verify the worktree base** before any schema work; if migration 9 turns out to be needed, pin 9 explicitly (session-10 lesson).
- Do not chain merge + pytest in one backgrounded Bash call (session-8 lesson).
- Gates per milestone: `uv run pytest` + `uv run ruff check` + `uv run mypy src tests` green; coverage ≥ 80%; Google docstrings; `from __future__ import annotations`; `scripts/update_repository_map.py` after structural changes.
- Tests use fakes only at I/O boundaries; lifecycle writes take the lease, not the RLock; FK ON / WAL / per-thread connections.
- Thresholds in D1 are spec calibration targets — implement them as named constants next to the gate keyword tables; changing a threshold is an owner decision, not a tuning knob.
- Subagents stop and escalate on judgment calls beyond the brief (notably the D2 persistence question) instead of guessing.

## Explicitly out of scope (Phase E/F)

`--include-memory` query fusion, memory budget partition, footer coverage for crystals/units, reinforcement updates to `recall_count`/`last_recalled_at` at query time, ranking weights (relevance × importance × recency), `membox distill`, any LLM-backed consolidation default (spec rejects auto-run LLM consolidation), `membox memory gc` standalone alias (only if consolidate-owned decay proves insufficient).
