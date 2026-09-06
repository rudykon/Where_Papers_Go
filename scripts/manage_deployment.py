#!/usr/bin/env python3
"""Render, verify, and health-check the audited production deployment.

Rendering is a dry-run unless ``--apply`` is explicit. Existing output files
are preserved at timestamped backups before an atomic replacement; nothing is
deleted by this tool.
"""

from __future__ import annotations

import argparse
import base64
import configparser
import csv
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import compat32
import hashlib
import ipaddress
import io
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import ssl
import stat
import subprocess
import tempfile
import textwrap
import time
from typing import Any, Mapping, Sequence
import urllib.error
import urllib.request
import zipfile

try:  # pragma: no cover - the deployment target is Linux.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from where_paper_go.deployment_identity import (
    FORBIDDEN_SOURCE_COMPONENTS,
    SOURCE_ARTIFACT_TYPE,
    SOURCE_HEAD_ENV,
    SOURCE_MANIFEST_ENV,
    SOURCE_MANIFEST_FILE,
    SOURCE_MANIFEST_SHA256_ENV,
    SOURCE_TREE_ENV,
    atomic_rename_noreplace,
    process_start_ticks,
    validate_source_release,
)
from where_paper_go.paths import PROJECT_ROOT


SYSTEMD_TEMPLATE = PROJECT_ROOT / "deploy" / "systemd" / "where-papers-go.service.in"
NGINX_TEMPLATE = PROJECT_ROOT / "deploy" / "nginx" / "where-papers-go.conf.in"
NGINX_AUTHENTICATED_GATE_PORT = 18002
MONITOR_POLICY_RELATIVE = Path("deploy/monitoring/policy-v1.json")
MONITOR_SYSTEMD_SERVICE_RELATIVE = Path(
    "deploy/systemd/where-papers-go-monitor.service.in"
)
MONITOR_SYSTEMD_TIMER_RELATIVE = Path(
    "deploy/systemd/where-papers-go-monitor.timer.in"
)
MONITOR_SCRIPT_RELATIVE = Path("scripts/monitor_operations.py")
MONITOR_RENDERER_RELATIVE = Path("scripts/manage_deployment.py")
MONITOR_STATE_RELATIVE = Path(".local/state/where-papers-go/monitor")
MONITOR_SERVICE_NAME = "where-papers-go-monitor.service"
MONITOR_TIMER_NAME = "where-papers-go-monitor.timer"
MONITORED_SERVICE_NAME = "where-papers-go.service"
MONITOR_HEALTH_URL = "http://127.0.0.1:8001/api/health"
EXEC_BOUNDARY_UNSET_ENVIRONMENT = (
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
    "PYTHONPATH",
    "PYTHONPLATLIBDIR",
    "PYTHONPROFILEIMPORTTIME",
    "PYTHONPYCACHEPREFIX",
    "PYTHONSTARTUP",
    "PYTHONTRACEMALLOC",
    "PYTHONUSERBASE",
    "PYTHONWARNINGS",
)
GIT_BINARY = Path("/usr/bin/git")
READELF_BINARY = Path("/usr/bin/readelf")
LDD_BINARY = Path("/usr/bin/ldd")
EXPECTED_BACKEND = "lightrag_mix+property_graph_exact_vector+llm+search_api"
RUNTIME_MANIFEST = "runtime-shadow-manifest.json"
PYTHON_RUNTIME_MANIFEST = "python-runtime-manifest.json"
PYTHON_RUNTIME_ARTIFACT_TYPE = "where_papers_go_python_runtime"
PYTHON_RUNTIME_LOCK_ARTIFACT_TYPE = "where_papers_go_selected_wheel_lock"
PYTHON_RUNTIME_LOCK_DESTINATION = "provenance/dependency-lock"
PYTHON_RUNTIME_WHEEL_DIRECTORY = "provenance/wheels"
PRODUCTION_PYTHON_RUNTIME_LOCK = (
    PROJECT_ROOT
    / "deploy"
    / "python"
    / "selected-wheels-cpython-3.14.5-linux-x86_64.json"
)
PRODUCTION_PYTHON_RUNTIME_LOCK_SHA256 = (
    "f5057fc74abe9390884d4fe5a3ab77d01c2aa599ac50bf36d7bacd745c4d0f8b"
)
PYTHON_RUNTIME_ENV = "WPG_PYTHON_RUNTIME"
PYTHON_RUNTIME_MANIFEST_ENV = "WPG_PYTHON_RUNTIME_MANIFEST"
PYTHON_RUNTIME_MANIFEST_SHA256_ENV = "WPG_PYTHON_RUNTIME_MANIFEST_SHA256"
PYTHON_RUNTIME_TREE_SHA256_ENV = "WPG_PYTHON_RUNTIME_TREE_SHA256"
TAVILY_STATE_NAME = ".tavily_key_pool_state.json"
BACKEND_API_TOKEN_RELATIVE = Path(".config/where-papers-go/backend.token")
RUNTIME_LIGHTRAG_FILES = (
    "venue_import_manifest.json",
    "graph_chunk_entity_relation.graphml",
    "vdb_entities.json",
    "vdb_relationships.json",
    "vdb_chunks.json",
    "kv_store_text_chunks.json",
)
MAX_RUNTIME_SEED_BYTES = 2 * 1024 * 1024 * 1024
MAX_RUNTIME_SEED_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_RUNTIME_SEED_FILES = 100_000
MAX_SOURCE_RELEASE_BYTES = 2 * 1024 * 1024 * 1024
MAX_SOURCE_RELEASE_FILES = 100_000
MAX_PYTHON_RUNTIME_BYTES = 16 * 1024 * 1024 * 1024
MAX_PYTHON_RUNTIME_FILES = 500_000
MAX_PYTHON_RUNTIME_MANIFEST_BYTES = 256 * 1024 * 1024
MAX_PYTHON_RUNTIME_LOCK_BYTES = 16 * 1024 * 1024
SYSTEM_ABI_LIBRARY_NAMES = frozenset(
    {
        "libanl.so.1",
        "libc.so.6",
        "libcrypt.so.1",
        "libdl.so.2",
        "libgcc_s.so.1",
        "libm.so.6",
        "libpthread.so.0",
        "libresolv.so.2",
        "librt.so.1",
        "libstdc++.so.6",
        "libutil.so.1",
        "libz.so.1",
    }
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_replacement(value: Path | str, name: str) -> str:
    text = str(value)
    if not text or "\n" in text or "\r" in text:
        raise ValueError(f"{name} is empty or contains a newline")
    return text


def render_template(template: Path, replacements: Mapping[str, Path | str]) -> bytes:
    text = template.read_text(encoding="utf-8")
    for name, raw_value in replacements.items():
        marker = "@@" + name + "@@"
        if marker not in text:
            raise ValueError(f"template does not contain {marker}")
        text = text.replace(marker, _safe_replacement(raw_value, name))
    unresolved = sorted(set(__import__("re").findall(r"@@[A-Z0-9_]+@@", text)))
    if unresolved:
        raise ValueError("unresolved template markers: " + ", ".join(unresolved))
    return text.encode("utf-8")


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _fsync_directory(path: Path) -> None:
    """Persist one directory entry transition or surface an ambiguous install."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _set_mode_durable(path: Path, mode: int) -> None:
    """Set regular-file mode without following symlinks and persist metadata."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"refusing to chmod non-regular output: {path}")
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_directory(path: Path, *, create: bool) -> Path:
    """Resolve one owned, non-symlink directory with no group/world access."""

    expanded = path.expanduser()
    if create and not os.path.lexists(expanded):
        expanded.mkdir(parents=True, mode=0o700)
    try:
        info = expanded.lstat()
    except OSError as exc:
        raise ValueError(f"private runtime directory is missing: {expanded}") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise ValueError(
            f"runtime directory must be owned, real, and private: {expanded}"
        )
    return expanded.resolve()


def _git_output(project_root: Path, *arguments: str) -> bytes:
    """Run one read-only Git object query with a bounded, explicit argv."""

    completed = subprocess.run(
        [str(GIT_BINARY), "-C", str(project_root), *arguments],
        env={
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ATTR_NOSYSTEM": "1",
        },
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"Git source identity query failed: {detail[-1000:]}")
    return completed.stdout


def _source_release_path(raw_value: bytes) -> str:
    """Decode and constrain one tracked path before creating filesystem names."""

    try:
        value = raw_value.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("source release contains a non-UTF-8 Git path") from exc
    relative = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or relative.is_absolute()
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
        or any(part in FORBIDDEN_SOURCE_COMPONENTS for part in relative.parts)
        or any(part == SOURCE_MANIFEST_FILE for part in relative.parts)
        or relative.suffix.casefold() in {".pyc", ".pyo"}
    ):
        raise ValueError(f"source release contains an unsafe tracked path: {value!r}")
    return value


def _current_git_source_plan(
    project_root: Path,
) -> tuple[str, str, list[dict[str, Any]], bytes]:
    """Bind the current commit/tree to SHA-256 rows read only from Git objects."""

    project = project_root.expanduser().resolve()
    try:
        top_level = Path(
            _git_output(project, "rev-parse", "--show-toplevel")
            .decode("utf-8")
            .strip()
        ).resolve()
    except (UnicodeError, OSError) as exc:
        raise ValueError("Git project root is unavailable") from exc
    if top_level != project:
        raise ValueError("--project-root must be the Git top-level directory")
    head = (
        _git_output(project, "rev-parse", "--verify", "HEAD^{commit}")
        .decode("ascii")
        .strip()
        .casefold()
    )
    tree = (
        _git_output(project, "rev-parse", "--verify", "HEAD^{tree}")
        .decode("ascii")
        .strip()
        .casefold()
    )
    if len(head) not in {40, 64} or len(tree) not in {40, 64}:
        raise ValueError("Git commit/tree identity has an unsupported format")
    if any(character not in "0123456789abcdef" for character in head + tree):
        raise ValueError("Git commit/tree identity is not hexadecimal")

    rows: list[dict[str, Any]] = []
    total_bytes = 0
    entries = _git_output(project, "ls-tree", "-rz", "--full-tree", head)
    for raw_entry in entries.split(b"\0"):
        if not raw_entry:
            continue
        try:
            metadata, raw_path = raw_entry.split(b"\t", 1)
            raw_mode, raw_type, raw_object = metadata.split(b" ", 2)
        except ValueError as exc:
            raise ValueError("Git tree returned an invalid source entry") from exc
        mode = raw_mode.decode("ascii")
        object_type = raw_type.decode("ascii")
        object_id = raw_object.decode("ascii")
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise ValueError("source release supports only regular tracked files")
        relative = _source_release_path(raw_path)
        if len(rows) >= MAX_SOURCE_RELEASE_FILES:
            raise ValueError("source release file count exceeds its bound")
        try:
            declared_bytes = int(
                _git_output(project, "cat-file", "-s", object_id)
                .decode("ascii")
                .strip()
            )
        except (UnicodeError, ValueError) as exc:
            raise ValueError("Git source blob size is invalid") from exc
        if declared_bytes < 0 or total_bytes + declared_bytes > MAX_SOURCE_RELEASE_BYTES:
            raise ValueError("source release exceeds its cumulative byte bound")
        content = _git_output(project, "cat-file", "blob", object_id)
        if len(content) != declared_bytes:
            raise ValueError("Git source blob size changed while being read")
        total_bytes += len(content)
        rows.append(
            {
                "path": relative,
                "bytes": len(content),
                "sha256": _sha256_bytes(content),
                "mode": "0555" if mode == "100755" else "0444",
            }
        )
    rows.sort(key=lambda row: str(row["path"]))
    if not rows or len(rows) > MAX_SOURCE_RELEASE_FILES:
        raise ValueError("source release file count is empty or exceeds its bound")
    if total_bytes > MAX_SOURCE_RELEASE_BYTES:
        raise ValueError("source release exceeds its cumulative byte bound")
    binding_payload = json.dumps(
        {"source_head": head, "source_tree": tree, "files": rows},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    manifest = {
        "schema_version": 1,
        "artifact_type": SOURCE_ARTIFACT_TYPE,
        "source_head": head,
        "source_tree": tree,
        "source_binding_sha256": _sha256_bytes(binding_payload),
        "file_count": len(rows),
        "files": rows,
        "immutable_files": True,
        "forbidden_entries_excluded": True,
    }
    manifest_payload = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    return head, tree, rows, manifest_payload


def _write_source_blob(path: Path, payload: bytes, *, mode: int) -> None:
    """Create and durably publish one release file without following links."""

    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(path.parent, 0o700)
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
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while publishing source release")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_exclusive_blob(path: Path, payload: bytes, *, mode: int) -> None:
    """Create one durable file without mutating or creating its parent."""

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
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while publishing exclusive file")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def prepare_source_release(args: argparse.Namespace) -> dict[str, Any]:
    """Build the current Git commit into an immutable content-addressed release."""

    project_root = args.project_root.expanduser().resolve()
    head, tree, rows, manifest_payload = _current_git_source_plan(project_root)
    manifest_sha256 = _sha256_bytes(manifest_payload)
    release_root_expanded = args.release_root.expanduser().resolve(strict=False)
    releases_expanded = release_root_expanded / "releases"
    release = releases_expanded / f"release-{manifest_sha256}"
    total_bytes = sum(int(row["bytes"]) for row in rows)
    result: dict[str, Any] = {
        "kind": "source-release",
        "status": "dry-run",
        "head": head,
        "tree": tree,
        "manifest_sha256": manifest_sha256,
        "source_binding_sha256": json.loads(manifest_payload)[
            "source_binding_sha256"
        ],
        "file_count": len(rows),
        "bytes": total_bytes,
        "release": str(release),
        "manifest": str(release / SOURCE_MANIFEST_FILE),
    }
    if not args.apply:
        return result

    for protected in (project_root, (project_root / "data").resolve()):
        try:
            release_root_expanded.relative_to(protected)
        except ValueError:
            pass
        else:
            raise ValueError("source release root must be outside protected sources")
    release_root = _private_directory(args.release_root, create=True)
    releases = _private_directory(release_root / "releases", create=True)
    release = releases / f"release-{manifest_sha256}"
    result.update(
        {
            "release": str(release),
            "manifest": str(release / SOURCE_MANIFEST_FILE),
        }
    )
    if os.path.lexists(release):
        identity = validate_source_release(
            release / SOURCE_MANIFEST_FILE,
            expected_head=head,
            expected_tree=tree,
            expected_manifest_sha256=manifest_sha256,
        )
        result.update({"status": "already-built", "identity": identity})
        return result

    building = releases / f".{release.name}.{_timestamp()}.building"
    building.mkdir(mode=0o700)
    try:
        for row in rows:
            content = _git_output(
                project_root,
                "cat-file",
                "blob",
                _git_output(
                    project_root,
                    "rev-parse",
                    f"{head}:{row['path']}",
                )
                .decode("ascii")
                .strip(),
            )
            if (
                len(content) != row["bytes"]
                or _sha256_bytes(content) != row["sha256"]
            ):
                raise ValueError("Git source object drifted while building release")
            _write_source_blob(
                building / str(row["path"]),
                content,
                mode=int(str(row["mode"]), 8),
            )
        _write_source_blob(
            building / SOURCE_MANIFEST_FILE,
            manifest_payload,
            mode=0o400,
        )
        directories = [building, *(path for path in building.rglob("*") if path.is_dir())]
        for directory in sorted(
            directories, key=lambda path: len(path.parts), reverse=True
        ):
            os.chmod(directory, 0o555)
            _fsync_directory(directory)
        validate_source_release(
            building / SOURCE_MANIFEST_FILE,
            expected_head=head,
            expected_tree=tree,
            expected_manifest_sha256=manifest_sha256,
            require_content_addressed_name=False,
        )
        current_head = (
            _git_output(project_root, "rev-parse", "--verify", "HEAD^{commit}")
            .decode("ascii")
            .strip()
            .casefold()
        )
        current_tree = (
            _git_output(project_root, "rev-parse", "--verify", "HEAD^{tree}")
            .decode("ascii")
            .strip()
            .casefold()
        )
        if (current_head, current_tree) != (head, tree):
            raise ValueError("Git HEAD/tree changed while building source release")
        atomic_rename_noreplace(building, release)
        _fsync_directory(releases)
    except BaseException:
        # Preserve a private partial tree for diagnosis; never alter an older
        # content-addressed release.
        raise
    identity = validate_source_release(
        release / SOURCE_MANIFEST_FILE,
        expected_head=head,
        expected_tree=tree,
        expected_manifest_sha256=manifest_sha256,
    )
    result.update({"status": "built", "identity": identity})
    return result


def _python_runtime_relative_path(
    raw_value: Path | str,
    *,
    name: str,
    allow_root: bool = False,
) -> str:
    """Return one canonical POSIX path that cannot escape a runtime tree."""

    value = str(raw_value)
    if allow_root and value == ".":
        return value
    relative = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or "\n" in value
        or "\r" in value
        or relative.is_absolute()
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError(f"{name} is not a canonical runtime-relative path")
    return value


def _file_stability_tuple(info: os.stat_result) -> tuple[int, ...]:
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


def _stable_regular_file(
    path: Path,
    *,
    max_bytes: int = MAX_PYTHON_RUNTIME_BYTES,
) -> tuple[int, str, os.stat_result]:
    """Hash one no-follow regular file and reject concurrent mutation."""

    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"Python runtime input is not a regular file: {path}")
        if before.st_size < 0 or before.st_size > max_bytes:
            raise ValueError(f"Python runtime input exceeds its byte bound: {path}")
        digest = hashlib.sha256()
        total = 0
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > max_bytes:
                raise ValueError(f"Python runtime input exceeds its byte bound: {path}")
            digest.update(block)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if _file_stability_tuple(before) != _file_stability_tuple(after):
        raise ValueError(f"Python runtime input changed while being read: {path}")
    if total != before.st_size:
        raise ValueError(f"Python runtime input size changed while being read: {path}")
    return total, digest.hexdigest(), after


def _validate_runtime_shebang(
    path: Path, *, relative: str, expected_info: os.stat_result
) -> None:
    """Reject relocated console scripts that retain a non-system interpreter."""

    if PurePosixPath(relative).name == "pyvenv.cfg":
        raise ValueError("self-contained Python runtime must not contain pyvenv.cfg")
    if not stat.S_IMODE(expected_info.st_mode) & 0o111:
        return
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(descriptor)
        prefix = os.read(descriptor, 4096)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        _file_stability_tuple(before) != _file_stability_tuple(after)
        or _file_stability_tuple(before) != _file_stability_tuple(expected_info)
    ):
        raise ValueError(f"Python runtime script changed while inspected: {relative}")
    if not prefix.startswith(b"#!"):
        return
    raw_line, separator, _remainder = prefix.partition(b"\n")
    if not separator or len(raw_line) > 1024:
        raise ValueError(f"Python runtime script has an invalid shebang: {relative}")
    try:
        command = raw_line[2:].decode("utf-8").strip().split()[0]
    except (UnicodeError, IndexError) as exc:
        raise ValueError(f"Python runtime script has an invalid shebang: {relative}") from exc
    interpreter = PurePosixPath(command)
    if not interpreter.is_absolute() or interpreter.parts[:2] not in {
        ("/", "bin"),
        ("/", "usr"),
    }:
        raise ValueError(
            f"Python runtime script has an external absolute shebang: {relative}"
        )
    if interpreter.parts[:2] == ("/", "usr") and interpreter.parts[:3] != (
        "/",
        "usr",
        "bin",
    ):
        raise ValueError(
            f"Python runtime script has an external absolute shebang: {relative}"
        )


def _runtime_or_system_library(
    candidate: Path, *, runtime_root: Path, library_name: str
) -> str:
    """Canonicalize one loaded library or reject mutable non-ABI locations."""

    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"ELF dependency is unavailable: {library_name}") from exc
    try:
        relative = resolved.relative_to(runtime_root).as_posix()
    except ValueError:
        pass
    else:
        return "$RUNTIME/" + relative
    system_roots = {
        root.resolve()
        for root in (
            Path("/lib"),
            Path("/lib64"),
            Path("/usr/lib"),
            Path("/usr/lib64"),
        )
        if root.is_dir()
    }
    if not any(resolved.is_relative_to(root) for root in system_roots):
        raise ValueError(f"ELF dependency escaped the runtime: {library_name}")
    if library_name not in SYSTEM_ABI_LIBRARY_NAMES and not library_name.startswith(
        ("ld-linux", "ld-musl")
    ):
        raise ValueError(f"ELF dependency is not an allowed system ABI: {library_name}")
    return "$SYSTEM" + str(resolved)


def _resolved_system_library_roots() -> tuple[Path, ...]:
    roots = {
        root.resolve()
        for root in (
            Path("/lib"),
            Path("/lib64"),
            Path("/usr/lib"),
            Path("/usr/lib64"),
        )
        if root.is_dir()
    }
    if not roots:
        raise ValueError("no approved system ABI library root is available")
    return tuple(sorted(roots, key=lambda path: str(path)))


