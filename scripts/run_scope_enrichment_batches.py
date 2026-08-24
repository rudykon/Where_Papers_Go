#!/usr/bin/env python3
"""Run resumable aims-and-scope enrichment in bounded 50--100 journal batches.

The runner is intentionally conservative: an entity is attempted at most once
per attempt log, every batch is archived, retrieval assets are refreshed on a
configurable cadence, and a lock prevents two writers from updating the CSV
catalog concurrently.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.enrich_journal_scope_catalog import (
    DEFAULT_OUTPUT_DIR,
    benchmark_issns,
    load_attempted_entity_ids,
    load_scope_entities,
    prioritized_entities,
    append_attempt,
)
from where_paper_go import enrichment
from where_paper_go.paths import DATA_DIR, PROJECT_ROOT


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def log_line(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{now_iso()}] {message}\n")
        handle.flush()


def run_logged(command: list[str], log_path: Path) -> int:
    log_line(log_path, "RUN " + " ".join(command))
    with log_path.open("a", encoding="utf-8") as handle:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            stdout=handle,
            stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            check=False,
        )
    log_line(log_path, f"EXIT {completed.returncode}")
    return completed.returncode


def queue_snapshot(args: argparse.Namespace, attempt_log: Path) -> dict[str, int]:
    entities = load_scope_entities(args.data_dir)
    priority = benchmark_issns(args.benchmark_dataset)
    attempted = load_attempted_entity_ids(attempt_log)
    queue = prioritized_entities(
        entities,
        seed=args.seed,
        priority_issns=priority,
        attempted_entity_ids=attempted,
        retry_attempted=False,
    )
    priority_pending = sum(bool(priority.intersection(entity.issns)) for entity in queue)
    return {
        "entities": len(entities),
        "attempted": len(attempted),
        "unattempted_pending": len(queue),
        "priority_unattempted_pending": priority_pending,
    }


def save_state(path: Path, state: dict[str, Any], **changes: Any) -> None:
    state.update(changes)
    state["updated_at"] = now_iso()
    atomic_json(path, state)


def archive_batch(status_path: Path, batch_dir: Path, batch_number: int) -> tuple[str, dict[str, Any]]:
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    destination = batch_dir / f"batch-{batch_number:04d}.json"
    atomic_json(destination, payload)
    return str(destination), payload


def unhealthy_batch(payload: dict[str, Any]) -> str:
    """Detect an upstream outage before it consumes the remaining queue."""
    selected = int(payload.get("selected") or 0)
    outcomes = payload.get("outcomes") or {}
    no_pages = int(outcomes.get("no_candidate_pages") or 0)
    errors = sum(
        int(value or 0)
        for key, value in outcomes.items()
        if str(key).startswith("error")
    )
    if selected >= 20 and no_pages / selected >= 0.9:
        return f"circuit breaker: {no_pages}/{selected} results had no candidate pages"
    if selected >= 20 and errors / selected >= 0.5:
        return f"circuit breaker: {errors}/{selected} results failed"
    return ""


def requeue_latest_attempts(path: Path, count: int, *, reason: str) -> list[int]:
    attempts = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if payload.get("event") != "requeue":
                attempts.append(payload)
    selected = attempts[-count:]
    entity_ids = []
    for payload in selected:
        entity_id = int(payload["entity_id"])
        append_attempt(
            path,
            {
                "attempted_at": now_iso(),
                "entity_id": entity_id,
                "event": "requeue",
                "name": payload.get("name", ""),
                "reason": reason,
            },
        )
        entity_ids.append(entity_id)
    return entity_ids


def search_health_error(args: argparse.Namespace) -> str:
    try:
        config = enrichment.search_config(enrichment.load_api_config(args.api_config))
        with tempfile.TemporaryDirectory(prefix="wpg-search-health-") as directory:
            results = enrichment.search_web(
                "Nature journal official aims and scope",
                config,
                Path(directory),
                min(30, args.health_timeout),
                1,
                raise_on_error=True,
            )
        if not results:
            return "search provider returned no health-check results"
        return ""
    except Exception as exc:  # noqa: BLE001 - state captures provider details.
        return f"{type(exc).__name__}: {exc}"[:500]


def wait_for_search(args: argparse.Namespace, state_path: Path, state: dict[str, Any], log_path: Path, stop_path: Path) -> bool:
    while True:
        error = search_health_error(args)
        if not error:
            save_state(state_path, state, status="running", search_status="ready", search_error="")
            log_line(log_path, "search health check passed")
            return True
        save_state(
            state_path,
            state,
            status="waiting_search",
            search_status="blocked",
            search_error=error,
            next_health_check_seconds=args.health_retry_seconds,
        )
        log_line(log_path, f"search health check blocked: {error}")
        remaining = args.health_retry_seconds
        while remaining > 0:
            if stop_path.exists():
                return False
            delay = min(30.0, remaining)
            time.sleep(delay)
            remaining -= delay


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-config", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument(
        "--benchmark-dataset",
        type=Path,
        default=PROJECT_ROOT / "benchmark_artifacts" / "recent_journals" / "dataset.jsonl",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--checkpoint-every", type=int, default=10)
    parser.add_argument("--index-every", type=int, default=1)
    parser.add_argument("--batch-delay", type=float, default=5.0)
    parser.add_argument("--health-retry-seconds", type=float, default=900.0)
    parser.add_argument("--health-timeout", type=int, default=30)
    parser.add_argument(
        "--requeue-latest-outage-batch",
        action="store_true",
        help="将最新且触发熔断条件的批次恢复到未尝试队列，然后退出。",
    )
    parser.add_argument("--max-batches", type=int, default=0)
    parser.add_argument("--priority-only", action="store_true")
    parser.add_argument("--max-search-queries", type=int, default=2)
    parser.add_argument("--seed", default="where-papers-go-scope-catalog-v1")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if not 50 <= args.batch_size <= 100:
        raise ValueError("--batch-size must be between 50 and 100")
    if args.workers < 1 or args.checkpoint_every < 1 or args.index_every < 1:
        raise ValueError("workers/checkpoint/index values must be positive")
    if args.max_batches < 0 or args.batch_delay < 0 or args.health_retry_seconds < 1:
        raise ValueError("max-batches and batch-delay cannot be negative")
    if args.health_timeout < 1:
        raise ValueError("health-timeout must be positive")


def run(args: argparse.Namespace) -> int:
    validate_args(args)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    status_path = args.output_dir / "status.json"
    attempt_log = args.output_dir / "attempts.jsonl"
    queue_path = args.output_dir / "queue.jsonl"
    state_path = args.output_dir / "runner_state.json"
    log_path = args.output_dir / "runner.log"
    index_log = args.output_dir / "index_refresh.log"
    stop_path = args.output_dir / "STOP"
    pid_path = args.output_dir / "runner.pid"
    lock_path = args.output_dir / "runner.lock"
    batch_dir = args.output_dir / "batches"
    batch_dir.mkdir(parents=True, exist_ok=True)

    if args.requeue_latest_outage_batch:
        reports = sorted(batch_dir.glob("batch-*.json"))
        if not reports:
            raise ValueError("no archived batch is available to requeue")
        report_path = reports[-1]
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        reason = unhealthy_batch(payload)
        if not reason:
            raise ValueError(f"latest batch is not unhealthy: {report_path}")
        entity_ids = requeue_latest_attempts(
            attempt_log,
            int(payload.get("selected") or 0),
            reason=reason,
        )
        print(
            json.dumps(
                {
                    "status": "requeued",
                    "batch_report": str(report_path),
                    "reason": reason,
                    "entity_count": len(entity_ids),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    existing_batch_numbers = []
    for path in batch_dir.glob("batch-*.json"):
        try:
            existing_batch_numbers.append(int(path.stem.split("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    last_archived_batch = max(existing_batch_numbers, default=0)

    lock_handle = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log_line(log_path, "another scope enrichment runner already owns the lock")
        return 2

    pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    if stop_path.exists():
        stop_path.unlink()
    state: dict[str, Any] = {
        "status": "running",
        "pid": os.getpid(),
        "started_at": now_iso(),
        "batch_size": args.batch_size,
        "workers": args.workers,
        "priority_only": args.priority_only,
        "batch_number": last_archived_batch,
        "successful_batches": 0,
        "log": str(log_path),
        "stop_file": str(stop_path),
    }
    save_state(state_path, state, queue=queue_snapshot(args, attempt_log))
    log_line(log_path, f"runner started pid={os.getpid()} priority_only={args.priority_only}")

    try:
        while True:
            if stop_path.exists():
                save_state(state_path, state, status="stopped", reason="STOP file detected")
                log_line(log_path, "STOP file detected; exiting between batches")
                return 0
            if args.max_batches and state["successful_batches"] >= args.max_batches:
                save_state(state_path, state, status="complete", reason="max_batches reached")
                return 0

            if not wait_for_search(args, state_path, state, log_path, stop_path):
                save_state(state_path, state, status="stopped", reason="STOP file detected")
                return 0

            snapshot = queue_snapshot(args, attempt_log)
            pending = (
                snapshot["priority_unattempted_pending"]
                if args.priority_only
                else snapshot["unattempted_pending"]
            )
            save_state(state_path, state, queue=snapshot)
            if pending == 0:
                save_state(
                    state_path,
                    state,
                    status="complete",
                    reason="target queue exhausted",
                    queue=snapshot,
                )
                log_line(log_path, "target queue exhausted")
                return 0

            batch_number = state["batch_number"] + 1
            batch_limit = min(args.batch_size, pending)
            save_state(
                state_path,
                state,
                status="running",
                batch_number=batch_number,
                current_batch_limit=batch_limit,
                batch_started_at=now_iso(),
            )
            command = [
                sys.executable,
                "-m",
                "scripts.enrich_journal_scope_catalog",
                "--api-config",
                str(args.api_config),
                "--data-dir",
                str(args.data_dir),
                "--benchmark-dataset",
                str(args.benchmark_dataset),
                "--queue-output",
                str(queue_path),
                "--status-output",
                str(status_path),
                "--attempt-log",
                str(attempt_log),
                "--limit",
                str(batch_limit),
                "--workers",
                str(args.workers),
                "--checkpoint-every",
                str(args.checkpoint_every),
                "--progress-every",
                str(args.checkpoint_every),
                "--max-search-queries",
                str(args.max_search_queries),
                "--seed",
                args.seed,
                "--skip-attempted",
            ]
            returncode = run_logged(command, log_path)
            if returncode != 0:
                save_state(
                    state_path,
                    state,
                    status="failed",
                    last_error=f"enrichment exited with {returncode}",
                )
                return returncode

            archive, batch_payload = archive_batch(status_path, batch_dir, batch_number)
            state["successful_batches"] += 1
            save_state(
                state_path,
                state,
                last_batch_finished_at=now_iso(),
                last_batch_report=archive,
                queue=queue_snapshot(args, attempt_log),
            )

            unhealthy_reason = unhealthy_batch(batch_payload)
            if unhealthy_reason:
                requeued_ids = requeue_latest_attempts(
                    attempt_log,
                    int(batch_payload.get("selected") or 0),
                    reason=unhealthy_reason,
                )
                save_state(
                    state_path,
                    state,
                    status="failed",
                    reason=unhealthy_reason,
                    last_error=unhealthy_reason,
                    requeued_entity_ids=requeued_ids,
                )
                log_line(log_path, unhealthy_reason)
                return 3

            should_index = state["successful_batches"] % args.index_every == 0
            if should_index:
                save_state(state_path, state, index_status="running")
                index_command = [
                    sys.executable,
                    "-m",
                    "scripts.prepare_retrieval",
                    "--api-config",
                    str(args.api_config),
                    "--force",
                    "--force-graph",
                ]
                index_returncode = run_logged(index_command, index_log)
                if index_returncode != 0:
                    save_state(
                        state_path,
                        state,
                        status="failed",
                        index_status="failed",
                        last_error=f"retrieval refresh exited with {index_returncode}",
                    )
                    return index_returncode
                save_state(state_path, state, index_status="ready", indexed_at=now_iso())

            if args.batch_delay:
                time.sleep(args.batch_delay)
    except Exception as exc:
        save_state(state_path, state, status="failed", last_error=f"{type(exc).__name__}: {exc}")
        log_line(log_path, f"UNHANDLED {type(exc).__name__}: {exc}")
        return 1
    finally:
        try:
            pid_path.unlink()
        except FileNotFoundError:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except ValueError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
