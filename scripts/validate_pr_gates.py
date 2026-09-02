#!/usr/bin/env python3
"""Run the fixed, provider-offline pull-request acceptance gates.

This validator is intentionally independent of the production closeout.  It
uses only tracked repository inputs, never inspects or mutates the user
service, and emits aggregate evidence.  The production closeout remains the
separate host/deployment gate.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import socket
import stat
import subprocess
import sys
import tempfile
import types
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_CLOSEOUT_TESTS_PATH = PROJECT_ROOT / "scripts" / "run_closeout_tests.py"
RUN_CLOSEOUT_TESTS_SHA256 = (
    "f8e62c5db35dce200532b4b64dc3ac709e1ededc4a79a6185a2f30c768bdabd7"
)


def _load_fixed_python_source(
    path: Path, *, expected_sha256: str, module_name: str
) -> Any:
    """Compile exactly the regular-file bytes whose digest was approved."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError(f"trusted Python source is not a single regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read()
    finally:
        os.close(descriptor)
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise RuntimeError(f"trusted Python source digest drifted: {path}")
    module = types.ModuleType(module_name)
    module.__file__ = os.fspath(path)
    module.__package__ = ""
    module.__loader__ = None
    sys.modules[module_name] = module
    try:
        code = compile(payload, os.fspath(path), "exec", dont_inherit=True)
        exec(code, module.__dict__)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


# Never import the repository's ``scripts`` package during gate bootstrap.
# The workflow starts this file with ``-I -S``; the fixed runner is loaded from
# one already-digested byte string so a changed ``scripts/__init__.py`` or a
# sibling stdlib-shadowing module cannot run before the manifest is checked.
run_closeout_tests = _load_fixed_python_source(
    RUN_CLOSEOUT_TESTS_PATH,
    expected_sha256=RUN_CLOSEOUT_TESTS_SHA256,
    module_name="_wpg_fixed_run_closeout_tests",
)


MANIFEST_PATH = PROJECT_ROOT / ".github" / "pr-gate-manifest.json"
GIT_BINARY = Path("/usr/bin/git")
ARTIFACT_TYPE = "where_papers_go_pr_gate_manifest"
SCHEMA_VERSION = 1
LOGO_PATH = "docs/Where-Papers-Go.png"
LOGO_GIT_BLOB_SHA1 = "42b021f7088e08c165fa615a8d3b7bd60af25fd1"
LOGO_SHA256 = "80266c537c4a8251766e1d8e53c5a1e9def90e34080b76d8a3e00be770ba3b11"
RETRIEVAL_CASE_DEFINITION_SHA256 = (
    "81bd948067df401ccc434eb4c345d877034602ae5e9d52d61defd0848c95118a"
)
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
HEX_SHA1 = re.compile(r"[0-9a-f]{40}\Z")
HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

FULL_TEST_ID_SHA256 = (
    "ddc285a4a7b74373dd0cf92f2da5515899d382a16e3c31ccb3e27963565eccc4"
)
FULL_SKIPPED_TEST_ID_SHA256 = (
    "1727325b45f14c02cfe3ea8e8a26faaadbebc2fa3004527a8c7769c5daf928a6"
)
FULL_SKIP_ALLOWLIST_SHA256 = (
    "d970d6124c58fd064d3241a151dfc2001b2c841c003c2c2bba3dfecaf71a246b"
)
MODEL_TEST_ID_SHA256 = (
    "651d59643b938f9f13712a8838a08f51efe74bb18d100fc07e8f0c825c866b94"
)
MODEL_SKIPPED_TEST_ID_SHA256 = (
    "a9f31e92ced15b4367d3e78d93aa1e820909a0fd49fb3495d0c36cce9817c3dc"
)
MODEL_SKIP_ALLOWLIST_SHA256 = (
    "ecbbeafb099c4e91937fc5570d6dbf6ffdde3700e59245704340e92d8d558fed"
)
MODEL_REQUIREMENTS_SHA256 = (
    "c4affc60b45576553cd4fd15043f062706bef966980882c37541b8b69582940e"
)
MODEL_LOCK_PATH = PROJECT_ROOT / ".github" / "pylock.wpg-pr-model.toml"
MODEL_LOCK_SHA256 = (
    "dbfdb43b9a9dea53bf445f0cc1db5d412fb49af2e0a2c2b1c89b2c230c65aa2b"
)
WHEEL_BUILD_LOCK_PATH = (
    PROJECT_ROOT / ".github" / "pylock.wpg-wheel-build.toml"
)
WHEEL_BUILD_LOCK_SHA256 = (
    "f329f2faaee80e0bf1a02de88631af4a548eb1daeab2b32ceddbdaa6b0f29a53"
)
WHEEL_BUILD_LOCK_VERSIONS = {
    "build": "1.3.0",
    "packaging": "26.3",
    "pyproject-hooks": "1.2.0",
    "setuptools": "81.0.0",
}
MODEL_LOCK_VERSIONS = {
    "certifi": "2026.7.22",
    "charset-normalizer": "3.5.1",
    "filelock": "3.32.5",
    "fsspec": "2026.7.0",
    "hf-xet": "1.6.0",
    "huggingface-hub": "0.36.2",
    "idna": "3.19",
    "jinja2": "3.1.6",
    "markupsafe": "3.0.3",
    "mpmath": "1.3.0",
    "networkx": "3.6.1",
    "numpy": "2.5.2",
    "packaging": "26.3",
    "pyyaml": "6.0.3",
    "regex": "2026.9.3",
    "requests": "2.34.2",
    "safetensors": "0.7.0",
    "setuptools": "81.0.0",
    "sympy": "1.14.0",
    "tokenizers": "0.22.2",
    "torch": "2.11.0+cpu",
    "tqdm": "4.70.0",
    "transformers": "4.57.6",
    "typing-extensions": "4.16.0",
    "urllib3": "2.7.0",
}
MODEL_RUNTIME_VERSIONS = {
    "safetensors": "0.7.0",
    "tokenizers": "0.22.2",
    "torch": "2.11.0+cpu",
    "transformers": "4.57.6",
}
MODEL_RUNTIME_MODULES = tuple(MODEL_RUNTIME_VERSIONS)
OS_OFFLINE_ENV = "WPG_PR_OS_OFFLINE_ACTIVE"
OS_OFFLINE_TOKEN = "linux-sandbox-v3"
OS_HOST_NETNS_ENV = "WPG_PR_HOST_NETNS_ID"
OS_CALLER_UID_ENV = "WPG_PR_CALLER_UID"
OS_CALLER_GID_ENV = "WPG_PR_CALLER_GID"
OS_CALLER_HOME_ENV = "WPG_PR_CALLER_HOME"
OS_SANDBOX_ROOT_ENV = "WPG_PR_SANDBOX_ROOT"
OS_RUNNER_COMMANDS_ENV = "WPG_PR_RUNNER_COMMANDS_DIR"
OS_RUNNER_TOOL_CACHE_ENV = "WPG_PR_RUNNER_TOOL_CACHE"
KNOWN_SYNTHETIC_OPENAI_MATCH_SHA256 = (
    "5dbb4fb2fc75fe7c7728ec846b2d1c86da06e3950a0823c7f1af2100d9823064"
)
SYNTHETIC_TAVILY_MARKERS = (
    b"synthetic",
    b"fictitious",
    b"exhausted",
)

EXPECTED_TESTS: Mapping[str, Mapping[str, Any]] = {
    "full": {
        "total": 489,
        "passed": 485,
        "skipped": 4,
        "test_id_sha256": FULL_TEST_ID_SHA256,
        "skipped_test_id_sha256": FULL_SKIPPED_TEST_ID_SHA256,
        "skip_allowlist_sha256": FULL_SKIP_ALLOWLIST_SHA256,
    },
    "model-focused": {
        "total": 6,
        "passed": 6,
        "skipped": 0,
        "test_id_sha256": MODEL_TEST_ID_SHA256,
        "skipped_test_id_sha256": MODEL_SKIPPED_TEST_ID_SHA256,
        "skip_allowlist_sha256": MODEL_SKIP_ALLOWLIST_SHA256,
    },
}

