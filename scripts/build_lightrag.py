#!/usr/bin/env python3
"""Build or validate the mandatory local LightRAG venue workspace."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from where_paper_go.graph_index import GraphIndexError, VenueGraphIndex, default_graph_path
from where_paper_go.lightrag import (
    LightRAGRuntimeError,
    default_lightrag_working_dir,
    import_lightrag_graph,
)
from where_paper_go.recommender import DEFAULT_DATA_DIR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--graph", type=Path, default=None)
    parser.add_argument("--working-dir", type=Path, default=None)
    parser.add_argument("--api-config", type=Path, default=None)
    parser.add_argument("--embedding-cache", type=Path, default=None)
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制重建；已有工作目录会改名备份。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只校验属性图和统计导入规模，不调用 API。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    graph_path = (args.graph or default_graph_path(args.data_dir)).resolve()
    working_dir = (
        args.working_dir or default_lightrag_working_dir(args.data_dir)
    ).resolve()
    try:
        with VenueGraphIndex(graph_path) as graph:
            graph.validate()
            custom_kg = graph.to_lightrag_custom_kg()
    except (GraphIndexError, OSError, ValueError) as exc:
        parser.error(f"无法读取属性图谱：{exc}")

    counts = {key: len(value) for key, value in custom_kg.items()}
    print(json.dumps({"graph": str(graph_path), **counts}, ensure_ascii=False))
    if args.dry_run:
        return 0
    try:
        manifest = import_lightrag_graph(
            graph_path,
            working_dir,
            args.api_config,
            args.embedding_cache,
            force=args.force,
        )
    except (LightRAGRuntimeError, OSError, ValueError, RuntimeError) as exc:
        parser.error(f"LightRAG 导入失败：{exc}")
    print(f"lightrag_working_dir={working_dir}")
    print(f"query_mode={manifest.get('query_mode')}")
    print("vector_storage=NanoVectorDBStorage")
    print("graph_storage=NetworkXStorage")
    print("neo4j_used=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
