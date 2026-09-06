"""Process-safe hard budget for explicitly authorized external HTTP attempts.

The limiter is dormant during normal product use.  A controlled evaluator sets
all three environment variables below for its worker process.  Every supported
HTTP transport reserves one durable ledger entry *before* opening a socket, so
retries and concurrency cannot restore spent allowance.

Each ledger has an independent ``.highwater.jsonl`` mirror and a mode-0400
``.binding.json`` file which fixes both file identities, the run ID, and the
budget.  Reservations are fsynced to the high-water file before the primary
ledger.  A truncated or rolled-back primary, a replaced inode, or a crash
between the two writes thus leaves a persistent mismatch and fails closed
before later transport.

Threat boundary: these local files detect accidental damage and rollback of
only part of the state.  They are not a cryptographic or remote witness.  An OS
administrator, or any principal with the same effective write access, can
truncate the two writable ledger/high-water inodes to the same older valid
prefix without changing the mode-0400 identity binding.  That coordinated
rollback is explicitly outside this mechanism's protection.  Such an
adversary requires an independently administered append-only/WORM or signed
remote audit service.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlsplit
import urllib.request

try:  # pragma: no cover - production and benchmark hosts are Linux.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


LEDGER_SCHEMA_VERSION = "1"
LEDGER_ENV = "WPG_EXTERNAL_CALL_LEDGER"
BUDGET_ENV = "WPG_EXTERNAL_CALL_BUDGET"
RUN_ID_ENV = "WPG_EXTERNAL_CALL_RUN_ID"
LEDGER_BINDING_SCHEMA_VERSION = "1"
LEDGER_HIGHWATER_SUFFIX = ".highwater.jsonl"
LEDGER_BINDING_SUFFIX = ".binding.json"
LEDGER_BINDING_MAX_BYTES = 16 * 1024
_PROCESS_LOCK = threading.Lock()


class ExternalCallBudgetError(RuntimeError):
    """Base error for a missing, corrupt, or exhausted hard-call ledger."""


class ExternalCallBudgetExceeded(ExternalCallBudgetError):
    """Raised before transport when the durable attempt budget is exhausted."""


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Turn every redirect urllib would otherwise follow into an HTTPError."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_endpoint(url: str) -> str:
    """Return an audit-safe origin with credentials, path, and query removed."""

    try:
        parsed = urlsplit(str(url or ""))
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
    except ValueError:
        return "invalid"
    return f"{parsed.scheme}://{host}" if parsed.scheme and host else "invalid"


def _header(*, budget: int, run_id: str) -> dict[str, Any]:
    return {
        "record_type": "header",
        "schema_version": LEDGER_SCHEMA_VERSION,
        "run_id": run_id,
        "budget": budget,
        "created_at": _utc_now(),
    }


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short external-call ledger write")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _highwater_path(path: Path) -> Path:
    return path.with_name(path.name + LEDGER_HIGHWATER_SUFFIX)


def _binding_path(path: Path) -> Path:
    return path.with_name(path.name + LEDGER_BINDING_SUFFIX)


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _path_sha256(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()


def _create_exclusive_file(
    path: Path, payload: bytes, *, mode: int
) -> os.stat_result:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, mode)
    try:
        os.fchmod(descriptor, mode)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        return os.fstat(descriptor)
    finally:
        os.close(descriptor)


def _binding_payload(
    path: Path,
    highwater: Path,
    *,
    ledger_info: os.stat_result,
    highwater_info: os.stat_result,
    budget: int,
    run_id: str,
) -> dict[str, Any]:
    return {
        "artifact_type": "external_call_ledger_binding",
        "schema_version": LEDGER_BINDING_SCHEMA_VERSION,
        "run_id": run_id,
        "budget": budget,
        "ledger_path_sha256": _path_sha256(path),
        "ledger_device": ledger_info.st_dev,
        "ledger_inode": ledger_info.st_ino,
        "highwater_path_sha256": _path_sha256(highwater),
        "highwater_device": highwater_info.st_dev,
        "highwater_inode": highwater_info.st_ino,
    }


def initialize_external_call_ledger(
    path: Path,
    *,
    budget: int,
    run_id: str,
) -> None:
    """Create one new ledger/high-water/binding set without overwriting state.

    A failure after any exclusive create intentionally preserves the partial
    state.  A later status check or reservation then fails closed instead of
    manufacturing a fresh authorization history.
    """

    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1:
        raise ExternalCallBudgetError("external-call budget must be positive")
    if not str(run_id).strip():
        raise ExternalCallBudgetError("external-call ledger requires a run ID")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        _header(budget=budget, run_id=str(run_id)),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    highwater = _highwater_path(path)
    try:
        ledger_info = _create_exclusive_file(path, payload, mode=0o600)
        highwater_info = _create_exclusive_file(highwater, payload, mode=0o600)
        binding = _binding_payload(
            path,
            highwater,
            ledger_info=ledger_info,
            highwater_info=highwater_info,
            budget=budget,
            run_id=str(run_id),
        )
        binding_payload = json.dumps(
            binding,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        _create_exclusive_file(_binding_path(path), binding_payload, mode=0o400)
    except BaseException:
        try:
            _fsync_directory(path.parent)
        except OSError:
            pass
        raise
    _fsync_directory(path.parent)


def _parse_ledger(data: bytes) -> tuple[Mapping[str, Any], int]:
    if not data.endswith(b"\n"):
        raise ExternalCallBudgetError(
            "external-call ledger has a truncated final record"
        )
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise ExternalCallBudgetError("external-call ledger is not UTF-8") from exc
    if not lines:
        raise ExternalCallBudgetError("external-call ledger is empty")
    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise ExternalCallBudgetError("external-call ledger header is invalid") from exc
    if (
        not isinstance(header, Mapping)
        or header.get("record_type") != "header"
        or header.get("schema_version") != LEDGER_SCHEMA_VERSION
        or not isinstance(header.get("budget"), int)
        or isinstance(header.get("budget"), bool)
        or int(header["budget"]) < 1
        or not str(header.get("run_id") or "").strip()
    ):
        raise ExternalCallBudgetError("external-call ledger header is incompatible")
    attempts = 0
    for line_number, line in enumerate(lines[1:], 2):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ExternalCallBudgetError(
                f"external-call ledger record {line_number} is invalid"
            ) from exc
        if not isinstance(record, Mapping) or record.get("record_type") != "attempt":
            raise ExternalCallBudgetError(
                f"external-call ledger record {line_number} is incompatible"
            )
        if record.get("ordinal") != attempts + 1:
            raise ExternalCallBudgetError(
                f"external-call ledger record {line_number} has a non-sequential ordinal"
            )
        attempts += 1
    return header, attempts


def _validate_private_regular(
    descriptor: int,
    *,
    label: str,
    expected_mode: int | None = None,
) -> os.stat_result:
    info = os.fstat(descriptor)
    mode = stat.S_IMODE(info.st_mode)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or mode & 0o077
        or (expected_mode is not None and mode != expected_mode)
    ):
        if expected_mode is None:
            detail = "an owned private regular file"
        else:
            detail = f"an owned regular file with mode {expected_mode:04o}"
        raise ExternalCallBudgetError(f"external-call {label} must be {detail}")
    return info


def _validate_path_matches_descriptor(
    path: Path,
    descriptor: int,
    *,
    label: str,
    expected_mode: int | None = None,
) -> os.stat_result:
    """Reject a path swap before trusting an already-open continuity file."""

    descriptor_info = _validate_private_regular(
        descriptor, label=label, expected_mode=expected_mode
    )
    try:
        path_info = path.lstat()
    except OSError as exc:
        raise ExternalCallBudgetError(
            f"external-call {label} path is unavailable; refusing transport"
        ) from exc
    if (
        stat.S_ISLNK(path_info.st_mode)
        or not stat.S_ISREG(path_info.st_mode)
        or path_info.st_uid != os.geteuid()
        or (path_info.st_dev, path_info.st_ino)
        != (descriptor_info.st_dev, descriptor_info.st_ino)
    ):
        raise ExternalCallBudgetError(
            f"external-call {label} path was replaced; refusing transport"
        )
    return descriptor_info


@contextmanager
def _locked_ledger_pair(
    path: Path, *, exclusive: bool
) -> Iterator[dict[str, int]]:
    if fcntl is None:  # pragma: no cover
        raise ExternalCallBudgetError(
            "process-safe external-call locking is unavailable; refusing transport"
        )
    flags = (os.O_RDWR | os.O_APPEND) if exclusive else os.O_RDONLY
    # O_NONBLOCK is inert for regular files, but prevents a malicious or
    # accidentally substituted FIFO from hanging the fail-closed type check.
    flags |= (
        getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    candidates = (
        ("ledger", path),
        ("high-water anchor", _highwater_path(path)),
    )
    paths_by_label = dict(candidates)
    descriptors: dict[str, int] = {}
    try:
        try:
            for label, candidate in candidates:
                descriptor = os.open(candidate, flags)
                descriptors[label] = descriptor
                fcntl.flock(
                    descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                )
                _validate_path_matches_descriptor(
                    candidate, descriptor, label=label
                )
        except OSError as exc:
            raise ExternalCallBudgetError(
                "external-call ledger continuity files are unavailable; "
                "refusing transport"
            ) from exc
        yield descriptors
    finally:
        pending_error: ExternalCallBudgetError | None = None
        for label, descriptor in descriptors.items():
            try:
                _validate_path_matches_descriptor(
                    paths_by_label[label], descriptor, label=label
                )
            except ExternalCallBudgetError as exc:
                pending_error = pending_error or exc
        for descriptor in reversed(tuple(descriptors.values())):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        if pending_error is not None:
            raise pending_error


def _read_locked_pair(
    descriptors: Mapping[str, int],
) -> tuple[bytes, os.stat_result, os.stat_result]:
    before = {
        label: os.fstat(descriptor) for label, descriptor in descriptors.items()
    }
    payloads: dict[str, bytes] = {}
    for label, descriptor in descriptors.items():
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            chunks.append(chunk)
        payloads[label] = b"".join(chunks)
    after = {
        label: os.fstat(descriptor) for label, descriptor in descriptors.items()
    }
    for label in descriptors:
        if (
            _identity(before[label]) != _identity(after[label])
            or len(payloads[label]) != before[label].st_size
        ):
            raise ExternalCallBudgetError(
                f"external-call {label} changed while continuity was checked"
            )
    ledger = payloads["ledger"]
    highwater = payloads["high-water anchor"]
    if ledger != highwater:
        raise ExternalCallBudgetError(
            "external-call ledger rolled back or diverged from its high-water anchor"
        )
    return ledger, after["ledger"], after["high-water anchor"]


def _read_and_validate_binding(
    path: Path,
    *,
    ledger_info: os.stat_result,
    highwater_info: os.stat_result,
    header: Mapping[str, Any],
) -> None:
    binding_path = _binding_path(path)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(binding_path, flags)
    except OSError as exc:
        raise ExternalCallBudgetError(
            "external-call ledger binding is unavailable; refusing transport"
        ) from exc
    try:
        before = _validate_path_matches_descriptor(
            binding_path,
            descriptor,
            label="ledger binding",
            expected_mode=0o400,
        )
        if before.st_size > LEDGER_BINDING_MAX_BYTES:
            raise ExternalCallBudgetError("external-call ledger binding is oversized")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 4096)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        raw = b"".join(chunks)
        path_after = _validate_path_matches_descriptor(
            binding_path,
            descriptor,
            label="ledger binding",
            expected_mode=0o400,
        )
        if (
            _identity(before) != _identity(after)
            or _identity(after) != _identity(path_after)
            or len(raw) != before.st_size
        ):
            raise ExternalCallBudgetError(
                "external-call ledger binding changed while it was checked"
            )
    finally:
        os.close(descriptor)
    try:
        observed = json.loads(raw.decode("utf-8"))
        budget = int(header.get("budget"))
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ExternalCallBudgetError("external-call ledger binding is invalid") from exc
    expected = _binding_payload(
        path,
        _highwater_path(path),
        ledger_info=ledger_info,
        highwater_info=highwater_info,
        budget=budget,
        run_id=str(header.get("run_id") or ""),
    )
    expected_raw = json.dumps(
        expected,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if not isinstance(observed, Mapping) or observed != expected or raw != expected_raw:
        raise ExternalCallBudgetError(
            "external-call ledger binding mismatch; file identity or budget was replaced"
        )


def _validated_locked_state(
    path: Path, descriptors: Mapping[str, int]
) -> tuple[Mapping[str, Any], int]:
    raw, ledger_info, highwater_info = _read_locked_pair(descriptors)
    header, used = _parse_ledger(raw)
    _read_and_validate_binding(
        path,
        ledger_info=ledger_info,
        highwater_info=highwater_info,
        header=header,
    )
    return header, used


def _append_durable(descriptor: int, payload: bytes) -> None:
    os.lseek(descriptor, 0, os.SEEK_END)
    _write_all(descriptor, payload)
    os.fsync(descriptor)


def external_call_ledger_status(path: Path) -> dict[str, Any]:
    """Return counter data only after persistent continuity verification."""

    path = Path(path)
    with _PROCESS_LOCK, _locked_ledger_pair(path, exclusive=False) as descriptors:
        header, used = _validated_locked_state(path, descriptors)
    budget = int(header.get("budget"))
    return {
        "schema_version": str(header.get("schema_version")),
        "run_id": str(header.get("run_id") or ""),
        "budget": budget,
        "used": used,
        "remaining": max(0, budget - used),
        "continuity_verified": True,
    }


def reserve_external_call(kind: str, url: str) -> int | None:
    """Durably reserve one configured attempt, or do nothing outside a budgeted run.

    A partially configured environment fails closed.  The return value is the
    one-based attempt number, or ``None`` when no evaluator budget is active.
    """

    ledger_value = os.environ.get(LEDGER_ENV)
    budget_value = os.environ.get(BUDGET_ENV)
    run_id = os.environ.get(RUN_ID_ENV)
    configured = tuple(value is not None for value in (ledger_value, budget_value, run_id))
    if not any(configured):
        return None
    if not all(configured):
        raise ExternalCallBudgetError(
            "external-call limiter environment is incomplete; refusing transport"
        )
    try:
        budget = int(str(budget_value))
    except (TypeError, ValueError) as exc:
        raise ExternalCallBudgetError(
            "external-call budget environment is invalid"
        ) from exc
    if budget < 1 or not str(run_id).strip():
        raise ExternalCallBudgetError(
            "external-call limiter environment is invalid; refusing transport"
        )
    path = Path(str(ledger_value))
    with _PROCESS_LOCK:
        try:
            with _locked_ledger_pair(path, exclusive=True) as descriptors:
                header, used = _validated_locked_state(path, descriptors)
                if int(header.get("budget", -1)) != budget or str(
                    header.get("run_id") or ""
                ) != str(run_id):
                    raise ExternalCallBudgetError(
                        "external-call ledger identity does not match the worker environment"
                    )
                if used >= budget:
                    raise ExternalCallBudgetExceeded(
                        f"external-call budget exhausted ({used}/{budget}); refusing transport"
                    )
                ordinal = used + 1
                record = {
                    "record_type": "attempt",
                    "ordinal": ordinal,
                    "reserved_at": _utc_now(),
                    "pid": os.getpid(),
                    "kind": str(kind or "http")[:32],
                    "endpoint": _safe_endpoint(url),
                }
                encoded = json.dumps(
                    record, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8") + b"\n"
                # The high-water anchor is authoritative.  Persist it first so
                # every interruption before the primary catches up is a durable
                # fail-closed divergence, never restored allowance.
                _append_durable(descriptors["high-water anchor"], encoded)
                _append_durable(descriptors["ledger"], encoded)
                return ordinal
        except ExternalCallBudgetError:
            raise
        except OSError as exc:
            raise ExternalCallBudgetError(
                "external-call reservation could not be persisted; refusing transport"
            ) from exc


def prepare_external_call_urlopen(
    kind: str,
    url: str,
    *,
    unbudgeted_open: Callable[..., Any],
    proxy_handler: urllib.request.ProxyHandler | None = None,
) -> Callable[..., Any]:
    """Reserve an attempt and select the only opener allowed for that attempt.

    Normal product traffic keeps its caller-provided transport, including the
    usual urllib redirect behavior.  An explicitly budgeted worker instead
    gets a fresh opener which preserves its selected proxy policy but refuses
    all redirects.  Creating a fresh opener also prevents a process-global or
    caller-created opener from silently adding follow-up HTTP requests after a
    single durable reservation.

    Reservation deliberately happens before opener construction.  Therefore
    an exhausted or malformed ledger cannot reach any transport or proxy.
    """

    ordinal = reserve_external_call(kind, url)
    if ordinal is None:
        return unbudgeted_open
    handlers: list[urllib.request.BaseHandler] = []
    if proxy_handler is not None:
        handlers.append(proxy_handler)
    handlers.append(_RejectRedirectHandler())
    return urllib.request.build_opener(*handlers).open


__all__ = [
    "BUDGET_ENV",
    "ExternalCallBudgetError",
    "ExternalCallBudgetExceeded",
    "LEDGER_ENV",
    "RUN_ID_ENV",
    "external_call_ledger_status",
    "initialize_external_call_ledger",
    "prepare_external_call_urlopen",
    "reserve_external_call",
]