CRITICAL_TEST_PATHS = (
    "tests/test_build_recent_journal_benchmark.py",
    "tests/test_closeout_runner.py",
    "tests/test_deployment.py",
    "tests/test_enrich_journal_scope_catalog.py",
    "tests/test_evaluate_recent_journals.py",
    "tests/test_expert_review.py",
    "tests/test_external_call_budget.py",
    "tests/test_external_call_redirects.py",
    "tests/test_graph_runs.py",
    "tests/test_historical_builder.py",
    "tests/test_llm_streaming.py",
    "tests/test_local_model_runtime.py",
    "tests/test_m3_unified_config.py",
    "tests/test_merge_recent_journal_evaluation.py",
    "tests/test_model_assets.py",
    "tests/test_model_runs.py",
    "tests/test_nginx_integration.py",
    "tests/test_normalize_tavily_keys.py",
    "tests/test_reranker_runs.py",
    "tests/test_research_offline_benchmark.py",
    "tests/test_scope_enrichment_batches.py",
    "tests/test_scope_rank.py",
    "tests/test_scope_rank_config.py",
    "tests/test_scope_rank_evaluation_config.py",
    "tests/test_scope_rank_inference.py",
    "tests/test_scope_rank_runs.py",
    "tests/test_scope_rank_selective.py",
    "tests/test_sealed_evaluation.py",
    "tests/test_sealed_namespace_crosswalk.py",
    "tests/test_sealed_namespace_repair.py",
    "tests/test_sealed_preflight.py",
    "tests/test_sealed_sources.py",
    "tests/test_sealed_test.py",
    "tests/test_search_web.py",
    "tests/test_systemd_host_integration.py",
    "tests/test_tavily_key_pool.py",
    "tests/test_validate_closeout.py",
    "tests/test_venue_api_assistant.py",
    "tests/test_venue_graph_index.py",
    "tests/test_venue_lightrag.py",
    "tests/test_venue_recommender.py",
    "tests/test_venue_search_index.py",
    "tests/test_web_app.py",
    "tests/test_web_security.py",
    "tests/test_worker.py",
)

REQUIRED_CRITICAL_PATHS = (
    ".github/CODEOWNERS",
    ".github/pr-model-requirements.txt",
    ".github/pylock.wpg-pr-model.toml",
    ".github/pylock.wpg-wheel-build.toml",
    ".github/workflows/tests.yml",
    "data/cas_partition_2025.csv",
    "data/ccf_conferences_2026.csv",
    "data/curated_venue_scopes.tsv",
    "data/jcr_partition_2025.csv",
    "data/manifest.json",
    "data/th_cpl_partition_2019.csv",
    "deploy/monitoring/policy-v1.json",
    "deploy/nginx/where-papers-go.conf.in",
    "deploy/python/selected-wheels-cpython-3.14.5-linux-x86_64.json",
    "deploy/systemd/where-papers-go-monitor.service.in",
    "deploy/systemd/where-papers-go-monitor.timer.in",
    "deploy/systemd/where-papers-go.service.in",
    LOGO_PATH,
    "scripts/__init__.py",
    "scripts/benchmark_retrieval.py",
    "scripts/build_graph.py",
    "scripts/closeout_offline_guard/sitecustomize.py",
    "scripts/manage_deployment.py",
    "scripts/monitor_operations.py",
    "scripts/run_linux_offline_gate.sh",
    "scripts/run_closeout_tests.py",
    "scripts/validate_closeout.py",
    "scripts/validate_pr_gates.py",
    *CRITICAL_TEST_PATHS,
    "pyproject.toml",
    "uv.lock",
    "where_paper_go/deployment_identity.py",
    "where_paper_go/graph_index.py",
    "where_paper_go/recommender.py",
    "where_paper_go/web_app.py",
    "where_paper_go/web_security.py",
    "where_paper_go/worker.py",
)

CANONICAL_RETRIEVAL_CASES: tuple[Mapping[str, Any], ...] = (
    {
        "name": "network_exact",
        "targets": ["CCF-A"],
        "query": "计算机网络",
        "top_k": 4,
        "expected": ["INFOCOM", "MobiCom", "NSDI", "SIGCOMM"],
        "reviewed_only": False,
    },
    {
        "name": "wireless_project",
        "targets": ["CCF-A", "THCPL-A", "中科院1区"],
        "query": "截止期约束的联合波束与资源分配 无线边缘网络",
        "top_k": 2,
        "expected": ["INFOCOM", "TWC"],
        "reviewed_only": False,
    },
    {
        "name": "storage_cross_language",
        "targets": ["CCF-A"],
        "query": "文件系统与存储可靠性",
        "top_k": 5,
        "expected": ["FAST", "OSDI", "SOSP"],
        "reviewed_only": False,
    },
    {
        "name": "language_models",
        "targets": ["CCF-A"],
        "query": "大语言模型与自然语言处理",
        "top_k": 5,
        "expected": ["ACL", "ICLR", "ICML", "NeurIPS"],
        "reviewed_only": False,
    },
    {
        "name": "colloquial_wireless",
        "targets": ["CCF-A"],
        "query": "手机在信号时好时坏时自动调整传输策略",
        "top_k": 3,
        "expected": ["INFOCOM", "MobiCom", "SIGCOMM"],
        "reviewed_only": False,
    },
    {
        "name": "fpga_specialized",
        "targets": ["CCF-A", "THCPL-A", "中科院1区"],
        "query": "FPGA 可重构计算与高层综合",
        "top_k": 1,
        "expected": ["FPGA"],
        "reviewed_only": True,
    },
    {
        "name": "bioinformatics",
        "targets": ["CCF-A", "THCPL-A", "中科院1区"],
        "query": "生物信息学 蛋白质组学 计算方法",
        "top_k": 2,
        "expected": ["ISMB", "RECOMB"],
        "reviewed_only": True,
    },
)

