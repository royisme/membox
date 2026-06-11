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

The following spec drafts are under review and not yet accepted:

| File | Topic |
|------|-------|
| [spec-v0.2-draft.md](spec-v0.2-draft.md) | Storage model v0.2 (per-project DB files) — pending review |
