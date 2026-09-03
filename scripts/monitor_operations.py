#!/usr/bin/env python3
"""Run one bounded, provider-free production operations observation.

The command is deliberately a one-shot state machine.  It reads the user
systemd unit, the kernel boot identity, the authenticated *loopback* detailed
health endpoint, and (unless explicitly disabled) an incremental slice of the
user journal.  It never calls Search, LLM, or embedding providers.

Dry-run is the default: the proposed journal cursor and alert transitions are
reported but no file is created or changed.  ``--apply`` serializes concurrent
runs and atomically replaces one owner-only 0600 state file.  Alert delivery is
outside this command; the latest pending event for every fixed alert code is
retained in state for a later notifier.

Exit status contract:

* 0: a valid observation produced no first/escalation/repeat/recovery event;
* 2: a valid observation produced at least one such event;
* 3: arguments, trusted input, collection, or state validation failed closed.

The pure parsing, aggregation, and transition functions are intentionally kept
separate from I/O so journal fixtures and alert timing can be tested without a
running service or any network transport.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import selectors
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request

try:  # pragma: no cover - production and the deployment target are Linux.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from scripts import manage_deployment
from where_paper_go import deployment_identity


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = PROJECT_ROOT / "deploy" / "monitoring" / "policy-v1.json"
DEFAULT_TOKEN = Path("~/.config/where-papers-go/backend.token")
STATE_BASE_RELATIVE = Path(".local/state/where-papers-go/monitor")
STATE_FILENAME = "operations-monitor-v1.json"

POLICY_ARTIFACT = "where_papers_go_operations_monitor_policy"
STATE_ARTIFACT = "where_papers_go_operations_monitor_state"
STATE_NAMESPACE_ARTIFACT = "where_papers_go_operations_monitor_state_namespace"
REPORT_ARTIFACT = "where_papers_go_operations_monitor_report"
SCHEMA_VERSION = 1
FIXED_UNIT = "where-papers-go.service"
FIXED_HEALTH_URL = "http://127.0.0.1:8001/api/health"
FIXED_JOURNAL_GREP = r"^\[audit\] "
SYSTEMCTL = Path("/usr/bin/systemctl")
JOURNALCTL = Path("/usr/bin/journalctl")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
UPTIME_PATH = Path("/proc/uptime")

HISTOGRAM_UPPER_BOUNDS_MS = (
    1_000,
    3_000,
    5_000,
    10_000,
    30_000,
    60_000,
    120_000,
    300_000,
    900_000,
)
LIGHTRAG_FILE_COUNT = 6
MAX_POLICY_BYTES = 64 * 1024
MAX_STATE_BYTES = 256 * 1024
MAX_TIMESTAMP_CHARS = 32
MAX_CURSOR_CHARS = 2_048
MAX_STATE_REVISION = 2**63 - 1
MAX_COUNTER = 2**63 - 1
MAX_COMMAND_STDERR_BYTES = 64 * 1024
MAX_DURATION_MS = 24 * 60 * 60 * 1_000
SEARCH_HARD_TIMEOUT_MS = 900_000
FULL_PROOF_INTERVAL_SECONDS = 6 * 60 * 60

ALERT_CODES = (
    "DAEMON_RELOAD_REQUIRED",
    "HEALTH_NOT_READY",
    "HEALTH_UNAVAILABLE",
    "LIGHTRAG_SIX_FILE_PROOF_MISMATCH",
    "PYTHON_RUNTIME_PROOF_MISMATCH",
    "RUNTIME_MANIFEST_PROOF_MISMATCH",
    "SEARCH_ERROR_RATE_HIGH",
    "SEARCH_HARD_TIMEOUT",
    "SEARCH_METRICS_UNAVAILABLE",
    "SERVICE_INACTIVE",
    "SERVICE_RECENTLY_STARTED",
    "SERVICE_RESTART_DELTA",
    "SOURCE_PROOF_MISMATCH",
    "TAVILY_QUOTA_CONSUMPTION_HIGH",
    "TAVILY_QUOTA_COUNTER_REGRESSION",
    "TAVILY_QUOTA_LOW",
    "TAVILY_QUOTA_UNAVAILABLE",
    "WORKER_PROOF_MISMATCH",
)
ALERT_CODE_SET = frozenset(ALERT_CODES)
SEVERITY_RANK = {"warning": 1, "critical": 2}
EVENT_KINDS = frozenset({"first", "escalation", "repeat", "recovery"})
TAVILY_STATUSES = ("active", "cooldown", "exhausted", "invalid")

_LOWER_HEX = re.compile(r"[0-9a-f]+\Z")
_BOOT_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
_INVOCATION_ID = re.compile(r"[0-9a-f]{32}\Z")
_CURSOR = re.compile(r"[A-Za-z0-9;=_.:+@/-]{1,2048}\Z")
_REQUEST_ID = re.compile(r"[0-9a-f]{32}\Z")
_CURSOR_UNAVAILABLE_STDERR = re.compile(
    rb"Failed to seek to cursor: "
    rb"(?:Invalid argument|Cannot assign requested address|"
    rb"No such file or directory)\n?\Z"
)


class MonitorError(RuntimeError):
    """One fail-closed monitoring error with a non-sensitive fixed code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class StrictArgumentParser(argparse.ArgumentParser):
    """Keep argparse's ordinary usage failures out of success status 2."""

    def error(self, message: str) -> None:  # pragma: no cover - argparse plumbing.
        self.print_usage(sys.stderr)
        self.exit(3, f"{self.prog}: error: {message}\n")


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    """Never let a trusted loopback URL redirect the monitor elsewhere."""

    def redirect_request(self, req: Any, fp: Any, code: Any, msg: Any,
                         headers: Any, newurl: Any) -> None:
        raise urllib.error.HTTPError(
            req.full_url, int(code), "redirect rejected", headers, fp
        )


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def parse_json_bytes(payload: bytes, *, label: str) -> Any:
    """Decode strict UTF-8 JSON, rejecting duplicates and non-finite numbers."""

    try:
        text = payload.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise MonitorError(f"{label}_JSON_INVALID") from exc


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _lower_hex(value: Any, *lengths: int) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) in lengths
        and _LOWER_HEX.fullmatch(value) is not None
    )


def _integer(value: Any, *, minimum: int = 0, maximum: int = MAX_COUNTER) -> bool:
    return bool(
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= maximum
    )


def _number(value: Any, *, minimum: float, maximum: float) -> bool:
    return bool(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and minimum <= float(value) <= maximum
    )


