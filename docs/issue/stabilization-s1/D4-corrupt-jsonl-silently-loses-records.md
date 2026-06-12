# D4 — Corrupted JSONL line is silently absorbed; importer reports success with reduced counts and no warning

**Severity**: Major (silent data loss; an operator who trusts the summary line will not notice that records were dropped).
**Discovered**: Stabilization S1, 2026-06-12, against `main` @ `01258cc`.

## Repro

```bash
# Take the 6-line smoke fixture (1 session header, 4 messages, 1 event) and
# inject a non-JSON line right after the header.
SAMPLE2=$WORK/bad.jsonl
head -1 $SAMPLE > $SAMPLE2
echo 'this line is not json' >> $SAMPLE2
sed -n '2,$p' $SAMPLE >> $SAMPLE2
# Fresh DB:
uv run membox history pull --db "$DB" --adapt membox --project smoke "$SAMPLE2"
```

## Expected

Either (a) the import fails loudly on the malformed line, or (b) it succeeds but the summary line reports `Imported N messages, M events, skipped K malformed lines` so the operator can investigate.

## Actual

```
Imported 4 messages, 1 events into session membox-capture:test-session-1
```

…but the source file has 4 valid messages after the header. The summary is not obviously wrong (it matches the good records) only because the smoke fixture is small. A larger file with one bad line in the middle would import N-1 records and never warn. The `iter_jsonl` helper is documented to skip unparseable lines silently (per `tests/test_history_importers.py::test_iter_jsonl_skips_blank_and_garbage_lines`), so the importer inherits the silent-skip behavior without surfacing it.

## Why this matters

JSONL files in the wild are written by agents that crash mid-line, by humans who edit them, by log rotation. Silent loss of half a session's decisions is the worst possible failure mode: the operator sees `Imported 200 messages`, trusts it, and never knows that 50 records were lost on line 4,732. The Trace layer's whole value proposition is "you can re-read every decision that was made" — silent loss breaks that.

## Suggested fix

1. `import_history` counts the number of `iter_jsonl` skips and includes them in its return shape (a `skipped_lines: int` field on the import result is enough).
2. The CLI surfaces `skipped_lines` in the pull summary: `Imported N messages, M events, K malformed lines skipped` — and a non-zero K exits with a non-zero status (or prints a clear warning to stderr).
3. The fix is backward compatible: a fixture with no skipped lines prints the same summary as before.

## Acceptance criteria

- A test inserts a non-JSON line into the middle of an otherwise valid fixture and asserts the summary line reports the skip count.
- The smoke repro above, when re-run on a fresh DB, prints something like `Imported 4 messages, 1 events, 1 malformed line skipped` (or fails outright — both are acceptable; silent success is not).
- `tests/test_history_importers.py` already covers `iter_jsonl` skipping; add coverage that the history importer surface reports it.
