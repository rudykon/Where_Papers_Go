"""Fail-closed identity checks for an immutable source release.

The service is started from a content-addressed directory produced by
``scripts.manage_deployment prepare-source-release``.  This module deliberately
does not consult Git at runtime: the read-only manifest, its externally pinned
digest, and every regular file in the release are the complete trust input.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import threading
from typing import Any, Mapping


SOURCE_HEAD_ENV = "WPG_SOURCE_HEAD"
SOURCE_TREE_ENV = "WPG_SOURCE_TREE"
SOURCE_MANIFEST_ENV = "WPG_SOURCE_MANIFEST"
SOURCE_MANIFEST_SHA256_ENV = "WPG_SOURCE_MANIFEST_SHA256"
SOURCE_MANIFEST_FILE = "source-release-manifest.json"
SOURCE_MANIFEST_MAX_BYTES = 16 * 1024 * 1024
SOURCE_ARTIFACT_TYPE = "where_papers_go_source_release"
FORBIDDEN_SOURCE_COMPONENTS = frozenset({".git", "__pycache__"})
PYTHON_RUNTIME_ENV = "WPG_PYTHON_RUNTIME"
PYTHON_RUNTIME_MANIFEST_ENV = "WPG_PYTHON_RUNTIME_MANIFEST"
PYTHON_RUNTIME_MANIFEST_SHA256_ENV = "WPG_PYTHON_RUNTIME_MANIFEST_SHA256"
PYTHON_RUNTIME_TREE_SHA256_ENV = "WPG_PYTHON_RUNTIME_TREE_SHA256"
PYTHON_RUNTIME_MANIFEST_FILE = "python-runtime-manifest.json"

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_GIT_OBJECT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_CACHE_LOCK = threading.RLock()
_CACHED_BINDING: tuple[str, str, str, str] | None = None
_CACHED_IDENTITY: dict[str, Any] | None = None
_CACHED_RELEASE_STAMP: tuple[tuple[Any, ...], ...] | None = None
_CACHED_PYTHON_RUNTIME_BINDING: tuple[str, str, str, str] | None = None
_CACHED_PYTHON_RUNTIME_IDENTITY: dict[str, Any] | None = None
_CACHED_PYTHON_RUNTIME_STAMP: tuple[tuple[Any, ...], ...] | None = None
_CACHED_PYTHON_SYSTEM_ABI_STAMP: tuple[tuple[Any, ...], ...] | None = None


@dataclass(frozen=True)
class ProcessExecutableStamp:
    """Health-safe, immutable identity for one live Linux process."""

    pid: int
    start_ticks: int
    executable_sha256: str


@dataclass(frozen=True)
class ProcessIdentitySnapshot:
    """Private, complete identity for one live child process.

    Unlike :class:`ProcessExecutableStamp`, this object is deliberately not
    serialized into health output: command, cwd and environment bindings can
    contain deployment paths.  The parent caches it and compares a newly read
    snapshot at every trust boundary.
    """

    process: ProcessExecutableStamp
    parent_pid: int
    command_line: tuple[str, ...]
    working_directory: str
    working_directory_identity: tuple[int, ...]
    selected_environment: tuple[tuple[str, str], ...]
    environment_sha256: str
    forbidden_environment_clear: bool


class SourceIdentityError(ValueError):
    """Raised when a release cannot prove its configured source identity."""


def atomic_rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically publish ``source`` without ever replacing ``destination``.

    The production contract is Linux-only and deliberately fails closed when
    ``renameat2(RENAME_NOREPLACE)`` is unavailable.  A preceding ``exists``
    check is not a substitute because another process can create the final
    name between that check and a conventional ``rename``.
    """

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2(RENAME_NOREPLACE) is unavailable")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    source_raw = os.fsencode(os.fspath(source))
    destination_raw = os.fsencode(os.fspath(destination))
    if renameat2(-100, source_raw, -100, destination_raw, 1) != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            os.fspath(source),
            os.fspath(destination),
        )


def _stable_regular_bytes(
    path: Path,
    *,
    expected_mode: int,
    max_bytes: int | None = None,
) -> bytes:
    """Read one owned, unlinked regular file through a stable no-follow fd."""

    try:
        path_info = path.lstat()
    except OSError as exc:
        raise SourceIdentityError("source release file is unavailable") from exc
    if (
        stat.S_ISLNK(path_info.st_mode)
        or not stat.S_ISREG(path_info.st_mode)
        or path_info.st_uid != os.geteuid()
        or path_info.st_nlink != 1
        or stat.S_IMODE(path_info.st_mode) != expected_mode
        or (max_bytes is not None and path_info.st_size > max_bytes)
    ):
        raise SourceIdentityError("source release file identity or mode is unsafe")

    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(descriptor)
        path_identity = (
            path_info.st_dev,
            path_info.st_ino,
            path_info.st_size,
            path_info.st_mtime_ns,
            path_info.st_ctime_ns,
        )
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != expected_mode
            or path_identity != before_identity
        ):
            raise SourceIdentityError("source release file changed before verification")
        digest_input: list[bytes] = []
        size = 0
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest_input.append(block)
            size += len(block)
            if max_bytes is not None and size > max_bytes:
                raise SourceIdentityError("source release file exceeds its size bound")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if before_identity != after_identity or size != before.st_size:
        raise SourceIdentityError("source release file changed during verification")
    return b"".join(digest_input)


