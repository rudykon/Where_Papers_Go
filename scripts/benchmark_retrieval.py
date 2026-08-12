#!/usr/bin/env python3
"""Benchmark graph retrieval latency and reviewed-theme Top-K coverage."""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from where_paper_go.graph_index import VenueGraphIndex, default_graph_path
from where_paper_go.recommender import (
    DEFAULT_DATA_DIR,
    VenueCandidate,
    VenueRecord,
    build_candidates_from_groups,
    parse_targets,
    rank_candidates_indexed,
)


@dataclass(frozen=True)
class QualityCase:
    name: str
    targets: tuple[str, ...]
    query: str
    top_k: int
    expected: frozenset[str]
    reviewed_only: bool = False


QUALITY_CASES = (
    QualityCase(
        "network_exact",
        ("CCF-A",),
        "计算机网络",
        4,
        frozenset({"SIGCOMM", "MobiCom", "INFOCOM", "NSDI"}),
    ),
    QualityCase(
        "wireless_project",
        ("CCF-A", "THCPL-A", "中科院1区"),
        "截止期约束的联合波束与资源分配 无线边缘网络",
        2,
        frozenset({"TWC", "INFOCOM"}),
    ),
    QualityCase(
        "storage_cross_language",
        ("CCF-A",),
        "文件系统与存储可靠性",
        5,
        frozenset({"FAST", "OSDI", "SOSP"}),
    ),
    QualityCase(
        "language_models",
        ("CCF-A",),
        "大语言模型与自然语言处理",
        5,
        frozenset({"ACL", "ICLR", "ICML", "NeurIPS"}),
    ),
    QualityCase(
        "colloquial_wireless",
        ("CCF-A",),
        "手机在信号时好时坏时自动调整传输策略",
        3,
        frozenset({"SIGCOMM", "MobiCom", "INFOCOM"}),
    ),
    QualityCase(
        "fpga_specialized",
        ("CCF-A", "THCPL-A", "中科院1区"),
        "FPGA 可重构计算与高层综合",
        1,
        frozenset({"FPGA"}),
        True,
    ),
    QualityCase(
        "bioinformatics",
        ("CCF-A", "THCPL-A", "中科院1区"),
        "生物信息学 蛋白质组学 计算方法",
        2,
        frozenset({"ISMB", "RECOMB"}),
        True,
    ),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--graph", type=Path, default=None)
    parser.add_argument(
        "--legacy-index",
        type=Path,
        default=None,
        help="可选：与旧 SQLite/FTS5 索引比较相同 Top-K。",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def _identifier(candidate: VenueCandidate) -> str:
    return candidate.abbreviation or candidate.name


def _load_groups(store: Any, case: QualityCase) -> list[list[VenueRecord]]:
    targets = parse_targets(case.targets)
    rows = store.load_groups_for_targets([target.key for target in targets])
    return [
        [VenueRecord(**record) for record in group]
        for _entity_id, group in rows
    ]


def _run_case(store: Any, case: QualityCase) -> tuple[list[str], float]:
    targets = parse_targets(case.targets)
    groups = _load_groups(store, case)
    candidates = build_candidates_from_groups(
        groups,
        targets,
        reviewed_scope_only=case.reviewed_only,
    )
    started = time.perf_counter()
    ranked = rank_candidates_indexed(candidates, case.query, store)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return [_identifier(candidate) for candidate in ranked[: case.top_k]], elapsed_ms


def benchmark(
    graph_path: Path,
    legacy_index: Path | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    graph = VenueGraphIndex(graph_path)
    graph.validate()
    load_ms = (time.perf_counter() - started) * 1000
    legacy = None
    if legacy_index is not None:
        from venue_search_index import VenueSearchIndex

        legacy = VenueSearchIndex(legacy_index)
    try:
        cases = []
        elapsed_values = []
        matched_total = 0
        expected_total = 0
        for case in QUALITY_CASES:
            result, elapsed_ms = _run_case(graph, case)
            elapsed_values.append(elapsed_ms)
            matched = sorted(case.expected & set(result))
            matched_total += len(matched)
            expected_total += len(case.expected)
            row: dict[str, Any] = {
                "name": case.name,
                "query": case.query,
                "top_k": case.top_k,
                "expected": sorted(case.expected),
                "result": result,
                "matched": matched,
                "recall_at_k": len(matched) / len(case.expected),
                "query_ms": round(elapsed_ms, 3),
            }
            if legacy is not None:
                legacy_result, legacy_ms = _run_case(legacy, case)
                union = set(result) | set(legacy_result)
                row["legacy_result"] = legacy_result
                row["legacy_query_ms"] = round(legacy_ms, 3)
                row["legacy_top_k_jaccard"] = (
                    len(set(result) & set(legacy_result)) / len(union) if union else 1.0
                )
            cases.append(row)
        return {
            "graph": str(graph_path.resolve()),
            "graph_load_ms": round(load_ms, 3),
            "peak_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "case_count": len(cases),
            "micro_recall_at_k": matched_total / expected_total,
            "all_cases_full_recall": all(row["recall_at_k"] == 1.0 for row in cases),
            "query_ms": {
                "mean": round(statistics.mean(elapsed_values), 3),
                "median": round(statistics.median(elapsed_values), 3),
                "max": round(max(elapsed_values), 3),
            },
            "cases": cases,
        }
    finally:
        graph.close()
        if legacy is not None:
            legacy.close()


def render_text(result: dict[str, Any]) -> str:
    lines = [
        f"graph={result['graph']}",
        f"graph_load_ms={result['graph_load_ms']}",
        f"peak_rss_kb={result['peak_rss_kb']}",
        f"micro_recall_at_k={result['micro_recall_at_k']:.3f}",
        f"all_cases_full_recall={str(result['all_cases_full_recall']).lower()}",
        (
            "query_ms="
            f"mean:{result['query_ms']['mean']},"
            f"median:{result['query_ms']['median']},max:{result['query_ms']['max']}"
        ),
    ]
    for row in result["cases"]:
        line = (
            f"{row['name']}: recall@{row['top_k']}={row['recall_at_k']:.3f}; "
            f"ms={row['query_ms']}; result={','.join(row['result'])}"
        )
        if "legacy_top_k_jaccard" in row:
            line += f"; legacy_jaccard={row['legacy_top_k_jaccard']:.3f}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    graph_path = args.graph or default_graph_path(args.data_dir)
    result = benchmark(graph_path, args.legacy_index)
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_text(result), end="")
    return 0 if result["all_cases_full_recall"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
