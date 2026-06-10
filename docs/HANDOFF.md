# Membox — Session Handoff

> Single source of truth for cross-session context. Read at session start; update before ending.

**Last updated**: 2026-06-09 (session 2 — phases 1-7 implemented, awaiting review and merge)
**Current phase**: Phase 7 complete, unmerged; Phases 8-10 not started

---

## What's been done

### Session 1 (prior — date unknown)
- Project scaffolding: `pyproject.toml`, pre-commit hooks, CI, optional deps (`openai`, `tree-sitter`)
- Spec and roadmap docs written (`docs/spec.md`, `docs/roadmap.md`)
- CLAUDE.md created referencing AGENTS.md

### Session 2 (2026-06-09)
Implemented Phases 1-7 in full, each on its own feature branch. All phases pass all quality gates (mypy strict, ruff, coverage ≥ 80%).

| Phase | Branch | Key deliverable |
|-------|--------|-----------------|
| 1 | `feature/phase-1-framework-skeleton` | All modules + Protocol stubs; import chain wired; CLI shows all 6 commands |
| 2 | `feature/phase-2-storage` | SQLite DDL, CRUD, FK, WAL, per-thread connections, `find_or_create_entity` basic cascade |
| 3 | `feature/phase-3-predicate-normalization` | `_PREDICATE_CANONICAL` synonym dict; English + Chinese variants; 133 tests |
| 4 | `feature/phase-4-entity-disambiguation` | 3-layer cascade tests (alias/cosine/create); 8-thread concurrency; `_ControlledEmbedder` |
| 5 | `feature/phase-5-bfs-retrieval` | `bfs_query()` iterative BFS with cycle guard, evidence dedup; `MemoryAgent.retrieve` end-to-end |
| 6 | `feature/phase-6-concurrency` | WAL concurrent read-during-write test; 5×10 thread stress; 20-thread RLock contention |
| 7 | `feature/phase-7-openai` | `OpenAIExtractor`, `OpenAIEmbedder`; `examples/demo.py`; openai added to pre-commit mypy deps |

Final stats on `feature/phase-7-openai`: **165 tests, 97.82% coverage**, mypy strict + ruff clean.

---

## Current state

- **main** sits at `b480415` (docs-only: roadmap restructure). Zero implementation code is on main.
- **All 7 feature branches exist but are unmerged.** Each branch is a linear stack on top of the previous; they must be merged in order (1 → 2 → … → 7) or rebased/squashed before merge.
- The user's rule: **"每完成一个phase就进行一次提交，确认通过才能合并"** — each phase requires explicit user confirmation before merging.
- No phase has been confirmed/merged yet. Session ended before user reviewed any branch.
- `develop` branch exists but has no commits beyond main. Standard workflow would be: merge phases into `develop`, then `develop` → `main`.

### Locked architectural decisions
- No external services, HTTP/MCP servers, background daemons, or hosted vector databases (see `AGENTS.md`).
- Storage: local SQLite, WAL mode, `PRAGMA foreign_keys=ON`, per-thread connections, `RLock` for find-or-create.
- `find_or_create_entity` 3-layer cascade: alias → cosine embedding (≥ 0.85) → create. Phase 4 tests cover this; the implementation is in Phase 2's store but the test contract is Phase 4's.
- `bfs_query` is now implemented (Phase 5). No remaining Phase 5 stubs.
- Branches: always `feature/*` or `fix/*` — never commit to `main` or `develop` directly.

---

## Open questions / decisions needed

1. **Phase review queue** — User must confirm each phase before merging. Phases 1-7 are waiting. Confirm in order:
   - `feature/phase-1-framework-skeleton` → merge to develop?
   - `feature/phase-2-storage` → merge to develop?
   - … through Phase 7
2. **Merge strategy** — squash per phase, or preserve commit history? Not decided. Each branch currently has 2 commits (implementation + repository-map fix from pre-commit hook).
3. **Phase 8 (tree-sitter AST)** — Not started. Optional; requires `tree-sitter` + `tree-sitter-python` deps already in `pyproject.toml`.
4. **Phase 9 (Skill file / agent integration)** — Not started.
5. **Phase 10 (release / bump version)** — Not started. `scripts/bump_version.py` and `scripts/generate_changelog.py` exist.
6. **mypy `tree_sitter.*` unused override** — `pyproject.toml` shows `module = ['tree_sitter.*']` as unused (no tree-sitter code yet). Safe to leave until Phase 8.

---

## Next concrete steps

1. **User reviews Phase 1 branch** (`feature/phase-1-framework-skeleton`): run `git diff main..feature/phase-1-framework-skeleton`, confirm tests pass, say "merge phase 1".
2. **Merge phases sequentially** once each is confirmed. Suggested: merge to `develop`, verify, then `develop` → `main` when all confirmed.
3. After all 7 phases merged: update roadmap checkboxes, bump version to `0.2.0` with `uv run python scripts/bump_version.py 0.2.0`.
4. Begin Phase 8 (tree-sitter) on `feature/phase-8-tree-sitter` once Phase 7 is on `develop`.

---

## Notes / scratchpad

- Pre-commit `mypy` hook additional_dependencies: `[pytest, pydantic, rich, typer, openai]` — needed after Phase 7 to avoid `[import-not-found]` for `openai.*` in src files.
- `mypy_path = ["src"]` added to `pyproject.toml` in Phase 4 to fix pre-commit mypy failing on new test files (resolves `[import-not-found]` for `membox.*` imports inside function bodies in test files when only the new file is passed to mypy).
- `examples/demo.py` requires `OPENAI_API_KEY` env var and `membox[llm]`. Without it, exits gracefully with an error message.
- ResourceWarning about unclosed SQLite connections appears in test output (from Typer CLI runner creating agents that aren't explicitly closed). Warnings only, not failures. Could be addressed by adding a `close()` method to `KnowledgeStore` in a future cleanup.