# These detectors intentionally target high-confidence credential formats.
# Matches are never printed.  Synthetic test fixtures are admitted only by an
# exact (detector, tracked path, domain-separated match digest) manifest entry.
CREDENTIAL_DETECTORS = (
    (
        "openai_key",
        re.compile(rb"(?<![A-Za-z0-9])sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    ),
    (
        "tavily_key",
        re.compile(rb"(?<![A-Za-z0-9])tvly-[A-Za-z0-9_-]{20,}"),
    ),
    (
        "github_token",
        re.compile(rb"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{20,}"),
    ),
    (
        "github_fine_grained_token",
        re.compile(rb"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{20,}"),
    ),
    ("aws_access_key", re.compile(rb"(?<![A-Z0-9])AKIA[0-9A-Z]{16}")),
    ("google_api_key", re.compile(rb"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}")),
    (
        "slack_token",
        re.compile(rb"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}"),
    ),
    (
        "private_key",
        re.compile(
            rb"-----BEGIN (?:ENCRYPTED |RSA |EC |OPENSSH )?PRIVATE KEY-----"
        ),
    ),
)

MANIFEST_KEYS = {
    "artifact_type",
    "schema_version",
    "credential_scan",
    "critical_files",
    "logo_protection",
    "network_policy",
    "retrieval",
    "tests",
    "test_sources",
}
CRITICAL_FILE_KEYS = {"bytes", "git_blob_sha1", "mode", "path", "sha256"}
FINDING_KEYS = {"detector", "match_sha256", "occurrence", "path"}


class PrGateError(RuntimeError):
    """Raised when a fixed pull-request gate fails closed."""


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PrGateError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("ascii")


def _emit(value: Mapping[str, Any]) -> None:
    sys.stdout.buffer.write(_canonical_json(value))


def _git(arguments: Sequence[str], *, binary: bool = False) -> str | bytes:
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_PAGER": "cat",
    }
    completed = subprocess.run(
        [
            os.fspath(GIT_BINARY),
            "-c",
            "color.ui=false",
            "-c",
            "core.pager=cat",
            *arguments,
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise PrGateError(f"git {' '.join(arguments[:2])} failed: {detail}")
    if binary:
        return completed.stdout
    return completed.stdout.decode("utf-8", errors="strict").strip()


def _safe_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise PrGateError("manifest path must be a non-empty string")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value:
        raise PrGateError(f"manifest path is unsafe: {value!r}")
    return value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tracked_mode(path: str) -> str:
    raw = _git(("ls-files", "--stage", "--", path))
    assert isinstance(raw, str)
    rows = [row for row in raw.splitlines() if row]
    if len(rows) != 1 or "\t" not in rows[0]:
        raise PrGateError(f"critical path is not one tracked index entry: {path}")
    metadata, observed_path = rows[0].split("\t", 1)
    fields = metadata.split()
    if len(fields) != 3 or fields[2] != "0" or observed_path != path:
        raise PrGateError(f"critical path has an unsafe index entry: {path}")
    return fields[0]


def _working_file_record(
    path: str, *, allow_untracked: bool = False
) -> dict[str, Any]:
    safe_path = _safe_relative_path(path)
    absolute = PROJECT_ROOT / safe_path
    try:
        metadata = absolute.lstat()
        data = absolute.read_bytes()
    except OSError as exc:
        raise PrGateError(f"critical file is unavailable: {safe_path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise PrGateError(f"critical path is not a regular file: {safe_path}")
    try:
        mode = _tracked_mode(safe_path)
    except PrGateError:
        if not allow_untracked:
            raise
        mode = "100755" if metadata.st_mode & stat.S_IXUSR else "100644"
    blob = _git(("hash-object", "--no-filters", "--", safe_path))
    assert isinstance(blob, str)
    if HEX_SHA1.fullmatch(blob) is None:
        raise PrGateError(f"invalid Git blob identity for {safe_path}")
    return {
        "path": safe_path,
        "mode": mode,
        "bytes": len(data),
        "git_blob_sha1": blob,
        "sha256": _sha256(data),
    }


def _credential_detector_sha256() -> str:
    digest = hashlib.sha256(b"where-papers-go-credential-detectors-v2\0")
    for name, detector in CREDENTIAL_DETECTORS:
        for value in (
            name.encode("ascii"),
            detector.pattern,
            str(detector.flags).encode("ascii"),
        ):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    for value in (
        b"tests/",
        KNOWN_SYNTHETIC_OPENAI_MATCH_SHA256.encode("ascii"),
        *SYNTHETIC_TAVILY_MARKERS,
    ):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return digest.hexdigest()


def _credential_match_sha256(detector: str, match: bytes) -> str:
    digest = hashlib.sha256(b"where-papers-go-credential-match-v1\0")
    digest.update(detector.encode("ascii"))
    digest.update(b"\0")
    digest.update(match)
    return digest.hexdigest()


def _tracked_entries() -> tuple[tuple[str, str, str], ...]:
    raw = _git(("ls-files", "--stage", "-z"), binary=True)
    assert isinstance(raw, bytes)
    entries: list[tuple[str, str, str]] = []
    for encoded in (part for part in raw.split(b"\0") if part):
        if b"\t" not in encoded:
            raise PrGateError("tracked index entry has invalid framing")
        metadata, encoded_path = encoded.split(b"\t", 1)
        fields = metadata.split()
        if len(fields) != 3 or fields[2] != b"0":
            raise PrGateError("tracked index contains an unresolved stage")
        try:
            mode = fields[0].decode("ascii")
            blob = fields[1].decode("ascii")
            path = encoded_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PrGateError("tracked index metadata is not valid text") from exc
        _safe_relative_path(path)
        if mode not in {"100644", "100755"}:
            raise PrGateError(
                f"tracked credential scan rejects non-regular mode {mode}: {path}"
            )
        if HEX_SHA1.fullmatch(blob) is None:
            raise PrGateError(f"tracked path has an invalid Git blob: {path}")
        entries.append((path, mode, blob))
    paths = tuple(path for path, _mode, _blob in entries)
    if len(paths) != len(set(paths)):
        raise PrGateError("tracked path inventory contains duplicates")
    return tuple(entries)


def _tracked_paths() -> tuple[str, ...]:
    return tuple(path for path, _mode, _blob in _tracked_entries())


def _tracked_bytes(path: str, blob: str, *, source: str) -> bytes:
    if source == "index":
        data = _git(("cat-file", "blob", blob), binary=True)
        assert isinstance(data, bytes)
        return data
    if source != "worktree":
        raise PrGateError("credential scan source is invalid")
    absolute = PROJECT_ROOT / path
    try:
        metadata = absolute.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise PrGateError(
                f"tracked worktree path is not a regular file: {path}"
            )
        return absolute.read_bytes()
    except PrGateError:
        raise
    except OSError as exc:
        raise PrGateError(f"cannot scan tracked worktree path: {path}") from exc


def _credential_findings(
    *, source: str = "index"
) -> tuple[dict[str, Any], ...]:
    findings: list[dict[str, Any]] = []
    for path, _mode, blob in _tracked_entries():
        safe_path = _safe_relative_path(path)
        data = _tracked_bytes(safe_path, blob, source=source)
        for name, detector in CREDENTIAL_DETECTORS:
            for occurrence, match in enumerate(detector.finditer(data), start=1):
                matched = match.group(0)
                matched_sha256 = _credential_match_sha256(name, matched)
                if safe_path.startswith("tests/"):
                    explicitly_synthetic = (
                        name == "openai_key"
                        and matched_sha256
                        == KNOWN_SYNTHETIC_OPENAI_MATCH_SHA256
                    ) or (
                        name == "tavily_key"
                        and any(
                            marker in matched.lower()
                            for marker in SYNTHETIC_TAVILY_MARKERS
                        )
                    )
                    if not explicitly_synthetic:
                        raise PrGateError(
                            "credential-like test value is not an approved "
                            f"synthetic format: {safe_path} ({name})"
                        )
                findings.append(
                    {
                        "detector": name,
                        "path": safe_path,
                        "occurrence": occurrence,
                        "match_sha256": matched_sha256,
                    }
                )
    return tuple(
        sorted(
            findings,
            key=lambda row: (
                row["path"], row["detector"], row["occurrence"], row["match_sha256"]
            ),
        )
    )


def _retrieval_definition(
    *, verify_implementation: bool = False
) -> tuple[list[dict[str, Any]], str]:
    canonical = [dict(row) for row in CANONICAL_RETRIEVAL_CASES]
    if verify_implementation:
        from scripts.benchmark_retrieval import QUALITY_CASES

        observed = [
            {
                "name": case.name,
                "targets": list(case.targets),
                "query": case.query,
                "top_k": case.top_k,
                "expected": sorted(case.expected),
                "reviewed_only": case.reviewed_only,
            }
            for case in QUALITY_CASES
        ]
        if observed != canonical:
            raise PrGateError(
                "retrieval cases differ from the canonical 7-case contract"
            )
    digest = hashlib.sha256(b"where-papers-go-retrieval-cases-v1\0")
    digest.update(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if digest.hexdigest() != RETRIEVAL_CASE_DEFINITION_SHA256:
        raise PrGateError("canonical retrieval-case digest is invalid")
    return canonical, RETRIEVAL_CASE_DEFINITION_SHA256


def _test_source_inventory() -> dict[str, Any]:
    paths = tuple(
        sorted(
            path
            for path in _tracked_paths()
            if path.startswith("tests/test_") and path.endswith(".py")
        )
    )
    if len(paths) != 45:
        raise PrGateError("fixed test-source inventory must contain 45 modules")
    records = [_working_file_record(path) for path in paths]
    digest = hashlib.sha256(b"where-papers-go-test-source-inventory-v1\0")
    for record in records:
        encoded = _canonical_json(record)
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return {"count": len(records), "inventory_sha256": digest.hexdigest()}


def _manifest_template() -> dict[str, Any]:
    critical = [
        _working_file_record(path, allow_untracked=True)
        for path in REQUIRED_CRITICAL_PATHS
    ]
    cases, case_digest = _retrieval_definition()
    guard = next(
        row
        for row in critical
        if row["path"] == "scripts/closeout_offline_guard/sitecustomize.py"
    )
    os_wrapper = next(
        row
        for row in critical
        if row["path"] == "scripts/run_linux_offline_gate.sh"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": ARTIFACT_TYPE,
        "tests": {name: dict(value) for name, value in EXPECTED_TESTS.items()},
        "test_sources": _test_source_inventory(),
        "retrieval": {
            "case_count": len(cases),
            "case_definition_sha256": case_digest,
            "required_full_recall_cases": len(cases),
            "required_micro_recall_at_k": 1.0,
        },
        "credential_scan": {
            "detectors_sha256": _credential_detector_sha256(),
            "allowed_findings": list(_credential_findings()),
        },
        "logo_protection": {
            "path": LOGO_PATH,
            "git_blob_sha1": LOGO_GIT_BLOB_SHA1,
            "sha256": LOGO_SHA256,
        },
        "network_policy": {
            "guard_path": guard["path"],
            "guard_sha256": guard["sha256"],
            "os_isolation_path": os_wrapper["path"],
            "os_isolation_sha256": os_wrapper["sha256"],
            "os_isolation_token": OS_OFFLINE_TOKEN,
            "ipv4_ipv6_nonloopback_egress_allowed": 0,
            "loopback_allowed": True,
            "native_non_python_children_blocked": True,
            "nonloopback_python_socket_attempts_allowed": 0,
            "standard_host_privilege_af_unix_channels_visible": False,
            "masked_host_runtime_roots": ["/dev/shm", "/run", "/tmp"],
            "supplementary_groups_allowed": 0,
            "linux_capability_sets_allowed": 0,
            "no_new_privs_required": True,
            "checkout_writable": False,
            "caller_home_writable": False,
            "runner_command_files_writable": False,
            "runner_tool_cache_writable": False,
            "sandbox_cwd_rebound_to_checkout": True,
            "scope": "ci_gate_mount_net_ipc_uts_pid_sandbox_plus_python_socket_audit",
        },
        "critical_files": critical,
    }


def _load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        raw = path.read_bytes()
    except OSError as exc:
        raise PrGateError(f"PR gate manifest is unavailable: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise PrGateError("PR gate manifest must be a regular file")
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_duplicate_rejecting_object
        )
    except PrGateError:
        raise
    except (UnicodeError, ValueError) as exc:
        raise PrGateError("PR gate manifest is invalid JSON") from exc
    if not isinstance(value, dict) or set(value) != MANIFEST_KEYS:
        raise PrGateError("PR gate manifest has unexpected top-level keys")
    if (
        value["schema_version"] != SCHEMA_VERSION
        or value["artifact_type"] != ARTIFACT_TYPE
    ):
        raise PrGateError("PR gate manifest identity is invalid")
    return value


def _validate_manifest(value: Mapping[str, Any]) -> None:
    if value.get("tests") != {
        name: dict(expected) for name, expected in EXPECTED_TESTS.items()
    }:
        raise PrGateError("manifest test contract differs from the fixed gate")
    test_sources = value.get("test_sources")
    if (
        not isinstance(test_sources, dict)
        or set(test_sources) != {"count", "inventory_sha256"}
        or test_sources.get("count") != 45
        or not isinstance(test_sources.get("inventory_sha256"), str)
        or HEX_SHA256.fullmatch(test_sources["inventory_sha256"]) is None
    ):
        raise PrGateError("manifest test-source inventory is invalid")

    cases, case_digest = _retrieval_definition()
    expected_retrieval = {
        "case_count": len(cases),
        "case_definition_sha256": case_digest,
        "required_full_recall_cases": len(cases),
        "required_micro_recall_at_k": 1.0,
    }
    if value.get("retrieval") != expected_retrieval or len(cases) != 7:
        raise PrGateError("manifest retrieval contract differs from the fixed 7/7 gate")

    if value.get("logo_protection") != {
        "path": LOGO_PATH,
        "git_blob_sha1": LOGO_GIT_BLOB_SHA1,
        "sha256": LOGO_SHA256,
    }:
        raise PrGateError("manifest logo protection contract is invalid")

    critical = value.get("critical_files")
    if not isinstance(critical, list):
        raise PrGateError("manifest critical_files must be a list")
    records: dict[str, Mapping[str, Any]] = {}
    for row in critical:
        if not isinstance(row, dict) or set(row) != CRITICAL_FILE_KEYS:
            raise PrGateError("manifest critical-file record is invalid")
        path = _safe_relative_path(row["path"])
        if path in records:
            raise PrGateError(f"duplicate critical-file record: {path}")
        if (
            row["mode"] not in {"100644", "100755"}
            or isinstance(row["bytes"], bool)
            or not isinstance(row["bytes"], int)
            or row["bytes"] < 0
            or not isinstance(row["git_blob_sha1"], str)
            or HEX_SHA1.fullmatch(row["git_blob_sha1"]) is None
            or not isinstance(row["sha256"], str)
            or HEX_SHA256.fullmatch(row["sha256"]) is None
        ):
            raise PrGateError(f"critical-file metadata is invalid: {path}")
        records[path] = row
    if tuple(sorted(records)) != tuple(sorted(REQUIRED_CRITICAL_PATHS)):
        raise PrGateError("manifest critical-file inventory is not the fixed inventory")

    logo_record = records[LOGO_PATH]
    if (
        logo_record["git_blob_sha1"] != LOGO_GIT_BLOB_SHA1
        or logo_record["sha256"] != LOGO_SHA256
        or logo_record["mode"] != "100644"
    ):
        raise PrGateError("critical-file inventory does not preserve the logo")

    guard_record = records["scripts/closeout_offline_guard/sitecustomize.py"]
    os_wrapper_record = records["scripts/run_linux_offline_gate.sh"]
    runner_record = records["scripts/run_closeout_tests.py"]
    if (
        runner_record["sha256"] != RUN_CLOSEOUT_TESTS_SHA256
        or runner_record["mode"] != "100644"
    ):
        raise PrGateError("fixed closeout runner bootstrap digest drifted")
    expected_network = {
        "guard_path": guard_record["path"],
        "guard_sha256": guard_record["sha256"],
        "os_isolation_path": os_wrapper_record["path"],
        "os_isolation_sha256": os_wrapper_record["sha256"],
        "os_isolation_token": OS_OFFLINE_TOKEN,
        "ipv4_ipv6_nonloopback_egress_allowed": 0,
        "loopback_allowed": True,
        "native_non_python_children_blocked": True,
        "nonloopback_python_socket_attempts_allowed": 0,
        "standard_host_privilege_af_unix_channels_visible": False,
        "masked_host_runtime_roots": ["/dev/shm", "/run", "/tmp"],
        "supplementary_groups_allowed": 0,
        "linux_capability_sets_allowed": 0,
        "no_new_privs_required": True,
        "checkout_writable": False,
        "caller_home_writable": False,
        "runner_command_files_writable": False,
        "runner_tool_cache_writable": False,
        "sandbox_cwd_rebound_to_checkout": True,
        "scope": "ci_gate_mount_net_ipc_uts_pid_sandbox_plus_python_socket_audit",
    }
    if value.get("network_policy") != expected_network:
        raise PrGateError("manifest offline-network policy is invalid")

    credential = value.get("credential_scan")
    if not isinstance(credential, dict) or set(credential) != {
        "allowed_findings",
        "detectors_sha256",
    }:
        raise PrGateError("manifest credential-scan contract is invalid")
    if credential["detectors_sha256"] != _credential_detector_sha256():
        raise PrGateError("credential detector fingerprint drifted")
    allowed = credential["allowed_findings"]
    if not isinstance(allowed, list):
        raise PrGateError("credential allowed_findings must be a list")
    normalized: list[dict[str, Any]] = []
    detector_names = {name for name, _pattern in CREDENTIAL_DETECTORS}
    for row in allowed:
        if not isinstance(row, dict) or set(row) != FINDING_KEYS:
            raise PrGateError("credential allowlist record is invalid")
        path = _safe_relative_path(row["path"])
        if not path.startswith("tests/"):
            raise PrGateError("credential fixtures may be allowed only below tests/")
        if row["detector"] not in detector_names:
            raise PrGateError("credential allowlist names an unknown detector")
        if (
            isinstance(row["occurrence"], bool)
            or not isinstance(row["occurrence"], int)
            or row["occurrence"] < 1
            or not isinstance(row["match_sha256"], str)
            or HEX_SHA256.fullmatch(row["match_sha256"]) is None
        ):
            raise PrGateError("credential allowlist has an invalid match digest")
        normalized.append(dict(row))
    expected_order = sorted(
        normalized,
        key=lambda row: (
            row["path"], row["detector"], row["occurrence"], row["match_sha256"]
        ),
    )
    if normalized != expected_order or len(normalized) != len(
        {
            (
                row["path"],
                row["detector"],
                row["occurrence"],
                row["match_sha256"],
            )
            for row in normalized
        }
    ):
        raise PrGateError("credential allowlist must be unique and canonically sorted")


def _verify_critical_files(manifest: Mapping[str, Any]) -> None:
    expected = {
        row["path"]: row for row in manifest["critical_files"]
    }
    for path in REQUIRED_CRITICAL_PATHS:
        if _working_file_record(path) != expected[path]:
            raise PrGateError(f"tracked critical-file hash drifted: {path}")
    if _test_source_inventory() != manifest["test_sources"]:
        raise PrGateError("tracked test-source inventory hash drifted")


def _verify_credentials(manifest: Mapping[str, Any]) -> int:
    observed = list(_credential_findings(source="index"))
    expected = manifest["credential_scan"]["allowed_findings"]
    if observed != expected:
        observed_set = {
            (
                row["path"],
                row["detector"],
                row["occurrence"],
                row["match_sha256"],
            )
            for row in observed
        }
        expected_set = {
            (
                row["path"],
                row["detector"],
                row["occurrence"],
                row["match_sha256"],
            )
            for row in expected
        }
        unexpected = observed_set - expected_set
        missing = expected_set - observed_set
        paths = sorted({row[0] for row in unexpected})
        suffix = f" in {', '.join(paths)}" if paths else ""
        raise PrGateError(
            "credential scan differs from the exact synthetic-fixture allowlist: "
            f"{len(unexpected)} unexpected{suffix}; {len(missing)} missing"
        )
    if list(_credential_findings(source="worktree")) != observed:
        raise PrGateError(
            "tracked worktree credential scan differs from the reviewed Git index"
        )
    return len(observed)


def _verify_logo_and_diff(base: str) -> None:
    if not base or "\x00" in base or base.startswith("-") or any(
        character.isspace() for character in base
    ):
        raise PrGateError("base revision is unsafe")
    _git(("cat-file", "-e", f"{base}^{{commit}}"))
    _git(("merge-base", "--is-ancestor", base, "HEAD"))
    base_logo_record = _git(
        ("ls-tree", "-z", base, "--", LOGO_PATH), binary=True
    )
    head_logo = _git(("rev-parse", "--verify", f"HEAD:{LOGO_PATH}"))
    expected_base_record = (
        f"100644 blob {LOGO_GIT_BLOB_SHA1}\t{LOGO_PATH}\0".encode("utf-8")
    )
    if base_logo_record not in {b"", expected_base_record}:
        raise PrGateError("base may omit but may not replace the approved logo")
    if head_logo != LOGO_GIT_BLOB_SHA1:
        raise PrGateError("HEAD must preserve the approved logo Git blob")
    for arguments in (
        ("diff", "--check", f"{base}...HEAD"),
        ("diff", "--check"),
        ("diff", "--cached", "--check"),
    ):
        output = _git(arguments)
        if output:
            raise PrGateError("git diff --check reported whitespace errors")


def _validate_test_report(raw: Mapping[str, Any], *, suite: str) -> dict[str, Any]:
    expected = EXPECTED_TESTS.get(suite)
    if expected is None:
        raise PrGateError("unknown fixed test suite")
    if set(raw) != run_closeout_tests.REPORT_KEYS:
        raise PrGateError("test report has unexpected keys")
    if (
        raw.get("schema_version") != run_closeout_tests.REPORT_SCHEMA_VERSION
        or raw.get("artifact_type") != run_closeout_tests.ARTIFACT_TYPE
        or raw.get("guard_active") is not True
    ):
        raise PrGateError("test report identity/offline guard is invalid")
    integer_fields = (
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
    for field in integer_fields:
        value = raw.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PrGateError(f"test report {field} is invalid")
    if raw["total"] != sum(
        raw[field]
        for field in (
            "passed",
            "skipped",
            "failures",
            "errors",
            "expected_failures",
            "unexpected_successes",
        )
    ):
        raise PrGateError("test report counts do not add up")
    if raw["test_id_count"] != raw["total"]:
        raise PrGateError("test ID count does not equal total")
    if raw["skipped_test_id_count"] != raw["skipped"]:
        raise PrGateError("skipped-test ID count does not equal skipped")
    outcome_fields = (
        "failures",
        "errors",
        "expected_failures",
        "unexpected_successes",
    )
    nonzero_outcomes = [
        f"{field}={raw[field]}" for field in outcome_fields if raw[field] != 0
    ]
    if nonzero_outcomes:
        raise PrGateError(
            f"fixed {suite} suite has non-passing aggregate outcomes: "
            + ", ".join(nonzero_outcomes)
        )
    for field, value in expected.items():
        if raw.get(field) != value:
            raise PrGateError(f"fixed {suite} report drifted at {field}")
    return {
        "suite": suite,
        "total": raw["total"],
        "passed": raw["passed"],
        "skipped": raw["skipped"],
        "test_id_sha256": raw["test_id_sha256"],
        "skipped_test_id_sha256": raw["skipped_test_id_sha256"],
        "skip_allowlist_sha256": raw["skip_allowlist_sha256"],
        "offline_guard_active": True,
    }


def _private_empty_file(path: Path) -> os.stat_result:
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        return os.fstat(descriptor)
    finally:
        os.close(descriptor)


def _verify_empty_audit(path: Path, initial: os.stat_result) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or (metadata.st_dev, metadata.st_ino) != (initial.st_dev, initial.st_ino)
        or metadata.st_size != 0
        or metadata.st_mtime_ns != initial.st_mtime_ns
        or metadata.st_ctime_ns != initial.st_ctime_ns
        or path.read_bytes() != b""
    ):
        raise PrGateError("offline-guard audit was written, replaced, or reset")


def _load_report(path: Path) -> dict[str, Any]:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or metadata.st_size > 64 * 1024
    ):
        raise PrGateError("test report file metadata is unsafe")
    try:
        value = json.loads(
            path.read_text(encoding="ascii"),
            object_pairs_hook=_duplicate_rejecting_object,
        )
    except PrGateError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise PrGateError("test report is invalid JSON") from exc
    if not isinstance(value, dict):
        raise PrGateError("test report must be an object")
    return value


def _model_requirements_sha256() -> str:
    path = PROJECT_ROOT / ".github" / "pr-model-requirements.txt"
    try:
        metadata = path.lstat()
        data = path.read_bytes()
    except OSError as exc:
        raise PrGateError("fixed model requirements are unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or len(data) > 4 * 1024
    ):
        raise PrGateError("fixed model requirements metadata is unsafe")
    digest = _sha256(data)
    if digest != MODEL_REQUIREMENTS_SHA256:
        raise PrGateError("fixed model requirements fingerprint drifted")
    return digest


def _model_lock_versions() -> dict[str, str]:
    try:
        import tomllib
    except ModuleNotFoundError as exc:  # pragma: no cover - fixed CI is 3.12.
        raise PrGateError(
            "PEP 751 model-lock validation requires Python 3.11+"
        ) from exc
    try:
        metadata = MODEL_LOCK_PATH.lstat()
        raw = MODEL_LOCK_PATH.read_bytes()
    except OSError as exc:
        raise PrGateError("fixed PEP 751 model lock is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or len(raw) > 64 * 1024
    ):
        raise PrGateError("fixed PEP 751 model-lock metadata is unsafe")
    if _sha256(raw) != MODEL_LOCK_SHA256:
        raise PrGateError("fixed PEP 751 model-lock fingerprint drifted")
    try:
        value = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise PrGateError("fixed PEP 751 model lock is invalid") from exc
    if not isinstance(value, dict) or set(value) != {
        "lock-version",
        "created-by",
        "requires-python",
        "packages",
    }:
        raise PrGateError("fixed PEP 751 model lock has unexpected keys")
    if (
        value["lock-version"] != "1.0"
        or value["created-by"] != "uv"
        or value["requires-python"] != ">=3.12.3"
    ):
        raise PrGateError("fixed PEP 751 model-lock identity is invalid")
    packages = value["packages"]
    if not isinstance(packages, list) or len(packages) != len(MODEL_LOCK_VERSIONS):
        raise PrGateError("fixed PEP 751 model lock has the wrong package count")

    observed: dict[str, str] = {}
    wheel_hashes: set[str] = set()
    expected_marker = (
        "platform_machine == 'aarch64' or platform_machine == 'amd64' or "
        "platform_machine == 'arm64' or platform_machine == 'x86_64'"
    )
    for row in packages:
        if not isinstance(row, dict):
            raise PrGateError("fixed PEP 751 model-lock package is invalid")
        allowed_keys = {"name", "version", "wheels"}
        if row.get("name") == "hf-xet":
            allowed_keys.add("marker")
        if set(row) != allowed_keys:
            raise PrGateError("fixed PEP 751 model-lock package keys are invalid")
        name = row.get("name")
        version = row.get("version")
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) is None
            or not isinstance(version, str)
            or not version
            or name in observed
        ):
            raise PrGateError("fixed PEP 751 model-lock package pin is invalid")
        if name == "hf-xet" and row.get("marker") != expected_marker:
            raise PrGateError("fixed PEP 751 model-lock marker is invalid")
        wheels = row.get("wheels")
        if not isinstance(wheels, list) or not wheels:
            raise PrGateError("fixed PEP 751 model-lock wheel set is empty")
        for wheel in wheels:
            if not isinstance(wheel, dict) or set(wheel) not in (
                {"url", "upload-time", "hashes"},
                {"url", "upload-time", "size", "hashes"},
            ):
                raise PrGateError("fixed PEP 751 model-lock wheel is invalid")
            url = wheel.get("url")
            hashes = wheel.get("hashes")
            size = wheel.get("size")
            if (
                not isinstance(url, str)
                or not isinstance(hashes, dict)
                or set(hashes) != {"sha256"}
                or not isinstance(hashes.get("sha256"), str)
                or HEX_SHA256.fullmatch(hashes["sha256"]) is None
                or (size is not None and (
                    isinstance(size, bool) or not isinstance(size, int) or size <= 0
                ))
            ):
                raise PrGateError("fixed PEP 751 model-lock wheel metadata is invalid")
            parsed = urlsplit(url)
            expected_host = (
                "download-r2.pytorch.org" if name == "torch"
                else "files.pythonhosted.org"
            )
            if (
                parsed.scheme != "https"
                or parsed.hostname != expected_host
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port not in (None, 443)
                or parsed.query
                or parsed.fragment
                or not unquote(parsed.path).endswith(".whl")
            ):
                raise PrGateError("fixed PEP 751 model-lock wheel URL is invalid")
            if hashes["sha256"] in wheel_hashes:
                raise PrGateError("fixed PEP 751 model-lock wheel hash is duplicated")
            wheel_hashes.add(hashes["sha256"])
        observed[name] = version
    if observed != MODEL_LOCK_VERSIONS:
        raise PrGateError("fixed PEP 751 model-lock package pins drifted")
    return observed


def _wheel_build_lock_versions() -> dict[str, str]:
    try:
        import tomllib
    except ModuleNotFoundError as exc:  # pragma: no cover - fixed CI is 3.12.
        raise PrGateError(
            "PEP 751 wheel-build lock validation requires Python 3.11+"
        ) from exc
    try:
        metadata = WHEEL_BUILD_LOCK_PATH.lstat()
        raw = WHEEL_BUILD_LOCK_PATH.read_bytes()
    except OSError as exc:
        raise PrGateError("fixed PEP 751 wheel-build lock is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or len(raw) > 16 * 1024
        or _sha256(raw) != WHEEL_BUILD_LOCK_SHA256
    ):
        raise PrGateError("fixed PEP 751 wheel-build lock fingerprint drifted")
    try:
        value = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise PrGateError("fixed PEP 751 wheel-build lock is invalid") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {
            "lock-version",
            "created-by",
            "requires-python",
            "packages",
        }
        or value["lock-version"] != "1.0"
        or value["created-by"] != "uv"
        or value["requires-python"] != ">=3.12.3"
    ):
        raise PrGateError("fixed PEP 751 wheel-build lock identity is invalid")
    packages = value["packages"]
    if not isinstance(packages, list) or len(packages) != len(
        WHEEL_BUILD_LOCK_VERSIONS
    ):
        raise PrGateError("fixed PEP 751 wheel-build lock package count drifted")
    observed: dict[str, str] = {}
    hashes: set[str] = set()
    for row in packages:
        if not isinstance(row, dict) or set(row) != {"name", "version", "wheels"}:
            raise PrGateError("fixed PEP 751 wheel-build package is invalid")
        name = row.get("name")
        version = row.get("version")
        wheels = row.get("wheels")
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) is None
            or not isinstance(version, str)
            or not version
            or name in observed
            or not isinstance(wheels, list)
            or len(wheels) != 1
        ):
            raise PrGateError("fixed PEP 751 wheel-build package pin is invalid")
        wheel = wheels[0]
        if not isinstance(wheel, dict) or set(wheel) != {
            "url",
            "upload-time",
            "size",
            "hashes",
        }:
            raise PrGateError("fixed PEP 751 wheel-build wheel is invalid")
        url = wheel.get("url")
        digest = wheel.get("hashes")
        size = wheel.get("size")
        parsed = urlsplit(url) if isinstance(url, str) else None
        if (
            parsed is None
            or parsed.scheme != "https"
            or parsed.hostname != "files.pythonhosted.org"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.query
            or parsed.fragment
            or not unquote(parsed.path).endswith(".whl")
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
            or not isinstance(digest, dict)
            or set(digest) != {"sha256"}
            or not isinstance(digest.get("sha256"), str)
            or HEX_SHA256.fullmatch(digest["sha256"]) is None
            or digest["sha256"] in hashes
        ):
            raise PrGateError("fixed PEP 751 wheel-build metadata is invalid")
        hashes.add(digest["sha256"])
        observed[name] = version
    if observed != WHEEL_BUILD_LOCK_VERSIONS:
        raise PrGateError("fixed PEP 751 wheel-build package pins drifted")
    return observed


