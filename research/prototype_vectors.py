"""Frozen PCL/bge-m3 runs over multi-prototype venue profiles."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence
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

from .data import DatasetBundle, ResearchDataError, sha256_file, write_run
from .historical_builder import now_iso
from .types import Run, ScoredDocument


@dataclass(frozen=True)
class PrototypeUnit:
    venue_id: str
    prototype_id: str
    text: str
    weight: float


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
    profiles_path: Path,
    cache_path: Path,
    output_path: Path,
    top_k: int = 100,
    query_batch_size: int = 16,
    prototype_chunk_size: int = 4096,
    apply_prototype_weights: bool = True,
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
    all_texts = dict(prototype_texts)
    all_texts.update(query_texts)
    with FileEmbeddingCache(cache_path) as cache:
        dimensions, embedded_count, cached_count = ensure_cached_embeddings(
            provider, all_texts, cache
        )
        prototype_vectors = _vectors_from_cache(cache, provider, prototype_hashes)
        query_vectors = _vectors_from_cache(cache, provider, query_hashes)

    unit_venues = np.asarray([venue_index[unit.venue_id] for unit in units], dtype=np.int32)
    unit_weights = np.asarray([unit.weight for unit in units], dtype=np.float32)
    run: Run = {}
    candidate_count = len(venue_ids)
    keep = min(top_k, candidate_count)
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
    write_run(output_path, run)
    manifest = {
        "schema_version": 1,
        "created_at": now_iso(),
        "method": "cosine prototype max pooling",
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
        "inputs": {
            "profiles": {"path": str(profiles_path), "sha256": sha256_file(profiles_path)},
        },
        "output": {"path": str(output_path), "sha256": sha256_file(output_path)},
    }
    manifest_path = output_path.with_suffix(output_path.suffix + ".manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def pcl_embedding_provider(api_config: Path) -> OpenAICompatibleEmbeddingProvider:
    config = load_embedding_config(api_config)
    hostname = (urllib.parse.urlparse(config.endpoint).hostname or "").casefold()
    if not (hostname == "pcl.ac.cn" or hostname.endswith(".pcl.ac.cn")):
        raise ResearchDataError("prototype vector runs require the configured PCL API")
    return OpenAICompatibleEmbeddingProvider(config)
