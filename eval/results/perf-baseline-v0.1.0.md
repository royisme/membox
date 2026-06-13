# Performance Baseline — No-API-Key Lifecycle (v0.1.0, S4)

Reference numbers for the deterministic, no-LLM lifecycle: **import**
(`history pull`) followed by **process** (`memory triage` → `memory extract`).
These are performance-sanity figures for the v0.1.0 stabilization track, not a
regression gate. Re-measure when the importer, triage gate, or extractor changes
materially.

## Method

- Target: the membox project's own real Claude Code history (a representative
  large history), pulled via auto-discovery from `~/.claude/projects`.
- Fresh temp DB; cold import, then an immediate idempotent re-import; then triage
  and extract over the full trace (`--limit 100000`, above the row count, so no
  sampling).
- Wall-clock is the `time` REAL value; peak RSS via `/usr/bin/time -l`.

```bash
DB=/tmp/membox-perf.db; rm -f "$DB" "$DB"-wal "$DB"-shm
MEMBOX_SESSION_ROOT="$HOME/.claude/projects" \
  uv run membox history pull --db "$DB" --adapt claude          # import (cold, then re-run = warm)
uv run membox memory triage  --apply --db "$DB" --project <p> --limit 100000   # process step 1
uv run membox memory extract --apply --db "$DB" --project <p> --limit 100000   # process step 2
```

## Baseline (2026-06-13, session 15)

**Environment:** macOS Darwin 25.5.0 arm64 · 12 CPU · 32 GB RAM · Python 3.13.9 · uv

| Phase | Wall (s) | Throughput | Output | Peak RSS |
|---|---|---|---|---|
| Import (cold) | 3.12 | ~4,030 rows/s | 21 sessions, 6,996 msgs, 5,568 events | 71.8 MB |
| Import (warm, idempotent) | 0.16 | — (0 new) | unchanged sources skipped | 37.3 MB |
| Triage | 1.05 | ~11,970 rows/s | 12,564 triage rows (100% of msgs+events) | 59.0 MB |
| Extract | 0.91 | ~1,080 units/s | 986 memory units | 45.4 MB |

- **Full lifecycle (import + triage + extract): 5.24 s** over 12,564 trace rows.
- **Idempotent re-import speedup: ~19.5×** (3.12 s → 0.16 s) — unchanged sources
  are skipped on size+mtime before any parse.
- **Final DB size on disk: 33.8 MB.**
- **Extract density:** 986 units / 12,564 rows ≈ 7.8% — expected; most rows are
  tool-call / file-content trace that deterministically extract to nothing.

## Notes vs. scope

- This baseline covers the **no-API-key** path only. The LLM-backed ingest path
  (`membox process` draining the async queue, or `ingest-graph`) is governed
  separately by the plan_03 ingest-perf mechanism numbers (embed cache, batch
  embedder, `MEMBOX_INGEST_CONCURRENCY`); no regression vs plan_03 is expected
  here because this path does not touch the embedder.
- Import is I/O-bound (reading session JSONL); triage and extract are pure
  in-process SQLite. No phase was a hotspot at this scale.
- `--limit` defaults to 100 on `memory triage`/`extract`; pass a high `--limit`
  to process a full history in one pass.
- Not yet stress-tested at the largest available scale (the local
  `~/.codex/sessions` tree is ~1.2 GB). Record a higher-scale data point on the
  next opportunity; the path is linear-friendly (streaming parse + SQLite), so no
  scaling cliff is expected.
