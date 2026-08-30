#!/usr/bin/env python3
"""Long-lived JSON-lines worker for the mandatory retrieval pipeline."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import stat
import sys
import time
import traceback
from typing import Any, Callable, TextIO

from . import lightrag, recommender
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
MAX_WORKER_MESSAGE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class WorkerCacheBindings:
    """Write-cache paths frozen before the persistent runtime is preloaded."""

    api_cache_dir: Path
    query_embedding_cache: Path
    lightrag_embedding_cache: Path
    lightrag_working_dir: Path
    graph_path: Path


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


def _preload(bindings: WorkerCacheBindings | None = None) -> dict[str, Any]:
    bindings = bindings or _worker_cache_bindings()
    started = time.perf_counter()
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
        cache_bindings = _worker_cache_bindings()
        # Keep third-party startup chatter away from the JSON-lines protocol.
        with redirect_stdout(sys.stderr):
            runtime = _preload(cache_bindings)
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
