#!/usr/bin/env python3
"""Long-lived JSON-lines worker for the mandatory retrieval pipeline."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import time
import traceback
from typing import Any, Callable, Mapping, TextIO

from . import deployment_identity, lightrag, recommender
from .embeddings import (
    default_graph_embedding_cache_path,
    default_query_embedding_cache_path,
)
from .graph_index import default_graph_path
from .paths import DATA_DIR, DEFAULT_CONFIG_PATH, PROJECT_ROOT


ROOT = PROJECT_ROOT
CONFIG_PATH = DEFAULT_CONFIG_PATH
LIGHTRAG_WORKING_DIR_ENV = "WPG_LIGHTRAG_WORKING_DIR"
GRAPH_PATH_ENV = "WPG_GRAPH_PATH"
STRICT_GRAPH_READ_ONLY_ENV = "WPG_STRICT_GRAPH_READ_ONLY"
REQUIRE_RUNTIME_SHADOW_ENV = "WPG_REQUIRE_RUNTIME_SHADOW"
RUNTIME_GENERATION_ENV = "WPG_RUNTIME_GENERATION"
RUNTIME_MANIFEST_ENV = "WPG_RUNTIME_MANIFEST"
RUNTIME_MANIFEST_SHA256_ENV = "WPG_RUNTIME_MANIFEST_SHA256"
RUNTIME_MANIFEST_FILE = "runtime-shadow-manifest.json"
RUNTIME_MANIFEST_MAX_BYTES = 16 * 1024 * 1024
RUNTIME_STORE_MAX_BYTES = 2 * 1024 * 1024 * 1024
RUNTIME_STORE_MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_WORKER_MESSAGE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class WorkerCacheBindings:
    """Write-cache paths frozen before the persistent runtime is preloaded."""

    api_cache_dir: Path
    query_embedding_cache: Path
    lightrag_embedding_cache: Path
    lightrag_working_dir: Path
    graph_path: Path


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    """Return the fields that must remain stable across one verified read."""

    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read_stable_private_file(
    path: Path,
    *,
    expected_bytes: int | None = None,
    exact_mode: int | None = None,
    max_bytes: int,
    capture: bool,
) -> tuple[str, bytes | None, int]:
    """Hash an owned regular file through one stable, no-follow descriptor."""

    try:
        path_before = path.lstat()
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
    except OSError as exc:
        raise ValueError(f"frozen runtime file is unavailable: {path.name}") from exc
    try:
        before = os.fstat(descriptor)
        mode = stat.S_IMODE(before.st_mode)
        if (
            stat.S_ISLNK(path_before.st_mode)
            or not stat.S_ISREG(path_before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or mode & 0o077
            or (exact_mode is not None and mode != exact_mode)
            or before.st_size < 0
            or before.st_size > max_bytes
            or (expected_bytes is not None and before.st_size != expected_bytes)
            or _file_identity(path_before) != _file_identity(before)
        ):
            raise ValueError(f"frozen runtime file has an unsafe identity: {path.name}")
        digest = hashlib.sha256()
        chunks: list[bytes] | None = [] if capture else None
        observed = 0
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            observed += len(block)
            if observed > max_bytes:
                raise ValueError(f"frozen runtime file is oversized: {path.name}")
            digest.update(block)
            if chunks is not None:
                chunks.append(block)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        path_after = path.lstat()
    except OSError as exc:
        raise ValueError(f"frozen runtime file changed while read: {path.name}") from exc
    if (
        observed != before.st_size
        or _file_identity(before) != _file_identity(after)
        or _file_identity(before) != _file_identity(path_after)
    ):
        raise ValueError(f"frozen runtime file changed while read: {path.name}")
    return digest.hexdigest(), b"".join(chunks) if chunks is not None else None, observed


def _validate_frozen_lightrag_store(
    bindings: WorkerCacheBindings,
) -> dict[str, Any]:
    """Verify every frozen LightRAG store hash before any runtime is opened."""

    if os.environ.get(REQUIRE_RUNTIME_SHADOW_ENV, "").strip() != "1":
        return {
            "required": False,
            "verified": False,
            "file_count": 0,
            "bytes": 0,
            "manifest_sha256": None,
            "store_binding_sha256": None,
        }

    generation_raw = os.environ.get(RUNTIME_GENERATION_ENV, "").strip()
    manifest_raw = os.environ.get(RUNTIME_MANIFEST_ENV, "").strip()
    expected_manifest_sha256 = os.environ.get(
        RUNTIME_MANIFEST_SHA256_ENV, ""
    ).strip()
    if (
        not generation_raw
        or not manifest_raw
        or not Path(generation_raw).expanduser().is_absolute()
        or not Path(manifest_raw).expanduser().is_absolute()
        or len(expected_manifest_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in expected_manifest_sha256
        )
    ):
        raise ValueError("production runtime manifest binding is incomplete or invalid")

    generation = Path(generation_raw).expanduser().resolve()
    manifest_path = Path(manifest_raw).expanduser()
    expected_manifest_path = generation / RUNTIME_MANIFEST_FILE
    if manifest_path.resolve() != expected_manifest_path:
        raise ValueError("production runtime manifest path is not generation-bound")
    expected_working_dir = generation / "lightrag_storage"
    if bindings.lightrag_working_dir != expected_working_dir:
        raise ValueError("LightRAG working directory is not the frozen runtime store")
    try:
        working_info = expected_working_dir.lstat()
    except OSError as exc:
        raise ValueError("frozen LightRAG store directory is unavailable") from exc
    if (
        stat.S_ISLNK(working_info.st_mode)
        or not stat.S_ISDIR(working_info.st_mode)
        or working_info.st_uid != os.geteuid()
        or stat.S_IMODE(working_info.st_mode) & 0o077
    ):
        raise ValueError("frozen LightRAG store directory has an unsafe identity")

    actual_manifest_sha256, raw_manifest, _manifest_bytes = _read_stable_private_file(
        manifest_path,
        exact_mode=0o400,
        max_bytes=RUNTIME_MANIFEST_MAX_BYTES,
        capture=True,
    )
    if actual_manifest_sha256 != expected_manifest_sha256:
        raise ValueError("production runtime manifest SHA-256 drifted")
    assert raw_manifest is not None
    try:
        manifest = json.loads(raw_manifest.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("production runtime manifest is unreadable") from exc
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("schema_version") != 1
        or manifest.get("artifact_type") != "where_papers_go_runtime_shadow"
        or manifest.get("source_data_dir") != str(DATA_DIR.resolve())
        or not isinstance(manifest.get("source_binding_sha256"), str)
        or len(str(manifest.get("source_binding_sha256"))) != 64
        or any(
            character not in "0123456789abcdef"
            for character in str(manifest.get("source_binding_sha256"))
        )
        or manifest.get("write_boundary") != "runtime_generation_only"
        or manifest.get("protected_sources_never_replaced") is not True
        or not isinstance(manifest.get("files"), list)
    ):
        raise ValueError("production runtime manifest contract is invalid")

    file_bindings: dict[str, Mapping[str, Any]] = {}
    for row in manifest["files"]:
        if not isinstance(row, Mapping) or not isinstance(row.get("runtime_path"), str):
            raise ValueError("production runtime manifest contains an invalid file row")
        runtime_path = str(row["runtime_path"])
        if runtime_path in file_bindings:
            raise ValueError("production runtime manifest contains duplicate file bindings")
        file_bindings[runtime_path] = row

    required_names = (lightrag.MANIFEST_FILE, *lightrag.QUERY_STORAGE_FILES)
    verified_rows: list[dict[str, Any]] = []
    total_bytes = 0
    for name in required_names:
        relative = f"lightrag_storage/{name}"
        row = file_bindings.get(relative)
        if row is None:
            raise ValueError(f"runtime manifest does not bind frozen LightRAG file: {name}")
        expected_bytes = row.get("bytes")
        expected_sha256 = row.get("sha256")
        if (
            isinstance(expected_bytes, bool)
            or not isinstance(expected_bytes, int)
            or expected_bytes < 0
            or expected_bytes > RUNTIME_STORE_MAX_BYTES
            or not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in expected_sha256
            )
        ):
            raise ValueError(f"runtime manifest has an invalid LightRAG binding: {name}")
        actual_sha256, _raw, observed_bytes = _read_stable_private_file(
            expected_working_dir / name,
            expected_bytes=expected_bytes,
            max_bytes=RUNTIME_STORE_MAX_BYTES,
            capture=False,
        )
        if actual_sha256 != expected_sha256:
            raise ValueError(f"frozen LightRAG store SHA-256 drifted: {name}")
        total_bytes += observed_bytes
        if total_bytes > RUNTIME_STORE_MAX_TOTAL_BYTES:
            raise ValueError("frozen LightRAG stores exceed the cumulative size bound")
        verified_rows.append(
            {
                "runtime_path": relative,
                "bytes": expected_bytes,
                "sha256": expected_sha256,
            }
        )
    binding_payload = json.dumps(
        verified_rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "required": True,
        "verified": True,
        "file_count": len(verified_rows),
        "bytes": total_bytes,
        "manifest_sha256": actual_manifest_sha256,
        "store_binding_sha256": hashlib.sha256(binding_payload).hexdigest(),
    }


def _optional_environment_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser().resolve() if value else None


def _worker_cache_bindings() -> WorkerCacheBindings:
    """Resolve run-local bindings without creating or opening any cache."""

    api_cache_dir = _optional_environment_path(
        recommender.API_CACHE_DIR_ENV
    ) or (DATA_DIR / ".query_api_cache").resolve()
    query_cache = _optional_environment_path(
        recommender.QUERY_EMBEDDING_CACHE_ENV
    ) or default_query_embedding_cache_path(DATA_DIR).resolve()
    lightrag_cache = _optional_environment_path(
        recommender.LIGHTRAG_EMBEDDING_CACHE_ENV
    ) or default_graph_embedding_cache_path(DATA_DIR).resolve()
    lightrag_working_dir = _optional_environment_path(
        LIGHTRAG_WORKING_DIR_ENV
    ) or lightrag.default_lightrag_working_dir(DATA_DIR).resolve()
    graph_path = _optional_environment_path(
        GRAPH_PATH_ENV
    ) or default_graph_path(DATA_DIR).resolve()
    if query_cache == lightrag_cache:
        raise ValueError(
            f"{recommender.QUERY_EMBEDDING_CACHE_ENV} and "
            f"{recommender.LIGHTRAG_EMBEDDING_CACHE_ENV} must name different files"
        )
    if any(
        cache_path == api_cache_dir
        or cache_path.is_relative_to(api_cache_dir)
        or api_cache_dir.is_relative_to(cache_path)
        for cache_path in (query_cache, lightrag_cache)
    ):
        raise ValueError(
            "API cache directory must not contain either embedding cache file"
        )
    write_files = (query_cache, lightrag_cache)
    if any(
        cache_path == lightrag_working_dir
        or cache_path.is_relative_to(lightrag_working_dir)
        or lightrag_working_dir.is_relative_to(cache_path)
        for cache_path in write_files
    ):
        raise ValueError(
            "LightRAG working directory must not contain either embedding cache file"
        )
    if (
        api_cache_dir == lightrag_working_dir
        or api_cache_dir.is_relative_to(lightrag_working_dir)
        or lightrag_working_dir.is_relative_to(api_cache_dir)
    ):
        raise ValueError("API cache and LightRAG working directories must be disjoint")
    if os.environ.get(REQUIRE_RUNTIME_SHADOW_ENV, "").strip() == "1":
        protected_data = DATA_DIR.resolve()
        protected_project = PROJECT_ROOT.resolve()

        def inside_protected(path: Path) -> bool:
            try:
                path.relative_to(protected_data)
                return True
            except ValueError:
                return False

        if any(
            inside_protected(path) or path.is_relative_to(protected_project)
            for path in (
                api_cache_dir,
                query_cache,
                lightrag_cache,
                lightrag_working_dir,
            )
        ):
            raise ValueError(
                "production runtime write/cache bindings must be outside protected sources"
            )
        generation_raw = os.environ.get(RUNTIME_GENERATION_ENV, "").strip()
        generation_path = Path(generation_raw).expanduser()
        if not generation_raw or not generation_path.is_absolute():
            raise ValueError(
                "production runtime shadow requires an absolute WPG_RUNTIME_GENERATION"
            )
        generation = generation_path.resolve()
        try:
            generation_info = generation_path.lstat()
        except OSError as exc:
            raise ValueError("production runtime generation is unavailable") from exc
        if (
            stat.S_ISLNK(generation_info.st_mode)
            or not stat.S_ISDIR(generation_info.st_mode)
            or generation_info.st_uid != os.geteuid()
            or stat.S_IMODE(generation_info.st_mode) & 0o077
        ):
            raise ValueError(
                "production runtime generation has unsafe ownership, type, or mode"
            )
        if any(
            not path.is_relative_to(generation)
            for path in (
                api_cache_dir,
                query_cache,
                lightrag_cache,
                lightrag_working_dir,
            )
        ):
            raise ValueError(
                "production runtime write/cache bindings must belong to one generation"
            )
        if os.environ.get(STRICT_GRAPH_READ_ONLY_ENV, "").strip() != "1":
            raise ValueError(
                "production runtime shadow requires WPG_STRICT_GRAPH_READ_ONLY=1"
            )
    return WorkerCacheBindings(
        api_cache_dir=api_cache_dir,
        query_embedding_cache=query_cache,
        lightrag_embedding_cache=lightrag_cache,
        lightrag_working_dir=lightrag_working_dir,
        graph_path=graph_path,
    )


def _argv_option_value(argv: list[str], option: str) -> Path | None:
    values: list[Path] = []
    for index, value in enumerate(argv):
        if value == option:
            if index + 1 >= len(argv):
                raise ValueError(f"{option} requires a path")
            values.append(Path(argv[index + 1]).resolve())
        elif value.startswith(option + "="):
            raw_value = value.partition("=")[2]
            if not raw_value:
                raise ValueError(f"{option} requires a path")
            values.append(Path(raw_value).resolve())
    if not values:
        return None
    if any(value != values[0] for value in values[1:]):
        raise ValueError(f"{option} was provided with conflicting paths")
    return values[0]


def _bind_cache_argv(
    argv: list[str], bindings: WorkerCacheBindings
) -> list[str]:
    """Make every query use the cache paths frozen before worker preload."""

    bound_argv = list(argv)
    options = (
        ("--api-cache-dir", bindings.api_cache_dir),
        ("--query-embedding-cache", bindings.query_embedding_cache),
        ("--lightrag-embedding-cache", bindings.lightrag_embedding_cache),
        ("--lightrag-working-dir", bindings.lightrag_working_dir),
        ("--graph", bindings.graph_path),
    )
    for option, expected in options:
        supplied = _argv_option_value(bound_argv, option)
        if supplied is not None and expected is None:
            raise ValueError(
                f"{option} requires the matching worker startup environment binding"
            )
        if supplied is not None and supplied != expected:
            raise ValueError(f"{option} differs from the worker startup binding")
        if supplied is None and expected is not None:
            bound_argv.extend((option, str(expected)))
    return bound_argv


def _emit(payload: dict[str, Any], stream: TextIO | None = None) -> None:
    target = stream or sys.stdout
    target.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    target.flush()


def _verified_worker_identity() -> dict[str, Any]:
    """Prove this worker's source, Python runtime, and live proc identity."""

    interpreter = {
        "argv_exact": True,
        "no_site": sys.flags.no_site == 1,
        "safe_path": sys.flags.safe_path is True,
        "dont_write_bytecode": sys.flags.dont_write_bytecode == 1,
    }
    if (
        interpreter
        != {
            "argv_exact": True,
            "no_site": True,
            "safe_path": True,
            "dont_write_bytecode": True,
        }
        or __spec__ is None
        or __spec__.name != "where_paper_go.worker"
    ):
        raise RuntimeError("worker interpreter invocation flags are not exact")
    source = deployment_identity.require_source_identity()
    python_runtime = deployment_identity.require_python_runtime_identity()
    executable_sha256 = python_runtime.get("python_executable_sha256")
    if not isinstance(executable_sha256, str):
        raise RuntimeError("Python runtime executable identity is incomplete")
    process = deployment_identity.process_executable_stamp(
        os.getpid(),
        expected_executable=Path(sys.executable),
        expected_sha256=executable_sha256,
    )
    if (
        source.get("ready") is not True
        or source.get("files_verified") is not True
        or source.get("process_pid") != process.pid
        or source.get("process_start_ticks") != process.start_ticks
        or python_runtime.get("ready") is not True
        or python_runtime.get("files_verified") is not True
        or python_runtime.get("proc_exe_matches") is not True
        or python_runtime.get("system_abi_stat_verified") is not True
        or python_runtime.get("process_pid") != process.pid
        or python_runtime.get("process_start_ticks") != process.start_ticks
        or python_runtime.get("python_executable_sha256")
        != process.executable_sha256
    ):
        raise RuntimeError("worker immutable identity proofs do not agree")
    return {
        "schema_version": 1,
        "exact": True,
        "interpreter": interpreter,
        "process": deployment_identity.process_executable_stamp_payload(process),
        "source": {
            "head": source.get("head"),
            "tree": source.get("tree"),
            "manifest_sha256": source.get("manifest_sha256"),
            "files_verified": True,
        },
        "python_runtime": {
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
            "system_directory_count": python_runtime.get(
                "system_directory_count"
            ),
            "files_verified": True,
            "proc_exe_matches": True,
            "system_abi_stat_verified": True,
        },
    }


