#!/usr/bin/env python3
"""Small same-origin web service for the venue recommendation workbench.

The service deliberately delegates topical retrieval to the recommender module.
That keeps the browser, CLI, and future clients on the exact same mandatory
LightRAG + vector + LLM + Search API path, without adding a second ranking
implementation or a heavyweight web framework.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
import hashlib
import json
import mimetypes
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import select
import socket
import stat
import subprocess
import sys
import threading
import tempfile
import time
from typing import Any, Mapping
import uuid
from urllib.parse import unquote, urlparse

from . import lightrag, recommender
from .embeddings import (
    default_graph_embedding_cache_path,
    default_query_embedding_cache_path,
)
from .graph_index import vector_path_for_graph
from .paths import DATA_DIR, DEFAULT_CONFIG_PATH, PROJECT_ROOT, WEB_DIR
from .tavily_pool import (
    TAVILY_STATE_FILE_ENV,
    TavilyKeyPool,
    TavilyKeyPoolError,
)
from .web_security import (
    SlidingWindowRateLimiter,
    WebSecurityConfig,
    audit_record,
    client_ip,
    configured_secret_values,
    redact_sensitive_text,
)


ROOT = PROJECT_ROOT
DEFAULT_CONFIG = DEFAULT_CONFIG_PATH
RESULT_CACHE_DIR = Path(
    os.environ.get("WPG_RESULT_CACHE_DIR")
    or (DATA_DIR / ".query_api_cache" / "result")
).expanduser().resolve()
RESULT_CACHE_SCHEMA_VERSION = "4"
RESULT_CACHE_TTL_SECONDS = 6 * 60 * 60
MAX_QUERY_CHARS = 8_000
MAX_LIMIT = 50
RUNTIME_GENERATION_ENV = "WPG_RUNTIME_GENERATION"
RUNTIME_MANIFEST_ENV = "WPG_RUNTIME_MANIFEST"
RUNTIME_MANIFEST_SHA256_ENV = "WPG_RUNTIME_MANIFEST_SHA256"
RUNTIME_MANIFEST_FILE = "runtime-shadow-manifest.json"
RUNTIME_MANIFEST_MAX_BYTES = 16 * 1024 * 1024
TARGET_ORDER = {"ccf": 0, "th_cpl": 1, "cas": 2, "jcr": 3}
TARGET_PREFIX = {"ccf": "CCF", "th_cpl": "TH-CPL", "cas": "CAS", "jcr": "JCR"}


class RequestBodyTooLarge(ValueError):
    """Raised before reading a body that exceeds the configured hard limit."""


class RetrievalWorkerManager:
    """Own one preloaded worker and coalesce identical concurrent searches."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None
        self._dependency_stamp: list[tuple[str, int, int, int, int, int]] | None = None
        self._transport_lock = threading.RLock()
        self._inflight_lock = threading.Lock()
        self._inflight: dict[str, Future[subprocess.CompletedProcess[str]]] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="retrieval-worker"
        )
        self.preload_ms: int | None = None
        self.runtime_bindings: dict[str, str] = {}

    @property
    def process_ready(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    @property
    def bindings_current(self) -> bool:
        return (
            self.process_ready
            and self._dependency_stamp is not None
            and self._dependency_stamp == _result_dependency_stamp()
            and self.runtime_bindings == _expected_worker_bindings()
        )

    @property
    def ready(self) -> bool:
        return self.process_ready and self.bindings_current

    def _readline(self, timeout: float) -> str:
        process = self._process
        if process is None or process.stdout is None:
            raise RuntimeError("检索工作进程未启动")
        readable, _writable, _exceptional = select.select(
            [process.stdout], [], [], timeout
        )
        if not readable:
            raise subprocess.TimeoutExpired(process.args, timeout)
        # The worker emits exactly one compact line per request.
        line = process.stdout.readline()
        if not line:
            raise RuntimeError("检索工作进程已退出")
        return line

    def _dispose_process(self, *, graceful: bool) -> None:
        """Reap one worker without ever reusing an uncertain protocol stream."""

        process = self._process
        self._process = None
        self._dependency_stamp = None
        self.runtime_bindings = {}
        if process is None:
            return
        if process.poll() is not None:
            try:
                process.wait(timeout=0)
            except (OSError, subprocess.TimeoutExpired):
                pass
            return
        if graceful:
            try:
                if process.stdin is not None:
                    process.stdin.write(
                        json.dumps(
                            {"op": "shutdown", "request_id": uuid.uuid4().hex}
                        )
                        + "\n"
                    )
                    process.stdin.flush()
                process.wait(timeout=30)
                return
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                pass
        try:
            process.terminate()
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def start(self) -> None:
        with self._transport_lock:
            if self.ready:
                return
            try:
                self._dispose_process(graceful=self.process_ready)
                dependency_before = _result_dependency_stamp()
                expected_bindings_before = _expected_worker_bindings()
                self._process = subprocess.Popen(
                    [sys.executable, "-m", "where_paper_go.worker"],
                    cwd=ROOT,
                    text=True,
                    encoding="utf-8",
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=None,
                    bufsize=1,
                )
                line = self._readline(180)
                ready = json.loads(line)
                if not isinstance(ready, dict) or not ready.get("ready"):
                    detail = str(
                        ready.get("error") if isinstance(ready, dict) else line
                    )
                    raise RuntimeError(f"检索工作进程预热失败：{detail[-2000:]}")
                dependency_after = _result_dependency_stamp()
                if dependency_before != dependency_after:
                    raise RuntimeError("检索依赖在预热期间发生变化，已拒绝混合旧新索引")
                expected_bindings_after = _expected_worker_bindings()
                if expected_bindings_before != expected_bindings_after:
                    raise RuntimeError("检索运行时绑定在预热期间发生变化")
                bindings = ready.get("bindings")
                if not (
                    isinstance(bindings, dict)
                    and all(
                        isinstance(key, str) and isinstance(value, str)
                        for key, value in bindings.items()
                    )
                    and bindings == expected_bindings_after
                ):
                    raise RuntimeError("检索工作进程报告的运行时绑定与父进程不一致")
                self._dependency_stamp = dependency_after
                self.preload_ms = int(ready.get("preload_ms") or 0)
                self.runtime_bindings = dict(bindings)
            except (
                OSError,
                ValueError,
                json.JSONDecodeError,
                RuntimeError,
                subprocess.TimeoutExpired,
            ) as exc:
                self._dispose_process(graceful=False)
                if isinstance(exc, RuntimeError):
                    raise
                raise RuntimeError("检索工作进程启动协议无效") from exc

    def _round_trip(
        self, command: list[str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        with self._transport_lock:
            try:
                self.start()
                process = self._process
                assert process is not None and process.stdin is not None
                request_stamp = list(self._dependency_stamp or ())
                if request_stamp != _result_dependency_stamp():
                    raise RuntimeError(
                        "检索依赖在请求开始前发生变化，已拒绝旧 worker"
                    )
                request_id = uuid.uuid4().hex
                request = {
                    "op": "search",
                    "request_id": request_id,
                    # Strip the interpreter and script path; the worker calls the
                    # same venue_recommender.main entry point in-process.
                    "argv": _recommender_argv(command),
                }
                process.stdin.write(
                    json.dumps(request, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
                process.stdin.flush()
                response = json.loads(self._readline(timeout))
                if request_stamp != _result_dependency_stamp():
                    raise RuntimeError(
                        "检索依赖在请求期间发生变化，已丢弃结果"
                    )
                if not isinstance(response, dict):
                    raise RuntimeError("检索工作进程返回了无效协议数据")
                if response.get("request_id") != request_id:
                    raise RuntimeError("检索工作进程响应与请求不匹配")
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=int(response.get("returncode", 1)),
                    stdout=str(response.get("stdout") or ""),
                    stderr=str(response.get("stderr") or ""),
                )
            except subprocess.TimeoutExpired:
                self._dispose_process(graceful=False)
                raise
            except (BrokenPipeError, OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
                self._dispose_process(graceful=False)
                raise RuntimeError("检索工作进程通信失败，未返回部分结果") from exc

    def run(
        self, command: list[str], *, timeout: int = 900
    ) -> subprocess.CompletedProcess[str]:
        key = json.dumps(command, ensure_ascii=False, separators=(",", ":"))
        with self._inflight_lock:
            future = self._inflight.get(key)
            if future is None:
                future = self._executor.submit(self._round_trip, command, timeout)
                self._inflight[key] = future
        try:
            return future.result(timeout=timeout + 5)
        except FutureTimeout as exc:
            process = self._process
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
            raise subprocess.TimeoutExpired(command, timeout) from exc
        finally:
            with self._inflight_lock:
                if future.done() and self._inflight.get(key) is future:
                    self._inflight.pop(key, None)

    def stream(
        self,
        command: list[str],
        on_event: Any,
        *,
        timeout: int = 900,
    ) -> subprocess.CompletedProcess[str]:
        """Run one search and relay worker events until its terminal response."""

        with self._transport_lock:
            try:
                self.start()
                process = self._process
                assert process is not None and process.stdin is not None
                request_stamp = list(self._dependency_stamp or ())
                if request_stamp != _result_dependency_stamp():
                    raise RuntimeError(
                        "检索依赖在流式请求开始前发生变化"
                    )
                request_id = uuid.uuid4().hex
                process.stdin.write(
                    json.dumps(
                        {
                            "op": "search_stream",
                            "request_id": request_id,
                            "argv": _recommender_argv(command),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                process.stdin.flush()
                deadline = time.monotonic() + timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, timeout)
                    response = json.loads(self._readline(remaining))
                    if request_stamp != _result_dependency_stamp():
                        raise RuntimeError(
                            "检索依赖在流式请求期间发生变化，已中止结果"
                        )
                    if not isinstance(response, dict):
                        raise RuntimeError("检索工作进程返回了无效协议数据")
                    if response.get("request_id") != request_id:
                        raise RuntimeError("检索工作进程流式响应与请求不匹配")
                    event = response.get("event")
                    if isinstance(event, dict):
                        on_event(event)
                        continue
                    if response.get("final"):
                        return subprocess.CompletedProcess(
                            args=command,
                            returncode=int(response.get("returncode", 1)),
                            stdout=str(response.get("stdout") or ""),
                            stderr=str(response.get("stderr") or ""),
                        )
            except subprocess.TimeoutExpired:
                self._dispose_process(graceful=False)
                raise
            except (BrokenPipeError, OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
                self._dispose_process(graceful=False)
                raise RuntimeError("检索工作进程流式通信失败，未返回部分结果") from exc

    def close(self) -> None:
        with self._transport_lock:
            self._dispose_process(graceful=True)
            self._executor.shutdown(wait=True, cancel_futures=False)


_SEARCH_RUNTIME = RetrievalWorkerManager()
_RESULT_CACHE_LOCK = threading.Lock()


def _recommender_argv(command: list[str]) -> list[str]:
    """Extract recommender arguments from the canonical module command."""

    expected = [sys.executable, "-m", "where_paper_go.recommender"]
    if command[:3] != expected:
        raise ValueError("检索命令入口无效")
    return command[3:]


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _retrieval_runtime_paths() -> tuple[Path, Path]:
    graph_path = Path(
        os.environ.get("WPG_GRAPH_PATH") or DATA_DIR / "venue_graph.json.gz"
    ).expanduser().resolve()
    lightrag_dir = Path(
        os.environ.get("WPG_LIGHTRAG_WORKING_DIR")
        or DATA_DIR / "lightrag_storage"
    ).expanduser().resolve()
    return graph_path, lightrag_dir


def _environment_path(name: str, fallback: Path | None = None) -> Path | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return fallback.expanduser().resolve() if fallback is not None else None
    return Path(raw).expanduser().resolve()


def _expected_worker_bindings() -> dict[str, str]:
    """Resolve the exact startup contract expected from the child worker."""

    graph_path, lightrag_dir = _retrieval_runtime_paths()
    api_cache = _environment_path(
        recommender.API_CACHE_DIR_ENV, DATA_DIR / ".query_api_cache"
    )
    query_cache = _environment_path(
        recommender.QUERY_EMBEDDING_CACHE_ENV,
        default_query_embedding_cache_path(DATA_DIR),
    )
    lightrag_cache = _environment_path(
        recommender.LIGHTRAG_EMBEDDING_CACHE_ENV,
        default_graph_embedding_cache_path(DATA_DIR),
    )
    assert api_cache is not None and query_cache is not None and lightrag_cache is not None
    return {
        "graph_path": str(graph_path),
        "lightrag_working_dir": str(lightrag_dir),
        "api_cache_dir": str(api_cache),
        "query_embedding_cache": str(query_cache),
        "lightrag_embedding_cache": str(lightrag_cache),
    }


def _configured_result_cache_dir() -> Path:
    return _environment_path("WPG_RESULT_CACHE_DIR", RESULT_CACHE_DIR) or RESULT_CACHE_DIR


def _result_dependency_stamp() -> list[tuple[str, int, int, int, int, int]]:
    """Return filesystem identities for every source affecting retrieval output."""

    graph_path, lightrag_dir = _retrieval_runtime_paths()
    paths = [
        DEFAULT_CONFIG,
        graph_path,
        vector_path_for_graph(graph_path),
        lightrag_dir / "venue_import_manifest.json",
        *(
            lightrag_dir / name for name in lightrag.QUERY_STORAGE_FILES
        ),
        *(DATA_DIR / name for name in recommender.DATA_FILES),
        DATA_DIR / recommender.CURATED_SCOPE_FILE,
    ]
    stamps: list[tuple[str, int, int, int, int, int]] = []
    for path in paths:
        try:
            stat = path.stat()
            stamps.append(
                (
                    str(path.resolve()),
                    stat.st_dev,
                    stat.st_ino,
                    stat.st_mtime_ns,
                    stat.st_ctime_ns,
                    stat.st_size,
                )
            )
        except FileNotFoundError:
            stamps.append((str(path.resolve()), -1, -1, -1, -1, -1))
    return stamps


def _result_cache_path(
    command: list[str],
    dependency_stamp: list[tuple[str, int, int, int, int, int]] | None = None,
) -> Path:
    identity = json.dumps(
        {
            "schema": RESULT_CACHE_SCHEMA_VERSION,
            "argv": command[2:],
            "dependencies": (
                _result_dependency_stamp()
                if dependency_stamp is None
                else dependency_stamp
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return _configured_result_cache_dir() / f"{digest}.json"


def _load_result_cache(
    command: list[str],
    dependency_stamp: list[tuple[str, int, int, int, int, int]] | None = None,
) -> tuple[dict[str, Any], float] | None:
    path = _result_cache_path(command, dependency_stamp)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        created_at = float(record.get("created_at"))
        payload = record.get("payload")
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    age = max(0.0, time.time() - created_at)
    if (
        record.get("schema") != RESULT_CACHE_SCHEMA_VERSION
        or age > RESULT_CACHE_TTL_SECONDS
        or not isinstance(payload, dict)
    ):
        return None
    return dict(payload), age


def _store_result_cache(
    command: list[str],
    payload: dict[str, Any],
    dependency_stamp: list[tuple[str, int, int, int, int, int]] | None = None,
) -> None:
    path = _result_cache_path(command, dependency_stamp)
    cached_payload = dict(payload)
    cached_payload.pop("elapsed_ms", None)
    cached_payload.pop("result_cache", None)
    record = {
        "schema": RESULT_CACHE_SCHEMA_VERSION,
        "created_at": time.time(),
        "payload": cached_payload,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with _RESULT_CACHE_LOCK:
        temporary = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        )
        temporary_path = Path(temporary.name)
        try:
            with temporary:
                json.dump(record, temporary, ensure_ascii=False, separators=(",", ":"))
                temporary.write("\n")
            os.replace(temporary_path, path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise


def _mark_result_cache(
    payload: dict[str, Any], *, hit: bool, age_seconds: float = 0.0
) -> dict[str, Any]:
    result = dict(payload)
    result["result_cache"] = {
        "hit": hit,
        "age_seconds": round(age_seconds, 3),
        "ttl_seconds": RESULT_CACHE_TTL_SECONDS,
        "schema": RESULT_CACHE_SCHEMA_VERSION,
    }
    return result


def _normalise_list(value: Any, limit: int = 20) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:limit]:
        text = recommender.normalize_space(str(item))
        if text and text not in result:
            result.append(text)
    return result


def _configured_secret(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    upper = text.upper()
    return not upper.startswith(("YOUR_", "REPLACE_", "<"))


def _config_status() -> dict[str, Any]:
    result: dict[str, Any] = {
        "file": DEFAULT_CONFIG.name,
        "exists": DEFAULT_CONFIG.exists(),
        "ready": False,
        "llm_provider": None,
        "llm_model": None,
        "embedding_provider": None,
        "embedding_model": None,
        "search_provider": None,
        "search_key_configured": False,
        "search_key_count": 0,
        "search_total_quota": 0,
        "search_quota_audit": {
            "required": False,
            "ready": True,
            "state_revision": None,
            "configuration_current": None,
            "replicated_revision": None,
            "copies": {},
        },
    }
    if not DEFAULT_CONFIG.exists():
        return result
    try:
        root = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        result["parse_error"] = True
        return result
    if not isinstance(root, dict):
        result["parse_error"] = True
        return result
    llm = root.get("llm")
    embedding = root.get("embedding")
    search = root.get("search")
    llm = llm if isinstance(llm, dict) else {}
    embedding = embedding if isinstance(embedding, dict) else {}
    search = search if isinstance(search, dict) else {}
    if isinstance(llm, dict):
        result["llm_provider"] = llm.get("provider", "openai_compatible")
        result["llm_model"] = llm.get("model")
    if isinstance(embedding, dict):
        result["embedding_provider"] = embedding.get(
            "provider", "openai_compatible"
        )
        result["embedding_model"] = embedding.get("model")
    if isinstance(search, dict):
        result["search_provider"] = search.get("provider")
        configured_keys: list[Any] = []
        api_keys = search.get("api_keys")
        if isinstance(api_keys, (list, tuple)):
            configured_keys.extend(api_keys)
        elif api_keys:
            configured_keys.append(api_keys)
        if not configured_keys:
            for name in (
                "api_key",
                "key",
                "api_key2",
                "api_key_2",
                "backup_api_key",
                "fallback_api_key",
            ):
                value = search.get(name)
                if isinstance(value, (list, tuple)):
                    configured_keys.extend(value)
                elif value:
                    configured_keys.append(value)
        unique_keys = {
            str(value).strip()
            for value in configured_keys
            if _configured_secret(value)
        }
        result["search_key_configured"] = bool(unique_keys)
        result["search_key_count"] = len(unique_keys)
        try:
            quota_per_key = max(0, int(search.get("quota_per_key", 0)))
        except (TypeError, ValueError):
            quota_per_key = 0
        result["search_total_quota"] = len(unique_keys) * quota_per_key
        if str(result["search_provider"] or "").strip().lower() == "tavily":
            quota_audit: dict[str, Any] = {
                "required": True,
                "ready": False,
                "state_revision": None,
                "configuration_current": False,
                "replicated_revision": False,
                "copies": {},
            }
            if unique_keys:
                try:
                    snapshot = TavilyKeyPool.from_config(search).audit_snapshot()
                    copies = snapshot.get("copies")
                    copies = copies if isinstance(copies, dict) else {}
                    sanitized_copies: dict[str, dict[str, Any]] = {}
                    for name in ("primary", "backup"):
                        candidate = copies.get(name)
                        candidate = candidate if isinstance(candidate, dict) else {}
                        sanitized_copies[name] = {
                            "present": candidate.get("present") is True,
                            "valid": candidate.get("valid") is True,
                            "revision": candidate.get("revision"),
                            "sha256": candidate.get("sha256"),
                            "bytes": candidate.get("bytes", 0),
                            "mode": candidate.get("mode"),
                        }
                    revision = snapshot.get("state_revision")
                    replicated = bool(
                        isinstance(revision, int)
                        and not isinstance(revision, bool)
                        and all(
                            candidate["present"]
                            and candidate["valid"]
                            and candidate["revision"] == revision
                            for candidate in sanitized_copies.values()
                        )
                        and sanitized_copies.get("primary", {}).get("sha256")
                        == sanitized_copies.get("backup", {}).get("sha256")
                    )
                    configuration_current = (
                        snapshot.get("configuration_current") is True
                    )
                    quota_audit.update(
                        {
                            "ready": replicated and configuration_current,
                            "state_revision": revision,
                            "configuration_current": configuration_current,
                            "replicated_revision": replicated,
                            "copies": sanitized_copies,
                            "used": snapshot.get("used"),
                            "remaining": snapshot.get("remaining"),
                            "total_capacity": snapshot.get("total_capacity"),
                            "status_counts": snapshot.get("status_counts", {}),
                            "configured_keyset_sha256": snapshot.get(
                                "configured_keyset_sha256"
                            ),
                        }
                    )
                except (OSError, ValueError, TavilyKeyPoolError):
                    quota_audit["error"] = "unavailable_or_unsafe"
            result["search_quota_audit"] = quota_audit
    llm_endpoint = bool(
        isinstance(llm, dict)
        and (llm.get("base_url") or llm.get("api_base") or llm.get("endpoint"))
    )
    embedding_endpoint = bool(
        isinstance(embedding, dict)
        and (
            embedding.get("base_url")
            or embedding.get("endpoint")
            or embedding.get("embeddings_url")
            or (isinstance(llm, dict) and llm.get("base_url"))
        )
    )
    result["ready"] = bool(
        result["llm_model"]
        and llm_endpoint
        and result["embedding_model"]
        and embedding_endpoint
        and result["search_provider"]
        and result["search_key_configured"]
        and bool(result["search_quota_audit"].get("ready"))
    )
    return result


def _target_value(dataset: str, level: str) -> str:
    return f"{TARGET_PREFIX.get(dataset, dataset.upper())}-{level}"


def _options_payload() -> dict[str, Any]:
    records = recommender.load_records(DATA_DIR)
    target_counts: Counter[tuple[str, str]] = Counter(
        (record.dataset, record.level) for record in records
    )
    target_names: dict[tuple[str, str], set[str]] = {}
    for record in records:
        target_names.setdefault((record.dataset, record.level), set()).add(
            recommender.normalize_name(record.name)
        )
    target_options = []
    for dataset, level in sorted(
        target_counts,
        key=lambda item: (TARGET_ORDER.get(item[0], 99), item[1]),
    ):
        value = _target_value(dataset, level)
        target_options.append(
            {
                "value": value,
                "label": recommender.ranking_label(dataset, level),
                "dataset": dataset,
                "level": level,
                "count": len(target_names[(dataset, level)]),
            }
        )
    area_counts = Counter(recommender.normalize_space(record.area) for record in records)
    areas = [
        {"value": area, "count": count}
        for area, count in area_counts.most_common(40)
        if area
    ]
    return {
        "targets": target_options,
        "record_types": [
            {"value": "all", "label": "会议 + 期刊"},
            {"value": "conference", "label": "会议"},
            {"value": "journal", "label": "期刊"},
        ],
        "areas": areas,
        "counts": {
            "records": len(records),
            "venues": len({recommender.normalize_name(record.name) for record in records}),
        },
    }


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _runtime_manifest_status(generation: Path | None) -> dict[str, Any]:
    """Read and bind the immutable runtime manifest without exposing its path."""

    raw_path = os.environ.get(RUNTIME_MANIFEST_ENV, "").strip()
    expected_sha256 = os.environ.get(RUNTIME_MANIFEST_SHA256_ENV, "").strip()
    result: dict[str, Any] = {
        "configured": bool(raw_path and expected_sha256),
        "file": Path(raw_path).name if raw_path else None,
        "path_bound": False,
        "private_regular": False,
        "expected_sha256": expected_sha256 or None,
        "actual_sha256": None,
        "sha256_matched": False,
        "content_valid": False,
        "ready": False,
    }
    if (
        generation is None
        or not raw_path
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        return result
    configured_path = Path(raw_path).expanduser()
    if not configured_path.is_absolute():
        return result
    try:
        manifest_path = configured_path.resolve()
        result["path_bound"] = bool(
            manifest_path.parent == generation
            and manifest_path.name == RUNTIME_MANIFEST_FILE
        )
        before_path = configured_path.lstat()
        if (
            stat.S_ISLNK(before_path.st_mode)
            or not stat.S_ISREG(before_path.st_mode)
            or before_path.st_uid != os.geteuid()
            or stat.S_IMODE(before_path.st_mode) != 0o400
            or before_path.st_size > RUNTIME_MANIFEST_MAX_BYTES
        ):
            return result
        result["private_regular"] = True
        descriptor = os.open(
            configured_path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o400
                or before.st_size > RUNTIME_MANIFEST_MAX_BYTES
                or (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                )
                != (
                    before_path.st_dev,
                    before_path.st_ino,
                    before_path.st_size,
                    before_path.st_mtime_ns,
                    before_path.st_ctime_ns,
                )
            ):
                return result
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > RUNTIME_MANIFEST_MAX_BYTES:
                    return result
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
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
        if identity_before != identity_after or size != before.st_size:
            return result
        raw = b"".join(chunks)
        actual_sha256 = hashlib.sha256(raw).hexdigest()
        result["actual_sha256"] = actual_sha256
        result["sha256_matched"] = actual_sha256 == expected_sha256
        manifest = json.loads(raw.decode("utf-8"))
        result["content_valid"] = bool(
            isinstance(manifest, dict)
            and manifest.get("schema_version") == 1
            and manifest.get("artifact_type")
            == "where_papers_go_runtime_shadow"
            and manifest.get("source_data_dir") == str(DATA_DIR.resolve())
            and isinstance(manifest.get("source_binding_sha256"), str)
            and len(manifest.get("source_binding_sha256")) == 64
            and isinstance(manifest.get("files"), list)
            and manifest.get("write_boundary") == "runtime_generation_only"
            and manifest.get("protected_sources_never_replaced") is True
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return result
    result["ready"] = bool(
        result["configured"]
        and result["path_bound"]
        and result["private_regular"]
        and result["sha256_matched"]
        and result["content_valid"]
    )
    return result


def _runtime_status() -> dict[str, Any]:
    graph_path, lightrag_dir = _retrieval_runtime_paths()
    runtime_shadow_required = os.environ.get(
        "WPG_REQUIRE_RUNTIME_SHADOW", ""
    ).strip() == "1"
    strict_graph = os.environ.get("WPG_STRICT_GRAPH_READ_ONLY", "").strip() == "1"
    generation_raw = os.environ.get(RUNTIME_GENERATION_ENV, "").strip()
    generation: Path | None = None
    generation_private = False
    if generation_raw and Path(generation_raw).expanduser().is_absolute():
        try:
            configured_generation = Path(generation_raw).expanduser()
            generation = configured_generation.resolve()
            generation_info = configured_generation.lstat()
            generation_private = bool(
                not stat.S_ISLNK(generation_info.st_mode)
                and stat.S_ISDIR(generation_info.st_mode)
                and generation_info.st_uid == os.geteuid()
                and stat.S_IMODE(generation_info.st_mode) & 0o077 == 0
            )
        except OSError:
            generation = None

    expected = _expected_worker_bindings()
    api_cache = Path(expected["api_cache_dir"])
    query_cache = Path(expected["query_embedding_cache"])
    lightrag_cache = Path(expected["lightrag_embedding_cache"])
    result_cache = _configured_result_cache_dir()
    tavily_state = _environment_path(TAVILY_STATE_FILE_ENV)
    protected = (PROJECT_ROOT.resolve(), DATA_DIR.resolve())

    core_write_paths = {
        "api_cache": api_cache,
        "result_cache": result_cache,
        "query_embedding_cache": query_cache,
        "lightrag_embedding_cache": lightrag_cache,
        "lightrag_working_dir": lightrag_dir,
    }
    generation_bound = {
        name: bool(generation is not None and _is_within(path, generation))
        for name, path in core_write_paths.items()
    }
    protected_source_isolated = {
        name: not any(_is_within(path, root) for root in protected)
        for name, path in core_write_paths.items()
    }
    tavily_shared = bool(
        tavily_state is not None
        and generation is not None
        and not _is_within(tavily_state, generation)
        and not any(_is_within(tavily_state, root) for root in protected)
    )
    result_nested = _is_within(result_cache, api_cache)
    generation_isolated = bool(
        generation is not None
        and not any(_is_within(generation, root) for root in protected)
    )
    write_isolated = bool(
        strict_graph
        and generation_private
        and generation_isolated
        and all(generation_bound.values())
        and all(protected_source_isolated.values())
        and result_nested
        and tavily_shared
    )
    manifest_status = _runtime_manifest_status(generation)
    worker_bindings_match = _SEARCH_RUNTIME.runtime_bindings == expected
    process_ready = _SEARCH_RUNTIME.process_ready
    bindings_current = _SEARCH_RUNTIME.bindings_current
    runtime_contract = bool(
        _SEARCH_RUNTIME.ready
        and (
            not runtime_shadow_required
            or (write_isolated and manifest_status["ready"])
        )
    )
    return {
        "persistent_worker": True,
        "process_ready": process_ready,
        "bindings_current": bindings_current,
        "runtime_shadow_required": runtime_shadow_required,
        "write_isolated": write_isolated,
        "ready": runtime_contract,
        "preload_ms": _SEARCH_RUNTIME.preload_ms,
        "graph_file": graph_path.name,
        "lightrag_directory": lightrag_dir.name,
        "generation": {
            "configured": bool(generation_raw),
            "name": generation.name if generation is not None else None,
            "private": generation_private,
            "outside_protected_sources": generation_isolated,
        },
        "write_bindings": {
            name: {
                "generation_bound": generation_bound[name],
                "outside_protected_sources": protected_source_isolated[name],
            }
            for name in core_write_paths
        },
        "result_cache_within_api_cache": result_nested,
        "tavily_state_shared": tavily_shared,
        "runtime_manifest": manifest_status,
        "worker_bindings": {
            "exact_match": worker_bindings_match,
            "expected_keys": sorted(expected),
            "reported_keys": sorted(_SEARCH_RUNTIME.runtime_bindings),
        },
    }


def _health_payload() -> dict[str, Any]:
    graph_path, lightrag_dir = _retrieval_runtime_paths()
    vector_path = vector_path_for_graph(graph_path)
    lightrag_manifest = lightrag_dir / "venue_import_manifest.json"
    light_info: dict[str, Any] = {}
    if lightrag_manifest.exists():
        try:
            light_info = json.loads(lightrag_manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            light_info = {"parse_error": True}
    counts = light_info.get("counts") if isinstance(light_info, dict) else {}
    config = _config_status()
    runtime = _runtime_status()
    quota_audit = config.get("search_quota_audit")
    quota_audit = quota_audit if isinstance(quota_audit, dict) else {}
    checks = {
        "graph": graph_path.is_file(),
        "vectors": vector_path.is_file(),
        "lightrag_manifest": lightrag_manifest.is_file()
        and not light_info.get("parse_error"),
        "api_config": bool(config.get("ready")),
        "search_quota_audit": bool(
            quota_audit.get("ready")
            if quota_audit.get("required") is True
            else True
        ),
        "worker": bool(runtime.get("process_ready")),
        "bindings_current": bool(runtime.get("bindings_current")),
        "runtime_contract": bool(runtime.get("ready")),
    }
    ready = all(checks.values())
    return {
        "status": "ready" if ready else "incomplete",
        "ready": ready,
        "checks": checks,
        "graph": {"exists": graph_path.is_file(), "file": graph_path.name},
        "vectors": {"exists": vector_path.is_file(), "file": vector_path.name},
        "lightrag": {
            "exists": lightrag_dir.is_dir(),
            "manifest_exists": lightrag_manifest.is_file(),
            "mode": light_info.get("query_mode", "mix"),
            "embedding_model": light_info.get("embedding_model"),
            "dimensions": light_info.get("embedding_dimensions"),
            "counts": counts or {},
            "binding": {
                "source_digest": light_info.get("source_digest"),
                "semantic_digest": light_info.get("semantic_digest"),
                "embedding_provider_fingerprint": light_info.get(
                    "embedding_provider_fingerprint"
                ),
            },
        },
        "config": config,
        "runtime": runtime,
        "backend": "lightrag_mix+property_graph_exact_vector+llm+search_api",
    }


def _public_error_detail(value: Any) -> str:
    return redact_sensitive_text(value, configured_secret_values(DEFAULT_CONFIG))


def _search_command(body: dict[str, Any]) -> list[str]:
    targets = _normalise_list(body.get("targets"), limit=8) or ["CCF-A"]
    query = recommender.normalize_space(str(body.get("query") or ""))
    if not query:
        raise ValueError("请先输入论文题目、摘要或研究主题")
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"主题文本不能超过 {MAX_QUERY_CHARS} 个字符")
    try:
        limit = max(1, min(MAX_LIMIT, int(body.get("limit", 10))))
    except (TypeError, ValueError) as exc:
        raise ValueError("结果数量必须是 1 到 50 的整数") from exc
    record_type = str(body.get("record_type") or "all")
    if record_type not in {"all", "conference", "journal"}:
        raise ValueError("record_type 只能是 all、conference 或 journal")
    cmd = [
        sys.executable,
        "-m",
        "where_paper_go.recommender",
        "--query",
        query,
        "--record-type",
        record_type,
        "--limit",
        str(limit),
        "--format",
        "json",
        "--api-config",
        str(DEFAULT_CONFIG),
    ]
    for target in targets:
        cmd.extend(("--target", target))
    for area in _normalise_list(body.get("areas"), limit=8):
        cmd.extend(("--area", area))
    for scope in _normalise_list(body.get("scopes"), limit=8):
        cmd.extend(("--scope", scope))
    if bool(body.get("reviewed_scope_only")):
        cmd.append("--reviewed-scope-only")
    if bool(body.get("match_official_scope", True)):
        cmd.append("--match-official-scope")
    try:
        api_timeout = max(1, min(120, int(body.get("api_timeout", 20))))
    except (TypeError, ValueError) as exc:
        raise ValueError("Search API 超时必须是 1 到 120 秒的整数") from exc
    cmd.extend(("--api-timeout", str(api_timeout)))
    return cmd


def _run_search(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    command = _search_command(body)
    started = time.perf_counter()
    dependency_stamp = _result_dependency_stamp()
    cached = _load_result_cache(command, dependency_stamp)
    if cached is not None:
        if dependency_stamp != _result_dependency_stamp():
            return HTTPStatus.SERVICE_UNAVAILABLE, {
                "error": "检索依赖在缓存读取期间发生变化",
                "retryable": True,
            }
        payload, age = cached
        payload = _mark_result_cache(payload, hit=True, age_seconds=age)
        payload["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        return HTTPStatus.OK, payload
    try:
        completed = _SEARCH_RUNTIME.run(command, timeout=900)
    except subprocess.TimeoutExpired:
        return HTTPStatus.GATEWAY_TIMEOUT, {
            "error": "检索超时",
            "detail": "完整链路包含 LightRAG、向量、LLM 和 Search API，服务端等待超过 15 分钟。",
        }
    except (BrokenPipeError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        return HTTPStatus.SERVICE_UNAVAILABLE, {
            "error": "检索工作进程不可用",
            "detail": _public_error_detail(exc),
            "retryable": True,
        }
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    if dependency_stamp != _result_dependency_stamp():
        return HTTPStatus.SERVICE_UNAVAILABLE, {
            "error": "检索依赖在请求期间发生变化，已丢弃结果",
            "retryable": True,
        }
    status, payload = _completed_search_payload(completed, elapsed_ms)
    if status == HTTPStatus.OK:
        try:
            _store_result_cache(command, payload, dependency_stamp)
        except OSError:
            pass
        payload = _mark_result_cache(payload, hit=False)
    return status, payload


def _completed_search_payload(
    completed: subprocess.CompletedProcess[str], elapsed_ms: int
) -> tuple[int, dict[str, Any]]:
    if completed.returncode != 0:
        detail = (completed.stderr or "检索进程失败").strip().splitlines()
        detail = _public_error_detail("\n".join(detail[-8:]))
        status = HTTPStatus.SERVICE_UNAVAILABLE if any(
            token in detail for token in ("Search API", "LLM", "embedding", "LightRAG")
        ) else HTTPStatus.UNPROCESSABLE_ENTITY
        return status, {
            "error": "检索未完成",
            "detail": detail,
            "elapsed_ms": elapsed_ms,
            "retryable": status == HTTPStatus.SERVICE_UNAVAILABLE,
        }
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return HTTPStatus.INTERNAL_SERVER_ERROR, {
            "error": "后端返回了无法解析的结果",
            "elapsed_ms": elapsed_ms,
        }
    if not isinstance(payload, dict):
        return HTTPStatus.INTERNAL_SERVER_ERROR, {
            "error": "后端结果格式无效",
            "elapsed_ms": elapsed_ms,
        }
    payload["elapsed_ms"] = elapsed_ms
    return HTTPStatus.OK, payload


def _run_search_stream(body: dict[str, Any], emit: Any) -> None:
    command = _search_command(body)
    started = time.perf_counter()
    emit({"type": "accepted", "elapsed_ms": 0})
    dependency_stamp = _result_dependency_stamp()
    cached = _load_result_cache(command, dependency_stamp)
    if cached is not None:
        if dependency_stamp != _result_dependency_stamp():
            emit(
                {
                    "type": "error",
                    "status": HTTPStatus.SERVICE_UNAVAILABLE,
                    "error": "检索依赖在缓存读取期间发生变化",
                    "retryable": True,
                }
            )
            return
        payload, age = cached
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        payload = _mark_result_cache(payload, hit=True, age_seconds=age)
        payload["elapsed_ms"] = elapsed_ms
        emit(
            {
                "type": "complete",
                "payload": payload,
                "elapsed_ms": elapsed_ms,
                "cached": True,
            }
        )
        return
    try:
        completed = _SEARCH_RUNTIME.stream(command, emit, timeout=900)
    except subprocess.TimeoutExpired:
        emit(
            {
                "type": "error",
                "status": HTTPStatus.GATEWAY_TIMEOUT,
                "error": "检索超时",
                "detail": "完整链路包含 LightRAG、向量、LLM 和 Search API，服务端等待超过 15 分钟。",
            }
        )
        return
    except (BrokenPipeError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        emit(
            {
                "type": "error",
                "status": HTTPStatus.SERVICE_UNAVAILABLE,
                "error": "检索工作进程不可用",
                "detail": _public_error_detail(exc),
                "retryable": True,
            }
        )
        return
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    if dependency_stamp != _result_dependency_stamp():
        emit(
            {
                "type": "error",
                "status": HTTPStatus.SERVICE_UNAVAILABLE,
                "error": "检索依赖在请求期间发生变化，已丢弃结果",
                "retryable": True,
            }
        )
        return
    status, payload = _completed_search_payload(completed, elapsed_ms)
    if status == HTTPStatus.OK:
        try:
            _store_result_cache(command, payload, dependency_stamp)
        except OSError:
            pass
        payload = _mark_result_cache(payload, hit=False)
        emit({"type": "complete", "payload": payload, "elapsed_ms": elapsed_ms})
    else:
        emit({"type": "error", "status": status, **payload})


class VenueHTTPServer(ThreadingHTTPServer):
    """Threaded server with bounded admission for the expensive search path."""

    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        security_config: WebSecurityConfig,
        bind_and_activate: bool = True,
    ) -> None:
        self.security_config = security_config
        if ":" in server_address[0]:
            self.address_family = socket.AF_INET6
        self.rate_limiter = SlidingWindowRateLimiter(
            security_config.rate_limit_requests,
            security_config.rate_limit_window_seconds,
        )
        self.search_slots = threading.BoundedSemaphore(
            security_config.max_concurrent_searches
        )
        self.connection_slots = threading.BoundedSemaphore(
            security_config.max_concurrent_connections
        )
        super().__init__(
            server_address,
            handler_class,
            bind_and_activate=bind_and_activate,
        )

    def verify_request(self, request: Any, client_address: Any) -> bool:
        """Reject disallowed peers before creating a handler thread."""

        del request
        peer = str(client_address[0]) if client_address else "invalid"
        return self.security_config.client_allowed(peer)

    def process_request(self, request: Any, client_address: Any) -> None:
        """Apply a hard bound to all HTTP connection threads, not only searches."""

        if not self.connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connection_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connection_slots.release()


class VenueHandler(BaseHTTPRequestHandler):
    server_version = "where-paper-go/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(
            self.server.security_config.request_read_timeout_seconds
        )

    def version_string(self) -> str:
        return self.server_version

    def handle_expect_100(self) -> bool:
        """Validate perimeter, auth, and framing before accepting a request body."""

        if not self._authorize_network():
            return False
        path = urlparse(self.path).path
        if self.command != "POST" or path not in {"/api/search", "/api/search/stream"}:
            self._send_json(
                HTTPStatus.EXPECTATION_FAILED,
                {"error": "Expect: 100-continue 仅支持检索接口"},
                close_connection=True,
            )
            return False
        if not self._authorize_search():
            return False
        try:
            self._content_length()
        except RequestBodyTooLarge as exc:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": str(exc)},
                close_connection=True,
            )
            return False
        except ValueError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": str(exc)},
                close_connection=True,
            )
            return False
        return super().handle_expect_100()

    def handle_one_request(self) -> None:
        # ``BaseHTTPRequestHandler`` reuses the instance for keep-alive. Clear
        # the preceding request before it attempts to read the next line so an
        # ordinary client disconnect cannot produce a duplicate status=0 audit.
        self.requestline = ""
        self.command = ""
        self.path = ""
        self._request_started = time.monotonic()
        self._request_id = uuid.uuid4().hex
        self._response_status = 0
        self._response_bytes = 0
        self._network_state = "unchecked"
        self._auth_state = "not_applicable"
        self._rate_limited = False
        try:
            super().handle_one_request()
        finally:
            if (
                self.server.security_config.audit_enabled
                and getattr(self, "requestline", "")
            ):
                peer = str(self.client_address[0]) if self.client_address else "unknown"
                identity = client_ip(
                    peer,
                    getattr(self, "headers", {}),
                    self.server.security_config,
                )
                path = urlparse(getattr(self, "path", "")).path or "/"
                record = audit_record(
                    request_id=self._request_id,
                    client_ip=identity,
                    method=getattr(self, "command", ""),
                    path=path,
                    status=self._response_status,
                    response_bytes=self._response_bytes,
                    duration_ms=round(
                        (time.monotonic() - self._request_started) * 1000
                    ),
                    network=self._network_state,
                    auth=self._auth_state,
                    rate_limited=self._rate_limited,
                )
                sys.stderr.write("[audit] " + record + "\n")

    def log_message(self, format: str, *args: Any) -> None:
        # BaseHTTPRequestHandler includes the raw request line.  The structured
        # audit record above deliberately logs only the normalized path.
        return

    def send_response(self, code: int, message: str | None = None) -> None:
        self._response_status = int(code)
        super().send_response(code, message)

    def end_headers(self) -> None:
        self.send_header("X-Request-ID", self._request_id)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; "
            "style-src 'self' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
        )
        super().end_headers()

    def _send_json(
        self,
        status: int,
        payload: Any,
        *,
        headers: Mapping[str, str] | None = None,
        close_connection: bool = False,
    ) -> None:
        body = _json_bytes(payload)
        try:
            if close_connection:
                self.close_connection = True
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if close_connection:
                self.send_header("Connection", "close")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
            self._response_bytes = len(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True
            self._response_bytes = 0

    def _send_file(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or path.suffix in {".js", ".json"}:
            content_type += "; charset=utf-8"
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            self._response_bytes = len(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True
            self._response_bytes = 0

    def _content_length(self) -> int:
        if self.headers.get("Transfer-Encoding") is not None:
            raise ValueError("Transfer-Encoding 不受支持")
        raw_lengths = self.headers.get_all("Content-Length", failobj=[])
        if len(raw_lengths) > 1:
            raise ValueError("Content-Length 不能重复")
        try:
            length = int(raw_lengths[0] if raw_lengths else "0")
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if length < 0:
            raise ValueError("Content-Length 不能为负数")
        if length > self.server.security_config.request_body_limit:
            raise RequestBodyTooLarge("请求体过大")
        return length

    def _read_json_body(self) -> dict[str, Any]:
        length = self._content_length()
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("请求体长度与 Content-Length 不一致")
        body = json.loads(raw or b"{}")
        if not isinstance(body, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return body

    def _authorize_search(self) -> bool:
        security = self.server.security_config
        if not security.api_auth_configured and not security.require_api_auth:
            self._auth_state = "not_configured"
            return True
        if security.authorize(self.headers.get("Authorization")):
            self._auth_state = "accepted"
            return True
        self._auth_state = "rejected"
        self._send_json(
            HTTPStatus.UNAUTHORIZED,
            {"error": "API 认证失败"},
            headers={"WWW-Authenticate": 'Bearer realm="where-papers-go"'},
            close_connection=True,
        )
        return False

    def _authorize_network(self) -> bool:
        """Fail closed on the direct TCP peer before reading a request body."""

        peer = str(self.client_address[0]) if self.client_address else "invalid"
        if self.server.security_config.client_allowed(peer):
            self._network_state = "accepted"
            return True
        self._network_state = "rejected"
        self._send_json(
            HTTPStatus.FORBIDDEN,
            {"error": "客户端网络不在服务许可范围内"},
            close_connection=True,
        )
        return False

    def _admit_search(self) -> bool:
        peer = str(self.client_address[0]) if self.client_address else "unknown"
        identity = client_ip(peer, self.headers, self.server.security_config)
        allowed, retry_after = self.server.rate_limiter.allow(identity)
        if not allowed:
            self._rate_limited = True
            self._send_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": "检索请求过于频繁", "retry_after_seconds": retry_after},
                headers={"Retry-After": str(retry_after)},
                close_connection=True,
            )
            return False
        if not self.server.search_slots.acquire(blocking=False):
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "检索工作进程已满载", "retryable": True},
                headers={"Retry-After": "5"},
                close_connection=True,
            )
            return False
        return True

    def _log_internal_error(self, exc: BaseException) -> None:
        detail = _public_error_detail(exc)
        sys.stderr.write(
            "[web-error] "
            + json.dumps(
                {
                    "request_id": self._request_id,
                    "error_type": type(exc).__name__,
                    "detail": detail,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )

    def _send_search_stream(self, body: dict[str, Any]) -> None:
        # Validate all user-controlled arguments before committing a 200 stream.
        _search_command(body)
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True
            return
        self.close_connection = True
        disconnected = False

        def emit(payload: dict[str, Any]) -> None:
            nonlocal disconnected
            if disconnected:
                return
            try:
                encoded = _json_bytes(payload) + b"\n"
                self.wfile.write(encoded)
                self.wfile.flush()
                self._response_bytes += len(encoded)
            except (BrokenPipeError, ConnectionResetError, OSError):
                disconnected = True

        _run_search_stream(body, emit)

    def do_GET(self) -> None:  # noqa: N802 - stdlib HTTPServer API
        if not self._authorize_network():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/health/live":
            self._send_json(HTTPStatus.OK, {"status": "alive", "alive": True})
            return
        if parsed.path == "/api/health":
            payload = _health_payload()
            status = HTTPStatus.OK if payload["ready"] else HTTPStatus.SERVICE_UNAVAILABLE
            self._send_json(status, payload)
            return
        if parsed.path == "/api/options":
            try:
                self._send_json(HTTPStatus.OK, _options_payload())
            except (OSError, ValueError) as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        path_text = unquote(parsed.path)
        if path_text == "/":
            path_text = "/index.html"
        candidate = (WEB_DIR / path_text.lstrip("/")).resolve()
        if WEB_DIR.resolve() not in candidate.parents and candidate != WEB_DIR.resolve():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._send_file(candidate)

    def do_POST(self) -> None:  # noqa: N802 - stdlib HTTPServer API
        if not self._authorize_network():
            return
        parsed = urlparse(self.path)
        if parsed.path not in {"/api/search", "/api/search/stream"}:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "接口不存在"},
                close_connection=True,
            )
            return
        if not self._authorize_search() or not self._admit_search():
            return
        stream_response_sent = False
        close_response = False
        try:
            body = self._read_json_body()
            if parsed.path == "/api/search/stream":
                # Validate before marking the response committed.  The stream
                # helper validates again immediately before sending its 200.
                _search_command(body)
                stream_response_sent = True
                self._send_search_stream(body)
                return
            status, payload = _run_search(body)
        except TimeoutError:
            close_response = True
            status, payload = HTTPStatus.REQUEST_TIMEOUT, {
                "error": "读取请求体超时"
            }
        except RequestBodyTooLarge as exc:
            close_response = True
            status, payload = HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": str(exc)}
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            close_response = True
            status, payload = HTTPStatus.BAD_REQUEST, {"error": str(exc)}
        except Exception as exc:  # keep the API process alive for one bad request
            close_response = True
            self._log_internal_error(exc)
            status, payload = HTTPStatus.INTERNAL_SERVER_ERROR, {
                "error": "服务端异常",
                "request_id": self._request_id,
            }
        finally:
            self.server.search_slots.release()
        if not stream_response_sent:
            self._send_json(status, payload, close_connection=close_response)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Where Papers Go venue recommendation web app")
    parser.add_argument("--host", default=os.environ.get("WPG_HOST", "127.0.0.1"))
    try:
        default_port = int(os.environ.get("WPG_PORT", "8000"))
    except ValueError:
        default_port = -1
    parser.add_argument("--port", type=int, default=default_port)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65_535:
        parser.error("--port/WPG_PORT 必须是 1 到 65535")
    if not WEB_DIR.exists():
        parser.error(f"缺少前端目录：{WEB_DIR}")
    try:
        security_config = WebSecurityConfig.from_environment()
    except ValueError as exc:
        parser.error(str(exc))
    server = VenueHTTPServer(
        (args.host, args.port),
        VenueHandler,
        security_config,
        bind_and_activate=False,
    )
    try:
        # Reserve the address before the expensive preload, but do not listen:
        # startup probes must receive connection-refused instead of queuing and
        # timing out only to trigger BrokenPipe traces once preload completes.
        server.server_bind()
        _SEARCH_RUNTIME.start()
        server.server_activate()
    except (OSError, RuntimeError) as exc:
        server.server_close()
        parser.error(str(exc))
    print(
        f"Where Papers Go running at http://{args.host}:{args.port} "
        f"(retrieval preload {_SEARCH_RUNTIME.preload_ms} ms; "
        f"api_auth={'enabled' if security_config.api_auth_configured else 'disabled'}; "
        f"allowed_client_networks={len(security_config.allowed_client_cidrs)}; "
        f"rate_limit={security_config.rate_limit_requests}/"
        f"{security_config.rate_limit_window_seconds}s)",
        flush=True,
    )
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        print(
            "WARNING: non-loopback socket; direct peers outside "
            "WPG_ALLOWED_CLIENT_CIDRS are rejected. Keep the allowlist restricted "
            "to a trusted LAN until the documented HTTPS/auth proxy is active.",
            file=sys.stderr,
            flush=True,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        _SEARCH_RUNTIME.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
