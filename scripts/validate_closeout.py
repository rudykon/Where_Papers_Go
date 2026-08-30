#!/usr/bin/env python3
"""Run and publish the fixed aggregate-only machine closeout.

The request can bind only a Git HEAD, its non-main ``agent/*`` branch, and the
expected SHA-256 values for a fixed set of read-only aggregate artifacts.  Test
results, network-attempt counts, deployment state, artifact paths, and output
field names are produced by tracked code and cannot be supplied by the caller.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterator, Mapping, Sequence
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "benchmark_artifacts"
GIT_BINARY = Path("/usr/bin/git")
SYSTEMCTL_BINARY = Path("/usr/bin/systemctl")
SS_BINARY = Path("/usr/bin/ss")
SERVICE_UNIT = "where-papers-go.service"
HEALTH_HOST = "127.0.0.1"
HEALTH_PORT = 8001
HEALTH_PATH = "/api/health"
EXPECTED_BACKEND = "lightrag_mix+property_graph_exact_vector+llm+search_api"
REQUEST_ARTIFACT_TYPE = "where_papers_go_aggregate_closeout_request"
OUTPUT_ARTIFACT_TYPE = "where_papers_go_aggregate_closeout_validation"
TEST_REPORT_ARTIFACT_TYPE = "where_papers_go_closeout_test_report"
OUTPUT_PREFIX = "final_delivery_validation_v2_"
OUTPUT_SCHEMA_VERSION = 3
FULL_TEST_COUNT = 482
FULL_TEST_ID_SHA256 = (
    "d59330bf8f317661ae543cbd6d56c48cc1db168ef72c64be1b6618cdcb243268"
)
MODEL_FOCUSED_TEST_COUNT = 6
MODEL_FOCUSED_TEST_ID_SHA256 = (
    "651d59643b938f9f13712a8838a08f51efe74bb18d100fc07e8f0c825c866b94"
)
MODEL_RUNTIME_PYTHON = Path(
    "benchmark_artifacts/m3_model_runtime_20260828/venv/bin/python"
)
MODEL_RUNTIME_RESOLVED_PYTHON = Path("/usr/bin/python3.12")
MODEL_RUNTIME_INTERPRETER_SHA256 = (
    "1643dacd9feaedc58f3cc581e4d22577dfe25c09b10282936186ccf0f2e61118"
)
MODEL_RUNTIME_INTERPRETER_BYTES = 8020928
MODEL_RUNTIME_INTERPRETER_MODE = 0o755
FULL_TEST_KEY = "full_unittest"
MODEL_TEST_KEY = "model_focused_4_test_double_plus_2_synthetic_safetensors"
EXPECTED_CLOSEOUT_BRANCH = "agent/aggregate-only-closeout-20260831"
HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
HEX_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
AGENT_BRANCH = re.compile(r"agent/[a-z0-9][a-z0-9._/-]{0,126}\Z")
VERSION_DIRECTORY = re.compile(
    rf"{re.escape(OUTPUT_PREFIX)}\d{{8}}T\d{{12}}Z-[0-9a-f]{{12}}\Z"
)
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

# This is intentionally a fixed allowlist.  All files are aggregate records
# already frozen mode 0444.  The legacy closeout transitively binds the older
# P0/M3/SCOPE hashes while remaining byte-for-byte preserved.
REQUIRED_ARTIFACTS: Mapping[str, Path] = {
    "legacy_closeout_summary": Path(
        "benchmark_artifacts/final_delivery_validation_20260830/summary.json"
    ),
    "future_dataset_manifest": Path(
        "benchmark_artifacts/future_sealed_test_202607_v1/manifest.json"
    ),
    "future_namespace_repair_authorization": Path(
        "benchmark_artifacts/future_sealed_namespace_repair_202607_v1/authorization.json"
    ),
    "future_namespace_repair_manifest": Path(
        "benchmark_artifacts/future_sealed_namespace_repair_202607_v1/manifest.json"
    ),
    "future_evaluation_manifest": Path(
        "benchmark_artifacts/future_sealed_evaluation_202607_v1/manifest.json"
    ),
    "future_evaluation_metrics": Path(
        "benchmark_artifacts/future_sealed_evaluation_202607_v1/metrics.json"
    ),
    "future_evaluation_leakage_audit": Path(
        "benchmark_artifacts/future_sealed_evaluation_202607_v1/leakage_audit.json"
    ),
    "future_evaluation_namespace_audit": Path(
        "benchmark_artifacts/future_sealed_evaluation_202607_v1/namespace_mapping_audit.json"
    ),
}
TRACKED_HELPERS: Mapping[str, Path] = {
    "test_runner_sha256": Path("scripts/run_closeout_tests.py"),
    "offline_guard_sha256": Path(
        "scripts/closeout_offline_guard/sitecustomize.py"
    ),
}
TRACKED_IMPLEMENTATION_FILES: Mapping[str, Path] = {
    "closeout_validator_sha256": Path("scripts/validate_closeout.py"),
    "closeout_validator_test_sha256": Path("tests/test_validate_closeout.py"),
    "closeout_runner_test_sha256": Path("tests/test_closeout_runner.py"),
    **TRACKED_HELPERS,
    "external_call_budget_sha256": Path(
        "where_paper_go/external_call_budget.py"
    ),
    "external_call_budget_test_sha256": Path(
        "tests/test_external_call_budget.py"
    ),
    "worker_sha256": Path("where_paper_go/worker.py"),
    "worker_test_sha256": Path("tests/test_worker.py"),
    "web_app_sha256": Path("where_paper_go/web_app.py"),
    "web_app_test_sha256": Path("tests/test_web_app.py"),
    "formal500_builder_test_sha256": Path(
        "tests/test_build_recent_journal_benchmark.py"
    ),
    "model_runs_test_sha256": Path("tests/test_model_runs.py"),
    "local_model_runtime_test_sha256": Path(
        "tests/test_local_model_runtime.py"
    ),
    "handoff_sha256": Path("HANDOFF.md"),
    "production_deployment_doc_sha256": Path(
        "docs/production-deployment.md"
    ),
    "recent_journal_benchmark_doc_sha256": Path(
        "docs/recent-journal-benchmark.md"
    ),
    "research_readme_sha256": Path("research/README.md"),
}
PINNED_ARTIFACT_SHA256: Mapping[str, str] = {
    "legacy_closeout_summary": "02bf056f663ae2d3578e7295fa7248fc358f2047e5ad88a0526084ab34182e57",
    "future_dataset_manifest": "b11de0a6bfce3869643a4c0dab38a0ac3d92913a0720d579c1cf850ab98d9650",
    "future_namespace_repair_authorization": "aa5f2bda39074028ebae944645acd68b5253003e0c3ab2e27e8e71f539aa513e",
    "future_namespace_repair_manifest": "64456236a956ece0929bffc923b2f918a09c292fd3d35c1f2a9bd55eb2940d33",
    "future_evaluation_manifest": "b0eb5d5045df10a0e64f7dc0ffba264bdc479671cb669197b5f3580d79391a0b",
    "future_evaluation_metrics": "e50da50af5a39266a8af9ef2fdde05bfc82abf2a5d11a047813567060cc7e52a",
    "future_evaluation_leakage_audit": "54cb5246cca70decb8b5383da650670dc0630c07e8b4f3b31fb9cc4b74e7e725",
    "future_evaluation_namespace_audit": "e42d787a4a595ed2e8effefe3e91c0fbb0be544f95bde66ee522f95842248c71",
}
PINNED_ARTIFACT_BYTES: Mapping[str, int] = {
    "legacy_closeout_summary": 6862,
    "future_dataset_manifest": 14455,
    "future_namespace_repair_authorization": 2070,
    "future_namespace_repair_manifest": 4760,
    "future_evaluation_manifest": 10995,
    "future_evaluation_metrics": 1089118,
    "future_evaluation_leakage_audit": 1734,
    "future_evaluation_namespace_audit": 1373,
}
REQUEST_KEYS = {
    "schema_version",
    "artifact_type",
    "expected_head",
    "expected_branch",
    "artifacts",
}
TEST_REPORT_KEYS = {
    "schema_version",
    "artifact_type",
    "guard_active",
    "total",
    "passed",
    "skipped",
    "failures",
    "errors",
    "expected_failures",
    "unexpected_successes",
    "test_id_count",
    "test_id_sha256",
}
OUTPUT_KEYS = {
    "schema_version",
    "artifact_type",
    "status",
    "recorded_at",
    "aggregate_only",
    "contains_per_query_values",
    "git",
    "request_sha256",
    "tests",
    "tracked_implementation",
    "critical_artifacts",
    "deployment",
    "external_calls",
    "excluded_actions",
    "publication",
    "threat_model_limitations",
}
TEST_PUBLIC_KEYS = {
    "total",
    "passed",
    "skipped",
    "failures",
    "errors",
    "expected_failures",
    "unexpected_successes",
    "test_id_count",
    "test_id_sha256",
    "offline_guard_active",
    "report_sha256",
    "offline_guard_audit_sha256",
}
FULL_TEST_OUTPUT_KEYS = TEST_PUBLIC_KEYS | {
    "test_runner_sha256",
    "offline_guard_sha256",
}
MODEL_TEST_OUTPUT_KEYS = TEST_PUBLIC_KEYS | {
    "test_runner_sha256",
    "offline_guard_sha256",
    "model_runtime_interpreter_sha256",
}
TESTS_OUTPUT_KEYS = {
    FULL_TEST_KEY,
    MODEL_TEST_KEY,
    "official_weight_inference_tests",
}
TRACKED_IMPLEMENTATION_OUTPUT_KEYS = set(TRACKED_IMPLEMENTATION_FILES)
DEPLOYMENT_OUTPUT_KEYS = {
    "active",
    "enabled",
    "ready",
    "bindings_current",
    "lightrag_store_hashes_verified",
    "listener_scope",
    "main_pid",
    "nrestarts",
    "lightrag_manifest_sha256",
    "lightrag_store_binding_sha256",
    "systemd_snapshot_sha256",
    "health_snapshot_sha256",
    "listener_snapshot_sha256",
}
EXTERNAL_CALL_OUTPUT_KEYS = {
    "enforcement",
    "scope",
    "guard_observed_nonloopback_socket_attempts",
    "loopback_health_allowed",
    "loopback_test_traffic_allowed",
    "af_unix_allowed",
    "native_child_network_instrumented",
}
PUBLICATION_OUTPUT_KEYS = {
    "directory",
    "directory_mode",
    "summary_mode",
    "existing_directories_preserved",
    "overwrite_supported",
    "same_head_replay_supported",
}
EXCLUDED_ACTION_OUTPUT_KEYS = {
    "live_formal500_executed",
    "human_evaluation_executed",
    "production_service_mutated",
    "live_external_provider_workflows_requested_by_validator",
    "loopback_health_probe",
    "scope",
}
THREAT_MODEL_LIMITATIONS = (
    "The tracked runner emits aggregate counts and a test-ID fingerprint only; no per-test or per-query values are published.",
    "The socket guard covers the closeout test interpreters and inheriting Python children; fixed systemd, listener, and loopback-health inspections are read-only local probes, and later user-authorized Git transport is outside this observation.",
    "The socket guard permits loopback and AF_UNIX and does not instrument native non-Python child networking; therefore an empty audit proves zero observed non-loopback attempts only within guarded Python interpreters, not an absolute zero-provider-call claim. The fixed tracked suite, sanitized environment, cleared host opt-ins, and offline dependency settings constrain that residual scope.",
    "The systemd MainPID, ss listener, and HTTP health result are separate read-only observations; the listener snapshot does not expose process ownership, so this local probe does not cryptographically prove that all three observations came from one process.",
    "Local modes, hashes, exclusive creation, and drift checks do not defend against an administrator with equal or greater file permissions who can rewrite evidence, anchors, code, or the clock together.",
)


class CloseoutValidationError(RuntimeError):
    """Aggregate closeout evidence is incomplete, unsafe, or has drifted."""


@dataclass(frozen=True)
class FileSnapshot:
    device: int
    inode: int
    size: int
    mode: int
    mtime_ns: int
    ctime_ns: int
    sha256: str


@dataclass(frozen=True)
class GitState:
    head: str
    tree: str
    branch: str
    worktree_clean: bool


@dataclass(frozen=True)
class TestEvidence:
    public: Mapping[str, Any]
    report_path: Path
    report_snapshot: FileSnapshot
    audit_path: Path
    audit_snapshot: FileSnapshot
    interpreter_path: Path | None = None
    interpreter_resolved_path: Path | None = None
    interpreter_snapshot: FileSnapshot | None = None


@dataclass(frozen=True)
class ArtifactEvidence:
    public: Mapping[str, Mapping[str, Any]]
    snapshots: Mapping[str, FileSnapshot]


@dataclass(frozen=True)
class DeploymentEvidence:
    active: bool
    enabled: bool
    ready: bool
    bindings_current: bool
    lightrag_store_hashes_verified: bool
    main_pid: int
    nrestarts: int
    lightrag_manifest_sha256: str | None
    lightrag_store_binding_sha256: str | None
    systemd_snapshot_sha256: str
    health_snapshot_sha256: str
    listener_snapshot_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "enabled": self.enabled,
            "ready": self.ready,
            "bindings_current": self.bindings_current,
            "lightrag_store_hashes_verified": (
                self.lightrag_store_hashes_verified
            ),
            "listener_scope": "loopback_only",
            "main_pid": self.main_pid,
            "nrestarts": self.nrestarts,
            "lightrag_manifest_sha256": self.lightrag_manifest_sha256,
            "lightrag_store_binding_sha256": (
                self.lightrag_store_binding_sha256
            ),
            "systemd_snapshot_sha256": self.systemd_snapshot_sha256,
            "health_snapshot_sha256": self.health_snapshot_sha256,
            "listener_snapshot_sha256": self.listener_snapshot_sha256,
        }


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CloseoutValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_exact_keys(
    value: Mapping[str, Any], expected: set[str], *, context: str
) -> None:
    actual = set(value)
    if actual != expected:
        raise CloseoutValidationError(
            f"{context} keys mismatch; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _inspect_regular_file(
    path: Path,
    *,
    capture: bool,
    expected_mode: int | None = None,
    maximum_bytes: int | None = None,
    require_current_owner: bool = True,
) -> tuple[bytes | None, FileSnapshot]:
    """Bind path lstat, opened fd, fd-after-read, and path-after-read."""

    try:
        path_before = path.lstat()
    except OSError as exc:
        raise CloseoutValidationError(f"cannot lstat regular file: {exc}") from exc
    if stat.S_ISLNK(path_before.st_mode) or not stat.S_ISREG(path_before.st_mode):
        raise CloseoutValidationError("required path is not a non-symlink regular file")
    # O_NONBLOCK is inert for regular files and prevents a path-swap to a FIFO
    # from hanging the validator before the descriptor type check can fail.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | O_NOFOLLOW
        | O_NONBLOCK
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CloseoutValidationError(f"cannot open regular file: {exc}") from exc
    try:
        fd_before = os.fstat(descriptor)
        if _stat_identity(path_before) != _stat_identity(fd_before):
            raise CloseoutValidationError("path was replaced between lstat and open")
        if require_current_owner and fd_before.st_uid != os.geteuid():
            raise CloseoutValidationError(
                "required regular file is not owned by the current user"
            )
        mode = stat.S_IMODE(fd_before.st_mode)
        if expected_mode is not None and mode != expected_mode:
            raise CloseoutValidationError(
                f"regular file mode mismatch: expected {expected_mode:04o}, "
                f"observed {mode:04o}"
            )
        if maximum_bytes is not None and fd_before.st_size > maximum_bytes:
            raise CloseoutValidationError("regular file exceeds aggregate size limit")
        chunks: list[bytes] | None = [] if capture else None
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            if chunks is not None:
                chunks.append(chunk)
            digest.update(chunk)
        fd_after = os.fstat(descriptor)
        if _stat_identity(fd_before) != _stat_identity(fd_after):
            raise CloseoutValidationError("regular file changed while being read")
    finally:
        os.close(descriptor)
    try:
        path_after = path.lstat()
    except OSError as exc:
        raise CloseoutValidationError(
            f"regular file path disappeared after read: {exc}"
        ) from exc
    if _stat_identity(fd_after) != _stat_identity(path_after):
        raise CloseoutValidationError("regular file path was replaced after read")
    snapshot = FileSnapshot(
        device=fd_after.st_dev,
        inode=fd_after.st_ino,
        size=fd_after.st_size,
        mode=stat.S_IMODE(fd_after.st_mode),
        mtime_ns=fd_after.st_mtime_ns,
        ctime_ns=fd_after.st_ctime_ns,
        sha256=digest.hexdigest(),
    )
    return (b"".join(chunks) if chunks is not None else None), snapshot


def _load_json_regular(
    path: Path, *, expected_mode: int, maximum_bytes: int
) -> tuple[dict[str, Any], FileSnapshot]:
    raw, snapshot = _inspect_regular_file(
        path,
        capture=True,
        expected_mode=expected_mode,
        maximum_bytes=maximum_bytes,
    )
    assert raw is not None
    try:
        decoded = raw.decode("utf-8")
        value = json.loads(decoded, object_pairs_hook=_duplicate_rejecting_object)
    except CloseoutValidationError:
        raise
    except (UnicodeError, TypeError, ValueError) as exc:
        raise CloseoutValidationError(f"invalid aggregate JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise CloseoutValidationError("aggregate JSON must be an object")
    return value, snapshot


def _validate_branch(value: Any) -> str:
    if (
        not isinstance(value, str)
        or AGENT_BRANCH.fullmatch(value) is None
        or ".." in value
        or "//" in value
        or value.endswith(("/", "."))
    ):
        raise CloseoutValidationError(
            "expected_branch must be a normalized non-main agent/* branch"
        )
    if value != EXPECTED_CLOSEOUT_BRANCH:
        raise CloseoutValidationError(
            "expected_branch must equal the fixed aggregate-only closeout branch"
        )
    return value


def _validate_request(raw: Mapping[str, Any]) -> dict[str, Any]:
    _require_exact_keys(raw, REQUEST_KEYS, context="closeout request")
    if raw["schema_version"] != 2:
        raise CloseoutValidationError("unsupported closeout request schema_version")
    if raw["artifact_type"] != REQUEST_ARTIFACT_TYPE:
        raise CloseoutValidationError("unexpected closeout request artifact_type")
    head = raw["expected_head"]
    if not isinstance(head, str) or HEX_COMMIT.fullmatch(head) is None:
        raise CloseoutValidationError("expected_head must be lowercase 40-hex")
    branch = _validate_branch(raw["expected_branch"])
    artifacts = raw["artifacts"]
    if not isinstance(artifacts, dict):
        raise CloseoutValidationError("artifacts must be an object")
    _require_exact_keys(
        artifacts, set(REQUIRED_ARTIFACTS), context="request artifacts"
    )
    normalized_artifacts: dict[str, str] = {}
    for name in REQUIRED_ARTIFACTS:
        digest = artifacts[name]
        if not isinstance(digest, str) or HEX_SHA256.fullmatch(digest) is None:
            raise CloseoutValidationError(
                f"request artifact {name} must be lowercase SHA-256"
            )
        normalized_artifacts[name] = digest
    if normalized_artifacts != dict(PINNED_ARTIFACT_SHA256):
        raise CloseoutValidationError(
            "request artifact hashes must equal the fixed known SHA-256 values"
        )
    return {
        "schema_version": 2,
        "artifact_type": REQUEST_ARTIFACT_TYPE,
        "expected_head": head,
        "expected_branch": branch,
        "artifacts": normalized_artifacts,
    }


def _git_environment() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
    }


def _run_git(project_root: Path, arguments: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            [str(GIT_BINARY), *arguments],
            cwd=project_root,
            env=_git_environment(),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CloseoutValidationError("fixed Git inspection failed") from exc
    return completed.stdout.strip()


def _git_state(project_root: Path) -> GitState:
    top_level = _run_git(project_root, ("rev-parse", "--show-toplevel"))
    if Path(os.path.realpath(top_level)) != project_root:
        raise CloseoutValidationError("Git top-level does not match project root")
    head = _run_git(project_root, ("rev-parse", "HEAD"))
    if HEX_COMMIT.fullmatch(head) is None:
        raise CloseoutValidationError("Git returned an invalid HEAD")
    tree = _run_git(project_root, ("rev-parse", "HEAD^{tree}"))
    if HEX_COMMIT.fullmatch(tree) is None:
        raise CloseoutValidationError("Git returned an invalid tree")
    branch = _run_git(
        project_root, ("symbolic-ref", "--quiet", "--short", "HEAD")
    )
    _validate_branch(branch)
    porcelain = _run_git(
        project_root, ("status", "--porcelain=v1", "--untracked-files=normal")
    )
    return GitState(
        head=head,
        tree=tree,
        branch=branch,
        worktree_clean=not bool(porcelain),
    )


def _verify_tracked_helpers(project_root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for output_name, relative in TRACKED_IMPLEMENTATION_FILES.items():
        relative_text = relative.as_posix()
        stage = _run_git(
            project_root,
            ("ls-files", "--stage", "--error-unmatch", "--", relative_text),
        )
        if not stage.startswith("100644 "):
            raise CloseoutValidationError("closeout helper must be tracked mode 100644")
        head_blob = _run_git(project_root, ("rev-parse", f"HEAD:{relative_text}"))
        worktree_blob = _run_git(project_root, ("hash-object", "--", relative_text))
        if head_blob != worktree_blob:
            raise CloseoutValidationError("tracked closeout helper differs from HEAD")
        _unused, snapshot = _inspect_regular_file(
            project_root / relative, capture=False
        )
        result[output_name] = snapshot.sha256
    return result


def _fixed_path(project_root: Path, relative: Path) -> Path:
    current = project_root
    for component in relative.parts:
        if component in {"", ".", ".."}:
            raise CloseoutValidationError("invalid fixed artifact path")
        current = current / component
        try:
            component_stat = current.lstat()
        except OSError as exc:
            raise CloseoutValidationError("fixed artifact path is missing") from exc
        if stat.S_ISLNK(component_stat.st_mode):
            raise CloseoutValidationError("fixed artifact path contains a symlink")
    return current


def _verify_artifacts(
    project_root: Path, expected: Mapping[str, str]
) -> ArtifactEvidence:
    result: dict[str, dict[str, Any]] = {}
    snapshots: dict[str, FileSnapshot] = {}
    for name, relative in REQUIRED_ARTIFACTS.items():
        path = _fixed_path(project_root, relative)
        _unused, snapshot = _inspect_regular_file(
            path, capture=False, expected_mode=0o444
        )
        if snapshot.sha256 != expected[name]:
            raise CloseoutValidationError(f"critical artifact hash mismatch: {name}")
        if (
            snapshot.sha256 != PINNED_ARTIFACT_SHA256[name]
            or snapshot.size != PINNED_ARTIFACT_BYTES[name]
        ):
            raise CloseoutValidationError(
                f"critical artifact does not match fixed hash/size: {name}"
            )
        result[name] = {
            "sha256": snapshot.sha256,
            "bytes": snapshot.size,
            "mode": "0444",
        }
        snapshots[name] = snapshot
    return ArtifactEvidence(public=result, snapshots=snapshots)


def _validate_count(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CloseoutValidationError(f"test report {field} must be non-negative int")
    return value


def _validate_test_report(
    raw: Mapping[str, Any], *, expected_total: int | None = None
) -> dict[str, Any]:
    _require_exact_keys(raw, TEST_REPORT_KEYS, context="tracked test report")
    if raw["schema_version"] != 1 or raw["artifact_type"] != TEST_REPORT_ARTIFACT_TYPE:
        raise CloseoutValidationError("unexpected tracked test report identity")
    if raw["guard_active"] is not True:
        raise CloseoutValidationError("offline sitecustomize guard was not active")
    fields = (
        "total",
        "passed",
        "skipped",
        "failures",
        "errors",
        "expected_failures",
        "unexpected_successes",
        "test_id_count",
    )
    counts = {field: _validate_count(raw[field], field=field) for field in fields}
    if expected_total is None:
        if counts["total"] != FULL_TEST_COUNT:
            raise CloseoutValidationError(
                f"full unittest suite must execute exactly {FULL_TEST_COUNT} tests"
            )
    elif counts["total"] != expected_total:
        raise CloseoutValidationError(
            f"model-focused suite must execute exactly {expected_total} tests"
        )
    if counts["test_id_count"] != counts["total"]:
        raise CloseoutValidationError("test ID count does not match executed total")
    if counts["total"] != sum(
        counts[field]
        for field in (
            "passed",
            "skipped",
            "failures",
            "errors",
            "expected_failures",
            "unexpected_successes",
        )
    ):
        raise CloseoutValidationError("tracked test counts do not add up")
    if (
        counts["failures"]
        or counts["errors"]
        or counts["expected_failures"]
        or counts["unexpected_successes"]
    ):
        raise CloseoutValidationError("tracked unittest suite did not pass")
    if expected_total is not None and (
        counts["passed"] != expected_total
        or counts["skipped"]
        or counts["expected_failures"]
    ):
        raise CloseoutValidationError(
            "model-focused suite must pass all six tests without skips"
        )
    fingerprint = raw["test_id_sha256"]
    if not isinstance(fingerprint, str) or HEX_SHA256.fullmatch(fingerprint) is None:
        raise CloseoutValidationError("invalid aggregate test-ID fingerprint")
    if expected_total is None and fingerprint != FULL_TEST_ID_SHA256:
        raise CloseoutValidationError(
            "full unittest test-ID fingerprint does not match the fixed suite"
        )
    if expected_total is not None and fingerprint != MODEL_FOCUSED_TEST_ID_SHA256:
        raise CloseoutValidationError(
            "model-focused test-ID fingerprint does not match the fixed suite"
        )
    return {
        "total": counts["total"],
        "passed": counts["passed"],
        "skipped": counts["skipped"],
        "failures": counts["failures"],
        "errors": counts["errors"],
        "expected_failures": counts["expected_failures"],
        "unexpected_successes": counts["unexpected_successes"],
        "test_id_count": counts["test_id_count"],
        "test_id_sha256": fingerprint,
        "offline_guard_active": True,
    }


def _validated_test_output(
    evidence: TestEvidence, *, expected_total: int | None, model_focused: bool
) -> dict[str, Any]:
    expected_keys = set(TEST_PUBLIC_KEYS)
    if model_focused:
        expected_keys.add("model_runtime_interpreter_sha256")
    _require_exact_keys(
        evidence.public, expected_keys, context="tracked test evidence"
    )
    report = {
        "schema_version": 1,
        "artifact_type": TEST_REPORT_ARTIFACT_TYPE,
        "guard_active": evidence.public["offline_guard_active"],
        **{
            name: evidence.public[name]
            for name in (
                "total",
                "passed",
                "skipped",
                "failures",
                "errors",
                "expected_failures",
                "unexpected_successes",
                "test_id_count",
                "test_id_sha256",
            )
        },
    }
    validated = _validate_test_report(report, expected_total=expected_total)
    for name in ("report_sha256", "offline_guard_audit_sha256"):
        value = evidence.public[name]
        if not isinstance(value, str) or HEX_SHA256.fullmatch(value) is None:
            raise CloseoutValidationError(f"invalid tracked test evidence {name}")
        validated[name] = value
    if model_focused:
        interpreter_hash = evidence.public[
            "model_runtime_interpreter_sha256"
        ]
        if (
            not isinstance(interpreter_hash, str)
            or HEX_SHA256.fullmatch(interpreter_hash) is None
        ):
            raise CloseoutValidationError(
                "invalid isolated model-runtime interpreter hash"
            )
        validated["model_runtime_interpreter_sha256"] = interpreter_hash
    return validated


def _test_environment(
    project_root: Path, audit_path: Path
) -> dict[str, str]:
    environment: dict[str, str] = {}
    for name in ("HOME", "USER", "LOGNAME", "SHELL"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    guard_dir = project_root / "scripts" / "closeout_offline_guard"
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join((str(guard_dir), str(project_root))),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "WPG_RUN_HOST_SYSTEMD_TESTS": "0",
            "WPG_RUN_NGINX_TESTS": "0",
            "WPG_RUN_HOST_TESTS": "0",
            "WPG_CLOSEOUT_NETWORK_AUDIT": str(audit_path),
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
            "TMPDIR": "/tmp",
            "NO_PROXY": "localhost,127.0.0.0/8,::1",
            "no_proxy": "localhost,127.0.0.0/8,::1",
        }
    )
    return environment


def _create_private_file(path: Path) -> FileSnapshot:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _unused, snapshot = _inspect_regular_file(
        path, capture=False, expected_mode=0o600
    )
    return snapshot


def _inspect_model_runtime_interpreter(
    project_root: Path,
) -> tuple[Path, Path, FileSnapshot]:
    _fixed_path(project_root, MODEL_RUNTIME_PYTHON.parent)
    invocation_path = project_root / MODEL_RUNTIME_PYTHON
    try:
        link_before = invocation_path.lstat()
        invocation_target = os.readlink(invocation_path)
        resolved_path = Path(os.path.realpath(invocation_path))
    except OSError as exc:
        raise CloseoutValidationError(
            "fixed isolated model-runtime interpreter is unavailable"
        ) from exc
    if (
        not stat.S_ISLNK(link_before.st_mode)
        or invocation_target != "python3.12"
        or resolved_path != MODEL_RUNTIME_RESOLVED_PYTHON
    ):
        raise CloseoutValidationError(
            "fixed isolated model-runtime interpreter path is unsafe"
        )
    secondary = invocation_path.parent / "python3.12"
    try:
        secondary_stat = secondary.lstat()
        secondary_target = os.readlink(secondary)
    except OSError as exc:
        raise CloseoutValidationError(
            "fixed isolated model-runtime interpreter chain is unavailable"
        ) from exc
    if (
        not stat.S_ISLNK(secondary_stat.st_mode)
        or secondary_target != os.fspath(MODEL_RUNTIME_RESOLVED_PYTHON)
    ):
        raise CloseoutValidationError(
            "fixed isolated model-runtime interpreter chain is unsafe"
        )
    _unused, snapshot = _inspect_regular_file(
        resolved_path,
        capture=False,
        expected_mode=MODEL_RUNTIME_INTERPRETER_MODE,
        require_current_owner=False,
    )
    if (
        snapshot.sha256 != MODEL_RUNTIME_INTERPRETER_SHA256
        or snapshot.size != MODEL_RUNTIME_INTERPRETER_BYTES
    ):
        raise CloseoutValidationError(
            "fixed isolated model-runtime interpreter hash/size mismatch"
        )
    try:
        link_after = invocation_path.lstat()
        resolved_after = Path(os.path.realpath(invocation_path))
    except OSError as exc:
        raise CloseoutValidationError(
            "fixed isolated model-runtime interpreter changed"
        ) from exc
    if (
        _stat_identity(link_before) != _stat_identity(link_after)
        or resolved_after != resolved_path
    ):
        raise CloseoutValidationError(
            "fixed isolated model-runtime interpreter changed"
        )
    return invocation_path, resolved_path, snapshot


def _run_test_suite(
    project_root: Path,
    workspace: Path,
    *,
    suite: str,
    interpreter: Path,
    report_filename: str,
    audit_filename: str,
    expected_total: int | None,
    interpreter_resolved_path: Path | None = None,
    interpreter_snapshot: FileSnapshot | None = None,
) -> TestEvidence:
    if suite not in {"full", "model-focused"}:
        raise CloseoutValidationError("invalid fixed closeout test suite")
    report_path = workspace / report_filename
    audit_path = workspace / audit_filename
    audit_initial = _create_private_file(audit_path)
    runner = project_root / TRACKED_HELPERS["test_runner_sha256"]
    try:
        completed = subprocess.run(
            [
                os.fspath(interpreter),
                os.fspath(runner),
                "--suite",
                suite,
                "--output",
                os.fspath(report_path),
            ],
            cwd=project_root,
            env=_test_environment(project_root, audit_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1800,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CloseoutValidationError("tracked closeout test runner failed") from exc
    report, report_snapshot = _load_json_regular(
        report_path, expected_mode=0o600, maximum_bytes=32 * 1024
    )
    public = _validate_test_report(report, expected_total=expected_total)
    audit_raw, audit_snapshot = _inspect_regular_file(
        audit_path,
        capture=True,
        expected_mode=0o600,
        maximum_bytes=1024 * 1024,
    )
    assert audit_raw is not None
    if (
        audit_snapshot.device != audit_initial.device
        or audit_snapshot.inode != audit_initial.inode
    ):
        raise CloseoutValidationError("offline-guard audit file was replaced")
    blocked_attempts = sum(bool(line) for line in audit_raw.splitlines())
    if blocked_attempts:
        raise CloseoutValidationError("non-loopback socket attempt was blocked")
    if completed.returncode != 0:
        raise CloseoutValidationError("tracked closeout unittest suite failed")
    public = {
        **public,
        "report_sha256": report_snapshot.sha256,
        "offline_guard_audit_sha256": audit_snapshot.sha256,
    }
    return TestEvidence(
        public=public,
        report_path=report_path,
        report_snapshot=report_snapshot,
        audit_path=audit_path,
        audit_snapshot=audit_snapshot,
        interpreter_path=(interpreter if interpreter_snapshot is not None else None),
        interpreter_resolved_path=interpreter_resolved_path,
        interpreter_snapshot=interpreter_snapshot,
    )


def _run_full_test_suite(project_root: Path, workspace: Path) -> TestEvidence:
    return _run_test_suite(
        project_root,
        workspace,
        suite="full",
        interpreter=Path(sys.executable),
        report_filename="full-unittest-report.json",
        audit_filename="full-nonloopback-socket-attempts.log",
        expected_total=None,
    )


def _run_model_focused_suite(project_root: Path, workspace: Path) -> TestEvidence:
    interpreter, resolved, interpreter_snapshot = (
        _inspect_model_runtime_interpreter(project_root)
    )
    evidence = _run_test_suite(
        project_root,
        workspace,
        suite="model-focused",
        interpreter=interpreter,
        report_filename="model-focused-report.json",
        audit_filename="model-focused-nonloopback-socket-attempts.log",
        expected_total=MODEL_FOCUSED_TEST_COUNT,
        interpreter_resolved_path=resolved,
        interpreter_snapshot=interpreter_snapshot,
    )
    return TestEvidence(
        public={
            **evidence.public,
            "model_runtime_interpreter_sha256": interpreter_snapshot.sha256,
        },
        report_path=evidence.report_path,
        report_snapshot=evidence.report_snapshot,
        audit_path=evidence.audit_path,
        audit_snapshot=evidence.audit_snapshot,
        interpreter_path=evidence.interpreter_path,
        interpreter_resolved_path=evidence.interpreter_resolved_path,
        interpreter_snapshot=evidence.interpreter_snapshot,
    )


def _reverify_test_evidence(evidence: TestEvidence) -> None:
    _unused, report = _inspect_regular_file(
        evidence.report_path, capture=False, expected_mode=0o600
    )
    audit_raw, audit = _inspect_regular_file(
        evidence.audit_path,
        capture=True,
        expected_mode=0o600,
        maximum_bytes=1024 * 1024,
    )
    if report != evidence.report_snapshot or audit != evidence.audit_snapshot:
        raise CloseoutValidationError("tracked test evidence changed after execution")
    assert audit_raw is not None
    if any(audit_raw.splitlines()):
        raise CloseoutValidationError("offline-guard audit gained blocked attempts")
    interpreter_values = (
        evidence.interpreter_path,
        evidence.interpreter_resolved_path,
        evidence.interpreter_snapshot,
    )
    if any(value is not None for value in interpreter_values):
        if any(value is None for value in interpreter_values):
            raise CloseoutValidationError("incomplete model interpreter evidence")
        assert evidence.interpreter_path is not None
        assert evidence.interpreter_resolved_path is not None
        assert evidence.interpreter_snapshot is not None
        if Path(os.path.realpath(evidence.interpreter_path)) != evidence.interpreter_resolved_path:
            raise CloseoutValidationError("model interpreter path changed")
        _unused, current = _inspect_regular_file(
            evidence.interpreter_resolved_path,
            capture=False,
            require_current_owner=False,
        )
        if current != evidence.interpreter_snapshot:
            raise CloseoutValidationError("model interpreter changed")


def _systemd_environment() -> dict[str, str]:
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "SYSTEMD_PAGER": "",
        "SYSTEMD_COLORS": "0",
    }
    for name in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _systemctl_show() -> str:
    properties = (
        "ActiveState",
        "SubState",
        "UnitFileState",
        "MainPID",
        "NRestarts",
        "Result",
        "NeedDaemonReload",
    )
    command = [str(SYSTEMCTL_BINARY), "--user", "show", SERVICE_UNIT, "--no-pager"]
    for name in properties:
        command.extend(("--property", name))
    try:
        completed = subprocess.run(
            command,
            env=_systemd_environment(),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CloseoutValidationError("fixed systemctl --user inspection failed") from exc
    return completed.stdout


def _ss_listeners() -> str:
    try:
        completed = subprocess.run(
            [
                str(SS_BINARY),
                "-H",
                "-ltn",
                f"sport = :{HEALTH_PORT}",
            ],
            env=_systemd_environment(),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CloseoutValidationError("fixed listener inspection failed") from exc
    return completed.stdout


def _parse_listener_snapshot(raw: str) -> dict[str, Any]:
    listeners: set[str] = set()
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0] != "LISTEN":
            raise CloseoutValidationError("unexpected fixed listener output")
        local = fields[3]
        if local.startswith("["):
            match = re.fullmatch(r"\[([^]]+)\]:(\d+)", local)
            if match is None:
                raise CloseoutValidationError("invalid bracketed listener address")
            address_text, port_text = match.groups()
        else:
            address_text, separator, port_text = local.rpartition(":")
            if not separator or not address_text:
                raise CloseoutValidationError("invalid listener address")
        try:
            address = ipaddress.ip_address(address_text.split("%", 1)[0])
            port = int(port_text)
        except (ValueError, TypeError) as exc:
            raise CloseoutValidationError("invalid numeric listener address") from exc
        if port != HEALTH_PORT or not address.is_loopback:
            raise CloseoutValidationError(
                "production listener is not restricted to loopback"
            )
        listeners.add(f"{address.compressed}:{port}")
    if not listeners:
        raise CloseoutValidationError("production listener was not found")
    return {"listener_scope": "loopback_only", "listeners": sorted(listeners)}


def _fetch_loopback_health() -> bytes:
    connection = http.client.HTTPConnection(HEALTH_HOST, HEALTH_PORT, timeout=5)
    try:
        connection.request(
            "GET",
            HEALTH_PATH,
            headers={"Accept": "application/json", "Connection": "close"},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise CloseoutValidationError("loopback health did not return HTTP 200")
        body = response.read(2 * 1024 * 1024 + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise CloseoutValidationError("fixed loopback health request failed") from exc
    finally:
        connection.close()
    if len(body) > 2 * 1024 * 1024:
        raise CloseoutValidationError("loopback health response is too large")
    return body


def _parse_systemd_snapshot(raw: str) -> dict[str, Any]:
    expected = {
        "ActiveState",
        "SubState",
        "UnitFileState",
        "MainPID",
        "NRestarts",
        "Result",
        "NeedDaemonReload",
    }
    values: dict[str, str] = {}
    for line in raw.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in expected or key in values:
            raise CloseoutValidationError("unexpected fixed systemctl output")
        values[key] = value
    _require_exact_keys(values, expected, context="systemctl snapshot")
    if (
        values["ActiveState"] != "active"
        or values["SubState"] != "running"
        or values["UnitFileState"] != "enabled"
        or values["Result"] != "success"
        or values["NeedDaemonReload"] != "no"
    ):
        raise CloseoutValidationError("production user service is not fully active")
    try:
        main_pid = int(values["MainPID"])
        nrestarts = int(values["NRestarts"])
    except ValueError as exc:
        raise CloseoutValidationError("invalid numeric systemctl state") from exc
    if main_pid <= 0 or nrestarts < 0:
        raise CloseoutValidationError("invalid production process state")
    return {
        "active": True,
        "enabled": True,
        "main_pid": main_pid,
        "nrestarts": nrestarts,
        "result": "success",
        "need_daemon_reload": False,
    }


def _optional_sha256(value: Any) -> str | None:
    return value if isinstance(value, str) and HEX_SHA256.fullmatch(value) else None


def _parse_health_snapshot(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_duplicate_rejecting_object
        )
    except CloseoutValidationError:
        raise
    except (UnicodeError, TypeError, ValueError) as exc:
        raise CloseoutValidationError("invalid loopback health JSON") from exc
    if not isinstance(value, dict):
        raise CloseoutValidationError("loopback health JSON must be an object")
    if (
        value.get("status") != "ready"
        or value.get("ready") is not True
        or value.get("backend") != EXPECTED_BACKEND
    ):
        raise CloseoutValidationError("loopback health is not ready")
    checks = value.get("checks")
    runtime = value.get("runtime")
    if not isinstance(checks, dict) or not isinstance(runtime, dict):
        raise CloseoutValidationError("loopback health lacks aggregate runtime checks")
    for name in (
        "graph",
        "vectors",
        "lightrag_manifest",
        "api_config",
        "search_quota_audit",
        "worker",
        "bindings_current",
        "runtime_contract",
    ):
        if checks.get(name) is not True:
            raise CloseoutValidationError("mandatory loopback health check failed")
    for name in (
        "persistent_worker",
        "process_ready",
        "bindings_current",
        "ready",
    ):
        if runtime.get(name) is not True:
            raise CloseoutValidationError("mandatory runtime health check failed")
    verification = runtime.get("lightrag_store_verification")
    verification = verification if isinstance(verification, dict) else {}
    store_verified = bool(
        checks.get("lightrag_store_hashes") is True
        and verification.get("verified") is True
    )
    manifest_sha256 = _optional_sha256(verification.get("manifest_sha256"))
    store_binding_sha256 = _optional_sha256(
        verification.get("store_binding_sha256")
    )
    if store_verified and (
        manifest_sha256 is None or store_binding_sha256 is None
    ):
        raise CloseoutValidationError(
            "loopback health reports verified LightRAG stores without valid hashes"
        )
    return {
        "ready": True,
        "bindings_current": True,
        "lightrag_store_hashes_verified": store_verified,
        "lightrag_manifest_sha256": manifest_sha256,
        "lightrag_store_binding_sha256": store_binding_sha256,
    }


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _deployment_state() -> DeploymentEvidence:
    systemd = _parse_systemd_snapshot(_systemctl_show())
    listeners = _parse_listener_snapshot(_ss_listeners())
    health = _parse_health_snapshot(_fetch_loopback_health())
    return DeploymentEvidence(
        active=True,
        enabled=True,
        ready=True,
        bindings_current=True,
        lightrag_store_hashes_verified=health[
            "lightrag_store_hashes_verified"
        ],
        main_pid=systemd["main_pid"],
        nrestarts=systemd["nrestarts"],
        lightrag_manifest_sha256=health["lightrag_manifest_sha256"],
        lightrag_store_binding_sha256=health[
            "lightrag_store_binding_sha256"
        ],
        systemd_snapshot_sha256=_canonical_sha256(systemd),
        health_snapshot_sha256=_canonical_sha256(health),
        listener_snapshot_sha256=_canonical_sha256(listeners),
    )


def _utc_stamp() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    return now.isoformat(timespec="microseconds").replace("+00:00", "Z"), now.strftime(
        "%Y%m%dT%H%M%S%fZ"
    )


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _exclusive_output_lock(output_root: Path) -> Iterator[None]:
    descriptor = os.open(
        output_root,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _require_sha256(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or HEX_SHA256.fullmatch(value) is None:
        raise CloseoutValidationError(f"{context} must be lowercase SHA-256")
    return value


def _validate_existing_test_group(
    value: Any,
    *,
    expected_keys: set[str],
    expected_total: int | None,
    tracked: Mapping[str, str],
    model_focused: bool,
) -> None:
    if not isinstance(value, dict):
        raise CloseoutValidationError("existing v2 test evidence must be an object")
    _require_exact_keys(value, expected_keys, context="existing v2 test evidence")
    public_keys = set(TEST_PUBLIC_KEYS)
    if model_focused:
        public_keys.add("model_runtime_interpreter_sha256")
    public = {name: value[name] for name in public_keys}
    _validated_test_output(
        TestEvidence(
            public=public,
            report_path=Path(),
            report_snapshot=FileSnapshot(0, 0, 0, 0, 0, 0, "0" * 64),
            audit_path=Path(),
            audit_snapshot=FileSnapshot(0, 0, 0, 0, 0, 0, "0" * 64),
        ),
        expected_total=expected_total,
        model_focused=model_focused,
    )
    for name in TRACKED_HELPERS:
        if value[name] != tracked[name]:
            raise CloseoutValidationError(
                "existing v2 test/helper hash binding is inconsistent"
            )


def _validate_existing_summary(
    value: Mapping[str, Any], *, directory: str
) -> str:
    _require_exact_keys(value, OUTPUT_KEYS, context="existing v2 summary")
    if (
        value["schema_version"] != OUTPUT_SCHEMA_VERSION
        or value["artifact_type"] != OUTPUT_ARTIFACT_TYPE
        or value["status"] != "aggregate_only_closeout_validation_complete"
        or value["aggregate_only"] is not True
        or value["contains_per_query_values"] is not False
    ):
        raise CloseoutValidationError("existing v2 summary has invalid identity")
    recorded_at = value["recorded_at"]
    if not isinstance(recorded_at, str):
        raise CloseoutValidationError("existing v2 summary has invalid timestamp")
    try:
        recorded = datetime.strptime(recorded_at, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise CloseoutValidationError(
            "existing v2 summary has invalid timestamp"
        ) from exc
    if recorded.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != recorded_at:
        raise CloseoutValidationError("existing v2 summary timestamp is not canonical")
    _require_sha256(value["request_sha256"], context="existing v2 request hash")
    git = value["git"]
    if not isinstance(git, dict):
        raise CloseoutValidationError("existing v2 summary has invalid Git binding")
    _require_exact_keys(
        git,
        {"head", "tree", "branch", "tracked_and_nonignored_worktree_clean"},
        context="existing v2 Git binding",
    )
    head = git["head"]
    if not isinstance(head, str) or HEX_COMMIT.fullmatch(head) is None:
        raise CloseoutValidationError("existing v2 summary has invalid HEAD")
    tree = git["tree"]
    if not isinstance(tree, str) or HEX_COMMIT.fullmatch(tree) is None:
        raise CloseoutValidationError("existing v2 summary has invalid tree")
    _validate_branch(git["branch"])
    if git["tracked_and_nonignored_worktree_clean"] is not True:
        raise CloseoutValidationError("existing v2 summary was not clean")

    tracked = value["tracked_implementation"]
    if not isinstance(tracked, dict):
        raise CloseoutValidationError(
            "existing v2 tracked implementation must be an object"
        )
    _require_exact_keys(
        tracked,
        TRACKED_IMPLEMENTATION_OUTPUT_KEYS,
        context="existing v2 tracked implementation",
    )
    for name, digest in tracked.items():
        _require_sha256(digest, context=f"existing v2 tracked hash {name}")

    tests = value["tests"]
    if not isinstance(tests, dict):
        raise CloseoutValidationError("existing v2 tests must be an object")
    _require_exact_keys(tests, TESTS_OUTPUT_KEYS, context="existing v2 tests")
    if (
        isinstance(tests["official_weight_inference_tests"], bool)
        or tests["official_weight_inference_tests"] != 0
    ):
        raise CloseoutValidationError(
            "existing v2 official-weight inference count must be zero"
        )
    _validate_existing_test_group(
        tests[FULL_TEST_KEY],
        expected_keys=FULL_TEST_OUTPUT_KEYS,
        expected_total=None,
        tracked=tracked,
        model_focused=False,
    )
    _validate_existing_test_group(
        tests[MODEL_TEST_KEY],
        expected_keys=MODEL_TEST_OUTPUT_KEYS,
        expected_total=MODEL_FOCUSED_TEST_COUNT,
        tracked=tracked,
        model_focused=True,
    )

    artifacts = value["critical_artifacts"]
    if not isinstance(artifacts, dict):
        raise CloseoutValidationError("existing v2 artifacts must be an object")
    _require_exact_keys(
        artifacts, set(REQUIRED_ARTIFACTS), context="existing v2 artifacts"
    )
    for name in REQUIRED_ARTIFACTS:
        artifact = artifacts[name]
        if not isinstance(artifact, dict):
            raise CloseoutValidationError("existing v2 artifact must be an object")
        _require_exact_keys(
            artifact, {"sha256", "bytes", "mode"}, context="existing v2 artifact"
        )
        if (
            artifact["sha256"] != PINNED_ARTIFACT_SHA256[name]
            or artifact["bytes"] != PINNED_ARTIFACT_BYTES[name]
            or isinstance(artifact["bytes"], bool)
            or artifact["mode"] != "0444"
        ):
            raise CloseoutValidationError(
                "existing v2 artifact does not match fixed evidence"
            )

    deployment = value["deployment"]
    if not isinstance(deployment, dict):
        raise CloseoutValidationError("existing v2 deployment must be an object")
    _require_exact_keys(
        deployment, DEPLOYMENT_OUTPUT_KEYS, context="existing v2 deployment"
    )
    for name in ("active", "enabled", "ready", "bindings_current"):
        if deployment[name] is not True:
            raise CloseoutValidationError("existing v2 deployment is not ready")
    if not isinstance(deployment["lightrag_store_hashes_verified"], bool):
        raise CloseoutValidationError("existing v2 LightRAG proof flag is invalid")
    if deployment["listener_scope"] != "loopback_only":
        raise CloseoutValidationError("existing v2 listener scope is invalid")
    for name in ("main_pid", "nrestarts"):
        number = deployment[name]
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise CloseoutValidationError("existing v2 process state is invalid")
    if deployment["main_pid"] <= 0:
        raise CloseoutValidationError("existing v2 process PID is invalid")
    for name in (
        "systemd_snapshot_sha256",
        "health_snapshot_sha256",
        "listener_snapshot_sha256",
    ):
        _require_sha256(deployment[name], context=f"existing v2 deployment {name}")
    for name in ("lightrag_manifest_sha256", "lightrag_store_binding_sha256"):
        digest = deployment[name]
        if digest is not None:
            _require_sha256(digest, context=f"existing v2 deployment {name}")
    if deployment["lightrag_store_hashes_verified"] and (
        deployment["lightrag_manifest_sha256"] is None
        or deployment["lightrag_store_binding_sha256"] is None
    ):
        raise CloseoutValidationError("existing v2 LightRAG proof hashes are missing")

    external = value["external_calls"]
    if not isinstance(external, dict):
        raise CloseoutValidationError("existing v2 external calls must be an object")
    _require_exact_keys(
        external, EXTERNAL_CALL_OUTPUT_KEYS, context="existing v2 external calls"
    )
    if external != {
        "enforcement": "tracked_sitecustomize_nonloopback_socket_guard",
        "scope": "guarded_python_test_interpreters_only_excludes_loopback_af_unix_native_children_and_later_git_transport",
        "guard_observed_nonloopback_socket_attempts": 0,
        "loopback_health_allowed": True,
        "loopback_test_traffic_allowed": True,
        "af_unix_allowed": True,
        "native_child_network_instrumented": False,
    }:
        raise CloseoutValidationError("existing v2 external-call evidence is invalid")

    excluded = value["excluded_actions"]
    if not isinstance(excluded, dict):
        raise CloseoutValidationError("existing v2 excluded actions must be an object")
    _require_exact_keys(
        excluded, EXCLUDED_ACTION_OUTPUT_KEYS, context="existing v2 excluded actions"
    )
    if excluded != {
        "live_formal500_executed": False,
        "human_evaluation_executed": False,
        "production_service_mutated": False,
        "live_external_provider_workflows_requested_by_validator": False,
        "loopback_health_probe": "read_only",
        "scope": "validator_actions_only_not_an_absolute_network_observation",
    }:
        raise CloseoutValidationError("existing v2 excluded-action evidence is invalid")

    publication = value["publication"]
    if not isinstance(publication, dict):
        raise CloseoutValidationError("existing v2 publication must be an object")
    _require_exact_keys(
        publication, PUBLICATION_OUTPUT_KEYS, context="existing v2 publication"
    )
    expected_directory = (
        f"{OUTPUT_PREFIX}{recorded.strftime('%Y%m%dT%H%M%S%fZ')}-{head[:12]}"
    )
    if directory != expected_directory or publication != {
        "directory": directory,
        "directory_mode": "0555",
        "summary_mode": "0444",
        "existing_directories_preserved": True,
        "overwrite_supported": False,
        "same_head_replay_supported": False,
    }:
        raise CloseoutValidationError("existing v2 publication binding is invalid")
    if value["threat_model_limitations"] != list(THREAT_MODEL_LIMITATIONS):
        raise CloseoutValidationError("existing v2 threat model is invalid")
    return head


def _scan_existing_success(output_root: Path, *, head: str) -> None:
    try:
        entries = sorted(output_root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise CloseoutValidationError("cannot scan prior closeout directories") from exc
    for entry in entries:
        if VERSION_DIRECTORY.fullmatch(entry.name) is None:
            continue
        try:
            directory_stat = entry.lstat()
        except OSError as exc:
            raise CloseoutValidationError("cannot inspect prior v2 closeout") from exc
        if (
            stat.S_ISLNK(directory_stat.st_mode)
            or not stat.S_ISDIR(directory_stat.st_mode)
            or stat.S_IMODE(directory_stat.st_mode) != 0o555
        ):
            raise CloseoutValidationError("prior v2 closeout directory is unsafe")
        try:
            names = sorted(child.name for child in entry.iterdir())
        except OSError as exc:
            raise CloseoutValidationError("cannot enumerate prior v2 closeout") from exc
        if names != ["summary.json"]:
            raise CloseoutValidationError("prior v2 closeout has unexpected entries")
        summary, _snapshot = _load_json_regular(
            entry / "summary.json", expected_mode=0o444, maximum_bytes=1024 * 1024
        )
        if _validate_existing_summary(summary, directory=entry.name) == head:
            raise CloseoutValidationError("HEAD already has a successful v2 closeout")


def _verify_published_directory(
    target: Path, summary: Path, *, expected_summary_sha256: str
) -> None:
    directory_stat = target.lstat()
    if (
        stat.S_ISLNK(directory_stat.st_mode)
        or not stat.S_ISDIR(directory_stat.st_mode)
        or stat.S_IMODE(directory_stat.st_mode) != 0o555
    ):
        raise CloseoutValidationError("published directory did not retain mode 0555")
    names = sorted(child.name for child in target.iterdir())
    if names != ["summary.json"]:
        raise CloseoutValidationError("published directory has unexpected entries")
    _unused, snapshot = _inspect_regular_file(
        summary, capture=False, expected_mode=0o444
    )
    if snapshot.sha256 != expected_summary_sha256:
        raise CloseoutValidationError("published summary failed final SHA-256 check")


def _mark_failed(output_root: Path, target: Path, name: str) -> None:
    try:
        target.chmod(0o700)
        failed = output_root / f".{name}.failed-{uuid.uuid4().hex}"
        if failed.exists() or failed.is_symlink():
            raise OSError("failed closeout destination collision")
        os.rename(target, failed)
        _fsync_directory(output_root)
    except OSError:
        pass


def _write_new_directory(
    output_root: Path,
    name: str,
    payload: Mapping[str, Any],
    *,
    final_checks: Callable[[], None],
) -> tuple[Path, str]:
    target = output_root / name
    if target.exists() or target.is_symlink():
        raise CloseoutValidationError("refusing to overwrite closeout directory")
    target.mkdir(mode=0o700, exist_ok=False)
    summary = target / "summary.json"
    encoded = _canonical_json(payload)
    expected_hash = hashlib.sha256(encoded).hexdigest()
    try:
        descriptor = os.open(
            summary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | O_NOFOLLOW,
            0o600,
        )
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short closeout summary write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        summary.chmod(0o444)
        _fsync_directory(target)
        target.chmod(0o555)
        _fsync_directory(output_root)
        _verify_published_directory(
            target, summary, expected_summary_sha256=expected_hash
        )
        final_checks()
        _verify_published_directory(
            target, summary, expected_summary_sha256=expected_hash
        )
    except BaseException:
        _mark_failed(output_root, target, name)
        raise
    return target, expected_hash


def _ensure_project_and_output(project_root: Path, output_root: Path) -> None:
    if Path(os.path.realpath(project_root)) != project_root:
        raise CloseoutValidationError("project root must not contain a symlink")
    project_stat = project_root.lstat()
    output_stat = output_root.lstat()
    if stat.S_ISLNK(project_stat.st_mode) or not stat.S_ISDIR(project_stat.st_mode):
        raise CloseoutValidationError("project root must be a real directory")
    if output_root != project_root / "benchmark_artifacts":
        raise CloseoutValidationError("output root must be repository benchmark_artifacts")
    if stat.S_ISLNK(output_stat.st_mode) or not stat.S_ISDIR(output_stat.st_mode):
        raise CloseoutValidationError("output root must be a real directory")


def create_closeout(
    *, input_path: Path, project_root: Path, output_root: Path
) -> tuple[Path, dict[str, Any], str]:
    project_root = Path(os.path.abspath(project_root))
    output_root = Path(os.path.abspath(output_root))
    _ensure_project_and_output(project_root, output_root)
    request_raw, request_snapshot = _load_json_regular(
        input_path, expected_mode=0o444, maximum_bytes=64 * 1024
    )
    request = _validate_request(request_raw)

    with _exclusive_output_lock(output_root):
        initial_git = _git_state(project_root)
        if not initial_git.worktree_clean:
            raise CloseoutValidationError(
                "tracked and non-ignored worktree must be clean"
            )
        if (
            initial_git.head != request["expected_head"]
            or initial_git.branch != request["expected_branch"]
        ):
            raise CloseoutValidationError("request does not match current HEAD/branch")
        _scan_existing_success(output_root, head=initial_git.head)
        helpers = _verify_tracked_helpers(project_root)
        _require_exact_keys(
            helpers,
            TRACKED_IMPLEMENTATION_OUTPUT_KEYS,
            context="tracked implementation evidence",
        )
        for name, digest in helpers.items():
            _require_sha256(digest, context=f"tracked implementation {name}")
        artifacts = _verify_artifacts(project_root, request["artifacts"])

        with tempfile.TemporaryDirectory(prefix="wpg-closeout-") as temporary:
            workspace = Path(temporary)
            full_tests = _run_full_test_suite(project_root, workspace)
            model_tests = _run_model_focused_suite(project_root, workspace)
            # Revalidate even if a mocked or future runner returns directly.
            validated_full_tests = _validated_test_output(
                full_tests, expected_total=None, model_focused=False
            )
            validated_model_tests = _validated_test_output(
                model_tests,
                expected_total=MODEL_FOCUSED_TEST_COUNT,
                model_focused=True,
            )
            helper_hashes = {
                name: helpers[name] for name in TRACKED_HELPERS
            }
            validated_full_tests.update(helper_hashes)
            validated_model_tests.update(helper_hashes)
            validated_tests = {
                FULL_TEST_KEY: validated_full_tests,
                MODEL_TEST_KEY: validated_model_tests,
                "official_weight_inference_tests": 0,
            }
            deployment = _deployment_state()

            prepublish_git = _git_state(project_root)
            if prepublish_git != initial_git:
                raise CloseoutValidationError("Git state changed during closeout")
            request_raw_again, request_again = _load_json_regular(
                input_path, expected_mode=0o444, maximum_bytes=64 * 1024
            )
            if request_again != request_snapshot or request_raw_again != request_raw:
                raise CloseoutValidationError("closeout request changed")
            if _verify_artifacts(project_root, request["artifacts"]) != artifacts:
                raise CloseoutValidationError("critical artifacts changed")
            _reverify_test_evidence(full_tests)
            _reverify_test_evidence(model_tests)
            _scan_existing_success(output_root, head=initial_git.head)

            recorded_at, stamp = _utc_stamp()
            name = f"{OUTPUT_PREFIX}{stamp}-{initial_git.head[:12]}"
            payload: dict[str, Any] = {
                "schema_version": OUTPUT_SCHEMA_VERSION,
                "artifact_type": OUTPUT_ARTIFACT_TYPE,
                "status": "aggregate_only_closeout_validation_complete",
                "recorded_at": recorded_at,
                "aggregate_only": True,
                "contains_per_query_values": False,
                "git": {
                    "head": initial_git.head,
                    "tree": initial_git.tree,
                    "branch": initial_git.branch,
                    "tracked_and_nonignored_worktree_clean": True,
                },
                "request_sha256": request_snapshot.sha256,
                "tests": validated_tests,
                "tracked_implementation": helpers,
                "critical_artifacts": artifacts.public,
                "deployment": deployment.as_dict(),
                "external_calls": {
                    "enforcement": "tracked_sitecustomize_nonloopback_socket_guard",
                    "scope": "guarded_python_test_interpreters_only_excludes_loopback_af_unix_native_children_and_later_git_transport",
                    "guard_observed_nonloopback_socket_attempts": 0,
                    "loopback_health_allowed": True,
                    "loopback_test_traffic_allowed": True,
                    "af_unix_allowed": True,
                    "native_child_network_instrumented": False,
                },
                "excluded_actions": {
                    "live_formal500_executed": False,
                    "human_evaluation_executed": False,
                    "production_service_mutated": False,
                    "live_external_provider_workflows_requested_by_validator": False,
                    "loopback_health_probe": "read_only",
                    "scope": "validator_actions_only_not_an_absolute_network_observation",
                },
                "publication": {
                    "directory": name,
                    "directory_mode": "0555",
                    "summary_mode": "0444",
                    "existing_directories_preserved": True,
                    "overwrite_supported": False,
                    "same_head_replay_supported": False,
                },
                "threat_model_limitations": list(THREAT_MODEL_LIMITATIONS),
            }

            def final_checks() -> None:
                if _git_state(project_root) != initial_git:
                    raise CloseoutValidationError(
                        "Git state changed after closeout publication"
                    )
                final_request, final_request_snapshot = _load_json_regular(
                    input_path, expected_mode=0o444, maximum_bytes=64 * 1024
                )
                if (
                    final_request != request_raw
                    or final_request_snapshot != request_snapshot
                ):
                    raise CloseoutValidationError(
                        "closeout request changed after publication"
                    )
                if _verify_artifacts(
                    project_root, request["artifacts"]
                ) != artifacts:
                    raise CloseoutValidationError(
                        "critical artifacts changed after publication"
                    )
                _reverify_test_evidence(full_tests)
                _reverify_test_evidence(model_tests)
                if _deployment_state() != deployment:
                    raise CloseoutValidationError(
                        "deployment changed after closeout publication"
                    )

            return_value = _write_new_directory(
                output_root, name, payload, final_checks=final_checks
            )
            target, summary_sha256 = return_value
            return target, payload, summary_sha256


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        target, _payload, summary_sha256 = create_closeout(
            input_path=arguments.input,
            project_root=PROJECT_ROOT,
            output_root=DEFAULT_OUTPUT_ROOT,
        )
    except (CloseoutValidationError, OSError) as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            {
                "status": "aggregate_only_closeout_validation_complete",
                "directory": str(target),
                "summary_sha256": summary_sha256,
                "guard_observed_nonloopback_socket_attempts": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
