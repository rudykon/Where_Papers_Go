#!/usr/bin/env python3
"""Long-lived JSON-lines worker for the mandatory retrieval pipeline."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import time
import traceback
from typing import Any, Callable, TextIO

from . import lightrag, recommender
from .embeddings import default_graph_embedding_cache_path
from .graph_index import default_graph_path
from .paths import DATA_DIR, DEFAULT_CONFIG_PATH, PROJECT_ROOT


ROOT = PROJECT_ROOT
CONFIG_PATH = DEFAULT_CONFIG_PATH


def _emit(payload: dict[str, Any], stream: TextIO | None = None) -> None:
    target = stream or sys.stdout
    target.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    target.flush()


def _preload() -> dict[str, Any]:
    started = time.perf_counter()
    graph_path = default_graph_path(DATA_DIR)
    graph, _rebuilt, _reason = recommender.open_persistent_graph(
        DATA_DIR, graph_path
    )
    graph.preload_vectors()
    lightrag.preload_persistent_runtime(
        lightrag.default_lightrag_working_dir(DATA_DIR),
        graph_path,
        CONFIG_PATH,
        default_graph_embedding_cache_path(DATA_DIR),
    )
    return {
        "graph": str(graph_path),
        "preload_ms": round((time.perf_counter() - started) * 1000),
    }


def _run_search(
    argv: list[str],
    event_callback: Callable[[dict[str, Any]], None] | None = None,
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
    protocol_stdout = sys.stdout
    try:
        # Keep third-party startup chatter away from the JSON-lines protocol.
        with redirect_stdout(sys.stderr):
            runtime = _preload()
    except BaseException:
        _emit({"op": "ready", "ready": False, "error": traceback.format_exc()})
        return 1
    _emit({"op": "ready", "ready": True, **runtime})

    try:
        for line in sys.stdin:
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

                    result = _run_search(argv, event_callback=send_event)
                    _emit(
                        {"request_id": request_id, "final": True, **result},
                        stream=protocol_stdout,
                    )
                else:
                    _emit({"request_id": request_id, **_run_search(argv)})
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