def _preload(bindings: WorkerCacheBindings | None = None) -> dict[str, Any]:
    bindings = bindings or _worker_cache_bindings()
    started = time.perf_counter()
    store_verification = _validate_frozen_lightrag_store(bindings)
    graph_path = bindings.graph_path
    graph, _rebuilt, _reason = recommender.open_persistent_graph(
        DATA_DIR, graph_path
    )
    graph.preload_vectors()
    lightrag.preload_persistent_runtime(
        bindings.lightrag_working_dir,
        graph_path,
        CONFIG_PATH,
        bindings.lightrag_embedding_cache,
    )
    return {
        "graph": str(graph_path),
        "preload_ms": round((time.perf_counter() - started) * 1000),
        "lightrag_store_verification": store_verification,
        "bindings": {
            "graph_path": str(bindings.graph_path),
            "lightrag_working_dir": str(bindings.lightrag_working_dir),
            "api_cache_dir": str(bindings.api_cache_dir),
            "query_embedding_cache": str(bindings.query_embedding_cache),
            "lightrag_embedding_cache": str(bindings.lightrag_embedding_cache),
        },
    }


def _run_search(
    argv: list[str],
    event_callback: Callable[[dict[str, Any]], None] | None = None,
    *,
    cache_bindings: WorkerCacheBindings | None = None,
) -> dict[str, Any]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    started = time.perf_counter()
    returncode = 0

    def timed_event(event: dict[str, Any]) -> None:
        if event_callback is None:
            return
        event_callback(
            {
                **event,
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
            }
        )

    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            if cache_bindings is not None:
                argv = _bind_cache_argv(argv, cache_bindings)
            returncode = int(
                recommender.main(argv, event_callback=timed_event)
            )
    except SystemExit as exc:
        returncode = int(exc.code) if isinstance(exc.code, int) else 1
    except BaseException:
        returncode = 1
        traceback.print_exc(file=stderr)
    return {
        "returncode": returncode,
        "stdout": stdout.getvalue(),
        "stderr": stderr.getvalue(),
        "worker_elapsed_ms": round((time.perf_counter() - started) * 1000),
    }


