"""Frozen PCL/bge-m3 runs over multi-prototype venue profiles."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Mapping, Sequence
import urllib.parse

import numpy as np

from where_paper_go.embeddings import (
    EmbeddingProvider,
    FileEmbeddingCache,
    OpenAICompatibleEmbeddingProvider,
    ensure_cached_embeddings,
    load_embedding_config,
    unpack_float32,
)

from .data import (
    DatasetBundle,
    ResearchDataError,
    build_run_binding,
    canonical_json_sha256,
    runtime_provenance,
    sha256_file,
    write_run,
)
from .types import Run, ScoredDocument


@dataclass(frozen=True)
class PrototypeUnit:
    venue_id: str
    prototype_id: str
    text: str
    weight: float


_REFERENCE_BINDING_FIELDS = (
    ("dataset", "sha256"),
    ("dataset", "bytes"),
    ("queries", "count"),
    ("queries", "ordered_ids_sha256"),
    ("profiles", "sha256"),
    ("profiles", "bytes"),
    ("candidates", "count"),
    ("candidates", "ordering"),
    ("candidates", "ordered_ids_sha256"),
)


def validate_reference_binding(
    reference_manifest_path: Path,
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    """Fail closed unless a frozen run exactly matches a reference binding."""

    try:
        payload = json.loads(reference_manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResearchDataError(
            f"cannot read reference binding manifest: {reference_manifest_path}"
        ) from exc
    reference = payload.get("binding") if isinstance(payload, Mapping) else None
    if not isinstance(reference, Mapping):
        raise ResearchDataError("reference manifest is missing a binding object")

    verified: dict[str, dict[str, Any]] = {}
    for section, field in _REFERENCE_BINDING_FIELDS:
        expected_section = reference.get(section)
        actual_section = binding.get(section)
        if not isinstance(expected_section, Mapping) or field not in expected_section:
            raise ResearchDataError(
                f"reference binding is missing {section}.{field}"
            )
        if not isinstance(actual_section, Mapping) or field not in actual_section:
            raise ResearchDataError(f"run binding is missing {section}.{field}")
        expected = expected_section[field]
        actual = actual_section[field]
        if actual != expected:
            raise ResearchDataError(
                f"reference binding mismatch for {section}.{field}"
            )
        verified.setdefault(section, {})[field] = actual

    return {
        "path": str(reference_manifest_path.resolve()),
        "sha256": sha256_file(reference_manifest_path),
        "bytes": reference_manifest_path.stat().st_size,
        "verified_fields": verified,
    }


def load_prototype_units(path: Path) -> tuple[list[PrototypeUnit], list[str]]:
    units: list[PrototypeUnit] = []
    venue_ids: list[str] = []
    seen_venues: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ResearchDataError(
                    f"{path}:{line_number}: invalid profile JSON"
                ) from exc
            if not isinstance(row, Mapping):
                raise ResearchDataError(f"{path}:{line_number}: expected an object")
            venue_id = str(row.get("venue_id") or "").strip()
            if not venue_id or venue_id in seen_venues:
                raise ResearchDataError(
                    f"{path}:{line_number}: missing or duplicate venue_id"
                )
            seen_venues.add(venue_id)
            venue_ids.append(venue_id)
            prototypes = row.get("prototypes")
            if not isinstance(prototypes, list):
                prototypes = []
            added = 0
            for index, prototype in enumerate(prototypes):
                if not isinstance(prototype, Mapping):
                    continue
                if prototype.get("temporal_eligible", True) is False:
                    continue
                text = " ".join(str(prototype.get("text") or "").split())
                if not text:
                    continue
                try:
                    weight = float(prototype.get("weight", 1.0))
                except (TypeError, ValueError):
                    weight = 1.0
                units.append(
                    PrototypeUnit(
                        venue_id=venue_id,
                        prototype_id=str(
                            prototype.get("prototype_id") or f"{venue_id}:prototype:{index}"
                        ),
                        text=text,
                        weight=max(0.0, min(2.0, weight)),
                    )
                )
                added += 1
            if not added:
                text = " ".join(
                    str(row.get("profile_text") or row.get("name") or "").split()
                )
                if not text:
                    raise ResearchDataError(f"{path}:{line_number}: empty profile")
                units.append(
                    PrototypeUnit(venue_id, f"{venue_id}:fallback", text, 1.0)
                )
    if not venue_ids or not units:
        raise ResearchDataError(f"profile corpus is empty: {path}")
    return units, sorted(venue_ids)


def _prepared_hashes(
    provider: EmbeddingProvider, texts: Sequence[str]
) -> tuple[list[str], dict[str, str]]:
    hashes: list[str] = []
    unique: dict[str, str] = {}
    for text in texts:
        prepared = provider.prepare_text(text)
        text_hash = hashlib.sha256(prepared.encode("utf-8")).hexdigest()
        hashes.append(text_hash)
        unique.setdefault(text_hash, prepared)
    return hashes, unique


def _vectors_from_cache(
    cache: FileEmbeddingCache,
    provider: EmbeddingProvider,
    hashes: Sequence[str],
) -> np.ndarray:
    rows = cache.get_many(provider.fingerprint, list(hashes))
    vectors: list[list[float]] = []
    dimensions: set[int] = set()
    for text_hash in hashes:
        if text_hash not in rows:
            raise ResearchDataError("embedding cache is missing a requested text")
        dimension, blob = rows[text_hash]
        dimensions.add(dimension)
        vectors.append(unpack_float32(blob, dimension))
    if len(dimensions) != 1:
        raise ResearchDataError(f"inconsistent embedding dimensions: {sorted(dimensions)}")
    return np.asarray(vectors, dtype=np.float32)


def build_prototype_vector_run(
    *,
    provider: EmbeddingProvider,
    bundle: DatasetBundle,
    dataset_path: Path,
    profiles_path: Path,
    cache_path: Path,
    output_path: Path,
    top_k: int = 100,
    query_batch_size: int = 16,
    prototype_chunk_size: int = 4096,
    apply_prototype_weights: bool = True,
    query_fields: Sequence[str] = ("title", "abstract"),
    reference_manifest_path: Path | None = None,
    embedding_progress: Callable[[int, int], None] | None = None,
    cache_only: bool = False,
    generation_command: Sequence[str],
) -> dict[str, Any]:
    """Embed profiles/queries, max-pool prototypes, and freeze a reusable run."""

    if top_k < 1 or query_batch_size < 1 or prototype_chunk_size < 1:
        raise ResearchDataError("vector run sizes must be positive")
    units, venue_ids = load_prototype_units(profiles_path)
    venue_index = {venue_id: index for index, venue_id in enumerate(venue_ids)}
    prototype_hashes, prototype_texts = _prepared_hashes(
        provider, [unit.text for unit in units]
    )
    query_hashes, query_texts = _prepared_hashes(
        provider, [query.text for query in bundle.queries]
    )
    generation_config = {
        "builder": "prototype-vector-max-pooling-v3",
        "query_fields": list(query_fields),
        "top_k": top_k,
        "query_batch_size": query_batch_size,
        "prototype_chunk_size": prototype_chunk_size,
        "apply_prototype_weights": apply_prototype_weights,
        "model": provider.model,
        "provider_fingerprint": provider.fingerprint,
        "cache_only": cache_only,
    }
    binding = build_run_binding(
        dataset_path=dataset_path,
        profiles_path=profiles_path,
        query_ids=tuple(query.query_id for query in bundle.queries),
        candidate_ids=venue_ids,
        configuration=generation_config,
    )
    reference_binding = (
        validate_reference_binding(reference_manifest_path, binding)
        if reference_manifest_path is not None
        else None
    )
    all_texts = dict(prototype_texts)
    all_texts.update(query_texts)
    embedding_started = perf_counter()
    with FileEmbeddingCache(cache_path) as cache:
        if cache_only:
            cached_rows = cache.get_many(provider.fingerprint, list(all_texts))
            missing_count = len(all_texts) - len(cached_rows)
            if missing_count:
                raise ResearchDataError(
                    "cache-only vector run refuses external embedding calls: "
                    f"{missing_count} prepared texts are missing"
                )
            cached_dimensions = {
                dimension for dimension, _vector in cached_rows.values()
            }
            if len(cached_dimensions) != 1:
                raise ResearchDataError(
                    "cache-only embedding dimensions are inconsistent: "
                    f"{sorted(cached_dimensions)}"
                )
            dimensions = next(iter(cached_dimensions))
            embedded_count = 0
            cached_count = len(cached_rows)
        else:
            dimensions, embedded_count, cached_count = ensure_cached_embeddings(
                provider, all_texts, cache, progress=embedding_progress
            )
        prototype_vectors = _vectors_from_cache(cache, provider, prototype_hashes)
        query_vectors = _vectors_from_cache(cache, provider, query_hashes)
    embedding_total_ms = (perf_counter() - embedding_started) * 1000.0

    unit_venues = np.asarray([venue_index[unit.venue_id] for unit in units], dtype=np.int32)
    unit_weights = np.asarray([unit.weight for unit in units], dtype=np.float32)
    run: Run = {}
    candidate_count = len(venue_ids)
    keep = min(top_k, candidate_count)
    scoring_started = perf_counter()
    for query_offset in range(0, len(bundle.queries), query_batch_size):
        query_chunk = query_vectors[query_offset : query_offset + query_batch_size]
        pooled = np.full((len(query_chunk), candidate_count), -np.inf, dtype=np.float32)
        for prototype_offset in range(0, len(units), prototype_chunk_size):
            end = prototype_offset + prototype_chunk_size
            scores = query_chunk @ prototype_vectors[prototype_offset:end].T
            if apply_prototype_weights:
                scores *= unit_weights[prototype_offset:end][None, :]
            venue_chunk = unit_venues[prototype_offset:end]
            for query_index in range(len(query_chunk)):
                np.maximum.at(pooled[query_index], venue_chunk, scores[query_index])
        for local_index, query in enumerate(
            bundle.queries[query_offset : query_offset + query_batch_size]
        ):
            values = pooled[local_index]
            if keep == candidate_count:
                selected = np.arange(candidate_count)
            else:
                selected = np.argpartition(values, candidate_count - keep)[-keep:]
            ranked = sorted(
                ((venue_ids[int(index)], float(values[int(index)])) for index in selected),
                key=lambda item: (-item[1], item[0]),
            )
            run[query.query_id] = [
                ScoredDocument(doc_id=venue_id, score=score)
                for venue_id, score in ranked
            ]
    scoring_total_ms = (perf_counter() - scoring_started) * 1000.0
    runtime = runtime_provenance()
    implementation_revision = (
        "prototype-vector-max-pooling-v3@" + sha256_file(Path(__file__))
    )
    method = {
        "name": "prototype_vector_max_pool",
        "kind": "vector",
        "implementation": "research.prototype_vectors.build_prototype_vector_run",
        "implementation_revision": implementation_revision,
        "provider_fingerprint": provider.fingerprint,
        "model": provider.model,
        "configuration_sha256": canonical_json_sha256(generation_config),
    }
    manifest = write_run(
        output_path,
        run,
        binding=binding,
        query_ids=tuple(query.query_id for query in bundle.queries),
        candidate_ids=venue_ids,
        top_k=top_k,
        method=method,
        command=generation_command,
        working_directory=Path.cwd(),
        runtime=runtime,
        additional_manifest_fields={
            "retrieval_method": "cosine prototype max pooling",
            "model": provider.model,
            "provider_fingerprint": provider.fingerprint,
            "dimensions": dimensions,
            "venue_count": len(venue_ids),
            "prototype_count": len(units),
            "query_count": len(bundle.queries),
            "top_k": top_k,
            "apply_prototype_weights": apply_prototype_weights,
            "embedding_cache": str(cache_path),
            "embedded_text_count": embedded_count,
            "cached_text_count": cached_count,
            "execution": {
                "cache_only": cache_only,
                "embedding_total_ms": embedding_total_ms,
                "scoring_total_ms": scoring_total_ms,
                "mean_scoring_ms_per_query": scoring_total_ms / len(bundle.queries),
                "external_api_calls": 0 if cache_only else None,
                "estimated_external_cost_usd": 0.0 if cache_only else None,
                "failed_query_count": 0,
                "offline_only": cache_only,
                "search_free": True,
            },
            **(
                {"reference_binding_manifest": reference_binding}
                if reference_binding is not None
                else {}
            ),
        },
    )
    return manifest


def pcl_embedding_provider(api_config: Path) -> OpenAICompatibleEmbeddingProvider:
    config = load_embedding_config(api_config)
    hostname = (urllib.parse.urlparse(config.endpoint).hostname or "").casefold()
    if not (hostname == "pcl.ac.cn" or hostname.endswith(".pcl.ac.cn")):
        raise ResearchDataError("prototype vector runs require the configured PCL API")
    return OpenAICompatibleEmbeddingProvider(config)
