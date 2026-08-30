#!/usr/bin/env python3
"""Mandatory LightRAG runtime for venue graph retrieval.

The deterministic property graph remains responsible for ranking/type hard
constraints.  LightRAG is the semantic graph+vector recall engine and always
runs in ``mix`` mode for topical queries.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import http.client
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
from typing import Any, Mapping, Sequence
import urllib.error

from .enrichment import api_headers, http_request, llm_config
from .api_assistant import ApiAssistantError, load_api_assistant_config
from .embeddings import (
    EmbeddingConfigurationError,
    EmbeddingError,
    FileEmbeddingCache,
    OpenAICompatibleEmbeddingProvider,
    default_graph_embedding_cache_path,
    ensure_cached_embeddings,
    load_embedding_config,
    unpack_float32,
)
from .graph_index import GraphIndexError, VenueGraphIndex


MANIFEST_SCHEMA_VERSION = "1"
MANIFEST_FILE = "venue_import_manifest.json"
# The working directory is already dedicated to this index.  Pin an empty
# workspace so all supported LightRAG versions use the same on-disk paths.
LIGHTRAG_WORKSPACE = ""
VENUE_ID_RE = re.compile(r"(?i)VENUE::(\d+)::")
IMPORT_EVENT_LOOP_HEARTBEAT_SECONDS = 0.25
_LIGHTRAG_LOGGING_LOCK = threading.Lock()


class LightRAGRuntimeError(RuntimeError):
    """Raised when mandatory LightRAG storage or retrieval is unavailable."""


def _configure_lightrag_logging() -> None:
    """Keep third-party LightRAG query content out of production logs.

    LightRAG 1.5.6 logs raw node, edge, and vector queries at ``INFO`` on the
    process-global ``lightrag`` logger.  A per-request context manager would
    race with concurrent requests, so enforce a process-level floor instead.
    Existing namespace loggers and handlers are covered because propagated
    child records are filtered by handler level, while warnings and errors
    remain observable.
    """

    with _LIGHTRAG_LOGGING_LOCK:
        logger = logging.getLogger("lightrag")
        logger.setLevel(logging.WARNING)
        logger.propagate = False
        for handler in logger.handlers:
            handler.setLevel(logging.WARNING)


class _UnicodeCodepointTokenizer:
    """Offline, deterministic tokenizer implementing LightRAG's interface.

    One Unicode code point is counted as one token.  This is conservative for
    English text and avoids a first-run network fetch of tiktoken BPE assets.
    """

    def encode(self, content: str) -> list[int]:
        return [ord(character) for character in content]

    def decode(self, tokens: list[int]) -> str:
        return "".join(chr(int(token)) for token in tokens)

    def __deepcopy__(self, _memo):
        return self


@dataclass(frozen=True)
class LightRAGRecall:
    entity_ids: tuple[int, ...]
    scores: Mapping[int, float]
    channels: Mapping[int, tuple[str, ...]]
    entity_count: int
    relationship_count: int
    chunk_count: int
    query_mode: str = "mix"

    def to_info(self) -> dict[str, object]:
        return {
            "enabled": True,
            "status": "ok",
            "mode": self.query_mode,
            "recalled_venue_count": len(self.entity_ids),
            "entity_count": self.entity_count,
            "relationship_count": self.relationship_count,
            "chunk_count": self.chunk_count,
        }


def default_lightrag_working_dir(data_dir: Path) -> Path:
    return data_dir / "lightrag_storage"


def manifest_path(working_dir: Path) -> Path:
    return working_dir / MANIFEST_FILE


# LightRAG mix queries read every one of these stores.  Keep this list shared
# with the web worker/cache binding so a partial atomic index switch cannot be
# mistaken for the manifest-bound workspace that the worker preloaded.
QUERY_STORAGE_FILES = (
    "graph_chunk_entity_relation.graphml",
    "vdb_entities.json",
    "vdb_relationships.json",
    "vdb_chunks.json",
    "kv_store_text_chunks.json",
)


def _load_manifest(working_dir: Path) -> dict[str, Any]:
    path = manifest_path(working_dir)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LightRAGRuntimeError(
            "LightRAG 知识库尚未构建；请先运行 python3 -m scripts.prepare_retrieval"
        ) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LightRAGRuntimeError(f"LightRAG 导入清单无法读取：{path}") from exc
    if not isinstance(value, dict):
        raise LightRAGRuntimeError("LightRAG 导入清单格式无效")
    return value


def validate_lightrag_workspace(
    working_dir: Path,
    graph_path: Path,
    provider_fingerprint: str,
) -> dict[str, Any]:
    """Fail closed unless the LightRAG workspace exactly matches graph+model."""

    manifest = _load_manifest(working_dir)
    try:
        with VenueGraphIndex(graph_path) as graph:
            metadata = graph.metadata()
    except (GraphIndexError, OSError, ValueError) as exc:
        raise LightRAGRuntimeError(f"无法校验 LightRAG 的源图谱：{exc}") from exc

    expected = {
        "manifest_schema": MANIFEST_SCHEMA_VERSION,
        "source_digest": str(metadata.get("source_digest") or ""),
        "semantic_digest": str(metadata.get("semantic_digest") or ""),
        "embedding_provider_fingerprint": provider_fingerprint,
        "query_mode": "mix",
    }
    mismatches = [
        key for key, value in expected.items() if str(manifest.get(key) or "") != value
    ]
    if mismatches:
        raise LightRAGRuntimeError(
            "LightRAG 知识库已过期或模型不一致（"
            + "、".join(mismatches)
            + "）；请运行 python3 -m scripts.prepare_retrieval --force"
        )
    missing = [
        name for name in QUERY_STORAGE_FILES if not (working_dir / name).is_file()
    ]
    if missing:
        raise LightRAGRuntimeError(
            "LightRAG 存储不完整，缺少：" + "、".join(missing)
        )
    return manifest


class _CachedEmbeddingAdapter:
    """Share the database-free embedding cache with the exact vector index."""

    def __init__(
        self,
        provider: OpenAICompatibleEmbeddingProvider,
        cache_path: Path,
    ) -> None:
        self.provider = provider
        self.cache_path = cache_path
        self._lock = threading.Lock()
        self._cache = FileEmbeddingCache(cache_path)
        self._new_since_flush = 0

    def embed(self, texts: Sequence[str]):
        try:
            import numpy as np
        except ImportError as exc:  # pragma: no cover - LightRAG installs NumPy
            raise LightRAGRuntimeError("LightRAG 缺少 NumPy 依赖") from exc
        prepared = [self.provider.prepare_text(text) for text in texts]
        hashes = [hashlib.sha256(text.encode("utf-8")).hexdigest() for text in prepared]
        unique = dict(zip(hashes, prepared))
        with self._lock:
            _dimensions, embedded_count, _cached_count = ensure_cached_embeddings(
                self.provider, unique, self._cache
            )
            self._new_since_flush += embedded_count
            cached = self._cache.get_many(self.provider.fingerprint, hashes)
            # Persist progress occasionally without rewriting a growing gzip
            # file for every LightRAG batch.
            if self._new_since_flush >= 4096:
                self._cache.close()
                self._new_since_flush = 0
        vectors = [
            unpack_float32(cached[text_hash][1], cached[text_hash][0])
            for text_hash in hashes
        ]
        return np.asarray(vectors, dtype=np.float32)

    def close(self) -> None:
        with self._lock:
            self._cache.close()
            self._new_since_flush = 0


class _OpenAICompatibleLightRAGLLM:
    """Small dependency-free LightRAG LLM binding using the project config."""

    def __init__(self, root_config: Mapping[str, Any]) -> None:
        self.config = dict(llm_config(dict(root_config)))
        base_url = str(
            self.config.get("base_url")
            or self.config.get("api_base")
            or self.config.get("endpoint")
            or ""
        ).strip()
        self.model = str(self.config.get("model") or "").strip()
        if not base_url or not self.model:
            raise LightRAGRuntimeError("LightRAG LLM 必须配置 base_url 和 model")
        self.endpoint = str(self.config.get("chat_completions_url") or "").strip()
        if not self.endpoint:
            self.endpoint = base_url.rstrip("/") + "/chat/completions"
        self.timeout = int(self.config.get("timeout", 60))
        self.max_retries = int(self.config.get("max_retries", 1))

    async def __call__(
        self,
        prompt: str,
        system_prompt: str | None = None,
        history_messages: list[dict[str, str]] | None = None,
        **kwargs: Any,
    ) -> str:
        return await asyncio.to_thread(
            self._complete,
            prompt,
            system_prompt,
            history_messages or [],
            kwargs,
        )

    def _complete(
        self,
        prompt: str,
        system_prompt: str | None,
        history_messages: Sequence[Mapping[str, str]],
        runtime_kwargs: Mapping[str, Any],
    ) -> str:
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.extend(
            {
                "role": str(item.get("role") or "user"),
                "content": str(item.get("content") or ""),
            }
            for item in history_messages
        )
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, Any] = {
            **dict(self.config.get("extra_body") or {}),
            "model": self.model,
            "messages": messages,
            "temperature": runtime_kwargs.get(
                "temperature", self.config.get("temperature", 0)
            ),
        }
        for key in ("max_tokens", "max_completion_tokens"):
            value = runtime_kwargs.get(key, self.config.get(key))
            if value is not None:
                payload[key] = int(value)
        headers = api_headers(self.config)
        headers["User-Agent"] = "venue-recommender-lightrag/1.0"
        last_error: BaseException | None = None
        for attempt in range(self.max_retries + 1):
            try:
                _status, _response_headers, content = http_request(
                    self.endpoint,
                    method="POST",
                    headers=headers,
                    body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    timeout=self.timeout,
                    max_bytes=8_000_000,
                )
                response = json.loads(content.decode("utf-8"))
                message = response.get("choices", [{}])[0].get("message", {})
                value = message.get("content", "")
                if isinstance(value, str):
                    return value
                if isinstance(value, list):
                    return "\n".join(
                        str(item.get("text") or "")
                        for item in value
                        if isinstance(item, Mapping)
                    )
                raise ValueError("LLM response content is invalid")
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code < 500 and exc.code != 429:
                    break
            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
                http.client.HTTPException,
                UnicodeError,
                json.JSONDecodeError,
                ValueError,
                IndexError,
                TypeError,
            ) as exc:
                last_error = exc
            if attempt < self.max_retries:
                time.sleep(min(4.0, 2**attempt))
        if isinstance(last_error, urllib.error.HTTPError):
            detail = f"HTTP {last_error.code}"
        else:
            detail = type(last_error).__name__ if last_error else "unknown error"
        raise LightRAGRuntimeError(f"LightRAG LLM 请求失败：{detail}") from last_error


def _runtime_components(
    working_dir: Path,
    config_path: Path | None,
    embedding_cache: Path | None,
):
    try:
        from lightrag import LightRAG
        from lightrag.utils import Tokenizer, wrap_embedding_func_with_attrs
    except ImportError as exc:
        raise LightRAGRuntimeError(
            "未安装 LightRAG；请运行 python3 -m pip install -e ."
        ) from exc
    _configure_lightrag_logging()

    try:
        root = load_api_assistant_config(config_path)
        embedding_config = load_embedding_config(config_path)
    except (ApiAssistantError, EmbeddingConfigurationError) as exc:
        raise LightRAGRuntimeError(str(exc)) from exc
    if embedding_config.dimensions is None:
        raise LightRAGRuntimeError("LightRAG 必须配置 embedding.dimensions")
    provider = OpenAICompatibleEmbeddingProvider(embedding_config)
    cache_path = embedding_cache or default_graph_embedding_cache_path(
        working_dir.parent
    )
    adapter = _CachedEmbeddingAdapter(provider, cache_path)

    @wrap_embedding_func_with_attrs(
        embedding_dim=embedding_config.dimensions,
        max_token_size=max(128, embedding_config.max_chars // 4),
        model_name=embedding_config.model,
    )
    async def embedding_func(texts: list[str]):
        # LightRAG keeps embedding worker coroutines alive for an instance.
        # Running a nested ``to_thread`` here leaves asyncio's default executor
        # waiting during one-shot CLI shutdown on some Python/LightRAG versions.
        # Retrieval quality is the priority for this project, so serialize the
        # blocking provider call directly in the dedicated LightRAG event loop.
        return adapter.embed(texts)

    llm = _OpenAICompatibleLightRAGLLM(root)
    working_dir.mkdir(parents=True, exist_ok=True)
    rag = LightRAG(
        working_dir=str(working_dir.resolve()),
        workspace=LIGHTRAG_WORKSPACE,
        kv_storage="JsonKVStorage",
        vector_storage="NanoVectorDBStorage",
        graph_storage="NetworkXStorage",
        doc_status_storage="JsonDocStatusStorage",
        llm_model_func=llm,
        llm_model_name=llm.model,
        llm_model_max_async=1,
        tokenizer=Tokenizer(
            model_name="unicode-codepoint-v1",
            tokenizer=_UnicodeCodepointTokenizer(),
        ),
        embedding_func=embedding_func,
        embedding_batch_num=embedding_config.batch_size,
        embedding_func_max_async=1,
        addon_params={"language": "Chinese"},
    )
    return rag, provider, llm, adapter


def _finalize_lightrag_shared_state() -> None:
    """Reset LightRAG's process-global manager after a one-shot CLI call."""

    try:
        from lightrag.kg.shared_storage import finalize_share_data
    except ImportError:
        return
    finalize_share_data()