def _exact_keys(value: Any, expected: set[str], *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise MonitorError(f"{label}_SCHEMA_INVALID")
    return value


def _file_stamp(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _absolute_canonical(path: Path, *, label: str, must_exist: bool) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise MonitorError(f"{label}_PATH_UNSAFE")
    try:
        resolved = expanded.resolve(strict=must_exist)
    except OSError as exc:
        raise MonitorError(f"{label}_PATH_UNSAFE") from exc
    if resolved != expanded:
        raise MonitorError(f"{label}_PATH_UNSAFE")
    return expanded


def _state_namespace_sha256(
    *, policy_sha256: str, binding_sha256: str
) -> str:
    """Return the sole namespace accepted for one policy/deployment pair."""

    if not _lower_hex(policy_sha256, 64) or not _lower_hex(binding_sha256, 64):
        raise MonitorError("STATE_NAMESPACE_BINDING_INVALID")
    return _sha256(
        _canonical_json(
            {
                "artifact_type": STATE_NAMESPACE_ARTIFACT,
                "schema_version": 1,
                "policy_sha256": policy_sha256,
                "deployment_binding_sha256": binding_sha256,
            }
        )
    )


def _passwd_home() -> Path:
    """Resolve the effective user's canonical, non-writable-by-others home."""

    try:
        raw_home = pwd.getpwuid(os.geteuid()).pw_dir
    except KeyError as exc:
        raise MonitorError("STATE_HOME_UNAVAILABLE") from exc
    home = Path(raw_home)
    if not home.is_absolute() or ".." in home.parts:
        raise MonitorError("STATE_HOME_UNSAFE")
    try:
        info = home.lstat()
        resolved = home.resolve(strict=True)
    except OSError as exc:
        raise MonitorError("STATE_HOME_UNSAFE") from exc
    if (
        resolved != home
        or stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise MonitorError("STATE_HOME_UNSAFE")
    return home


def _fixed_state_path(
    path: Path,
    *,
    policy_sha256: str,
    binding_sha256: str,
    create_directories: bool,
) -> Path:
    """Validate/create only the fixed private content-addressed state path.

    No environment-controlled ``HOME`` or ``~`` expansion participates in
    this decision.  Missing directories are created one component at a time
    only after every existing ancestor has been verified.
    """

    home = _passwd_home()
    namespace = _state_namespace_sha256(
        policy_sha256=policy_sha256, binding_sha256=binding_sha256
    )
    base = home / STATE_BASE_RELATIVE
    namespace_directory = base / namespace
    expected = namespace_directory / STATE_FILENAME
    if not path.is_absolute() or ".." in path.parts or path != expected:
        raise MonitorError("STATE_PATH_UNSAFE")
    try:
        if path.resolve(strict=False) != path:
            raise MonitorError("STATE_PATH_UNSAFE")
    except OSError as exc:
        raise MonitorError("STATE_PATH_UNSAFE") from exc

    chain: list[Path] = []
    current = home
    for component in (*STATE_BASE_RELATIVE.parts, namespace):
        current /= component
        chain.append(current)
    missing_seen = False
    for directory in chain:
        exists = os.path.lexists(directory)
        if not exists:
            missing_seen = True
            if not create_directories:
                continue
            try:
                directory.mkdir(mode=0o700)
                os.chmod(directory, 0o700, follow_symlinks=False)
                _fsync_directory(directory.parent)
            except OSError as exc:
                raise MonitorError("STATE_DIRECTORY_UNSAFE") from exc
            exists = True
            missing_seen = False
        elif missing_seen:
            # A descendant cannot pre-exist after an absent real parent.
            raise MonitorError("STATE_DIRECTORY_UNSAFE")
        if not exists:
            continue
        try:
            info = directory.lstat()
            resolved = directory.resolve(strict=True)
        except OSError as exc:
            raise MonitorError("STATE_DIRECTORY_UNSAFE") from exc
        mode = stat.S_IMODE(info.st_mode)
        requires_exact_private_mode = directory in {base, namespace_directory}
        if (
            resolved != directory
            or stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or mode & 0o022
            or (requires_exact_private_mode and mode != 0o700)
        ):
            raise MonitorError("STATE_DIRECTORY_UNSAFE")
    return expected


def _read_stable_file(
    path: Path,
    *,
    label: str,
    max_bytes: int,
    exact_mode: int | None,
    allow_readonly_policy_modes: bool = False,
) -> tuple[bytes, os.stat_result]:
    """Read one canonical, owned, single-link regular file without following."""

    target = _absolute_canonical(path, label=label, must_exist=True)
    try:
        path_before = target.lstat()
        descriptor = os.open(
            target,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise MonitorError(f"{label}_FILE_UNSAFE") from exc
    try:
        before = os.fstat(descriptor)
        mode = stat.S_IMODE(before.st_mode)
        safe_mode = (
            mode == exact_mode
            if exact_mode is not None
            else bool(
                allow_readonly_policy_modes
                and mode in {0o400, 0o440, 0o444}
            )
        )
        readonly_required = bool(
            allow_readonly_policy_modes
            or (exact_mode is not None and exact_mode & 0o222 == 0)
        )
        if (
            stat.S_ISLNK(path_before.st_mode)
            or not stat.S_ISREG(path_before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or not safe_mode
            or (readonly_required and mode & 0o222 != 0)
            or before.st_size < 0
            or before.st_size > max_bytes
            or _file_stamp(path_before) != _file_stamp(before)
        ):
            raise MonitorError(f"{label}_FILE_UNSAFE")
        chunks: list[bytes] = []
        observed = 0
        while True:
            block = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - observed))
            if not block:
                break
            chunks.append(block)
            observed += len(block)
            if observed > max_bytes:
                raise MonitorError(f"{label}_FILE_TOO_LARGE")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        path_after = target.lstat()
    except OSError as exc:
        raise MonitorError(f"{label}_FILE_UNSAFE") from exc
    if (
        observed != before.st_size
        or _file_stamp(before) != _file_stamp(after)
        or _file_stamp(before) != _file_stamp(path_after)
    ):
        raise MonitorError(f"{label}_FILE_CHANGED")
    return b"".join(chunks), after


def _validate_private_directory(path: Path, *, label: str, create: bool) -> Path:
    target = _absolute_canonical(path, label=label, must_exist=False)
    if create and not os.path.lexists(target):
        try:
            target.mkdir(parents=True, mode=0o700)
            os.chmod(target, 0o700, follow_symlinks=False)
        except OSError as exc:
            raise MonitorError(f"{label}_DIRECTORY_UNSAFE") from exc
    try:
        info = target.lstat()
        resolved = target.resolve(strict=True)
    except OSError as exc:
        raise MonitorError(f"{label}_DIRECTORY_UNSAFE") from exc
    if (
        resolved != target
        or stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise MonitorError(f"{label}_DIRECTORY_UNSAFE")
    return target


def _validate_loopback_health_url(value: Any) -> str:
    if not isinstance(value, str) or value != FIXED_HEALTH_URL:
        raise MonitorError("POLICY_HEALTH_URL_INVALID")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != 8001
        or parsed.path != "/api/health"
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise MonitorError("POLICY_HEALTH_URL_INVALID")
    return value


def validate_policy(value: Any) -> dict[str, Any]:
    """Validate the exact policy-v1 schema and all operational bounds."""

    root = _exact_keys(
        value,
        {
            "artifact_type",
            "schema_version",
            "service",
            "journal",
            "thresholds",
            "deduplication",
            "histogram_upper_bounds_ms",
            "limits",
        },
        label="POLICY",
    )
    if root["artifact_type"] != POLICY_ARTIFACT or root["schema_version"] != 1:
        raise MonitorError("POLICY_SCHEMA_INVALID")
    service = _exact_keys(
        root["service"],
        {"unit", "health_url", "health_timeout_seconds"},
        label="POLICY_SERVICE",
    )
    if service["unit"] != FIXED_UNIT:
        raise MonitorError("POLICY_UNIT_INVALID")
    _validate_loopback_health_url(service["health_url"])
    if not _integer(service["health_timeout_seconds"], minimum=1, maximum=300):
        raise MonitorError("POLICY_SERVICE_SCHEMA_INVALID")
    journal = _exact_keys(
        root["journal"],
        {"enabled", "initial_lookback_seconds", "max_entries", "timeout_seconds"},
        label="POLICY_JOURNAL",
    )
    if (
        not isinstance(journal["enabled"], bool)
        or not _integer(journal["initial_lookback_seconds"], minimum=60, maximum=604800)
        or not _integer(journal["max_entries"], minimum=1, maximum=20_000)
        or not _integer(journal["timeout_seconds"], minimum=1, maximum=300)
    ):
        raise MonitorError("POLICY_JOURNAL_SCHEMA_INVALID")
    thresholds = _exact_keys(
        root["thresholds"],
        {
            "minimum_uptime_seconds",
            "restart_warning_delta",
            "restart_critical_delta",
            "search_minimum_samples",
            "search_error_warning_count",
            "search_error_critical_count",
            "search_error_warning_rate",
            "search_error_critical_rate",
            "search_latency_warning_ms",
            "search_latency_critical_ms",
            "tavily_remaining_warning_ratio",
            "tavily_remaining_critical_ratio",
            "tavily_consumed_warning_delta",
            "tavily_consumed_critical_delta",
        },
        label="POLICY_THRESHOLDS",
    )
    integer_thresholds = {
        "minimum_uptime_seconds": (0, 86400),
        "restart_warning_delta": (1, 10_000),
        "restart_critical_delta": (1, 10_000),
        "search_minimum_samples": (1, 1_000_000),
        "search_error_warning_count": (1, 1_000_000),
        "search_error_critical_count": (1, 1_000_000),
        "tavily_consumed_warning_delta": (1, MAX_COUNTER),
        "tavily_consumed_critical_delta": (1, MAX_COUNTER),
    }
    if any(
        not _integer(thresholds[name], minimum=limits[0], maximum=limits[1])
        for name, limits in integer_thresholds.items()
    ) or thresholds["search_latency_warning_ms"] is not None or thresholds[
        "search_latency_critical_ms"
    ] is not None or any(
        not _number(thresholds[name], minimum=0.0, maximum=1.0)
        for name in (
            "search_error_warning_rate",
            "search_error_critical_rate",
            "tavily_remaining_warning_ratio",
            "tavily_remaining_critical_ratio",
        )
    ):
        raise MonitorError("POLICY_THRESHOLDS_SCHEMA_INVALID")
    if not (
        thresholds["restart_warning_delta"] <= thresholds["restart_critical_delta"]
        and thresholds["search_error_warning_count"]
        <= thresholds["search_error_critical_count"]
        and float(thresholds["search_error_warning_rate"])
        <= float(thresholds["search_error_critical_rate"])
        and float(thresholds["tavily_remaining_critical_ratio"])
        <= float(thresholds["tavily_remaining_warning_ratio"])
        and thresholds["tavily_consumed_warning_delta"]
        <= thresholds["tavily_consumed_critical_delta"]
    ):
        raise MonitorError("POLICY_THRESHOLDS_ORDER_INVALID")
    dedup = _exact_keys(
        root["deduplication"],
        {"repeat_seconds", "recovery_observations"},
        label="POLICY_DEDUPLICATION",
    )
    if dedup["repeat_seconds"] != 21600 or dedup["recovery_observations"] != 2:
        raise MonitorError("POLICY_DEDUPLICATION_INVALID")
    histogram = root["histogram_upper_bounds_ms"]
    if not isinstance(histogram, list) or tuple(histogram) != HISTOGRAM_UPPER_BOUNDS_MS:
        raise MonitorError("POLICY_HISTOGRAM_INVALID")
    limits = _exact_keys(
        root["limits"],
        {
            "policy_bytes",
            "state_bytes",
            "health_response_bytes",
            "journal_output_bytes",
            "journal_message_bytes",
            "systemctl_output_bytes",
            "pending_events",
        },
        label="POLICY_LIMITS",
    )
    bounds = {
        "policy_bytes": (1024, MAX_POLICY_BYTES),
        "state_bytes": (4096, MAX_STATE_BYTES),
        "health_response_bytes": (1024, 16 * 1024 * 1024),
        "journal_output_bytes": (1024, 64 * 1024 * 1024),
        "journal_message_bytes": (256, 1024 * 1024),
        "systemctl_output_bytes": (1024, 1024 * 1024),
        "pending_events": (len(ALERT_CODES), 128),
    }
    if any(
        not _integer(limits[name], minimum=bound[0], maximum=bound[1])
        for name, bound in bounds.items()
    ):
        raise MonitorError("POLICY_LIMITS_SCHEMA_INVALID")
    return json.loads(_canonical_json(root).decode("ascii"))


def load_policy(path: Path, expected_sha256: str) -> tuple[dict[str, Any], str]:
    if not _lower_hex(expected_sha256, 64):
        raise MonitorError("EXPECTED_POLICY_SHA256_INVALID")
    payload, _info = _read_stable_file(
        path,
        label="POLICY",
        max_bytes=MAX_POLICY_BYTES,
        exact_mode=None,
        allow_readonly_policy_modes=True,
    )
    observed = _sha256(payload)
    if observed != expected_sha256:
        raise MonitorError("POLICY_SHA256_MISMATCH")
    policy = validate_policy(parse_json_bytes(payload, label="POLICY"))
    if len(payload) > int(policy["limits"]["policy_bytes"]):
        raise MonitorError("POLICY_FILE_TOO_LARGE")
    return policy, observed


def _parse_timestamp(value: Any, *, nullable: bool) -> datetime | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not 20 <= len(value) <= MAX_TIMESTAMP_CHARS:
        raise MonitorError("STATE_TIMESTAMP_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MonitorError("STATE_TIMESTAMP_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise MonitorError("STATE_TIMESTAMP_INVALID")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")


def _empty_alert_state() -> dict[str, Any]:
    return {
        "active": False,
        "severity": None,
        "first_seen_at": None,
        "last_observed_at": None,
        "last_emitted_at": None,
        "recovery_streak": 0,
    }


def initial_state(*, policy_sha256: str, binding_sha256: str) -> dict[str, Any]:
    return {
        "artifact_type": STATE_ARTIFACT,
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "last_applied_at": None,
        "last_full_proof_at": None,
        "policy_sha256": policy_sha256,
        "binding_sha256": binding_sha256,
        "baseline": {
            "boot_id": None,
            "invocation_id": None,
            "nrestarts": None,
            "quota_revision": None,
            "quota_used": None,
        },
        "journal_cursor": None,
        "alert_states": {code: _empty_alert_state() for code in ALERT_CODES},
        "pending_events": {},
    }


def _validate_cursor(value: Any, *, nullable: bool = True) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or _CURSOR.fullmatch(value) is None:
        raise MonitorError("STATE_JOURNAL_CURSOR_INVALID")
    return value


def validate_state(value: Any, *, pending_limit: int) -> dict[str, Any]:
    """Validate the complete state schema before any field influences a run."""

    root = _exact_keys(
        value,
        {
            "artifact_type",
            "schema_version",
            "revision",
            "last_applied_at",
            "last_full_proof_at",
            "policy_sha256",
            "binding_sha256",
            "baseline",
            "journal_cursor",
            "alert_states",
            "pending_events",
        },
        label="STATE",
    )
    if (
        root["artifact_type"] != STATE_ARTIFACT
        or root["schema_version"] != SCHEMA_VERSION
        or not _integer(root["revision"], maximum=MAX_STATE_REVISION)
        or not _lower_hex(root["policy_sha256"], 64)
        or not _lower_hex(root["binding_sha256"], 64)
    ):
        raise MonitorError("STATE_SCHEMA_INVALID")
    last_applied = _parse_timestamp(root["last_applied_at"], nullable=True)
    last_full_proof = _parse_timestamp(root["last_full_proof_at"], nullable=True)
    if last_full_proof is not None and (
        last_applied is None or last_full_proof > last_applied
    ):
        raise MonitorError("STATE_FULL_PROOF_TIMESTAMP_INVALID")
    baseline = _exact_keys(
        root["baseline"],
        {"boot_id", "invocation_id", "nrestarts", "quota_revision", "quota_used"},
        label="STATE_BASELINE",
    )
    if baseline["boot_id"] is not None and (
        not isinstance(baseline["boot_id"], str)
        or _BOOT_ID.fullmatch(baseline["boot_id"]) is None
    ):
        raise MonitorError("STATE_BASELINE_SCHEMA_INVALID")
    if baseline["invocation_id"] is not None and (
        not isinstance(baseline["invocation_id"], str)
        or _INVOCATION_ID.fullmatch(baseline["invocation_id"]) is None
    ):
        raise MonitorError("STATE_BASELINE_SCHEMA_INVALID")
    for name in ("nrestarts", "quota_revision", "quota_used"):
        if baseline[name] is not None and not _integer(baseline[name]):
            raise MonitorError("STATE_BASELINE_SCHEMA_INVALID")
    _validate_cursor(root["journal_cursor"])
    states = root["alert_states"]
    if not isinstance(states, Mapping) or set(states) != ALERT_CODE_SET:
        raise MonitorError("STATE_ALERTS_SCHEMA_INVALID")
    for code in ALERT_CODES:
        alert = _exact_keys(
            states[code],
            {
                "active",
                "severity",
                "first_seen_at",
                "last_observed_at",
                "last_emitted_at",
                "recovery_streak",
            },
            label="STATE_ALERT",
        )
        if not isinstance(alert["active"], bool):
            raise MonitorError("STATE_ALERTS_SCHEMA_INVALID")
        if alert["severity"] is not None and alert["severity"] not in SEVERITY_RANK:
            raise MonitorError("STATE_ALERTS_SCHEMA_INVALID")
        for name in ("first_seen_at", "last_observed_at", "last_emitted_at"):
            _parse_timestamp(alert[name], nullable=True)
        if not _integer(alert["recovery_streak"], maximum=2):
            raise MonitorError("STATE_ALERTS_SCHEMA_INVALID")
        if alert["active"] and (
            alert["severity"] is None
            or alert["first_seen_at"] is None
            or alert["last_observed_at"] is None
            or alert["last_emitted_at"] is None
        ):
            raise MonitorError("STATE_ALERTS_SCHEMA_INVALID")
        if not alert["active"] and (
            alert["severity"] is not None or alert["recovery_streak"] != 0
        ):
            raise MonitorError("STATE_ALERTS_SCHEMA_INVALID")
    pending = root["pending_events"]
    if (
        not isinstance(pending, Mapping)
        or len(pending) > pending_limit
        or not set(pending).issubset(ALERT_CODE_SET)
    ):
        raise MonitorError("STATE_PENDING_EVENTS_SCHEMA_INVALID")
    for code, raw_event in pending.items():
        event = _exact_keys(
            raw_event,
            {"event_id", "code", "kind", "severity", "at"},
            label="STATE_PENDING_EVENT",
        )
        if (
            event["code"] != code
            or event["kind"] not in EVENT_KINDS
            or event["severity"] not in SEVERITY_RANK
            or not _lower_hex(event["event_id"], 64)
        ):
            raise MonitorError("STATE_PENDING_EVENTS_SCHEMA_INVALID")
        _parse_timestamp(event["at"], nullable=False)
    return json.loads(_canonical_json(root).decode("ascii"))


def _load_state(
    path: Path,
    *,
    policy: Mapping[str, Any],
    policy_sha256: str,
    binding_sha256: str,
) -> dict[str, Any]:
    target = _fixed_state_path(
        path,
        policy_sha256=policy_sha256,
        binding_sha256=binding_sha256,
        create_directories=False,
    )
    if not os.path.lexists(target):
        return initial_state(
            policy_sha256=policy_sha256, binding_sha256=binding_sha256
        )
    _validate_private_directory(target.parent, label="STATE", create=False)
    payload, _info = _read_stable_file(
        target,
        label="STATE",
        max_bytes=min(MAX_STATE_BYTES, int(policy["limits"]["state_bytes"])),
        exact_mode=0o600,
    )
    state = validate_state(
        parse_json_bytes(payload, label="STATE"),
        pending_limit=int(policy["limits"]["pending_events"]),
    )
    # A monitor state is evidence for exactly one policy and immutable
    # deployment identity.  Never carry restart/quota baselines, alert
    # histories, or journal cursors across either boundary.  The renderer uses
    # a content-addressed state namespace so an intentional successor starts a
    # new file while preserving its predecessor's state.
    if state["policy_sha256"] != policy_sha256:
        raise MonitorError("STATE_POLICY_SHA256_MISMATCH")
    if state["binding_sha256"] != binding_sha256:
        raise MonitorError("STATE_BINDING_SHA256_MISMATCH")
    return state


@contextmanager
def _state_lock(
    state_path: Path, *, policy_sha256: str, binding_sha256: str
) -> Iterator[None]:
    if fcntl is None:
        raise MonitorError("STATE_LOCK_UNAVAILABLE")
    target = _fixed_state_path(
        state_path,
        policy_sha256=policy_sha256,
        binding_sha256=binding_sha256,
        create_directories=True,
    )
    parent = _validate_private_directory(target.parent, label="STATE", create=False)
    lock_path = parent / (target.name + ".lock")
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        try:
            descriptor = os.open(lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            _fsync_directory(parent)
        except FileExistsError:
            descriptor = os.open(lock_path, flags)
        info = os.fstat(descriptor)
        path_info = lock_path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or _file_stamp(info) != _file_stamp(path_info)
        ):
            raise MonitorError("STATE_LOCK_UNSAFE")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except OSError as exc:
        raise MonitorError("STATE_LOCK_UNSAFE") from exc
    finally:
        if "descriptor" in locals():
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_state_atomic(
    path: Path,
    state: Mapping[str, Any],
    *,
    policy_sha256: str,
    binding_sha256: str,
    max_bytes: int,
) -> None:
    target = _fixed_state_path(
        path,
        policy_sha256=policy_sha256,
        binding_sha256=binding_sha256,
        create_directories=False,
    )
    parent = _validate_private_directory(target.parent, label="STATE", create=False)
    payload = _canonical_json(state) + b"\n"
    if len(payload) > max_bytes:
        raise MonitorError("STATE_FILE_TOO_LARGE")
    previous_stamp: tuple[int, ...] | None = None
    if os.path.lexists(target):
        _old, old_info = _read_stable_file(
            target,
            label="STATE",
            max_bytes=max_bytes,
            exact_mode=0o600,
        )
        previous_stamp = _file_stamp(old_info)
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, raw_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=parent)
        temporary = Path(raw_name)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise MonitorError("STATE_TEMPORARY_UNSAFE")
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if previous_stamp is not None:
            try:
                current = target.lstat()
            except OSError as exc:
                raise MonitorError("STATE_FILE_CHANGED") from exc
            if _file_stamp(current) != previous_stamp:
                raise MonitorError("STATE_FILE_CHANGED")
        elif os.path.lexists(target):
            raise MonitorError("STATE_FILE_CHANGED")
        os.replace(temporary, target)
        temporary = None
        final = target.lstat()
        if (
            not stat.S_ISREG(final.st_mode)
            or final.st_uid != os.geteuid()
            or final.st_nlink != 1
            or stat.S_IMODE(final.st_mode) != 0o600
        ):
            raise MonitorError("STATE_FILE_UNSAFE")
        _fsync_directory(parent)
    except OSError as exc:
        raise MonitorError("STATE_WRITE_FAILED") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _command_environment() -> dict[str, str]:
    uid = os.geteuid()
    try:
        home = pwd.getpwuid(uid).pw_dir
    except KeyError as exc:
        raise MonitorError("USER_IDENTITY_UNAVAILABLE") from exc
    runtime = f"/run/user/{uid}"
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "HOME": home,
        "XDG_RUNTIME_DIR": runtime,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
    }


def _run_command(
    argv: Sequence[str], *, timeout: int, max_stdout: int
) -> subprocess.CompletedProcess[bytes]:
    """Run fixed local argv while enforcing byte and time bounds as data arrives."""

    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_command_environment(),
        )
        assert process.stdout is not None and process.stderr is not None
        output = bytearray()
        errors = bytearray()
        limits = {
            process.stdout.fileno(): (output, max_stdout),
            process.stderr.fileno(): (errors, MAX_COMMAND_STDERR_BYTES),
        }
        selector = selectors.DefaultSelector()
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MonitorError("LOCAL_COMMAND_TIMEOUT")
                events = selector.select(min(remaining, 1.0))
                if not events and process.poll() is not None:
                    # Drain the final pipe bytes on the next selector pass.
                    events = selector.select(0)
                for key, _mask in events:
                    descriptor = key.fileobj.fileno()
                    target, limit = limits[descriptor]
                    try:
                        block = os.read(descriptor, min(64 * 1024, limit + 1 - len(target)))
                    except BlockingIOError:
                        continue
                    if not block:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    target.extend(block)
                    if len(target) > limit:
                        raise MonitorError("LOCAL_COMMAND_OUTPUT_TOO_LARGE")
        finally:
            selector.close()
        remaining = max(0.0, deadline - time.monotonic())
        returncode = process.wait(timeout=remaining)
        return subprocess.CompletedProcess(
            args=list(argv),
            returncode=returncode,
            stdout=bytes(output),
            stderr=bytes(errors),
        )
    except MonitorError:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        raise MonitorError("LOCAL_COMMAND_FAILED") from exc


SYSTEMD_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "SubState",
    "Result",
    "MainPID",
    "InvocationID",
    "NRestarts",
    "ActiveEnterTimestampMonotonic",
    "NeedDaemonReload",
)


def parse_systemctl_show(payload: bytes) -> dict[str, Any]:
    """Parse exactly the bounded properties requested from ``systemctl show``."""

    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise MonitorError("SYSTEMD_OUTPUT_INVALID") from exc
    rows: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in SYSTEMD_PROPERTIES or key in rows:
            raise MonitorError("SYSTEMD_OUTPUT_INVALID")
        rows[key] = value
    if set(rows) != set(SYSTEMD_PROPERTIES):
        raise MonitorError("SYSTEMD_OUTPUT_INVALID")
    if rows["LoadState"] != "loaded":
        raise MonitorError("SYSTEMD_UNIT_NOT_LOADED")
    try:
        main_pid = int(rows["MainPID"])
        nrestarts = int(rows["NRestarts"])
        active_enter_us = int(rows["ActiveEnterTimestampMonotonic"])
    except ValueError as exc:
        raise MonitorError("SYSTEMD_OUTPUT_INVALID") from exc
    if not all(_integer(value) for value in (main_pid, nrestarts, active_enter_us)):
        raise MonitorError("SYSTEMD_OUTPUT_INVALID")
    invocation = rows["InvocationID"]
    if invocation and _INVOCATION_ID.fullmatch(invocation) is None:
        raise MonitorError("SYSTEMD_OUTPUT_INVALID")
    if rows["NeedDaemonReload"] not in {"yes", "no"}:
        raise MonitorError("SYSTEMD_OUTPUT_INVALID")
    for name in ("ActiveState", "SubState", "Result"):
        if not rows[name] or len(rows[name]) > 64 or not rows[name].isascii():
            raise MonitorError("SYSTEMD_OUTPUT_INVALID")
    return {
        "load_state": rows["LoadState"],
        "active_state": rows["ActiveState"],
        "sub_state": rows["SubState"],
        "result": rows["Result"],
        "main_pid": main_pid,
        "invocation_id": invocation or None,
        "nrestarts": nrestarts,
        "active_enter_monotonic_us": active_enter_us,
        "need_daemon_reload": rows["NeedDaemonReload"] == "yes",
    }


def _read_boot_id() -> str:
    try:
        payload = BOOT_ID_PATH.read_bytes()
    except OSError as exc:
        raise MonitorError("BOOT_ID_UNAVAILABLE") from exc
    if len(payload) > 128:
        raise MonitorError("BOOT_ID_INVALID")
    try:
        value = payload.decode("ascii", errors="strict").strip()
    except UnicodeError as exc:
        raise MonitorError("BOOT_ID_INVALID") from exc
    if _BOOT_ID.fullmatch(value) is None:
        raise MonitorError("BOOT_ID_INVALID")
    return value


def _read_boot_uptime_seconds() -> float:
    try:
        payload = UPTIME_PATH.read_bytes()
        if len(payload) > 256:
            raise ValueError("oversized")
        value = float(payload.decode("ascii", errors="strict").split()[0])
    except (OSError, UnicodeError, ValueError, IndexError) as exc:
        raise MonitorError("BOOT_UPTIME_INVALID") from exc
    if not math.isfinite(value) or not 0 <= value <= 10**10:
        raise MonitorError("BOOT_UPTIME_INVALID")
    return value


def collect_systemd(policy: Mapping[str, Any]) -> dict[str, Any]:
    limit = int(policy["limits"]["systemctl_output_bytes"])
    properties = ",".join(SYSTEMD_PROPERTIES)
    completed = _run_command(
        [
            str(SYSTEMCTL),
            "--user",
            "show",
            FIXED_UNIT,
            "--no-pager",
            f"--property={properties}",
        ],
        timeout=30,
        max_stdout=limit,
    )
    if completed.returncode != 0:
        raise MonitorError("SYSTEMD_COLLECTION_FAILED")
    result = parse_systemctl_show(completed.stdout)
    boot_uptime = _read_boot_uptime_seconds()
    active_enter = result["active_enter_monotonic_us"] / 1_000_000
    result["uptime_seconds"] = round(
        max(0.0, boot_uptime - active_enter) if active_enter > 0 else 0.0, 3
    )
    result["boot_uptime_seconds"] = round(boot_uptime, 3)
    return result


def _read_token(path: Path) -> str:
    target = _absolute_canonical(path, label="TOKEN", must_exist=True)
    _validate_private_directory(target.parent, label="TOKEN", create=False)
    try:
        return manage_deployment._read_private_bearer_token(
            target, label="monitor health bearer token file"
        )
    except (OSError, ValueError) as exc:
        raise MonitorError("TOKEN_FILE_UNSAFE") from exc


def _http_json(
    *, url: str, token: str, timeout: int, max_bytes: int
) -> tuple[int, Any]:
    _validate_loopback_health_url(url)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer " + token,
            "Connection": "close",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPHandler(), _RejectRedirect()
    )
    with opener.open(request, timeout=timeout) as response:
        status_code = int(response.status)
        if response.geturl() != url:
            raise MonitorError("HEALTH_REDIRECT_REJECTED")
        raw_length = response.headers.get("Content-Length")
        if raw_length is not None:
            try:
                declared = int(raw_length)
            except ValueError as exc:
                raise MonitorError("HEALTH_RESPONSE_INVALID") from exc
            if declared < 0 or declared > max_bytes:
                raise MonitorError("HEALTH_RESPONSE_TOO_LARGE")
        payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise MonitorError("HEALTH_RESPONSE_TOO_LARGE")
    return status_code, parse_json_bytes(payload, label="HEALTH")


