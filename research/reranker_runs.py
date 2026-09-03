"""Pinned, local cross-encoder reranking over frozen first-stage runs."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from time import perf_counter
from typing import Any, Mapping, Protocol, Sequence

import numpy as np

from .data import (
    DatasetBundle,
    ResearchDataError,
    build_run_binding,
    canonical_json_sha256,
    load_score_run,
    runtime_provenance,
    sha256_file,
    write_run,
)
from .model_runs import _directory_record, load_scientific_prototypes
from .prototype_vectors import validate_reference_binding
from .types import Run, ScoredDocument


class CrossEncoderProvider(Protocol):
    model: str
    model_repo: str
    model_revision: str
    fingerprint: str
    batch_size: int
    max_length: int
    device: str
    asset_record: Mapping[str, Any]

    def prepare_pair(self, query: str, passage: str) -> tuple[str, str]: ...

    def score_pairs(self, pairs: Sequence[tuple[str, str]]) -> list[float]: ...


def _normalize(value: object) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ResearchDataError("cross-encoder input cannot be empty")
    return text


class LocalBGECrossEncoderProvider:
    """Official Transformers protocol for ``bge-reranker-v2-m3``."""

    def __init__(
        self,
        *,
        model_dir: Path,
        model_repo: str,
        model_revision: str,
        device: str = "cuda:0",
        batch_size: int = 32,
        max_length: int = 512,
        fp16: bool = True,
    ) -> None:
        if not model_repo.strip() or not re.fullmatch(r"[0-9a-f]{40}", model_revision):
            raise ResearchDataError("cross-encoder requires repo and exact revision")
        if batch_size < 1 or max_length < 8 or max_length > 512:
            raise ResearchDataError("invalid cross-encoder batch or sequence length")
        self.model_dir = model_dir.resolve()
        self.model_repo = model_repo.strip()
        self.model_revision = model_revision.strip()
        self.model = self.model_repo
        self.device = device
        self.batch_size = batch_size
        self.max_length = max_length
        self.fp16 = bool(fp16)
        directory = _directory_record(self.model_dir)
        self.asset_record = {
            "model": {
                "repo": self.model_repo,
                "revision": self.model_revision,
                "directory": directory,
            }
        }
        self.fingerprint = canonical_json_sha256(
            {
                "provider": "local_bge_cross_encoder_v1",
                "model_repo": self.model_repo,
                "model_revision": self.model_revision,
                "model_tree_sha256": directory["tree_sha256"],
                "max_length": self.max_length,
                "batch_size": self.batch_size,
                "device": self.device,
                "fp16": self.fp16,
                "score": "raw_sequence_classification_logit",
                "deterministic_algorithms": True,
            }
        )
        self._torch: Any = None
        self._tokenizer: Any = None
        self._model: Any = None

    def prepare_pair(self, query: str, passage: str) -> tuple[str, str]:
        return _normalize(query), _normalize(passage)

    def _load(self) -> None:
        if self._model is not None:
            return
        os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
        try:
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
        except ImportError as exc:  # pragma: no cover - host runtime dependency
            raise ResearchDataError(
                "cross-encoder runtime requires torch and transformers"
            ) from exc
        torch.use_deterministic_algorithms(True)
        if hasattr(torch.backends, "cuda") and hasattr(torch.backends.cuda, "matmul"):
            torch.backends.cuda.matmul.allow_tf32 = False
        tokenizer = AutoTokenizer.from_pretrained(
            str(self.model_dir),
            local_files_only=True,
            trust_remote_code=False,
        )
        model = AutoModelForSequenceClassification.from_pretrained(
            str(self.model_dir),
            local_files_only=True,
            trust_remote_code=False,
        )
        try:
            model.to(self.device)
        except (RuntimeError, ValueError) as exc:
            raise ResearchDataError(
                f"cannot place cross-encoder on {self.device!r}"
            ) from exc
        model.eval()
        self._torch = torch
        self._tokenizer = tokenizer
        self._model = model

    def score_pairs(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        if not pairs:
            return []
        self._load()
        prepared = [self.prepare_pair(query, passage) for query, passage in pairs]
        inputs = self._tokenizer(
            prepared,
            padding=True,
            truncation=True,
            return_tensors="pt",
            max_length=self.max_length,
        )
        inputs = {key: value.to(self.device) for key, value in inputs.items()}
        use_amp = self.fp16 and str(self.device).startswith("cuda")
        device_type = "cuda" if str(self.device).startswith("cuda") else "cpu"
        with self._torch.inference_mode():
            with self._torch.autocast(
                device_type=device_type,
                dtype=self._torch.float16,
                enabled=use_amp,
            ):
                logits = self._model(**inputs, return_dict=True).logits.view(-1)
        scores = logits.float().cpu().numpy().astype(np.float32, copy=False).tolist()
        if len(scores) != len(prepared) or any(not math.isfinite(score) for score in scores):
            raise ResearchDataError("cross-encoder returned invalid scores")
        return [float(score) for score in scores]


class PairScoreCache:
    """Batch-committed, resumable cache for deterministic pair logits."""

    SCHEMA_VERSION = "1"

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS cache_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS pair_score (
                provider_fingerprint TEXT NOT NULL,
                pair_sha256 TEXT NOT NULL,
                score REAL NOT NULL,
                PRIMARY KEY (provider_fingerprint, pair_sha256)
            ) WITHOUT ROWID;
            """
        )
        current = self.connection.execute(
            "SELECT value FROM cache_meta WHERE key = 'schema_version'"
        ).fetchone()
        if current is not None and current[0] != self.SCHEMA_VERSION:
            self.connection.close()
            raise ResearchDataError("pair score cache schema is incompatible")
        self.connection.execute(
            "INSERT OR REPLACE INTO cache_meta(key, value) VALUES (?, ?)",
            ("schema_version", self.SCHEMA_VERSION),
        )
        self.connection.commit()

    def __enter__(self) -> "PairScoreCache":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.connection.close()

    def get_many(
        self, provider_fingerprint: str, pair_hashes: Sequence[str]
    ) -> dict[str, float]:
        result: dict[str, float] = {}
        unique = list(dict.fromkeys(pair_hashes))
        for offset in range(0, len(unique), 400):
            chunk = unique[offset : offset + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                "SELECT pair_sha256, score FROM pair_score "
                f"WHERE provider_fingerprint = ? AND pair_sha256 IN ({placeholders})",
                (provider_fingerprint, *chunk),
            )
            for pair_hash, score in rows:
                value = float(score)
                if not math.isfinite(value):
                    raise ResearchDataError("pair score cache contains non-finite data")
                result[str(pair_hash)] = value
        return result

    def put_many(
        self,
        provider_fingerprint: str,
        rows: Sequence[tuple[str, float]],
    ) -> None:
        checked: list[tuple[str, str, float]] = []
        for pair_hash, score in rows:
            value = float(score)
            if not math.isfinite(value):
                raise ResearchDataError("cannot cache a non-finite pair score")
            checked.append((provider_fingerprint, pair_hash, value))
        self.connection.executemany(
            "INSERT OR REPLACE INTO pair_score(provider_fingerprint, pair_sha256, score) "
            "VALUES (?, ?, ?)",
            checked,
        )
        self.connection.commit()


