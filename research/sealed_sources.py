"""Label-blind reference binding and lexical source runs for future tests."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from time import perf_counter
from typing import Any, Mapping, Sequence

from .baselines import BM25Baseline, TfidfBaseline
from .data import (
    DatasetBundle,
    ResearchDataError,
    build_run_binding,
    canonical_json_sha256,
    load_jsonl_corpus,
    runtime_provenance,
    sha256_file,
    write_run,
)
from .prototype_vectors import validate_reference_binding


def _atomic_json(path: Path, payload: Any) -> None:
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
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


def _preflight(path: Path) -> None:
    if path.exists():
        raise ResearchDataError(f"refusing to overwrite sealed artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)


def build_sealed_reference_binding(
    *,
    bundle: DatasetBundle,
    dataset_path: Path,
    profiles_path: Path,
    output_path: Path,
    profile_cutoff: str,
    generation_command: Sequence[str],
) -> dict[str, Any]:
    """Freeze the future-query order and unchanged candidate universe."""

    _preflight(output_path)
    if bundle.qrels:
        raise ResearchDataError("sealed reference binding refuses a labeled query bundle")
    corpus = load_jsonl_corpus(
        profiles_path,
        id_field="venue_id",
        text_fields=("name",),
        snapshot_field="snapshot_date",
    )
    if any(document.snapshot_date > profile_cutoff for document in corpus):
        raise ResearchDataError("sealed reference contains a post-cutoff profile")
    query_ids = tuple(query.query_id for query in bundle.queries)
    candidate_ids = tuple(document.doc_id for document in corpus)
    generation_config = {
        "builder": "sealed-query-candidate-binding-v1",
        "label_blind": True,
        "profile_cutoff": profile_cutoff,
        "query_fields": ["title", "abstract"],
    }
    binding = build_run_binding(
        dataset_path=dataset_path,
        profiles_path=profiles_path,
        query_ids=query_ids,
        candidate_ids=candidate_ids,
        configuration=generation_config,
    )
    manifest = {
        "schema_version": 1,
        "artifact_type": "sealed_query_candidate_binding",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "frozen_before_prediction",
        "binding": binding,
        "runtime": runtime_provenance(),
        "generation": {
            "command": [str(value) for value in generation_command],
            "working_directory": str(Path.cwd().resolve()),
        },
        "label_boundary": {
            "qrels_present": False,
            "query_input": "physically label-free closed schema",
            "labels_accessed": False,
        },
        "execution": {
            "offline_only": True,
            "search_free": True,
            "external_api_calls": 0,
            "estimated_external_cost_usd": 0.0,
        },
    }
    _atomic_json(output_path, manifest)
    return {**manifest, "manifest": _artifact(output_path)}


def build_sealed_lexical_run(
    *,
    bundle: DatasetBundle,
    dataset_path: Path,
    profiles_path: Path,
    reference_manifest_path: Path,
    output_path: Path,
    method_name: str,
    method_type: str,
    top_k: int,
    k1: float = 1.2,
    b: float = 0.75,
    sublinear_tf: bool = True,
    use_prototypes: bool = True,
    generation_command: Sequence[str],
) -> dict[str, Any]:
    """Build a BM25/TF-IDF run from blind queries without an evaluator."""

    _preflight(output_path)
    if bundle.qrels:
        raise ResearchDataError("sealed lexical builder refuses a labeled query bundle")
    if method_type not in {"bm25", "tfidf"}:
        raise ResearchDataError("sealed lexical method_type must be bm25 or tfidf")
    corpus = load_jsonl_corpus(
        profiles_path,
        id_field="venue_id",
        text_fields=("name",),
        snapshot_field="snapshot_date",
    )
    query_ids = tuple(query.query_id for query in bundle.queries)
    candidate_ids = tuple(document.doc_id for document in corpus)
    generation_config = {
        "builder": "sealed-lexical-score-run-v1",
        "method_name": method_name,
        "method_type": method_type,
        "query_fields": ["title", "abstract"],
        "top_k": top_k,
        "use_prototypes": use_prototypes,
        **(
            {"k1": k1, "b": b}
            if method_type == "bm25"
            else {"sublinear_tf": sublinear_tf}
        ),
    }
    binding = build_run_binding(
        dataset_path=dataset_path,
        profiles_path=profiles_path,
        query_ids=query_ids,
        candidate_ids=candidate_ids,
        configuration=generation_config,
    )
    reference = validate_reference_binding(reference_manifest_path, binding)
    retriever = (
        BM25Baseline(
            name=method_name,
            k1=k1,
            b=b,
            use_prototypes=use_prototypes,
        )
        if method_type == "bm25"
        else TfidfBaseline(
            name=method_name,
            sublinear_tf=sublinear_tf,
            use_prototypes=use_prototypes,
        )
    )
    started = perf_counter()
    run = retriever.fit(corpus).run(bundle.queries, top_k=top_k)
    elapsed_ms = (perf_counter() - started) * 1000.0
    implementation_revision = "sealed-lexical-score-run-v1@" + sha256_file(
        Path(__file__)
    )
    return write_run(
        output_path,
        run,
        binding=binding,
        query_ids=query_ids,
        candidate_ids=candidate_ids,
        top_k=top_k,
        method={
            "name": method_name,
            "kind": method_type,
            "implementation": "research.sealed_sources.build_sealed_lexical_run",
            "implementation_revision": implementation_revision,
            "configuration_sha256": canonical_json_sha256(generation_config),
        },
        command=generation_command,
        working_directory=Path.cwd(),
        runtime=runtime_provenance(),
        additional_manifest_fields={
            "reference_binding_manifest": reference,
            "label_boundary": {
                "qrels_present": False,
                "labels_accessed": False,
            },
            "execution": {
                "total_ms": elapsed_ms,
                "mean_ms_per_query": elapsed_ms / len(query_ids),
                "external_api_calls": 0,
                "estimated_external_cost_usd": 0.0,
                "offline_only": True,
                "search_free": True,
                "failed_query_count": 0,
                "empty_ranking_count": sum(not ranking for ranking in run.values()),
            },
        },
    )


__all__ = ["build_sealed_lexical_run", "build_sealed_reference_binding"]