def _normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _require_isolated_startup() -> None:
    """Require a stdlib-only bootstrap with no repository import path."""

    if not (
        sys.flags.isolated
        and sys.flags.no_site
        and sys.flags.no_user_site
        and sys.flags.ignore_environment
        and sys.flags.safe_path
    ):
        raise PrGateError(
            "PR gate must start as an absolute script with Python -I -S"
        )


def _activate_runtime_site_packages() -> Path:
    """Expose one venv's packages without executing ``site`` or ``.pth``."""

    executable = Path(sys.executable)
    if not executable.is_absolute() or executable.parent.name != "bin":
        raise PrGateError("fixed gate runtime is not an absolute virtualenv Python")
    environment_root = executable.parent.parent
    config_path = environment_root / "pyvenv.cfg"
    site_packages = (
        environment_root
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    try:
        root_metadata = environment_root.lstat()
        config_metadata = config_path.lstat()
        site_metadata = site_packages.lstat()
        config = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PrGateError("fixed gate virtualenv metadata is unavailable") from exc
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_ISLNK(root_metadata.st_mode)
        or environment_root.resolve() != environment_root
        or not stat.S_ISREG(config_metadata.st_mode)
        or stat.S_ISLNK(config_metadata.st_mode)
        or not stat.S_ISDIR(site_metadata.st_mode)
        or stat.S_ISLNK(site_metadata.st_mode)
        or site_packages.resolve() != site_packages
    ):
        raise PrGateError("fixed gate virtualenv paths are redirected")
    settings: dict[str, str] = {}
    for line in config.splitlines():
        name, separator, value = line.partition("=")
        if separator:
            settings[name.strip().casefold()] = value.strip()
    if settings.get("include-system-site-packages", "").casefold() != "false":
        raise PrGateError("fixed gate virtualenv enables system site packages")
    encoded = os.fspath(site_packages)
    if encoded not in sys.path:
        sys.path.append(encoded)
        importlib.invalidate_caches()
    return site_packages


