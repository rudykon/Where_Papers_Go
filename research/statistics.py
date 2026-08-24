"""Paired confidence intervals and significance tests for retrieval runs."""

from __future__ import annotations

import itertools
import math
import random
from statistics import fmean
from typing import Any, Mapping


def _paired_differences(
    left: Mapping[str, Mapping[str, float]],
    right: Mapping[str, Mapping[str, float]],
    metric: str,
) -> tuple[list[str], list[float]]:
    query_ids = sorted(set(left) & set(right))
    if not query_ids:
        raise ValueError("paired comparison has no shared query IDs")
    try:
        differences = [float(left[qid][metric]) - float(right[qid][metric]) for qid in query_ids]
    except KeyError as exc:
        raise ValueError(f"metric is missing from paired evaluation: {metric}") from exc
    return query_ids, differences


def _percentile(values: list[float], probability: float) -> float:
    if not values:
        raise ValueError("cannot compute percentile of an empty sample")
    ordered = sorted(values)
    position = min(max(probability, 0.0), 1.0) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def paired_bootstrap_ci(
    left: Mapping[str, Mapping[str, float]],
    right: Mapping[str, Mapping[str, float]],
    *,
    metric: str,
    iterations: int = 10_000,
    confidence: float = 0.95,
    seed: int = 20260814,
) -> dict[str, Any]:
    """Percentile bootstrap CI for macro ``left - right`` on paired queries."""

    if iterations < 100:
        raise ValueError("paired bootstrap requires at least 100 iterations")
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between zero and one")
    query_ids, differences = _paired_differences(left, right, metric)
    generator = random.Random(seed)
    count = len(differences)
    samples = [
        fmean(differences[generator.randrange(count)] for _ in range(count))
        for _ in range(iterations)
    ]
    alpha = 1.0 - confidence
    observed = fmean(differences)
    return {
        "metric": metric,
        "query_count": len(query_ids),
        "left_minus_right": observed,
        "confidence": confidence,
        "ci_low": _percentile(samples, alpha / 2.0),
        "ci_high": _percentile(samples, 1.0 - alpha / 2.0),
        "iterations": iterations,
        "seed": seed,
        "probability_positive": sum(value > 0 for value in samples) / iterations,
    }


def paired_permutation_test(
    left: Mapping[str, Mapping[str, float]],
    right: Mapping[str, Mapping[str, float]],
    *,
    metric: str,
    iterations: int = 10_000,
    seed: int = 20260814,
    exact_limit: int = 16,
) -> dict[str, Any]:
    """Two-sided paired randomization test using exact signs when feasible."""

    if iterations < 100:
        raise ValueError("paired permutation test requires at least 100 iterations")
    query_ids, differences = _paired_differences(left, right, metric)
    observed = abs(fmean(differences))
    tolerance = 1e-15
    if len(differences) <= exact_limit:
        statistics = (
            abs(fmean(sign * value for sign, value in zip(signs, differences)))
            for signs in itertools.product((-1.0, 1.0), repeat=len(differences))
        )
        extreme = 0
        total = 0
        for statistic in statistics:
            total += 1
            if statistic + tolerance >= observed:
                extreme += 1
        p_value = extreme / total
        mode = "exact"
    else:
        generator = random.Random(seed)
        extreme = 0
        for _ in range(iterations):
            statistic = abs(
                fmean(value if generator.random() < 0.5 else -value for value in differences)
            )
            if statistic + tolerance >= observed:
                extreme += 1
        # Add-one correction prevents a zero Monte Carlo p-value.
        p_value = (extreme + 1) / (iterations + 1)
        total = iterations
        mode = "monte_carlo"
    return {
        "metric": metric,
        "query_count": len(query_ids),
        "left_minus_right": fmean(differences),
        "two_sided_p_value": p_value,
        "mode": mode,
        "permutations": total,
        "seed": seed,
    }