def _validated_relative_path(raw_value: Any) -> PurePosixPath:
    if not isinstance(raw_value, str) or not raw_value or "\\" in raw_value:
        raise SourceIdentityError("source release manifest contains an unsafe path")
    relative = PurePosixPath(raw_value)
    if (
        relative.is_absolute()
        or relative.as_posix() != raw_value
        or any(part in {"", ".", ".."} for part in relative.parts)
        or any(part in FORBIDDEN_SOURCE_COMPONENTS for part in relative.parts)
        or any(part == SOURCE_MANIFEST_FILE for part in relative.parts)
        or relative.suffix.casefold() in {".pyc", ".pyo"}
    ):
        raise SourceIdentityError("source release manifest contains an unsafe path")
    return relative


def _directory_identity(path: Path) -> tuple[int, int, int, int, int]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise SourceIdentityError("source release directory is unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o222
    ):
        raise SourceIdentityError("source release directory is not owned/read-only")
    return (
        info.st_dev,
        info.st_ino,
        info.st_mtime_ns,
        info.st_ctime_ns,
        stat.S_IMODE(info.st_mode),
    )


def _release_stat_stamp(release: Path) -> tuple[tuple[Any, ...], ...]:
    """Capture every namespace/inode attribute that can precede content drift."""

    rows: list[tuple[Any, ...]] = []

    def walk_error(error: OSError) -> None:
        raise SourceIdentityError("source release directory cannot be traversed") from error

    for directory, directory_names, file_names in os.walk(
        release, followlinks=False, onerror=walk_error
    ):
        directory_path = Path(directory)
        directory_info = directory_path.lstat()
        if (
            stat.S_ISLNK(directory_info.st_mode)
            or not stat.S_ISDIR(directory_info.st_mode)
            or directory_info.st_uid != os.geteuid()
            or stat.S_IMODE(directory_info.st_mode) & 0o222
        ):
            raise SourceIdentityError("source release directory is not owned/read-only")
        relative_directory = directory_path.relative_to(release).as_posix()
        rows.append(
            (
                "directory",
                relative_directory,
                directory_info.st_dev,
                directory_info.st_ino,
                stat.S_IMODE(directory_info.st_mode),
                directory_info.st_mtime_ns,
                directory_info.st_ctime_ns,
            )
        )
        for name in directory_names:
            candidate = directory_path / name
            _directory_identity(candidate)
            _validated_relative_path(candidate.relative_to(release).as_posix())
        for name in file_names:
            candidate = directory_path / name
            relative = candidate.relative_to(release).as_posix()
            if relative != SOURCE_MANIFEST_FILE:
                _validated_relative_path(relative)
            info = candidate.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & 0o222
            ):
                raise SourceIdentityError("source release file identity or mode is unsafe")
            rows.append(
                (
                    "file",
                    relative,
                    info.st_dev,
                    info.st_ino,
                    stat.S_IMODE(info.st_mode),
                    info.st_nlink,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )
            )
    return tuple(sorted(rows))


def _python_runtime_stat_stamp(runtime: Path) -> tuple[tuple[Any, ...], ...]:
    """Capture the complete immutable runtime namespace without rehashing it.

    The first observation is always preceded by the deployment validator's
    full file-tree hash verification.  Later health calls compare inode,
    namespace, mode, size, mtime, and ctime for every entry.  Because the
    published tree is owner-read-only, any chmod, replacement, addition, or
    removal changes at least one captured ctime even if an mtime is restored.
    """

    rows: list[tuple[Any, ...]] = []

    def walk_error(error: OSError) -> None:
        raise SourceIdentityError("Python runtime directory cannot be traversed") from error

    for directory, directory_names, file_names in os.walk(
        runtime, followlinks=False, onerror=walk_error
    ):
        directory_path = Path(directory)
        try:
            directory_info = directory_path.lstat()
        except OSError as exc:
            raise SourceIdentityError("Python runtime directory is unavailable") from exc
        if (
            stat.S_ISLNK(directory_info.st_mode)
            or not stat.S_ISDIR(directory_info.st_mode)
            or directory_info.st_uid != os.geteuid()
            or stat.S_IMODE(directory_info.st_mode) != 0o555
        ):
            raise SourceIdentityError("Python runtime directory identity is unsafe")
        relative_directory = directory_path.relative_to(runtime).as_posix()
        rows.append(
            (
                "directory",
                relative_directory,
                directory_info.st_dev,
                directory_info.st_ino,
                stat.S_IMODE(directory_info.st_mode),
                directory_info.st_mtime_ns,
                directory_info.st_ctime_ns,
            )
        )
        for name in directory_names:
            candidate = directory_path / name
            if candidate.is_symlink():
                raise SourceIdentityError("Python runtime contains a symlink")
        for name in file_names:
            candidate = directory_path / name
            try:
                info = candidate.lstat()
            except OSError as exc:
                raise SourceIdentityError("Python runtime file is unavailable") from exc
            mode = stat.S_IMODE(info.st_mode)
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or mode not in {0o400, 0o444, 0o555}
                or (mode == 0o400 and candidate.name != PYTHON_RUNTIME_MANIFEST_FILE)
            ):
                raise SourceIdentityError("Python runtime file identity is unsafe")
            rows.append(
                (
                    "file",
                    candidate.relative_to(runtime).as_posix(),
                    info.st_dev,
                    info.st_ino,
                    mode,
                    info.st_nlink,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )
            )
    return tuple(sorted(rows))


