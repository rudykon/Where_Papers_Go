"""Fail-closed identity checks for an immutable source release.

The service is started from a content-addressed directory produced by
``scripts.manage_deployment prepare-source-release``.  This module deliberately
does not consult Git at runtime: the read-only manifest, its externally pinned
digest, and every regular file in the release are the complete trust input.
"""

from __future__ import annotations

import ctypes
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

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_GIT_OBJECT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_CACHE_LOCK = threading.RLock()
_CACHED_BINDING: tuple[str, str, str, str] | None = None
_CACHED_IDENTITY: dict[str, Any] | None = None
_CACHED_RELEASE_STAMP: tuple[tuple[Any, ...], ...] | None = None


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
