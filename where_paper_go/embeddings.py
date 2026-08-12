#!/usr/bin/env python3
"""Embedding providers, cache, and vector-index population utilities."""

from __future__ import annotations

import array
import base64
import gzip
import hashlib
import json
import math
import os
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request

from .paths import PROJECT_ROOT
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence


EMBEDDING_CACHE_SCHEMA_VERSION = "1"
DEFAULT_EMBEDDING_CACHE_FILE = ".embedding_cache.sqlite3"
DEFAULT_GRAPH_EMBEDDING_CACHE_FILE = ".embedding_cache.json.gz"
DEFAULT_QUERY_EMBEDDING_CACHE_FILE = ".query_embedding_cache.json.gz"


class EmbeddingError(RuntimeError):
    """Base error for embedding configuration, transport, and validation."""


class EmbeddingConfigurationError(EmbeddingError):
    """Raised when no usable embedding section exists in the API config."""


class EmbeddingProvider(Protocol):
    model: str
    fingerprint: str
    batch_size: int

    def prepare_text(self, text: str) -> str: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: str
    base_url: str
    api_key: str
    model: str
    endpoint: str
    dimensions: int | None
    send_dimensions: bool
    timeout: int
    batch_size: int
    max_chars: int
    max_retries: int
    headers: Mapping[str, str]
    extra_body: Mapping[str, Any]

    @property
    def fingerprint(self) -> str:
        safe_identity = {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "model": self.model,
            "dimensions": self.dimensions,
            "send_dimensions": self.send_dimensions,
            "extra_body": self.extra_body,
        }
        encoded = json.dumps(
            safe_identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class VectorBuildResult:
    entity_count: int
    unique_text_count: int
    embedded_text_count: int
    cached_text_count: int
    dimensions: int
    model: str
    provider_fingerprint: str


class OpenAICompatibleEmbeddingProvider:
    def __init__(self, config: EmbeddingConfig):
        if config.provider not in {"openai_compatible", "openai-compatible"}:
            raise EmbeddingConfigurationError(
                f"unsupported embedding provider: {config.provider!r}"
            )
        self.config = config
        self.model = config.model
        self.fingerprint = config.fingerprint
        self.batch_size = config.batch_size

    def prepare_text(self, text: str) -> str:
        normalized = " ".join((text or "").split())
        if not normalized:
            raise EmbeddingError("embedding text cannot be empty")
        return normalized[: self.config.max_chars]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        prepared = [self.prepare_text(text) for text in texts]
        if not prepared:
            return []
        payload: dict[str, Any] = {
            **self.config.extra_body,
            "model": self.config.model,
            "input": prepared,
        }
        if self.config.dimensions is not None and self.config.send_dimensions:
            payload["dimensions"] = self.config.dimensions
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "venue-recommender-embeddings/1.0",
            **{str(key): str(value) for key, value in self.config.headers.items()},
        }
        if self.config.api_key and not any(
            key.casefold() == "authorization" for key in headers
        ):
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            request = urllib.request.Request(
                self.config.endpoint,
                data=body,
                method="POST",
                headers=headers,
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self.config.timeout
                ) as response:
                    result = json.loads(response.read(20_000_000).decode("utf-8"))
                vectors = _parse_embedding_response(result, len(prepared))
                if self.config.dimensions is not None and any(
                    len(vector) != self.config.dimensions for vector in vectors
                ):
                    raise EmbeddingError(
                        "embedding response dimensions do not match the configuration"
                    )
                return vectors
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in {408, 409, 429, 500, 502, 503, 504}:
                    break
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt < self.config.max_retries:
                time.sleep(min(8.0, 0.5 * (2**attempt)))
        detail = type(last_error).__name__ if last_error else "unknown error"
        if isinstance(last_error, urllib.error.HTTPError):
            detail += f" HTTP {last_error.code}"
        raise EmbeddingError(f"embedding request failed: {detail}") from last_error


