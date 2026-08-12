#!/usr/bin/env python3
"""Prepare every mandatory retrieval layer in one command."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from where_paper_go.api_assistant import ApiAssistantError, load_api_assistant_config
from where_paper_go.embeddings import (
    EmbeddingError,
    OpenAICompatibleEmbeddingProvider,
    build_graph_vector_index,
    default_graph_embedding_cache_path,
    load_embedding_config,
)
from where_paper_go.graph_index import default_graph_path
from where_paper_go.lightrag import (
    LightRAGRuntimeError,
    default_lightrag_working_dir,
    import_lightrag_graph,
)
from where_paper_go.recommender import DEFAULT_DATA_DIR, open_persistent_graph


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--graph", type=Path, default=None)
    parser.add_argument("--working-dir", type=Path, default=None)
    parser.add_argument(
        "--api-config",
        type=Path,
        default=None,
        help="必须同时包含 llm、embedding 和 search 配置。",
    )
    parser.add_argument("--embedding-cache", type=Path, default=None)
    parser.add_argument(
        "--force",
        action="store_true",
        help="重建图向量和 LightRAG；旧 LightRAG 目录会改名备份。",
    )
    parser.add_argument(
        "--force-graph",
        action="store_true",
        help="同时强制重建确定性属性图。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    graph_path = (args.graph or default_graph_path(args.data_dir)).resolve()
    working_dir = (
        args.working_dir or default_lightrag_working_dir(args.data_dir)
    ).resolve()
    cache_path = (
        args.embedding_cache or default_graph_embedding_cache_path(args.data_dir)
    ).resolve()

    try:
        api_root = load_api_assistant_config(args.api_config)
        search = api_root.get("search")
        if not isinstance(search, dict) or not str(search.get("provider") or "").strip():
            raise ApiAssistantError("强制检索需要显式的 search.provider 配置")
        embedding_config = load_embedding_config(args.api_config)
        provider = OpenAICompatibleEmbeddingProvider(embedding_config)
        graph, graph_rebuilt, graph_reason = open_persistent_graph(
            args.data_dir,
            graph_path,
            force_rebuild=args.force_graph,
        )
        graph.close()
    except (ApiAssistantError, EmbeddingError, OSError, ValueError, RuntimeError) as exc:
        parser.error(f"检索配置/图谱准备失败：{exc}")

    last_progress = -1

    def progress(completed: int, total: int) -> None:
        nonlocal last_progress
        percent = int(completed * 100 / max(1, total))
        if percent >= last_progress + 5 or completed == total:
            last_progress = percent
            print(
                f"embedding_progress={completed}/{total} ({percent}%)",
                file=sys.stderr,
                flush=True,
            )

    try:
        vector_result = build_graph_vector_index(
            graph_path,
            provider,
            cache_path,
            force=args.force,
            progress=progress,
        )
        manifest = import_lightrag_graph(
            graph_path,
            working_dir,
            args.api_config,
            cache_path,
            force=args.force,
        )
    except (EmbeddingError, LightRAGRuntimeError, OSError, ValueError, RuntimeError) as exc:
        parser.error(f"强制检索库准备失败：{exc}")

    print(
        json.dumps(
            {
                "status": "ready",
                "graph": str(graph_path),
                "graph_rebuilt": graph_rebuilt,
                "graph_reason": graph_reason,
                "vector": {
                    "model": vector_result.model,
                    "dimensions": vector_result.dimensions,
                    "entity_count": vector_result.entity_count,
                    "new_embeddings": vector_result.embedded_text_count,
                    "cached_embeddings": vector_result.cached_text_count,
                },
                "lightrag": {
                    "working_dir": str(working_dir),
                    "mode": manifest.get("query_mode"),
                    "storages": manifest.get("storages"),
                    "counts": manifest.get("counts"),
                },
                "llm_model": manifest.get("llm_model"),
                "search_provider": str(search.get("provider")),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
