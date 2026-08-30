#!/usr/bin/env python3
"""Render, verify, and health-check the audited production deployment.

Rendering is a dry-run unless ``--apply`` is explicit. Existing output files
are preserved at timestamped backups before an atomic replacement; nothing is
deleted by this tool.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence
import urllib.error
import urllib.request

try:  # pragma: no cover - the deployment target is Linux.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from where_paper_go.paths import PROJECT_ROOT


SYSTEMD_TEMPLATE = PROJECT_ROOT / "deploy" / "systemd" / "where-papers-go.service.in"
NGINX_TEMPLATE = PROJECT_ROOT / "deploy" / "nginx" / "where-papers-go.conf.in"
EXPECTED_BACKEND = "lightrag_mix+property_graph_exact_vector+llm+search_api"
RUNTIME_MANIFEST = "runtime-shadow-manifest.json"
TAVILY_STATE_NAME = ".tavily_key_pool_state.json"
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
        os.rename(building, shared_expanded)
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
        os.rename(building, generation)
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


def render_systemd(args: argparse.Namespace) -> dict[str, Any]:
    project_root = args.project_root.resolve()
    python = args.python.resolve()
    data_dir = args.data_dir.expanduser().resolve()
    runtime_dir = _validated_runtime_shadow(args.runtime_dir, data_dir=data_dir)
    shared_state_dir = _private_directory(args.shared_state_dir, create=False)
    _validate_shared_tavily_state(shared_state_dir, api_config=args.api_config)
    if not project_root.is_dir():
        raise ValueError(f"project root is not a directory: {project_root}")
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError(f"python executable is unavailable: {python}")
    payload = render_template(
        args.template,
        {
            "PROJECT_ROOT": project_root,
            "PYTHON": python,
            "DATA_DIR": data_dir,
            "CONFIG_PATH": args.api_config.expanduser().resolve(),
            "ENV_FILE": args.environment_file,
            "RUNTIME_DIR": runtime_dir,
            "SHARED_STATE_DIR": shared_state_dir,
            "RUNTIME_MANIFEST_SHA256": _runtime_manifest_sha256(runtime_dir),
        },
    )
    return _render_result(
        kind="systemd-unit",
        output=args.output.expanduser(),
        payload=payload,
        apply=args.apply,
        mode=0o644,
    )


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
    for path_name in ("tls_certificate", "tls_certificate_key", "htpasswd"):
        if not getattr(args, path_name).is_absolute():
            raise ValueError(f"--{path_name.replace('_', '-')} must be absolute")
    payload = render_template(
        args.template,
        {
            "UPSTREAM_PORT": str(args.upstream_port),
            "SERVER_NAME": server_name,
            "TLS_CERTIFICATE": args.tls_certificate,
            "TLS_CERTIFICATE_KEY": args.tls_certificate_key,
            "HTPASSWD": args.htpasswd,
        },
    )
    return _render_result(
        kind="nginx-config",
        output=args.output.expanduser(),
        payload=payload,
        apply=args.apply,
        mode=0o644,
    )


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
    if args.api_token_file is not None and not args.api_token_file.is_absolute():
        raise ValueError("--api-token-file must be absolute")
    if args.require_api_auth and args.api_token_file is None:
        raise ValueError("--require-api-auth requires --api-token-file")
    try:
        trusted_proxy_cidrs = ",".join(
            str(ipaddress.ip_network(item.strip(), strict=False))
            for item in args.trusted_proxy_cidrs.split(",")
            if item.strip()
        )
    except ValueError as exc:
        raise ValueError("--trusted-proxy-cidrs contains an invalid network") from exc
    if not trusted_proxy_cidrs:
        raise ValueError("--trusted-proxy-cidrs must not be empty")
    try:
        allowed_client_cidrs = ",".join(
            str(ipaddress.ip_network(item.strip(), strict=False))
            for item in args.allowed_client_cidrs.split(",")
            if item.strip()
        )
    except ValueError as exc:
        raise ValueError("--allowed-client-cidrs contains an invalid network") from exc
    if not allowed_client_cidrs:
        raise ValueError("--allowed-client-cidrs must not be empty")
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
    if args.api_token_file is not None:
        lines.append(
            "WPG_API_TOKEN_FILE="
            + _safe_replacement(args.api_token_file, "api-token-file")
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
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("health bearer token file must be a private regular file")
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 32 or any(character.isspace() for character in token):
        raise ValueError("health bearer token is invalid")
    return token


def validate_health_payload(payload: Any) -> list[str]:
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
    token = _read_health_token(args.token_file.expanduser() if args.token_file else None)
    last_failure: str | None = None
    payload: Any = None
    for attempt in range(1, args.attempts + 1):
        request = urllib.request.Request(args.url, headers={"Accept": "application/json"})
        if token is not None:
            request.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(request, timeout=args.timeout) as response:
                payload = json.load(response)
            failures = validate_health_payload(payload)
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
        "runtime": payload.get("runtime"),
        "hashes": hashes,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    systemd = subparsers.add_parser("render-systemd", help="render the user unit")
    systemd.add_argument("--template", type=Path, default=SYSTEMD_TEMPLATE)
    systemd.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    systemd.add_argument("--python", type=Path, default=Path(sys.executable))
    systemd.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    systemd.add_argument(
        "--api-config", type=Path, default=PROJECT_ROOT / "llmapi.json"
    )
    systemd.add_argument(
        "--environment-file",
        default="%h/.config/where-papers-go/runtime.env",
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

    nginx = subparsers.add_parser("render-nginx", help="render the TLS proxy")
    nginx.add_argument("--template", type=Path, default=NGINX_TEMPLATE)
    nginx.add_argument("--output", type=Path, required=True)
    nginx.add_argument("--server-name", required=True)
    nginx.add_argument("--tls-certificate", type=Path, required=True)
    nginx.add_argument("--tls-certificate-key", type=Path, required=True)
    nginx.add_argument("--htpasswd", type=Path, required=True)
    nginx.add_argument("--upstream-port", type=int, default=8001)
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
    if not 1 <= getattr(args, "port", 1) <= 65_535:
        parser.error("--port must be between 1 and 65535")
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
    expected_manifest = getattr(args, "expected_manifest_sha256", None)
    if expected_manifest is not None and (
        len(expected_manifest) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in expected_manifest)
    ):
        parser.error("--expected-manifest-sha256 must be 64 hexadecimal characters")
    try:
        result = args.handler(args)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
