#!/usr/bin/env python3
"""Build or validate the deprecated SQLite compatibility index."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from where_paper_go.recommender import (
    DEFAULT_DATA_DIR,
    VenueCandidate,
    group_records,
    load_records,
    normalize_name,
    tokenize,
)
from where_paper_go.search_index import (
    SearchIndexError,
    VenueSearchIndex,
    build_index,
    default_index_path,
    inspect_index,
    source_digest,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--index", type=Path, default=None)
    parser.add_argument("--force", action="store_true", help="即使索引仍然有效也强制重建。")
    parser.add_argument("--check", action="store_true", help="只检查索引新鲜度，不执行构建。")
    parser.add_argument(
        "--with-vectors",
        action="store_true",
        help="同时构建向量索引；可能调用 embedding API。",
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
        help="embedding 缓存路径；默认位于数据目录。",
    )
    parser.add_argument(
        "--force-vectors",
        action="store_true",
        help="即使模型和实体数未变化，也重新写入向量索引。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.force_vectors and not args.with_vectors:
        parser.error("--force-vectors 需要同时指定 --with-vectors")
    index_path = args.index or default_index_path(args.data_dir)
    digest = source_digest(args.data_dir)
    freshness = inspect_index(index_path, args.data_dir, expected_digest=digest)
    if args.check:
        print(f"index={index_path}")
        print(f"fresh={str(freshness.fresh).lower()}")
        print(f"reason={freshness.reason}")
        print(f"source_digest={freshness.source_digest}")
        vector_metadata: dict[str, str] = {}
        if freshness.fresh:
            try:
                with VenueSearchIndex(index_path) as search_index:
                    vector_metadata = search_index.vector_metadata()
            except (OSError, SearchIndexError, ValueError) as exc:
                print(f"vector_error={exc}")
        print(f"vector_available={str(bool(vector_metadata)).lower()}")
        print(f"vector_count={vector_metadata.get('vector_count', '0')}")
        if vector_metadata:
            print(f"vector_model={vector_metadata['vector_model']}")
            print(f"vector_dimensions={vector_metadata['vector_dimensions']}")
        return 0 if freshness.fresh else 1
    if freshness.fresh and not args.force and not args.with_vectors:
        print(f"index is fresh: {index_path}")
        return 0

    if not freshness.fresh or args.force:
        records = load_records(args.data_dir)
        groups = group_records(records)
        result = build_index(
            index_path,
            args.data_dir,
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
        print(f"built index: {result.path}")
        print(f"records={result.record_count}")
        print(f"entities={result.entity_count}")
        print(f"source_digest={result.source_digest}")
    else:
        print(f"base index is fresh: {index_path}")

    if args.with_vectors:
        from venue_embeddings import (
            EmbeddingError,
            OpenAICompatibleEmbeddingProvider,
            build_vector_index,
            default_embedding_cache_path,
            load_embedding_config,
        )

        try:
            provider = OpenAICompatibleEmbeddingProvider(
                load_embedding_config(args.embedding_config)
            )
            cache_path = args.embedding_cache or default_embedding_cache_path(
                args.data_dir
            )
            vector_result = build_vector_index(
                index_path,
                provider,
                cache_path,
                force=args.force_vectors,
                progress=lambda completed, total: print(
                    f"embedding_progress={completed}/{total}", file=sys.stderr
                ),
            )
        except (EmbeddingError, OSError, ValueError) as exc:
            parser.error(f"无法构建向量索引：{exc}")
        print("built vectors: true")
        print(f"vector_entities={vector_result.entity_count}")
        print(f"unique_texts={vector_result.unique_text_count}")
        print(f"new_embeddings={vector_result.embedded_text_count}")
        print(f"cached_embeddings={vector_result.cached_text_count}")
        print(f"vector_model={vector_result.model}")
        print(f"vector_dimensions={vector_result.dimensions}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
