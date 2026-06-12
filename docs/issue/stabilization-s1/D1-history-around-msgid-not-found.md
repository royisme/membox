# D1 — `membox history around <msg_id>` returns "no such message" for an id returned by SQLite

**Severity**: Major (read-path regression; blocks verification of the lifecycle chain end-to-end).
**Discovered**: Stabilization S1, 2026-06-12, against `main` @ `01258cc`.

## Repro

```bash
# After `history pull` has imported a session successfully (rows in history_messages):
MSGID=$(sqlite3 "$DB" "SELECT id FROM history_messages ORDER BY seq LIMIT 1;")
# MSGID = membox-capture:test-session-1:msg:m1
uv run membox history around --db "$DB" "$MSGID"
```

## Expected

The CLI prints the conversation window around the message (preceding message + target + following message, formatted).

## Actual

```
Error: no such message: membox-capture:test-session-1:msg:m1
```

## Why this matters

`history around` is the primary tool for inspecting a single step's context. If the id format the CLI accepts and the id format the storage layer exposes do not align, the read path is effectively inaccessible to a human operator — every retrieval starts in SQLite first. This also blocks verifying D3's lifecycle chain: once memory units exist, the way to read the source message back is `history around`.

## Likely cause

Two leading candidates — verify before fixing:

1. **Prefix / namespace mismatch in the CLI resolver.** The history_messages table stores the id as `{source_kind.value}:{external_id}:msg:{ext}` (per `membox_jsonl.py:117`), and `around` may look up by a different namespace (e.g. matching on `external_id` only or expecting a prefix the CLI does not strip).
2. **Argument type confusion.** The CLI may parse the positional argument as an integer (legacy shape, "around message seq N") rather than as a string id, causing the resolver to compare `1` against `membox-capture:test-session-1:msg:m1`.

## Suggested fix

- Read the resolver in `src/membox/cli/commands/history.py` for the `around` subcommand; identify which column it queries (id, seq, external_id) and why it returns no rows.
- Normalize the input: if the user passes a string containing `:`, query by `id`; if they pass a bare integer, query by `seq` and clarify the lookup column.
- Add a test that mirrors the smoke repro: import a fixture, query `history around` with the sqlite-returned id, assert non-empty output.

## Acceptance criteria

- `history around <id_from_sqlite>` returns the conversation window.
- The CLI help text for `around` clarifies whether the argument is a message id (string) or a seq (integer); the resolver matches that contract.
- A test in `tests/test_history_cli.py` (or equivalent) covers the id-based lookup.
