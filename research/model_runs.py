"""Local, pinned scientific-encoder score-run builders.

No model is fetched by this module.  Model and adapter directories must be
materialized separately at exact revisions, and every consumed file is hashed
before inference.  Embeddings are committed batch-by-batch to a resumable
SQLite cache.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
from time import perf_counter
from typing import Any, Mapping, Protocol, Sequence

import numpy as np

from where_paper_go.embeddings import (
    EmbeddingCache,
    ensure_cached_embeddings,
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
from .prototype_vectors import validate_reference_binding
from .types import Run, ScoredDocument


TITLE_ABSTRACT_SEPARATOR = "\n<|title_abstract_sep|>\n"


@dataclass(frozen=True)
class ScientificPrototype:
    venue_id: str
    prototype_id: str
    title: str
    abstract: str
    weight: float

    @property
    def model_input(self) -> str:
        return self.title + TITLE_ABSTRACT_SEPARATOR + self.abstract


class ScientificEmbeddingProvider(Protocol):
    model: str
    model_repo: str
    model_revision: str
    protocol: str
    fingerprint: str
    batch_size: int
    max_length: int
    device: str
    asset_record: Mapping[str, Any]

    def prepare_text(self, text: str) -> str: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def _normalize_field(value: object) -> str:
    return " ".join(str(value or "").split())


def _model_input(title: object, abstract: object) -> str:
    normalized_title = _normalize_field(title)
    normalized_abstract = _normalize_field(abstract)
    if not normalized_title and not normalized_abstract:
        raise ResearchDataError("scientific encoder input cannot be empty")
    if not normalized_title:
        normalized_title = normalized_abstract
        normalized_abstract = ""
    return normalized_title + TITLE_ABSTRACT_SEPARATOR + normalized_abstract


def load_scientific_prototypes(
    path: Path,
) -> tuple[list[ScientificPrototype], tuple[str, ...]]:
    """Load profile prototypes with an explicit title/abstract mapping."""

    prototypes: list[ScientificPrototype] = []
    venues: set[str] = set()
    prototype_ids: set[str] = set()
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise ResearchDataError(f"cannot open scientific profile corpus: {path}") from exc
    with handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ResearchDataError(
                    f"{path}:{line_number}: invalid scientific profile JSON"
                ) from exc
            if not isinstance(row, Mapping):
                raise ResearchDataError(f"{path}:{line_number}: expected an object")
            venue_id = str(row.get("venue_id") or "").strip()
            if not venue_id or venue_id in venues:
                raise ResearchDataError(
                    f"{path}:{line_number}: missing or duplicate venue_id"
                )
            venues.add(venue_id)
            raw_prototypes = row.get("prototypes")
            if not isinstance(raw_prototypes, list) or not raw_prototypes:
                raise ResearchDataError(
                    f"{path}:{line_number}: scientific profile has no prototypes"
                )
            added = 0
            for index, raw in enumerate(raw_prototypes):
                if not isinstance(raw, Mapping):
                    continue
                if raw.get("temporal_eligible", True) is False:
                    continue
                prototype_id = str(
                    raw.get("prototype_id") or f"{venue_id}:prototype:{index}"
                ).strip()
                title = _normalize_field(raw.get("label") or row.get("name"))
                abstract = _normalize_field(raw.get("text"))
                if not prototype_id or prototype_id in prototype_ids or not abstract:
                    raise ResearchDataError(
                        f"{path}:{line_number}: invalid or duplicate prototype"
                    )
                try:
                    weight = float(raw.get("weight", 1.0))
                except (TypeError, ValueError) as exc:
                    raise ResearchDataError(
                        f"{path}:{line_number}: invalid prototype weight"
                    ) from exc
                if not math.isfinite(weight) or not 0.0 <= weight <= 2.0:
                    raise ResearchDataError(
                        f"{path}:{line_number}: invalid prototype weight"
                    )
                prototype_ids.add(prototype_id)
                prototypes.append(
                    ScientificPrototype(
                        venue_id=venue_id,
                        prototype_id=prototype_id,
                        title=title or abstract,
                        abstract=abstract,
                        weight=weight,
                    )
                )
                added += 1
            if not added:
                raise ResearchDataError(
                    f"{path}:{line_number}: no temporal scientific prototypes"
                )
    if not venues or not prototypes:
        raise ResearchDataError(f"scientific profile corpus is empty: {path}")
    return prototypes, tuple(sorted(venues))


def _directory_record(path: Path) -> dict[str, Any]:
    root = path.resolve()
    if not root.is_dir():
        raise ResearchDataError(f"model asset directory does not exist: {root}")
    files: list[dict[str, Any]] = []
    for item in sorted(root.rglob("*")):
        if item.is_symlink():
            raise ResearchDataError(f"model asset must not be a symlink: {item}")
        if not item.is_file():
            continue
        relative = item.relative_to(root)
        if relative.parts and relative.parts[0] in {".cache", ".locks"}:
            continue
        files.append(
            {
                "path": relative.as_posix(),
                "sha256": sha256_file(item),
                "bytes": item.stat().st_size,
            }
        )
    if not files:
        raise ResearchDataError(f"model asset directory contains no files: {root}")
    return {
        "path": str(root),
        "file_count": len(files),
        "bytes": sum(int(item["bytes"]) for item in files),
        "tree_sha256": canonical_json_sha256(files),
        "files": files,
    }


class LocalScientificEncoderProvider:
    """Pinned local SPECTER2 or SciNCL CLS encoder."""

    def __init__(
        self,
        *,
        protocol: str,
        model_dir: Path,
        model_repo: str,
        model_revision: str,
        adapter_dir: Path | None = None,
        adapter_repo: str = "",
        adapter_revision: str = "",
        device: str = "cuda:0",
        batch_size: int = 32,
        max_length: int = 512,
        fp16: bool = True,
    ) -> None:
        if protocol not in {"specter2", "scincl"}:
            raise ResearchDataError(f"unsupported scientific protocol: {protocol!r}")
        if not model_repo.strip() or not re.fullmatch(r"[0-9a-f]{40}", model_revision):
            raise ResearchDataError("scientific encoder requires repo and exact revision")
        if batch_size < 1 or max_length < 8 or max_length > 512:
            raise ResearchDataError("invalid scientific encoder batch or sequence length")
        if protocol == "specter2":
            if (
                adapter_dir is None
                or not adapter_repo.strip()
                or not re.fullmatch(r"[0-9a-f]{40}", adapter_revision)
            ):
                raise ResearchDataError(
                    "SPECTER2 requires a pinned local proximity adapter"
                )
        elif adapter_dir is not None or adapter_repo or adapter_revision:
            raise ResearchDataError("SciNCL does not accept a SPECTER2 adapter")
        self.protocol = protocol
        self.model_repo = model_repo.strip()
        self.model_revision = model_revision.strip()
        self.model = self.model_repo
        self.model_dir = model_dir.resolve()
        self.adapter_dir = adapter_dir.resolve() if adapter_dir is not None else None
        self.adapter_repo = adapter_repo.strip()
        self.adapter_revision = adapter_revision.strip()
        self.device = device
        self.batch_size = batch_size
        self.max_length = max_length
        self.fp16 = bool(fp16)
        model_record = _directory_record(self.model_dir)
        adapter_record = (
            _directory_record(self.adapter_dir) if self.adapter_dir is not None else None
        )
        self.asset_record = {
            "model": {
                "repo": self.model_repo,
                "revision": self.model_revision,
                "directory": model_record,
            },
            **(
                {
                    "adapter": {
                        "repo": self.adapter_repo,
                        "revision": self.adapter_revision,
                        "directory": adapter_record,
                    }
                }
                if adapter_record is not None
                else {}
            ),
        }
        self.fingerprint = canonical_json_sha256(
            {
                "provider": "local_transformers_scientific_cls_v1",
                "protocol": self.protocol,
                "model_repo": self.model_repo,
                "model_revision": self.model_revision,
                "model_tree_sha256": model_record["tree_sha256"],
                "adapter_repo": self.adapter_repo,
                "adapter_revision": self.adapter_revision,
                "adapter_tree_sha256": (
                    adapter_record["tree_sha256"] if adapter_record else ""
                ),
                "max_length": self.max_length,
                "batch_size": self.batch_size,
                "device": self.device,
                "fp16": self.fp16,
                "deterministic_algorithms": True,
                "l2_normalize": True,
                "pooling": "last_hidden_state_cls",
            }
        )
        self._tokenizer: Any = None
        self._model: Any = None
        self._torch: Any = None

    def prepare_text(self, text: str) -> str:
        if TITLE_ABSTRACT_SEPARATOR not in text:
            raise ResearchDataError(
                "scientific input must contain the title/abstract separator"
            )
        title, abstract = text.split(TITLE_ABSTRACT_SEPARATOR, 1)
        return _model_input(title, abstract)

    def _load(self) -> None:
        if self._model is not None:
            return
        os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:  # pragma: no cover - host runtime dependency
            raise ResearchDataError(
                "scientific encoder runtime requires torch and transformers"
            ) from exc
        torch.use_deterministic_algorithms(True)
        if hasattr(torch.backends, "cuda") and hasattr(torch.backends.cuda, "matmul"):
            torch.backends.cuda.matmul.allow_tf32 = False
        tokenizer = AutoTokenizer.from_pretrained(
            str(self.model_dir),
            local_files_only=True,
            trust_remote_code=False,
        )
        if self.protocol == "specter2":
            try:
                from adapters import AutoAdapterModel
            except ImportError as exc:  # pragma: no cover - host dependency
                raise ResearchDataError(
                    "SPECTER2 runtime requires the adapters package"
                ) from exc
            model = AutoAdapterModel.from_pretrained(
                str(self.model_dir),
                local_files_only=True,
            )
            adapter_name = model.load_adapter(
                str(self.adapter_dir),
                load_as="specter2_proximity",
                set_active=True,
            )
            if not adapter_name:
                raise ResearchDataError("SPECTER2 proximity adapter did not load")
        else:
            model = AutoModel.from_pretrained(
                str(self.model_dir),
                local_files_only=True,
                trust_remote_code=False,
            )
        try:
            model.to(self.device)
        except (RuntimeError, ValueError) as exc:
            raise ResearchDataError(
                f"cannot place scientific encoder on {self.device!r}"
            ) from exc
        model.eval()
        self._torch = torch
        self._tokenizer = tokenizer
        self._model = model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        self._load()
        torch = self._torch
        tokenizer = self._tokenizer
        model = self._model
        separator = tokenizer.sep_token
        if not separator:
            raise ResearchDataError("scientific tokenizer has no SEP token")
        prepared: list[str] = []
        for text in texts:
            title, abstract = self.prepare_text(text).split(
                TITLE_ABSTRACT_SEPARATOR, 1
            )
            prepared.append(title + separator + abstract)
        inputs = tokenizer(
            prepared,
            padding=True,
            truncation=True,
            return_tensors="pt",
            max_length=self.max_length,
        )
        if self.protocol == "specter2":
            inputs.pop("token_type_ids", None)
        inputs = {key: value.to(self.device) for key, value in inputs.items()}
        use_amp = self.fp16 and str(self.device).startswith("cuda")
        device_type = "cuda" if str(self.device).startswith("cuda") else "cpu"
        with torch.inference_mode():
            with torch.autocast(
                device_type=device_type,
                dtype=torch.float16,
                enabled=use_amp,
            ):
                output = model(**inputs)
                vectors = output.last_hidden_state[:, 0, :]
            vectors = torch.nn.functional.normalize(vectors.float(), p=2, dim=1)
        return vectors.cpu().numpy().astype(np.float32, copy=False).tolist()


def _prepared_hashes(
    provider: ScientificEmbeddingProvider,
    texts: Sequence[str],
) -> tuple[list[str], dict[str, str]]:
    hashes: list[str] = []
    unique: dict[str, str] = {}
    for text in texts:
        prepared = provider.prepare_text(text)
        text_hash = hashlib.sha256(prepared.encode("utf-8")).hexdigest()
        hashes.append(text_hash)
        unique.setdefault(text_hash, prepared)
    return hashes, unique


def _cached_vectors(
    cache: EmbeddingCache,
    provider: ScientificEmbeddingProvider,
    hashes: Sequence[str],
) -> np.ndarray:
    rows = cache.get_many(provider.fingerprint, hashes)
    vectors: list[list[float]] = []
    dimensions: set[int] = set()
    for text_hash in hashes:
        row = rows.get(text_hash)
        if row is None:
            raise ResearchDataError("scientific embedding cache is incomplete")
        dimension, blob = row
        dimensions.add(dimension)
        vectors.append(unpack_float32(blob, dimension))
    if len(dimensions) != 1:
        raise ResearchDataError("scientific embedding cache dimensions are inconsistent")
    return np.asarray(vectors, dtype=np.float32)


def _preflight_new_run(path: Path) -> None:
    manifest = path.with_suffix(path.suffix + ".manifest.json")
    temporary = path.with_name("." + path.name + ".tmp")
    manifest_temporary = manifest.with_name("." + manifest.name + ".tmp")
    conflicts = [
        item
        for item in (path, manifest, temporary, manifest_temporary)
        if item.exists()
    ]
    if conflicts:
        raise ResearchDataError(
            "refusing to overwrite an existing scientific run artifact: "
            + ", ".join(str(item) for item in conflicts)
        )


def build_scientific_encoder_run(
    *,
    provider: ScientificEmbeddingProvider,
    bundle: DatasetBundle,
    dataset_path: Path,
    profiles_path: Path,
    reference_manifest_path: Path,
    cache_path: Path,
    output_path: Path,
    top_k: int = 100,
    query_batch_size: int = 16,
    prototype_chunk_size: int = 4096,
    apply_prototype_weights: bool = True,
    generation_command: Sequence[str],
    embedding_progress: Any = None,
) -> dict[str, Any]:
    """Encode all frozen prototypes/queries and max-pool scores by venue."""

    if top_k < 1 or query_batch_size < 1 or prototype_chunk_size < 1:
        raise ResearchDataError("scientific score-run sizes must be positive")
    _preflight_new_run(output_path)
    units, venue_ids = load_scientific_prototypes(profiles_path)
    venue_index = {venue_id: index for index, venue_id in enumerate(venue_ids)}
    prototype_hashes, prototype_texts = _prepared_hashes(
        provider, [unit.model_input for unit in units]
    )
    query_inputs = [
        _model_input(query.title, query.abstract)
        for query in bundle.queries
    ]
    query_hashes, query_texts = _prepared_hashes(provider, query_inputs)
    generation_config = {
        "builder": "scientific-prototype-cls-max-pooling-v1",
        "protocol": provider.protocol,
        "model_repo": provider.model_repo,
        "model_revision": provider.model_revision,
        "provider_fingerprint": provider.fingerprint,
        "query_fields": ["title", "abstract"],
        "candidate_mapping": "prototype label as title; prototype text as abstract",
        "query_mapping": "paper title as title; paper abstract as abstract",
        "sequence_format": "title + tokenizer.sep_token + abstract",
        "pooling": "last_hidden_state[:,0,:]",
        "l2_normalize": True,
        "max_length": provider.max_length,
        "top_k": top_k,
        "query_batch_size": query_batch_size,
        "prototype_chunk_size": prototype_chunk_size,
        "apply_prototype_weights": apply_prototype_weights,
    }
    binding = build_run_binding(
        dataset_path=dataset_path,
        profiles_path=profiles_path,
        query_ids=tuple(query.query_id for query in bundle.queries),
        candidate_ids=venue_ids,
        configuration=generation_config,
    )
    reference_binding = validate_reference_binding(reference_manifest_path, binding)
    all_texts = dict(prototype_texts)
    all_texts.update(query_texts)
    embedding_started = perf_counter()
    with EmbeddingCache(cache_path) as cache:
        dimensions, embedded_count, cached_count = ensure_cached_embeddings(
            provider,
            all_texts,
            cache,
            progress=embedding_progress,
        )
        prototype_vectors = _cached_vectors(cache, provider, prototype_hashes)
        query_vectors = _cached_vectors(cache, provider, query_hashes)
    embedding_total_ms = (perf_counter() - embedding_started) * 1000.0

    unit_venues = np.asarray(
        [venue_index[unit.venue_id] for unit in units], dtype=np.int32
    )
    unit_weights = np.asarray([unit.weight for unit in units], dtype=np.float32)
    candidate_count = len(venue_ids)
    keep = min(top_k, candidate_count)
    run: Run = {}
    scoring_started = perf_counter()
    for query_offset in range(0, len(bundle.queries), query_batch_size):
        query_chunk = query_vectors[query_offset : query_offset + query_batch_size]
        pooled = np.full(
            (len(query_chunk), candidate_count), -np.inf, dtype=np.float32
        )
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
            selected = (
                np.arange(candidate_count)
                if keep == candidate_count
                else np.argpartition(values, candidate_count - keep)[-keep:]
            )
            ranked = sorted(
                (
                    (venue_ids[int(index)], float(values[int(index)]))
                    for index in selected
                ),
                key=lambda item: (-item[1], item[0]),
            )
            run[query.query_id] = [
                ScoredDocument(doc_id=venue_id, score=score)
                for venue_id, score in ranked
            ]
    scoring_total_ms = (perf_counter() - scoring_started) * 1000.0
    cache_record = {
        "path": str(cache_path.resolve()),
        "sha256": sha256_file(cache_path),
        "bytes": cache_path.stat().st_size,
    }
    implementation_revision = (
        "scientific-prototype-cls-max-pooling-v1@" + sha256_file(Path(__file__))
    )
    return write_run(
        output_path,
        run,
        binding=binding,
        query_ids=tuple(query.query_id for query in bundle.queries),
        candidate_ids=venue_ids,
        top_k=top_k,
        method={
            "name": f"{provider.protocol}_prototype_max",
            "kind": "scientific_encoder",
            "implementation": "research.model_runs.build_scientific_encoder_run",
            "implementation_revision": implementation_revision,
            "model_revision": f"{provider.model_repo}@{provider.model_revision}",
            "provider_fingerprint": provider.fingerprint,
            "configuration_sha256": canonical_json_sha256(generation_config),
        },
        command=generation_command,
        working_directory=Path.cwd(),
        runtime=runtime_provenance(),
        additional_manifest_fields={
            "retrieval_method": (
                f"{provider.protocol} CLS cosine prototype max pooling"
            ),
            "official_input_protocol": {
                "sequence": "title + tokenizer.sep_token + abstract",
                "max_length": provider.max_length,
                "token_type_ids_removed": provider.protocol == "specter2",
                "pooling": "last_hidden_state[:,0,:]",
                "normalization": "L2",
                "deterministic_algorithms": True,
                "candidate_surrogate_disclosure": (
                    "venue prototypes are mapped to title/abstract fields; they are "
                    "not represented as original papers"
                ),
            },
            "model_assets": dict(provider.asset_record),
            "reference_binding_manifest": reference_binding,
            "embedding_cache": cache_record,
            "coverage_details": {
                "venue_count": len(venue_ids),
                "prototype_count": len(units),
                "query_count": len(bundle.queries),
                "dimensions": dimensions,
                "embedded_text_count": embedded_count,
                "cached_text_count": cached_count,
            },
            "execution": {
                "offline_only": True,
                "search_free": True,
                "local_files_only": True,
                "external_api_calls": 0,
                "estimated_external_cost_usd": 0.0,
                "device": provider.device,
                "embedding_total_ms": embedding_total_ms,
                "scoring_total_ms": scoring_total_ms,
                "failed_query_count": 0,
            },
        },
    )
