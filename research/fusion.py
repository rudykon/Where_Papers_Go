"""Rank fusion baselines, including a small reproducible learned fusion."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping, Sequence

from .types import Qrels, Run, ScoredDocument, sort_ranking


def rrf_fuse(
    runs: Mapping[str, Run],
    *,
    top_k: int,
    rrf_k: int = 60,
    weights: Mapping[str, float] | None = None,
) -> Run:
    """Weighted reciprocal-rank fusion over canonical runs."""

    if rrf_k < 1:
        raise ValueError("rrf_k must be positive")
    if not runs:
        return {}
    weights = dict(weights or {})
    query_ids = sorted({query_id for run in runs.values() for query_id in run})
    output: Run = {}
    for query_id in query_ids:
        scores: dict[str, float] = {}
        for name, run in runs.items():
            weight = float(weights.get(name, 1.0))
            for rank, item in enumerate(run.get(query_id, ()), 1):
                scores[item.doc_id] = scores.get(item.doc_id, 0.0) + weight / (rrf_k + rank)
        output[query_id] = sort_ranking(scores, top_k)
    return output


def _normalized_channel_features(runs: Mapping[str, Run], query_id: str) -> dict[str, tuple[float, ...]]:
    """Create scale-safe score/rank features for the union candidate pool."""

    names = tuple(runs)
    candidates = {
        item.doc_id
        for run in runs.values()
        for item in run.get(query_id, ())
    }
    features = {candidate: [0.0] * len(names) for candidate in candidates}
    for channel_index, name in enumerate(names):
        ranking = runs[name].get(query_id, ())
        if not ranking:
            continue
        values = [item.score for item in ranking]
        lower, upper = min(values), max(values)
        span = upper - lower
        for rank, item in enumerate(ranking, 1):
            score_component = (item.score - lower) / span if span > 1e-12 else 1.0
            rank_component = 1.0 / math.log2(rank + 1.0)
            features[item.doc_id][channel_index] = 0.7 * score_component + 0.3 * rank_component
    return {candidate: tuple(values) for candidate, values in features.items()}


@dataclass(frozen=True)
class LearnedFusionReport:
    weights: Mapping[str, float]
    training_queries: int
    training_pairs: int
    skipped_queries: int
    epochs: int
    learning_rate: float
    l2: float


class LearnedLinearFusion:
    """Pairwise logistic fusion trained only on the temporal train split.

    It learns one transparent weight per retrieval channel.  Training uses the
    relevant venue against hard negatives from the channel union, so it is a
    research baseline rather than a claim of model novelty.
    """

    def __init__(
        self,
        *,
        epochs: int = 200,
        learning_rate: float = 0.08,
        l2: float = 0.01,
        hard_negatives: int = 20,
    ) -> None:
        if epochs < 1 or learning_rate <= 0 or l2 < 0 or hard_negatives < 1:
            raise ValueError("invalid learned-fusion hyperparameters")
        self.epochs = epochs
        self.learning_rate = learning_rate
        self.l2 = l2
        self.hard_negatives = hard_negatives
        self.channel_names: tuple[str, ...] = ()
        self.weights: tuple[float, ...] = ()
        self.report: LearnedFusionReport | None = None

    def fit(
        self,
        runs: Mapping[str, Run],
        qrels: Qrels,
        train_query_ids: Sequence[str],
    ) -> "LearnedLinearFusion":
        if not runs:
            raise ValueError("learned fusion requires at least one source run")
        self.channel_names = tuple(runs)
        pairs: list[tuple[float, ...]] = []
        used_queries = 0
        skipped_queries = 0
        for query_id in train_query_ids:
            features = _normalized_channel_features(runs, query_id)
            relevant = [
                doc_id
                for doc_id, gain in qrels.get(query_id, {}).items()
                if gain > 0 and doc_id in features
            ]
            if not relevant:
                skipped_queries += 1
                continue
            used_queries += 1
            positive = max(relevant, key=lambda doc_id: sum(features[doc_id]))
            negatives = sorted(
                (doc_id for doc_id in features if doc_id not in relevant),
                key=lambda doc_id: (-sum(features[doc_id]), doc_id),
            )[: self.hard_negatives]
            positive_features = features[positive]
            for negative in negatives:
                pairs.append(
                    tuple(
                        positive_features[index] - features[negative][index]
                        for index in range(len(self.channel_names))
                    )
                )
        weights = [1.0 / len(self.channel_names)] * len(self.channel_names)
        if pairs:
            for _epoch in range(self.epochs):
                gradient = [self.l2 * weight for weight in weights]
                for difference in pairs:
                    margin = sum(weight * value for weight, value in zip(weights, difference))
                    # derivative of log(1 + exp(-margin)), written stably
                    coefficient = -1.0 / (1.0 + math.exp(min(60.0, margin)))
                    for index, value in enumerate(difference):
                        gradient[index] += coefficient * value / len(pairs)
                for index in range(len(weights)):
                    weights[index] -= self.learning_rate * gradient[index]
        self.weights = tuple(weights)
        self.report = LearnedFusionReport(
            weights=dict(zip(self.channel_names, self.weights)),
            training_queries=used_queries,
            training_pairs=len(pairs),
            skipped_queries=skipped_queries,
            epochs=self.epochs,
            learning_rate=self.learning_rate,
            l2=self.l2,
        )
        return self

    def run(self, runs: Mapping[str, Run], *, query_ids: Sequence[str], top_k: int) -> Run:
        if not self.channel_names or not self.weights:
            raise RuntimeError("learned fusion must be fitted before run()")
        if tuple(runs) != self.channel_names:
            raise ValueError("fusion source names/order differ from fitted runs")
        output: Run = {}
        for query_id in query_ids:
            features = _normalized_channel_features(runs, query_id)
            scores = {
                doc_id: sum(weight * value for weight, value in zip(self.weights, values))
                for doc_id, values in features.items()
            }
            output[query_id] = sort_ranking(scores, top_k)
        return output


def truncate_run(run: Run, query_ids: Sequence[str], top_k: int) -> Run:
    return {query_id: list(run.get(query_id, ()))[:top_k] for query_id in query_ids}
