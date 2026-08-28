"""Paired confidence intervals and significance tests for retrieval runs."""

from __future__ import annotations

import itertools
import math
from statistics import fmean
from typing import Any, Mapping

import numpy as np


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
    generator = np.random.default_rng(seed)
    count = len(differences)
    values = np.asarray(differences, dtype=np.float64)
    samples: list[float] = []
    # Bound peak memory while keeping the expensive resampling in native code.
    batch_size = min(256, iterations)
    for offset in range(0, iterations, batch_size):
        size = min(batch_size, iterations - offset)
        indices = generator.integers(0, count, size=(size, count))
        samples.extend(values[indices].mean(axis=1).tolist())
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
        generator = np.random.default_rng(seed)
        values = np.asarray(differences, dtype=np.float64)
        extreme = 0
        batch_size = min(256, iterations)
        for offset in range(0, iterations, batch_size):
            size = min(batch_size, iterations - offset)
            signs = generator.integers(
                0, 2, size=(size, len(values)), dtype=np.int8
            )
            signs = signs * 2 - 1
            statistics = np.abs(signs @ values / len(values))
            extreme += int(np.count_nonzero(statistics + tolerance >= observed))
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


def adjust_p_values(
    p_values: Mapping[str, float],
) -> dict[str, dict[str, float]]:
    """Apply Holm family-wise and Benjamini-Hochberg FDR corrections.

    The mapping keys are stable comparison identities.  Both procedures are
    computed over the complete supplied family and returned in input order.
    """

    if not p_values:
        raise ValueError("multiple-comparison correction requires p-values")
    checked: list[tuple[str, float, int]] = []
    for index, (raw_name, raw_value) in enumerate(p_values.items()):
        name = str(raw_name).strip()
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid p-value for {name!r}") from exc
        if not name or not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"invalid p-value for {name!r}")
        checked.append((name, value, index))
    if len({name for name, _value, _index in checked}) != len(checked):
        raise ValueError("multiple-comparison identities must be unique")

    ordered = sorted(checked, key=lambda item: (item[1], item[2], item[0]))
    count = len(ordered)
    holm: dict[str, float] = {}
    running_holm = 0.0
    for rank, (name, value, _index) in enumerate(ordered, 1):
        running_holm = max(running_holm, (count - rank + 1) * value)
        holm[name] = min(1.0, running_holm)

    benjamini_hochberg: dict[str, float] = {}
    running_bh = 1.0
    for rank in range(count, 0, -1):
        name, value, _index = ordered[rank - 1]
        running_bh = min(running_bh, value * count / rank)
        benjamini_hochberg[name] = min(1.0, running_bh)

    return {
        name: {
            "raw_two_sided_p_value": value,
            "holm_family_wise_p_value": holm[name],
            "benjamini_hochberg_fdr_p_value": benjamini_hochberg[name],
        }
        for name, value, _index in checked
    }