def _system_path_component_identity(
    path: Path, *, directory: bool
) -> dict[str, Any]:
    """Require one canonical path component to be root-owned and immutable."""

    try:
        resolved = path.resolve(strict=True)
        info = path.lstat()
    except OSError as exc:
        raise ValueError("system ABI path component is unavailable") from exc
    expected_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(
        info.st_mode
    )
    if (
        not path.is_absolute()
        or resolved != path
        or stat.S_ISLNK(info.st_mode)
        or not expected_type
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise ValueError("system ABI path component is not root-controlled")
    return {
        "path": str(path),
        "owner_uid": 0,
        "mode": f"{stat.S_IMODE(info.st_mode):04o}",
    }


def _trusted_system_directory_chain(directory: Path) -> list[dict[str, Any]]:
    if not directory.is_absolute() or directory.resolve(strict=True) != directory:
        raise ValueError("system ABI directory path is not fully resolved")
    if not any(
        directory.is_relative_to(root) for root in _resolved_system_library_roots()
    ):
        raise ValueError("system ABI directory is outside approved roots")
    directories = [Path("/")]
    current = Path("/")
    for component in directory.parts[1:]:
        current /= component
        directories.append(current)
    return [
        _system_path_component_identity(directory, directory=True)
        for directory in directories
    ]


def _trusted_system_library_directories(path: Path) -> list[dict[str, Any]]:
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("system ABI library path is not fully resolved")
    return _trusted_system_directory_chain(path.parent)


def _validate_opened_system_library(
    info: os.stat_result, *, expected_mode: int
) -> None:
    if (
        info.st_uid != 0
        or stat.S_IMODE(info.st_mode) != expected_mode
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise ValueError("opened system ABI library is not root-controlled")


def _stable_system_library_identity(
    path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Hash one system ABI file and its complete root-owned path chain."""

    directories_before = _trusted_system_library_directories(path)
    file_before = _system_path_component_identity(path, directory=False)
    path_before = path.lstat()
    size, digest, opened = _stable_regular_file(path)
    _validate_opened_system_library(
        opened, expected_mode=int(file_before["mode"], 8)
    )
    file_after = _system_path_component_identity(path, directory=False)
    path_after = path.lstat()
    directories_after = _trusted_system_library_directories(path)
    if (
        file_before != file_after
        or directories_before != directories_after
        or _file_stability_tuple(path_before) != _file_stability_tuple(opened)
        or _file_stability_tuple(path_before) != _file_stability_tuple(path_after)
    ):
        raise ValueError("system ABI library identity or permissions are unsafe")
    directory_binding = _sha256_bytes(
        json.dumps(
            directories_before,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return (
        {
            "path": str(path),
            "bytes": size,
            "mode": file_before["mode"],
            "owner_uid": 0,
            "sha256": digest,
            "trusted_directories_sha256": directory_binding,
        },
        directories_before,
    )


def _canonical_elf_search_path(
    raw_value: str, *, elf_path: Path, runtime_root: Path
) -> str:
    if not raw_value:
        raise ValueError("ELF RPATH/RUNPATH contains an empty component")
    origin = elf_path.parent
    expanded = raw_value.replace("${ORIGIN}", str(origin)).replace(
        "$ORIGIN", str(origin)
    )
    if "$" in expanded:
        raise ValueError("ELF RPATH/RUNPATH contains an unsupported loader variable")
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = origin / candidate
    resolved = candidate.resolve(strict=False)
    try:
        relative = resolved.relative_to(runtime_root).as_posix()
    except ValueError:
        system_roots = {
            root.resolve()
            for root in (
                Path("/lib"),
                Path("/lib64"),
                Path("/usr/lib"),
                Path("/usr/lib64"),
            )
            if root.is_dir()
        }
        if not any(resolved.is_relative_to(root) for root in system_roots):
            raise ValueError("ELF RPATH/RUNPATH escaped the runtime")
        if not resolved.is_dir() or resolved.is_symlink():
            raise ValueError("ELF RPATH/RUNPATH does not name a real system directory")
        return "$SYSTEM" + str(resolved)
    if not resolved.is_dir() or resolved.is_symlink():
        raise ValueError("ELF RPATH/RUNPATH does not name a real runtime directory")
    return "$RUNTIME/" + relative


def _canonical_elf_needed_path(
    raw_value: str, *, elf_path: Path, runtime_root: Path
) -> str:
    """Bind a path-valued DT_NEEDED entry to a relocatable runtime file."""

    if not raw_value.startswith(("$ORIGIN/", "${ORIGIN}/")):
        raise ValueError("ELF path-valued DT_NEEDED is not $ORIGIN-relative")
    origin = elf_path.parent
    expanded = raw_value.replace("${ORIGIN}", str(origin)).replace(
        "$ORIGIN", str(origin)
    )
    if "$" in expanded:
        raise ValueError("ELF DT_NEEDED contains an unsupported loader variable")
    try:
        resolved = Path(expanded).resolve(strict=True)
        relative = resolved.relative_to(runtime_root).as_posix()
        info = resolved.lstat()
    except (OSError, ValueError) as exc:
        raise ValueError("ELF DT_NEEDED escaped the runtime") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError("ELF DT_NEEDED does not name a real runtime file")
    return "$RUNTIME/" + relative


def _is_elf_file(path: Path) -> bool:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(descriptor)
        prefix = os.read(descriptor, 4)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if _file_stability_tuple(before) != _file_stability_tuple(after):
        raise ValueError("runtime file changed during ELF classification")
    return prefix == b"\x7fELF"


def _audit_one_elf(path: Path, *, runtime_root: Path) -> dict[str, Any]:
    relative = path.relative_to(runtime_root).as_posix()
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
    }
    try:
        dynamic = subprocess.run(
            [READELF_BINARY, "-dW", "--", path],
            cwd=runtime_root,
            env=environment,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"ELF dynamic-section audit failed: {relative}") from exc
    if dynamic.returncode != 0:
        raise ValueError(
            f"ELF dynamic-section audit failed: {relative}: "
            + dynamic.stderr.strip()[-500:]
        )
    needed = sorted(
        re.findall(r"\(NEEDED\).*?\[(.*?)\]", dynamic.stdout)
    )
    raw_search_paths: list[str] = []
    for raw_group in re.findall(
        r"\((?:RPATH|RUNPATH)\).*?\[(.*?)\]", dynamic.stdout
    ):
        raw_search_paths.extend(raw_group.split(":"))
    try:
        search_paths = [
            _canonical_elf_search_path(
                value, elf_path=path, runtime_root=runtime_root
            )
            for value in raw_search_paths
        ]
        needed_path_bindings = [
            {
                "needed": dependency,
                "path": _canonical_elf_needed_path(
                    dependency, elf_path=path, runtime_root=runtime_root
                ),
            }
            for dependency in needed
            if "/" in dependency
        ]
    except ValueError as exc:
        raise ValueError(f"{exc}: {relative}") from exc
    resolved_libraries: list[dict[str, str]] = []
    if needed:
        try:
            linked = subprocess.run(
                [LDD_BINARY, path],
                cwd=runtime_root,
                env=environment,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError(f"ELF loader audit failed: {relative}") from exc
        if linked.returncode != 0:
            raise ValueError(
                f"ELF loader audit failed: {relative}: "
                + linked.stderr.strip()[-500:]
            )
        for raw_line in linked.stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if "=>" in line:
                library_name, raw_target = (
                    component.strip() for component in line.split("=>", 1)
                )
                target = raw_target.split()[0]
                if target == "not":
                    raise ValueError(f"ELF dependency is unresolved: {library_name}")
            else:
                target = line.split()[0]
                if target.startswith("linux-vdso"):
                    resolved_libraries.append(
                        {"name": target, "path": "$KERNEL/linux-vdso"}
                    )
                    continue
                if not target.startswith("/"):
                    continue
                library_name = Path(target).name
            if not target.startswith("/"):
                raise ValueError(f"ELF loader returned a relative path: {relative}")
            resolved_libraries.append(
                {
                    "name": library_name,
                    "path": _runtime_or_system_library(
                        Path(target),
                        runtime_root=runtime_root,
                        library_name=library_name,
                    ),
                }
            )
        resolved_names = {row["name"] for row in resolved_libraries}
        resolved_paths = {row["path"] for row in resolved_libraries}
        plain_needed = [dependency for dependency in needed if "/" not in dependency]
        if any(dependency not in resolved_names for dependency in plain_needed) or any(
            row["path"] not in resolved_paths for row in needed_path_bindings
        ):
            raise ValueError(f"ELF dependency resolution is incomplete: {relative}")
    resolved_libraries.sort(key=lambda row: (row["name"], row["path"]))
    return {
        "path": relative,
        "needed": needed,
        "needed_path_bindings": needed_path_bindings,
        "search_paths": search_paths,
        "resolved_libraries": resolved_libraries,
    }


def _audit_python_runtime_elf(runtime_root: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for current, _directories, filenames in os.walk(
        runtime_root, topdown=True, followlinks=False
    ):
        for filename in sorted(filenames):
            path = Path(current) / filename
            if path.name == PYTHON_RUNTIME_MANIFEST:
                continue
            if _is_elf_file(path):
                rows.append(_audit_one_elf(path, runtime_root=runtime_root))
    rows.sort(key=lambda row: str(row["path"]))
    raw_system_paths = sorted(
        {
            str(library["path"])[len("$SYSTEM") :]
            for row in rows
            for library in row["resolved_libraries"]
            if str(library["path"]).startswith("$SYSTEM/")
        }
    )
    system_identities = [
        _stable_system_library_identity(Path(path)) for path in raw_system_paths
    ]
    system_libraries = [identity for identity, _directories in system_identities]
    directories_by_path: dict[str, dict[str, Any]] = {}
    for _identity, directories in system_identities:
        for directory in directories:
            path = str(directory["path"])
            existing = directories_by_path.get(path)
            if existing is not None and existing != directory:
                raise ValueError("system ABI directory trust identity is inconsistent")
            directories_by_path[path] = directory
    raw_system_search_paths = sorted(
        {
            str(search_path)[len("$SYSTEM") :]
            for row in rows
            for search_path in row["search_paths"]
            if str(search_path).startswith("$SYSTEM/")
        }
    )
    for search_path in raw_system_search_paths:
        for directory in _trusted_system_directory_chain(Path(search_path)):
            path = str(directory["path"])
            existing = directories_by_path.get(path)
            if existing is not None and existing != directory:
                raise ValueError("system ABI directory trust identity is inconsistent")
            directories_by_path[path] = directory
    system_directories = [
        directories_by_path[path] for path in sorted(directories_by_path)
    ]
    system_allowlist = sorted(SYSTEM_ABI_LIBRARY_NAMES)
    payload = json.dumps(
        {
            "files": rows,
            "system_abi_allowlist": system_allowlist,
            "system_directories": system_directories,
            "system_libraries": system_libraries,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "schema_version": 1,
        "file_count": len(rows),
        "files": rows,
        "system_directory_count": len(system_directories),
        "system_directories": system_directories,
        "system_library_count": len(system_libraries),
        "system_libraries": system_libraries,
        "binding_sha256": _sha256_bytes(payload),
        "system_abi_allowlist": system_allowlist,
    }


def _stable_regular_bytes(path: Path, *, max_bytes: int) -> tuple[bytes, os.stat_result]:
    """Read one small no-follow file with the same stability contract."""

    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"expected a regular file: {path}")
        if before.st_size < 0 or before.st_size > max_bytes:
            raise ValueError(f"file exceeds its byte bound: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > max_bytes:
                raise ValueError(f"file exceeds its byte bound: {path}")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if _file_stability_tuple(before) != _file_stability_tuple(after):
        raise ValueError(f"file changed while being read: {path}")
    payload = b"".join(chunks)
    if len(payload) != before.st_size:
        raise ValueError(f"file size changed while being read: {path}")
    return payload, after


def _real_input_directory(path: Path, *, name: str) -> Path:
    expanded = path.expanduser()
    try:
        info = expanded.lstat()
    except OSError as exc:
        raise ValueError(f"{name} is unavailable: {expanded}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"{name} must be a real directory: {expanded}")
    return expanded.resolve()


def _scan_python_prefix(
    source_prefix: Path,
) -> tuple[list[dict[str, str]], list[tuple[Path, dict[str, Any]]]]:
    """Inventory a symlink-free prefix without copying it yet."""

    directories: list[dict[str, str]] = [{"path": ".", "mode": "0555"}]
    copies: list[tuple[Path, dict[str, Any]]] = []
    pending = [source_prefix]
    total_bytes = 0
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            raise ValueError(f"cannot enumerate Python runtime prefix: {directory}") from exc
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(source_prefix).as_posix()
            _python_runtime_relative_path(relative, name="Python runtime entry")
            top_level = PurePosixPath(relative).parts[0]
            if top_level in {PYTHON_RUNTIME_MANIFEST, "provenance"}:
                raise ValueError(
                    f"Python runtime prefix uses reserved entry: {relative}"
                )
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ValueError(f"cannot inspect Python runtime entry: {path}") from exc
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"Python runtime prefix contains a symlink: {relative}")
            if stat.S_ISDIR(info.st_mode):
                directories.append({"path": relative, "mode": "0555"})
                pending.append(path)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(
                    f"Python runtime prefix contains a special file: {relative}"
                )
            if len(copies) >= MAX_PYTHON_RUNTIME_FILES:
                raise ValueError("Python runtime file count exceeds its bound")
            size, digest, stable = _stable_regular_file(path)
            _validate_runtime_shebang(
                path, relative=relative, expected_info=stable
            )
            total_bytes += size
            if total_bytes > MAX_PYTHON_RUNTIME_BYTES:
                raise ValueError("Python runtime exceeds its cumulative byte bound")
            mode = "0555" if stat.S_IMODE(stable.st_mode) & 0o111 else "0444"
            copies.append(
                (
                    path,
                    {
                        "path": relative,
                        "bytes": size,
                        "sha256": digest,
                        "mode": mode,
                    },
                )
            )
    directories.sort(key=lambda row: str(row["path"]))
    copies.sort(key=lambda pair: str(pair[1]["path"]))
    return directories, copies


def _probe_python_runtime_layout(
    runtime_root: Path, executable_relative: str
) -> dict[str, Any]:
    """Prove interpreter, /proc/exe, prefix and ABI identity in isolation."""

    executable = runtime_root / executable_relative
    program = textwrap.dedent(
        """\
        import hashlib
        import json
        import os
        import pathlib
        import platform
        import sys
        import sysconfig

        expected_executable = pathlib.Path(sys.argv[1]).resolve()
        expected_root = pathlib.Path(sys.argv[2]).resolve()
        process_executable = pathlib.Path("/proc/self/exe").resolve(strict=True)

        digest = hashlib.sha256()
        with process_executable.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)

        print(json.dumps({
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "version_info": {
                "major": sys.version_info.major,
                "minor": sys.version_info.minor,
                "micro": sys.version_info.micro,
                "releaselevel": sys.version_info.releaselevel,
                "serial": sys.version_info.serial,
            },
            "cache_tag": sys.implementation.cache_tag,
            "soabi": sysconfig.get_config_var("SOABI"),
            "platform": sysconfig.get_platform(),
            "sys_executable": str(pathlib.Path(sys.executable).resolve()),
            "proc_exe": str(process_executable),
            "proc_exe_sha256": digest.hexdigest(),
            "prefix": str(pathlib.Path(sys.prefix).resolve()),
            "base_prefix": str(pathlib.Path(sys.base_prefix).resolve()),
            "stdlib": sysconfig.get_path("stdlib"),
            "platstdlib": sysconfig.get_path("platstdlib"),
            "purelib": sysconfig.get_path("purelib"),
            "platlib": sysconfig.get_path("platlib"),
            "sys_path": list(sys.path),
            "expected_executable": str(expected_executable),
            "expected_root": str(expected_root),
        }, sort_keys=True, separators=(",", ":")))
        """
    )
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
    }
    try:
        completed = subprocess.run(
            [
                executable,
                "-I",
                "-S",
                "-P",
                "-B",
                "-c",
                program,
                str(executable),
                str(runtime_root),
            ],
            cwd=runtime_root,
            env=environment,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("self-contained Python runtime probe failed") from exc
    if completed.returncode != 0:
        raise ValueError(
            "self-contained Python runtime probe failed: "
            + completed.stderr.strip()[-1000:]
        )
    try:
        raw = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("self-contained Python runtime probe returned invalid JSON") from exc
    root = runtime_root.resolve()
    expected_executable = executable.resolve()
    if raw.get("implementation") != "CPython":
        raise ValueError("Python runtime must use CPython")
    if Path(str(raw.get("sys_executable", ""))).resolve() != expected_executable:
        raise ValueError("Python sys.executable escaped the runtime")
    if Path(str(raw.get("proc_exe", ""))).resolve() != expected_executable:
        raise ValueError("/proc/self/exe escaped the runtime")
    if Path(str(raw.get("prefix", ""))).resolve() != root:
        raise ValueError("Python sys.prefix escaped the runtime")
    if Path(str(raw.get("base_prefix", ""))).resolve() != root:
        raise ValueError("Python sys.base_prefix escaped the runtime")

    relative_paths: dict[str, str] = {}
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        candidate = Path(str(raw.get(key, ""))).resolve(strict=False)
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError(f"Python {key} escaped the runtime") from exc
        if not candidate.is_dir() or candidate.is_symlink():
            raise ValueError(f"Python {key} is not a real runtime directory")
        relative_paths[key] = _python_runtime_relative_path(
            relative, name=f"Python {key}"
        )

    isolated_sys_path: list[str] = []
    raw_sys_path = raw.get("sys_path")
    if not isinstance(raw_sys_path, list):
        raise ValueError("Python runtime probe returned an invalid sys.path")
    for raw_path in raw_sys_path:
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("isolated Python sys.path contains an unsafe entry")
        candidate = Path(raw_path).resolve(strict=False)
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError("isolated Python sys.path escaped the runtime") from exc
        isolated_sys_path.append(
            _python_runtime_relative_path(relative, name="isolated Python sys.path")
        )

    version = raw.get("version")
    version_info = raw.get("version_info")
    cache_tag = raw.get("cache_tag")
    soabi = raw.get("soabi")
    platform_name = raw.get("platform")
    if (
        not isinstance(version, str)
        or not version
        or not isinstance(version_info, dict)
        or not isinstance(cache_tag, str)
        or not cache_tag
        or not isinstance(soabi, str)
        or not soabi
        or not isinstance(platform_name, str)
        or not platform_name
    ):
        raise ValueError("Python runtime probe omitted version or ABI identity")
    proc_digest = str(raw.get("proc_exe_sha256", "")).casefold()
    if len(proc_digest) != 64 or any(c not in "0123456789abcdef" for c in proc_digest):
        raise ValueError("Python runtime probe returned an invalid /proc/exe hash")
    return {
        "implementation": "CPython",
        "version": version,
        "version_info": version_info,
        "cache_tag": cache_tag,
        "soabi": soabi,
        "platform": platform_name,
        "proc_exe_sha256": proc_digest,
        "stdlib": relative_paths["stdlib"],
        "platstdlib": relative_paths["platstdlib"],
        "purelib": relative_paths["purelib"],
        "platlib": relative_paths["platlib"],
        "isolated_sys_path": isolated_sys_path,
    }


def _python_lock_identity(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Select the interpreter fields that determine compatible wheels."""

    return {
        "implementation": metadata["implementation"],
        "version": metadata["version"],
        "version_info": metadata["version_info"],
        "cache_tag": metadata["cache_tag"],
        "soabi": metadata["soabi"],
        "platform": metadata["platform"],
    }


def _normalized_project_name(value: str) -> str:
    normalized = re.sub(r"[-_.]+", "-", value).casefold()
    if (
        not normalized
        or normalized[0] == "-"
        or normalized[-1] == "-"
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
            for character in normalized
        )
    ):
        raise ValueError("wheel metadata contains an invalid project name")
    return normalized


def _wheel_tag_compatible(tag: str, python_identity: Mapping[str, Any]) -> bool:
    """Conservatively recognize wheel tags usable by the locked CPython ABI."""

    components = tag.split("-")
    version_info = python_identity.get("version_info")
    if len(components) != 3 or not isinstance(version_info, Mapping):
        return False
    try:
        major = int(version_info["major"])
        minor = int(version_info["minor"])
    except (KeyError, TypeError, ValueError):
        return False
    if major != 3 or python_identity.get("implementation") != "CPython":
        return False
    exact_cp = f"cp{major}{minor}"
    exact_py = f"py{major}{minor}"
    target_platform = str(python_identity.get("platform", "")).replace("-", "_")
    if not target_platform:
        return False
    target_arch = target_platform.removeprefix("linux_")

    def manylinux_compatible(platform_tag: str) -> bool:
        policy_match = re.fullmatch(
            rf"manylinux(?:(1|2010|2014)|_([0-9]+)_([0-9]+))_{re.escape(target_arch)}",
            platform_tag,
        )
        if policy_match is None:
            return False
        if policy_match.group(1):
            required = {
                "1": (2, 5),
                "2010": (2, 12),
                "2014": (2, 17),
            }[policy_match.group(1)]
        else:
            required = (int(policy_match.group(2)), int(policy_match.group(3)))
        try:
            libc_identity = os.confstr("CS_GNU_LIBC_VERSION")
        except (OSError, ValueError):
            return False
        libc_match = re.fullmatch(r"glibc ([0-9]+)\.([0-9]+)", libc_identity or "")
        return bool(
            libc_match
            and (int(libc_match.group(1)), int(libc_match.group(2))) >= required
        )

    for interpreter in components[0].split("."):
        for abi in components[1].split("."):
            interpreter_compatible = False
            if interpreter in {f"py{major}", exact_py} and abi == "none":
                interpreter_compatible = True
            elif interpreter == exact_cp and abi in {exact_cp, "abi3", "none"}:
                interpreter_compatible = True
            elif interpreter.startswith("cp3") and abi == "abi3":
                prior_minor = interpreter[3:]
                interpreter_compatible = bool(
                    prior_minor.isdigit() and 2 <= int(prior_minor) <= minor
                )
            if not interpreter_compatible:
                continue
            for platform_tag in components[2].split("."):
                if platform_tag == "any" or platform_tag == target_platform:
                    return True
                if (
                    target_platform.startswith("linux_")
                    and manylinux_compatible(platform_tag)
                ):
                    return True
    return False


def _stable_wheel_identity(path: Path) -> dict[str, Any]:
    """Bind one wheel's archive metadata and bytes through a stable descriptor."""

    filename = _python_runtime_relative_path(path.name, name="wheel filename")
    if "/" in filename or not filename.casefold().endswith(".whl"):
        raise ValueError(f"wheelhouse contains a non-wheel entry: {filename}")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) & 0o111
            or before.st_size <= 0
            or before.st_size > MAX_PYTHON_RUNTIME_BYTES
        ):
            raise ValueError(f"wheel is not a bounded non-executable file: {filename}")
        with os.fdopen(os.dup(descriptor), "rb") as wheel_handle:
            try:
                with zipfile.ZipFile(wheel_handle) as archive:
                    entries = archive.infolist()
                    names = [entry.filename for entry in entries]
                    if len(names) != len(set(names)):
                        raise ValueError("wheel archive contains duplicate members")
                    for entry in entries:
                        archive_name = entry.filename.rstrip("/")
                        if archive_name:
                            _python_runtime_relative_path(
                                archive_name, name="wheel archive member"
                            )
                    metadata_entries = [
                        entry
                        for entry in entries
                        if entry.filename.count("/") == 1
                        and entry.filename.endswith(".dist-info/METADATA")
                    ]
                    wheel_entries = [
                        entry
                        for entry in entries
                        if entry.filename.count("/") == 1
                        and entry.filename.endswith(".dist-info/WHEEL")
                    ]
                    if (
                        len(metadata_entries) != 1
                        or len(wheel_entries) != 1
                        or metadata_entries[0].filename.rpartition("/")[0]
                        != wheel_entries[0].filename.rpartition("/")[0]
                    ):
                        raise ValueError(
                            "wheel archive must contain one matching METADATA/WHEEL pair"
                        )
                    if (
                        metadata_entries[0].file_size > 1024 * 1024
                        or wheel_entries[0].file_size > 1024 * 1024
                    ):
                        raise ValueError("wheel metadata exceeds its byte bound")
                    metadata_payload = archive.read(metadata_entries[0])
                    wheel_payload = archive.read(wheel_entries[0])
            except (KeyError, RuntimeError, zipfile.BadZipFile) as exc:
                raise ValueError(f"wheel archive is invalid: {filename}") from exc
        metadata_message = BytesParser(policy=compat32).parsebytes(metadata_payload)
        wheel_message = BytesParser(policy=compat32).parsebytes(wheel_payload)
        metadata_names = metadata_message.get_all("Name", [])
        metadata_versions = metadata_message.get_all("Version", [])
        wheel_versions = wheel_message.get_all("Wheel-Version", [])
        if (
            metadata_message.defects
            or wheel_message.defects
            or len(metadata_names) != 1
            or len(metadata_versions) != 1
            or len(wheel_versions) != 1
        ):
            raise ValueError(f"wheel metadata headers are ambiguous: {filename}")
        project_name = str(metadata_names[0])
        version = str(metadata_versions[0])
        wheel_version = str(wheel_versions[0])
        tags = sorted(set(str(value) for value in wheel_message.get_all("Tag", [])))
        if (
            not project_name
            or project_name.strip() != project_name
            or "\n" in project_name
            or "\r" in project_name
            or not version
            or version.strip() != version
            or any(character.isspace() for character in version)
            or not wheel_version.startswith("1.")
            or not tags
            or any(
                not tag
                or tag.strip() != tag
                or any(character.isspace() for character in tag)
                for tag in tags
            )
        ):
            raise ValueError(f"wheel metadata is incomplete or invalid: {filename}")
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        total = 0
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            total += len(block)
            digest.update(block)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        _file_stability_tuple(before) != _file_stability_tuple(after)
        or total != before.st_size
    ):
        raise ValueError(f"wheel changed while being inspected: {filename}")
    return {
        "filename": filename,
        "name": project_name,
        "normalized_name": _normalized_project_name(project_name),
        "version": version,
        "bytes": total,
        "sha256": digest.hexdigest(),
        "tags": tags,
    }


