"""Label-free preflight for the one-time future sealed-test evaluation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .data import (
    ResearchDataError,
    build_run_binding,
    canonical_json_sha256,
    load_blind_query_dataset,
    load_jsonl_corpus,
    load_score_run,
    ordered_ids_sha256,
    sha256_file,
)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchDataError(f"{label} must be an object")
    return value


def _read_object(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResearchDataError(f"cannot read {label}: {path}") from exc
    return _mapping(value, label)


def _resolve(config_path: Path, value: Any) -> Path:
    path = Path(str(value or ""))
    if not str(path):
        raise ResearchDataError("sealed preflight contains an empty path")
    return path if path.is_absolute() else (config_path.parent / path).resolve()


def _verified_artifact(path: Path, record: Mapping[str, Any], label: str) -> None:
    if not path.is_file():
        raise ResearchDataError(f"{label} does not exist: {path}")
    expected_hash = str(record.get("sha256") or "")
    if len(expected_hash) != 64 or sha256_file(path) != expected_hash:
        raise ResearchDataError(f"{label} SHA-256 mismatch")
    if "bytes" in record and path.stat().st_size != int(record.get("bytes", -1)):
        raise ResearchDataError(f"{label} byte-size mismatch")


def _method_identity(sidecar: Mapping[str, Any]) -> dict[str, str]:
    method = _mapping(sidecar.get("method"), "run method")
    identity = {
        key: str(method[key])
        for key in (
            "model_revision",
            "provider_fingerprint",
            "implementation_revision",
        )
        if str(method.get(key) or "").strip()
    }
    if not identity:
        raise ResearchDataError("sealed run has no exact method identity")
    return identity


def _committed_pairs(commitment: Mapping[str, Any]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for section_name in ("sources", "variants"):
        section = _mapping(commitment.get(section_name), section_name)
        for value in section.values():
            record = _mapping(value, f"{section_name}[]")
            run = _mapping(record.get("run"), "committed run")
            manifest = _mapping(record.get("manifest"), "committed manifest")
            pair = (str(run.get("sha256") or ""), str(manifest.get("sha256") or ""))
            if any(len(value) != 64 for value in pair):
                raise ResearchDataError("prediction commitment contains invalid hashes")
            pairs.add(pair)
    return pairs


def _require_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise ResearchDataError(f"sealed preflight mismatch for {label}")


def preflight_sealed_evaluation(config_path: Path) -> dict[str, Any]:
    """Verify every label-independent condition before the one permitted unseal."""

    config_path = config_path.resolve()
    config = _read_object(config_path, "sealed-evaluation configuration")
    _require_equal(config.get("schema_version"), 1, "config schema_version")
    _require_equal(
        config.get("status"),
        "predictions_committed_before_label_unseal",
        "config status",
    )
    _require_equal(config.get("offline_only"), True, "offline_only")
    _require_equal(config.get("search_free"), True, "search_free")

    sealed_config = _mapping(config.get("sealed_test"), "sealed_test")
    sealed_manifest_path = _resolve(config_path, sealed_config.get("manifest"))
    _verified_artifact(
        sealed_manifest_path,
        {"sha256": sealed_config.get("manifest_sha256")},
        "sealed-test manifest",
    )
    sealed_manifest = _read_object(sealed_manifest_path, "sealed-test manifest")
    _require_equal(
        sealed_manifest.get("artifact_type"), "future_sealed_test", "artifact type"
    )
    _require_equal(
        sealed_manifest.get("status"),
        "labels_sealed_predictions_pending",
        "sealed-test status",
    )

    frozen_config_record = _mapping(sealed_manifest.get("config"), "freeze config")
    frozen_config_path = Path(str(frozen_config_record.get("path") or ""))
    _verified_artifact(frozen_config_path, frozen_config_record, "freeze config")
    frozen_config = _read_object(frozen_config_path, "freeze config")
    frozen_method = _mapping(frozen_config.get("method_freeze"), "method freeze")
    sealed_freeze = _mapping(sealed_manifest.get("method_freeze"), "sealed freeze")
    for name in ("method_hyperparameters", "metrics", "statistics"):
        expected_hash = str(sealed_freeze.get(name + "_sha256") or "")
        _require_equal(
            canonical_json_sha256(_mapping(frozen_method.get(name), name)),
            expected_hash,
            name + " hash",
        )
    frozen_family = tuple(str(value) for value in frozen_method.get("method_family", ()))
    _require_equal(
        tuple(str(value) for value in sealed_freeze.get("method_family", ())),
        frozen_family,
        "method family",
    )

    dataset = _mapping(sealed_manifest.get("dataset"), "sealed dataset")
    blind_record = _mapping(dataset.get("blind_queries"), "blind queries")
    label_record = _mapping(dataset.get("sealed_labels"), "sealed labels")
    blind_path = Path(str(blind_record.get("path") or ""))
    label_path = Path(str(label_record.get("path") or ""))
    _verified_artifact(blind_path, blind_record, "blind queries")
    _verified_artifact(label_path, label_record, "sealed label vault")
    if label_path.stat().st_mode & 0o077:
        raise ResearchDataError("sealed label vault is accessible to group or other users")
    bundle = load_blind_query_dataset(blind_path)
    query_ids = tuple(query.query_id for query in bundle.queries)
    expected_query_count = int(dataset.get("record_count", -1))
    _require_equal(len(query_ids), expected_query_count, "query denominator")

    commitment_config = _mapping(
        config.get("prediction_commitment"), "prediction_commitment"
    )
    commitment_path = _resolve(config_path, commitment_config.get("path"))
    _verified_artifact(
        commitment_path,
        {"sha256": commitment_config.get("sha256")},
        "prediction commitment",
    )
    commitment = _read_object(commitment_path, "prediction commitment")
    _require_equal(
        commitment.get("status"),
        "predictions_committed_before_label_access",
        "prediction status",
    )
    committed_label = _mapping(
        commitment.get("label_vault_commitment"), "committed label vault"
    )
    _require_equal(
        committed_label.get("sha256"), label_record.get("sha256"), "label commitment"
    )
    _require_equal(
        committed_label.get("content_parsed"), False, "pre-commit label boundary"
    )
    _require_equal(commitment.get("query_count"), len(query_ids), "committed queries")
    _require_equal(
        commitment.get("query_ids_sha256"),
        ordered_ids_sha256(query_ids),
        "query ordering",
    )

    output_dir = _resolve(config_path, config.get("output_dir"))
    if output_dir.exists():
        raise ResearchDataError(f"sealed evaluation output already exists: {output_dir}")
    prior_access = sorted(output_dir.parent.glob(output_dir.name + ".label-access-*.json"))
    if prior_access:
        raise ResearchDataError("sealed label-access audit already exists: " + str(prior_access[0]))

    corpus_config = _mapping(config.get("corpus"), "corpus")
    profiles_path = _resolve(config_path, corpus_config.get("path"))
    corpus = load_jsonl_corpus(
        profiles_path,
        id_field=str(corpus_config.get("id_field") or "venue_id"),
        text_fields=tuple(corpus_config.get("text_fields") or ("name",)),
        snapshot_field=str(corpus_config.get("snapshot_field") or "snapshot_date"),
    )
    candidate_ids = tuple(document.doc_id for document in corpus)
    frozen_candidates = _mapping(sealed_freeze.get("candidates"), "frozen candidates")
    _require_equal(len(candidate_ids), int(frozen_candidates.get("count", -1)), "candidates")
    candidate_fingerprint = ordered_ids_sha256(tuple(sorted(candidate_ids)))
    _require_equal(
        candidate_fingerprint,
        frozen_candidates.get("ordered_ids_sha256"),
        "candidate ordering",
    )
    _require_equal(
        sha256_file(profiles_path), frozen_candidates.get("profiles_sha256"), "profiles"
    )
    _require_equal(commitment.get("candidate_count"), len(candidate_ids), "committed candidates")
    _require_equal(
        commitment.get("candidate_ids_sha256"),
        candidate_fingerprint,
        "committed candidate ordering",
    )

    expected_binding = build_run_binding(
        dataset_path=blind_path,
        profiles_path=profiles_path,
        query_ids=query_ids,
        candidate_ids=candidate_ids,
        configuration=config,
        configuration_path=config_path,
    )
    committed_pairs = _committed_pairs(commitment)
    raw_methods = config.get("methods")
    if not isinstance(raw_methods, list):
        raise ResearchDataError("sealed methods must be an array")
    method_names = tuple(
        str(_mapping(value, "methods[]").get("name") or "") for value in raw_methods
    )
    _require_equal(method_names, frozen_family, "ordered frozen method family")
    metrics = _mapping(frozen_method.get("metrics"), "frozen metrics")
    depth = int(metrics.get("retrieval_depth", 100))
    method_reports: list[dict[str, Any]] = []
    for raw in raw_methods:
        method_config = _mapping(raw, "methods[]")
        name = str(method_config.get("name") or "")
        run_path = _resolve(config_path, method_config.get("path"))
        manifest_path = _resolve(config_path, method_config.get("manifest_path"))
        run_hash = str(method_config.get("run_sha256") or "")
        manifest_hash = str(method_config.get("manifest_sha256") or "")
        _verified_artifact(run_path, {"sha256": run_hash}, name + " run")
        _verified_artifact(
            manifest_path, {"sha256": manifest_hash}, name + " sidecar"
        )
        if (run_hash, manifest_hash) not in committed_pairs:
            raise ResearchDataError(f"method {name!r} is not in the commitment")
        sidecar = _read_object(manifest_path, name + " sidecar")
        method_record = _mapping(sidecar.get("method"), name + " method")
        _require_equal(method_record.get("name"), name, name + " method identity")
        binding = _mapping(sidecar.get("binding"), name + " binding")
        generation = _mapping(binding.get("configuration"), name + " configuration")
        load_score_run(
            run_path,
            expected_query_ids=query_ids,
            candidate_ids=candidate_ids,
            expected_binding=expected_binding,
            expected_manifest_sha256=manifest_hash,
            expected_configuration_sha256=str(generation.get("canonical_sha256") or ""),
            expected_method_identity=_method_identity(sidecar),
            manifest_path=manifest_path,
            top_k=depth,
        )
        execution = _mapping(sidecar.get("execution"), name + " execution")
        _require_equal(execution.get("failed_query_count"), 0, name + " failures")
        _require_equal(execution.get("external_api_calls"), 0, name + " API calls")
        _require_equal(execution.get("search_free"), True, name + " Search boundary")
        method_reports.append(
            {"name": name, "run_sha256": run_hash, "manifest_sha256": manifest_hash}
        )

    evaluation = _mapping(config.get("evaluation"), "evaluation")
    _require_equal(
        tuple(int(value) for value in evaluation.get("cutoffs", ())),
        tuple(int(value) for value in metrics.get("cutoffs", ())),
        "metric cutoffs",
    )
    statistics = _mapping(config.get("statistics"), "statistics")
    frozen_statistics = _mapping(frozen_method.get("statistics"), "frozen statistics")
    for name in (
        "comparison_family",
        "metric",
        "bootstrap_iterations",
        "permutation_iterations",
        "confidence",
        "seed",
    ):
        _require_equal(statistics.get(name), frozen_statistics.get(name), "statistics." + name)
    _require_equal(statistics.get("metric"), metrics.get("primary"), "primary metric")
    _require_equal(
        frozen_statistics.get("multiple_comparison_corrections"),
        ["holm_family_wise", "benjamini_hochberg_fdr"],
        "multiple-comparison corrections",
    )
    _require_equal(
        metrics.get("denominator_policy"),
        "all_300_queries_no_failure_removal",
        "denominator policy",
    )

    return {
        "schema_version": 1,
        "artifact_type": "sealed_evaluation_label_free_preflight",
        "status": "ready_for_single_label_access",
        "label_content_parsed": False,
        "config": {
            "path": str(config_path),
            "sha256": sha256_file(config_path),
            "bytes": config_path.stat().st_size,
        },
        "sealed_test_manifest_sha256": sha256_file(sealed_manifest_path),
        "prediction_commitment_sha256": sha256_file(commitment_path),
        "label_vault": {
            "sha256": sha256_file(label_path),
            "bytes": label_path.stat().st_size,
            "private_mode": oct(label_path.stat().st_mode & 0o777),
            "content_parsed": False,
        },
        "coverage": {
            "query_count": len(query_ids),
            "candidate_count": len(candidate_ids),
            "method_count": len(method_reports),
            "method_order": list(method_names),
        },
        "methods": method_reports,
        "protocol": {
            "metric": statistics["metric"],
            "cutoffs": list(evaluation["cutoffs"]),
            "bootstrap_iterations": statistics["bootstrap_iterations"],
            "permutation_iterations": statistics["permutation_iterations"],
            "confidence": statistics["confidence"],
            "seed": statistics["seed"],
            "comparison_family": statistics["comparison_family"],
            "corrections": list(
                frozen_statistics["multiple_comparison_corrections"]
            ),
        },
        "output_dir": str(output_dir),
        "prior_label_access_audit_count": 0,
    }


__all__ = ["preflight_sealed_evaluation"]