class EmbeddingCache:
    """Legacy SQLite cache retained for the opt-in legacy search backend."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            PRAGMA journal_mode = WAL;
            PRAGMA synchronous = NORMAL;
            CREATE TABLE IF NOT EXISTS cache_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS embedding_cache (
                provider_fingerprint TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                vector BLOB NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (provider_fingerprint, text_hash)
            ) WITHOUT ROWID;
            """
        )
        current = self.connection.execute(
            "SELECT value FROM cache_meta WHERE key = 'schema_version'"
        ).fetchone()
        if current and current[0] != EMBEDDING_CACHE_SCHEMA_VERSION:
            self.connection.close()
            raise EmbeddingError("embedding cache schema version is incompatible")
        self.connection.execute(
            "INSERT OR REPLACE INTO cache_meta(key, value) VALUES ('schema_version', ?)",
            (EMBEDDING_CACHE_SCHEMA_VERSION,),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "EmbeddingCache":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def existing_hashes(
        self, provider_fingerprint: str, text_hashes: Sequence[str]
    ) -> set[str]:
        existing: set[str] = set()
        for chunk in _chunks(list(dict.fromkeys(text_hashes)), 400):
            placeholders = ", ".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"""
                SELECT text_hash
                FROM embedding_cache
                WHERE provider_fingerprint = ?
                  AND text_hash IN ({placeholders})
                """,
                (provider_fingerprint, *chunk),
            )
            existing.update(str(row[0]) for row in rows)
        return existing

    def get_many(
        self, provider_fingerprint: str, text_hashes: Sequence[str]
    ) -> dict[str, tuple[int, bytes]]:
        result: dict[str, tuple[int, bytes]] = {}
        for chunk in _chunks(list(dict.fromkeys(text_hashes)), 400):
            placeholders = ", ".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"""
                SELECT text_hash, dimensions, vector
                FROM embedding_cache
                WHERE provider_fingerprint = ?
                  AND text_hash IN ({placeholders})
                """,
                (provider_fingerprint, *chunk),
            )
            for row in rows:
                result[str(row["text_hash"])] = (
                    int(row["dimensions"]),
                    bytes(row["vector"]),
                )
        return result

    def put_many(
        self,
        provider_fingerprint: str,
        rows: Sequence[tuple[str, int, bytes]],
    ) -> None:
        self.connection.executemany(
            """
            INSERT OR REPLACE INTO embedding_cache(
                provider_fingerprint, text_hash, dimensions, vector
            ) VALUES (?, ?, ?, ?)
            """,
            (
                (provider_fingerprint, text_hash, dimensions, vector)
                for text_hash, dimensions, vector in rows
            ),
        )
        self.connection.commit()


