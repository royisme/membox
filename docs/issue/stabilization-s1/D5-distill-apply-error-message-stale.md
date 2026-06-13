# D5 — `membox distill` without `--dry-run` errors with stale message inconsistent with `--apply` help

**Severity**: Minor (UX; not blocking).
**Discovered**: Stabilization S1, 2026-06-12, against `main` @ `01258cc`.

## Repro

```bash
uv run membox distill --db "$DB" --project smoke --root /path/to/repo
```

## Expected

A clear message: "`--apply` is not yet implemented; please use `--dry-run`."

## Actual

```text
Error: pass --dry-run for Phase F distill
```

## Why this is wrong

The help text for `--apply` reads `Reserved for a later Phase F apply path`, which already says it's not implemented. The error message repeats that, but with wording that doesn't quite match the help text and feels dismissive ("for Phase F distill" — which phase is Phase F? an operator new to the project would not know).

## Suggested fix

A one-liner copy fix in `src/membox/cli/commands/distill.py`. Suggested wording: `--apply is not yet implemented; use --dry-run to preview candidates.`

## Acceptance criteria

- Running the repro prints the new message.
- A test in `tests/test_distill.py` (or `tests/cli/test_distill_cli.py`) asserts the new wording.
