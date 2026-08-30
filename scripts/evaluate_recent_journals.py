#!/usr/bin/env python3
"""Evaluate journal recommendations against a recent-paper JSONL dataset.

The evaluator talks directly to one long-lived ``where_paper_go.worker``
process.  This keeps the property graph, exact vectors, and LightRAG runtime
resident while preserving the same mandatory retrieval path used by the web
application.  Each completed case is appended and fsynced before the next one
starts, so an interrupted evaluation can resume safely.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import select
import shutil
import ssl
import stat
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
import unicodedata
import urllib.parse
import uuid

try:  # pragma: no cover - the supported evaluator host is Linux.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from where_paper_go.paths import DATA_DIR, DEFAULT_CONFIG_PATH, PROJECT_ROOT
from where_paper_go.lightrag import (
    MANIFEST_FILE as LIGHTRAG_MANIFEST_FILE,
    QUERY_STORAGE_FILES,
    default_lightrag_working_dir,
)
from where_paper_go.recommender import (
    CURATED_SCOPE_FILE,
    DATA_FILES,
    journal_identity_name,
    normalize_space,
    valid_issn_token,
)
from where_paper_go.graph_index import (
    graph_source_digest,
    inspect_graph,
    vector_path_for_graph,
)
from where_paper_go.external_call_budget import (
    BUDGET_ENV,
    LEDGER_ENV,
    RUN_ID_ENV,
    external_call_ledger_status,
    initialize_external_call_ledger,
)


SCHEMA_VERSION = "2"
TARGETS = ("JCR-Q1", "JCR-Q2", "JCR-Q3", "JCR-Q4")
TRACKS = ("title_abstract", "abstract_only")
FINAL_K = 10
DEFAULT_PRELIMINARY_K = 40
RUN_MANIFEST_FILE = "run_manifest.json"
RUN_LOCK_FILE = ".evaluation.lock"
RUN_MANIFEST_SCHEMA_VERSION = "2"
GENERATION_DIR = "raw_segments"
RUNTIME_CACHE_DIR = "runtime_cache"
SOURCE_EVIDENCE_DIR = "source_evidence"
AUTHORIZATION_REGISTRY_SCHEMA_VERSION = "1"
CLOSEOUT_ANCHOR_SCHEMA_VERSION = "1"
AUTHORIZATION_GRANT_SCHEMA_VERSION = "1"
DEFAULT_AUTHORIZATION_REGISTRY_DIR = (
    PROJECT_ROOT / "benchmark_artifacts" / ".recent_journal_authorization_registry"
)
FORMAL_MODE = "formal_500_full_denominator"
DIAGNOSTIC_MODE = "diagnostic_nonformal"
LIGHTRAG_WORKSPACE_FILES = (LIGHTRAG_MANIFEST_FILE, *QUERY_STORAGE_FILES)
MAX_WORKER_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_SEGMENT_RECORD_BYTES = 64 * 1024 * 1024


class EvaluationError(RuntimeError):
    """Raised for invalid datasets, worker failures, or corrupt output."""


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    doi: str
    title: str
    abstract: str
    published_date: str
    gold_entity_id: int | None
    gold_journal_name: str
    gold_issns: tuple[str, ...]
    gold_jcr_quartile: str
    primary_field: str
    mapping_method: str
    source_url: str

    @property
    def catalog_covered(self) -> bool:
        return self.gold_entity_id is not None

    def query_for(self, track: str) -> str:
        if track == "title_abstract":
            return normalize_space(f"Title: {self.title}\nAbstract: {self.abstract}")
        if track == "abstract_only":
            return normalize_space(self.abstract)
        raise ValueError(f"unknown evaluation track: {track}")

    def public_metadata(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "doi": self.doi,
            "title": self.title,
            "abstract": self.abstract,
            "published_date": self.published_date,
            "gold_entity_id": self.gold_entity_id,
            "gold_journal_name": self.gold_journal_name,
            "gold_issns": list(self.gold_issns),
            "gold_jcr_quartile": self.gold_jcr_quartile,
            "primary_field": self.primary_field,
            "mapping_method": self.mapping_method,
            "source_url": self.source_url,
            "catalog_covered": self.catalog_covered,
        }


def _nested(mapping: Mapping[str, Any], *path: str) -> Any:
    value: Any = mapping
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _first(mapping: Mapping[str, Any], *paths: str | tuple[str, ...]) -> Any:
    for path in paths:
        keys = (path,) if isinstance(path, str) else path
        value = _nested(mapping, *keys)
        if value is not None and value != "":
            return value
    return None


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _string_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        values: Iterable[Any] = re.split(r"[;,|\s]+", value)
    elif isinstance(value, Sequence):
        values = value
    else:
        return ()
    result: list[str] = []
    for item in values:
        text = normalize_space(str(item))
        if text and text not in result:
            result.append(text)
    return tuple(result)


def case_from_payload(payload: Mapping[str, Any], line_number: int) -> BenchmarkCase:
    case_id = normalize_space(
        str(_first(payload, "case_id", "sample_id", "paper_id", "id") or "")
    )
    title = normalize_space(str(_first(payload, "title", ("paper", "title")) or ""))
    abstract = normalize_space(
        str(_first(payload, "abstract", ("paper", "abstract")) or "")
    )
    gold_name = normalize_space(
        str(
            _first(
                payload,
                "gold_journal_name",
                ("journal", "name"),
                "journal_name",
            )
            or ""
        )
    )
    if not case_id:
        raise EvaluationError(f"dataset line {line_number}: missing case_id")
    if not title:
        raise EvaluationError(f"dataset line {line_number}: missing title")
    if not abstract:
        raise EvaluationError(f"dataset line {line_number}: missing abstract")
    if not gold_name:
        raise EvaluationError(f"dataset line {line_number}: missing gold journal name")
    return BenchmarkCase(
        case_id=case_id,
        doi=normalize_doi(str(_first(payload, "doi", ("paper", "doi")) or "")),
        title=title,
        abstract=abstract,
        published_date=normalize_space(
            str(_first(payload, "published_date", "publication_date") or "")
        ),
        gold_entity_id=_optional_int(
            _first(payload, "gold_entity_id", ("journal", "entity_id"))
        ),
        gold_journal_name=gold_name,
        gold_issns=_string_list(
            _first(payload, "gold_issns", ("journal", "issns"), "gold_issn")
        ),
        gold_jcr_quartile=normalize_space(
            str(
                _first(
                    payload,
                    "gold_jcr_quartile",
                    ("journal", "jcr_quartile"),
                    "jcr_quartile",
                )
                or "unknown"
            )
        ).upper(),
        primary_field=normalize_space(
            str(
                _first(
                    payload,
                    "primary_field",
                    "broad_field",
                    "field",
                    ("paper", "field"),
                )
                or "unknown"
            )
        ),
        mapping_method=normalize_space(str(payload.get("mapping_method") or "")),
        source_url=normalize_space(str(payload.get("source_url") or "")),
    )


def load_dataset(path: Path) -> list[BenchmarkCase]:
    cases: list[BenchmarkCase] = []
    seen: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise EvaluationError(f"cannot read dataset: {path}") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvaluationError(
                f"dataset line {line_number}: invalid JSON: {exc.msg}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise EvaluationError(f"dataset line {line_number}: expected an object")
        case = case_from_payload(payload, line_number)
        if case.case_id in seen:
            raise EvaluationError(f"dataset line {line_number}: duplicate case_id {case.case_id!r}")
        seen.add(case.case_id)
        cases.append(case)
    if not cases:
        raise EvaluationError("dataset contains no cases")
    return cases


def normalize_doi(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    text = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", text)
    return text.strip().rstrip(".,;)")


def _match_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9\u3400-\u9fff]+", " ", text).split())


def normalized_title_similarity(left: str, right: str) -> float:
    left_text, right_text = _match_text(left), _match_text(right)
    if not left_text or not right_text:
        return 0.0
    return SequenceMatcher(None, left_text, right_text).ratio()


def audit_search_leakage(
    evidence: Sequence[Mapping[str, Any]],
    *,
    doi: str,
    gold_journal_name: str,
    paper_title: str,
    title_similarity_threshold: float = 0.90,
) -> dict[str, Any]:
    """Audit evidence without treating ordinary topical overlap as a leak."""

    normalized_doi = normalize_doi(doi)
    journal = _match_text(gold_journal_name)
    normalized_paper_title = _match_text(paper_title)
    matches: list[dict[str, Any]] = []
    reason_counts = {"doi": 0, "title": 0, "gold_journal": 0}
    for item in evidence:
        title = str(item.get("title") or "")
        url = str(item.get("url") or "")
        snippet = str(item.get("snippet") or "")
        combined_raw = " ".join((title, url, snippet))
        combined = _match_text(combined_raw)
        reasons: list[str] = []
        if normalized_doi and normalized_doi in combined_raw.casefold():
            reasons.append("doi")
        similarity = normalized_title_similarity(title, paper_title)
        if normalized_paper_title and (
            similarity >= title_similarity_threshold
            or (len(normalized_paper_title) >= 24 and normalized_paper_title in combined)
        ):
            reasons.append("title")
        if journal and len(journal) >= 4 and journal in combined:
            reasons.append("gold_journal")
        if not reasons:
            continue
        for reason in reasons:
            reason_counts[reason] += 1
        matches.append(
            {
                "url": url,
                "query": str(item.get("query") or ""),
                "reasons": reasons,
                "title_similarity": round(similarity, 4),
            }
        )
    article_leak = bool(reason_counts["doi"] or reason_counts["title"])
    return {
        "any_leak": bool(matches),
        "article_leak": article_leak,
        "gold_journal_mentioned": bool(reason_counts["gold_journal"]),
        "reason_counts": reason_counts,
        "matches": matches,
    }


def prediction_matches_gold(
    prediction: Mapping[str, Any],
    gold_entity_id: int | None,
    gold_journal_name: str,
) -> bool:
    predicted_id = _optional_int(prediction.get("entity_id"))
    # Current benchmark records must resolve to a current catalog entity.  A
    # missing ID is an uncovered case, never a name-only hit.
    return bool(
        gold_entity_id is not None
        and predicted_id is not None
        and predicted_id == gold_entity_id
    )


def gold_rank(
    predictions: Sequence[Mapping[str, Any]],
    gold_entity_id: int | None,
    gold_journal_name: str,
) -> int | None:
    for rank, prediction in enumerate(predictions, 1):
        if prediction_matches_gold(prediction, gold_entity_id, gold_journal_name):
            return rank
    return None


def ranking_metrics(ranks: Sequence[int | None], *, max_k: int = FINAL_K) -> dict[str, Any]:
    denominator = len(ranks)
    result: dict[str, Any] = {"count": denominator}
    for k in (1, 3, 5, 10):
        hits = sum(rank is not None and rank <= min(k, max_k) for rank in ranks)
        result[f"hit_at_{k}"] = hits / denominator if denominator else None
        result[f"hits_at_{k}"] = hits
    reciprocal_sum = sum(
        1.0 / rank for rank in ranks if rank is not None and rank <= max_k
    )
    result[f"mrr_at_{max_k}"] = reciprocal_sum / denominator if denominator else None
    result["reciprocal_rank_sum"] = reciprocal_sum
    return result


def preliminary_metrics(
    ranks: Sequence[int | None], *, preliminary_k: int
) -> dict[str, Any]:
    """Return normal Top-10 metrics plus the wider preliminary recall cutoff."""

    result = ranking_metrics(ranks)
    hits = sum(rank is not None and rank <= preliminary_k for rank in ranks)
    result[f"hit_at_{preliminary_k}"] = hits / len(ranks) if ranks else None
    result[f"hits_at_{preliminary_k}"] = hits
    return result


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _rank_from_record(record: Mapping[str, Any], key: str) -> int | None:
    value = record.get(key)
    return _optional_int(value)


def summarize_records(
    records: Sequence[Mapping[str, Any]],
    *,
    preliminary_k: int = DEFAULT_PRELIMINARY_K,
) -> dict[str, Any]:
    """Summarize one track or stratum; failures and uncovered cases are misses."""

    final_ranks = [_rank_from_record(record, "final_gold_rank") for record in records]
    preliminary_ranks = [
        _rank_from_record(record, "preliminary_gold_rank") for record in records
    ]
    recall_pool_ranks = [
        _rank_from_record(record, "recall_pool_gold_rank") for record in records
    ]
    covered = [record for record in records if bool(record.get("catalog_covered"))]
    completed = [record for record in records if record.get("status") == "ok"]
    covered_final = [_rank_from_record(record, "final_gold_rank") for record in covered]
    covered_preliminary = [
        _rank_from_record(record, "preliminary_gold_rank") for record in covered
    ]
    latency = [
        float(record["latency_ms"])
        for record in completed
        if isinstance(record.get("latency_ms"), (int, float))
    ]
    preliminary_latency = [
        float(record["preliminary_latency_ms"])
        for record in completed
        if isinstance(record.get("preliminary_latency_ms"), (int, float))
    ]
    article_leaks = sum(
        bool(_nested(record, "leakage", "article_leak")) for record in completed
    )
    journal_mentions = sum(
        bool(_nested(record, "leakage", "gold_journal_mentioned"))
        for record in completed
    )
    no_search_leak = [
        record
        for record in completed
        if not bool(_nested(record, "leakage", "any_leak"))
    ]
    no_leak_final = [
        _rank_from_record(record, "final_gold_rank") for record in no_search_leak
    ]
    no_leak_preliminary = [
        _rank_from_record(record, "preliminary_gold_rank")
        for record in no_search_leak
    ]
    leakage_safe_final = [
        (
            _rank_from_record(record, "final_gold_rank")
            if record.get("status") == "ok"
            and not bool(_nested(record, "leakage", "any_leak"))
            else None
        )
        for record in records
    ]
    leakage_safe_preliminary = [
        (
            _rank_from_record(record, "preliminary_gold_rank")
            if record.get("status") == "ok"
            and not bool(_nested(record, "leakage", "any_leak"))
            else None
        )
        for record in records
    ]
    preliminary = preliminary_metrics(
        preliminary_ranks, preliminary_k=preliminary_k
    )
    recall_pool = preliminary_metrics(
        recall_pool_ranks, preliminary_k=preliminary_k
    )
    covered_prelim_metrics = preliminary_metrics(
        covered_preliminary, preliminary_k=preliminary_k
    )
    final = ranking_metrics(final_ranks)
    rerank_delta = {
        f"hit_at_{k}": (
            final[f"hit_at_{k}"] - preliminary[f"hit_at_{k}"]
            if final[f"hit_at_{k}"] is not None
            and preliminary[f"hit_at_{k}"] is not None
            else None
        )
        for k in (1, 3, 5, 10)
    }
    rerank_delta["mrr_at_10"] = (
        final["mrr_at_10"] - preliminary["mrr_at_10"]
        if final["mrr_at_10"] is not None and preliminary["mrr_at_10"] is not None
        else None
    )
    return {
        "case_count": len(records),
        "catalog_covered": len(covered),
        "catalog_coverage": len(covered) / len(records) if records else None,
        "completed": len(completed),
        "errors": len(records) - len(completed),
        "error_rate": (len(records) - len(completed)) / len(records) if records else None,
        "final": final,
        "preliminary": preliminary,
        "recall_pool": recall_pool,
        "rerank_delta": rerank_delta,
        "coverage_conditioned": {
            "final": ranking_metrics(covered_final),
            "preliminary": covered_prelim_metrics,
        },
        "no_search_leak": {
            "case_count": len(no_search_leak),
            "excluded_completed_cases": len(completed) - len(no_search_leak),
            "final": ranking_metrics(no_leak_final),
            "preliminary": preliminary_metrics(
                no_leak_preliminary, preliminary_k=preliminary_k
            ),
        },
        "search_leakage_safe_lower_bound": {
            "policy": "search-leaked and failed cases count as misses",
            "final": ranking_metrics(leakage_safe_final),
            "preliminary": preliminary_metrics(
                leakage_safe_preliminary, preliminary_k=preliminary_k
            ),
        },
        "latency_ms": {
            "count": len(latency),
            "median": _percentile(latency, 0.5),
            "p90": _percentile(latency, 0.9),
        },
        "preliminary_latency_ms": {
            "count": len(preliminary_latency),
            "median": _percentile(preliminary_latency, 0.5),
            "p90": _percentile(preliminary_latency, 0.9),
        },
        "leakage": {
            "article_leak_cases": article_leaks,
            "article_leak_rate": article_leaks / len(completed) if completed else None,
            "gold_journal_mentioned_cases": journal_mentions,
            "gold_journal_mentioned_rate": journal_mentions / len(completed)
            if completed
            else None,
        },
    }


def stratified_summary(
    records: Sequence[Mapping[str, Any]],
    key: str,
    *,
    preliminary_k: int = DEFAULT_PRELIMINARY_K,
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        value = normalize_space(str(record.get(key) or "unknown")) or "unknown"
        groups.setdefault(value, []).append(record)
    return {
        value: summarize_records(group, preliminary_k=preliminary_k)
        for value, group in sorted(groups.items(), key=lambda item: item[0].casefold())
    }


class PersistentWorker:
    """Minimal unbuffered client for the worker's line-delimited protocol."""

    def __init__(
        self,
        *,
        cwd: Path = PROJECT_ROOT,
        startup_timeout: int = 240,
        external_call_ledger: Path | None = None,
        external_call_budget: int | None = None,
        run_id: str | None = None,
        api_cache_dir: Path | None = None,
        query_embedding_cache: Path | None = None,
        lightrag_embedding_cache: Path | None = None,
        lightrag_working_dir: Path | None = None,
        graph_path: Path | None = None,
        api_config_snapshot: Path | None = None,
        verify_bindings: Callable[[], None] | None = None,
    ):
        self.cwd = cwd
        self.startup_timeout = startup_timeout
        self.external_call_ledger = external_call_ledger
        self.external_call_budget = external_call_budget
        self.run_id = run_id
        self.api_cache_dir = api_cache_dir
        self.query_embedding_cache = query_embedding_cache
        self.lightrag_embedding_cache = lightrag_embedding_cache
        self.lightrag_working_dir = lightrag_working_dir
        self.graph_path = graph_path
        self.api_config_snapshot = api_config_snapshot
        self.verify_bindings = verify_bindings
        self.process: subprocess.Popen[bytes] | None = None
        self._read_buffer = bytearray()
        self.preload_ms: int | None = None

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        self.close()
        if self.verify_bindings is not None:
            self.verify_bindings()
        worker_env = os.environ.copy()
        worker_env.update(
            {
                "PYTHONHASHSEED": "0",
                "OMP_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
                "OPENBLAS_NUM_THREADS": "1",
                "NUMEXPR_NUM_THREADS": "1",
            }
        )
        if self.external_call_ledger is not None:
            if self.external_call_budget is None or not self.run_id:
                raise EvaluationError("worker external-call limiter is incomplete")
            worker_env.update(
                {
                    LEDGER_ENV: str(self.external_call_ledger.resolve()),
                    BUDGET_ENV: str(self.external_call_budget),
                    RUN_ID_ENV: self.run_id,
                }
            )
        runtime_values = (
            self.api_cache_dir,
            self.query_embedding_cache,
            self.lightrag_embedding_cache,
            self.lightrag_working_dir,
            self.graph_path,
            self.api_config_snapshot,
        )
        if any(value is not None for value in runtime_values):
            if not all(value is not None for value in runtime_values):
                raise EvaluationError("worker runtime-cache binding is incomplete")
            worker_env.update(
                {
                    "WPG_API_CACHE_DIR": str(self.api_cache_dir.resolve()),
                    "WPG_QUERY_EMBEDDING_CACHE": str(
                        self.query_embedding_cache.resolve()
                    ),
                    "WPG_LIGHTRAG_EMBEDDING_CACHE": str(
                        self.lightrag_embedding_cache.resolve()
                    ),
                    "WPG_LIGHTRAG_WORKING_DIR": str(
                        self.lightrag_working_dir.resolve()
                    ),
                    "WPG_GRAPH_PATH": str(self.graph_path.resolve()),
                    "WPG_STRICT_GRAPH_READ_ONLY": "1",
                    "WPG_API_CONFIG": str(self.api_config_snapshot.resolve()),
                }
            )
        self.process = subprocess.Popen(
            [sys.executable, "-m", "where_paper_go.worker"],
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # Third-party startup chatter is not part of the protocol.  Avoid a
            # PIPE here: an undrained stderr could deadlock a long benchmark.
            stderr=subprocess.DEVNULL,
            bufsize=0,
            env=worker_env,
        )
        ready = self._read_message(self.startup_timeout)
        if not ready.get("ready"):
            self.close()
            raise EvaluationError(f"worker preload failed: {ready.get('error', 'unknown')}")
        self.preload_ms = _optional_int(ready.get("preload_ms"))

    def _read_message(self, timeout: float) -> dict[str, Any]:
        process = self.process
        if process is None or process.stdout is None:
            raise EvaluationError("worker is not running")
        deadline = time.monotonic() + timeout
        while True:
            newline = self._read_buffer.find(b"\n")
            if newline >= 0:
                if newline > MAX_WORKER_MESSAGE_BYTES:
                    raise EvaluationError("worker response exceeds protocol size limit")
                raw = bytes(self._read_buffer[:newline])
                del self._read_buffer[: newline + 1]
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise EvaluationError("worker returned invalid JSON") from exc
                if not isinstance(payload, dict):
                    raise EvaluationError("worker response is not an object")
                return payload
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EvaluationError("worker response timed out")
            readable, _writable, _exceptional = select.select(
                [process.stdout.fileno()], [], [], remaining
            )
            if not readable:
                raise EvaluationError("worker response timed out")
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                code = process.poll()
                raise EvaluationError(f"worker exited unexpectedly (code={code})")
            self._read_buffer.extend(chunk)
            if len(self._read_buffer) > MAX_WORKER_MESSAGE_BYTES:
                raise EvaluationError("worker response exceeds protocol size limit")

    def search_stream(
        self, argv: Sequence[str], *, timeout: int
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        self.start()
        process = self.process
        assert process is not None and process.stdin is not None
        request_id = uuid.uuid4().hex
        request = {
            "op": "search_stream",
            "request_id": request_id,
            "argv": list(argv),
        }
        encoded = json.dumps(
            request, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8") + b"\n"
        if len(encoded) > MAX_WORKER_MESSAGE_BYTES:
            raise EvaluationError("worker request exceeds protocol size limit")
        try:
            process.stdin.write(encoded)
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self.close()
            raise EvaluationError("worker input pipe closed") from exc
        deadline = time.monotonic() + timeout
        events: list[dict[str, Any]] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EvaluationError("worker search timed out")
            response = self._read_message(remaining)
            if response.get("request_id") != request_id:
                raise EvaluationError("worker response request_id mismatch")
            event = response.get("event")
            if isinstance(event, dict):
                events.append(event)
                continue
            if response.get("final"):
                return response, events

    def close(self) -> None:
        process, self.process = self.process, None
        self._read_buffer.clear()
        if process is None:
            return
        if process.poll() is None:
            try:
                if process.stdin is not None:
                    process.stdin.write(
                        json.dumps(
                            {"op": "shutdown", "request_id": uuid.uuid4().hex}
                        ).encode("utf-8")
                        + b"\n"
                    )
                    process.stdin.flush()
                process.wait(timeout=20)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()

    def __enter__(self) -> "PersistentWorker":
        self.start()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()


def _prediction_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("results"), list):
        return []
    return [dict(item) for item in payload["results"] if isinstance(item, Mapping)]


def _results_event(
    events: Sequence[Mapping[str, Any]], phase: str
) -> Mapping[str, Any] | None:
    for event in events:
        if event.get("type") == "results" and event.get("phase") == phase:
            payload = event.get("payload")
            if isinstance(payload, Mapping):
                return payload
    return None


def _preliminary_event(events: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    return _results_event(events, "preliminary")


def build_recommender_argv(
    query: str,
    *,
    api_config: Path,
    preliminary_k: int,
    api_timeout: int,
    skip_explanations: bool = False,
    api_cache_dir: Path | None = None,
    query_embedding_cache: Path | None = None,
    lightrag_embedding_cache: Path | None = None,
    lightrag_working_dir: Path | None = None,
    graph_path: Path | None = None,
) -> list[str]:
    argv = [
        "--query",
        query,
        "--record-type",
        "journal",
        "--limit",
        str(preliminary_k),
        "--format",
        "json",
        "--api-config",
        str(api_config.resolve()),
        "--api-timeout",
        str(api_timeout),
    ]
    for target in TARGETS:
        argv.extend(("--target", target))
    runtime_values = (
        api_cache_dir,
        query_embedding_cache,
        lightrag_embedding_cache,
        lightrag_working_dir,
        graph_path,
    )
    if any(value is not None for value in runtime_values):
        if not all(value is not None for value in runtime_values):
            raise EvaluationError("recommender runtime-cache binding is incomplete")
        argv.extend(("--api-cache-dir", str(api_cache_dir.resolve())))
        argv.extend(("--query-embedding-cache", str(query_embedding_cache.resolve())))
        argv.extend(("--lightrag-embedding-cache", str(lightrag_embedding_cache.resolve())))
        argv.extend(("--lightrag-working-dir", str(lightrag_working_dir.resolve())))
        argv.extend(("--graph", str(graph_path.resolve())))
    if skip_explanations:
        argv.append("--no-api-explanations")
    return argv


def resolve_gold_entity_ids(cases: Sequence[BenchmarkCase]) -> list[BenchmarkCase]:
    """Resolve stable benchmark ISSNs against the current catalog every run."""

    from where_paper_go.paths import DATA_DIR
    from where_paper_go.recommender import (
        build_candidates,
        load_records,
        parse_targets,
        valid_issn_token,
    )

    owners: dict[str, set[int]] = {}
    for candidate in build_candidates(
        load_records(DATA_DIR),
        parse_targets(list(TARGETS)),
        record_type="journal",
    ):
        entity_id = min(record.row_id for record in candidate.records)
        for record in candidate.records:
            for value in (record.issn, record.eissn):
                token = valid_issn_token(value)
                if token:
                    owners.setdefault(token, set()).add(entity_id)

    resolved: list[BenchmarkCase] = []
    for case in cases:
        matched_ids: set[int] = set()
        for value in case.gold_issns:
            token = valid_issn_token(value)
            if token:
                matched_ids.update(owners.get(token, ()))
        current_id = next(iter(matched_ids)) if len(matched_ids) == 1 else None
        resolved.append(
            BenchmarkCase(
                **{
                    **case.__dict__,
                    "gold_entity_id": current_id,
                    "mapping_method": "current_catalog_exact_unique_issn",
                }
            )
        )
    return resolved


def evaluate_case(
    worker: PersistentWorker,
    case: BenchmarkCase,
    track: str,
    *,
    run_id: str,
    api_config: Path,
    preliminary_k: int,
    api_timeout: int,
    worker_timeout: int,
    title_similarity_threshold: float,
    skip_explanations: bool = False,
    api_cache_dir: Path | None = None,
    query_embedding_cache: Path | None = None,
    lightrag_embedding_cache: Path | None = None,
    lightrag_working_dir: Path | None = None,
    graph_path: Path | None = None,
) -> dict[str, Any]:
    query = case.query_for(track)
    started = time.perf_counter()
    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "case_id": case.case_id,
        "track": track,
        **case.public_metadata(),
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        response, events = worker.search_stream(
            build_recommender_argv(
                query,
                api_config=api_config,
                preliminary_k=preliminary_k,
                api_timeout=api_timeout,
                skip_explanations=skip_explanations,
                api_cache_dir=api_cache_dir,
                query_embedding_cache=query_embedding_cache,
                lightrag_embedding_cache=lightrag_embedding_cache,
                lightrag_working_dir=lightrag_working_dir,
                graph_path=graph_path,
            ),
            timeout=worker_timeout,
        )
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        if int(response.get("returncode", 1)) != 0:
            return {
                **base,
                "status": "error",
                "latency_ms": latency_ms,
                "worker_elapsed_ms": response.get("worker_elapsed_ms"),
                "error": str(response.get("stderr") or "worker search failed")[-8000:],
            }
        try:
            final_payload = json.loads(str(response.get("stdout") or ""))
        except json.JSONDecodeError as exc:
            raise EvaluationError("recommender returned invalid JSON") from exc
        if not isinstance(final_payload, Mapping):
            raise EvaluationError("recommender result is not an object")
        preliminary_payload = _preliminary_event(events)
        if preliminary_payload is None:
            raise EvaluationError("worker did not emit a preliminary results event")
        recall_pool_payload = _results_event(events, "recall_pool")
        if recall_pool_payload is None:
            raise EvaluationError("worker did not emit a multichannel recall-pool event")
        final_predictions = _prediction_rows(final_payload)
        preliminary_predictions = _prediction_rows(preliminary_payload)
        recall_pool_predictions = _prediction_rows(recall_pool_payload)
        final_rank = gold_rank(
            final_predictions, case.gold_entity_id, case.gold_journal_name
        )
        preliminary_rank = gold_rank(
            preliminary_predictions, case.gold_entity_id, case.gold_journal_name
        )
        recall_pool_rank = gold_rank(
            recall_pool_predictions, case.gold_entity_id, case.gold_journal_name
        )
        evidence = _nested(final_payload, "api_assisted_search", "search_results")
        if not isinstance(evidence, list):
            evidence = []
        leakage = audit_search_leakage(
            [item for item in evidence if isinstance(item, Mapping)],
            doi=case.doi,
            gold_journal_name=case.gold_journal_name,
            paper_title=case.title,
            title_similarity_threshold=title_similarity_threshold,
        )
        preliminary_latency = None
        for event in events:
            if event.get("type") == "results" and event.get("phase") == "preliminary":
                if isinstance(event.get("elapsed_ms"), (int, float)):
                    preliminary_latency = float(event["elapsed_ms"])
                break
        progress_events = [
            dict(event)
            for event in events
            if event.get("type") != "results"
        ]
        return {
            **base,
            "status": "ok",
            "query": query,
            "latency_ms": latency_ms,
            "preliminary_latency_ms": preliminary_latency,
            "worker_elapsed_ms": response.get("worker_elapsed_ms"),
            "preliminary_gold_rank": preliminary_rank,
            "recall_pool_gold_rank": recall_pool_rank,
            "final_gold_rank": final_rank,
            "preliminary_prediction_count": len(preliminary_predictions),
            "recall_pool_prediction_count": len(recall_pool_predictions),
            "final_prediction_count": len(final_predictions),
            "leakage": leakage,
            "progress_events": progress_events,
            "preliminary_payload": dict(preliminary_payload or {}),
            "recall_pool_payload": dict(recall_pool_payload or {}),
            "final_payload": dict(final_payload),
        }
    except Exception as exc:
        if isinstance(exc, EvaluationError):
            # A timeout or malformed protocol reply leaves unread bytes whose
            # request ID could poison the next case. Restart cleanly next time.
            worker.close()
        return {
            **base,
            "status": "error",
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "error": f"{type(exc).__name__}: {exc}",
        }


def append_jsonl_durable(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8") + b"\n"
    if len(encoded) > MAX_SEGMENT_RECORD_BYTES:
        raise EvaluationError("evaluation record exceeds the bounded segment protocol")
    flags = os.O_WRONLY | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise EvaluationError("generation segment must be a private regular file")
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short JSONL append")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _optional_file_sha256(path: Path) -> str:
    try:
        return _file_sha256(path)
    except OSError:
        return "missing"


def _decimal_argument(value: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("expected a finite decimal number") from exc
    if not parsed.is_finite() or parsed < 0:
        raise argparse.ArgumentTypeError("expected a finite non-negative decimal")
    return parsed


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if not normalized else format(normalized, "f")


def _read_api_config_for_plan(path: Path) -> dict[str, Any]:
    """Read and validate only the non-secret shape needed for a dry-run plan."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"cannot read API config for planning: {path}") from exc
    if not isinstance(payload, dict):
        raise EvaluationError("API config must be a JSON object")
    for section in ("llm", "embedding", "search"):
        if not isinstance(payload.get(section), Mapping):
            raise EvaluationError(f"API config requires an object-valued {section} section")
    return payload


def _config_int(
    mapping: Mapping[str, Any], name: str, default: int, *, minimum: int = 0
) -> int:
    try:
        value = int(mapping.get(name, default))
    except (TypeError, ValueError) as exc:
        raise EvaluationError(f"API config {name} must be an integer") from exc
    if value < minimum:
        raise EvaluationError(f"API config {name} must be at least {minimum}")
    return value


def _api_cache_inventory(
    cache_dir: Path,
    query_embedding_cache: Path,
    lightrag_embedding_cache: Path,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "cache_dir": str(cache_dir.resolve()),
        "exact_case_track_attribution": False,
        "note": (
            "LLM plan output determines later Search/rerank/semantic-vector cache keys; "
            "dry-run therefore reports a read-only inventory, not false exact hits."
        ),
    }
    for kind in ("llm", "search"):
        directory = cache_dir / kind
        try:
            count = sum(1 for path in directory.glob("*.json") if path.is_file())
        except OSError:
            count = 0
        result[f"{kind}_entry_count"] = count
    for name, path in (
        ("exact_query_embedding", query_embedding_cache),
        ("lightrag_embedding", lightrag_embedding_cache),
    ):
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        result[f"{name}_cache"] = {
            "path": str(path.resolve()),
            "exists": size is not None,
            "bytes": size,
            "exact_case_track_attribution": False,
        }
    return result


def _tavily_quota_snapshot(search: Mapping[str, Any]) -> dict[str, Any]:
    from where_paper_go.tavily_pool import TavilyKeyPool, TavilyKeyPoolError

    try:
        pool = TavilyKeyPool.from_config(search)
        audit = pool.audit_snapshot()
    except TavilyKeyPoolError as exc:
        return {
            "provider": "tavily",
            "state_status": "unreadable_fail_closed",
            "error_type": type(exc).__name__,
        }
    copies = audit.get("copies") if isinstance(audit, Mapping) else None
    healthy_dual_copy = isinstance(copies, Mapping) and all(
        isinstance(copies.get(name), Mapping)
        and copies[name].get("present") is True
        and copies[name].get("valid") is True
        and copies[name].get("revision") == audit.get("state_revision")
        for name in ("primary", "backup")
    )
    return {
        "provider": "tavily",
        "state_status": (
            "readable_durable_dual_copy"
            if healthy_dual_copy
            else "readable_degraded_fail_closed"
        ),
        "state_paths": {
            "primary": str(pool.state_file.resolve()),
            "backup": str(pool.backup_file.resolve()),
            "lock": str(pool.lock_file.resolve()),
        },
        **audit,
    }


def _assert_quota_snapshot_monotonic(
    current: Mapping[str, Any], previous: Mapping[str, Any], *, label: str
) -> None:
    if current.get("provider") != previous.get("provider"):
        raise EvaluationError(f"{label} quota provider drifted")
    if current.get("provider") != "tavily":
        if dict(current) != dict(previous):
            raise EvaluationError(f"{label} non-Tavily quota binding drifted")
        return
    if (
        current.get("state_status") != "readable_durable_dual_copy"
        or previous.get("state_status") != "readable_durable_dual_copy"
    ):
        raise EvaluationError(f"{label} Tavily quota ledger is not auditable")
    for key in (
        "configured_keyset_sha256",
        "key_count",
        "quota_per_key",
        "total_capacity",
        "state_paths",
    ):
        if current.get(key) != previous.get(key):
            raise EvaluationError(f"{label} Tavily quota identity drifted: {key}")
    for key in ("state_revision", "used"):
        current_value = current.get(key)
        previous_value = previous.get(key)
        if (
            not isinstance(current_value, int)
            or isinstance(current_value, bool)
            or not isinstance(previous_value, int)
            or isinstance(previous_value, bool)
            or current_value < previous_value
        ):
            raise EvaluationError(f"{label} Tavily quota {key} moved backwards")


def _external_quota_snapshot(api_config: Mapping[str, Any]) -> dict[str, Any]:
    search = api_config.get("search")
    if not isinstance(search, Mapping):
        raise EvaluationError("API config requires an object-valued search section")
    provider = str(search.get("provider") or "").strip().lower()
    if provider == "tavily":
        return _tavily_quota_snapshot(search)
    return {"provider": provider, "status": "provider_has_no_local_quota_ledger"}


def estimate_external_attempts(
    *,
    case_track_count: int,
    api_config: Mapping[str, Any],
    skip_explanations: bool,
) -> dict[str, Any]:
    """Return a transparent configured-path estimate, never a fabricated bound."""

    llm = dict(api_config["llm"])
    embedding = dict(api_config["embedding"])
    search = dict(api_config["search"])
    llm_attempts = _config_int(llm, "max_retries", 2) + 1
    rerank_batch_size = max(
        5, min(40, _config_int(llm, "rerank_batch_size", 15, minimum=1))
    )
    rerank_concurrency = max(
        1, min(2, _config_int(llm, "rerank_concurrency", 2, minimum=1))
    )
    rerank_batches = math.ceil(40 / rerank_batch_size)
    # Concurrent batch failures are retried once sequentially by the product.
    rerank_logical_max = (
        rerank_batches * 2
        if rerank_concurrency > 1 and rerank_batches > 1
        else rerank_batches
    )
    llm_per_case = (
        1 + rerank_logical_max + (0 if skip_explanations else 1)
    ) * llm_attempts

    provider = str(search.get("provider") or "").strip().lower()
    if provider == "tavily":
        from where_paper_go.tavily_pool import configured_tavily_keys

        key_count = len(configured_tavily_keys(search))
        if key_count < 1:
            raise EvaluationError(
                "tavily has no configured keys; bounded live planning fails closed"
            )
        max_key_attempts = min(
            key_count,
            _config_int(search, "max_key_attempts", 3, minimum=1),
        )
        direct_fallback = search.get("direct_fallback", True)
        if isinstance(direct_fallback, str):
            direct_fallback = direct_fallback.strip().lower() not in {
                "0",
                "false",
                "no",
                "off",
            }
        search_per_query = max_key_attempts * (2 if direct_fallback else 1)
    elif provider in {"brave", "bing", "serpapi", "duckduckgo", "llm_native"}:
        search_per_query = 1
    else:
        raise EvaluationError(
            f"unsupported search provider for bounded planning: {provider or 'missing'}"
        )
    search_per_case = 3 * search_per_query
    embedding_attempts = _config_int(embedding, "max_retries", 2) + 1
    assumed_embedding_per_case = 2 * embedding_attempts
    nominal_per_case = llm_per_case + search_per_case + assumed_embedding_per_case
    return {
        "case_track_count": case_track_count,
        "configured_path_attempt_estimate": {
            "llm_per_case_track": llm_per_case,
            "search_per_case_track": search_per_case,
            "embedding_two_uncached_consumers_per_case_track": assumed_embedding_per_case,
            "nominal_total_per_case_track": nominal_per_case,
            "nominal_total": nominal_per_case * case_track_count,
        },
        "not_a_completion_upper_bound": True,
        "reason": (
            "LightRAG is a third-party callback consumer and its query-time embedding "
            "callback multiplicity is not treated as a static promise. The durable "
            "runtime ledger, not this estimate, is the hard all-attempt upper bound."
        ),
    }


def _selection_sha256(cases: Sequence[BenchmarkCase], tracks: Sequence[str]) -> str:
    payload = {
        "case_ids": [case.case_id for case in cases],
        "tracks": list(tracks),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _regular_file_snapshot(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        info = path.lstat()
    except OSError:
        return {"path": str(resolved), "exists": False, "sha256": "missing"}
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise EvaluationError(f"binding must be a non-symlink regular file: {path}")
    return {
        "path": str(resolved),
        "exists": True,
        "bytes": info.st_size,
        "sha256": _file_sha256(path),
    }


def _stable_source_evidence_snapshot(path: Path) -> dict[str, Any]:
    """Hash one evidence source through a stable, non-symlink file descriptor."""

    absolute = Path(os.path.abspath(path))
    try:
        path_before = absolute.lstat()
    except OSError:
        return {"path": str(absolute), "exists": False, "sha256": "missing"}
    if stat.S_ISLNK(path_before.st_mode) or not stat.S_ISREG(path_before.st_mode):
        raise EvaluationError(
            f"source evidence must be a non-symlink regular file: {absolute}"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute, flags)
    except OSError as exc:
        raise EvaluationError(f"source evidence is unreadable: {absolute}") from exc
    digest = hashlib.sha256()
    observed_bytes = 0
    try:
        descriptor_before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(descriptor_before.st_mode)
            or (path_before.st_dev, path_before.st_ino)
            != (descriptor_before.st_dev, descriptor_before.st_ino)
        ):
            raise EvaluationError(f"source evidence changed before read: {absolute}")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            observed_bytes += len(chunk)
        descriptor_after = os.fstat(descriptor)
        try:
            path_after = absolute.lstat()
        except OSError as exc:
            raise EvaluationError(f"source evidence changed during read: {absolute}") from exc
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            stat.S_ISLNK(path_after.st_mode)
            or not stat.S_ISREG(path_after.st_mode)
            or observed_bytes != descriptor_before.st_size
            or any(
                getattr(descriptor_before, field) != getattr(descriptor_after, field)
                or getattr(descriptor_before, field) != getattr(path_after, field)
                for field in stable_fields
            )
        ):
            raise EvaluationError(f"source evidence changed during read: {absolute}")
    finally:
        os.close(descriptor)
    return {
        "path": str(absolute),
        "exists": True,
        "bytes": observed_bytes,
        "sha256": digest.hexdigest(),
    }


def _directory_snapshot(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if not path.exists():
        return {
            "path": str(resolved),
            "exists": False,
            "file_count": 0,
            "tree_sha256": "missing",
        }
    if path.is_symlink() or not path.is_dir():
        raise EvaluationError(f"cache seed must be a non-symlink directory: {path}")
    digest = hashlib.sha256()
    count = 0
    for candidate in sorted(path.rglob("*"), key=lambda item: item.as_posix()):
        if candidate.is_symlink():
            raise EvaluationError(f"cache seed contains a symlink: {candidate}")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise EvaluationError(f"cache seed contains a non-regular file: {candidate}")
        relative = candidate.relative_to(path).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(_file_sha256(candidate)))
        count += 1
    return {
        "path": str(resolved),
        "exists": True,
        "file_count": count,
        "tree_sha256": digest.hexdigest(),
    }


def _lightrag_workspace_snapshot(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        info = path.lstat()
    except OSError as exc:
        raise EvaluationError(f"LightRAG workspace is missing: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise EvaluationError(f"LightRAG workspace must be a real directory: {path}")
    files = {
        name: _regular_file_snapshot(path / name)
        for name in LIGHTRAG_WORKSPACE_FILES
    }
    missing = [name for name, snapshot in files.items() if not snapshot["exists"]]
    if missing:
        raise EvaluationError(
            "LightRAG workspace is incomplete: " + ", ".join(sorted(missing))
        )
    return {"path": str(resolved), "files": files}


def _source_tree_snapshot() -> dict[str, Any]:
    source_root = PROJECT_ROOT / "where_paper_go"
    files: dict[str, str] = {}
    digest = hashlib.sha256()
    for path in sorted(source_root.rglob("*.py"), key=lambda item: item.as_posix()):
        if path.is_symlink() or not path.is_file():
            raise EvaluationError(f"pipeline source must be a regular file: {path}")
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        file_digest = _file_sha256(path)
        files[relative] = file_digest
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(bytes.fromhex(file_digest))
    if not files:
        raise EvaluationError("pipeline source tree is empty")
    return {"file_count": len(files), "tree_sha256": digest.hexdigest(), "files": files}


def _dependency_environment_snapshot() -> dict[str, Any]:
    distributions: dict[str, str] = {}
    for name in ("lightrag-hku", "numpy", "openai", "scipy"):
        try:
            distributions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            distributions[name] = "not-installed"
    return {
        "python": sys.version,
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "openssl": ssl.OPENSSL_VERSION,
        "distributions": distributions,
        "worker_determinism_environment": {
            "PYTHONHASHSEED": "0",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
        },
    }


def _ca_directory_snapshot(path: Path) -> dict[str, Any]:
    """Hash a certificate directory without following directory symlinks."""

    resolved = path.resolve()
    try:
        root_info = path.lstat()
    except OSError:
        return {
            "path": str(resolved),
            "exists": False,
            "entry_count": 0,
            "tree_sha256": "missing",
            "binding_paths": [],
        }
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise EvaluationError(f"TLS CA directory must be a real directory: {path}")
    digest = hashlib.sha256()
    binding_paths: list[str] = [str(path.resolve())]
    entry_count = 0
    for current_root, directory_names, file_names in os.walk(
        path, topdown=True, followlinks=False
    ):
        directory_names.sort()
        file_names.sort()
        current = Path(current_root)
        for directory_name in list(directory_names):
            candidate = current / directory_name
            info = candidate.lstat()
            relative = candidate.relative_to(path).as_posix()
            if stat.S_ISLNK(info.st_mode):
                directory_names.remove(directory_name)
                target = os.readlink(candidate)
                row = f"L\t{relative}\t{target}\t"
                try:
                    final = candidate.resolve(strict=True)
                    final_info = final.stat()
                except OSError as exc:
                    raise EvaluationError(
                        f"TLS CA directory contains a broken symlink: {candidate}"
                    ) from exc
                if not stat.S_ISREG(final_info.st_mode):
                    raise EvaluationError(
                        f"TLS CA symlink target must be a regular file: {candidate}"
                    )
                row += _file_sha256(final)
                binding_paths.extend((str(candidate), str(final)))
                entry_count += 1
                digest.update((row + "\n").encode("utf-8"))
            elif not stat.S_ISDIR(info.st_mode):
                raise EvaluationError(
                    f"TLS CA directory contains an unsupported entry: {candidate}"
                )
            else:
                binding_paths.append(str(candidate.resolve()))
                digest.update(f"D\t{relative}\n".encode("utf-8"))
                entry_count += 1
        for file_name in file_names:
            candidate = current / file_name
            info = candidate.lstat()
            relative = candidate.relative_to(path).as_posix()
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(candidate)
                try:
                    final = candidate.resolve(strict=True)
                    final_info = final.stat()
                except OSError as exc:
                    raise EvaluationError(
                        f"TLS CA directory contains a broken symlink: {candidate}"
                    ) from exc
                if not stat.S_ISREG(final_info.st_mode):
                    raise EvaluationError(
                        f"TLS CA symlink target must be a regular file: {candidate}"
                    )
                row = f"L\t{relative}\t{target}\t{_file_sha256(final)}\n"
                binding_paths.extend((str(candidate), str(final)))
            elif stat.S_ISREG(info.st_mode):
                row = f"F\t{relative}\t{info.st_size}\t{_file_sha256(candidate)}\n"
                binding_paths.append(str(candidate.resolve()))
            else:
                raise EvaluationError(
                    f"TLS CA directory contains an unsupported entry: {candidate}"
                )
            entry_count += 1
            digest.update(row.encode("utf-8"))
    return {
        "path": str(resolved),
        "exists": True,
        "entry_count": entry_count,
        "tree_sha256": digest.hexdigest(),
        "binding_paths": sorted(set(binding_paths)),
    }


def _tls_file_snapshot(path: Path) -> dict[str, Any]:
    """Hash a TLS file and, when applicable, its symlink identity and target."""

    try:
        info = path.lstat()
    except OSError:
        return {
            "path": str(path.resolve()),
            "exists": False,
            "sha256": "missing",
            "binding_paths": [],
        }
    if stat.S_ISLNK(info.st_mode):
        link_target = os.readlink(path)
        try:
            resolved = path.resolve(strict=True)
            target_info = resolved.stat()
        except OSError as exc:
            raise EvaluationError(f"TLS trust file is a broken symlink: {path}") from exc
        if not stat.S_ISREG(target_info.st_mode):
            raise EvaluationError(
                f"TLS trust symlink target must be a regular file: {path}"
            )
        return {
            "path": str(path.absolute()),
            "exists": True,
            "symlink": True,
            "symlink_target": link_target,
            "resolved_target": str(resolved),
            "bytes": target_info.st_size,
            "sha256": _file_sha256(resolved),
            "binding_paths": [str(path.absolute()), str(resolved)],
        }
    if not stat.S_ISREG(info.st_mode):
        raise EvaluationError(f"TLS trust file must be regular or a file symlink: {path}")
    resolved = path.resolve()
    return {
        "path": str(resolved),
        "exists": True,
        "symlink": False,
        "bytes": info.st_size,
        "sha256": _file_sha256(path),
        "binding_paths": [str(resolved)],
    }


def _tls_trust_snapshot() -> dict[str, Any]:
    """Bind the actual OpenSSL trust material without serializing PEM bytes."""

    defaults = ssl.get_default_verify_paths()
    candidates: dict[str, tuple[str, Path]] = {}
    if defaults.cafile:
        candidates["active_default_cafile"] = ("file", Path(defaults.cafile))
    if defaults.capath:
        candidates["active_default_capath"] = ("directory", Path(defaults.capath))
    for variable in (
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "OPENSSL_CONF",
    ):
        raw = os.environ.get(variable)
        if raw:
            candidates[f"environment_{variable}"] = ("file", Path(raw))
    for variable in ("SSL_CERT_DIR", "OPENSSL_MODULES"):
        raw = os.environ.get(variable)
        if raw:
            candidates[f"environment_{variable}"] = ("directory", Path(raw))
    material: dict[str, Any] = {}
    binding_paths: list[str] = []
    available = False
    for name, (kind, path) in candidates.items():
        snapshot = (
            _tls_file_snapshot(path)
            if kind == "file"
            else _ca_directory_snapshot(path)
        )
        material[name] = {"kind": kind, **snapshot}
        if name in {"active_default_cafile", "active_default_capath"} and snapshot.get(
            "exists"
        ):
            available = True
        binding_paths.extend(str(value) for value in snapshot.get("binding_paths", ()))
    return {
        "openssl_version": ssl.OPENSSL_VERSION,
        "default_verify_paths": {
            "openssl_cafile_env": defaults.openssl_cafile_env,
            "openssl_cafile": defaults.openssl_cafile,
            "openssl_capath_env": defaults.openssl_capath_env,
            "openssl_capath": defaults.openssl_capath,
            "active_cafile": defaults.cafile,
            "active_capath": defaults.capath,
        },
        "verification_material_available": available,
        "material": material,
        "binding_paths": sorted(set(binding_paths)),
    }


def _network_environment_snapshot() -> dict[str, Any]:
    names = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "SSLKEYLOGFILE",
        "PYTHONHTTPSVERIFY",
        "OPENSSL_CONF",
        "OPENSSL_MODULES",
    )
    result: dict[str, Any] = {}
    for name in names:
        value = os.environ.get(name)
        result[name] = {
            "present": value is not None,
            "bytes": len(value.encode("utf-8")) if value is not None else 0,
            "sha256": (
                hashlib.sha256(value.encode("utf-8")).hexdigest()
                if value is not None
                else None
            ),
        }
    return {
        "policy": "urllib trust_environment is permitted only with this exact hashed environment",
        "variables": result,
        "tls_trust": _tls_trust_snapshot(),
    }


def _output_secret_isolation_status(output_dir: Path) -> dict[str, Any]:
    resolved = output_dir.resolve()
    if not resolved.is_relative_to(PROJECT_ROOT.resolve()):
        return {
            "status": "outside_repository_private_output_required",
            "git_ignored": None,
            "tracked_entries": 0,
        }
    relative = resolved.relative_to(PROJECT_ROOT.resolve())
    building_probe = relative.with_name(f".{relative.name}.building-probe")
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "--", str(relative)],
            cwd=PROJECT_ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.splitlines()
        ignored_output = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", "--", str(relative)],
            cwd=PROJECT_ROOT,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        ).returncode == 0
        ignored_building = subprocess.run(
            [
                "git",
                "check-ignore",
                "--no-index",
                "-q",
                "--",
                str(building_probe),
            ],
            cwd=PROJECT_ROOT,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvaluationError("cannot verify Git isolation for credential-bearing output") from exc
    return {
        "status": (
            "repository_output_is_ignored_and_untracked"
            if ignored_output and ignored_building and not tracked
            else "unsafe_repository_output"
        ),
        "git_ignored": ignored_output,
        "building_namespace_ignored": ignored_building,
        "tracked_entries": len(tracked),
    }


def _cache_seed_snapshot(
    api_cache_dir: Path,
    query_embedding_cache: Path,
    lightrag_embedding_cache: Path,
    lightrag_working_dir: Path,
) -> dict[str, Any]:
    return {
        "api_cache": _directory_snapshot(api_cache_dir),
        "query_embedding_cache": _regular_file_snapshot(query_embedding_cache),
        "lightrag_embedding_cache": _regular_file_snapshot(lightrag_embedding_cache),
        "lightrag_workspace": _lightrag_workspace_snapshot(lightrag_working_dir),
    }


def _runtime_cache_layout() -> dict[str, str]:
    return {
        "api_cache_dir": f"{RUNTIME_CACHE_DIR}/api_cache",
        "query_embedding_cache": f"{RUNTIME_CACHE_DIR}/query_embedding_cache.json.gz",
        "lightrag_embedding_cache": f"{RUNTIME_CACHE_DIR}/lightrag_embedding_cache.json.gz",
        "api_config_snapshot": f"{RUNTIME_CACHE_DIR}/api_config.snapshot.json",
        "lightrag_working_dir": f"{RUNTIME_CACHE_DIR}/lightrag_storage",
        "graph_path": f"{RUNTIME_CACHE_DIR}/venue_graph.json.gz",
        "vector_path": f"{RUNTIME_CACHE_DIR}/venue_graph_vectors.json.gz",
    }


def _verify_fresh_runtime_cache_clone(
    expected_seeds: Mapping[str, Any], runtime_paths: Mapping[str, Path]
) -> None:
    runtime_api = _directory_snapshot(runtime_paths["api_cache_dir"])
    seed_api = dict(expected_seeds["api_cache"])
    if seed_api.get("exists"):
        if (
            runtime_api.get("file_count") != seed_api.get("file_count")
            or runtime_api.get("tree_sha256") != seed_api.get("tree_sha256")
        ):
            raise EvaluationError("run-local API cache clone does not match frozen seed")
    for key in ("query_embedding_cache", "lightrag_embedding_cache"):
        seed = dict(expected_seeds[key])
        runtime = _regular_file_snapshot(runtime_paths[key])
        if bool(runtime.get("exists")) != bool(seed.get("exists")) or (
            seed.get("exists") and runtime.get("sha256") != seed.get("sha256")
        ):
            raise EvaluationError(f"run-local {key} clone does not match frozen seed")
    expected_workspace = dict(expected_seeds["lightrag_workspace"])
    runtime_workspace = _lightrag_workspace_snapshot(
        runtime_paths["lightrag_working_dir"]
    )
    for name in LIGHTRAG_WORKSPACE_FILES:
        expected_file = dict(expected_workspace["files"][name])
        runtime_file = dict(runtime_workspace["files"][name])
        if (
            expected_file.get("bytes") != runtime_file.get("bytes")
            or expected_file.get("sha256") != runtime_file.get("sha256")
        ):
            raise EvaluationError(
                f"run-local LightRAG workspace clone drifted: {name}"
            )


def _verify_fresh_graph_binding(graph_path: Path) -> None:
    freshness = inspect_graph(
        graph_path,
        DATA_DIR,
        expected_digest=graph_source_digest(DATA_DIR),
    )
    if not freshness.fresh:
        raise EvaluationError(
            "frozen property graph is stale/unreadable; strict evaluation refuses rebuild: "
            + freshness.reason
        )
    vector_path = vector_path_for_graph(graph_path)
    vector_snapshot = _regular_file_snapshot(vector_path)
    if not vector_snapshot["exists"]:
        raise EvaluationError("frozen graph vector sidecar is missing")


def _runtime_binding_snapshot(
    api_config: Path, lightrag_working_dir: Path
) -> dict[str, Any]:
    return {
        "api_config": _regular_file_snapshot(api_config),
        "catalog_graph_source_digest": graph_source_digest(DATA_DIR),
        "catalog_sources": {
            name: _regular_file_snapshot(DATA_DIR / name)
            for name in (*DATA_FILES, CURATED_SCOPE_FILE)
        },
        "pipeline_source_tree": _source_tree_snapshot(),
        "evaluator_source_sha256": _optional_file_sha256(Path(__file__)),
        "dependency_environment": _dependency_environment_snapshot(),
        "network_environment": _network_environment_snapshot(),
        "graph_artifact": _regular_file_snapshot(DATA_DIR / "venue_graph.json.gz"),
        "vector_artifact": _regular_file_snapshot(
            DATA_DIR / "venue_graph_vectors.json.gz"
        ),
        "lightrag_manifest": _regular_file_snapshot(
            lightrag_working_dir / LIGHTRAG_MANIFEST_FILE
        ),
        "lightrag_query_stores": {
            name: _regular_file_snapshot(lightrag_working_dir / name)
            for name in QUERY_STORAGE_FILES
        },
    }


def _verify_runtime_bindings(
    expected: Mapping[str, Any], api_config: Path, lightrag_working_dir: Path
) -> None:
    current = _runtime_binding_snapshot(api_config, lightrag_working_dir)
    if current != dict(expected):
        raise EvaluationError(
            "frozen API/code/graph/vector/LightRAG binding drifted; refusing mixed run"
        )


def _binding_file_stamp(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
    except OSError:
        return {"path": str(path.resolve()), "exists": False}
    return {
        "path": str(path.resolve()),
        "exists": True,
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": stat.S_IFMT(info.st_mode),
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
    }


def _runtime_binding_stamps(
    api_config: Path,
    api_config_snapshot: Path,
    lightrag_seed_dir: Path,
    lightrag_runtime_dir: Path,
    source_evidence_paths: Iterable[Path] = (),
) -> dict[str, Any]:
    paths = [
        api_config,
        api_config_snapshot,
        Path(__file__),
        DATA_DIR / "venue_graph.json.gz",
        DATA_DIR / "venue_graph_vectors.json.gz",
        lightrag_seed_dir / LIGHTRAG_MANIFEST_FILE,
        *(lightrag_seed_dir / name for name in QUERY_STORAGE_FILES),
        lightrag_runtime_dir / LIGHTRAG_MANIFEST_FILE,
        *(lightrag_runtime_dir / name for name in QUERY_STORAGE_FILES),
        lightrag_runtime_dir.parent / "venue_graph.json.gz",
        lightrag_runtime_dir.parent / "venue_graph_vectors.json.gz",
        *(DATA_DIR / name for name in (*DATA_FILES, CURATED_SCOPE_FILE)),
        *sorted((PROJECT_ROOT / "where_paper_go").rglob("*.py")),
        *(Path(path) for path in _tls_trust_snapshot()["binding_paths"]),
        *source_evidence_paths,
    ]
    return {
        str(path.absolute() if path.is_symlink() else path.resolve()): _binding_file_stamp(path)
        for path in paths
    }


def _verify_runtime_binding_stamps(expected: Mapping[str, Any]) -> None:
    current = {
        path: _binding_file_stamp(Path(path)) for path in sorted(expected)
    }
    if current != dict(expected):
        raise EvaluationError(
            "frozen API/code/graph/vector/LightRAG file identity drifted; refusing mixed run"
        )


def _authorization_reference_looks_secret(value: str) -> bool:
    if re.search(
        r"(?i)(?:\bbearer\s+\S+|"
        r"\bbasic\s+[A-Za-z0-9+/=]{12,}|"
        r"\b(?:api[_ -]?key|authorization|token|secret|password)\s*[:=]\s*\S+|"
        r"\b(?:sk|rk|pk|sess|tvly|ghp|github_pat|glpat|hf)[-_][A-Za-z0-9_-]{12,}|"
        r"\bAKIA[0-9A-Z]{16}\b)",
        value,
    ):
        return True
    if re.search(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b", value):
        return True
    for token in re.findall(r"[A-Za-z0-9_+/=-]{32,}", value):
        frequencies = {character: token.count(character) for character in set(token)}
        entropy = -sum(
            (count / len(token)) * math.log2(count / len(token))
            for count in frequencies.values()
        )
        if entropy >= 3.5 and re.search(r"[A-Za-z]", token) and re.search(r"\d", token):
            return True
    return False


def _authorization_reference_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _formal_date_in_window(value: str) -> bool:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return date(2026, 1, 1) <= parsed <= date(2026, 6, 30)


def _secure_formal_evidence_bytes(
    path: Path, *, expected_mode: int, max_bytes: int
) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EvaluationError(f"formal evidence is missing/unsafe: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != expected_mode
            or before.st_size > max_bytes
        ):
            raise EvaluationError(f"formal evidence has unsafe type/owner/mode/size: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise EvaluationError(f"formal evidence exceeds its size bound: {path}")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise EvaluationError(f"formal evidence changed during read: {path}")
    finally:
        os.close(descriptor)
    return b"".join(chunks), before


def _strict_json_value(raw: bytes, *, label: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    try:
        return json.loads(
            raw.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_pairs,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise EvaluationError(f"invalid strict JSON in {label}") from exc


def _strict_jsonl_rows(raw: bytes, *, label: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line:
            continue
        value = _strict_json_value(line, label=f"{label}:{line_number}")
        if not isinstance(value, dict):
            raise EvaluationError(f"formal JSONL row is not an object: {label}:{line_number}")
        rows.append(value)
    return rows


def _validate_formal_acquisition_evidence(
    builder_payload: Mapping[str, Any],
    *,
    builder_manifest: Path,
    dataset: Path,
    dataset_sha256: str,
    raw_rows: Sequence[Mapping[str, Any]],
    expected_count: int = 500,
    acquisition_window: Any | None = None,
    min_abstract_chars: int = 300,
) -> dict[str, Any]:
    """Replay the acquisition-time Crossref evidence bundle without network access."""

    from scripts import build_recent_journal_benchmark as builder

    window = acquisition_window or builder.BuildWindow(
        date(2026, 1, 1), date(2026, 6, 30)
    )
    expected_bulk_rows = _nested(builder_payload, "configuration", "bulk_rows")
    expected_journal_rows = _nested(
        builder_payload, "configuration", "rows_per_journal"
    )
    if (
        not isinstance(expected_bulk_rows, int)
        or isinstance(expected_bulk_rows, bool)
        or not 1 <= expected_bulk_rows <= 1000
        or not isinstance(expected_journal_rows, int)
        or isinstance(expected_journal_rows, bool)
        or not 1 <= expected_journal_rows <= 1000
    ):
        raise EvaluationError(
            "formal acquisition evidence lacks fixed Crossref page-size bindings"
        )

    bundle = builder_payload.get("acquisition_evidence")
    required_bundle_keys = {
        "schema_version",
        "artifact_type",
        "complete",
        "dataset_record_count",
        "provenance",
        "cache_leaves",
        "cache_tree",
        "ledger",
        "builder_source",
        "redirect_policy",
        "assurance_scope",
    }
    if (
        not isinstance(bundle, Mapping)
        or set(bundle) != required_bundle_keys
        or bundle.get("schema_version") != 1
        or bundle.get("artifact_type") != "crossref_acquisition_evidence_bundle"
        or bundle.get("complete") is not True
        or bundle.get("dataset_record_count") != expected_count
        or bundle.get("redirect_policy") != "fail_closed_no_redirect_hops"
        or bundle.get("assurance_scope")
        != (
            "locally replayable accepted-row provenance and used successful Crossref "
            "responses with pre-socket reservation prefixes under operator discipline; "
            "excludes unselected/failed response completeness and is not cryptographic "
            "attestation by Crossref"
        )
    ):
        raise EvaluationError("formal Crossref acquisition evidence bundle is missing/incomplete")
    base = builder_manifest.parent

    def bound_file(
        binding: Any,
        *,
        expected_name: str,
        max_bytes: int,
    ) -> tuple[Path, bytes]:
        if (
            not isinstance(binding, Mapping)
            or binding.get("path") != expected_name
            or not re.fullmatch(r"[0-9a-f]{64}", str(binding.get("sha256") or ""))
        ):
            raise EvaluationError(f"formal evidence binding is invalid: {expected_name}")
        path = base / expected_name
        raw, _info = _secure_formal_evidence_bytes(
            path, expected_mode=0o444, max_bytes=max_bytes
        )
        if hashlib.sha256(raw).hexdigest() != binding.get("sha256"):
            raise EvaluationError(f"formal evidence hash mismatch: {expected_name}")
        return path, raw

    provenance_path, provenance_raw = bound_file(
        bundle.get("provenance"),
        expected_name="provenance.jsonl",
        max_bytes=64 * 1024 * 1024,
    )
    leaves_path, leaves_raw = bound_file(
        bundle.get("cache_leaves"),
        expected_name="cache_evidence.jsonl",
        max_bytes=64 * 1024 * 1024,
    )
    tree_path, tree_raw = bound_file(
        bundle.get("cache_tree"),
        expected_name="cache_evidence_manifest.json",
        max_bytes=16 * 1024 * 1024,
    )
    provenance_rows = _strict_jsonl_rows(provenance_raw, label="provenance.jsonl")
    leaves = _strict_jsonl_rows(leaves_raw, label="cache_evidence.jsonl")
    tree = _strict_json_value(tree_raw, label="cache_evidence_manifest.json")
    if not isinstance(tree, Mapping):
        raise EvaluationError("formal cache evidence manifest must be an object")
    provenance_binding = bundle.get("provenance")
    leaves_binding = bundle.get("cache_leaves")
    tree_binding = bundle.get("cache_tree")
    if (
        not isinstance(provenance_binding, Mapping)
        or set(provenance_binding)
        != {"path", "sha256", "record_count", "schema_version"}
        or provenance_binding.get("schema_version") != 1
        or not isinstance(leaves_binding, Mapping)
        or set(leaves_binding)
        != {"path", "sha256", "record_count", "schema_version"}
        or leaves_binding.get("schema_version") != 1
        or not isinstance(tree_binding, Mapping)
        or set(tree_binding)
        != {"path", "sha256", "schema_version", "merkle_sha256"}
        or tree_binding.get("schema_version") != 1
    ):
        raise EvaluationError("formal evidence nested binding schema is not exact")
    if (
        _nested(bundle, "provenance", "record_count") != len(provenance_rows)
        or len(provenance_rows) != expected_count
        or _nested(bundle, "cache_leaves", "record_count") != len(leaves)
        or _nested(bundle, "cache_tree", "merkle_sha256")
        != tree.get("merkle_sha256")
    ):
        raise EvaluationError("formal evidence record counts/tree binding mismatch")
    required_tree = {
        "schema_version",
        "artifact_type",
        "cache_root",
        "cache_root_mode",
        "leaf_count",
        "merkle_sha256",
        "accepted_record_count",
        "provenance_replay_verified",
        "ledger",
        "builder_source",
        "redirect_policy",
        "every_used_response_bound_to_reservation",
        "ledger_private_appendable",
        "reservation_usage_anchors_bound",
        "budget_ceiling_bound",
        "complete",
        "assurance_scope",
        "dataset",
        "provenance",
        "cache_leaves",
    }
    if (
        set(tree) != required_tree
        or tree.get("schema_version") != 1
        or tree.get("artifact_type") != "crossref_acquisition_evidence_tree"
        or tree.get("cache_root") != "raw_cache"
        or tree.get("cache_root_mode") != "0700"
        or tree.get("leaf_count") != len(leaves)
        or tree.get("accepted_record_count") != expected_count
        or tree.get("provenance_replay_verified") != expected_count
        or tree.get("redirect_policy") != "fail_closed_no_redirect_hops"
        or tree.get("every_used_response_bound_to_reservation") is not True
        or tree.get("ledger_private_appendable") is not True
        or tree.get("reservation_usage_anchors_bound") is not True
        or tree.get("budget_ceiling_bound") is not True
        or tree.get("complete") is not True
        or tree.get("assurance_scope") != bundle.get("assurance_scope")
        or tree.get("dataset")
        != {
            "path": dataset.name,
            "sha256": dataset_sha256,
            "record_count": expected_count,
        }
        or tree.get("provenance") != bundle.get("provenance")
        or tree.get("cache_leaves") != bundle.get("cache_leaves")
        or builder._merkle_root(leaves) != tree.get("merkle_sha256")
        or bundle.get("ledger") != tree.get("ledger")
        or bundle.get("builder_source") != tree.get("builder_source")
    ):
        raise EvaluationError("formal Crossref evidence tree is inconsistent")
    current_builder_sha = _file_sha256(
        PROJECT_ROOT / "scripts" / "build_recent_journal_benchmark.py"
    )
    if tree.get("builder_source") != {
        "path": "scripts/build_recent_journal_benchmark.py",
        "sha256": current_builder_sha,
    } or builder_payload.get("builder_source") != tree.get("builder_source"):
        raise EvaluationError("formal evidence does not bind the current builder source")

    ledger = tree.get("ledger")
    required_ledger_keys = {
        "path",
        "sha256",
        "bytes",
        "attempt_records",
        "budget_id",
        "append_only_reservations",
        "mode",
        "hard_http_attempt_ceiling",
        "source_ledger_device",
        "source_ledger_inode",
        "highwater",
        "global_usage",
        "budget_binding",
        "global_budget_claim",
        "source_mode",
        "immutable_prefix_snapshot",
    }
    if not isinstance(ledger, Mapping) or set(ledger) != required_ledger_keys:
        raise EvaluationError("formal evidence has no request ledger prefix")
    ledger_path, ledger_raw = bound_file(
        ledger,
        expected_name="request-ledger-prefix.jsonl",
        max_bytes=64 * 1024 * 1024,
    )
    budget_id = str(ledger.get("budget_id") or "")
    hard_ceiling = ledger.get("hard_http_attempt_ceiling")
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}", budget_id)
        or not isinstance(hard_ceiling, int)
        or isinstance(hard_ceiling, bool)
        or hard_ceiling < 1
        or ledger.get("mode") != "0444"
        or ledger.get("source_mode") != "0600"
        or ledger.get("immutable_prefix_snapshot") is not True
        or ledger.get("append_only_reservations") is not True
        or ledger.get("bytes") != len(ledger_raw)
        or not isinstance(ledger.get("source_ledger_device"), int)
        or isinstance(ledger.get("source_ledger_device"), bool)
        or not isinstance(ledger.get("source_ledger_inode"), int)
        or isinstance(ledger.get("source_ledger_inode"), bool)
    ):
        raise EvaluationError("formal request ledger binding is invalid")
    try:
        ledger_rows = builder._parse_request_ledger_lines(
            ledger_raw.decode("utf-8").splitlines(keepends=True),
            path=ledger_path,
            budget_id=budget_id,
        )
    except (UnicodeError, ValueError) as exc:
        raise EvaluationError("formal request ledger prefix is invalid") from exc
    if (
        len(ledger_rows) != ledger.get("attempt_records")
        or len(ledger_rows) > hard_ceiling
    ):
        raise EvaluationError("formal request ledger count exceeds/breaks its ceiling")
    ledger_sequences: dict[str, list[int]] = {}
    for row in ledger_rows:
        ledger_sequences.setdefault(str(row["request_url_sha256"]), []).append(
            int(row["sequence"])
        )
    evidence_sources: dict[str, dict[str, Any]] = {
        "crossref_provenance": {
            "path": str(provenance_path),
            "sha256": hashlib.sha256(provenance_raw).hexdigest(),
            "bytes": len(provenance_raw),
        },
        "crossref_cache_leaves": {
            "path": str(leaves_path),
            "sha256": hashlib.sha256(leaves_raw).hexdigest(),
            "bytes": len(leaves_raw),
        },
        "crossref_cache_tree": {
            "path": str(tree_path),
            "sha256": hashlib.sha256(tree_raw).hexdigest(),
            "bytes": len(tree_raw),
        },
        "crossref_request_ledger": {
            "path": str(ledger_path),
            "sha256": hashlib.sha256(ledger_raw).hexdigest(),
            "bytes": len(ledger_raw),
        },
    }

    usage_metadata_keys = {
        "path",
        "sha256",
        "bytes",
        "attempt_records",
        "mode",
        "source_mode",
        "immutable_prefix_snapshot",
    }
    for usage_name, expected_name in (
        ("highwater", "request-ledger-highwater-prefix.jsonl"),
        ("global_usage", "request-ledger-global-prefix.jsonl"),
    ):
        metadata = ledger.get(usage_name)
        if not isinstance(metadata, Mapping) or set(metadata) != usage_metadata_keys:
            raise EvaluationError(f"formal Crossref {usage_name} binding is invalid")
        path, raw = bound_file(
            metadata, expected_name=expected_name, max_bytes=64 * 1024 * 1024
        )
        try:
            rows = builder._parse_request_ledger_lines(
                raw.decode("utf-8").splitlines(keepends=True),
                path=path,
                budget_id=budget_id,
            )
        except (UnicodeError, ValueError) as exc:
            raise EvaluationError(
                f"formal Crossref {usage_name} prefix is invalid"
            ) from exc
        if (
            raw != ledger_raw
            or rows != ledger_rows
            or metadata.get("bytes") != len(raw)
            or metadata.get("attempt_records") != len(rows)
            or metadata.get("mode") != "0444"
            or metadata.get("source_mode") != "0600"
            or metadata.get("immutable_prefix_snapshot") is not True
        ):
            raise EvaluationError(
                f"formal Crossref {usage_name} diverges from the request ledger"
            )
        evidence_sources[f"crossref_request_{usage_name}"] = {
            "path": str(path),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
        }

    identity_keys = {
        "ledger_path_sha256",
        "ledger_device",
        "ledger_inode",
        "highwater_path_sha256",
        "highwater_device",
        "highwater_inode",
    }
    budget_metadata_keys = {
        "path",
        "sha256",
        "bytes",
        "mode",
        "schema_version",
        "artifact_type",
        "budget_id",
        "hard_http_attempt_ceiling",
        *identity_keys,
        "source_mode",
        "immutable_snapshot",
    }
    claim_metadata_keys = {
        *budget_metadata_keys,
        "global_usage_path_sha256",
        "global_usage_device",
        "global_usage_inode",
    }
    local_metadata = ledger.get("budget_binding")
    global_metadata = ledger.get("global_budget_claim")
    if (
        not isinstance(local_metadata, Mapping)
        or set(local_metadata) != budget_metadata_keys
        or not isinstance(global_metadata, Mapping)
        or set(global_metadata) != claim_metadata_keys
    ):
        raise EvaluationError("formal Crossref budget binding metadata is invalid")
    identity = {key: local_metadata.get(key) for key in identity_keys}
    if (
        identity["ledger_device"] != ledger.get("source_ledger_device")
        or identity["ledger_inode"] != ledger.get("source_ledger_inode")
        or any(
            not re.fullmatch(r"[0-9a-f]{64}", str(identity[key] or ""))
            for key in ("ledger_path_sha256", "highwater_path_sha256")
        )
        or any(
            not isinstance(identity[key], int) or isinstance(identity[key], bool)
            for key in (
                "ledger_device",
                "ledger_inode",
                "highwater_device",
                "highwater_inode",
            )
        )
    ):
        raise EvaluationError("formal Crossref budget source identity is invalid")
    expected_local = {
        "schema_version": 1,
        "artifact_type": "crossref_http_attempt_budget_binding",
        "budget_id": budget_id,
        "hard_http_attempt_ceiling": hard_ceiling,
        **identity,
    }
    global_identity = {
        key: global_metadata.get(key)
        for key in (
            "global_usage_path_sha256",
            "global_usage_device",
            "global_usage_inode",
        )
    }
    if (
        any(global_metadata.get(key) != value for key, value in identity.items())
        or not re.fullmatch(
            r"[0-9a-f]{64}", str(global_identity["global_usage_path_sha256"] or "")
        )
        or any(
            not isinstance(global_identity[key], int)
            or isinstance(global_identity[key], bool)
            for key in ("global_usage_device", "global_usage_inode")
        )
    ):
        raise EvaluationError("formal Crossref global budget identity is invalid")
    expected_global = {
        **expected_local,
        "artifact_type": "crossref_global_http_attempt_budget_claim",
        **global_identity,
    }
    for binding_name, expected_name, metadata, expected_payload in (
        (
            "budget_binding",
            "request-budget-binding.json",
            local_metadata,
            expected_local,
        ),
        (
            "global_budget_claim",
            "request-budget-global-claim.json",
            global_metadata,
            expected_global,
        ),
    ):
        path, raw = bound_file(
            metadata, expected_name=expected_name, max_bytes=1024 * 1024
        )
        value = _strict_json_value(raw, label=expected_name)
        if (
            value != expected_payload
            or any(metadata.get(key) != val for key, val in expected_payload.items())
            or metadata.get("mode") != "0444"
            or metadata.get("source_mode") != "0400"
            or metadata.get("immutable_snapshot") is not True
            or metadata.get("bytes") != len(raw)
        ):
            raise EvaluationError(f"formal Crossref {binding_name} is inconsistent")
        evidence_sources[f"crossref_{binding_name}"] = {
            "path": str(path),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
        }

    raw_cache = base / "raw_cache"
    try:
        cache_info = raw_cache.lstat()
    except OSError as exc:
        raise EvaluationError("formal raw Crossref cache snapshot is missing") from exc
    if (
        stat.S_ISLNK(cache_info.st_mode)
        or not stat.S_ISDIR(cache_info.st_mode)
        or cache_info.st_uid != os.getuid()
        or stat.S_IMODE(cache_info.st_mode) != 0o700
    ):
        raise EvaluationError("formal raw Crossref cache must be private mode 0700")
    leaf_by_path: dict[str, Mapping[str, Any]] = {}
    raw_objects: dict[str, tuple[bytes, Mapping[str, Any]]] = {}
    for leaf in leaves:
        relative = str(leaf.get("cache_relative_path") or "")
        request_sha = str(leaf.get("request_url_sha256") or "")
        if (
            set(leaf)
            != {
                "schema_version",
                "artifact_type",
                "cache_relative_path",
                "request_url_sha256",
                "request_descriptor",
                "request_descriptor_sha256",
                "response_sha256",
                "bytes",
                "mode",
                "ledger_sequences",
            }
            or leaf.get("schema_version") != 1
            or leaf.get("artifact_type") != "crossref_response_cache_leaf"
            or relative != f"raw_cache/{request_sha}.json"
            or not re.fullmatch(r"[0-9a-f]{64}", request_sha)
            or relative in leaf_by_path
        ):
            raise EvaluationError("formal Crossref cache leaf schema/path is invalid")
        try:
            descriptor, descriptor_sha = builder._validate_crossref_request_descriptor(
                leaf.get("request_descriptor"),
                expected_mailto=str(_nested(builder_payload, "configuration", "mailto") or ""),
                window=window,
                expected_bulk_rows=expected_bulk_rows,
                expected_journal_rows=expected_journal_rows,
            )
        except ValueError as exc:
            raise EvaluationError("formal Crossref request descriptor is invalid") from exc
        if (
            descriptor_sha != request_sha
            or hashlib.sha256(builder._canonical_json_bytes(descriptor)).hexdigest()
            != leaf.get("request_descriptor_sha256")
            or leaf.get("ledger_sequences") != ledger_sequences.get(request_sha, [])
            or not leaf.get("ledger_sequences")
            or leaf.get("mode") != "0400"
        ):
            raise EvaluationError("formal Crossref request/cache reservation binding failed")
        raw_path = base / relative
        raw, _raw_info = _secure_formal_evidence_bytes(
            raw_path,
            expected_mode=0o400,
            max_bytes=builder.MAX_CROSSREF_RESPONSE_BYTES,
        )
        if (
            len(raw) != leaf.get("bytes")
            or hashlib.sha256(raw).hexdigest() != leaf.get("response_sha256")
        ):
            raise EvaluationError("formal raw Crossref response hash/size mismatch")
        value = builder._load_json_object_bytes(raw, source=raw_path)
        leaf_by_path[relative] = leaf
        raw_objects[relative] = (raw, value)
        evidence_sources[f"crossref_raw_{request_sha}"] = {
            "path": str(raw_path),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
        }
    actual_raw_names = {path.name for path in raw_cache.iterdir()}
    expected_raw_names = {Path(path).name for path in leaf_by_path}
    if actual_raw_names != expected_raw_names:
        raise EvaluationError("formal raw Crossref cache file set is not exact")

    venues, _ambiguous = builder.load_jcr_venues(DATA_DIR)
    venue_by_id = {venue.venue_id: venue for venue in venues}
    issn_index = builder.build_issn_index(venues)
    if len(provenance_rows) != len(raw_rows) or len(raw_rows) != expected_count:
        raise EvaluationError("formal Crossref provenance denominator is not 500")
    for index, (provenance, dataset_row) in enumerate(
        zip(provenance_rows, raw_rows, strict=True)
    ):
        relative = str(provenance.get("cache_relative_path") or "")
        leaf = leaf_by_path.get(relative)
        if (
            set(provenance)
            != {
                "schema_version",
                "artifact_type",
                "dataset_index",
                "paper_id",
                "request_url_sha256",
                "request_descriptor_sha256",
                "cache_relative_path",
                "response_sha256",
                "response_bytes",
                "item_index",
                "canonical_item_sha256",
                "observed_via",
                "prepared_record_sha256",
                "ledger_sequences",
            }
            or
            not isinstance(leaf, Mapping)
            or provenance.get("schema_version") != 1
            or provenance.get("artifact_type")
            != "crossref_accepted_record_provenance"
            or provenance.get("dataset_index") != index
            or provenance.get("paper_id") != dataset_row.get("paper_id")
            or provenance.get("request_url_sha256")
            != leaf.get("request_url_sha256")
            or provenance.get("request_descriptor_sha256")
            != leaf.get("request_descriptor_sha256")
            or provenance.get("response_sha256") != leaf.get("response_sha256")
            or provenance.get("response_bytes") != leaf.get("bytes")
            or provenance.get("ledger_sequences") != leaf.get("ledger_sequences")
            or provenance.get("observed_via") not in {"network", "cache"}
            or provenance.get("prepared_record_sha256")
            != hashlib.sha256(builder._canonical_json_bytes(dataset_row)).hexdigest()
        ):
            raise EvaluationError(f"formal Crossref provenance mismatch at row {index}")
        _raw, response = raw_objects[relative]
        message = response.get("message") if isinstance(response, Mapping) else None
        items = message.get("items") if isinstance(message, Mapping) else None
        item_index = provenance.get("item_index")
        if (
            not isinstance(items, list)
            or not isinstance(item_index, int)
            or isinstance(item_index, bool)
            or not 0 <= item_index < len(items)
            or not isinstance(items[item_index], dict)
        ):
            raise EvaluationError(f"formal Crossref item index is invalid at row {index}")
        item = items[item_index]
        if hashlib.sha256(builder._canonical_json_bytes(item)).hexdigest() != provenance.get(
            "canonical_item_sha256"
        ):
            raise EvaluationError(f"formal Crossref item hash mismatch at row {index}")
        venue = venue_by_id.get(str(dataset_row.get("gold_journal_id") or ""))
        rebuilt, status = builder.prepare_crossref_record(
            item,
            issn_index=issn_index,
            expected_venue=venue,
            window=window,
            min_abstract_chars=min_abstract_chars,
        ) if venue is not None else (None, "unknown_venue")
        if status != "ok" or rebuilt != dict(dataset_row):
            raise EvaluationError(f"formal Crossref replay failed at row {index}")
    return {
        "status": "acquisition_time_evidence_replayed",
        "assurance_scope": bundle["assurance_scope"],
        "provenance_record_count": len(provenance_rows),
        "used_response_count": len(leaves),
        "http_attempt_prefix_count": len(ledger_rows),
        "hard_http_attempt_ceiling": hard_ceiling,
        "merkle_sha256": tree["merkle_sha256"],
        "source_files": evidence_sources,
    }


def _authorization_grant_binding(
    path: Path,
    *,
    authorization_reference_sha256: str,
    reviewed_plan_digest: str,
    output_dir: Path,
    evaluation_mode: str,
    external_call_budget: int,
    attempt_cost_ceiling_usd: Decimal,
    authorized_max_cost_usd: Decimal,
) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        path_info = path.lstat()
    except OSError as exc:
        raise EvaluationError("formal authorization grant is missing") from exc
    if stat.S_ISLNK(path_info.st_mode) or not stat.S_ISREG(path_info.st_mode):
        raise EvaluationError("formal authorization grant must be a regular file")
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EvaluationError("formal authorization grant is missing") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise EvaluationError("formal authorization grant must be a regular file")
        if stat.S_IMODE(before.st_mode) != 0o444 or before.st_uid != os.getuid():
            raise EvaluationError(
                "formal authorization grant must be owned by this user and mode 0444"
            )
        if before.st_size > 1024 * 1024:
            raise EvaluationError("formal authorization grant exceeds 1 MiB")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 1024 * 1024:
                raise EvaluationError("formal authorization grant exceeds 1 MiB")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise EvaluationError("formal authorization grant changed during read")
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    try:
        payload = json.loads(
            raw.decode("utf-8"), object_pairs_hook=reject_duplicate_pairs
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise EvaluationError("formal authorization grant is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise EvaluationError("formal authorization grant must be a JSON object")
    expected = {
        "schema_version": AUTHORIZATION_GRANT_SCHEMA_VERSION,
        "status": "explicit_external_api_authorization",
        "authorization_reference_sha256": authorization_reference_sha256,
        "reviewed_plan_digest": reviewed_plan_digest,
        "output_identity_sha256": hashlib.sha256(
            str(output_dir.resolve()).encode("utf-8")
        ).hexdigest(),
        "evaluation_mode": evaluation_mode,
        "external_call_budget": external_call_budget,
        "external_attempt_cost_ceiling_usd": _decimal_text(
            attempt_cost_ceiling_usd
        ),
        "authorized_max_cost_usd": _decimal_text(authorized_max_cost_usd),
        "maximum_estimated_cost_usd": _decimal_text(
            attempt_cost_ceiling_usd * external_call_budget
        ),
    }
    if set(payload) != set(expected) or any(
        payload.get(key) != value for key, value in expected.items()
    ):
        raise EvaluationError(
            "formal authorization grant does not bind this exact plan/output/budget"
        )
    return {
        "path": str(path.absolute()),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "validation": expected,
        "assurance": (
            "immutable local audit sentinel under operator discipline; "
            "not a cryptographic signature or proof of human identity"
        ),
    }


def _validate_formal_builder_manifest(
    path: Path | None,
    *,
    dataset: Path,
    dataset_sha256: str,
    cases: Sequence[BenchmarkCase],
    source_cases: Sequence[BenchmarkCase],
) -> dict[str, Any]:
    if path is None:
        raise EvaluationError("formal mode requires --builder-manifest")
    try:
        path_info = path.lstat()
    except OSError as exc:
        raise EvaluationError("formal builder manifest is unreadable") from exc
    if stat.S_ISLNK(path_info.st_mode) or not stat.S_ISREG(path_info.st_mode):
        raise EvaluationError("formal builder manifest must be a regular file")
    try:
        dataset_info = dataset.lstat()
    except OSError as exc:
        raise EvaluationError("formal dataset is unreadable") from exc
    if (
        path_info.st_uid != os.getuid()
        or stat.S_IMODE(path_info.st_mode) != 0o444
        or stat.S_ISLNK(dataset_info.st_mode)
        or not stat.S_ISREG(dataset_info.st_mode)
        or dataset_info.st_uid != os.getuid()
        or stat.S_IMODE(dataset_info.st_mode) != 0o444
    ):
        raise EvaluationError(
            "formal dataset and builder manifest must be owned regular mode-0444 files"
        )
    resolved = Path(os.path.abspath(path))
    manifest_raw, _manifest_info = _secure_formal_evidence_bytes(
        resolved, expected_mode=0o444, max_bytes=16 * 1024 * 1024
    )
    payload = _strict_json_value(manifest_raw, label="formal builder manifest")
    if not isinstance(payload, Mapping):
        raise EvaluationError("formal builder manifest must be an object")
    source_filters = _nested(payload, "source", "filters")
    fields = _nested(payload, "configuration", "fields")
    quartiles = _nested(payload, "configuration", "quartiles")
    strata = payload.get("strata")
    source_hashes = _nested(payload, "internal_catalog", "source_files_sha256")
    expected_source_hashes = {
        name: _file_sha256(DATA_DIR / name)
        for name in (*DATA_FILES, CURATED_SCOPE_FILE)
    }
    strata_valid = isinstance(strata, Mapping) and len(strata) == 36
    try:
        if strata_valid:
            strata_valid = all(
                isinstance(row, Mapping)
                and type(row.get("accepted")) is int
                and type(row.get("target")) is int
                and row.get("complete") is True
                and row.get("accepted") == row.get("target")
                and int(row["accepted"]) > 0
                for row in strata.values()
            ) and sum(int(row["accepted"]) for row in strata.values()) == 500
    except (TypeError, ValueError, KeyError):
        strata_valid = False
    dataset_raw, _dataset_info = _secure_formal_evidence_bytes(
        dataset, expected_mode=0o444, max_bytes=256 * 1024 * 1024
    )
    if hashlib.sha256(dataset_raw).hexdigest() != dataset_sha256:
        raise EvaluationError("formal dataset changed before revalidation")
    raw_rows = _strict_jsonl_rows(dataset_raw, label="formal dataset")
    raw_rows_valid = len(raw_rows) == 500 and all(
        isinstance(row, Mapping) for row in raw_rows
    )
    checks = {
        "schema_version": payload.get("schema_version") == 1,
        "builder": payload.get("builder") == "scripts/build_recent_journal_benchmark.py",
        "dataset_complete": _nested(payload, "dataset", "complete") is True,
        "record_count": _nested(payload, "dataset", "record_count") == 500,
        "dataset_sha256": _nested(payload, "dataset", "sha256") == dataset_sha256,
        "accepted_records": _nested(payload, "coverage", "accepted_records") == 500,
        "target_records": _nested(payload, "coverage", "target_records") == 500,
        "configured_sample_size": _nested(payload, "configuration", "sample_size")
        == 500,
        "fixed_builder_protocol": (
            _nested(payload, "configuration", "min_abstract_chars") == 300
            and _nested(payload, "configuration", "max_papers_per_journal") == 1
            and _nested(payload, "configuration", "seed")
            == "where-papers-go-recent-journals-v1"
            and _nested(payload, "configuration", "samples_per_stratum") == 10
        ),
        "dataset_relative_path": (
            _nested(payload, "dataset", "path") == dataset.name
            and (resolved.parent / dataset.name).resolve() == dataset.resolve()
        ),
        "dataset_format": _nested(payload, "dataset", "format") == "JSON Lines",
        "model_input_fields": _nested(payload, "dataset", "model_input_fields")
        == ["title", "abstract"],
        "label_fields": _nested(payload, "dataset", "label_fields")
        == [
            "gold_journal_id",
            "gold_entity_id",
            "gold_journal_name",
            "gold_issns",
            "gold_jcr_quartile",
            "gold_jcr_category",
        ],
        "source_crossref_https": (
            _nested(payload, "source", "name") == "Crossref REST API"
            and _nested(payload, "source", "base_url") == "https://api.crossref.org"
        ),
        "fixed_time_window": source_filters
        == {
            "from_pub_date": "2026-01-01",
            "has_abstract": True,
            "type": "journal-article",
            "until_pub_date": "2026-06-30",
        },
        "fixed_fields": fields
        == [
            "arts_humanities",
            "clinical_medicine",
            "computer_engineering",
            "earth_environment_agriculture",
            "life_sciences",
            "mathematics_statistics",
            "multidisciplinary_other",
            "physical_chemical_materials",
            "social_sciences",
        ],
        "fixed_quartiles": quartiles == ["Q1", "Q2", "Q3", "Q4"],
        "strata_complete": strata_valid,
        "coverage_strata": (
            _nested(payload, "coverage", "complete_strata") == 36
            and _nested(payload, "coverage", "covered_strata") == 36
            and _nested(payload, "coverage", "targeted_strata") == 36
        ),
        "catalog_universe": _nested(
            payload, "internal_catalog", "eligible_q1_q4_journals"
        )
        == 20087,
        "catalog_source_hashes": source_hashes == expected_source_hashes,
    }
    doi_values = [case.doi for case in cases]
    case_values = [case.case_id for case in cases]
    minimum_abstract = 300
    case_strata: dict[str, int] = {}
    for case in cases:
        key = f"{case.primary_field}/{case.gold_jcr_quartile}"
        case_strata[key] = case_strata.get(key, 0) + 1
    expected_strata = (
        {str(key): int(row["accepted"]) for key, row in strata.items()}
        if strata_valid and isinstance(strata, Mapping)
        else {}
    )
    checks.update(
        {
            "unique_nonempty_dois": (
                len(doi_values) == 500
                and all(doi_values)
                and len(set(doi_values)) == 500
                and all(
                    re.fullmatch(r"10\.\d{4,9}/\S+", doi) is not None
                    for doi in doi_values
                )
            ),
            "case_id_doi_identity": (
                len(case_values) == 500
                and len(set(case_values)) == 500
                and all(
                    case.case_id == f"doi:{case.doi}" for case in cases
                )
            ),
            "case_dates_in_window": all(
                _formal_date_in_window(case.published_date)
                for case in cases
            ),
            "case_inputs_and_labels_complete": all(
                len(case.title) >= 1
                and len(case.abstract) >= minimum_abstract
                and case.gold_entity_id is not None
                and any(valid_issn_token(value) for value in case.gold_issns)
                and case.gold_jcr_quartile in {"Q1", "Q2", "Q3", "Q4"}
                and case.primary_field in set(fields or ())
                for case in cases
            ),
            "case_strata_match_manifest": case_strata == expected_strata,
            "resolved_catalog_mapping_complete": (
                len(cases) == len(source_cases) == 500
                and all(case.gold_entity_id is not None for case in cases)
                and len({case.gold_entity_id for case in cases}) == 500
                and all(
                    source.gold_entity_id == resolved_case.gold_entity_id
                    for source, resolved_case in zip(source_cases, cases)
                )
            ),
            "unique_normalized_titles": len({_match_text(case.title) for case in cases})
            == 500,
            "raw_crossref_provenance": raw_rows_valid
            and all(
                row.get("article_type") == "journal-article"
                and row.get("source") == "crossref"
                and row.get("publication_date_precision") in {"day", "month"}
                and row.get("source_url")
                == "https://doi.org/"
                + urllib.parse.quote(
                    normalize_space(str(row.get("doi") or "")).casefold(), safe="/"
                )
                for row in raw_rows
                if isinstance(row, Mapping)
            ),
            "one_paper_per_journal": raw_rows_valid
            and len(
                {
                    str(row.get("gold_journal_id") or "")
                    for row in raw_rows
                    if isinstance(row, Mapping)
                }
            )
            == 500,
        }
    )
    if not all(checks.values()):
        failed = ", ".join(key for key, value in checks.items() if not value)
        raise EvaluationError(f"formal builder manifest is incomplete/mismatched: {failed}")
    acquisition_evidence = _validate_formal_acquisition_evidence(
        payload,
        builder_manifest=resolved,
        dataset=dataset,
        dataset_sha256=dataset_sha256,
        raw_rows=raw_rows,
    )
    return {
        "path": str(resolved),
        "sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "builder_source": {
            "path": "scripts/build_recent_journal_benchmark.py",
            "sha256": _file_sha256(
                PROJECT_ROOT / "scripts" / "build_recent_journal_benchmark.py"
            ),
        },
        "dataset_path": str(dataset),
        "validation": checks,
        "acquisition_evidence": acquisition_evidence,
        "collection_count_assurance": (
            "acquisition-time local evidence replayed and hard HTTP ceiling bound; "
            "not cryptographic attestation by Crossref"
        ),
    }


def _reviewed_plan_digest(payload: Mapping[str, Any]) -> str:
    return _canonical_sha256(payload)


def _authorization_registry_paths(
    registry_dir: Path, authorization_reference: str
) -> tuple[str, Path, Path]:
    identity = hashlib.sha256(
        ("where-papers-go/recent-journal-authorization/v1\0" + authorization_reference).encode(
            "utf-8"
        )
    ).hexdigest()
    return (
        identity,
        registry_dir / f"{identity}.registry.json",
        registry_dir / f"{identity}.external_call_ledger.jsonl",
    )


def _claim_or_verify_authorization_registry(
    registry_dir: Path,
    *,
    authorization_reference: str,
    approved_plan_digest: str,
    budget: int,
    attempt_cost_ceiling_usd: Decimal,
    authorized_max_cost_usd: Decimal,
    authorization_grant: Mapping[str, Any] | None,
    retry_errors: bool,
) -> tuple[dict[str, Any], Path]:
    registry_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if registry_dir.is_symlink() or not registry_dir.is_dir():
        raise EvaluationError("authorization registry must be a private real directory")
    os.chmod(registry_dir, 0o700)
    identity, entry_path, ledger_path = _authorization_registry_paths(
        registry_dir, authorization_reference
    )
    entry = {
        "schema_version": AUTHORIZATION_REGISTRY_SCHEMA_VERSION,
        "registry_identity": identity,
        "authorization_reference_sha256": hashlib.sha256(
            authorization_reference.encode("utf-8")
        ).hexdigest(),
        "approved_plan_digest": approved_plan_digest,
        "external_call_budget": budget,
        "external_attempt_cost_ceiling_usd": _decimal_text(
            attempt_cost_ceiling_usd
        ),
        "authorized_max_cost_usd": _decimal_text(authorized_max_cost_usd),
        "maximum_estimated_cost_usd": _decimal_text(
            attempt_cost_ceiling_usd * budget
        ),
        "authorization_grant_sha256": (
            authorization_grant.get("sha256") if authorization_grant else None
        ),
        "retry_errors_authorized": bool(retry_errors),
    }
    created = False
    try:
        _write_json_exclusive(entry_path, entry)
        created = True
    except FileExistsError:
        pass
    if created:
        try:
            initialize_external_call_ledger(
                ledger_path, budget=budget, run_id=identity
            )
        except Exception as exc:
            raise EvaluationError(
                "authorization registry was claimed but its global ledger could not be "
                "created; fail closed and preserve the claim for audit"
            ) from exc
    try:
        existing = json.loads(entry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvaluationError("authorization registry claim is unreadable") from exc
    if existing != entry:
        raise EvaluationError(
            "authorization reference is already bound to a different approved plan/budget"
        )
    status = external_call_ledger_status(ledger_path)
    if status["run_id"] != identity or status["budget"] != budget:
        raise EvaluationError("global authorization ledger identity/budget mismatch")
    reference = {
        "schema_version": AUTHORIZATION_REGISTRY_SCHEMA_VERSION,
        "registry_identity": identity,
        "registry_entry": str(entry_path.resolve()),
        "registry_entry_sha256": _file_sha256(entry_path),
        "global_ledger": str(ledger_path.resolve()),
        "approved_plan_digest": approved_plan_digest,
    }
    return reference, ledger_path


def _run_manifest_payload(
    *,
    run_id: str,
    dataset: Path,
    dataset_sha256: str,
    cases: Sequence[BenchmarkCase],
    tracks: Sequence[str],
    api_config: Path,
    authorization_reference_sha256: str,
    external_call_budget: int,
    attempt_cost_ceiling_usd: Decimal,
    authorized_max_cost_usd: Decimal,
    evaluation_mode: str,
    approved_plan_digest: str,
    runtime_bindings: Mapping[str, Any],
    cache_seeds: Mapping[str, Any],
    authorization_registry: Mapping[str, Any],
    builder_manifest: Mapping[str, Any] | None,
    authorization_grant: Mapping[str, Any] | None,
    retry_errors: bool,
    source_evidence: Mapping[str, Any] | None = None,
    shared_external_quota_initial: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": RUN_MANIFEST_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "dataset": str(dataset),
        "dataset_sha256": dataset_sha256,
        "api_config_sha256": _file_sha256(api_config),
        "case_count": len(cases),
        "tracks": list(tracks),
        "case_track_count": len(cases) * len(tracks),
        "selection_sha256": _selection_sha256(cases, tracks),
        "authorization_reference_sha256": authorization_reference_sha256,
        "approved_plan_digest": approved_plan_digest,
        "evaluation_mode": evaluation_mode,
        "formal_full_denominator": evaluation_mode == FORMAL_MODE,
        "builder_manifest": dict(builder_manifest or {}),
        "runtime_bindings": dict(runtime_bindings),
        "cache_seeds": dict(cache_seeds),
        "runtime_cache_layout": _runtime_cache_layout(),
        "source_evidence": dict(source_evidence or {}),
        "shared_external_quota_initial": dict(
            shared_external_quota_initial or {}
        ),
        "authorization_registry": dict(authorization_registry),
        "authorization_grant": dict(authorization_grant or {}),
        "retry_errors_authorized": bool(retry_errors),
        "external_call_budget": external_call_budget,
        "external_attempt_cost_ceiling_usd": _decimal_text(
            attempt_cost_ceiling_usd
        ),
        "authorized_max_cost_usd": _decimal_text(authorized_max_cost_usd),
        "maximum_estimated_cost_usd": _decimal_text(
            attempt_cost_ceiling_usd * external_call_budget
        ),
        "output_policy": (
            "immutable manifest; append-only generation segments; versioned closeouts; "
            "explicit verified resume only"
        ),
    }


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stage_bytes_for_publish(path: Path, encoded: bytes, *, mode: int) -> Path:
    temporary = path.with_name(f".{path.name}.building-{uuid.uuid4().hex}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short exclusive artifact staging write")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return temporary


def _publish_staged_exclusive(temporary: Path, path: Path) -> None:
    # A hardlink is an atomic no-replace commit marker. On any failure the
    # uniquely named staged inode remains available for audit/recovery.
    os.link(temporary, path, follow_symlinks=False)
    _fsync_directory(path.parent)
    temporary.unlink()
    _fsync_directory(path.parent)


def _publish_bytes_exclusive(path: Path, encoded: bytes, *, mode: int) -> None:
    temporary = _stage_bytes_for_publish(path, encoded, mode=mode)
    _publish_staged_exclusive(temporary, path)


def _write_json_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    _publish_bytes_exclusive(path, encoded, mode=0o444)


def _manifest_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    ignored = {"created_at"}
    return {key: value for key, value in payload.items() if key not in ignored}


def _verify_run_manifest(path: Path, expected: Mapping[str, Any]) -> None:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvaluationError(
            "resume requires a readable immutable run_manifest.json"
        ) from exc
    if not isinstance(existing, Mapping) or _manifest_identity(existing) != _manifest_identity(
        expected
    ):
        raise EvaluationError(
            "resume manifest does not exactly match dataset, selection, authorization, and budgets"
        )


def _clone_private_regular_file(source: Path, destination: Path) -> None:
    try:
        info = source.lstat()
    except OSError as exc:
        raise EvaluationError(f"frozen seed file is unreadable: {source}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise EvaluationError(f"frozen seed must be a regular file: {source}")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with source.open("rb") as input_handle, destination.open("xb") as output_handle:
        cloned = False
        if fcntl is not None:
            try:
                # Linux FICLONE: copy-on-write extent clone, never a hardlink.
                fcntl.ioctl(output_handle.fileno(), 0x40049409, input_handle.fileno())
                cloned = True
            except OSError:
                output_handle.seek(0)
                output_handle.truncate(0)
        if not cloned:
            shutil.copyfileobj(input_handle, output_handle, 1024 * 1024)
        output_handle.flush()
        os.fchmod(output_handle.fileno(), 0o600)
        os.fsync(output_handle.fileno())


def _source_evidence_plan(
    dataset: Path,
    *,
    builder_manifest: Path | None,
    authorization_grant: Path | None,
    additional_sources: Mapping[str, Path] | None = None,
) -> tuple[dict[str, Any], dict[str, Path]]:
    sources: dict[str, Path] = {"dataset": dataset}
    if builder_manifest is not None:
        sources["builder_manifest"] = builder_manifest
    if authorization_grant is not None:
        sources["authorization_grant"] = authorization_grant
    for name, source in (additional_sources or {}).items():
        if (
            not re.fullmatch(r"[a-z0-9_]{3,96}", str(name))
            or name in sources
        ):
            raise EvaluationError(f"invalid/duplicate source evidence key: {name}")
        sources[str(name)] = source
    plan: dict[str, Any] = {}
    for name, source in sources.items():
        snapshot = _stable_source_evidence_snapshot(source)
        if not snapshot["exists"]:
            raise EvaluationError(f"source evidence is missing: {source}")
        suffix = source.suffix or ".bin"
        relative = f"{SOURCE_EVIDENCE_DIR}/{name}{suffix}"
        plan[name] = {
            "path": relative,
            "sha256": snapshot["sha256"],
            "bytes": snapshot["bytes"],
        }
    return plan, sources


def _assert_source_evidence_file_binding(
    name: str, source: Path, binding: Mapping[str, Any]
) -> None:
    if set(binding) != {"path", "sha256", "bytes"}:
        raise EvaluationError(f"source evidence binding schema is not exact: {name}")
    snapshot = _stable_source_evidence_snapshot(source)
    if (
        not snapshot["exists"]
        or snapshot.get("sha256") != binding.get("sha256")
        or snapshot.get("bytes") != binding.get("bytes")
    ):
        raise EvaluationError(f"source evidence drifted: {name}")


def _assert_source_evidence_sources_match_plan(
    expected: Mapping[str, Any], sources: Mapping[str, Path]
) -> None:
    if set(expected) != set(sources):
        raise EvaluationError("source evidence source set does not match run manifest")
    for name, source in sources.items():
        binding = expected.get(name)
        if not isinstance(binding, Mapping):
            raise EvaluationError(f"run manifest does not bind source evidence: {name}")
        _assert_source_evidence_file_binding(str(name), source, binding)


def _formal_acquisition_source_bindings(
    builder_binding: Mapping[str, Any],
) -> tuple[dict[str, Path], dict[str, dict[str, Any]]]:
    bound_sources = _nested(builder_binding, "acquisition_evidence", "source_files")
    if not isinstance(bound_sources, Mapping) or not bound_sources:
        raise EvaluationError("formal acquisition evidence has no bound source files")
    sources: dict[str, Path] = {}
    expected: dict[str, dict[str, Any]] = {}
    for raw_name, raw_metadata in bound_sources.items():
        name = str(raw_name)
        if (
            not isinstance(raw_name, str)
            or not re.fullmatch(r"[a-z0-9_]{3,96}", name)
            or not isinstance(raw_metadata, Mapping)
            or set(raw_metadata) != {"path", "sha256", "bytes"}
        ):
            raise EvaluationError("formal acquisition source binding is malformed")
        path_text = raw_metadata.get("path")
        digest = raw_metadata.get("sha256")
        byte_count = raw_metadata.get("bytes")
        if (
            not isinstance(path_text, str)
            or path_text != os.path.abspath(path_text)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(byte_count, int)
            or isinstance(byte_count, bool)
            or byte_count < 0
        ):
            raise EvaluationError("formal acquisition source binding is malformed")
        source = Path(path_text)
        sources[name] = source
        expected[name] = {
            "path": path_text,
            "sha256": str(digest),
            "bytes": byte_count,
        }
    if len({metadata["path"] for metadata in expected.values()}) != len(expected):
        raise EvaluationError("formal acquisition source paths are not unique")
    return sources, expected


def _assert_formal_acquisition_sources_match_plan(
    expected: Mapping[str, Mapping[str, Any]],
    sources: Mapping[str, Path],
    plan: Mapping[str, Any],
) -> None:
    if set(expected) != set(sources):
        raise EvaluationError("formal acquisition source set drifted")
    for name, verified in expected.items():
        planned = plan.get(name)
        source = sources.get(name)
        if (
            source is None
            or str(Path(os.path.abspath(source))) != verified.get("path")
            or not isinstance(planned, Mapping)
            or set(planned) != {"path", "sha256", "bytes"}
            or planned.get("sha256") != verified.get("sha256")
            or planned.get("bytes") != verified.get("bytes")
        ):
            raise EvaluationError(
                f"formal acquisition source no longer matches verified binding: {name}"
            )


def _clone_bound_source_evidence_file(
    name: str,
    source: Path,
    destination: Path,
    binding: Mapping[str, Any],
) -> None:
    # Check both sides of the clone.  The destination digest additionally
    # catches a path replacement in the narrow interval around open(2).
    _assert_source_evidence_file_binding(name, source, binding)
    _clone_private_regular_file(source, destination)
    os.chmod(destination, 0o400)
    if (
        _file_sha256(destination) != binding.get("sha256")
        or destination.stat().st_size != binding.get("bytes")
    ):
        raise EvaluationError(f"source evidence drifted while cloning: {name}")
    _assert_source_evidence_file_binding(name, source, binding)


def _source_evidence_integrity_snapshot(
    output_dir: Path, expected: Mapping[str, Any]
) -> dict[str, Any]:
    evidence_dir = output_dir / SOURCE_EVIDENCE_DIR
    try:
        directory_info = evidence_dir.lstat()
    except OSError as exc:
        raise EvaluationError("source evidence directory is missing") from exc
    if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(directory_info.st_mode):
        raise EvaluationError("source evidence directory must be real")
    if stat.S_IMODE(directory_info.st_mode) != 0o700:
        raise EvaluationError("source evidence directory must be mode 0700")
    actual_names = {path.name for path in evidence_dir.iterdir()}
    expected_names = {
        Path(str(binding["path"])).name
        for binding in expected.values()
        if isinstance(binding, Mapping)
    }
    if actual_names != expected_names:
        raise EvaluationError("source evidence file set drifted")
    result: dict[str, Any] = {}
    for name, binding in expected.items():
        if not isinstance(binding, Mapping):
            raise EvaluationError("source evidence manifest is malformed")
        path = output_dir / str(binding.get("path") or "")
        snapshot = _regular_file_snapshot(path)
        mode = stat.S_IMODE(path.lstat().st_mode) if snapshot["exists"] else None
        observed = {
            "path": str(binding.get("path")),
            "sha256": snapshot.get("sha256"),
            "bytes": snapshot.get("bytes"),
            "mode": mode,
        }
        if (
            not snapshot["exists"]
            or mode != 0o400
            or snapshot.get("sha256") != binding.get("sha256")
            or snapshot.get("bytes") != binding.get("bytes")
        ):
            raise EvaluationError(f"source evidence drifted: {name}")
        result[str(name)] = observed
    return result


def _create_fresh_output(
    output_dir: Path,
    *,
    manifest: Mapping[str, Any],
    api_config: Path,
    api_cache_seed_dir: Path,
    query_embedding_cache_seed: Path,
    lightrag_embedding_cache_seed: Path,
    lightrag_working_dir_seed: Path,
    source_evidence_sources: Mapping[str, Path],
) -> dict[str, Path]:
    """Shadow-seed private caches, then claim a never-before-existing output."""

    expected_source_evidence = manifest.get("source_evidence")
    if not isinstance(expected_source_evidence, Mapping):
        raise EvaluationError("run manifest has no source evidence binding")
    _assert_source_evidence_sources_match_plan(
        expected_source_evidence, source_evidence_sources
    )
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    building = output_dir.with_name(f".{output_dir.name}.building-{uuid.uuid4().hex}")
    building.mkdir(mode=0o700)
    building_manifest = building / RUN_MANIFEST_FILE
    _write_json_exclusive(building_manifest, manifest)
    runtime_dir = building / RUNTIME_CACHE_DIR
    runtime_dir.mkdir(mode=0o700)
    api_cache = runtime_dir / "api_cache"
    api_cache.mkdir(mode=0o700)
    if api_cache_seed_dir.exists():
        for source in sorted(api_cache_seed_dir.rglob("*"), key=lambda item: item.as_posix()):
            if source.is_symlink():
                raise EvaluationError(f"cache seed contains a symlink: {source}")
            relative = source.relative_to(api_cache_seed_dir)
            destination = api_cache / relative
            if source.is_dir():
                destination.mkdir(mode=0o700, exist_ok=True)
            elif source.is_file():
                _clone_private_regular_file(source, destination)
            else:
                raise EvaluationError(f"cache seed contains a non-regular file: {source}")

    runtime_files = {
        "query_embedding_cache": runtime_dir / "query_embedding_cache.json.gz",
        "lightrag_embedding_cache": runtime_dir / "lightrag_embedding_cache.json.gz",
        "api_config_snapshot": runtime_dir / "api_config.snapshot.json",
    }
    for source, name in (
        (query_embedding_cache_seed, "query_embedding_cache"),
        (lightrag_embedding_cache_seed, "lightrag_embedding_cache"),
        (api_config, "api_config_snapshot"),
    ):
        destination = runtime_files[name]
        if not source.exists() and name != "api_config_snapshot":
            continue
        _clone_private_regular_file(source, destination)
    lightrag_runtime_dir = runtime_dir / "lightrag_storage"
    lightrag_runtime_dir.mkdir(mode=0o700)
    for name in LIGHTRAG_WORKSPACE_FILES:
        _clone_private_regular_file(
            lightrag_working_dir_seed / name,
            lightrag_runtime_dir / name,
        )
    _fsync_directory(lightrag_runtime_dir)
    _clone_private_regular_file(
        DATA_DIR / "venue_graph.json.gz",
        runtime_dir / "venue_graph.json.gz",
    )
    _clone_private_regular_file(
        DATA_DIR / "venue_graph_vectors.json.gz",
        runtime_dir / "venue_graph_vectors.json.gz",
    )
    _fsync_directory(runtime_dir)
    source_evidence_dir = building / SOURCE_EVIDENCE_DIR
    source_evidence_dir.mkdir(mode=0o700)
    for name, source in source_evidence_sources.items():
        binding = expected_source_evidence.get(name)
        if not isinstance(binding, Mapping):
            raise EvaluationError(f"run manifest does not bind source evidence: {name}")
        relative = Path(str(binding.get("path") or ""))
        if (
            relative.parent != Path(SOURCE_EVIDENCE_DIR)
            or relative.name in {"", ".", ".."}
        ):
            raise EvaluationError("unsafe source evidence destination")
        destination = building / relative
        _clone_bound_source_evidence_file(str(name), source, destination, binding)
    _assert_source_evidence_sources_match_plan(
        expected_source_evidence, source_evidence_sources
    )
    _fsync_directory(source_evidence_dir)
    generation_dir = building / GENERATION_DIR
    generation_dir.mkdir(mode=0o700)
    building_fd = os.open(building, os.O_RDONLY)
    try:
        os.fsync(building_fd)
    finally:
        os.close(building_fd)

    # The mkdir is the no-replace claim.  Existing outputs are never renamed
    # over.  Fully seeded subtrees move into the claimed directory atomically;
    # a crash leaves the unique sibling .building directory for audit/recovery.
    output_dir.mkdir(mode=0o700)
    _fsync_directory(output_dir.parent)
    os.link(building_manifest, output_dir / RUN_MANIFEST_FILE)
    os.rename(runtime_dir, output_dir / RUNTIME_CACHE_DIR)
    os.rename(source_evidence_dir, output_dir / SOURCE_EVIDENCE_DIR)
    os.rename(generation_dir, output_dir / GENERATION_DIR)
    _fsync_directory(output_dir)
    _fsync_directory(output_dir.parent)
    building_manifest.unlink()
    building.rmdir()
    _fsync_directory(output_dir.parent)
    _source_evidence_integrity_snapshot(output_dir, expected_source_evidence)
    return _runtime_cache_paths(output_dir)


def _runtime_cache_paths(output_dir: Path) -> dict[str, Path]:
    runtime_dir = output_dir / RUNTIME_CACHE_DIR
    return {
        "api_cache_dir": runtime_dir / "api_cache",
        "query_embedding_cache": runtime_dir / "query_embedding_cache.json.gz",
        "lightrag_embedding_cache": runtime_dir / "lightrag_embedding_cache.json.gz",
        "api_config_snapshot": runtime_dir / "api_config.snapshot.json",
        "lightrag_working_dir": runtime_dir / "lightrag_storage",
        "graph_path": runtime_dir / "venue_graph.json.gz",
        "vector_path": runtime_dir / "venue_graph_vectors.json.gz",
    }


def _private_file_integrity(path: Path) -> dict[str, Any]:
    snapshot = _regular_file_snapshot(path)
    if not snapshot["exists"]:
        return {key: value for key, value in snapshot.items() if key != "path"}
    mode = stat.S_IMODE(path.lstat().st_mode)
    if mode & 0o077:
        raise EvaluationError(f"runtime cache file is not private: {path}")
    return {
        key: value for key, value in snapshot.items() if key != "path"
    } | {"mode": mode}


def _private_directory_integrity(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise EvaluationError(f"runtime cache directory is missing: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise EvaluationError(f"runtime cache directory must be real: {path}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise EvaluationError(f"runtime cache directory is not private: {path}")
    for candidate in path.rglob("*"):
        candidate_info = candidate.lstat()
        if stat.S_ISLNK(candidate_info.st_mode):
            raise EvaluationError(f"runtime cache contains a symlink: {candidate}")
        if stat.S_IMODE(candidate_info.st_mode) & 0o077:
            raise EvaluationError(f"runtime cache entry is not private: {candidate}")
    snapshot = _directory_snapshot(path)
    return {key: value for key, value in snapshot.items() if key != "path"} | {
        "mode": stat.S_IMODE(info.st_mode)
    }


def _runtime_cache_integrity_snapshot(
    runtime_paths: Mapping[str, Path],
) -> dict[str, Any]:
    workspace = _lightrag_workspace_snapshot(runtime_paths["lightrag_working_dir"])
    workspace_files = {
        name: _private_file_integrity(runtime_paths["lightrag_working_dir"] / name)
        for name in LIGHTRAG_WORKSPACE_FILES
    }
    del workspace
    return {
        "api_cache": _private_directory_integrity(runtime_paths["api_cache_dir"]),
        "query_embedding_cache": _private_file_integrity(
            runtime_paths["query_embedding_cache"]
        ),
        "lightrag_embedding_cache": _private_file_integrity(
            runtime_paths["lightrag_embedding_cache"]
        ),
        "api_config_snapshot": _private_file_integrity(
            runtime_paths["api_config_snapshot"]
        ),
        "lightrag_workspace": {
            "mode": stat.S_IMODE(
                runtime_paths["lightrag_working_dir"].lstat().st_mode
            ),
            "files": workspace_files,
        },
        "graph_artifact": _private_file_integrity(runtime_paths["graph_path"]),
        "vector_artifact": _private_file_integrity(runtime_paths["vector_path"]),
    }


def _closeout_paths(output_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for candidate in output_dir.iterdir():
        if not candidate.name.startswith("closeout.generation-"):
            continue
        if not re.fullmatch(r"closeout\.generation-\d{6}\.json", candidate.name):
            raise EvaluationError(f"invalid closeout artifact name: {candidate.name}")
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise EvaluationError(f"closeout must be a regular file: {candidate}")
        if stat.S_IMODE(info.st_mode) != 0o444:
            raise EvaluationError(f"closeout must be immutable mode 0444: {candidate}")
        paths.append(candidate)
    paths.sort(key=lambda item: item.name)
    generations = [int(path.stem.rsplit("-", 1)[1]) for path in paths]
    if generations != list(range(1, len(paths) + 1)):
        raise EvaluationError("closeouts must be contiguous from generation 1")
    return paths


def _closeout_anchor_path(
    registry_dir: Path,
    *,
    registry_identity: str,
    output_dir: Path,
    generation: int,
) -> Path:
    output_identity = _file_sha256(output_dir / RUN_MANIFEST_FILE)
    return registry_dir / (
        f"{registry_identity}.{output_identity}."
        f"closeout-{generation:06d}.anchor.json"
    )


def _write_or_verify_closeout_anchor(
    registry_dir: Path,
    *,
    registry_identity: str,
    output_dir: Path,
    generation: int,
    closeout_path: Path,
    create: bool,
    closeout_sha256: str | None = None,
) -> None:
    if create:
        registry_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(registry_dir, 0o700)
    if registry_dir.is_symlink() or not registry_dir.is_dir():
        raise EvaluationError("closeout anchor registry must be a real directory")
    if stat.S_IMODE(registry_dir.lstat().st_mode) != 0o700:
        raise EvaluationError("closeout anchor registry must be private mode 0700")
    payload = {
        "schema_version": CLOSEOUT_ANCHOR_SCHEMA_VERSION,
        "registry_identity": registry_identity,
        "output_identity_sha256": _file_sha256(
            output_dir / RUN_MANIFEST_FILE
        ),
        "generation": generation,
        "run_manifest_sha256": _file_sha256(output_dir / RUN_MANIFEST_FILE),
        "closeout_sha256": closeout_sha256 or _file_sha256(closeout_path),
    }
    anchor_path = _closeout_anchor_path(
        registry_dir,
        registry_identity=registry_identity,
        output_dir=output_dir,
        generation=generation,
    )
    if not anchor_path.exists():
        if not create:
            raise EvaluationError("external closeout anchor is missing")
        _write_json_exclusive(anchor_path, payload)
    existing = _read_json_object(anchor_path, label="closeout anchor")
    if existing != payload:
        raise EvaluationError("external closeout anchor mismatch")
    info = anchor_path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise EvaluationError("external closeout anchor must be a regular file")
    if stat.S_IMODE(info.st_mode) != 0o444:
        raise EvaluationError("external closeout anchor must be immutable mode 0444")


def _publish_closeout_with_anchor(
    closeout_path: Path,
    payload: Mapping[str, Any],
    *,
    anchor_registry_dir: Path,
    registry_identity: str,
    output_dir: Path,
    generation: int,
) -> None:
    expected_manifest_sha256 = payload.get("run_manifest_sha256")
    if (
        not isinstance(expected_manifest_sha256, str)
        or _file_sha256(output_dir / RUN_MANIFEST_FILE) != expected_manifest_sha256
    ):
        raise EvaluationError(
            "run manifest drifted; refusing to publish or anchor a closeout"
        )
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    staged = _stage_bytes_for_publish(closeout_path, encoded, mode=0o444)
    staged_sha256 = _file_sha256(staged)
    _write_or_verify_closeout_anchor(
        anchor_registry_dir,
        registry_identity=registry_identity,
        output_dir=output_dir,
        generation=generation,
        closeout_path=staged,
        closeout_sha256=staged_sha256,
        create=True,
    )
    _publish_staged_exclusive(staged, closeout_path)


def _pending_staged_closeout(
    output_dir: Path,
    *,
    anchor_registry_dir: Path,
    registry_identity: str,
    generation: int,
) -> tuple[Path, Path] | None:
    final_path = output_dir / f"closeout.generation-{generation:06d}.json"
    if final_path.exists():
        return None
    anchor_path = _closeout_anchor_path(
        anchor_registry_dir,
        registry_identity=registry_identity,
        output_dir=output_dir,
        generation=generation,
    )
    if not anchor_path.exists():
        return None
    anchor = _read_json_object(anchor_path, label="closeout anchor")
    expected_sha256 = anchor.get("closeout_sha256")
    candidates = [
        path
        for path in output_dir.glob(
            f".closeout.generation-{generation:06d}.json.building-*"
        )
        if path.is_file()
        and not path.is_symlink()
        and _file_sha256(path) == expected_sha256
    ]
    if len(candidates) != 1:
        raise EvaluationError(
            "anchored closeout publish is incomplete and has no unique staged inode"
        )
    if stat.S_IMODE(candidates[0].lstat().st_mode) != 0o444:
        raise EvaluationError("anchored staged closeout is not immutable mode 0444")
    return candidates[0], final_path


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"{label} is unreadable: {path}") from exc
    if not isinstance(payload, Mapping):
        raise EvaluationError(f"{label} must contain a JSON object: {path}")
    return dict(payload)


def _verify_closeout_artifact(
    output_dir: Path, reference: Any, *, expected_name: str
) -> None:
    if not isinstance(reference, Mapping) or set(reference) != {"path", "sha256"}:
        raise EvaluationError(f"closeout artifact reference is invalid: {expected_name}")
    if reference.get("path") != expected_name:
        raise EvaluationError(f"closeout artifact path mismatch: {expected_name}")
    path = output_dir / expected_name
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise EvaluationError(f"closeout artifact must be regular: {expected_name}")
    if stat.S_IMODE(info.st_mode) != 0o444:
        raise EvaluationError(f"closeout artifact must be immutable: {expected_name}")
    if reference.get("sha256") != _file_sha256(path):
        raise EvaluationError(f"closeout artifact hash mismatch: {expected_name}")


def _orphan_output_audits(output_dir: Path, generation: int) -> list[dict[str, Any]]:
    stem = f"generation-{generation:06d}"
    result: list[dict[str, Any]] = []
    for name in (f"summary.{stem}.json", f"summary.{stem}.md"):
        path = output_dir / name
        if not path.exists():
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise EvaluationError(f"unclean orphan output is not regular: {name}")
        result.append(
            {
                "path": name,
                "bytes": info.st_size,
                "mode": stat.S_IMODE(info.st_mode),
                "sha256": _file_sha256(path),
            }
        )
    return result


def _evaluation_exit_code(
    *, interrupted: bool, fatal_error: Any, outcomes: Mapping[str, Any]
) -> int:
    if interrupted:
        return 130
    if int(outcomes.get("missing", 0)):
        return 4
    if fatal_error is not None or int(outcomes.get("error", 0)):
        return 3
    return 0


def _verify_closeout_chain(
    output_dir: Path,
    *,
    run_id: str,
    expected_keys: set[tuple[str, str]],
    runtime_paths: Mapping[str, Path],
    anchor_registry_dir: Path,
    registry_identity: str,
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, Any] | None,
    list[Path],
    tuple[Path, Path] | None,
]:
    manifest_payload = _read_json_object(
        output_dir / RUN_MANIFEST_FILE, label="run manifest"
    )
    source_evidence = manifest_payload.get("source_evidence")
    if not isinstance(source_evidence, Mapping) or not source_evidence:
        raise EvaluationError("run manifest has no immutable source evidence")
    current_source_evidence = _source_evidence_integrity_snapshot(
        output_dir, source_evidence
    )
    quota_previous = manifest_payload.get("shared_external_quota_initial")
    if not isinstance(quota_previous, Mapping) or not quota_previous:
        raise EvaluationError("run manifest has no shared quota binding")
    segment_paths = _generation_paths(output_dir)
    closeout_paths = _closeout_paths(output_dir)
    pending_publish = _pending_staged_closeout(
        output_dir,
        anchor_registry_dir=anchor_registry_dir,
        registry_identity=registry_identity,
        generation=len(closeout_paths) + 1,
    )
    if pending_publish is not None:
        closeout_paths.append(pending_publish[0])
    if len(segment_paths) not in {len(closeout_paths), len(closeout_paths) + 1}:
        raise EvaluationError(
            "resume permits only a contiguous closed prefix and at most one newest unclosed segment"
        )
    manifest_sha256 = _file_sha256(output_dir / RUN_MANIFEST_FILE)
    previous_closeout_sha256: str | None = None
    head: dict[str, Any] | None = None
    recovered_tail_generations: set[int] = set()
    for generation, closeout_path in enumerate(closeout_paths, 1):
        payload = _read_json_object(closeout_path, label="closeout")
        expected_previous = previous_closeout_sha256
        if (
            payload.get("schema_version") != "2"
            or payload.get("run_id") != run_id
            or payload.get("generation") != generation
            or payload.get("run_manifest_sha256") != manifest_sha256
            or payload.get("previous_closeout_sha256") != expected_previous
            or payload.get("source_evidence_final") != current_source_evidence
        ):
            raise EvaluationError(
                f"closeout hash-chain/core mismatch: {closeout_path.name}"
            )
        quota_final = payload.get("shared_external_quota_final")
        if not isinstance(quota_final, Mapping):
            raise EvaluationError(
                f"closeout has no shared quota audit: {closeout_path.name}"
            )
        _assert_quota_snapshot_monotonic(
            quota_final, quota_previous, label=f"closeout-{generation}"
        )
        quota_previous = quota_final
        _records, prefix_outcomes, prefix_audits = _scan_generation_segments(
            segment_paths[:generation], run_id=run_id, expected_keys=expected_keys
        )
        if any(audit["mode"] != 0o400 for audit in prefix_audits):
            raise EvaluationError("closed generation segment is not immutable mode 0400")
        if payload.get("segments") != prefix_audits or payload.get(
            "current_segment"
        ) != prefix_audits[-1]:
            raise EvaluationError(
                f"closeout segment audit mismatch: {closeout_path.name}"
            )
        if payload.get("outcomes") != prefix_outcomes:
            raise EvaluationError(
                f"closeout outcome audit mismatch: {closeout_path.name}"
            )
        closure_kind = payload.get("closure_kind")
        stem = f"generation-{generation:06d}"
        if any(
            audit["incomplete_tail_ignored"]
            and audit["generation"] not in recovered_tail_generations
            for audit in prefix_audits[:-1]
        ):
            raise EvaluationError(
                "an incomplete tail lacks a recovered-unclean closeout"
            )
        if closure_kind == "normal":
            if prefix_audits[-1]["incomplete_tail_ignored"]:
                raise EvaluationError("normal closeout cannot ignore an incomplete tail")
            expected_exit = _evaluation_exit_code(
                interrupted=payload.get("interrupted") is True,
                fatal_error=payload.get("fatal_error"),
                outcomes=prefix_outcomes,
            )
            if payload.get("exit_code") != expected_exit:
                raise EvaluationError(
                    f"closeout exit status mismatch: {closeout_path.name}"
                )
            _verify_closeout_artifact(
                output_dir,
                payload.get("summary"),
                expected_name=f"summary.{stem}.json",
            )
            _verify_closeout_artifact(
                output_dir,
                payload.get("report"),
                expected_name=f"summary.{stem}.md",
            )
            summary_payload = _read_json_object(
                output_dir / f"summary.{stem}.json", label="summary"
            )
            for key, expected_value in (
                ("run_id", run_id),
                ("generation", generation),
                ("exit_code", expected_exit),
                ("fatal_error", payload.get("fatal_error")),
                ("execution_outcomes", prefix_outcomes),
            ):
                if summary_payload.get(key) != expected_value:
                    raise EvaluationError(
                        f"summary/closeout core mismatch: {closeout_path.name}"
                    )
        elif closure_kind == "recovered_unclean":
            if payload.get("summary") is not None or payload.get("report") is not None:
                raise EvaluationError("recovered-unclean closeout cannot claim a summary")
            if (
                payload.get("exit_code") != 3
                or not isinstance(payload.get("fatal_error"), str)
                or not payload["fatal_error"]
                or payload.get("interrupted") is not False
                or payload.get("orphan_outputs")
                != _orphan_output_audits(output_dir, generation)
            ):
                raise EvaluationError(
                    f"recovered-unclean closeout status/evidence mismatch: {closeout_path.name}"
                )
            recovered_tail_generations.add(generation)
        else:
            raise EvaluationError(f"unknown closeout closure_kind: {closure_kind}")
        previous_closeout_sha256 = _file_sha256(closeout_path)
        _write_or_verify_closeout_anchor(
            anchor_registry_dir,
            registry_identity=registry_identity,
            output_dir=output_dir,
            generation=generation,
            closeout_path=closeout_path,
            create=False,
        )
        head = payload
    unclosed = segment_paths[len(closeout_paths) :]
    if not unclosed and head is not None:
        if head.get("runtime_cache_final") != _runtime_cache_integrity_snapshot(
            runtime_paths
        ):
            raise EvaluationError(
                "run-local runtime cache drifted after the latest committed closeout"
            )
    records, outcomes, audits = _scan_generation_segments(
        segment_paths, run_id=run_id, expected_keys=expected_keys
    )
    return records, outcomes, audits, head, unclosed, pending_publish


def _recover_unclosed_generation(
    output_dir: Path,
    *,
    segment_path: Path,
    run_id: str,
    expected_keys: set[tuple[str, str]],
    runtime_paths: Mapping[str, Path],
    ledger_path: Path,
    anchor_registry_dir: Path,
    registry_identity: str,
    shared_external_quota_final: Mapping[str, Any],
) -> None:
    generation = int(segment_path.stem.rsplit("-", 1)[1])
    closeout_path = output_dir / f"closeout.generation-{generation:06d}.json"
    if closeout_path.exists():
        raise EvaluationError("unclean recovery closeout already exists")
    _seal_generation_segment(segment_path)
    records, outcomes, audits = _scan_generation_segments(
        _generation_paths(output_dir), run_id=run_id, expected_keys=expected_keys
    )
    del records
    current = audits[-1]
    if current["generation"] != generation:
        raise EvaluationError("unclean recovery generation mismatch")
    previous_path = (
        output_dir / f"closeout.generation-{generation - 1:06d}.json"
        if generation > 1
        else None
    )
    previous_sha256 = _file_sha256(previous_path) if previous_path else None
    stem = f"generation-{generation:06d}"
    orphan_outputs = _orphan_output_audits(output_dir, generation)
    try:
        ledger_status = external_call_ledger_status(ledger_path)
    except RuntimeError as exc:
        raise EvaluationError("global ledger unreadable during unclean recovery") from exc
    closeout = {
        "schema_version": "2",
        "run_id": run_id,
        "generation": generation,
        "closure_kind": "recovered_unclean",
        "closed_at": datetime.now(timezone.utc).isoformat(),
        "run_manifest_sha256": _file_sha256(output_dir / RUN_MANIFEST_FILE),
        "previous_closeout_sha256": previous_sha256,
        "exit_code": 3,
        "interrupted": False,
        "fatal_error": (
            "recovered after an unclean shutdown; the previous process outcome is unknown"
        ),
        "outcomes": outcomes,
        "segments": audits,
        "current_segment": current,
        "summary": None,
        "report": None,
        "orphan_outputs": orphan_outputs,
        "global_ledger_status": ledger_status,
        "runtime_cache_observation": "observed_on_resume_after_unclean_shutdown",
        "runtime_cache_final": _runtime_cache_integrity_snapshot(runtime_paths),
        "shared_external_quota_final": dict(shared_external_quota_final),
        "source_evidence_final": _source_evidence_integrity_snapshot(
            output_dir,
            _read_json_object(
                output_dir / RUN_MANIFEST_FILE, label="run manifest"
            )["source_evidence"],
        ),
    }
    _publish_closeout_with_anchor(
        closeout_path,
        closeout,
        anchor_registry_dir=anchor_registry_dir,
        registry_identity=registry_identity,
        output_dir=output_dir,
        generation=generation,
    )


def _acquire_output_lock(output_dir: Path) -> int:
    if fcntl is None:  # pragma: no cover
        raise EvaluationError(
            "process-safe evaluation output locking is unavailable; refusing live run"
        )
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(output_dir / RUN_LOCK_FILE, flags, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise EvaluationError(
            "another evaluator owns this output directory; refusing concurrent resume"
        ) from exc
    assert descriptor is not None
    return descriptor


_SUMMARY_RECORD_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "generation",
        "case_id",
        "track",
        "status",
        "catalog_covered",
        "gold_entity_id",
        "gold_journal_name",
        "gold_jcr_quartile",
        "primary_field",
        "mapping_method",
        "final_gold_rank",
        "preliminary_gold_rank",
        "recall_pool_gold_rank",
        "latency_ms",
        "preliminary_latency_ms",
        "leakage",
        "error",
    }
)


def _compact_summary_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return {key: record.get(key) for key in _SUMMARY_RECORD_KEYS if key in record}


def _generation_paths(output_dir: Path) -> list[Path]:
    directory = output_dir / GENERATION_DIR
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise EvaluationError("generation path must be a real directory")
    paths = sorted(directory.iterdir(), key=lambda item: item.name)
    for path in paths:
        if path.is_symlink() or not path.is_file() or not re.fullmatch(
            r"generation-\d{6}\.jsonl", path.name
        ):
            raise EvaluationError(f"invalid generation segment: {path}")
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise EvaluationError(f"generation segment is not private: {path}")
    generations = [int(path.stem.rsplit("-", 1)[1]) for path in paths]
    if generations != list(range(1, len(paths) + 1)):
        raise EvaluationError("generation segments must be contiguous from generation 1")
    return paths


def _create_generation_segment(output_dir: Path) -> tuple[int, Path]:
    existing = _generation_paths(output_dir)
    generation = (
        max(int(path.stem.rsplit("-", 1)[1]) for path in existing) + 1
        if existing
        else 1
    )
    path = output_dir / GENERATION_DIR / f"generation-{generation:06d}.jsonl"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)
    return generation, path


def _seal_generation_segment(path: Path) -> None:
    """Make one completed/crashed segment byte-immutable before later resumes."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise EvaluationError("generation segment must be a private regular file")
        os.fchmod(descriptor, 0o400)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _scan_generation_segments(
    paths: Sequence[Path],
    *,
    run_id: str,
    expected_keys: set[tuple[str, str]],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    ever_failed: set[tuple[str, str]] = set()
    attempted_records = 0
    audits: list[dict[str, Any]] = []
    for path in paths:
        generation = int(path.stem.rsplit("-", 1)[1])
        tail_ignored = False
        tail_bytes = 0
        tail_sha256: str | None = None
        valid_prefix_bytes = 0
        valid_records = 0
        segment_keys: set[tuple[str, str]] = set()
        try:
            with path.open("rb") as handle:
                line_number = 0
                while True:
                    line = handle.readline(MAX_SEGMENT_RECORD_BYTES + 1)
                    if not line:
                        break
                    if len(line) > MAX_SEGMENT_RECORD_BYTES:
                        raise EvaluationError(
                            f"segment record exceeds size limit: {path.name} line {line_number + 1}"
                        )
                    line_number += 1
                    if not line.endswith(b"\n"):
                        tail_ignored = True
                        tail_bytes = len(line)
                        tail_sha256 = hashlib.sha256(line).hexdigest()
                        break
                    valid_prefix_bytes += len(line)
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except (UnicodeError, json.JSONDecodeError) as exc:
                        raise EvaluationError(
                            f"corrupt immutable segment {path.name} line {line_number}"
                        ) from exc
                    if not isinstance(record, Mapping):
                        raise EvaluationError(
                            f"invalid immutable segment object {path.name} line {line_number}"
                        )
                    if str(record.get("run_id")) != run_id:
                        raise EvaluationError(f"foreign run ID in segment {path.name}")
                    if record.get("generation") != generation:
                        raise EvaluationError(
                            f"generation mismatch in segment {path.name} line {line_number}"
                        )
                    key = (str(record.get("case_id")), str(record.get("track")))
                    if key not in expected_keys:
                        raise EvaluationError(f"unexpected case-track in segment {path.name}: {key}")
                    status_value = str(record.get("status"))
                    if status_value not in {"ok", "error"}:
                        raise EvaluationError(
                            f"invalid outcome status in segment {path.name}: {status_value}"
                        )
                    if key in segment_keys:
                        raise EvaluationError(
                            f"duplicate case-track in segment {path.name}: {key}"
                        )
                    segment_keys.add(key)
                    if key in latest and latest[key].get("status") != "error":
                        raise EvaluationError(
                            f"case-track retried after a non-error outcome in {path.name}: {key}"
                        )
                    compact = _compact_summary_record(record)
                    latest[key] = compact
                    if status_value == "error":
                        ever_failed.add(key)
                    attempted_records += 1
                    valid_records += 1
        except OSError as exc:
            raise EvaluationError(f"cannot scan immutable segment: {path}") from exc
        audits.append(
            {
                "path": f"{GENERATION_DIR}/{path.name}",
                "generation": generation,
                "bytes": path.stat().st_size,
                "sha256": _file_sha256(path),
                "mode": stat.S_IMODE(path.stat().st_mode),
                "valid_records": valid_records,
                "valid_prefix_bytes": valid_prefix_bytes,
                "incomplete_tail_ignored": tail_ignored,
                "incomplete_tail_bytes": tail_bytes,
                "incomplete_tail_sha256": tail_sha256,
            }
        )
    latest_ok = {key for key, record in latest.items() if record.get("status") == "ok"}
    latest_error = set(latest) - latest_ok
    stats = {
        "attempt_records": attempted_records,
        "attempted": len(latest),
        "ok": len(latest_ok),
        "error": len(latest_error),
        "missing": len(expected_keys - set(latest)),
        "ever_failed": len(ever_failed),
        "recovered": len(ever_failed & latest_ok),
    }
    return list(latest.values()), stats, audits


def _write_text_exclusive(path: Path, text: str, *, mode: int = 0o444) -> None:
    encoded = text.encode("utf-8")
    _publish_bytes_exclusive(path, encoded, mode=mode)


def make_run_id(
    dataset: Path,
    api_config: Path,
    *,
    preliminary_k: int,
    api_timeout: int,
    title_similarity_threshold: float,
    skip_explanations: bool = False,
    lightrag_working_dir: Path | None = None,
) -> tuple[str, str]:
    dataset_digest = _file_sha256(dataset)
    working_dir = (
        lightrag_working_dir or default_lightrag_working_dir(DATA_DIR)
    ).resolve()
    identity = {
        "schema": SCHEMA_VERSION,
        "dataset_sha256": dataset_digest,
        "targets": TARGETS,
        "record_type": "journal",
        "preliminary_k": preliminary_k,
        "final_scoring_k": FINAL_K,
        "api_timeout": api_timeout,
        "title_similarity_threshold": title_similarity_threshold,
        "skip_explanations": bool(skip_explanations),
        "api_config_sha256": _optional_file_sha256(api_config),
        "catalog_graph_source_digest": graph_source_digest(DATA_DIR),
        "pipeline_source_tree": _source_tree_snapshot(),
        "dependency_environment": _dependency_environment_snapshot(),
        "evaluator_source_sha256": _optional_file_sha256(Path(__file__)),
        "lightrag_manifest_sha256": _optional_file_sha256(
            working_dir / LIGHTRAG_MANIFEST_FILE
        ),
        "lightrag_query_store_sha256": {
            name: _optional_file_sha256(working_dir / name)
            for name in QUERY_STORAGE_FILES
        },
        "graph_artifact_sha256": _optional_file_sha256(
            DATA_DIR / "venue_graph.json.gz"
        ),
        "vector_artifact_sha256": _optional_file_sha256(
            DATA_DIR / "venue_graph_vectors.json.gz"
        ),
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:20], dataset_digest


def build_summary(
    records: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    dataset: Path,
    dataset_sha256: str,
    expected_case_count: int,
    tracks: Sequence[str],
    preliminary_k: int,
    interrupted: bool,
    expected_case_ids: Sequence[str] | None = None,
    expected_case_metadata: Mapping[str, Mapping[str, Any]] | None = None,
    execution_outcomes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    allowed_case_ids = set(expected_case_ids or ())
    allowed_tracks = set(tracks)
    current = [
        record
        for record in records
        if record.get("run_id") == run_id
        and (not allowed_case_ids or str(record.get("case_id")) in allowed_case_ids)
        and str(record.get("track")) in allowed_tracks
    ]
    by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    for record in current:
        by_key[(str(record.get("case_id")), str(record.get("track")))] = record
    deduped = list(by_key.values())
    present_keys = set(by_key)
    attempted_case_tracks = len(present_keys)
    if expected_case_ids:
        for case_id in expected_case_ids:
            for track in tracks:
                key = (str(case_id), str(track))
                if key not in present_keys:
                    expected = dict((expected_case_metadata or {}).get(str(case_id), {}))
                    deduped.append(
                        {
                            **expected,
                            "run_id": run_id,
                            "case_id": case_id,
                            "track": track,
                            "status": "missing",
                            "error": "evaluation not completed",
                        }
                    )
    track_summaries: dict[str, Any] = {}
    for track in tracks:
        rows = [record for record in deduped if record.get("track") == track]
        track_summaries[track] = {
            **summarize_records(rows, preliminary_k=preliminary_k),
            "by_quartile": stratified_summary(
                rows, "gold_jcr_quartile", preliminary_k=preliminary_k
            ),
            "by_field": stratified_summary(
                rows, "primary_field", preliminary_k=preliminary_k
            ),
        }
    outcomes = dict(execution_outcomes or {})
    if not outcomes:
        latest_ok = sum(record.get("status") == "ok" for record in by_key.values())
        outcomes = {
            "attempt_records": len(current),
            "attempted": attempted_case_tracks,
            "ok": latest_ok,
            "error": attempted_case_tracks - latest_ok,
            "missing": expected_case_count * len(tracks) - attempted_case_tracks,
            "ever_failed": sum(record.get("status") == "error" for record in by_key.values()),
            "recovered": 0,
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "dataset": str(dataset.resolve()),
        "dataset_sha256": dataset_sha256,
        "expected_case_count": expected_case_count,
        "tracks": list(tracks),
        "targets": list(TARGETS),
        "record_type": "journal",
        "preliminary_k": preliminary_k,
        "final_payload_prediction_limit": preliminary_k,
        "final_metric_cutoff": FINAL_K,
        "completed_case_tracks": int(outcomes.get("ok", 0)),
        "execution_outcomes": outcomes,
        "expected_case_tracks": expected_case_count * len(tracks),
        "interrupted": interrupted,
        "track_results": track_summaries,
    }


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{100 * float(value):.2f}%"


def _number(value: Any, digits: int = 3) -> str:
    return "n/a" if value is None else f"{float(value):.{digits}f}"


def render_markdown(summary: Mapping[str, Any]) -> str:
    lines = [
        "# Recent-journal recommendation evaluation",
        "",
        f"- Run: `{summary['run_id']}`",
        f"- Dataset SHA-256: `{summary['dataset_sha256']}`",
        f"- Cases: {summary['expected_case_count']}",
        f"- Targets: {', '.join(summary['targets'])}; journals only",
        (
            f"- Retrieval keeps {summary['preliminary_k']} predictions so preliminary "
            f"Hit@{summary['preliminary_k']} can be measured; final ranking metrics are "
            f"strictly cut off at Top-{summary['final_metric_cutoff']}."
        ),
        "",
        "Errors and catalog-uncovered gold journals count as misses in end-to-end metrics. "
        "Coverage-conditioned metrics use all catalog-covered cases and still count retrieval errors as misses.",
        "",
        "## Overall",
        "",
        f"| Track | Cases | Coverage | Errors | Final H@1 | H@3 | H@5 | H@10 | MRR@10 | Pre H@10 | Pre H@{summary['preliminary_k']} | Pool H@{summary['preliminary_k']} | ΔH@10 | Median ms | P90 ms | Article leak | No-leak N | No-leak H@10 | No-leak MRR@10 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for track, result in summary["track_results"].items():
        final, preliminary = result["final"], result["preliminary"]
        recall_pool = result["recall_pool"]
        no_leak = result["no_search_leak"]
        lines.append(
            "| "
            + " | ".join(
                (
                    track,
                    str(result["case_count"]),
                    _pct(result["catalog_coverage"]),
                    str(result["errors"]),
                    _pct(final["hit_at_1"]),
                    _pct(final["hit_at_3"]),
                    _pct(final["hit_at_5"]),
                    _pct(final["hit_at_10"]),
                    _number(final["mrr_at_10"]),
                    _pct(preliminary["hit_at_10"]),
                    _pct(preliminary[f"hit_at_{summary['preliminary_k']}"]),
                    _pct(recall_pool[f"hit_at_{summary['preliminary_k']}"]),
                    _pct(result["rerank_delta"]["hit_at_10"]),
                    _number(result["latency_ms"]["median"], 1),
                    _number(result["latency_ms"]["p90"], 1),
                    _pct(result["leakage"]["article_leak_rate"]),
                    str(no_leak["case_count"]),
                    _pct(no_leak["final"]["hit_at_10"]),
                    _number(no_leak["final"]["mrr_at_10"]),
                )
            )
            + " |"
        )
    lines.extend(
        (
            "",
            "### Conservative leakage-safe lower bound",
            "",
            "Every case with any detected Search identity leak remains in the denominator but is counted as a miss.",
            "",
            f"| Track | Safe final H@1 | H@3 | H@5 | H@10 | Safe MRR@10 | Safe pre H@{summary['preliminary_k']} |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        )
    )
    for track, result in summary["track_results"].items():
        safe = result["search_leakage_safe_lower_bound"]
        final, preliminary = safe["final"], safe["preliminary"]
        lines.append(
            "| "
            + " | ".join(
                (
                    track,
                    _pct(final["hit_at_1"]),
                    _pct(final["hit_at_3"]),
                    _pct(final["hit_at_5"]),
                    _pct(final["hit_at_10"]),
                    _number(final["mrr_at_10"]),
                    _pct(preliminary[f"hit_at_{summary['preliminary_k']}"]),
                )
            )
            + " |"
        )
    for track, result in summary["track_results"].items():
        lines.extend(
            (
                "",
                f"## {track}: coverage-conditioned",
                "",
                f"| Stratum | N | Final H@1 | H@3 | H@5 | H@10 | MRR@10 | Pre H@10 | Pre H@{summary['preliminary_k']} |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            )
        )
        for label, row in [("ALL", result), *result["by_quartile"].items()]:
            conditioned = row["coverage_conditioned"]
            final, preliminary = conditioned["final"], conditioned["preliminary"]
            lines.append(
                "| "
                + " | ".join(
                    (
                        label,
                        str(row["catalog_covered"]),
                        _pct(final["hit_at_1"]),
                        _pct(final["hit_at_3"]),
                        _pct(final["hit_at_5"]),
                        _pct(final["hit_at_10"]),
                        _number(final["mrr_at_10"]),
                        _pct(preliminary["hit_at_10"]),
                        _pct(preliminary[f"hit_at_{summary['preliminary_k']}"]),
                    )
                )
                + " |"
            )
        lines.extend(
            (
                "",
                f"### {track}: by field (end-to-end)",
                "",
                "| Field | N | Coverage | Errors | Final H@1 | H@5 | H@10 | MRR@10 |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            )
        )
        for field, row in result["by_field"].items():
            final = row["final"]
            lines.append(
                "| "
                + " | ".join(
                    (
                        field.replace("|", "\\|"),
                        str(row["case_count"]),
                        _pct(row["catalog_coverage"]),
                        str(row["errors"]),
                        _pct(final["hit_at_1"]),
                        _pct(final["hit_at_5"]),
                        _pct(final["hit_at_10"]),
                        _number(final["mrr_at_10"]),
                    )
                )
                + " |"
            )
    lines.extend(
        (
            "",
            "## Leakage interpretation",
            "",
            "`article_leak` means a Search result exposed the DOI or a near-exact paper title. "
            "The stricter `no_search_leak` subset also excludes cases where Search evidence "
            "mentions the gold journal. Use that subset before treating the score as pure "
            "topic-to-journal matching quality.",
            "",
        )
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True, help="Input benchmark JSONL.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory for immutable generation segments and versioned closeouts.",
    )
    parser.add_argument("--api-config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument(
        "--track", action="append", choices=TRACKS, default=[], help="Repeat to select tracks."
    )
    parser.add_argument("--case-id", action="append", default=[], help="Run selected case IDs only.")
    parser.add_argument("--max-cases", type=int, default=None, help="Run at most this many cases.")
    parser.add_argument(
        "--preliminary-k", type=int, default=DEFAULT_PRELIMINARY_K, help="Retrieve this many rows; final metrics remain Top-10."
    )
    parser.add_argument("--api-timeout", type=int, default=20)
    parser.add_argument("--worker-timeout", type=int, default=900)
    parser.add_argument("--worker-startup-timeout", type=int, default=240)
    parser.add_argument("--title-similarity-threshold", type=float, default=0.90)
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help=(
            "Pre-authorize retrying prior error rows on a later --resume. The same "
            "flag must be present on the initial reviewed plan and every resume."
        ),
    )
    parser.add_argument(
        "--evaluation-mode",
        choices=(DIAGNOSTIC_MODE, FORMAL_MODE),
        default=DIAGNOSTIC_MODE,
        help="Only formal_500_full_denominator may produce a formal 500-paper result.",
    )
    parser.add_argument(
        "--builder-manifest",
        type=Path,
        default=None,
        help="Required complete 500-record builder manifest in formal mode.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print a read-only execution/cache/quota/cost plan. Never creates a "
            "worker, output directory, client, or network request."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Explicitly append only missing/error rows to an existing output whose "
            "immutable run manifest and call ledger match exactly."
        ),
    )
    parser.add_argument(
        "--authorization-reference",
        default="",
        help="Non-secret audit reference to the explicit authorization for live APIs.",
    )
    parser.add_argument(
        "--reviewed-plan-digest",
        default="",
        help="Exact SHA-256 printed by a reviewed dry-run; required live.",
    )
    parser.add_argument(
        "--authorization-grant",
        type=Path,
        default=None,
        help=(
            "Pre-created immutable formal live-run audit grant. Required only for "
            "formal_500_full_denominator live execution."
        ),
    )
    parser.add_argument(
        "--external-call-budget",
        type=int,
        default=None,
        help="Hard all-provider HTTP-attempt budget; required for live execution.",
    )
    parser.add_argument(
        "--external-attempt-cost-ceiling-usd",
        type=_decimal_argument,
        default=None,
        help="Conservative maximum USD cost of any one HTTP attempt; required live.",
    )
    parser.add_argument(
        "--authorized-max-cost-usd",
        type=_decimal_argument,
        default=None,
        help="Maximum explicitly authorized USD cost; required live.",
    )
    parser.add_argument(
        "--skip-explanations",
        action="store_true",
        help="评测时跳过不影响排序的前十解释调用，保留完整重排与命中指标。",
    )
    parser.add_argument(
        "--api-cache-seed-dir",
        type=Path,
        default=DATA_DIR / ".query_api_cache",
        help="Read-only seed copied into output/runtime_cache/api_cache before live use.",
    )
    parser.add_argument(
        "--query-embedding-cache-seed",
        type=Path,
        default=DATA_DIR / ".query_embedding_cache.json.gz",
    )
    parser.add_argument(
        "--lightrag-embedding-cache-seed",
        type=Path,
        default=DATA_DIR / ".embedding_cache.json.gz",
    )
    parser.add_argument(
        "--lightrag-working-dir-seed",
        type=Path,
        default=default_lightrag_working_dir(DATA_DIR),
        help=(
            "Frozen LightRAG query workspace; its manifest and five query stores "
            "are cloned into output/runtime_cache before live use."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Deprecated unsafe duplicate-row mode; always rejected.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.force:
        raise SystemExit("--force is disabled: use a new output or explicit --resume")
    if args.max_cases is not None and args.max_cases < 1:
        raise SystemExit("--max-cases must be positive")
    if args.preliminary_k < FINAL_K:
        raise SystemExit(f"--preliminary-k must be at least {FINAL_K}")
    if min(args.api_timeout, args.worker_timeout, args.worker_startup_timeout) < 1:
        raise SystemExit("timeouts must be positive")
    if not 0.0 < args.title_similarity_threshold <= 1.0:
        raise SystemExit("--title-similarity-threshold must be in (0, 1]")
    if args.evaluation_mode == FORMAL_MODE and (
        args.case_id
        or args.max_cases is not None
        or args.retry_errors
        or (args.track and tuple(args.track) != TRACKS)
    ):
        raise SystemExit(
            "formal_500_full_denominator forbids --case-id, --max-cases, "
            "--retry-errors, duplicate/reordered/single --track selections"
        )

    if args.dataset.is_symlink() or args.api_config.is_symlink():
        raise SystemExit("dataset and API config must be non-symlink regular files")
    dataset = args.dataset.resolve()
    api_config = args.api_config.resolve()
    if args.evaluation_mode == FORMAL_MODE and (
        args.builder_manifest is None or args.builder_manifest.is_symlink()
    ):
        raise SystemExit("formal builder manifest must be a non-symlink regular file")
    if not api_config.exists():
        raise SystemExit(f"API config does not exist: {api_config}")
    if args.external_call_budget is not None and args.external_call_budget < 1:
        raise SystemExit("--external-call-budget must be positive")
    authorization_reference = normalize_space(args.authorization_reference)
    raw_authorization_reference = str(args.authorization_reference)
    if (
        any(character in raw_authorization_reference for character in ("\n", "\r"))
        or len(authorization_reference) > 128
        or (
            authorization_reference
            and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}", authorization_reference)
        )
        or _authorization_reference_looks_secret(raw_authorization_reference)
    ):
        raise SystemExit("--authorization-reference must be one bounded non-secret audit ID")
    authorization_reference_digest = (
        _authorization_reference_sha256(authorization_reference)
        if authorization_reference
        else ""
    )

    try:
        loaded_cases = load_dataset(dataset)
        run_id, dataset_digest = make_run_id(
            dataset,
            api_config,
            preliminary_k=args.preliminary_k,
            api_timeout=args.api_timeout,
            title_similarity_threshold=args.title_similarity_threshold,
            skip_explanations=args.skip_explanations,
            lightrag_working_dir=args.lightrag_working_dir_seed.resolve(),
        )
        cases = resolve_gold_entity_ids(loaded_cases)
        builder_binding = (
            _validate_formal_builder_manifest(
                args.builder_manifest,
                dataset=dataset,
                dataset_sha256=dataset_digest,
                cases=cases,
                source_cases=loaded_cases,
            )
            if args.evaluation_mode == FORMAL_MODE
            else None
        )
    except EvaluationError as exc:
        raise SystemExit(str(exc)) from exc
    selected_ids = set(args.case_id)
    if selected_ids:
        cases = [case for case in cases if case.case_id in selected_ids]
        unknown = selected_ids - {case.case_id for case in cases}
        if unknown:
            raise SystemExit(f"unknown --case-id values: {', '.join(sorted(unknown))}")
    if args.max_cases is not None:
        cases = cases[: args.max_cases]
    tracks = tuple(dict.fromkeys(args.track or TRACKS))
    if args.evaluation_mode == FORMAL_MODE and (
        len(cases) != 500 or tracks != TRACKS or len(cases) * len(tracks) != 1000
    ):
        raise SystemExit("formal mode requires exactly 500 cases, two tracks, and 1,000 case-tracks")

    output_dir = args.output_dir.resolve()
    if output_dir.exists() and not output_dir.is_dir():
        raise SystemExit(f"output path exists and is not a directory: {output_dir}")
    if output_dir.exists() and not args.resume:
        raise SystemExit(
            f"output directory already exists; refusing overwrite without --resume: {output_dir}"
        )
    if args.resume and not output_dir.exists():
        raise SystemExit(f"--resume output directory does not exist: {output_dir}")
    try:
        output_secret_isolation = _output_secret_isolation_status(output_dir)
    except EvaluationError as exc:
        raise SystemExit(str(exc)) from exc

    seed_api_dir = args.api_cache_seed_dir.resolve()
    seed_query = args.query_embedding_cache_seed.resolve()
    seed_lightrag = args.lightrag_embedding_cache_seed.resolve()
    seed_lightrag_working_dir = args.lightrag_working_dir_seed.resolve()
    formal_acquisition_sources: dict[str, Path] = {}
    formal_acquisition_expected: dict[str, dict[str, Any]] = {}
    if builder_binding is not None:
        try:
            (
                formal_acquisition_sources,
                formal_acquisition_expected,
            ) = _formal_acquisition_source_bindings(builder_binding)
        except EvaluationError as exc:
            raise SystemExit(str(exc)) from exc
    try:
        api_plan_config = _read_api_config_for_plan(api_config)
        _verify_fresh_graph_binding(DATA_DIR / "venue_graph.json.gz")
        runtime_bindings = _runtime_binding_snapshot(
            api_config, seed_lightrag_working_dir
        )
        cache_seeds = _cache_seed_snapshot(
            seed_api_dir,
            seed_query,
            seed_lightrag,
            seed_lightrag_working_dir,
        )
        plan_source_evidence, _plan_source_evidence_sources = _source_evidence_plan(
            dataset,
            builder_manifest=(
                args.builder_manifest.resolve() if builder_binding is not None else None
            ),
            authorization_grant=None,
            additional_sources=formal_acquisition_sources,
        )
        if plan_source_evidence["dataset"]["sha256"] != dataset_digest:
            raise EvaluationError("dataset drifted while constructing the reviewed plan")
        if builder_binding is not None and plan_source_evidence[
            "builder_manifest"
        ]["sha256"] != builder_binding.get("sha256"):
            raise EvaluationError(
                "builder manifest drifted while constructing the reviewed plan"
            )
        if builder_binding is not None:
            _assert_formal_acquisition_sources_match_plan(
                formal_acquisition_expected,
                formal_acquisition_sources,
                plan_source_evidence,
            )
    except EvaluationError as exc:
        raise SystemExit(str(exc)) from exc
    if args.evaluation_mode == FORMAL_MODE and not all(
        (
            cache_seeds["api_cache"]["exists"],
            cache_seeds["query_embedding_cache"]["exists"],
            cache_seeds["lightrag_embedding_cache"]["exists"],
        )
    ):
        raise SystemExit("formal mode requires all three frozen cache seeds")
    if args.evaluation_mode == FORMAL_MODE and not all(
        bool(runtime_bindings[name]["exists"])
        for name in ("graph_artifact", "vector_artifact", "lightrag_manifest")
    ):
        raise SystemExit("formal mode requires graph, vector, and LightRAG bindings")

    expected_keys = {(case.case_id, track) for case in cases for track in tracks}
    expected_metadata = {
        case.case_id: {
            "catalog_covered": case.catalog_covered,
            "gold_entity_id": case.gold_entity_id,
            "gold_journal_name": case.gold_journal_name,
            "gold_jcr_quartile": case.gold_jcr_quartile,
            "primary_field": case.primary_field,
            "mapping_method": case.mapping_method,
        }
        for case in cases
    }
    existing_records: list[dict[str, Any]] = []
    existing_outcomes = {
        "attempt_records": 0,
        "attempted": 0,
        "ok": 0,
        "error": 0,
        "missing": len(expected_keys),
        "ever_failed": 0,
        "recovered": 0,
    }
    unclean_recovery_required = False
    staged_closeout_publish_required = False
    resume_manifest_payload: Mapping[str, Any] | None = None
    resume_closeout_head: Mapping[str, Any] | None = None
    if args.resume:
        try:
            resume_manifest_for_chain = _read_json_object(
                output_dir / RUN_MANIFEST_FILE, label="run manifest"
            )
            resume_manifest_payload = resume_manifest_for_chain
            resume_registry_identity = str(
                _nested(
                    resume_manifest_for_chain,
                    "authorization_registry",
                    "registry_identity",
                )
                or ""
            )
            if not re.fullmatch(r"[0-9a-f]{64}", resume_registry_identity):
                raise EvaluationError("resume manifest has no valid registry identity")
            (
                existing_records,
                existing_outcomes,
                _audits,
                resume_closeout_head,
                unclosed_segments,
                pending_staged_closeout,
            ) = _verify_closeout_chain(
                output_dir,
                run_id=run_id,
                expected_keys=expected_keys,
                runtime_paths=_runtime_cache_paths(output_dir),
                anchor_registry_dir=DEFAULT_AUTHORIZATION_REGISTRY_DIR,
                registry_identity=resume_registry_identity,
            )
            unclean_recovery_required = bool(unclosed_segments)
            staged_closeout_publish_required = pending_staged_closeout is not None
        except EvaluationError as exc:
            raise SystemExit(str(exc)) from exc
    latest = {
        (str(record.get("case_id")), str(record.get("track"))): record
        for record in existing_records
    }
    pending = [
        (case, track)
        for case in cases
        for track in tracks
        if (case.case_id, track) not in latest
        or (args.retry_errors and latest[(case.case_id, track)].get("status") != "ok")
    ]

    try:
        attempt_estimate = estimate_external_attempts(
            case_track_count=len(pending),
            api_config=api_plan_config,
            skip_explanations=args.skip_explanations,
        )
        search_config = dict(api_plan_config["search"])
        provider = str(search_config.get("provider") or "").strip().lower()
        quota_current = _external_quota_snapshot(api_plan_config)
        if args.resume:
            if not isinstance(resume_manifest_payload, Mapping):
                raise EvaluationError("resume manifest is unavailable for quota binding")
            manifest_quota = resume_manifest_payload.get(
                "shared_external_quota_initial"
            )
            if not isinstance(manifest_quota, Mapping) or not manifest_quota:
                raise EvaluationError("resume manifest has no shared quota binding")
            quota_initial = dict(manifest_quota)
            _assert_quota_snapshot_monotonic(
                quota_current, quota_initial, label="resume-initial"
            )
            if resume_closeout_head is not None:
                previous_quota = resume_closeout_head.get(
                    "shared_external_quota_final"
                )
                if not isinstance(previous_quota, Mapping):
                    raise EvaluationError("latest closeout has no shared quota audit")
                _assert_quota_snapshot_monotonic(
                    quota_current, previous_quota, label="resume-closeout"
                )
        else:
            quota_initial = dict(quota_current)
    except EvaluationError as exc:
        raise SystemExit(str(exc)) from exc

    cost_ceiling = args.external_attempt_cost_ceiling_usd
    authorized_cost = args.authorized_max_cost_usd
    maximum_estimated_cost = (
        cost_ceiling * args.external_call_budget
        if cost_ceiling is not None and args.external_call_budget is not None
        else None
    )
    plan_basis = {
        "schema_version": "2",
        "evaluation_mode": args.evaluation_mode,
        "run_id": run_id,
        "dataset_sha256": dataset_digest,
        "case_count": len(cases),
        "tracks": list(tracks),
        "case_track_count": len(expected_keys),
        "selection_sha256": _selection_sha256(cases, tracks),
        "api_config_sha256": runtime_bindings["api_config"]["sha256"],
        "builder_manifest": builder_binding or {},
        "runtime_bindings": runtime_bindings,
        "cache_seeds": cache_seeds,
        "runtime_cache_layout": _runtime_cache_layout(),
        "source_evidence": plan_source_evidence,
        "shared_external_quota_initial": quota_initial,
        "authorization_reference_sha256": authorization_reference_digest,
        "retry_policy": {
            "retry_errors": bool(args.retry_errors),
        },
        "external_call_budget": args.external_call_budget,
        "external_attempt_cost_ceiling_usd": (
            _decimal_text(cost_ceiling) if cost_ceiling is not None else None
        ),
        "authorized_max_cost_usd": (
            _decimal_text(authorized_cost) if authorized_cost is not None else None
        ),
        "method": {
            "targets": list(TARGETS),
            "preliminary_k": args.preliminary_k,
            "final_k": FINAL_K,
            "api_timeout": args.api_timeout,
            "worker_timeout": args.worker_timeout,
            "worker_startup_timeout": args.worker_startup_timeout,
            "title_similarity_threshold": args.title_similarity_threshold,
            "skip_explanations": bool(args.skip_explanations),
        },
    }
    reviewed_digest = _reviewed_plan_digest(plan_basis)
    authorization_grant: dict[str, Any] | None = None
    live_missing: list[str] = []
    if not authorization_reference:
        live_missing.append("--authorization-reference")
    if args.external_call_budget is None:
        live_missing.append("--external-call-budget")
    if cost_ceiling is None:
        live_missing.append("--external-attempt-cost-ceiling-usd")
    if authorized_cost is None:
        live_missing.append("--authorized-max-cost-usd")
    supplied_digest = normalize_space(args.reviewed_plan_digest)
    if not re.fullmatch(r"[0-9a-f]{64}", supplied_digest):
        live_missing.append("--reviewed-plan-digest (64 lowercase hex characters)")
    elif supplied_digest != reviewed_digest:
        live_missing.append("--reviewed-plan-digest does not match this exact plan")
    if maximum_estimated_cost is not None and authorized_cost is not None and maximum_estimated_cost > authorized_cost:
        live_missing.append("authorized cost is lower than call-budget × per-attempt ceiling")
    if output_secret_isolation["status"] == "unsafe_repository_output":
        live_missing.append(
            "--output-dir is inside the repository but is tracked/unignored; refusing credential snapshot"
        )
    if provider == "tavily" and quota_current.get("state_status") != (
        "readable_durable_dual_copy"
    ):
        live_missing.append("Tavily shared quota ledger is missing/unsafe/unreadable")
    if args.evaluation_mode == FORMAL_MODE:
        tls_trust = _nested(
            runtime_bindings, "network_environment", "tls_trust"
        )
        if not isinstance(tls_trust, Mapping) or not tls_trust.get(
            "verification_material_available"
        ):
            live_missing.append(
                "formal mode requires hashable active TLS verification material"
            )
        if _nested(
            runtime_bindings,
            "network_environment",
            "variables",
            "SSLKEYLOGFILE",
            "present",
        ):
            live_missing.append(
                "formal mode refuses SSLKEYLOGFILE because it exports TLS session secrets"
            )
        if args.authorization_grant is None:
            live_missing.append("--authorization-grant")
        elif (
            authorization_reference
            and args.external_call_budget is not None
            and cost_ceiling is not None
            and authorized_cost is not None
            and supplied_digest == reviewed_digest
        ):
            try:
                authorization_grant = _authorization_grant_binding(
                    Path(os.path.abspath(args.authorization_grant)),
                    authorization_reference_sha256=authorization_reference_digest,
                    reviewed_plan_digest=reviewed_digest,
                    output_dir=output_dir,
                    evaluation_mode=args.evaluation_mode,
                    external_call_budget=args.external_call_budget,
                    attempt_cost_ceiling_usd=cost_ceiling,
                    authorized_max_cost_usd=authorized_cost,
                )
            except EvaluationError as exc:
                live_missing.append(str(exc))
        else:
            live_missing.append("--authorization-grant cannot be verified before all controls match")

    nominal_attempts = int(attempt_estimate["configured_path_attempt_estimate"]["nominal_total"])
    warnings: list[str] = []
    if args.external_call_budget is not None and args.external_call_budget < nominal_attempts:
        warnings.append("hard budget is below the configured-path nominal estimate")
    plan = {
        **plan_basis,
        "mode": "dry-run" if args.dry_run else "live-preflight",
        "claim_status": (
            "formal 500-paper full-denominator protocol"
            if args.evaluation_mode == FORMAL_MODE
            else "diagnostic_nonformal; must not be reported as a formal 500-paper evaluation"
        ),
        "reviewed_plan_digest": reviewed_digest,
        "network_calls_made": 0,
        "live_clients_instantiated": False,
        "output": {
            "path": str(output_dir),
            "exists": output_dir.exists(),
            "policy": "append-only segments; versioned summaries/closeouts; no repair/overwrite",
            "resume_requested": bool(args.resume),
            "existing_outcomes": existing_outcomes,
            "pending_case_tracks": len(pending),
            "unclean_recovery_required": unclean_recovery_required,
            "staged_closeout_publish_required": staged_closeout_publish_required,
        },
        "cache_coverage": {
            "evaluation_output_exact": {
                "covered_case_tracks": len(expected_keys) - len(pending),
                "pending_case_tracks": len(pending),
            },
            "api_cache_inventory": _api_cache_inventory(
                seed_api_dir, seed_query, seed_lightrag
            ),
            "runtime_policy": "read-only copied seeds; all writes isolated under output/runtime_cache",
        },
        "quota": {
            "initial_reviewed_binding": quota_initial,
            "current_observation": quota_current,
            "mutation_policy": (
                "Tavily is the only shared mutable state; revisions and used attempts "
                "must be monotonic and every closeout records the final sanitized audit"
            ),
        },
        "attempt_estimate": attempt_estimate,
        "budget_assessment": {
            "uncached_configured_path_nominal_attempts": nominal_attempts,
            "completion_certified": False,
            "warnings": warnings,
        },
        "maximum_estimated_cost_usd": (
            _decimal_text(maximum_estimated_cost) if maximum_estimated_cost is not None else None
        ),
        "authorization_grant": authorization_grant,
        "output_secret_isolation": output_secret_isolation,
        "live_control_ready": not live_missing,
        "live_missing_or_invalid_controls": live_missing,
    }
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    if live_missing:
        raise SystemExit(
            "live evaluation refused; missing/invalid controls: "
            + ", ".join(live_missing)
            + ". Run and review --dry-run first."
        )

    assert args.external_call_budget is not None
    assert cost_ceiling is not None
    assert authorized_cost is not None
    try:
        source_evidence, source_evidence_sources = _source_evidence_plan(
            dataset,
            builder_manifest=(
                args.builder_manifest.resolve() if builder_binding is not None else None
            ),
            authorization_grant=(
                Path(os.path.abspath(args.authorization_grant))
                if authorization_grant is not None and args.authorization_grant is not None
                else None
            ),
            additional_sources=formal_acquisition_sources,
        )
    except EvaluationError as exc:
        raise SystemExit(str(exc)) from exc
    if {
        name: binding
        for name, binding in source_evidence.items()
        if name != "authorization_grant"
    } != plan_source_evidence:
        raise SystemExit(
            "dataset/builder source evidence drifted after plan review; refusing live run"
        )
    if builder_binding is not None:
        try:
            _assert_formal_acquisition_sources_match_plan(
                formal_acquisition_expected,
                formal_acquisition_sources,
                source_evidence,
            )
        except EvaluationError as exc:
            raise SystemExit(str(exc)) from exc
    if args.resume:
        try:
            resume_manifest = json.loads(
                (output_dir / RUN_MANIFEST_FILE).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SystemExit("resume requires a readable immutable run manifest") from exc
        resume_core = {
            "run_id": run_id,
            "dataset_sha256": dataset_digest,
            "selection_sha256": _selection_sha256(cases, tracks),
            "authorization_reference_sha256": authorization_reference_digest,
            "approved_plan_digest": reviewed_digest,
            "external_call_budget": args.external_call_budget,
            "evaluation_mode": args.evaluation_mode,
            "retry_errors_authorized": bool(args.retry_errors),
        }
        if not isinstance(resume_manifest, Mapping) or any(
            resume_manifest.get(key) != value for key, value in resume_core.items()
        ):
            raise SystemExit(
                "resume manifest core does not match the approved plan; global registry untouched"
            )
    try:
        registry_reference, ledger_path = _claim_or_verify_authorization_registry(
            DEFAULT_AUTHORIZATION_REGISTRY_DIR,
            authorization_reference=authorization_reference,
            approved_plan_digest=reviewed_digest,
            budget=args.external_call_budget,
            attempt_cost_ceiling_usd=cost_ceiling,
            authorized_max_cost_usd=authorized_cost,
            authorization_grant=authorization_grant,
            retry_errors=bool(args.retry_errors),
        )
    except (EvaluationError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc
    manifest = _run_manifest_payload(
        run_id=run_id,
        dataset=dataset,
        dataset_sha256=dataset_digest,
        cases=cases,
        tracks=tracks,
        api_config=api_config,
        authorization_reference_sha256=authorization_reference_digest,
        external_call_budget=args.external_call_budget,
        attempt_cost_ceiling_usd=cost_ceiling,
        authorized_max_cost_usd=authorized_cost,
        evaluation_mode=args.evaluation_mode,
        approved_plan_digest=reviewed_digest,
        runtime_bindings=runtime_bindings,
        cache_seeds=cache_seeds,
        authorization_registry=registry_reference,
        builder_manifest=builder_binding,
        authorization_grant=authorization_grant,
        retry_errors=bool(args.retry_errors),
        source_evidence=source_evidence,
        shared_external_quota_initial=quota_initial,
    )

    output_lock_descriptor: int | None = None
    try:
        if args.resume:
            _verify_run_manifest(output_dir / RUN_MANIFEST_FILE, manifest)
            runtime_paths = _runtime_cache_paths(output_dir)
        else:
            runtime_paths = _create_fresh_output(
                output_dir,
                manifest=manifest,
                api_config=api_config,
                api_cache_seed_dir=seed_api_dir,
                query_embedding_cache_seed=seed_query,
                lightrag_embedding_cache_seed=seed_lightrag,
                lightrag_working_dir_seed=seed_lightrag_working_dir,
                source_evidence_sources=source_evidence_sources,
            )
            if _cache_seed_snapshot(
                seed_api_dir,
                seed_query,
                seed_lightrag,
                seed_lightrag_working_dir,
            ) != cache_seeds:
                raise EvaluationError("formal cache seeds drifted while cloning")
            _verify_fresh_runtime_cache_clone(cache_seeds, runtime_paths)
            if (
                _file_sha256(runtime_paths["graph_path"])
                != runtime_bindings["graph_artifact"]["sha256"]
                or _file_sha256(runtime_paths["vector_path"])
                != runtime_bindings["vector_artifact"]["sha256"]
            ):
                raise EvaluationError("run-local graph/vector clone drifted")
            _verify_fresh_graph_binding(runtime_paths["graph_path"])
        output_lock_descriptor = _acquire_output_lock(output_dir)
        _source_evidence_integrity_snapshot(output_dir, source_evidence)
        expected_run_manifest_sha256 = _file_sha256(
            output_dir / RUN_MANIFEST_FILE
        )
        quota_pre_worker = _external_quota_snapshot(api_plan_config)
        if args.resume:
            _assert_quota_snapshot_monotonic(
                quota_pre_worker, quota_current, label="pre-worker-resume"
            )
        elif quota_pre_worker != quota_initial:
            raise EvaluationError(
                "shared external quota state drifted after plan review"
            )
        quota_last_observation = dict(quota_pre_worker)
        if _file_sha256(runtime_paths["api_config_snapshot"]) != runtime_bindings["api_config"]["sha256"]:
            raise EvaluationError("frozen API config snapshot is missing or mismatched")
        _verify_runtime_bindings(
            runtime_bindings, api_config, seed_lightrag_working_dir
        )
        frozen_binding_stamps = _runtime_binding_stamps(
            api_config,
            runtime_paths["api_config_snapshot"],
            seed_lightrag_working_dir,
            runtime_paths["lightrag_working_dir"],
            (
                output_dir / RUN_MANIFEST_FILE,
                *(
                    output_dir / str(binding["path"])
                    for binding in source_evidence.values()
                    if isinstance(binding, Mapping)
                ),
            ),
        )
        (
            existing_records,
            existing_outcomes,
            _before_audits,
            _head,
            unclosed_segments,
            pending_staged_closeout,
        ) = _verify_closeout_chain(
            output_dir,
            run_id=run_id,
            expected_keys=expected_keys,
            runtime_paths=runtime_paths,
            anchor_registry_dir=DEFAULT_AUTHORIZATION_REGISTRY_DIR,
            registry_identity=registry_reference["registry_identity"],
        )
        if pending_staged_closeout is not None:
            _publish_staged_exclusive(*pending_staged_closeout)
            (
                existing_records,
                existing_outcomes,
                _before_audits,
                _head,
                unclosed_segments,
                still_pending_publish,
            ) = _verify_closeout_chain(
                output_dir,
                run_id=run_id,
                expected_keys=expected_keys,
                runtime_paths=runtime_paths,
                anchor_registry_dir=DEFAULT_AUTHORIZATION_REGISTRY_DIR,
                registry_identity=registry_reference["registry_identity"],
            )
            if still_pending_publish is not None:
                raise EvaluationError("staged closeout publish recovery did not commit")
        if unclosed_segments:
            _recover_unclosed_generation(
                output_dir,
                segment_path=unclosed_segments[0],
                run_id=run_id,
                expected_keys=expected_keys,
                runtime_paths=runtime_paths,
                ledger_path=ledger_path,
                anchor_registry_dir=DEFAULT_AUTHORIZATION_REGISTRY_DIR,
                registry_identity=registry_reference["registry_identity"],
                shared_external_quota_final=quota_last_observation,
            )
            (
                existing_records,
                existing_outcomes,
                _before_audits,
                _head,
                remaining_unclosed,
                remaining_pending_publish,
            ) = _verify_closeout_chain(
                output_dir,
                run_id=run_id,
                expected_keys=expected_keys,
                runtime_paths=runtime_paths,
                anchor_registry_dir=DEFAULT_AUTHORIZATION_REGISTRY_DIR,
                registry_identity=registry_reference["registry_identity"],
            )
            if remaining_unclosed or remaining_pending_publish is not None:
                raise EvaluationError("unclean recovery did not commit its closeout")
        latest = {
            (str(record.get("case_id")), str(record.get("track"))): record
            for record in existing_records
        }
        pending = [
            (case, track)
            for case in cases
            for track in tracks
            if (case.case_id, track) not in latest
            or (args.retry_errors and latest[(case.case_id, track)].get("status") != "ok")
        ]
        generation, segment_path = _create_generation_segment(output_dir)
    except (EvaluationError, FileExistsError, OSError) as exc:
        if output_lock_descriptor is not None:
            os.close(output_lock_descriptor)
        raise SystemExit(str(exc)) from exc

    def verify_worker_bindings() -> None:
        nonlocal quota_last_observation
        quota_observation = _external_quota_snapshot(api_plan_config)
        _assert_quota_snapshot_monotonic(
            quota_observation, quota_last_observation, label="worker-boundary"
        )
        quota_last_observation = dict(quota_observation)
        _source_evidence_integrity_snapshot(output_dir, source_evidence)
        _verify_runtime_bindings(
            runtime_bindings, api_config, seed_lightrag_working_dir
        )
        if _file_sha256(runtime_paths["api_config_snapshot"]) != runtime_bindings["api_config"]["sha256"]:
            raise EvaluationError("frozen API config snapshot drifted")
        _verify_runtime_binding_stamps(frozen_binding_stamps)

    def verify_case_binding_stamps() -> None:
        nonlocal quota_last_observation
        quota_observation = _external_quota_snapshot(api_plan_config)
        _assert_quota_snapshot_monotonic(
            quota_observation, quota_last_observation, label="case-boundary"
        )
        quota_last_observation = dict(quota_observation)
        _source_evidence_integrity_snapshot(output_dir, source_evidence)
        _verify_runtime_binding_stamps(frozen_binding_stamps)

    print(
        f"run={run_id} generation={generation} cases={len(cases)} "
        f"tracks={','.join(tracks)} pending={len(pending)}",
        flush=True,
    )
    interrupted = False
    fatal_error: str | None = None
    worker = PersistentWorker(
        startup_timeout=args.worker_startup_timeout,
        external_call_ledger=ledger_path,
        external_call_budget=args.external_call_budget,
        run_id=registry_reference["registry_identity"],
        api_cache_dir=runtime_paths["api_cache_dir"],
        query_embedding_cache=runtime_paths["query_embedding_cache"],
        lightrag_embedding_cache=runtime_paths["lightrag_embedding_cache"],
        lightrag_working_dir=runtime_paths["lightrag_working_dir"],
        graph_path=runtime_paths["graph_path"],
        api_config_snapshot=runtime_paths["api_config_snapshot"],
        verify_bindings=verify_worker_bindings,
    )
    try:
        if pending:
            worker.start()
            print(f"worker_preload_ms={worker.preload_ms}", flush=True)
        for index, (case, track) in enumerate(pending, 1):
            verify_case_binding_stamps()
            print(f"[{index}/{len(pending)}] {case.case_id} {track}", flush=True)
            record = evaluate_case(
                worker,
                case,
                track,
                run_id=run_id,
                api_config=runtime_paths["api_config_snapshot"],
                preliminary_k=args.preliminary_k,
                api_timeout=args.api_timeout,
                worker_timeout=args.worker_timeout,
                title_similarity_threshold=args.title_similarity_threshold,
                skip_explanations=args.skip_explanations,
                api_cache_dir=runtime_paths["api_cache_dir"],
                query_embedding_cache=runtime_paths["query_embedding_cache"],
                lightrag_embedding_cache=runtime_paths["lightrag_embedding_cache"],
                lightrag_working_dir=runtime_paths["lightrag_working_dir"],
                graph_path=runtime_paths["graph_path"],
            )
            try:
                verify_case_binding_stamps()
            except EvaluationError as exc:
                record = {
                    "schema_version": SCHEMA_VERSION,
                    "run_id": run_id,
                    "case_id": case.case_id,
                    "track": track,
                    **case.public_metadata(),
                    "status": "error",
                    "error": f"EvaluationError: result rejected after binding drift: {exc}",
                }
                fatal_error = str(exc)
            if "generation" in record and record.get("generation") != generation:
                raise EvaluationError("worker returned a conflicting generation binding")
            record = {**record, "generation": generation}
            append_jsonl_durable(segment_path, record)
            print(f"  status={record['status']} final_rank={record.get('final_gold_rank')}", flush=True)
            if fatal_error is not None:
                break
            if record["status"] == "error" and worker.process is not None and worker.process.poll() is not None:
                worker.close()
    except KeyboardInterrupt:
        interrupted = True
        print("interrupted; preserving immutable partial segment", file=sys.stderr, flush=True)
    except EvaluationError as exc:
        fatal_error = str(exc)
        print(f"evaluation stopped fail-closed: {exc}", file=sys.stderr, flush=True)
    except Exception as exc:
        fatal_error = f"{type(exc).__name__}: {exc}"
        print(
            f"evaluation stopped after an unexpected failure: {fatal_error}",
            file=sys.stderr,
            flush=True,
        )
    finally:
        try:
            try:
                worker.close()
            except Exception as exc:  # closeout must survive worker teardown failure
                fatal_error = fatal_error or f"worker close failed: {type(exc).__name__}: {exc}"
            try:
                verify_case_binding_stamps()
            except EvaluationError as exc:
                fatal_error = fatal_error or f"final binding verification failed: {exc}"
            quota_closeout_observation = _external_quota_snapshot(api_plan_config)
            _assert_quota_snapshot_monotonic(
                quota_closeout_observation,
                quota_last_observation,
                label="closeout",
            )
            quota_last_observation = dict(quota_closeout_observation)
            _seal_generation_segment(segment_path)
            records, outcomes, segment_audits = _scan_generation_segments(
                _generation_paths(output_dir), run_id=run_id, expected_keys=expected_keys
            )
            exit_code = _evaluation_exit_code(
                interrupted=interrupted,
                fatal_error=fatal_error,
                outcomes=outcomes,
            )
            try:
                final_ledger_status = external_call_ledger_status(ledger_path)
            except RuntimeError as exc:
                fatal_error = fatal_error or f"global ledger unreadable at closeout: {exc}"
                if exit_code == 0:
                    exit_code = 3
                final_ledger_status = {
                    "schema_version": "unreadable",
                    "run_id": registry_reference["registry_identity"],
                    "budget": args.external_call_budget,
                    "used": None,
                    "remaining": None,
                    "error": str(exc),
                }
            summary = build_summary(
                records,
                run_id=run_id,
                dataset=dataset,
                dataset_sha256=dataset_digest,
                expected_case_count=len(cases),
                tracks=tracks,
                preliminary_k=args.preliminary_k,
                interrupted=interrupted,
                expected_case_ids=[case.case_id for case in cases],
                expected_case_metadata=expected_metadata,
                execution_outcomes=outcomes,
            )
            summary.update(
                {
                    "evaluation_mode": args.evaluation_mode,
                    "formal_full_denominator": args.evaluation_mode == FORMAL_MODE,
                    "claim_status": plan["claim_status"],
                    "reviewed_plan_digest": reviewed_digest,
                    "generation": generation,
                    "exit_code": exit_code,
                    "fatal_error": fatal_error,
                    "execution_control": {
                        "authorization_reference_sha256": authorization_reference_digest,
                        "authorization_registry": registry_reference,
                        "external_call_ledger_status": final_ledger_status,
                        "shared_external_quota_final": quota_last_observation,
                        "reserved_attempt_cost_ceiling_usd": (
                            _decimal_text(cost_ceiling * int(final_ledger_status["used"]))
                            if isinstance(final_ledger_status.get("used"), int)
                            else None
                        ),
                        "output_policy": "append-only generation segments; no tail repair; versioned closeouts",
                    },
                }
            )
            stem = f"generation-{generation:06d}"
            summary_path = output_dir / f"summary.{stem}.json"
            markdown_path = output_dir / f"summary.{stem}.md"
            closeout_path = output_dir / f"closeout.{stem}.json"
            _write_text_exclusive(
                summary_path,
                json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            )
            _write_text_exclusive(markdown_path, render_markdown(summary))
            closeout = {
                "schema_version": "2",
                "run_id": run_id,
                "generation": generation,
                "closure_kind": "normal",
                "closed_at": datetime.now(timezone.utc).isoformat(),
                "run_manifest_sha256": expected_run_manifest_sha256,
                "previous_closeout_sha256": (
                    _file_sha256(
                        output_dir
                        / f"closeout.generation-{generation - 1:06d}.json"
                    )
                    if generation > 1
                    else None
                ),
                "exit_code": exit_code,
                "interrupted": interrupted,
                "fatal_error": fatal_error,
                "outcomes": outcomes,
                "segments": segment_audits,
                "current_segment": next(
                    audit
                    for audit in segment_audits
                    if audit["generation"] == generation
                ),
                "summary": {
                    "path": summary_path.name,
                    "sha256": _file_sha256(summary_path),
                },
                "report": {
                    "path": markdown_path.name,
                    "sha256": _file_sha256(markdown_path),
                },
                "global_ledger_status": final_ledger_status,
                "shared_external_quota_final": quota_last_observation,
                "runtime_cache_final": _runtime_cache_integrity_snapshot(
                    runtime_paths
                ),
                "source_evidence_final": _source_evidence_integrity_snapshot(
                    output_dir, source_evidence
                ),
            }
            _publish_closeout_with_anchor(
                closeout_path,
                closeout,
                anchor_registry_dir=DEFAULT_AUTHORIZATION_REGISTRY_DIR,
                registry_identity=registry_reference["registry_identity"],
                output_dir=output_dir,
                generation=generation,
            )
            print(f"raw_segment={segment_path}", flush=True)
            print(f"summary={summary_path}", flush=True)
            print(f"report={markdown_path}", flush=True)
            print(f"closeout={closeout_path}", flush=True)
        finally:
            if output_lock_descriptor is not None:
                os.close(output_lock_descriptor)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
