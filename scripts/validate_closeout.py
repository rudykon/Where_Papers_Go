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
import pwd
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any, Callable, Iterator, Mapping, Sequence
import uuid

from scripts.manage_deployment import (
    PYTHON_RUNTIME_MANIFEST,
    _current_git_source_plan,
    _process_executable_identity,
    validate_python_runtime_release,
)
from where_paper_go.deployment_identity import atomic_rename_noreplace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "benchmark_artifacts"
GIT_BINARY = Path("/usr/bin/git")
SYSTEMCTL_BINARY = Path("/usr/bin/systemctl")
SS_BINARY = Path("/usr/bin/ss")
SERVICE_UNIT = "where-papers-go.service"
HEALTH_HOST = "127.0.0.1"
HEALTH_PATH = "/api/health"
EXPECTED_BACKEND = "lightrag_mix+property_graph_exact_vector+llm+search_api"
REQUEST_ARTIFACT_TYPE = "where_papers_go_aggregate_closeout_request"
OUTPUT_ARTIFACT_TYPE = "where_papers_go_aggregate_closeout_validation"
REPROOF_ARTIFACT_TYPE = "where_papers_go_post_deployment_reproof"
TEST_REPORT_ARTIFACT_TYPE = "where_papers_go_closeout_test_report"
LEGACY_OUTPUT_PREFIX = "final_delivery_validation_v3_"
LEGACY_REPROOF_PREFIX = "final_delivery_deployment_reproof_v1_"
OUTPUT_PREFIX = "final_delivery_validation_v4_"
REPROOF_PREFIX = "final_delivery_deployment_reproof_v2_"
OUTPUT_SCHEMA_VERSION = 5
REPROOF_SCHEMA_VERSION = 2
FULL_TEST_COUNT = 489
FULL_TEST_ID_SHA256 = (
    "ddc285a4a7b74373dd0cf92f2da5515899d382a16e3c31ccb3e27963565eccc4"
)
MODEL_FOCUSED_TEST_COUNT = 6
MODEL_FOCUSED_TEST_ID_SHA256 = (
    "651d59643b938f9f13712a8838a08f51efe74bb18d100fc07e8f0c825c866b94"
)
SKIPPED_TEST_ID_HASH_DOMAIN = (
    b"where-papers-go-closeout-skipped-test-ids-v1\0"
)
FULL_SKIP_ALLOWLIST_SHA256 = (
    "d970d6124c58fd064d3241a151dfc2001b2c841c003c2c2bba3dfecaf71a246b"
)
MODEL_SKIP_ALLOWLIST_SHA256 = (
    "ecbbeafb099c4e91937fc5570d6dbf6ffdde3700e59245704340e92d8d558fed"
)
FULL_ALLOWED_SKIP_TEST_IDS = (
    "test_local_model_runtime.LocalModelRuntimeIntegrationTests."
    "test_cross_encoder_provider_loads_local_safetensors",
    "test_local_model_runtime.LocalModelRuntimeIntegrationTests."
    "test_scientific_cls_provider_loads_local_safetensors",
    "test_nginx_integration.NginxIntegrationTests."
    "test_nginx_syntax_tls_auth_and_proxy_redaction",
    "test_systemd_host_integration.HostSystemdIntegrationTests."
    "test_main_process_sigkill_is_automatically_restarted_and_ready",
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
SELECTED_WHEEL_LOCK_PATH = Path(
    "deploy/python/selected-wheels-cpython-3.14.5-linux-x86_64.json"
)
UV_LOCK_PATH = Path("uv.lock")
SYSTEMD_TEMPLATE_PATH = Path(
    "deploy/systemd/where-papers-go.service.in"
)
SYSTEMD_FRAGMENT_RELATIVE = Path(
    ".config/systemd/user/where-papers-go.service"
)
SOURCE_RELEASE_ROOT_RELATIVE = Path(
    ".local/lib/where-papers-go/releases"
)
PYTHON_RUNTIME_ROOT_RELATIVE = Path(
    ".local/lib/where-papers-go/python-runtimes"
)
API_TOKEN_RELATIVE = Path(".config/where-papers-go/backend.token")
FULL_TEST_KEY = "full_unittest"
MODEL_TEST_KEY = "model_focused_4_test_double_plus_2_synthetic_safetensors"
EXPECTED_CLOSEOUT_BRANCH = "agent/aggregate-only-closeout-20260831"
HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
HEX_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
API_TOKEN = re.compile(r"[A-Za-z0-9._~-]{32,256}\Z")
AGENT_BRANCH = re.compile(r"agent/[a-z0-9][a-z0-9._/-]{0,126}\Z")
VERSION_DIRECTORY = re.compile(
    rf"{re.escape(OUTPUT_PREFIX)}\d{{8}}T\d{{12}}Z-[0-9a-f]{{12}}\Z"
)
REPROOF_VERSION_DIRECTORY = re.compile(
    rf"{re.escape(REPROOF_PREFIX)}\d{{8}}T\d{{12}}Z-[0-9a-f]{{12}}\Z"
)
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def _skipped_test_id_digest(identifiers: Sequence[str]) -> str:
    digest = hashlib.sha256()
    digest.update(SKIPPED_TEST_ID_HASH_DOMAIN)
    for identifier in sorted(set(identifiers)):
        digest.update(identifier.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


FULL_ALLOWED_SKIP_FINGERPRINTS = {
    (len(subset), _skipped_test_id_digest(subset))
    for mask in range(1 << len(FULL_ALLOWED_SKIP_TEST_IDS))
    for subset in [
        tuple(
            identifier
            for index, identifier in enumerate(FULL_ALLOWED_SKIP_TEST_IDS)
            if mask & (1 << index)
        )
    ]
}
MODEL_SKIPPED_TEST_ID_SHA256 = _skipped_test_id_digest(())

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
    "deployment_identity_sha256": Path(
        "where_paper_go/deployment_identity.py"
    ),
    "deployment_manager_sha256": Path("scripts/manage_deployment.py"),
    "deployment_manager_test_sha256": Path("tests/test_deployment.py"),
    "systemd_template_sha256": SYSTEMD_TEMPLATE_PATH,
    "selected_wheel_lock_sha256": SELECTED_WHEEL_LOCK_PATH,
    "uv_lock_sha256": UV_LOCK_PATH,
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
    "skipped_test_id_count",
    "skipped_test_id_sha256",
    "skip_allowlist_sha256",
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
REPROOF_OUTPUT_KEYS = {
    "schema_version",
    "artifact_type",
    "status",
    "recorded_at",
    "base_closeout",
    "git",
    "tracked_implementation",
    "deployment",
    "publication",
    "threat_model_limitations",
}
REPROOF_BASE_KEYS = {
    "directory",
    "summary_sha256",
    "recorded_at",
    "head",
    "tree",
    "deployment_invocation_id",
    "deployment_process_start_ticks",
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
    "skipped_test_id_count",
    "skipped_test_id_sha256",
    "skip_allowlist_sha256",
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
    "process_start_ticks",
    "systemd_invocation_id",
    "source_head",
    "source_tree",
    "source_manifest_sha256",
    "source_release",
    "source_files_verified",
    "lightrag_file_count",
    "lightrag_manifest_sha256",
    "lightrag_store_binding_sha256",
    "systemd_snapshot_sha256",
    "process_snapshot_sha256",
    "health_snapshot_sha256",
    "listener_snapshot_sha256",
    "python_runtime",
    "worker_process",
}
PYTHON_RUNTIME_VALIDATOR_KEYS = {
    "runtime",
    "manifest",
    "manifest_sha256",
    "runtime_tree_sha256",
    "python_executable",
    "python_executable_sha256",
    "python_version",
    "python_soabi",
    "python_platform",
    "import_paths",
    "elf_audit_sha256",
    "elf_file_count",
    "system_library_count",
    "system_directory_count",
    "installed_distributions_sha256",
    "installed_distribution_count",
    "installed_record_entry_count",
    "omitted_entry_point_count",
    "dependency_lock_sha256",
    "wheel_count",
    "files_verified",
}
PYTHON_RUNTIME_OUTPUT_KEYS = PYTHON_RUNTIME_VALIDATOR_KEYS | {
    "system_abi_stat_verified"
}
HEALTH_PYTHON_RUNTIME_KEYS = {
    "ready",
    "manifest_sha256",
    "runtime_tree_sha256",
    "python_executable_sha256",
    "python_version",
    "python_soabi",
    "python_platform",
    "wheel_count",
    "elf_audit_sha256",
    "system_library_count",
    "system_directory_count",
    "system_abi_stat_verified",
    "files_verified",
    "proc_exe_matches",
    "process_pid",
    "process_start_ticks",
}
WORKER_INTERPRETER_KEYS = {
    "argv_exact",
    "no_site",
    "safe_path",
    "dont_write_bytecode",
}
WORKER_SOURCE_KEYS = {"head", "tree", "manifest_sha256", "files_verified"}
WORKER_PYTHON_RUNTIME_KEYS = {
    "manifest_sha256",
    "runtime_tree_sha256",
    "python_executable_sha256",
    "python_version",
    "python_soabi",
    "python_platform",
    "wheel_count",
    "elf_audit_sha256",
    "system_library_count",
    "system_directory_count",
    "files_verified",
    "proc_exe_matches",
    "system_abi_stat_verified",
}
HEALTH_WORKER_PROCESS_KEYS = {
    "exact",
    "pid",
    "start_ticks",
    "executable_sha256",
    "proc_exe_verified",
    "interpreter",
    "source",
    "python_runtime",
}
DEPLOYMENT_WORKER_PROCESS_KEYS = HEALTH_WORKER_PROCESS_KEYS | {
    "parent_pid",
    "python_executable",
    "process_snapshot_sha256",
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
    "published_from_hidden_building",
    "atomic_directory_rename",
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
    "The deployment probe binds systemd MainPID and invocation ID, the canonical drop-in-free user-unit fragment and its exact tracked deterministic render, effective hardening/filesystem/environment/ExecStart properties, /proc NoNewPrivs and race-resistant start/executable evidence, fixed passwd-home source/Python-runtime roots, cwd, exact source/runtime/offline/proxy-auth flags, a canonical owned single-link 0600 bearer-token file used for loopback health, rejected loader/OpenSSL/proxy/CA/Python injection variables, ss loopback listener ownership, the tracked selected-wheel lock, independent immutable Python runtime validation, and health process/source/runtime identity. It separately proves the worker PPid, PID/start/executable, exact interpreter argv, cwd, environment, source, runtime and system ABI before requiring an identical second deployment observation. Kernel, systemd, and filesystem observations remain local host evidence rather than remote attestation.",
    "Local modes, hashes, exclusive creation, and drift checks do not defend against an administrator with equal or greater file permissions who can rewrite evidence, anchors, code, or the clock together.",
)
REPROOF_THREAT_MODEL_LIMITATIONS = (
    "This append-only record independently re-observes deployment state for an immutable base closeout; it does not rerun the base test suites or rewrite the base summary.",
    "A same-HEAD service restart or redeployment is intentionally supported by creating another immutable reproof directory, never by updating an existing closeout or reproof.",
    THREAT_MODEL_LIMITATIONS[-2],
    THREAT_MODEL_LIMITATIONS[-1],
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
class BaseCloseoutEvidence:
    directory: Path
    summary_path: Path
    summary_snapshot: FileSnapshot
    summary: Mapping[str, Any]
    git: GitState


@dataclass(frozen=True)
class DeploymentEvidence:
    active: bool
    enabled: bool
    ready: bool
    bindings_current: bool
    lightrag_store_hashes_verified: bool
    listener_scope: str
    main_pid: int
    nrestarts: int
    process_start_ticks: int
    systemd_invocation_id: str
    source_head: str
    source_tree: str
    source_manifest_sha256: str
    source_release: str
    source_files_verified: bool
    lightrag_file_count: int
    lightrag_manifest_sha256: str
    lightrag_store_binding_sha256: str
    systemd_snapshot_sha256: str
    process_snapshot_sha256: str
    health_snapshot_sha256: str
    listener_snapshot_sha256: str
    python_runtime: Mapping[str, Any]
    worker_process: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "enabled": self.enabled,
            "ready": self.ready,
            "bindings_current": self.bindings_current,
            "lightrag_store_hashes_verified": (
                self.lightrag_store_hashes_verified
            ),
            "listener_scope": self.listener_scope,
            "main_pid": self.main_pid,
            "nrestarts": self.nrestarts,
            "process_start_ticks": self.process_start_ticks,
            "systemd_invocation_id": self.systemd_invocation_id,
            "source_head": self.source_head,
            "source_tree": self.source_tree,
            "source_manifest_sha256": self.source_manifest_sha256,
            "source_release": self.source_release,
            "source_files_verified": self.source_files_verified,
            "lightrag_file_count": self.lightrag_file_count,
            "lightrag_manifest_sha256": self.lightrag_manifest_sha256,
            "lightrag_store_binding_sha256": (
                self.lightrag_store_binding_sha256
            ),
            "systemd_snapshot_sha256": self.systemd_snapshot_sha256,
            "process_snapshot_sha256": self.process_snapshot_sha256,
            "health_snapshot_sha256": self.health_snapshot_sha256,
            "listener_snapshot_sha256": self.listener_snapshot_sha256,
            "python_runtime": dict(self.python_runtime),
            "worker_process": dict(self.worker_process),
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
    if raw["schema_version"] != 2 or raw["artifact_type"] != TEST_REPORT_ARTIFACT_TYPE:
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
        "skipped_test_id_count",
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
    if counts["skipped_test_id_count"] != counts["skipped"]:
        raise CloseoutValidationError(
            "skipped-test ID count does not match aggregate skipped count"
        )
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
    skipped_fingerprint = raw["skipped_test_id_sha256"]
    allowlist_fingerprint = raw["skip_allowlist_sha256"]
    for name, fingerprint_value in (
        ("skipped-test ID", skipped_fingerprint),
        ("skip allowlist", allowlist_fingerprint),
    ):
        if (
            not isinstance(fingerprint_value, str)
            or HEX_SHA256.fullmatch(fingerprint_value) is None
        ):
            raise CloseoutValidationError(
                f"invalid aggregate {name} fingerprint"
            )
    if expected_total is None:
        if (
            counts["skipped_test_id_count"],
            skipped_fingerprint,
        ) not in FULL_ALLOWED_SKIP_FINGERPRINTS:
            raise CloseoutValidationError(
                "full unittest skipped-test fingerprint is outside the fixed allowlist"
            )
        if allowlist_fingerprint != FULL_SKIP_ALLOWLIST_SHA256:
            raise CloseoutValidationError(
                "full unittest skip-allowlist fingerprint does not match policy"
            )
    elif (
        skipped_fingerprint != MODEL_SKIPPED_TEST_ID_SHA256
        or allowlist_fingerprint != MODEL_SKIP_ALLOWLIST_SHA256
    ):
        raise CloseoutValidationError(
            "model-focused skip fingerprints do not match the empty policy"
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
        "skipped_test_id_count": counts["skipped_test_id_count"],
        "skipped_test_id_sha256": skipped_fingerprint,
        "skip_allowlist_sha256": allowlist_fingerprint,
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
        "schema_version": 2,
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
                "skipped_test_id_count",
                "skipped_test_id_sha256",
                "skip_allowlist_sha256",
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


def _current_user_home() -> Path:
    """Return the canonical passwd home for the effective service user."""

    try:
        raw = pwd.getpwuid(os.geteuid()).pw_dir
    except KeyError as exc:
        raise CloseoutValidationError(
            "effective user has no passwd home"
        ) from exc
    home = Path(raw)
    if not raw or not home.is_absolute():
        raise CloseoutValidationError(
            "effective user passwd home is not absolute"
        )
    canonical = Path(os.path.realpath(home))
    try:
        home_info = home.lstat()
    except OSError as exc:
        raise CloseoutValidationError(
            "effective user passwd home is unavailable"
        ) from exc
    if (
        canonical != home
        or stat.S_ISLNK(home_info.st_mode)
        or not stat.S_ISDIR(home_info.st_mode)
        or home_info.st_uid != os.geteuid()
    ):
        raise CloseoutValidationError(
            "effective user passwd home is not a canonical owned directory"
        )
    return home


def _canonical_user_path(relative: Path) -> Path:
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise CloseoutValidationError("invalid fixed user-relative path")
    path = _current_user_home() / relative
    if Path(os.path.realpath(path)) != path:
        raise CloseoutValidationError(
            "fixed user path contains a symbolic-link component"
        )
    return path


def _validate_owned_user_directory_chain(
    path: Path, *, label: str, exact_leaf_mode: int | None = None
) -> None:
    """Reject replaceable passwd-home directory components."""

    home = _current_user_home()
    try:
        relative = path.relative_to(home)
    except ValueError as exc:
        raise CloseoutValidationError(f"{label} is outside passwd home") from exc
    current = home
    chain = [home]
    for component in relative.parts:
        current /= component
        chain.append(current)
    for index, directory in enumerate(chain):
        try:
            info = directory.lstat()
        except OSError as exc:
            raise CloseoutValidationError(
                f"{label} directory chain is unavailable"
            ) from exc
        mode = stat.S_IMODE(info.st_mode)
        if (
            Path(os.path.realpath(directory)) != directory
            or stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or mode & 0o022
        ):
            raise CloseoutValidationError(
                f"{label} directory chain is not owned, real, and non-writable"
            )
        if (
            index == len(chain) - 1
            and exact_leaf_mode is not None
            and mode != exact_leaf_mode
        ):
            raise CloseoutValidationError(
                f"{label} must have mode {exact_leaf_mode:04o}"
            )


def _systemctl_show() -> str:
    properties = (
        "ActiveState",
        "SubState",
        "UnitFileState",
        "FragmentPath",
        "DropInPaths",
        "MainPID",
        "NRestarts",
        "Result",
        "NeedDaemonReload",
        "InvocationID",
        "ControlGroup",
        "ExecMainStartTimestampMonotonic",
        "NoNewPrivileges",
        "PrivateTmp",
        "ProtectSystem",
        "ProtectHome",
        "WorkingDirectory",
        "ReadOnlyPaths",
        "ReadWritePaths",
        "Environment",
        "UnsetEnvironment",
        "ExecStart",
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


def _ss_listeners(port: int) -> str:
    try:
        completed = subprocess.run(
            [
                str(SS_BINARY),
                "-H",
                "-ltnp",
                f"sport = :{port}",
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


def _parse_listener_snapshot(
    raw: str, *, expected_host: str, expected_port: int, expected_pid: int
) -> dict[str, Any]:
    listeners: set[str] = set()
    owner_pids: set[int] = set()
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
        if port != expected_port:
            raise CloseoutValidationError("production listener port is unexpected")
        if expected_host in {"localhost", "127.0.0.1"}:
            if not address.is_loopback:
                raise CloseoutValidationError(
                    "production listener does not match the configured loopback host"
                )
            listener_scope = "loopback_only"
        elif expected_host == "0.0.0.0":
            raise CloseoutValidationError(
                "v4 production closeout rejects an IPv4 wildcard listener"
            )
        else:
            raise CloseoutValidationError("unsupported production listener host")
        matched_pids = {
            int(value) for value in re.findall(r"\bpid=(\d+)\b", line)
        }
        if matched_pids != {expected_pid}:
            raise CloseoutValidationError(
                "production listener owner does not equal systemd MainPID"
            )
        owner_pids.update(matched_pids)
        listeners.add(f"{address.compressed}:{port}")
    if not listeners:
        raise CloseoutValidationError("production listener was not found")
    return {
        "listener_scope": listener_scope,
        "listeners": sorted(listeners),
        "owner_pids": sorted(owner_pids),
    }


def _read_closeout_api_token(path_text: str) -> tuple[str, str]:
    path = Path(path_text)
    expected = _canonical_user_path(API_TOKEN_RELATIVE)
    if (
        path != expected
        or not path.is_absolute()
        or Path(os.path.realpath(path)) != path
    ):
        raise CloseoutValidationError(
            "WPG_API_TOKEN_FILE is not the fixed passwd-home token path"
        )
    _validate_owned_user_directory_chain(
        path.parent,
        label="WPG_API_TOKEN_FILE parent",
        exact_leaf_mode=0o700,
    )
    try:
        path_before = path.lstat()
    except OSError as exc:
        raise CloseoutValidationError(
            "WPG_API_TOKEN_FILE is unavailable"
        ) from exc
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | O_NOFOLLOW
        | O_NONBLOCK
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CloseoutValidationError(
            "WPG_API_TOKEN_FILE cannot be opened safely"
        ) from exc
    try:
        before = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        path_identity = (
            path_before.st_dev,
            path_before.st_ino,
            path_before.st_mode,
            path_before.st_uid,
            path_before.st_nlink,
            path_before.st_size,
            path_before.st_mtime_ns,
            path_before.st_ctime_ns,
        )
        if identity_before != path_identity:
            raise CloseoutValidationError(
                "WPG_API_TOKEN_FILE changed between lstat and open"
            )
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > 257
        ):
            raise CloseoutValidationError(
                "WPG_API_TOKEN_FILE must be owned regular nlink=1 mode 0600"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, min(258, 258 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > 257:
                raise CloseoutValidationError(
                    "WPG_API_TOKEN_FILE exceeds its size limit"
                )
        after = os.fstat(descriptor)
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_after != identity_before:
            raise CloseoutValidationError(
                "WPG_API_TOKEN_FILE changed while being read"
            )
    finally:
        os.close(descriptor)
    try:
        path_after = path.lstat()
    except OSError as exc:
        raise CloseoutValidationError(
            "WPG_API_TOKEN_FILE disappeared after read"
        ) from exc
    path_after_identity = (
        path_after.st_dev,
        path_after.st_ino,
        path_after.st_mode,
        path_after.st_uid,
        path_after.st_nlink,
        path_after.st_size,
        path_after.st_mtime_ns,
        path_after.st_ctime_ns,
    )
    if path_after_identity != identity_before:
        raise CloseoutValidationError(
            "WPG_API_TOKEN_FILE path changed after read"
        )
    raw = b"".join(chunks)
    if len(raw) != before.st_size:
        raise CloseoutValidationError("WPG_API_TOKEN_FILE read was incomplete")
    try:
        token_raw = raw[:-1] if raw.endswith(b"\n") else raw
        token = token_raw.decode("ascii")
    except UnicodeError as exc:
        raise CloseoutValidationError(
            "WPG_API_TOKEN_FILE token is not safe ASCII"
        ) from exc
    if (
        API_TOKEN.fullmatch(token) is None
        or raw not in {token_raw, token_raw + b"\n"}
    ):
        raise CloseoutValidationError(
            "WPG_API_TOKEN_FILE must contain one 32..256 character safe token with at most one LF"
        )
    return token, hashlib.sha256(raw).hexdigest()


def _fetch_loopback_health(port: int, *, bearer_token: str) -> bytes:
    connection = http.client.HTTPConnection(HEALTH_HOST, port, timeout=5)
    try:
        connection.request(
            "GET",
            HEALTH_PATH,
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer " + bearer_token,
                "Connection": "close",
            },
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
        "FragmentPath",
        "DropInPaths",
        "MainPID",
        "NRestarts",
        "Result",
        "NeedDaemonReload",
        "InvocationID",
        "ControlGroup",
        "ExecMainStartTimestampMonotonic",
        "NoNewPrivileges",
        "PrivateTmp",
        "ProtectSystem",
        "ProtectHome",
        "WorkingDirectory",
        "ReadOnlyPaths",
        "ReadWritePaths",
        "Environment",
        "UnsetEnvironment",
        "ExecStart",
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
    expected_fragment = _canonical_user_path(SYSTEMD_FRAGMENT_RELATIVE)
    fragment = Path(values["FragmentPath"])
    if (
        not fragment.is_absolute()
        or fragment != expected_fragment
        or Path(os.path.realpath(fragment)) != fragment
    ):
        raise CloseoutValidationError(
            "systemd FragmentPath is not the canonical user unit"
        )
    if values["DropInPaths"]:
        raise CloseoutValidationError(
            "systemd user unit must not have overriding drop-ins"
        )
    expected_hardening = {
        "NoNewPrivileges": "yes",
        "PrivateTmp": "yes",
        "ProtectSystem": "strict",
        "ProtectHome": "read-only",
    }
    if any(
        values[name] != expected_value
        for name, expected_value in expected_hardening.items()
    ):
        raise CloseoutValidationError(
            "effective systemd hardening properties are weakened"
        )
    working_directory = Path(values["WorkingDirectory"])
    if (
        not working_directory.is_absolute()
        or Path(os.path.realpath(working_directory)) != working_directory
        or not values["ReadOnlyPaths"]
        or not values["ReadWritePaths"]
        or not values["Environment"]
        or not values["UnsetEnvironment"]
        or not values["ExecStart"]
    ):
        raise CloseoutValidationError(
            "effective systemd execution properties are incomplete"
        )
    try:
        main_pid = int(values["MainPID"])
        nrestarts = int(values["NRestarts"])
        start_monotonic = int(values["ExecMainStartTimestampMonotonic"])
    except ValueError as exc:
        raise CloseoutValidationError("invalid numeric systemctl state") from exc
    if main_pid <= 0 or nrestarts < 0 or start_monotonic <= 0:
        raise CloseoutValidationError("invalid production process state")
    invocation_id = values["InvocationID"]
    if re.fullmatch(r"[0-9a-f]{32}", invocation_id) is None:
        raise CloseoutValidationError("invalid systemd invocation identity")
    if not values["ControlGroup"].endswith(f"/{SERVICE_UNIT}"):
        raise CloseoutValidationError("systemd control group is not bound to the unit")
    return {
        "active": True,
        "enabled": True,
        "main_pid": main_pid,
        "nrestarts": nrestarts,
        "invocation_id": invocation_id,
        "control_group": values["ControlGroup"],
        "exec_main_start_monotonic": start_monotonic,
        "result": "success",
        "need_daemon_reload": False,
        "fragment_path": str(fragment),
        "drop_in_paths": [],
        "hardening": {
            "no_new_privileges": True,
            "private_tmp": True,
            "protect_system": "strict",
            "protect_home": "read-only",
        },
        "working_directory": str(working_directory),
        "read_only_paths": values["ReadOnlyPaths"],
        "read_write_paths": values["ReadWritePaths"],
        "environment": values["Environment"],
        "unset_environment": values["UnsetEnvironment"],
        "exec_start": values["ExecStart"],
    }


def _required_sha256(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or HEX_SHA256.fullmatch(value) is None:
        raise CloseoutValidationError(f"{context} must be lowercase SHA-256")
    return value


def _required_commit(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or HEX_COMMIT.fullmatch(value) is None:
        raise CloseoutValidationError(f"{context} must be lowercase 40-hex")
    return value


def _parse_health_python_runtime(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CloseoutValidationError(
            "loopback health lacks an immutable Python runtime proof"
        )
    _require_exact_keys(
        value,
        HEALTH_PYTHON_RUNTIME_KEYS,
        context="loopback health Python runtime",
    )
    if not (
        value["ready"] is True
        and value["files_verified"] is True
        and value["proc_exe_matches"] is True
        and value["system_abi_stat_verified"] is True
    ):
        raise CloseoutValidationError(
            "loopback health Python runtime integrity is not true"
        )
    for name in (
        "manifest_sha256",
        "runtime_tree_sha256",
        "python_executable_sha256",
        "elf_audit_sha256",
    ):
        _required_sha256(
            value[name], context=f"health Python runtime {name}"
        )
    for name in ("python_version", "python_soabi", "python_platform"):
        if not isinstance(value[name], str) or not value[name]:
            raise CloseoutValidationError(
                f"health Python runtime {name} is invalid"
            )
    for name in (
        "wheel_count",
        "system_library_count",
        "system_directory_count",
        "process_pid",
        "process_start_ticks",
    ):
        number = value[name]
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise CloseoutValidationError(
                f"health Python runtime {name} is invalid"
            )
    return dict(value)


def _parse_health_worker_process(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CloseoutValidationError(
            "loopback health lacks an exact worker process proof"
        )
    _require_exact_keys(
        value,
        HEALTH_WORKER_PROCESS_KEYS,
        context="loopback health worker process",
    )
    if value["exact"] is not True or value["proc_exe_verified"] is not True:
        raise CloseoutValidationError(
            "loopback health worker process identity is not exact"
        )
    for name in ("pid", "start_ticks"):
        number = value[name]
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise CloseoutValidationError(
                f"loopback health worker {name} is invalid"
            )
    _required_sha256(
        value["executable_sha256"], context="health worker executable hash"
    )

    interpreter = value["interpreter"]
    if not isinstance(interpreter, dict):
        raise CloseoutValidationError("health worker interpreter proof is invalid")
    _require_exact_keys(
        interpreter,
        WORKER_INTERPRETER_KEYS,
        context="health worker interpreter",
    )
    if any(interpreter[name] is not True for name in WORKER_INTERPRETER_KEYS):
        raise CloseoutValidationError(
            "health worker interpreter flags are not exact"
        )

    source = value["source"]
    if not isinstance(source, dict):
        raise CloseoutValidationError("health worker source proof is invalid")
    _require_exact_keys(source, WORKER_SOURCE_KEYS, context="health worker source")
    _required_commit(source["head"], context="health worker source HEAD")
    _required_commit(source["tree"], context="health worker source tree")
    _required_sha256(
        source["manifest_sha256"], context="health worker source manifest hash"
    )
    if source["files_verified"] is not True:
        raise CloseoutValidationError("health worker source files are not verified")

    python_runtime = value["python_runtime"]
    if not isinstance(python_runtime, dict):
        raise CloseoutValidationError("health worker Python runtime proof is invalid")
    _require_exact_keys(
        python_runtime,
        WORKER_PYTHON_RUNTIME_KEYS,
        context="health worker Python runtime",
    )
    for name in (
        "manifest_sha256",
        "runtime_tree_sha256",
        "python_executable_sha256",
        "elf_audit_sha256",
    ):
        _required_sha256(
            python_runtime[name], context=f"health worker runtime {name}"
        )
    for name in ("python_version", "python_soabi", "python_platform"):
        if not isinstance(python_runtime[name], str) or not python_runtime[name]:
            raise CloseoutValidationError(
                f"health worker runtime {name} is invalid"
            )
    for name in (
        "wheel_count",
        "system_library_count",
        "system_directory_count",
    ):
        number = python_runtime[name]
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise CloseoutValidationError(
                f"health worker runtime {name} is invalid"
            )
    for name in (
        "files_verified",
        "proc_exe_matches",
        "system_abi_stat_verified",
    ):
        if python_runtime[name] is not True:
            raise CloseoutValidationError(
                f"health worker runtime {name} is not true"
            )
    if python_runtime["python_executable_sha256"] != value["executable_sha256"]:
        raise CloseoutValidationError(
            "health worker process and runtime executable hashes disagree"
        )
    return {
        **{name: value[name] for name in HEALTH_WORKER_PROCESS_KEYS},
        "interpreter": dict(interpreter),
        "source": dict(source),
        "python_runtime": dict(python_runtime),
    }


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
        "worker_process_identity",
        "bindings_current",
        "runtime_contract",
        "source_identity",
        "python_runtime_identity",
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
    runtime_manifest = runtime.get("runtime_manifest")
    if not isinstance(verification, dict) or not (
        checks.get("lightrag_store_hashes") is True
        and verification.get("required") is True
        and verification.get("verified") is True
        and verification.get("file_count") == 6
        and isinstance(runtime_manifest, dict)
        and runtime_manifest.get("ready") is True
        and runtime_manifest.get("actual_sha256")
        == verification.get("manifest_sha256")
    ):
        raise CloseoutValidationError(
            "loopback health lacks a true six-file LightRAG integrity proof"
        )
    manifest_sha256 = _required_sha256(
        verification.get("manifest_sha256"),
        context="LightRAG manifest hash",
    )
    store_binding_sha256 = _required_sha256(
        verification.get("store_binding_sha256"),
        context="LightRAG store binding hash",
    )
    source = value.get("source")
    if not isinstance(source, dict) or not (
        source.get("ready") is True
        and source.get("files_verified") is True
    ):
        raise CloseoutValidationError(
            "loopback health lacks a verified immutable source release"
        )
    source_head = _required_commit(
        source.get("head"), context="health source HEAD"
    )
    source_tree = _required_commit(
        source.get("tree"), context="health source tree"
    )
    source_manifest_sha256 = _required_sha256(
        source.get("manifest_sha256"), context="health source manifest hash"
    )
    python_runtime = _parse_health_python_runtime(value.get("python_runtime"))
    worker_process = _parse_health_worker_process(runtime.get("worker_process"))
    process_pid = source.get("process_pid")
    process_start_ticks = source.get("process_start_ticks")
    for name, number in (
        ("process PID", process_pid),
        ("process start ticks", process_start_ticks),
    ):
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise CloseoutValidationError(f"health source {name} is invalid")
    if worker_process["pid"] == process_pid:
        raise CloseoutValidationError(
            "health worker PID must differ from the service MainPID"
        )
    if worker_process["source"] != {
        "head": source_head,
        "tree": source_tree,
        "manifest_sha256": source_manifest_sha256,
        "files_verified": True,
    }:
        raise CloseoutValidationError(
            "health worker and parent source identities disagree"
        )
    worker_runtime_expected = {
        name: python_runtime[name]
        for name in WORKER_PYTHON_RUNTIME_KEYS
    }
    if worker_process["python_runtime"] != worker_runtime_expected:
        raise CloseoutValidationError(
            "health worker and parent Python runtime identities disagree"
        )
    return {
        "ready": True,
        "bindings_current": True,
        "lightrag_store_hashes_verified": True,
        "lightrag_file_count": 6,
        "lightrag_manifest_sha256": manifest_sha256,
        "lightrag_store_binding_sha256": store_binding_sha256,
        "source_head": source_head,
        "source_tree": source_tree,
        "source_manifest_sha256": source_manifest_sha256,
        "source_files_verified": True,
        "process_pid": process_pid,
        "process_start_ticks": process_start_ticks,
        "python_runtime": python_runtime,
        "worker_process": worker_process,
    }


def _read_proc_bytes(path: Path, *, maximum_bytes: int) -> bytes:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CloseoutValidationError("cannot inspect the service process") from exc
    if len(raw) > maximum_bytes:
        raise CloseoutValidationError("systemd MainPID metadata is too large")
    return raw


def _proc_start_ticks(raw: bytes) -> int:
    try:
        text = raw.decode("ascii")
        close = text.rindex(")")
        fields = text[close + 2 :].split()
        value = int(fields[19])
    except (UnicodeError, ValueError, IndexError) as exc:
        raise CloseoutValidationError("invalid /proc process stat") from exc
    if value <= 0:
        raise CloseoutValidationError("invalid /proc process start ticks")
    return value


def _proc_parent_pid(raw: bytes) -> int:
    try:
        text = raw.decode("ascii")
        close = text.rindex(")")
        fields = text[close + 2 :].split()
        value = int(fields[1])
    except (UnicodeError, ValueError, IndexError) as exc:
        raise CloseoutValidationError("invalid /proc process stat") from exc
    if value <= 0:
        raise CloseoutValidationError("invalid /proc process parent PID")
    return value


def _proc_no_new_privileges(raw: bytes) -> bool:
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeError as exc:
        raise CloseoutValidationError("invalid /proc process status") from exc
    values = [
        line.partition(":")[2].strip()
        for line in lines
        if line.partition(":")[0] == "NoNewPrivs"
    ]
    if values != ["1"]:
        raise CloseoutValidationError(
            "systemd MainPID does not have NoNewPrivs enabled"
        )
    return True


_PROCESS_ENVIRONMENT_KEYS = frozenset(
    {
        "PATH",
        "WPG_HOST",
        "WPG_PORT",
        "WPG_DATA_DIR",
        "WPG_API_CONFIG",
        "WPG_API_CACHE_DIR",
        "WPG_RESULT_CACHE_DIR",
        "WPG_QUERY_EMBEDDING_CACHE",
        "WPG_LIGHTRAG_EMBEDDING_CACHE",
        "WPG_LIGHTRAG_WORKING_DIR",
        "WPG_GRAPH_PATH",
        "WPG_TAVILY_STATE_FILE",
        "WPG_RUNTIME_GENERATION",
        "WPG_RUNTIME_MANIFEST",
        "WPG_RUNTIME_MANIFEST_SHA256",
        "WPG_STRICT_GRAPH_READ_ONLY",
        "WPG_REQUIRE_RUNTIME_SHADOW",
        "WPG_RATE_LIMIT_REQUESTS",
        "WPG_RATE_LIMIT_WINDOW_SECONDS",
        "WPG_MAX_CONCURRENT_CONNECTIONS",
        "WPG_MAX_CONCURRENT_SEARCHES",
        "WPG_REQUEST_BODY_LIMIT",
        "WPG_REQUEST_READ_TIMEOUT",
        "WPG_ALLOWED_CLIENT_CIDRS",
        "WPG_TRUST_PROXY_HEADERS",
        "WPG_TRUSTED_PROXY_CIDRS",
        "WPG_REQUIRE_API_AUTH",
        "WPG_API_TOKEN_FILE",
        "WPG_AUDIT_LOG",
        "WPG_SOURCE_HEAD",
        "WPG_SOURCE_TREE",
        "WPG_SOURCE_MANIFEST",
        "WPG_SOURCE_MANIFEST_SHA256",
        "WPG_PYTHON_RUNTIME",
        "WPG_PYTHON_RUNTIME_MANIFEST",
        "WPG_PYTHON_RUNTIME_MANIFEST_SHA256",
        "WPG_PYTHON_RUNTIME_TREE_SHA256",
        "PYTHONPATH",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONNOUSERSITE",
        "PYTHONSAFEPATH",
        "PIP_NO_INDEX",
        "PIP_DISABLE_PIP_VERSION_CHECK",
        "UV_OFFLINE",
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
    }
)
_FORBIDDEN_PROCESS_ENVIRONMENT_KEYS = frozenset(
    {
        "GCONV_PATH",
        "GLIBC_TUNABLES",
        "LD_ASSUME_KERNEL",
        "LD_AUDIT",
        "LD_BIND_NOT",
        "LD_BIND_NOW",
        "LD_DEBUG",
        "LD_DEBUG_OUTPUT",
        "LD_DYNAMIC_WEAK",
        "LD_HWCAP_MASK",
        "LD_LIBRARY_PATH",
        "LD_ORIGIN_PATH",
        "LD_PREFER_MAP_32BIT_EXEC",
        "LD_PRELOAD",
        "LD_PROFILE",
        "LD_SHOW_AUXV",
        "LD_TRACE_LOADED_OBJECTS",
        "OPENSSL_CONF",
        "OPENSSL_CONF_INCLUDE",
        "OPENSSL_ENGINES",
        "OPENSSL_MODULES",
        "SSLKEYLOGFILE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "AWS_CA_BUNDLE",
        "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "PYTHONBREAKPOINT",
        "PYTHONCASEOK",
        "PYTHONDEBUG",
        "PYTHONDEVMODE",
        "PYTHONDUMPREFS",
        "PYTHONEXECUTABLE",
        "PYTHONFAULTHANDLER",
        "PYTHONHASHSEED",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONMALLOC",
        "PYTHONOPTIMIZE",
        "PYTHONPLATLIBDIR",
        "PYTHONPROFILEIMPORTTIME",
        "PYTHONPYCACHEPREFIX",
        "PYTHONSTARTUP",
        "PYTHONTRACEMALLOC",
        "PYTHONUSERBASE",
        "PYTHONWARNINGS",
    }
)

_EXACT_PROCESS_SECURITY_ENVIRONMENT = {
    "PATH": "/usr/bin:/bin",
    "WPG_HOST": "127.0.0.1",
    "WPG_PORT": "8001",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
    "PYTHONSAFEPATH": "1",
    "PIP_NO_INDEX": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "UV_OFFLINE": "1",
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "WPG_STRICT_GRAPH_READ_ONLY": "1",
    "WPG_REQUIRE_RUNTIME_SHADOW": "1",
    "WPG_RATE_LIMIT_REQUESTS": "6",
    "WPG_RATE_LIMIT_WINDOW_SECONDS": "60",
    "WPG_MAX_CONCURRENT_CONNECTIONS": "64",
    "WPG_MAX_CONCURRENT_SEARCHES": "2",
    "WPG_REQUEST_BODY_LIMIT": "200000",
    "WPG_REQUEST_READ_TIMEOUT": "30",
    "WPG_AUDIT_LOG": "1",
    "WPG_REQUIRE_API_AUTH": "1",
    "WPG_TRUST_PROXY_HEADERS": "1",
    "WPG_ALLOWED_CLIENT_CIDRS": "127.0.0.0/8,::1/128",
    "WPG_TRUSTED_PROXY_CIDRS": "127.0.0.0/8,::1/128",
}


def _parse_process_environment(raw: bytes) -> dict[str, str]:
    environment: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        key, separator, value = item.partition(b"=")
        if not separator:
            raise CloseoutValidationError("invalid systemd MainPID environment")
        try:
            name = key.decode("ascii")
        except UnicodeError as exc:
            raise CloseoutValidationError(
                "invalid systemd MainPID environment name"
            ) from exc
        if (
            name in _FORBIDDEN_PROCESS_ENVIRONMENT_KEYS
            or name.startswith("LD_")
            or (
                name.startswith("PYTHON")
                and name not in _PROCESS_ENVIRONMENT_KEYS
            )
        ):
            raise CloseoutValidationError(
                f"forbidden systemd MainPID environment variable: {name}"
            )
        if name in _PROCESS_ENVIRONMENT_KEYS:
            if name in environment:
                raise CloseoutValidationError(
                    "duplicate systemd MainPID source environment"
                )
            try:
                environment[name] = value.decode("utf-8", errors="strict")
            except UnicodeError as exc:
                raise CloseoutValidationError(
                    "invalid systemd MainPID source environment"
                ) from exc
    _require_exact_keys(
        environment,
        _PROCESS_ENVIRONMENT_KEYS,
        context="systemd MainPID bound environment",
    )
    for name, expected in _EXACT_PROCESS_SECURITY_ENVIRONMENT.items():
        if environment[name] != expected:
            raise CloseoutValidationError(
                f"systemd MainPID security environment differs: {name}"
            )
    return environment


def _validated_live_unit_bindings(
    environment: Mapping[str, str],
) -> dict[str, Path]:
    path_names = (
        "WPG_DATA_DIR",
        "WPG_API_CONFIG",
        "WPG_API_CACHE_DIR",
        "WPG_RESULT_CACHE_DIR",
        "WPG_QUERY_EMBEDDING_CACHE",
        "WPG_LIGHTRAG_EMBEDDING_CACHE",
        "WPG_LIGHTRAG_WORKING_DIR",
        "WPG_GRAPH_PATH",
        "WPG_TAVILY_STATE_FILE",
        "WPG_RUNTIME_GENERATION",
        "WPG_RUNTIME_MANIFEST",
        "WPG_API_TOKEN_FILE",
    )
    paths = {name: Path(environment[name]) for name in path_names}
    if any(
        not value.is_absolute() or Path(os.path.realpath(value)) != value
        for value in paths.values()
    ):
        raise CloseoutValidationError(
            "systemd MainPID live unit binding path is not canonical"
        )
    runtime = paths["WPG_RUNTIME_GENERATION"]
    data = paths["WPG_DATA_DIR"]
    expected = {
        "WPG_API_CACHE_DIR": runtime / "api_cache",
        "WPG_RESULT_CACHE_DIR": runtime / "api_cache" / "result",
        "WPG_QUERY_EMBEDDING_CACHE": (
            runtime / "query_embedding_cache.json.gz"
        ),
        "WPG_LIGHTRAG_EMBEDDING_CACHE": (
            runtime / "lightrag_embedding_cache.json.gz"
        ),
        "WPG_LIGHTRAG_WORKING_DIR": runtime / "lightrag_storage",
        "WPG_GRAPH_PATH": data / "venue_graph.json.gz",
        "WPG_RUNTIME_MANIFEST": runtime / "runtime-shadow-manifest.json",
    }
    if any(paths[name] != value for name, value in expected.items()):
        raise CloseoutValidationError(
            "systemd MainPID live unit path bindings are inconsistent"
        )
    if paths["WPG_TAVILY_STATE_FILE"].name != ".tavily_key_pool_state.json":
        raise CloseoutValidationError(
            "systemd MainPID shared-state binding is invalid"
        )
    _required_sha256(
        environment["WPG_RUNTIME_MANIFEST_SHA256"],
        context="process runtime-shadow manifest hash",
    )
    return paths


def _process_snapshot(main_pid: int) -> dict[str, Any]:
    proc = Path("/proc") / str(main_pid)
    initial_ticks = _proc_start_ticks(
        _read_proc_bytes(proc / "stat", maximum_bytes=64 * 1024)
    )
    _proc_no_new_privileges(
        _read_proc_bytes(proc / "status", maximum_bytes=256 * 1024)
    )
    try:
        cwd = Path(os.path.realpath(proc / "cwd"))
    except OSError as exc:
        raise CloseoutValidationError("cannot inspect systemd MainPID cwd") from exc
    command = [
        item.decode("utf-8", errors="strict")
        for item in _read_proc_bytes(
            proc / "cmdline", maximum_bytes=1024 * 1024
        ).split(b"\0")
        if item
    ]
    if len(command) != 6 or command[1:] != [
        "-S",
        "-P",
        "-B",
        "-m",
        "where_paper_go.web_app",
    ]:
        raise CloseoutValidationError(
            "systemd MainPID command flags are not the fixed web application invocation"
        )
    try:
        executable_text, executable_sha256 = _process_executable_identity(
            main_pid
        )
        executable = Path(executable_text)
    except (OSError, ValueError) as exc:
        raise CloseoutValidationError(
            "cannot inspect the systemd MainPID executable"
        ) from exc
    if not executable.is_absolute():
        raise CloseoutValidationError("systemd MainPID executable is not absolute")
    environment = _parse_process_environment(
        _read_proc_bytes(proc / "environ", maximum_bytes=4 * 1024 * 1024)
    )
    _validated_live_unit_bindings(environment)
    try:
        port = int(environment["WPG_PORT"])
    except ValueError as exc:
        raise CloseoutValidationError("invalid systemd MainPID port") from exc
    if not 1 <= port <= 65535:
        raise CloseoutValidationError("invalid systemd MainPID port")
    host = environment["WPG_HOST"]
    if host != "127.0.0.1":
        raise CloseoutValidationError(
            "systemd MainPID host is not the fixed proxy loopback"
        )
    _required_commit(environment["WPG_SOURCE_HEAD"], context="process source HEAD")
    _required_commit(environment["WPG_SOURCE_TREE"], context="process source tree")
    _required_sha256(
        environment["WPG_SOURCE_MANIFEST_SHA256"],
        context="process source manifest hash",
    )
    _required_sha256(
        environment["WPG_PYTHON_RUNTIME_MANIFEST_SHA256"],
        context="process Python runtime manifest hash",
    )
    _required_sha256(
        environment["WPG_PYTHON_RUNTIME_TREE_SHA256"],
        context="process Python runtime tree hash",
    )
    manifest = Path(environment["WPG_SOURCE_MANIFEST"])
    if not manifest.is_absolute() or manifest.parent.resolve() != cwd:
        raise CloseoutValidationError(
            "systemd MainPID cwd is not the immutable source release"
        )
    python_paths = environment["PYTHONPATH"].split(os.pathsep)
    if (
        len(python_paths) < 2
        or any(not value for value in python_paths)
        or any(not Path(value).is_absolute() for value in python_paths)
        or Path(python_paths[0]).resolve() != cwd
    ):
        raise CloseoutValidationError(
            "systemd MainPID Python import root is not source-bound"
        )
    runtime = Path(environment["WPG_PYTHON_RUNTIME"])
    runtime_manifest = Path(environment["WPG_PYTHON_RUNTIME_MANIFEST"])
    if (
        not runtime.is_absolute()
        or not runtime_manifest.is_absolute()
        or runtime_manifest != runtime / PYTHON_RUNTIME_MANIFEST
    ):
        raise CloseoutValidationError(
            "systemd MainPID Python runtime paths are not exactly bound"
        )
    _proc_no_new_privileges(
        _read_proc_bytes(proc / "status", maximum_bytes=256 * 1024)
    )
    final_ticks = _proc_start_ticks(
        _read_proc_bytes(proc / "stat", maximum_bytes=64 * 1024)
    )
    if final_ticks != initial_ticks:
        raise CloseoutValidationError("systemd MainPID changed during /proc inspection")
    return {
        "pid": main_pid,
        "start_ticks": initial_ticks,
        "cwd": str(cwd),
        "command": command,
        "executable": str(executable),
        "executable_sha256": executable_sha256,
        "no_new_privileges": True,
        "environment": environment,
        "host": host,
        "port": port,
    }


def _worker_process_snapshot(
    *,
    main_process: Mapping[str, Any],
    health_worker: Mapping[str, Any],
    python_runtime: Mapping[str, Any],
) -> dict[str, Any]:
    """Independently bind the live worker to its parent and immutable runtime."""

    worker_pid = health_worker["pid"]
    proc = Path("/proc") / str(worker_pid)
    initial_stat = _read_proc_bytes(proc / "stat", maximum_bytes=64 * 1024)
    initial_ticks = _proc_start_ticks(initial_stat)
    initial_parent = _proc_parent_pid(initial_stat)
    if initial_parent != main_process["pid"]:
        raise CloseoutValidationError(
            "worker process parent PID does not equal systemd MainPID"
        )
    try:
        cwd = Path(os.path.realpath(proc / "cwd"))
    except OSError as exc:
        raise CloseoutValidationError("cannot inspect worker process cwd") from exc
    command = [
        item.decode("utf-8", errors="strict")
        for item in _read_proc_bytes(
            proc / "cmdline", maximum_bytes=1024 * 1024
        ).split(b"\0")
        if item
    ]
    expected_command = [
        python_runtime["python_executable"],
        "-S",
        "-P",
        "-B",
        "-m",
        "where_paper_go.worker",
    ]
    if command != expected_command:
        raise CloseoutValidationError(
            "worker process command flags are not the exact fixed invocation"
        )
    try:
        executable, executable_sha256 = _process_executable_identity(worker_pid)
    except (OSError, ValueError) as exc:
        raise CloseoutValidationError(
            "cannot inspect the worker process executable"
        ) from exc
    environment = _parse_process_environment(
        _read_proc_bytes(proc / "environ", maximum_bytes=4 * 1024 * 1024)
    )
    try:
        cwd_after = Path(os.path.realpath(proc / "cwd"))
    except OSError as exc:
        raise CloseoutValidationError("cannot recheck worker process cwd") from exc
    command_after = [
        item.decode("utf-8", errors="strict")
        for item in _read_proc_bytes(
            proc / "cmdline", maximum_bytes=1024 * 1024
        ).split(b"\0")
        if item
    ]
    environment_after = _parse_process_environment(
        _read_proc_bytes(proc / "environ", maximum_bytes=4 * 1024 * 1024)
    )
    final_stat = _read_proc_bytes(proc / "stat", maximum_bytes=64 * 1024)
    final_ticks = _proc_start_ticks(final_stat)
    final_parent = _proc_parent_pid(final_stat)
    main_ticks_after = _proc_start_ticks(
        _read_proc_bytes(
            Path("/proc") / str(main_process["pid"]) / "stat",
            maximum_bytes=64 * 1024,
        )
    )
    if (
        initial_ticks != final_ticks
        or initial_parent != final_parent
        or main_ticks_after != main_process["start_ticks"]
        or cwd_after != cwd
        or command_after != command
        or environment_after != environment
    ):
        raise CloseoutValidationError(
            "parent/worker process identity changed during /proc inspection"
        )
    if (
        initial_ticks != health_worker["start_ticks"]
        or executable != python_runtime["python_executable"]
        or executable_sha256 != python_runtime["python_executable_sha256"]
        or executable_sha256 != health_worker["executable_sha256"]
        or cwd != Path(str(main_process["cwd"]))
        or environment != main_process["environment"]
    ):
        raise CloseoutValidationError(
            "worker /proc identity disagrees with parent, health, or runtime"
        )
    return {
        "pid": worker_pid,
        "parent_pid": initial_parent,
        "start_ticks": initial_ticks,
        "cwd": str(cwd),
        "command": command,
        "executable": executable,
        "executable_sha256": executable_sha256,
        "environment": environment,
    }


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _required_persistent_install_roots(
    *, source_release: Path, python_runtime: Path
) -> tuple[Path, Path]:
    source_root = _canonical_user_path(SOURCE_RELEASE_ROOT_RELATIVE)
    runtime_root = _canonical_user_path(PYTHON_RUNTIME_ROOT_RELATIVE)
    for name, root in (
        ("source release", source_root),
        ("Python runtime", runtime_root),
    ):
        _validate_owned_user_directory_chain(
            root,
            label=f"fixed persistent {name} root",
            exact_leaf_mode=0o700,
        )
        try:
            info = root.lstat()
        except OSError as exc:
            raise CloseoutValidationError(
                f"fixed persistent {name} root is unavailable"
            ) from exc
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
        ):
            raise CloseoutValidationError(
                f"fixed persistent {name} root is not an owned real directory"
            )
    if (
        source_release != Path(os.path.realpath(source_release))
        or source_release.parent != source_root
    ):
        raise CloseoutValidationError(
            "source release is not a direct child of the fixed persistent root"
        )
    if (
        python_runtime != Path(os.path.realpath(python_runtime))
        or python_runtime.parent != runtime_root
    ):
        raise CloseoutValidationError(
            "Python runtime is not a direct child of the fixed persistent root"
        )
    return source_root, runtime_root


def _render_expected_systemd_unit(
    template_raw: bytes, replacements: Mapping[str, str]
) -> bytes:
    try:
        text = template_raw.decode("utf-8")
    except UnicodeError as exc:
        raise CloseoutValidationError(
            "tracked systemd template is not UTF-8"
        ) from exc
    for name, value in replacements.items():
        marker = f"@@{name}@@"
        if marker not in text or not value or any(
            character in value for character in ("\0", "\n", "\r")
        ):
            raise CloseoutValidationError(
                "live binding cannot deterministically render the systemd unit"
            )
        text = text.replace(marker, value)
    if re.search(r"@@[A-Z0-9_]+@@", text) is not None:
        raise CloseoutValidationError(
            "tracked systemd template has unresolved bindings"
        )
    return text.encode("utf-8")


def _rendered_unit_values(rendered: bytes, directive: str) -> list[str]:
    prefix = directive + "="
    try:
        lines = rendered.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise CloseoutValidationError("rendered systemd unit is invalid") from exc
    return [line[len(prefix) :] for line in lines if line.startswith(prefix)]


def _validated_systemd_fragment(
    project_root: Path,
    *,
    systemd: Mapping[str, Any],
    process: Mapping[str, Any],
    python_runtime: Mapping[str, Any],
) -> dict[str, Any]:
    environment = process.get("environment")
    if not isinstance(environment, dict):
        raise CloseoutValidationError("process unit bindings are invalid")
    paths = _validated_live_unit_bindings(environment)
    expected_fragment = _canonical_user_path(SYSTEMD_FRAGMENT_RELATIVE)
    if systemd.get("fragment_path") != str(expected_fragment):
        raise CloseoutValidationError(
            "systemd snapshot and canonical FragmentPath disagree"
        )

    template_raw, template_snapshot = _inspect_regular_file(
        project_root / SYSTEMD_TEMPLATE_PATH,
        capture=True,
        expected_mode=0o644,
        maximum_bytes=1024 * 1024,
    )
    assert template_raw is not None
    replacements = {
        "SOURCE_RELEASE": str(process["cwd"]),
        "SOURCE_HEAD": environment["WPG_SOURCE_HEAD"],
        "SOURCE_TREE": environment["WPG_SOURCE_TREE"],
        "SOURCE_MANIFEST": environment["WPG_SOURCE_MANIFEST"],
        "SOURCE_MANIFEST_SHA256": environment[
            "WPG_SOURCE_MANIFEST_SHA256"
        ],
        "PYTHON": str(python_runtime["python_executable"]),
        "PYTHON_RUNTIME": str(python_runtime["runtime"]),
        "PYTHON_RUNTIME_MANIFEST": str(python_runtime["manifest"]),
        "PYTHON_RUNTIME_MANIFEST_SHA256": str(
            python_runtime["manifest_sha256"]
        ),
        "PYTHON_RUNTIME_TREE_SHA256": str(
            python_runtime["runtime_tree_sha256"]
        ),
        "PYTHON_IMPORT_PATH": os.pathsep.join(
            str(value) for value in python_runtime["import_paths"]
        ),
        "DATA_DIR": str(paths["WPG_DATA_DIR"]),
        "CONFIG_PATH": str(paths["WPG_API_CONFIG"]),
        "API_TOKEN_FILE": environment["WPG_API_TOKEN_FILE"],
        "RUNTIME_DIR": str(paths["WPG_RUNTIME_GENERATION"]),
        "SHARED_STATE_DIR": str(paths["WPG_TAVILY_STATE_FILE"].parent),
        "RUNTIME_MANIFEST_SHA256": environment[
            "WPG_RUNTIME_MANIFEST_SHA256"
        ],
    }
    rendered = _render_expected_systemd_unit(template_raw, replacements)
    fragment_raw, fragment_snapshot = _inspect_regular_file(
        expected_fragment,
        capture=True,
        expected_mode=0o644,
        maximum_bytes=1024 * 1024,
    )
    assert fragment_raw is not None
    if fragment_raw != rendered:
        raise CloseoutValidationError(
            "live systemd fragment bytes differ from the tracked rendered unit"
        )

    if systemd.get("working_directory") != process["cwd"]:
        raise CloseoutValidationError(
            "effective systemd WorkingDirectory differs from the live source"
        )
    expected_read_only = set(_rendered_unit_values(rendered, "ReadOnlyPaths"))
    expected_read_write = set(_rendered_unit_values(rendered, "ReadWritePaths"))
    if (
        set(str(systemd.get("read_only_paths", "")).split())
        != expected_read_only
        or set(str(systemd.get("read_write_paths", "")).split())
        != expected_read_write
    ):
        raise CloseoutValidationError(
            "effective systemd filesystem protections differ from the rendered unit"
        )
    expected_environment = set(_rendered_unit_values(rendered, "Environment"))
    expected_unset_values = _rendered_unit_values(rendered, "UnsetEnvironment")
    if (
        set(str(systemd.get("environment", "")).split())
        != expected_environment
        or len(expected_unset_values) != 1
        or set(str(systemd.get("unset_environment", "")).split())
        != set(expected_unset_values[0].split())
    ):
        raise CloseoutValidationError(
            "effective systemd environment protections differ from the rendered unit"
        )
    expected_exec_values = _rendered_unit_values(rendered, "ExecStart")
    effective_exec = str(systemd.get("exec_start", ""))
    if (
        len(expected_exec_values) != 1
        or "path=/usr/bin/env" not in effective_exec
        or f"argv[]={expected_exec_values[0]} ;" not in effective_exec
    ):
        raise CloseoutValidationError(
            "effective systemd ExecStart differs from the rendered unit"
        )
    return {
        **dict(systemd),
        "unit_template_sha256": template_snapshot.sha256,
        "fragment_sha256": fragment_snapshot.sha256,
    }


def _expected_source_manifest_sha256(
    project_root: Path, git_state: GitState
) -> str:
    """Rebuild the release manifest from Git objects, not release labels."""

    try:
        source_head, source_tree, _source_rows, payload = (
            _current_git_source_plan(project_root)
        )
    except (OSError, ValueError) as exc:
        raise CloseoutValidationError(
            "cannot derive the immutable source release from current Git objects"
        ) from exc
    if (source_head, source_tree) != (git_state.head, git_state.tree):
        raise CloseoutValidationError(
            "Git source-release plan changed during deployment inspection"
        )
    return hashlib.sha256(payload).hexdigest()


def _validated_python_runtime_identity(
    process: Mapping[str, Any], health: Mapping[str, Any]
) -> dict[str, Any]:
    """Independently prove and cross-bind runtime, process, and health."""

    environment = process.get("environment")
    if not isinstance(environment, dict):
        raise CloseoutValidationError("process Python runtime environment is invalid")
    runtime_raw = environment["WPG_PYTHON_RUNTIME"]
    manifest_raw = environment["WPG_PYTHON_RUNTIME_MANIFEST"]
    runtime = Path(runtime_raw)
    manifest = Path(manifest_raw)
    if (
        runtime != Path(os.path.realpath(runtime))
        or manifest != Path(os.path.realpath(manifest))
        or manifest != runtime / PYTHON_RUNTIME_MANIFEST
    ):
        raise CloseoutValidationError(
            "process Python runtime path is not canonical and manifest-bound"
        )
    try:
        identity = validate_python_runtime_release(
            manifest,
            expected_manifest_sha256=environment[
                "WPG_PYTHON_RUNTIME_MANIFEST_SHA256"
            ],
        )
    except (OSError, ValueError) as exc:
        raise CloseoutValidationError(
            "independent immutable Python runtime validation failed"
        ) from exc
    if not isinstance(identity, dict):
        raise CloseoutValidationError(
            "independent Python runtime validator returned invalid evidence"
        )
    _require_exact_keys(
        identity,
        PYTHON_RUNTIME_VALIDATOR_KEYS,
        context="independent Python runtime evidence",
    )
    for name in ("runtime", "manifest", "python_executable"):
        path_value = identity[name]
        if (
            not isinstance(path_value, str)
            or not Path(path_value).is_absolute()
            or Path(path_value) != Path(os.path.realpath(path_value))
        ):
            raise CloseoutValidationError(
                f"independent Python runtime {name} path is invalid"
            )
    if (
        identity["runtime"] != str(runtime)
        or identity["manifest"] != str(manifest)
        or not Path(identity["python_executable"]).is_relative_to(runtime)
        or Path(identity["python_executable"]) == runtime
    ):
        raise CloseoutValidationError(
            "independent Python runtime paths disagree with process environment"
        )
    for name in (
        "manifest_sha256",
        "runtime_tree_sha256",
        "python_executable_sha256",
        "elf_audit_sha256",
        "installed_distributions_sha256",
        "dependency_lock_sha256",
    ):
        _required_sha256(
            identity[name], context=f"independent Python runtime {name}"
        )
    for name in ("python_version", "python_soabi", "python_platform"):
        if not isinstance(identity[name], str) or not identity[name]:
            raise CloseoutValidationError(
                f"independent Python runtime {name} is invalid"
            )
    positive_counts = (
        "elf_file_count",
        "system_library_count",
        "system_directory_count",
        "installed_distribution_count",
        "installed_record_entry_count",
        "wheel_count",
    )
    for name in positive_counts:
        number = identity[name]
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise CloseoutValidationError(
                f"independent Python runtime {name} is invalid"
            )
    omitted_count = identity["omitted_entry_point_count"]
    if (
        isinstance(omitted_count, bool)
        or not isinstance(omitted_count, int)
        or omitted_count < 0
        or identity["files_verified"] is not True
    ):
        raise CloseoutValidationError(
            "independent Python runtime file/RECORD proof is invalid"
        )
    import_paths = identity["import_paths"]
    if (
        not isinstance(import_paths, list)
        or not import_paths
        or any(not isinstance(value, str) or not value for value in import_paths)
        or len(set(import_paths)) != len(import_paths)
        or any(
            not Path(value).is_absolute()
            or not Path(value).resolve().is_relative_to(runtime)
            for value in import_paths
        )
    ):
        raise CloseoutValidationError(
            "independent Python runtime import paths are invalid"
        )
    expected_environment = {
        "WPG_PYTHON_RUNTIME": identity["runtime"],
        "WPG_PYTHON_RUNTIME_MANIFEST": identity["manifest"],
        "WPG_PYTHON_RUNTIME_MANIFEST_SHA256": identity["manifest_sha256"],
        "WPG_PYTHON_RUNTIME_TREE_SHA256": identity["runtime_tree_sha256"],
    }
    if any(
        environment[name] != expected
        for name, expected in expected_environment.items()
    ):
        raise CloseoutValidationError(
            "process environment disagrees with the independent Python runtime"
        )
    if (
        process.get("executable") != identity["python_executable"]
        or process.get("executable_sha256")
        != identity["python_executable_sha256"]
        or process.get("command")
        != [
            identity["python_executable"],
            "-S",
            "-P",
            "-B",
            "-m",
            "where_paper_go.web_app",
        ]
    ):
        raise CloseoutValidationError(
            "process executable/flags disagree with the independent Python runtime"
        )
    expected_python_path = os.pathsep.join(
        [str(process["cwd"]), *import_paths]
    )
    if environment["PYTHONPATH"] != expected_python_path:
        raise CloseoutValidationError(
            "process PYTHONPATH disagrees with source/runtime bindings"
        )

    health_runtime = health.get("python_runtime")
    if not isinstance(health_runtime, dict):
        raise CloseoutValidationError("health Python runtime evidence is invalid")
    expected_health = {
        "ready": True,
        "manifest_sha256": identity["manifest_sha256"],
        "runtime_tree_sha256": identity["runtime_tree_sha256"],
        "python_executable_sha256": identity["python_executable_sha256"],
        "python_version": identity["python_version"],
        "python_soabi": identity["python_soabi"],
        "python_platform": identity["python_platform"],
        "wheel_count": identity["wheel_count"],
        "elf_audit_sha256": identity["elf_audit_sha256"],
        "system_library_count": identity["system_library_count"],
        "system_directory_count": identity["system_directory_count"],
        "system_abi_stat_verified": True,
        "files_verified": True,
        "proc_exe_matches": True,
        "process_pid": process["pid"],
        "process_start_ticks": process["start_ticks"],
    }
    if health_runtime != expected_health:
        raise CloseoutValidationError(
            "health, process, and independent Python runtime evidence disagree"
        )
    return {**identity, "system_abi_stat_verified": True}


def _selected_wheel_lock_sha256(project_root: Path) -> str:
    _unused, snapshot = _inspect_regular_file(
        project_root / SELECTED_WHEEL_LOCK_PATH,
        capture=False,
        maximum_bytes=16 * 1024 * 1024,
    )
    return snapshot.sha256


def _deployment_state(project_root: Path, git_state: GitState) -> DeploymentEvidence:
    expected_source_manifest_sha256 = _expected_source_manifest_sha256(
        project_root, git_state
    )
    systemd = _parse_systemd_snapshot(_systemctl_show())
    process = _process_snapshot(systemd["main_pid"])
    source_environment = process["environment"]
    if (
        source_environment["WPG_SOURCE_HEAD"] != git_state.head
        or source_environment["WPG_SOURCE_TREE"] != git_state.tree
        or source_environment["WPG_SOURCE_MANIFEST_SHA256"]
        != expected_source_manifest_sha256
    ):
        raise CloseoutValidationError(
            "systemd MainPID source release does not equal current Git objects"
        )
    bearer_token, api_token_file_sha256 = _read_closeout_api_token(
        process["environment"]["WPG_API_TOKEN_FILE"]
    )
    process = {
        **process,
        "api_token_file_sha256": api_token_file_sha256,
    }
    listeners = _parse_listener_snapshot(
        _ss_listeners(process["port"]),
        expected_host=process["host"],
        expected_port=process["port"],
        expected_pid=systemd["main_pid"],
    )
    health = _parse_health_snapshot(
        _fetch_loopback_health(
            process["port"], bearer_token=bearer_token
        )
    )
    python_runtime = _validated_python_runtime_identity(process, health)
    _required_persistent_install_roots(
        source_release=Path(str(process["cwd"])),
        python_runtime=Path(str(python_runtime["runtime"])),
    )
    systemd = _validated_systemd_fragment(
        project_root,
        systemd=systemd,
        process=process,
        python_runtime=python_runtime,
    )
    if (
        process["environment"]["WPG_RUNTIME_MANIFEST_SHA256"]
        != health["lightrag_manifest_sha256"]
    ):
        raise CloseoutValidationError(
            "live runtime-generation manifest differs from health"
        )
    if (
        python_runtime["dependency_lock_sha256"]
        != _selected_wheel_lock_sha256(project_root)
    ):
        raise CloseoutValidationError(
            "Python runtime dependency lock differs from the tracked selected-wheel lock"
        )
    worker_snapshot = _worker_process_snapshot(
        main_process=process,
        health_worker=health["worker_process"],
        python_runtime=python_runtime,
    )
    worker_process = {
        **health["worker_process"],
        "parent_pid": worker_snapshot["parent_pid"],
        "python_executable": worker_snapshot["executable"],
        "process_snapshot_sha256": _canonical_sha256(worker_snapshot),
    }
    expected_bindings = {
        "source_head": git_state.head,
        "source_tree": git_state.tree,
        "source_manifest_sha256": expected_source_manifest_sha256,
        "process_pid": systemd["main_pid"],
        "process_start_ticks": process["start_ticks"],
    }
    for name, expected in expected_bindings.items():
        if health[name] != expected:
            raise CloseoutValidationError(
                f"health, process, and current Git disagree on {name}"
            )
    try:
        Path(python_runtime["runtime"]).relative_to(project_root)
    except ValueError:
        pass
    else:
        raise CloseoutValidationError(
            "systemd MainPID Python runtime is inside the mutable project tree"
        )
    manifest = Path(source_environment["WPG_SOURCE_MANIFEST"])
    release = Path(process["cwd"])
    try:
        release_info = release.lstat()
    except OSError as exc:
        raise CloseoutValidationError(
            "immutable source release directory is unavailable"
        ) from exc
    if (
        stat.S_ISLNK(release_info.st_mode)
        or not stat.S_ISDIR(release_info.st_mode)
        or release_info.st_uid != os.geteuid()
        or stat.S_IMODE(release_info.st_mode) != 0o555
        or manifest != release / "source-release-manifest.json"
    ):
        raise CloseoutValidationError(
            "systemd MainPID source release directory is unsafe"
        )
    try:
        release.relative_to(project_root)
    except ValueError:
        pass
    else:
        raise CloseoutValidationError(
            "systemd MainPID still executes from the mutable project tree"
        )
    if (
        release != manifest.parent.resolve()
        or release.name
        != f"release-{health['source_manifest_sha256']}"
    ):
        raise CloseoutValidationError(
            "systemd MainPID source release is not content-addressed"
        )
    _unused, manifest_snapshot = _inspect_regular_file(
        manifest,
        capture=False,
        expected_mode=0o400,
        maximum_bytes=32 * 1024 * 1024,
    )
    if manifest_snapshot.sha256 != health["source_manifest_sha256"]:
        raise CloseoutValidationError(
            "immutable source manifest hash differs from process and health"
        )
    return DeploymentEvidence(
        active=True,
        enabled=True,
        ready=True,
        bindings_current=True,
        lightrag_store_hashes_verified=health[
            "lightrag_store_hashes_verified"
        ],
        listener_scope=listeners["listener_scope"],
        main_pid=systemd["main_pid"],
        nrestarts=systemd["nrestarts"],
        process_start_ticks=process["start_ticks"],
        systemd_invocation_id=systemd["invocation_id"],
        source_head=health["source_head"],
        source_tree=health["source_tree"],
        source_manifest_sha256=health["source_manifest_sha256"],
        source_release=process["cwd"],
        source_files_verified=health["source_files_verified"],
        lightrag_file_count=health["lightrag_file_count"],
        lightrag_manifest_sha256=health["lightrag_manifest_sha256"],
        lightrag_store_binding_sha256=health[
            "lightrag_store_binding_sha256"
        ],
        systemd_snapshot_sha256=_canonical_sha256(systemd),
        process_snapshot_sha256=_canonical_sha256(process),
        health_snapshot_sha256=_canonical_sha256(health),
        listener_snapshot_sha256=_canonical_sha256(listeners),
        python_runtime=python_runtime,
        worker_process=worker_process,
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
        raise CloseoutValidationError("existing v4 test evidence must be an object")
    _require_exact_keys(value, expected_keys, context="existing v4 test evidence")
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
                "existing v4 test/helper hash binding is inconsistent"
            )


def _validate_existing_summary(
    value: Mapping[str, Any], *, directory: str
) -> str:
    _require_exact_keys(value, OUTPUT_KEYS, context="existing v4 summary")
    if (
        value["schema_version"] != OUTPUT_SCHEMA_VERSION
        or value["artifact_type"] != OUTPUT_ARTIFACT_TYPE
        or value["status"] != "aggregate_only_closeout_validation_complete"
        or value["aggregate_only"] is not True
        or value["contains_per_query_values"] is not False
    ):
        raise CloseoutValidationError("existing v4 summary has invalid identity")
    recorded_at = value["recorded_at"]
    if not isinstance(recorded_at, str):
        raise CloseoutValidationError("existing v4 summary has invalid timestamp")
    try:
        recorded = datetime.strptime(recorded_at, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise CloseoutValidationError(
            "existing v4 summary has invalid timestamp"
        ) from exc
    if recorded.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != recorded_at:
        raise CloseoutValidationError("existing v4 summary timestamp is not canonical")
    _require_sha256(value["request_sha256"], context="existing v4 request hash")
    git = value["git"]
    if not isinstance(git, dict):
        raise CloseoutValidationError("existing v4 summary has invalid Git binding")
    _require_exact_keys(
        git,
        {"head", "tree", "branch", "tracked_and_nonignored_worktree_clean"},
        context="existing v4 Git binding",
    )
    head = git["head"]
    if not isinstance(head, str) or HEX_COMMIT.fullmatch(head) is None:
        raise CloseoutValidationError("existing v4 summary has invalid HEAD")
    tree = git["tree"]
    if not isinstance(tree, str) or HEX_COMMIT.fullmatch(tree) is None:
        raise CloseoutValidationError("existing v4 summary has invalid tree")
    _validate_branch(git["branch"])
    if git["tracked_and_nonignored_worktree_clean"] is not True:
        raise CloseoutValidationError("existing v4 summary was not clean")

    tracked = value["tracked_implementation"]
    if not isinstance(tracked, dict):
        raise CloseoutValidationError(
            "existing v4 tracked implementation must be an object"
        )
    _require_exact_keys(
        tracked,
        TRACKED_IMPLEMENTATION_OUTPUT_KEYS,
        context="existing v4 tracked implementation",
    )
    for name, digest in tracked.items():
        _require_sha256(digest, context=f"existing v4 tracked hash {name}")

    tests = value["tests"]
    if not isinstance(tests, dict):
        raise CloseoutValidationError("existing v4 tests must be an object")
    _require_exact_keys(tests, TESTS_OUTPUT_KEYS, context="existing v4 tests")
    if (
        isinstance(tests["official_weight_inference_tests"], bool)
        or tests["official_weight_inference_tests"] != 0
    ):
        raise CloseoutValidationError(
            "existing v4 official-weight inference count must be zero"
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
        raise CloseoutValidationError("existing v4 artifacts must be an object")
    _require_exact_keys(
        artifacts, set(REQUIRED_ARTIFACTS), context="existing v4 artifacts"
    )
    for name in REQUIRED_ARTIFACTS:
        artifact = artifacts[name]
        if not isinstance(artifact, dict):
            raise CloseoutValidationError("existing v4 artifact must be an object")
        _require_exact_keys(
            artifact, {"sha256", "bytes", "mode"}, context="existing v4 artifact"
        )
        if (
            artifact["sha256"] != PINNED_ARTIFACT_SHA256[name]
            or artifact["bytes"] != PINNED_ARTIFACT_BYTES[name]
            or isinstance(artifact["bytes"], bool)
            or artifact["mode"] != "0444"
        ):
            raise CloseoutValidationError(
                "existing v4 artifact does not match fixed evidence"
            )

    deployment = value["deployment"]
    if not isinstance(deployment, dict):
        raise CloseoutValidationError("existing v4 deployment must be an object")
    _require_exact_keys(
        deployment, DEPLOYMENT_OUTPUT_KEYS, context="existing v4 deployment"
    )
    for name in ("active", "enabled", "ready", "bindings_current"):
        if deployment[name] is not True:
            raise CloseoutValidationError("existing v4 deployment is not ready")
    if (
        deployment["lightrag_store_hashes_verified"] is not True
        or deployment["source_files_verified"] is not True
        or deployment["lightrag_file_count"] != 6
    ):
        raise CloseoutValidationError(
            "existing v4 integrity gates were not all true"
        )
    if deployment["listener_scope"] != "loopback_only":
        raise CloseoutValidationError("existing v4 listener scope is invalid")
    for name in ("main_pid", "nrestarts", "process_start_ticks"):
        number = deployment[name]
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise CloseoutValidationError("existing v4 process state is invalid")
    if deployment["main_pid"] <= 0:
        raise CloseoutValidationError("existing v4 process PID is invalid")
    if deployment["process_start_ticks"] <= 0:
        raise CloseoutValidationError("existing v4 process start ticks are invalid")
    if (
        not isinstance(deployment["systemd_invocation_id"], str)
        or re.fullmatch(
            r"[0-9a-f]{32}", deployment["systemd_invocation_id"]
        )
        is None
    ):
        raise CloseoutValidationError("existing v4 invocation identity is invalid")
    if (
        deployment["source_head"] != head
        or deployment["source_tree"] != tree
        or not isinstance(deployment["source_release"], str)
        or not deployment["source_release"].startswith("/")
    ):
        raise CloseoutValidationError("existing v4 source binding is invalid")
    for name in (
        "systemd_snapshot_sha256",
        "process_snapshot_sha256",
        "health_snapshot_sha256",
        "listener_snapshot_sha256",
        "source_manifest_sha256",
        "lightrag_manifest_sha256",
        "lightrag_store_binding_sha256",
    ):
        _require_sha256(deployment[name], context=f"existing v4 deployment {name}")

    python_runtime = deployment["python_runtime"]
    if not isinstance(python_runtime, dict):
        raise CloseoutValidationError(
            "existing v4 Python runtime evidence must be an object"
        )
    _require_exact_keys(
        python_runtime,
        PYTHON_RUNTIME_OUTPUT_KEYS,
        context="existing v4 Python runtime evidence",
    )
    if (
        python_runtime["files_verified"] is not True
        or python_runtime["system_abi_stat_verified"] is not True
    ):
        raise CloseoutValidationError(
            "existing v4 Python runtime files/system ABI were not verified"
        )
    if (
        python_runtime["dependency_lock_sha256"]
        != tracked["selected_wheel_lock_sha256"]
    ):
        raise CloseoutValidationError(
            "existing v4 Python runtime dependency lock is not tracked"
        )
    for name in (
        "manifest_sha256",
        "runtime_tree_sha256",
        "python_executable_sha256",
        "elf_audit_sha256",
        "installed_distributions_sha256",
        "dependency_lock_sha256",
    ):
        _require_sha256(
            python_runtime[name], context=f"existing v4 Python runtime {name}"
        )
    runtime_path = python_runtime["runtime"]
    manifest_path = python_runtime["manifest"]
    executable_path = python_runtime["python_executable"]
    import_paths = python_runtime["import_paths"]
    if not all(
        isinstance(path, str) and Path(path).is_absolute()
        for path in (runtime_path, manifest_path, executable_path)
    ) or not isinstance(import_paths, list):
        raise CloseoutValidationError(
            "existing v4 Python runtime paths are invalid"
        )
    runtime = Path(runtime_path)
    if (
        runtime.name != f"python-runtime-{python_runtime['manifest_sha256']}"
        or Path(manifest_path) != runtime / PYTHON_RUNTIME_MANIFEST
        or not Path(executable_path).is_relative_to(runtime)
        or not import_paths
        or any(
            not isinstance(path, str)
            or not Path(path).is_absolute()
            or not Path(path).is_relative_to(runtime)
            for path in import_paths
        )
    ):
        raise CloseoutValidationError(
            "existing v4 Python runtime content-address binding is invalid"
        )
    for name in ("python_version", "python_soabi", "python_platform"):
        if not isinstance(python_runtime[name], str) or not python_runtime[name]:
            raise CloseoutValidationError(
                f"existing v4 Python runtime {name} is invalid"
            )
    for name in (
        "elf_file_count",
        "system_library_count",
        "system_directory_count",
        "installed_distribution_count",
        "installed_record_entry_count",
        "wheel_count",
    ):
        number = python_runtime[name]
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise CloseoutValidationError(
                f"existing v4 Python runtime {name} is invalid"
            )
    omitted = python_runtime["omitted_entry_point_count"]
    if isinstance(omitted, bool) or not isinstance(omitted, int) or omitted < 0:
        raise CloseoutValidationError(
            "existing v4 Python runtime omitted-entry count is invalid"
        )

    worker_process = deployment["worker_process"]
    if not isinstance(worker_process, dict):
        raise CloseoutValidationError(
            "existing v4 worker process evidence must be an object"
        )
    _require_exact_keys(
        worker_process,
        DEPLOYMENT_WORKER_PROCESS_KEYS,
        context="existing v4 worker process evidence",
    )
    if (
        worker_process["exact"] is not True
        or worker_process["proc_exe_verified"] is not True
        or worker_process["parent_pid"] != deployment["main_pid"]
        or worker_process["pid"] == deployment["main_pid"]
        or worker_process["python_executable"]
        != python_runtime["python_executable"]
        or worker_process["executable_sha256"]
        != python_runtime["python_executable_sha256"]
    ):
        raise CloseoutValidationError(
            "existing v4 worker process binding is invalid"
        )
    for name in ("pid", "parent_pid", "start_ticks"):
        number = worker_process[name]
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise CloseoutValidationError(
                f"existing v4 worker process {name} is invalid"
            )
    for name in ("executable_sha256", "process_snapshot_sha256"):
        _require_sha256(
            worker_process[name], context=f"existing v4 worker process {name}"
        )
    interpreter = worker_process["interpreter"]
    if not isinstance(interpreter, dict):
        raise CloseoutValidationError(
            "existing v4 worker interpreter evidence is invalid"
        )
    _require_exact_keys(
        interpreter,
        WORKER_INTERPRETER_KEYS,
        context="existing v4 worker interpreter evidence",
    )
    if any(interpreter[name] is not True for name in WORKER_INTERPRETER_KEYS):
        raise CloseoutValidationError(
            "existing v4 worker interpreter flags are invalid"
        )
    if worker_process["source"] != {
        "head": head,
        "tree": tree,
        "manifest_sha256": deployment["source_manifest_sha256"],
        "files_verified": True,
    }:
        raise CloseoutValidationError(
            "existing v4 worker source binding is invalid"
        )
    expected_worker_runtime = {
        "manifest_sha256": python_runtime["manifest_sha256"],
        "runtime_tree_sha256": python_runtime["runtime_tree_sha256"],
        "python_executable_sha256": python_runtime[
            "python_executable_sha256"
        ],
        "python_version": python_runtime["python_version"],
        "python_soabi": python_runtime["python_soabi"],
        "python_platform": python_runtime["python_platform"],
        "wheel_count": python_runtime["wheel_count"],
        "elf_audit_sha256": python_runtime["elf_audit_sha256"],
        "system_library_count": python_runtime["system_library_count"],
        "system_directory_count": python_runtime["system_directory_count"],
        "files_verified": True,
        "proc_exe_matches": True,
        "system_abi_stat_verified": True,
    }
    if worker_process["python_runtime"] != expected_worker_runtime:
        raise CloseoutValidationError(
            "existing v4 worker Python runtime binding is invalid"
        )

    external = value["external_calls"]
    if not isinstance(external, dict):
        raise CloseoutValidationError("existing v4 external calls must be an object")
    _require_exact_keys(
        external, EXTERNAL_CALL_OUTPUT_KEYS, context="existing v4 external calls"
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
        raise CloseoutValidationError("existing v4 external-call evidence is invalid")

    excluded = value["excluded_actions"]
    if not isinstance(excluded, dict):
        raise CloseoutValidationError("existing v4 excluded actions must be an object")
    _require_exact_keys(
        excluded, EXCLUDED_ACTION_OUTPUT_KEYS, context="existing v4 excluded actions"
    )
    if excluded != {
        "live_formal500_executed": False,
        "human_evaluation_executed": False,
        "production_service_mutated": False,
        "live_external_provider_workflows_requested_by_validator": False,
        "loopback_health_probe": "read_only",
        "scope": "validator_actions_only_not_an_absolute_network_observation",
    }:
        raise CloseoutValidationError("existing v4 excluded-action evidence is invalid")

    publication = value["publication"]
    if not isinstance(publication, dict):
        raise CloseoutValidationError("existing v4 publication must be an object")
    _require_exact_keys(
        publication, PUBLICATION_OUTPUT_KEYS, context="existing v4 publication"
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
        "published_from_hidden_building": True,
        "atomic_directory_rename": True,
    }:
        raise CloseoutValidationError("existing v4 publication binding is invalid")
    if value["threat_model_limitations"] != list(THREAT_MODEL_LIMITATIONS):
        raise CloseoutValidationError("existing v4 threat model is invalid")
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
            raise CloseoutValidationError("cannot inspect prior v4 closeout") from exc
        if (
            stat.S_ISLNK(directory_stat.st_mode)
            or not stat.S_ISDIR(directory_stat.st_mode)
            or stat.S_IMODE(directory_stat.st_mode) != 0o555
        ):
            raise CloseoutValidationError("prior v4 closeout directory is unsafe")
        try:
            names = sorted(child.name for child in entry.iterdir())
        except OSError as exc:
            raise CloseoutValidationError("cannot enumerate prior v4 closeout") from exc
        if names != ["summary.json"]:
            raise CloseoutValidationError("prior v4 closeout has unexpected entries")
        summary, _snapshot = _load_json_regular(
            entry / "summary.json", expected_mode=0o444, maximum_bytes=1024 * 1024
        )
        if _validate_existing_summary(summary, directory=entry.name) == head:
            raise CloseoutValidationError("HEAD already has a successful v4 closeout")


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
        atomic_rename_noreplace(target, failed)
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
    building = output_root / f".{name}.building-{uuid.uuid4().hex}"
    if building.exists() or building.is_symlink():
        raise CloseoutValidationError("hidden closeout building path collision")
    building.mkdir(mode=0o700, exist_ok=False)
    summary = building / "summary.json"
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
        descriptor = os.open(
            summary,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | O_NOFOLLOW,
        )
        try:
            os.fchmod(descriptor, 0o444)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(building)
        building.chmod(0o555)
        _fsync_directory(building)
        _verify_published_directory(
            building, summary, expected_summary_sha256=expected_hash
        )
        final_checks()
        _verify_published_directory(
            building, summary, expected_summary_sha256=expected_hash
        )
        if target.exists() or target.is_symlink():
            raise CloseoutValidationError("refusing to overwrite closeout directory")
        atomic_rename_noreplace(building, target)
        _fsync_directory(output_root)
        summary = target / "summary.json"
        _verify_published_directory(
            target, summary, expected_summary_sha256=expected_hash
        )
    except BaseException:
        failed_source = building if building.exists() else target
        if failed_source.exists():
            _mark_failed(output_root, failed_source, name)
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
            deployment = _deployment_state(project_root, initial_git)

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
                    "published_from_hidden_building": True,
                    "atomic_directory_rename": True,
                },
                "threat_model_limitations": list(THREAT_MODEL_LIMITATIONS),
            }

            def final_checks() -> None:
                if _git_state(project_root) != initial_git:
                    raise CloseoutValidationError(
                        "Git state changed before closeout publication"
                    )
                final_request, final_request_snapshot = _load_json_regular(
                    input_path, expected_mode=0o444, maximum_bytes=64 * 1024
                )
                if (
                    final_request != request_raw
                    or final_request_snapshot != request_snapshot
                ):
                    raise CloseoutValidationError(
                        "closeout request changed before publication"
                    )
                if _verify_artifacts(
                    project_root, request["artifacts"]
                ) != artifacts:
                    raise CloseoutValidationError(
                        "critical artifacts changed before publication"
                    )
                _reverify_test_evidence(full_tests)
                _reverify_test_evidence(model_tests)
                if _deployment_state(project_root, initial_git) != deployment:
                    raise CloseoutValidationError(
                        "deployment changed before closeout publication"
                    )

            return_value = _write_new_directory(
                output_root, name, payload, final_checks=final_checks
            )
            target, summary_sha256 = return_value
            return target, payload, summary_sha256


def _load_base_closeout(
    base_summary_path: Path, *, output_root: Path
) -> BaseCloseoutEvidence:
    summary_path = Path(os.path.abspath(base_summary_path))
    if Path(os.path.realpath(summary_path)) != summary_path:
        raise CloseoutValidationError("base closeout path must not contain a symlink")
    directory = summary_path.parent
    if (
        directory.parent != output_root
        or VERSION_DIRECTORY.fullmatch(directory.name) is None
        or summary_path.name != "summary.json"
    ):
        raise CloseoutValidationError(
            "post-deployment reproof requires one canonical v4 closeout summary"
        )
    _verify_published_directory(
        directory,
        summary_path,
        expected_summary_sha256=_inspect_regular_file(
            summary_path,
            capture=False,
            expected_mode=0o444,
            maximum_bytes=2 * 1024 * 1024,
        )[1].sha256,
    )
    summary, snapshot = _load_json_regular(
        summary_path, expected_mode=0o444, maximum_bytes=2 * 1024 * 1024
    )
    _validate_existing_summary(summary, directory=directory.name)
    git_raw = summary["git"]
    assert isinstance(git_raw, dict)
    git_state = GitState(
        head=git_raw["head"],
        tree=git_raw["tree"],
        branch=git_raw["branch"],
        worktree_clean=True,
    )
    return BaseCloseoutEvidence(
        directory=directory,
        summary_path=summary_path,
        summary_snapshot=snapshot,
        summary=summary,
        git=git_state,
    )


def create_deployment_reproof(
    *, base_summary_path: Path, project_root: Path, output_root: Path
) -> tuple[Path, dict[str, Any], str]:
    """Append an immutable deployment observation for an existing closeout."""

    project_root = Path(os.path.abspath(project_root))
    output_root = Path(os.path.abspath(output_root))
    _ensure_project_and_output(project_root, output_root)
    with _exclusive_output_lock(output_root):
        base = _load_base_closeout(base_summary_path, output_root=output_root)
        current_git = _git_state(project_root)
        if current_git != base.git or not current_git.worktree_clean:
            raise CloseoutValidationError(
                "current clean Git state does not equal the base closeout"
            )
        helpers = _verify_tracked_helpers(project_root)
        if helpers != base.summary["tracked_implementation"]:
            raise CloseoutValidationError(
                "current tracked implementation differs from the base closeout"
            )
        deployment = _deployment_state(project_root, current_git)
        recorded_at, stamp = _utc_stamp()
        name = f"{REPROOF_PREFIX}{stamp}-{current_git.head[:12]}"
        base_deployment = base.summary["deployment"]
        assert isinstance(base_deployment, dict)
        payload: dict[str, Any] = {
            "schema_version": REPROOF_SCHEMA_VERSION,
            "artifact_type": REPROOF_ARTIFACT_TYPE,
            "status": "post_deployment_reproof_complete",
            "recorded_at": recorded_at,
            "base_closeout": {
                "directory": base.directory.name,
                "summary_sha256": base.summary_snapshot.sha256,
                "recorded_at": base.summary["recorded_at"],
                "head": current_git.head,
                "tree": current_git.tree,
                "deployment_invocation_id": base_deployment[
                    "systemd_invocation_id"
                ],
                "deployment_process_start_ticks": base_deployment[
                    "process_start_ticks"
                ],
            },
            "git": {
                "head": current_git.head,
                "tree": current_git.tree,
                "branch": current_git.branch,
                "tracked_and_nonignored_worktree_clean": True,
            },
            "tracked_implementation": helpers,
            "deployment": deployment.as_dict(),
            "publication": {
                "directory": name,
                "directory_mode": "0555",
                "summary_mode": "0444",
                "existing_directories_preserved": True,
                "overwrite_supported": False,
                "same_head_replay_supported": True,
                "published_from_hidden_building": True,
                "atomic_directory_rename": True,
            },
            "threat_model_limitations": list(
                REPROOF_THREAT_MODEL_LIMITATIONS
            ),
        }
        _require_exact_keys(
            payload, REPROOF_OUTPUT_KEYS, context="deployment reproof"
        )
        _require_exact_keys(
            payload["base_closeout"],
            REPROOF_BASE_KEYS,
            context="deployment reproof base",
        )

        def final_checks() -> None:
            if _git_state(project_root) != current_git:
                raise CloseoutValidationError(
                    "Git state changed before reproof publication"
                )
            final_base = _load_base_closeout(
                base.summary_path, output_root=output_root
            )
            if (
                final_base.summary_snapshot != base.summary_snapshot
                or final_base.summary != base.summary
            ):
                raise CloseoutValidationError(
                    "base closeout changed before reproof publication"
                )
            if _verify_tracked_helpers(project_root) != helpers:
                raise CloseoutValidationError(
                    "tracked implementation changed before reproof publication"
                )
            if _deployment_state(project_root, current_git) != deployment:
                raise CloseoutValidationError(
                    "deployment changed before reproof publication"
                )

        target, summary_sha256 = _write_new_directory(
            output_root, name, payload, final_checks=final_checks
        )
        return target, payload, summary_sha256


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--input", type=Path)
    operation.add_argument("--post-deployment-from", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.input is not None:
            target, _payload, summary_sha256 = create_closeout(
                input_path=arguments.input,
                project_root=PROJECT_ROOT,
                output_root=DEFAULT_OUTPUT_ROOT,
            )
            status = "aggregate_only_closeout_validation_complete"
        else:
            target, _payload, summary_sha256 = create_deployment_reproof(
                base_summary_path=arguments.post_deployment_from,
                project_root=PROJECT_ROOT,
                output_root=DEFAULT_OUTPUT_ROOT,
            )
            status = "post_deployment_reproof_complete"
    except (CloseoutValidationError, OSError) as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            {
                "status": status,
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