async def _import_event_loop_heartbeat() -> None:
    """Keep Python 3.14 responsive to LightRAG executor completions.

    LightRAG 1.5.6's chunking executor can finish its concurrent future
    without waking an otherwise idle Python 3.14 event loop.  A short timer
    keeps the loop polling until the one-shot custom-KG import completes.
    """

    while True:
        await asyncio.sleep(IMPORT_EVENT_LOOP_HEARTBEAT_SECONDS)


async def _import_async(
    custom_kg: dict[str, list[dict[str, Any]]],
    working_dir: Path,
    config_path: Path | None,
    embedding_cache: Path | None,
) -> tuple[str, str]:
    rag, provider, llm, adapter = _runtime_components(
        working_dir, config_path, embedding_cache
    )
    await rag.initialize_storages()
    heartbeat = asyncio.create_task(
        _import_event_loop_heartbeat(), name="lightrag-import-heartbeat"
    )
    try:
        await rag.ainsert_custom_kg(custom_kg)
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass
        try:
            await rag.finalize_storages()
        finally:
            adapter.close()
    return provider.fingerprint, llm.model


def import_lightrag_graph(
    graph_path: Path,
    working_dir: Path,
    config_path: Path | None = None,
    embedding_cache: Path | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Build a complete LightRAG workspace and swap it in after success."""

    graph_path = graph_path.resolve()
    working_dir = working_dir.resolve()
    try:
        embedding_config = load_embedding_config(config_path)
        provider = OpenAICompatibleEmbeddingProvider(embedding_config)
        with VenueGraphIndex(graph_path) as graph:
            graph.validate()
            metadata = graph.metadata()
            custom_kg = graph.to_lightrag_custom_kg()
    except (EmbeddingError, GraphIndexError, OSError, ValueError) as exc:
        raise LightRAGRuntimeError(f"LightRAG 导入前置校验失败：{exc}") from exc

    if working_dir.exists() and not force:
        try:
            return validate_lightrag_workspace(
                working_dir, graph_path, provider.fingerprint
            )
        except LightRAGRuntimeError as exc:
            raise LightRAGRuntimeError(f"{exc}；如需重建请加 --force") from exc

    working_dir.parent.mkdir(parents=True, exist_ok=True)
    build_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{working_dir.name}.building-", dir=working_dir.parent
        )
    )
    try:
        try:
            provider_fingerprint, llm_model = asyncio.run(
                _import_async(custom_kg, build_dir, config_path, embedding_cache)
            )
        finally:
            _finalize_lightrag_shared_state()
        counts = {key: len(value) for key, value in custom_kg.items()}
        manifest: dict[str, Any] = {
            "manifest_schema": MANIFEST_SCHEMA_VERSION,
            "source_digest": str(metadata.get("source_digest") or ""),
            "semantic_digest": str(metadata.get("semantic_digest") or ""),
            "graph": str(graph_path),
            "working_dir": str(working_dir),
            "embedding_provider_fingerprint": provider_fingerprint,
            "embedding_model": embedding_config.model,
            "embedding_dimensions": embedding_config.dimensions,
            "llm_model": llm_model,
            "query_mode": "mix",
            "storages": {
                "kv": "JsonKVStorage",
                "vector": "NanoVectorDBStorage",
                "graph": "NetworkXStorage",
                "doc_status": "JsonDocStatusStorage",
            },
            "counts": counts,
            "imported_at": datetime.now(timezone.utc).isoformat(),
        }
        temporary_manifest = build_dir / f".{MANIFEST_FILE}.tmp"
        temporary_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_manifest, build_dir / MANIFEST_FILE)

        backup: Path | None = None
        if working_dir.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = working_dir.with_name(f"{working_dir.name}.backup-{stamp}")
            suffix = 1
            while backup.exists():
                backup = working_dir.with_name(
                    f"{working_dir.name}.backup-{stamp}-{suffix}"
                )
                suffix += 1
            os.replace(working_dir, backup)
            manifest["previous_workspace_backup"] = str(backup)
        try:
            os.replace(build_dir, working_dir)
        except BaseException:
            if backup is not None and backup.exists() and not working_dir.exists():
                os.replace(backup, working_dir)
            raise
        return manifest
    except BaseException:
        if build_dir.exists():
            shutil.rmtree(build_dir)
        raise


async def _query_async(
    query: str,
    working_dir: Path,
    config_path: Path | None,
    embedding_cache: Path | None,
    high_level_keywords: Sequence[str],
    low_level_keywords: Sequence[str],
    top_k: int,
    chunk_top_k: int,
) -> tuple[dict[str, Any], str]:
    try:
        from lightrag import QueryParam
    except ImportError as exc:
        raise LightRAGRuntimeError("LightRAG 未安装") from exc
    rag, provider, _llm, adapter = _runtime_components(
        working_dir, config_path, embedding_cache
    )
    high_keywords = list(
        dict.fromkeys(
            str(value).strip() for value in high_level_keywords if str(value).strip()
        )
    )
    low_keywords = list(
        dict.fromkeys(
            str(value).strip() for value in low_level_keywords if str(value).strip()
        )
    )
    if not high_keywords and not low_keywords:
        low_keywords = [query]
    await rag.initialize_storages()
    try:
        _configure_lightrag_logging()
        result = await rag.aquery_data(
            query,
            QueryParam(
                mode="mix",
                top_k=top_k,
                chunk_top_k=chunk_top_k,
                hl_keywords=high_keywords,
                ll_keywords=low_keywords,
                enable_rerank=False,
            ),
        )
    finally:
        try:
            await rag.finalize_storages()
        finally:
            adapter.close()
    return result, provider.fingerprint


class _PersistentLightRAGRuntime:
    """One initialized LightRAG instance owned by the long-lived web worker."""

    def __init__(
        self,
        working_dir: Path,
        graph_path: Path,
        config_path: Path | None,
        embedding_cache: Path | None,
    ) -> None:
        self.loop = asyncio.new_event_loop()
        self.rag = None
        self.adapter = None
        try:
            asyncio.set_event_loop(self.loop)
            embedding_config = load_embedding_config(config_path)
            expected_provider = OpenAICompatibleEmbeddingProvider(embedding_config)
            validate_lightrag_workspace(
                working_dir, graph_path, expected_provider.fingerprint
            )
            self.rag, provider, _llm, self.adapter = _runtime_components(
                working_dir, config_path, embedding_cache
            )
            if provider.fingerprint != expected_provider.fingerprint:
                raise LightRAGRuntimeError(
                    "LightRAG 运行时 embedding 指纹发生变化"
                )
            self.provider_fingerprint = provider.fingerprint
            self.loop.run_until_complete(self.rag.initialize_storages())
        except BaseException:
            self.close()
            raise

    async def _query(
        self,
        query: str,
        high_level_keywords: Sequence[str],
        low_level_keywords: Sequence[str],
        top_k: int,
        chunk_top_k: int,
    ) -> dict[str, Any]:
        try:
            from lightrag import QueryParam
        except ImportError as exc:
            raise LightRAGRuntimeError("LightRAG 未安装") from exc
        high_keywords = list(
            dict.fromkeys(
                str(value).strip()
                for value in high_level_keywords
                if str(value).strip()
            )
        )
        low_keywords = list(
            dict.fromkeys(
                str(value).strip()
                for value in low_level_keywords
                if str(value).strip()
            )
        )
        if not high_keywords and not low_keywords:
            low_keywords = [query]
        assert self.rag is not None
        _configure_lightrag_logging()
        return await self.rag.aquery_data(
            query,
            QueryParam(
                mode="mix",
                top_k=top_k,
                chunk_top_k=chunk_top_k,
                hl_keywords=high_keywords,
                ll_keywords=low_keywords,
                enable_rerank=False,
            ),
        )

    def query(
        self,
        query: str,
        high_level_keywords: Sequence[str],
        low_level_keywords: Sequence[str],
        top_k: int,
        chunk_top_k: int,
    ) -> dict[str, Any]:
        asyncio.set_event_loop(self.loop)
        return self.loop.run_until_complete(
            self._query(
                query,
                high_level_keywords,
                low_level_keywords,
                top_k,
                chunk_top_k,
            )
        )

    def close(self) -> None:
        rag, adapter = getattr(self, "rag", None), getattr(self, "adapter", None)
        loop = getattr(self, "loop", None)
        try:
            if rag is not None and loop is not None and not loop.is_closed():
                asyncio.set_event_loop(loop)
                loop.run_until_complete(rag.finalize_storages())
        finally:
            if adapter is not None:
                adapter.close()
            self.rag = None
            self.adapter = None
            _finalize_lightrag_shared_state()
            if loop is not None and not loop.is_closed():
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                loop.close()
            asyncio.set_event_loop(None)


_PERSISTENT_RUNTIME_ENABLED = False
_PERSISTENT_RUNTIME_LOCK = threading.RLock()
_PERSISTENT_RUNTIME_KEY: tuple[object, ...] | None = None
_PERSISTENT_RUNTIME: _PersistentLightRAGRuntime | None = None


def _path_stamp(path: Path | None) -> tuple[str, int, int, int, int, int] | None:
    if path is None:
        return None
    resolved = path.resolve()
    try:
        stat = resolved.stat()
        return (
            str(resolved),
            stat.st_dev,
            stat.st_ino,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
            stat.st_size,
        )
    except FileNotFoundError:
        return str(resolved), -1, -1, -1, -1, -1


def _persistent_key(
    working_dir: Path,
    graph_path: Path,
    config_path: Path | None,
    embedding_cache: Path | None,
) -> tuple[object, ...]:
    return (
        str(working_dir.resolve()),
        _path_stamp(graph_path),
        _path_stamp(manifest_path(working_dir)),
        *(_path_stamp(working_dir / name) for name in QUERY_STORAGE_FILES),
        _path_stamp(config_path),
        str(embedding_cache.resolve()) if embedding_cache is not None else None,
    )


def enable_persistent_runtime() -> None:
    """Keep immutable LightRAG stores resident for repeated web searches."""

    global _PERSISTENT_RUNTIME_ENABLED
    _PERSISTENT_RUNTIME_ENABLED = True


def close_persistent_runtime() -> None:
    global _PERSISTENT_RUNTIME, _PERSISTENT_RUNTIME_KEY
    with _PERSISTENT_RUNTIME_LOCK:
        if _PERSISTENT_RUNTIME is not None:
            _PERSISTENT_RUNTIME.close()
        _PERSISTENT_RUNTIME = None
        _PERSISTENT_RUNTIME_KEY = None


def disable_persistent_runtime() -> None:
    global _PERSISTENT_RUNTIME_ENABLED
    close_persistent_runtime()
    _PERSISTENT_RUNTIME_ENABLED = False


def _get_persistent_runtime(
    working_dir: Path,
    graph_path: Path,
    config_path: Path | None,
    embedding_cache: Path | None,
) -> _PersistentLightRAGRuntime:
    global _PERSISTENT_RUNTIME, _PERSISTENT_RUNTIME_KEY
    key = _persistent_key(working_dir, graph_path, config_path, embedding_cache)
    with _PERSISTENT_RUNTIME_LOCK:
        if _PERSISTENT_RUNTIME is None or _PERSISTENT_RUNTIME_KEY != key:
            close_persistent_runtime()
            _PERSISTENT_RUNTIME = _PersistentLightRAGRuntime(
                working_dir, graph_path, config_path, embedding_cache
            )
            _PERSISTENT_RUNTIME_KEY = key
        return _PERSISTENT_RUNTIME


def preload_persistent_runtime(
    working_dir: Path,
    graph_path: Path,
    config_path: Path | None = None,
    embedding_cache: Path | None = None,
) -> None:
    """Initialize the same LightRAG runtime that subsequent queries will use."""

    enable_persistent_runtime()
    _get_persistent_runtime(working_dir, graph_path, config_path, embedding_cache)


def query_lightrag(
    query: str,
    working_dir: Path,
    graph_path: Path,
    config_path: Path | None = None,
    embedding_cache: Path | None = None,
    *,
    high_level_keywords: Sequence[str] = (),
    low_level_keywords: Sequence[str] = (),
    allowed_entity_ids: Sequence[int] = (),
    top_k: int = 200,
    chunk_top_k: int = 200,
) -> LightRAGRecall:
    """Run mandatory LightRAG graph+vector retrieval and map results to venues."""

    if not query.strip():
        raise LightRAGRuntimeError("LightRAG 查询不能为空")
    if top_k < 1 or chunk_top_k < 1:
        raise LightRAGRuntimeError("LightRAG top_k 必须大于 0")
    try:
        if _PERSISTENT_RUNTIME_ENABLED:
            runtime = _get_persistent_runtime(
                working_dir, graph_path, config_path, embedding_cache
            )
            result = runtime.query(
                query,
                high_level_keywords,
                low_level_keywords,
                top_k,
                chunk_top_k,
            )
            provider_fingerprint = runtime.provider_fingerprint
        else:
            embedding_config = load_embedding_config(config_path)
            provider = OpenAICompatibleEmbeddingProvider(embedding_config)
            validate_lightrag_workspace(
                working_dir, graph_path, provider.fingerprint
            )
            try:
                result, provider_fingerprint = asyncio.run(
                    _query_async(
                        query,
                        working_dir,
                        config_path,
                        embedding_cache,
                        high_level_keywords,
                        low_level_keywords,
                        top_k,
                        chunk_top_k,
                    )
                )
            finally:
                _finalize_lightrag_shared_state()
            if provider_fingerprint != provider.fingerprint:
                raise LightRAGRuntimeError(
                    "LightRAG 运行时 embedding 指纹发生变化"
                )
    except (EmbeddingError, OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, LightRAGRuntimeError):
            raise
        raise LightRAGRuntimeError(f"LightRAG mix 检索失败：{exc}") from exc

    if not isinstance(result, dict):
        raise LightRAGRuntimeError("LightRAG 返回了无效数据")
    data = result.get("data")
    if not isinstance(data, dict):
        message = str(result.get("message") or "").casefold()
        failure_reason = str(
            (result.get("metadata") or {}).get("failure_reason") or ""
        ).casefold()
        if "no result" in message or failure_reason == "no_results":
            data = {}
        else:
            raise LightRAGRuntimeError(
                "LightRAG 检索未成功：" + str(result.get("message") or "unknown")
            )
    return recall_from_lightrag_data(data, allowed_entity_ids=allowed_entity_ids)


def recall_from_lightrag_data(
    data: Mapping[str, Any],
    *,
    allowed_entity_ids: Sequence[int] = (),
) -> LightRAGRecall:
    """Convert structured LightRAG output to deterministic candidate signals."""

    allowed = set(int(value) for value in allowed_entity_ids)
    scores: dict[int, float] = {}
    channels: dict[int, list[str]] = {}
    order: list[int] = []

    def add(value: Any, channel: str, channel_weight: float, rank: int) -> None:
        for match in VENUE_ID_RE.finditer(str(value or "")):
            entity_id = int(match.group(1))
            if allowed and entity_id not in allowed:
                continue
            score = channel_weight / math.log2(rank + 1.0)
            if entity_id not in scores:
                order.append(entity_id)
                scores[entity_id] = score
                channels[entity_id] = [channel]
            else:
                scores[entity_id] = max(scores[entity_id], score)
                if channel not in channels[entity_id]:
                    channels[entity_id].append(channel)

    entities = data.get("entities") if isinstance(data.get("entities"), list) else []
    relationships = (
        data.get("relationships")
        if isinstance(data.get("relationships"), list)
        else []
    )
    chunks = data.get("chunks") if isinstance(data.get("chunks"), list) else []
    for rank, item in enumerate(entities, 1):
        if not isinstance(item, Mapping):
            continue
        add(item.get("entity_name"), "entity_vector", 1.0, rank)
        add(item.get("description"), "entity_description", 0.9, rank)
    for rank, item in enumerate(relationships, 1):
        if not isinstance(item, Mapping):
            continue
        add(item.get("src_id"), "relationship", 0.85, rank)
        add(item.get("tgt_id"), "relationship", 0.85, rank)
        add(item.get("description"), "relationship_description", 0.7, rank)
    for rank, item in enumerate(chunks, 1):
        if isinstance(item, Mapping):
            add(item.get("content"), "chunk_vector", 0.8, rank)

    return LightRAGRecall(
        entity_ids=tuple(order),
        scores=scores,
        channels={key: tuple(value) for key, value in channels.items()},
        entity_count=len(entities),
        relationship_count=len(relationships),
        chunk_count=len(chunks),
    )