def collect_health(
    policy: Mapping[str, Any], *, token: str, expected_process_pid: int | None
) -> dict[str, Any]:
    """Collect detailed health; outages become alertable data, never provider calls."""

    try:
        status_code, payload = _http_json(
            url=str(policy["service"]["health_url"]),
            token=token,
            timeout=int(policy["service"]["health_timeout_seconds"]),
            max_bytes=int(policy["limits"]["health_response_bytes"]),
        )
    except urllib.error.HTTPError as exc:
        return {
            "available": False,
            "http_status": int(exc.code),
            "error_code": "HEALTH_HTTP_ERROR",
            "payload": None,
            "validation_failures": [],
        }
    except (urllib.error.URLError, TimeoutError, OSError):
        return {
            "available": False,
            "http_status": None,
            "error_code": "HEALTH_UNREACHABLE",
            "payload": None,
            "validation_failures": [],
        }
    if status_code != 200 or not isinstance(payload, dict):
        return {
            "available": False,
            "http_status": status_code,
            "error_code": "HEALTH_RESPONSE_INVALID",
            "payload": None,
            "validation_failures": [],
        }
    try:
        failures = manage_deployment.validate_health_payload(
            payload, expected_process_pid=expected_process_pid
        )
    except (OSError, ValueError, KeyError, TypeError):
        failures = ["health validator failed closed"]
    failures = [str(item)[:256] for item in failures[:32]]
    return {
        "available": True,
        "http_status": status_code,
        "error_code": None,
        "payload": payload,
        "validation_failures": failures,
    }


