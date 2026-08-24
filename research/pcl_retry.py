"""Persistent, independent retry queue for PCL prototype synthesis.

The historical evidence collectors must not be blocked by a slow chat model.
This module therefore owns a small background worker pool and an append-only
audit log.  Venue shards remain the source of truth; the queue log only makes
retry decisions and crash recovery observable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import heapq
import json
from pathlib import Path
import threading
import time
from typing import Callable, Mapping


@dataclass(frozen=True)
class PCLRetryPolicy:
    """Retry and concurrency limits for the independent PCL queue."""

    max_attempts: int = 3
    second_pass_attempts: int = 2
    backoff_base: float = 2.0
    backoff_max: float = 30.0
    workers: int = 1

    def validate(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("PCL max_attempts must be positive")
        if self.second_pass_attempts < 0:
            raise ValueError("PCL second_pass_attempts must be non-negative")
        if self.backoff_base < 0 or self.backoff_max < 0:
            raise ValueError("PCL retry delays must be non-negative")
        if self.backoff_max and self.backoff_base > self.backoff_max:
            raise ValueError("PCL backoff_base must not exceed backoff_max")
        if self.workers < 1:
            raise ValueError("PCL workers must be positive")

    def backoff_delay(self, failed_attempt: int) -> float:
        """Return base, 2*base, 4*base... capped at ``backoff_max``."""

        exponent = max(0, int(failed_attempt) - 1)
        delay = self.backoff_base * (2**exponent)
        return min(self.backoff_max, delay) if self.backoff_max else delay


@dataclass(frozen=True)
class PCLRetryOutcome:
    """One PCL attempt result returned by the shard-aware handler."""

    ok: bool
    status: str
    error: str = ""
    retryable: bool = True


@dataclass(frozen=True)
class _PCLJob:
    venue_id: str
    pass_number: int
    attempts_completed: int
    origin: str


class PCLRetryQueue:
    """Run PCL work independently and persist every scheduling transition.

    Jobs contain only a venue ID.  Evidence stays in the already-atomic venue
    shard, so queue recovery never needs to duplicate large prompts or secrets.
    """

    TERMINAL_EVENTS = {"recovered", "succeeded", "failed"}

    def __init__(
        self,
        output_dir: Path,
        handler: Callable[[str], PCLRetryOutcome],
        policy: PCLRetryPolicy | None = None,
    ) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.queue_path = output_dir / "pcl_retry_queue.jsonl"
        self.attempts_path = output_dir / "pcl_retry_attempts.jsonl"
        self.state_path = output_dir / "pcl_retry_state.json"
        self.handler = handler
        self.policy = policy or PCLRetryPolicy()
        self.policy.validate()

        self._condition = threading.Condition(threading.RLock())
        self._heap: list[tuple[float, int, _PCLJob]] = []
        self._sequence = 0
        self._scheduled: set[str] = set()
        self._inflight = 0
        self._accepting = True
        self._stop = False
        self._latest_events, self._event_totals = self._load_latest_events()
        self._fatal_error = ""
        self._stats = {
            "queued": 0,
            "attempted": 0,
            "retried": 0,
            "second_pass_queued": 0,
            "succeeded": 0,
            "recovered": 0,
            "failed": 0,
        }
        self._threads = [
            threading.Thread(
                target=self._worker_entry,
                name=f"pcl-retry-{index + 1}",
                daemon=True,
            )
            for index in range(self.policy.workers)
        ]
        # Persist before starting threads so a constructor failure cannot leak
        # daemon workers that outlive the caller's output-directory lock.
        self._persist_state()
        started_threads: list[threading.Thread] = []
        try:
            for thread in self._threads:
                thread.start()
                started_threads.append(thread)
        except BaseException:
            with self._condition:
                self._accepting = False
                self._stop = True
                self._condition.notify_all()
            for thread in started_threads:
                thread.join()
            raise

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    @staticmethod
    def _epoch_iso(value: float) -> str:
        return datetime.fromtimestamp(value, timezone.utc).replace(microsecond=0).isoformat()

    @staticmethod
    def _safe_error(value: object) -> str:
        return " ".join(str(value or "").split())[:240]

    def _load_latest_events(
        self,
    ) -> tuple[dict[str, dict[str, object]], dict[str, int]]:
        latest: dict[str, dict[str, object]] = {}
        totals: dict[str, int] = {}
        if not self.queue_path.is_file():
            return latest, totals
        try:
            with self.queue_path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, Mapping) and str(row.get("venue_id") or ""):
                        latest[str(row["venue_id"])] = dict(row)
                        event = str(row.get("event") or "unknown")
                        totals[event] = totals.get(event, 0) + 1
        except OSError:
            return {}, {}
        return latest, totals

    @staticmethod
    def _append_jsonl(path: Path, payload: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                + "\n"
            )
            handle.flush()

    def _event(self, venue_id: str, event: str, **fields: object) -> None:
        attempts_completed = int(fields.get("attempts_completed") or 0)
        row: dict[str, object] = {
            "event": event,
            "recorded_at": self._now_iso(),
            "status": fields.get("status") or event,
            "attempt": fields.get("attempt") or attempts_completed,
            "venue_id": venue_id,
            **fields,
        }
        with self._condition:
            self._append_jsonl(self.queue_path, row)
            self._latest_events[venue_id] = row
            self._event_totals[event] = self._event_totals.get(event, 0) + 1

    def _attempt_event(self, venue_id: str, **fields: object) -> None:
        row: dict[str, object] = {
            "attempted_at": self._now_iso(),
            "venue_id": venue_id,
            **fields,
        }
        with self._condition:
            self._append_jsonl(self.attempts_path, row)

    def _state_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "updated_at": self._now_iso(),
            "accepting": self._accepting,
            "workers": self.policy.workers,
            "pending": len(self._heap),
            "active": len(self._scheduled),
            "inflight": self._inflight,
            "fatal_error": self._fatal_error,
            "stats_this_run": dict(self._stats),
            "event_totals": dict(sorted(self._event_totals.items())),
            **self._stats,
        }

    def _persist_state(self) -> None:
        with self._condition:
            payload = self._state_payload()
            temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.state_path)

    def snapshot(self) -> dict[str, object]:
        with self._condition:
            return dict(self._state_payload())

    def raise_if_failed(self) -> None:
        """Fail the producer promptly when a background worker has died."""

        with self._condition:
            if self._fatal_error:
                raise RuntimeError(f"PCL retry worker failed: {self._fatal_error}")

    def enqueue(
        self,
        venue_id: str,
        *,
        second_pass: bool = False,
        origin: str = "new_evidence",
        force: bool = False,
    ) -> bool:
        """Schedule one venue, recovering unfinished audit-log jobs if needed."""

        venue_id = str(venue_id).strip()
        if not venue_id:
            raise ValueError("PCL queue requires a venue_id")
        with self._condition:
            if self._fatal_error:
                raise RuntimeError(f"PCL retry worker failed: {self._fatal_error}")
            if not self._accepting or venue_id in self._scheduled:
                return False
            latest = self._latest_events.get(venue_id, {})
            latest_event = str(latest.get("event") or "")
            if latest_event in self.TERMINAL_EVENTS and not force:
                return False
            if latest_event in {
                "queued",
                "retrying",
                "second_pass",
                "in_progress",
                "requeued_after_restart",
            }:
                pass_number = int(latest.get("pass_number") or (2 if second_pass else 1))
                attempts_completed = int(latest.get("attempts_completed") or 0)
                event = "requeued_after_restart"
                try:
                    next_attempt_epoch = float(latest.get("next_attempt_epoch") or 0)
                except (TypeError, ValueError):
                    next_attempt_epoch = 0.0
                delay = max(0.0, next_attempt_epoch - time.time())
            else:
                pass_number = 2 if second_pass else 1
                attempts_completed = 0
                event = "second_pass" if second_pass else "queued"
                delay = 0.0
            self._scheduled.add(venue_id)
            self._sequence += 1
            heapq.heappush(
                self._heap,
                (
                    time.monotonic() + delay,
                    self._sequence,
                    _PCLJob(venue_id, pass_number, attempts_completed, origin),
                ),
            )
            self._stats["queued"] += 1
            if pass_number == 2:
                self._stats["second_pass_queued"] += 1
            self._event(
                venue_id,
                event,
                pass_number=pass_number,
                attempts_completed=attempts_completed,
                origin=origin,
                next_attempt_at=(
                    self._epoch_iso(time.time() + delay) if delay > 0 else ""
                ),
                next_attempt_epoch=(time.time() + delay if delay > 0 else 0),
            )
            self._persist_state()
            self._condition.notify_all()
            return True

    def _reschedule(self, job: _PCLJob, delay: float, event: str) -> None:
        next_attempt_epoch = time.time() + max(0.0, delay)
        self._sequence += 1
        heapq.heappush(
            self._heap,
            (time.monotonic() + max(0.0, delay), self._sequence, job),
        )
        self._stats["retried"] += 1
        if event == "second_pass":
            self._stats["second_pass_queued"] += 1
        self._event(
            job.venue_id,
            event,
            pass_number=job.pass_number,
            attempts_completed=job.attempts_completed,
            delay_seconds=round(delay, 3),
            next_attempt_at=self._epoch_iso(next_attempt_epoch),
            next_attempt_epoch=next_attempt_epoch,
            origin=job.origin,
        )

    def _worker_entry(self) -> None:
        try:
            self._worker()
        except BaseException as exc:  # noqa: BLE001 - wake waiters on worker death.
            with self._condition:
                self._fatal_error = (
                    f"{type(exc).__name__}: {self._safe_error(exc)}"
                )
                self._accepting = False
                self._stop = True
                self._condition.notify_all()

    def _worker(self) -> None:
        while True:
            with self._condition:
                while not self._heap and not self._stop:
                    self._condition.wait(timeout=0.5)
                if self._stop:
                    return
                ready_at, sequence, job = heapq.heappop(self._heap)
                delay = ready_at - time.monotonic()
                if delay > 0:
                    heapq.heappush(self._heap, (ready_at, sequence, job))
                    self._condition.wait(timeout=min(delay, 0.5))
                    continue
                self._inflight += 1
                self._stats["attempted"] += 1
                attempt_number = job.attempts_completed + 1
                self._event(
                    job.venue_id,
                    "in_progress",
                    pass_number=job.pass_number,
                    attempts_completed=job.attempts_completed,
                    attempt=attempt_number,
                    origin=job.origin,
                )
                self._persist_state()

            started = time.monotonic()
            try:
                outcome = self.handler(job.venue_id)
                if not isinstance(outcome, PCLRetryOutcome):
                    raise TypeError("PCL retry handler returned an invalid outcome")
            except Exception as exc:  # noqa: BLE001 - queue must retain every failed job.
                outcome = PCLRetryOutcome(
                    ok=False,
                    status=f"error:{type(exc).__name__}",
                    error=f"{type(exc).__name__}: {self._safe_error(exc)}",
                    retryable=True,
                )
            duration = time.monotonic() - started
            self._attempt_event(
                job.venue_id,
                pass_number=job.pass_number,
                attempt=attempt_number,
                status=outcome.status,
                ok=outcome.ok,
                retryable=outcome.retryable,
                duration_seconds=round(duration, 3),
                error=self._safe_error(outcome.error),
                origin=job.origin,
            )

            with self._condition:
                self._inflight -= 1
                if outcome.ok:
                    self._scheduled.discard(job.venue_id)
                    recovered = job.origin != "new_evidence" or job.pass_number == 2
                    key = "recovered" if recovered else "succeeded"
                    self._stats[key] += 1
                    self._event(
                        job.venue_id,
                        key,
                        pass_number=job.pass_number,
                        attempts_completed=attempt_number,
                        status=outcome.status,
                        origin=job.origin,
                    )
                else:
                    limit = (
                        self.policy.max_attempts
                        if job.pass_number == 1
                        else self.policy.second_pass_attempts
                    )
                    completed = attempt_number
                    if outcome.retryable and completed < limit:
                        retry_job = _PCLJob(
                            job.venue_id,
                            job.pass_number,
                            completed,
                            job.origin,
                        )
                        self._reschedule(
                            retry_job,
                            self.policy.backoff_delay(completed),
                            "retrying",
                        )
                    elif (
                        outcome.retryable
                        and job.pass_number == 1
                        and self.policy.second_pass_attempts > 0
                    ):
                        retry_job = _PCLJob(job.venue_id, 2, 0, job.origin)
                        self._reschedule(
                            retry_job,
                            self.policy.backoff_delay(completed),
                            "second_pass",
                        )
                    else:
                        self._scheduled.discard(job.venue_id)
                        self._stats["failed"] += 1
                        self._event(
                            job.venue_id,
                            "failed",
                            pass_number=job.pass_number,
                            attempts_completed=completed,
                            status=outcome.status,
                            retryable=outcome.retryable,
                            error=self._safe_error(outcome.error),
                            origin=job.origin,
                        )
                self._persist_state()
                self._condition.notify_all()

    def wait(self, timeout: float | None = None) -> bool:
        """Wait until no queued or in-flight jobs remain."""

        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            if self._fatal_error:
                raise RuntimeError(f"PCL retry worker failed: {self._fatal_error}")
            while self._scheduled or self._inflight:
                if self._fatal_error:
                    raise RuntimeError(
                        f"PCL retry worker failed: {self._fatal_error}"
                    )
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._condition.wait(timeout=min(remaining, 0.5))
                else:
                    self._condition.wait(timeout=0.5)
            return True

    def close(self, *, drain: bool = True) -> dict[str, object]:
        """Stop workers, optionally leaving queued jobs for restart recovery."""

        wait_error: BaseException | None = None
        if drain:
            try:
                self.wait()
            except BaseException as exc:  # noqa: BLE001 - still stop peer workers.
                wait_error = exc
        with self._condition:
            self._accepting = False
            self._stop = True
            self._condition.notify_all()
        for thread in self._threads:
            # An in-flight HTTP call must finish before corpus assembly starts;
            # otherwise two writers could replace the same venue shard.
            thread.join()
        try:
            self._persist_state()
        except BaseException as exc:  # noqa: BLE001 - report persistence failure.
            if wait_error is None:
                wait_error = exc
        snapshot = self.snapshot()
        if self._fatal_error and wait_error is None:
            wait_error = RuntimeError(
                f"PCL retry worker failed: {self._fatal_error}"
            )
        if wait_error is not None:
            raise wait_error
        return snapshot
