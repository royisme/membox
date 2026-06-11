"""History log importers — one module per upstream format, parsing only.

Each importer implements the :class:`~membox.services.importers.base.HistoryImporter`
``Protocol``: it parses one source file into a normalized
:class:`~membox.model.schema.HistoryImportBatch`.  No business logic lives
here — redaction, preview capping, and upsert semantics are enforced by the
store layer, and incremental-import orchestration lives in
:mod:`membox.core.history_import`.
"""

from __future__ import annotations

from membox.services.importers.base import HistoryImporter
from membox.services.importers.codex_jsonl import CodexJsonlImporter
from membox.services.importers.membox_jsonl import MemboxHistoryJsonlImporter

IMPORTER_FORMATS: dict[str, type] = {
    "membox-history-jsonl": MemboxHistoryJsonlImporter,
    "codex-jsonl": CodexJsonlImporter,
}
"""CLI ``--format`` name → importer class."""


def get_importer(format_name: str) -> HistoryImporter:
    """Return an importer instance for a CLI ``--format`` name.

    Args:
        format_name: One of :data:`IMPORTER_FORMATS`.

    Returns:
        A fresh importer instance.

    Raises:
        ValueError: If the format name is unknown.
    """
    cls = IMPORTER_FORMATS.get(format_name)
    if cls is None:
        known = ", ".join(sorted(IMPORTER_FORMATS))
        msg = f"Unknown history format {format_name!r}; known formats: {known}"
        raise ValueError(msg)
    importer: HistoryImporter = cls()
    return importer


__all__ = [
    "IMPORTER_FORMATS",
    "CodexJsonlImporter",
    "HistoryImporter",
    "MemboxHistoryJsonlImporter",
    "get_importer",
]
