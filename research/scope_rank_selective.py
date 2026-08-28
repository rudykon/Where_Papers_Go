"""Post-hoc selective-risk evaluation for a frozen SCOPE-Rank suite.

This module consumes already-published decisions.  It never fits or changes a
ranker/calibrator and therefore cannot feed exposed validation/test labels back
into the frozen method.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
from statistics import NormalDist
from typing import Any, Mapping, Sequence
import uuid

from .data import (
    ResearchDataError,
    canonical_json_sha256,
    load_recent_journal_dataset,
    ordered_ids_sha256,
    runtime_provenance,
    sha256_file,
    temporal_split,
)
from .leakage import identity_unsafe_query_ids


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _read_object(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), parse_constant=_reject_constant
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ResearchDataError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, Mapping):
        raise ResearchDataError(f"{label} must be an object: {path}")
    return value


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchDataError(f"{label} must be an object")
    return value


def _resolve(config_path: Path, value: Any) -> Path:
    path = Path(str(value or ""))
    if not str(path):
        raise ResearchDataError("selective-evaluation config contains an empty path")
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


def _artifact(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _iter_decisions(path: Path) -> Sequence[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    raise ResearchDataError(
                        f"{path}:{line_number}: blank decision row"
                    )
                try:
                    row = json.loads(line, parse_constant=_reject_constant)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise ResearchDataError(
                        f"{path}:{line_number}: invalid decision JSON"
                    ) from exc
                if not isinstance(row, Mapping):
                    raise ResearchDataError(
                        f"{path}:{line_number}: decision must be an object"
                    )
                rows.append(row)
    except (OSError, UnicodeError) as exc:
        raise ResearchDataError(f"cannot read decisions: {path}") from exc
    if not rows:
        raise ResearchDataError("selective decisions are empty")
    return rows


def _probability(value: Any, label: str) -> float:
    try:
        resolved = float(value)
    except (TypeError, ValueError) as exc:
        raise ResearchDataError(f"{label} is not numeric") from exc
    if not math.isfinite(resolved) or not 0.0 <= resolved <= 1.0:
        raise ResearchDataError(f"{label} must be finite and in [0, 1]")
    return resolved


def _wilson_interval(
    successes: int, total: int, *, confidence: float
) -> dict[str, float] | None:
    if total == 0:
        return None
    z = NormalDist().inv_cdf(0.5 + confidence / 2.0)
    proportion = successes / total
    denominator = 1.0 + z * z / total
    center = (proportion + z * z / (2.0 * total)) / denominator
    radius = (
        z
        * math.sqrt(
            proportion * (1.0 - proportion) / total
            + z * z / (4.0 * total * total)
        )
        / denominator
    )
    return {"low": max(0.0, center - radius), "high": min(1.0, center + radius)}


def selective_metrics(
    rows: Sequence[Mapping[str, Any]],
    *,
    bins: int,
    confidence: float,
) -> dict[str, Any]:
    """Compute coverage, selective precision/risk and calibration diagnostics."""

    if not rows:
        raise ResearchDataError("selective metric slice is empty")
    if bins < 2:
        raise ResearchDataError("calibration bins must be at least two")
    if not 0.0 < confidence < 1.0:
        raise ResearchDataError("confidence must be in (0, 1)")
    accepted = [row for row in rows if not bool(row["abstain"])]
    correct = sum(bool(row["correct"]) for row in rows)
    accepted_correct = sum(bool(row["correct"]) for row in accepted)
    scores = [_probability(row["calibrated_score"], "calibrated_score") for row in rows]
    labels = [1.0 if bool(row["correct"]) else 0.0 for row in rows]
    brier = sum((score - label) ** 2 for score, label in zip(scores, labels)) / len(rows)
    log_loss = -sum(
        label * math.log(min(1.0 - 1e-12, max(1e-12, score)))
        + (1.0 - label)
        * math.log(min(1.0 - 1e-12, max(1e-12, 1.0 - score)))
        for score, label in zip(scores, labels)
    ) / len(rows)
    bin_rows: list[dict[str, Any]] = []
    ece = 0.0
    for index in range(bins):
        members = [
            offset
            for offset, score in enumerate(scores)
            if min(bins - 1, int(score * bins)) == index
        ]
        if not members:
            continue
        mean_score = sum(scores[offset] for offset in members) / len(members)
        accuracy = sum(labels[offset] for offset in members) / len(members)
        gap = abs(mean_score - accuracy)
        ece += len(members) / len(rows) * gap
        bin_rows.append(
            {
                "index": index,
                "lower": index / bins,
                "upper": (index + 1) / bins,
                "count": len(members),
                "mean_score": mean_score,
                "accuracy": accuracy,
                "absolute_gap": gap,
            }
        )
    accepted_count = len(accepted)
    selective_precision = (
        accepted_correct / accepted_count if accepted_count else None
    )
    reasons = Counter(
        str(row.get("reason") or "unspecified")
        for row in rows
        if bool(row["abstain"])
    )
    return {
        "query_count": len(rows),
        "top1_correct_count": correct,
        "top1_accuracy": correct / len(rows),
        "top1_accuracy_wilson_ci": _wilson_interval(
            correct, len(rows), confidence=confidence
        ),
        "accepted_count": accepted_count,
        "abstained_count": len(rows) - accepted_count,
        "coverage": accepted_count / len(rows),
        "coverage_wilson_ci": _wilson_interval(
            accepted_count, len(rows), confidence=confidence
        ),
        "accepted_correct_count": accepted_correct,
        "selective_precision": selective_precision,
        "selective_risk": (
            1.0 - selective_precision if selective_precision is not None else None
        ),
        "selective_precision_wilson_ci": _wilson_interval(
            accepted_correct, accepted_count, confidence=confidence
        ),
        "correct_acceptance_recall": (
            accepted_correct / correct if correct else None
        ),
        "abstained_correct_count": correct - accepted_correct,
        "mean_calibrated_score": sum(scores) / len(scores),
        "brier_score": brier,
        "log_loss": log_loss,
        "expected_calibration_error": ece,
        "calibration_bins": bin_rows,
        "abstention_reason_counts": dict(sorted(reasons.items())),
    }


def evaluate_scope_rank_selective(
    config_path: Path,
    *,
    generation_command: Sequence[str],
) -> dict[str, Any]:
    """Evaluate frozen selective decisions without altering the method."""

    config_path = config_path.resolve()
    config = _read_object(config_path, "selective-evaluation config")
    if config.get("schema_version") != 1:
        raise ResearchDataError("unsupported selective-evaluation schema")
    if config.get("offline_only") is not True:
        raise ResearchDataError("selective evaluation requires offline_only=true")
    if config.get("evaluation_status") != "exposed_development_not_sealed":
        raise ResearchDataError(
            "selective evaluation must be marked exposed_development_not_sealed"
        )
    output_dir = _resolve(config_path, config.get("output_dir"))
    if output_dir.exists():
        raise ResearchDataError(
            f"selective-evaluation output exists and will not be overwritten: {output_dir}"
        )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir()
    try:
        dataset_config = _mapping(config.get("dataset"), "dataset")
        split_config = _mapping(config.get("temporal_split"), "temporal_split")
        dataset_path = _resolve(config_path, dataset_config.get("path"))
        suite_path = _resolve(config_path, config.get("suite_manifest"))
        decisions_path = _resolve(config_path, config.get("decisions"))
        leakage_path = _resolve(config_path, config.get("leakage_audit"))
        expected_hashes = {
            "suite_manifest": str(config.get("suite_manifest_sha256") or ""),
            "decisions": str(config.get("decisions_sha256") or ""),
            "leakage_audit": str(config.get("leakage_audit_sha256") or ""),
        }
        actual_paths = {
            "suite_manifest": suite_path,
            "decisions": decisions_path,
            "leakage_audit": leakage_path,
        }
        for name, path in actual_paths.items():
            if len(expected_hashes[name]) != 64 or sha256_file(path) != expected_hashes[name]:
                raise ResearchDataError(f"selective-evaluation {name} SHA-256 mismatch")

        suite = _read_object(suite_path, "SCOPE-Rank suite manifest")
        leakage = _read_object(leakage_path, "SCOPE-Rank leakage audit")
        if suite.get("artifact_type") != "scope_rank_suite":
            raise ResearchDataError("source manifest is not a SCOPE-Rank suite")
        if suite.get("status") != "complete_exposed_development_not_sealed":
            raise ResearchDataError("source SCOPE-Rank suite is not complete")
        if leakage.get("passed") is not True:
            raise ResearchDataError("source SCOPE-Rank leakage audit did not pass")
        suite_outputs = _mapping(suite.get("outputs"), "suite.outputs")
        decision_record = _mapping(suite_outputs.get("decisions"), "suite decisions")
        if decision_record.get("sha256") != expected_hashes["decisions"]:
            raise ResearchDataError("suite manifest does not bind the decisions file")
        leakage_record = _mapping(suite.get("leakage_audit"), "suite leakage audit")
        if leakage_record.get("sha256") != expected_hashes["leakage_audit"]:
            raise ResearchDataError("suite manifest does not bind the leakage audit")

        bundle = load_recent_journal_dataset(
            dataset_path,
            query_fields=tuple(dataset_config.get("query_fields") or ("title", "abstract")),
            relevance_field=str(dataset_config.get("relevance_field") or "gold_journal_id"),
        )
        split = temporal_split(
            bundle.queries,
            start=str(split_config.get("start") or "") or None,
            train_end=str(split_config.get("train_end") or ""),
            validation_end=str(split_config.get("validation_end") or ""),
            test_end=str(split_config.get("test_end") or ""),
        )
        query_ids = tuple(query.query_id for query in bundle.queries)
        suite_binding = _mapping(suite.get("binding"), "suite.binding")
        suite_dataset = _mapping(suite_binding.get("dataset"), "suite.binding.dataset")
        suite_queries = _mapping(suite_binding.get("queries"), "suite.binding.queries")
        if suite_dataset.get("sha256") != sha256_file(dataset_path):
            raise ResearchDataError("selective dataset does not match suite binding")
        if (
            suite_queries.get("count") != len(query_ids)
            or suite_queries.get("ordered_ids_sha256") != ordered_ids_sha256(query_ids)
        ):
            raise ResearchDataError("selective query order does not match suite binding")

        variants = _mapping(suite.get("variants"), "suite.variants")
        variant_names = tuple(str(value) for value in variants)
        expected_pairs = {(variant, query_id) for variant in variant_names for query_id in query_ids}
        observed_pairs: set[tuple[str, str]] = set()
        rows_by_variant: dict[str, list[dict[str, Any]]] = {
            variant: [] for variant in variant_names
        }
        for row in _iter_decisions(decisions_path):
            variant = str(row.get("variant") or "")
            query_id = str(row.get("query_id") or "")
            pair = (variant, query_id)
            if pair not in expected_pairs or pair in observed_pairs:
                raise ResearchDataError(
                    f"unknown or duplicate selective decision: {variant}/{query_id}"
                )
            observed_pairs.add(pair)
            top_candidate_id = str(row.get("top_candidate_id") or "")
            if not top_candidate_id:
                raise ResearchDataError("selective decision has an empty top candidate")
            if not isinstance(row.get("abstain"), bool):
                raise ResearchDataError("selective abstain flag must be boolean")
            abstain = bool(row["abstain"])
            reason = row.get("reason")
            if abstain != bool(reason):
                raise ResearchDataError("selective abstention reason is inconsistent")
            calibrated_score = _probability(
                row.get("calibrated_score"), "calibrated_score"
            )
            confidence_score = _probability(row.get("confidence"), "confidence")
            correct = any(
                gain > 0.0 and candidate_id == top_candidate_id
                for candidate_id, gain in bundle.qrels[query_id].items()
            )
            rows_by_variant[variant].append(
                {
                    **dict(row),
                    "calibrated_score": calibrated_score,
                    "confidence": confidence_score,
                    "correct": correct,
                }
            )
        if observed_pairs != expected_pairs:
            raise ResearchDataError(
                "selective decisions do not cover every variant/query exactly once"
            )

        bins = int(config.get("calibration_bins", 10))
        confidence = float(config.get("confidence", 0.95))
        test_ids = set(split.test)
        unsafe_ids = set(identity_unsafe_query_ids(leakage)) & test_ids
        safe_ids = test_ids - unsafe_ids
        methods: dict[str, Any] = {}
        for variant, rows in rows_by_variant.items():
            primary_rows = [row for row in rows if row["query_id"] in test_ids]
            safe_rows = [row for row in rows if row["query_id"] in safe_ids]
            variant_record = _mapping(variants[variant], f"variant {variant}")
            methods[variant] = {
                "primary_test": selective_metrics(
                    primary_rows, bins=bins, confidence=confidence
                ),
                "identity_safe_test": selective_metrics(
                    safe_rows, bins=bins, confidence=confidence
                ),
                "frozen_run": variant_record.get("run"),
                "frozen_calibration": variant_record.get("calibration"),
                "frozen_selective_output": variant_record.get("selective_output"),
            }

        full = _mapping(variants.get("scope_rank_full"), "scope_rank_full")
        no_calibration = _mapping(
            variants.get("scope_rank_ablate_calibration"),
            "scope_rank_ablate_calibration",
        )
        no_constraints = _mapping(
            variants.get("scope_rank_ablate_constraint_features"),
            "scope_rank_ablate_constraint_features",
        )
        report = {
            "schema_version": 1,
            "artifact_type": "scope_rank_selective_metrics",
            "status": "complete_exposed_development_not_sealed",
            "evaluation_split": {
                "name": "test",
                "query_count": len(test_ids),
                "query_ids_sha256": ordered_ids_sha256(split.test),
                "identity_safe_query_count": len(safe_ids),
                "identity_unsafe_query_count": len(unsafe_ids),
            },
            "label_usage": {
                "mode": "post_hoc_evaluation_only",
                "method_or_threshold_refit": False,
                "validation_labels_used_for_refit": False,
                "test_labels_used_for_metrics_only": True,
                "sealed_test": False,
            },
            "inputs": {
                "suite_manifest": _artifact(suite_path),
                "decisions": _artifact(decisions_path),
                "leakage_audit": _artifact(leakage_path),
                "dataset": _artifact(dataset_path),
            },
            "metric_policy": {
                "confidence": confidence,
                "interval": "Wilson score",
                "calibration_bins": bins,
                "ece_binning": "equal-width",
                "zero_acceptance_precision": None,
            },
            "methods": methods,
            "frozen_ablation_checks": {
                "calibration_ablation_preserves_ranking": (
                    _mapping(full.get("run"), "full.run").get("sha256")
                    == _mapping(no_calibration.get("run"), "no_calibration.run").get("sha256")
                ),
                "constraint_feature_ablation_preserves_ranking": (
                    _mapping(full.get("run"), "full.run").get("sha256")
                    == _mapping(no_constraints.get("run"), "no_constraints.run").get("sha256")
                ),
                "note": (
                    "Calibration is a reject option and cannot alter retrieval order. "
                    "The exposed dataset has no user quartile constraints, so its "
                    "constraint features are pairwise constant while hard filters remain enabled."
                ),
            },
            "execution": {
                "external_api_calls": 0,
                "estimated_external_cost_usd": 0.0,
                "offline_only": True,
                "search_free": True,
            },
        }
        metrics_path = output_dir / "metrics.json"
        _atomic_json(metrics_path, report)
        manifest = {
            "schema_version": 1,
            "artifact_type": "scope_rank_selective_evaluation",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": report["status"],
            "configuration": {
                "canonical_sha256": canonical_json_sha256(config),
                "source": _artifact(config_path),
            },
            "runtime": runtime_provenance(),
            "generation": {
                "command": [str(value) for value in generation_command],
                "working_directory": str(Path.cwd().resolve()),
            },
            "inputs": report["inputs"],
            "output": _artifact(metrics_path),
            "coverage": {
                "variant_count": len(methods),
                "decision_count": len(observed_pairs),
                "test_query_count": len(test_ids),
                "identity_safe_test_query_count": len(safe_ids),
            },
            "execution": report["execution"],
        }
        manifest_path = output_dir / "manifest.json"
        _atomic_json(manifest_path, manifest)
        return {**manifest, "manifest": _artifact(manifest_path)}
    except Exception:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        failed_path = output_dir.with_name(
            f"{output_dir.name}.failed-{timestamp}-{uuid.uuid4().hex[:8]}"
        )
        os.replace(output_dir, failed_path)
        raise


__all__ = ["evaluate_scope_rank_selective", "selective_metrics"]