def _read_object(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResearchDataError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, Mapping):
        raise ResearchDataError(f"{label} must be a JSON object")
    return value


def _method_identity(manifest: Mapping[str, Any]) -> dict[str, str]:
    method = manifest.get("method")
    if not isinstance(method, Mapping):
        raise ResearchDataError("first-stage run manifest has no method")
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
        raise ResearchDataError("first-stage run has no exact method identity")
    return identity


def _artifact_record(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    return {
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "bytes": resolved.stat().st_size,
    }


def _load_first_stage(
    *,
    path: Path,
    query_ids: Sequence[str],
    candidate_ids: Sequence[str],
    reference_manifest_path: Path,
    candidate_pool: int,
) -> tuple[Run, Mapping[str, Any], dict[str, Any]]:
    manifest_path = path.with_suffix(path.suffix + ".manifest.json")
    manifest = _read_object(manifest_path, "first-stage run manifest")
    binding = manifest.get("binding")
    coverage = manifest.get("coverage")
    if not isinstance(binding, Mapping) or not isinstance(coverage, Mapping):
        raise ResearchDataError("first-stage run manifest is incomplete")
    configuration = binding.get("configuration")
    if not isinstance(configuration, Mapping):
        raise ResearchDataError("first-stage binding lacks configuration")
    if int(coverage.get("top_k", 0)) < candidate_pool:
        raise ResearchDataError("first-stage run depth is smaller than candidate pool")
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
        top_k=candidate_pool,
    )
    return run, manifest, {
        "run": _artifact_record(path),
        "manifest": _artifact_record(manifest_path),
        "reference_binding": reference,
    }


