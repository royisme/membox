"""`membox extract-prompt` — emit the canonical extraction prompt for the agent.

The calling agent runs this prompt itself; membox never calls an LLM here.
The output is a self-contained prompt block the agent can consume and produce
JSON from, which then pipes directly into ``membox ingest-graph``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from membox.model.schema import ExtractedGraph
from membox.services.prompts import (
    EXTRACTION_SYSTEM_PROMPT,
    QUERY_KEYWORDS_SYSTEM_PROMPT,
)

_FOR_HELP = (
    "Which extraction prompt to emit. 'entities' (default) emits the full "
    "entity/relation graph prompt + ExtractedGraph JSON schema; 'query' emits "
    "the query-seed prompt + a JSON array-of-strings schema."
)

_QUERY_SCHEMA: dict[str, object] = {
    "type": "array",
    "items": {"type": "string"},
}

_FINAL_INSTRUCTION_ENTITIES = (
    "Output ONLY a single JSON object conforming to the schema above. "
    "Do not include any prose, code fences, or commentary — the JSON is "
    "piped directly into `membox ingest-graph`."
)
_FINAL_INSTRUCTION_QUERY = (
    "Output ONLY a JSON array of up to 3 entity name strings. "
    "Do not include any prose, code fences, or commentary."
)


def _read_source(source: str) -> str:
    """Resolve ``-`` (stdin) or a file path to a document string.

    Args:
        source: Either the literal ``"-"`` (read stdin) or a filesystem path.

    Returns:
        Document content as a UTF-8 string.

    Raises:
        typer.Exit: Exits with code 1 when the file does not exist.
    """
    if source == "-":
        return sys.stdin.read()
    path = Path(source)
    if not path.exists():
        typer.echo(f"Error: file not found: {source}", err=True)
        raise typer.Exit(1)
    return path.read_text(encoding="utf-8")


def _build_entities_prompt(content: str) -> str:
    """Compose the entities extraction prompt block.

    Args:
        content: Document text to wrap.

    Returns:
        Self-contained prompt the agent can execute verbatim.
    """
    schema_json = json.dumps(ExtractedGraph.model_json_schema(), indent=2, ensure_ascii=False)
    return (
        "## Instructions\n"
        f"{EXTRACTION_SYSTEM_PROMPT}\n"
        "\n"
        "## Output JSON schema\n"
        "Respond with a JSON object matching this schema:\n"
        "```json\n"
        f"{schema_json}\n"
        "```\n"
        "\n"
        "## Document\n"
        f"{content}\n"
        "\n"
        "## Final instruction\n"
        f"{_FINAL_INSTRUCTION_ENTITIES}\n"
    )


def _build_query_prompt(content: str) -> str:
    """Compose the query-keywords extraction prompt block.

    Args:
        content: Query text to wrap.

    Returns:
        Self-contained prompt the agent can execute verbatim.
    """
    schema_json = json.dumps(_QUERY_SCHEMA, indent=2, ensure_ascii=False)
    return (
        "## Instructions\n"
        f"{QUERY_KEYWORDS_SYSTEM_PROMPT}\n"
        "\n"
        "## Output JSON schema\n"
        "Respond with a JSON array matching this schema:\n"
        "```json\n"
        f"{schema_json}\n"
        "```\n"
        "\n"
        "## Query\n"
        f"{content}\n"
        "\n"
        "## Final instruction\n"
        f"{_FINAL_INSTRUCTION_QUERY}\n"
    )


def extract_prompt(
    file: str = typer.Argument(..., help="File to extract from, or '-' for stdin"),
    for_: str = typer.Option("entities", "--for", help=_FOR_HELP),
) -> None:
    """Print the canonical extraction prompt wrapping ``file``.

    The calling agent reads the printed prompt, runs it, and feeds the
    resulting JSON to ``membox ingest-graph``.  No LLM call is made by
    membox itself; no store is touched.

    Args:
        file: Path to the document, or ``"-"`` for stdin.
        for_: Prompt kind — ``"entities"`` (default) or ``"query"``.
    """
    kind = for_.strip().lower()
    if kind not in {"entities", "query"}:
        typer.echo(
            f"Error: --for must be 'entities' or 'query' (got '{for_}').",
            err=True,
        )
        raise typer.Exit(1)
    content = _read_source(file)
    prompt = _build_query_prompt(content) if kind == "query" else _build_entities_prompt(content)
    sys.stdout.write(prompt)
    if not prompt.endswith("\n"):
        sys.stdout.write("\n")


__all__ = ["extract_prompt"]
