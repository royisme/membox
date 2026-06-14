# Automating Membox Capture with Hooks

A skill description only influences whether an agent *remembers to* run the
membox commands — it cannot guarantee they run. The two highest-value
touchpoints are also the least reliable to leave to the model: session-start
recall is easy to skip, and session-end capture is worse — sessions often
just end, so the model never gets a turn to checkpoint. The fix is to let the
**harness** run them deterministically. In Claude Code, wire them as hooks in
`.claude/settings.json`.

## SessionEnd → capture this session

This is the high-value, low-risk one — fires once per session and persists
the session into memory:

```json
{
  "hooks": {
    "SessionEnd": [
      { "hooks": [ { "type": "command",
        "command": "MEMBOX_SESSION_ROOT=\"$HOME/.claude/projects\" membox checkpoint --adapt claude || true" } ] }
    ]
  }
}
```

## SessionStart → recall context

Optional; it injects tokens and adds a little latency every session, so keep
the budget tight:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command",
        "command": "membox query \"project context, key decisions, conventions\" --include-memory --budget 3000 || true" } ] }
    ]
  }
}
```

## Notes

- Use **`SessionEnd`** for checkpoint (fires once per session), never `Stop`
  (fires every turn — far too frequent).
- The hook runs in the project directory, so membox infers `--project` from the
  git-root name; pass `--project` explicitly if your layout needs it.
- `|| true` keeps a membox hiccup from blocking session exit. Append
  `>> "$HOME/.membox/checkpoint.log" 2>&1` to capture output for debugging.
- **Safe to run every session.** `history pull` is incremental and idempotent:
  the `history_import_state` table skips unchanged session files by `mtime+size`,
  resumes the current file from its stored byte offset, and upserts on
  `(session_id, external_id)`; triage and extract then guard on gate-version and
  a `consumed_at` marker. Net work per run is **O(new messages)**, not O(all
  history). The *first* run does a one-time backlog import, and triage is capped
  by `--limit` (default 100) per run, so a large backlog drains over several
  sessions — run `membox checkpoint --apply` manually a few times to catch up.
