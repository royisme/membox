"""HistoryImporter protocol — the contract every log-format adapter satisfies.

Importers are pure file parsers: path in, normalized
:class:`~membox.model.schema.HistoryImportBatch` out.  They never touch the
database, never redact, and never truncate — those are store-boundary
concerns.  ``text`` / ``body`` therefore carry the full upstream payload, and
the same ``parse()`` call doubles as the resolver for ``history fetch``
(re-parse the upstream file, look up the record by identity).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from membox.model.schema import HistoryImportBatch, HistorySessionRecord


class HistoryImporter(Protocol):
    """Parses one upstream session log into a normalized import batch."""

    format_name: str
    """CLI ``--format`` name this importer handles."""

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
        record IDs so that repeated and incremental imports converge (see the
        lifecycle design's incremental re-import semantics).

        Args:
            path: Source log file.
            project: Project override; None lets the importer infer one from
                the log itself (or leave it empty).
            offset_bytes: Byte offset to resume from (0 = full parse).  Only
                offsets previously returned as ``next_offset_bytes`` are
                valid.
            next_seq: First message ``seq`` value to assign when resuming.
            session: Previously imported session record, required when
                resuming past the log's session header.

        Returns:
            The normalized batch including resume state.

        Raises:
            FileNotFoundError: If ``path`` does not exist.
        """
        ...
