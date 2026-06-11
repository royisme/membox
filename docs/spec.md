# Membox — Spec Index

This file is the **precedence anchor** for the Membox project. Tools, agents, and
CLAUDE.md all reference `docs/spec.md`; this index remains at that path.

The normative content lives in the chapters under `docs/spec/`. New requirement
chapters are added as `docs/spec/spec_NN_<topic>.md`.

## Chapters

| # | File | Title |
|---|------|-------|
| 01 | [spec/spec_01_core.md](spec/spec_01_core.md) | Core: Knowledge Graph + RAG (v0.1 spec) |
| 02 | [spec/spec_02_memory_lifecycle.md](spec/spec_02_memory_lifecycle.md) | Agent Memory Lifecycle (Trace → Unit → Crystal) — accepted v2.3 (2026-06-11), Phase B implemented |

## Precedence

When documents conflict:
`docs/spec.md` (this index + its chapters) > `docs/roadmap.md` > `docs/code-standards.md` > `docs/workflow.md` > `pyproject.toml`

## Drafts

No spec drafts are currently under review.

Retired: [spec-v0.2-draft.md](spec-v0.2-draft.md) (per-project DB files) — rejected as a whole
(owner decision 2026-06-13): the two-DB storage model conflicts with the locked single-global-DB
decision, and its lifecycle/gate/provenance ideas were independently realized in
[spec_02_memory_lifecycle.md](spec/spec_02_memory_lifecycle.md). One idea survives: a **global
memory scope** (distilling cross-project preferences/procedures), formally parked as a future
`spec_NN` chapter in `docs/roadmap.md` Future Tracks. The draft file is kept as design history.
