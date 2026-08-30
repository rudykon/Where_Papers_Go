"""Process-safe hard budget for explicitly authorized external HTTP attempts.

The limiter is dormant during normal product use.  A controlled evaluator sets
all three environment variables below for its worker process.  Every supported
HTTP transport reserves one durable ledger entry *before* opening a socket, so
retries, concurrency, and crashes cannot restore spent allowance.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import threading
from typing import Any, Callable, Mapping
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
_PROCESS_LOCK = threading.Lock()
_LEDGER_CACHE: dict[
    str, tuple[int, int, int, int, int, Mapping[str, Any], int]
] = {}


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


def initialize_external_call_ledger(
    path: Path,
    *,
    budget: int,
    run_id: str,
) -> None:
    """Create a new ledger exclusively; an existing path is never overwritten."""

    if budget < 1:
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
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        parent_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except OSError:
        pass


def _parse_ledger(data: bytes) -> tuple[Mapping[str, Any], int]:
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


def external_call_ledger_status(path: Path) -> dict[str, Any]:
    """Return header/counter data without exposing request or credential content."""

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(Path(path), flags)
    except (OSError, TypeError, ValueError) as exc:
        raise ExternalCallBudgetError("cannot read external-call ledger") from exc
    try:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_SH)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise ExternalCallBudgetError(
                "external-call ledger must be a private regular file"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            chunks.append(chunk)
        header, used = _parse_ledger(b"".join(chunks))
        budget = int(header.get("budget"))
    except (OSError, TypeError, ValueError) as exc:
        raise ExternalCallBudgetError("cannot read external-call ledger") from exc
    finally:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    return {
        "schema_version": str(header.get("schema_version")),
        "run_id": str(header.get("run_id") or ""),
        "budget": budget,
        "used": used,
        "remaining": max(0, budget - used),
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
    if fcntl is None:  # pragma: no cover
        raise ExternalCallBudgetError(
            "process-safe external-call locking is unavailable; refusing transport"
        )

    path = Path(str(ledger_value))
    flags = os.O_RDWR | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    with _PROCESS_LOCK:
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ExternalCallBudgetError(
                "external-call ledger is unavailable; refusing transport"
            ) from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                raise ExternalCallBudgetError(
                    "external-call ledger must be a private regular file"
                )
            cache_key = str(path.resolve())
            cached = _LEDGER_CACHE.get(cache_key)
            if cached is not None and cached[:5] == (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            ):
                header, used = cached[5], cached[6]
            else:
                os.lseek(descriptor, 0, os.SEEK_SET)
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(descriptor, 65_536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                header, used = _parse_ledger(b"".join(chunks))
                _LEDGER_CACHE[cache_key] = (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                    header,
                    used,
                )
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
            os.lseek(descriptor, 0, os.SEEK_END)
            _write_all(descriptor, encoded)
            os.fsync(descriptor)
            updated = os.fstat(descriptor)
            _LEDGER_CACHE[cache_key] = (
                updated.st_dev,
                updated.st_ino,
                updated.st_size,
                updated.st_mtime_ns,
                updated.st_ctime_ns,
                header,
                ordinal,
            )
            return ordinal
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


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