def _activate_verified_project_imports() -> None:
    encoded = os.fspath(PROJECT_ROOT)
    if encoded not in sys.path:
        sys.path.append(encoded)
        importlib.invalidate_caches()


def _guarded_child_environment(audit_path: str) -> dict[str, str]:
    environment = run_closeout_tests._sanitized_environment(audit_path)
    for name in (
        OS_OFFLINE_ENV,
        OS_HOST_NETNS_ENV,
        OS_CALLER_UID_ENV,
        OS_CALLER_GID_ENV,
        OS_CALLER_HOME_ENV,
        OS_SANDBOX_ROOT_ENV,
        OS_RUNNER_COMMANDS_ENV,
        OS_RUNNER_TOOL_CACHE_ENV,
    ):
        value = os.environ.get(name)
        if value is None or "\x00" in value:
            raise PrGateError(f"sandbox child is missing {name}")
        environment[name] = value
    return environment


def _install_verified_guard(manifest: Mapping[str, Any]) -> Any:
    if "sitecustomize" in sys.modules:
        raise PrGateError("offline guard module was loaded before verification")
    guard_record = next(
        row
        for row in manifest["critical_files"]
        if row["path"] == "scripts/closeout_offline_guard/sitecustomize.py"
    )
    try:
        module = _load_fixed_python_source(
            PROJECT_ROOT / guard_record["path"],
            expected_sha256=guard_record["sha256"],
            module_name="sitecustomize",
        )
    except RuntimeError as exc:
        raise PrGateError("verified offline guard could not be loaded") from exc
    try:
        active = bool(module.guard_self_check())
    except BaseException as exc:
        raise PrGateError("verified offline guard self-check failed") from exc
    if not active:
        raise PrGateError("verified offline guard did not activate")
    return module


