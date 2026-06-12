# eval/ — Memory Quality Evaluation Corpus

This directory contains the evaluation corpus and gold-standard QA pairs for
Membox Phase 7.5 (Memory Quality Validation).

## Corpus

`eval/corpus/` holds snapshots of real handoff documents from across the user's
projects. These are read-only reference documents — do not edit them after the
initial snapshot.

**Snapshot date**: 2026-06-09

| File | Origin project | Language |
|------|---------------|----------|
| `membox--HANDOFF.md` | membox (this repo) | English |
| `easymem--HANDOFF.md` | easymem (Rust/Cargo) | English |
| `m5go--handoff-2026-03-16.md` | m5go (Python device server) | English |
| `moziBot--HANDOFF.md` | moziBot (TypeScript bot) | English |
| `china-zhouyi-app--HANDOFF.md` | 玲珑命理 (TypeScript/CF Workers) | Chinese |
| `pika--HANDOFF_STT_COHERENT_PROFILE.md` | pika (Electron/React app) | Chinese |
| `pika--HANDOFF_ANSWER_TRANSCRIPT_DISPLAY.md` | pika (Electron/React app) | Chinese |
| `playfun--HANDOFF.md` | playfun/Kishima (TypeScript game) | English |
| `playfun--EVENTS.md` | playfun/Kishima (TypeScript game) | English |

Naming convention: `<project>--<original-filename>`

## gold.yaml Schema

```yaml
- id: q01                          # unique identifier, zero-padded 2 digits
  category: single_hop             # single_hop | multi_hop | temporal
  question: "..."                  # natural-language question (English or Chinese)
  expected_keywords: ["...", "..."] # ALL must appear in retrieval output (case-insensitive)
  source: ["membox--HANDOFF.md"]   # which corpus file(s) contain the answer
  notes: "optional"                # rationale / section reference
```

### Categories

- **single_hop** (~12-15 entries): A fact stated in a single document. Tests basic
  recall and entity/relation extraction.
- **multi_hop** (~6-8 entries): Requires connecting facts across multiple documents
  or projects (e.g. "which projects use SQLite"). Tests graph traversal and cross-
  document retrieval.
- **temporal** (~4-5 entries): Questions about the *current* state or phase of a
  project. The correct answer must come from the *latest* version of the document.
  These are used in Phase 7.5 M3 to test supersession semantics — after re-ingesting
  an updated handoff, the old answer must be superseded.

### Hit criterion

A question is a **hit** if the retrieval output contains **all** `expected_keywords`
as case-insensitive substrings. Keyword lists are kept minimal (2-4 terms) and
specific enough to rule out accidental matches.

## Distribution (26 QA pairs)

| Category | Count |
|----------|-------|
| single_hop | 15 |
| multi_hop | 7 |
| temporal | 4 |

Bilingual split: ~7 questions include Chinese keywords or are asked in Chinese
(q12, q13, q18, q21, q25 fully/partially Chinese; q08, q09, q15 reference Chinese
terms from bilingual docs).

## How M3's eval_memory.py will consume this

`scripts/eval_memory.py` (Phase 7.5 M3) will:

1. Ingest every file in `eval/corpus/` into a fresh in-memory membox store.
2. For each entry in `eval/gold.yaml`, run `MemoryAgent.retrieve(question)`.
3. For each retrieval result, check whether **all** `expected_keywords` appear
   (case-insensitive) in the combined output string.
4. Report per-category hit rates and estimated output token counts.
5. Acceptance threshold: hit rate ≥ 80% within the eval token budget.
   **Budget is corpus-scale-dependent**: the pre-resnapshot corpus (2026-06-09,
   ~309 lines) was baselined at the default `--budget 2000` (24/26). The
   2026-06-11 re-snapshot corpus is ~3x larger (~912 lines); runs against it
   must use `--budget 4000` (baseline 26/26, temporal 4/4, multi-hop 7/7 on
   Gemini). At budget 2000 the new corpus structurally fails multi-hop
   (output truncation, not retrieval regression) — such runs are not
   comparable to either baseline.

The temporal category additionally tests re-ingestion: the test runner ingests an
updated version of the source document and verifies the retrieval now returns the
updated answer (old relation superseded, new one active).
