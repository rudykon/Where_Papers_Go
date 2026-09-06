#!/usr/bin/env python3
"""Build, validate, or export the database-free venue property graph."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from where_paper_go.graph_index import (
    GraphIndexError,
    VenueGraphIndex,
    build_graph,
    default_graph_path,
    export_lightrag_custom_kg,
    graph_source_digest,
    inspect_graph,
    vector_path_for_graph,
)
from where_paper_go.recommender import (
    DEFAULT_DATA_DIR,
    VenueCandidate,
    group_records,
    load_records,
    normalize_name,
    tokenize,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--graph", type=Path, default=None)
    parser.add_argument("--vector-file", type=Path, default=None)
    parser.add_argument("--force", action="store_true", help="强制重建属性图谱。")
    parser.add_argument("--check", action="store_true", help="只检查图谱及向量状态。")
    parser.add_argument(
        "--with-vectors",
        action="store_true",
        help="构建图节点向量文件；可能调用 embedding API。",
    )
    parser.add_argument(
        "--embedding-config",
        type=Path,
        default=None,
        help="含独立 embedding 配置节的 JSON 文件。",
    )
    parser.add_argument(
        "--embedding-cache",
        type=Path,
        default=None,
        help="gzip/JSON embedding 缓存；默认位于数据目录。",
    )
    parser.add_argument(
        "--force-vectors",
        action="store_true",
        help="即使模型与节点数未变化，也重写图节点向量。",
    )
    parser.add_argument(
        "--export-lightrag",
        type=Path,
        default=None,
        metavar="PATH",
        help="导出为 LightRAG insert_custom_kg 可直接接受的 JSON。",
    )
    parser.add_argument(
        "--summary-json",
        action="store_true",
        help="构建后输出完整节点/关系类型统计。",
    )
    return parser


def _build_base_graph(graph_path: Path, data_dir: Path, digest: str) -> None:
    records = load_records(data_dir)
    groups = group_records(records)
    result = build_graph(
        graph_path,
        data_dir,
        records,
        groups,
        tokenize=tokenize,
        normalize_alias=normalize_name,
        display_name_for_group=lambda group: VenueCandidate(
            list(group), list(group)
        ).name,
        matching_document_for_group=lambda group: VenueCandidate(
            list(group), list(group)
        ).matching_document(True),
        expected_digest=digest,
    )
    print(f"built_graph={result.path}")
    print(f"records={result.record_count}")
    print(f"entities={result.entity_count}")
    print(f"nodes={result.node_count}")
    print(f"edges={result.edge_count}")
    print(f"source_digest={result.source_digest}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.force_vectors and not args.with_vectors:
        parser.error("--force-vectors 需要同时指定 --with-vectors")

    graph_path = (args.graph or default_graph_path(args.data_dir)).resolve()
    vector_path = (args.vector_file or vector_path_for_graph(graph_path)).resolve()
    digest = graph_source_digest(args.data_dir)
    freshness = inspect_graph(graph_path, args.data_dir, expected_digest=digest)
    if args.check:
        print(f"graph={graph_path}")
        print(f"fresh={str(freshness.fresh).lower()}")
        print(f"reason={freshness.reason}")
        print(f"source_digest={freshness.source_digest}")
        vector_metadata: dict[str, str] = {}
        if freshness.fresh:
            try:
                with VenueGraphIndex(graph_path, vector_path=vector_path) as graph:
                    graph.validate()
                    vector_metadata = graph.vector_metadata()
            except (OSError, GraphIndexError, ValueError) as exc:
                print(f"vector_error={exc}")
        print(f"vector_available={str(bool(vector_metadata)).lower()}")
        print(f"vector_count={vector_metadata.get('vector_count', '0')}")
        if vector_metadata:
            print(f"vector_model={vector_metadata['vector_model']}")
            print(f"vector_dimensions={vector_metadata['vector_dimensions']}")
        return 0 if freshness.fresh else 1

    if args.force or not freshness.fresh:
        try:
            _build_base_graph(graph_path, args.data_dir, digest)
        except (FileNotFoundError, OSError, ValueError, GraphIndexError) as exc:
            parser.error(f"无法构建属性图谱：{exc}")
    else:
        print(f"graph_is_fresh={graph_path}")

    if args.with_vectors:
        from where_paper_go.embeddings import (
            EmbeddingError,
            OpenAICompatibleEmbeddingProvider,
            build_graph_vector_index,
            default_graph_embedding_cache_path,
            load_embedding_config,
        )

        try:
            provider = OpenAICompatibleEmbeddingProvider(
                load_embedding_config(args.embedding_config)
            )
            cache_path = args.embedding_cache or default_graph_embedding_cache_path(
                args.data_dir
            )
            result = build_graph_vector_index(
                graph_path,
                provider,
                cache_path,
                vector_path=vector_path,
                force=args.force_vectors,
                progress=lambda completed, total: print(
                    f"embedding_progress={completed}/{total}", file=sys.stderr
                ),
            )
        except (EmbeddingError, GraphIndexError, OSError, ValueError) as exc:
            parser.error(f"无法构建图节点向量：{exc}")
        print(f"built_vectors={result.entity_count}")
        print(f"unique_texts={result.unique_text_count}")
        print(f"new_embeddings={result.embedded_text_count}")
        print(f"cached_embeddings={result.cached_text_count}")
        print(f"vector_model={result.model}")
        print(f"vector_dimensions={result.dimensions}")

    if args.export_lightrag:
        try:
            counts = export_lightrag_custom_kg(graph_path, args.export_lightrag)
        except (GraphIndexError, OSError, ValueError) as exc:
            parser.error(f"无法导出 LightRAG 自定义图谱：{exc}")
        print(f"lightrag_export={args.export_lightrag.resolve()}")
        print(f"lightrag_counts={json.dumps(counts, ensure_ascii=False)}")

    if args.summary_json:
        try:
            with VenueGraphIndex(graph_path, vector_path=vector_path) as graph:
                print(json.dumps(graph.graph_summary(), ensure_ascii=False, indent=2))
        except (GraphIndexError, OSError, ValueError) as exc:
            parser.error(f"无法读取图谱统计：{exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