def _manifest_relative_path(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise MonitorError(f"{label}_PATH_INVALID")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise MonitorError(f"{label}_PATH_INVALID")
    normalized = candidate.as_posix()
    if normalized != value:
        raise MonitorError(f"{label}_PATH_INVALID")
    return normalized


def _immutable_root_metadata(path: Path, *, label: str, exact_mode: int) -> None:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise MonitorError(f"{label}_ROOT_UNSAFE") from exc
    if (
        resolved != path
        or stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != exact_mode
    ):
        raise MonitorError(f"{label}_ROOT_UNSAFE")


def _manifest_file_inventory(
    raw_rows: Any, *, label: str
) -> dict[str, dict[str, Any]]:
    """Normalize the byte bindings needed for bounded loaded-module checks."""

    if not isinstance(raw_rows, list):
        raise MonitorError(f"{label}_MANIFEST_SCHEMA_INVALID")
    rows: dict[str, dict[str, Any]] = {}
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping):
            raise MonitorError(f"{label}_MANIFEST_SCHEMA_INVALID")
        relative = _manifest_relative_path(raw_row.get("path"), label=label)
        size = raw_row.get("bytes")
        digest = raw_row.get("sha256")
        mode = raw_row.get("mode")
        if (
            relative in rows
            or not _integer(size)
            or not _lower_hex(digest, 64)
            or mode not in {"0444", "0555"}
        ):
            raise MonitorError(f"{label}_MANIFEST_SCHEMA_INVALID")
        rows[relative] = {
            "path": relative,
            "bytes": int(size),
            "sha256": str(digest),
            "mode": str(mode),
        }
    return rows


def _python_manifest_identity(
    document: Mapping[str, Any], manifest: Path, expected: Mapping[str, str]
) -> dict[str, Any]:
    """Extract only path/hash metadata from an already byte-pinned manifest."""

    python = document.get("python")
    file_inventory = _manifest_file_inventory(
        document.get("files"), label="PYTHON_RUNTIME"
    )
    if (
        document.get("schema_version") != 1
        or document.get("artifact_type")
        != manage_deployment.PYTHON_RUNTIME_ARTIFACT_TYPE
        or document.get("content_addressed") is not True
        or document.get("self_contained") is not True
        or document.get("runtime_tree_sha256")
        != expected["python_runtime_tree_sha256"]
        or document.get("file_count") != len(file_inventory)
        or not isinstance(python, Mapping)
        or python.get("executable_sha256")
        != expected["python_executable_sha256"]
        or python.get("proc_exe_sha256")
        != expected["python_executable_sha256"]
        or python.get("proc_exe_matches_executable") is not True
        or python.get("invocation_flags") != ["-S", "-P", "-B"]
    ):
        raise MonitorError("PYTHON_RUNTIME_MANIFEST_SCHEMA_INVALID")
    runtime = manifest.parent
    if runtime.name != f"python-runtime-{expected['python_runtime_manifest_sha256']}":
        raise MonitorError("PYTHON_RUNTIME_MANIFEST_PATH_INVALID")
    executable_relative = _manifest_relative_path(
        python.get("executable"), label="PYTHON_EXECUTABLE"
    )
    executable = runtime / executable_relative
    try:
        if executable.resolve(strict=True) != executable or not executable.is_relative_to(runtime):
            raise MonitorError("PYTHON_RUNTIME_EXECUTABLE_BINDING_INVALID")
    except OSError as exc:
        raise MonitorError("PYTHON_RUNTIME_EXECUTABLE_BINDING_INVALID") from exc

    def paths(field: str) -> list[Path]:
        raw_values = python.get(field)
        if not isinstance(raw_values, list) or not raw_values:
            raise MonitorError("PYTHON_RUNTIME_MANIFEST_SCHEMA_INVALID")
        result = [
            runtime / _manifest_relative_path(value, label="PYTHON_IMPORT")
            for value in raw_values
        ]
        if len(set(result)) != len(result):
            raise MonitorError("PYTHON_RUNTIME_MANIFEST_SCHEMA_INVALID")
        for candidate in result:
            try:
                resolved = candidate.resolve(strict=False)
            except OSError as exc:
                raise MonitorError("PYTHON_RUNTIME_MANIFEST_SCHEMA_INVALID") from exc
            if resolved != candidate or not candidate.is_relative_to(runtime):
                raise MonitorError("PYTHON_RUNTIME_MANIFEST_SCHEMA_INVALID")
        return result

    import_paths = paths("import_paths")
    isolated_sys_path = paths("isolated_sys_path")
    return {
        "runtime": runtime,
        "executable": executable,
        "import_paths": import_paths,
        "isolated_sys_path": isolated_sys_path,
        "file_inventory": file_inventory,
        "file_count": document.get("file_count"),
        "wheel_count": len(document.get("wheels", []))
        if isinstance(document.get("wheels"), list)
        else None,
    }


