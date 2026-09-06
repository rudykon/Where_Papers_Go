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
import math
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
from typing import Any, Callable, Iterator, Mapping, Sequence

try:  # pragma: no cover - the deployment target is Linux.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from .paths import DATA_DIR, PROJECT_ROOT


STATE_SCHEMA_VERSION = 1
STATE_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_STATE_FILE = DATA_DIR / ".tavily_key_pool_state.json"
TAVILY_STATE_FILE_ENV = "WPG_TAVILY_STATE_FILE"
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


@dataclass(frozen=True)
class _StateCandidate:
    """One securely-read state copy and its non-secret audit metadata."""

    state: dict[str, Any] | None
    revision: int | None
    content_sha256: str
    byte_count: int
    mode: str
    valid: bool

    def audit_metadata(self) -> dict[str, Any]:
        return {
            "present": True,
            "valid": self.valid,
            "revision": self.revision,
            "sha256": self.content_sha256,
            "bytes": self.byte_count,
            "mode": self.mode,
        }


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
        environment_state_file = os.environ.get(TAVILY_STATE_FILE_ENV, "").strip()
        raw_state_file = (
            environment_state_file
            or config.get("key_pool_state_file")
            or DEFAULT_STATE_FILE
        )
        state_file = Path(str(raw_state_file)).expanduser()
        if environment_state_file and not state_file.is_absolute():
            raise TavilyKeyPoolConfigError(
                f"{TAVILY_STATE_FILE_ENV} must be an absolute path"
            )
        if not state_file.is_absolute():
            state_file = (
                DATA_DIR.joinpath(*state_file.parts[1:])
                if state_file.parts and state_file.parts[0] == "data"
                else PROJECT_ROOT / state_file
            )
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
            "state_revision": 0,
            "strategy": "round_robin",
            "quota_per_key": self.quota_per_key,
            "next_index": 0,
            "updated_at": _utc_now(),
            "order": list(self._fingerprints),
            "keys": {},
        }

    @staticmethod
    def _is_valid_state(value: Any) -> bool:
        if not (
            isinstance(value, dict)
            and isinstance(value.get("schema_version"), int)
            and not isinstance(value.get("schema_version"), bool)
            and value.get("schema_version") == STATE_SCHEMA_VERSION
            and isinstance(value.get("keys"), dict)
        ):
            return False
        # Files written before the durability revision was introduced are
        # intentionally interpreted as revision zero.
        revision = value.get("state_revision", 0)
        return isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0

    @staticmethod
    def _candidate_revision(value: Any) -> int | None:
        if not isinstance(value, dict):
            return None
        revision = value.get("state_revision", 0)
        if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0:
            return revision
        return None

    @staticmethod
    def _is_nonnegative_integer(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    @staticmethod
    def _is_fingerprint(value: Any) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    def _is_valid_record(self, record: Any) -> bool:
        if not isinstance(record, dict):
            return False
        allowed_fields = {
            "position",
            "limit",
            "used",
            "successes",
            "failures",
            "empty_results",
            "status",
            "cooldown_until",
            "last_http_status",
            "last_event",
            "updated_at",
            "configured",
        }
        if set(record) != allowed_fields:
            return False
        for name in ("position", "limit", "used", "successes", "failures", "empty_results"):
            if not self._is_nonnegative_integer(record.get(name)):
                return False
        if record["limit"] <= 0:
            return False
        cooldown = record.get("cooldown_until")
        if (
            isinstance(cooldown, bool)
            or not isinstance(cooldown, (int, float))
            or not math.isfinite(float(cooldown))
            or float(cooldown) < 0.0
        ):
            return False
        if record.get("status") not in {"active", "cooldown", "invalid", "exhausted"}:
            return False
        if (
            record["used"] >= record["limit"]
            and record["status"] not in {"invalid", "exhausted"}
        ):
            return False
        http_status = record.get("last_http_status")
        if http_status is not None and (
            not isinstance(http_status, int)
            or isinstance(http_status, bool)
            or not 100 <= http_status <= 599
        ):
            return False
        last_event = record.get("last_event")
        updated_at = record.get("updated_at")
        if not isinstance(last_event, str) or not last_event or len(last_event) > 64:
            return False
        if not isinstance(updated_at, str) or not updated_at or len(updated_at) > 64:
            return False
        if not isinstance(record.get("configured"), bool):
            return False
        return True

    def _is_valid_persisted_state(self, value: Any) -> bool:
        if not self._is_valid_state(value):
            return False
        required_fields = {
            "schema_version",
            "strategy",
            "quota_per_key",
            "next_index",
            "updated_at",
            "order",
            "keys",
        }
        allowed_fields = required_fields | {"state_revision"}
        if not required_fields.issubset(value) or not set(value).issubset(allowed_fields):
            return False
        if value.get("strategy") != "round_robin":
            return False
        quota = value.get("quota_per_key")
        next_index = value.get("next_index")
        if (
            not self._is_nonnegative_integer(quota)
            or quota <= 0
            or not self._is_nonnegative_integer(next_index)
        ):
            return False
        updated_at = value.get("updated_at")
        if not isinstance(updated_at, str) or not updated_at or len(updated_at) > 64:
            return False
        order = value.get("order")
        keys = value.get("keys")
        if (
            not isinstance(order, list)
            or not order
            or not isinstance(keys, dict)
            or not all(self._is_fingerprint(fingerprint) for fingerprint in order)
            or len(set(order)) != len(order)
            or next_index >= len(order)
        ):
            return False
        if not all(self._is_fingerprint(fingerprint) for fingerprint in keys):
            return False
        if not all(self._is_valid_record(record) for record in keys.values()):
            return False
        # Every key named by the persisted configuration must have a complete
        # historical record, with its durable position and limit intact.
        for position, fingerprint in enumerate(order):
            record = keys.get(fingerprint)
            if not isinstance(record, dict):
                return False
            if record["position"] != position or record["limit"] != quota:
                return False
        # A configured fingerprint absent from both the persisted order and
        # records is a deliberate key addition and may start at zero.  In
        # contrast, an order member without its historical record was rejected
        # above, so an existing key can never regain quota through reconciliation.
        return True

    @staticmethod
    def _state_revision(state: Mapping[str, Any]) -> int:
        revision = state.get("state_revision", 0)
        if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0:
            return revision
        raise TavilyKeyPoolStateError(
            "Tavily key-pool state has an invalid revision; refusing to reset quota"
        )

    @staticmethod
    def _strict_json_loads(raw: bytes) -> Any:
        def reject_constant(value: str) -> None:
            raise ValueError(f"non-finite JSON value: {value}")

        def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON object key")
                result[key] = value
            return result

        return json.loads(
            raw.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_pairs,
        )

    @staticmethod
    def _canonical_state_bytes(state: Mapping[str, Any]) -> bytes:
        normalized = dict(state)
        normalized.setdefault("state_revision", 0)
        return json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    def _read_state_candidate(self, path: Path) -> _StateCandidate | None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise TavilyKeyPoolStateError(
                "Tavily key-pool state copy is unsafe or unreadable"
            ) from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise TavilyKeyPoolStateError(
                    "Tavily key-pool state copy is not a regular file"
                )
            if before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) != 0o600:
                raise TavilyKeyPoolStateError(
                    "Tavily key-pool state copy has unsafe ownership or mode"
                )
            if before.st_size > STATE_MAX_BYTES:
                raise TavilyKeyPoolStateError(
                    "Tavily key-pool state copy exceeds the safe size limit"
                )
            chunks: list[bytes] = []
            byte_count = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                byte_count += len(chunk)
                if byte_count > STATE_MAX_BYTES:
                    raise TavilyKeyPoolStateError(
                        "Tavily key-pool state copy exceeds the safe size limit"
                    )
            after = os.fstat(descriptor)
            before_identity = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            after_identity = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if before_identity != after_identity:
                raise TavilyKeyPoolStateError(
                    "Tavily key-pool state changed during read"
                )
        finally:
            os.close(descriptor)

        raw = b"".join(chunks)
        digest = hashlib.sha256(raw).hexdigest()
        mode = f"{stat.S_IMODE(before.st_mode):04o}"
        try:
            value = self._strict_json_loads(raw)
        except (UnicodeError, json.JSONDecodeError, ValueError):
            return _StateCandidate(None, None, digest, len(raw), mode, False)
        revision = self._candidate_revision(value)
        if not self._is_valid_persisted_state(value):
            return _StateCandidate(None, revision, digest, len(raw), mode, False)
        assert revision is not None
        return _StateCandidate(value, revision, digest, len(raw), mode, True)

    def _load_state_with_candidates(
        self,
        *,
        allow_fresh: bool = False,
    ) -> tuple[dict[str, Any], _StateCandidate | None, _StateCandidate | None]:
        primary = self._read_state_candidate(self.state_file)
        backup = self._read_state_candidate(self.backup_file)
        valid = [
            candidate
            for candidate in (primary, backup)
            if candidate is not None and candidate.valid
        ]
        if not valid:
            if primary is None and backup is None:
                if allow_fresh:
                    return self._fresh_state(), primary, backup
                raise TavilyKeyPoolStateError(
                    "Tavily key-pool state copies are missing behind an existing lock"
                )
            raise TavilyKeyPoolStateError(
                "Tavily key-pool state copies are invalid; refusing to reset quota"
            )
        highest_valid_revision = max(int(candidate.revision or 0) for candidate in valid)
        if any(
            candidate is not None
            and not candidate.valid
            and candidate.revision is not None
            and candidate.revision >= highest_valid_revision
            for candidate in (primary, backup)
        ):
            raise TavilyKeyPoolStateError(
                "Tavily key-pool state has a corrupt current-or-newer revision"
            )
        if (
            primary is not None
            and primary.valid
            and backup is not None
            and backup.valid
            and primary.revision == backup.revision
            and self._canonical_state_bytes(primary.state or {})
            != self._canonical_state_bytes(backup.state or {})
        ):
            raise TavilyKeyPoolStateError(
                "Tavily key-pool state copies conflict at the same revision"
            )
        selected = max(valid, key=lambda candidate: int(candidate.revision or 0))
        assert selected.state is not None
        return dict(selected.state), primary, backup

    def _load_state(self, *, allow_fresh: bool = False) -> dict[str, Any]:
        state, _, _ = self._load_state_with_candidates(allow_fresh=allow_fresh)
        return state

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

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
        )
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _publish_state_file(self, path: Path, payload: bytes) -> None:
        """Atomically publish and durably anchor one complete state copy."""

        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
            self._fsync_directory(path.parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def _atomic_write(self, state: dict[str, Any]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        state["state_revision"] = self._state_revision(state) + 1
        payload = json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
        # Backup is deliberately committed first.  Primary is not replaced
        # until a complete new generation is independently durable; a crash in
        # between leaves the higher revision in backup, which readers select.
        self._publish_state_file(self.backup_file, payload)
        self._publish_state_file(self.state_file, payload)

    def _open_lock_descriptor(self, *, create: bool) -> tuple[int, bool]:
        flags = (
            (os.O_RDWR if create else os.O_RDONLY)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            if create:
                try:
                    descriptor = os.open(
                        self.lock_file, flags | os.O_CREAT | os.O_EXCL, 0o600
                    )
                    created = True
                except FileExistsError:
                    descriptor = os.open(self.lock_file, flags)
                    created = False
            else:
                descriptor = os.open(self.lock_file, flags)
                created = False
        except FileNotFoundError as exc:
            if not create:
                raise TavilyKeyPoolStateError(
                    "Tavily key-pool audit requires an existing lock file"
                ) from exc
            raise TavilyKeyPoolStateError(
                "Tavily key-pool lock file cannot be created"
            ) from exc
        except OSError as exc:
            raise TavilyKeyPoolStateError(
                "Tavily key-pool lock file is unsafe or unreadable"
            ) from exc
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            os.close(descriptor)
            raise TavilyKeyPoolStateError(
                "Tavily key-pool lock file has unsafe ownership, type, or mode"
            )
        if created:
            # This empty file is the durable initialization sentinel.  Once it
            # exists, missing primary+backup can never be interpreted as a new
            # pool, even after a host crash.
            try:
                os.fsync(descriptor)
                self._fsync_directory(self.lock_file.parent)
            except OSError as exc:
                os.close(descriptor)
                raise TavilyKeyPoolStateError(
                    "Tavily key-pool initialization sentinel is not durable"
                ) from exc
        return descriptor, created

    @contextmanager
    def _state_lock(self, *, exclusive: bool, create: bool) -> Iterator[bool]:
        descriptor, created = self._open_lock_descriptor(create=create)
        try:
            if fcntl is not None:
                operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                fcntl.flock(descriptor, operation)
            try:
                yield created
            finally:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    @contextmanager
    def _locked_state(self) -> Iterator[dict[str, Any]]:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            with self._state_lock(exclusive=True, create=True) as lock_created:
                state = self._reconcile(
                    self._load_state(allow_fresh=lock_created)
                )
                try:
                    yield state
                finally:
                    state["updated_at"] = _utc_now()
                    self._atomic_write(state)

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

    def _aggregate_state(self, state: Mapping[str, Any]) -> dict[str, Any]:
        keys = state.get("keys") if isinstance(state.get("keys"), dict) else {}
        records = [keys[fingerprint] for fingerprint in self._fingerprints]
        status_counts: dict[str, int] = {}
        for record in records:
            status = str(record.get("status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
        used = sum(
            min(self.quota_per_key, int(record.get("used", 0))) for record in records
        )
        return {
            "key_count": len(records),
            "quota_per_key": self.quota_per_key,
            "total_capacity": len(records) * self.quota_per_key,
            "used": used,
            "remaining": max(0, len(records) * self.quota_per_key - used),
            "status_counts": status_counts,
            "strategy": "round_robin",
        }

    def _configured_keyset_sha256(self) -> str:
        canonical = json.dumps(
            sorted(self._fingerprints), separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        return hashlib.sha256(b"tavily-configured-keyset-v1\0" + canonical).hexdigest()

    @staticmethod
    def _candidate_audit_metadata(
        candidate: _StateCandidate | None,
    ) -> dict[str, Any]:
        if candidate is not None:
            return candidate.audit_metadata()
        return {
            "present": False,
            "valid": False,
            "revision": None,
            "sha256": None,
            "bytes": 0,
            "mode": None,
        }

    def audit_snapshot(self) -> dict[str, Any]:
        """Return a sanitized, read-only, durability-aware quota snapshot.

        Unlike :meth:`summary`, this path never enters ``_locked_state`` and
        therefore never reconciles or republishes state.  It requires the
        already-created private lock file and takes a shared process lock while
        securely reading both durable copies.
        """

        with self._thread_lock:
            with self._state_lock(exclusive=False, create=False):
                state, primary, backup = self._load_state_with_candidates()
                selected_revision = self._state_revision(state)
                configuration_current = bool(
                    state.get("order") == list(self._fingerprints)
                    and state.get("quota_per_key") == self.quota_per_key
                    and all(
                        isinstance(state.get("keys", {}).get(fingerprint), dict)
                        and state["keys"][fingerprint].get("configured") is True
                        for fingerprint in self._fingerprints
                    )
                    and all(
                        fingerprint in self._fingerprints
                        or record.get("configured") is False
                        for fingerprint, record in state.get("keys", {}).items()
                        if isinstance(record, dict)
                    )
                )
                reconciled = self._reconcile(state)
                snapshot = self._aggregate_state(reconciled)
                snapshot.update(
                    {
                        "schema_version": STATE_SCHEMA_VERSION,
                        "state_revision": selected_revision,
                        "configured_keyset_sha256": self._configured_keyset_sha256(),
                        "configuration_current": configuration_current,
                        "copies": {
                            "primary": self._candidate_audit_metadata(primary),
                            "backup": self._candidate_audit_metadata(backup),
                        },
                    }
                )
                return snapshot

    def summary(self) -> dict[str, Any]:
        """Return aggregate state safe for logs and health endpoints."""

        with self._locked_state() as state:
            summary = self._aggregate_state(state)
            summary["state_file"] = str(self.state_file)
            return summary


__all__ = [
    "DEFAULT_STATE_FILE",
    "TAVILY_STATE_FILE_ENV",
    "TavilyKeyLease",
    "TavilyKeyPool",
    "TavilyKeyPoolConfigError",
    "TavilyKeyPoolError",
    "TavilyKeyPoolStateError",
    "TavilyKeyPoolUnavailable",
    "configured_tavily_keys",
]
