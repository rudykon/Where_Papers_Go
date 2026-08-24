"""Ranking metrics that retain every requested query in the denominator."""

from __future__ import annotations

import math
from statistics import fmean
from typing import Any, Mapping, Sequence

from .types import Qrels, Run


def _query_metrics(
    ranking: Sequence[str], relevance: Mapping[str, float], ks: Sequence[int]
) -> dict[str, float]:
    relevant = {doc_id for doc_id, gain in relevance.items() if gain > 0}
    total_relevant = len(relevant)
    result: dict[str, float] = {}
    for k in ks:
        top = ranking[:k]
        binary = [1 if doc_id in relevant else 0 for doc_id in top]
        hits = sum(binary)
        result[f"recall@{k}"] = hits / total_relevant if total_relevant else 0.0
        result[f"hit@{k}"] = 1.0 if hits else 0.0
        first = next((rank for rank, hit in enumerate(binary, 1) if hit), None)
        result[f"mrr@{k}"] = 1.0 / first if first else 0.0

        dcg = sum(
            (2.0 ** float(relevance.get(doc_id, 0.0)) - 1.0) / math.log2(rank + 1.0)
            for rank, doc_id in enumerate(top, 1)
            if relevance.get(doc_id, 0.0) > 0
        )
        ideal_gains = sorted(
            (float(gain) for gain in relevance.values() if gain > 0), reverse=True
        )[:k]
        ideal_dcg = sum(
            (2.0**gain - 1.0) / math.log2(rank + 1.0)
            for rank, gain in enumerate(ideal_gains, 1)
        )
        result[f"ndcg@{k}"] = dcg / ideal_dcg if ideal_dcg else 0.0

        precision_sum = 0.0
        cumulative_hits = 0
        for rank, hit in enumerate(binary, 1):
            if hit:
                cumulative_hits += 1
                precision_sum += cumulative_hits / rank
        # Standard AP@K uses all known relevant documents as the denominator;
        # therefore failure to retrieve a relevant item within K is penalized.
        result[f"ap@{k}"] = precision_sum / total_relevant if total_relevant else 0.0
    return result


def evaluate_run(
    run: Run,
    qrels: Qrels,
    *,
    query_ids: Sequence[str],
    ks: Sequence[int] = (1, 3, 5, 10, 20, 50),
) -> dict[str, Any]:
    """Compute macro Recall/Hit/MRR/nDCG/MAP at each cutoff."""

    cutoffs = tuple(sorted({int(k) for k in ks if int(k) > 0}))
    if not cutoffs:
        raise ValueError("at least one positive cutoff is required")
    if not query_ids:
        raise ValueError("evaluation query_ids cannot be empty")
    per_query: dict[str, dict[str, float]] = {}
    for query_id in query_ids:
        ranking = [item.doc_id for item in run.get(query_id, ())]
        # De-duplicate defensively: duplicate IDs must not earn repeated credit.
        ranking = list(dict.fromkeys(ranking))
        per_query[query_id] = _query_metrics(ranking, qrels.get(query_id, {}), cutoffs)
    aggregate: dict[str, float] = {}
    for k in cutoffs:
        for metric in ("recall", "hit", "mrr", "ndcg"):
            key = f"{metric}@{k}"
            aggregate[key] = fmean(values[key] for values in per_query.values())
        aggregate[f"map@{k}"] = fmean(values[f"ap@{k}"] for values in per_query.values())
    return {
        "query_count": len(query_ids),
        "cutoffs": list(cutoffs),
        "aggregate": aggregate,
        "per_query": per_query,
    }


def stratified_metrics(
    evaluation: Mapping[str, Any],
    query_groups: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    """Aggregate an existing per-query evaluation by field/quartile/etc."""

    per_query = evaluation.get("per_query")
    if not isinstance(per_query, Mapping):
        raise ValueError("evaluation is missing per_query metrics")
    grouped: dict[str, list[Mapping[str, float]]] = {}
    for query_id, values in per_query.items():
        group = str(query_groups.get(query_id, "unknown"))
        grouped.setdefault(group, []).append(values)
    result: dict[str, dict[str, Any]] = {}
    for group, rows in sorted(grouped.items()):
        keys = sorted(rows[0]) if rows else []
        result[group] = {
            "query_count": len(rows),
            "aggregate": {key: fmean(float(row[key]) for row in rows) for key in keys},
        }
    return result
