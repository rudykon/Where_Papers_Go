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
import subprocess
import sys
import threading
import tempfile
import time
from typing import Any, Mapping
import uuid
from urllib.parse import unquote, urlparse

from . import recommender
from .paths import DATA_DIR, DEFAULT_CONFIG_PATH, PROJECT_ROOT, WEB_DIR
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
RESULT_CACHE_DIR = DATA_DIR / ".query_api_cache" / "result"
RESULT_CACHE_SCHEMA_VERSION = "2"
RESULT_CACHE_TTL_SECONDS = 6 * 60 * 60
MAX_QUERY_CHARS = 8_000
MAX_LIMIT = 50
TARGET_ORDER = {"ccf": 0, "th_cpl": 1, "cas": 2, "jcr": 3}
TARGET_PREFIX = {"ccf": "CCF", "th_cpl": "TH-CPL", "cas": "CAS", "jcr": "JCR"}


class RetrievalWorkerManager:
    """Own one preloaded worker and coalesce identical concurrent searches."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None
        self._dependency_stamp: list[tuple[str, int, int]] | None = None
        self._transport_lock = threading.RLock()
        self._inflight_lock = threading.Lock()
        self._inflight: dict[str, Future[subprocess.CompletedProcess[str]]] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="retrieval-worker"
        )
        self.preload_ms: int | None = None

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
                self._dependency_stamp = dependency_after
                self.preload_ms = int(ready.get("preload_ms") or 0)
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


def _result_dependency_stamp() -> list[tuple[str, int, int]]:
    """Return cheap versions for every source that can change retrieval output."""

    paths = [
        DEFAULT_CONFIG,
        DATA_DIR / "venue_graph.json.gz",
        DATA_DIR / "venue_graph_vectors.json.gz",
        DATA_DIR / "lightrag_storage" / "venue_import_manifest.json",
        *(DATA_DIR / name for name in recommender.DATA_FILES),
        DATA_DIR / recommender.CURATED_SCOPE_FILE,
    ]
    stamps: list[tuple[str, int, int]] = []
    for path in paths:
        try:
            stat = path.stat()
            stamps.append((str(path.resolve()), stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            stamps.append((str(path.resolve()), -1, -1))
    return stamps


def _result_cache_path(command: list[str]) -> Path:
    identity = json.dumps(
        {
            "schema": RESULT_CACHE_SCHEMA_VERSION,
            "argv": command[2:],
            "dependencies": _result_dependency_stamp(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return RESULT_CACHE_DIR / f"{digest}.json"


def _load_result_cache(command: list[str]) -> tuple[dict[str, Any], float] | None:
    path = _result_cache_path(command)
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


def _store_result_cache(command: list[str], payload: dict[str, Any]) -> None:
    path = _result_cache_path(command)
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
    }
    if not DEFAULT_CONFIG.exists():
        return result
    try:
        root = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        result["parse_error"] = True
        return result
    llm = root.get("llm") if isinstance(root, dict) else {}
    embedding = root.get("embedding") if isinstance(root, dict) else {}
    search = root.get("search") if isinstance(root, dict) else {}
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


def _runtime_status() -> dict[str, Any]:
    return {
        "persistent_worker": True,
        "process_ready": _SEARCH_RUNTIME.process_ready,
        "bindings_current": _SEARCH_RUNTIME.bindings_current,
        "ready": _SEARCH_RUNTIME.ready,
        "preload_ms": _SEARCH_RUNTIME.preload_ms,
    }


def _health_payload() -> dict[str, Any]:
    graph_path = DATA_DIR / "venue_graph.json.gz"
    vector_path = DATA_DIR / "venue_graph_vectors.json.gz"
    lightrag_dir = DATA_DIR / "lightrag_storage"
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
    checks = {
        "graph": graph_path.is_file(),
        "vectors": vector_path.is_file(),
        "lightrag_manifest": lightrag_manifest.is_file()
        and not light_info.get("parse_error"),
        "api_config": bool(config.get("ready")),
        "worker": bool(runtime.get("process_ready")),
        "bindings_current": bool(runtime.get("bindings_current")),
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
    cached = _load_result_cache(command)
    if cached is not None:
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
    status, payload = _completed_search_payload(completed, elapsed_ms)
    if status == HTTPStatus.OK:
        try:
            _store_result_cache(command, payload)
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
    cached = _load_result_cache(command)
    if cached is not None:
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
    status, payload = _completed_search_payload(completed, elapsed_ms)
    if status == HTTPStatus.OK:
        try:
            _store_result_cache(command, payload)
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
    ) -> None:
        self.security_config = security_config
        self.rate_limiter = SlidingWindowRateLimiter(
            security_config.rate_limit_requests,
            security_config.rate_limit_window_seconds,
        )
        self.search_slots = threading.BoundedSemaphore(
            security_config.max_concurrent_searches
        )
        super().__init__(server_address, handler_class)


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
    ) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)
        self._response_bytes = len(body)

    def _send_file(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or path.suffix in {".js", ".json"}:
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)
        self._response_bytes = len(body)

    def _read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if length < 0:
            raise ValueError("Content-Length 不能为负数")
        if length > self.server.security_config.request_body_limit:
            raise ValueError("请求体过大")
        body = json.loads(self.rfile.read(length) or b"{}")
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
            )
            return False
        if not self.server.search_slots.acquire(blocking=False):
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "检索工作进程已满载", "retryable": True},
                headers={"Retry-After": "5"},
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
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
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
        parsed = urlparse(self.path)
        if parsed.path not in {"/api/search", "/api/search/stream"}:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
            return
        if not self._authorize_search() or not self._admit_search():
            return
        stream_response_sent = False
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
            status, payload = HTTPStatus.REQUEST_TIMEOUT, {
                "error": "读取请求体超时"
            }
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            status, payload = HTTPStatus.BAD_REQUEST, {"error": str(exc)}
        except Exception as exc:  # keep the API process alive for one bad request
            self._log_internal_error(exc)
            status, payload = HTTPStatus.INTERNAL_SERVER_ERROR, {
                "error": "服务端异常",
                "request_id": self._request_id,
            }
        finally:
            self.server.search_slots.release()
        if not stream_response_sent:
            self._send_json(status, payload)


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
    server = VenueHTTPServer((args.host, args.port), VenueHandler, security_config)
    try:
        _SEARCH_RUNTIME.start()
    except RuntimeError as exc:
        server.server_close()
        parser.error(str(exc))
    print(
        f"Where Papers Go running at http://{args.host}:{args.port} "
        f"(retrieval preload {_SEARCH_RUNTIME.preload_ms} ms; "
        f"api_auth={'enabled' if security_config.api_auth_configured else 'disabled'}; "
        f"rate_limit={security_config.rate_limit_requests}/"
        f"{security_config.rate_limit_window_seconds}s)",
        flush=True,
    )
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        print(
            "WARNING: non-loopback listener; use only on a trusted LAN until the "
            "documented HTTPS/auth reverse proxy is active.",
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
