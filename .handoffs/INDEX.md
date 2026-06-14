# Handoff Index

Tracks every file in `.handoffs/`. `HANDOFF.md` is the project's durable
session memory; this directory is the working set for short-lived coordination
packets and the cross-thread transfer pattern. Read at session start; update
on lifecycle transitions.

## Active

<!-- New cross-thread transfer handoffs land here. -->

## Waiting

<!-- In-flight but blocked on user input, another thread, or an external condition. -->

## Recent Archive

<!-- Last 30 days; older entries can be pruned. -->

## Notes

- `HANDOFF.md` (sibling) is the durable project handoff document — read it for
  project state, open questions, and next concrete steps. It is *not* a
  cross-thread transfer packet and is not subject to the lifecycle states
  below.
- Cross-thread transfer files should be placed in
  `active/YYYY-MM-DD-<slug>.md`, follow the spec's transfer template, and be
  moved to `archive/YYYY/MM/` when resolved/cancelled/expired (or after 7
  days in `active/`, 14 in `waiting/`, or when > 12 KB).
