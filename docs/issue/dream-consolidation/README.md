<!-- Design spec for the "dream" side: consolidation validator gate + MEMORY.md-style presentation. -->

# Dream side — consolidation validator gate + MEMORY.md presentation

**Status**: design accepted (2026-06-12) — pending implementation, staged.
**Scope**: enrich `membox memory consolidate` (the "dream" responsibility —
cross-session durable integration). Capture (`checkpoint`) and recall (`query`)
are out of scope here. See `docs/issue/checkpoint-command/README.md` for the
checkpoint/dream/distill split.

## Starting point — what already exists

`memory consolidate` is **deterministic** today and already has a validator
gate. `build_consolidation_plan(...)` (`core/consolidate.py:155`) runs
`validate_units` (`consolidate.py:264-305`) **before** any transition, and
already rejects:

- `no source` — unit has zero provenance rows.
- `content length out of bounds` — `<3` or `>4000` chars.
- `duplicate title` — same `(project, casefold(title))` as another unit.
- `stale source path` — an absolute DOCUMENT ref that no longer exists.
- `unsupported claim heuristic` (`_unsupported_claim`, weak).

Rejections surface as `ConsolidationIssue` in `plan.validator_rejections` and
print in the CLI (`memory.py:344`). **Promotion criterion** (`crystal_policy`,
`consolidate.py:122-136`): explicit user confirmation, OR ≥3 independent
sources, OR a DECISION with `confidence≥0.90 ∧ importance≥0.80`.

The `LLMComparator` seam (`consolidate.py:439`) exists but is **disabled** —
`memory_consolidate` passes `comparator=None` and discards `--no-llm`
(`memory.py:200`). So consolidation is fully deterministic in production.

So this is **extension, not greenfield**. The two enrichments — a stronger
validator gate and MEMORY.md-style presentation — split into a no-migration v1
and a schema-changing v2.

## Memory-unit model (current)

`MemoryUnitRecord` (`model/schema.py:147-170`) has: `unit_type`
(PREFERENCE/DECISION/PROCEDURE/FACT/LEARNING/PLAN/EVENT/CONTEXT), `status`
(…/CRYSTAL/…), `title`, `content`, `context`, `importance_score`,
`confidence_score`, `temporal_type`, `labels` (closed set), `sources`
(side-table provenance: source_kind/source_ref/source_message_id/quote).
A **crystal is a `status` value**, not a separate table.

**Absent fields**: `why`/rationale, `how_to_apply`, `next_step`, `category`.
These are the gating constraint on the richer validators below. Latest
migration = **0008**; new work needing columns = **migration 0009**.

---

## Part A — Validator gate

### A1 (v1, no migration) — harden the existing gate

Extend `validate_units` and the promotion path without schema changes:

1. **Promote-time budget guard**: today token budget is enforced only at render
   (`agent.py:646`). Add a per-crystal content-size ceiling at promotion so the
   durable set stays compact (reject/flag crystals whose `content` exceeds a
   crystal cap, distinct from the 4000-char unit cap).
2. **Provenance strength**: beyond "has ≥1 source", require for **promotion** at
   least one source with a non-empty `quote`, or ≥2 distinct `source_ref`s —
   prevents promoting a thinly-sourced claim to crystal.
3. **Vague-content heuristic**: flag units whose `content` is below a
   signal threshold (e.g. all-stopword, < N meaningful tokens, or pure
   restatement of `title`). Surface as a `ConsolidationIssue`, do not promote.
4. **Stale-path → all source kinds**: today only absolute DOCUMENT refs are
   checked; extend the existence check to any source carrying a filesystem path.
5. **Surfacing**: keep rejections in `plan.validator_rejections`; make the CLI
   summary count them explicitly (`N promoted, M rejected: <reasons>`), never
   silently drop.

### A2 (v2, migration 0009) — why / how / next_step fields

Add three nullable columns to `memory_units` (migration 0009) +
`MemoryUnitRecord` fields: `why` (rationale), `how_to_apply`, `next_step`.
Rationale: the user's own durable-memory format and the MiMo analysis both
treat **Why / How-to-apply** as first-class. Once present, add validators:

- DECISION/LEARNING/PROCEDURE units missing `why` → flag (not promotable to
  crystal without rationale).
- PROCEDURE/PLAN units with an empty or vague `next_step` → flag.
- `how_to_apply` empty on a PROCEDURE → flag.