def _runtime_preflight(suite: str) -> dict[str, Any]:
    requirements_sha256 = _model_requirements_sha256()
    if suite == "full":
        present = [
            name
            for name in MODEL_RUNTIME_MODULES
            if importlib.util.find_spec(name) is not None
        ]
        if present:
            raise PrGateError(
                "fixed full suite requires an isolated runtime without model packages: "
                + ", ".join(present)
            )
        if "WPG_NGINX_BIN" in os.environ:
            raise PrGateError(
                "fixed full suite requires WPG_NGINX_BIN to be unset"
            )
        locked_versions = _model_lock_versions()
        return {
            "model_requirements_sha256": requirements_sha256,
            "model_lock_sha256": MODEL_LOCK_SHA256,
            "model_lock_package_count": len(locked_versions),
        }
    if suite != "model-focused":
        raise PrGateError("unknown fixed test suite")
    locked_versions = _model_lock_versions()
    observed: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        raw_name = distribution.metadata.get("Name")
        if not isinstance(raw_name, str) or not raw_name:
            raise PrGateError("fixed model runtime has unnamed distribution metadata")
        name = _normalized_distribution_name(raw_name)
        if name in observed:
            raise PrGateError("fixed model runtime has duplicate distributions")
        observed[name] = distribution.version
    if observed != locked_versions:
        raise PrGateError("fixed model runtime differs from the complete PEP 751 lock")
    return {
        "model_requirements_sha256": requirements_sha256,
        "model_lock_sha256": MODEL_LOCK_SHA256,
        "model_lock_package_count": len(locked_versions),
        **{name: observed[name] for name in MODEL_RUNTIME_VERSIONS},
    }


def _sandbox_status_is_unprivileged(status_text: str, uid: int, gid: int) -> bool:
    fields: dict[str, list[str]] = {}
    for line in status_text.splitlines():
        key, separator, value = line.partition(":")
        if separator and key not in fields:
            fields[key] = value.split()
    expected_capability = ["0000000000000000"]
    return (
        fields.get("Pid") == ["1"]
        and fields.get("Uid") == [str(uid)] * 4
        and fields.get("Gid") == [str(gid)] * 4
        and fields.get("Groups") == []
        and fields.get("NoNewPrivs") == ["1"]
        and all(
            fields.get(name) == expected_capability
            for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
        )
    )


_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")