def _selected_wheel_inventory(
    wheelhouse: Path,
) -> list[tuple[Path, dict[str, Any]]]:
    root = _real_input_directory(wheelhouse, name="wheelhouse")
    try:
        with os.scandir(root) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
    except OSError as exc:
        raise ValueError("cannot enumerate wheelhouse") from exc
    inventory: list[tuple[Path, dict[str, Any]]] = []
    for entry in entries:
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise ValueError(f"cannot inspect wheelhouse entry: {entry.name}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError(f"wheelhouse contains a non-wheel entry: {entry.name}")
        path = Path(entry.path)
        inventory.append((path, _stable_wheel_identity(path)))
    if not inventory:
        raise ValueError("wheelhouse must contain at least one selected wheel")
    if len(inventory) > MAX_PYTHON_RUNTIME_FILES:
        raise ValueError("wheelhouse file count exceeds its bound")
    rows = [row for _path, row in inventory]
    if len({str(row["filename"]) for row in rows}) != len(rows):
        raise ValueError("wheelhouse contains duplicate filenames")
    if len({str(row["normalized_name"]) for row in rows}) != len(rows):
        raise ValueError("wheelhouse contains multiple wheels for one project")
    if len({str(row["sha256"]) for row in rows}) != len(rows):
        raise ValueError("wheelhouse contains duplicate wheel content")
    if sum(int(row["bytes"]) for row in rows) > MAX_PYTHON_RUNTIME_BYTES:
        raise ValueError("wheelhouse exceeds its cumulative byte bound")
    return inventory


def _record_sha256(raw_value: str) -> str:
    algorithm, separator, encoded = raw_value.partition("=")
    if algorithm != "sha256" or not separator or not encoded or "=" in encoded:
        raise ValueError("installed RECORD uses a non-canonical SHA-256")
    try:
        padding = "=" * (-len(encoded) % 4)
        decoded = base64.urlsafe_b64decode(encoded + padding)
    except (ValueError, TypeError) as exc:
        raise ValueError("installed RECORD SHA-256 is invalid") from exc
    if (
        len(decoded) != 32
        or base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != encoded
    ):
        raise ValueError("installed RECORD SHA-256 is invalid")
    return decoded.hex()


def _metadata_name_version(payload: bytes, *, name: str) -> tuple[str, str]:
    message = BytesParser(policy=compat32).parsebytes(payload)
    names = message.get_all("Name", [])
    versions = message.get_all("Version", [])
    if message.defects or len(names) != 1 or len(versions) != 1:
        raise ValueError(f"{name} has ambiguous Name/Version headers")
    project_name = str(names[0])
    version = str(versions[0])
    if (
        not project_name
        or project_name.strip() != project_name
        or "\n" in project_name
        or "\r" in project_name
        or not version
        or version.strip() != version
        or any(character.isspace() for character in version)
    ):
        raise ValueError(f"{name} has invalid Name/Version headers")
    return project_name, version


def _zip_member_sha256(
    archive: zipfile.ZipFile, entry: zipfile.ZipInfo
) -> tuple[int, str]:
    if entry.file_size < 0 or entry.file_size > MAX_PYTHON_RUNTIME_BYTES:
        raise ValueError("wheel member exceeds its byte bound")
    digest = hashlib.sha256()
    total = 0
    with archive.open(entry, "r") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > MAX_PYTHON_RUNTIME_BYTES:
                raise ValueError("wheel member exceeds its byte bound")
            digest.update(block)
    if total != entry.file_size:
        raise ValueError("wheel member size changed while read")
    return total, digest.hexdigest()


def _locked_wheel_installation_identity(
    wheel_path: Path,
    expected_wheel: Mapping[str, Any],
    *,
    runtime_root: Path,
    purelib: Path,
    platlib: Path,
) -> tuple[dict[str, Any], set[str], dict[str, str]]:
    """Verify every locked wheel member installed into its target scheme."""

    if _stable_wheel_identity(wheel_path) != dict(expected_wheel):
        raise ValueError("locked wheel changed before installation verification")
    descriptor = os.open(
        wheel_path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size != expected_wheel["bytes"]
            or stat.S_IMODE(before.st_mode) & 0o111
        ):
            raise ValueError("locked wheel file identity is unsafe")
        with os.fdopen(os.dup(descriptor), "rb") as wheel_handle:
            try:
                with zipfile.ZipFile(wheel_handle) as archive:
                    entries = archive.infolist()
                    names = [entry.filename for entry in entries]
                    if len(names) != len(set(names)):
                        raise ValueError("wheel archive contains duplicate members")
                    metadata_entries = [
                        entry
                        for entry in entries
                        if entry.filename.count("/") == 1
                        and entry.filename.endswith(".dist-info/METADATA")
                    ]
                    wheel_entries = [
                        entry
                        for entry in entries
                        if entry.filename.count("/") == 1
                        and entry.filename.endswith(".dist-info/WHEEL")
                    ]
                    record_entries = [
                        entry
                        for entry in entries
                        if entry.filename.count("/") == 1
                        and entry.filename.endswith(".dist-info/RECORD")
                    ]
                    if not (
                        len(metadata_entries)
                        == len(wheel_entries)
                        == len(record_entries)
                        == 1
                    ):
                        raise ValueError("wheel must contain one METADATA/WHEEL/RECORD")
                    if wheel_entries[0].file_size > 1024 * 1024:
                        raise ValueError("wheel metadata exceeds its byte bound")
                    dist_info = metadata_entries[0].filename.rpartition("/")[0]
                    if any(
                        entry.filename.rpartition("/")[0] != dist_info
                        for entry in (*wheel_entries, *record_entries)
                    ):
                        raise ValueError("wheel metadata directories differ")
                    wheel_payload = archive.read(wheel_entries[0])
                    wheel_message = BytesParser(policy=compat32).parsebytes(
                        wheel_payload
                    )
                    root_headers = wheel_message.get_all("Root-Is-Purelib", [])
                    if wheel_message.defects or len(root_headers) != 1:
                        raise ValueError("wheel Root-Is-Purelib header is invalid")
                    root_value = str(root_headers[0]).casefold()
                    if root_value not in {"true", "false"}:
                        raise ValueError("wheel Root-Is-Purelib header is invalid")
                    install_root = purelib if root_value == "true" else platlib

                    authorized_scripts: dict[str, str] = {}
                    entry_points = [
                        entry
                        for entry in entries
                        if entry.filename == f"{dist_info}/entry_points.txt"
                    ]
                    if len(entry_points) > 1:
                        raise ValueError("wheel contains duplicate entry_points.txt")
                    if entry_points:
                        if entry_points[0].file_size > 1024 * 1024:
                            raise ValueError("wheel entry_points.txt is too large")
                        try:
                            entry_text = archive.read(entry_points[0]).decode("utf-8")
                            parser = configparser.ConfigParser(
                                interpolation=None,
                                strict=True,
                            )
                            parser.optionxform = str
                            parser.read_string(entry_text)
                        except (UnicodeError, configparser.Error) as exc:
                            raise ValueError("wheel entry_points.txt is invalid") from exc
                        for section in ("console_scripts", "gui_scripts"):
                            if not parser.has_section(section):
                                continue
                            for script_name in parser[section]:
                                normalized_script = _python_runtime_relative_path(
                                    script_name,
                                    name="wheel entry-point script",
                                )
                                if "/" in normalized_script or script_name.strip() != script_name:
                                    raise ValueError("wheel entry-point name is invalid")
                                if normalized_script in authorized_scripts:
                                    raise ValueError("wheel entry-point name is duplicate")
                                authorized_scripts[normalized_script] = section

                    installed_rows: list[dict[str, Any]] = []
                    expected_paths: set[str] = set()
                    data_prefix = dist_info.removesuffix(".dist-info") + ".data"
                    for entry in sorted(entries, key=lambda value: value.filename):
                        archive_name = entry.filename.rstrip("/")
                        if not archive_name:
                            continue
                        _python_runtime_relative_path(
                            archive_name, name="wheel archive member"
                        )
                        if entry.is_dir():
                            continue
                        member_size, member_sha256 = _zip_member_sha256(
                            archive, entry
                        )
                        parts = PurePosixPath(archive_name).parts
                        if parts[0].endswith(".data"):
                            if parts[0] != data_prefix or len(parts) < 3:
                                raise ValueError("wheel .data member path is invalid")
                            scheme = parts[1]
                            remainder = PurePosixPath(*parts[2:]).as_posix()
                            if scheme == "scripts":
                                if "/" in remainder:
                                    raise ValueError("wheel script is not a basename")
                                if remainder in authorized_scripts:
                                    raise ValueError("wheel script name is duplicate")
                                authorized_scripts[remainder] = "wheel_data_script"
                                continue
                            if scheme == "purelib":
                                target = purelib / remainder
                            elif scheme == "platlib":
                                target = platlib / remainder
                            else:
                                raise ValueError("wheel .data install scheme is unsupported")
                        else:
                            target = install_root / archive_name
                        try:
                            runtime_relative = target.resolve(strict=False).relative_to(
                                runtime_root
                            ).as_posix()
                        except ValueError as exc:
                            raise ValueError("wheel member install path escaped runtime") from exc
                        if runtime_relative in expected_paths:
                            raise ValueError("wheel maps duplicate installed file paths")
                        expected_paths.add(runtime_relative)
                        if archive_name == f"{dist_info}/RECORD":
                            continue
                        installed_size, installed_sha256, _info = _stable_regular_file(
                            target
                        )
                        if (
                            installed_size != member_size
                            or installed_sha256 != member_sha256
                        ):
                            raise ValueError(
                                "installed file differs from locked wheel member: "
                                + runtime_relative
                            )
                        installed_rows.append(
                            {
                                "archive_path": archive_name,
                                "runtime_path": runtime_relative,
                                "bytes": installed_size,
                                "sha256": installed_sha256,
                            }
                        )
            except (KeyError, RuntimeError, zipfile.BadZipFile) as exc:
                raise ValueError("locked wheel archive is invalid") from exc
        os.lseek(descriptor, 0, os.SEEK_SET)
        wheel_digest = hashlib.sha256()
        wheel_bytes = 0
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            wheel_bytes += len(block)
            wheel_digest.update(block)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        _file_stability_tuple(before) != _file_stability_tuple(after)
        or wheel_bytes != expected_wheel["bytes"]
        or wheel_digest.hexdigest() != expected_wheel["sha256"]
    ):
        raise ValueError("locked wheel changed during installation verification")
    installed_payload = json.dumps(
        installed_rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    identity = {
        "filename": expected_wheel["filename"],
        "sha256": expected_wheel["sha256"],
        "installed_file_count": len(installed_rows),
        "installed_file_bytes": sum(row["bytes"] for row in installed_rows),
        "installed_files_sha256": _sha256_bytes(installed_payload),
        "authorized_entry_points": [
            {"name": name, "source": authorized_scripts[name]}
            for name in sorted(authorized_scripts)
        ],
    }
    return identity, expected_paths, authorized_scripts


def _installed_distributions_identity(
    runtime_root: Path,
    python_identity: Mapping[str, Any],
    wheel_inventory: Sequence[tuple[Path, Mapping[str, Any]]],
) -> dict[str, Any]:
    """Bind the exact installed tree to locked wheels and verified RECORDs."""

    purelib_relative = _python_runtime_relative_path(
        str(python_identity["purelib"]), name="Python purelib"
    )
    platlib_relative = _python_runtime_relative_path(
        str(python_identity["platlib"]), name="Python platlib"
    )
    stdlib_relative = _python_runtime_relative_path(
        str(python_identity["stdlib"]), name="Python stdlib"
    )
    purelib = runtime_root / purelib_relative
    platlib = runtime_root / platlib_relative
    install_roots = sorted({purelib, platlib}, key=lambda path: str(path))
    if any(not path.is_dir() or path.is_symlink() for path in install_roots):
        raise ValueError("installed distribution root is not a real directory")
    if os.path.lexists(runtime_root / stdlib_relative / "ensurepip"):
        raise ValueError("production Python runtime must not contain ensurepip")
    if any(os.path.lexists(path / "pip") for path in install_roots):
        raise ValueError("production Python runtime must not contain pip")
    if any(os.path.lexists(path / "bin") for path in install_roots):
        raise ValueError("production Python runtime must not contain site-packages/bin")
    runtime_bin = runtime_root / "bin"
    if runtime_bin.is_dir():
        for candidate in runtime_bin.iterdir():
            if re.fullmatch(r"pip(?:[0-9]+(?:\.[0-9]+)?)?", candidate.name):
                raise ValueError("production Python runtime must not contain pip scripts")

    wheel_rows = [dict(row) for _path, row in wheel_inventory]
    locked_by_name = {str(row["normalized_name"]): row for row in wheel_rows}
    wheel_paths_by_name = {
        str(row["normalized_name"]): path
        for path, row in wheel_inventory
    }
    wheel_installations: dict[str, dict[str, Any]] = {}
    wheel_expected_paths: dict[str, set[str]] = {}
    wheel_scripts: dict[str, dict[str, str]] = {}
    global_wheel_paths: set[str] = set()
    for normalized_name in sorted(locked_by_name):
        identity, expected_paths, authorized_scripts = (
            _locked_wheel_installation_identity(
                wheel_paths_by_name[normalized_name],
                locked_by_name[normalized_name],
                runtime_root=runtime_root,
                purelib=purelib,
                platlib=platlib,
            )
        )
        if global_wheel_paths.intersection(expected_paths):
            raise ValueError("locked wheels overlap installed file paths")
        global_wheel_paths.update(expected_paths)
        wheel_installations[normalized_name] = identity
        wheel_expected_paths[normalized_name] = expected_paths
        wheel_scripts[normalized_name] = authorized_scripts

    dist_info_directories: list[Path] = []
    for install_root in install_roots:
        for candidate in install_root.iterdir():
            if candidate.name.endswith(".egg-info"):
                raise ValueError("installed .egg-info distributions are forbidden")
            if candidate.name.endswith(".dist-info"):
                if not candidate.is_dir() or candidate.is_symlink():
                    raise ValueError("installed dist-info is not a real directory")
                dist_info_directories.append(candidate)
    if len({path.resolve() for path in dist_info_directories}) != len(
        dist_info_directories
    ):
        raise ValueError("installed dist-info directories are duplicated")

    distributions: list[dict[str, Any]] = []
    installed_names: set[str] = set()
    globally_recorded_paths: set[str] = set()
    total_record_entries = 0
    total_hashed_files = 0
    total_omissions = 0
    for dist_info in sorted(dist_info_directories, key=lambda path: str(path)):
        metadata_path = dist_info / "METADATA"
        record_path = dist_info / "RECORD"
        metadata_payload, _metadata_info = _stable_regular_bytes(
            metadata_path, max_bytes=1024 * 1024
        )
        record_payload, _record_info = _stable_regular_bytes(
            record_path, max_bytes=64 * 1024 * 1024
        )
        project_name, version = _metadata_name_version(
            metadata_payload,
            name="installed distribution METADATA",
        )
        normalized_name = _normalized_project_name(project_name)
        locked = locked_by_name.get(normalized_name)
        if (
            locked is None
            or normalized_name in installed_names
            or project_name != locked["name"]
            or version != locked["version"]
        ):
            raise ValueError(
                "installed distribution Name/Version differs from selected lock"
            )
        installed_names.add(normalized_name)
        try:
            record_text = record_payload.decode("utf-8")
            raw_record_rows = list(csv.reader(io.StringIO(record_text, newline="")))
        except (UnicodeError, csv.Error) as exc:
            raise ValueError("installed distribution RECORD is invalid") from exc
        if not raw_record_rows or len(raw_record_rows) > MAX_PYTHON_RUNTIME_FILES:
            raise ValueError("installed distribution RECORD count is invalid")

        record_rows: list[dict[str, Any]] = []
        record_paths_seen: set[str] = set()
        present_runtime_paths: set[str] = set()
        omitted_entry_points: list[dict[str, Any]] = []
        for raw_row in raw_record_rows:
            if len(raw_row) != 3:
                raise ValueError("installed distribution RECORD row is invalid")
            raw_path, raw_digest, raw_size = raw_row
            if raw_path in record_paths_seen:
                raise ValueError("installed distribution RECORD path is duplicate")
            record_paths_seen.add(raw_path)
            record_parts = PurePosixPath(raw_path).parts
            is_script = (
                len(record_parts) == 4
                and record_parts[:3] == ("..", "..", "bin")
                and record_parts[3] not in {"", ".", ".."}
            )
            if is_script:
                script_name = _python_runtime_relative_path(
                    record_parts[3], name="installed entry-point script"
                )
                runtime_relative = f"bin/{script_name}"
                target = runtime_root / runtime_relative
            else:
                canonical_record_path = _python_runtime_relative_path(
                    raw_path, name="installed RECORD path"
                )
                install_root = purelib if dist_info.is_relative_to(purelib) else platlib
                target = install_root / canonical_record_path
                try:
                    runtime_relative = target.resolve(strict=False).relative_to(
                        runtime_root
                    ).as_posix()
                except ValueError as exc:
                    raise ValueError("installed RECORD path escaped runtime") from exc

            if bool(raw_digest) != bool(raw_size):
                raise ValueError("installed RECORD hash/size fields are incomplete")
            if raw_digest:
                recorded_sha256 = _record_sha256(raw_digest)
                if not raw_size.isdigit() or str(int(raw_size)) != raw_size:
                    raise ValueError("installed RECORD size is not canonical")
                recorded_size = int(raw_size)
                if target.is_file() and not target.is_symlink():
                    observed_size, observed_sha256, _info = _stable_regular_file(target)
                    if (
                        observed_size != recorded_size
                        or observed_sha256 != recorded_sha256
                    ):
                        raise ValueError(
                            "installed file differs from its RECORD: "
                            + runtime_relative
                        )
                    if is_script:
                        raise ValueError(
                            "distribution entry-point scripts must be omitted"
                        )
                    present_runtime_paths.add(runtime_relative)
                    record_rows.append(
                        {
                            "record_path": raw_path,
                            "runtime_path": runtime_relative,
                            "bytes": observed_size,
                            "sha256": observed_sha256,
                            "present": True,
                        }
                    )
                else:
                    authorization = wheel_scripts[normalized_name].get(
                        record_parts[3] if is_script else ""
                    )
                    if not is_script or authorization is None or os.path.lexists(target):
                        raise ValueError(
                            "hashed installed RECORD file is unexpectedly missing"
                        )
                    omission = {
                        "record_path": raw_path,
                        "runtime_path": runtime_relative,
                        "bytes": recorded_size,
                        "sha256": recorded_sha256,
                        "authorization": authorization,
                    }
                    omitted_entry_points.append(omission)
                    record_rows.append({**omission, "present": False})
            else:
                expected_record_relative = record_path.relative_to(
                    runtime_root
                ).as_posix()
                if raw_path != dist_info.name + "/RECORD" or (
                    runtime_relative != expected_record_relative
                ):
                    raise ValueError(
                        "only RECORD itself may omit its hash and size"
                    )
                observed_size, observed_sha256, _info = _stable_regular_file(target)
                present_runtime_paths.add(runtime_relative)
                record_rows.append(
                    {
                        "record_path": raw_path,
                        "runtime_path": runtime_relative,
                        "bytes": observed_size,
                        "sha256": observed_sha256,
                        "present": True,
                    }
                )

        authorized_names = set(wheel_scripts[normalized_name])
        omitted_names = {
            PurePosixPath(row["runtime_path"]).name
            for row in omitted_entry_points
        }
        if omitted_names != authorized_names:
            raise ValueError(
                "omitted entry points differ from the locked wheel declarations"
            )
        generated_paths = {
            (dist_info / filename).relative_to(runtime_root).as_posix()
            for filename in ("INSTALLER", "REQUESTED")
        }
        if present_runtime_paths != wheel_expected_paths[normalized_name] | generated_paths:
            raise ValueError(
                "installed RECORD paths differ from the locked wheel file set"
            )
        if globally_recorded_paths.intersection(present_runtime_paths):
            raise ValueError("installed distributions overlap recorded files")
        globally_recorded_paths.update(present_runtime_paths)
        record_rows.sort(key=lambda row: str(row["record_path"]))
        record_binding = _sha256_bytes(
            json.dumps(
                record_rows,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        distribution = {
            "name": project_name,
            "normalized_name": normalized_name,
            "version": version,
            "dist_info": dist_info.relative_to(runtime_root).as_posix(),
            "metadata": {
                "path": metadata_path.relative_to(runtime_root).as_posix(),
                "bytes": len(metadata_payload),
                "sha256": _sha256_bytes(metadata_payload),
            },
            "record": {
                "path": record_path.relative_to(runtime_root).as_posix(),
                "bytes": len(record_payload),
                "sha256": _sha256_bytes(record_payload),
                "entry_count": len(record_rows),
                "hashed_file_count": sum(bool(row[1]) for row in raw_record_rows),
                "verified_present_file_count": sum(
                    bool(row["present"]) for row in record_rows
                ),
                "files_binding_sha256": record_binding,
            },
            "locked_wheel_installation": wheel_installations[normalized_name],
            "omitted_entry_points": sorted(
                omitted_entry_points, key=lambda row: str(row["record_path"])
            ),
        }
        distributions.append(distribution)
        total_record_entries += len(record_rows)
        total_hashed_files += distribution["record"]["hashed_file_count"]
        total_omissions += len(omitted_entry_points)

    if installed_names != set(locked_by_name):
        raise ValueError("installed distribution set differs from selected lock")
    actual_install_files = {
        path.relative_to(runtime_root).as_posix()
        for install_root in install_roots
        for path in install_root.rglob("*")
        if path.is_file()
    }
    if actual_install_files != globally_recorded_paths:
        raise ValueError("installed package tree contains unrecorded files")
    distributions.sort(key=lambda row: str(row["normalized_name"]))
    binding_payload = json.dumps(
        {
            "install_roots": [
                path.relative_to(runtime_root).as_posix() for path in install_roots
            ],
            "distributions": distributions,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "schema_version": 1,
        "install_roots": [
            path.relative_to(runtime_root).as_posix() for path in install_roots
        ],
        "distribution_count": len(distributions),
        "record_entry_count": total_record_entries,
        "record_hashed_file_count": total_hashed_files,
        "omitted_entry_point_count": total_omissions,
        "distributions": distributions,
        "binding_sha256": _sha256_bytes(binding_payload),
        "exactly_matches_locked_wheels": True,
        "record_files_verified": True,
        "pip_bootstrap_absent": True,
    }


def _validate_selected_wheel_lock(
    payload: bytes,
    *,
    expected_python: Mapping[str, Any] | None = None,
    expected_wheels: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    try:
        document = _json_object_without_duplicate_keys(
            payload, name="selected wheel lock"
        )
    except ValueError as exc:
        raise ValueError(
            "dependency lock must be canonical selected-wheel JSON, not uv.lock"
        ) from exc
    if set(document) != {
        "schema_version",
        "artifact_type",
        "python",
        "wheel_count",
        "wheels",
    } or document.get("schema_version") != 1 or document.get(
        "artifact_type"
    ) != PYTHON_RUNTIME_LOCK_ARTIFACT_TYPE:
        raise ValueError(
            "dependency lock must be a canonical selected-wheel lock, not uv.lock"
        )
    python_identity = document.get("python")
    if not isinstance(python_identity, dict) or set(python_identity) != {
        "implementation",
        "version",
        "version_info",
        "cache_tag",
        "soabi",
        "platform",
    }:
        raise ValueError("selected wheel lock Python identity is invalid")
    if (
        python_identity.get("implementation") != "CPython"
        or any(
            not isinstance(python_identity.get(field), str)
            or not python_identity[field]
            for field in ("version", "cache_tag", "soabi", "platform")
        )
        or not isinstance(python_identity.get("version_info"), dict)
    ):
        raise ValueError("selected wheel lock Python identity is incomplete")
    wheels = document.get("wheels")
    if (
        not isinstance(wheels, list)
        or not wheels
        or document.get("wheel_count") != len(wheels)
    ):
        raise ValueError("selected wheel lock count is invalid")
    normalized: list[dict[str, Any]] = []
    for row in wheels:
        if not isinstance(row, dict) or set(row) != {
            "filename",
            "name",
            "normalized_name",
            "version",
            "bytes",
            "sha256",
            "tags",
        }:
            raise ValueError("selected wheel lock row is invalid")
        filename = _python_runtime_relative_path(
            str(row.get("filename", "")), name="selected wheel filename"
        )
        name = row.get("name")
        normalized_name = row.get("normalized_name")
        version = row.get("version")
        size = row.get("bytes")
        tags = row.get("tags")
        if (
            "/" in filename
            or not filename.casefold().endswith(".whl")
            or not isinstance(name, str)
            or _normalized_project_name(name) != normalized_name
            or not isinstance(version, str)
            or not version
            or any(character.isspace() for character in version)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
            or not isinstance(tags, list)
            or not tags
            or tags != sorted(set(tags))
            or any(
                not isinstance(tag, str)
                or not tag
                or any(character.isspace() for character in tag)
                for tag in tags
            )
        ):
            raise ValueError("selected wheel lock row values are invalid")
        normalized.append(
            {
                "filename": filename,
                "name": name,
                "normalized_name": normalized_name,
                "version": version,
                "bytes": size,
                "sha256": _require_sha256(
                    row.get("sha256"), name="selected wheel SHA-256"
                ),
                "tags": tags,
            }
        )
    if normalized != sorted(normalized, key=lambda row: str(row["filename"])):
        raise ValueError("selected wheel lock rows are not canonically ordered")
    if len({row["filename"] for row in normalized}) != len(normalized) or len(
        {row["normalized_name"] for row in normalized}
    ) != len(normalized):
        raise ValueError("selected wheel lock contains duplicate wheels/projects")
    normalized_document = {
        "schema_version": 1,
        "artifact_type": PYTHON_RUNTIME_LOCK_ARTIFACT_TYPE,
        "python": python_identity,
        "wheel_count": len(normalized),
        "wheels": normalized,
    }
    canonical_payload = (
        json.dumps(
            normalized_document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if payload != canonical_payload:
        raise ValueError("selected wheel lock is not canonical JSON")
    if expected_python is not None and python_identity != dict(expected_python):
        raise ValueError("selected wheel lock Python platform identity differs")
    for row in normalized:
        if not any(
            _wheel_tag_compatible(tag, python_identity) for tag in row["tags"]
        ):
            raise ValueError(
                "selected wheel has no tag compatible with the locked Python ABI: "
                + row["filename"]
            )
    if expected_wheels is not None and normalized != [
        dict(row) for row in expected_wheels
    ]:
        raise ValueError("selected wheel lock differs from the wheelhouse")
    return normalized_document


def _selected_wheel_lock_plan(
    *, source_prefix: Path, executable_relative: Path | str, wheelhouse: Path
) -> tuple[dict[str, Any], bytes, list[tuple[Path, dict[str, Any]]]]:
    source = _real_input_directory(source_prefix, name="Python source prefix")
    executable_name = _python_runtime_relative_path(
        executable_relative, name="Python executable"
    )
    metadata = _probe_python_runtime_layout(source, executable_name)
    wheel_inventory = _selected_wheel_inventory(wheelhouse)
    wheels = [row for _path, row in wheel_inventory]
    document = {
        "schema_version": 1,
        "artifact_type": PYTHON_RUNTIME_LOCK_ARTIFACT_TYPE,
        "python": _python_lock_identity(metadata),
        "wheel_count": len(wheels),
        "wheels": wheels,
    }
    payload = (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    _validate_selected_wheel_lock(
        payload,
        expected_python=document["python"],
        expected_wheels=wheels,
    )
    if len(payload) > MAX_PYTHON_RUNTIME_LOCK_BYTES:
        raise ValueError("selected wheel lock exceeds its byte bound")
    return document, payload, wheel_inventory


def prepare_python_runtime_lock(args: argparse.Namespace) -> dict[str, Any]:
    """Generate one exclusive, immutable lock for the selected target wheels."""

    document, payload, _inventory = _selected_wheel_lock_plan(
        source_prefix=args.source_prefix,
        executable_relative=args.python_relative_path,
        wheelhouse=args.wheelhouse,
    )
    output = Path(os.path.abspath(args.output.expanduser()))
    payload_sha256 = _sha256_bytes(payload)
    result: dict[str, Any] = {
        "kind": "selected-wheel-lock",
        "status": "dry-run",
        "output": str(output),
        "sha256": payload_sha256,
        "bytes": len(payload),
        "wheel_count": document["wheel_count"],
        "python": document["python"],
    }
    if not args.apply:
        return result
    parent = _real_input_directory(output.parent, name="selected wheel lock parent")
    output = parent / output.name
    if os.path.lexists(output):
        existing, info = _stable_regular_bytes(
            output, max_bytes=MAX_PYTHON_RUNTIME_LOCK_BYTES
        )
        if (
            existing != payload
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o400
        ):
            raise ValueError("selected wheel lock already exists with another identity")
        result["status"] = "already-built"
        return result
    _write_exclusive_blob(output, payload, mode=0o400)
    _fsync_directory(parent)
    result["status"] = "built"
    return result


def _python_runtime_plan(
    *,
    source_prefix: Path,
    executable_relative: Path | str,
    dependency_lock: Path,
    wheelhouse: Path,
) -> tuple[dict[str, Any], bytes, list[tuple[Path, dict[str, Any]]]]:
    source = _real_input_directory(source_prefix, name="Python source prefix")
    wheels_root = _real_input_directory(wheelhouse, name="wheelhouse")
    executable_name = _python_runtime_relative_path(
        executable_relative, name="Python executable"
    )
    directories, copies = _scan_python_prefix(source)
    rows_by_path = {str(row["path"]): row for _path, row in copies}
    executable_row = rows_by_path.get(executable_name)
    if executable_row is None or executable_row["mode"] != "0555":
        raise ValueError("Python executable is missing or not executable in the prefix")
    metadata = _probe_python_runtime_layout(source, executable_name)
    if metadata["proc_exe_sha256"] != executable_row["sha256"]:
        raise ValueError("/proc/self/exe hash does not match the runtime executable")
    wheel_inventory = _selected_wheel_inventory(wheels_root)
    selected_wheels = [row for _path, row in wheel_inventory]
    elf_audit = _audit_python_runtime_elf(source)

    lock_path = dependency_lock.expanduser()
    try:
        lock_info = lock_path.lstat()
    except OSError as exc:
        raise ValueError(f"dependency lock is unavailable: {lock_path}") from exc
    if stat.S_ISLNK(lock_info.st_mode) or not stat.S_ISREG(lock_info.st_mode):
        raise ValueError("dependency lock must be a real regular file")
    if stat.S_IMODE(lock_info.st_mode) & 0o111:
        raise ValueError("dependency lock must not be executable")
    lock_payload, _stable_lock = _stable_regular_bytes(
        lock_path, max_bytes=MAX_PYTHON_RUNTIME_LOCK_BYTES
    )
    if not lock_payload:
        raise ValueError("dependency lock must not be empty")
    _validate_selected_wheel_lock(
        lock_payload,
        expected_python=_python_lock_identity(metadata),
        expected_wheels=selected_wheels,
    )
    installed_distributions = _installed_distributions_identity(
        source,
        metadata,
        wheel_inventory,
    )
    lock_digest = _sha256_bytes(lock_payload)
    lock_row: dict[str, Any] = {
        "path": PYTHON_RUNTIME_LOCK_DESTINATION,
        "bytes": len(lock_payload),
        "sha256": lock_digest,
        "mode": "0444",
    }
    copies.append((lock_path, lock_row))

    wheel_rows: list[dict[str, Any]] = []
    for path, selected in wheel_inventory:
        row = {
            "path": (
                f"{PYTHON_RUNTIME_WHEEL_DIRECTORY}/{selected['filename']}"
            ),
            **selected,
            "mode": "0444",
        }
        wheel_rows.append(row)
        copies.append(
            (
                path,
                {
                    "path": row["path"],
                    "bytes": row["bytes"],
                    "sha256": row["sha256"],
                    "mode": row["mode"],
                },
            )
        )

    directories.extend(
        [
            {"path": "provenance", "mode": "0555"},
            {"path": PYTHON_RUNTIME_WHEEL_DIRECTORY, "mode": "0555"},
        ]
    )
    directories.sort(key=lambda row: str(row["path"]))
    copies.sort(key=lambda pair: str(pair[1]["path"]))
    files = [row for _path, row in copies]
    if len(files) > MAX_PYTHON_RUNTIME_FILES:
        raise ValueError("Python runtime file count exceeds its bound")
    if len({str(row["path"]) for row in files}) != len(files):
        raise ValueError("Python runtime plan contains duplicate paths")
    total_bytes = sum(int(row["bytes"]) for row in files)
    if total_bytes > MAX_PYTHON_RUNTIME_BYTES:
        raise ValueError("Python runtime exceeds its cumulative byte bound")

    import_paths: list[str] = []
    for key in ("purelib", "platlib"):
        value = str(metadata[key])
        if value not in import_paths:
            import_paths.append(value)
    python_identity = {
        "executable": executable_name,
        "executable_bytes": executable_row["bytes"],
        "executable_sha256": executable_row["sha256"],
        **metadata,
        "import_paths": import_paths,
        "invocation_flags": ["-S", "-P", "-B"],
        "proc_exe_matches_executable": True,
    }
    tree_payload = json.dumps(
        {"directories": directories, "files": files},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": PYTHON_RUNTIME_ARTIFACT_TYPE,
        "python": python_identity,
        "elf_audit": elf_audit,
        "installed_distributions": installed_distributions,
        "dependency_lock": lock_row,
        "wheels": wheel_rows,
        "directory_count": len(directories),
        "file_count": len(files),
        "total_file_bytes": total_bytes,
        "directories": directories,
        "files": files,
        "runtime_tree_sha256": _sha256_bytes(tree_payload),
        "permission_policy": {
            "directories": "0555",
            "files": ["0444", "0555"],
            "manifest": "0400",
            "owner_uid": os.geteuid(),
            "hardlinks_forbidden": True,
            "symlinks_forbidden": True,
        },
        "content_addressed": True,
        "self_contained": True,
    }
    manifest_payload = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if len(manifest_payload) > MAX_PYTHON_RUNTIME_MANIFEST_BYTES:
        raise ValueError("Python runtime manifest exceeds its byte bound")
    return manifest, manifest_payload, copies


def _copy_python_runtime_file(
    source: Path, destination: Path, expected: Mapping[str, Any]
) -> None:
    """Copy one planned file, proving both source stability and output bytes."""

    source_descriptor = os.open(
        source,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    destination_descriptor: int | None = None
    try:
        before = os.fstat(source_descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"Python runtime source became non-regular: {source}")
        normalized_mode = "0555" if stat.S_IMODE(before.st_mode) & 0o111 else "0444"
        if normalized_mode != expected["mode"]:
            raise ValueError(f"Python runtime source mode changed: {source}")
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        digest = hashlib.sha256()
        total = 0
        while True:
            block = os.read(source_descriptor, 1024 * 1024)
            if not block:
                break
            total += len(block)
            digest.update(block)
            view = memoryview(block)
            while view:
                written = os.write(destination_descriptor, view)
                if written <= 0:
                    raise OSError("short write while publishing Python runtime")
                view = view[written:]
        after = os.fstat(source_descriptor)
        if _file_stability_tuple(before) != _file_stability_tuple(after):
            raise ValueError(f"Python runtime source changed while being copied: {source}")
        if total != expected["bytes"] or digest.hexdigest() != expected["sha256"]:
            raise ValueError(f"Python runtime source drifted from its plan: {source}")
        os.fchmod(destination_descriptor, int(str(expected["mode"]), 8))
        os.fsync(destination_descriptor)
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        os.close(source_descriptor)


def _json_object_without_duplicate_keys(payload: bytes, *, name: str) -> dict[str, Any]:
    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{name} contains duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=pairs_hook)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _require_sha256(value: Any, *, name: str) -> str:
    digest = str(value).casefold()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError(f"{name} must be 64 hexadecimal characters")
    return digest


def validate_python_runtime_release(
    manifest_path: Path,
    *,
    expected_manifest_sha256: str | None = None,
    require_content_addressed_name: bool = True,
    run_probe: bool = True,
) -> dict[str, Any]:
    """Fail closed unless an immutable runtime exactly matches its manifest."""

    manifest = manifest_path.expanduser()
    if manifest.name != PYTHON_RUNTIME_MANIFEST:
        raise ValueError("Python runtime manifest filename is invalid")
    payload, manifest_info = _stable_regular_bytes(
        manifest, max_bytes=MAX_PYTHON_RUNTIME_MANIFEST_BYTES
    )
    manifest_sha256 = _sha256_bytes(payload)
    if expected_manifest_sha256 is not None and manifest_sha256 != _require_sha256(
        expected_manifest_sha256, name="expected Python runtime manifest SHA-256"
    ):
        raise ValueError("Python runtime manifest SHA-256 does not match expectation")
    if (
        manifest_info.st_uid != os.geteuid()
        or manifest_info.st_nlink != 1
        or stat.S_IMODE(manifest_info.st_mode) != 0o400
    ):
        raise ValueError("Python runtime manifest ownership/link count/mode is invalid")
    runtime = manifest.parent.resolve()
    if require_content_addressed_name and runtime.name != f"python-runtime-{manifest_sha256}":
        raise ValueError("Python runtime directory is not content-addressed by its manifest")

    document = _json_object_without_duplicate_keys(
        payload, name="Python runtime manifest"
    )
    if document.get("schema_version") != 1:
        raise ValueError("unsupported Python runtime manifest schema")
    if document.get("artifact_type") != PYTHON_RUNTIME_ARTIFACT_TYPE:
        raise ValueError("unexpected Python runtime artifact type")
    if document.get("content_addressed") is not True or document.get("self_contained") is not True:
        raise ValueError("Python runtime manifest does not require content addressing")
    if set(document) != {
        "schema_version",
        "artifact_type",
        "python",
        "elf_audit",
        "installed_distributions",
        "dependency_lock",
        "wheels",
        "directory_count",
        "file_count",
        "total_file_bytes",
        "directories",
        "files",
        "runtime_tree_sha256",
        "permission_policy",
        "content_addressed",
        "self_contained",
    }:
        raise ValueError("Python runtime manifest fields are invalid")
    expected_permissions = {
        "directories": "0555",
        "files": ["0444", "0555"],
        "manifest": "0400",
        "owner_uid": os.geteuid(),
        "hardlinks_forbidden": True,
        "symlinks_forbidden": True,
    }
    if document.get("permission_policy") != expected_permissions:
        raise ValueError("Python runtime permission policy is invalid")

    raw_directories = document.get("directories")
    raw_files = document.get("files")
    if not isinstance(raw_directories, list) or not isinstance(raw_files, list):
        raise ValueError("Python runtime manifest tree inventory is invalid")
    if (
        not raw_directories
        or len(raw_directories) > MAX_PYTHON_RUNTIME_FILES
        or not raw_files
        or len(raw_files) > MAX_PYTHON_RUNTIME_FILES
        or document.get("directory_count") != len(raw_directories)
        or document.get("file_count") != len(raw_files)
    ):
        raise ValueError("Python runtime manifest tree counts are invalid")

    directories: list[dict[str, str]] = []
    directory_names: set[str] = set()
    for raw_row in raw_directories:
        if not isinstance(raw_row, dict) or set(raw_row) != {"path", "mode"}:
            raise ValueError("Python runtime directory row is invalid")
        path = _python_runtime_relative_path(
            str(raw_row.get("path", "")), name="runtime directory", allow_root=True
        )
        if raw_row.get("mode") != "0555" or path in directory_names:
            raise ValueError("Python runtime directory mode/path is invalid")
        directory_names.add(path)
        directories.append({"path": path, "mode": "0555"})
    if "." not in directory_names:
        raise ValueError("Python runtime directory inventory omits its root")
    if directories != sorted(directories, key=lambda row: row["path"]):
        raise ValueError("Python runtime directories are not canonically ordered")

    files: list[dict[str, Any]] = []
    file_names: set[str] = set()
    total_bytes = 0
    for raw_row in raw_files:
        if not isinstance(raw_row, dict) or set(raw_row) != {"path", "bytes", "sha256", "mode"}:
            raise ValueError("Python runtime file row is invalid")
        path = _python_runtime_relative_path(
            str(raw_row.get("path", "")), name="runtime file"
        )
        if path == PYTHON_RUNTIME_MANIFEST or path in file_names:
            raise ValueError("Python runtime file path is duplicate or reserved")
        mode = raw_row.get("mode")
        size = raw_row.get("bytes")
        if (
            mode not in {"0444", "0555"}
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise ValueError("Python runtime file size/mode is invalid")
        digest = _require_sha256(raw_row.get("sha256"), name="runtime file SHA-256")
        row = {"path": path, "bytes": size, "sha256": digest, "mode": mode}
        file_names.add(path)
        files.append(row)
        total_bytes += size
        if total_bytes > MAX_PYTHON_RUNTIME_BYTES:
            raise ValueError("Python runtime exceeds its cumulative byte bound")
    if files != sorted(files, key=lambda row: row["path"]):
        raise ValueError("Python runtime files are not canonically ordered")
    if document.get("total_file_bytes") != total_bytes:
        raise ValueError("Python runtime total byte count is invalid")
    tree_payload = json.dumps(
        {"directories": directories, "files": files},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    tree_sha256 = _sha256_bytes(tree_payload)
    if document.get("runtime_tree_sha256") != tree_sha256:
        raise ValueError("Python runtime tree binding is invalid")

    actual_directories: set[str] = set()
    actual_files: set[str] = set()
    for current, child_directories, child_files in os.walk(
        runtime, topdown=True, followlinks=False
    ):
        current_path = Path(current)
        relative_current = current_path.relative_to(runtime).as_posix()
        if relative_current == ".":
            relative_current = "."
        current_info = current_path.lstat()
        if (
            stat.S_ISLNK(current_info.st_mode)
            or not stat.S_ISDIR(current_info.st_mode)
            or current_info.st_uid != os.geteuid()
            or stat.S_IMODE(current_info.st_mode) != 0o555
        ):
            raise ValueError(f"Python runtime directory is not immutable: {relative_current}")
        actual_directories.add(relative_current)
        for child in [*child_directories, *child_files]:
            child_path = current_path / child
            child_info = child_path.lstat()
            relative_child = child_path.relative_to(runtime).as_posix()
            if stat.S_ISLNK(child_info.st_mode):
                raise ValueError(f"Python runtime contains a symlink: {relative_child}")
            if not stat.S_ISDIR(child_info.st_mode) and not stat.S_ISREG(child_info.st_mode):
                raise ValueError(f"Python runtime contains a special file: {relative_child}")
        actual_files.update(
            (current_path / child).relative_to(runtime).as_posix()
            for child in child_files
        )
    if actual_directories != directory_names:
        raise ValueError("Python runtime directory tree differs from its manifest")
    if actual_files != file_names | {PYTHON_RUNTIME_MANIFEST}:
        raise ValueError("Python runtime file tree differs from its manifest")

    rows_by_path = {row["path"]: row for row in files}
    for row in files:
        path = runtime / row["path"]
        size, digest, info = _stable_regular_file(path)
        if (
            info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != int(row["mode"], 8)
        ):
            raise ValueError(
                "Python runtime file ownership/link count/mode is invalid: "
                + str(row["path"])
            )
        if size != row["bytes"] or digest != row["sha256"]:
            raise ValueError(f"Python runtime file hash/size is invalid: {row['path']}")

    observed_elf_audit = _audit_python_runtime_elf(runtime)
    if document.get("elf_audit") != observed_elf_audit:
        raise ValueError("Python runtime ELF loader identity differs from its manifest")

    lock = document.get("dependency_lock")
    if not isinstance(lock, dict) or set(lock) != {"path", "bytes", "sha256", "mode"}:
        raise ValueError("Python runtime dependency lock identity is invalid")
    if lock != rows_by_path.get(PYTHON_RUNTIME_LOCK_DESTINATION):
        raise ValueError("Python runtime dependency lock is not tree-bound")
    wheels = document.get("wheels")
    if not isinstance(wheels, list) or not wheels:
        raise ValueError("Python runtime wheel identity is invalid")
    normalized_wheels: list[dict[str, Any]] = []
    lock_wheels: list[dict[str, Any]] = []
    wheel_paths: set[str] = set()
    for wheel in wheels:
        if not isinstance(wheel, dict) or set(wheel) != {
            "path",
            "filename",
            "name",
            "normalized_name",
            "version",
            "bytes",
            "sha256",
            "tags",
            "mode",
        }:
            raise ValueError("Python runtime wheel row is invalid")
        file_row = {
            "path": wheel.get("path"),
            "bytes": wheel.get("bytes"),
            "sha256": wheel.get("sha256"),
            "mode": wheel.get("mode"),
        }
        if rows_by_path.get(str(wheel.get("path", ""))) != file_row:
            raise ValueError("Python runtime wheel is not tree-bound")
        filename = _python_runtime_relative_path(
            str(wheel.get("filename", "")), name="wheel filename"
        )
        if "/" in filename or not filename.casefold().endswith(".whl"):
            raise ValueError("Python runtime wheel filename is invalid")
        if wheel.get("path") != f"{PYTHON_RUNTIME_WHEEL_DIRECTORY}/{filename}":
            raise ValueError("Python runtime wheel filename/path binding is invalid")
        wheel_path = str(wheel["path"])
        if wheel_path in wheel_paths:
            raise ValueError("Python runtime wheel path is duplicate")
        wheel_paths.add(wheel_path)
        normalized_wheels.append(wheel)
        lock_wheels.append(
            {
                key: wheel[key]
                for key in (
                    "filename",
                    "name",
                    "normalized_name",
                    "version",
                    "bytes",
                    "sha256",
                    "tags",
                )
            }
        )
    if normalized_wheels != sorted(
        normalized_wheels, key=lambda row: str(row["filename"])
    ):
        raise ValueError("Python runtime wheels are not canonically ordered")
    if wheel_paths != {
        path
        for path in file_names
        if path.startswith(PYTHON_RUNTIME_WHEEL_DIRECTORY + "/")
    }:
        raise ValueError("Python runtime wheel inventory is incomplete")
    lock_payload, _lock_info = _stable_regular_bytes(
        runtime / PYTHON_RUNTIME_LOCK_DESTINATION,
        max_bytes=MAX_PYTHON_RUNTIME_LOCK_BYTES,
    )

    python_identity = document.get("python")
    if not isinstance(python_identity, dict):
        raise ValueError("Python runtime interpreter identity is invalid")
    if set(python_identity) != {
        "executable",
        "executable_bytes",
        "executable_sha256",
        "implementation",
        "version",
        "version_info",
        "cache_tag",
        "soabi",
        "platform",
        "proc_exe_sha256",
        "stdlib",
        "platstdlib",
        "purelib",
        "platlib",
        "isolated_sys_path",
        "import_paths",
        "invocation_flags",
        "proc_exe_matches_executable",
    }:
        raise ValueError("Python runtime interpreter fields are invalid")
    executable_name = _python_runtime_relative_path(
        str(python_identity.get("executable", "")), name="Python executable"
    )
    executable_row = rows_by_path.get(executable_name)
    if executable_row is None or executable_row["mode"] != "0555":
        raise ValueError("Python runtime executable is not tree-bound")
    if (
        python_identity.get("executable_bytes") != executable_row["bytes"]
        or python_identity.get("executable_sha256") != executable_row["sha256"]
        or python_identity.get("proc_exe_sha256") != executable_row["sha256"]
        or python_identity.get("proc_exe_matches_executable") is not True
        or python_identity.get("invocation_flags") != ["-S", "-P", "-B"]
    ):
        raise ValueError("Python executable and /proc/exe binding is invalid")
    for field in ("version", "cache_tag", "soabi", "platform"):
        if not isinstance(python_identity.get(field), str) or not python_identity[field]:
            raise ValueError("Python runtime version/ABI identity is invalid")
    if python_identity.get("implementation") != "CPython":
        raise ValueError("Python runtime implementation identity is invalid")
    version_info = python_identity.get("version_info")
    if not isinstance(version_info, dict) or set(version_info) != {
        "major",
        "minor",
        "micro",
        "releaselevel",
        "serial",
    }:
        raise ValueError("Python runtime version_info identity is invalid")
    if (
        any(
            not isinstance(version_info[field], int)
            or isinstance(version_info[field], bool)
            or version_info[field] < 0
            for field in ("major", "minor", "micro", "serial")
        )
        or version_info["releaselevel"] not in {
            "alpha",
            "beta",
            "candidate",
            "final",
        }
    ):
        raise ValueError("Python runtime version_info values are invalid")
    runtime_layout_paths: dict[str, str] = {}
    for field in ("stdlib", "platstdlib", "purelib", "platlib"):
        relative = _python_runtime_relative_path(
            str(python_identity.get(field, "")), name=f"Python {field}"
        )
        if relative not in directory_names:
            raise ValueError("Python runtime layout path is not tree-bound")
        runtime_layout_paths[field] = relative
    isolated_sys_path = python_identity.get("isolated_sys_path")
    if not isinstance(isolated_sys_path, list) or not isolated_sys_path:
        raise ValueError("isolated Python sys.path identity is invalid")
    normalized_sys_path = [
        _python_runtime_relative_path(value, name="isolated Python sys.path")
        for value in isolated_sys_path
    ]
    if len(set(normalized_sys_path)) != len(normalized_sys_path):
        raise ValueError("isolated Python sys.path contains duplicates")
    import_paths = python_identity.get("import_paths")
    if not isinstance(import_paths, list) or not import_paths:
        raise ValueError("Python runtime import paths are invalid")
    normalized_import_paths = [
        _python_runtime_relative_path(value, name="Python import path")
        for value in import_paths
    ]
    if len(set(normalized_import_paths)) != len(normalized_import_paths):
        raise ValueError("Python runtime import paths contain duplicates")
    for relative in normalized_import_paths:
        if relative not in directory_names:
            raise ValueError("Python runtime import path is not tree-bound")
    expected_import_paths: list[str] = []
    for field in ("purelib", "platlib"):
        relative = runtime_layout_paths[field]
        if relative not in expected_import_paths:
            expected_import_paths.append(relative)
    if normalized_import_paths != expected_import_paths:
        raise ValueError("Python runtime import paths differ from its ABI layout")

    _validate_selected_wheel_lock(
        lock_payload,
        expected_python=_python_lock_identity(python_identity),
        expected_wheels=lock_wheels,
    )
    published_wheel_inventory = _selected_wheel_inventory(
        runtime / PYTHON_RUNTIME_WHEEL_DIRECTORY
    )
    observed_wheels = [row for _path, row in published_wheel_inventory]
    if observed_wheels != lock_wheels:
        raise ValueError("published wheel archive metadata differs from its lock")
    observed_installed_distributions = _installed_distributions_identity(
        runtime,
        python_identity,
        published_wheel_inventory,
    )
    if document.get("installed_distributions") != observed_installed_distributions:
        raise ValueError(
            "installed distribution identity differs from its manifest"
        )

    probe_keys = (
        "implementation",
        "version",
        "version_info",
        "cache_tag",
        "soabi",
        "platform",
        "proc_exe_sha256",
        "stdlib",
        "platstdlib",
        "purelib",
        "platlib",
        "isolated_sys_path",
    )
    if run_probe:
        observed = _probe_python_runtime_layout(runtime, executable_name)
        expected_probe = {key: python_identity.get(key) for key in probe_keys}
        if observed != expected_probe:
            raise ValueError("live Python version/ABI/prefix identity differs from its manifest")
    return {
        "runtime": str(runtime),
        "manifest": str(manifest.resolve()),
        "manifest_sha256": manifest_sha256,
        "runtime_tree_sha256": tree_sha256,
        "python_executable": str(runtime / executable_name),
        "python_executable_sha256": executable_row["sha256"],
        "python_version": python_identity.get("version"),
        "python_soabi": python_identity.get("soabi"),
        "python_platform": python_identity.get("platform"),
        "import_paths": [str(runtime / path) for path in normalized_import_paths],
        "elf_audit_sha256": observed_elf_audit["binding_sha256"],
        "elf_file_count": observed_elf_audit["file_count"],
        "system_library_count": observed_elf_audit["system_library_count"],
        "system_directory_count": observed_elf_audit["system_directory_count"],
        "installed_distributions_sha256": observed_installed_distributions[
            "binding_sha256"
        ],
        "installed_distribution_count": observed_installed_distributions[
            "distribution_count"
        ],
        "installed_record_entry_count": observed_installed_distributions[
            "record_entry_count"
        ],
        "omitted_entry_point_count": observed_installed_distributions[
            "omitted_entry_point_count"
        ],
        "dependency_lock_sha256": lock["sha256"],
        "wheel_count": len(wheels),
        "files_verified": True,
    }


def prepare_python_runtime(args: argparse.Namespace) -> dict[str, Any]:
    """Publish one complete Python prefix as an immutable addressed object."""

    source_prefix = _real_input_directory(
        args.source_prefix, name="Python source prefix"
    )
    wheelhouse = _real_input_directory(args.wheelhouse, name="wheelhouse")
    dependency_lock = args.dependency_lock.expanduser()
    manifest, manifest_payload, copies = _python_runtime_plan(
        source_prefix=source_prefix,
        executable_relative=args.python_relative_path,
        dependency_lock=dependency_lock,
        wheelhouse=wheelhouse,
    )
    manifest_sha256 = _sha256_bytes(manifest_payload)
    release_root_expanded = args.release_root.expanduser().resolve(strict=False)
    runtimes_expanded = release_root_expanded / "python-runtimes"
    release = runtimes_expanded / f"python-runtime-{manifest_sha256}"
    result: dict[str, Any] = {
        "kind": "python-runtime-release",
        "status": "dry-run",
        "manifest_sha256": manifest_sha256,
        "runtime_tree_sha256": manifest["runtime_tree_sha256"],
        "elf_audit_sha256": manifest["elf_audit"]["binding_sha256"],
        "elf_file_count": manifest["elf_audit"]["file_count"],
        "system_library_count": manifest["elf_audit"]["system_library_count"],
        "system_directory_count": manifest["elf_audit"]["system_directory_count"],
        "installed_distributions_sha256": manifest["installed_distributions"][
            "binding_sha256"
        ],
        "installed_distribution_count": manifest["installed_distributions"][
            "distribution_count"
        ],
        "installed_record_entry_count": manifest["installed_distributions"][
            "record_entry_count"
        ],
        "omitted_entry_point_count": manifest["installed_distributions"][
            "omitted_entry_point_count"
        ],
        "file_count": manifest["file_count"],
        "bytes": manifest["total_file_bytes"],
        "wheel_count": len(manifest["wheels"]),
        "runtime": str(release),
        "manifest": str(release / PYTHON_RUNTIME_MANIFEST),
    }
    if not args.apply:
        return result

    protected_roots = (
        PROJECT_ROOT.resolve(),
        (PROJECT_ROOT / "data").resolve(),
        source_prefix,
        wheelhouse,
    )
    for protected in protected_roots:
        try:
            release_root_expanded.relative_to(protected)
        except ValueError:
            pass
        else:
            raise ValueError("Python runtime release root overlaps protected input")
    release_root = _private_directory(args.release_root, create=True)
    runtimes = _private_directory(release_root / "python-runtimes", create=True)
    release = runtimes / f"python-runtime-{manifest_sha256}"
    result.update(
        {
            "runtime": str(release),
            "manifest": str(release / PYTHON_RUNTIME_MANIFEST),
        }
    )
    if os.path.lexists(release):
        identity = validate_python_runtime_release(
            release / PYTHON_RUNTIME_MANIFEST,
            expected_manifest_sha256=manifest_sha256,
        )
        result.update({"status": "already-built", "identity": identity})
        return result

    building = runtimes / f".{release.name}.{_timestamp()}.building"
    building.mkdir(mode=0o700)
    try:
        raw_directories = manifest["directories"]
        for row in sorted(
            (row for row in raw_directories if row["path"] != "."),
            key=lambda row: (len(PurePosixPath(row["path"]).parts), row["path"]),
        ):
            (building / row["path"]).mkdir(mode=0o700)
        for source, row in copies:
            _copy_python_runtime_file(source, building / row["path"], row)
        _write_source_blob(
            building / PYTHON_RUNTIME_MANIFEST,
            manifest_payload,
            mode=0o400,
        )
        directories = [building, *(path for path in building.rglob("*") if path.is_dir())]
        for directory in sorted(
            directories, key=lambda path: len(path.parts), reverse=True
        ):
            os.chmod(directory, 0o555)
            _fsync_directory(directory)
        validate_python_runtime_release(
            building / PYTHON_RUNTIME_MANIFEST,
            expected_manifest_sha256=manifest_sha256,
            require_content_addressed_name=False,
        )
        atomic_rename_noreplace(building, release)
        _fsync_directory(runtimes)
    except BaseException:
        # Preserve the frozen partial candidate for diagnosis. It is hidden and
        # never becomes an addressable production runtime.
        raise
    identity = validate_python_runtime_release(
        release / PYTHON_RUNTIME_MANIFEST,
        expected_manifest_sha256=manifest_sha256,
    )
    result.update({"status": "built", "identity": identity})
    return result


def _runtime_seed_sources(data_dir: Path) -> list[tuple[Path, Path]]:
    """Return the exact ignored/runtime inputs copied into a new generation."""

    source = data_dir.expanduser().resolve()
    pairs: list[tuple[Path, Path]] = [
        (source / "lightrag_storage" / name, Path("lightrag_storage") / name)
        for name in RUNTIME_LIGHTRAG_FILES
    ]
    for name, destination in (
        (".query_embedding_cache.json.gz", "query_embedding_cache.json.gz"),
        (".embedding_cache.json.gz", "lightrag_embedding_cache.json.gz"),
    ):
        candidate = source / name
        if candidate.exists():
            pairs.append((candidate, Path(destination)))
    api_cache = source / ".query_api_cache"
    if api_cache.exists():
        try:
            root_info = api_cache.lstat()
        except OSError as exc:
            raise ValueError(f"cannot inspect runtime API cache seed: {api_cache}") from exc
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise ValueError(f"runtime API cache seed is not a real directory: {api_cache}")
        for candidate in sorted(api_cache.rglob("*"), key=lambda item: item.as_posix()):
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"runtime API cache seed contains a symlink: {candidate}")
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(
                    f"runtime API cache seed contains a non-regular file: {candidate}"
                )
            pairs.append(
                (candidate, Path("api_cache") / candidate.relative_to(api_cache))
            )
    destinations = [destination.as_posix() for _source, destination in pairs]
    if len(destinations) != len(set(destinations)):
        raise ValueError("runtime seed destination collision")
    return pairs


def _runtime_root_and_generation(path: Path) -> tuple[Path, Path]:
    """Resolve an explicit generation or the single audited ``current`` selector."""

    expanded = path.expanduser()
    try:
        info = expanded.lstat()
    except OSError as exc:
        raise ValueError(f"runtime selector is missing: {expanded}") from exc
    if stat.S_ISLNK(info.st_mode):
        if expanded.name != "current":
            raise ValueError("only the runtime current selector may be a symlink")
        runtime_root = _private_directory(expanded.parent, create=False)
        raw_target = os.readlink(expanded)
        expected_prefix = "generations/generation-"
        if os.path.isabs(raw_target) or not raw_target.startswith(expected_prefix):
            raise ValueError("runtime current selector has an unsafe target")
        generation = expanded.resolve(strict=True)
    else:
        generation = _private_directory(expanded, create=False)
        if generation.parent.name != "generations":
            raise ValueError("runtime shadow must be an explicit generation")
        runtime_root = _private_directory(generation.parent.parent, create=False)
    generations = _private_directory(runtime_root / "generations", create=False)
    if generation.parent != generations or not generation.name.startswith("generation-"):
        raise ValueError("runtime generation is outside the selected runtime root")
    return runtime_root, _private_directory(generation, create=False)


def _validated_runtime_shadow(path: Path, *, data_dir: Path | None = None) -> Path:
    """Validate the immutable core of one selected runtime generation."""

    _runtime_root, runtime = _runtime_root_and_generation(path)
    try:
        runtime.relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        pass
    else:
        raise ValueError("runtime shadow must be outside the protected repository")
    manifest_path = runtime / RUNTIME_MANIFEST
    try:
        manifest_info = manifest_path.lstat()
        if (
            stat.S_ISLNK(manifest_info.st_mode)
            or not stat.S_ISREG(manifest_info.st_mode)
            or manifest_info.st_uid != os.getuid()
            or stat.S_IMODE(manifest_info.st_mode) != 0o400
            or manifest_info.st_size > 16 * 1024 * 1024
        ):
            raise ValueError("runtime shadow manifest has an unsafe identity/mode")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("runtime shadow manifest is unreadable") from exc
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema_version") != 1
        or payload.get("artifact_type") != "where_papers_go_runtime_shadow"
        or payload.get("protected_sources_never_replaced") is not True
        or not isinstance(payload.get("files"), list)
    ):
        raise ValueError("runtime shadow manifest contract is invalid")
    if data_dir is not None:
        protected_data = data_dir.expanduser().resolve()
        if payload.get("source_data_dir") != str(protected_data):
            raise ValueError("runtime shadow source data binding differs")
        try:
            runtime.relative_to(protected_data)
        except ValueError:
            pass
        else:
            raise ValueError("runtime shadow must be outside the protected data directory")
    file_bindings = {
        str(row.get("runtime_path")): row
        for row in payload["files"]
        if isinstance(row, Mapping)
    }
    for name in RUNTIME_LIGHTRAG_FILES:
        relative = f"lightrag_storage/{name}"
        binding = file_bindings.get(relative)
        candidate = runtime / relative
        try:
            info = candidate.lstat()
        except OSError as exc:
            raise ValueError(f"runtime shadow is missing {relative}") from exc
        if (
            not isinstance(binding, Mapping)
            or stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size != binding.get("bytes")
            or sha256_file(candidate) != binding.get("sha256")
        ):
            raise ValueError(f"runtime shadow immutable binding drifted: {relative}")
    api_cache = runtime / "api_cache"
    _private_directory(api_cache, create=False)
    return runtime


def _runtime_manifest_sha256(runtime: Path) -> str:
    return sha256_file(runtime / RUNTIME_MANIFEST)


def _read_config_search(path: Path) -> Mapping[str, Any]:
    """Read only the Search configuration without ever returning secret values."""

    config_path = path.expanduser().resolve()
    try:
        info = config_path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError("API config must be a real regular file")
        root = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("API config is unavailable or invalid") from exc
    if not isinstance(root, Mapping) or not isinstance(root.get("search"), Mapping):
        raise ValueError("API config has no Search object")
    return root["search"]


def _tavily_pool_for_state(api_config: Path, state_file: Path):
    from where_paper_go.tavily_pool import TavilyKeyPool, configured_tavily_keys

    search = _read_config_search(api_config)
    keys = configured_tavily_keys(search)
    return TavilyKeyPool(
        keys,
        quota_per_key=int(search.get("quota_per_key", 1000)),
        state_file=state_file,
        rate_limit_cooldown_seconds=float(
            search.get("rate_limit_cooldown_seconds", 3600)
        ),
        transient_cooldown_seconds=float(
            search.get("transient_cooldown_seconds", 60)
        ),
    )


def _validate_shared_tavily_state(
    shared_state_dir: Path, *, api_config: Path
) -> dict[str, Any]:
    shared = _private_directory(shared_state_dir, create=False)
    state_file = shared / TAVILY_STATE_NAME
    for candidate in (
        state_file,
        state_file.with_name(state_file.name + ".bak"),
        state_file.with_name(state_file.name + ".lock"),
    ):
        try:
            info = candidate.lstat()
        except OSError as exc:
            raise ValueError("shared Tavily state is incomplete") from exc
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("shared Tavily state has an unsafe identity or mode")
    try:
        snapshot = _tavily_pool_for_state(api_config, state_file).audit_snapshot()
    except Exception as exc:
        raise ValueError("shared Tavily state failed its read-only audit") from exc
    copies = snapshot.get("copies")
    selected_revision = snapshot.get("state_revision")
    if (
        not isinstance(copies, Mapping)
        or not isinstance(copies.get("primary"), Mapping)
        or not isinstance(copies.get("backup"), Mapping)
        or copies["primary"].get("valid") is not True
        or copies["backup"].get("valid") is not True
        or snapshot.get("configuration_current") is not True
        or not isinstance(selected_revision, int)
        or isinstance(selected_revision, bool)
        or copies["primary"].get("revision") != selected_revision
        or copies["backup"].get("revision") != selected_revision
        or copies["primary"].get("sha256") != copies["backup"].get("sha256")
    ):
        raise ValueError(
            "shared Tavily state is not current and identically replicated"
        )
    return {
        "state_revision": snapshot.get("state_revision"),
        "configured_keyset_sha256": snapshot.get("configured_keyset_sha256"),
        "primary_sha256": copies["primary"].get("sha256"),
        "backup_sha256": copies["backup"].get("sha256"),
    }


def _prepare_shared_tavily_state(
    *,
    data_dir: Path,
    runtime_root: Path,
    shared_state_dir: Path,
    api_config: Path,
) -> dict[str, Any]:
    """Create one persistent quota ledger without changing the legacy copies."""

    shared_expanded = shared_state_dir.expanduser()
    if os.path.lexists(shared_expanded):
        audit = _validate_shared_tavily_state(shared_expanded, api_config=api_config)
        return {"status": "existing", "path": str(shared_expanded.resolve()), **audit}
    if shared_expanded.parent.resolve() != runtime_root:
        raise ValueError("new shared Tavily state must be a direct runtime-root child")
    building = runtime_root / f".shared.building-{_timestamp()}"
    building.mkdir(mode=0o700)
    state_file = building / TAVILY_STATE_NAME
    legacy = data_dir / TAVILY_STATE_NAME
    legacy_files = (
        legacy,
        legacy.with_name(legacy.name + ".bak"),
        legacy.with_name(legacy.name + ".lock"),
    )
    presence = [os.path.lexists(path) for path in legacy_files]
    try:
        if any(presence) and not all(presence):
            raise ValueError("legacy Tavily state is partial; refusing quota recovery")
        if all(presence):
            if fcntl is None:
                raise ValueError("Tavily state migration requires POSIX file locking")
            lock_fd = os.open(
                legacy_files[2],
                os.O_RDWR
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                lock_info = os.fstat(lock_fd)
                if (
                    not stat.S_ISREG(lock_info.st_mode)
                    or lock_info.st_uid != os.getuid()
                    or stat.S_IMODE(lock_info.st_mode) != 0o600
                ):
                    raise ValueError("legacy Tavily lock has an unsafe identity/mode")
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                for legacy_copy in legacy_files[:2]:
                    copy_info = legacy_copy.lstat()
                    if (
                        stat.S_ISLNK(copy_info.st_mode)
                        or not stat.S_ISREG(copy_info.st_mode)
                        or copy_info.st_uid != os.getuid()
                        or stat.S_IMODE(copy_info.st_mode) != 0o600
                    ):
                        raise ValueError(
                            "legacy Tavily state copy has an unsafe identity/mode"
                        )
                _clone_runtime_seed(legacy_files[0], state_file)
                _clone_runtime_seed(
                    legacy_files[1], state_file.with_name(state_file.name + ".bak")
                )
                descriptor = os.open(
                    state_file.with_name(state_file.name + ".lock"),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                os.close(lock_fd)
        else:
            _tavily_pool_for_state(api_config, state_file).summary()
        _fsync_directory(building)
        atomic_rename_noreplace(building, shared_expanded)
        _fsync_directory(runtime_root)
    except BaseException:
        # Preserve ambiguous or partial state for diagnosis; never reset it.
        raise
    audit = _validate_shared_tavily_state(shared_expanded, api_config=api_config)
    return {"status": "installed", "path": str(shared_expanded.resolve()), **audit}


def _clone_runtime_seed(source: Path, destination: Path) -> dict[str, Any]:
    """Clone one source through stable fds and verify it did not drift."""

    source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    source_fd = os.open(source, source_flags)
    destination_fd = -1
    try:
        before = os.fstat(source_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_size > MAX_RUNTIME_SEED_BYTES
        ):
            raise ValueError(f"unsafe or oversized runtime seed: {source}")
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(destination.parent, 0o700)
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        digest = hashlib.sha256()
        copied = 0
        while True:
            block = os.read(source_fd, 1024 * 1024)
            if not block:
                break
            view = memoryview(block)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise OSError("short write while cloning runtime seed")
                view = view[written:]
            digest.update(block)
            copied += len(block)
        os.fchmod(destination_fd, 0o600)
        os.fsync(destination_fd)
        after = os.fstat(source_fd)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_before != identity_after or copied != before.st_size:
            raise ValueError(f"runtime seed changed while being cloned: {source}")
        return {
            "source": str(source.resolve()),
            "runtime_path": destination.name,
            "bytes": copied,
            "sha256": digest.hexdigest(),
            "source_device": before.st_dev,
            "source_inode": before.st_ino,
            "source_mtime_ns": before.st_mtime_ns,
            "source_ctime_ns": before.st_ctime_ns,
        }
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        os.close(source_fd)


def _observed_runtime_pointer(runtime_root: Path) -> str | None:
    current = runtime_root / "current"
    if not os.path.lexists(current):
        return None
    info = current.lstat()
    if not stat.S_ISLNK(info.st_mode):
        raise ValueError(f"runtime current pointer is not a symlink: {current}")
    raw_target = os.readlink(current)
    if os.path.isabs(raw_target) or not raw_target.startswith(
        "generations/generation-"
    ):
        raise ValueError("runtime current pointer has an unsafe target")
    target = current.resolve(strict=True)
    generations = _private_directory(runtime_root / "generations", create=False)
    if target.parent != generations or not target.name.startswith("generation-"):
        raise ValueError("runtime current pointer escapes the generations directory")
    _private_directory(target, create=False)
    return raw_target


def _atomic_runtime_pointer(
    runtime_root: Path, generation: Path, *, expected_current: str | None
) -> Path | None:
    """CAS-switch ``current`` to a validated generation and retain its predecessor."""

    current = runtime_root / "current"
    observed = _observed_runtime_pointer(runtime_root)
    if observed != expected_current:
        raise ValueError(
            "runtime current pointer changed after planning; refusing activation"
        )
    backup: Path | None = None
    if observed is not None:
        backup = runtime_root / f"current.backup-{_timestamp()}"
        os.symlink(observed, backup)
        _fsync_directory(runtime_root)
    temporary = runtime_root / f".current.{_timestamp()}.tmp"
    os.symlink(os.path.relpath(generation, runtime_root), temporary)
    try:
        os.replace(temporary, current)
        _fsync_directory(runtime_root)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return backup


def prepare_runtime(args: argparse.Namespace) -> dict[str, Any]:
    """Build, but never activate, a private runtime shadow generation."""

    data_expanded = args.data_dir.expanduser()
    try:
        data_info = data_expanded.lstat()
    except OSError as exc:
        raise ValueError("source data directory is unavailable") from exc
    if stat.S_ISLNK(data_info.st_mode) or not stat.S_ISDIR(data_info.st_mode):
        raise ValueError(f"source data directory is unavailable/unsafe: {data_expanded}")
    data_dir = data_expanded.resolve()
    sources = _runtime_seed_sources(data_dir)
    if not sources:
        raise ValueError("runtime shadow has no source files")
    source_plan: list[dict[str, Any]] = []
    for source, destination in sources:
        try:
            info = source.lstat()
        except OSError as exc:
            raise ValueError(f"required runtime seed is missing: {source}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError(f"runtime seed is not a regular file: {source}")
        source_plan.append(
            {
                "source": str(source.resolve()),
                "runtime_path": destination.as_posix(),
                "bytes": info.st_size,
                "sha256": sha256_file(source),
            }
        )
    binding_sha256 = _sha256_bytes(
        json.dumps(
            source_plan,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    total_bytes = sum(int(row["bytes"]) for row in source_plan)
    if len(source_plan) > MAX_RUNTIME_SEED_FILES:
        raise ValueError("runtime shadow exceeds its file-count bound")
    if total_bytes > MAX_RUNTIME_SEED_TOTAL_BYTES:
        raise ValueError("runtime shadow exceeds its cumulative byte bound")
    result: dict[str, Any] = {
        "kind": "runtime-shadow",
        "status": "dry-run",
        "source_data_dir": str(data_dir),
        "source_binding_sha256": binding_sha256,
        "file_count": len(source_plan),
        "bytes": total_bytes,
    }
    if not args.apply:
        return result

    runtime_root = _private_directory(args.runtime_root, create=True)
    try:
        runtime_root.relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        pass
    else:
        raise ValueError("runtime root must be outside the protected repository")
    try:
        runtime_root.relative_to(data_dir)
    except ValueError:
        pass
    else:
        raise ValueError("runtime root must be outside the protected data directory")
    generations = runtime_root / "generations"
    generations = _private_directory(generations, create=True)
    generation_name = f"generation-{_timestamp()}-{binding_sha256[:12]}"
    building = generations / f".{generation_name}.building"
    generation = generations / generation_name
    building.mkdir(mode=0o700)
    cloned: list[dict[str, Any]] = []
    try:
        for (source, relative), planned in zip(sources, source_plan, strict=True):
            row = _clone_runtime_seed(source, building / relative)
            row["runtime_path"] = relative.as_posix()
            if (
                row["sha256"] != planned["sha256"]
                or row["bytes"] != planned["bytes"]
            ):
                raise ValueError(f"runtime seed drifted after dry binding: {source}")
            cloned.append(row)
        for required in ("api_cache",):
            directory = building / required
            directory.mkdir(mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
        manifest = {
            "schema_version": 1,
            "artifact_type": "where_papers_go_runtime_shadow",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_data_dir": str(data_dir),
            "source_binding_sha256": binding_sha256,
            "files": cloned,
            "write_boundary": "runtime_generation_only",
            "protected_sources_never_replaced": True,
        }
        manifest_payload = (
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        manifest_path = building / RUNTIME_MANIFEST
        descriptor = os.open(
            manifest_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o400,
        )
        try:
            view = memoryview(manifest_payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write while publishing runtime manifest")
                view = view[written:]
            os.fchmod(descriptor, 0o400)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        for directory in sorted(
            {path.parent for path in building.rglob("*") if path.is_file()},
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            _fsync_directory(directory)
        _fsync_directory(building)
        atomic_rename_noreplace(building, generation)
        _fsync_directory(generations)
    except BaseException:
        # Preserve the private .building tree for diagnosis. Never erase a
        # partially cloned seed or any older generation.
        raise
    result.update(
        {
            "status": "built-not-active",
            "generation": str(generation),
            "current": str(runtime_root / "current"),
            "observed_current": _observed_runtime_pointer(runtime_root),
            "manifest_sha256": _sha256_bytes(manifest_payload),
        }
    )
    return result


def prepare_shared_state(args: argparse.Namespace) -> dict[str, Any]:
    """Plan or install the cross-generation Tavily quota state."""

    data_expanded = args.data_dir.expanduser()
    try:
        data_info = data_expanded.lstat()
    except OSError as exc:
        raise ValueError("source data directory is unavailable") from exc
    if stat.S_ISLNK(data_info.st_mode) or not stat.S_ISDIR(data_info.st_mode):
        raise ValueError("source data directory is unavailable or unsafe")
    data_dir = data_expanded.resolve()
    runtime_root_expanded = args.runtime_root.expanduser()
    shared_state_dir = (
        args.shared_state_dir.expanduser()
        if args.shared_state_dir is not None
        else runtime_root_expanded / "shared"
    )
    result: dict[str, Any] = {
        "kind": "shared-tavily-state",
        "status": "dry-run",
        "source_data_dir": str(data_dir),
        "shared_state_dir": str(shared_state_dir),
        "legacy_files_present": sum(
            int(os.path.lexists(data_dir / name))
            for name in (
                TAVILY_STATE_NAME,
                TAVILY_STATE_NAME + ".bak",
                TAVILY_STATE_NAME + ".lock",
            )
        ),
        "shared_exists": os.path.lexists(shared_state_dir),
    }
    if not args.apply:
        if os.path.lexists(shared_state_dir):
            result["audit"] = _validate_shared_tavily_state(
                shared_state_dir, api_config=args.api_config
            )
        return result
    runtime_root = _private_directory(runtime_root_expanded, create=True)
    for protected in (PROJECT_ROOT.resolve(), data_dir):
        try:
            runtime_root.relative_to(protected)
        except ValueError:
            pass
        else:
            raise ValueError("runtime root must be outside protected sources")
    installed = _prepare_shared_tavily_state(
        data_dir=data_dir,
        runtime_root=runtime_root,
        shared_state_dir=shared_state_dir,
        api_config=args.api_config,
    )
    result.update({"status": installed.pop("status"), "audit": installed})
    return result


def activate_runtime(args: argparse.Namespace) -> dict[str, Any]:
    """Compare-and-swap the audited runtime selector after shadow validation."""

    runtime_root = _private_directory(args.runtime_root, create=False)
    data_dir = args.data_dir.expanduser().resolve()
    generation = _validated_runtime_shadow(args.generation, data_dir=data_dir)
    if generation.parent.parent != runtime_root:
        raise ValueError("generation does not belong to --runtime-root")
    manifest_sha256 = _runtime_manifest_sha256(generation)
    if manifest_sha256 != args.expected_manifest_sha256.casefold():
        raise ValueError("runtime manifest differs from the approved shadow hash")
    expected_current = None if args.expected_current == "none" else args.expected_current
    observed = _observed_runtime_pointer(runtime_root)
    if observed != expected_current:
        raise ValueError("runtime current pointer differs from --expected-current")
    target = os.path.relpath(generation, runtime_root)
    result: dict[str, Any] = {
        "kind": "runtime-activation",
        "status": "dry-run",
        "runtime_root": str(runtime_root),
        "generation": str(generation),
        "target": target,
        "manifest_sha256": manifest_sha256,
        "observed_current": observed,
    }
    if args.apply:
        backup = _atomic_runtime_pointer(
            runtime_root, generation, expected_current=expected_current
        )
        result.update(
            {
                "status": "activated",
                "current": str(runtime_root / "current"),
                "previous_pointer_backup": str(backup) if backup else None,
            }
        )
    return result


def atomic_install(path: Path, payload: bytes, *, mode: int) -> Path | None:
    """Install bytes atomically and preserve a differently-valued predecessor."""

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        info = path.lstat()
    except FileNotFoundError:
        info = None
    if info is not None and (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
        raise ValueError(f"refusing to replace non-regular output: {path}")
    if info is not None and path.read_bytes() == payload:
        _set_mode_durable(path, mode)
        return None
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    backup: Path | None = None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        if info is not None:
            backup = path.with_name(f"{path.name}.backup-{_timestamp()}")
            if backup.exists():
                raise FileExistsError(f"refusing to overwrite backup: {backup}")
            try:
                # A same-directory hard link preserves the predecessor inode
                # without ever removing the active path. The following replace
                # is then one crash-atomic namespace switch.
                os.link(path, backup, follow_symlinks=False)
            except OSError:
                predecessor = path.read_bytes()
                predecessor_mode = stat.S_IMODE(path.stat().st_mode)
                backup_descriptor = os.open(
                    backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, predecessor_mode
                )
                try:
                    with os.fdopen(backup_descriptor, "wb") as backup_handle:
                        backup_handle.write(predecessor)
                        backup_handle.flush()
                        os.fchmod(backup_handle.fileno(), predecessor_mode)
                        os.fsync(backup_handle.fileno())
                except BaseException:
                    backup.unlink(missing_ok=True)
                    raise
            # The backup must be durable before the active name can point at
            # new bytes; otherwise a crash could retain only the replacement.
            _fsync_directory(path.parent)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
        return backup
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _render_result(
    *, kind: str, output: Path, payload: bytes, apply: bool, mode: int
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "kind": kind,
        "status": "dry-run",
        "output": str(output),
        "sha256": _sha256_bytes(payload),
        "bytes": len(payload),
        "changed": not output.exists() or output.read_bytes() != payload,
    }
    if apply:
        backup = atomic_install(output, payload, mode=mode)
        result["status"] = "installed"
        result["backup"] = str(backup) if backup is not None else None
    return result


def _validate_python_runtime(
    python: Path, *, source_release: Path, dependency_paths: Sequence[Path]
) -> None:
    """Prove that the selected launcher can import the production stack.

    User-site discovery remains disabled.  The audited source release is first
    on ``PYTHONPATH`` and explicit, runtime-bound dependency roots supply the
    installed LightRAG/numeric packages. This catches an interpreter/dependency
    ABI mismatch before a unit is emitted.
    """

    program = textwrap.dedent(
        """\
        import asyncio
        import pathlib
        import sys
        import tempfile

        source = pathlib.Path(sys.argv[1]).resolve()
        deps = [pathlib.Path(value).resolve() for value in sys.argv[2:]]
        assert deps
        assert "sitecustomize" not in sys.modules

        def is_runtime_module(module):
            origin = pathlib.Path(module.__file__).resolve()
            return any(origin.is_relative_to(root) for root in deps)

        def audit(event, _args):
            if event in {
                "subprocess.Popen",
                "os.system",
                "os.posix_spawn",
                "os.posix_spawnp",
            }:
                raise RuntimeError("offline dependency probe denied " + event)

        sys.addaudithook(audit)

        import pipmaster as pm

        pipmaster_calls = []

        def deny_install(*args, **kwargs):
            pipmaster_calls.append((args, kwargs))
            raise RuntimeError(
                "pipmaster installation is forbidden during offline preflight"
            )

        for name in (
            "install",
            "install_if_missing",
            "install_multiple",
            "install_multiple_if_not_installed",
            "install_or_update",
            "install_or_update_multiple",
            "install_version",
            "async_install",
            "async_install_if_missing",
            "async_install_multiple",
        ):
            if hasattr(pm, name):
                setattr(pm, name, deny_install)

        import nano_vectordb
        import networkx
        import numpy as np
        from lightrag import LightRAG, QueryParam
        from lightrag.kg.factory import get_storage_class
        from lightrag.utils import Tokenizer, wrap_embedding_func_with_attrs
        from where_paper_go import web_app
        from where_paper_go.lightrag import _UnicodeCodepointTokenizer

        assert pathlib.Path(web_app.__file__).resolve().is_relative_to(source)
        for module in (pm, nano_vectordb, networkx, np):
            assert is_runtime_module(module)

        for storage_name in (
            "JsonKVStorage",
            "NanoVectorDBStorage",
            "NetworkXStorage",
            "JsonDocStatusStorage",
        ):
            storage_class = get_storage_class(storage_name)
            storage_module = sys.modules[storage_class.__module__]
            assert is_runtime_module(storage_module)

        @wrap_embedding_func_with_attrs(
            embedding_dim=8,
            max_token_size=256,
            model_name="where-papers-go-offline-probe",
        )
        async def embedding_func(texts):
            rows = np.zeros((len(texts), 8), dtype=np.float32)
            rows[:, 0] = 1.0
            return rows

        async def llm_func(_prompt, *args, **kwargs):
            return "{}"

        async def run_probe():
            with tempfile.TemporaryDirectory(
                prefix="wpg-lightrag-probe-"
            ) as directory:
                rag = LightRAG(
                    working_dir=directory,
                    workspace="offline_probe",
                    kv_storage="JsonKVStorage",
                    vector_storage="NanoVectorDBStorage",
                    graph_storage="NetworkXStorage",
                    doc_status_storage="JsonDocStatusStorage",
                    llm_model_func=llm_func,
                    llm_model_name="where-papers-go-offline-probe",
                    tokenizer=Tokenizer(
                        model_name="unicode-codepoint-v1",
                        tokenizer=_UnicodeCodepointTokenizer(),
                    ),
                    embedding_func=embedding_func,
                    addon_params={"language": "Chinese"},
                )
                initialized = False
                try:
                    await rag.initialize_storages()
                    initialized = True
                    result = await rag.aquery_data(
                        "offline dependency probe",
                        QueryParam(mode="bypass"),
                    )
                    assert result.get("status") == "success"
                finally:
                    if initialized:
                        await rag.finalize_storages()

        asyncio.run(run_probe())
        assert pipmaster_calls == []
        assert "lightrag.llm.openai" not in sys.modules
        """
    )
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "PYTHONPATH": os.pathsep.join(
            [str(source_release), *(str(path) for path in dependency_paths)]
        ),
        "PIP_NO_INDEX": "1",
        "UV_OFFLINE": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
    }
    try:
        completed = subprocess.run(
            [
                python,
                "-S",
                "-P",
                "-B",
                "-c",
                program,
                str(source_release),
                *(str(path) for path in dependency_paths),
            ],
            cwd=source_release,
            env=environment,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("production Python dependency probe failed") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip()
        raise ValueError(
            "production Python cannot import the pinned source/dependency stack: "
            + detail[-1000:]
        )


def render_systemd(args: argparse.Namespace) -> dict[str, Any]:
    python_runtime = args.python_runtime.expanduser().resolve()
    python_runtime_manifest_sha256 = (
        args.expected_python_runtime_manifest_sha256.casefold()
    )
    python_runtime_identity = validate_python_runtime_release(
        python_runtime / PYTHON_RUNTIME_MANIFEST,
        expected_manifest_sha256=python_runtime_manifest_sha256,
    )
    python = Path(str(python_runtime_identity["python_executable"]))
    dependency_paths = [
        Path(str(value)) for value in python_runtime_identity["import_paths"]
    ]
    data_dir = args.data_dir.expanduser().resolve()
    runtime_dir = _validated_runtime_shadow(args.runtime_dir, data_dir=data_dir)
    shared_state_dir = _private_directory(args.shared_state_dir, create=False)
    _validate_shared_tavily_state(shared_state_dir, api_config=args.api_config)
    source_release = args.source_release.expanduser().resolve()
    source_manifest_sha256 = args.expected_source_manifest_sha256.casefold()
    source_identity = validate_source_release(
        source_release / SOURCE_MANIFEST_FILE,
        expected_manifest_sha256=source_manifest_sha256,
    )
    _validate_python_runtime(
        python,
        source_release=source_release,
        dependency_paths=dependency_paths,
    )
    api_token_file = _fixed_backend_api_token_file(args.api_token_file)
    _read_private_bearer_token(api_token_file, label="API token file")
    payload = render_template(
        args.template,
        {
            "SOURCE_RELEASE": source_release,
            "SOURCE_HEAD": source_identity["head"],
            "SOURCE_TREE": source_identity["tree"],
            "SOURCE_MANIFEST": source_release / SOURCE_MANIFEST_FILE,
            "SOURCE_MANIFEST_SHA256": source_manifest_sha256,
            "PYTHON": python,
            "PYTHON_RUNTIME": python_runtime,
            "PYTHON_RUNTIME_MANIFEST": python_runtime / PYTHON_RUNTIME_MANIFEST,
            "PYTHON_RUNTIME_MANIFEST_SHA256": python_runtime_manifest_sha256,
            "PYTHON_RUNTIME_TREE_SHA256": python_runtime_identity[
                "runtime_tree_sha256"
            ],
            "PYTHON_IMPORT_PATH": os.pathsep.join(
                str(path) for path in dependency_paths
            ),
            "DATA_DIR": data_dir,
            "CONFIG_PATH": args.api_config.expanduser().resolve(),
            "API_TOKEN_FILE": api_token_file,
            "RUNTIME_DIR": runtime_dir,
            "SHARED_STATE_DIR": shared_state_dir,
            "RUNTIME_MANIFEST_SHA256": _runtime_manifest_sha256(runtime_dir),
        },
    )
    result = _render_result(
        kind="systemd-unit",
        output=args.output.expanduser(),
        payload=payload,
        apply=args.apply,
        mode=0o644,
    )
    result.update(
        {
            "python_runtime_manifest_sha256": python_runtime_manifest_sha256,
            "python_runtime_tree_sha256": python_runtime_identity[
                "runtime_tree_sha256"
            ],
            "python_runtime": str(python_runtime),
        }
    )
    return result


def _fixed_monitor_state_directory(
    path: Path, *, create: bool
) -> tuple[Path, bool]:
    """Validate the sole monitor write boundary at its fixed passwd-home path.

    A dry run may describe a not-yet-created directory, but it never creates
    any component.  ``--apply`` creates only missing components below the
    already validated passwd home and gives each newly created directory mode
    0700 before proceeding.
    """

    try:
        home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except KeyError as exc:
        raise ValueError("effective user has no passwd home") from exc
    expected = home / MONITOR_STATE_RELATIVE
    expanded = path.expanduser()
    if (
        not home.is_absolute()
        or Path(os.path.realpath(home)) != home
        or not expanded.is_absolute()
        or expanded != expected
        or ".." in expanded.parts
        or Path(os.path.realpath(expanded)) != expanded
    ):
        raise ValueError(
            "monitor state directory must be the fixed canonical passwd-home path"
        )

    chain = [home]
    current = home
    for component in MONITOR_STATE_RELATIVE.parts:
        current /= component
        chain.append(current)
    existed_before = os.path.lexists(expected)
    missing_parent = False
    for index, directory in enumerate(chain):
        if not os.path.lexists(directory):
            if index == 0:
                raise ValueError("monitor passwd home is unavailable")
            if not create:
                missing_parent = True
                continue
            if missing_parent:
                # Earlier missing components are created in this same loop;
                # reaching here with a missing parent signals namespace drift.
                raise ValueError("monitor state directory parent changed")
            try:
                directory.mkdir(mode=0o700)
                descriptor = os.open(
                    directory,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    os.fchmod(descriptor, 0o700)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                _fsync_directory(directory.parent)
            except OSError as exc:
                raise ValueError(
                    "monitor state directory cannot be created safely"
                ) from exc
        if missing_parent:
            raise ValueError("monitor state directory has an impossible partial path")
        try:
            info = directory.lstat()
            resolved = directory.resolve(strict=True)
        except OSError as exc:
            raise ValueError("monitor state directory chain is unavailable") from exc
        mode = stat.S_IMODE(info.st_mode)
        if (
            resolved != directory
            or stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or mode & 0o022
            or (directory == expected and mode != 0o700)
        ):
            raise ValueError(
                "monitor state directory chain must be owned, real, and private"
            )
    return expected, existed_before


def _monitor_state_namespace_directory(
    base: Path, *, namespace_sha256: str, create: bool
) -> tuple[Path, bool]:
    """Select one private, content-addressed state namespace without reuse."""

    namespace = _require_sha256(
        namespace_sha256, name="monitor state namespace SHA-256"
    )
    validated_base, _base_existed = _fixed_monitor_state_directory(
        base, create=create
    )
    selected = validated_base / namespace
    if Path(os.path.realpath(selected)) != selected:
        raise ValueError("monitor state namespace is not canonical")
    existed_before = os.path.lexists(selected)
    if create and not existed_before:
        try:
            selected.mkdir(mode=0o700)
            descriptor = os.open(
                selected,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fchmod(descriptor, 0o700)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _fsync_directory(validated_base)
        except OSError as exc:
            raise ValueError(
                "monitor state namespace cannot be created safely"
            ) from exc
    if os.path.lexists(selected):
        try:
            info = selected.lstat()
            resolved = selected.resolve(strict=True)
        except OSError as exc:
            raise ValueError("monitor state namespace is unavailable") from exc
        if (
            resolved != selected
            or stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ValueError(
                "monitor state namespace must be owned, real, and mode 0700"
            )
    return selected, existed_before


def _monitor_output_path(path: Path, *, expected_name: str) -> Path:
    """Reject ambiguous/symlinked unit outputs before either file is installed."""

    expanded = path.expanduser()
    if (
        not expanded.is_absolute()
        or expanded.name != expected_name
        or ".." in expanded.parts
        or Path(os.path.realpath(expanded)) != expanded
    ):
        raise ValueError(f"monitor unit output must be canonical {expected_name}")
    if os.path.lexists(expanded):
        info = expanded.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError(f"monitor unit output is not a real file: {expanded}")
    parent = expanded.parent
    if os.path.lexists(parent):
        info = parent.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or parent.resolve(strict=True) != parent
        ):
            raise ValueError("monitor unit output parent is unsafe")
    return expanded


def _monitor_systemd_path_token(path: Path, *, name: str) -> str:
    """Constrain paths substituted into unquoted systemd command tokens."""

    value = os.fspath(path)
    forbidden = frozenset("\\\"'`$%:;#{}")
    if (
        not path.is_absolute()
        or not value
        or any(
            character.isspace()
            or ord(character) < 0x20
            or ord(character) == 0x7F
            or character in forbidden
            for character in value
        )
    ):
        raise ValueError(f"{name} is not a safe systemd command path token")
    return value


def _validate_monitor_systemd_template_contract(
    monitor_template: Path, *, main_template: Path
) -> None:
    """Lock the pre-exec scrub, env -i layer, and bounded unit timeout."""

    expected_unset = "UnsetEnvironment=" + " ".join(
        EXEC_BOUNDARY_UNSET_ENVIRONMENT
    )
    documents: dict[str, list[str]] = {}
    for label, path in (
        ("monitor", monitor_template),
        ("main", main_template),
    ):
        payload, info = _stable_regular_bytes(path, max_bytes=1024 * 1024)
        if (
            info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) not in {0o400, 0o440, 0o444}
        ):
            raise ValueError(f"{label} systemd template identity/mode is unsafe")
        try:
            documents[label] = payload.decode("utf-8", errors="strict").splitlines()
        except UnicodeError as exc:
            raise ValueError(f"{label} systemd template is not UTF-8") from exc
    for label, lines in documents.items():
        unset_lines = [line for line in lines if line.startswith("UnsetEnvironment=")]
        if unset_lines != [expected_unset]:
            raise ValueError(
                f"{label} systemd template pre-exec environment scrub differs"
            )
    monitor_lines = documents["monitor"]
    exec_lines = [line for line in monitor_lines if line.startswith("ExecStart=")]
    if (
        len(exec_lines) != 1
        or not exec_lines[0].startswith("ExecStart=/usr/bin/env -i ")
        or monitor_lines.index(expected_unset) >= monitor_lines.index(exec_lines[0])
        or monitor_lines.count("TimeoutStartSec=300") != 1
        or any(line.startswith("EnvironmentFile=") for line in monitor_lines)
    ):
        raise ValueError("monitor systemd exec-boundary contract is invalid")


def _monitor_renderer_checkout_binding(
    selected_renderer: Path, *, expected_head: str, expected_tree: str
) -> dict[str, str]:
    """Bind a selected release to the clean checkout executing its renderer.

    A content-addressed release proves its own manifest, but rendering a unit
    through a different checkout could otherwise mix two implementations.  In
    particular, merely comparing the selected and worktree files would allow
    the same uncommitted edit to exist in both.  Require the selected bytes,
    current pathname bytes, and the blob named by the current Git commit to be
    identical, and require that commit/tree to be the selected release's exact
    identity.
    """

    project = PROJECT_ROOT.expanduser().resolve(strict=True)
    current_renderer = project / MONITOR_RENDERER_RELATIVE

    def git_identity() -> tuple[str, str]:
        try:
            top_level = Path(
                _git_output(project, "rev-parse", "--show-toplevel")
                .decode("utf-8", errors="strict")
                .strip()
            ).resolve(strict=True)
            head = (
                _git_output(project, "rev-parse", "--verify", "HEAD^{commit}")
                .decode("ascii", errors="strict")
                .strip()
                .casefold()
            )
            tree = (
                _git_output(project, "rev-parse", "--verify", "HEAD^{tree}")
                .decode("ascii", errors="strict")
                .strip()
                .casefold()
            )
        except (OSError, UnicodeError) as exc:
            raise ValueError("renderer checkout Git identity is unavailable") from exc
        if (
            top_level != project
            or len(head) not in {40, 64}
            or len(tree) != len(head)
            or any(character not in "0123456789abcdef" for character in head + tree)
        ):
            raise ValueError("renderer checkout Git identity is invalid")
        return head, tree

    head, tree = git_identity()
    if expected_head != head or expected_tree != tree:
        raise ValueError(
            "selected source release HEAD/tree differs from renderer checkout"
        )

    try:
        loaded_renderer = Path(__file__).resolve(strict=True)
        current_path_info = current_renderer.lstat()
        selected_path_info = selected_renderer.lstat()
        current_payload, current_info = _stable_regular_bytes(
            current_renderer, max_bytes=4 * 1024 * 1024
        )
        selected_payload, selected_info = _stable_regular_bytes(
            selected_renderer, max_bytes=4 * 1024 * 1024
        )
    except OSError as exc:
        raise ValueError("monitor renderer byte proof is unavailable") from exc
    if (
        loaded_renderer != current_renderer
        or stat.S_ISLNK(current_path_info.st_mode)
        or not stat.S_ISREG(current_path_info.st_mode)
        or current_path_info.st_uid != os.geteuid()
        or current_path_info.st_nlink != 1
        or _file_stability_tuple(current_path_info)
        != _file_stability_tuple(current_info)
    ):
        raise ValueError("current monitor renderer pathname is unsafe")
    if (
        selected_renderer.resolve(strict=True) != selected_renderer
        or stat.S_ISLNK(selected_path_info.st_mode)
        or not stat.S_ISREG(selected_path_info.st_mode)
        or selected_path_info.st_uid != os.geteuid()
        or selected_path_info.st_nlink != 1
        or stat.S_IMODE(selected_path_info.st_mode) not in {0o400, 0o440, 0o444}
        or _file_stability_tuple(selected_path_info)
        != _file_stability_tuple(selected_info)
    ):
        raise ValueError("selected monitor renderer pathname is unsafe")

    object_name = f"{head}:{MONITOR_RENDERER_RELATIVE.as_posix()}"
    try:
        committed_size = int(
            _git_output(project, "cat-file", "-s", object_name)
            .decode("ascii", errors="strict")
            .strip()
        )
    except (UnicodeError, ValueError) as exc:
        raise ValueError("committed monitor renderer size is invalid") from exc
    if committed_size < 0 or committed_size > 4 * 1024 * 1024:
        raise ValueError("committed monitor renderer exceeds its byte bound")
    committed_payload = _git_output(project, "cat-file", "blob", object_name)
    if len(committed_payload) != committed_size:
        raise ValueError("committed monitor renderer size changed while read")
    if current_payload != committed_payload:
        raise ValueError(
            "renderer checkout contains uncommitted manage_deployment.py drift"
        )
    if selected_payload != committed_payload:
        raise ValueError(
            "selected source renderer differs from the committed checkout"
        )

    repeated_head, repeated_tree = git_identity()
    repeated_current, repeated_current_info = _stable_regular_bytes(
        current_renderer, max_bytes=4 * 1024 * 1024
    )
    if (
        (repeated_head, repeated_tree) != (head, tree)
        or repeated_current != current_payload
        or _file_stability_tuple(repeated_current_info)
        != _file_stability_tuple(current_info)
    ):
        raise ValueError("renderer checkout changed while it was being bound")
    return {
        "head": head,
        "tree": tree,
        "renderer_sha256": _sha256_bytes(committed_payload),
    }


def _selected_monitor_core(selected_script: Path) -> Any:
    """Require the imported monitor core to equal the selected release bytes."""

    from scripts import monitor_operations

    current_script = PROJECT_ROOT / MONITOR_SCRIPT_RELATIVE
    try:
        loaded_script = Path(str(monitor_operations.__file__)).resolve(strict=True)
        current_resolved = current_script.resolve(strict=True)
        current_info = current_script.lstat()
        selected_payload, selected_info = _stable_regular_bytes(
            selected_script, max_bytes=4 * 1024 * 1024
        )
        current_payload, opened_current = _stable_regular_bytes(
            current_script, max_bytes=4 * 1024 * 1024
        )
    except OSError as exc:
        raise ValueError("monitor core checkout/release proof is unavailable") from exc
    if (
        loaded_script != current_resolved
        or stat.S_ISLNK(current_info.st_mode)
        or not stat.S_ISREG(current_info.st_mode)
        or current_info.st_uid != os.geteuid()
        or current_info.st_nlink != 1
        or _file_stability_tuple(current_info)
        != _file_stability_tuple(opened_current)
        or selected_info.st_uid != os.geteuid()
        or selected_info.st_nlink != 1
        or _sha256_bytes(current_payload) != _sha256_bytes(selected_payload)
    ):
        raise ValueError(
            "selected monitor core differs from the renderer checkout"
        )
    return monitor_operations


def _monitor_policy_sha256(policy: Path, *, monitor_core: Any) -> str:
    """Hash and schema-check the policy that is pinned inside the source release."""

    payload, info = _stable_regular_bytes(policy, max_bytes=64 * 1024)
    if (
        info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) not in {0o400, 0o440, 0o444}
    ):
        raise ValueError("monitor policy identity/mode is unsafe")
    digest = _sha256_bytes(payload)
    try:
        current_policy = PROJECT_ROOT / MONITOR_POLICY_RELATIVE
        current_payload, current_info = _stable_regular_bytes(
            current_policy, max_bytes=64 * 1024
        )
        validated, observed = monitor_core.load_policy(policy, digest)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("monitor policy failed its fixed schema validation") from exc
    service = validated.get("service")
    if (
        observed != digest
        or current_info.st_uid != os.geteuid()
        or current_info.st_nlink != 1
        or _sha256_bytes(current_payload) != digest
        or not isinstance(service, Mapping)
        or service.get("unit") != MONITORED_SERVICE_NAME
        or service.get("health_url") != MONITOR_HEALTH_URL
    ):
        raise ValueError("monitor policy changed its fixed service binding")
    return digest


def _monitor_deployment_binding_sha256(
    expected: Mapping[str, str], *, monitor_core: Any
) -> str:
    """Match the monitor core's complete immutable deployment binding."""

    required = {
        "source_head",
        "source_tree",
        "source_manifest_sha256",
        "python_runtime_manifest_sha256",
        "python_runtime_tree_sha256",
        "python_executable_sha256",
        "runtime_manifest_sha256",
        "store_binding_sha256",
    }
    if set(expected) != required:
        raise ValueError("monitor deployment binding fields are incomplete")
    for name, value in expected.items():
        lengths = {40, 64} if name in {"source_head", "source_tree"} else {64}
        if (
            not isinstance(value, str)
            or len(value) not in lengths
            or value != value.casefold()
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("monitor deployment binding contains an invalid digest")
    payload = json.dumps(
        {
            "schema_version": 1,
            "unit": MONITORED_SERVICE_NAME,
            "health_url": MONITOR_HEALTH_URL,
            "expected": dict(expected),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    digest = _sha256_bytes(payload)
    try:
        core_digest = monitor_core._binding_sha256(expected)
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("monitor core deployment binding is unavailable") from exc
    if core_digest != digest:
        raise ValueError("renderer/core monitor deployment bindings differ")
    return digest


def _monitor_state_namespace_sha256(
    *, policy_sha256: str, deployment_binding_sha256: str
) -> str:
    """Bind mutable monitor history to exactly one policy and deployment."""

    policy = _require_sha256(policy_sha256, name="monitor policy SHA-256")
    deployment = _require_sha256(
        deployment_binding_sha256,
        name="monitor deployment binding SHA-256",
    )
    payload = json.dumps(
        {
            "artifact_type": "where_papers_go_operations_monitor_state_namespace",
            "schema_version": 1,
            "policy_sha256": policy,
            "deployment_binding_sha256": deployment,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return _sha256_bytes(payload)


def _runtime_store_binding(runtime: Path) -> tuple[str, list[dict[str, Any]]]:
    """Derive the worker's exact six-file binding from the validated manifest."""

    manifest = runtime / RUNTIME_MANIFEST
    payload, info = _stable_regular_bytes(manifest, max_bytes=16 * 1024 * 1024)
    if (
        info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o400
    ):
        raise ValueError("runtime shadow manifest identity/mode is unsafe")
    document = _json_object_without_duplicate_keys(
        payload, name="runtime shadow manifest"
    )
    raw_rows = document.get("files")
    if not isinstance(raw_rows, list):
        raise ValueError("runtime shadow manifest file inventory is invalid")
    rows: dict[str, Mapping[str, Any]] = {}
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping):
            raise ValueError("runtime shadow manifest contains an invalid file row")
        relative = raw_row.get("runtime_path")
        if not isinstance(relative, str) or relative in rows:
            raise ValueError("runtime shadow manifest has duplicate/invalid paths")
        rows[relative] = raw_row

    verified: list[dict[str, Any]] = []
    for name in RUNTIME_LIGHTRAG_FILES:
        relative = f"lightrag_storage/{name}"
        row = rows.get(relative)
        if not isinstance(row, Mapping):
            raise ValueError("runtime shadow omits a required LightRAG file")
        size = row.get("bytes")
        digest = row.get("sha256")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or size > MAX_RUNTIME_SEED_BYTES
            or not isinstance(digest, str)
            or digest != digest.casefold()
            or _require_sha256(digest, name=f"runtime {relative} SHA-256") != digest
        ):
            raise ValueError("runtime shadow has an invalid LightRAG binding")
        candidate = runtime / relative
        observed_size, observed_sha256, observed_info = _stable_regular_file(
            candidate, max_bytes=MAX_RUNTIME_SEED_BYTES
        )
        if (
            observed_info.st_uid != os.geteuid()
            or observed_info.st_nlink != 1
            or stat.S_IMODE(observed_info.st_mode) & 0o077
            or observed_size != size
            or observed_sha256 != digest
        ):
            raise ValueError("runtime LightRAG file drifted while binding monitor")
        verified.append(
            {"runtime_path": relative, "bytes": size, "sha256": digest}
        )
    binding_payload = json.dumps(
        verified,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(binding_payload), verified


def render_monitor_systemd(args: argparse.Namespace) -> dict[str, Any]:
    """Render both fixed monitor units from one fully validated deployment."""

    source_manifest_sha256 = _require_sha256(
        args.expected_source_manifest_sha256,
        name="expected source manifest SHA-256",
    )
    source_release = args.source_release.expanduser().resolve()
    source_identity = validate_source_release(
        source_release / SOURCE_MANIFEST_FILE,
        expected_manifest_sha256=source_manifest_sha256,
    )
    if Path(str(source_identity.get("release", ""))) != source_release:
        raise ValueError("source release validator returned a different root")
    service_template = source_release / MONITOR_SYSTEMD_SERVICE_RELATIVE
    timer_template = source_release / MONITOR_SYSTEMD_TIMER_RELATIVE
    main_service_template = source_release / SYSTEMD_TEMPLATE.relative_to(
        PROJECT_ROOT
    )
    policy = source_release / MONITOR_POLICY_RELATIVE
    monitor_script = source_release / MONITOR_SCRIPT_RELATIVE
    selected_renderer = source_release / MONITOR_RENDERER_RELATIVE
    for asset in (
        service_template,
        timer_template,
        main_service_template,
        policy,
        monitor_script,
        selected_renderer,
    ):
        info = asset.lstat()
        if (
            asset.resolve(strict=True) != asset
            or stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
        ):
            raise ValueError("monitor asset is not fixed inside the source release")
    renderer_checkout = _monitor_renderer_checkout_binding(
        selected_renderer,
        expected_head=str(source_identity["head"]),
        expected_tree=str(source_identity["tree"]),
    )
    _validate_monitor_systemd_template_contract(
        service_template, main_template=main_service_template
    )
    monitor_core = _selected_monitor_core(monitor_script)
    policy_sha256 = _monitor_policy_sha256(
        policy, monitor_core=monitor_core
    )

    python_runtime_manifest_sha256 = _require_sha256(
        args.expected_python_runtime_manifest_sha256,
        name="expected Python runtime manifest SHA-256",
    )
    python_runtime = args.python_runtime.expanduser().resolve()
    python_manifest = python_runtime / PYTHON_RUNTIME_MANIFEST
    python_identity = validate_python_runtime_release(
        python_manifest,
        expected_manifest_sha256=python_runtime_manifest_sha256,
    )
    python = Path(str(python_identity.get("python_executable", "")))
    python_executable_sha256 = _require_sha256(
        python_identity.get("python_executable_sha256"),
        name="Python executable SHA-256",
    )
    python_runtime_tree_sha256 = _require_sha256(
        python_identity.get("runtime_tree_sha256"),
        name="Python runtime tree SHA-256",
    )
    raw_import_paths = python_identity.get("import_paths")
    if (
        Path(str(python_identity.get("runtime", ""))) != python_runtime
        or Path(str(python_identity.get("manifest", ""))) != python_manifest
        or python_identity.get("manifest_sha256") != python_runtime_manifest_sha256
        or not python.is_absolute()
        or python.resolve(strict=True) != python
        or not python.is_relative_to(python_runtime)
        or not isinstance(raw_import_paths, list)
        or not raw_import_paths
    ):
        raise ValueError("Python runtime validator returned inconsistent bindings")
    dependency_paths: list[Path] = []
    for raw_path in raw_import_paths:
        dependency = Path(str(raw_path))
        if (
            not dependency.is_absolute()
            or dependency.resolve(strict=True) != dependency
            or not dependency.is_dir()
            or not dependency.is_relative_to(python_runtime)
            or dependency in dependency_paths
        ):
            raise ValueError("Python import path escaped its immutable runtime")
        dependency_paths.append(dependency)
    _validate_python_runtime(
        python,
        source_release=source_release,
        dependency_paths=dependency_paths,
    )

    expected_runtime_manifest_sha256 = _require_sha256(
        args.expected_runtime_manifest_sha256,
        name="expected runtime manifest SHA-256",
    )
    runtime_selector = args.runtime_dir
    runtime = _validated_runtime_shadow(runtime_selector)
    runtime_manifest = runtime / RUNTIME_MANIFEST
    observed_runtime_manifest_sha256 = _runtime_manifest_sha256(runtime)
    if observed_runtime_manifest_sha256 != expected_runtime_manifest_sha256:
        raise ValueError("runtime manifest SHA-256 does not match expectation")
    store_binding_sha256, store_rows = _runtime_store_binding(runtime)
    # Re-resolve an optional ``current`` selector and revalidate after deriving
    # the binding so a concurrent activation cannot mix two generations.
    runtime_after = _validated_runtime_shadow(runtime_selector)
    if (
        runtime_after != runtime
        or _runtime_manifest_sha256(runtime_after)
        != expected_runtime_manifest_sha256
    ):
        raise ValueError("runtime generation changed while rendering monitor units")
    repeated_store_binding, _rows = _runtime_store_binding(runtime_after)
    if repeated_store_binding != store_binding_sha256:
        raise ValueError("runtime LightRAG binding changed while rendering")

    expected_bindings = {
        "source_head": str(source_identity["head"]),
        "source_tree": str(source_identity["tree"]),
        "source_manifest_sha256": source_manifest_sha256,
        "python_runtime_manifest_sha256": python_runtime_manifest_sha256,
        "python_runtime_tree_sha256": python_runtime_tree_sha256,
        "python_executable_sha256": python_executable_sha256,
        "runtime_manifest_sha256": expected_runtime_manifest_sha256,
        "store_binding_sha256": store_binding_sha256,
    }
    deployment_binding_sha256 = _monitor_deployment_binding_sha256(
        expected_bindings, monitor_core=monitor_core
    )
    state_namespace_sha256 = _monitor_state_namespace_sha256(
        policy_sha256=policy_sha256,
        deployment_binding_sha256=deployment_binding_sha256,
    )

    api_token_file = _fixed_backend_api_token_file(args.api_token_file)
    _read_private_bearer_token(api_token_file, label="API token file")
    state_base, state_base_existed = _fixed_monitor_state_directory(
        args.state_dir, create=False
    )
    state_dir, state_existed = _monitor_state_namespace_directory(
        state_base,
        namespace_sha256=state_namespace_sha256,
        create=False,
    )
    service_output = _monitor_output_path(
        args.service_output, expected_name=MONITOR_SERVICE_NAME
    )
    timer_output = _monitor_output_path(
        args.timer_output, expected_name=MONITOR_TIMER_NAME
    )
    if service_output == timer_output:
        raise ValueError("monitor service and timer outputs must differ")
    for output in (service_output, timer_output):
        if any(
            output.is_relative_to(protected)
            for protected in (
                source_release,
                python_runtime,
                runtime,
                state_base,
            )
        ):
            raise ValueError("monitor unit output overlaps a protected deployment root")
    for name, path in (
        ("source release", source_release),
        ("source manifest", source_release / SOURCE_MANIFEST_FILE),
        ("Python runtime", python_runtime),
        ("Python manifest", python_manifest),
        ("Python executable", python),
        *(("Python import path", path) for path in dependency_paths),
        ("monitor policy", policy),
        ("monitor state base", state_base),
        ("monitor state directory", state_dir),
        ("API token file", api_token_file),
        ("runtime manifest", runtime_manifest),
    ):
        _monitor_systemd_path_token(path, name=name)

    service_payload = render_template(
        service_template,
        {
            "SOURCE_RELEASE": source_release,
            "SOURCE_HEAD": str(source_identity["head"]),
            "SOURCE_TREE": str(source_identity["tree"]),
            "SOURCE_MANIFEST": source_release / SOURCE_MANIFEST_FILE,
            "SOURCE_MANIFEST_SHA256": source_manifest_sha256,
            "PYTHON": python,
            "PYTHON_RUNTIME": python_runtime,
            "PYTHON_RUNTIME_MANIFEST": python_manifest,
            "PYTHON_RUNTIME_MANIFEST_SHA256": python_runtime_manifest_sha256,
            "PYTHON_RUNTIME_TREE_SHA256": python_runtime_tree_sha256,
            "PYTHON_EXECUTABLE_SHA256": python_executable_sha256,
            "PYTHON_IMPORT_PATH": os.pathsep.join(
                str(path) for path in dependency_paths
            ),
            "MONITOR_POLICY": policy,
            "MONITOR_POLICY_SHA256": policy_sha256,
            "MONITOR_STATE_DIR": state_dir,
            "API_TOKEN_FILE": api_token_file,
            "RUNTIME_MANIFEST": runtime_manifest,
            "RUNTIME_MANIFEST_SHA256": expected_runtime_manifest_sha256,
            "LIGHTRAG_STORE_BINDING_SHA256": store_binding_sha256,
        },
    )
    timer_payload = render_template(timer_template, {})
    if (
        _monitor_renderer_checkout_binding(
            selected_renderer,
            expected_head=str(source_identity["head"]),
            expected_tree=str(source_identity["tree"]),
        )
        != renderer_checkout
    ):
        raise ValueError("renderer checkout binding changed while rendering")
    if args.apply:
        _monitor_state_namespace_directory(
            state_base,
            namespace_sha256=state_namespace_sha256,
            create=True,
        )
    service_result = _render_result(
        kind="monitor-systemd-service",
        output=service_output,
        payload=service_payload,
        apply=args.apply,
        mode=0o644,
    )
    timer_result = _render_result(
        kind="monitor-systemd-timer",
        output=timer_output,
        payload=timer_payload,
        apply=args.apply,
        mode=0o644,
    )
    service_result["content"] = service_payload.decode("utf-8")
    timer_result["content"] = timer_payload.decode("utf-8")
    return {
        "kind": "monitor-systemd-units",
        "status": "installed" if args.apply else "dry-run",
        "source_head": source_identity["head"],
        "source_tree": source_identity["tree"],
        "renderer_sha256": renderer_checkout["renderer_sha256"],
        "renderer_checkout_bound": True,
        "source_manifest_sha256": source_manifest_sha256,
        "policy_sha256": policy_sha256,
        "python_runtime_manifest_sha256": python_runtime_manifest_sha256,
        "python_runtime_tree_sha256": python_runtime_tree_sha256,
        "python_executable_sha256": python_executable_sha256,
        "runtime_manifest_sha256": expected_runtime_manifest_sha256,
        "lightrag_store_binding_sha256": store_binding_sha256,
        "lightrag_store_file_count": len(store_rows),
        "deployment_binding_sha256": deployment_binding_sha256,
        "monitor_state_namespace_sha256": state_namespace_sha256,
        "monitor_state_base": str(state_base),
        "monitor_state_base_existed_before": state_base_existed,
        "monitor_state_dir": str(state_dir),
        "monitor_state_existed_before": state_existed,
        "service": service_result,
        "timer": timer_result,
        "manager_reloaded": False,
        "units_enabled": False,
        "units_started": False,
    }


def _nginx_literal_path(value: Path, *, name: str) -> Path:
    """Return one injection-safe absolute path for an unquoted Nginx token."""

    raw = os.fspath(value)
    forbidden = frozenset('$;{}#"\'\\`')
    if (
        not raw
        or not value.is_absolute()
        or ".." in value.parts
        or any(
            character.isspace()
            or ord(character) < 0x20
            or ord(character) == 0x7F
            or character in forbidden
            for character in raw
        )
    ):
        raise ValueError(f"--{name} is not a safe absolute Nginx path token")
    return value


def _validated_nginx_input_file(path: Path, *, kind: str) -> bytes:
    """Read one stable credential/TLS input with a fail-closed mode policy."""

    try:
        resolved = path.resolve(strict=True)
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"Nginx {kind} input is unavailable") from exc
    mode = stat.S_IMODE(info.st_mode)
    if (
        resolved != path
        or stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid not in {0, os.geteuid()}
        or info.st_nlink != 1
        or mode & 0o022
    ):
        raise ValueError(f"Nginx {kind} input has an unsafe identity or mode")
    if kind == "private key" and mode & 0o077:
        raise ValueError("Nginx private key must not be group/world accessible")
    if kind == "htpasswd" and mode & 0o007:
        raise ValueError("Nginx htpasswd must not be world accessible")
    try:
        payload, opened = _stable_regular_bytes(path, max_bytes=2 * 1024 * 1024)
        path_after = path.lstat()
        resolved_after = path.resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise ValueError(f"Nginx {kind} input changed while inspected") from exc
    if (
        not payload
        or resolved_after != path
        or _file_stability_tuple(info) != _file_stability_tuple(opened)
        or _file_stability_tuple(path_after) != _file_stability_tuple(opened)
    ):
        raise ValueError(f"Nginx {kind} input changed while inspected")
    return payload


def _validate_certificate_pair_bytes(certificate: bytes, private_key: bytes) -> None:
    """Validate the exact captured TLS bytes without reopening source paths."""

    try:
        with tempfile.TemporaryDirectory(prefix="wpg-certificate-check-") as directory:
            private_directory = Path(directory)
            certificate_copy = private_directory / "certificate.pem"
            private_key_copy = private_directory / "private-key.pem"
            _write_exclusive_blob(certificate_copy, certificate, mode=0o600)
            _write_exclusive_blob(private_key_copy, private_key, mode=0o600)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(
                certfile=os.fspath(certificate_copy),
                keyfile=os.fspath(private_key_copy),
            )
    except (OSError, ssl.SSLError) as exc:
        raise ValueError("Nginx certificate/private key validation failed") from exc


def _validate_htpasswd_payload(payload: bytes) -> None:
    try:
        lines = payload.decode("utf-8", errors="strict").splitlines()
    except UnicodeError as exc:
        raise ValueError("Nginx htpasswd is not valid UTF-8") from exc
    accepted = 0
    digest_pattern = re.compile(
        r"(?:\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}"
        r"|\$apr1\$[^$:\s]{1,16}\$[./A-Za-z0-9]{22}"
        r"|\{SHA\}[A-Za-z0-9+/]{27}=|\$[156]\$[^:\s]+)\Z"
    )
    for line in lines:
        if not line or line.startswith("#"):
            continue
        username, separator, digest = line.partition(":")
        if (
            not separator
            or re.fullmatch(r"[A-Za-z0-9._@-]{1,128}", username) is None
            or digest_pattern.fullmatch(digest) is None
        ):
            raise ValueError("Nginx htpasswd contains an invalid or plaintext entry")
        accepted += 1
    if accepted == 0:
        raise ValueError("Nginx htpasswd contains no usable account")


def _read_private_bearer_token(path: Path, *, label: str) -> str:
    """Read one canonical, single-link, owner-only proxy bearer token."""

    expanded = path.expanduser()
    try:
        resolved = expanded.resolve(strict=True)
        info = expanded.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if (
        not expanded.is_absolute()
        or resolved != expanded
        or stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise ValueError(
            f"{label} must be a canonical, current-user-owned, single-link 0600 file"
        )
    try:
        payload, opened = _stable_regular_bytes(expanded, max_bytes=1024)
        path_after = expanded.lstat()
        resolved_after = expanded.resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} is not stably readable") from exc
    if (
        resolved_after != expanded
        or _file_stability_tuple(info) != _file_stability_tuple(opened)
        or _file_stability_tuple(path_after) != _file_stability_tuple(opened)
    ):
        raise ValueError(f"{label} changed while inspected")
    try:
        token = payload.decode("utf-8", errors="strict").strip()
    except UnicodeError as exc:
        raise ValueError(f"{label} is not valid UTF-8") from exc
    if payload not in {token.encode("utf-8"), (token + "\n").encode("utf-8")}:
        raise ValueError(f"{label} must contain exactly one token")
    if re.fullmatch(r"[A-Za-z0-9._~-]{32,256}", token) is None:
        raise ValueError(f"{label} must contain one 32..256 character URL-safe token")
    return token


def _fixed_backend_api_token_file(path: Path) -> Path:
    """Require the production token at one persistent, private passwd-home path."""

    try:
        home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except KeyError as exc:
        raise ValueError("effective user has no passwd home") from exc
    expected = home / BACKEND_API_TOKEN_RELATIVE
    expanded = path.expanduser()
    if (
        not home.is_absolute()
        or Path(os.path.realpath(home)) != home
        or expanded != expected
        or Path(os.path.realpath(expanded)) != expanded
    ):
        raise ValueError(
            "API token file must be the fixed passwd-home backend.token path"
        )
    chain = (home, home / ".config", expected.parent)
    for index, directory in enumerate(chain):
        try:
            info = directory.lstat()
        except OSError as exc:
            raise ValueError("API token directory chain is unavailable") from exc
        mode = stat.S_IMODE(info.st_mode)
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or mode & 0o022
            or (index == len(chain) - 1 and mode != 0o700)
        ):
            raise ValueError(
                "API token directory chain must be owned, real, and non-writable"
            )
    return expected


def render_nginx(args: argparse.Namespace) -> dict[str, Any]:
    server_name = str(args.server_name).strip().rstrip(".")
    labels = server_name.split(".")
    if (
        not server_name
        or len(server_name) > 253
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not all(
                character.isascii()
                and (character.isalnum() or character == "-")
                for character in label
            )
            for label in labels
        )
    ):
        raise ValueError("--server-name must be one DNS hostname or IPv4 address")
    if (
        isinstance(args.upstream_port, bool)
        or not isinstance(args.upstream_port, int)
        or not 1 <= args.upstream_port <= 65535
    ):
        raise ValueError("--upstream-port must be in 1..65535")
    authenticated_gate_port = getattr(
        args, "authenticated_gate_port", NGINX_AUTHENTICATED_GATE_PORT
    )
    if (
        isinstance(authenticated_gate_port, bool)
        or not isinstance(authenticated_gate_port, int)
        or not 1024 <= authenticated_gate_port <= 65535
    ):
        raise ValueError("--authenticated-gate-port must be in 1024..65535")
    if authenticated_gate_port in {80, 443, 8765, args.upstream_port}:
        raise ValueError(
            "--authenticated-gate-port must differ from the public, legacy, "
            "and backend ports"
        )
    inputs = {
        "tls_certificate": _nginx_literal_path(
            args.tls_certificate, name="tls-certificate"
        ),
        "tls_certificate_key": _nginx_literal_path(
            args.tls_certificate_key, name="tls-certificate-key"
        ),
        "htpasswd": _nginx_literal_path(args.htpasswd, name="htpasswd"),
    }
    backend_api_token_file = _fixed_backend_api_token_file(
        args.backend_api_token_file
    )
    backend_api_token = _read_private_bearer_token(
        backend_api_token_file, label="backend API token file"
    )
    validate_privileged_inputs = bool(
        args.apply
        and not getattr(args, "defer_privileged_input_validation", False)
    )
    if validate_privileged_inputs:
        certificate_payload = _validated_nginx_input_file(
            inputs["tls_certificate"], kind="certificate"
        )
        private_key_payload = _validated_nginx_input_file(
            inputs["tls_certificate_key"], kind="private key"
        )
        htpasswd_payload = _validated_nginx_input_file(
            inputs["htpasswd"], kind="htpasswd"
        )
        _validate_htpasswd_payload(htpasswd_payload)
        _validate_certificate_pair_bytes(certificate_payload, private_key_payload)
    payload = render_template(
        args.template,
        {
            "UPSTREAM_PORT": str(args.upstream_port),
            "AUTHENTICATED_GATE_PORT": str(authenticated_gate_port),
            "SERVER_NAME": server_name,
            "TLS_CERTIFICATE": inputs["tls_certificate"],
            "TLS_CERTIFICATE_KEY": inputs["tls_certificate_key"],
            "HTPASSWD": inputs["htpasswd"],
            "BACKEND_API_TOKEN": backend_api_token,
        },
    )
    result = _render_result(
        kind="nginx-config",
        output=args.output.expanduser(),
        payload=payload,
        apply=args.apply,
        mode=0o600,
    )
    result["privileged_inputs_validated"] = validate_privileged_inputs
    result["authenticated_gate_port"] = authenticated_gate_port
    return result


def render_environment(args: argparse.Namespace) -> dict[str, Any]:
    if args.host not in {"localhost", "127.0.0.1", "0.0.0.0"}:
        raise ValueError(
            "--host must be localhost, 127.0.0.1, or 0.0.0.0 so the "
            "checked-in loopback health gate reaches the listener"
        )
    data_dir = args.data_dir.expanduser().resolve()
    api_config = args.api_config.expanduser().resolve()
    runtime_dir = _validated_runtime_shadow(args.runtime_dir, data_dir=data_dir)
    shared_state_dir = _private_directory(args.shared_state_dir, create=False)
    _validate_shared_tavily_state(shared_state_dir, api_config=api_config)
    runtime_manifest = runtime_dir / RUNTIME_MANIFEST
    runtime_manifest_sha256 = sha256_file(runtime_manifest)
    api_token_file = (
        args.api_token_file.expanduser() if args.api_token_file is not None else None
    )
    if api_token_file is not None:
        _read_private_bearer_token(api_token_file, label="API token file")
    if args.require_api_auth and args.api_token_file is None:
        raise ValueError("--require-api-auth requires --api-token-file")
    try:
        trusted_proxy_networks = tuple(
            ipaddress.ip_network(item.strip(), strict=False)
            for item in args.trusted_proxy_cidrs.split(",")
            if item.strip()
        )
    except ValueError as exc:
        raise ValueError("--trusted-proxy-cidrs contains an invalid network") from exc
    if not trusted_proxy_networks:
        raise ValueError("--trusted-proxy-cidrs must not be empty")
    trusted_proxy_cidrs = ",".join(str(item) for item in trusted_proxy_networks)
    try:
        allowed_client_networks = tuple(
            ipaddress.ip_network(item.strip(), strict=False)
            for item in args.allowed_client_cidrs.split(",")
            if item.strip()
        )
    except ValueError as exc:
        raise ValueError("--allowed-client-cidrs contains an invalid network") from exc
    if not allowed_client_networks:
        raise ValueError("--allowed-client-cidrs must not be empty")
    allowed_client_cidrs = ",".join(str(item) for item in allowed_client_networks)
    if args.trust_proxy:
        if args.host != "127.0.0.1":
            raise ValueError("--trust-proxy requires --host=127.0.0.1")
        if not args.require_api_auth or api_token_file is None:
            raise ValueError(
                "--trust-proxy requires --require-api-auth and --api-token-file"
            )
        ipv4_loopback = ipaddress.ip_network("127.0.0.0/8")
        ipv6_loopback = ipaddress.ip_network("::1/128")

        def is_loopback(
            network: ipaddress.IPv4Network | ipaddress.IPv6Network,
        ) -> bool:
            expected = ipv4_loopback if network.version == 4 else ipv6_loopback
            return network.subnet_of(expected)

        if any(not is_loopback(item) for item in trusted_proxy_networks):
            raise ValueError(
                "--trusted-proxy-cidrs must contain only loopback networks "
                "when --trust-proxy is enabled"
            )
        if any(not is_loopback(item) for item in allowed_client_networks):
            raise ValueError(
                "--allowed-client-cidrs must contain only loopback networks "
                "when --trust-proxy is enabled"
            )
    lines = [
        "# Generated by python -m scripts.manage_deployment render-env.",
        "# This file contains paths and controls only, never API credentials.",
        f"WPG_HOST={args.host}",
        f"WPG_PORT={args.port}",
        f"WPG_DATA_DIR={data_dir}",
        f"WPG_API_CONFIG={api_config}",
        f"WPG_API_CACHE_DIR={runtime_dir / 'api_cache'}",
        f"WPG_RESULT_CACHE_DIR={runtime_dir / 'api_cache' / 'result'}",
        f"WPG_QUERY_EMBEDDING_CACHE={runtime_dir / 'query_embedding_cache.json.gz'}",
        f"WPG_LIGHTRAG_EMBEDDING_CACHE={runtime_dir / 'lightrag_embedding_cache.json.gz'}",
        f"WPG_LIGHTRAG_WORKING_DIR={runtime_dir / 'lightrag_storage'}",
        f"WPG_GRAPH_PATH={data_dir / 'venue_graph.json.gz'}",
        f"WPG_TAVILY_STATE_FILE={shared_state_dir / TAVILY_STATE_NAME}",
        f"WPG_RUNTIME_GENERATION={runtime_dir}",
        f"WPG_RUNTIME_MANIFEST={runtime_manifest}",
        f"WPG_RUNTIME_MANIFEST_SHA256={runtime_manifest_sha256}",
        "WPG_STRICT_GRAPH_READ_ONLY=1",
        "WPG_REQUIRE_RUNTIME_SHADOW=1",
        f"WPG_RATE_LIMIT_REQUESTS={args.rate_limit_requests}",
        f"WPG_RATE_LIMIT_WINDOW_SECONDS={args.rate_limit_window_seconds}",
        f"WPG_MAX_CONCURRENT_CONNECTIONS={args.max_concurrent_connections}",
        f"WPG_MAX_CONCURRENT_SEARCHES={args.max_concurrent_searches}",
        f"WPG_REQUEST_BODY_LIMIT={args.request_body_limit}",
        f"WPG_REQUEST_READ_TIMEOUT={args.request_read_timeout}",
        f"WPG_AUDIT_LOG={1 if args.audit_log else 0}",
        f"WPG_ALLOWED_CLIENT_CIDRS={allowed_client_cidrs}",
        f"WPG_TRUST_PROXY_HEADERS={1 if args.trust_proxy else 0}",
        f"WPG_TRUSTED_PROXY_CIDRS={trusted_proxy_cidrs}",
        f"WPG_REQUIRE_API_AUTH={1 if args.require_api_auth else 0}",
    ]
    if api_token_file is not None:
        lines.append(
            "WPG_API_TOKEN_FILE="
            + _safe_replacement(api_token_file, "api-token-file")
        )
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    return _render_result(
        kind="runtime-environment",
        output=args.output.expanduser(),
        payload=payload,
        apply=args.apply,
        mode=0o600,
    )


def restore_file(args: argparse.Namespace) -> dict[str, Any]:
    source = args.source.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"restore source is not a file: {source}")
    return _render_result(
        kind="restore",
        output=args.output.expanduser(),
        payload=source.read_bytes(),
        apply=args.apply,
        mode=args.mode,
    )


def _read_health_token(path: Path | None) -> str | None:
    if path is None:
        return None
    return _read_private_bearer_token(path, label="health bearer token file")


def _lower_hex(value: Any, *lengths: int) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) in lengths
        and value == value.casefold()
        and all(character in "0123456789abcdef" for character in value)
    )


def _process_executable_identity(pid: int) -> tuple[str, str]:
    """Read one live process executable path and bytes without pathname races."""

    proc_exe = Path("/proc") / str(pid) / "exe"
    before_link = os.readlink(proc_exe)
    if before_link.endswith(" (deleted)"):
        raise ValueError("live process executable has been unlinked")
    descriptor = os.open(proc_exe, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("live process executable is not a regular file")
        digest = hashlib.sha256()
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after_link = os.readlink(proc_exe)
    if (
        before_link != after_link
        or _file_stability_tuple(before) != _file_stability_tuple(after)
    ):
        raise ValueError("live process executable changed while being read")
    return str(Path(before_link).resolve()), digest.hexdigest()


def _read_process_environment(pid: int, *, max_bytes: int = 1024 * 1024) -> dict[str, str]:
    """Read a bounded, duplicate-free snapshot of /proc/PID/environ."""

    descriptor = os.open(
        Path("/proc") / str(pid) / "environ",
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, min(64 * 1024, max_bytes + 1 - total))
            if not block:
                break
            total += len(block)
            if total > max_bytes:
                raise ValueError("live process environment exceeds its byte bound")
            chunks.append(block)
    finally:
        os.close(descriptor)
    result: dict[str, str] = {}
    for raw_entry in b"".join(chunks).split(b"\0"):
        if not raw_entry:
            continue
        try:
            raw_name, raw_value = raw_entry.split(b"=", 1)
            name = raw_name.decode("ascii")
            value = raw_value.decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise ValueError("live process environment is malformed") from exc
        if name in result:
            raise ValueError("live process environment contains duplicate names")
        result[name] = value
    return result


def _python_runtime_process_failures(
    *, process_pid: int, process_start: int, health_runtime: Any
) -> list[str]:
    """Bind health, target ``/proc`` state, and the approved Python runtime.

    The checker process is deliberately not the trust source.  A bare
    operator invocation has no runtime variables of its own, while the target
    service does.  Reading the target environment also prevents an omitted
    four-variable binding from silently turning this mandatory gate off.
    """

    required_health_keys = {
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
    if not isinstance(health_runtime, dict) or set(health_runtime) != required_health_keys:
        return ["Python runtime health identity is absent or malformed"]
    try:
        if process_start_ticks(process_pid) != process_start:
            raise ValueError("process start tuple changed before runtime validation")
        process_environment = _read_process_environment(process_pid)
    except (OSError, ValueError):
        return ["live process Python runtime environment is unavailable"]
    configured = {
        PYTHON_RUNTIME_ENV: process_environment.get(PYTHON_RUNTIME_ENV, ""),
        PYTHON_RUNTIME_MANIFEST_ENV: process_environment.get(
            PYTHON_RUNTIME_MANIFEST_ENV, ""
        ),
        PYTHON_RUNTIME_MANIFEST_SHA256_ENV: process_environment.get(
            PYTHON_RUNTIME_MANIFEST_SHA256_ENV, ""
        ).casefold(),
        PYTHON_RUNTIME_TREE_SHA256_ENV: process_environment.get(
            PYTHON_RUNTIME_TREE_SHA256_ENV, ""
        ).casefold(),
    }
    if not all(configured.values()):
        return ["live process Python runtime environment binding is incomplete"]
    if not _lower_hex(configured[PYTHON_RUNTIME_MANIFEST_SHA256_ENV], 64) or not _lower_hex(
        configured[PYTHON_RUNTIME_TREE_SHA256_ENV], 64
    ):
        return ["live process Python runtime environment digest is invalid"]
    runtime = Path(configured[PYTHON_RUNTIME_ENV]).expanduser().resolve()
    expected_manifest = runtime / PYTHON_RUNTIME_MANIFEST
    configured_manifest = Path(
        configured[PYTHON_RUNTIME_MANIFEST_ENV]
    ).expanduser().resolve()
    if configured_manifest != expected_manifest:
        return ["Python runtime manifest path is not bound to its runtime"]
    try:
        identity = validate_python_runtime_release(
            expected_manifest,
            expected_manifest_sha256=configured[
                PYTHON_RUNTIME_MANIFEST_SHA256_ENV
            ],
        )
        if identity["runtime_tree_sha256"] != configured[
            PYTHON_RUNTIME_TREE_SHA256_ENV
        ]:
            raise ValueError("runtime tree SHA-256 differs")
        executable_path, executable_sha256 = _process_executable_identity(process_pid)
        if process_start_ticks(process_pid) != process_start:
            raise ValueError("process start tuple changed during runtime validation")
    except (OSError, ValueError, KeyError):
        return ["live process Python runtime validation failed"]
    if (
        executable_path != identity["python_executable"]
        or executable_sha256 != identity["python_executable_sha256"]
    ):
        return ["live process executable differs from the approved Python runtime"]
    expected_process_environment = {
        PYTHON_RUNTIME_ENV: identity["runtime"],
        PYTHON_RUNTIME_MANIFEST_ENV: identity["manifest"],
        PYTHON_RUNTIME_MANIFEST_SHA256_ENV: identity["manifest_sha256"],
        PYTHON_RUNTIME_TREE_SHA256_ENV: identity["runtime_tree_sha256"],
    }
    if any(
        process_environment.get(name) != value
        for name, value in expected_process_environment.items()
    ):
        return ["live process environment differs from the approved Python runtime"]
    checker_environment = {
        name: os.environ.get(name, "").strip()
        for name in expected_process_environment
    }
    if any(checker_environment.values()) and checker_environment != expected_process_environment:
        return ["health checker environment differs from the approved Python runtime"]
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
        "process_pid": process_pid,
        "process_start_ticks": process_start,
    }
    if health_runtime != expected_health:
        return ["health Python runtime differs from the live approved runtime"]
    return []


def _worker_process_health_failures(
    *, worker_process: Any, parent_pid: int, source: Any, python_runtime: Any
) -> list[str]:
    """Independently bind the persistent recommendation worker to health."""

    worker_keys = {
        "exact",
        "pid",
        "start_ticks",
        "executable_sha256",
        "proc_exe_verified",
        "interpreter",
        "source",
        "python_runtime",
    }
    interpreter = {
        "argv_exact": True,
        "no_site": True,
        "safe_path": True,
        "dont_write_bytecode": True,
    }
    if (
        not isinstance(worker_process, dict)
        or set(worker_process) != worker_keys
        or worker_process.get("exact") is not True
        or worker_process.get("proc_exe_verified") is not True
        or worker_process.get("interpreter") != interpreter
        or not isinstance(source, dict)
        or not isinstance(python_runtime, dict)
    ):
        return ["persistent worker process identity is absent or malformed"]
    expected_source = {
        "head": source.get("head"),
        "tree": source.get("tree"),
        "manifest_sha256": source.get("manifest_sha256"),
        "files_verified": True,
    }
    expected_python_runtime = {
        "manifest_sha256": python_runtime.get("manifest_sha256"),
        "runtime_tree_sha256": python_runtime.get("runtime_tree_sha256"),
        "python_executable_sha256": python_runtime.get(
            "python_executable_sha256"
        ),
        "python_version": python_runtime.get("python_version"),
        "python_soabi": python_runtime.get("python_soabi"),
        "python_platform": python_runtime.get("python_platform"),
        "wheel_count": python_runtime.get("wheel_count"),
        "elf_audit_sha256": python_runtime.get("elf_audit_sha256"),
        "system_library_count": python_runtime.get("system_library_count"),
        "system_directory_count": python_runtime.get("system_directory_count"),
        "files_verified": True,
        "proc_exe_matches": True,
        "system_abi_stat_verified": True,
    }
    if (
        worker_process.get("source") != expected_source
        or worker_process.get("python_runtime") != expected_python_runtime
    ):
        return ["persistent worker source/runtime proof differs from its parent"]
    worker_pid = worker_process.get("pid")
    worker_start = worker_process.get("start_ticks")
    executable_sha256 = worker_process.get("executable_sha256")
    if (
        isinstance(worker_pid, bool)
        or not isinstance(worker_pid, int)
        or worker_pid <= 0
        or worker_pid == parent_pid
        or isinstance(worker_start, bool)
        or not isinstance(worker_start, int)
        or worker_start <= 0
        or not _lower_hex(executable_sha256, 64)
        or executable_sha256 != python_runtime.get("python_executable_sha256")
    ):
        return ["persistent worker PID/start/executable proof is invalid"]
    try:
        if process_start_ticks(worker_pid) != worker_start:
            raise ValueError("worker start tuple changed")
        executable_path, observed_sha256 = _process_executable_identity(worker_pid)
        command_raw = (Path("/proc") / str(worker_pid) / "cmdline").read_bytes()
        if len(command_raw) > 1024 * 1024:
            raise ValueError("worker command line exceeds its bound")
        command = [
            value.decode("utf-8", errors="strict")
            for value in command_raw.split(b"\0")
            if value
        ]
        status_raw = (Path("/proc") / str(worker_pid) / "status").read_text(
            encoding="ascii"
        )
        parent_rows = [
            row for row in status_raw.splitlines() if row.startswith("PPid:")
        ]
        if len(parent_rows) != 1:
            raise ValueError("worker parent process is unavailable")
        observed_parent = int(parent_rows[0].split(":", 1)[1].strip())
        parent_environment = _read_process_environment(parent_pid)
        worker_environment = _read_process_environment(worker_pid)
        if process_start_ticks(worker_pid) != worker_start:
            raise ValueError("worker start tuple changed during validation")
    except (OSError, UnicodeError, ValueError):
        return ["persistent worker live process validation failed"]
    if (
        observed_parent != parent_pid
        or observed_sha256 != executable_sha256
        or command
        != [
            executable_path,
            "-S",
            "-P",
            "-B",
            "-m",
            "where_paper_go.worker",
        ]
    ):
        return ["persistent worker executable/parent/flags differ"]
    inherited_names = {
        SOURCE_HEAD_ENV,
        SOURCE_TREE_ENV,
        SOURCE_MANIFEST_ENV,
        SOURCE_MANIFEST_SHA256_ENV,
        PYTHON_RUNTIME_ENV,
        PYTHON_RUNTIME_MANIFEST_ENV,
        PYTHON_RUNTIME_MANIFEST_SHA256_ENV,
        PYTHON_RUNTIME_TREE_SHA256_ENV,
        "PATH",
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
    if any(
        worker_environment.get(name) != parent_environment.get(name)
        for name in inherited_names
    ):
        return ["persistent worker inherited identity/security environment differs"]
    forbidden_exact = {
        "GCONV_PATH",
        "GLIBC_TUNABLES",
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
    }
    if any(
        name in forbidden_exact
        or name.startswith("LD_")
        or (name.startswith("PYTHON") and name not in inherited_names)
        for name in worker_environment
    ):
        return ["persistent worker retains a forbidden injection environment"]
    return []


def validate_health_payload(
    payload: Any, *, expected_process_pid: int | None = None
) -> list[str]:
    failures: list[str] = []
    if not isinstance(payload, dict):
        return ["health payload is not an object"]
    if payload.get("ready") is not True or payload.get("status") != "ready":
        failures.append("service is not ready")
    if payload.get("backend") != EXPECTED_BACKEND:
        failures.append("mandatory backend contract differs")
    for name in ("graph", "vectors"):
        section = payload.get(name)
        if not isinstance(section, dict) or section.get("exists") is not True:
            failures.append(f"{name} is unavailable")
    lightrag = payload.get("lightrag")
    if (
        not isinstance(lightrag, dict)
        or lightrag.get("exists") is not True
        or lightrag.get("manifest_exists") is not True
        or lightrag.get("mode") != "mix"
    ):
        failures.append("LightRAG mix workspace is unavailable")
    runtime = payload.get("runtime")
    if (
        not isinstance(runtime, dict)
        or runtime.get("process_ready") is not True
        or runtime.get("bindings_current") is not True
        or runtime.get("write_isolated") is not True
        or runtime.get("tavily_state_shared") is not True
        or runtime.get("ready") is not True
    ):
        failures.append("worker or runtime binding is not ready")
    if isinstance(runtime, dict):
        worker_bindings = runtime.get("worker_bindings")
        manifest = runtime.get("runtime_manifest")
        if (
            not isinstance(worker_bindings, dict)
            or worker_bindings.get("exact_match") is not True
        ):
            failures.append("worker binding identity is not exact")
        if (
            not isinstance(manifest, dict)
            or manifest.get("ready") is not True
            or manifest.get("sha256_matched") is not True
            or manifest.get("path_bound") is not True
        ):
            failures.append("runtime manifest is not bound to the running generation")
        store_verification = runtime.get("lightrag_store_verification")
        runtime_manifest_sha256 = (
            manifest.get("actual_sha256") if isinstance(manifest, dict) else None
        )
        if not (
            isinstance(store_verification, dict)
            and store_verification.get("required") is True
            and store_verification.get("verified") is True
            and store_verification.get("file_count")
            == len(RUNTIME_LIGHTRAG_FILES)
            and _lower_hex(store_verification.get("manifest_sha256"), 64)
            and _lower_hex(store_verification.get("store_binding_sha256"), 64)
            and _lower_hex(runtime_manifest_sha256, 64)
            and store_verification.get("manifest_sha256")
            == runtime_manifest_sha256
        ):
            failures.append("frozen LightRAG store proof is absent or invalid")
        expected_runtime_manifest = os.environ.get(
            "WPG_RUNTIME_MANIFEST_SHA256", ""
        ).strip().casefold()
        if expected_runtime_manifest and (
            not _lower_hex(expected_runtime_manifest, 64)
            or runtime_manifest_sha256 != expected_runtime_manifest
        ):
            failures.append("runtime manifest health identity differs from environment")
    source = payload.get("source")
    source_ready = bool(
        isinstance(source, dict)
        and source.get("ready") is True
        and source.get("files_verified") is True
        and isinstance(source.get("file_count"), int)
        and not isinstance(source.get("file_count"), bool)
        and source.get("file_count") > 0
        and _lower_hex(source.get("head"), 40, 64)
        and _lower_hex(source.get("tree"), 40, 64)
        and _lower_hex(source.get("manifest_sha256"), 64)
        and isinstance(source.get("process_pid"), int)
        and not isinstance(source.get("process_pid"), bool)
        and source.get("process_pid") > 0
        and isinstance(source.get("process_start_ticks"), int)
        and not isinstance(source.get("process_start_ticks"), bool)
        and source.get("process_start_ticks") > 0
    )
    if not source_ready:
        failures.append("immutable source/process identity is absent or invalid")
    elif isinstance(source, dict):
        source_pid = int(source["process_pid"])
        try:
            observed_start_ticks = process_start_ticks(source_pid)
        except ValueError:
            observed_start_ticks = None
        if observed_start_ticks != source.get("process_start_ticks"):
            failures.append("source process start identity is not live/current")
        if expected_process_pid is not None and source_pid != expected_process_pid:
            failures.append("health response process is not the expected service MainPID")
        for environment_name, field in (
            (SOURCE_HEAD_ENV, "head"),
            (SOURCE_TREE_ENV, "tree"),
            (SOURCE_MANIFEST_SHA256_ENV, "manifest_sha256"),
        ):
            expected = os.environ.get(environment_name, "").strip().casefold()
            if expected and source.get(field) != expected:
                failures.append(
                    "source health identity differs from the service environment"
                )
                break
        configured_source = {
            "head": os.environ.get(SOURCE_HEAD_ENV, "").strip().casefold(),
            "tree": os.environ.get(SOURCE_TREE_ENV, "").strip().casefold(),
            "manifest": os.environ.get(SOURCE_MANIFEST_ENV, "").strip(),
            "manifest_sha256": os.environ.get(
                SOURCE_MANIFEST_SHA256_ENV, ""
            ).strip().casefold(),
        }
        if any(configured_source.values()):
            try:
                local_source = validate_source_release(
                    Path(configured_source["manifest"]),
                    expected_head=configured_source["head"],
                    expected_tree=configured_source["tree"],
                    expected_manifest_sha256=configured_source["manifest_sha256"],
                )
            except (OSError, ValueError):
                failures.append("local immutable source release validation failed")
            else:
                if (
                    source.get("file_count") != local_source["file_count"]
                    or source.get("head") != local_source["head"]
                    or source.get("tree") != local_source["tree"]
                    or source.get("manifest_sha256")
                    != local_source["manifest_sha256"]
                ):
                    failures.append(
                        "health source proof differs from the local approved release"
                    )
        failures.extend(
            _python_runtime_process_failures(
                process_pid=source_pid,
                process_start=int(source["process_start_ticks"]),
                health_runtime=payload.get("python_runtime"),
            )
        )
        if isinstance(runtime, dict):
            failures.extend(
                _worker_process_health_failures(
                    worker_process=runtime.get("worker_process"),
                    parent_pid=source_pid,
                    source=source,
                    python_runtime=payload.get("python_runtime"),
                )
            )
    checks = payload.get("checks")
    if (
        not isinstance(checks, dict)
        or checks.get("lightrag_store_hashes") is not True
        or checks.get("source_identity") is not True
        or checks.get("python_runtime_identity") is not True
        or checks.get("worker_process_identity") is not True
    ):
        failures.append("mandatory integrity health checks are not true")
    config = payload.get("config")
    if not isinstance(config, dict) or config.get("ready") is not True:
        failures.append("LLM/embedding/Search configuration is incomplete")
    elif (
        not isinstance(config.get("search_quota_audit"), dict)
        or config["search_quota_audit"].get("ready") is not True
        or config["search_quota_audit"].get("replicated_revision") is not True
        or config["search_quota_audit"].get("configuration_current") is not True
    ):
        failures.append("Search quota state is not durably replicated/current")
    return failures


def _parse_expected_hash(value: str) -> tuple[Path, str]:
    path_text, separator, expected = value.rpartition("=")
    if not separator or len(expected) != 64:
        raise ValueError("--expect-sha256 must use PATH=64_HEX_DIGEST")
    try:
        int(expected, 16)
    except ValueError as exc:
        raise ValueError("--expect-sha256 digest is not hexadecimal") from exc
    return Path(path_text).expanduser(), expected.casefold()


def health_check(args: argparse.Namespace) -> dict[str, Any]:
    token_path = args.token_file.expanduser() if args.token_file else None
    require_api_auth = os.environ.get("WPG_REQUIRE_API_AUTH", "0").strip().casefold()
    if require_api_auth not in {"0", "1", "false", "true", "no", "yes", "off", "on"}:
        raise ValueError("WPG_REQUIRE_API_AUTH must be a boolean")
    if token_path is None and require_api_auth in {"1", "true", "yes", "on"}:
        raw_token_path = os.environ.get("WPG_API_TOKEN_FILE", "").strip()
        if not raw_token_path:
            raise ValueError(
                "WPG_REQUIRE_API_AUTH requires WPG_API_TOKEN_FILE for health"
            )
        token_path = Path(raw_token_path).expanduser()
    token = _read_health_token(token_path)
    last_failure: str | None = None
    payload: Any = None
    for attempt in range(1, args.attempts + 1):
        request = urllib.request.Request(args.url, headers={"Accept": "application/json"})
        if token is not None:
            request.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(request, timeout=args.timeout) as response:
                payload = json.load(response)
            failures = validate_health_payload(
                payload,
                expected_process_pid=getattr(args, "expect_process_pid", None),
            )
            if not failures:
                break
            last_failure = "; ".join(failures)
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
            last_failure = f"{type(exc).__name__}: {exc}"
        if attempt < args.attempts:
            time.sleep(args.interval)
    else:
        raise RuntimeError(last_failure or "health check failed")

    hashes: list[dict[str, Any]] = []
    for raw_expectation in args.expect_sha256:
        path, expected = _parse_expected_hash(raw_expectation)
        actual = sha256_file(path)
        if actual != expected:
            raise RuntimeError(f"SHA-256 mismatch for {path}")
        hashes.append({"file": path.name, "sha256": actual, "matched": True})
    return {
        "status": "ready",
        "url": args.url,
        "attempt": attempt,
        "backend": payload.get("backend"),
        "source": payload.get("source"),
        "python_runtime": payload.get("python_runtime"),
        "runtime": payload.get("runtime"),
        "hashes": hashes,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    systemd = subparsers.add_parser("render-systemd", help="render the user unit")
    systemd.add_argument("--template", type=Path, default=SYSTEMD_TEMPLATE)
    systemd.add_argument(
        "--source-release",
        type=Path,
        required=True,
        help="explicit content-addressed release from prepare-source-release",
    )
    systemd.add_argument(
        "--expected-source-manifest-sha256",
        required=True,
        help="approved source manifest SHA-256 from prepare-source-release",
    )
    systemd.add_argument(
        "--python-runtime",
        type=Path,
        required=True,
        help="immutable content-addressed runtime from prepare-python-runtime",
    )
    systemd.add_argument(
        "--expected-python-runtime-manifest-sha256",
        required=True,
        help="approved Python runtime manifest SHA-256",
    )
    systemd.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    systemd.add_argument(
        "--api-config", type=Path, default=PROJECT_ROOT / "llmapi.json"
    )
    systemd.add_argument(
        "--api-token-file",
        type=Path,
        default=Path("~/.config/where-papers-go/backend.token"),
    )
    systemd.add_argument(
        "--runtime-dir",
        type=Path,
        default=Path("~/.local/state/where-papers-go/current"),
    )
    systemd.add_argument(
        "--shared-state-dir",
        type=Path,
        default=Path("~/.local/state/where-papers-go/shared"),
    )
    systemd.add_argument("--output", type=Path, required=True)
    systemd.add_argument("--apply", action="store_true")
    systemd.set_defaults(handler=render_systemd)

    monitor_systemd = subparsers.add_parser(
        "render-monitor-systemd",
        help="render the fixed operations-monitor user service and timer",
    )
    monitor_systemd.add_argument("--source-release", type=Path, required=True)
    monitor_systemd.add_argument(
        "--expected-source-manifest-sha256", required=True
    )
    monitor_systemd.add_argument("--python-runtime", type=Path, required=True)
    monitor_systemd.add_argument(
        "--expected-python-runtime-manifest-sha256", required=True
    )
    monitor_systemd.add_argument("--runtime-dir", type=Path, required=True)
    monitor_systemd.add_argument(
        "--expected-runtime-manifest-sha256", required=True
    )
    monitor_systemd.add_argument("--api-token-file", type=Path, required=True)
    monitor_systemd.add_argument("--state-dir", type=Path, required=True)
    monitor_systemd.add_argument("--service-output", type=Path, required=True)
    monitor_systemd.add_argument("--timer-output", type=Path, required=True)
    monitor_systemd.add_argument("--apply", action="store_true")
    monitor_systemd.set_defaults(handler=render_monitor_systemd)

    nginx = subparsers.add_parser("render-nginx", help="render the TLS proxy")
    nginx.add_argument("--template", type=Path, default=NGINX_TEMPLATE)
    nginx.add_argument("--output", type=Path, required=True)
    nginx.add_argument("--server-name", required=True)
    nginx.add_argument("--tls-certificate", type=Path, required=True)
    nginx.add_argument("--tls-certificate-key", type=Path, required=True)
    nginx.add_argument("--htpasswd", type=Path, required=True)
    nginx.add_argument("--backend-api-token-file", type=Path, required=True)
    nginx.add_argument("--upstream-port", type=int, default=8001)
    nginx.add_argument(
        "--authenticated-gate-port",
        type=int,
        default=NGINX_AUTHENTICATED_GATE_PORT,
        help="loopback-only authenticated gate port (1024..65535; not 8765)",
    )
    nginx.add_argument(
        "--defer-privileged-input-validation",
        action="store_true",
        help=(
            "write a private non-root candidate without opening root-only TLS/"
            "htpasswd inputs; nginx -t and worker-readable checks are then mandatory"
        ),
    )
    nginx.add_argument("--apply", action="store_true")
    nginx.set_defaults(handler=render_nginx)

    environment = subparsers.add_parser(
        "render-env", help="render the non-secret service environment"
    )
    environment.add_argument("--output", type=Path, required=True)
    environment.add_argument("--host", default="127.0.0.1")
    environment.add_argument("--port", type=int, default=8001)
    environment.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    environment.add_argument(
        "--api-config", type=Path, default=PROJECT_ROOT / "llmapi.json"
    )
    environment.add_argument(
        "--runtime-dir",
        type=Path,
        default=Path("~/.local/state/where-papers-go/current"),
    )
    environment.add_argument(
        "--shared-state-dir",
        type=Path,
        default=Path("~/.local/state/where-papers-go/shared"),
    )
    environment.add_argument("--rate-limit-requests", type=int, default=6)
    environment.add_argument("--rate-limit-window-seconds", type=int, default=60)
    environment.add_argument("--max-concurrent-connections", type=int, default=64)
    environment.add_argument("--max-concurrent-searches", type=int, default=2)
    environment.add_argument("--request-body-limit", type=int, default=200_000)
    environment.add_argument("--request-read-timeout", type=int, default=30)
    environment.add_argument("--audit-log", action=argparse.BooleanOptionalAction, default=True)
    environment.add_argument(
        "--allowed-client-cidrs", default="127.0.0.0/8,::1/128"
    )
    environment.add_argument("--trust-proxy", action="store_true")
    environment.add_argument(
        "--trusted-proxy-cidrs", default="127.0.0.0/8,::1/128"
    )
    environment.add_argument("--require-api-auth", action="store_true")
    environment.add_argument("--api-token-file", type=Path)
    environment.add_argument("--apply", action="store_true")
    environment.set_defaults(handler=render_environment)

    source_release = subparsers.add_parser(
        "prepare-source-release",
        help="build the current Git commit/tree into an immutable source release",
    )
    source_release.add_argument(
        "--project-root", type=Path, default=PROJECT_ROOT
    )
    source_release.add_argument(
        "--release-root",
        type=Path,
        default=Path("~/.local/lib/where-papers-go"),
    )
    source_release.add_argument("--apply", action="store_true")
    source_release.set_defaults(handler=prepare_source_release)

    python_runtime_lock = subparsers.add_parser(
        "prepare-python-runtime-lock",
        help="freeze target-compatible wheel archives into an exclusive lock",
    )
    python_runtime_lock.add_argument(
        "--source-prefix",
        type=Path,
        required=True,
        help="self-contained CPython prefix whose ABI/platform is locked",
    )
    python_runtime_lock.add_argument(
        "--python-relative-path",
        type=Path,
        required=True,
        help="executable path relative to --source-prefix",
    )
    python_runtime_lock.add_argument(
        "--wheelhouse",
        type=Path,
        required=True,
        help="flat wheelhouse containing exactly one selected wheel per project",
    )
    python_runtime_lock.add_argument("--output", type=Path, required=True)
    python_runtime_lock.add_argument("--apply", action="store_true")
    python_runtime_lock.set_defaults(handler=prepare_python_runtime_lock)

    python_runtime = subparsers.add_parser(
        "prepare-python-runtime",
        help="publish a complete Python prefix as an immutable addressed runtime",
    )
    python_runtime.add_argument(
        "--source-prefix",
        type=Path,
        required=True,
        help="symlink-free, self-contained CPython installation prefix",
    )
    python_runtime.add_argument(
        "--python-relative-path",
        type=Path,
        required=True,
        help="executable path relative to --source-prefix (for example bin/python3)",
    )
    python_runtime.add_argument(
        "--dependency-lock",
        type=Path,
        default=PRODUCTION_PYTHON_RUNTIME_LOCK,
        help=(
            "immutable selected-wheel JSON (not uv.lock); defaults to the tracked "
            "CPython 3.14.5 linux-x86_64 production lock"
        ),
    )
    python_runtime.add_argument(
        "--wheelhouse",
        type=Path,
        required=True,
        help="flat offline wheelhouse containing only locked .whl files",
    )
    python_runtime.add_argument(
        "--release-root",
        type=Path,
        default=Path("~/.local/lib/where-papers-go"),
    )
    python_runtime.add_argument("--apply", action="store_true")
    python_runtime.set_defaults(handler=prepare_python_runtime)

    runtime = subparsers.add_parser(
        "prepare-runtime",
        help="clone protected retrieval/cache inputs into a private generation",
    )
    runtime.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    runtime.add_argument(
        "--runtime-root",
        type=Path,
        default=Path("~/.local/state/where-papers-go"),
    )
    runtime.add_argument("--apply", action="store_true")
    runtime.set_defaults(handler=prepare_runtime)

    shared = subparsers.add_parser(
        "prepare-shared-state",
        help="migrate/initialize persistent quota state while the old service is stopped",
    )
    shared.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    shared.add_argument(
        "--runtime-root",
        type=Path,
        default=Path("~/.local/state/where-papers-go"),
    )
    shared.add_argument(
        "--shared-state-dir",
        type=Path,
        help="persistent quota directory (default: RUNTIME_ROOT/shared)",
    )
    shared.add_argument(
        "--api-config", type=Path, default=PROJECT_ROOT / "llmapi.json"
    )
    shared.add_argument("--apply", action="store_true")
    shared.set_defaults(handler=prepare_shared_state)

    activate = subparsers.add_parser(
        "activate-runtime",
        help="CAS-activate a separately health-checked runtime generation",
    )
    activate.add_argument("--generation", type=Path, required=True)
    activate.add_argument(
        "--runtime-root",
        type=Path,
        default=Path("~/.local/state/where-papers-go"),
    )
    activate.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    activate.add_argument(
        "--expected-manifest-sha256",
        required=True,
        type=str,
        help="64-hex hash printed by prepare-runtime",
    )
    activate.add_argument(
        "--expected-current",
        required=True,
        help="exact observed_current printed by prepare-runtime, or 'none'",
    )
    activate.add_argument("--apply", action="store_true")
    activate.set_defaults(handler=activate_runtime)

    restore = subparsers.add_parser(
        "restore", help="atomically restore a preserved unit/proxy backup"
    )
    restore.add_argument("--source", type=Path, required=True)
    restore.add_argument("--output", type=Path, required=True)
    restore.add_argument("--mode", type=lambda value: int(value, 8), default=0o644)
    restore.add_argument("--apply", action="store_true")
    restore.set_defaults(handler=restore_file)

    health = subparsers.add_parser("health", help="verify ready health and hashes")
    health.add_argument("--url", default="http://127.0.0.1:8001/api/health")
    health.add_argument("--token-file", type=Path)
    health.add_argument("--attempts", type=int, default=1)
    health.add_argument("--interval", type=float, default=1.0)
    health.add_argument("--timeout", type=float, default=5.0)
    health.add_argument(
        "--expect-process-pid",
        type=int,
        help="require the health response to come from this live PID/start tuple",
    )
    health.add_argument("--expect-sha256", action="append", default=[])
    health.set_defaults(handler=health_check)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "attempts", 1) < 1:
        parser.error("--attempts must be positive")
    if getattr(args, "interval", 1.0) < 0 or getattr(args, "timeout", 1.0) <= 0:
        parser.error("--interval/--timeout must be non-negative/positive")
    if not 1 <= getattr(args, "upstream_port", 1) <= 65_535:
        parser.error("--upstream-port must be between 1 and 65535")
    if not 1024 <= getattr(args, "authenticated_gate_port", 1024) <= 65_535:
        parser.error("--authenticated-gate-port must be between 1024 and 65535")
    if not 1 <= getattr(args, "port", 1) <= 65_535:
        parser.error("--port must be between 1 and 65535")
    expected_process_pid = getattr(args, "expect_process_pid", None)
    if expected_process_pid is not None and expected_process_pid <= 0:
        parser.error("--expect-process-pid must be positive")
    for name in (
        "rate_limit_requests",
        "rate_limit_window_seconds",
        "max_concurrent_connections",
        "max_concurrent_searches",
        "request_body_limit",
        "request_read_timeout",
    ):
        if getattr(args, name, 1) < 1:
            parser.error("--" + name.replace("_", "-") + " must be positive")
    for attribute, flag in (
        ("expected_manifest_sha256", "--expected-manifest-sha256"),
        (
            "expected_source_manifest_sha256",
            "--expected-source-manifest-sha256",
        ),
        (
            "expected_python_runtime_manifest_sha256",
            "--expected-python-runtime-manifest-sha256",
        ),
        (
            "expected_runtime_manifest_sha256",
            "--expected-runtime-manifest-sha256",
        ),
    ):
        expected_manifest = getattr(args, attribute, None)
        if expected_manifest is not None and (
            len(expected_manifest) != 64
            or any(
                character not in "0123456789abcdefABCDEF"
                for character in expected_manifest
            )
        ):
            parser.error(f"{flag} must be 64 hexadecimal characters")
    try:
        result = args.handler(args)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
