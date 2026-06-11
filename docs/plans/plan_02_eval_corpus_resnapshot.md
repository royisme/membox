# Plan 02 — Re-snapshot Eval Corpus + Update Temporal Gold Answers

> **Status**: Ready for execution · **Context**: HANDOFF "Next concrete steps" #2; queued since M4 ("Re-snapshotting is an M4-era task paired with updating temporal gold answers"). M4 supersession is now live (migration 7).
> **Baseline at risk**: 24/26 (92.3%) — gemini-3.1-flash-lite + gemini-embedding-001, `chunk_share=0.4`, `fts_fallback_k=10`, CJK sidecar. Changing the eval model invalidates baseline comparability (owner decision 2026-06-12).

## Why now

The 4 temporal gold questions (q23–q26) ask "current status" questions whose answers were true at snapshot time (2026-06-10) but have since changed in the source projects (e.g. q23: "are any membox feature branches merged?" — answer was "no, all unmerged"; today everything is merged). M4 supersession changes re-ingest semantics: a newer doc version asserting a different object marks the old relation `superseded_by`, and retrieval excludes superseded edges by default (`relations.py` superseded clause; `membox query --include-superseded` for audit). Re-snapshot + re-ingest is now the designed way to exercise this path — and the temporal questions must be re-verified against it.

## Facts that bound the work

- `eval/corpus/` is **gitignored** (private handoff docs from local sibling projects) — frozen since 2026-06-10 15:53, 9 files: `china-zhouyi-app--HANDOFF.md`, `easymem--HANDOFF.md`, `m5go--handoff-2026-03-16.md`, `membox--HANDOFF.md`, `moziBot--HANDOFF.md`, `pika--HANDOFF_ANSWER_TRANSCRIPT_DISPLAY.md`, `pika--HANDOFF_STT_COHERENT_PROFILE.md`, `playfun--EVENTS.md`, `playfun--HANDOFF.md`.
- `eval/gold.yaml`: 26 questions (15 single_hop / 7 multi_hop / 4 temporal). Fields: `id, category, question, expected_keywords, source, notes`. Hit = ALL `expected_keywords` appear (case-insensitive substring) in retrieval output (`scripts/eval_memory.py:241`). No checksums are asserted anywhere; `tests/test_eval_corpus.py` only requires source files to exist, ≥2 keywords per entry, and category minimums (12/6/4).
- Temporal question → source mapping: q23 → `membox--HANDOFF.md`, q24 → `playfun--HANDOFF.md`, q25 → `china-zhouyi-app--HANDOFF.md`, q26 → `easymem--HANDOFF.md`.
- Eval ingest is synchronous (`ingest_corpus` → `agent.ingest_file` per file); versioned re-ingest of the same `source_path` is idempotent and bumps doc version (M2).

## Steps

### Step 1 — Re-snapshot (requires user's machine; cannot be fully delegated to an isolated worktree agent)

For each of the 4 source projects whose handoff docs back temporal questions (membox, playfun, china-zhouyi-app, easymem): copy the current handoff doc over the old corpus file, **keeping the existing filename** (so `gold.yaml` `source` fields and `test_source_files_exist` stay valid). The other 5 files may be refreshed opportunistically but are not required. Source paths to check: sibling dirs under `~/software/myproject/` (e.g. `python/easymem`, the playfun/pika/moziBot/m5go/china-zhouyi-app project roots — locate each project's HANDOFF.md before copying; if a project no longer has one, keep the old snapshot and note it).

Keep a copy of the OLD 4 files (e.g. `eval/corpus-pre-resnapshot/`, gitignored) — needed for the supersession test in Step 3.

### Step 2 — Update temporal gold answers

For q23–q26, read the new snapshots and rewrite `expected_keywords` (and `notes`) to match current truth. Keywords must be answer-bearing and unlikely to appear by accident; ≥2 per question (test-enforced). If a project's status question is no longer meaningful (e.g. the tracked PR merged long ago and the doc no longer mentions it), replace the question with a new temporal question grounded in the new doc — keep temporal count ≥ 4 (`MIN_TEMPORAL`). Update non-temporal questions ONLY if a re-snapshotted doc no longer contains their answer (check all 26 against the new corpus before running the paid eval).

### Step 3 — Supersession-path verification (the M4-specific part)

On a throwaway DB: ingest the OLD membox handoff snapshot, then re-ingest the NEW one with a newer `--doc-date` and same `--project`/source identity. Assert:

1. Relations whose object changed are marked `superseded_by` (audit via `membox query --include-superseded`).
2. Default `membox query` for the q23 topic returns the NEW answer (superseded edges excluded).
3. Evidence rows for the old relations still exist (never deleted).

This can be an offline scripted check (DummyExtractor won't produce matching triples — use the Gemini provider for this one ingest pair, or assert at the storage layer with a crafted fixture if API cost is a concern; prefer the real-pipeline check since this is the first real-world supersession exercise).

### Step 4 — Full eval rerun + baseline record

`uv run python scripts/eval_memory.py --provider gemini --check-gates` on a fresh DB with the re-snapshotted corpus. Gates: overall ≥ 80%, temporal 4/4, multi-hop ≥ 6/7 (do not regress below the shipped 24/26 hit set except where a gold answer legitimately changed). Record the new baseline numbers in `docs/HANDOFF.md` (Notes section reference points) and note the snapshot date in `eval/gold.yaml`'s header comment. The old 24/26 number stays in HANDOFF history as the pre-resnapshot baseline.

Cost note: one full run ingests ~58+ chunks (~35 min serial on Gemini — see plan_03; consider landing plan_03's batching first if iteration is expected). Use `--max-files` smoke mode while editing gold answers; reserve the full run for sign-off.

## Acceptance

- All 26 gold questions answerable from the new corpus; `uv run pytest tests/test_eval_corpus.py` green locally.
- Supersession behavior verified per Step 3 (new answer wins by default, old evidence retained, `--include-superseded` exposes history).
- Full Gemini eval ≥ 80% with temporal 4/4; new baseline recorded in HANDOFF.
- No gold entry references a corpus file that doesn't exist; category minimums hold.

## Constraints

- `eval/corpus/` content remains gitignored; only `eval/gold.yaml` and doc updates are committed. Branch: `feature/eval-corpus-resnapshot-2026-06`.
- Step 1 needs the user's local sibling projects — confirm paths with the user if a handoff doc can't be located; do not invent corpus content.
- Frozen-data principle still applies AFTER the re-snapshot: the new snapshots become the new frozen corpus.