def _decode_mount_component(value: str) -> str:
    return _MOUNT_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def _sandbox_mounts_are_private(
    mountinfo_text: str,
    *,
    project_root: str,
    caller_home: str,
    runner_commands_dir: str,
    runner_tool_cache: str,
) -> bool:
    entries: dict[str, tuple[set[str], str]] = {}
    for line in mountinfo_text.splitlines():
        parts = line.split()
        try:
            separator = parts.index("-")
        except ValueError:
            return False
        if separator < 6 or len(parts) <= separator + 2:
            return False
        if any(
            field.startswith(("shared:", "master:", "propagate_from:"))
            for field in parts[6:separator]
        ):
            return False
        mountpoint = _decode_mount_component(parts[4])
        entries[mountpoint] = (set(parts[5].split(",")), parts[separator + 1])

    required_tmpfs = {
        "/run": {"rw", "nosuid", "nodev", "noexec"},
        "/tmp": {"rw", "nosuid", "nodev"},
        "/dev/shm": {"rw", "nosuid", "nodev", "noexec"},
    }
    for path, required_options in required_tmpfs.items():
        options, filesystem = entries.get(path, (set(), ""))
        if filesystem != "tmpfs" or not required_options.issubset(options):
            return False
    if entries.get("/proc", (set(), ""))[1] != "proc":
        return False
    checkout_options = entries.get(project_root, (set(), ""))[0]
    if not {"ro", "nosuid", "nodev", "noexec"}.issubset(checkout_options):
        return False
    home_options = entries.get(caller_home, (set(), ""))[0]
    if not {"ro", "nosuid", "nodev"}.issubset(home_options):
        return False
    if runner_commands_dir != "/nonexistent":
        command_options = entries.get(runner_commands_dir, (set(), ""))[0]
        if not {"ro", "nosuid", "nodev", "noexec"}.issubset(command_options):
            return False
    if runner_tool_cache != "/nonexistent":
        tool_options = entries.get(runner_tool_cache, (set(), ""))[0]
        if not {"ro", "nosuid", "nodev"}.issubset(tool_options):
            return False
    return True


def _raw_socket_creation_is_blocked() -> bool:
    if not hasattr(socket, "AF_PACKET"):
        return True
    try:
        probe = socket.socket(
            socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003)
        )
    except OSError:
        return True
    probe.close()
    return False


def _sandbox_attestation() -> bool:
    host_netns_id = os.environ.get(OS_HOST_NETNS_ENV, "")
    uid_text = os.environ.get(OS_CALLER_UID_ENV, "")
    gid_text = os.environ.get(OS_CALLER_GID_ENV, "")
    caller_home = os.environ.get(OS_CALLER_HOME_ENV, "")
    project_root = os.environ.get(OS_SANDBOX_ROOT_ENV, "")
    runner_commands_dir = os.environ.get(OS_RUNNER_COMMANDS_ENV, "")
    runner_tool_cache = os.environ.get(OS_RUNNER_TOOL_CACHE_ENV, "")
    if (
        re.fullmatch(r"[0-9]+:[0-9]+", host_netns_id) is None
        or not uid_text.isascii()
        or not uid_text.isdecimal()
        or not gid_text.isascii()
        or not gid_text.isdecimal()
        or not caller_home.startswith("/")
        or project_root != os.fspath(PROJECT_ROOT)
        or not runner_commands_dir.startswith("/")
        or not runner_tool_cache.startswith("/")
    ):
        return False
    uid = int(uid_text)
    gid = int(gid_text)
    if uid == 0 or gid == 0:
        return False
    try:
        current_namespace = os.stat("/proc/self/ns/net")
        status_text = Path("/proc/self/status").read_text(encoding="ascii")
        mountinfo_text = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        interfaces = set(os.listdir("/sys/class/net"))
    except (OSError, UnicodeError):
        return False
    current_netns_id = f"{current_namespace.st_dev}:{current_namespace.st_ino}"
    if (
        current_netns_id == host_netns_id
        or os.getpid() != 1
        or os.getuid() != uid
        or os.geteuid() != uid
        or os.getgid() != gid
        or os.getegid() != gid
        or os.getgroups()
        or Path.cwd().resolve() != PROJECT_ROOT
        or os.access(".", os.W_OK)
        or os.access(PROJECT_ROOT, os.W_OK)
        or interfaces != {"lo"}
        or not _sandbox_status_is_unprivileged(status_text, uid, gid)
        or not _sandbox_mounts_are_private(
            mountinfo_text,
            project_root=project_root,
            caller_home=caller_home,
            runner_commands_dir=runner_commands_dir,
            runner_tool_cache=runner_tool_cache,
        )
        or not _raw_socket_creation_is_blocked()
    ):
        return False
    for name in (
        "GITHUB_ENV",
        "GITHUB_PATH",
        "GITHUB_OUTPUT",
        "SSH_AUTH_SOCK",
        "DBUS_SESSION_BUS_ADDRESS",
        "DOCKER_HOST",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "OPENAI_API_KEY",
        "TAVILY_API_KEY",
        "GITHUB_TOKEN",
    ):
        if name in os.environ:
            return False
    for path in (
        "/run/docker.sock",
        "/var/run/docker.sock",
        "/run/containerd/containerd.sock",
        "/run/dbus/system_bus_socket",
        "/run/systemd/private",
        f"/run/user/{uid}/bus",
    ):
        try:
            if stat.S_ISSOCK(os.stat(path, follow_symlinks=False).st_mode):
                return False
        except FileNotFoundError:
            pass
        except OSError:
            return False
    try:
        sudo_probe = subprocess.run(
            ["/usr/bin/sudo", "-n", "/usr/bin/true"],
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return sudo_probe.returncode != 0


def _os_network_isolation_active(*, required: bool) -> bool:
    active = (
        os.environ.get(OS_OFFLINE_ENV) == OS_OFFLINE_TOKEN
        and _sandbox_attestation()
    )
    if required and not active:
        raise PrGateError("required OS-level offline sandbox isolation is inactive")
    return active


def _run_test_gate(
    suite: str,
    manifest_path: Path,
    *,
    require_os_isolation: bool = False,
) -> dict[str, Any]:
    manifest = _load_manifest(manifest_path)
    _validate_manifest(manifest)
    _verify_critical_files(manifest)
    _activate_runtime_site_packages()
    runtime = _runtime_preflight(suite)
    os_isolation = _os_network_isolation_active(required=require_os_isolation)
    with tempfile.TemporaryDirectory(prefix=f"wpg-pr-{suite}-") as temporary:
        workspace = Path(temporary)
        os.chmod(workspace, 0o700)
        audit = workspace / "nonloopback-socket-attempts.log"
        report = workspace / "test-report.json"
        initial = _private_empty_file(audit)
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                os.fspath(Path(__file__).resolve()),
                "_tests-child",
                "--suite",
                suite,
                "--manifest",
                os.fspath(manifest_path.resolve()),
                "--output",
                os.fspath(report),
            ],
            cwd=PROJECT_ROOT,
            env=_guarded_child_environment(os.fspath(audit)),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1800,
            check=False,
        )
        result = _validate_test_report(_load_report(report), suite=suite)
        _verify_empty_audit(audit, initial)
        if completed.returncode != 0:
            raise PrGateError(f"fixed {suite} runner returned failure")
        return {
            "artifact_type": "where_papers_go_pr_test_gate",
            "status": "passed",
            **result,
            "nonloopback_python_socket_attempts": 0,
            "offline_guard_audit_sha256": EMPTY_SHA256,
            "os_network_isolation": os_isolation,
            "python_version": ".".join(str(value) for value in sys.version_info[:3]),
            "runtime": runtime,
        }


def _tests_child(suite: str, manifest_path: Path, report: Path) -> int:
    manifest = _load_manifest(manifest_path)
    _validate_manifest(manifest)
    _verify_critical_files(manifest)
    _os_network_isolation_active(required=True)
    _install_verified_guard(manifest)
    _activate_runtime_site_packages()
    _runtime_preflight(suite)
    _activate_verified_project_imports()
    original_argv = sys.argv
    try:
        sys.argv = [
            os.fspath(RUN_CLOSEOUT_TESTS_PATH),
            "--suite",
            suite,
            "--output",
            os.fspath(report),
        ]
        return int(run_closeout_tests.main())
    finally:
        sys.argv = original_argv


def _is_nonnegative_finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and value >= 0
    )


def _validate_retrieval_benchmark(
    result: Mapping[str, Any], *, graph: Path
) -> None:
    expected_top_level = {
        "graph",
        "graph_load_ms",
        "peak_rss_kb",
        "case_count",
        "micro_recall_at_k",
        "all_cases_full_recall",
        "query_ms",
        "cases",
    }
    if set(result) != expected_top_level:
        raise PrGateError("retrieval benchmark schema drifted")
    if (
        result.get("graph") != os.fspath(graph.resolve())
        or result.get("case_count") != 7
        or result.get("micro_recall_at_k") != 1.0
        or result.get("all_cases_full_recall") is not True
        or not _is_nonnegative_finite_number(result.get("graph_load_ms"))
        or isinstance(result.get("peak_rss_kb"), bool)
        or not isinstance(result.get("peak_rss_kb"), int)
        or result["peak_rss_kb"] <= 0
    ):
        raise PrGateError("retrieval aggregate is not the fixed 7/7 contract")
    timing = result.get("query_ms")
    if (
        not isinstance(timing, dict)
        or set(timing) != {"mean", "median", "max"}
        or not all(
            _is_nonnegative_finite_number(value) for value in timing.values()
        )
    ):
        raise PrGateError("retrieval timing aggregate is invalid")

    cases = result.get("cases")
    canonical, _case_digest = _retrieval_definition()
    if not isinstance(cases, list) or len(cases) != len(canonical):
        raise PrGateError("retrieval benchmark did not return seven cases")
    expected_case_keys = {
        "name",
        "query",
        "top_k",
        "expected",
        "result",
        "matched",
        "recall_at_k",
        "query_ms",
    }
    for row, expected in zip(cases, canonical, strict=True):
        if not isinstance(row, dict) or set(row) != expected_case_keys:
            raise PrGateError("retrieval case schema drifted")
        expected_venues = list(expected["expected"])
        ranked = row.get("result")
        if (
            row.get("name") != expected["name"]
            or row.get("query") != expected["query"]
            or row.get("top_k") != expected["top_k"]
            or row.get("expected") != expected_venues
            or row.get("matched") != expected_venues
            or row.get("recall_at_k") != 1.0
            or not isinstance(ranked, list)
            or len(ranked) > expected["top_k"]
            or len(ranked) != len(set(ranked))
            or not all(isinstance(item, str) and item for item in ranked)
            or sorted(set(ranked) & set(expected_venues)) != expected_venues
            or not _is_nonnegative_finite_number(row.get("query_ms"))
        ):
            raise PrGateError(
                f"retrieval case differs from canonical full recall: "
                f"{expected['name']}"
            )


