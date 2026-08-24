"""Persistent, quota-aware Tavily API key rotation.

Only irreversible key fingerprints are written to disk.  Quota is reserved
before a network attempt so concurrent workers and interrupted processes cannot
silently exceed the configured local allowance.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from typing import Any, Callable, Iterator, Mapping, Sequence

try:  # pragma: no cover - the deployment target is Linux.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from .paths import PROJECT_ROOT


STATE_SCHEMA_VERSION = 1
DEFAULT_STATE_FILE = PROJECT_ROOT / "data" / ".tavily_key_pool_state.json"
LEGACY_KEY_FIELDS = (
    "api_key",
    "key",
    "api_key2",
    "api_key_2",
    "backup_api_key",
    "fallback_api_key",
)


class TavilyKeyPoolError(RuntimeError):
    """Base error whose message never contains a plaintext credential."""


class TavilyKeyPoolConfigError(TavilyKeyPoolError):
    """The key-pool configuration is invalid."""


class TavilyKeyPoolStateError(TavilyKeyPoolError):
    """Persistent state cannot be trusted and quota allocation fails closed."""


class TavilyKeyPoolUnavailable(TavilyKeyPoolError):
    """No key is currently available."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        exhausted: bool = False,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.exhausted = exhausted


@dataclass(frozen=True)
class TavilyKeyLease:
    """One quota reservation.  The API key is deliberately excluded from repr."""

    api_key: str = field(repr=False)
    fingerprint: str
    position: int
    quota_per_key: int


_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _path_lock(path: Path) -> threading.RLock:
    identity = str(path.resolve())
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(identity, threading.RLock())


def _fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _as_key_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        value = value.get("api_key") or value.get("key")
    if isinstance(value, str):
        # A JSON array is preferred, but newline/comma separated legacy values
        # are accepted to make configuration recovery less fragile.
        candidates = value.replace(",", "\n").splitlines()
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        candidates = []
        for item in value:
            candidates.extend(_as_key_values(item))
    else:
        candidates = [str(value)]
    return [candidate.strip() for candidate in candidates if candidate.strip()]


