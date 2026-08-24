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
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import time
from typing import Any, Iterable, Mapping, Sequence
import unicodedata
import uuid

from where_paper_go.paths import DEFAULT_CONFIG_PATH, PROJECT_ROOT
from where_paper_go.recommender import journal_identity_name, normalize_space
from where_paper_go.graph_index import graph_source_digest


SCHEMA_VERSION = "2"
TARGETS = ("JCR-Q1", "JCR-Q2", "JCR-Q3", "JCR-Q4")
TRACKS = ("title_abstract", "abstract_only")
FINAL_K = 10
DEFAULT_PRELIMINARY_K = 40


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

    def __init__(self, *, cwd: Path = PROJECT_ROOT, startup_timeout: int = 240):
        self.cwd = cwd
        self.startup_timeout = startup_timeout
        self.process: subprocess.Popen[bytes] | None = None
        self._read_buffer = bytearray()
        self.preload_ms: int | None = None

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        self.close()
        self.process = subprocess.Popen(
            [sys.executable, "-m", "where_paper_go.worker"],
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # Third-party startup chatter is not part of the protocol.  Avoid a
            # PIPE here: an undrained stderr could deadlock a long benchmark.
            stderr=subprocess.DEVNULL,
            bufsize=0,
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


def _repair_and_load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = path.read_bytes()
    lines = data.splitlines(keepends=True)
    records: list[dict[str, Any]] = []
    valid_bytes = 0
    for index, line in enumerate(lines):
        if not line.strip():
            valid_bytes += len(line)
            continue
        try:
            payload = json.loads(line.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            if index != len(lines) - 1:
                raise EvaluationError(f"corrupt JSONL output at line {index + 1}") from exc
            with path.open("r+b") as handle:
                handle.truncate(valid_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            break
        if not isinstance(payload, dict):
            raise EvaluationError(f"invalid JSONL output object at line {index + 1}")
        records.append(payload)
        valid_bytes += len(line)
    if data and not data.endswith(b"\n") and valid_bytes == len(data):
        with path.open("ab") as handle:
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    return records


def append_jsonl_durable(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8") + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short JSONL append")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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


def make_run_id(
    dataset: Path,
    api_config: Path,
    *,
    preliminary_k: int,
    api_timeout: int,
    title_similarity_threshold: float,
    skip_explanations: bool = False,
) -> tuple[str, str]:
    dataset_digest = _file_sha256(dataset)
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
        "catalog_graph_source_digest": graph_source_digest(PROJECT_ROOT / "data"),
        "pipeline_source_sha256": {
            name: _optional_file_sha256(PROJECT_ROOT / "where_paper_go" / name)
            for name in (
                "recommender.py",
                "api_assistant.py",
                "embeddings.py",
                "graph_index.py",
                "lightrag.py",
            )
        },
        "lightrag_manifest_sha256": _optional_file_sha256(
            PROJECT_ROOT / "data" / "lightrag_storage" / "venue_import_manifest.json"
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
    if expected_case_ids:
        for case_id in expected_case_ids:
            for track in tracks:
                key = (str(case_id), str(track))
                if key not in present_keys:
                    deduped.append(
                        {
                            "run_id": run_id,
                            "case_id": case_id,
                            "track": track,
                            "status": "missing",
                            "catalog_covered": False,
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
        "completed_case_tracks": len(deduped),
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
        "--output-dir", type=Path, required=True, help="Directory for raw.jsonl and summaries."
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
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument(
        "--skip-explanations",
        action="store_true",
        help="评测时跳过不影响排序的前十解释调用，保留完整重排与命中指标。",
    )
    parser.add_argument("--force", action="store_true", help="Append fresh rows even when this run is complete.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_cases is not None and args.max_cases < 1:
        raise SystemExit("--max-cases must be positive")
    if args.preliminary_k < FINAL_K:
        raise SystemExit(f"--preliminary-k must be at least {FINAL_K}")
    if args.api_timeout < 1 or args.worker_timeout < 1 or args.worker_startup_timeout < 1:
        raise SystemExit("timeouts must be positive")
    if not 0.0 < args.title_similarity_threshold <= 1.0:
        raise SystemExit("--title-similarity-threshold must be in (0, 1]")
    dataset = args.dataset.resolve()
    api_config = args.api_config.resolve()
    if not api_config.exists():
        raise SystemExit(f"API config does not exist: {api_config}")
    cases = resolve_gold_entity_ids(load_dataset(dataset))
    selected_ids = set(args.case_id)
    if selected_ids:
        cases = [case for case in cases if case.case_id in selected_ids]
        missing = selected_ids - {case.case_id for case in cases}
        if missing:
            raise SystemExit(f"unknown --case-id values: {', '.join(sorted(missing))}")
    if args.max_cases is not None:
        cases = cases[: args.max_cases]
    tracks = tuple(dict.fromkeys(args.track or TRACKS))
    output_dir = args.output_dir.resolve()
    raw_path = output_dir / "raw.jsonl"
    summary_path = output_dir / "summary.json"
    markdown_path = output_dir / "summary.md"
    existing = _repair_and_load_jsonl(raw_path)
    run_id, dataset_digest = make_run_id(
        dataset,
        api_config,
        preliminary_k=args.preliminary_k,
        api_timeout=args.api_timeout,
        title_similarity_threshold=args.title_similarity_threshold,
        skip_explanations=args.skip_explanations,
    )
    latest: dict[tuple[str, str], Mapping[str, Any]] = {}
    for record in existing:
        if record.get("run_id") == run_id:
            latest[(str(record.get("case_id")), str(record.get("track")))] = record

    pending: list[tuple[BenchmarkCase, str]] = []
    for case in cases:
        for track in tracks:
            previous = latest.get((case.case_id, track))
            if args.force or previous is None or (
                args.retry_errors and previous.get("status") != "ok"
            ):
                pending.append((case, track))
    print(
        f"run={run_id} cases={len(cases)} tracks={','.join(tracks)} "
        f"pending={len(pending)} resumed={len(cases) * len(tracks) - len(pending)}",
        flush=True,
    )
    interrupted = False
    worker = PersistentWorker(startup_timeout=args.worker_startup_timeout)
    try:
        if pending:
            worker.start()
            print(f"worker_preload_ms={worker.preload_ms}", flush=True)
        for index, (case, track) in enumerate(pending, 1):
            print(
                f"[{index}/{len(pending)}] {case.case_id} {track}", flush=True
            )
            record = evaluate_case(
                worker,
                case,
                track,
                run_id=run_id,
                api_config=api_config,
                preliminary_k=args.preliminary_k,
                api_timeout=args.api_timeout,
                worker_timeout=args.worker_timeout,
                title_similarity_threshold=args.title_similarity_threshold,
                skip_explanations=args.skip_explanations,
            )
            append_jsonl_durable(raw_path, record)
            existing.append(record)
            print(
                f"  status={record['status']} final_rank={record.get('final_gold_rank')} "
                f"pre_rank={record.get('preliminary_gold_rank')} "
                f"elapsed_ms={record.get('latency_ms')}",
                flush=True,
            )
            if record["status"] == "error" and worker.process is not None and worker.process.poll() is not None:
                worker.close()
    except KeyboardInterrupt:
        interrupted = True
        print("interrupted; writing partial summary", file=sys.stderr, flush=True)
    finally:
        worker.close()
        summary = build_summary(
            existing,
            run_id=run_id,
            dataset=dataset,
            dataset_sha256=dataset_digest,
            expected_case_count=len(cases),
            tracks=tracks,
            preliminary_k=args.preliminary_k,
            interrupted=interrupted,
            expected_case_ids=[case.case_id for case in cases],
        )
        _atomic_write_text(
            summary_path,
            json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        )
        _atomic_write_text(markdown_path, render_markdown(summary))
        print(f"raw={raw_path}", flush=True)
        print(f"summary={summary_path}", flush=True)
        print(f"report={markdown_path}", flush=True)
    return 130 if interrupted else 0


if __name__ == "__main__":
    raise SystemExit(main())