def _retrieval_child(manifest_path: Path, workspace: Path) -> dict[str, Any]:
    manifest = _load_manifest(manifest_path)
    _validate_manifest(manifest)
    _verify_critical_files(manifest)
    _os_network_isolation_active(required=True)
    sitecustomize = _install_verified_guard(manifest)
    initial = sitecustomize.audit_snapshot()
    _activate_runtime_site_packages()
    _runtime_preflight("full")
    _activate_verified_project_imports()
    _expected_cases, case_digest = _retrieval_definition(
        verify_implementation=True
    )

    from scripts import build_graph as build_graph_script
    from scripts.benchmark_retrieval import benchmark

    graph = workspace / "venue-graph.json.gz"
    captured = io.StringIO()
    with redirect_stdout(captured), redirect_stderr(captured):
        status = build_graph_script.main(
            ["--data-dir", os.fspath(PROJECT_ROOT / "data"), "--graph", os.fspath(graph), "--force"]
        )
    if status != 0:
        raise PrGateError("tracked retrieval graph build failed")
    result = benchmark(graph)
    if not isinstance(result, dict):
        raise PrGateError("retrieval benchmark did not return an object")
    _validate_retrieval_benchmark(result, graph=graph)
    final = sitecustomize.audit_snapshot()
    if (
        not sitecustomize.guard_self_check()
        or not run_closeout_tests._audit_is_pristine_and_unchanged(initial, final)
    ):
        raise PrGateError("retrieval offline-guard audit changed")
    return {
        "artifact_type": "where_papers_go_pr_retrieval_gate",
        "status": "passed",
        "case_count": 7,
        "full_recall_cases": 7,
        "micro_recall_at_k": 1.0,
        "case_definition_sha256": case_digest,
        "offline_guard_active": True,
        "nonloopback_python_socket_attempts": 0,
    }


def _run_retrieval_gate(
    manifest_path: Path, *, require_os_isolation: bool = False
) -> dict[str, Any]:
    manifest = _load_manifest(manifest_path)
    _validate_manifest(manifest)
    _verify_critical_files(manifest)
    os_isolation = _os_network_isolation_active(required=require_os_isolation)
    with tempfile.TemporaryDirectory(prefix="wpg-pr-retrieval-") as temporary:
        workspace = Path(temporary)
        os.chmod(workspace, 0o700)
        audit = workspace / "nonloopback-socket-attempts.log"
        result_path = workspace / "retrieval-result.json"
        initial = _private_empty_file(audit)
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                os.fspath(Path(__file__).resolve()),
                "_retrieval-child",
                "--manifest",
                os.fspath(manifest_path.resolve()),
                "--workspace",
                os.fspath(workspace),
                "--output",
                os.fspath(result_path),
            ],
            cwd=PROJECT_ROOT,
            env=_guarded_child_environment(os.fspath(audit)),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=300,
            check=False,
        )
        _verify_empty_audit(audit, initial)
        if completed.returncode != 0:
            raise PrGateError("fixed offline retrieval child failed")
        try:
            result = json.loads(result_path.read_text(encoding="ascii"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise PrGateError("retrieval child result is invalid") from exc
        if not isinstance(result, dict):
            raise PrGateError("retrieval child result must be an object")
        expected = {
            "artifact_type": "where_papers_go_pr_retrieval_gate",
            "status": "passed",
            "case_count": 7,
            "full_recall_cases": 7,
            "micro_recall_at_k": 1.0,
            "case_definition_sha256": RETRIEVAL_CASE_DEFINITION_SHA256,
            "offline_guard_active": True,
            "nonloopback_python_socket_attempts": 0,
        }
        if result != expected:
            raise PrGateError("retrieval child result differs from fixed evidence")
        return {
            **result,
            "offline_guard_audit_sha256": EMPTY_SHA256,
            "os_network_isolation": os_isolation,
        }


def _write_private_result(path: Path, value: Mapping[str, Any]) -> None:
    payload = _canonical_json(value)
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _run_static_gate(base: str, manifest_path: Path) -> dict[str, Any]:
    requirements_sha256 = _model_requirements_sha256()
    locked_versions = _model_lock_versions()
    wheel_build_versions = _wheel_build_lock_versions()
    manifest = _load_manifest(manifest_path)
    _validate_manifest(manifest)
    _verify_critical_files(manifest)
    fixture_count = _verify_credentials(manifest)
    _verify_logo_and_diff(base)
    return {
        "artifact_type": "where_papers_go_pr_static_gate",
        "status": "passed",
        "base": base,
        "critical_file_count": len(REQUIRED_CRITICAL_PATHS),
        "credential_findings": 0,
        "allowed_synthetic_fixture_findings": fixture_count,
        "logo_git_blob_sha1": LOGO_GIT_BLOB_SHA1,
        "logo_sha256": LOGO_SHA256,
        "diff_check": True,
        "model_requirements_sha256": requirements_sha256,
        "model_lock_sha256": MODEL_LOCK_SHA256,
        "model_lock_package_count": len(locked_versions),
        "wheel_build_lock_sha256": WHEEL_BUILD_LOCK_SHA256,
        "wheel_build_lock_package_count": len(wheel_build_versions),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    static_gate = subparsers.add_parser(
        "static", help="verify hashes, credentials, logo, and git diff"
    )
    static_gate.add_argument("--base", required=True)
    static_gate.add_argument("--manifest", type=Path, default=MANIFEST_PATH)

    tests_gate = subparsers.add_parser(
        "tests", help="run one fixed aggregate unittest gate"
    )
    tests_gate.add_argument(
        "--suite", choices=("full", "model-focused"), required=True
    )
    tests_gate.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    tests_gate.add_argument("--require-os-isolation", action="store_true")

    retrieval_gate = subparsers.add_parser(
        "retrieval", help="build and run the guarded fixed 7/7 retrieval gate"
    )
    retrieval_gate.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    retrieval_gate.add_argument("--require-os-isolation", action="store_true")

    subparsers.add_parser(
        "manifest-template",
        help="print the current fixed-manifest candidate without writing it",
    )

    child = subparsers.add_parser("_retrieval-child", help=argparse.SUPPRESS)
    child.add_argument("--manifest", type=Path, required=True)
    child.add_argument("--workspace", type=Path, required=True)
    child.add_argument("--output", type=Path, required=True)

    tests_child = subparsers.add_parser("_tests-child", help=argparse.SUPPRESS)
    tests_child.add_argument(
        "--suite", choices=("full", "model-focused"), required=True
    )
    tests_child.add_argument("--manifest", type=Path, required=True)
    tests_child.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        _require_isolated_startup()
        args = build_parser().parse_args(argv)
        if args.command == "static":
            result = _run_static_gate(args.base, args.manifest.resolve())
        elif args.command == "tests":
            result = _run_test_gate(
                args.suite,
                args.manifest.resolve(),
                require_os_isolation=args.require_os_isolation,
            )
        elif args.command == "retrieval":
            result = _run_retrieval_gate(
                args.manifest.resolve(),
                require_os_isolation=args.require_os_isolation,
            )
        elif args.command == "manifest-template":
            print(
                json.dumps(
                    _manifest_template(),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        elif args.command == "_retrieval-child":
            workspace = args.workspace.resolve()
            if not workspace.is_dir() or stat.S_IMODE(workspace.stat().st_mode) != 0o700:
                raise PrGateError("retrieval child workspace is unsafe")
            result = _retrieval_child(args.manifest.resolve(), workspace)
            _write_private_result(args.output.resolve(), result)
            return 0
        elif args.command == "_tests-child":
            return _tests_child(
                args.suite,
                args.manifest.resolve(),
                args.output.resolve(),
            )
        else:  # pragma: no cover - argparse enforces the command set.
            raise PrGateError("unsupported PR gate command")
    except (OSError, PrGateError, subprocess.SubprocessError) as exc:
        print(f"PR gate failed: {exc}", file=sys.stderr)
        return 1
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
