"""Three-expert blinded review packages, progress audit, and immutable export."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
from typing import Any, Iterable, Mapping, Sequence
import uuid

from .data import (
    ResearchDataError,
    build_run_binding,
    load_blind_query_dataset,
    load_jsonl_corpus,
    load_score_run,
    sha256_file,
)


RATING_FIELDS: Mapping[str, tuple[Any, ...]] = {
    "relevance": (0, 1, 2, 3),
    "submission_fit": (0, 1, 2, 3),
    "constraint_violation": ("none", "minor", "major", "unclear"),
    "explanation_quality": (0, 1, 2, 3, "not_available"),
}


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchDataError(f"{label} must be an object")
    return value


def _resolve(config_path: Path, value: Any) -> Path:
    path = Path(str(value or ""))
    if not str(path):
        raise ResearchDataError("expert-review configuration contains an empty path")
    return path if path.is_absolute() else (config_path.parent / path).resolve()


def _read_object(path: Path, label: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResearchDataError(f"cannot read {label}: {path}") from exc
    return _mapping(payload, label)


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _atomic_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _stable(seed: str, *parts: str) -> str:
    return hashlib.sha256("\x1f".join((seed, *parts)).encode("utf-8")).hexdigest()


def _audit_public_blinding(value: Any, *, path: str = "public") -> None:
    forbidden = {"appearances", "method", "methods", "original_rank", "rank", "score"}
    if isinstance(value, Mapping):
        leaking = sorted(forbidden & {str(key).casefold() for key in value})
        if leaking:
            raise ResearchDataError(
                f"expert public payload leaks hidden ranking fields at {path}: {leaking}"
            )
        for key, nested in value.items():
            _audit_public_blinding(nested, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _audit_public_blinding(nested, path=f"{path}[{index}]")


def _committed_pairs(commitment: Mapping[str, Any]) -> set[tuple[str, str]]:
    output: set[tuple[str, str]] = set()
    for section_name in ("sources", "variants"):
        section = _mapping(commitment.get(section_name), section_name)
        for raw in section.values():
            record = _mapping(raw, f"{section_name}[]")
            run = _mapping(record.get("run"), "committed run")
            manifest = _mapping(record.get("manifest"), "committed manifest")
            output.add((str(run.get("sha256") or ""), str(manifest.get("sha256") or "")))
    return output


def _method_identity(sidecar: Mapping[str, Any]) -> dict[str, str]:
    method = _mapping(sidecar.get("method"), "method")
    identity = {
        key: str(method[key])
        for key in ("model_revision", "provider_fingerprint", "implementation_revision")
        if str(method.get(key) or "").strip()
    }
    if not identity:
        raise ResearchDataError("expert-review source has no exact method identity")
    return identity


def _read_explanations(path: Path) -> dict[tuple[str, str], Mapping[str, Any]]:
    output: dict[tuple[str, str], Mapping[str, Any]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ResearchDataError(
                    f"{path}:{line_number}: invalid explanation JSON"
                ) from exc
            if not isinstance(row, Mapping):
                raise ResearchDataError(f"{path}:{line_number}: expected an object")
            key = (str(row.get("query_id") or ""), str(row.get("candidate_id") or ""))
            if not all(key) or key in output:
                raise ResearchDataError("duplicate or invalid expert explanation key")
            output[key] = row
    return output


def _public_explanation(
    document: Any, raw: Mapping[str, Any] | None
) -> dict[str, Any]:
    metadata = document.metadata
    provenance = raw.get("profile_provenance") if isinstance(raw, Mapping) else None
    provenance = provenance if isinstance(provenance, Mapping) else {}
    prototypes = provenance.get("prototypes")
    if not isinstance(prototypes, list):
        prototypes = []
    return {
        "available": bool(raw),
        "profile_snapshot_date": str(
            provenance.get("snapshot_date") or document.snapshot_date
        ),
        "profile_level": str(
            provenance.get("profile_level") or metadata.get("profile_level") or "unknown"
        ),
        "evidence_grade": str(
            provenance.get("evidence_grade") or metadata.get("evidence_grade") or "unknown"
        ),
        "history_paper_count": int(
            provenance.get("history_paper_count")
            or metadata.get("history_paper_count")
            or 0
        ),
        "prototype_provenance": [
            {
                "kind": str(item.get("kind") or ""),
                "derived_by": str(item.get("derived_by") or ""),
                "source_count_shown": len(item.get("source_ids") or ()),
                "source_max_date": str(item.get("source_max_date") or ""),
            }
            for item in prototypes[:3]
            if isinstance(item, Mapping)
        ],
        "constraint_checks": (
            dict(raw.get("constraint_checks"))
            if isinstance(raw, Mapping)
            and isinstance(raw.get("constraint_checks"), Mapping)
            else {}
        ),
    }


def build_expert_review_package(
    config_path: Path,
    *,
    generation_command: Sequence[str],
) -> dict[str, Any]:
    """Merge and randomize committed rankings without exposing method or rank."""

    config_path = config_path.resolve()
    config = _read_object(config_path, "expert-review configuration")
    if config.get("schema_version") != 1:
        raise ResearchDataError("unsupported expert-review configuration schema")
    if config.get("status") != "materials_only_human_evaluation_pending":
        raise ResearchDataError("expert review must remain marked human-evaluation pending")
    experts = tuple(str(value) for value in config.get("experts", ()))
    if len(experts) != 3 or len(set(experts)) != 3 or any(not value for value in experts):
        raise ResearchDataError("expert review requires exactly three anonymous expert IDs")
    sample = _mapping(config.get("sample"), "sample")
    query_count = int(sample.get("query_count", 250))
    if not 200 <= query_count <= 300:
        raise ResearchDataError("expert review query_count must be between 200 and 300")
    top_depth = int(sample.get("top_depth_per_method", 10))
    if top_depth != 10:
        raise ResearchDataError(
            "expert review must load Top-10 so both frozen Top-5 and Top-10 unions are retained"
        )
    seed = str(sample.get("seed") or "where-papers-go-expert-review-v1")

    commitment_config = _mapping(config.get("prediction_commitment"), "prediction_commitment")
    commitment_path = _resolve(config_path, commitment_config.get("path"))
    if sha256_file(commitment_path) != str(commitment_config.get("sha256") or ""):
        raise ResearchDataError("expert-review prediction commitment mismatch")
    commitment = _read_object(commitment_path, "prediction commitment")
    if commitment.get("status") != "predictions_committed_before_label_access":
        raise ResearchDataError("expert materials require committed predictions")
    committed_pairs = _committed_pairs(commitment)

    sealed_config = _mapping(config.get("sealed_test"), "sealed_test")
    sealed_manifest_path = _resolve(config_path, sealed_config.get("manifest"))
    if sha256_file(sealed_manifest_path) != str(sealed_config.get("manifest_sha256") or ""):
        raise ResearchDataError("expert-review sealed-test manifest mismatch")
    sealed_manifest = _read_object(sealed_manifest_path, "sealed-test manifest")
    dataset = _mapping(sealed_manifest.get("dataset"), "sealed dataset")
    blind_record = _mapping(dataset.get("blind_queries"), "blind queries")
    blind_path = Path(str(blind_record.get("path") or ""))
    if sha256_file(blind_path) != str(blind_record.get("sha256") or ""):
        raise ResearchDataError("expert-review blind queries changed")
    bundle = load_blind_query_dataset(blind_path)
    if query_count > len(bundle.queries):
        raise ResearchDataError("expert sample exceeds the sealed denominator")
    selected_queries = tuple(
        sorted(
            bundle.queries,
            key=lambda query: (_stable(seed, "query", query.query_id), query.query_id),
        )[:query_count]
    )
    selected_ids = {query.query_id for query in selected_queries}

    corpus_config = _mapping(config.get("corpus"), "corpus")
    profiles_path = _resolve(config_path, corpus_config.get("path"))
    corpus = load_jsonl_corpus(
        profiles_path,
        id_field=str(corpus_config.get("id_field") or "venue_id"),
        text_fields=tuple(corpus_config.get("text_fields") or ("name",)),
        snapshot_field=str(corpus_config.get("snapshot_field") or "snapshot_date"),
    )
    documents = {document.doc_id: document for document in corpus}
    query_ids = tuple(query.query_id for query in bundle.queries)
    candidate_ids = tuple(document.doc_id for document in corpus)
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
        raise ResearchDataError("expert review requires at least two committed methods")
    runs = {}
    method_artifacts = {}
    for index, raw in enumerate(raw_methods):
        method = _mapping(raw, f"methods[{index}]")
        name = str(method.get("name") or "").strip()
        if not name or name in runs:
            raise ResearchDataError("expert-review method names must be unique")
        run_path = _resolve(config_path, method.get("path"))
        manifest_path = _resolve(config_path, method.get("manifest_path"))
        run_hash = str(method.get("run_sha256") or "")
        manifest_hash = str(method.get("manifest_sha256") or "")
        if (
            sha256_file(run_path) != run_hash
            or sha256_file(manifest_path) != manifest_hash
            or (run_hash, manifest_hash) not in committed_pairs
        ):
            raise ResearchDataError("expert-review method was not pre-label committed")
        sidecar = _read_object(manifest_path, f"{name} manifest")
        binding = _mapping(sidecar.get("binding"), f"{name} binding")
        generation = _mapping(binding.get("configuration"), f"{name} config")
        runs[name] = load_score_run(
            run_path,
            expected_query_ids=query_ids,
            candidate_ids=candidate_ids,
            expected_binding=expected_binding,
            expected_manifest_sha256=manifest_hash,
            expected_configuration_sha256=str(generation.get("canonical_sha256") or ""),
            expected_method_identity=_method_identity(sidecar),
            manifest_path=manifest_path,
            top_k=top_depth,
        )
        method_artifacts[name] = {
            "run": _artifact(run_path),
            "manifest": _artifact(manifest_path),
        }

    explanations: dict[tuple[str, str], Mapping[str, Any]] = {}
    explanation_config = config.get("explanations")
    if isinstance(explanation_config, Mapping):
        explanation_path = _resolve(config_path, explanation_config.get("path"))
        expected = str(explanation_config.get("sha256") or "")
        committed_output = _mapping(commitment.get("outputs"), "committed outputs")
        committed_explanations = _mapping(
            committed_output.get("explanations"), "committed explanations"
        )
        if (
            sha256_file(explanation_path) != expected
            or expected != str(committed_explanations.get("sha256") or "")
        ):
            raise ResearchDataError("expert explanation artifact was not committed")
        explanations = _read_explanations(explanation_path)

    public_rows: list[dict[str, Any]] = []
    mapping_rows: list[dict[str, Any]] = []
    query_aliases = {
        query.query_id: f"Q{index:03d}"
        for index, query in enumerate(
            sorted(selected_queries, key=lambda item: (_stable(seed, "alias", item.query_id), item.query_id)),
            1,
        )
    }
    for query in selected_queries:
        appearances: dict[str, list[dict[str, Any]]] = {}
        for method_name, run in runs.items():
            for rank, item in enumerate(run[query.query_id][:top_depth], 1):
                appearances.setdefault(item.doc_id, []).append(
                    {
                        "method": method_name,
                        "original_rank": rank,
                        "score": item.score,
                        "cutoff_membership": [5, 10] if rank <= 5 else [10],
                    }
                )
        ordered_candidates = sorted(
            appearances,
            key=lambda candidate_id: (
                _stable(seed, "candidate", query.query_id, candidate_id),
                candidate_id,
            ),
        )
        for candidate_index, candidate_id in enumerate(ordered_candidates, 1):
            document = documents[candidate_id]
            review_id = "R-" + _stable(seed, query.query_id, candidate_id)[:20]
            candidate_alias = f"{query_aliases[query.query_id]}-C{candidate_index:02d}"
            raw_explanation = explanations.get((query.query_id, candidate_id))
            public_rows.append(
                {
                    "review_id": review_id,
                    "query_alias": query_aliases[query.query_id],
                    "candidate_alias": candidate_alias,
                    "query": {
                        "title": query.title,
                        "abstract": query.abstract,
                        "article_type": bundle.source_rows[query.query_id].get("article_type"),
                        "language": bundle.source_rows[query.query_id].get("language"),
                        "user_constraints": bundle.source_rows[query.query_id].get("user_constraints", {}),
                    },
                    "candidate": {
                        "name": document.name,
                        "snapshot_date": document.snapshot_date,
                        "jcr_quartile": document.metadata.get("jcr_quartile")
                        or document.metadata.get("level")
                        or "unknown",
                        "broad_field": document.metadata.get("broad_field") or "unknown",
                        "profile_level": document.metadata.get("profile_level") or "unknown",
                        "evidence_grade": document.metadata.get("evidence_grade") or "unknown",
                    },
                    "explanation": _public_explanation(document, raw_explanation),
                }
            )
            mapping_rows.append(
                {
                    "review_id": review_id,
                    "query_id": query.query_id,
                    "candidate_id": candidate_id,
                    "appearances": sorted(
                        appearances[candidate_id],
                        key=lambda item: (str(item["method"]), int(item["original_rank"])),
                    ),
                }
            )
    public_rows.sort(key=lambda row: str(row["review_id"]))
    mapping_rows.sort(key=lambda row: str(row["review_id"]))
    _audit_public_blinding(public_rows)
    output_dir = _resolve(config_path, config.get("output_dir"))
    if output_dir.exists():
        raise ResearchDataError(
            f"expert-review output exists and will not be overwritten: {output_dir}"
        )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = output_dir.with_name(
        "." + output_dir.name + ".building-" + uuid.uuid4().hex[:12]
    )
    staging.mkdir()
    try:
        public_path = staging / "review_items.public.jsonl"
        mapping_path = staging / "method_mapping.sealed.jsonl"
        schema_path = staging / "annotation_schema.json"
        _atomic_jsonl(public_path, public_rows)
        _atomic_jsonl(mapping_path, mapping_rows)
        os.chmod(mapping_path, 0o600)
        _atomic_json(
            schema_path,
            {
                "schema_version": 1,
                "rating_fields": {
                    key: {"allowed_values": list(values)}
                    for key, values in RATING_FIELDS.items()
                },
                "notes": {"type": "string", "maximum_characters": 2000},
                "initial_review_required": True,
                "conflict_review_supported": True,
            },
        )
        assignments = {}
        review_ids = [str(row["review_id"]) for row in public_rows]
        for expert in experts:
            assignment_path = staging / f"assignment.{expert}.json"
            ordered = sorted(
                review_ids,
                key=lambda review_id: (_stable(seed, "expert", expert, review_id), review_id),
            )
            _atomic_json(
                assignment_path,
                {
                    "schema_version": 1,
                    "expert_id": expert,
                    "review_ids": ordered,
                },
            )
            assignments[expert] = _artifact(
                assignment_path,
                published_path=output_dir / assignment_path.name,
            )
        manifest = {
            "schema_version": 1,
            "artifact_type": "three_expert_blind_review_package",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "tools_and_materials_complete_human_evaluation_pending",
            "config_commitment": {
                "sha256": sha256_file(config_path),
                "bytes": config_path.stat().st_size,
            },
            "prediction_commitment": {
                "sha256": sha256_file(commitment_path),
                "bytes": commitment_path.stat().st_size,
            },
            "sealed_test_manifest": _artifact(sealed_manifest_path),
            "method_inputs_sealed": {
                "count": len(method_artifacts),
                "commitment_sha256": hashlib.sha256(
                    json.dumps(
                        method_artifacts,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                "identities_and_ranks_available_only_in": mapping_path.name,
            },
            "sample": {
                "query_count": len(selected_queries),
                "review_item_count": len(public_rows),
                "top_depth_per_method": top_depth,
                "merged_cutoffs": [5, 10],
                "method_count": len(runs),
                "seed_sha256": hashlib.sha256(seed.encode("utf-8")).hexdigest(),
                "query_selection": "deterministic hash order over blind query IDs",
                "candidate_merge": "deduplicated union of every method Top-5 and Top-10",
            },
            "experts": list(experts),
            "outputs": {
                "public_items": _artifact(
                    public_path, published_path=output_dir / public_path.name
                ),
                "sealed_mapping": _artifact(
                    mapping_path, published_path=output_dir / mapping_path.name
                ),
                "annotation_schema": _artifact(
                    schema_path, published_path=output_dir / schema_path.name
                ),
                "assignments": assignments,
            },
            "blinding_audit": {
                "public_method_names_present": False,
                "public_original_ranks_present": False,
                "public_scores_present": False,
                "gold_labels_accessed": False,
                "deterministic_randomization": True,
                "sealed_mapping_mode": "0600",
            },
            "human_dependency": {
                "real_annotations_received": 0,
                "expert_evaluation_complete": False,
                "agreement_available": False,
                "required_next_state": "three real experts complete blinded annotations",
            },
            "generation": {
                "command": [str(value) for value in generation_command],
                "working_directory": str(Path.cwd().resolve()),
            },
        }
        manifest_path = staging / "manifest.json"
        _atomic_json(manifest_path, manifest)
        os.replace(staging, output_dir)
        return {**manifest, "manifest": _artifact(output_dir / "manifest.json")}
    except Exception:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        failed = output_dir.with_name(
            output_dir.name + f".failed-{timestamp}-{uuid.uuid4().hex[:8]}"
        )
        if staging.exists():
            os.replace(staging, failed)
        raise


def _load_public_items(package_dir: Path) -> tuple[Mapping[str, Any], dict[str, Mapping[str, Any]]]:
    manifest = _read_object(package_dir / "manifest.json", "expert package manifest")
    if manifest.get("artifact_type") != "three_expert_blind_review_package":
        raise ResearchDataError("directory is not an expert-review package")
    public_record = _mapping(
        _mapping(manifest.get("outputs"), "outputs").get("public_items"),
        "public items",
    )
    path = Path(str(public_record.get("path") or ""))
    if sha256_file(path) != str(public_record.get("sha256") or ""):
        raise ResearchDataError("expert public items SHA-256 mismatch")
    items: dict[str, Mapping[str, Any]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, Mapping):
                raise ResearchDataError(f"{path}:{line_number}: expected an object")
            review_id = str(row.get("review_id") or "")
            if not review_id or review_id in items:
                raise ResearchDataError("expert public items contain duplicate IDs")
            items[review_id] = row
    return manifest, items


def _validate_annotation(
    annotation: Mapping[str, Any], *, review_ids: set[str]
) -> dict[str, Any]:
    review_id = str(annotation.get("review_id") or "").strip()
    if review_id not in review_ids:
        raise ResearchDataError("annotation has an unknown review_id")
    output: dict[str, Any] = {"review_id": review_id}
    for field, allowed in RATING_FIELDS.items():
        value = annotation.get(field)
        if value not in allowed:
            raise ResearchDataError(f"annotation {field} is outside the frozen scale")
        output[field] = value
    notes = str(annotation.get("notes") or "")
    if len(notes) > 2000:
        raise ResearchDataError("annotation notes exceed 2,000 characters")
    output["notes"] = notes
    return output


class ExpertReviewStore:
    """Atomic snapshots plus an append-only hash-chained audit journal."""

    def __init__(self, package_dir: Path, state_dir: Path, expert_id: str) -> None:
        self.package_dir = package_dir.resolve()
        self.state_dir = state_dir.resolve()
        self.manifest, self.items = _load_public_items(self.package_dir)
        experts = tuple(str(value) for value in self.manifest.get("experts", ()))
        if expert_id not in experts:
            raise ResearchDataError("unknown anonymous expert ID")
        self.expert_id = expert_id
        assignment = _read_object(
            self.package_dir / f"assignment.{expert_id}.json",
            "expert assignment",
        )
        self.review_ids = tuple(str(value) for value in assignment.get("review_ids", ()))
        if set(self.review_ids) != set(self.items):
            raise ResearchDataError("expert assignment does not cover the public package")
        self.expert_dir = self.state_dir / expert_id
        self.expert_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_path = self.expert_dir / "annotations.snapshot.json"
        self.audit_path = self.expert_dir / "annotations.audit.jsonl"

    def snapshot(self) -> dict[str, Mapping[str, Any]]:
        if not self.snapshot_path.exists():
            return {}
        payload = _read_object(self.snapshot_path, "expert annotation snapshot")
        annotations = payload.get("annotations")
        if not isinstance(annotations, Mapping):
            raise ResearchDataError("expert annotation snapshot is invalid")
        return {str(key): _mapping(value, "annotation") for key, value in annotations.items()}

    def save(self, annotation: Mapping[str, Any], *, phase: str = "initial") -> Mapping[str, Any]:
        if phase not in {"initial", "conflict_review"}:
            raise ResearchDataError("annotation phase must be initial or conflict_review")
        validated = _validate_annotation(annotation, review_ids=set(self.review_ids))
        current = self.snapshot()
        previous = current.get(validated["review_id"])
        timestamp = datetime.now(timezone.utc).isoformat()
        event_payload = {
            "expert_id": self.expert_id,
            "phase": phase,
            "review_id": validated["review_id"],
            "timestamp": timestamp,
            "previous_annotation_sha256": (
                hashlib.sha256(
                    json.dumps(previous, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                if previous is not None
                else None
            ),
            "annotation": validated,
        }
        previous_event_hash = "0" * 64
        if self.audit_path.exists():
            with self.audit_path.open("rb") as handle:
                for line in handle:
                    if line.strip():
                        previous_event_hash = str(json.loads(line)["event_sha256"])
        event_payload["previous_event_sha256"] = previous_event_hash
        event_hash = hashlib.sha256(
            json.dumps(
                event_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        event = {**event_payload, "event_sha256": event_hash}
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        current[validated["review_id"]] = {
            **validated,
            "phase": phase,
            "updated_at": timestamp,
            "event_sha256": event_hash,
        }
        _atomic_json(
            self.snapshot_path,
            {
                "schema_version": 1,
                "expert_id": self.expert_id,
                "annotations": current,
            },
        )
        return current[validated["review_id"]]

    def progress(self) -> dict[str, Any]:
        completed = len(self.snapshot())
        total = len(self.review_ids)
        return {
            "expert_id": self.expert_id,
            "completed": completed,
            "total": total,
            "remaining": total - completed,
            "completion_rate": completed / total if total else 1.0,
        }


def fleiss_kappa(
    rows: Sequence[Sequence[Any]], *, categories: Sequence[Any]
) -> float | None:
    """Compute Fleiss' kappa for complete fixed-rater categorical rows."""

    if not rows:
        return None
    category_index = {value: index for index, value in enumerate(categories)}
    if len(category_index) != len(categories):
        raise ResearchDataError("agreement categories must be unique")
    rater_count = len(rows[0])
    if rater_count < 2 or any(len(row) != rater_count for row in rows):
        return None
    counts: list[list[int]] = []
    for row in rows:
        values = [0] * len(categories)
        for rating in row:
            if rating not in category_index:
                raise ResearchDataError("agreement rating is outside the frozen scale")
            values[category_index[rating]] += 1
        counts.append(values)
    observed = sum(
        (sum(value * value for value in row) - rater_count)
        / (rater_count * (rater_count - 1))
        for row in counts
    ) / len(counts)
    category_totals = [sum(row[index] for row in counts) for index in range(len(categories))]
    denominator = len(counts) * rater_count
    expected = sum((total / denominator) ** 2 for total in category_totals)
    if math.isclose(expected, 1.0):
        return 1.0 if math.isclose(observed, 1.0) else None
    return (observed - expected) / (1.0 - expected)


