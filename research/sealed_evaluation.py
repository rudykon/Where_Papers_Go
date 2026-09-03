"""Post-commit evaluation for a physically separated future label vault."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import combinations
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence
import uuid

from .data import (
    DatasetBundle,
    ResearchDataError,
    TemporalSplit,
    build_run_binding,
    load_blind_query_dataset,
    load_jsonl_corpus,
    load_score_run,
    sha256_file,
)
from .leakage import audit_leakage
from .metrics import evaluate_run, stratified_metrics
from .reporting import build_query_strata, summarize_strata
from .statistics import adjust_p_values, paired_bootstrap_ci, paired_permutation_test
from .types import Query, Run


@dataclass(frozen=True)
class UnsealedLabels:
    bundle: DatasetBundle
    label_file: Path
    label_sha256: str
    record_count: int


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchDataError(f"{label} must be an object")
    return value


def _resolve(config_path: Path, value: Any) -> Path:
    path = Path(str(value or ""))
    if not str(path):
        raise ResearchDataError("sealed-evaluation configuration contains an empty path")
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


def _artifact(path: Path, *, published_path: Path | None = None) -> dict[str, Any]:
    return {
        "path": str((published_path or path).resolve()),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _committed_artifact_pairs(commitment: Mapping[str, Any]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for section_name in ("sources", "variants"):
        section = _mapping(commitment.get(section_name), section_name)
        for raw in section.values():
            record = _mapping(raw, f"{section_name}[]")
            run = _mapping(record.get("run"), "committed run")
            manifest = _mapping(record.get("manifest"), "committed manifest")
            run_hash = str(run.get("sha256") or "")
            manifest_hash = str(manifest.get("sha256") or "")
            if len(run_hash) != 64 or len(manifest_hash) != 64:
                raise ResearchDataError("prediction commitment contains invalid hashes")
            pairs.add((run_hash, manifest_hash))
    return pairs


def unseal_labels_after_prediction_commitment(
    *,
    blind_path: Path,
    label_path: Path,
    expected_label_sha256: str,
    expected_query_count: int,
) -> UnsealedLabels:
    """Join labels only after the caller has verified the prediction commitment."""

    if len(expected_label_sha256) != 64 or sha256_file(label_path) != expected_label_sha256:
        raise ResearchDataError("sealed label commitment mismatch")
    blind = load_blind_query_dataset(blind_path)
    labels: dict[str, Mapping[str, Any]] = {}
    with label_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ResearchDataError(
                    f"{label_path}:{line_number}: invalid sealed label JSON"
                ) from exc
            if not isinstance(row, Mapping):
                raise ResearchDataError(f"{label_path}:{line_number}: expected an object")
            query_id = str(row.get("paper_id") or "").strip()
            gold = str(row.get("gold_journal_id") or "").strip()
            if not query_id or not gold or query_id in labels:
                raise ResearchDataError(
                    f"{label_path}:{line_number}: invalid or duplicate sealed label"
                )
            labels[query_id] = row
    query_ids = tuple(query.query_id for query in blind.queries)
    if (
        len(query_ids) != expected_query_count
        or len(labels) != expected_query_count
        or set(labels) != set(query_ids)
    ):
        raise ResearchDataError("sealed labels do not match the committed denominator")
    queries: list[Query] = []
    qrels = {}
    for query in blind.queries:
        row = labels[query.query_id]
        gold = str(row["gold_journal_id"])
        queries.append(
            Query(
                query_id=query.query_id,
                text=query.text,
                publication_date=query.publication_date,
                title=query.title,
                abstract=query.abstract,
                doi=str(row.get("doi") or ""),
                gold_venue_name=str(row.get("gold_journal_name") or ""),
                metadata={
                    "field": row.get("broad_field") or "unknown",
                    "quartile": row.get("gold_jcr_quartile") or "unknown",
                    "language": query.metadata.get("language") or "unknown",
                },
            )
        )
        qrels[query.query_id] = {gold: 1.0}
    bundle = DatasetBundle(tuple(queries), qrels, blind.source_rows)
    return UnsealedLabels(
        bundle=bundle,
        label_file=label_path,
        label_sha256=expected_label_sha256,
        record_count=len(labels),
    )


def _method_identity(manifest: Mapping[str, Any]) -> dict[str, str]:
    method = _mapping(manifest.get("method"), "run method")
    identity = {
        key: str(method[key])
        for key in ("model_revision", "provider_fingerprint", "implementation_revision")
        if str(method.get(key) or "").strip()
    }
    if not identity:
        raise ResearchDataError("sealed run has no exact method identity")
    return identity


def evaluate_sealed_test(
    config_path: Path,
    *,
    generation_command: Sequence[str],
) -> dict[str, Any]:
    """Verify prediction hashes, then unseal once and evaluate the full denominator."""

    config_path = config_path.resolve()
    config = _read_object(config_path, "sealed-evaluation configuration")
    if config.get("schema_version") != 1:
        raise ResearchDataError("unsupported sealed-evaluation configuration schema")
    if config.get("status") != "predictions_committed_before_label_unseal":
        raise ResearchDataError("sealed evaluation is not authorized to unseal")
    if config.get("offline_only") is not True or config.get("search_free") is not True:
        raise ResearchDataError("sealed evaluation must remain offline and Search-free")

    sealed_config = _mapping(config.get("sealed_test"), "sealed_test")
    sealed_manifest_path = _resolve(config_path, sealed_config.get("manifest"))
    if sha256_file(sealed_manifest_path) != str(sealed_config.get("manifest_sha256") or ""):
        raise ResearchDataError("sealed-test manifest changed before evaluation")
    sealed_manifest = _read_object(sealed_manifest_path, "sealed-test manifest")
    dataset_record = _mapping(sealed_manifest.get("dataset"), "sealed dataset")
    blind_record = _mapping(dataset_record.get("blind_queries"), "blind queries")
    label_record = _mapping(dataset_record.get("sealed_labels"), "sealed labels")
    blind_path = Path(str(blind_record.get("path") or ""))
    label_path = Path(str(label_record.get("path") or ""))
    if sha256_file(blind_path) != str(blind_record.get("sha256") or ""):
        raise ResearchDataError("blind query file changed before evaluation")

    commitment_config = _mapping(config.get("prediction_commitment"), "prediction_commitment")
    commitment_path = _resolve(config_path, commitment_config.get("path"))
    expected_commitment_hash = str(commitment_config.get("sha256") or "")
    if len(expected_commitment_hash) != 64 or sha256_file(commitment_path) != expected_commitment_hash:
        raise ResearchDataError("prediction commitment SHA-256 mismatch")
    commitment = _read_object(commitment_path, "prediction commitment")
    if commitment.get("status") != "predictions_committed_before_label_access":
        raise ResearchDataError("predictions were not committed before label access")
    committed_label = _mapping(
        commitment.get("label_vault_commitment"), "label vault commitment"
    )
    if (
        str(committed_label.get("sha256") or "")
        != str(label_record.get("sha256") or "")
        or int(commitment.get("query_count", -1))
        != int(dataset_record.get("record_count", -2))
    ):
        raise ResearchDataError("prediction commitment does not bind this label vault")
    committed_pairs = _committed_artifact_pairs(commitment)
    output_dir = _resolve(config_path, config.get("output_dir"))
    if output_dir.exists():
        raise ResearchDataError(
            f"sealed evaluation output exists and will not be overwritten: {output_dir}"
        )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    prior_access = sorted(
        output_dir.parent.glob(output_dir.name + ".label-access-*.json")
    )
    if prior_access:
        raise ResearchDataError(
            "sealed labels have already been accessed; refusing a second evaluation: "
            + str(prior_access[0])
        )

    # This is the first semantic read of labels. Everything above is a byte/hash
    # check and all score runs have already been committed.
    accessed_at = datetime.now(timezone.utc).isoformat()
    unsealed = unseal_labels_after_prediction_commitment(
        blind_path=blind_path,
        label_path=label_path,
        expected_label_sha256=str(label_record.get("sha256") or ""),
        expected_query_count=int(commitment["query_count"]),
    )
    label_access = {
        "schema_version": 1,
        "artifact_type": "sealed_label_access_audit",
        "accessed_at": accessed_at,
        "reason": "post-commit metric evaluation",
        "prediction_commitment": _artifact(commitment_path),
        "label_file": {
            "path": str(label_path.resolve()),
            "sha256": unsealed.label_sha256,
            "bytes": label_path.stat().st_size,
            "record_count": unsealed.record_count,
        },
        "predictions_committed_before_access": True,
        "refit_after_access": False,
        "hyperparameter_change_after_access": False,
    }
    audit_stamp = accessed_at.replace(":", "").replace("-", "")
    label_access_path = output_dir.with_name(
        output_dir.name + f".label-access-{audit_stamp}.json"
    )
    _atomic_json(label_access_path, label_access)
    os.chmod(label_access_path, 0o444)
    bundle = unsealed.bundle
    query_ids = tuple(query.query_id for query in bundle.queries)
    corpus_config = _mapping(config.get("corpus"), "corpus")
    profiles_path = _resolve(config_path, corpus_config.get("path"))
    corpus = load_jsonl_corpus(
        profiles_path,
        id_field=str(corpus_config.get("id_field") or "venue_id"),
        text_fields=tuple(corpus_config.get("text_fields") or ("name",)),
        snapshot_field=str(corpus_config.get("snapshot_field") or "snapshot_date"),
    )
    candidate_ids = tuple(document.doc_id for document in corpus)
    gold_ids = {
        venue_id
        for relevance in bundle.qrels.values()
        for venue_id, gain in relevance.items()
        if gain > 0
    }
    if not gold_ids <= set(candidate_ids):
        raise ResearchDataError("sealed labels contain out-of-candidate gold venues")
    expected_binding = build_run_binding(
        dataset_path=blind_path,
        profiles_path=profiles_path,
        query_ids=query_ids,
        candidate_ids=candidate_ids,
        configuration=config,
        configuration_path=config_path,
    )

    raw_methods = config.get("methods")
    if not isinstance(raw_methods, list) or len(raw_methods) < 2:
        raise ResearchDataError("sealed evaluation requires at least two methods")
    method_order: list[str] = []
    runs: dict[str, Run] = {}
    method_records: dict[str, Any] = {}
    for index, raw in enumerate(raw_methods):
        method_config = _mapping(raw, f"methods[{index}]")
        name = str(method_config.get("name") or "").strip()
        if not name or name in runs:
            raise ResearchDataError("sealed evaluation method names must be unique")
        run_path = _resolve(config_path, method_config.get("path"))
        manifest_path = _resolve(config_path, method_config.get("manifest_path"))
        run_hash = str(method_config.get("run_sha256") or "")
        manifest_hash = str(method_config.get("manifest_sha256") or "")
        if (
            sha256_file(run_path) != run_hash
            or sha256_file(manifest_path) != manifest_hash
            or (run_hash, manifest_hash) not in committed_pairs
        ):
            raise ResearchDataError(
                f"method {name!r} was not part of the pre-label commitment"
            )
        sidecar = _read_object(manifest_path, f"{name} run manifest")
        binding = _mapping(sidecar.get("binding"), f"{name} binding")
        generation_config = _mapping(
            binding.get("configuration"), f"{name} generation configuration"
        )
        runs[name] = load_score_run(
            run_path,
            expected_query_ids=query_ids,
            candidate_ids=candidate_ids,
            expected_binding=expected_binding,
            expected_manifest_sha256=manifest_hash,
            expected_configuration_sha256=str(
                generation_config.get("canonical_sha256") or ""
            ),
            expected_method_identity=_method_identity(sidecar),
            manifest_path=manifest_path,
        )
        method_order.append(name)
        method_records[name] = {
            "run": _artifact(run_path),
            "manifest": _artifact(manifest_path),
            "method": sidecar["method"],
            "execution": sidecar.get("execution"),
        }

    evaluation_config = _mapping(config.get("evaluation"), "evaluation")
    cutoffs = tuple(int(value) for value in evaluation_config.get("cutoffs", (1, 3, 5, 10, 20, 50)))
    evaluations = {
        name: evaluate_run(runs[name], bundle.qrels, query_ids=query_ids, ks=cutoffs)
        for name in method_order
    }
    queries_by_id = {query.query_id: query for query in bundle.queries}
    strata = build_query_strata(
        query_ids=query_ids,
        qrels=bundle.qrels,
        queries=queries_by_id,
        corpus=corpus,
    )
    strata_summary = summarize_strata(strata, query_count=len(query_ids))
    method_strata = {
        name: {
            dimension: stratified_metrics(evaluations[name], assignments)
            for dimension, assignments in strata.items()
        }
        for name in method_order
    }
    statistics_config = _mapping(config.get("statistics"), "statistics")
    metric = str(statistics_config.get("metric") or "ndcg@10")
    bootstrap_iterations = int(statistics_config.get("bootstrap_iterations", 2000))
    permutation_iterations = int(statistics_config.get("permutation_iterations", 2000))
    confidence = float(statistics_config.get("confidence", 0.95))
    seed = int(statistics_config.get("seed", 20260828))
    if statistics_config.get("comparison_family") != "all_methods_unordered_pairs":
        raise ResearchDataError("sealed comparison family must include every method pair")
    comparisons_payload: dict[str, Any] = {}
    raw_p: dict[str, float] = {}
    for pair_index, (left, right) in enumerate(combinations(method_order, 2)):
        identity = left + "__vs__" + right
        bootstrap = paired_bootstrap_ci(
            evaluations[left]["per_query"],
            evaluations[right]["per_query"],
            metric=metric,
            iterations=bootstrap_iterations,
            confidence=confidence,
            seed=seed + pair_index,
        )
        permutation = paired_permutation_test(
            evaluations[left]["per_query"],
            evaluations[right]["per_query"],
            metric=metric,
            iterations=permutation_iterations,
            seed=seed + pair_index,
        )
        comparisons_payload[identity] = {
            "left": left,
            "right": right,
            "bootstrap": bootstrap,
            "permutation": permutation,
        }
        raw_p[identity] = float(permutation["two_sided_p_value"])
    corrections = adjust_p_values(raw_p)
    for identity, adjusted in corrections.items():
        comparisons_payload[identity]["multiple_comparison_correction"] = adjusted

    split = TemporalSplit(train=(), validation=(), test=query_ids, excluded=())
    leakage = audit_leakage(
        bundle,
        corpus,
        split,
        evaluation_splits=("test",),
        corpus_views=("document", "prototypes"),
    )
    staging = output_dir.with_name(
        "." + output_dir.name + ".building-" + uuid.uuid4().hex[:12]
    )
    staging.mkdir()
    try:
        leakage_path = staging / "leakage_audit.json"
        _atomic_json(leakage_path, leakage)
        if not leakage["passed"]:
            raise ResearchDataError(
                "critical sealed-test leakage found; failed label-access audit is preserved"
            )
        metrics = {
            "schema_version": 1,
            "artifact_type": "sealed_retrieval_metrics",
            "status": "complete_full_denominator",
            "primary_metric": metric,
            "query_count": len(query_ids),
            "candidate_count": len(candidate_ids),
            "method_order": method_order,
            "methods": {
                name: {
                    "aggregate": evaluations[name]["aggregate"],
                    "per_query": evaluations[name]["per_query"],
                    "stratified": method_strata[name],
                }
                for name in method_order
            },
            "strata": strata_summary,
            "paired_comparisons": comparisons_payload,
            "statistics": {
                "metric": metric,
                "bootstrap_iterations": bootstrap_iterations,
                "permutation_iterations": permutation_iterations,
                "confidence": confidence,
                "seed": seed,
                "comparison_family": "all_methods_unordered_pairs",
                "pair_count": len(comparisons_payload),
                "corrections": ["Holm family-wise", "Benjamini-Hochberg FDR"],
            },
        }
        metrics_path = staging / "metrics.json"
        _atomic_json(metrics_path, metrics)
        manifest = {
            "schema_version": 1,
            "artifact_type": "future_sealed_test_evaluation",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "complete_sealed_test_evaluation",
            "config": _artifact(config_path),
            "sealed_test_manifest": _artifact(sealed_manifest_path),
            "prediction_commitment": _artifact(commitment_path),
            "label_access_audit": _artifact(label_access_path),
            "leakage_audit": _artifact(
                leakage_path,
                published_path=output_dir / leakage_path.name,
            ),
            "metrics": _artifact(
                metrics_path,
                published_path=output_dir / metrics_path.name,
            ),
            "methods": method_records,
            "coverage": {
                "query_count": len(query_ids),
                "full_denominator_retained": True,
                "failed_query_count": 0,
                "method_count": len(method_order),
                "paired_comparison_count": len(comparisons_payload),
                "critical_leakage_count": int(
                    leakage["severity_counts"].get("critical", 0)
                ),
            },
            "generation": {
                "command": [str(value) for value in generation_command],
                "working_directory": str(Path.cwd().resolve()),
            },
            "claim_boundary": (
                "The artifact reports every frozen method and comparison. Scientific "
                "claims must follow the observed corrected statistics, including null "
                "and negative results."
            ),
        }
        manifest_path = staging / "manifest.json"
        _atomic_json(manifest_path, manifest)
        os.replace(staging, output_dir)
        return {
            **manifest,
            "manifest": _artifact(output_dir / "manifest.json"),
        }
    except Exception:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        failed = output_dir.with_name(
            output_dir.name + f".failed-{timestamp}-{uuid.uuid4().hex[:8]}"
        )
        if staging.exists():
            os.replace(staging, failed)
        raise


__all__ = [
    "UnsealedLabels",
    "evaluate_sealed_test",
    "unseal_labels_after_prediction_commitment",
]
