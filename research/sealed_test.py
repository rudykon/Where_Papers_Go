"""Future-test acquisition, physical label sealing, and freeze verification.

This module is intentionally separate from evaluation.  Acquisition may write
gold labels into a permission-restricted vault, but every pre-commit inference
command consumes only ``queries.blind.jsonl``.  Labels are not parsed by the
prediction path and may only be joined after immutable run hashes are recorded.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping, Sequence
import uuid

from scripts.build_recent_journal_benchmark import (
    BROAD_FIELDS,
    QUARTILES,
    build_benchmark,
    build_parser as build_crossref_parser,
    plan_benchmark,
)

from .data import (
    BLIND_QUERY_ALLOWED_FIELDS,
    ResearchDataError,
    canonical_json_sha256,
    ordered_ids_sha256,
    parse_iso_date,
    sha256_file,
)


_LABEL_FIELDS = (
    "broad_field",
    "doi",
    "gold_container_title",
    "gold_entity_id",
    "gold_issns",
    "gold_jcr_category",
    "gold_jcr_quartile",
    "gold_journal_id",
    "gold_journal_name",
    "paper_id",
    "source",
    "source_url",
)
_BLIND_COPY_FIELDS = (
    "abstract",
    "article_type",
    "language",
    "paper_id",
    "publication_date",
    "publication_date_precision",
    "title",
)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchDataError(f"{label} must be an object")
    return value


def _resolve(config_path: Path, value: Any) -> Path:
    path = Path(str(value or ""))
    if not str(path):
        raise ResearchDataError("sealed-test configuration contains an empty path")
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


def _artifact(path: Path, *, published_path: Path | None = None) -> dict[str, Any]:
    return {
        "path": str((published_path or path).resolve()),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _git_object_exists(commit: str) -> bool:
    try:
        subprocess.run(
            ("git", "cat-file", "-e", commit + "^{commit}"),
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


def verify_method_freeze(
    config_path: Path, freeze: Mapping[str, Any]
) -> dict[str, Any]:
    """Verify exact pre-acquisition commits, configs, models, and artifacts."""

    if freeze.get("status") != "frozen_before_future_data_acquisition":
        raise ResearchDataError("method freeze has not been declared before acquisition")
    commits = freeze.get("commits")
    if not isinstance(commits, Mapping) or not commits:
        raise ResearchDataError("method freeze must bind at least one commit")
    verified_commits: dict[str, str] = {}
    for name, value in commits.items():
        commit = str(value or "").strip()
        if len(commit) != 40 or not _git_object_exists(commit):
            raise ResearchDataError(f"method freeze commit is missing: {name}")
        verified_commits[str(name)] = commit
    raw_artifacts = freeze.get("artifacts")
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        raise ResearchDataError("method freeze must bind artifact hashes")
    verified_artifacts: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_artifacts):
        item = _mapping(raw, f"method_freeze.artifacts[{index}]")
        path = _resolve(config_path, item.get("path"))
        expected = str(item.get("sha256") or "").strip()
        if len(expected) != 64 or not path.is_file() or sha256_file(path) != expected:
            raise ResearchDataError(f"method freeze artifact mismatch: {path}")
        verified_artifacts.append({"name": str(item.get("name") or path.name), **_artifact(path)})
    metrics = _mapping(freeze.get("metrics"), "method_freeze.metrics")
    statistics = _mapping(freeze.get("statistics"), "method_freeze.statistics")
    candidates = _mapping(freeze.get("candidates"), "method_freeze.candidates")
    method_hyperparameters = _mapping(
        freeze.get("method_hyperparameters"),
        "method_freeze.method_hyperparameters",
    )
    expected_method_hash = str(
        freeze.get("method_hyperparameters_canonical_sha256") or ""
    )
    actual_method_hash = canonical_json_sha256(method_hyperparameters)
    if len(expected_method_hash) != 64 or actual_method_hash != expected_method_hash:
        raise ResearchDataError("sealed-test method hyperparameters are not frozen")
    method_family = freeze.get("method_family")
    if (
        not isinstance(method_family, list)
        or not method_family
        or any(not str(value).strip() for value in method_family)
        or len({str(value) for value in method_family}) != len(method_family)
    ):
        raise ResearchDataError("sealed-test method family is empty or duplicated")
    source_protocol = _mapping(
        freeze.get("source_protocol"), "method_freeze.source_protocol"
    )
    if not source_protocol:
        raise ResearchDataError("sealed-test source protocol is not frozen")
    if int(candidates.get("count", 0)) != 20087:
        raise ResearchDataError("sealed-test freeze must retain 20,087 candidates")
    fingerprint = str(candidates.get("ordered_ids_sha256") or "")
    if len(fingerprint) != 64:
        raise ResearchDataError("sealed-test candidate fingerprint is invalid")
    if metrics.get("primary") != "ndcg@10":
        raise ResearchDataError("sealed-test primary metric must be frozen as ndcg@10")
    if statistics.get("comparison_family") != "all_methods_unordered_pairs":
        raise ResearchDataError("sealed-test comparison family is not frozen")
    return {
        "status": str(freeze["status"]),
        "commits": verified_commits,
        "artifacts": verified_artifacts,
        "candidates": dict(candidates),
        "method_family": [str(value) for value in method_family],
        "method_hyperparameters_sha256": actual_method_hash,
        "source_protocol_sha256": canonical_json_sha256(source_protocol),
        "metrics_sha256": canonical_json_sha256(metrics),
        "statistics_sha256": canonical_json_sha256(statistics),
    }


def split_labeled_dataset(
    source_path: Path,
    *,
    blind_path: Path,
    labels_path: Path,
    development_cutoff: str,
    window_start: str,
    window_end: str,
) -> dict[str, Any]:
    """Split a builder result without printing or returning any label value."""

    cutoff = parse_iso_date(development_cutoff, field_name="development cutoff")
    start = parse_iso_date(window_start, field_name="sealed window start")
    end = parse_iso_date(window_end, field_name="sealed window end")
    if not cutoff < start <= end:
        raise ResearchDataError("sealed window must be strictly after development cutoff")
    blind_rows: list[dict[str, Any]] = []
    label_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    ordered: list[tuple[str, str]] = []
    with source_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ResearchDataError(
                    f"{source_path}:{line_number}: invalid labeled JSON"
                ) from exc
            if not isinstance(row, Mapping):
                raise ResearchDataError(f"{source_path}:{line_number}: expected an object")
            missing = [field for field in (*_BLIND_COPY_FIELDS, *_LABEL_FIELDS) if field not in row]
            if missing:
                raise ResearchDataError(
                    f"{source_path}:{line_number}: missing sealed fields: {missing}"
                )
            query_id = str(row["paper_id"] or "").strip()
            if not query_id or query_id in seen:
                raise ResearchDataError(
                    f"{source_path}:{line_number}: empty or duplicate paper_id"
                )
            published = parse_iso_date(
                row["publication_date"], field_name="publication date"
            )
            if not start <= published <= end:
                raise ResearchDataError(
                    f"{source_path}:{line_number}: paper outside sealed window"
                )
            if not str(row.get("gold_journal_id") or "").strip():
                raise ResearchDataError(
                    f"{source_path}:{line_number}: missing sealed relevance label"
                )
            blind = {field: row[field] for field in _BLIND_COPY_FIELDS}
            blind["user_constraints"] = {}
            if set(blind) != BLIND_QUERY_ALLOWED_FIELDS:
                raise ResearchDataError("internal blind query schema mismatch")
            labels = {field: row[field] for field in _LABEL_FIELDS}
            blind_rows.append(blind)
            label_rows.append(labels)
            seen.add(query_id)
            ordered.append((published.isoformat(), query_id))
    if not blind_rows:
        raise ResearchDataError("sealed dataset contains no records")
    _atomic_jsonl(blind_path, blind_rows)
    _atomic_jsonl(labels_path, label_rows)
    os.chmod(labels_path, 0o600)
    model_order = [query_id for _published, query_id in sorted(ordered)]
    return {
        "record_count": len(blind_rows),
        "physical_query_order_sha256": ordered_ids_sha256(
            tuple(str(row["paper_id"]) for row in blind_rows)
        ),
        "model_query_order_sha256": ordered_ids_sha256(tuple(model_order)),
        "blind_queries": _artifact(blind_path),
        "sealed_labels": _artifact(labels_path),
        "label_values_returned_or_printed": False,
        "development_cutoff": cutoff.isoformat(),
        "window": {"from": start.isoformat(), "until": end.isoformat()},
    }


def _load_config(config_path: Path) -> Mapping[str, Any]:
    config = _read_object(config_path.resolve(), "sealed-test configuration")
    if config.get("schema_version") != 1:
        raise ResearchDataError("unsupported sealed-test configuration schema")
    if config.get("offline_scoring_search_free") is not True:
        raise ResearchDataError("sealed-test offline scoring must be Search-free")
    return config


def _crossref_args(
    config_path: Path,
    config: Mapping[str, Any],
    *,
    output_dir: Path,
) -> argparse.Namespace:
    acquisition = _mapping(config.get("acquisition"), "acquisition")
    argv = [
        "--data-dir",
        str(_resolve(config_path, acquisition.get("data_dir"))),
        "--output-dir",
        str(output_dir),
        "--cache-dir",
        str(output_dir / "crossref_cache"),
        "--from-date",
        str(acquisition.get("from_date") or ""),
        "--until-date",
        str(acquisition.get("until_date") or ""),
        "--fields",
        ",".join(str(value) for value in acquisition.get("fields", BROAD_FIELDS)),
        "--quartiles",
        ",".join(str(value) for value in acquisition.get("quartiles", QUARTILES)),
        "--sample-size",
        str(int(acquisition.get("sample_size", 300))),
        "--max-papers-per-journal",
        str(int(acquisition.get("max_papers_per_journal", 1))),
        "--journal-attempt-multiplier",
        str(int(acquisition.get("journal_attempt_multiplier", 3))),
        "--journal-workers",
        str(int(acquisition.get("journal_workers", 8))),
        "--bulk-pages",
        str(int(acquisition.get("bulk_pages", 8))),
        "--bulk-rows",
        str(int(acquisition.get("bulk_rows", 1000))),
        "--rows-per-journal",
        str(int(acquisition.get("rows_per_journal", 20))),
        "--min-abstract-chars",
        str(int(acquisition.get("min_abstract_chars", 300))),
        "--seed",
        str(acquisition.get("seed") or "where-papers-go-future-sealed-v1"),
        "--mailto",
        str(acquisition.get("mailto") or "rudykon@users.noreply.github.com"),
        "--timeout",
        str(float(acquisition.get("timeout", 30.0))),
        "--retries",
        str(int(acquisition.get("retries", 4))),
        "--request-interval",
        str(float(acquisition.get("request_interval", 0.12))),
        "--max-network-requests",
        str(int(acquisition.get("max_network_requests", 1000))),
    ]
    return build_crossref_parser().parse_args(argv)


def plan_sealed_test(config_path: Path) -> dict[str, Any]:
    config_path = config_path.resolve()
    config = _load_config(config_path)
    freeze = verify_method_freeze(
        config_path, _mapping(config.get("method_freeze"), "method_freeze")
    )
    output_dir = _resolve(config_path, config.get("output_dir"))
    args = _crossref_args(config_path, config, output_dir=output_dir)
    plan = plan_benchmark(args)
    acquisition = _mapping(config.get("acquisition"), "acquisition")
    cutoff = parse_iso_date(
        config.get("development_cutoff"), field_name="development cutoff"
    )
    start = parse_iso_date(acquisition.get("from_date"), field_name="sealed window start")
    end = parse_iso_date(acquisition.get("until_date"), field_name="sealed window end")
    if not cutoff < start <= end < date.today():
        raise ResearchDataError(
            "sealed window must be closed, strictly after the development cutoff, "
            "and before the current date"
        )
    return {
        "schema_version": 1,
        "artifact_type": "future_sealed_test_plan",
        "network_performed": False,
        "config": _artifact(config_path),
        "method_freeze": freeze,
        "crossref": plan,
        "authorization": {
            "required_before_build": True,
            "required_http_attempt_cap": args.max_network_requests,
            "reference": str(acquisition.get("authorization_reference") or ""),
        },
        "claim_boundary": (
            "A plan is not a sealed dataset, prediction, evaluation, or method result."
        ),
    }


def build_sealed_test(
    config_path: Path,
    *,
    generation_command: Sequence[str],
) -> dict[str, Any]:
    """Acquire into a shadow directory and atomically publish a label vault."""

    config_path = config_path.resolve()
    config = _load_config(config_path)
    plan = plan_sealed_test(config_path)
    acquisition = _mapping(config.get("acquisition"), "acquisition")
    authorization = str(acquisition.get("authorization_reference") or "").strip()
    if not authorization:
        raise ResearchDataError(
            "bounded Crossref acquisition requires an explicit authorization reference"
        )
    output_dir = _resolve(config_path, config.get("output_dir"))
    if output_dir.exists():
        raise ResearchDataError(
            f"sealed-test output already exists and will not be overwritten: {output_dir}"
        )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = output_dir.with_name(
        "." + output_dir.name + ".building-" + uuid.uuid4().hex[:12]
    )
    staging.mkdir()
    try:
        _atomic_json(staging / "acquisition_plan.json", plan)
        args = _crossref_args(config_path, config, output_dir=staging)
        crossref_manifest = build_benchmark(args)
        if not crossref_manifest.get("dataset", {}).get("complete"):
            raise ResearchDataError("future dataset is incomplete; denominator not reduced")
        source_vault = staging / "dataset.jsonl"
        os.chmod(source_vault, 0o600)
        acquisition_manifest_path = staging / "crossref_acquisition_manifest.json"
        (staging / "manifest.json").replace(acquisition_manifest_path)
        blind_path = staging / "queries.blind.jsonl"
        labels_path = staging / "labels.sealed.jsonl"
        split = split_labeled_dataset(
            source_vault,
            blind_path=blind_path,
            labels_path=labels_path,
            development_cutoff=str(config.get("development_cutoff") or ""),
            window_start=str(acquisition.get("from_date") or ""),
            window_end=str(acquisition.get("until_date") or ""),
        )
        expected_count = int(acquisition.get("sample_size", 300))
        if split["record_count"] != expected_count:
            raise ResearchDataError(
                f"sealed denominator mismatch: {split['record_count']} != {expected_count}"
            )
        split["blind_queries"] = _artifact(
            blind_path, published_path=output_dir / blind_path.name
        )
        split["sealed_labels"] = _artifact(
            labels_path, published_path=output_dir / labels_path.name
        )
        manifest = {
            "schema_version": 1,
            "artifact_type": "future_sealed_test",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "labels_sealed_predictions_pending",
            "config": _artifact(config_path),
            "method_freeze": plan["method_freeze"],
            "temporal_boundary": {
                "development_cutoff": str(config.get("development_cutoff")),
                "future_window": split["window"],
                "window_closed_before_acquisition": True,
            },
            "acquisition": {
                "provider": "Crossref REST API",
                "authorization_reference": authorization,
                "plan": _artifact(
                    staging / "acquisition_plan.json",
                    published_path=output_dir / "acquisition_plan.json",
                ),
                "manifest": _artifact(
                    acquisition_manifest_path,
                    published_path=output_dir / acquisition_manifest_path.name,
                ),
                "network_requests": crossref_manifest["source"]["network_requests"],
                "cache_hits": crossref_manifest["source"]["cache_hits"],
                "request_budget": args.max_network_requests,
                "estimated_external_cost_usd": 0.0,
                "search_calls": 0,
                "llm_calls": 0,
                "embedding_calls": 0,
            },
            "dataset": {
                **split,
                "source_label_vault": _artifact(
                    source_vault, published_path=output_dir / source_vault.name
                ),
                "crossref_abstract_redistribution_restricted": True,
            },
            "label_boundary": {
                "prediction_input": blind_path.name,
                "prediction_forbidden_input": labels_path.name,
                "blind_allowed_fields": sorted(BLIND_QUERY_ALLOWED_FIELDS),
                "label_fields": list(_LABEL_FIELDS),
                "labels_parsed_by_prediction_path": False,
                "prediction_commitment_required_before_evaluation": True,
            },
            "generation": {
                "command": [str(value) for value in generation_command],
                "working_directory": str(Path.cwd().resolve()),
            },
            "claim_boundary": (
                "Labels exist only in the restricted vault. No prediction or sealed "
                "metric has been produced at this state."
            ),
        }
        _atomic_json(staging / "manifest.json", manifest)
        os.chmod(staging / "manifest.json", 0o444)
        os.replace(staging, output_dir)
        final_manifest = output_dir / "manifest.json"
        return {**manifest, "manifest": _artifact(final_manifest)}
    except Exception:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        failed = output_dir.with_name(
            output_dir.name + f".failed-{timestamp}-{uuid.uuid4().hex[:8]}"
        )
        if staging.exists():
            os.replace(staging, failed)
        raise


__all__ = [
    "build_sealed_test",
    "plan_sealed_test",
    "split_labeled_dataset",
    "verify_method_freeze",
]
