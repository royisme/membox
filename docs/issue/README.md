# Issue Backlog

Working backlog of issues surfaced during the Stabilization Track. Each item is a self-contained file with repro, expected, actual, and acceptance criteria. New items get added to the appropriate subdirectory.

| Subdirectory | Source | Priority |
|---|---|---|
| [stabilization-s1/](./stabilization-s1/README.md) | End-to-end dogfooding run on `main` @ `01258cc`, 2026-06-12. | First; D3 unblocks everything else. |
| [pr5-deferred/](./pr5-deferred/README.md) | Review threads that were intentionally carried out of PR #5 into the Stabilization Track. | After D3; R1 first, then R2, then R3, then R4. |

**Convention**: file naming is `<id>-<slug>.md`, where `<id>` is a stable, sequential identifier (D1, D2, ... for dogfooding defects; R1, R2, ... for PR5-deferred review items). The `README.md` in each subdirectory is the index, with severity, title, file pointer, and proposed execution order.

**When to add a new item here**: any defect or review item that is not going to be fixed in the PR that surfaced it. The corresponding PR records the deferral with a link to the item file (e.g., `See docs/issue/pr5-deferred/R1-atomic-apply-batching.md`).