def validate_source_release(
    manifest_path: Path,
    *,
    expected_head: str | None = None,
    expected_tree: str | None = None,
    expected_manifest_sha256: str | None = None,
    require_content_addressed_name: bool = True,
) -> dict[str, Any]:
    """Verify one complete immutable release or raise ``SourceIdentityError``."""

    configured = manifest_path.expanduser()
    if not configured.is_absolute() or configured.name != SOURCE_MANIFEST_FILE:
        raise SourceIdentityError("source manifest path is not absolute/canonical")
    try:
        release = configured.parent.resolve(strict=True)
    except OSError as exc:
        raise SourceIdentityError("source release is unavailable") from exc
    manifest_path = release / SOURCE_MANIFEST_FILE
    release_before = _directory_identity(release)
    raw = _stable_regular_bytes(
        manifest_path,
        expected_mode=0o400,
        max_bytes=SOURCE_MANIFEST_MAX_BYTES,
    )
    manifest_sha256 = hashlib.sha256(raw).hexdigest()
    if (
        expected_manifest_sha256 is not None
        and manifest_sha256 != expected_manifest_sha256
    ):
        raise SourceIdentityError("source manifest SHA-256 differs")
    if (
        require_content_addressed_name
        and release.name != f"release-{manifest_sha256}"
    ):
        raise SourceIdentityError("source release directory is not content-addressed")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SourceIdentityError("source manifest is not valid JSON") from exc
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema_version") != 1
        or payload.get("artifact_type") != SOURCE_ARTIFACT_TYPE
        or payload.get("immutable_files") is not True
        or payload.get("forbidden_entries_excluded") is not True
        or not isinstance(payload.get("files"), list)
    ):
        raise SourceIdentityError("source manifest contract is invalid")
    head = payload.get("source_head")
    tree = payload.get("source_tree")
    if not isinstance(head, str) or _GIT_OBJECT_RE.fullmatch(head) is None:
        raise SourceIdentityError("source manifest commit identity is invalid")
    if not isinstance(tree, str) or _GIT_OBJECT_RE.fullmatch(tree) is None:
        raise SourceIdentityError("source manifest tree identity is invalid")
    if expected_head is not None and head != expected_head:
        raise SourceIdentityError("source manifest commit differs")
    if expected_tree is not None and tree != expected_tree:
        raise SourceIdentityError("source manifest tree differs")

    rows: dict[str, Mapping[str, Any]] = {}
    for raw_row in payload["files"]:
        if not isinstance(raw_row, Mapping):
            raise SourceIdentityError("source manifest file binding is invalid")
        relative = _validated_relative_path(raw_row.get("path"))
        name = relative.as_posix()
        size = raw_row.get("bytes")
        digest = raw_row.get("sha256")
        mode = raw_row.get("mode")
        if (
            name in rows
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
            or mode not in {"0444", "0555"}
        ):
            raise SourceIdentityError("source manifest file binding is invalid")
        rows[name] = raw_row
    if payload.get("file_count") != len(rows):
        raise SourceIdentityError("source manifest file count differs")

    binding_payload = json.dumps(
        {"source_head": head, "source_tree": tree, "files": list(payload["files"])},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    binding_sha256 = hashlib.sha256(binding_payload).hexdigest()
    if payload.get("source_binding_sha256") != binding_sha256:
        raise SourceIdentityError("source manifest binding digest differs")

    expected_directories = {
        parent.as_posix()
        for name in rows
        for parent in PurePosixPath(name).parents
        if parent.as_posix() != "."
    }
    actual_directories: set[str] = set()
    actual_files: set[str] = set()

    def walk_error(error: OSError) -> None:
        raise SourceIdentityError("source release directory cannot be traversed") from error

    for directory, directory_names, file_names in os.walk(
        release, followlinks=False, onerror=walk_error
    ):
        directory_path = Path(directory)
        _directory_identity(directory_path)
        for name in directory_names:
            candidate = directory_path / name
            _directory_identity(candidate)
            relative = candidate.relative_to(release).as_posix()
            _validated_relative_path(relative)
            actual_directories.add(relative)
        for name in file_names:
            candidate = directory_path / name
            relative = candidate.relative_to(release).as_posix()
            if relative == SOURCE_MANIFEST_FILE:
                continue
            _validated_relative_path(relative)
            actual_files.add(relative)
    if actual_files != set(rows) or actual_directories != expected_directories:
        raise SourceIdentityError("source release tree differs from its manifest")

    for relative, row in rows.items():
        expected_mode = int(str(row["mode"]), 8)
        content = _stable_regular_bytes(
            release / relative,
            expected_mode=expected_mode,
        )
        if len(content) != row["bytes"]:
            raise SourceIdentityError("source release file size differs")
        if hashlib.sha256(content).hexdigest() != row["sha256"]:
            raise SourceIdentityError("source release file SHA-256 differs")
    if _directory_identity(release) != release_before:
        raise SourceIdentityError("source release directory changed during verification")
    return {
        "ready": True,
        "head": head,
        "tree": tree,
        "manifest_sha256": manifest_sha256,
        "source_binding_sha256": binding_sha256,
        "file_count": len(rows),
        "files_verified": True,
        "release": str(release),
    }


def process_start_ticks(pid: int | None = None) -> int:
    """Return Linux `/proc` field 22 for one process (the caller by default)."""

    selected_pid = os.getpid() if pid is None else pid
    if isinstance(selected_pid, bool) or not isinstance(selected_pid, int) or selected_pid <= 0:
        raise SourceIdentityError("process identity is invalid")
    try:
        raw = Path(f"/proc/{selected_pid}/stat").read_text(encoding="ascii")
        closing_parenthesis = raw.rfind(")")
        fields = raw[closing_parenthesis + 2 :].split()
        value = int(fields[19])
    except (OSError, UnicodeError, ValueError, IndexError) as exc:
        raise SourceIdentityError("process start identity is unavailable") from exc
    if value <= 0:
        raise SourceIdentityError("process start identity is invalid")
    return value


def _process_target_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def process_executable_stamp(
    pid: int,
    *,
    expected_executable: Path | None = None,
    expected_sha256: str | None = None,
) -> ProcessExecutableStamp:
    """Capture one stable ``/proc/<pid>/exe`` identity without exposing its path.

    The start time is sampled around the executable read, and the proc link,
    target inode, and bytes must remain stable throughout.  Callers may bind
    both the canonical executable path and its already trusted digest.
    """

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise SourceIdentityError("process identity is invalid")
    if expected_sha256 is not None:
        expected_sha256 = expected_sha256.strip().casefold()
        if _SHA256_RE.fullmatch(expected_sha256) is None:
            raise SourceIdentityError("expected executable SHA-256 is invalid")
    expected_path: Path | None = None
    if expected_executable is not None:
        try:
            expected_path = expected_executable.expanduser().resolve(strict=True)
        except OSError as exc:
            raise SourceIdentityError("expected executable is unavailable") from exc

    proc_executable = Path(f"/proc/{pid}/exe")
    try:
        start_before = process_start_ticks(pid)
        link_before = os.readlink(proc_executable)
        if link_before.endswith(" (deleted)"):
            raise SourceIdentityError("process executable was deleted")
        target = Path(link_before)
        if not target.is_absolute() or target.resolve(strict=True) != target:
            raise SourceIdentityError("process executable path is not canonical")
        if expected_path is not None and target != expected_path:
            raise SourceIdentityError("process executable path differs")
        target_before = target.lstat()
        descriptor = os.open(
            proc_executable,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            opened_before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(target_before.st_mode)
                or not stat.S_ISREG(opened_before.st_mode)
                or _process_target_identity(target_before)
                != _process_target_identity(opened_before)
            ):
                raise SourceIdentityError("process executable inode differs")
            digest = hashlib.sha256()
            observed_bytes = 0
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                observed_bytes += len(block)
                if observed_bytes > 256 * 1024 * 1024:
                    raise SourceIdentityError("process executable exceeds its bound")
                digest.update(block)
            opened_after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        target_after = target.lstat()
        link_after = os.readlink(proc_executable)
        start_after = process_start_ticks(pid)
    except (OSError, UnicodeError) as exc:
        raise SourceIdentityError("process executable identity is unavailable") from exc
    if (
        start_before != start_after
        or link_before != link_after
        or observed_bytes != opened_before.st_size
        or _process_target_identity(opened_before)
        != _process_target_identity(opened_after)
        or _process_target_identity(opened_before)
        != _process_target_identity(target_after)
    ):
        raise SourceIdentityError("process executable changed during verification")
    executable_sha256 = digest.hexdigest()
    if expected_sha256 is not None and executable_sha256 != expected_sha256:
        raise SourceIdentityError("process executable SHA-256 differs")
    return ProcessExecutableStamp(
        pid=pid,
        start_ticks=start_after,
        executable_sha256=executable_sha256,
    )


def process_executable_stamp_payload(
    stamp: ProcessExecutableStamp,
) -> dict[str, Any]:
    """Return the deliberately path-free JSON representation of a stamp."""

    return {
        "pid": stamp.pid,
        "start_ticks": stamp.start_ticks,
        "executable_sha256": stamp.executable_sha256,
        "proc_exe_verified": True,
    }


def _process_executable_stamp_at(
    proc_descriptor: int,
    *,
    pid: int,
    start_ticks: int,
    expected_executable: Path,
    expected_sha256: str,
) -> ProcessExecutableStamp:
    """Hash ``exe`` through an already opened, start-bound PID directory."""

    expected_digest = expected_sha256.strip().casefold()
    if _SHA256_RE.fullmatch(expected_digest) is None:
        raise SourceIdentityError("expected executable SHA-256 is invalid")
    link_before = os.readlink("exe", dir_fd=proc_descriptor)
    if link_before.endswith(" (deleted)"):
        raise SourceIdentityError("process executable was deleted")
    target = Path(link_before)
    try:
        target_resolved = target.resolve(strict=True)
    except OSError as exc:
        raise SourceIdentityError("process executable target is unavailable") from exc
    if (
        not target.is_absolute()
        or target_resolved != target
        or target != expected_executable
    ):
        raise SourceIdentityError("process executable path differs")

    target_before = target.lstat()
    descriptor = os.open(
        "exe",
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        dir_fd=proc_descriptor,
    )
    try:
        opened_before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(target_before.st_mode)
            or not stat.S_ISREG(opened_before.st_mode)
            or _process_target_identity(target_before)
            != _process_target_identity(opened_before)
        ):
            raise SourceIdentityError("process executable inode differs")
        digest = hashlib.sha256()
        observed_bytes = 0
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            observed_bytes += len(block)
            if observed_bytes > 256 * 1024 * 1024:
                raise SourceIdentityError("process executable exceeds its bound")
            digest.update(block)
        opened_after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    target_after = target.lstat()
    link_after = os.readlink("exe", dir_fd=proc_descriptor)
    if (
        link_before != link_after
        or observed_bytes != opened_before.st_size
        or _process_target_identity(opened_before)
        != _process_target_identity(opened_after)
        or _process_target_identity(opened_before)
        != _process_target_identity(target_after)
    ):
        raise SourceIdentityError("process executable changed during verification")
    executable_sha256 = digest.hexdigest()
    if executable_sha256 != expected_digest:
        raise SourceIdentityError("process executable SHA-256 differs")
    return ProcessExecutableStamp(
        pid=pid,
        start_ticks=start_ticks,
        executable_sha256=executable_sha256,
    )