def build_conflict_report(package_dir: Path, state_dir: Path) -> dict[str, Any]:
    manifest, items = _load_public_items(package_dir)
    experts = tuple(str(value) for value in manifest.get("experts", ()))
    snapshots = {
        expert: ExpertReviewStore(package_dir, state_dir, expert).snapshot()
        for expert in experts
    }
    conflicts: list[dict[str, Any]] = []
    complete = 0
    for review_id in sorted(items):
        if not all(review_id in snapshots[expert] for expert in experts):
            continue
        complete += 1
        fields = {
            field: [snapshots[expert][review_id][field] for expert in experts]
            for field in RATING_FIELDS
        }
        disagree = {
            field: values for field, values in fields.items() if len(set(values)) > 1
        }
        if disagree:
            conflicts.append({"review_id": review_id, "disagreements": disagree})
    return {
        "schema_version": 1,
        "artifact_type": "expert_review_conflicts",
        "status": "human_conflict_review_pending" if conflicts else "no_current_conflicts",
        "complete_triplet_count": complete,
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
        "method_sources_exposed": False,
    }


def export_expert_review(
    package_dir: Path,
    state_dir: Path,
    output_dir: Path,
    *,
    generation_command: Sequence[str],
) -> dict[str, Any]:
    """Create a new immutable export; never synthesize or fill missing ratings."""

    if output_dir.exists():
        raise ResearchDataError(f"expert export exists and will not be overwritten: {output_dir}")
    manifest, items = _load_public_items(package_dir)
    experts = tuple(str(value) for value in manifest.get("experts", ()))
    stores = {expert: ExpertReviewStore(package_dir, state_dir, expert) for expert in experts}
    snapshots = {expert: store.snapshot() for expert, store in stores.items()}
    incomplete = {
        expert: len(items) - len(snapshot)
        for expert, snapshot in snapshots.items()
        if len(snapshot) != len(items)
    }
    if incomplete:
        raise ResearchDataError(
            "real expert annotations are incomplete; refusing final export: "
            + ", ".join(f"{expert}={count}" for expert, count in incomplete.items())
        )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = output_dir.with_name(
        "." + output_dir.name + ".building-" + uuid.uuid4().hex[:12]
    )
    staging.mkdir()
    try:
        raw_rows = [
            {
                "review_id": review_id,
                "annotations": {
                    expert: snapshots[expert][review_id] for expert in experts
                },
            }
            for review_id in sorted(items)
        ]
        raw_path = staging / "anonymous_annotations.raw.jsonl"
        _atomic_jsonl(raw_path, raw_rows)
        agreement = {
            field: {
                "statistic": "fleiss_kappa",
                "value": fleiss_kappa(
                    [
                        [snapshots[expert][review_id][field] for expert in experts]
                        for review_id in sorted(items)
                    ],
                    categories=allowed,
                ),
                "item_count": len(items),
                "rater_count": len(experts),
            }
            for field, allowed in RATING_FIELDS.items()
        }
        conflict = build_conflict_report(package_dir, state_dir)
        agreement_path = staging / "agreement.json"
        conflict_path = staging / "conflicts.json"
        _atomic_json(agreement_path, agreement)
        _atomic_json(conflict_path, conflict)
        audit_records = {}
        for expert, store in stores.items():
            target = staging / f"audit.{expert}.jsonl"
            shutil.copyfile(store.audit_path, target)
            audit_records[expert] = _artifact(
                target, published_path=output_dir / target.name
            )
        export_manifest = {
            "schema_version": 1,
            "artifact_type": "immutable_three_expert_review_export",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "human_evaluation_complete",
            "source_package": _artifact(package_dir / "manifest.json"),
            "expert_ids": list(experts),
            "real_annotation_count": len(items) * len(experts),
            "synthetic_annotation_count": 0,
            "outputs": {
                "raw_anonymous_annotations": _artifact(
                    raw_path, published_path=output_dir / raw_path.name
                ),
                "agreement": _artifact(
                    agreement_path, published_path=output_dir / agreement_path.name
                ),
                "conflicts": _artifact(
                    conflict_path, published_path=output_dir / conflict_path.name
                ),
                "audit_journals": audit_records,
            },
            "agreement": agreement,
            "generation": {
                "command": [str(value) for value in generation_command],
                "working_directory": str(Path.cwd().resolve()),
            },
        }
        manifest_path = staging / "manifest.json"
        _atomic_json(manifest_path, export_manifest)
        for path in staging.iterdir():
            if path.is_file():
                os.chmod(path, 0o444)
        os.replace(staging, output_dir)
        return {
            **export_manifest,
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
    "ExpertReviewStore",
    "RATING_FIELDS",
    "build_conflict_report",
    "build_expert_review_package",
    "export_expert_review",
    "fleiss_kappa",
]