def _verify_loaded_module_files(
    *,
    source_root: Path,
    python_runtime: Path,
    source_inventory: Mapping[str, Mapping[str, Any]],
    python_inventory: Mapping[str, Mapping[str, Any]],
    modules: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """Hash every real loaded module against its pinned manifest file row."""

    selected_modules = tuple(sys.modules.values()) if modules is None else tuple(modules)
    observed: dict[tuple[str, str], dict[str, Any]] = {}
    for module in selected_modules:
        specification = getattr(module, "__spec__", None)
        origin = getattr(specification, "origin", None)
        if origin in {"built-in", "frozen"}:
            continue
        raw_file = getattr(module, "__file__", None)
        if raw_file is None:
            continue
        if not isinstance(raw_file, str):
            raise MonitorError("MONITOR_IMPORT_ROOT_INVALID")
        if raw_file in {"<built-in>", "<frozen>"} or raw_file.startswith(
            "<frozen "
        ):
            continue
        if raw_file.startswith("<"):
            raise MonitorError("MONITOR_IMPORT_ROOT_INVALID")
        candidate = Path(raw_file)
        if not candidate.is_absolute() or ".." in candidate.parts:
            raise MonitorError("MONITOR_IMPORT_ROOT_INVALID")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise MonitorError("MONITOR_IMPORT_ROOT_INVALID") from exc
        if resolved != candidate:
            raise MonitorError("MONITOR_IMPORT_ROOT_INVALID")
        if candidate.is_relative_to(source_root):
            root_name = "source"
            root = source_root
            inventory = source_inventory
        elif candidate.is_relative_to(python_runtime):
            root_name = "python_runtime"
            root = python_runtime
            inventory = python_inventory
        else:
            raise MonitorError("MONITOR_IMPORT_ROOT_INVALID")
        relative = candidate.relative_to(root).as_posix()
        row = inventory.get(relative)
        if not isinstance(row, Mapping):
            raise MonitorError("LOADED_MODULE_MANIFEST_MISSING")
        size = row.get("bytes")
        digest = row.get("sha256")
        mode = row.get("mode")
        if (
            not _integer(size)
            or not _lower_hex(digest, 64)
            or mode not in {"0444", "0555"}
        ):
            raise MonitorError("LOADED_MODULE_MANIFEST_INVALID")
        payload, _info = _read_stable_file(
            candidate,
            label="LOADED_MODULE",
            max_bytes=int(size),
            exact_mode=int(str(mode), 8),
        )
        observed_digest = _sha256(payload)
        if len(payload) != int(size) or observed_digest != digest:
            raise MonitorError("LOADED_MODULE_FILE_MISMATCH")
        observed[(root_name, relative)] = {
            "root": root_name,
            "path": relative,
            "bytes": len(payload),
            "sha256": observed_digest,
            "mode": str(mode),
        }

    required_source_modules = {
        "scripts/manage_deployment.py",
        "scripts/monitor_operations.py",
        "where_paper_go/deployment_identity.py",
    }
    observed_source_modules = {
        relative
        for (root_name, relative) in observed
        if root_name == "source"
    }
    if not required_source_modules.issubset(observed_source_modules):
        raise MonitorError("MONITOR_CORE_MODULE_PROOF_INCOMPLETE")
    rows = [observed[key] for key in sorted(observed)]
    return {
        "loaded_module_file_count": len(rows),
        "loaded_module_binding_sha256": _sha256(_canonical_json(rows)),
        "source_module_file_count": len(observed_source_modules),
        "python_runtime_module_file_count": len(rows)
        - len(observed_source_modules),
    }


def _validate_monitor_process_identity(
    *,
    source_root: Path,
    source_inventory: Mapping[str, Mapping[str, Any]],
    python_identity: Mapping[str, Any],
    expected_sha256: str,
) -> dict[str, Any]:
    """Bind this live monitor to its selected source and Python process.

    This helper is deliberately a separate mandatory production boundary so
    ordinary unit tests can mock it explicitly without weakening
    ``run_observation``.
    """

    script = source_root / "scripts" / "monitor_operations.py"
    executable = python_identity.get("executable")
    runtime = python_identity.get("runtime")
    import_paths = python_identity.get("import_paths")
    isolated_sys_path = python_identity.get("isolated_sys_path")
    python_inventory = python_identity.get("file_inventory")
    if (
        source_root != PROJECT_ROOT
        or Path(__file__).resolve(strict=True) != script
        or not isinstance(executable, Path)
        or not isinstance(runtime, Path)
        or not isinstance(import_paths, list)
        or not isinstance(isolated_sys_path, list)
        or not isinstance(python_inventory, Mapping)
        or sys.flags.no_site != 1
        or sys.flags.safe_path is not True
        or sys.flags.dont_write_bytecode != 1
    ):
        raise MonitorError("MONITOR_PROCESS_IDENTITY_INVALID")
    try:
        resolved_executable = Path(sys.executable).resolve(strict=True)
        resolved_prefix = Path(sys.prefix).resolve(strict=True)
        resolved_base_prefix = Path(sys.base_prefix).resolve(strict=True)
    except OSError as exc:
        raise MonitorError("MONITOR_PROCESS_IDENTITY_INVALID") from exc
    if (
        resolved_executable != executable
        or resolved_prefix != runtime
        or resolved_base_prefix != runtime
    ):
        raise MonitorError("MONITOR_PROCESS_IDENTITY_INVALID")

    expected_pythonpath = [source_root, *import_paths]
    raw_pythonpath = os.environ.get("PYTHONPATH")
    if not isinstance(raw_pythonpath, str):
        raise MonitorError("MONITOR_IMPORT_PATH_INVALID")
    configured_paths = raw_pythonpath.split(os.pathsep)
    if any(not value for value in configured_paths):
        raise MonitorError("MONITOR_IMPORT_PATH_INVALID")
    try:
        configured = [Path(value).resolve(strict=True) for value in configured_paths]
    except OSError as exc:
        raise MonitorError("MONITOR_IMPORT_PATH_INVALID") from exc
    if configured != expected_pythonpath:
        raise MonitorError("MONITOR_IMPORT_PATH_INVALID")

    allowed_paths = {source_root, *import_paths, *isolated_sys_path}
    observed_paths: list[Path] = []
    for raw_path in sys.path:
        if not isinstance(raw_path, str) or not raw_path:
            raise MonitorError("MONITOR_IMPORT_PATH_INVALID")
        try:
            candidate = Path(raw_path).resolve(strict=False)
        except OSError as exc:
            raise MonitorError("MONITOR_IMPORT_PATH_INVALID") from exc
        if candidate not in allowed_paths or candidate in observed_paths:
            raise MonitorError("MONITOR_IMPORT_PATH_INVALID")
        observed_paths.append(candidate)
    if source_root not in observed_paths or any(
        path not in observed_paths for path in import_paths
    ):
        raise MonitorError("MONITOR_IMPORT_PATH_INVALID")

    loaded_module_proof = _verify_loaded_module_files(
        source_root=source_root,
        python_runtime=runtime,
        source_inventory=source_inventory,
        python_inventory=python_inventory,
    )

    try:
        process = deployment_identity.process_executable_stamp(
            os.getpid(),
            expected_executable=executable,
            expected_sha256=expected_sha256,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise MonitorError("MONITOR_PROCESS_IDENTITY_INVALID") from exc
    if process.pid != os.getpid() or process.executable_sha256 != expected_sha256:
        raise MonitorError("MONITOR_PROCESS_IDENTITY_INVALID")
    return {
        "verified": True,
        "pid": process.pid,
        "start_ticks": process.start_ticks,
        "python_executable_sha256": process.executable_sha256,
        "no_site": True,
        "safe_path": True,
        "dont_write_bytecode": True,
        "source_root_bound": True,
        "import_roots_bound": True,
        **loaded_module_proof,
    }


def collect_local_proofs(
    args: argparse.Namespace, expected: Mapping[str, str], *, full: bool = True
) -> dict[str, Any]:
    """Verify pinned manifests every run and traverse their trees every six hours."""

    source_manifest = _absolute_canonical(
        args.source_manifest, label="SOURCE_MANIFEST", must_exist=True
    )
    python_manifest = _absolute_canonical(
        args.python_runtime_manifest,
        label="PYTHON_RUNTIME_MANIFEST",
        must_exist=True,
    )
    runtime_manifest = _absolute_canonical(
        args.runtime_manifest, label="RUNTIME_MANIFEST", must_exist=True
    )
    if (
        source_manifest.name != deployment_identity.SOURCE_MANIFEST_FILE
        or source_manifest.parent != PROJECT_ROOT
    ):
        raise MonitorError("SOURCE_MANIFEST_PATH_INVALID")
    if python_manifest.name != manage_deployment.PYTHON_RUNTIME_MANIFEST:
        raise MonitorError("PYTHON_RUNTIME_MANIFEST_PATH_INVALID")
    if runtime_manifest.name != manage_deployment.RUNTIME_MANIFEST:
        raise MonitorError("RUNTIME_MANIFEST_PATH_INVALID")
    _immutable_root_metadata(source_manifest.parent, label="SOURCE", exact_mode=0o555)
    _immutable_root_metadata(
        python_manifest.parent, label="PYTHON_RUNTIME", exact_mode=0o555
    )
    _immutable_root_metadata(runtime_manifest.parent, label="RUNTIME", exact_mode=0o700)

    source_raw, _source_info = _read_stable_file(
        source_manifest,
        label="SOURCE_MANIFEST",
        max_bytes=16 * 1024 * 1024,
        exact_mode=0o400,
    )
    python_raw, _python_info = _read_stable_file(
        python_manifest,
        label="PYTHON_RUNTIME_MANIFEST",
        max_bytes=manage_deployment.MAX_PYTHON_RUNTIME_MANIFEST_BYTES,
        exact_mode=0o400,
    )
    runtime_raw, _runtime_info = _read_stable_file(
        runtime_manifest,
        label="RUNTIME_MANIFEST",
        max_bytes=16 * 1024 * 1024,
        exact_mode=0o400,
    )
    if _sha256(source_raw) != expected["source_manifest_sha256"]:
        raise MonitorError("SOURCE_MANIFEST_SHA256_MISMATCH")
    if _sha256(python_raw) != expected["python_runtime_manifest_sha256"]:
        raise MonitorError("PYTHON_RUNTIME_MANIFEST_SHA256_MISMATCH")
    if _sha256(runtime_raw) != expected["runtime_manifest_sha256"]:
        raise MonitorError("RUNTIME_MANIFEST_SHA256_MISMATCH")

    source_document = parse_json_bytes(source_raw, label="SOURCE_MANIFEST")
    python_document = parse_json_bytes(python_raw, label="PYTHON_RUNTIME_MANIFEST")
    runtime_document = parse_json_bytes(runtime_raw, label="RUNTIME_MANIFEST")
    if not all(
        isinstance(value, Mapping)
        for value in (source_document, python_document, runtime_document)
    ):
        raise MonitorError("LOCAL_MANIFEST_SCHEMA_INVALID")
    assert isinstance(source_document, Mapping)
    assert isinstance(python_document, Mapping)
    assert isinstance(runtime_document, Mapping)
    source_rows = source_document.get("files")
    if (
        source_document.get("schema_version") != 1
        or source_document.get("artifact_type")
        != deployment_identity.SOURCE_ARTIFACT_TYPE
        or source_document.get("immutable_files") is not True
        or source_document.get("forbidden_entries_excluded") is not True
        or source_document.get("source_head") != expected["source_head"]
        or source_document.get("source_tree") != expected["source_tree"]
        or not isinstance(source_rows, list)
        or source_document.get("file_count") != len(source_rows)
        or source_manifest.parent.name
        != f"release-{expected['source_manifest_sha256']}"
    ):
        raise MonitorError("SOURCE_MANIFEST_SCHEMA_INVALID")
    source_inventory = _manifest_file_inventory(source_rows, label="SOURCE")
    if len(source_inventory) != source_document.get("file_count"):
        raise MonitorError("SOURCE_MANIFEST_SCHEMA_INVALID")
    script_row = source_inventory.get("scripts/monitor_operations.py")
    if script_row is None:
        raise MonitorError("MONITOR_SOURCE_BINDING_INVALID")
    script_raw, _script_info = _read_stable_file(
        PROJECT_ROOT / "scripts" / "monitor_operations.py",
        label="MONITOR_SOURCE",
        max_bytes=4 * 1024 * 1024,
        exact_mode=0o444,
    )
    if (
        script_row.get("mode") != "0444"
        or script_row.get("bytes") != len(script_raw)
        or script_row.get("sha256") != _sha256(script_raw)
    ):
        raise MonitorError("MONITOR_SOURCE_BINDING_INVALID")

    python_metadata = _python_manifest_identity(
        python_document, python_manifest, expected
    )
    process_identity = _validate_monitor_process_identity(
        source_root=source_manifest.parent,
        source_inventory=source_inventory,
        python_identity=python_metadata,
        expected_sha256=expected["python_executable_sha256"],
    )

    raw_runtime_rows = runtime_document.get("files")
    if (
        runtime_document.get("schema_version") != 1
        or runtime_document.get("artifact_type") != "where_papers_go_runtime_shadow"
        or runtime_document.get("protected_sources_never_replaced") is not True
        or not isinstance(raw_runtime_rows, list)
    ):
        raise MonitorError("RUNTIME_MANIFEST_SCHEMA_INVALID")
    rows: dict[str, Mapping[str, Any]] = {}
    for raw_row in raw_runtime_rows:
        if not isinstance(raw_row, Mapping):
            raise MonitorError("RUNTIME_MANIFEST_SCHEMA_INVALID")
        runtime_path = raw_row.get("runtime_path")
        if not isinstance(runtime_path, str) or runtime_path in rows:
            raise MonitorError("RUNTIME_MANIFEST_SCHEMA_INVALID")
        rows[runtime_path] = raw_row
    verified_rows: list[dict[str, Any]] = []
    for name in manage_deployment.RUNTIME_LIGHTRAG_FILES:
        relative = f"lightrag_storage/{name}"
        row = rows.get(relative)
        if (
            not isinstance(row, Mapping)
            or not _integer(row.get("bytes"))
            or not _lower_hex(row.get("sha256"), 64)
        ):
            raise MonitorError("RUNTIME_SIX_FILE_BINDING_INVALID")
        verified_rows.append(
            {
                "runtime_path": relative,
                "bytes": int(row["bytes"]),
                "sha256": str(row["sha256"]),
            }
        )
    store_binding = _sha256(_canonical_json(verified_rows))
    if store_binding != expected["store_binding_sha256"]:
        raise MonitorError("RUNTIME_SIX_FILE_BINDING_MISMATCH")

    source_identity: Mapping[str, Any] = {
        "head": source_document["source_head"],
        "tree": source_document["source_tree"],
        "manifest_sha256": expected["source_manifest_sha256"],
        "file_count": source_document["file_count"],
        "files_verified": False,
    }
    python_identity: Mapping[str, Any] = {
        "manifest_sha256": expected["python_runtime_manifest_sha256"],
        "runtime_tree_sha256": expected["python_runtime_tree_sha256"],
        "python_executable_sha256": expected["python_executable_sha256"],
        "file_count": python_metadata["file_count"],
        "wheel_count": python_metadata["wheel_count"],
        "files_verified": False,
    }
    if full:
        try:
            source_identity = manage_deployment.validate_source_release(
                source_manifest,
                expected_head=expected["source_head"],
                expected_tree=expected["source_tree"],
                expected_manifest_sha256=expected["source_manifest_sha256"],
            )
            python_identity = manage_deployment.validate_python_runtime_release(
                python_manifest,
                expected_manifest_sha256=expected["python_runtime_manifest_sha256"],
                run_probe=False,
            )
            runtime_root = manage_deployment._validated_runtime_shadow(
                runtime_manifest.parent
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise MonitorError("LOCAL_IMMUTABLE_PROOF_INVALID") from exc
        if runtime_root != runtime_manifest.parent:
            raise MonitorError("RUNTIME_MANIFEST_PATH_INVALID")
        if (
            source_identity.get("head") != expected["source_head"]
            or source_identity.get("tree") != expected["source_tree"]
            or source_identity.get("manifest_sha256")
            != expected["source_manifest_sha256"]
            or source_identity.get("files_verified") is not True
            or python_identity.get("manifest_sha256")
            != expected["python_runtime_manifest_sha256"]
            or python_identity.get("runtime_tree_sha256")
            != expected["python_runtime_tree_sha256"]
            or python_identity.get("python_executable_sha256")
            != expected["python_executable_sha256"]
            or python_identity.get("files_verified") is not True
        ):
            raise MonitorError("LOCAL_IMMUTABLE_PROOF_INVALID")
    return {
        "proof_mode": "full" if full else "manifest-only",
        "full_tree_verification_performed": bool(full),
        "source": {
            "manifest_verified": True,
            "full_tree_verified": bool(full),
            "manifest_sha256": expected["source_manifest_sha256"],
            "head": expected["source_head"],
            "tree": expected["source_tree"],
            "file_count": source_identity.get("file_count"),
        },
        "python_runtime": {
            "manifest_verified": True,
            "full_tree_verified": bool(full),
            "manifest_sha256": expected["python_runtime_manifest_sha256"],
            "runtime_tree_sha256": expected["python_runtime_tree_sha256"],
            "python_executable_sha256": expected["python_executable_sha256"],
            "file_count": python_identity.get("file_count"),
            "wheel_count": python_identity.get("wheel_count"),
        },
        "runtime_manifest": {
            "manifest_verified": True,
            "full_tree_verified": bool(full),
            "manifest_sha256": expected["runtime_manifest_sha256"],
        },
        "lightrag_six_files": {
            "manifest_binding_verified": True,
            "full_files_verified": bool(full),
            "file_count": LIGHTRAG_FILE_COUNT,
            "store_binding_sha256": store_binding,
        },
        "monitor_process": process_identity,
    }


def _proofs(
    health: Mapping[str, Any],
    expected: Mapping[str, str],
    local: Mapping[str, Any],
) -> dict[str, Any]:
    payload = health.get("payload")
    if not isinstance(payload, Mapping):
        return {
            "source": {"verified": False},
            "python_runtime": {"verified": False},
            "worker": {"verified": False},
            "runtime_manifest": {"verified": False},
            "lightrag_six_files": {"verified": False, "file_count": 0},
        }
    source = payload.get("source")
    python_runtime = payload.get("python_runtime")
    runtime = payload.get("runtime")
    source = source if isinstance(source, Mapping) else {}
    python_runtime = python_runtime if isinstance(python_runtime, Mapping) else {}
    runtime = runtime if isinstance(runtime, Mapping) else {}
    worker = runtime.get("worker_process")
    worker = worker if isinstance(worker, Mapping) else {}
    runtime_manifest = runtime.get("runtime_manifest")
    runtime_manifest = runtime_manifest if isinstance(runtime_manifest, Mapping) else {}
    store = runtime.get("lightrag_store_verification")
    store = store if isinstance(store, Mapping) else {}

    source_verified = bool(
        isinstance(local.get("source"), Mapping)
        and local["source"].get("manifest_verified") is True
        and
        source.get("ready") is True
        and source.get("files_verified") is True
        and source.get("head") == expected["source_head"]
        and source.get("tree") == expected["source_tree"]
        and source.get("manifest_sha256") == expected["source_manifest_sha256"]
    )
    python_verified = bool(
        isinstance(local.get("python_runtime"), Mapping)
        and local["python_runtime"].get("manifest_verified") is True
        and
        python_runtime.get("ready") is True
        and python_runtime.get("files_verified") is True
        and python_runtime.get("proc_exe_matches") is True
        and python_runtime.get("system_abi_stat_verified") is True
        and python_runtime.get("manifest_sha256")
        == expected["python_runtime_manifest_sha256"]
        and python_runtime.get("runtime_tree_sha256")
        == expected["python_runtime_tree_sha256"]
        and python_runtime.get("python_executable_sha256")
        == expected["python_executable_sha256"]
    )
    worker_source = worker.get("source")
    worker_python = worker.get("python_runtime")
    worker_verified = bool(
        worker.get("exact") is True
        and worker.get("proc_exe_verified") is True
        and isinstance(worker_source, Mapping)
        and worker_source.get("head") == expected["source_head"]
        and worker_source.get("tree") == expected["source_tree"]
        and worker_source.get("manifest_sha256")
        == expected["source_manifest_sha256"]
        and worker_source.get("files_verified") is True
        and isinstance(worker_python, Mapping)
        and worker_python.get("manifest_sha256")
        == expected["python_runtime_manifest_sha256"]
        and worker_python.get("runtime_tree_sha256")
        == expected["python_runtime_tree_sha256"]
        and worker_python.get("python_executable_sha256")
        == expected["python_executable_sha256"]
        and worker_python.get("files_verified") is True
        and worker_python.get("proc_exe_matches") is True
        and worker_python.get("system_abi_stat_verified") is True
    )
    runtime_manifest_verified = bool(
        isinstance(local.get("runtime_manifest"), Mapping)
        and local["runtime_manifest"].get("manifest_verified") is True
        and
        runtime_manifest.get("ready") is True
        and runtime_manifest.get("sha256_matched") is True
        and runtime_manifest.get("path_bound") is True
        and runtime_manifest.get("actual_sha256")
        == expected["runtime_manifest_sha256"]
    )
    store_verified = bool(
        isinstance(local.get("lightrag_six_files"), Mapping)
        and local["lightrag_six_files"].get("manifest_binding_verified") is True
        and runtime.get("bindings_current") is True
        and store.get("required") is True
        and store.get("verified") is True
        and store.get("file_count") == LIGHTRAG_FILE_COUNT
        and store.get("manifest_sha256") == expected["runtime_manifest_sha256"]
        and store.get("store_binding_sha256")
        == expected["store_binding_sha256"]
    )
    return {
        "source": {
            "verified": source_verified,
            "head": source.get("head") if _lower_hex(source.get("head"), 40, 64) else None,
            "tree": source.get("tree") if _lower_hex(source.get("tree"), 40, 64) else None,
            "manifest_sha256": source.get("manifest_sha256")
            if _lower_hex(source.get("manifest_sha256"), 64)
            else None,
        },
        "python_runtime": {
            "verified": python_verified,
            "manifest_sha256": python_runtime.get("manifest_sha256")
            if _lower_hex(python_runtime.get("manifest_sha256"), 64)
            else None,
            "runtime_tree_sha256": python_runtime.get("runtime_tree_sha256")
            if _lower_hex(python_runtime.get("runtime_tree_sha256"), 64)
            else None,
            "python_executable_sha256": python_runtime.get("python_executable_sha256")
            if _lower_hex(python_runtime.get("python_executable_sha256"), 64)
            else None,
        },
        "worker": {
            "verified": worker_verified,
            "pid": worker.get("pid") if _integer(worker.get("pid"), minimum=1) else None,
            "start_ticks": worker.get("start_ticks")
            if _integer(worker.get("start_ticks"), minimum=1)
            else None,
        },
        "runtime_manifest": {
            "verified": runtime_manifest_verified,
            "sha256": runtime_manifest.get("actual_sha256")
            if _lower_hex(runtime_manifest.get("actual_sha256"), 64)
            else None,
        },
        "lightrag_six_files": {
            "verified": store_verified,
            "file_count": store.get("file_count")
            if _integer(store.get("file_count"), maximum=LIGHTRAG_FILE_COUNT)
            else 0,
            "bytes": store.get("bytes") if _integer(store.get("bytes")) else None,
            "runtime_manifest_sha256": store.get("manifest_sha256")
            if _lower_hex(store.get("manifest_sha256"), 64)
            else None,
            "store_binding_sha256": store.get("store_binding_sha256")
            if _lower_hex(store.get("store_binding_sha256"), 64)
            else None,
        },
    }


def _sanitized_quota(health: Mapping[str, Any]) -> dict[str, Any]:
    payload = health.get("payload")
    if not isinstance(payload, Mapping):
        return {"available": False}
    config = payload.get("config")
    config = config if isinstance(config, Mapping) else {}
    audit = config.get("search_quota_audit")
    if not isinstance(audit, Mapping):
        return {"available": False}
    revision = audit.get("state_revision")
    used = audit.get("used")
    remaining = audit.get("remaining")
    capacity = audit.get("total_capacity")
    statuses = audit.get("status_counts")
    if not (
        audit.get("required") is True
        and audit.get("ready") is True
        and audit.get("configuration_current") is True
        and audit.get("replicated_revision") is True
        and _integer(revision)
        and _integer(used)
        and _integer(remaining)
        and _integer(capacity, minimum=1)
        and used + remaining == capacity
        and isinstance(statuses, Mapping)
        and set(statuses).issubset(set(TAVILY_STATUSES))
        and all(_integer(value) for value in statuses.values())
    ):
        return {"available": False}
    return {
        "available": True,
        "state_revision": revision,
        "used": used,
        "remaining": remaining,
        "total_capacity": capacity,
        "status_counts": {name: int(statuses.get(name, 0)) for name in TAVILY_STATUSES},
    }


def _journal_message_payload(message: str, *, max_bytes: int) -> Mapping[str, Any] | None:
    prefix = "[audit] "
    if not message.startswith(prefix):
        # Ordinary application/library messages are outside the audit schema.
        # The enclosing journal command already has a total-output bound; do
        # not let an unrelated large line fail the security signal parser.
        return None
    try:
        encoded = message.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise MonitorError("JOURNAL_AUDIT_JSON_INVALID") from exc
    if len(encoded) > max_bytes:
        raise MonitorError("JOURNAL_MESSAGE_TOO_LARGE")
    candidate = message[len(prefix):]
    try:
        value = json.loads(
            candidate,
            object_pairs_hook=_json_object,
            parse_constant=_reject_json_constant,
        )
    except (ValueError, json.JSONDecodeError) as exc:
        raise MonitorError("JOURNAL_AUDIT_JSON_INVALID") from exc
    if not isinstance(value, Mapping):
        raise MonitorError("JOURNAL_AUDIT_SCHEMA_INVALID")
    return value


def _search_sample(
    event: Mapping[str, Any], *, fallback_id: str
) -> tuple[str, int, dict[str, Any]] | None:
    kind = event.get("event")
    if kind == "http_request":
        if event.get("method") != "POST" or event.get("path") not in {
            "/api/search",
            "/api/search/stream",
        }:
            return None
        endpoint = "stream" if event.get("path") == "/api/search/stream" else "search"
        schema_version = event.get("audit_schema_version")
        outcome = event.get("recommendation_outcome")
        canonical = schema_version == 2
        if schema_version is not None and not canonical:
            raise MonitorError("JOURNAL_AUDIT_SCHEMA_INVALID")
        if canonical:
            # The request audit is the canonical terminal record.  In
            # particular, an NDJSON terminal error still has an outer HTTP
            # status of 200, and a disconnected/no-terminal stream is an
            # incomplete recommendation rather than a successful handshake.
            if outcome not in {
                "complete",
                "error",
                "incomplete",
                "not_applicable",
            }:
                raise MonitorError("JOURNAL_AUDIT_SCHEMA_INVALID")
            outer_status = event.get("status")
            outer_duration = event.get("duration_ms")
            request_id = event.get("request_id")
            if (
                not _integer(outer_status, minimum=100, maximum=599)
                or not _number(
                    outer_duration, minimum=0, maximum=MAX_DURATION_MS
                )
                or not isinstance(event.get("client_disconnected"), bool)
                or not isinstance(request_id, str)
                or _REQUEST_ID.fullmatch(request_id) is None
            ):
                raise MonitorError("JOURNAL_AUDIT_SCHEMA_INVALID")
            if outcome == "not_applicable":
                if (
                    event.get("terminal_status") is not None
                    or event.get("terminal_elapsed_ms") is not None
                ):
                    raise MonitorError("JOURNAL_AUDIT_SCHEMA_INVALID")
                return None
            priority = 2
            if outcome == "incomplete":
                if (
                    event.get("terminal_status") is not None
                    or event.get("terminal_elapsed_ms") is not None
                ):
                    raise MonitorError("JOURNAL_AUDIT_SCHEMA_INVALID")
                status = 0
                duration = outer_duration
                failed = True
            else:
                status = event.get("terminal_status")
                duration = event.get("terminal_elapsed_ms")
                if not _integer(
                    status, minimum=100, maximum=599
                ) or not _number(
                    duration, minimum=0, maximum=MAX_DURATION_MS
                ):
                    raise MonitorError("JOURNAL_AUDIT_SCHEMA_INVALID")
                failed = bool(outcome == "error" or int(status) >= 400)
        else:
            # Compatibility for a bounded predecessor window.  A successful
            # stream handshake alone is never a terminal observation.
            status = event.get("status")
            duration = event.get("duration_ms")
            priority = 1
            failed = bool(_integer(status, minimum=400, maximum=599))
            if endpoint == "stream" and _integer(
                status, minimum=200, maximum=399
            ):
                return None
    else:
        raise MonitorError("JOURNAL_AUDIT_SCHEMA_INVALID")
    if not _integer(status, minimum=0, maximum=599) or not _number(
        duration, minimum=0, maximum=MAX_DURATION_MS
    ):
        return None
    if kind == "http_request" and not canonical and int(status) < 100:
        return None
    request_id = event.get("request_id")
    identity = (
        request_id
        if isinstance(request_id, str) and _REQUEST_ID.fullmatch(request_id)
        else fallback_id
    )
    return identity, priority, {
        "duration_ms": int(round(float(duration))),
        "failed": failed,
        "status": int(status),
        "endpoint": endpoint,
    }


def parse_journal_json(
    payload: bytes, *, max_entries: int, max_message_bytes: int
) -> tuple[list[dict[str, Any]], str, int]:
    """Parse journalctl JSON lines and its terminal cursor without side effects."""

    samples: dict[str, tuple[int, dict[str, Any]]] = {}
    cursor: str | None = None
    entries = 0
    lines = payload.splitlines()
    for index, raw_line in enumerate(lines):
        if raw_line.startswith(b"-- cursor: "):
            if cursor is not None or index != len(lines) - 1:
                raise MonitorError("JOURNAL_OUTPUT_INVALID")
            try:
                cursor = raw_line[len(b"-- cursor: "):].decode("ascii", errors="strict")
            except UnicodeError as exc:
                raise MonitorError("JOURNAL_CURSOR_INVALID") from exc
            _validate_cursor(cursor, nullable=False)
            continue
        if not raw_line:
            continue
        entries += 1
        if entries > max_entries:
            raise MonitorError("JOURNAL_ENTRY_LIMIT_EXCEEDED")
        row = parse_json_bytes(raw_line, label="JOURNAL_ENTRY")
        if not isinstance(row, Mapping):
            raise MonitorError("JOURNAL_OUTPUT_INVALID")
        row_cursor = row.get("__CURSOR")
        if row_cursor is not None:
            _validate_cursor(row_cursor, nullable=False)
        message = row.get("MESSAGE")
        if message is None:
            continue
        if not isinstance(message, str):
            raise MonitorError("JOURNAL_MESSAGE_INVALID")
        event = _journal_message_payload(message, max_bytes=max_message_bytes)
        if event is None:
            continue
        fallback = _sha256((str(row_cursor or "") + f"\0{index}").encode("utf-8"))
        sample = _search_sample(event, fallback_id=fallback)
        if sample is None:
            continue
        identity, priority, normalized = sample
        previous = samples.get(identity)
        if previous is None or priority > previous[0]:
            samples[identity] = (priority, normalized)
    if cursor is None:
        raise MonitorError("JOURNAL_CURSOR_MISSING")
    return [value for _priority, value in samples.values()], cursor, entries


def aggregate_search_metrics(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build a fixed, disjoint latency histogram and its bounded p95 bucket."""

    buckets = [0 for _bound in HISTOGRAM_UPPER_BOUNDS_MS]
    overflow = 0
    error_count = 0
    hard_timeout_count = 0
    status_errors = {"4xx": 0, "5xx": 0, "other": 0}
    endpoint_counts = {"search": 0, "stream": 0}
    latency_sample_count = 0
    latency_duration_ms = 0
    successful_latency_sample_count = 0
    successful_latency_duration_ms = 0
    for sample in samples:
        duration = sample.get("duration_ms")
        status = sample.get("status")
        endpoint = sample.get("endpoint")
        failed = sample.get("failed")
        if (
            not _integer(duration, maximum=MAX_DURATION_MS)
            or not _integer(status, maximum=599)
            or endpoint not in endpoint_counts
            or not isinstance(failed, bool)
        ):
            raise MonitorError("SEARCH_SAMPLE_INVALID")
        endpoint_counts[str(endpoint)] += 1
        # Status 0 represents a canonical incomplete/disconnected request. Its
        # duration is HTTP handler lifetime, not a recommendation terminal
        # elapsed time, so it must never contaminate latency metrics.
        if int(status) >= 100:
            latency_sample_count += 1
            latency_duration_ms += int(duration)
            if failed is False:
                successful_latency_sample_count += 1
                successful_latency_duration_ms += int(duration)
            placed = False
            for index, bound in enumerate(HISTOGRAM_UPPER_BOUNDS_MS):
                if int(duration) <= bound:
                    buckets[index] += 1
                    placed = True
                    break
            if not placed:
                overflow += 1
        timed_out_or_incomplete = int(duration) >= SEARCH_HARD_TIMEOUT_MS or (
            failed and int(status) < 100
        )
        if timed_out_or_incomplete:
            hard_timeout_count += 1
        if failed or timed_out_or_incomplete:
            error_count += 1
            if 400 <= int(status) <= 499:
                status_errors["4xx"] += 1
            elif 500 <= int(status) <= 599:
                status_errors["5xx"] += 1
            else:
                status_errors["other"] += 1
    total = len(samples)
    rank = math.ceil(latency_sample_count * 0.95) if latency_sample_count else 0
    cumulative = 0
    p95: int | None = None
    p95_overflow = False
    if rank:
        for count, bound in zip(buckets, HISTOGRAM_UPPER_BOUNDS_MS):
            cumulative += count
            if cumulative >= rank:
                p95 = bound
                break
        if p95 is None:
            p95_overflow = True
    return {
        "available": True,
        "terminal_count": total,
        "terminal_error_count": error_count,
        "terminal_error_rate": round(error_count / total, 6) if total else 0.0,
        # This uses the terminal audit durations themselves, not histogram
        # bucket bounds, and intentionally has no latency SLA attached.
        "latency_sample_count": latency_sample_count,
        "latency_mean_ms": latency_duration_ms / latency_sample_count
        if latency_sample_count
        else None,
        "successful_latency_sample_count": successful_latency_sample_count,
        "successful_latency_mean_ms": successful_latency_duration_ms
        / successful_latency_sample_count
        if successful_latency_sample_count
        else None,
        "hard_timeout_or_incomplete_count": hard_timeout_count,
        "hard_timeout_ms": SEARCH_HARD_TIMEOUT_MS,
        "terminal_errors_by_status_class": status_errors,
        "terminal_count_by_endpoint": endpoint_counts,
        "latency_histogram": [
            {"upper_bound_ms": bound, "count": count}
            for bound, count in zip(HISTOGRAM_UPPER_BOUNDS_MS, buckets)
        ]
        + [{"upper_bound_ms": None, "count": overflow}],
        "latency_p95_upper_bound_ms": p95,
        "latency_p95_overflow": p95_overflow,
    }


def _journal_cursor_explicitly_unavailable(
    completed: subprocess.CompletedProcess[bytes],
) -> bool:
    return bool(
        completed.returncode == 1
        and completed.stdout == b""
        and _CURSOR_UNAVAILABLE_STDERR.fullmatch(completed.stderr) is not None
    )


def collect_journal(
    policy: Mapping[str, Any], *, cursor: str | None, boot_changed: bool, now: datetime,
    disabled: bool,
) -> dict[str, Any]:
    if disabled or policy["journal"]["enabled"] is not True:
        return {
            "available": False,
            "reason": "disabled",
            "cursor": cursor,
            "entries_read": 0,
            "backlog_possible": False,
            "cursor_fallback_used": False,
            "replay_possible": False,
            "recovery_eligible": False,
            "cross_boot_cursor_used": False,
            "metrics": {"available": False},
        }
    max_entries = int(policy["journal"]["max_entries"])
    base_command = [
        str(JOURNALCTL),
        "--user-unit",
        FIXED_UNIT,
        "--no-pager",
        "--output=json",
        "--output-fields=MESSAGE,__CURSOR,__REALTIME_TIMESTAMP",
        f"--grep={FIXED_JOURNAL_GREP}",
        "--show-cursor",
        # A leading '+' selects the oldest matching entries.  Each bounded
        # run can therefore advance its cursor through a backlog instead of
        # repeatedly failing on (and eventually losing) the newest tail.
        f"--lines=+{max_entries}",
    ]
    timeout = int(policy["journal"]["timeout_seconds"])
    max_stdout = int(policy["limits"]["journal_output_bytes"])

    def execute(selector: str) -> subprocess.CompletedProcess[bytes]:
        return _run_command(
            [*base_command, selector], timeout=timeout, max_stdout=max_stdout
        )

    def unavailable(
        reason: str, *, fallback_used: bool, replay_possible: bool
    ) -> dict[str, Any]:
        return {
            "available": False,
            "reason": reason,
            "cursor": cursor,
            "entries_read": 0,
            "backlog_possible": False,
            "cursor_fallback_used": fallback_used,
            "replay_possible": replay_possible,
            "recovery_eligible": False,
            "cross_boot_cursor_used": False,
            "metrics": {"available": False},
        }

    fallback_used = False
    replay_possible = cursor is None
    cross_boot_cursor_used = False
    if cursor is not None:
        # A cursor is journal-global and remains the strongest incremental
        # boundary across a kernel reboot.  Never discard it merely because
        # boot_id changed.
        completed = execute(f"--after-cursor={cursor}")
        if completed.returncode not in {0, 1}:
            return unavailable(
                "journalctl_failed", fallback_used=False, replay_possible=False
            )
        try:
            samples, proposed_cursor, entries = parse_journal_json(
                completed.stdout,
                max_entries=max_entries,
                max_message_bytes=int(
                    policy["limits"]["journal_message_bytes"]
                ),
            )
        except MonitorError as exc:
            if not (
                exc.code == "JOURNAL_CURSOR_MISSING"
                and _journal_cursor_explicitly_unavailable(completed)
            ):
                raise
            fallback_used = True
            replay_possible = True
        else:
            cross_boot_cursor_used = bool(boot_changed)
    if cursor is None or fallback_used:
        since = int(now.timestamp()) - int(
            policy["journal"]["initial_lookback_seconds"]
        )
        completed = execute(f"--since=@{max(0, since)}")
        if completed.returncode not in {0, 1}:
            return unavailable(
                "journalctl_fallback_failed"
                if fallback_used
                else "journalctl_failed",
                fallback_used=fallback_used,
                replay_possible=True,
            )
        # journalctl uses 1 for a valid grep with no matching entries and
        # still emits --show-cursor.  Missing cursors remain fail-closed.
        samples, proposed_cursor, entries = parse_journal_json(
            completed.stdout,
            max_entries=max_entries,
            max_message_bytes=int(policy["limits"]["journal_message_bytes"]),
        )
    return {
        "available": True,
        "reason": None,
        "cursor": proposed_cursor,
        "entries_read": entries,
        "backlog_possible": entries == max_entries,
        "cursor_fallback_used": fallback_used,
        "replay_possible": replay_possible,
        "recovery_eligible": not replay_possible,
        "cross_boot_cursor_used": cross_boot_cursor_used,
        "metrics": aggregate_search_metrics(samples),
    }


def _restart_delta(
    systemd: Mapping[str, Any], baseline: Mapping[str, Any], *, boot_id: str
) -> tuple[int, bool, bool]:
    old_boot = baseline.get("boot_id")
    old_invocation = baseline.get("invocation_id")
    old_restarts = baseline.get("nrestarts")
    boot_changed = bool(old_boot is not None and old_boot != boot_id)
    invocation_changed = bool(
        old_invocation is not None and old_invocation != systemd["invocation_id"]
    )
    if old_boot is None or boot_changed or old_restarts is None:
        return 0, boot_changed, invocation_changed
    current = int(systemd["nrestarts"])
    if invocation_changed:
        return max(1, current), False, True
    return max(0, current - int(old_restarts)), False, False


def _quota_delta(quota: Mapping[str, Any], baseline: Mapping[str, Any]) -> tuple[int, bool]:
    if quota.get("available") is not True:
        return 0, False
    old_revision = baseline.get("quota_revision")
    old_used = baseline.get("quota_used")
    if old_revision is None or old_used is None:
        return 0, False
    regressed = bool(
        int(quota["state_revision"]) < int(old_revision)
        or int(quota["used"]) < int(old_used)
    )
    return max(0, int(quota["used"]) - int(old_used)), regressed


def build_conditions(
    *,
    policy: Mapping[str, Any],
    systemd: Mapping[str, Any],
    restart_delta: int,
    health: Mapping[str, Any],
    proofs: Mapping[str, Any],
    journal: Mapping[str, Any],
    quota: Mapping[str, Any],
    quota_delta: int,
    quota_regressed: bool,
) -> dict[str, str | bool | None]:
    """Return every fixed alert as false, a severity, or unknown/temporarily frozen."""

    result: dict[str, str | bool | None] = {code: False for code in ALERT_CODES}
    service_active = bool(
        systemd["active_state"] == "active"
        and systemd["sub_state"] == "running"
        and systemd["result"] == "success"
        and int(systemd["main_pid"]) > 0
        and systemd["invocation_id"] is not None
    )
    result["SERVICE_INACTIVE"] = "critical" if not service_active else False
    result["SERVICE_RECENTLY_STARTED"] = bool(
        service_active
        and float(systemd["uptime_seconds"])
        < int(policy["thresholds"]["minimum_uptime_seconds"])
    ) and "warning"
    if restart_delta >= int(policy["thresholds"]["restart_critical_delta"]):
        result["SERVICE_RESTART_DELTA"] = "critical"
    elif restart_delta >= int(policy["thresholds"]["restart_warning_delta"]):
        result["SERVICE_RESTART_DELTA"] = "warning"
    result["DAEMON_RELOAD_REQUIRED"] = (
        "warning" if systemd["need_daemon_reload"] is True else False
    )

    health_available = health.get("available") is True
    result["HEALTH_UNAVAILABLE"] = "critical" if not health_available else False
    if health_available:
        payload = health.get("payload")
        result["HEALTH_NOT_READY"] = (
            "critical"
            if not isinstance(payload, Mapping)
            or payload.get("ready") is not True
            or bool(health.get("validation_failures"))
            else False
        )
        proof_codes = {
            "SOURCE_PROOF_MISMATCH": "source",
            "PYTHON_RUNTIME_PROOF_MISMATCH": "python_runtime",
            "WORKER_PROOF_MISMATCH": "worker",
            "RUNTIME_MANIFEST_PROOF_MISMATCH": "runtime_manifest",
            "LIGHTRAG_SIX_FILE_PROOF_MISMATCH": "lightrag_six_files",
        }
        for code, section in proof_codes.items():
            result[code] = (
                False
                if isinstance(proofs.get(section), Mapping)
                and proofs[section].get("verified") is True
                else "critical"
            )
    else:
        for code in (
            "HEALTH_NOT_READY",
            "SOURCE_PROOF_MISMATCH",
            "PYTHON_RUNTIME_PROOF_MISMATCH",
            "WORKER_PROOF_MISMATCH",
            "RUNTIME_MANIFEST_PROOF_MISMATCH",
            "LIGHTRAG_SIX_FILE_PROOF_MISMATCH",
        ):
            result[code] = None

    metrics = journal.get("metrics")
    if journal.get("available") is not True or not isinstance(metrics, Mapping):
        result["SEARCH_METRICS_UNAVAILABLE"] = "warning"
        result["SEARCH_ERROR_RATE_HIGH"] = None
        result["SEARCH_HARD_TIMEOUT"] = None
    else:
        result["SEARCH_METRICS_UNAVAILABLE"] = False
        total = int(metrics["terminal_count"])
        errors = int(metrics["terminal_error_count"])
        rate = float(metrics["terminal_error_rate"])
        thresholds = policy["thresholds"]
        enough = total >= int(thresholds["search_minimum_samples"])
        recovery_eligible = journal.get("recovery_eligible") is not False
        if total <= 0 or not recovery_eligible:
            # An empty incremental batch contains no evidence that a prior
            # condition recovered. A replay/fallback batch is not attributable
            # to the current interval, so old successes and failures alike are
            # held out of alert transitions.
            result["SEARCH_ERROR_RATE_HIGH"] = None
            result["SEARCH_HARD_TIMEOUT"] = None
        else:
            if errors >= int(thresholds["search_error_critical_count"]) or (
                enough and rate >= float(thresholds["search_error_critical_rate"])
            ):
                result["SEARCH_ERROR_RATE_HIGH"] = "critical"
            elif errors >= int(thresholds["search_error_warning_count"]) or (
                enough and rate >= float(thresholds["search_error_warning_rate"])
            ):
                result["SEARCH_ERROR_RATE_HIGH"] = "warning"
            hard_timeout = int(metrics["hard_timeout_or_incomplete_count"]) > 0
            result["SEARCH_HARD_TIMEOUT"] = (
                "critical" if hard_timeout else False
            )

    if quota.get("available") is not True:
        result["TAVILY_QUOTA_UNAVAILABLE"] = "critical"
        for code in (
            "TAVILY_QUOTA_LOW",
            "TAVILY_QUOTA_CONSUMPTION_HIGH",
            "TAVILY_QUOTA_COUNTER_REGRESSION",
        ):
            result[code] = None
    else:
        result["TAVILY_QUOTA_UNAVAILABLE"] = False
        thresholds = policy["thresholds"]
        remaining = int(quota["remaining"])
        remaining_ratio = remaining / int(quota["total_capacity"])
        if remaining == 0 or remaining_ratio <= float(
            thresholds["tavily_remaining_critical_ratio"]
        ):
            result["TAVILY_QUOTA_LOW"] = "critical"
        elif remaining_ratio <= float(
            thresholds["tavily_remaining_warning_ratio"]
        ):
            result["TAVILY_QUOTA_LOW"] = "warning"
        if quota_delta >= int(thresholds["tavily_consumed_critical_delta"]):
            result["TAVILY_QUOTA_CONSUMPTION_HIGH"] = "critical"
        elif quota_delta >= int(thresholds["tavily_consumed_warning_delta"]):
            result["TAVILY_QUOTA_CONSUMPTION_HIGH"] = "warning"
        result["TAVILY_QUOTA_COUNTER_REGRESSION"] = (
            "critical" if quota_regressed else False
        )
    return result


def evaluate_alert_transitions(
    alert_states: Mapping[str, Mapping[str, Any]],
    conditions: Mapping[str, str | bool | None],
    *,
    now: datetime,
    repeat_seconds: int,
    recovery_observations: int,
    next_revision: int,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Apply first/escalation/6h-repeat/two-observation recovery de-duplication."""

    if set(alert_states) != ALERT_CODE_SET or set(conditions) != ALERT_CODE_SET:
        raise MonitorError("ALERT_TRANSITION_INPUT_INVALID")
    updated: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    for code in ALERT_CODES:
        previous = dict(alert_states[code])
        condition = conditions[code]
        previous_observed = _parse_timestamp(previous["last_observed_at"], nullable=True)
        effective_now = now.astimezone(timezone.utc)
        if previous_observed is not None and effective_now < previous_observed:
            effective_now = previous_observed
        at = _timestamp(effective_now)
        event_kind: str | None = None
        event_severity: str | None = None
        if condition is None:
            updated[code] = previous
            continue
        if condition in SEVERITY_RANK:
            severity = str(condition)
            if previous["active"] is not True:
                previous = {
                    "active": True,
                    "severity": severity,
                    "first_seen_at": at,
                    "last_observed_at": at,
                    "last_emitted_at": at,
                    "recovery_streak": 0,
                }
                event_kind = "first"
                event_severity = severity
            else:
                old_severity = str(previous["severity"])
                previous["last_observed_at"] = at
                previous["recovery_streak"] = 0
                last_emitted = _parse_timestamp(previous["last_emitted_at"], nullable=False)
                assert last_emitted is not None
                if SEVERITY_RANK[severity] > SEVERITY_RANK[old_severity]:
                    event_kind = "escalation"
                    event_severity = severity
                    previous["last_emitted_at"] = at
                elif (effective_now - last_emitted).total_seconds() >= repeat_seconds:
                    event_kind = "repeat"
                    event_severity = severity
                    previous["last_emitted_at"] = at
                previous["severity"] = severity
        elif condition is False:
            if previous["active"] is True:
                streak = int(previous["recovery_streak"]) + 1
                previous["last_observed_at"] = at
                if streak >= recovery_observations:
                    event_kind = "recovery"
                    event_severity = str(previous["severity"])
                    previous = {
                        "active": False,
                        "severity": None,
                        "first_seen_at": None,
                        "last_observed_at": at,
                        "last_emitted_at": at,
                        "recovery_streak": 0,
                    }
                else:
                    previous["recovery_streak"] = streak
            else:
                previous["last_observed_at"] = at
                previous["recovery_streak"] = 0
        else:
            raise MonitorError("ALERT_TRANSITION_INPUT_INVALID")
        updated[code] = previous
        if event_kind is not None and event_severity is not None:
            event_core = {
                "code": code,
                "kind": event_kind,
                "severity": event_severity,
                "at": at,
                "revision": next_revision,
            }
            events.append(
                {
                    "event_id": _sha256(_canonical_json(event_core)),
                    "code": code,
                    "kind": event_kind,
                    "severity": event_severity,
                    "at": at,
                }
            )
    return updated, events


def _expected_bindings(args: argparse.Namespace) -> dict[str, str]:
    values = {
        "source_head": args.expected_source_head,
        "source_tree": args.expected_source_tree,
        "source_manifest_sha256": args.expected_source_manifest_sha256,
        "python_runtime_manifest_sha256": args.expected_python_runtime_manifest_sha256,
        "python_runtime_tree_sha256": args.expected_python_runtime_tree_sha256,
        "python_executable_sha256": args.expected_python_executable_sha256,
        "runtime_manifest_sha256": args.expected_runtime_manifest_sha256,
        "store_binding_sha256": args.expected_store_binding_sha256,
    }
    for name, value in values.items():
        lengths = (40, 64) if name in {"source_head", "source_tree"} else (64,)
        if not _lower_hex(value, *lengths):
            raise MonitorError("EXPECTED_BINDING_INVALID")
    return values


def _binding_sha256(expected: Mapping[str, str]) -> str:
    return _sha256(
        _canonical_json(
            {
                "schema_version": 1,
                "unit": FIXED_UNIT,
                "health_url": FIXED_HEALTH_URL,
                "expected": expected,
            }
        )
    )


def _active_alert_report(states: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "code": code,
            "severity": states[code]["severity"],
            "first_seen_at": states[code]["first_seen_at"],
            "recovery_streak": states[code]["recovery_streak"],
        }
        for code in ALERT_CODES
        if states[code]["active"] is True
    ]


def run_observation(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Collect, aggregate, transition, optionally persist, and return report/status."""

    policy, policy_sha256 = load_policy(
        args.policy.expanduser(), args.expected_policy_sha256
    )
    expected = _expected_bindings(args)
    binding_sha256 = _binding_sha256(expected)
    state_path = _fixed_state_path(
        args.state,
        policy_sha256=policy_sha256,
        binding_sha256=binding_sha256,
        create_directories=False,
    )
    token = _read_token(args.token_file.expanduser())
    lock = (
        _state_lock(
            state_path,
            policy_sha256=policy_sha256,
            binding_sha256=binding_sha256,
        )
        if args.apply
        else nullcontext()
    )
    with lock:
        state = _load_state(
            state_path,
            policy=policy,
            policy_sha256=policy_sha256,
            binding_sha256=binding_sha256,
        )
        now = datetime.now(timezone.utc).replace(microsecond=0)
        last_applied = _parse_timestamp(state["last_applied_at"], nullable=True)
        if last_applied is not None and now < last_applied:
            now = last_applied
        previous_full_proof = _parse_timestamp(
            state["last_full_proof_at"], nullable=True
        )
        previous_full_proof_age = (
            max(0.0, (now - previous_full_proof).total_seconds())
            if previous_full_proof is not None
            else None
        )
        full_proof_due = bool(
            previous_full_proof is None
            or previous_full_proof_age is None
            or previous_full_proof_age >= FULL_PROOF_INTERVAL_SECONDS
        )
        local_proofs = collect_local_proofs(args, expected, full=full_proof_due)
        proposed_full_proof_at = (
            _timestamp(now) if full_proof_due else state["last_full_proof_at"]
        )
        boot_id = _read_boot_id()
        systemd = collect_systemd(policy)
        restart_delta, boot_changed, invocation_changed = _restart_delta(
            systemd, state["baseline"], boot_id=boot_id
        )
        health = collect_health(
            policy,
            token=token,
            expected_process_pid=int(systemd["main_pid"])
            if int(systemd["main_pid"]) > 0
            else None,
        )
        proofs = _proofs(health, expected, local_proofs)
        quota = _sanitized_quota(health)
        quota_delta, quota_regressed = _quota_delta(quota, state["baseline"])
        journal = collect_journal(
            policy,
            cursor=state["journal_cursor"],
            boot_changed=boot_changed,
            now=now,
            disabled=args.no_journal,
        )
        conditions = build_conditions(
            policy=policy,
            systemd=systemd,
            restart_delta=restart_delta,
            health=health,
            proofs=proofs,
            journal=journal,
            quota=quota,
            quota_delta=quota_delta,
            quota_regressed=quota_regressed,
        )
        next_revision = int(state["revision"]) + 1
        if next_revision > MAX_STATE_REVISION:
            raise MonitorError("STATE_REVISION_EXHAUSTED")
        alert_states, events = evaluate_alert_transitions(
            state["alert_states"],
            conditions,
            now=now,
            repeat_seconds=int(policy["deduplication"]["repeat_seconds"]),
            recovery_observations=int(
                policy["deduplication"]["recovery_observations"]
            ),
            next_revision=next_revision,
        )
        pending = dict(state["pending_events"])
        for event in events:
            pending[event["code"]] = event
        if len(pending) > int(policy["limits"]["pending_events"]):
            raise MonitorError("STATE_PENDING_EVENTS_FULL")
        proposed = {
            "artifact_type": STATE_ARTIFACT,
            "schema_version": SCHEMA_VERSION,
            "revision": next_revision,
            "last_applied_at": _timestamp(now),
            "last_full_proof_at": proposed_full_proof_at,
            "policy_sha256": policy_sha256,
            "binding_sha256": binding_sha256,
            "baseline": {
                "boot_id": boot_id,
                "invocation_id": systemd["invocation_id"],
                "nrestarts": systemd["nrestarts"],
                # These are high-water marks, not merely the last sample.
                # Freezing each maximum prevents a persistent rollback from
                # becoming the new baseline and falsely recovering two
                # observations later.
                "quota_revision": max(
                    int(quota.get("state_revision", 0)),
                    int(state["baseline"]["quota_revision"] or 0),
                )
                if quota.get("available") is True
                else state["baseline"]["quota_revision"],
                "quota_used": max(
                    int(quota.get("used", 0)),
                    int(state["baseline"]["quota_used"] or 0),
                )
                if quota.get("available") is True
                else state["baseline"]["quota_used"],
            },
            "journal_cursor": journal["cursor"]
            if journal["available"] is True
            else state["journal_cursor"],
            "alert_states": alert_states,
            "pending_events": pending,
        }
        validate_state(
            proposed, pending_limit=int(policy["limits"]["pending_events"])
        )
        if args.apply:
            _write_state_atomic(
                state_path,
                proposed,
                policy_sha256=policy_sha256,
                binding_sha256=binding_sha256,
                max_bytes=int(policy["limits"]["state_bytes"]),
            )

        health_payload = health.get("payload")
        health_ready = bool(
            health.get("available") is True
            and isinstance(health_payload, Mapping)
            and health_payload.get("ready") is True
            and not health.get("validation_failures")
        )
        report = {
            "artifact_type": REPORT_ARTIFACT,
            "schema_version": SCHEMA_VERSION,
            "observed_at": _timestamp(now),
            "mode": "apply" if args.apply else "dry-run",
            "provider_calls": 0,
            "policy_sha256": policy_sha256,
            "binding_sha256": binding_sha256,
            "service": {
                **systemd,
                "boot_id": boot_id,
                "boot_changed": boot_changed,
                "invocation_changed": invocation_changed,
                "restart_delta": restart_delta,
            },
            "health": {
                "available": health["available"],
                "http_status": health["http_status"],
                "ready": health_ready,
                "error_code": health["error_code"],
                "validation_failure_count": len(health["validation_failures"]),
                "validation_failures": health["validation_failures"],
            },
            "proofs": proofs,
            "local_proofs": local_proofs,
            "proof_refresh": {
                "mode": "full" if full_proof_due else "manifest-only",
                "full_proof_performed_this_run": full_proof_due,
                "full_proof_interval_seconds": FULL_PROOF_INTERVAL_SECONDS,
                "previous_full_proof_at": state["last_full_proof_at"],
                "effective_full_proof_at": proposed_full_proof_at,
                "full_proof_age_seconds": 0.0
                if full_proof_due
                else previous_full_proof_age,
                "full_proof_persisted_this_run": bool(
                    args.apply and full_proof_due
                ),
            },
            "search": {
                **journal["metrics"],
                "journal_available": journal["available"],
                "journal_reason": journal["reason"],
                "journal_entries_read": journal["entries_read"],
                "journal_backlog_possible": bool(
                    journal.get("backlog_possible")
                ),
                "journal_cursor_fallback_used": bool(
                    journal.get("cursor_fallback_used")
                ),
                "journal_replay_possible": bool(
                    journal.get("replay_possible")
                ),
                "journal_recovery_eligible": bool(
                    journal.get("recovery_eligible")
                ),
                "journal_cross_boot_cursor_used": bool(
                    journal.get("cross_boot_cursor_used")
                ),
                "cursor_advanced": bool(
                    journal["available"] is True
                    and journal["cursor"] != state["journal_cursor"]
                ),
                "cursor_persisted": bool(args.apply and journal["available"] is True),
            },
            "tavily_quota": {
                **quota,
                "remaining_ratio": round(
                    int(quota["remaining"]) / int(quota["total_capacity"]), 6
                )
                if quota.get("available") is True
                else None,
                "consumed_delta": quota_delta,
                "counter_regressed": quota_regressed,
            },
            "alerts": {
                "active": _active_alert_report(alert_states),
                "events": events,
                "pending_event_count": len(pending),
            },
            "state": {
                "applied": bool(args.apply),
                "current_revision": state["revision"],
                "proposed_revision": next_revision,
                "proposed_last_full_proof_at": proposed_full_proof_at,
                "cursor_moved_in_dry_run": False,
            },
        }
        return report, 2 if events else 0


def _parser() -> StrictArgumentParser:
    parser = StrictArgumentParser(
        description=(
            "Collect one provider-free systemd/health/journal operations sample; "
            "dry-run is the default."
        )
    )
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument(
        "--state",
        type=Path,
        required=True,
        help="renderer-produced content-addressed 0600 monitor state path",
    )
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN)
    parser.add_argument(
        "--source-manifest",
        type=Path,
        required=True,
        help="explicit immutable source-release-manifest.json (0400, no links)",
    )
    parser.add_argument(
        "--python-runtime-manifest",
        type=Path,
        required=True,
        help="explicit immutable python-runtime-manifest.json (0400, no links)",
    )
    parser.add_argument(
        "--runtime-manifest",
        type=Path,
        required=True,
        help="explicit generation runtime-shadow-manifest.json (0400, no links)",
    )
    parser.add_argument("--expected-policy-sha256", required=True)
    parser.add_argument("--expected-source-head", required=True)
    parser.add_argument("--expected-source-tree", required=True)
    parser.add_argument("--expected-source-manifest-sha256", required=True)
    parser.add_argument("--expected-python-runtime-manifest-sha256", required=True)
    parser.add_argument("--expected-python-runtime-tree-sha256", required=True)
    parser.add_argument("--expected-python-executable-sha256", required=True)
    parser.add_argument("--expected-runtime-manifest-sha256", required=True)
    parser.add_argument("--expected-store-binding-sha256", required=True)
    parser.add_argument(
        "--no-journal",
        action="store_true",
        help="skip journalctl, freeze its cursor, and alert that metrics are unavailable",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="atomically persist 0600 state; without this flag no cursor or state moves",
    )
    return parser


def _print_json(value: Mapping[str, Any]) -> None:
    sys.stdout.buffer.write(_canonical_json(value) + b"\n")
    sys.stdout.buffer.flush()


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    args = _parser().parse_args(argv)
    try:
        report, status = run_observation(args)
    except MonitorError as exc:
        _print_json(
            {
                "artifact_type": REPORT_ARTIFACT,
                "schema_version": SCHEMA_VERSION,
                "status": "error",
                "error_code": exc.code,
                "provider_calls": 0,
                "state_applied": False,
            }
        )
        return 3
    except Exception:
        _print_json(
            {
                "artifact_type": REPORT_ARTIFACT,
                "schema_version": SCHEMA_VERSION,
                "status": "error",
                "error_code": "MONITOR_INTERNAL_ERROR",
                "provider_calls": 0,
                "state_applied": False,
            }
        )
        return 3
    _print_json(report)
    return status


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