def _bounded_proc_bytes(
    proc_descriptor: int,
    name: str,
    *,
    maximum_bytes: int,
) -> bytes:
    """Read one proc pseudo-file through the already opened PID directory."""

    descriptor = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=proc_descriptor,
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SourceIdentityError("process proc entry is not regular")
        chunks: list[bytes] = []
        observed = 0
        while True:
            block = os.read(descriptor, min(64 * 1024, maximum_bytes + 1))
            if not block:
                break
            observed += len(block)
            if observed > maximum_bytes:
                raise SourceIdentityError("process proc entry exceeds its bound")
            chunks.append(block)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if _process_target_identity(before) != _process_target_identity(after):
        raise SourceIdentityError("process proc entry changed during inspection")
    return b"".join(chunks)


def _proc_stat_identity(raw: bytes, *, expected_pid: int) -> tuple[int, int]:
    """Return ``(PPid, start_ticks)`` from one bounded Linux proc stat row."""

    try:
        opening_parenthesis = raw.find(b"(")
        closing_parenthesis = raw.rfind(b")")
        if (
            opening_parenthesis <= 0
            or closing_parenthesis <= opening_parenthesis
            or int(raw[:opening_parenthesis].strip()) != expected_pid
        ):
            raise ValueError
        fields = raw[closing_parenthesis + 2 :].split()
        parent_pid = int(fields[1])
        start_ticks = int(fields[19])
    except (UnicodeError, ValueError, IndexError) as exc:
        raise SourceIdentityError("process stat identity is invalid") from exc
    if parent_pid <= 0 or start_ticks <= 0:
        raise SourceIdentityError("process stat identity is invalid")
    return parent_pid, start_ticks


