# D2 — `membox history search <q>` returns "No history hits" for words that exist in the imported text

**Severity**: Major (read-path regression; renders `history search` non-functional in the common case).
**Discovered**: Stabilization S1, 2026-06-12, against `main` @ `01258cc`.

## Repro

```bash
# Fixture: /tmp/membox-capture-sample.jsonl contains the literal text "membox" and
# "knowledge graph" in its messages. After a successful pull (4 messages, 1 event):
uv run membox history search --db "$DB" "membox"
```

## Expected

At least one hit referencing `membox-capture:test-session-1` with the matching message excerpt.

## Actual

```
No history hits.
```

## Why this matters

`history search` is the operator's main tool for finding prior decisions, command outputs, or context to re-attach to a current session. If the index never returns rows for known text, the entire trace layer is unreadable through the documented surface. This also blocks verifying any future corpus-quality work (recall/precision on the real history).

## Likely cause

Three candidates — verify by reading the code, not by guessing:

1. **FTS index is not populated on `import_history`.** The history pull path may write to `history_messages` but skip the FTS5 side table that `history search` queries.
2. **Project scoping drops the import's project.** The importer may record `project="smoke"` on the session row, and the search resolver may default to a different project filter (cwd-inferred, or empty) that excludes the row.
3. **Query is hitting a different table.** The search may target `messages_text` (knowledge-graph) when it should target `history_messages_fts` (trace).

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