**Cascade** (why this is v2, not v1): these fields must be **populated** by
extraction. That means the `ExtractedGraph` / memory-unit extraction path — both
the configured-LLM extractor and the **agent-as-provider** flow
(`extract-prompt` schema, see `docs/issue/agent-as-llm-provider/README.md`) —
must emit `why`/`how_to_apply`/`next_step`. The deterministic checkpoint
`extract` path leaves them null (heuristic capture can't synthesize rationale),
so these validators apply only to LLM-/agent-extracted units, and flagged-not-
failed for heuristic ones.

**Decision required from owner**: ship A2 (adds migration 0009 + extends the
extraction schema/prompts), or stay at A1 for v0.1 and defer A2. Recommendation:
**A1 now** (immediate, no migration), **A2 right after agent-as-provider lands**
(so the extraction schema gets `why`/`how`/`next_step` in one prompt change).

---

## Part B — MEMORY.md-style presentation

Today memory hits render as a **flat list**, two tiers (crystal/unit), via
`_render_memory_hits` (`agent.py:646`). MiMo's value is a **categorized**
durable view (Rules / Architecture decisions / Discovered durable knowledge /
Gotchas), each entry with its source.

### B1 (v1, no migration) — derive categories from `unit_type`

No `category` column needed; map the existing taxonomy:

| MEMORY.md section | Derived from |
|---|---|
| **Rules / Conventions** | `unit_type ∈ {PREFERENCE, PROCEDURE}` or `labels ∋ conventions` |
| **Architecture decisions** | `unit_type == DECISION` |
| **Discovered durable knowledge** | `unit_type ∈ {FACT, LEARNING}` |
| **Gotchas** | `_unsupported_claim` hits, or `labels ∋ {security, performance}` |

Deliver as a dedicated export, not by changing `query` output shape:
`membox memory export [--as memory-md] [--crystals-only] [--project …]` →
emits the categorized Markdown, each entry suffixed with provenance from the
`sources` side-table (`source_kind:source_ref`). This is a **derived cache /
view** — the KG + units remain the store of record ("raw evidence is source of
truth, memory is a derived index", consistent with Membox's provenance model).

Optionally let `query --include-memory` group hits by the same derived sections
instead of a flat list (small change in `_render_memory_hits`); keep it behind
the existing token budget.

### B2 (v2, optional, migration 0009) — stored `category`

Only if agent-authored category override is wanted (agent-as-provider could set
`category` explicitly). Otherwise B1's derivation is sufficient. Defer unless a
concrete need appears.

---

## LLM comparator (the dream's optional reasoning step)

Currently disabled. The dream's conflict-detection/merge step is the natural
place for **agent-as-provider** too: rather than wiring a separate LLM, a future
`membox memory consolidate --comparator agent` could emit conflict pairs for the
calling agent to adjudicate and feed verdicts back — same inversion as
`extract-prompt → ingest-graph`. **v1: leave deterministic** (comparator stays
`None`); track agent-comparator as a follow-on. Do **not** wire a background
auto-LLM dream (violates CLI-first, no-hidden-services).

## Cadence guidance (for the skill)

`consolidate` (dream) is **periodic and coarse**, not per-session: run after
several `checkpoint`s accumulate (crystals need ≥3 independent sources, so a
fresh project shows none — expected). Manual now; v0.2 may fire it on a hook
every N days (MiMo's 7-day default is a reasonable reference). `distill` is
coarser still (~30 days). Document all three cadences in `skills/membox-skill.md`.

## Implementation checklist

**v1 (no migration):**
- [ ] `core/consolidate.py`: add promote-time budget guard, provenance-strength
      rule, vague-content heuristic; extend stale-path to all path-bearing sources.
- [ ] CLI: explicit `N promoted / M rejected (reasons)` summary in
      `memory consolidate`.
- [ ] `cli/commands/memory.py`: `memory export --as memory-md [--crystals-only]`
      → categorized Markdown with provenance (derived from `unit_type`/`labels`).
- [ ] (optional) group `query --include-memory` hits by derived section.
- [ ] Tests: validator rejections (each new rule), export categorization +
      provenance, budget/rejection summary counts.

**v2 (migration 0009, after agent-as-provider):**
- [ ] Migration 0009: add `why`, `how_to_apply`, `next_step` (nullable) to
      `memory_units` + `MemoryUnitRecord`.
- [ ] Extend extraction (LLM + agent-as-provider `extract-prompt` schema) to
      populate them.
- [ ] Validators: missing-why / vague-next-step / empty-how on the relevant
      `unit_type`s (flag for heuristic-extracted, gate for LLM/agent-extracted).
- [ ] Tests + `scripts/update_repository_map.py`; green gate pytest+ruff+mypy.