def main() -> int:
    # Every cache artifact created by the persistent worker is private even on
    # hosts whose interactive umask would otherwise permit group/world reads.
    os.umask(0o077)
    protocol_stdout = sys.stdout
    try:
        worker_identity_before = _verified_worker_identity()
        cache_bindings = _worker_cache_bindings()
        # Keep third-party startup chatter away from the JSON-lines protocol.
        with redirect_stdout(sys.stderr):
            runtime = _preload(cache_bindings)
        worker_identity_after = _verified_worker_identity()
        if worker_identity_before != worker_identity_after:
            raise RuntimeError("worker identity changed during preload")
        runtime["worker_identity"] = worker_identity_after
    except BaseException:
        _emit({"op": "ready", "ready": False, "error": traceback.format_exc()})
        return 1
    _emit({"op": "ready", "ready": True, **runtime})

    try:
        while True:
            raw_line = sys.stdin.buffer.readline(MAX_WORKER_MESSAGE_BYTES + 1)
            if not raw_line:
                break
            if len(raw_line) > MAX_WORKER_MESSAGE_BYTES or not raw_line.endswith(b"\n"):
                _emit(
                    {
                        "request_id": "",
                        "returncode": 1,
                        "stdout": "",
                        "stderr": "worker request exceeds the bounded JSON-lines protocol",
                    }
                )
                return 1
            try:
                line = raw_line.decode("utf-8")
            except UnicodeError:
                _emit(
                    {
                        "request_id": "",
                        "returncode": 1,
                        "stdout": "",
                        "stderr": "worker request is not valid UTF-8",
                    }
                )
                return 1
            request: dict[str, Any] | None = None
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("worker request must be an object")
                request_id = str(request.get("request_id") or "")
                operation = str(request.get("op") or "search")
                if operation == "shutdown":
                    _emit({"request_id": request_id, "returncode": 0})
                    break
                argv = request.get("argv")
                if not isinstance(argv, list) or not all(
                    isinstance(value, str) for value in argv
                ):
                    raise ValueError("worker argv must be a string array")
                if operation == "search_stream":
                    def send_event(event: dict[str, Any]) -> None:
                        _emit(
                            {"request_id": request_id, "event": event},
                            stream=protocol_stdout,
                        )

                    result = _run_search(
                        argv,
                        event_callback=send_event,
                        cache_bindings=cache_bindings,
                    )
                    _emit(
                        {"request_id": request_id, "final": True, **result},
                        stream=protocol_stdout,
                    )
                else:
                    _emit(
                        {
                            "request_id": request_id,
                            **_run_search(argv, cache_bindings=cache_bindings),
                        }
                    )
            except BaseException:
                _emit(
                    {
                        "request_id": str(
                            request.get("request_id") if isinstance(request, dict) else ""
                        ),
                        "final": bool(
                            isinstance(request, dict)
                            and request.get("op") == "search_stream"
                        ),
                        "returncode": 1,
                        "stdout": "",
                        "stderr": traceback.format_exc(),
                    }
                )
    except KeyboardInterrupt:
        pass
    finally:
        lightrag.disable_persistent_runtime()
        recommender.clear_graph_runtime_cache()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
