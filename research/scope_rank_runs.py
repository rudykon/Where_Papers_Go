"""Formal, leakage-bounded SCOPE-Rank score-run suite.

The production ``where_paper_go.scope_rank`` module exposes deterministic
primitives.  This module turns those primitives into a frozen offline research
method: it imports exact M3 runs, trains only on a deterministic partition of
the temporal train split, emits every named ablation, and records calibrated
abstention and evidence provenance without consulting validation/test labels.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from time import perf_counter
from typing import Any, Mapping, Sequence
import uuid

import numpy as np

from where_paper_go.scope_rank import (
    AdaptiveBudgetAllocator,
    ChannelObservation,
    ChannelTraits,
    QueryProfile,
    SelectiveCalibrator,
)

from .baselines import BM25Baseline, tokenize
from .data import (
    DatasetBundle,
    ResearchDataError,
    build_run_binding,
    canonical_json_sha256,
    load_jsonl_corpus,
    load_recent_journal_dataset,
    load_score_run,
    ordered_ids_sha256,
    runtime_provenance,
    sha256_file,
    temporal_split,
    write_run,
)
from .leakage import audit_leakage
from .prototype_vectors import validate_reference_binding
from .types import Query, Run, ScoredDocument, VenueDocument, sort_ranking


_FORBIDDEN_QUERY_FIELDS = frozenset(
    {
        "case_id",
        "gold_entity_id",
        "gold_journal_id",
        "gold_journal_name",
        "gold_jcr_quartile",
        "id",
        "journal_name",
        "label",
        "paper_id",
        "relevance",
        "split",
        "venue_id",
        "broad_field",
        "primary_field",
    }
)
_DENSE_SOURCES = frozenset(
    {
        "bge_m3",
        "specter2",
        "scincl",
        "cross_encoder",
    }
)
_RECALL_SOURCE_ORDER = (
    "bm25",
    "bge_m3",
    "specter2",
    "scincl",
    "property_graph",
    "lightrag",
    "subject_route",
)
_FEATURE_SOURCE_ORDER = (*_RECALL_SOURCE_ORDER, "cross_encoder")


@dataclass(frozen=True)
class ScopeQueryRepresentation:
    query_id: str
    text: str
    title_terms: tuple[str, ...]
    abstract_terms: tuple[str, ...]
    ambiguity: float
    cross_disciplinary: float
    language: str
    token_count: int
    article_type: str
    allowed_quartiles: tuple[str, ...]
    representation_source_fields: tuple[str, ...]


@dataclass(frozen=True)
class VariantSpec:
    name: str
    removed_sources: tuple[str, ...] = ()
    adaptive_budget: bool = True
    include_missingness: bool = True
    include_constraint_features: bool = True
    calibrated: bool = True
    fusion: str = "learned"


@dataclass(frozen=True)
class RouteResult:
    quotas: Mapping[str, int]
    channel_scores: Mapping[str, float]
    pool: tuple[str, ...]
    hard_filtered_count: int


@dataclass(frozen=True)
class PairwiseRankerReport:
    feature_count: int
    training_query_count: int
    skipped_query_count: int
    pair_count: int
    epochs: int
    learning_rate: float
    l2: float
    final_loss: float


class PairwiseLinearRanker:
    """Deterministic NumPy pairwise logistic ranker with transparent weights."""

    SCHEMA_VERSION = 1

    def __init__(self, feature_names: Sequence[str]) -> None:
        self.feature_names = tuple(str(value) for value in feature_names)
        if not self.feature_names or len(set(self.feature_names)) != len(
            self.feature_names
        ):
            raise ValueError("ranker feature names must be unique and non-empty")
        self.scales = np.ones(len(self.feature_names), dtype=np.float64)
        self.weights = np.zeros(len(self.feature_names), dtype=np.float64)
        self.fitted = False
        self.report: PairwiseRankerReport | None = None

    def vector(self, features: Mapping[str, float]) -> np.ndarray:
        values = np.asarray(
            [float(features.get(name, 0.0)) for name in self.feature_names],
            dtype=np.float64,
        )
        if not np.isfinite(values).all():
            raise ResearchDataError("SCOPE-Rank feature vector contains NaN/Inf")
        return values

    def fit(
        self,
        pairs: Sequence[tuple[Mapping[str, float], Mapping[str, float]]],
        *,
        training_query_count: int,
        skipped_query_count: int,
        epochs: int,
        learning_rate: float,
        l2: float,
    ) -> "PairwiseLinearRanker":
        if not pairs:
            raise ResearchDataError("SCOPE-Rank has no train-only ranking pairs")
        if epochs < 1 or learning_rate <= 0.0 or l2 < 0.0:
            raise ResearchDataError("invalid SCOPE-Rank training hyperparameters")
        differences = np.stack(
            [self.vector(positive) - self.vector(negative) for positive, negative in pairs]
        )
        self.scales = np.maximum(
            np.sqrt(np.mean(np.square(differences), axis=0)), 1e-8
        )
        matrix = differences / self.scales
        weights = np.zeros(matrix.shape[1], dtype=np.float64)
        loss = math.inf
        for epoch in range(epochs):
            margins = np.clip(matrix @ weights, -35.0, 35.0)
            coefficients = -1.0 / (1.0 + np.exp(margins))
            gradient = matrix.T @ coefficients / len(matrix) + l2 * weights
            step = learning_rate / math.sqrt(1.0 + 0.01 * epoch)
            weights -= step * gradient
            loss = float(np.mean(np.logaddexp(0.0, -margins)))
            loss += 0.5 * l2 * float(weights @ weights)
        if not np.isfinite(weights).all() or not math.isfinite(loss):
            raise ResearchDataError("SCOPE-Rank training produced NaN/Inf")
        self.weights = weights
        self.fitted = True
        self.report = PairwiseRankerReport(
            feature_count=len(self.feature_names),
            training_query_count=training_query_count,
            skipped_query_count=skipped_query_count,
            pair_count=len(pairs),
            epochs=epochs,
            learning_rate=learning_rate,
            l2=l2,
            final_loss=loss,
        )
        return self

    @staticmethod
    def _sigmoid(value: float) -> float:
        if value >= 0.0:
            inverse = math.exp(-min(value, 35.0))
            return 1.0 / (1.0 + inverse)
        exponent = math.exp(max(value, -35.0))
        return exponent / (1.0 + exponent)

    def predict(self, features: Mapping[str, float]) -> float:
        if not self.fitted:
            raise RuntimeError("SCOPE-Rank ranker is not fitted")
        value = float((self.vector(features) / self.scales) @ self.weights)
        return self._sigmoid(value)

    def explain(
        self, features: Mapping[str, float], *, limit: int = 8
    ) -> list[dict[str, float | str]]:
        vector = self.vector(features)
        rows = []
        for name, raw, scale, weight in zip(
            self.feature_names, vector, self.scales, self.weights
        ):
            contribution = float(raw / scale * weight)
            rows.append(
                {
                    "feature": name,
                    "value": float(raw),
                    "weight": float(weight),
                    "contribution": contribution,
                }
            )
        rows.sort(key=lambda row: (-abs(float(row["contribution"])), str(row["feature"])))
        return rows[:limit]

    def to_dict(self) -> dict[str, Any]:
        if not self.fitted or self.report is None:
            raise RuntimeError("cannot serialize an unfitted SCOPE-Rank ranker")
        return {
            "schema_version": self.SCHEMA_VERSION,
            "model_type": "pairwise_linear_logistic",
            "feature_names": list(self.feature_names),
            "scales": self.scales.tolist(),
            "weights": self.weights.tolist(),
            "training_report": asdict(self.report),
        }


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchDataError(f"{label} must be an object")
    return value


def _resolve(config_path: Path, value: Any) -> Path:
    path = Path(str(value or ""))
    if not str(path):
        raise ResearchDataError("SCOPE-Rank configuration contains an empty path")
    return path if path.is_absolute() else (config_path.parent / path).resolve()


def _atomic_json(path: Path, payload: Any) -> None:
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _atomic_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    temporary = path.with_name("." + path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                )
                + "\n"
            )
    temporary.replace(path)


def _artifact(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _safe_terms(text: str, *, limit: int) -> tuple[str, ...]:
    return tuple(dict.fromkeys(tokenize(text)))[:limit]


def build_query_representation(
    query: Query,
    source_row: Mapping[str, Any],
) -> ScopeQueryRepresentation:
    """Create a label-blind representation from an explicit field whitelist."""

    # Forbidden fields are expected to be present in a labeled dataset.  This
    # function deliberately reads only the explicit fields below; the complete
    # forbidden set is recorded in the suite's label-boundary audit.
    profile = QueryProfile.from_text(
        query.text,
        language=(
            str(source_row.get("language") or "").strip().casefold()
            if str(source_row.get("language") or "").strip().casefold()
            in {"zh", "en", "mixed", "other"}
            else None
        ),
    )
    article_type = str(source_row.get("article_type") or "").strip().casefold()
    raw_constraints = source_row.get("user_constraints")
    constraints = raw_constraints if isinstance(raw_constraints, Mapping) else {}
    raw_quartiles = constraints.get("allowed_jcr_quartiles", ())
    allowed_quartiles = tuple(
        sorted(
            {
                str(value).upper()
                for value in raw_quartiles
                if str(value).upper() in {"Q1", "Q2", "Q3", "Q4"}
            }
        )
    ) if isinstance(raw_quartiles, list) else ()
    return ScopeQueryRepresentation(
        query_id=query.query_id,
        text=query.text,
        title_terms=_safe_terms(query.title, limit=32),
        abstract_terms=_safe_terms(query.abstract, limit=96),
        ambiguity=profile.ambiguity,
        cross_disciplinary=profile.cross_disciplinary,
        language=profile.language,
        token_count=profile.token_count,
        article_type=article_type,
        allowed_quartiles=allowed_quartiles,
        representation_source_fields=(
            "title",
            "abstract",
            "article_type",
            "language",
            "user_constraints.allowed_jcr_quartiles",
        ),
    )


def _subject_documents(corpus: Sequence[VenueDocument]) -> list[VenueDocument]:
    output = []
    for document in corpus:
        metadata = document.metadata
        text = " ".join(
            value
            for value in (
                str(metadata.get("subject") or "").strip(),
                str(metadata.get("broad_field") or "").strip(),
            )
            if value
        ) or "unknown subject"
        output.append(
            VenueDocument(
                doc_id=document.doc_id,
                text=text,
                name=document.name,
                snapshot_date=document.snapshot_date,
                metadata=metadata,
            )
        )
    return output


def _channel_traits() -> Mapping[str, ChannelTraits]:
    return {
        "bm25": ChannelTraits(1.0, 0.20, 0.45, 0.20, 0.20),
        "bge_m3": ChannelTraits(0.58, 0.92, 0.90, 0.72, 0.90),
        "specter2": ChannelTraits(0.60, 0.86, 1.0, 0.75, 0.72),
        "scincl": ChannelTraits(0.62, 0.88, 1.0, 0.75, 0.72),
        "property_graph": ChannelTraits(0.72, 0.52, 0.67, 1.0, 0.48),
        "lightrag": ChannelTraits(0.50, 0.85, 0.84, 1.0, 0.76),
        "subject_route": ChannelTraits(0.82, 0.58, 0.72, 0.82, 0.62),
    }


def _normalized_view(ranking: Sequence[ScoredDocument]) -> dict[str, dict[str, float]]:
    if not ranking:
        return {}
    values = [item.score for item in ranking]
    lower, upper = min(values), max(values)
    span = upper - lower
    output: dict[str, dict[str, float]] = {}
    for rank, item in enumerate(ranking, 1):
        score = (item.score - lower) / span if span > 1e-12 else 1.0
        output[item.doc_id] = {
            "score": score,
            "rank": 1.0 / math.log2(rank + 1.0),
            "position": float(rank),
            "raw_score": float(item.score),
        }
    return output


def _passes_hard_constraints(
    representation: ScopeQueryRepresentation,
    document: VenueDocument,
    *,
    cutoff: str,
) -> tuple[bool, dict[str, bool]]:
    temporal = document.snapshot_date <= cutoff
    journal_compatible = not representation.article_type or "journal" in representation.article_type
    quartile = str(document.metadata.get("jcr_quartile") or document.metadata.get("level") or "").upper()
    quartile_compatible = (
        not representation.allowed_quartiles or quartile in representation.allowed_quartiles
    )
    checks = {
        "profile_not_after_cutoff": temporal,
        "journal_article_to_journal": journal_compatible,
        "explicit_quartile_allowlist": quartile_compatible,
    }
    return all(checks.values()), checks


def _feature_sources(spec: VariantSpec) -> tuple[str, ...]:
    removed = set(spec.removed_sources)
    return tuple(name for name in _FEATURE_SOURCE_ORDER if name not in removed)


def _recall_sources(spec: VariantSpec) -> tuple[str, ...]:
    removed = set(spec.removed_sources)
    return tuple(name for name in _RECALL_SOURCE_ORDER if name not in removed)


def _feature_names(spec: VariantSpec) -> tuple[str, ...]:
    names: list[str] = []
    for source in _feature_sources(spec):
        names.extend(
            (
                f"channel:{source}:score",
                f"channel:{source}:rank",
                f"channel:{source}:routed_score",
            )
        )
        if spec.include_missingness:
            names.append(f"channel:{source}:missing")
    names.extend(
        (
            "profile:history_coverage",
            "profile:prototype_coverage",
            "profile:official_scope",
            "profile:level_a",
            "profile:level_b",
            "profile:level_cold",
            "profile:evidence_a",
            "profile:evidence_b",
            "interaction:dense_ambiguity",
            "interaction:graph_cross_domain",
            "interaction:lightrag_cross_domain",
            "interaction:subject_precision",
        )
    )
    if spec.include_missingness:
        names.extend(("profile:missing_history", "profile:missing_official_scope"))
    if spec.include_constraint_features:
        names.extend(
            (
                "constraint:known",
                "constraint:pass",
                "constraint:article_type_match",
                "constraint:quartile_match",
            )
        )
    return tuple(names)


def _route_query(
    representation: ScopeQueryRepresentation,
    runs: Mapping[str, Run],
    documents: Mapping[str, VenueDocument],
    spec: VariantSpec,
    *,
    total_budget: int,
    fixed_quotas: Mapping[str, int],
    cutoff: str,
) -> RouteResult:
    sources = _recall_sources(spec)
    if spec.adaptive_budget:
        observations = [
            ChannelObservation(
                name=source,
                confidence=min(1.0, len(runs[source][representation.query_id]) / 100.0),
                coverage=min(1.0, len(runs[source][representation.query_id]) / 100.0),
                freshness=1.0,
                available=bool(runs[source][representation.query_id]),
                capacity=len(runs[source][representation.query_id]),
            )
            for source in sources
        ]
        profile = QueryProfile.from_text(
            representation.text,
            ambiguity=representation.ambiguity,
            cross_disciplinary=representation.cross_disciplinary,
            language=representation.language,
        )
        allocation = AdaptiveBudgetAllocator(
            _channel_traits(), minimum_per_available=1
        ).allocate(profile, observations, total_budget=total_budget)
        quotas = dict(allocation.quotas)
        channel_scores = dict(allocation.channel_scores)
    else:
        quotas = {
            source: min(
                len(runs[source][representation.query_id]),
                max(0, int(fixed_quotas.get(source, 0))),
            )
            for source in sources
        }
        channel_scores = {source: 1.0 for source in sources}
    candidate_ids: set[str] = set()
    for source in sources:
        candidate_ids.update(
            item.doc_id
            for item in runs[source][representation.query_id][: quotas[source]]
        )
    kept: list[str] = []
    filtered = 0
    for candidate_id in sorted(candidate_ids):
        allowed, _checks = _passes_hard_constraints(
            representation, documents[candidate_id], cutoff=cutoff
        )
        if allowed:
            kept.append(candidate_id)
        else:
            filtered += 1
    return RouteResult(
        quotas=quotas,
        channel_scores=channel_scores,
        pool=tuple(kept),
        hard_filtered_count=filtered,
    )


def _profile_values(document: VenueDocument) -> dict[str, float]:
    metadata = document.metadata
    history = max(0, int(metadata.get("history_paper_count") or 0))
    prototypes = max(0, int(metadata.get("temporal_prototype_count") or 0))
    official = max(0, int(metadata.get("temporal_official_scope_count") or 0))
    level = str(metadata.get("profile_level") or "").upper()
    grade = str(metadata.get("evidence_grade") or "").upper()
    return {
        "profile:history_coverage": min(1.0, math.log1p(history) / math.log(51.0)),
        "profile:prototype_coverage": min(1.0, prototypes / 2.0),
        "profile:official_scope": 1.0 if official else 0.0,
        "profile:level_a": 1.0 if level == "A" else 0.0,
        "profile:level_b": 1.0 if level == "B" else 0.0,
        "profile:level_cold": 1.0 if level not in {"A", "B"} else 0.0,
        "profile:evidence_a": 1.0 if grade == "A" else 0.0,
        "profile:evidence_b": 1.0 if grade == "B" else 0.0,
        "profile:missing_history": 1.0 if history == 0 else 0.0,
        "profile:missing_official_scope": 1.0 if official == 0 else 0.0,
    }


def _candidate_features(
    representation: ScopeQueryRepresentation,
    document: VenueDocument,
    views: Mapping[str, Mapping[str, Mapping[str, float]]],
    route: RouteResult,
    spec: VariantSpec,
    *,
    cutoff: str,
) -> dict[str, float]:
    candidate_id = document.doc_id
    features: dict[str, float] = {}
    feature_sources = _feature_sources(spec)
    for source in feature_sources:
        row = views[source].get(candidate_id)
        score = float(row["score"]) if row else 0.0
        rank = float(row["rank"]) if row else 0.0
        share = route.quotas.get(source, 0) / max(1, sum(route.quotas.values()))
        features[f"channel:{source}:score"] = score
        features[f"channel:{source}:rank"] = rank
        features[f"channel:{source}:routed_score"] = score * share
        if spec.include_missingness:
            features[f"channel:{source}:missing"] = 0.0 if row else 1.0
    profile = _profile_values(document)
    features.update(profile)
    if not spec.include_missingness:
        features.pop("profile:missing_history", None)
        features.pop("profile:missing_official_scope", None)
    dense_scores = [
        float(views[source].get(candidate_id, {}).get("score", 0.0))
        for source in ("bge_m3", "specter2", "scincl", "cross_encoder")
        if source in feature_sources
    ]
    features["interaction:dense_ambiguity"] = (
        max(dense_scores, default=0.0) * representation.ambiguity
    )
    features["interaction:graph_cross_domain"] = (
        float(
            views.get("property_graph", {})
            .get(candidate_id, {})
            .get("score", 0.0)
        )
        * representation.cross_disciplinary
        if "property_graph" in feature_sources
        else 0.0
    )
    features["interaction:lightrag_cross_domain"] = (
        float(
            views.get("lightrag", {}).get(candidate_id, {}).get("score", 0.0)
        )
        * representation.cross_disciplinary
        if "lightrag" in feature_sources
        else 0.0
    )
    features["interaction:subject_precision"] = (
        float(
            views.get("subject_route", {})
            .get(candidate_id, {})
            .get("score", 0.0)
        )
        * (1.0 - representation.ambiguity)
        if "subject_route" in feature_sources
        else 0.0
    )
    allowed, checks = _passes_hard_constraints(
        representation, document, cutoff=cutoff
    )
    if spec.include_constraint_features:
        features["constraint:known"] = 1.0 if (
            representation.article_type or representation.allowed_quartiles
        ) else 0.0
        features["constraint:pass"] = 1.0 if allowed else 0.0
        features["constraint:article_type_match"] = (
            1.0 if checks["journal_article_to_journal"] else 0.0
        )
        features["constraint:quartile_match"] = (
            1.0 if checks["explicit_quartile_allowlist"] else 0.0
        )
    expected = set(_feature_names(spec))
    if set(features) != expected:
        raise ResearchDataError(
            f"SCOPE-Rank feature schema mismatch: missing={sorted(expected-set(features))}, "
            f"extra={sorted(set(features)-expected)}"
        )
    return features


def _query_state(
    query_id: str,
    representations: Mapping[str, ScopeQueryRepresentation],
    runs: Mapping[str, Run],
    documents: Mapping[str, VenueDocument],
    spec: VariantSpec,
    *,
    total_budget: int,
    fixed_quotas: Mapping[str, int],
    cutoff: str,
) -> tuple[RouteResult, dict[str, dict[str, float]], dict[str, dict[str, dict[str, float]]]]:
    representation = representations[query_id]
    views = {
        source: _normalized_view(runs[source][query_id])
        for source in _FEATURE_SOURCE_ORDER
    }
    route = _route_query(
        representation,
        runs,
        documents,
        spec,
        total_budget=total_budget,
        fixed_quotas=fixed_quotas,
        cutoff=cutoff,
    )
    features = {
        candidate_id: _candidate_features(
            representation,
            documents[candidate_id],
            views,
            route,
            spec,
            cutoff=cutoff,
        )
        for candidate_id in route.pool
    }
    return route, features, views


def _train_ranker(
    spec: VariantSpec,
    query_ids: Sequence[str],
    qrels: Mapping[str, Mapping[str, float]],
    representations: Mapping[str, ScopeQueryRepresentation],
    runs: Mapping[str, Run],
    documents: Mapping[str, VenueDocument],
    *,
    total_budget: int,
    fixed_quotas: Mapping[str, int],
    cutoff: str,
    hard_negatives: int,
    epochs: int,
    learning_rate: float,
    l2: float,
) -> PairwiseLinearRanker:
    pairs: list[tuple[Mapping[str, float], Mapping[str, float]]] = []
    used = 0
    skipped = 0
    for query_id in query_ids:
        _route, features, _views = _query_state(
            query_id,
            representations,
            runs,
            documents,
            spec,
            total_budget=total_budget,
            fixed_quotas=fixed_quotas,
            cutoff=cutoff,
        )
        positives = [
            candidate_id
            for candidate_id, gain in qrels[query_id].items()
            if gain > 0.0 and candidate_id in features
        ]
        if not positives:
            skipped += 1
            continue
        positive = min(positives)
        negatives = sorted(
            (candidate_id for candidate_id in features if candidate_id not in positives),
            key=lambda candidate_id: (
                -sum(
                    value
                    for name, value in features[candidate_id].items()
                    if name.endswith(":rank") or name.endswith(":score")
                ),
                candidate_id,
            ),
        )[:hard_negatives]
        if not negatives:
            skipped += 1
            continue
        used += 1
        pairs.extend((features[positive], features[negative]) for negative in negatives)
    return PairwiseLinearRanker(_feature_names(spec)).fit(
        pairs,
        training_query_count=used,
        skipped_query_count=skipped,
        epochs=epochs,
        learning_rate=learning_rate,
        l2=l2,
    )


def _normalize_probabilities(scores: Mapping[str, float]) -> dict[str, float]:
    if not scores:
        return {}
    values = list(scores.values())
    lower, upper = min(values), max(values)
    span = upper - lower
    if span <= 1e-15:
        return {candidate_id: 0.5 for candidate_id in scores}
    return {
        candidate_id: 0.001 + 0.998 * (score - lower) / span
        for candidate_id, score in scores.items()
    }


def _score_features(
    spec: VariantSpec,
    features: Mapping[str, Mapping[str, float]],
    views: Mapping[str, Mapping[str, Mapping[str, float]]],
    ranker: PairwiseLinearRanker | None,
) -> dict[str, float]:
    if spec.fusion == "learned":
        if ranker is None:
            raise RuntimeError("learned SCOPE-Rank variant has no ranker")
        return {candidate_id: ranker.predict(row) for candidate_id, row in features.items()}
    if spec.fusion == "rrf":
        scores = {}
        sources = _feature_sources(spec)
        for candidate_id in features:
            scores[candidate_id] = sum(
                1.0 / (60.0 + views[source][candidate_id]["position"])
                for source in sources
                if candidate_id in views[source]
            )
        return _normalize_probabilities(scores)
    if spec.fusion == "linear":
        scores = {}
        sources = _feature_sources(spec)
        for candidate_id in features:
            scores[candidate_id] = sum(
                0.7 * float(views[source].get(candidate_id, {}).get("score", 0.0))
                + 0.3 * float(views[source].get(candidate_id, {}).get("rank", 0.0))
                for source in sources
            ) / len(sources)
        return _normalize_probabilities(scores)
    raise ResearchDataError(f"unsupported SCOPE-Rank fusion: {spec.fusion!r}")


def _evidence_coverage(document: VenueDocument) -> float:
    profile = _profile_values(document)
    return min(
        1.0,
        0.5 * profile["profile:history_coverage"]
        + 0.3 * profile["profile:prototype_coverage"]
        + 0.2 * profile["profile:official_scope"],
    )


def _channel_agreement(
    candidate_id: str,
    views: Mapping[str, Mapping[str, Mapping[str, float]]],
    spec: VariantSpec,
) -> float:
    sources = _feature_sources(spec)
    return sum(candidate_id in views[source] for source in sources) / len(sources)


def _score_query(
    query_id: str,
    spec: VariantSpec,
    ranker: PairwiseLinearRanker | None,
    representations: Mapping[str, ScopeQueryRepresentation],
    runs: Mapping[str, Run],
    documents: Mapping[str, VenueDocument],
    *,
    total_budget: int,
    fixed_quotas: Mapping[str, int],
    cutoff: str,
    top_k: int,
) -> tuple[list[ScoredDocument], RouteResult, dict[str, dict[str, float]], dict[str, dict[str, dict[str, float]]]]:
    route, features, views = _query_state(
        query_id,
        representations,
        runs,
        documents,
        spec,
        total_budget=total_budget,
        fixed_quotas=fixed_quotas,
        cutoff=cutoff,
    )
    scores = _score_features(spec, features, views, ranker)
    return sort_ranking(scores, top_k), route, features, views


def _calibration_partition(
    train_query_ids: Sequence[str], *, salt: str, denominator: int
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if denominator < 2:
        raise ResearchDataError("calibration denominator must be at least two")
    fit: list[str] = []
    calibration: list[str] = []
    for query_id in train_query_ids:
        digest = hashlib.sha256(f"{salt}:{query_id}".encode()).digest()
        (calibration if int.from_bytes(digest[:8], "big") % denominator == 0 else fit).append(query_id)
    if not fit or not calibration:
        raise ResearchDataError("deterministic train/calibration partition is empty")
    return tuple(fit), tuple(calibration)


def _variant_specs() -> tuple[VariantSpec, ...]:
    return (
        VariantSpec("scope_rank_full"),
        VariantSpec("scope_rank_ablate_bm25", removed_sources=("bm25",)),
        VariantSpec(
            "scope_rank_ablate_dense",
            removed_sources=("bge_m3", "specter2", "scincl", "cross_encoder"),
        ),
        VariantSpec(
            "scope_rank_ablate_graph", removed_sources=("property_graph",)
        ),
        VariantSpec("scope_rank_ablate_lightrag", removed_sources=("lightrag",)),
        VariantSpec(
            "scope_rank_ablate_subject_routing", removed_sources=("subject_route",)
        ),
        VariantSpec("scope_rank_ablate_fixed_budget", adaptive_budget=False),
        VariantSpec("scope_rank_ablate_missingness", include_missingness=False),
        VariantSpec("scope_rank_ablate_calibration", calibrated=False),
        VariantSpec(
            "scope_rank_ablate_constraint_features",
            include_constraint_features=False,
        ),
        VariantSpec("scope_rank_replace_rrf", fusion="rrf"),
        VariantSpec("scope_rank_replace_linear", fusion="linear"),
    )


def _prototype_provenance(document: VenueDocument) -> list[dict[str, Any]]:
    raw = document.metadata.get("prototypes")
    prototypes = raw if isinstance(raw, list) else []
    output = []
    for prototype in prototypes[:3]:
        if not isinstance(prototype, Mapping):
            continue
        source_ids = prototype.get("source_ids")
        output.append(
            {
                "prototype_id": str(prototype.get("prototype_id") or ""),
                "kind": str(prototype.get("kind") or ""),
                "derived_by": str(prototype.get("derived_by") or ""),
                "source_ids": (
                    [str(value) for value in source_ids[:3]]
                    if isinstance(source_ids, list)
                    else []
                ),
                "source_max_date": str(prototype.get("source_max_date") or ""),
            }
        )
    return output


def _query_hash(query_ids: Sequence[str]) -> str:
    return ordered_ids_sha256(tuple(query_ids))


def _load_sources(
    config_path: Path,
    channel_configs: Sequence[Mapping[str, Any]],
    *,
    query_ids: Sequence[str],
    candidate_ids: Sequence[str],
    binding: Mapping[str, Any],
    top_k: int,
) -> tuple[dict[str, Run], dict[str, Any]]:
    runs: dict[str, Run] = {}
    records: dict[str, Any] = {}
    for raw in channel_configs:
        source = str(raw.get("name") or "")
        if source not in _FEATURE_SOURCE_ORDER or source == "subject_route":
            raise ResearchDataError(f"unknown or derived SCOPE-Rank source: {source!r}")
        if source in runs:
            raise ResearchDataError(f"duplicate SCOPE-Rank source: {source!r}")
        path = _resolve(config_path, raw.get("path"))
        manifest_path = _resolve(config_path, raw.get("manifest_path"))
        expected_run_sha256 = str(raw.get("run_sha256") or "").strip()
        if len(expected_run_sha256) != 64 or sha256_file(path) != expected_run_sha256:
            raise ResearchDataError(
                f"SCOPE-Rank source run SHA-256 mismatch: {source!r}"
            )
        identity = {
            key: str(raw[key])
            for key in (
                "model_revision",
                "provider_fingerprint",
                "implementation_revision",
            )
            if str(raw.get(key) or "")
        }
        runs[source] = load_score_run(
            path,
            expected_query_ids=query_ids,
            candidate_ids=candidate_ids,
            expected_binding=binding,
            expected_manifest_sha256=str(raw.get("manifest_sha256") or ""),
            expected_configuration_sha256=str(
                raw.get("generation_config_sha256") or ""
            ),
            expected_method_identity=identity,
            manifest_path=manifest_path,
            top_k=top_k,
        )
        records[source] = {
            "run": _artifact(path),
            "manifest": _artifact(manifest_path),
            "expected_run_sha256": expected_run_sha256,
            "generation_config_sha256": str(raw["generation_config_sha256"]),
            "method_identity": identity,
        }
    required = set(_FEATURE_SOURCE_ORDER) - {"subject_route"}
    if set(runs) != required:
        raise ResearchDataError(
            f"SCOPE-Rank source set mismatch: missing={sorted(required-set(runs))}, "
            f"extra={sorted(set(runs)-required)}"
        )
    return runs, records


def build_scope_rank_suite(
    config_path: Path,
    *,
    generation_command: Sequence[str],
) -> dict[str, Any]:
    """Build the formal full method and all named ablations."""

    config_path = config_path.resolve()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResearchDataError(f"cannot read SCOPE-Rank config: {config_path}") from exc
    config = _mapping(config, "SCOPE-Rank config")
    if config.get("schema_version") != 1:
        raise ResearchDataError("unsupported SCOPE-Rank configuration schema")
    if config.get("offline_only") is not True:
        raise ResearchDataError("SCOPE-Rank suite requires offline_only=true")
    if config.get("fail_on_critical_leakage") is not True:
        raise ResearchDataError(
            "SCOPE-Rank suite requires fail_on_critical_leakage=true"
        )
    if config.get("evaluation_status") != "exposed_development_not_sealed":
        raise ResearchDataError(
            "SCOPE-Rank suite must be marked exposed_development_not_sealed"
        )
    output_dir = _resolve(config_path, config.get("output_dir"))
    if output_dir.exists():
        raise ResearchDataError(
            f"SCOPE-Rank output already exists and will not be overwritten: {output_dir}"
        )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir()
    try:
        dataset_config = _mapping(config.get("dataset"), "dataset")
        corpus_config = _mapping(config.get("corpus"), "corpus")
        split_config = _mapping(config.get("temporal_split"), "temporal_split")
        method_config = _mapping(config.get("method"), "method")
        dataset_path = _resolve(config_path, dataset_config.get("path"))
        profiles_path = _resolve(config_path, corpus_config.get("path"))
        reference_path = _resolve(config_path, config.get("reference_manifest"))
        bundle = load_recent_journal_dataset(
            dataset_path,
            query_fields=tuple(dataset_config.get("query_fields") or ("title", "abstract")),
        )
        corpus = load_jsonl_corpus(
            profiles_path,
            id_field=str(corpus_config.get("id_field") or "venue_id"),
            text_fields=tuple(corpus_config.get("text_fields") or ("name",)),
            snapshot_field=str(corpus_config.get("snapshot_field") or "snapshot_date"),
        )
        split = temporal_split(
            bundle.queries,
            start=str(split_config.get("start") or "") or None,
            train_end=str(split_config.get("train_end") or ""),
            validation_end=str(split_config.get("validation_end") or ""),
            test_end=str(split_config.get("test_end") or ""),
        )
        query_ids = tuple(query.query_id for query in bundle.queries)
        split_query_ids = (*split.train, *split.validation, *split.test)
        if set(query_ids) != set(split_query_ids) or len(query_ids) != len(
            split_query_ids
        ):
            raise ResearchDataError(
                "SCOPE-Rank temporal split must cover every query exactly once"
            )
        candidate_ids = tuple(document.doc_id for document in corpus)
        binding = build_run_binding(
            dataset_path=dataset_path,
            profiles_path=profiles_path,
            query_ids=query_ids,
            candidate_ids=candidate_ids,
            configuration=config,
            configuration_path=config_path,
        )
        reference_sha256 = str(config.get("reference_manifest_sha256") or "")
        if (
            len(reference_sha256) != 64
            or sha256_file(reference_path) != reference_sha256
        ):
            raise ResearchDataError("SCOPE-Rank reference manifest SHA-256 mismatch")
        reference_binding = validate_reference_binding(reference_path, binding)
        channel_values = config.get("channels")
        if not isinstance(channel_values, list):
            raise ResearchDataError("SCOPE-Rank channels must be an array")
        source_runs, source_records = _load_sources(
            config_path,
            [_mapping(value, "channels[]") for value in channel_values],
            query_ids=query_ids,
            candidate_ids=candidate_ids,
            binding=binding,
            top_k=int(method_config.get("source_depth", 100)),
        )
        queries_by_id = {query.query_id: query for query in bundle.queries}
        documents = {document.doc_id: document for document in corpus}
        cutoff = str(method_config.get("profile_cutoff") or split_config.get("train_end"))
        if cutoff != str(split_config.get("train_end") or ""):
            raise ResearchDataError(
                "SCOPE-Rank profile_cutoff must equal the temporal train boundary"
            )
        if any(document.snapshot_date > cutoff for document in corpus):
            raise ResearchDataError("candidate profile is newer than SCOPE-Rank cutoff")
        leakage = audit_leakage(
            bundle,
            corpus,
            split,
            corpus_views=("document", "prototypes"),
        )
        leakage_path = output_dir / "leakage_audit.json"
        _atomic_json(leakage_path, leakage)
        if not leakage["passed"]:
            raise ResearchDataError(
                f"critical SCOPE-Rank leakage found; inspect {leakage_path}"
            )
        representations = {
            query_id: build_query_representation(
                queries_by_id[query_id], bundle.source_rows[query_id]
            )
            for query_id in query_ids
        }
        subject_started = perf_counter()
        subject_run = BM25Baseline(name="subject_route").fit(
            _subject_documents(corpus)
        ).run(bundle.queries, top_k=int(method_config.get("source_depth", 100)))
        source_runs["subject_route"] = subject_run
        subject_total_ms = (perf_counter() - subject_started) * 1000.0

        runtime = runtime_provenance()
        implementation_revision = "scope-rank-suite-v1@" + sha256_file(Path(__file__))
        subject_path = output_dir / "subject_route.jsonl"
        subject_manifest = write_run(
            subject_path,
            subject_run,
            binding=binding,
            query_ids=query_ids,
            candidate_ids=candidate_ids,
            top_k=int(method_config.get("source_depth", 100)),
            method={
                "name": "scope_rank_subject_route",
                "kind": "subject_route",
                "implementation": "research.scope_rank_runs._subject_documents",
                "implementation_revision": implementation_revision,
                "configuration_sha256": binding["configuration"]["canonical_sha256"],
            },
            command=generation_command,
            working_directory=Path.cwd(),
            runtime=runtime,
            additional_manifest_fields={
                "execution": {
                    "total_ms": subject_total_ms,
                    "external_api_calls": 0,
                    "estimated_external_cost_usd": 0.0,
                    "offline_only": True,
                    "search_free": True,
                    "failed_query_count": 0,
                },
                "label_blind_fields": ["metadata.subject", "metadata.broad_field"],
            },
        )
        source_records["subject_route"] = {
            "run": subject_manifest["output"],
            "manifest": _artifact(subject_path.with_suffix(".jsonl.manifest.json")),
            "derived_without_labels": True,
        }

        fit_ids, calibration_ids = _calibration_partition(
            split.train,
            salt=str(method_config.get("calibration_salt") or "scope-rank-v1"),
            denominator=int(method_config.get("calibration_denominator", 5)),
        )
        total_budget = int(method_config.get("total_recall_budget", 350))
        fixed_raw = _mapping(method_config.get("fixed_quotas"), "method.fixed_quotas")
        fixed_quotas = {str(name): int(value) for name, value in fixed_raw.items()}
        if set(fixed_quotas) != set(_RECALL_SOURCE_ORDER):
            raise ResearchDataError("fixed quotas must cover every recall source exactly")
        if total_budget < 1 or any(value < 0 for value in fixed_quotas.values()):
            raise ResearchDataError("SCOPE-Rank recall budgets must be non-negative")
        if sum(fixed_quotas.values()) != total_budget:
            raise ResearchDataError("fixed quotas must sum to total_recall_budget")
        top_k = int(method_config.get("top_k", 100))
        hard_negatives = int(method_config.get("hard_negatives", 20))
        epochs = int(method_config.get("epochs", 120))
        learning_rate = float(method_config.get("learning_rate", 0.12))
        l2 = float(method_config.get("l2", 0.002))
        if top_k < 1 or hard_negatives < 1:
            raise ResearchDataError("SCOPE-Rank top_k/hard_negatives must be positive")
        calibrator_config = _mapping(method_config.get("calibrator"), "method.calibrator")

        decisions: list[dict[str, Any]] = []
        explanations: list[dict[str, Any]] = []
        variants: dict[str, Any] = {}
        for spec in _variant_specs():
            variant_started = perf_counter()
            ranker = (
                _train_ranker(
                    spec,
                    fit_ids,
                    bundle.qrels,
                    representations,
                    source_runs,
                    documents,
                    total_budget=total_budget,
                    fixed_quotas=fixed_quotas,
                    cutoff=cutoff,
                    hard_negatives=hard_negatives,
                    epochs=epochs,
                    learning_rate=learning_rate,
                    l2=l2,
                )
                if spec.fusion == "learned"
                else None
            )
            model_record: dict[str, Any] | None = None
            if ranker is not None:
                model_path = output_dir / f"{spec.name}.model.json"
                _atomic_json(model_path, ranker.to_dict())
                model_record = _artifact(model_path)

            calibration_scores: list[float] = []
            calibration_labels: list[float] = []
            for query_id in calibration_ids:
                ranking, _route, _features, _views = _score_query(
                    query_id,
                    spec,
                    ranker,
                    representations,
                    source_runs,
                    documents,
                    total_budget=total_budget,
                    fixed_quotas=fixed_quotas,
                    cutoff=cutoff,
                    top_k=top_k,
                )
                if not ranking:
                    continue
                calibration_scores.append(float(ranking[0].score))
                calibration_labels.append(
                    1.0 if ranking[0].doc_id in bundle.qrels[query_id] else 0.0
                )
            calibrator = SelectiveCalibrator(
                target_precision=float(calibrator_config.get("target_precision", 0.15)),
                min_confidence=float(calibrator_config.get("min_confidence", 0.0)),
                min_evidence_coverage=float(
                    calibrator_config.get("min_evidence_coverage", 0.15)
                ),
                min_channel_agreement=float(
                    calibrator_config.get("min_channel_agreement", 0.10)
                ),
            )
            if spec.calibrated:
                calibrator.fit(calibration_scores, calibration_labels)

            run: Run = {}
            pool_sizes: list[int] = []
            filtered_total = 0
            abstained = 0
            reasons: dict[str, int] = {}
            for query_id in query_ids:
                ranking, route, features, views = _score_query(
                    query_id,
                    spec,
                    ranker,
                    representations,
                    source_runs,
                    documents,
                    total_budget=total_budget,
                    fixed_quotas=fixed_quotas,
                    cutoff=cutoff,
                    top_k=top_k,
                )
                if not ranking:
                    raise ResearchDataError(
                        f"SCOPE-Rank variant {spec.name} has an empty query: {query_id}"
                    )
                run[query_id] = ranking
                pool_sizes.append(len(route.pool))
                filtered_total += route.hard_filtered_count
                top = ranking[0]
                coverage = _evidence_coverage(documents[top.doc_id])
                agreement = _channel_agreement(top.doc_id, views, spec)
                if spec.calibrated:
                    prediction = calibrator.decide(
                        top.score,
                        evidence_coverage=coverage,
                        channel_agreement=agreement,
                    )
                    abstain = prediction.abstain
                    reason = prediction.reason
                    calibrated_score = prediction.score
                    confidence = prediction.confidence
                else:
                    abstain = False
                    reason = None
                    calibrated_score = top.score
                    confidence = top.score
                if abstain:
                    abstained += 1
                    assert reason is not None
                    reasons[reason] = reasons.get(reason, 0) + 1
                decisions.append(
                    {
                        "variant": spec.name,
                        "query_id": query_id,
                        "top_candidate_id": top.doc_id,
                        "raw_score": top.score,
                        "calibrated_score": calibrated_score,
                        "confidence": confidence,
                        "evidence_coverage": coverage,
                        "channel_agreement": agreement,
                        "abstain": abstain,
                        "reason": reason,
                        "pool_size": len(route.pool),
                        "hard_filtered_count": route.hard_filtered_count,
                        "route_quotas": dict(route.quotas),
                    }
                )
                if spec.name == "scope_rank_full":
                    for rank, item in enumerate(ranking[:5], 1):
                        allowed, checks = _passes_hard_constraints(
                            representations[query_id],
                            documents[item.doc_id],
                            cutoff=cutoff,
                        )
                        explanations.append(
                            {
                                "query_id": query_id,
                                "candidate_id": item.doc_id,
                                "rank": rank,
                                "score": item.score,
                                "channel_evidence": {
                                    source: {
                                        "rank": int(views[source][item.doc_id]["position"]),
                                        "raw_score": views[source][item.doc_id]["raw_score"],
                                    }
                                    for source in _FEATURE_SOURCE_ORDER
                                    if item.doc_id in views[source]
                                },
                                "feature_contributions": (
                                    ranker.explain(features[item.doc_id])
                                    if ranker is not None
                                    else []
                                ),
                                "hard_constraints_passed": allowed,
                                "constraint_checks": checks,
                                "profile_provenance": {
                                    "snapshot_date": documents[item.doc_id].snapshot_date,
                                    "profile_level": documents[item.doc_id].metadata.get(
                                        "profile_level"
                                    ),
                                    "evidence_grade": documents[item.doc_id].metadata.get(
                                        "evidence_grade"
                                    ),
                                    "history_paper_count": documents[
                                        item.doc_id
                                    ].metadata.get("history_paper_count", 0),
                                    "prototypes": _prototype_provenance(
                                        documents[item.doc_id]
                                    ),
                                },
                            }
                        )

            run_path = output_dir / f"{spec.name}.jsonl"
            method_identity = {
                "name": spec.name,
                "kind": "scope_rank" if spec.name == "scope_rank_full" else "scope_rank_ablation",
                "implementation": "research.scope_rank_runs.build_scope_rank_suite",
                "implementation_revision": implementation_revision,
                "configuration_sha256": binding["configuration"]["canonical_sha256"],
                "variant_sha256": canonical_json_sha256(asdict(spec)),
            }
            run_manifest = write_run(
                run_path,
                run,
                binding=binding,
                query_ids=query_ids,
                candidate_ids=candidate_ids,
                top_k=top_k,
                method=method_identity,
                command=generation_command,
                working_directory=Path.cwd(),
                runtime=runtime,
                additional_manifest_fields={
                    "variant": asdict(spec),
                    "training": {
                        "rank_fit_split": "train_only",
                        "rank_fit_query_count": len(fit_ids),
                        "rank_fit_query_ids_sha256": _query_hash(fit_ids),
                        "calibration_split": "disjoint_train_only",
                        "calibration_query_count": len(calibration_ids),
                        "calibration_query_ids_sha256": _query_hash(calibration_ids),
                        "ranker": asdict(ranker.report) if ranker and ranker.report else None,
                        "validation_labels_accessed": False,
                        "test_labels_accessed": False,
                    },
                    "calibration": {
                        "enabled": spec.calibrated,
                        "example_count": len(calibration_scores),
                        "positive_count": int(sum(calibration_labels)),
                        "temperature": calibrator.temperature,
                        "threshold": calibrator.threshold,
                        "can_accept": calibrator.can_accept,
                        "target_precision": calibrator.target_precision,
                    },
                    "constraints": {
                        "hard_filter_always_enabled": True,
                        "filtered_candidate_occurrences": filtered_total,
                        "output_violation_count": 0,
                        "gold_fields_allowed_as_constraints": False,
                    },
                    "selective_output": {
                        "query_count": len(query_ids),
                        "abstained_query_count": abstained,
                        "accepted_query_count": len(query_ids) - abstained,
                        "reason_counts": reasons,
                    },
                    "execution": {
                        "total_ms": (perf_counter() - variant_started) * 1000.0,
                        "mean_pool_size": sum(pool_sizes) / len(pool_sizes),
                        "min_pool_size": min(pool_sizes),
                        "max_pool_size": max(pool_sizes),
                        "external_api_calls": 0,
                        "estimated_external_cost_usd": 0.0,
                        "failed_query_count": 0,
                        "offline_only": True,
                        "search_free": True,
                    },
                    **({"model": model_record} if model_record else {}),
                },
            )
            variants[spec.name] = {
                "run": run_manifest["output"],
                "manifest": _artifact(run_path.with_suffix(".jsonl.manifest.json")),
                "method": method_identity,
                "model": model_record,
                "training": run_manifest["training"],
                "calibration": run_manifest["calibration"],
                "constraints": run_manifest["constraints"],
                "selective_output": run_manifest["selective_output"],
                "execution": run_manifest["execution"],
            }

        decisions_path = output_dir / "decisions.jsonl"
        explanations_path = output_dir / "scope_rank_full.explanations.jsonl"
        _atomic_jsonl(decisions_path, decisions)
        _atomic_jsonl(explanations_path, explanations)
        suite_manifest = {
            "schema_version": 1,
            "artifact_type": "scope_rank_suite",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "complete_exposed_development_not_sealed",
            "binding": binding,
            "reference_binding_manifest": reference_binding,
            "leakage_audit": _artifact(leakage_path),
            "runtime": runtime,
            "implementation_revision": implementation_revision,
            "generation": {
                "command": [str(value) for value in generation_command],
                "working_directory": str(Path.cwd().resolve()),
            },
            "label_boundary": {
                "forbidden_query_fields": sorted(_FORBIDDEN_QUERY_FIELDS),
                "representation_source_fields": list(
                    next(iter(representations.values())).representation_source_fields
                ),
                "rank_fit_query_count": len(fit_ids),
                "rank_fit_query_ids_sha256": _query_hash(fit_ids),
                "calibration_query_count": len(calibration_ids),
                "calibration_query_ids_sha256": _query_hash(calibration_ids),
                "partitions_disjoint": not bool(set(fit_ids) & set(calibration_ids)),
                "union_equals_temporal_train": set((*fit_ids, *calibration_ids)) == set(split.train),
                "validation_labels_accessed": False,
                "test_labels_accessed": False,
            },
            "splits": {
                "train": len(split.train),
                "validation": len(split.validation),
                "test": len(split.test),
                "train_query_ids_sha256": _query_hash(split.train),
                "validation_query_ids_sha256": _query_hash(split.validation),
                "test_query_ids_sha256": _query_hash(split.test),
            },
            "sources": source_records,
            "variants": variants,
            "outputs": {
                "decisions": _artifact(decisions_path),
                "explanations": _artifact(explanations_path),
            },
            "coverage": {
                "query_count": len(query_ids),
                "candidate_count": len(candidate_ids),
                "variant_count": len(variants),
                "ablation_count": len(variants) - 1,
                "decision_count": len(decisions),
                "explanation_count": len(explanations),
                "all_variants_complete": all(
                    value["run"]["bytes"] > 0 for value in variants.values()
                ),
            },
            "execution": {
                "external_api_calls": 0,
                "estimated_external_cost_usd": 0.0,
                "offline_only": True,
                "search_free": True,
            },
        }
        manifest_path = output_dir / "manifest.json"
        _atomic_json(manifest_path, suite_manifest)
        return {**suite_manifest, "manifest": _artifact(manifest_path)}
    except Exception:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        failed_path = output_dir.with_name(
            f"{output_dir.name}.failed-{timestamp}-{uuid.uuid4().hex[:8]}"
        )
        os.replace(output_dir, failed_path)
        raise


__all__ = [
    "PairwiseLinearRanker",
    "ScopeQueryRepresentation",
    "VariantSpec",
    "build_query_representation",
    "build_scope_rank_suite",
]
