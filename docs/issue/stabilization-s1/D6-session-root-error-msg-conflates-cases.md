# D6 — `MEMBOX_SESSION_ROOT` required-error conflates "no path argument" with "no session_root"

**Severity**: Minor (UX; not blocking, but confusing to first-time users).
**Discovered**: Stabilization S1, 2026-06-12, against `main` @ `01258cc`.

## Repro

```bash
# No env var, no flag, no path argument.
uv run membox history pull --db "$DB" --adapt membox --project smoke
```

## Expected

A message that distinguishes the two cases:
- "no path argument" → "no path argument provided; pass a file or set MEMBOX_SESSION_ROOT for auto-discovery"
- "path argument provided" → never raises this error (the path bypasses session_root lookup)

## Actual

```
Error: set MEMBOX_SESSION_ROOT or pass --session-root
```

The second half of that message ("or pass --session-root") is misleading. The smoke run did NOT pass a path, so it's actually about auto-discovery. But the message reads as "you could have passed --session-root and avoided this", which is correct for the auto-discovery case but obscures that the user has a third option (passing a `path` positional argument).

## Why this is wrong

The smoke run was the natural first thing a new user tries: "I just installed membox, I have no env var set, let me try `history pull` with no args to see what happens." The error should be friendly and list all the options (env, flag, positional path). The current message implies that the only two paths forward are env or flag.

## Suggested fix

In `src/membox/cli/commands/history.py`'s `_resolve_session_root`, branch on whether a path argument was provided:
- If yes: never raise; the path is sufficient.
- If no: raise with a message that lists all three: env, flag, or positional path.

Suggested wording: `Error: no path argument and no session root; pass a file path, set MEMBOX_SESSION_ROOT, or pass --session-root.`

## Acceptance criteria

- Running the repro prints the new message listing all three options.
- A test in `tests/test_history_cli.py` covers both branches (path provided → no error; no path + no env + no flag → new error).
