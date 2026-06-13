# D2 — `membox history search <q>` returns "No history hits" for words that exist in the imported text

**Severity**: Major (read-path regression; renders `history search` non-functional in the common case).
**Discovered**: Stabilization S1, 2026-06-12, against `main` @ `01258cc`.

## Repro

```bash
# Fixture: /tmp/membox-capture-sample.jsonl contains the literal text "membox" and
# "knowledge graph" in its messages. After a successful pull (4 messages, 1 event):
uv run membox history search --db "$DB" "membox"
# If the fixture was imported with --project smoke, pin that scope explicitly:
uv run membox history search --db "$DB" --project smoke "membox"
# Or bypass project filtering while debugging the index path:
uv run membox history search --db "$DB" --all-projects "membox"
```

## Expected

At least one hit referencing `membox-capture:test-session-1` with the matching message excerpt.

## Actual

```text
No history hits.
```

## Why this matters

`history search` is the operator's main tool for finding prior decisions, command outputs, or context to re-attach to a current session. If the index never returns rows for known text, the entire trace layer is unreadable through the documented surface. This also blocks verifying any future corpus-quality work (recall/precision on the real history).

## Likely cause

Three candidates — verify by reading the code, not by guessing:

1. **FTS index is not populated on `import_history`.** The history pull path may write to `history_messages` but skip the FTS5 side table that `history search` queries.
2. **Project scoping drops the import's project.** The importer may record `project="smoke"` on the session row, and the search resolver may default to a different project filter (cwd-inferred, or empty) that excludes the row.
3. **Importer/index-builder writes do not match search expectations.** The `import_history` path, the importer that writes `history_messages`, or the index-builder step that updates `history_messages_fts` may be writing to the wrong table or failing to populate the history FTS side table. The mismatch could be between what the importer/index-builder writes and what `history search` expects when it queries `history_messages_fts`.

## Suggested fix

- Trace `import_history` end-to-end: does it call into a function that writes FTS rows? If not, that's the gap.
- Trace `history search`: which table does it query? Is there a project filter, and what is its default? Is the smoke `project="smoke"` reaching that filter?
- Whichever side is broken, fix it AND add a test that:
  1. Imports a fixture with known text.
  2. Runs `history search` for a word in that text.
  3. Asserts at least one hit.

## Acceptance criteria

- After importing the smoke fixture, `history search "membox"` returns ≥ 1 hit.
- The contract — does the search default to the current project, or is `--project` mandatory? — is documented in the `history search` help text.
- A test pins the FTS-vs-table path against future drift.

## Resolution (2026-06-13)

**The FTS index was never the problem.** Verified on current `main`: a real
`membox history pull --adapt claude` (21 sessions, 6875 messages, no
`--project` override) followed by `history search` returns hits at default
scope for every known word. The original "No history hits" repro was a
**test asymmetry** — the fixture was imported with `--project smoke` but searched
with the CWD-inferred project (`membox`), so the scope filter dropped every row.

The real latent defect was a **write/read project-inference divergence**: the
Claude importer labelled sessions with `basename(session.cwd)`
(`services/importers/claude_jsonl.py`), while `history search` infers scope via
`infer_project` = the **git-root** name (`cli/commands/history.py`). These agree
only when a session's cwd *is* the git root; for work done in a **subdirectory of
a larger repo / monorepo** they diverge and search silently returns nothing.

**Fix**: `core/history_import.py::history_pull` now resolves the project once via
`project or infer_project(project_cwd / "_")` and passes that to every
`import_history`, so the write path and the read path use the *same* function by
construction. Regression test:
`tests/test_history_importers.py::test_history_pull_project_label_matches_infer_project`
constructs a session whose recorded cwd basename (`subdir`) differs from the
git-root name (`gitroot`) and asserts search at default scope still finds it.
