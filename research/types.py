"""Canonical types shared by every offline baseline and evaluator."""

from __future__ import annotations

from dataclasses import dataclass, field
import heapq
from typing import Any, Mapping, TypeAlias


@dataclass(frozen=True)
class Query:
    """A benchmark query whose publication date defines the leakage boundary."""

    query_id: str
    text: str
    publication_date: str
    title: str = ""
    abstract: str = ""
    doi: str = ""
    gold_venue_name: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VenueDocument:
    """A frozen venue profile available to retrieval."""

    doc_id: str
    text: str
    name: str = ""
    snapshot_date: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScoredDocument:
    """One item in a deterministic ranking."""

    doc_id: str
    score: float


Run: TypeAlias = dict[str, list[ScoredDocument]]
Qrels: TypeAlias = dict[str, dict[str, float]]


def sort_ranking(items: Mapping[str, float], top_k: int | None = None) -> list[ScoredDocument]:
    """Sort by descending score and stable document ID for reproducibility."""

    ranked = (
        ScoredDocument(doc_id=str(doc_id), score=float(score))
        for doc_id, score in items.items()
        if str(doc_id) and float(score) == float(score)  # discard NaN
    )
    if top_k is not None:
        top_k = max(0, top_k)
        # Retrieval commonly scores most of the 20K catalog.  A bounded heap
        # avoids sorting every candidate merely to keep the first 50--100.
        ranked = heapq.nsmallest(top_k, ranked, key=lambda item: (-item.score, item.doc_id))
    else:
        ranked = list(ranked)
    ranked.sort(key=lambda item: (-item.score, item.doc_id))
    return ranked