def _pair_hash(query: str, passage: str) -> str:
    return hashlib.sha256(
        json.dumps(
            [query, passage],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


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
            "refusing to overwrite an existing cross-encoder artifact: "
            + ", ".join(str(item) for item in conflicts)
        )


def build_cross_encoder_run(
    *,
    provider: CrossEncoderProvider,
    bundle: DatasetBundle,
    dataset_path: Path,
    profiles_path: Path,
    reference_manifest_path: Path,
    first_stage_run_path: Path,
    cache_path: Path,
    output_path: Path,
    candidate_pool: int = 100,
    top_k: int = 100,
    generation_command: Sequence[str],
    progress: Any = None,
) -> dict[str, Any]:
    """Rerank every first-stage candidate and retain the full denominator."""

    if candidate_pool < top_k or top_k < 1:
        raise ResearchDataError("cross-encoder requires candidate_pool >= top_k >= 1")
    _preflight_new_run(output_path)
    prototypes, candidate_ids = load_scientific_prototypes(profiles_path)
    prototypes_by_venue: dict[str, list[str]] = defaultdict(list)
    for prototype in prototypes:
        passage = _normalize(prototype.title + "\n" + prototype.abstract)
        prototypes_by_venue[prototype.venue_id].append(passage)
    query_ids = tuple(query.query_id for query in bundle.queries)
    first_stage, first_stage_manifest, first_stage_artifacts = _load_first_stage(
        path=first_stage_run_path,
        query_ids=query_ids,
        candidate_ids=candidate_ids,
        reference_manifest_path=reference_manifest_path,
        candidate_pool=candidate_pool,
    )
    generation_config = {
        "builder": "bge-cross-encoder-prototype-max-v1",
        "model_repo": provider.model_repo,
        "model_revision": provider.model_revision,
        "provider_fingerprint": provider.fingerprint,
        "query_mapping": "paper title newline paper abstract",
        "passage_mapping": "prototype label newline prototype text",
        "prototype_pooling": "maximum raw logit per venue",
        "candidate_pool": candidate_pool,
        "top_k": top_k,
        "max_length": provider.max_length,
        "first_stage_run_sha256": first_stage_artifacts["run"]["sha256"],
        "first_stage_manifest_sha256": first_stage_artifacts["manifest"]["sha256"],
    }
    binding = build_run_binding(
        dataset_path=dataset_path,
        profiles_path=profiles_path,
        query_ids=query_ids,
        candidate_ids=candidate_ids,
        configuration=generation_config,
    )
    reference_binding = validate_reference_binding(reference_manifest_path, binding)
    run: Run = {}
    pair_count = 0
    embedded_pair_count = 0
    cached_pair_count = 0
    started = perf_counter()
    with PairScoreCache(cache_path) as cache:
        for query_index, query in enumerate(bundle.queries, 1):
            query_text = _normalize(query.title + "\n" + query.abstract)
            entries: list[tuple[str, str, str]] = []
            for item in first_stage[query.query_id]:
                passages = prototypes_by_venue.get(item.doc_id)
                if not passages:
                    raise ResearchDataError(
                        f"candidate {item.doc_id!r} has no reranker passage"
                    )
                for passage in passages:
                    prepared_query, prepared_passage = provider.prepare_pair(
                        query_text, passage
                    )
                    entries.append(
                        (
                            item.doc_id,
                            _pair_hash(prepared_query, prepared_passage),
                            prepared_passage,
                        )
                    )
            pair_count += len(entries)
            existing = cache.get_many(
                provider.fingerprint, [pair_hash for _venue, pair_hash, _text in entries]
            )
            missing: dict[str, tuple[str, str]] = {}
            for _venue_id, pair_hash, passage in entries:
                if pair_hash not in existing:
                    missing.setdefault(pair_hash, (query_text, passage))
            missing_items = list(missing.items())
            for offset in range(0, len(missing_items), provider.batch_size):
                chunk = missing_items[offset : offset + provider.batch_size]
                scores = provider.score_pairs([pair for _pair_hash, pair in chunk])
                if len(scores) != len(chunk):
                    raise ResearchDataError("cross-encoder score count mismatch")
                rows = [
                    (pair_hash, float(score))
                    for (pair_hash, _pair), score in zip(chunk, scores)
                ]
                cache.put_many(provider.fingerprint, rows)
                existing.update(rows)
                embedded_pair_count += len(rows)
            cached_pair_count += len(entries) - len(missing)
            venue_scores: dict[str, float] = {}
            for venue_id, pair_hash, _passage in entries:
                if pair_hash not in existing:
                    raise ResearchDataError("cross-encoder cache is incomplete")
                venue_scores[venue_id] = max(
                    venue_scores.get(venue_id, -math.inf), existing[pair_hash]
                )
            ranked = sorted(
                venue_scores.items(), key=lambda item: (-item[1], item[0])
            )[:top_k]
            run[query.query_id] = [
                ScoredDocument(doc_id=venue_id, score=score)
                for venue_id, score in ranked
            ]
            if progress is not None:
                progress(query_index, len(bundle.queries), pair_count, embedded_pair_count)
    scoring_total_ms = (perf_counter() - started) * 1000.0
    cache_record = _artifact_record(cache_path)
    implementation_revision = (
        "bge-cross-encoder-prototype-max-v1@" + sha256_file(Path(__file__))
    )
    first_stage_method = first_stage_manifest.get("method")
    return write_run(
        output_path,
        run,
        binding=binding,
        query_ids=query_ids,
        candidate_ids=candidate_ids,
        top_k=top_k,
        method={
            "name": "bge_reranker_v2_m3_prototype_max",
            "kind": "cross_encoder",
            "implementation": "research.reranker_runs.build_cross_encoder_run",
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
                "bge-reranker-v2-m3 raw-logit reranking with prototype max pooling"
            ),
            "official_input_protocol": {
                "input": "tokenizer(query, passage)",
                "padding": True,
                "truncation": True,
                "max_length": provider.max_length,
                "score": "AutoModelForSequenceClassification logits",
                "sigmoid": False,
                "deterministic_algorithms": True,
                "candidate_surrogate_disclosure": (
                    "venue prototypes are passages; maximum prototype logit is the "
                    "venue score"
                ),
            },
            "model_assets": dict(provider.asset_record),
            "first_stage": {
                "artifacts": first_stage_artifacts,
                "method": dict(first_stage_method)
                if isinstance(first_stage_method, Mapping)
                else {},
                "candidate_pool": candidate_pool,
            },
            "reference_binding_manifest": reference_binding,
            "pair_score_cache": cache_record,
            "coverage_details": {
                "query_count": len(query_ids),
                "candidate_pool": candidate_pool,
                "pair_count": pair_count,
                "newly_scored_pair_count": embedded_pair_count,
                "cached_pair_count": cached_pair_count,
                "prototype_count": len(prototypes),
            },
            "execution": {
                "offline_only": True,
                "search_free": True,
                "local_files_only": True,
                "external_api_calls": 0,
                "estimated_external_cost_usd": 0.0,
                "device": provider.device,
                "scoring_total_ms": scoring_total_ms,
                "mean_ms_per_query": scoring_total_ms / len(query_ids),
                "failed_query_count": 0,
            },
        },
    )
