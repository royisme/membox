# AGENTS.md — Membox Agent Entry Point

This file serves as the **main bootstrap** for coding agents, retaining only startup rules and on-demand index reading to avoid loading full project details in every session.

## 1. Always-On Contract

Membox is a **local knowledge graph + RAG memory layer** designed for coding agents.

Default product constraints must not be violated:

- The `membox` CLI is the primary entry point.
- Core functions must not depend on external services, HTTP/MCP servers, background daemons, or hosted vector databases by default.
- Default storage is a local SQLite database file, supporting multi-process/multi-agent sharing on the local machine.
- Core logic should be implemented directly using Python + SQLite as a priority, avoiding heavy frameworks that hide critical behaviors.
- Tests must not rely on external LLMs or embedding APIs.

Any deviation from the above constraints requires explaining the trade-offs and obtaining user confirmation beforehand.

## 2. Context Loading Policy

Do not read all documents in every task. Load the minimal necessary context based on the task type:

| Task type | Read before editing |
|---|---|
| Product direction, data model, architecture, storage, retrieval, extraction, CLI behavior | `docs/spec.md` + `docs/agent/01-project-contract.md` |
| Module placement, SQLite safety, CLI implementation | `docs/agent/02-architecture-boundaries.md` |
| Dependencies, tests, validation, repository workflow, release automation | `docs/agent/03-engineering-rules.md` + `docs/workflow.md` |
| Coding style, typing, commit conventions | `docs/code-standards.md` |
| Branching, PR, CI workflow | `docs/workflow.md` |
| Implementation order or phase planning | `docs/roadmap.md` |
| Quick filesystem orientation | `docs/repository-map.md` |

If documents conflict, use this priority order:

1. `docs/spec.md`
2. `docs/roadmap.md`
3. `docs/code-standards.md`
4. `docs/workflow.md`
5. `pyproject.toml`

## 3. Repository Map Rule

`docs/repository-map.md` is generated and should stay current with structural changes.

- Do not update it manually.
- Run `uv run python scripts/update_repository_map.py` after creating, moving, or deleting files.
- A pre-commit hook also runs the generator. If it changes the file, review and stage the generated update before committing.

## 4. Work Rules for Agents

- Keep changes minimal and aligned with `docs/spec.md`.
- Do not mix storage, LLM calls, CLI presentation, and orchestration in one layer.
- Do not add heavy runtime dependencies unless the spec is intentionally revised.
- Prefer fake extractor/embedder implementations in tests.
- Every functional Python file under `src/`, `scripts/`, and `tests/` must start with a module docstring or leading `#` comment explaining its purpose.
- Before finishing code changes, run the narrowest relevant validation command first, then broader checks when practical.
- Do not manually edit the `PI-CREW` managed block below.

<!-- PI-CREW:GUIDANCE:START -->
<!-- PI-CREW:BLOCK:pi-crew-overview -->
## pi-crew

> Managed by **pi-crew v0.6.1** — do not edit this section manually.

pi-crew is a Pi extension for coordinated AI agent teams, workflows,
worktrees, and async task orchestration.
<!-- PI-CREW:/BLOCK:pi-crew-overview -->

<!-- PI-CREW:BLOCK:pi-crew-commands -->
### Quick Commands

| Command | Description |
|---|---|
| `team action='init'` | Initialize pi-crew for this project |
| `team action='run'` | Start a team run |
| `team action='status'` | Check run status |
| `team action='list'` | List available teams/agents/workflows |
| `team action='recommend'` | Get team/workflow recommendations |
<!-- PI-CREW:/BLOCK:pi-crew-commands -->
<!-- PI-CREW:GUIDANCE:END -->


<!-- headroom:rtk-instructions -->
# RTK (Rust Token Killer) - Token-Optimized Commands

When running shell commands, **always prefix with `rtk`**. This reduces context
usage by 60-90% with zero behavior change. If rtk has no filter for a command,
it passes through unchanged — so it is always safe to use.

## Key Commands
```bash
# Git (59-80% savings)
rtk git status          rtk git diff            rtk git log

# Files & Search (60-75% savings)
rtk ls <path>           rtk read <file>         rtk grep <pattern>
rtk find <pattern>      rtk diff <file>

# Test (90-99% savings) — shows failures only
rtk pytest tests/       rtk cargo test          rtk test <cmd>

# Build & Lint (80-90% savings) — shows errors only
rtk tsc                 rtk lint                rtk cargo build
rtk prettier --check    rtk mypy                rtk ruff check

# Analysis (70-90% savings)
rtk err <cmd>           rtk log <file>          rtk json <file>
rtk summary <cmd>       rtk deps                rtk env

# GitHub (26-87% savings)
rtk gh pr view <n>      rtk gh run list         rtk gh issue list

# Infrastructure (85% savings)
rtk docker ps           rtk kubectl get         rtk docker logs <c>

# Package managers (70-90% savings)
rtk pip list            rtk pnpm install        rtk npm run <script>
```

## Rules
- In command chains, prefix each segment: `rtk git add . && rtk git commit -m "msg"`
- For debugging, use raw command without rtk prefix
- `rtk proxy <cmd>` runs command without filtering but tracks usage
<!-- /headroom:rtk-instructions -->
