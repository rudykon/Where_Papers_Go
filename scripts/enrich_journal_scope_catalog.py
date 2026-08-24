#!/usr/bin/env python3
"""Enrich the 20,087 unique JCR Q1--Q4 journal entities with aims & scope.

Unlike the legacy row-oriented command, this catalog job deduplicates journals
across JCR/CAS/TH-CPL before making Search and LLM calls.  One successful result
is copied back to every source row belonging to that journal.  The queue is
deterministic, benchmark gold journals are processed first, and atomic CSV
checkpoints make a long run resumable.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import os
from typing import Any, Iterable, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.build_recent_journal_benchmark import classify_broad_field
from where_paper_go import enrichment
from where_paper_go.paths import DATA_DIR, PROJECT_ROOT
from where_paper_go.recommender import (
    DATA_FILES,
    build_candidates,
    load_records,
    parse_targets,
    valid_issn_token,
)


DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "benchmark_artifacts" / "scope_enrichment"
JCR_TARGETS = tuple(f"JCR-Q{quartile}" for quartile in range(1, 5))


@dataclass(frozen=True)
class ScopeEntity:
    entity_id: int
    name: str
    issns: tuple[str, ...]
    quartile: str
    category: str
    broad_field: str
    row_ids: tuple[int, ...]
    automatic_scope_ok: bool
    automatic_status: str
    reviewed_scope_available: bool

    @property
    def scope_available(self) -> bool:
        return self.automatic_scope_ok or self.reviewed_scope_available

    def to_dict(self, *, benchmark_priority: bool = False) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "name": self.name,
            "issns": list(self.issns),
            "quartile": self.quartile,
            "category": self.category,
            "broad_field": self.broad_field,
            "automatic_scope_ok": self.automatic_scope_ok,
            "automatic_status": self.automatic_status,
            "reviewed_scope_available": self.reviewed_scope_available,
            "scope_available": self.scope_available,
            "benchmark_priority": benchmark_priority,
        }


@dataclass
class CsvCatalog:
    fieldnames: dict[Path, list[str]]
    rows: dict[Path, list[dict[str, str]]]
    row_locations: dict[int, tuple[Path, int]]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stable_key(*values: object) -> str:
    return hashlib.sha256("\x1f".join(map(str, values)).encode()).hexdigest()


def load_scope_entities(data_dir: Path = DATA_DIR) -> list[ScopeEntity]:
    candidates = build_candidates(
        load_records(data_dir),
        parse_targets(JCR_TARGETS),
        record_type="journal",
    )
    entities: list[ScopeEntity] = []
    for candidate in candidates:
        jcr_rows = [
            record
            for record in candidate.matched_records
            if record.dataset == "jcr" and record.level in {"Q1", "Q2", "Q3", "Q4"}
        ]
        if not jcr_rows:
            continue
        category = jcr_rows[0].area or jcr_rows[0].taxonomy_scope
        entities.append(
            ScopeEntity(
                entity_id=min(record.row_id for record in candidate.records),
                name=candidate.name,
                issns=tuple(
                    sorted(
                        {
                            token
                            for record in candidate.records
                            for token in (
                                valid_issn_token(record.issn),
                                valid_issn_token(record.eissn),
                            )
                            if token
                        }
                    )
                ),
                quartile=jcr_rows[0].level,
                category=category,
                broad_field=classify_broad_field(category),
                row_ids=tuple(sorted(record.row_id for record in candidate.records)),
                automatic_scope_ok=bool(candidate.official_scope_candidates),
                automatic_status=next(
                    (
                        record.official_scope_status
                        for record in candidate.records
                        if record.official_scope_status
                    ),
                    "",
                ),
                reviewed_scope_available=bool(candidate.curated_scopes),
            )
        )
    return entities


def benchmark_issns(dataset: Path | None) -> set[str]:
    if dataset is None or not dataset.exists():
        return set()
    values: set[str] = set()
    with dataset.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid benchmark JSONL line {line_number}") from exc
            for value in payload.get("gold_issns", []):
                token = valid_issn_token(str(value))
                if token:
                    values.add(token)
    return values


def prioritized_entities(
    entities: Iterable[ScopeEntity],
    *,
    seed: str,
    priority_issns: set[str] | None = None,
    attempted_entity_ids: set[int] | None = None,
    overwrite_ok: bool = False,
    retry_attempted: bool = True,
) -> list[ScopeEntity]:
    priority_issns = priority_issns or set()
    attempted_entity_ids = attempted_entity_ids or set()
    pending = [
        entity
        for entity in entities
        if (overwrite_ok or not entity.automatic_scope_ok)
        and (retry_attempted or entity.entity_id not in attempted_entity_ids)
    ]
    pending.sort(
        key=lambda entity: (
            entity.entity_id in attempted_entity_ids,
            entity.automatic_status not in {"", "no_candidate_pages"}
            and not entity.automatic_status.startswith("error:"),
            stable_key(seed, entity.entity_id),
            entity.entity_id,
        )
    )
    priority = [entity for entity in pending if priority_issns.intersection(entity.issns)]
    priority_ids = {entity.entity_id for entity in priority}

    buckets: dict[tuple[str, str], list[ScopeEntity]] = defaultdict(list)
    for entity in pending:
        if entity.entity_id not in priority_ids:
            buckets[(entity.broad_field, entity.quartile)].append(entity)
    keys = sorted(buckets)
    balanced: list[ScopeEntity] = []
    while keys:
        next_keys: list[tuple[str, str]] = []
        for key in keys:
            if buckets[key]:
                balanced.append(buckets[key].pop(0))
            if buckets[key]:
                next_keys.append(key)
        keys = next_keys
    # Failed benchmark entities remain ahead of the general catalog, but they
    # follow previously unattempted benchmark entities so one bad publisher
    # page cannot monopolize every resumed batch.
    priority.sort(
        key=lambda entity: (
            entity.entity_id in attempted_entity_ids,
            entity.automatic_status not in {"", "no_candidate_pages"}
            and not entity.automatic_status.startswith("error:"),
            stable_key(seed, entity.entity_id),
            entity.entity_id,
        )
    )
    return priority + balanced


def load_attempted_entity_ids(path: Path) -> set[int]:
    if not path.exists():
        return set()
    values: set[int] = set()
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                entity_id = int(payload["entity_id"])
                if payload.get("event") == "requeue":
                    values.discard(entity_id)
                else:
                    values.add(entity_id)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid attempt log line {line_number}: {path}") from exc
    return values


def append_attempt(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def scope_status(entities: Sequence[ScopeEntity], priority_issns: set[str]) -> dict[str, Any]:
    automatic_ok = sum(entity.automatic_scope_ok for entity in entities)
    reviewed = sum(entity.reviewed_scope_available for entity in entities)
    available = sum(entity.scope_available for entity in entities)
    priority = [entity for entity in entities if priority_issns.intersection(entity.issns)]
    return {
        "generated_at": now_iso(),
        "jcr_q1_q4_unique_entities": len(entities),
        "automatic_aims_scope_ok": automatic_ok,
        "automatic_aims_scope_coverage": automatic_ok / len(entities) if entities else 0.0,
        "reviewed_scope_available": reviewed,
        "retrieval_scope_available_union": available,
        "retrieval_scope_coverage": available / len(entities) if entities else 0.0,
        "automatic_scope_pending": len(entities) - automatic_ok,
        "benchmark_priority_entities": len(priority),
        "benchmark_priority_automatic_ok": sum(entity.automatic_scope_ok for entity in priority),
        "by_quartile": dict(sorted(Counter(entity.quartile for entity in entities).items())),
        "by_broad_field": dict(sorted(Counter(entity.broad_field for entity in entities).items())),
    }


def load_csv_catalog(data_dir: Path) -> CsvCatalog:
    fieldnames: dict[Path, list[str]] = {}
    rows: dict[Path, list[dict[str, str]]] = {}
    row_locations: dict[int, tuple[Path, int]] = {}
    row_id = 0
    for filename in DATA_FILES:
        path = data_dir / filename
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            current_rows = list(reader)
            fieldnames[path] = enrichment.ensure_output_columns(reader.fieldnames or [])
            rows[path] = current_rows
        for index in range(len(current_rows)):
            row_locations[row_id] = (path, index)
            row_id += 1
    return CsvCatalog(fieldnames, rows, row_locations)


def representative_row(entity: ScopeEntity, catalog: CsvCatalog) -> dict[str, str]:
    candidates = [catalog.rows[path][index] for path, index in (catalog.row_locations[row_id] for row_id in entity.row_ids)]
    return dict(
        next(
            (row for row in candidates if row.get("dataset") == "jcr"),
            candidates[0],
        )
    )


def apply_result(
    entity: ScopeEntity,
    catalog: CsvCatalog,
    *,
    status: str,
    result: Mapping[str, Any] | None,
    error: str,
) -> set[Path]:
    changed: set[Path] = set()
    for row_id in entity.row_ids:
        path, index = catalog.row_locations[row_id]
        row = catalog.rows[path][index]
        if result is None:
            row["收稿方向_状态"] = status
            row["收稿方向_证据"] = error
            row["收稿方向_更新时间"] = now_iso()
        else:
            enrichment.update_row_from_result(row, dict(result), status, replace_scope=False)
        changed.add(path)
    return changed


def write_changed(catalog: CsvCatalog, paths: Iterable[Path]) -> None:
    for path in sorted(set(paths)):
        enrichment.write_csv_file(path, catalog.fieldnames[path], catalog.rows[path])


def atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_queue(path: Path, queue: Sequence[ScopeEntity], priority_issns: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for position, entity in enumerate(queue, start=1):
            payload = {
                "position": position,
                **entity.to_dict(benchmark_priority=bool(priority_issns.intersection(entity.issns))),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    temporary.replace(path)


def enrich_catalog(args: argparse.Namespace) -> dict[str, Any]:
    entities = load_scope_entities(args.data_dir)
    priority_issns = benchmark_issns(args.benchmark_dataset)
    attempted_ids = load_attempted_entity_ids(args.attempt_log)
    queue = prioritized_entities(
        entities,
        seed=args.seed,
        priority_issns=priority_issns,
        attempted_entity_ids=attempted_ids,
        overwrite_ok=args.overwrite_ok,
        retry_attempted=not args.skip_attempted,
    )
    write_queue(args.queue_output, queue, priority_issns)
    before = scope_status(entities, priority_issns)
    if args.status_only or args.dry_run:
        return {
            "status": "status_only" if args.status_only else "dry_run",
            "catalog_status": before,
            "before": before,
            "pending_queue": len(queue),
            "attempted_entities": len(attempted_ids),
            "selected": [entity.to_dict(benchmark_priority=bool(priority_issns.intersection(entity.issns))) for entity in queue[: args.limit or 10]],
            "queue_output": str(args.queue_output),
        }

    selected = queue[: args.limit] if args.limit is not None else queue
    config = enrichment.load_api_config(args.api_config)
    llm = enrichment.llm_config(config)
    search = enrichment.search_config(config)
    if not (llm.get("base_url") or llm.get("api_base") or llm.get("endpoint")) or not llm.get("model"):
        raise ValueError("scope enrichment requires a configured LLM endpoint and model")
    if not search.get("provider"):
        raise ValueError("scope enrichment requires an explicit Search API provider")

    catalog = load_csv_catalog(args.data_dir)
    selected_paths = {
        catalog.row_locations[row_id][0]
        for entity in selected
        for row_id in entity.row_ids
    }
    for path in selected_paths:
        backup = enrichment.backup_path(path)
        shutil.copy2(path, backup)

    # Adapt the catalog CLI names to the proven row-level enrichment function.
    args.skip_journal_homepage_lookup = not args.with_openalex_homepage
    args.replace_scope = False
    search_conf = enrichment.search_config(config)
    changed_paths: set[Path] = set()
    outcome_counts: Counter[str] = Counter()
    pending_attempts: list[dict[str, Any]] = []
    started = now_iso()
    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                enrichment.enrich_row,
                entity.entity_id,
                representative_row(entity, catalog),
                args,
                config,
                search_conf,
            ): entity
            for entity in selected
        }
        for future in as_completed(futures):
            entity = futures[future]
            _index, status, result, error = future.result()
            changed_paths.update(
                apply_result(
                    entity,
                    catalog,
                    status=status,
                    result=result,
                    error=error,
                )
            )
            outcome_counts[status.split(":", 1)[0]] += 1
            pending_attempts.append(
                {
                    "attempted_at": now_iso(),
                    "entity_id": entity.entity_id,
                    "name": entity.name,
                    "status": status,
                }
            )
            completed += 1
            if args.checkpoint_every and completed % args.checkpoint_every == 0:
                write_changed(catalog, changed_paths)
                for attempt in pending_attempts:
                    append_attempt(args.attempt_log, attempt)
                pending_attempts.clear()
                print(f"checkpoint {completed}/{len(selected)}", flush=True)
            if args.progress_every and completed % args.progress_every == 0:
                print(f"processed {completed}/{len(selected)} {dict(outcome_counts)}", flush=True)
    write_changed(catalog, changed_paths)
    for attempt in pending_attempts:
        append_attempt(args.attempt_log, attempt)

    refreshed = load_scope_entities(args.data_dir)
    return {
        "status": "complete",
        "started_at": started,
        "finished_at": now_iso(),
        "selected": len(selected),
        "completed": completed,
        "outcomes": dict(sorted(outcome_counts.items())),
        "attempt_log": str(args.attempt_log),
        "before": before,
        "after": (after := scope_status(refreshed, priority_issns)),
        "catalog_status": after,
        "queue_output": str(args.queue_output),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--api-config", type=Path, default=None)
    parser.add_argument(
        "--benchmark-dataset",
        type=Path,
        default=PROJECT_ROOT / "benchmark_artifacts" / "recent_journals" / "dataset.jsonl",
    )
    parser.add_argument("--queue-output", type=Path, default=DEFAULT_OUTPUT_DIR / "queue.jsonl")
    parser.add_argument("--status-output", type=Path, default=DEFAULT_OUTPUT_DIR / "status.json")
    parser.add_argument("--attempt-log", type=Path, default=DEFAULT_OUTPUT_DIR / "attempts.jsonl")
    parser.add_argument("--status-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--checkpoint-every", type=int, default=10)
    parser.add_argument("--progress-every", type=int, default=10)
    parser.add_argument("--overwrite-ok", action="store_true")
    parser.add_argument(
        "--skip-attempted",
        action="store_true",
        help="只处理本轮尚未写入 attempt 日志的期刊，适合自动批处理。",
    )
    parser.add_argument("--with-openalex-homepage", action="store_true")
    parser.add_argument("--allow-untrusted-domains", action="store_true")
    parser.add_argument("--cache-dir", type=Path, default=DATA_DIR / ".aims_scope_cache")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--search-results", type=int, default=5)
    parser.add_argument("--max-search-queries", type=int, default=2)
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--max-html-bytes", type=int, default=1_000_000)
    parser.add_argument("--max-chars-per-page", type=int, default=10_000)
    parser.add_argument("--seed", default="where-papers-go-scope-catalog-v1")
    parser.add_argument("--sleep", type=float, default=0.0)
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit must be positive")
    if args.workers < 1 or args.checkpoint_every < 1 or args.progress_every < 1:
        raise ValueError("workers/checkpoint/progress values must be positive")
    if args.search_results < 1 or args.max_search_queries < 1 or args.max_pages < 1:
        raise ValueError("search/page limits must be positive")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_args(args)
        result = enrich_catalog(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    atomic_json(args.status_output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