def _proc_string_vector(
    raw: bytes,
    *,
    context: str,
) -> tuple[str, ...]:
    """Decode a NUL-terminated proc cmdline without dropping empty entries."""

    if not raw or not raw.endswith(b"\0"):
        raise SourceIdentityError(f"process {context} is not NUL-terminated")
    values = raw[:-1].split(b"\0")
    if not values or any(not value for value in values):
        raise SourceIdentityError(f"process {context} is invalid")
    try:
        return tuple(value.decode("utf-8", errors="strict") for value in values)
    except UnicodeError as exc:
        raise SourceIdentityError(f"process {context} is not valid UTF-8") from exc


def _proc_environment(
    raw: bytes,
    *,
    selected_names: frozenset[str],
) -> tuple[dict[str, str], frozenset[str]]:
    """Parse selected values while retaining every name for injection checks."""

    if raw and not raw.endswith(b"\0"):
        raise SourceIdentityError("process environment is not NUL-terminated")
    selected: dict[str, str] = {}
    observed_names: set[str] = set()
    for item in raw.split(b"\0"):
        if not item:
            continue
        raw_name, separator, raw_value = item.partition(b"=")
        if not separator:
            raise SourceIdentityError("process environment entry is invalid")
        try:
            name = raw_name.decode("ascii", errors="strict")
        except UnicodeError as exc:
            raise SourceIdentityError("process environment name is invalid") from exc
        if (
            not name
            or name in observed_names
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
        ):
            raise SourceIdentityError("process environment name is invalid")
        observed_names.add(name)
        if name in selected_names:
            try:
                selected[name] = raw_value.decode("utf-8", errors="strict")
            except UnicodeError as exc:
                raise SourceIdentityError(
                    "selected process environment is not valid UTF-8"
                ) from exc
    return selected, frozenset(observed_names)


