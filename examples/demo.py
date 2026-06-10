"""End-to-end demo: ingest real documents and query the knowledge graph via OpenAI.

Usage:
    OPENAI_API_KEY=sk-... uv run python examples/demo.py

Requires the ``llm`` extra:
    uv pip install "membox[llm]"
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("Set OPENAI_API_KEY to run the live demo.", file=sys.stderr)
        sys.exit(1)

    try:
        from openai import OpenAI
    except ImportError:
        print(
            "openai package not installed. Run: uv pip install 'membox[llm]'",
            file=sys.stderr,
        )
        sys.exit(1)

    from membox import MemoryAgent
    from membox.services.embedding import OpenAIEmbedder
    from membox.services.extraction import OpenAIExtractor

    client = OpenAI(api_key=api_key)
    agent = MemoryAgent(
        extractor=OpenAIExtractor(client),
        embedder=OpenAIEmbedder(client),
        db_path="demo.db",
    )

    documents = [
        "我正在开发 codebase-rag 项目，主要使用 Python 和 AST 解析技术。",
        "codebase-rag 目前最大的挑战是如何处理超大型代码仓库的上下文裁剪。",
        "项目后端集成了 Neo4j 数据库用于图存储。",
    ]

    print("=== Ingesting documents ===")
    for doc in documents:
        print(f"  > {doc}")
        agent.ingest(doc)

    print("\n=== Listing entities ===")
    for entity in agent.list_entities():
        print(f"  [{entity.id}] {entity.name} ({entity.type})")

    print("\n=== Listing relations ===")
    for rel in agent.list_relations():
        print(f"  {rel.source_name} --({rel.predicate})--> {rel.target_name}")

    print("\n=== Query ===")
    query = "codebase-rag 项目用到哪些数据库和技术？现在有什么难点？"
    print(f"  Q: {query}")
    print()
    print(agent.query(query))


if __name__ == "__main__":
    main()