class FileEmbeddingCache:
    """Atomic gzip/JSON embedding cache used by the database-free graph backend."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._dirty = False
        self._entries: dict[str, dict[str, list[Any]]] = {}
        if not self.path.exists():
            return
        try:
            with gzip.open(self.path, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, EOFError, UnicodeError, json.JSONDecodeError) as exc:
            raise EmbeddingError(f"cannot read file embedding cache: {self.path}") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != EMBEDDING_CACHE_SCHEMA_VERSION
            or not isinstance(payload.get("entries"), dict)
        ):
            raise EmbeddingError("file embedding cache schema is incompatible")
        self._entries = {
            str(fingerprint): dict(rows)
            for fingerprint, rows in payload["entries"].items()
            if isinstance(rows, dict)
        }

    def close(self) -> None:
        if not self._dirty:
            return
        payload = {
            "schema_version": EMBEDDING_CACHE_SCHEMA_VERSION,
            "entries": self._entries,
        }
        temporary = tempfile.NamedTemporaryFile(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
            delete=False,
        )
        temporary_path = Path(temporary.name)
        temporary.close()
        try:
            with gzip.open(
                temporary_path, "wt", encoding="utf-8", compresslevel=6
            ) as handle:
                json.dump(payload, handle, separators=(",", ":"), allow_nan=False)
            os.replace(temporary_path, self.path)
            self._dirty = False
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

    def __enter__(self) -> "FileEmbeddingCache":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def existing_hashes(
        self, provider_fingerprint: str, text_hashes: Sequence[str]
    ) -> set[str]:
        return set(self.get_many(provider_fingerprint, text_hashes))

    def get_many(
        self, provider_fingerprint: str, text_hashes: Sequence[str]
    ) -> dict[str, tuple[int, bytes]]:
        rows = self._entries.get(provider_fingerprint, {})
        result: dict[str, tuple[int, bytes]] = {}
        for text_hash in dict.fromkeys(text_hashes):
            row = rows.get(text_hash)
            if not isinstance(row, list) or len(row) != 2:
                continue
            try:
                result[text_hash] = (
                    int(row[0]),
                    base64.b64decode(str(row[1]), validate=True),
                )
            except (TypeError, ValueError):
                continue
        return result

    def put_many(
        self,
        provider_fingerprint: str,
        rows: Sequence[tuple[str, int, bytes]],
    ) -> None:
        provider_rows = self._entries.setdefault(provider_fingerprint, {})
        for text_hash, dimensions, vector in rows:
            provider_rows[text_hash] = [
                int(dimensions),
                base64.b64encode(vector).decode("ascii"),
            ]
        self._dirty = True


def load_embedding_config(path: Path | None = None) -> EmbeddingConfig:
    config_path = path
    if config_path is None:
        for candidate in (PROJECT_ROOT / "api.json", PROJECT_ROOT / "llmapi.json"):
            if candidate.exists() and candidate.stat().st_size:
                config_path = candidate
                break
    if config_path is None or not config_path.exists():
        raise EmbeddingConfigurationError(
            "缺少 embedding API 配置；请在 api.json 的 embedding 节中配置模型"
        )
    try:
        root = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EmbeddingConfigurationError(
            f"无法读取 embedding API 配置：{config_path}"
        ) from exc
    if not isinstance(root, dict):
        raise EmbeddingConfigurationError(
            f"{config_path} 的顶层配置必须是 JSON 对象"
        )
    section = root.get("embedding") or root.get("embeddings")
    if not isinstance(section, dict):
        raise EmbeddingConfigurationError(
            f"{config_path} 缺少独立的 embedding 配置节；不会把聊天模型当作嵌入模型"
        )
    llm = root.get("llm") if isinstance(root.get("llm"), dict) else {}
    provider = str(section.get("provider") or "openai_compatible").strip().lower()
    base_url = str(section.get("base_url") or llm.get("base_url") or "").strip()
    endpoint = str(section.get("endpoint") or section.get("embeddings_url") or "").strip()
    if not endpoint and base_url:
        endpoint = base_url.rstrip("/") + "/embeddings"
    api_key = str(section.get("api_key") or section.get("key") or llm.get("api_key") or "")
    model = str(section.get("model") or "").strip()
    if not endpoint or not model:
        raise EmbeddingConfigurationError(
            f"{config_path} 的 embedding 配置必须提供 base_url/endpoint 和 model"
        )
    dimensions_value = section.get("dimensions")
    try:
        dimensions = (
            int(dimensions_value)
            if dimensions_value is not None and dimensions_value != ""
            else None
        )
    except (TypeError, ValueError) as exc:
        raise EmbeddingConfigurationError(
            "embedding dimensions 必须是整数"
        ) from exc
    if dimensions is not None and dimensions < 8:
        raise EmbeddingConfigurationError("embedding dimensions 必须至少为 8")
    send_dimensions = section.get("send_dimensions", True)
    if not isinstance(send_dimensions, bool):
        raise EmbeddingConfigurationError("embedding send_dimensions 必须是布尔值")
    try:
        batch_size = int(section.get("batch_size", 64))
        timeout = int(section.get("timeout", 60))
        max_chars = int(section.get("max_chars", 8000))
        max_retries = int(section.get("max_retries", 2))
    except (TypeError, ValueError) as exc:
        raise EmbeddingConfigurationError(
            "embedding 批量、超时、长度或重试配置必须是整数"
        ) from exc
    if batch_size < 1 or timeout < 1 or max_chars < 100 or max_retries < 0:
        raise EmbeddingConfigurationError("embedding 批量、超时、长度或重试配置无效")
    headers = section.get("headers") if isinstance(section.get("headers"), dict) else {}
    extra_body = (
        section.get("extra_body")
        if isinstance(section.get("extra_body"), dict)
        else {}
    )
    return EmbeddingConfig(
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        endpoint=endpoint,
        dimensions=dimensions,
        send_dimensions=send_dimensions,
        timeout=timeout,
        batch_size=batch_size,
        max_chars=max_chars,
        max_retries=max_retries,
        headers={str(key): str(value) for key, value in headers.items()},
        extra_body=extra_body,
    )


def default_embedding_cache_path(data_dir: Path) -> Path:
    return data_dir / DEFAULT_EMBEDDING_CACHE_FILE


def default_graph_embedding_cache_path(data_dir: Path) -> Path:
    return data_dir / DEFAULT_GRAPH_EMBEDDING_CACHE_FILE


def default_query_embedding_cache_path(data_dir: Path) -> Path:
    """Return the small cache used only for online query embeddings."""

    return data_dir / DEFAULT_QUERY_EMBEDDING_CACHE_FILE


def ensure_cached_embeddings(
    provider: EmbeddingProvider,
    texts_by_hash: Mapping[str, str],
    cache: EmbeddingCache | FileEmbeddingCache,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int, int]:
    hashes = list(texts_by_hash)
    existing = cache.existing_hashes(provider.fingerprint, hashes)
    missing = [text_hash for text_hash in hashes if text_hash not in existing]
    dimensions: set[int] = set()
    processed = 0
    total = len(missing)
    for chunk in _chunks(missing, provider.batch_size):
        vectors = provider.embed([texts_by_hash[text_hash] for text_hash in chunk])
        if len(vectors) != len(chunk):
            raise EmbeddingError(
                f"embedding provider returned {len(vectors)} vectors for {len(chunk)} inputs"
            )
        cache_rows = []
        for text_hash, vector in zip(chunk, vectors):
            normalized = normalize_vector(vector)
            if len(normalized) < 8:
                raise EmbeddingError("embedding vector must have at least 8 dimensions")
            dimensions.add(len(normalized))
            cache_rows.append(
                (text_hash, len(normalized), pack_float32(normalized))
            )
        cache.put_many(provider.fingerprint, cache_rows)
        processed += len(chunk)
        if progress:
            progress(processed, total)

    cached_rows = cache.get_many(provider.fingerprint, hashes)
    dimensions.update(dimension for dimension, _vector in cached_rows.values())
    if len(cached_rows) != len(hashes):
        raise EmbeddingError("embedding cache is incomplete after population")
    if len(dimensions) != 1:
        raise EmbeddingError(
            f"embedding dimensions are inconsistent: {sorted(dimensions)}"
        )
    return next(iter(dimensions)), len(missing), len(existing)


def build_vector_index(
    index_path: Path,
    provider: EmbeddingProvider,
    cache_path: Path,
    *,
    force: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> VectorBuildResult:
    connection = sqlite3.connect(index_path)
    connection.row_factory = sqlite3.Row
    try:
        metadata = dict(connection.execute("SELECT key, value FROM index_meta"))
        existing_count = int(metadata.get("vector_count", "0"))
        stored_count = int(
            connection.execute("SELECT COUNT(*) FROM vector_embedding").fetchone()[0]
        )
        entity_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM venue WHERE semantic_text <> ''"
            ).fetchone()[0]
        )
        if (
            not force
            and metadata.get("vector_provider_fingerprint") == provider.fingerprint
            and existing_count == entity_count
            and stored_count == entity_count
            and entity_count > 0
        ):
            return VectorBuildResult(
                entity_count=entity_count,
                unique_text_count=int(metadata.get("vector_unique_text_count", "0")),
                embedded_text_count=0,
                cached_text_count=int(metadata.get("vector_unique_text_count", "0")),
                dimensions=int(metadata["vector_dimensions"]),
                model=metadata.get("vector_model", provider.model),
                provider_fingerprint=provider.fingerprint,
            )

        entity_documents: list[tuple[int, str, str]] = []
        unique_texts: dict[str, str] = {}
        for row in connection.execute(
            "SELECT entity_id, semantic_text FROM venue WHERE semantic_text <> '' ORDER BY entity_id"
        ):
            prepared = provider.prepare_text(str(row["semantic_text"]))
            text_hash = hashlib.sha256(prepared.encode("utf-8")).hexdigest()
            entity_documents.append((int(row["entity_id"]), text_hash, prepared))
            unique_texts.setdefault(text_hash, prepared)

        with EmbeddingCache(cache_path) as cache:
            dimensions, embedded_count, cached_count = ensure_cached_embeddings(
                provider,
                unique_texts,
                cache,
                progress=progress,
            )
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM vector_embedding")
                for chunk in _chunks(entity_documents, 400):
                    hashes = [text_hash for _entity_id, text_hash, _text in chunk]
                    cached = cache.get_many(provider.fingerprint, hashes)
                    rows = []
                    for entity_id, text_hash, _text in chunk:
                        dimension, vector = cached[text_hash]
                        if dimension != dimensions:
                            raise EmbeddingError("cached embedding dimension changed")
                        rows.append(
                            (
                                entity_id,
                                provider.fingerprint,
                                provider.model,
                                dimensions,
                                text_hash,
                                vector,
                                sign_bits_from_blob(vector, dimensions),
                            )
                        )
                    connection.executemany(
                        """
                        INSERT INTO vector_embedding(
                            entity_id, provider_fingerprint, model, dimensions,
                            text_hash, vector, sign_bits
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
                vector_metadata = {
                    "vector_provider_fingerprint": provider.fingerprint,
                    "vector_model": provider.model,
                    "vector_dimensions": str(dimensions),
                    "vector_count": str(len(entity_documents)),
                    "vector_unique_text_count": str(len(unique_texts)),
                }
                connection.executemany(
                    "INSERT OR REPLACE INTO index_meta(key, value) VALUES (?, ?)",
                    vector_metadata.items(),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
    except sqlite3.Error as exc:
        raise EmbeddingError(f"cannot populate vector index: {exc}") from exc
    finally:
        connection.close()

    return VectorBuildResult(
        entity_count=len(entity_documents),
        unique_text_count=len(unique_texts),
        embedded_text_count=embedded_count,
        cached_text_count=cached_count,
        dimensions=dimensions,
        model=provider.model,
        provider_fingerprint=provider.fingerprint,
    )


def build_graph_vector_index(
    graph_path: Path,
    provider: EmbeddingProvider,
    cache_path: Path,
    *,
    vector_path: Path | None = None,
    force: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> VectorBuildResult:
    """Embed property-graph venue nodes into an atomic file sidecar."""

    from .graph_index import (
        GraphIndexError,
        VenueGraphIndex,
        vector_path_for_graph,
        write_graph_vectors,
    )

    destination = (vector_path or vector_path_for_graph(graph_path)).resolve()
    with VenueGraphIndex(graph_path, vector_path=destination) as graph:
        documents = graph.semantic_documents()
        vector_metadata: dict[str, str] = {}
        try:
            vector_metadata = graph.vector_metadata()
        except GraphIndexError:
            # A changed graph intentionally invalidates the old sidecar.
            vector_metadata = {}
        if (
            not force
            and vector_metadata.get("vector_provider_fingerprint")
            == provider.fingerprint
            and int(vector_metadata.get("vector_count", "0")) == len(documents)
            and documents
        ):
            return VectorBuildResult(
                entity_count=len(documents),
                unique_text_count=int(
                    vector_metadata.get("vector_unique_text_count", "0")
                ),
                embedded_text_count=0,
                cached_text_count=int(
                    vector_metadata.get("vector_unique_text_count", "0")
                ),
                dimensions=int(vector_metadata["vector_dimensions"]),
                model=vector_metadata.get("vector_model", provider.model),
                provider_fingerprint=provider.fingerprint,
            )

    entity_documents: list[tuple[int, str, str]] = []
    unique_texts: dict[str, str] = {}
    for entity_id, text in sorted(documents.items()):
        prepared = provider.prepare_text(text)
        text_hash = hashlib.sha256(prepared.encode("utf-8")).hexdigest()
        entity_documents.append((entity_id, text_hash, prepared))
        unique_texts.setdefault(text_hash, prepared)
    if not entity_documents:
        raise EmbeddingError("property graph contains no semantic venue text")

    with FileEmbeddingCache(cache_path) as cache:
        dimensions, embedded_count, cached_count = ensure_cached_embeddings(
            provider,
            unique_texts,
            cache,
            progress=progress,
        )
        cached = cache.get_many(provider.fingerprint, list(unique_texts))
        vectors: dict[int, tuple[str, bytes]] = {}
        for entity_id, text_hash, _text in entity_documents:
            dimension, blob = cached[text_hash]
            if dimension != dimensions:
                raise EmbeddingError("cached graph embedding dimension changed")
            vectors[entity_id] = (text_hash, blob)

    write_graph_vectors(
        graph_path,
        destination,
        provider_fingerprint=provider.fingerprint,
        model=provider.model,
        dimensions=dimensions,
        unique_text_count=len(unique_texts),
        vectors=vectors,
    )
    return VectorBuildResult(
        entity_count=len(entity_documents),
        unique_text_count=len(unique_texts),
        embedded_text_count=embedded_count,
        cached_text_count=cached_count,
        dimensions=dimensions,
        model=provider.model,
        provider_fingerprint=provider.fingerprint,
    )


def embed_query(
    query: str,
    provider: EmbeddingProvider,
    cache_path: Path,
) -> list[float]:
    prepared = provider.prepare_text(query)
    text_hash = hashlib.sha256(prepared.encode("utf-8")).hexdigest()
    with EmbeddingCache(cache_path) as cache:
        ensure_cached_embeddings(provider, {text_hash: prepared}, cache)
        dimension, blob = cache.get_many(provider.fingerprint, [text_hash])[text_hash]
    vector = unpack_float32(blob, dimension)
    return vector


def embed_query_graph(
    query: str,
    provider: EmbeddingProvider,
    cache_path: Path,
) -> list[float]:
    """Embed and cache a query without creating or opening SQLite."""

    prepared = provider.prepare_text(query)
    text_hash = hashlib.sha256(prepared.encode("utf-8")).hexdigest()
    with FileEmbeddingCache(cache_path) as cache:
        ensure_cached_embeddings(provider, {text_hash: prepared}, cache)
        dimension, blob = cache.get_many(provider.fingerprint, [text_hash])[text_hash]
    return unpack_float32(blob, dimension)


def normalize_vector(values: Sequence[float]) -> list[float]:
    if not values:
        raise EmbeddingError("embedding vector cannot be empty")
    vector = []
    squared_norm = 0.0
    for value in values:
        number = float(value)
        if not math.isfinite(number):
            raise EmbeddingError("embedding vector contains a non-finite value")
        vector.append(number)
        squared_norm += number * number
    if squared_norm <= 0 or not math.isfinite(squared_norm):
        raise EmbeddingError("embedding vector has zero norm")
    inverse_norm = 1.0 / math.sqrt(squared_norm)
    return [value * inverse_norm for value in vector]


def pack_float32(values: Sequence[float]) -> bytes:
    packed = array.array("f", values)
    if sys.byteorder != "little":
        packed.byteswap()
    return packed.tobytes()


def unpack_float32(blob: bytes, dimensions: int) -> list[float]:
    expected_bytes = dimensions * 4
    if len(blob) != expected_bytes:
        raise EmbeddingError(
            f"invalid float32 vector size: expected {expected_bytes}, got {len(blob)}"
        )
    values = array.array("f")
    values.frombytes(blob)
    if sys.byteorder != "little":
        values.byteswap()
    return list(values)


def sign_bits_from_blob(blob: bytes, dimensions: int) -> bytes:
    values = unpack_float32(blob, dimensions)
    result = 0
    for index, value in enumerate(values):
        if value >= 0:
            result |= 1 << index
    return result.to_bytes((dimensions + 7) // 8, byteorder="little")


def _parse_embedding_response(data: Any, expected_count: int) -> list[list[float]]:
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise EmbeddingError("embedding response does not contain a data array")
    indexed: dict[int, list[float]] = {}
    for fallback_index, item in enumerate(data["data"]):
        if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
            raise EmbeddingError("embedding response contains an invalid item")
        index = int(item.get("index", fallback_index))
        if index in indexed:
            raise EmbeddingError("embedding response contains duplicate indices")
        indexed[index] = [float(value) for value in item["embedding"]]
    if set(indexed) != set(range(expected_count)):
        raise EmbeddingError(
            f"embedding response indices do not match {expected_count} inputs"
        )
    return [indexed[index] for index in range(expected_count)]


def _chunks(values: Sequence[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(values), size):
        yield list(values[start : start + size])
