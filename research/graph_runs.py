"""Offline property-graph and LightRAG-mix score-run builders.

The builders in this module never call Search, an LLM, or an embedding API.
They consume only frozen local artifacts and emit the same strict score-run
contract used by every other research baseline.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import json
import math
from pathlib import Path
from statistics import fmean
from time import perf_counter
from typing import Any, Iterable, Mapping, Sequence

from .baselines import BM25Baseline
from .data import (
    DatasetBundle,
    ResearchDataError,
    build_run_binding,
    canonical_json_sha256,
    load_score_run,
    parse_iso_date,
    runtime_provenance,
    sha256_file,
    write_run,
)
from .prototype_vectors import load_prototype_units, validate_reference_binding
from .types import Query, Run, ScoredDocument, VenueDocument


@dataclass(frozen=True)
class GraphPrototype:
    prototype_id: str
    venue_id: str
    text: str
    source_ids: tuple[str, ...]
    weight: float


@dataclass(frozen=True)
class FrozenEdgeGraph:
    """Validated one-hop ``venue -> prototype -> evidence`` graph."""

    candidate_ids: tuple[str, ...]
    prototype_texts: Mapping[str, tuple[str, ...]]
    evidence_texts: Mapping[str, tuple[str, ...]]
    venue_edge_counts: Mapping[str, int]
    audit: Mapping[str, Any]


def _iter_jsonl(path: Path) -> Iterable[tuple[int, Mapping[str, Any]]]:
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise ResearchDataError(f"cannot open graph input: {path}") from exc
    with handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ResearchDataError(
                    f"{path}:{line_number}: invalid graph JSON"
                ) from exc
            if not isinstance(row, Mapping):
                raise ResearchDataError(f"{path}:{line_number}: expected an object")
            yield line_number, row


def _artifact_record(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    return {
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "bytes": resolved.stat().st_size,
    }


def _read_object(path: Path, *, label: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResearchDataError(f"cannot read {label}: {path}") from exc
    if not isinstance(payload, Mapping):
        raise ResearchDataError(f"{label} must be a JSON object: {path}")
    return payload


def _validate_corpus_artifacts(
    *,
    corpus_manifest_path: Path,
    profiles_path: Path,
    prototypes_path: Path,
    evidence_path: Path,
) -> dict[str, Any]:
    manifest = _read_object(corpus_manifest_path, label="corpus manifest")
    outputs = manifest.get("outputs")
    validation = manifest.get("validation")
    if not isinstance(outputs, Mapping) or not isinstance(validation, Mapping):
        raise ResearchDataError("corpus manifest is missing outputs or validation")
    if validation.get("candidate_profile_ids_match") is not True:
        raise ResearchDataError("corpus manifest does not validate candidate/profile IDs")
    if int(validation.get("missing_prototype_source_id_count", -1)) != 0:
        raise ResearchDataError("corpus manifest reports missing prototype sources")
    if int(validation.get("ambiguous_prototype_source_id_count", -1)) != 0:
        raise ResearchDataError("corpus manifest reports ambiguous prototype sources")
    if int(validation.get("research_post_cutoff_evidence_count", -1)) != 0:
        raise ResearchDataError("corpus manifest reports post-cutoff evidence")

    actual = {
        "profiles": _artifact_record(profiles_path),
        "prototypes": _artifact_record(prototypes_path),
        "research_evidence": _artifact_record(evidence_path),
    }
    for key, record in actual.items():
        declared = outputs.get(key)
        if not isinstance(declared, Mapping):
            raise ResearchDataError(f"corpus manifest is missing output {key!r}")
        if declared.get("sha256") != record["sha256"]:
            raise ResearchDataError(f"corpus artifact SHA-256 mismatch for {key!r}")
        if declared.get("bytes") != record["bytes"]:
            raise ResearchDataError(f"corpus artifact size mismatch for {key!r}")
    return {
        "manifest": _artifact_record(corpus_manifest_path),
        "artifacts": actual,
        "validation": {
            key: validation.get(key)
            for key in (
                "candidate_count",
                "profile_count",
                "candidate_profile_ids_match",
                "missing_prototype_source_id_count",
                "ambiguous_prototype_source_id_count",
                "research_non_temporal_evidence_count",
                "research_post_cutoff_evidence_count",
                "unrelated_evidence_id_collision_count",
            )
        },
    }


def load_frozen_edge_graph(
    *,
    profiles_path: Path,
    prototypes_path: Path,
    evidence_path: Path,
    cutoff: str,
) -> FrozenEdgeGraph:
    """Load and fail-closed validate every prototype-to-evidence edge."""

    cutoff_date = parse_iso_date(cutoff, field_name="property graph cutoff")
    profile_units, candidate_ids_list = load_prototype_units(profiles_path)
    candidate_ids = tuple(candidate_ids_list)
    candidate_set = set(candidate_ids)
    profile_prototypes: dict[str, tuple[str, str, float]] = {}
    for unit in profile_units:
        if unit.prototype_id in profile_prototypes:
            raise ResearchDataError(
                f"profile corpus contains duplicate prototype {unit.prototype_id!r}"
            )
        profile_prototypes[unit.prototype_id] = (
            unit.venue_id,
            unit.text,
            unit.weight,
        )

    prototypes: list[GraphPrototype] = []
    seen_prototypes: set[str] = set()
    required_sources: dict[str, set[str]] = defaultdict(set)
    source_edge_count = 0
    for line_number, row in _iter_jsonl(prototypes_path):
        prototype_id = str(row.get("prototype_id") or "").strip()
        venue_id = str(row.get("venue_id") or "").strip()
        text = " ".join(str(row.get("text") or "").split())
        raw_sources = row.get("source_ids")
        if (
            not prototype_id
            or prototype_id in seen_prototypes
            or venue_id not in candidate_set
            or not text
            or row.get("temporal_eligible") is not True
            or not isinstance(raw_sources, list)
            or not raw_sources
        ):
            raise ResearchDataError(
                f"{prototypes_path}:{line_number}: invalid graph prototype"
            )
        source_ids = tuple(str(value).strip() for value in raw_sources)
        if any(not value for value in source_ids) or len(set(source_ids)) != len(source_ids):
            raise ResearchDataError(
                f"{prototypes_path}:{line_number}: invalid or duplicate source IDs"
            )
        try:
            weight = float(row.get("weight", 1.0))
        except (TypeError, ValueError) as exc:
            raise ResearchDataError(
                f"{prototypes_path}:{line_number}: invalid prototype weight"
            ) from exc
        if not math.isfinite(weight) or weight < 0:
            raise ResearchDataError(
                f"{prototypes_path}:{line_number}: invalid prototype weight"
            )
        expected = profile_prototypes.get(prototype_id)
        if expected is None or expected[0] != venue_id or expected[1] != text:
            raise ResearchDataError(
                f"{prototypes_path}:{line_number}: prototype/profile mismatch"
            )
        if not math.isclose(expected[2], weight, rel_tol=0.0, abs_tol=1e-12):
            raise ResearchDataError(
                f"{prototypes_path}:{line_number}: prototype weight mismatch"
            )
        seen_prototypes.add(prototype_id)
        prototypes.append(
            GraphPrototype(prototype_id, venue_id, text, source_ids, weight)
        )
        for source_id in source_ids:
            required_sources[source_id].add(venue_id)
            source_edge_count += 1

    missing_prototypes = set(profile_prototypes) - seen_prototypes
    extra_prototypes = seen_prototypes - set(profile_prototypes)
    if missing_prototypes or extra_prototypes:
        raise ResearchDataError(
            "prototype artifact/profile coverage mismatch: "
            f"missing={sorted(missing_prototypes)[:5]}, "
            f"extra={sorted(extra_prototypes)[:5]}"
        )
    ambiguous_sources = {
        source_id: venues
        for source_id, venues in required_sources.items()
        if len(venues) != 1
    }
    if ambiguous_sources:
        raise ResearchDataError(
            "prototype evidence crosses candidate venues: "
            f"{sorted(ambiguous_sources)[:5]}"
        )

    evidence_by_id: dict[str, tuple[str, str, str]] = {}
    evidence_kind_counts: Counter[str] = Counter()
    matched_source_rows = 0
    for line_number, row in _iter_jsonl(evidence_path):
        evidence_id = str(row.get("evidence_id") or "").strip()
        if evidence_id not in required_sources:
            continue
        if evidence_id in evidence_by_id:
            raise ResearchDataError(
                f"{evidence_path}:{line_number}: duplicate linked evidence ID"
            )
        venue_id = str(row.get("venue_id") or "").strip()
        expected_venue = next(iter(required_sources[evidence_id]))
        valid_at = str(
            row.get("publication_date") or row.get("valid_at") or ""
        ).strip()[:10]
        if venue_id != expected_venue:
            raise ResearchDataError(
                f"{evidence_path}:{line_number}: evidence crosses candidate venues"
            )
        try:
            evidence_date = parse_iso_date(
                valid_at, field_name="property graph evidence valid_at"
            )
        except ResearchDataError as exc:
            raise ResearchDataError(
                f"{evidence_path}:{line_number}: invalid evidence date"
            ) from exc
        if row.get("temporal_eligible") is not True or evidence_date > cutoff_date:
            raise ResearchDataError(
                f"{evidence_path}:{line_number}: non-temporal or post-cutoff evidence"
            )
        text_parts: list[str] = []
        for field in ("title", "abstract", "text"):
            value = " ".join(str(row.get(field) or "").split())
            if value and value not in text_parts:
                text_parts.append(value)
        text = "\n".join(text_parts)
        if not text:
            raise ResearchDataError(
                f"{evidence_path}:{line_number}: linked evidence has no text"
            )
        kind = str(row.get("kind") or "unknown").strip() or "unknown"
        evidence_by_id[evidence_id] = (venue_id, text, kind)
        evidence_kind_counts[kind] += 1
        matched_source_rows += 1

    missing_sources = sorted(set(required_sources) - set(evidence_by_id))
    if missing_sources:
        raise ResearchDataError(
            f"prototype graph has missing evidence nodes: {missing_sources[:5]}"
        )

    prototype_texts: dict[str, list[str]] = {venue_id: [] for venue_id in candidate_ids}
    evidence_texts: dict[str, list[str]] = {venue_id: [] for venue_id in candidate_ids}
    venue_edge_counts: Counter[str] = Counter()
    for prototype in prototypes:
        prototype_texts[prototype.venue_id].append(prototype.text)
        venue_edge_counts[prototype.venue_id] += len(prototype.source_ids)
    for _source_id, (venue_id, text, _kind) in evidence_by_id.items():
        evidence_texts[venue_id].append(text)

    missing_prototype_venues = [
        venue_id for venue_id in candidate_ids if not prototype_texts[venue_id]
    ]
    missing_evidence_venues = [
        venue_id for venue_id in candidate_ids if not evidence_texts[venue_id]
    ]
    if missing_prototype_venues or missing_evidence_venues:
        raise ResearchDataError(
            "property graph lacks complete candidate coverage: "
            f"prototype={missing_prototype_venues[:5]}, "
            f"evidence={missing_evidence_venues[:5]}"
        )

    paper_edge_count = sum(
        1
        for prototype in prototypes
        for source_id in prototype.source_ids
        if evidence_by_id[source_id][2] == "paper"
    )
    audit = {
        "cutoff": cutoff,
        "candidate_count": len(candidate_ids),
        "profile_prototype_count": len(profile_prototypes),
        "prototype_count": len(prototypes),
        "prototype_evidence_edge_count": source_edge_count,
        "unique_linked_evidence_count": matched_source_rows,
        "paper_edge_count": paper_edge_count,
        "evidence_kind_counts": dict(sorted(evidence_kind_counts.items())),
        "max_prototype_degree": max(
            (len(prototype.source_ids) for prototype in prototypes), default=0
        ),
        "missing_prototype_count": 0,
        "missing_evidence_count": 0,
        "ambiguous_evidence_count": 0,
        "cross_venue_edge_count": 0,
        "non_temporal_evidence_count": 0,
        "post_cutoff_evidence_count": 0,
        "candidate_coverage_complete": True,
    }
    return FrozenEdgeGraph(
        candidate_ids=candidate_ids,
        prototype_texts={
            key: tuple(value) for key, value in prototype_texts.items()
        },
        evidence_texts={key: tuple(value) for key, value in evidence_texts.items()},
        venue_edge_counts=dict(venue_edge_counts),
        audit=audit,
    )


def _graph_documents(
    candidate_ids: Sequence[str], units: Mapping[str, Sequence[str]]
) -> list[VenueDocument]:
    return [
        VenueDocument(
            doc_id=venue_id,
            text="",
            metadata={
                "prototypes": [{"text": text} for text in units[venue_id]]
            },
        )
        for venue_id in candidate_ids
    ]


def _latency_summary(milliseconds: Sequence[float]) -> dict[str, float]:
    if not milliseconds:
        return {"mean_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
    ordered = sorted(milliseconds)

    def percentile(probability: float) -> float:
        position = probability * (len(ordered) - 1)
        lower = int(math.floor(position))
        upper = int(math.ceil(position))
        if lower == upper:
            return ordered[lower]
        fraction = position - lower
        return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction

    return {
        "mean_ms": fmean(milliseconds),
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "max_ms": max(milliseconds),
    }


def _preflight_new_run(path: Path) -> None:
    manifest_path = path.with_suffix(path.suffix + ".manifest.json")
    temporary_paths = (
        path.with_name("." + path.name + ".tmp"),
        manifest_path.with_name("." + manifest_path.name + ".tmp"),
    )
    conflicts = [item for item in (path, manifest_path, *temporary_paths) if item.exists()]
    if conflicts:
        raise ResearchDataError(
            "refusing to overwrite an existing run artifact: "
            + ", ".join(str(item) for item in conflicts)
        )


def _rank_map(ranking: Sequence[ScoredDocument]) -> dict[str, int]:
    return {item.doc_id: rank for rank, item in enumerate(ranking, 1)}


def build_property_graph_run(
    *,
    bundle: DatasetBundle,
    dataset_path: Path,
    profiles_path: Path,
    prototypes_path: Path,
    evidence_path: Path,
    corpus_manifest_path: Path,
    reference_manifest_path: Path,
    output_path: Path,
    cutoff: str = "2026-03-31",
    top_k: int = 100,
    candidate_pool: int = 1000,
    rrf_k: int = 60,
    prototype_weight: float = 1.0,
    evidence_weight: float = 1.0,
    edge_support_weight: float = 0.15,
    k1: float = 1.2,
    b: float = 0.75,
    query_fields: Sequence[str] = ("title", "abstract"),
    generation_command: Sequence[str],
) -> dict[str, Any]:
    """Build a deterministic BM25/RRF property-graph score run."""

    if top_k < 1 or candidate_pool < top_k or rrf_k < 1:
        raise ResearchDataError("graph depths require candidate_pool >= top_k >= 1")
    if min(prototype_weight, evidence_weight, edge_support_weight) < 0:
        raise ResearchDataError("graph channel weights must be non-negative")
    if prototype_weight + evidence_weight <= 0:
        raise ResearchDataError("at least one graph text channel must be enabled")
    _preflight_new_run(output_path)
    corpus_binding = _validate_corpus_artifacts(
        corpus_manifest_path=corpus_manifest_path,
        profiles_path=profiles_path,
        prototypes_path=prototypes_path,
        evidence_path=evidence_path,
    )

    graph_load_started = perf_counter()
    graph = load_frozen_edge_graph(
        profiles_path=profiles_path,
        prototypes_path=prototypes_path,
        evidence_path=evidence_path,
        cutoff=cutoff,
    )
    graph_load_ms = (perf_counter() - graph_load_started) * 1000.0
    keep_pool = min(candidate_pool, len(graph.candidate_ids))

    prototype_fit_started = perf_counter()
    prototype_retriever = BM25Baseline(
        name="property_graph_prototype_nodes",
        k1=k1,
        b=b,
        use_prototypes=True,
    ).fit(_graph_documents(graph.candidate_ids, graph.prototype_texts))
    prototype_fit_ms = (perf_counter() - prototype_fit_started) * 1000.0
    evidence_fit_started = perf_counter()
    evidence_retriever = BM25Baseline(
        name="property_graph_evidence_nodes",
        k1=k1,
        b=b,
        use_prototypes=True,
    ).fit(_graph_documents(graph.candidate_ids, graph.evidence_texts))
    evidence_fit_ms = (perf_counter() - evidence_fit_started) * 1000.0

    max_edges = max(graph.venue_edge_counts.values(), default=1)
    run: Run = {}
    query_latency_ms: list[float] = []
    for query in bundle.queries:
        started = perf_counter()
        prototype_ranking = prototype_retriever.run((query,), top_k=keep_pool)[
            query.query_id
        ]
        evidence_ranking = evidence_retriever.run((query,), top_k=keep_pool)[
            query.query_id
        ]
        prototype_ranks = _rank_map(prototype_ranking)
        evidence_ranks = _rank_map(evidence_ranking)
        scores: dict[str, float] = defaultdict(float)
        for venue_id, rank in prototype_ranks.items():
            scores[venue_id] += prototype_weight / (rrf_k + rank)
        for venue_id, rank in evidence_ranks.items():
            support = math.log1p(graph.venue_edge_counts[venue_id]) / math.log1p(
                max_edges
            )
            scores[venue_id] += evidence_weight * (
                1.0 + edge_support_weight * support
            ) / (rrf_k + rank)
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:top_k]
        run[query.query_id] = [ScoredDocument(doc_id, score) for doc_id, score in ranked]
        query_latency_ms.append((perf_counter() - started) * 1000.0)

    generation_config = {
        "builder": "property-graph-edge-bm25-rrf-v1",
        "cutoff": cutoff,
        "query_fields": list(query_fields),
        "top_k": top_k,
        "candidate_pool": candidate_pool,
        "rrf_k": rrf_k,
        "prototype_weight": prototype_weight,
        "evidence_weight": evidence_weight,
        "edge_support_weight": edge_support_weight,
        "bm25": {"k1": k1, "b": b},
        "corpus_manifest_sha256": corpus_binding["manifest"]["sha256"],
        "prototypes_sha256": corpus_binding["artifacts"]["prototypes"]["sha256"],
        "evidence_sha256": corpus_binding["artifacts"]["research_evidence"][
            "sha256"
        ],
    }
    binding = build_run_binding(
        dataset_path=dataset_path,
        profiles_path=profiles_path,
        query_ids=tuple(query.query_id for query in bundle.queries),
        candidate_ids=graph.candidate_ids,
        configuration=generation_config,
    )
    reference_binding = validate_reference_binding(reference_manifest_path, binding)
    implementation_revision = (
        "property-graph-edge-bm25-rrf-v1@" + sha256_file(Path(__file__))
    )
    runtime = runtime_provenance()
    return write_run(
        output_path,
        run,
        binding=binding,
        query_ids=tuple(query.query_id for query in bundle.queries),
        candidate_ids=graph.candidate_ids,
        top_k=top_k,
        method={
            "name": "property_graph_edge_bm25_rrf",
            "kind": "property_graph",
            "implementation": "research.graph_runs.build_property_graph_run",
            "implementation_revision": implementation_revision,
            "configuration_sha256": canonical_json_sha256(generation_config),
        },
        command=generation_command,
        working_directory=Path.cwd(),
        runtime=runtime,
        additional_manifest_fields={
            "retrieval_method": (
                "one-hop venue-to-prototype-to-evidence BM25 with deterministic RRF"
            ),
            "graph_semantics": {
                "nodes": ["venue", "prototype", "evidence"],
                "edges": ["venue_to_prototype", "prototype_to_evidence"],
                "evidence_channel_traversal": "evidence -> prototype -> venue",
                "generative_answering": False,
            },
            "edge_audit": dict(graph.audit),
            "corpus_binding": corpus_binding,
            "reference_binding_manifest": reference_binding,
            "execution": {
                "offline_only": True,
                "search_free": True,
                "external_api_calls": 0,
                "llm_calls": 0,
                "embedding_api_calls": 0,
                "estimated_external_cost_usd": 0.0,
                "graph_load_ms": graph_load_ms,
                "prototype_index_fit_ms": prototype_fit_ms,
                "evidence_index_fit_ms": evidence_fit_ms,
                "query_latency": _latency_summary(query_latency_ms),
                "failed_query_count": 0,
                "empty_ranking_count": sum(not ranking for ranking in run.values()),
            },
        },
    )


def _method_identity(manifest: Mapping[str, Any]) -> dict[str, str]:
    method = manifest.get("method")
    if not isinstance(method, Mapping):
        raise ResearchDataError("source run manifest is missing method identity")
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
        raise ResearchDataError("source run has no exact method identity")
    return identity


def _load_source_run(
    *,
    path: Path,
    query_ids: Sequence[str],
    candidate_ids: Sequence[str],
    reference_manifest_path: Path,
) -> tuple[Run, Mapping[str, Any], dict[str, Any]]:
    manifest_path = path.with_suffix(path.suffix + ".manifest.json")
    manifest = _read_object(manifest_path, label="source run manifest")
    binding = manifest.get("binding")
    if not isinstance(binding, Mapping):
        raise ResearchDataError("source run manifest is missing binding")
    configuration = binding.get("configuration")
    if not isinstance(configuration, Mapping):
        raise ResearchDataError("source run binding is missing configuration")
    reference = validate_reference_binding(reference_manifest_path, binding)
    run = load_score_run(
        path,
        expected_query_ids=query_ids,
        candidate_ids=candidate_ids,
        expected_binding=binding,
        expected_manifest_sha256=sha256_file(manifest_path),
        expected_configuration_sha256=str(configuration.get("canonical_sha256") or ""),
        expected_method_identity=_method_identity(manifest),
        manifest_path=manifest_path,
    )
    return run, manifest, {
        "run": _artifact_record(path),
        "manifest": _artifact_record(manifest_path),
        "reference_binding": reference,
    }


def mix_ranked_runs(
    local_run: Run,
    global_run: Run,
    *,
    query_ids: Sequence[str],
    top_k: int,
    rrf_k: int = 60,
    local_weight: float = 1.0,
    global_weight: float = 1.0,
) -> Run:
    """Fuse LightRAG local/global retrieval channels deterministically."""

    if top_k < 1 or rrf_k < 1 or min(local_weight, global_weight) < 0:
        raise ResearchDataError("invalid LightRAG mix configuration")
    if local_weight + global_weight <= 0:
        raise ResearchDataError("at least one LightRAG mix channel must be enabled")
    output: Run = {}
    for query_id in query_ids:
        scores: dict[str, float] = defaultdict(float)
        for rank, item in enumerate(local_run.get(query_id, ()), 1):
            scores[item.doc_id] += local_weight / (rrf_k + rank)
        for rank, item in enumerate(global_run.get(query_id, ()), 1):
            scores[item.doc_id] += global_weight / (rrf_k + rank)
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:top_k]
        output[query_id] = [ScoredDocument(doc_id, score) for doc_id, score in ranked]
    return output


def build_lightrag_mix_run(
    *,
    bundle: DatasetBundle,
    dataset_path: Path,
    profiles_path: Path,
    reference_manifest_path: Path,
    property_graph_run_path: Path,
    vector_run_path: Path,
    output_path: Path,
    top_k: int = 100,
    rrf_k: int = 60,
    local_weight: float = 1.0,
    global_weight: float = 1.0,
    query_fields: Sequence[str] = ("title", "abstract"),
    generation_command: Sequence[str],
) -> dict[str, Any]:
    """Build an offline LightRAG ``mix`` retrieval score run.

    ``local`` is the validated property-graph run over real prototype/evidence
    edges; ``global`` is the frozen dense retrieval run.  This is score-only
    evaluation and deliberately performs no generative LLM step.
    """

    _preflight_new_run(output_path)
    _profile_units, candidate_ids_list = load_prototype_units(profiles_path)
    candidate_ids = tuple(candidate_ids_list)
    query_ids = tuple(query.query_id for query in bundle.queries)
    local_run, local_manifest, local_artifacts = _load_source_run(
        path=property_graph_run_path,
        query_ids=query_ids,
        candidate_ids=candidate_ids,
        reference_manifest_path=reference_manifest_path,
    )
    global_run, global_manifest, global_artifacts = _load_source_run(
        path=vector_run_path,
        query_ids=query_ids,
        candidate_ids=candidate_ids,
        reference_manifest_path=reference_manifest_path,
    )
    edge_audit = local_manifest.get("edge_audit")
    local_method = local_manifest.get("method")
    if (
        not isinstance(local_method, Mapping)
        or local_method.get("kind") != "property_graph"
    ):
        raise ResearchDataError("LightRAG local channel must be a property-graph run")
    if not isinstance(edge_audit, Mapping):
        raise ResearchDataError("LightRAG local channel lacks a graph edge audit")
    required_zero_fields = (
        "missing_prototype_count",
        "missing_evidence_count",
        "ambiguous_evidence_count",
        "cross_venue_edge_count",
        "non_temporal_evidence_count",
        "post_cutoff_evidence_count",
    )
    if any(int(edge_audit.get(field, -1)) != 0 for field in required_zero_fields):
        raise ResearchDataError("LightRAG local channel graph audit did not pass")
    if int(edge_audit.get("prototype_evidence_edge_count", 0)) <= 0:
        raise ResearchDataError("LightRAG local channel has no prototype/evidence edges")
    if int(edge_audit.get("paper_edge_count", 0)) <= 0:
        raise ResearchDataError("LightRAG local channel has no real paper evidence edges")
    global_method = global_manifest.get("method")
    if not isinstance(global_method, Mapping) or global_method.get("kind") != "vector":
        raise ResearchDataError("LightRAG global channel must be a frozen vector run")
    for label, manifest in (("local", local_manifest), ("global", global_manifest)):
        coverage = manifest.get("coverage")
        if not isinstance(coverage, Mapping) or int(coverage.get("top_k", 0)) < top_k:
            raise ResearchDataError(
                f"LightRAG {label} channel depth is smaller than top_k={top_k}"
            )

    started = perf_counter()
    run = mix_ranked_runs(
        local_run,
        global_run,
        query_ids=query_ids,
        top_k=top_k,
        rrf_k=rrf_k,
        local_weight=local_weight,
        global_weight=global_weight,
    )
    elapsed_ms = (perf_counter() - started) * 1000.0
    generation_config = {
        "builder": "lightrag-mix-edge-rrf-v1",
        "query_fields": list(query_fields),
        "top_k": top_k,
        "rrf_k": rrf_k,
        "local_weight": local_weight,
        "global_weight": global_weight,
        "local_run_sha256": local_artifacts["run"]["sha256"],
        "local_manifest_sha256": local_artifacts["manifest"]["sha256"],
        "global_run_sha256": global_artifacts["run"]["sha256"],
        "global_manifest_sha256": global_artifacts["manifest"]["sha256"],
    }
    binding = build_run_binding(
        dataset_path=dataset_path,
        profiles_path=profiles_path,
        query_ids=query_ids,
        candidate_ids=candidate_ids,
        configuration=generation_config,
    )
    reference_binding = validate_reference_binding(reference_manifest_path, binding)
    implementation_revision = "lightrag-mix-edge-rrf-v1@" + sha256_file(
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
            "name": "lightrag_mix_edge_rrf",
            "kind": "lightrag_mix",
            "implementation": "research.graph_runs.build_lightrag_mix_run",
            "implementation_revision": implementation_revision,
            "configuration_sha256": canonical_json_sha256(generation_config),
        },
        command=generation_command,
        working_directory=Path.cwd(),
        runtime=runtime_provenance(),
        additional_manifest_fields={
            "retrieval_method": (
                "LightRAG mix score replay: property-graph local channel plus "
                "dense global channel using deterministic RRF"
            ),
            "lightrag_semantics": {
                "mode": "mix",
                "local_channel": "validated prototype-to-evidence property graph",
                "global_channel": "frozen dense venue retrieval",
                "real_prototype_evidence_edges": True,
                "generative_answering": False,
                "storage_independent_score_replay": True,
            },
            "edge_audit": dict(edge_audit),
            "channel_inputs": {
                "local": local_artifacts,
                "global": global_artifacts,
            },
            "reference_binding_manifest": reference_binding,
            "execution": {
                "offline_only": True,
                "search_free": True,
                "external_api_calls": 0,
                "llm_calls": 0,
                "embedding_api_calls": 0,
                "estimated_external_cost_usd": 0.0,
                "fusion_total_ms": elapsed_ms,
                "fusion_mean_ms_per_query": elapsed_ms / len(query_ids),
                "failed_query_count": 0,
                "empty_ranking_count": sum(not ranking for ranking in run.values()),
            },
        },
    )