def configured_tavily_keys(config: Mapping[str, Any]) -> list[str]:
    """Return unique keys in configured order.

    The canonical ``api_keys`` field is authoritative.  Legacy aliases are
    consulted only when that field is absent, which lets operators retire old
    exhausted keys without accidentally keeping them active.
    """

    values: list[str] = []
    if "api_keys" in config:
        values.extend(_as_key_values(config.get("api_keys")))
    else:
        for name in LEGACY_KEY_FIELDS:
            values.extend(_as_key_values(config.get(name)))
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _positive_int(value: Any, *, default: int, name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise TavilyKeyPoolConfigError(f"{name} must be a positive integer") from exc
    if result <= 0:
        raise TavilyKeyPoolConfigError(f"{name} must be a positive integer")
    return result


def _nonnegative_float(value: Any, *, default: float, name: str) -> float:
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise TavilyKeyPoolConfigError(f"{name} must be non-negative") from exc
    if result < 0:
        raise TavilyKeyPoolConfigError(f"{name} must be non-negative")
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class TavilyKeyPool:
    """Round-robin key pool with persistent per-key quotas and cooldowns."""

    def __init__(
        self,
        api_keys: Sequence[str],
        *,
        quota_per_key: int = 1000,
        state_file: Path = DEFAULT_STATE_FILE,
        rate_limit_cooldown_seconds: float = 3600,
        transient_cooldown_seconds: float = 60,
        clock: Callable[[], float] = time.time,
    ) -> None:
        unique: list[str] = []
        seen: set[str] = set()
        for raw in api_keys:
            value = str(raw or "").strip()
            if value and value not in seen:
                seen.add(value)
                unique.append(value)
        if not unique:
            raise TavilyKeyPoolConfigError("tavily search requires at least one API key")
        self._api_keys = tuple(unique)
        self._fingerprints = tuple(_fingerprint(value) for value in unique)
        self._by_fingerprint = dict(zip(self._fingerprints, self._api_keys))
        self.quota_per_key = _positive_int(
            quota_per_key, default=1000, name="quota_per_key"
        )
        self.state_file = Path(state_file)
        self.backup_file = self.state_file.with_name(self.state_file.name + ".bak")
        self.lock_file = self.state_file.with_name(self.state_file.name + ".lock")
        self.rate_limit_cooldown_seconds = _nonnegative_float(
            rate_limit_cooldown_seconds,
            default=3600,
            name="rate_limit_cooldown_seconds",
        )
        self.transient_cooldown_seconds = _nonnegative_float(
            transient_cooldown_seconds,
            default=60,
            name="transient_cooldown_seconds",
        )
        self._clock = clock
        self._thread_lock = _path_lock(self.state_file)

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "TavilyKeyPool":
        keys = configured_tavily_keys(config)
        raw_state_file = config.get("key_pool_state_file") or DEFAULT_STATE_FILE
        state_file = Path(str(raw_state_file))
        if not state_file.is_absolute():
            state_file = PROJECT_ROOT / state_file
        return cls(
            keys,
            quota_per_key=_positive_int(
                config.get("quota_per_key", 1000),
                default=1000,
                name="quota_per_key",
            ),
            state_file=state_file,
            rate_limit_cooldown_seconds=_nonnegative_float(
                config.get("rate_limit_cooldown_seconds"),
                default=3600,
                name="rate_limit_cooldown_seconds",
            ),
            transient_cooldown_seconds=_nonnegative_float(
                config.get("transient_cooldown_seconds"),
                default=60,
                name="transient_cooldown_seconds",
            ),
        )

    @property
    def key_count(self) -> int:
        return len(self._api_keys)

    def _fresh_state(self) -> dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "strategy": "round_robin",
            "quota_per_key": self.quota_per_key,
            "next_index": 0,
            "updated_at": _utc_now(),
            "order": list(self._fingerprints),
            "keys": {},
        }

    @staticmethod
    def _is_valid_state(value: Any) -> bool:
        return (
            isinstance(value, dict)
            and value.get("schema_version") == STATE_SCHEMA_VERSION
            and isinstance(value.get("keys"), dict)
        )

    def _read_json_file(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TavilyKeyPoolStateError(
                "Tavily key-pool state is unreadable; refusing to reset quota"
            ) from exc
        if not self._is_valid_state(value):
            raise TavilyKeyPoolStateError(
                "Tavily key-pool state has an unsupported schema; refusing to reset quota"
            )
        return value

    def _load_state(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return self._fresh_state()
        try:
            return self._read_json_file(self.state_file)
        except TavilyKeyPoolStateError as primary_error:
            if self.backup_file.exists():
                try:
                    return self._read_json_file(self.backup_file)
                except TavilyKeyPoolStateError:
                    pass
            raise primary_error

    def _record_template(self, position: int) -> dict[str, Any]:
        return {
            "position": position,
            "limit": self.quota_per_key,
            "used": 0,
            "successes": 0,
            "failures": 0,
            "empty_results": 0,
            "status": "active",
            "cooldown_until": 0.0,
            "last_http_status": None,
            "last_event": "configured",
            "updated_at": _utc_now(),
        }

    def _reconcile(self, state: dict[str, Any]) -> dict[str, Any]:
        old_keys = state.get("keys") if isinstance(state.get("keys"), dict) else {}
        # Retain counters for temporarily removed keys.  Re-adding a key must
        # never reset its local quota; only fingerprints, not secrets, persist.
        reconciled: dict[str, dict[str, Any]] = {
            str(fingerprint): dict(record)
            for fingerprint, record in old_keys.items()
            if isinstance(record, dict)
        }
        for record in reconciled.values():
            record["configured"] = False
        now = self._clock()
        for position, fingerprint in enumerate(self._fingerprints):
            old = old_keys.get(fingerprint)
            record = dict(old) if isinstance(old, dict) else self._record_template(position)
            record["position"] = position
            record["configured"] = True
            record["limit"] = self.quota_per_key
            for name in ("used", "successes", "failures", "empty_results"):
                try:
                    record[name] = max(0, int(record.get(name, 0)))
                except (TypeError, ValueError):
                    record[name] = 0
            try:
                record["cooldown_until"] = max(
                    0.0, float(record.get("cooldown_until", 0.0))
                )
            except (TypeError, ValueError):
                record["cooldown_until"] = 0.0
            status = str(record.get("status") or "active")
            event = str(record.get("last_event") or "")
            if status == "cooldown" and record["cooldown_until"] <= now:
                status = "active"
                record["cooldown_until"] = 0.0
                event = "cooldown_expired"
            if (
                status == "exhausted"
                and event == "local_quota"
                and record["used"] < self.quota_per_key
            ):
                status = "active"
            if record["used"] >= self.quota_per_key and status not in {
                "invalid",
                "exhausted",
            }:
                status = "exhausted"
                event = "local_quota"
            record["status"] = status
            record["last_event"] = event
            reconciled[fingerprint] = record
        state["schema_version"] = STATE_SCHEMA_VERSION
        state["strategy"] = "round_robin"
        state["quota_per_key"] = self.quota_per_key
        state["order"] = list(self._fingerprints)
        state["keys"] = reconciled
        try:
            next_index = int(state.get("next_index", 0))
        except (TypeError, ValueError):
            next_index = 0
        state["next_index"] = next_index % len(self._fingerprints)
        return state

    def _atomic_write(self, state: Mapping[str, Any]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{self.state_file.name}.",
            suffix=".tmp",
            dir=self.state_file.parent,
            text=True,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.state_file)
            # The backup mirrors the just-committed allocation, rather than
            # lagging one reservation behind and potentially restoring quota.
            shutil.copyfile(self.state_file, self.backup_file)
            os.chmod(self.backup_file, 0o600)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    @contextmanager
    def _locked_state(self) -> Iterator[dict[str, Any]]:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            with self.lock_file.open("a+", encoding="utf-8") as lock_handle:
                try:
                    os.chmod(self.lock_file, 0o600)
                except OSError:
                    pass
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                try:
                    state = self._reconcile(self._load_state())
                    try:
                        yield state
                    finally:
                        state["updated_at"] = _utc_now()
                        self._atomic_write(state)
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _eligible(record: Mapping[str, Any], now: float) -> bool:
        if str(record.get("status") or "active") != "active":
            return False
        return int(record.get("used", 0)) < int(record.get("limit", 0))

    def acquire(self) -> TavilyKeyLease:
        """Reserve one quota unit and return the next usable credential."""

        now = self._clock()
        with self._locked_state() as state:
            start = int(state["next_index"])
            for offset in range(len(self._fingerprints)):
                position = (start + offset) % len(self._fingerprints)
                fingerprint = self._fingerprints[position]
                record = state["keys"][fingerprint]
                if not self._eligible(record, now):
                    continue
                record["used"] += 1
                record["last_event"] = "reserved"
                record["updated_at"] = _utc_now()
                if record["used"] >= self.quota_per_key:
                    record["status"] = "exhausted"
                    record["last_event"] = "local_quota"
                state["next_index"] = (position + 1) % len(self._fingerprints)
                return TavilyKeyLease(
                    api_key=self._by_fingerprint[fingerprint],
                    fingerprint=fingerprint,
                    position=position,
                    quota_per_key=self.quota_per_key,
                )

            cooling = [
                float(record.get("cooldown_until", 0.0))
                for record in state["keys"].values()
                if str(record.get("status")) == "cooldown"
                and float(record.get("cooldown_until", 0.0)) > now
            ]
            if cooling:
                retry_after = max(0.0, min(cooling) - now)
                raise TavilyKeyPoolUnavailable(
                    "all Tavily API keys are cooling down",
                    retry_after_seconds=retry_after,
                    exhausted=False,
                )
            raise TavilyKeyPoolUnavailable(
                "all Tavily API keys are exhausted or invalid",
                exhausted=True,
            )

    def reserve_transport_retry(self, lease: TavilyKeyLease) -> None:
        """Conservatively reserve another unit before retrying the same key."""

        with self._locked_state() as state:
            record = state["keys"].get(lease.fingerprint)
            if not isinstance(record, dict) or not self._eligible(record, self._clock()):
                raise TavilyKeyPoolUnavailable(
                    "Tavily API key has no quota for a transport retry",
                    exhausted=True,
                )
            record["used"] += 1
            record["last_event"] = "transport_retry_reserved"
            record["updated_at"] = _utc_now()
            if record["used"] >= self.quota_per_key:
                record["status"] = "exhausted"
                record["last_event"] = "local_quota"

    def report_success(self, lease: TavilyKeyLease, *, empty: bool = False) -> None:
        with self._locked_state() as state:
            record = state["keys"].get(lease.fingerprint)
            if not isinstance(record, dict):
                return
            record["successes"] += 1
            if empty:
                record["empty_results"] += 1
                record["last_event"] = "success_empty"
            else:
                record["last_event"] = "success"
            record["last_http_status"] = 200
            record["updated_at"] = _utc_now()
            if record["used"] < self.quota_per_key and record["status"] == "cooldown":
                record["status"] = "active"
                record["cooldown_until"] = 0.0

    def report_failure(
        self,
        lease: TavilyKeyLease,
        *,
        http_status: int | None = None,
        retry_after_seconds: float | None = None,
        event: str = "request_error",
    ) -> None:
        """Persist a provider/transport failure without storing its raw message."""

        safe_event = "".join(
            character for character in str(event) if character.isalnum() or character in "_-"
        )[:64] or "request_error"
        with self._locked_state() as state:
            record = state["keys"].get(lease.fingerprint)
            if not isinstance(record, dict):
                return
            record["failures"] += 1
            record["last_http_status"] = http_status
            record["last_event"] = safe_event
            record["updated_at"] = _utc_now()
            if http_status in {401, 403}:
                record["status"] = "invalid"
                record["used"] = self.quota_per_key
                record["cooldown_until"] = 0.0
            elif http_status == 432:
                record["status"] = "exhausted"
                record["used"] = self.quota_per_key
                record["cooldown_until"] = 0.0
            elif record["used"] >= self.quota_per_key:
                record["status"] = "exhausted"
                record["last_event"] = "local_quota"
                record["cooldown_until"] = 0.0
            else:
                if http_status == 429:
                    delay = (
                        self.rate_limit_cooldown_seconds
                        if retry_after_seconds is None
                        else max(0.0, float(retry_after_seconds))
                    )
                else:
                    delay = self.transient_cooldown_seconds
                record["status"] = "cooldown"
                record["cooldown_until"] = self._clock() + delay

    def summary(self) -> dict[str, Any]:
        """Return aggregate state safe for logs and health endpoints."""

        with self._locked_state() as state:
            records = [state["keys"][fingerprint] for fingerprint in self._fingerprints]
            status_counts: dict[str, int] = {}
            for record in records:
                status = str(record.get("status") or "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1
            used = sum(min(self.quota_per_key, int(record.get("used", 0))) for record in records)
            return {
                "key_count": len(records),
                "quota_per_key": self.quota_per_key,
                "total_capacity": len(records) * self.quota_per_key,
                "used": used,
                "remaining": max(0, len(records) * self.quota_per_key - used),
                "status_counts": status_counts,
                "strategy": "round_robin",
                "state_file": str(self.state_file),
            }


__all__ = [
    "DEFAULT_STATE_FILE",
    "TavilyKeyLease",
    "TavilyKeyPool",
    "TavilyKeyPoolConfigError",
    "TavilyKeyPoolError",
    "TavilyKeyPoolStateError",
    "TavilyKeyPoolUnavailable",
    "configured_tavily_keys",
]
