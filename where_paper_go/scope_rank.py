"""Research primitives for the SCOPE-Rank retrieval and ranking method.

The production recommender historically reserved a fixed number of candidates
for each recall channel.  This module contains dependency-free, deterministic
building blocks for replacing that heuristic with a query-aware route and a
learned fusion model.  It deliberately knows nothing about ``VenueCandidate``
so that the method can be trained, ablated, and tested independently.

SCOPE-Rank is split into three small stages:

* :class:`AdaptiveBudgetAllocator` routes a recall budget from a query profile
  and live channel health observations;
* :class:`LinearSoftmaxFusion` learns a lightweight pointwise ranker over the
  resulting candidate features; and
* :class:`SelectiveCalibrator` calibrates result confidence and exposes an
  explicit reject option when evidence is weak.

Only the Python standard library is used.  All ordering and optimisation steps
have deterministic tie breaking, which is important for reproducible research.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_LATIN_RE = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)*")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def _clamp01(value: float, *, field_name: str) -> float:
    value = float(value)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{field_name} must be a finite value in [0, 1]")
    return value


def _detect_language(text: str) -> str:
    cjk_count = sum(len(value) for value in _CJK_RE.findall(text))
    latin_count = sum(len(value) for value in _LATIN_RE.findall(text))
    if cjk_count and latin_count:
        smaller = min(cjk_count, latin_count)
        larger = max(cjk_count, latin_count)
        return "mixed" if smaller / larger >= 0.08 else ("zh" if cjk_count > latin_count else "en")
    if cjk_count:
        return "zh"
    if latin_count:
        return "en"
    return "other"


def _count_query_tokens(text: str) -> int:
    """Approximate tokens without binding the research layer to a tokenizer."""

    latin_or_numbers = len(_LATIN_RE.findall(text)) + len(_NUMBER_RE.findall(text))
    # Two Han characters are a more useful approximation than treating a whole
    # Chinese sentence as one token or every character as an independent word.
    cjk_tokens = sum(max(1, math.ceil(len(value) / 2)) for value in _CJK_RE.findall(text))
    return latin_or_numbers + cjk_tokens


_VAGUE_MARKERS = re.compile(
    r"(?:\u76f8\u5173|\u65b9\u9762|\u67d0\u79cd|\u4e00\u4e9b|\u901a\u7528|\u7b49\u7b49|\u4e4b\u7c7b|\u4e0d\u786e\u5b9a|\u6a21\u7cca|\u53ef\u80fd|"
    r"\b(?:related|relevant|some|general|generic|broad|possibly|maybe|etcetera|etc)\b)",
    re.I,
)
_CROSS_MARKERS = re.compile(
    r"(?:\u8de8\s*(?:\u5b66\u79d1|\u9886\u57df|\u6a21\u6001)|\u8de8[^\uff0c,\uff1b;\s]{1,12}(?:\u4e0e|\u548c|\u53ca|\u3001)|\u4ea4\u53c9\s*(?:\u5b66\u79d1|\u9886\u57df|\u7814\u7a76)|\u878d\u5408\s*(?:\u591a\u5b66\u79d1|\u591a\u9886\u57df)|"
    r"\b(?:cross[- ](?:disciplinary|domain|modal)|interdisciplin\w+|multidisciplin\w+)\b)",
    re.I,
)

# This vocabulary is intentionally coarse.  It is only a dependency-free
# fallback; an LLM intent parser may supply a more accurate score explicitly.
_DOMAIN_MARKERS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        r"\u533b\u5b66|\u4e34\u5e8a|\u5f71\u50cf|\u75be\u75c5|\b(?:medical|clinical|health|radiology)\b",
        r"\u751f\u7269|\u57fa\u56e0|\u86cb\u767d\u8d28|\b(?:biology|genomic|protein|bioinformatics)\b",
        r"\u6570\u5b66|\u5b9a\u7406|\u51e0\u4f55|\u4ee3\u6570|\b(?:mathematics?|theorem|geometry|algebra)\b",
        r"\u8ba1\u7b97\u673a|\u7b97\u6cd5|\u8f6f\u4ef6|\u4eba\u5de5\u667a\u80fd|\b(?:computer|algorithm|software|machine learning|artificial intelligence)\b",
        r"\u7269\u7406|\u91cf\u5b50|\u529b\u5b66|\b(?:physics|quantum|mechanics)\b",
        r"\u5316\u5b66|\u5206\u5b50|\u6750\u6599|\b(?:chemistry|molecule|materials? science)\b",
        r"\u54f2\u5b66|\u4f26\u7406|\u903b\u8f91\u5b66|\b(?:philosophy|ethics|epistemology)\b",
        r"\u7ecf\u6d4e|\u91d1\u878d|\u7ba1\u7406|\b(?:economics?|finance|management)\b",
        r"\u793e\u4f1a|\u6559\u80b2|\u5fc3\u7406|\u8bed\u8a00\u5b66|\b(?:social|education|psychology|linguistics)\b",
    )
)


def _infer_ambiguity(text: str, token_count: int) -> float:
    vague_count = len(_VAGUE_MARKERS.findall(text))
    if token_count <= 3:
        length_adjustment = 0.28
    elif token_count <= 7:
        length_adjustment = 0.14
    elif token_count >= 28:
        length_adjustment = -0.18
    elif token_count >= 16:
        length_adjustment = -0.08
    else:
        length_adjustment = 0.0
    specificity = min(0.16, 0.025 * len(re.findall(r"[A-Z]{2,}|\d|[/+]", text)))
    return min(0.95, max(0.05, 0.43 + length_adjustment + 0.13 * vague_count - specificity))


def _infer_cross_disciplinary(text: str) -> float:
    explicit = 0.65 if _CROSS_MARKERS.search(text) else 0.0
    matched_domains = sum(bool(pattern.search(text)) for pattern in _DOMAIN_MARKERS)
    domain_signal = min(1.0, max(0, matched_domains - 1) * 0.34)
    return min(1.0, explicit + domain_signal)


@dataclass(frozen=True)
class QueryProfile:
    """Compact routing profile for a research-description query.

    Values may come from an LLM intent parser.  :meth:`from_text` provides a
    deterministic fallback for offline experiments and API failure handling.
    """

    text: str
    ambiguity: float
    cross_disciplinary: float
    language: str
    token_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("query text must not be empty")
        object.__setattr__(self, "ambiguity", _clamp01(self.ambiguity, field_name="ambiguity"))
        object.__setattr__(
            self,
            "cross_disciplinary",
            _clamp01(self.cross_disciplinary, field_name="cross_disciplinary"),
        )
        if self.language not in {"zh", "en", "mixed", "other"}:
            raise ValueError("language must be one of: zh, en, mixed, other")
        if (
            not isinstance(self.token_count, int)
            or isinstance(self.token_count, bool)
            or self.token_count < 1
        ):
            raise ValueError("token_count must be a positive integer")

    @classmethod
    def from_text(
        cls,
        text: str,
        *,
        ambiguity: float | None = None,
        cross_disciplinary: float | None = None,
        language: str | None = None,
    ) -> "QueryProfile":
        """Build a profile, accepting model-supplied scores when available."""

        if not isinstance(text, str) or not text.strip():
            raise ValueError("query text must not be empty")
        normalized = " ".join(text.split())
        token_count = max(1, _count_query_tokens(normalized))
        resolved_language = language or _detect_language(normalized)
        return cls(
            text=normalized,
            ambiguity=(
                _infer_ambiguity(normalized, token_count)
                if ambiguity is None
                else ambiguity
            ),
            cross_disciplinary=(
                _infer_cross_disciplinary(normalized)
                if cross_disciplinary is None
                else cross_disciplinary
            ),
            language=resolved_language,
            token_count=token_count,
        )

    @property
    def length_signal(self) -> float:
        """Saturating long-query signal in ``[0, 1]``."""

        return min(1.0, math.log1p(self.token_count) / math.log1p(40))

    @property
    def language_complexity(self) -> float:
        """Routing pressure for multilingual or non-English retrieval."""

        return {"en": 0.1, "zh": 0.65, "mixed": 1.0, "other": 0.8}[self.language]


@dataclass(frozen=True)
class ChannelObservation:
    """Live quality and availability of one independent recall channel."""

    name: str
    confidence: float
    coverage: float
    freshness: float
    available: bool = True
    capacity: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("channel name must not be empty")
        object.__setattr__(self, "confidence", _clamp01(self.confidence, field_name="confidence"))
        object.__setattr__(self, "coverage", _clamp01(self.coverage, field_name="coverage"))
        object.__setattr__(self, "freshness", _clamp01(self.freshness, field_name="freshness"))
        if self.capacity is not None and (
            not isinstance(self.capacity, int)
            or isinstance(self.capacity, bool)
            or self.capacity < 0
        ):
            raise ValueError("capacity must be a non-negative integer or None")


@dataclass(frozen=True)
class ChannelTraits:
    """How useful a channel is for different query conditions."""

    precise_query: float = 0.5
    ambiguous_query: float = 0.5
    long_query: float = 0.5
    cross_domain: float = 0.5
    multilingual: float = 0.5

    def __post_init__(self) -> None:
        for field_name in (
            "precise_query",
            "ambiguous_query",
            "long_query",
            "cross_domain",
            "multilingual",
        ):
            object.__setattr__(
                self,
                field_name,
                _clamp01(getattr(self, field_name), field_name=field_name),
            )


DEFAULT_CHANNEL_TRAITS: Mapping[str, ChannelTraits] = {
    "combined": ChannelTraits(1.0, 0.35, 0.45, 0.35, 0.45),
    "semantic_vector": ChannelTraits(0.55, 1.0, 0.90, 0.72, 0.88),
    "lightrag_mix": ChannelTraits(0.40, 0.82, 0.82, 1.0, 0.76),
    "property_graph": ChannelTraits(0.72, 0.52, 0.67, 1.0, 0.48),
    "llm_area_route": ChannelTraits(0.30, 1.0, 0.92, 0.92, 1.0),
    "search_hint": ChannelTraits(0.42, 0.78, 0.65, 0.80, 0.82),
}
_NEUTRAL_CHANNEL_TRAITS = ChannelTraits()


@dataclass(frozen=True)
class BudgetAllocation:
    """Auditable output of adaptive recall routing."""

    quotas: Mapping[str, int]
    channel_scores: Mapping[str, float]
    total_budget: int
    allocated: int
    unallocated: int


class AdaptiveBudgetAllocator:
    """Allocate recall seats from query demand and observed channel health.

    The algorithm first reserves a configurable minimum, then distributes the
    remaining seats proportionally with Hamilton's largest-remainder method.
    Capacities are respected and all ties are resolved by channel name.
    """

    def __init__(
        self,
        traits: Mapping[str, ChannelTraits] | None = None,
        *,
        minimum_per_available: int = 1,
    ) -> None:
        if (
            not isinstance(minimum_per_available, int)
            or isinstance(minimum_per_available, bool)
            or minimum_per_available < 0
        ):
            raise ValueError("minimum_per_available must be a non-negative integer")
        self.traits = dict(DEFAULT_CHANNEL_TRAITS if traits is None else traits)
        self.minimum_per_available = minimum_per_available

    def channel_score(self, profile: QueryProfile, observation: ChannelObservation) -> float:
        """Return the positive routing utility for a channel."""

        if not observation.available or observation.capacity == 0:
            return 0.0
        traits = self.traits.get(observation.name, _NEUTRAL_CHANNEL_TRAITS)
        quality = (
            0.46 * observation.confidence
            + 0.34 * observation.coverage
            + 0.20 * observation.freshness
        )
        exactness = 1.0 - profile.ambiguity
        demand = 1.0 + (
            0.60 * exactness * traits.precise_query
            + 0.90 * profile.ambiguity * traits.ambiguous_query
            + 0.55 * profile.length_signal * traits.long_query
            + 0.90 * profile.cross_disciplinary * traits.cross_domain
            + 0.50 * profile.language_complexity * traits.multilingual
        )
        # The exponent makes repeatedly weak/stale channels lose seats quickly,
        # without fully removing them when minimum reservation is enabled.
        return max(1e-12, quality**1.35 * demand)

    def allocate(
        self,
        profile: QueryProfile,
        observations: Sequence[ChannelObservation],
        *,
        total_budget: int,
    ) -> BudgetAllocation:
        if (
            not isinstance(total_budget, int)
            or isinstance(total_budget, bool)
            or total_budget < 0
        ):
            raise ValueError("total_budget must be a non-negative integer")
        names = [observation.name for observation in observations]
        if len(names) != len(set(names)):
            raise ValueError("channel observations must have unique names")

        quotas = {name: 0 for name in names}
        scores = {
            observation.name: self.channel_score(profile, observation)
            for observation in observations
        }
        by_name = {observation.name: observation for observation in observations}
        active = [
            observation.name
            for observation in observations
            if observation.available and observation.capacity != 0
        ]
        active.sort(key=lambda name: (-scores[name], name))
        remaining = total_budget

        # Minimums are allocated in rounds so that small pools still preserve
        # the most useful independent evidence channels.
        for _round in range(self.minimum_per_available):
            for name in active:
                if remaining == 0:
                    break
                capacity = by_name[name].capacity
                if capacity is not None and quotas[name] >= capacity:
                    continue
                quotas[name] += 1
                remaining -= 1
            if remaining == 0:
                break

        while remaining:
            eligible = [
                name
                for name in active
                if by_name[name].capacity is None
                or quotas[name] < int(by_name[name].capacity)
            ]
            if not eligible:
                break
            score_sum = sum(scores[name] for name in eligible)
            raw = {
                name: remaining * scores[name] / score_sum
                for name in eligible
            }
            added = 0
            for name in eligible:
                capacity = by_name[name].capacity
                room = remaining if capacity is None else capacity - quotas[name]
                amount = min(room, int(math.floor(raw[name])))
                if amount > 0:
                    quotas[name] += amount
                    added += amount
            remaining -= added
            if remaining == 0:
                break

            # Assign leftover fractional seats one at a time.  Re-entering the
            # loop recalculates proportions when a capacity becomes saturated.
            fractional = sorted(
                eligible,
                key=lambda name: (-(raw[name] - math.floor(raw[name])), -scores[name], name),
            )
            fractional_added = 0
            for name in fractional:
                if remaining == 0:
                    break
                capacity = by_name[name].capacity
                if capacity is not None and quotas[name] >= capacity:
                    continue
                quotas[name] += 1
                remaining -= 1
                fractional_added += 1
            if added == 0 and fractional_added == 0:
                break

        allocated = sum(quotas.values())
        return BudgetAllocation(
            quotas=quotas,
            channel_scores=scores,
            total_budget=total_budget,
            allocated=allocated,
            unallocated=total_budget - allocated,
        )


def allocate_channel_budgets(
    profile: QueryProfile,
    observations: Sequence[ChannelObservation],
    *,
    total_budget: int,
    minimum_per_available: int = 1,
) -> dict[str, int]:
    """Convenience wrapper returning only channel quotas."""

    allocation = AdaptiveBudgetAllocator(
        minimum_per_available=minimum_per_available
    ).allocate(profile, observations, total_budget=total_budget)
    return dict(allocation.quotas)


@dataclass(frozen=True)
class TrainingExample:
    """One pointwise fusion-training observation."""

    features: Mapping[str, float]
    label: float
    weight: float = 1.0

    def __post_init__(self) -> None:
        label = float(self.label)
        weight = float(self.weight)
        if label not in {0.0, 1.0}:
            raise ValueError("label must be 0 or 1")
        if not math.isfinite(weight) or weight <= 0.0:
            raise ValueError("training weight must be finite and positive")
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "weight", weight)
        for name, value in self.features.items():
            if not isinstance(name, str) or not name:
                raise ValueError("feature names must be non-empty strings")
            if not math.isfinite(float(value)):
                raise ValueError(f"feature {name!r} must be finite")


class LinearSoftmaxFusion:
    """Deterministic pointwise linear fusion with binary softmax loss.

    Feature standardisation is learned from the training set and serialized
    with the weights.  Missing features are treated as zero in the original
    feature space, making independently absent recall channels explicit.
    """

    SCHEMA_VERSION = 1

    def __init__(self, feature_names: Sequence[str] | None = None) -> None:
        resolved = tuple(feature_names or ())
        if (
            len(resolved) != len(set(resolved))
            or any(not isinstance(name, str) or not name for name in resolved)
        ):
            raise ValueError("feature_names must be unique non-empty strings")
        self.feature_names: tuple[str, ...] = resolved
        self.weights: list[float] = [0.0 for _name in resolved]
        self.bias = 0.0
        self.means: list[float] = [0.0 for _name in resolved]
        self.scales: list[float] = [1.0 for _name in resolved]
        self.fitted = False

    @staticmethod
    def _sigmoid(logit: float) -> float:
        if logit >= 0.0:
            value = math.exp(-min(logit, 35.0))
            return 1.0 / (1.0 + value)
        value = math.exp(max(logit, -35.0))
        return value / (1.0 + value)

    @staticmethod
    def _coerce_examples(
        examples: Iterable[
            TrainingExample
            | tuple[Mapping[str, float], float]
            | tuple[Mapping[str, float], float, float]
        ],
    ) -> list[TrainingExample]:
        resolved: list[TrainingExample] = []
        for example in examples:
            if isinstance(example, TrainingExample):
                resolved.append(example)
            elif len(example) == 2:
                features, label = example
                resolved.append(TrainingExample(features, label))
            elif len(example) == 3:
                features, label, weight = example
                resolved.append(TrainingExample(features, label, weight))
            else:
                raise ValueError("examples must contain (features, label[, weight])")
        if not resolved:
            raise ValueError("at least one training example is required")
        return resolved

    def _vector(self, features: Mapping[str, float]) -> list[float]:
        values: list[float] = []
        for index, name in enumerate(self.feature_names):
            raw = float(features.get(name, 0.0))
            if not math.isfinite(raw):
                raise ValueError(f"feature {name!r} must be finite")
            values.append((raw - self.means[index]) / self.scales[index])
        return values

    def fit(
        self,
        examples: Iterable[
            TrainingExample
            | tuple[Mapping[str, float], float]
            | tuple[Mapping[str, float], float, float]
        ],
        *,
        epochs: int = 500,
        learning_rate: float = 0.12,
        l2: float = 0.001,
    ) -> "LinearSoftmaxFusion":
        """Fit using deterministic full-batch gradient descent."""

        if not isinstance(epochs, int) or isinstance(epochs, bool) or epochs < 1:
            raise ValueError("epochs must be a positive integer")
        if not math.isfinite(learning_rate) or learning_rate <= 0.0:
            raise ValueError("learning_rate must be finite and positive")
        if not math.isfinite(l2) or l2 < 0.0:
            raise ValueError("l2 must be finite and non-negative")
        rows = self._coerce_examples(examples)
        if not self.feature_names:
            self.feature_names = tuple(
                sorted({name for row in rows for name in row.features})
            )
            if not self.feature_names:
                raise ValueError("training examples must contain at least one feature")

        total_weight = sum(row.weight for row in rows)
        self.means = []
        self.scales = []
        for name in self.feature_names:
            mean = sum(row.weight * float(row.features.get(name, 0.0)) for row in rows) / total_weight
            variance = sum(
                row.weight * (float(row.features.get(name, 0.0)) - mean) ** 2
                for row in rows
            ) / total_weight
            self.means.append(mean)
            self.scales.append(max(math.sqrt(variance), 1e-12))

        self.weights = [0.0 for _name in self.feature_names]
        positive_rate = sum(row.weight * row.label for row in rows) / total_weight
        clipped_rate = min(1.0 - 1e-6, max(1e-6, positive_rate))
        self.bias = math.log(clipped_rate / (1.0 - clipped_rate))

        vectors = [self._vector(row.features) for row in rows]
        for epoch in range(epochs):
            weight_gradients = [0.0 for _name in self.feature_names]
            bias_gradient = 0.0
            for row, vector in zip(rows, vectors):
                logit = self.bias + sum(
                    weight * value for weight, value in zip(self.weights, vector)
                )
                error = (self._sigmoid(logit) - row.label) * row.weight
                bias_gradient += error
                for index, value in enumerate(vector):
                    weight_gradients[index] += error * value
            # A mild inverse-square-root schedule remains deterministic and is
            # less sensitive to user-selected epoch counts.
            step = learning_rate / math.sqrt(1.0 + epoch * 0.01)
            self.bias -= step * bias_gradient / total_weight
            for index in range(len(self.weights)):
                gradient = weight_gradients[index] / total_weight + l2 * self.weights[index]
                self.weights[index] -= step * gradient

        self.fitted = True
        return self

    def predict(self, features: Mapping[str, float]) -> float:
        """Return the learned relevance probability for one candidate."""

        if not self.fitted:
            raise RuntimeError("fusion model must be fitted before prediction")
        vector = self._vector(features)
        logit = self.bias + sum(
            weight * value for weight, value in zip(self.weights, vector)
        )
        return self._sigmoid(logit)

    predict_proba = predict

    def rank(
        self,
        candidates: Mapping[str, Mapping[str, float]],
    ) -> list[tuple[str, float]]:
        """Rank candidates by probability with stable identifier tie breaking."""

        scored = [(candidate_id, self.predict(features)) for candidate_id, features in candidates.items()]
        return sorted(scored, key=lambda item: (-item[1], item[0]))

    def to_dict(self) -> dict[str, object]:
        if not self.fitted:
            raise RuntimeError("cannot serialize an unfitted fusion model")
        return {
            "schema_version": self.SCHEMA_VERSION,
            "model_type": "linear_binary_softmax",
            "feature_names": list(self.feature_names),
            "weights": self.weights,
            "bias": self.bias,
            "means": self.means,
            "scales": self.scales,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "LinearSoftmaxFusion":
        if payload.get("schema_version") != cls.SCHEMA_VERSION:
            raise ValueError("unsupported fusion model schema")
        if payload.get("model_type") != "linear_binary_softmax":
            raise ValueError("unsupported fusion model type")
        names = payload.get("feature_names")
        weights = payload.get("weights")
        means = payload.get("means")
        scales = payload.get("scales")
        if not all(isinstance(value, list) for value in (names, weights, means, scales)):
            raise ValueError("invalid fusion model arrays")
        model = cls([str(name) for name in names])
        expected = len(model.feature_names)
        if any(len(value) != expected for value in (weights, means, scales)):
            raise ValueError("fusion model arrays have inconsistent lengths")
        model.weights = [float(value) for value in weights]
        model.means = [float(value) for value in means]
        model.scales = [float(value) for value in scales]
        model.bias = float(payload.get("bias", 0.0))
        numeric = model.weights + model.means + model.scales + [model.bias]
        if not all(math.isfinite(value) for value in numeric) or any(
            scale <= 0.0 for scale in model.scales
        ):
            raise ValueError("fusion model contains invalid numeric values")
        model.fitted = True
        return model

    def save(self, path: str | Path) -> None:
        """Atomically save the model as portable JSON."""

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2
        ) + "\n"
        temporary_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_name = handle.name
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, target)
        finally:
            if temporary_name and os.path.exists(temporary_name):
                os.unlink(temporary_name)

    @classmethod
    def load(cls, path: str | Path) -> "LinearSoftmaxFusion":
        with Path(path).open(encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("fusion model JSON must contain an object")
        return cls.from_dict(payload)


@dataclass(frozen=True)
class SelectivePrediction:
    """Calibrated score and an auditable accept/abstain decision."""

    raw_score: float
    score: float
    confidence: float
    uncertainty: float
    abstain: bool
    reason: str | None


class SelectiveCalibrator:
    """Temperature calibration plus a precision-constrained reject option."""

    def __init__(
        self,
        *,
        target_precision: float = 0.80,
        min_confidence: float = 0.55,
        min_evidence_coverage: float = 0.25,
        min_channel_agreement: float = 0.20,
    ) -> None:
        self.target_precision = _clamp01(target_precision, field_name="target_precision")
        self.min_confidence = _clamp01(min_confidence, field_name="min_confidence")
        self.min_evidence_coverage = _clamp01(
            min_evidence_coverage, field_name="min_evidence_coverage"
        )
        self.min_channel_agreement = _clamp01(
            min_channel_agreement, field_name="min_channel_agreement"
        )
        self.temperature = 1.0
        self.threshold = 0.5
        self.can_accept = True
        self.fitted = False

    @staticmethod
    def _validate_probability(value: float, *, field_name: str) -> float:
        return _clamp01(value, field_name=field_name)

    @staticmethod
    def _logit(probability: float) -> float:
        clipped = min(1.0 - 1e-9, max(1e-9, probability))
        return math.log(clipped / (1.0 - clipped))

    @staticmethod
    def _sigmoid(logit: float) -> float:
        if logit >= 0.0:
            value = math.exp(-min(logit, 35.0))
            return 1.0 / (1.0 + value)
        value = math.exp(max(logit, -35.0))
        return value / (1.0 + value)

    def calibrate(self, raw_score: float) -> float:
        raw_score = self._validate_probability(raw_score, field_name="raw_score")
        return self._sigmoid(self._logit(raw_score) / self.temperature)

    def fit(
        self,
        raw_scores: Sequence[float],
        labels: Sequence[float],
    ) -> "SelectiveCalibrator":
        """Fit temperature by NLL and the widest precision-safe threshold."""

        if len(raw_scores) != len(labels) or not raw_scores:
            raise ValueError("raw_scores and labels must have the same non-zero length")
        scores = [
            self._validate_probability(value, field_name="raw_score")
            for value in raw_scores
        ]
        resolved_labels: list[float] = []
        for label in labels:
            value = float(label)
            if value not in {0.0, 1.0}:
                raise ValueError("calibration labels must be 0 or 1")
            resolved_labels.append(value)

        best_temperature = 1.0
        best_loss = math.inf
        # A fixed log-spaced grid is fast, deterministic, and adequate for a
        # one-parameter calibration layer.
        for index in range(161):
            temperature = math.exp(-2.0 + 4.0 * index / 160.0)
            loss = 0.0
            for score, label in zip(scores, resolved_labels):
                probability = self._sigmoid(self._logit(score) / temperature)
                probability = min(1.0 - 1e-12, max(1e-12, probability))
                loss -= label * math.log(probability) + (1.0 - label) * math.log(1.0 - probability)
            loss /= len(scores)
            if loss < best_loss - 1e-15:
                best_loss = loss
                best_temperature = temperature
        self.temperature = best_temperature

        calibrated = [self.calibrate(score) for score in scores]
        ranked = sorted(
            zip(calibrated, resolved_labels, range(len(calibrated))),
            key=lambda item: (-item[0], item[2]),
        )
        positives = 0.0
        seen = 0
        widest_safe_threshold: float | None = None
        index = 0
        # Evaluate whole equal-score groups.  A threshold cannot select only
        # part of a tie, so prefix-level selection would overstate precision.
        while index < len(ranked):
            group_score = ranked[index][0]
            group_positives = 0.0
            group_size = 0
            while index < len(ranked) and ranked[index][0] == group_score:
                group_positives += ranked[index][1]
                group_size += 1
                index += 1
            positives += group_positives
            seen += group_size
            if positives / seen + 1e-12 >= self.target_precision:
                widest_safe_threshold = group_score
        if widest_safe_threshold is not None:
            self.threshold = widest_safe_threshold
            self.can_accept = True
        else:
            self.threshold = 1.0
            self.can_accept = False
        self.fitted = True
        return self

    def decide(
        self,
        raw_score: float,
        *,
        evidence_coverage: float = 1.0,
        channel_agreement: float = 1.0,
    ) -> SelectivePrediction:
        """Calibrate a score and reject unreliable recommendations."""

        raw_score = self._validate_probability(raw_score, field_name="raw_score")
        evidence_coverage = self._validate_probability(
            evidence_coverage, field_name="evidence_coverage"
        )
        channel_agreement = self._validate_probability(
            channel_agreement, field_name="channel_agreement"
        )
        score = self.calibrate(raw_score)
        reliability = math.sqrt(evidence_coverage * channel_agreement)
        confidence = score * (0.5 + 0.5 * reliability)
        uncertainty = 1.0 - confidence

        reason: str | None = None
        if evidence_coverage < self.min_evidence_coverage:
            reason = "insufficient_evidence_coverage"
        elif channel_agreement < self.min_channel_agreement:
            reason = "insufficient_channel_agreement"
        elif not self.can_accept or score + 1e-12 < self.threshold:
            reason = "below_calibrated_relevance_threshold"
        elif confidence < self.min_confidence:
            reason = "high_predictive_uncertainty"
        return SelectivePrediction(
            raw_score=raw_score,
            score=score,
            confidence=confidence,
            uncertainty=uncertainty,
            abstain=reason is not None,
            reason=reason,
        )


__all__ = [
    "AdaptiveBudgetAllocator",
    "BudgetAllocation",
    "ChannelObservation",
    "ChannelTraits",
    "DEFAULT_CHANNEL_TRAITS",
    "LinearSoftmaxFusion",
    "QueryProfile",
    "SelectiveCalibrator",
    "SelectivePrediction",
    "TrainingExample",
    "allocate_channel_budgets",
]