def process_identity_snapshot(
    pid: int,
    *,
    expected_parent_pid: int,
    expected_executable: Path,
    expected_sha256: str,
    expected_command_line: tuple[str, ...],
    expected_working_directory: Path,
    expected_environment: Mapping[str, str],
    forbidden_environment_names: frozenset[str],
) -> ProcessIdentitySnapshot:
    """Capture and validate a race-resistant live child process snapshot.

    The PID directory is opened once.  Stat/PPid, cmdline, cwd and environ are
    sampled on both sides of the executable inode/hash proof.  The selected
    source/runtime/offline environment must match exactly; explicit injection
    names, every ``LD_*`` name, and any unselected ``PYTHON*`` name are rejected.
    """

    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or isinstance(expected_parent_pid, bool)
        or not isinstance(expected_parent_pid, int)
        or expected_parent_pid <= 0
        or not expected_command_line
        or any(not isinstance(value, str) or not value for value in expected_command_line)
        or any(
            not isinstance(name, str)
            or not name
            or not isinstance(value, str)
            for name, value in expected_environment.items()
        )
    ):
        raise SourceIdentityError("expected process identity is invalid")
    try:
        executable = expected_executable.expanduser().resolve(strict=True)
        working_directory = expected_working_directory.expanduser().resolve(strict=True)
    except OSError as exc:
        raise SourceIdentityError("expected process path is unavailable") from exc
    if expected_command_line[0] != str(executable):
        raise SourceIdentityError("expected process command does not bind executable")

    selected_names = frozenset(expected_environment)
    proc_path = Path("/proc") / str(pid)
    proc_descriptor = os.open(
        proc_path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        proc_before = os.fstat(proc_descriptor)
        stat_before = _bounded_proc_bytes(
            proc_descriptor, "stat", maximum_bytes=64 * 1024
        )
        parent_before, start_before = _proc_stat_identity(
            stat_before, expected_pid=pid
        )
        cwd_link_before = os.readlink("cwd", dir_fd=proc_descriptor)
        cwd_before = Path(cwd_link_before)
        cwd_info_before = os.stat(
            "cwd", dir_fd=proc_descriptor, follow_symlinks=True
        )
        command_raw_before = _bounded_proc_bytes(
            proc_descriptor, "cmdline", maximum_bytes=1024 * 1024
        )
        environment_raw_before = _bounded_proc_bytes(
            proc_descriptor, "environ", maximum_bytes=4 * 1024 * 1024
        )

        process = _process_executable_stamp_at(
            proc_descriptor,
            pid=pid,
            start_ticks=start_before,
            expected_executable=executable,
            expected_sha256=expected_sha256,
        )

        environment_raw_after = _bounded_proc_bytes(
            proc_descriptor, "environ", maximum_bytes=4 * 1024 * 1024
        )
        command_raw_after = _bounded_proc_bytes(
            proc_descriptor, "cmdline", maximum_bytes=1024 * 1024
        )
        cwd_link_after = os.readlink("cwd", dir_fd=proc_descriptor)
        cwd_info_after = os.stat(
            "cwd", dir_fd=proc_descriptor, follow_symlinks=True
        )
        stat_after = _bounded_proc_bytes(
            proc_descriptor, "stat", maximum_bytes=64 * 1024
        )
        parent_after, start_after = _proc_stat_identity(stat_after, expected_pid=pid)
        proc_after = os.fstat(proc_descriptor)
    except (OSError, UnicodeError) as exc:
        raise SourceIdentityError("live process identity is unavailable") from exc
    finally:
        os.close(proc_descriptor)

    if (
        _process_target_identity(proc_before) != _process_target_identity(proc_after)
        or parent_before != parent_after
        or start_before != start_after
        or process.pid != pid
        or process.start_ticks != start_after
        or cwd_link_before != cwd_link_after
        or _process_target_identity(cwd_info_before)
        != _process_target_identity(cwd_info_after)
        or command_raw_before != command_raw_after
        or environment_raw_before != environment_raw_after
    ):
        raise SourceIdentityError("live process identity changed during inspection")
    if parent_after != expected_parent_pid:
        raise SourceIdentityError("live process parent differs")
    if cwd_link_after.endswith(" (deleted)") or not cwd_before.is_absolute():
        raise SourceIdentityError("live process working directory is unsafe")
    try:
        observed_working_directory = cwd_before.resolve(strict=True)
        expected_cwd_info = working_directory.lstat()
    except OSError as exc:
        raise SourceIdentityError("live process working directory is unavailable") from exc
    if (
        observed_working_directory != working_directory
        or _process_target_identity(cwd_info_after)
        != _process_target_identity(expected_cwd_info)
    ):
        raise SourceIdentityError("live process working directory differs")

    command_line = _proc_string_vector(command_raw_after, context="command line")
    if command_line != expected_command_line:
        raise SourceIdentityError("live process command line differs")
    selected_environment, observed_names = _proc_environment(
        environment_raw_after,
        selected_names=selected_names,
    )
    if selected_environment != dict(expected_environment):
        raise SourceIdentityError("selected live process environment differs")
    if any(
        name in forbidden_environment_names
        or name.startswith("LD_")
        or (name.startswith("PYTHON") and name not in selected_names)
        for name in observed_names
    ):
        raise SourceIdentityError("live process environment contains injection input")

    return ProcessIdentitySnapshot(
        process=process,
        parent_pid=parent_after,
        command_line=command_line,
        working_directory=str(observed_working_directory),
        working_directory_identity=_process_target_identity(cwd_info_after),
        selected_environment=tuple(sorted(selected_environment.items())),
        environment_sha256=hashlib.sha256(environment_raw_after).hexdigest(),
        forbidden_environment_clear=True,
    )


def _system_abi_stat_stamp(raw_manifest: bytes) -> tuple[tuple[Any, ...], ...]:
    """Capture system ABI metadata after the manifest's full ELF validation."""

    try:
        document = json.loads(raw_manifest.decode("utf-8"))
        elf_audit = document["elf_audit"]
        raw_directories = elf_audit["system_directories"]
        raw_libraries = elf_audit["system_libraries"]
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceIdentityError("Python runtime system ABI binding is invalid") from exc
    if not isinstance(raw_directories, list) or not isinstance(raw_libraries, list):
        raise SourceIdentityError("Python runtime system ABI inventory is invalid")

    rows: list[tuple[Any, ...]] = []
    seen: set[str] = set()
    for kind, raw_rows in (
        ("directory", raw_directories),
        ("library", raw_libraries),
    ):
        for raw_row in raw_rows:
            if not isinstance(raw_row, Mapping):
                raise SourceIdentityError("Python runtime system ABI row is invalid")
            raw_path = raw_row.get("path")
            raw_mode = raw_row.get("mode")
            if (
                not isinstance(raw_path, str)
                or not raw_path.startswith("/")
                or raw_path in seen
                or not isinstance(raw_mode, str)
                or re.fullmatch(r"0[0-7]{3}", raw_mode) is None
                or raw_row.get("owner_uid") != 0
            ):
                raise SourceIdentityError("Python runtime system ABI row is invalid")
            seen.add(raw_path)
            path = Path(raw_path)
            try:
                resolved = path.resolve(strict=True)
                info = path.lstat()
            except OSError as exc:
                raise SourceIdentityError("Python runtime system ABI path is unavailable") from exc
            expected_directory = kind == "directory"
            if (
                resolved != path
                or stat.S_ISLNK(info.st_mode)
                or (expected_directory and not stat.S_ISDIR(info.st_mode))
                or (not expected_directory and not stat.S_ISREG(info.st_mode))
                or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != int(raw_mode, 8)
                or stat.S_IMODE(info.st_mode) & 0o022
                or (
                    not expected_directory
                    and (
                        isinstance(raw_row.get("bytes"), bool)
                        or raw_row.get("bytes") != info.st_size
                        or _SHA256_RE.fullmatch(str(raw_row.get("sha256", "")))
                        is None
                    )
                )
            ):
                raise SourceIdentityError("Python runtime system ABI metadata differs")
            rows.append(
                (
                    kind,
                    raw_path,
                    info.st_dev,
                    info.st_ino,
                    info.st_uid,
                    stat.S_IMODE(info.st_mode),
                    info.st_nlink,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                )
            )
    if (
        elf_audit.get("system_directory_count") != len(raw_directories)
        or elf_audit.get("system_library_count") != len(raw_libraries)
    ):
        raise SourceIdentityError("Python runtime system ABI count differs")
    return tuple(sorted(rows))


def source_identity_status(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Validate configured source identity and return a health-safe snapshot."""

    global _CACHED_BINDING, _CACHED_IDENTITY, _CACHED_RELEASE_STAMP
    environment = os.environ if environ is None else environ
    head = str(environment.get(SOURCE_HEAD_ENV, "")).strip().casefold()
    tree = str(environment.get(SOURCE_TREE_ENV, "")).strip().casefold()
    manifest_raw = str(environment.get(SOURCE_MANIFEST_ENV, "")).strip()
    expected_sha256 = str(
        environment.get(SOURCE_MANIFEST_SHA256_ENV, "")
    ).strip().casefold()
    result: dict[str, Any] = {
        "ready": False,
        "head": head if _GIT_OBJECT_RE.fullmatch(head) else None,
        "tree": tree if _GIT_OBJECT_RE.fullmatch(tree) else None,
        "manifest_sha256": None,
        "files_verified": False,
        "file_count": 0,
        "process_pid": os.getpid(),
        "process_start_ticks": None,
    }
    try:
        if (
            _GIT_OBJECT_RE.fullmatch(head) is None
            or _GIT_OBJECT_RE.fullmatch(tree) is None
            or _SHA256_RE.fullmatch(expected_sha256) is None
            or not manifest_raw
        ):
            raise SourceIdentityError("source identity environment is incomplete")
        binding = (head, tree, manifest_raw, expected_sha256)
        with _CACHE_LOCK:
            if (
                _CACHED_BINDING == binding
                and _CACHED_IDENTITY is not None
                and _CACHED_RELEASE_STAMP is not None
            ):
                manifest_path = Path(manifest_raw).expanduser()
                release = manifest_path.parent.resolve(strict=True)
                raw = _stable_regular_bytes(
                    release / SOURCE_MANIFEST_FILE,
                    expected_mode=0o400,
                    max_bytes=SOURCE_MANIFEST_MAX_BYTES,
                )
                if (
                    hashlib.sha256(raw).hexdigest() != expected_sha256
                    or release.name != f"release-{expected_sha256}"
                    or _release_stat_stamp(release) != _CACHED_RELEASE_STAMP
                ):
                    raise SourceIdentityError(
                        "source release changed after startup verification"
                    )
                identity = dict(_CACHED_IDENTITY)
            else:
                release = Path(manifest_raw).expanduser().parent.resolve(strict=True)
                stamp_before = _release_stat_stamp(release)
                identity = validate_source_release(
                    Path(manifest_raw),
                    expected_head=head,
                    expected_tree=tree,
                    expected_manifest_sha256=expected_sha256,
                )
                stamp_after = _release_stat_stamp(release)
                if stamp_before != stamp_after:
                    raise SourceIdentityError(
                        "source release changed across startup verification"
                    )
                _CACHED_BINDING = binding
                _CACHED_IDENTITY = dict(identity)
                _CACHED_RELEASE_STAMP = stamp_after
        start_ticks = process_start_ticks()
    except (OSError, SourceIdentityError, ValueError):
        return result
    result.update(
        {
            "ready": True,
            "head": identity["head"],
            "tree": identity["tree"],
            "manifest_sha256": identity["manifest_sha256"],
            "files_verified": True,
            "file_count": identity["file_count"],
            "process_start_ticks": start_ticks,
        }
    )
    return result


def require_source_identity() -> dict[str, Any]:
    """Return the current identity or prevent the service from starting."""

    status = source_identity_status()
    if status.get("ready") is not True:
        raise RuntimeError("不可变源码 release 身份验证失败")
    return status


def python_runtime_identity_status(
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return a health-safe proof of the running immutable Python runtime."""

    global _CACHED_PYTHON_RUNTIME_BINDING
    global _CACHED_PYTHON_RUNTIME_IDENTITY
    global _CACHED_PYTHON_RUNTIME_STAMP
    global _CACHED_PYTHON_SYSTEM_ABI_STAMP

    environment = os.environ if environ is None else environ
    runtime_raw = str(environment.get(PYTHON_RUNTIME_ENV, "")).strip()
    manifest_raw = str(environment.get(PYTHON_RUNTIME_MANIFEST_ENV, "")).strip()
    manifest_sha256 = str(
        environment.get(PYTHON_RUNTIME_MANIFEST_SHA256_ENV, "")
    ).strip().casefold()
    tree_sha256 = str(
        environment.get(PYTHON_RUNTIME_TREE_SHA256_ENV, "")
    ).strip().casefold()
    result: dict[str, Any] = {
        "ready": False,
        "manifest_sha256": manifest_sha256 if _SHA256_RE.fullmatch(manifest_sha256) else None,
        "runtime_tree_sha256": tree_sha256 if _SHA256_RE.fullmatch(tree_sha256) else None,
        "python_executable_sha256": None,
        "python_version": None,
        "python_soabi": None,
        "python_platform": None,
        "wheel_count": 0,
        "elf_audit_sha256": None,
        "system_library_count": 0,
        "system_directory_count": 0,
        "system_abi_stat_verified": False,
        "files_verified": False,
        "proc_exe_matches": False,
        "process_pid": os.getpid(),
        "process_start_ticks": None,
    }
    try:
        if (
            not runtime_raw
            or not manifest_raw
            or _SHA256_RE.fullmatch(manifest_sha256) is None
            or _SHA256_RE.fullmatch(tree_sha256) is None
        ):
            raise SourceIdentityError("Python runtime environment is incomplete")
        runtime = Path(runtime_raw).expanduser().resolve(strict=True)
        manifest = Path(manifest_raw).expanduser().resolve(strict=True)
        if manifest != runtime / PYTHON_RUNTIME_MANIFEST_FILE:
            raise SourceIdentityError("Python runtime manifest path is inconsistent")
        binding = (str(runtime), str(manifest), manifest_sha256, tree_sha256)

        # Kept lazy to avoid a module-import cycle: manage_deployment imports
        # this module for source-release validation.
        from scripts import manage_deployment

        with _CACHE_LOCK:
            if (
                _CACHED_PYTHON_RUNTIME_BINDING == binding
                and _CACHED_PYTHON_RUNTIME_IDENTITY is not None
                and _CACHED_PYTHON_RUNTIME_STAMP is not None
                and _CACHED_PYTHON_SYSTEM_ABI_STAMP is not None
            ):
                raw_manifest = _stable_regular_bytes(
                    manifest,
                    expected_mode=0o400,
                    max_bytes=manage_deployment.MAX_PYTHON_RUNTIME_MANIFEST_BYTES,
                )
                if (
                    hashlib.sha256(raw_manifest).hexdigest() != manifest_sha256
                    or runtime.name != f"python-runtime-{manifest_sha256}"
                    or _python_runtime_stat_stamp(runtime)
                    != _CACHED_PYTHON_RUNTIME_STAMP
                    or _system_abi_stat_stamp(raw_manifest)
                    != _CACHED_PYTHON_SYSTEM_ABI_STAMP
                ):
                    raise SourceIdentityError(
                        "Python runtime changed after startup verification"
                    )
                identity = dict(_CACHED_PYTHON_RUNTIME_IDENTITY)
            else:
                stamp_before = _python_runtime_stat_stamp(runtime)
                identity = manage_deployment.validate_python_runtime_release(
                    manifest,
                    expected_manifest_sha256=manifest_sha256,
                )
                if identity.get("runtime_tree_sha256") != tree_sha256:
                    raise SourceIdentityError("Python runtime tree SHA-256 differs")
                stamp_after = _python_runtime_stat_stamp(runtime)
                if stamp_before != stamp_after:
                    raise SourceIdentityError(
                        "Python runtime changed across startup verification"
                    )
                raw_manifest = _stable_regular_bytes(
                    manifest,
                    expected_mode=0o400,
                    max_bytes=manage_deployment.MAX_PYTHON_RUNTIME_MANIFEST_BYTES,
                )
                if hashlib.sha256(raw_manifest).hexdigest() != manifest_sha256:
                    raise SourceIdentityError(
                        "Python runtime manifest changed after full verification"
                    )
                system_abi_stamp = _system_abi_stat_stamp(raw_manifest)
                _CACHED_PYTHON_RUNTIME_BINDING = binding
                _CACHED_PYTHON_RUNTIME_IDENTITY = dict(identity)
                _CACHED_PYTHON_RUNTIME_STAMP = stamp_after
                _CACHED_PYTHON_SYSTEM_ABI_STAMP = system_abi_stamp

        start_before = process_start_ticks()
        executable_path, executable_sha256 = (
            manage_deployment._process_executable_identity(os.getpid())
        )
        start_after = process_start_ticks()
        if start_before != start_after:
            raise SourceIdentityError("process identity changed during runtime proof")
        if (
            executable_path != identity.get("python_executable")
            or executable_sha256 != identity.get("python_executable_sha256")
        ):
            raise SourceIdentityError("running executable differs from Python runtime")
    except (ImportError, KeyError, OSError, SourceIdentityError, ValueError):
        return result
    result.update(
        {
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
            "process_start_ticks": start_after,
        }
    )
    return result


def require_python_runtime_identity() -> dict[str, Any]:
    """Return the current Python identity or prevent the service from starting."""

    status = python_runtime_identity_status()
    if status.get("ready") is not True:
        raise RuntimeError("不可变 Python runtime 身份验证失败")
    return status
