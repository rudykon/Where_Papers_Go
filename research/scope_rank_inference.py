"""Inference-only application of frozen SCOPE-Rank variants to sealed queries."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
from time import perf_counter
from typing import Any, Mapping, Sequence
import uuid

import numpy as np

from where_paper_go.scope_rank import SelectiveCalibrator

from .baselines import BM25Baseline
from .data import (
    ResearchDataError,
    build_run_binding,
    canonical_json_sha256,
    load_blind_query_dataset,
    load_jsonl_corpus,
    ordered_ids_sha256,
    runtime_provenance,
    sha256_file,
    write_run,
)
from .scope_rank_runs import (
    PairwiseLinearRanker,
    PairwiseRankerReport,
    VariantSpec,
    _channel_agreement,
    _evidence_coverage,
    _feature_names,
    _load_sources,
    _passes_hard_constraints,
    _prototype_provenance,
    _score_query,
    _subject_documents,
    _variant_specs,
    build_query_representation,
)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchDataError(f"{label} must be an object")
    return value


def _resolve(config_path: Path, value: Any) -> Path:
    path = Path(str(value or ""))
    if not str(path):
        raise ResearchDataError("frozen-inference configuration contains an empty path")
    return path if path.is_absolute() else (config_path.parent / path).resolve()


def _read_object(path: Path, label: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResearchDataError(f"cannot read {label}: {path}") from exc
    return _mapping(payload, label)


def _atomic_json(path: Path, payload: Any) -> None:
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
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
                json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False)
                + "\n"
            )
    temporary.replace(path)


def _artifact(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def load_frozen_pairwise_ranker(
    path: Path,
    *,
    expected_sha256: str,
    expected_features: Sequence[str],
) -> PairwiseLinearRanker:
    """Load the exact train-only model without invoking any fit operation."""

    if len(expected_sha256) != 64 or sha256_file(path) != expected_sha256:
        raise ResearchDataError("frozen SCOPE-Rank model SHA-256 mismatch")
    payload = _read_object(path, "frozen SCOPE-Rank model")
    if payload.get("schema_version") != 1 or payload.get("model_type") != "pairwise_linear_logistic":
        raise ResearchDataError("unsupported frozen SCOPE-Rank model schema")
    feature_names = tuple(str(value) for value in payload.get("feature_names", ()))
    if feature_names != tuple(expected_features):
        raise ResearchDataError("frozen SCOPE-Rank feature schema mismatch")
    scales = np.asarray(payload.get("scales"), dtype=np.float64)
    weights = np.asarray(payload.get("weights"), dtype=np.float64)
    if (
        scales.shape != (len(feature_names),)
        or weights.shape != (len(feature_names),)
        or not np.isfinite(scales).all()
        or not np.isfinite(weights).all()
        or np.any(scales <= 0.0)
    ):
        raise ResearchDataError("frozen SCOPE-Rank model contains invalid arrays")
    raw_report = _mapping(payload.get("training_report"), "training_report")
    try:
        report = PairwiseRankerReport(
            feature_count=int(raw_report["feature_count"]),
            training_query_count=int(raw_report["training_query_count"]),
            skipped_query_count=int(raw_report["skipped_query_count"]),
            pair_count=int(raw_report["pair_count"]),
            epochs=int(raw_report["epochs"]),
            learning_rate=float(raw_report["learning_rate"]),
            l2=float(raw_report["l2"]),
            final_loss=float(raw_report["final_loss"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ResearchDataError("invalid frozen SCOPE-Rank training report") from exc
    if report.feature_count != len(feature_names) or not math.isfinite(report.final_loss):
        raise ResearchDataError("frozen SCOPE-Rank report does not match model")
    ranker = PairwiseLinearRanker(feature_names)
    ranker.scales = scales
    ranker.weights = weights
    ranker.report = report
    ranker.fitted = True
    return ranker


def _frozen_calibrator(
    method_config: Mapping[str, Any], record: Mapping[str, Any]
) -> SelectiveCalibrator:
    raw = _mapping(method_config.get("calibrator"), "method.calibrator")
    calibrator = SelectiveCalibrator(
        target_precision=float(raw.get("target_precision", 0.15)),
        min_confidence=float(raw.get("min_confidence", 0.0)),
        min_evidence_coverage=float(raw.get("min_evidence_coverage", 0.15)),
        min_channel_agreement=float(raw.get("min_channel_agreement", 0.10)),
    )
    try:
        calibrator.temperature = float(record["temperature"])
        calibrator.threshold = float(record["threshold"])
        calibrator.can_accept = bool(record["can_accept"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ResearchDataError("frozen calibrator record is incomplete") from exc
    if (
        not math.isfinite(calibrator.temperature)
        or calibrator.temperature <= 0.0
        or not 0.0 <= calibrator.threshold <= 1.0
        or float(record.get("target_precision", -1.0)) != calibrator.target_precision
    ):
        raise ResearchDataError("frozen calibrator parameters are invalid")
    calibrator.fitted = True
    return calibrator


def _verify_sealed_input(
    config_path: Path, config: Mapping[str, Any]
) -> tuple[Mapping[str, Any], Path, dict[str, Any]]:
    sealed_config = _mapping(config.get("sealed_test"), "sealed_test")
    manifest_path = _resolve(config_path, sealed_config.get("manifest"))
    expected_manifest_hash = str(sealed_config.get("manifest_sha256") or "")
    if len(expected_manifest_hash) != 64 or sha256_file(manifest_path) != expected_manifest_hash:
        raise ResearchDataError("sealed-test manifest SHA-256 mismatch")
    manifest = _read_object(manifest_path, "sealed-test manifest")
    if manifest.get("artifact_type") != "future_sealed_test" or manifest.get("status") != "labels_sealed_predictions_pending":
        raise ResearchDataError("sealed-test manifest is not awaiting predictions")
    dataset = _mapping(manifest.get("dataset"), "sealed-test dataset")
    blind_record = _mapping(dataset.get("blind_queries"), "blind query artifact")
    labels_record = _mapping(dataset.get("sealed_labels"), "sealed label artifact")
    blind_path = Path(str(blind_record.get("path") or ""))
    labels_path = Path(str(labels_record.get("path") or ""))
    for label, path, record in (
        ("blind queries", blind_path, blind_record),
        ("sealed labels", labels_path, labels_record),
    ):
        if (
            not path.is_file()
            or path.stat().st_size != int(record.get("bytes", -1))
            or sha256_file(path) != str(record.get("sha256") or "")
        ):
            raise ResearchDataError(f"{label} artifact mismatch")
    if labels_path.stat().st_mode & 0o077:
        raise ResearchDataError("sealed label vault is accessible to group or other users")
    return manifest, blind_path, {
        "path": str(labels_path.resolve()),
        "sha256": str(labels_record["sha256"]),
        "bytes": int(labels_record["bytes"]),
        "content_parsed": False,
        "verification": "byte hash and permission bits only",
    }


def _rewrite_published_run_path(manifest_path: Path, run_path: Path) -> None:
    payload = dict(_read_object(manifest_path, "staged run manifest"))
    output = dict(_mapping(payload.get("output"), "run output"))
    output["path"] = str(run_path.resolve())
    payload["output"] = output
    _atomic_json(manifest_path, payload)


def build_frozen_scope_predictions(
    config_path: Path,
    *,
    generation_command: Sequence[str],
) -> dict[str, Any]:
    """Apply frozen weights/calibrators while the label vault stays unopened."""

    config_path = config_path.resolve()
    config = _read_object(config_path, "frozen-inference configuration")
    if config.get("schema_version") != 1:
        raise ResearchDataError("unsupported frozen-inference configuration schema")
    if config.get("status") != "future_sealed_prelabel_inference":
        raise ResearchDataError("inference config is not marked pre-label")
    if config.get("offline_only") is not True or config.get("search_free") is not True:
        raise ResearchDataError("sealed-test prediction must be offline and Search-free")
    sealed_manifest, dataset_path, label_vault = _verify_sealed_input(config_path, config)
    corpus_config = _mapping(config.get("corpus"), "corpus")
    profiles_path = _resolve(config_path, corpus_config.get("path"))
    bundle = load_blind_query_dataset(
        dataset_path,
        query_fields=tuple(
            _mapping(config.get("dataset"), "dataset").get(
                "query_fields", ("title", "abstract")
            )
        ),
    )
    corpus = load_jsonl_corpus(
        profiles_path,
        id_field=str(corpus_config.get("id_field") or "venue_id"),
        text_fields=tuple(corpus_config.get("text_fields") or ("name",)),
        snapshot_field=str(corpus_config.get("snapshot_field") or "snapshot_date"),
    )
    query_ids = tuple(query.query_id for query in bundle.queries)
    candidate_ids = tuple(document.doc_id for document in corpus)
    freeze = _mapping(sealed_manifest.get("method_freeze"), "sealed method freeze")
    frozen_candidates = _mapping(freeze.get("candidates"), "frozen candidates")
    if (
        len(candidate_ids) != int(frozen_candidates.get("count", -1))
        or ordered_ids_sha256(tuple(sorted(candidate_ids)))
        != str(frozen_candidates.get("ordered_ids_sha256") or "")
    ):
        raise ResearchDataError("sealed inference candidate universe changed after freeze")
    method_config = _mapping(config.get("method"), "method")
    frozen_method = _mapping(config.get("frozen_method"), "frozen_method")
    expected_method_hash = str(frozen_method.get("method_config_sha256") or "")
    if canonical_json_sha256(method_config) != expected_method_hash:
        raise ResearchDataError("sealed inference method hyperparameters changed after freeze")
    cutoff = str(method_config.get("profile_cutoff") or "")
    if any(document.snapshot_date > cutoff for document in corpus):
        raise ResearchDataError("candidate profile is newer than frozen method cutoff")
    temporal = _mapping(sealed_manifest.get("temporal_boundary"), "temporal boundary")
    future_window = _mapping(temporal.get("future_window"), "future window")
    for query in bundle.queries:
        if not str(future_window.get("from")) <= query.publication_date <= str(future_window.get("until")):
            raise ResearchDataError("blind query falls outside committed future window")

    suite_path = _resolve(config_path, frozen_method.get("development_suite_manifest"))
    suite_hash = str(frozen_method.get("development_suite_manifest_sha256") or "")
    if len(suite_hash) != 64 or sha256_file(suite_path) != suite_hash:
        raise ResearchDataError("development SCOPE-Rank suite manifest mismatch")
    development_suite = _read_object(suite_path, "development SCOPE-Rank suite")
    if development_suite.get("status") != "complete_exposed_development_not_sealed":
        raise ResearchDataError("development SCOPE-Rank suite is not the frozen source")

    output_dir = _resolve(config_path, config.get("output_dir"))
    if output_dir.exists():
        raise ResearchDataError(
            f"sealed prediction output exists and will not be overwritten: {output_dir}"
        )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = output_dir.with_name(
        "." + output_dir.name + ".building-" + uuid.uuid4().hex[:12]
    )
    staging.mkdir()
    active_dir = staging
    try:
        binding = build_run_binding(
            dataset_path=dataset_path,
            profiles_path=profiles_path,
            query_ids=query_ids,
            candidate_ids=candidate_ids,
            configuration=config,
            configuration_path=config_path,
        )
        channel_values = config.get("channels")
        if not isinstance(channel_values, list):
            raise ResearchDataError("sealed inference channels must be an array")
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
        subject_path = staging / "subject_route.jsonl"
        runtime = runtime_provenance()
        implementation_revision = "frozen-scope-rank-inference-v1@" + sha256_file(
            Path(__file__)
        )
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
                    "total_ms": (perf_counter() - subject_started) * 1000.0,
                    "external_api_calls": 0,
                    "estimated_external_cost_usd": 0.0,
                    "offline_only": True,
                    "search_free": True,
                    "failed_query_count": 0,
                },
                "label_boundary": "physically label-free query file",
            },
        )
        source_records["subject_route"] = {
            "run": subject_manifest["output"],
            "manifest": _artifact(subject_path.with_suffix(".jsonl.manifest.json")),
            "derived_without_labels": True,
        }
        fixed_raw = _mapping(method_config.get("fixed_quotas"), "method.fixed_quotas")
        fixed_quotas = {str(name): int(value) for name, value in fixed_raw.items()}
        total_budget = int(method_config.get("total_recall_budget", 350))
        top_k = int(method_config.get("top_k", 100))
        all_specs = {spec.name: spec for spec in _variant_specs()}
        requested = tuple(str(value) for value in frozen_method.get("variants", ()))
        if not requested or len(set(requested)) != len(requested) or not set(requested) <= set(all_specs):
            raise ResearchDataError("sealed inference variant list is empty or unknown")
        development_variants = _mapping(
            development_suite.get("variants"), "development variants"
        )
        decisions: list[dict[str, Any]] = []
        explanations: list[dict[str, Any]] = []
        variant_records: dict[str, Any] = {}
        for variant_name in requested:
            started = perf_counter()
            spec = all_specs[variant_name]
            development = _mapping(
                development_variants.get(variant_name),
                f"development variant {variant_name}",
            )
            ranker = None
            model_record = development.get("model")
            if spec.fusion == "learned":
                model = _mapping(model_record, f"{variant_name}.model")
                model_path = Path(str(model.get("path") or ""))
                ranker = load_frozen_pairwise_ranker(
                    model_path,
                    expected_sha256=str(model.get("sha256") or ""),
                    expected_features=_feature_names(spec),
                )
            calibrator = _frozen_calibrator(
                method_config,
                _mapping(development.get("calibration"), f"{variant_name}.calibration"),
            )
            run = {}
            abstained = 0
            pool_sizes: list[int] = []
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
                        f"sealed SCOPE-Rank produced an empty ranking: {query_id}"
                    )
                run[query_id] = ranking
                pool_sizes.append(len(route.pool))
                top = ranking[0]
                prediction = calibrator.decide(
                    top.score,
                    evidence_coverage=_evidence_coverage(documents[top.doc_id]),
                    channel_agreement=_channel_agreement(top.doc_id, views, spec),
                )
                abstained += prediction.abstain
                decisions.append(
                    {
                        "variant": variant_name,
                        "query_id": query_id,
                        "top_candidate_id": top.doc_id,
                        "raw_score": top.score,
                        "calibrated_score": prediction.score,
                        "confidence": prediction.confidence,
                        "abstain": prediction.abstain,
                        "reason": prediction.reason,
                        "pool_size": len(route.pool),
                        "hard_filtered_count": route.hard_filtered_count,
                        "route_quotas": dict(route.quotas),
                    }
                )
                if variant_name == "scope_rank_full":
                    for rank, item in enumerate(ranking[:5], 1):
                        allowed, checks = _passes_hard_constraints(
                            representations[query_id], documents[item.doc_id], cutoff=cutoff
                        )
                        explanations.append(
                            {
                                "query_id": query_id,
                                "candidate_id": item.doc_id,
                                "rank": rank,
                                "score": item.score,
                                "channel_evidence": {
                                    source: {
                                        "rank": int(view[item.doc_id]["position"]),
                                        "raw_score": view[item.doc_id]["raw_score"],
                                    }
                                    for source, view in views.items()
                                    if item.doc_id in view
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
                                    "profile_level": documents[item.doc_id].metadata.get("profile_level"),
                                    "evidence_grade": documents[item.doc_id].metadata.get("evidence_grade"),
                                    "history_paper_count": documents[item.doc_id].metadata.get("history_paper_count", 0),
                                    "prototypes": _prototype_provenance(documents[item.doc_id]),
                                },
                            }
                        )
            run_path = staging / f"{variant_name}.jsonl"
            run_manifest = write_run(
                run_path,
                run,
                binding=binding,
                query_ids=query_ids,
                candidate_ids=candidate_ids,
                top_k=top_k,
                method={
                    "name": variant_name,
                    "kind": "frozen_scope_rank_inference",
                    "implementation": "research.scope_rank_inference.build_frozen_scope_predictions",
                    "implementation_revision": implementation_revision,
                    "configuration_sha256": binding["configuration"]["canonical_sha256"],
                },
                command=generation_command,
                working_directory=Path.cwd(),
                runtime=runtime,
                additional_manifest_fields={
                    "variant": asdict(spec),
                    "frozen_training_source": {
                        "development_suite_manifest_sha256": suite_hash,
                        "model": model_record,
                        "calibration": development["calibration"],
                        "refit_performed": False,
                        "validation_labels_accessed": False,
                        "sealed_labels_accessed": False,
                    },
                    "selective_output": {
                        "query_count": len(query_ids),
                        "abstained_query_count": abstained,
                        "accepted_query_count": len(query_ids) - abstained,
                    },
                    "execution": {
                        "total_ms": (perf_counter() - started) * 1000.0,
                        "mean_pool_size": sum(pool_sizes) / len(pool_sizes),
                        "external_api_calls": 0,
                        "estimated_external_cost_usd": 0.0,
                        "offline_only": True,
                        "search_free": True,
                        "failed_query_count": 0,
                    },
                },
            )
            variant_records[variant_name] = {
                "run": run_manifest["output"],
                "manifest": _artifact(run_path.with_suffix(".jsonl.manifest.json")),
                "selective_output": run_manifest["selective_output"],
                "execution": run_manifest["execution"],
            }

        decisions_path = staging / "decisions.jsonl"
        explanations_path = staging / "scope_rank_full.explanations.jsonl"
        _atomic_jsonl(decisions_path, decisions)
        _atomic_jsonl(explanations_path, explanations)
        os.replace(staging, output_dir)
        active_dir = output_dir
        for name in ("subject_route", *requested):
            run_path = output_dir / f"{name}.jsonl"
            _rewrite_published_run_path(
                run_path.with_suffix(".jsonl.manifest.json"), run_path
            )

        published_sources = dict(source_records)
        published_sources["subject_route"] = {
            "run": _artifact(output_dir / "subject_route.jsonl"),
            "manifest": _artifact(
                output_dir / "subject_route.jsonl.manifest.json"
            ),
            "derived_without_labels": True,
        }
        published_variants = {
            name: {
                **variant_records[name],
                "run": _artifact(output_dir / f"{name}.jsonl"),
                "manifest": _artifact(
                    output_dir / f"{name}.jsonl.manifest.json"
                ),
            }
            for name in requested
        }
        commitment = {
            "schema_version": 1,
            "artifact_type": "sealed_prediction_commitment",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "predictions_committed_before_label_access",
            "config": _artifact(config_path),
            "sealed_test_manifest": _artifact(
                _resolve(config_path, _mapping(config.get("sealed_test"), "sealed_test").get("manifest"))
            ),
            "label_vault_commitment": label_vault,
            "query_count": len(query_ids),
            "query_ids_sha256": ordered_ids_sha256(query_ids),
            "candidate_count": len(candidate_ids),
            "candidate_ids_sha256": ordered_ids_sha256(tuple(sorted(candidate_ids))),
            "sources": published_sources,
            "variants": published_variants,
            "outputs": {
                "decisions": _artifact(output_dir / "decisions.jsonl"),
                "explanations": _artifact(
                    output_dir / "scope_rank_full.explanations.jsonl"
                ),
            },
            "label_boundary": {
                "input_schema": "queries.blind.jsonl closed schema",
                "qrels_present_during_prediction": False,
                "label_file_content_parsed": False,
                "label_file_verified_by_hash_only": True,
                "refit_performed": False,
                "hyperparameters_changed": False,
            },
            "runtime": runtime,
            "generation": {
                "command": [str(value) for value in generation_command],
                "working_directory": str(Path.cwd().resolve()),
            },
            "execution": {
                "offline_only": True,
                "search_free": True,
                "external_api_calls": 0,
                "estimated_external_cost_usd": 0.0,
                "failed_query_count": 0,
            },
        }
        commitment_path = output_dir / "prediction_commitment.json"
        _atomic_json(commitment_path, commitment)
        manifest = {
            "schema_version": 1,
            "artifact_type": "frozen_scope_rank_sealed_predictions",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "predictions_committed_labels_unopened",
            "binding": binding,
            "development_suite": _artifact(suite_path),
            "sealed_test": _artifact(
                _resolve(config_path, _mapping(config.get("sealed_test"), "sealed_test").get("manifest"))
            ),
            "prediction_commitment": _artifact(commitment_path),
            "coverage": {
                "query_count": len(query_ids),
                "candidate_count": len(candidate_ids),
                "variant_count": len(requested),
                "all_rankings_complete": True,
                "failed_query_count": 0,
            },
            "claim_boundary": (
                "Predictions are immutable and labels remain unopened. No sealed-test "
                "metric or effectiveness claim exists at this state."
            ),
        }
        manifest_path = output_dir / "manifest.json"
        _atomic_json(manifest_path, manifest)
        return {**manifest, "manifest": _artifact(manifest_path)}
    except Exception:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        failed = output_dir.with_name(
            output_dir.name + f".failed-{timestamp}-{uuid.uuid4().hex[:8]}"
        )
        if active_dir.exists():
            os.replace(active_dir, failed)
        raise


__all__ = ["build_frozen_scope_predictions", "load_frozen_pairwise_ranker"]
