"""HistoryImporter protocol — the contract every log-format adapter satisfies.

Importers are pure file parsers: path in, normalized
:class:`~membox.model.schema.HistoryImportBatch` out.  They never touch the
database, never redact, and never truncate — those are store-boundary
concerns.  ``text`` / ``body`` therefore carry the full upstream payload, and
the same ``parse()`` call doubles as the resolver for ``history fetch``
(re-parse the upstream file, look up the record by identity).

Adapters may optionally implement ``discover_sessions`` to return session
file paths automatically — used by ``history pull`` when the CLI path
argument is omitted.  If an adapter does not implement discovery, the user
must provide an explicit file path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from membox.model.schema import HistoryImportBatch, HistorySessionRecord


class HistoryImporter(Protocol):
    """Parses one upstream session log into a normalized import batch."""

    format_name: str
    """CLI ``--adapt`` name this importer handles."""

    def parse(
        self,
        path: Path,
        *,
        project: str | None = None,
        offset_bytes: int = 0,
        next_seq: int = 0,
        session: HistorySessionRecord | None = None,
    ) -> HistoryImportBatch:
        """Parse a session log file, optionally resuming mid-file.

        Implementations must produce deterministic, file-position-independent
        record IDs so that repeated and incremental imports converge.
        """
        ...

    def discover_sessions(self, project_cwd: Path, session_root: Path) -> list[Path]:
        """Return session file paths matching *project_cwd* under *session_root*.

        Adapters that know their agent's session directory layout implement
        this so the user can run ``history pull`` without specifying files.

        Returns an empty list when discovery is unsupported or no sessions
        match.
        """
        ...
