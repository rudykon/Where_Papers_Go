#!/usr/bin/env python3
"""Legacy SQLite/FTS5 index for retrieval comparison and migration.

The normalized CSV/TSV files remain the source of truth.  This module stores
the already validated and entity-grouped representation as a disposable query
index so normal searches do not need to parse and regroup all source rows.
"""

from __future__ import annotations

import array
import hashlib
import heapq
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


INDEX_SCHEMA_VERSION = "3"
DEFAULT_INDEX_FILE = "venue_index.sqlite3"
SOURCE_FILE_NAMES = (
    "ccf_conferences_2026.csv",
    "th_cpl_partition_2019.csv",
    "cas_partition_2025.csv",
    "jcr_partition_2025.csv",
    "curated_venue_scopes.tsv",
)

FTS_COLUMNS_WITHOUT_OFFICIAL = (
    "name",
    "abbreviation",
    "area",
    "taxonomy_scope",
    "curated_scope",
    "curated_topics",
)
FTS_COLUMNS_WITH_OFFICIAL = (*FTS_COLUMNS_WITHOUT_OFFICIAL, "official_scope")
FTS_BM25_WEIGHTS = (0.0, 2.5, 4.0, 3.5, 2.5, 5.0, 5.5, 1.0)


class SearchIndexError(RuntimeError):
    """Raised when an index is missing required schema or metadata."""


@dataclass(frozen=True)
class IndexFreshness:
    fresh: bool
    reason: str
    source_digest: str


@dataclass(frozen=True)
class IndexBuildResult:
    path: Path
    source_digest: str
    record_count: int
    entity_count: int


@dataclass(frozen=True)
class RecallResult:
    entity_ids: list[int]
    lexical_scores: Mapping[int, float]
    total_documents: int
    reviewed_documents: int
    document_frequency: Mapping[str, int]
    concept_document_frequency: Mapping[str, int]


@dataclass(frozen=True)
class VectorRecallResult:
    entity_ids: list[int]
    similarities: Mapping[int, float]
    model: str
    dimensions: int


def default_index_path(data_dir: Path) -> Path:
    return data_dir / DEFAULT_INDEX_FILE


def source_digest(data_dir: Path) -> str:
    """Hash every source file that can affect the generated search index."""

    digest = hashlib.sha256()
    digest.update(f"schema:{INDEX_SCHEMA_VERSION}\n".encode())
    for name in SOURCE_FILE_NAMES:
        path = data_dir / name
        digest.update(f"file:{name}\n".encode())
        if not path.exists():
            digest.update(b"missing\n")
            continue
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\n")
    return digest.hexdigest()


def inspect_index(
    index_path: Path,
    data_dir: Path,
    *,
    expected_digest: str | None = None,
) -> IndexFreshness:
    """Return whether an index matches the current schema and source files."""

    digest = expected_digest or source_digest(data_dir)
    if not index_path.exists():
        return IndexFreshness(False, "index_missing", digest)
    try:
        with _read_only_connection(index_path) as connection:
            metadata = dict(connection.execute("SELECT key, value FROM index_meta"))
    except (OSError, sqlite3.Error):
        return IndexFreshness(False, "index_unreadable", digest)
    if metadata.get("schema_version") != INDEX_SCHEMA_VERSION:
        return IndexFreshness(False, "schema_changed", digest)
    if metadata.get("source_digest") != digest:
        return IndexFreshness(False, "source_changed", digest)
    return IndexFreshness(True, "fresh", digest)


def build_index(
    index_path: Path,
    data_dir: Path,
    records: Sequence[Any],
    groups: Sequence[Sequence[Any]],
    *,
    tokenize: Callable[[str], Mapping[str, int]],
    normalize_alias: Callable[[str | None], str],
    display_name_for_group: Callable[[Sequence[Any]], str],
    matching_document_for_group: Callable[[Sequence[Any]], Mapping[str, str]],
    expected_digest: str | None = None,
) -> IndexBuildResult:
    """Build a complete index in a temporary file and atomically replace it."""

    if not records:
        raise SearchIndexError("cannot build an index without venue records")
    index_path = index_path.resolve()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    digest = expected_digest or source_digest(data_dir)
    record_columns = [field.name for field in fields(type(records[0]))]
    temporary = tempfile.NamedTemporaryFile(
        prefix=f".{index_path.name}.",
        suffix=".tmp",
        dir=index_path.parent,
        delete=False,
    )
    temporary_path = Path(temporary.name)
    temporary.close()
    try:
        connection = sqlite3.connect(temporary_path)
        try:
            _create_schema(connection, record_columns)
            _populate_index(
                connection,
                records,
                groups,
                record_columns=record_columns,
                tokenize=tokenize,
                normalize_alias=normalize_alias,
                display_name_for_group=display_name_for_group,
                matching_document_for_group=matching_document_for_group,
                digest=digest,
            )
            check = connection.execute("PRAGMA quick_check").fetchone()
            if not check or check[0] != "ok":
                raise SearchIndexError(f"generated index failed quick_check: {check!r}")
        finally:
            connection.close()
        os.replace(temporary_path, index_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return IndexBuildResult(
        path=index_path,
        source_digest=digest,
        record_count=len(records),
        entity_count=len(groups),
    )


class VenueSearchIndex:
    """Read/query facade for a generated venue search index."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.connection = _read_only_connection(self.path)
        self.connection.row_factory = sqlite3.Row
        metadata = dict(self.connection.execute("SELECT key, value FROM index_meta"))
        if metadata.get("schema_version") != INDEX_SCHEMA_VERSION:
            self.connection.close()
            raise SearchIndexError("search index schema version is incompatible")
        try:
            self.record_columns = tuple(json.loads(metadata["record_columns"]))
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            self.connection.close()
            raise SearchIndexError("search index record metadata is invalid") from exc

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> VenueSearchIndex:
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def metadata(self) -> dict[str, str]:
        return dict(self.connection.execute("SELECT key, value FROM index_meta"))

    def load_groups_for_targets(
        self, targets: Sequence[tuple[str, str]]
    ) -> list[tuple[int, list[dict[str, Any]]]]:
        """Load complete entity groups that have at least one requested ranking."""

        if not targets:
            return []
        where, parameters = _target_predicate(targets, alias="ranking")
        selected_columns = ", ".join(
            f'venue_record."{column}"' for column in self.record_columns
        )
        rows = self.connection.execute(
            f"""
            SELECT venue_record.entity_id, {selected_columns}
            FROM venue_record
            JOIN (
                SELECT DISTINCT entity_id
                FROM ranking
                WHERE {where}
            ) AS selected USING (entity_id)
            ORDER BY venue_record.entity_id, venue_record.row_id
            """,
            parameters,
        )
        grouped: list[tuple[int, list[dict[str, Any]]]] = []
        current_entity: int | None = None
        current_rows: list[dict[str, Any]] = []
        for row in rows:
            entity_id = int(row["entity_id"])
            if current_entity is not None and entity_id != current_entity:
                grouped.append((current_entity, current_rows))
                current_rows = []
            current_entity = entity_id
            current_rows.append({column: row[column] for column in self.record_columns})
        if current_entity is not None:
            grouped.append((current_entity, current_rows))
        return grouped

    def recall(
        self,
        *,
        allowed_entity_ids: Sequence[int],
        query_tokens: Iterable[str],
        topic_tags: Iterable[str],
        include_official_scope: bool,
        lexical_limit: int | None = None,
    ) -> RecallResult:
        """Perform FTS5/BM25 and controlled-topic recall within hard filters."""

        allowed = sorted(set(int(value) for value in allowed_entity_ids))
        tokens = _unique_text(query_tokens)
        topics = _unique_text(topic_tags)
        if not allowed:
            return RecallResult([], {}, 0, 0, {}, {})
        self._replace_allowed_entities(allowed)
        total_documents = len(allowed)
        reviewed_documents = int(
            self.connection.execute(
                """
                SELECT COUNT(*)
                FROM venue
                JOIN temp.query_allowed USING (entity_id)
                WHERE venue.has_reviewed_scope = 1
                """
            ).fetchone()[0]
        )

        columns = (
            FTS_COLUMNS_WITH_OFFICIAL
            if include_official_scope
            else FTS_COLUMNS_WITHOUT_OFFICIAL
        )
        lexical_scores: dict[int, float] = {}
        document_frequency: dict[str, int] = {}
        if tokens:
            query = _fts_column_query(columns, tokens)
            limit_sql = "" if lexical_limit is None else " LIMIT ?"
            parameters: list[Any] = [query]
            if lexical_limit is not None:
                parameters.append(max(1, int(lexical_limit)))
            weight_sql = ", ".join(str(weight) for weight in FTS_BM25_WEIGHTS)
            rows = self.connection.execute(
                f"""
                SELECT CAST(venue_fts.entity_id AS INTEGER) AS entity_id,
                       bm25(venue_fts, {weight_sql}) AS lexical_rank
                FROM venue_fts
                JOIN temp.query_allowed
                  ON query_allowed.entity_id = CAST(venue_fts.entity_id AS INTEGER)
                WHERE venue_fts MATCH ?
                ORDER BY lexical_rank ASC, entity_id ASC
                {limit_sql}
                """,
                parameters,
            )
            for row in rows:
                lexical_scores[int(row["entity_id"])] = -float(row["lexical_rank"])
            document_frequency = {token: 0 for token in tokens}
            for chunk in _chunks(tokens, 400):
                placeholders = ", ".join("?" for _ in chunk)
                source_filter = "" if include_official_scope else "AND term_posting.source = 0"
                rows = self.connection.execute(
                    f"""
                    SELECT term_posting.term,
                           COUNT(DISTINCT term_posting.entity_id) AS document_count
                    FROM term_posting
                    JOIN temp.query_allowed USING (entity_id)
                    WHERE term_posting.term IN ({placeholders})
                      {source_filter}
                    GROUP BY term_posting.term
                    """,
                    chunk,
                )
                for row in rows:
                    document_frequency[str(row["term"])] = int(row["document_count"])

        concept_document_frequency: dict[str, int] = {topic: 0 for topic in topics}
        topic_entity_ids: set[int] = set()
        if topics:
            placeholders = ", ".join("?" for _ in topics)
            rows = self.connection.execute(
                f"""
                SELECT topic_posting.topic_tag, topic_posting.entity_id
                FROM topic_posting
                JOIN temp.query_allowed USING (entity_id)
                WHERE topic_posting.topic_tag IN ({placeholders})
                ORDER BY topic_posting.topic_tag, topic_posting.entity_id
                """,
                topics,
            )
            for row in rows:
                topic_tag = str(row["topic_tag"])
                concept_document_frequency[topic_tag] += 1
                topic_entity_ids.add(int(row["entity_id"]))

        recalled = list(lexical_scores)
        recalled.extend(sorted(topic_entity_ids - set(recalled)))
        return RecallResult(
            entity_ids=recalled,
            lexical_scores=lexical_scores,
            total_documents=total_documents,
            reviewed_documents=reviewed_documents,
            document_frequency=document_frequency,
            concept_document_frequency=concept_document_frequency,
        )

    def vector_metadata(self) -> dict[str, str]:
        """Return vector-index metadata; an empty mapping means not built."""

        metadata = self.metadata()
        try:
            declared_count = int(metadata.get("vector_count", "0"))
        except ValueError as exc:
            raise SearchIndexError("vector index metadata is invalid") from exc
        if declared_count == 0:
            return {}
        if declared_count < 0:
            raise SearchIndexError("vector index metadata is invalid")
        required = (
            "vector_provider_fingerprint",
            "vector_model",
            "vector_dimensions",
            "vector_count",
        )
        if any(not metadata.get(key) for key in required):
            raise SearchIndexError("vector index metadata is incomplete")
        try:
            dimensions = int(metadata["vector_dimensions"])
        except ValueError as exc:
            raise SearchIndexError("vector index metadata is invalid") from exc
        if declared_count <= 0 or dimensions <= 0:
            raise SearchIndexError("vector index metadata is invalid")
        actual_count = int(
            self.connection.execute(
                """
                SELECT COUNT(*)
                FROM vector_embedding
                WHERE provider_fingerprint = ? AND dimensions = ?
                """,
                (metadata["vector_provider_fingerprint"], dimensions),
            ).fetchone()[0]
        )
        if actual_count != declared_count:
            raise SearchIndexError(
                "vector index is incomplete; rebuild it with --with-vectors"
            )
        return {key: metadata[key] for key in required}

    def vector_recall(
        self,
        *,
        allowed_entity_ids: Sequence[int],
        query_vector: Sequence[float],
        provider_fingerprint: str,
        limit: int = 500,
        min_similarity: float = 0.35,
        approximate: bool = False,
    ) -> VectorRecallResult:
        """Recall semantically similar entities inside the hard-filtered set.

        Exact cosine scan is the default because retrieval completeness is more
        important than latency for this project.  The compact sign-bit
        shortlist remains available as an explicit acceleration option.
        """

        if limit < 1:
            raise ValueError("vector recall limit must be positive")
        if not -1.0 <= min_similarity <= 1.0:
            raise ValueError("vector minimum similarity must be between -1 and 1")
        metadata = self.vector_metadata()
        if not metadata:
            raise SearchIndexError(
                "vector index is not built; run python3 -m scripts.build_legacy_index --with-vectors"
            )
        if metadata["vector_provider_fingerprint"] != provider_fingerprint:
            raise SearchIndexError(
                "query embedding provider does not match the vector index"
            )
        dimensions = int(metadata["vector_dimensions"])
        normalized_query = _normalize_vector(query_vector)
        if len(normalized_query) != dimensions:
            raise SearchIndexError(
                f"query vector has {len(normalized_query)} dimensions; "
                f"the index uses {dimensions}"
            )

        allowed = sorted(set(int(value) for value in allowed_entity_ids))
        if not allowed:
            return VectorRecallResult([], {}, metadata["vector_model"], dimensions)
        self._replace_allowed_entities(allowed)

        if approximate:
            query_signs = _sign_integer(normalized_query)
            shortlist_size = min(len(allowed), max(200, limit * 4))
            shortlist_heap: list[tuple[float, int, int]] = []
            rows = self.connection.execute(
                """
                SELECT vector_embedding.entity_id, vector_embedding.sign_bits
                FROM vector_embedding
                JOIN temp.query_allowed USING (entity_id)
                WHERE vector_embedding.provider_fingerprint = ?
                  AND vector_embedding.dimensions = ?
                """,
                (provider_fingerprint, dimensions),
            )
            for row in rows:
                entity_id = int(row["entity_id"])
                stored_signs = int.from_bytes(bytes(row["sign_bits"]), "little")
                hamming_distance = (query_signs ^ stored_signs).bit_count()
                approximate_similarity = 1.0 - 2.0 * hamming_distance / dimensions
                item = (approximate_similarity, -entity_id, entity_id)
                if len(shortlist_heap) < shortlist_size:
                    heapq.heappush(shortlist_heap, item)
                elif item > shortlist_heap[0]:
                    heapq.heapreplace(shortlist_heap, item)
            shortlist = [item[2] for item in shortlist_heap]
            if not shortlist:
                return VectorRecallResult([], {}, metadata["vector_model"], dimensions)
            self._replace_vector_shortlist(shortlist)
            vector_join = "JOIN temp.vector_shortlist USING (entity_id)"
        else:
            vector_join = "JOIN temp.query_allowed USING (entity_id)"

        similarities: dict[int, float] = {}
        rows = self.connection.execute(
            f"""
            SELECT vector_embedding.entity_id, vector_embedding.vector
            FROM vector_embedding
            {vector_join}
            WHERE vector_embedding.provider_fingerprint = ?
              AND vector_embedding.dimensions = ?
            """,
            (provider_fingerprint, dimensions),
        )
        for row in rows:
            entity_id = int(row["entity_id"])
            stored_vector = _unpack_float32(bytes(row["vector"]), dimensions)
            similarity = max(
                -1.0,
                min(1.0, sum(left * right for left, right in zip(normalized_query, stored_vector))),
            )
            if similarity >= min_similarity:
                similarities[entity_id] = similarity
        ordered = sorted(similarities, key=lambda item: (-similarities[item], item))[
            :limit
        ]
        return VectorRecallResult(
            entity_ids=ordered,
            similarities={entity_id: similarities[entity_id] for entity_id in ordered},
            model=metadata["vector_model"],
            dimensions=dimensions,
        )

    def _replace_allowed_entities(self, entity_ids: Sequence[int]) -> None:
        self.connection.execute(
            "CREATE TEMP TABLE IF NOT EXISTS query_allowed "
            "(entity_id INTEGER PRIMARY KEY) WITHOUT ROWID"
        )
        self.connection.execute("DELETE FROM temp.query_allowed")
        self.connection.executemany(
            "INSERT INTO temp.query_allowed(entity_id) VALUES (?)",
            ((entity_id,) for entity_id in entity_ids),
        )

    def _replace_vector_shortlist(self, entity_ids: Sequence[int]) -> None:
        self.connection.execute(
            "CREATE TEMP TABLE IF NOT EXISTS vector_shortlist "
            "(entity_id INTEGER PRIMARY KEY) WITHOUT ROWID"
        )
        self.connection.execute("DELETE FROM temp.vector_shortlist")
        self.connection.executemany(
            "INSERT INTO temp.vector_shortlist(entity_id) VALUES (?)",
            ((entity_id,) for entity_id in entity_ids),
        )


def _create_schema(connection: sqlite3.Connection, record_columns: Sequence[str]) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        PRAGMA temp_store = MEMORY;
        PRAGMA foreign_keys = ON;

        CREATE TABLE index_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE venue (
            entity_id INTEGER PRIMARY KEY,
            canonical_name TEXT NOT NULL,
            record_type TEXT NOT NULL,
            abbreviation TEXT NOT NULL,
            has_reviewed_scope INTEGER NOT NULL CHECK (has_reviewed_scope IN (0, 1)),
            target_status TEXT NOT NULL,
            semantic_text TEXT NOT NULL
        );

        CREATE TABLE ranking (
            entity_id INTEGER NOT NULL REFERENCES venue(entity_id),
            row_id INTEGER NOT NULL,
            dataset TEXT NOT NULL,
            version_year TEXT NOT NULL,
            level TEXT NOT NULL,
            record_type TEXT NOT NULL,
            area TEXT NOT NULL,
            area_en TEXT NOT NULL,
            taxonomy_scope TEXT NOT NULL,
            PRIMARY KEY (entity_id, row_id)
        ) WITHOUT ROWID;

        CREATE INDEX idx_ranking_target
            ON ranking(dataset, level, entity_id);
        CREATE INDEX idx_ranking_type
            ON ranking(record_type, entity_id);

        CREATE TABLE venue_alias (
            entity_id INTEGER NOT NULL REFERENCES venue(entity_id),
            alias_kind TEXT NOT NULL,
            alias TEXT NOT NULL,
            normalized_alias TEXT NOT NULL,
            PRIMARY KEY (entity_id, alias_kind, normalized_alias)
        ) WITHOUT ROWID;

        CREATE INDEX idx_alias_lookup
            ON venue_alias(normalized_alias, alias_kind, entity_id);

        CREATE TABLE scope (
            scope_id TEXT PRIMARY KEY,
            entity_id INTEGER NOT NULL REFERENCES venue(entity_id),
            summary TEXT NOT NULL,
            topics_zh TEXT NOT NULL,
            topics_en TEXT NOT NULL,
            topic_tags TEXT NOT NULL,
            article_types TEXT NOT NULL,
            accepts_original_research TEXT NOT NULL,
            submission_mode TEXT NOT NULL,
            scope_context TEXT NOT NULL,
            scope_year TEXT NOT NULL,
            out_of_scope TEXT NOT NULL,
            source_type TEXT NOT NULL,
            review_status TEXT NOT NULL,
            secondary_source_urls TEXT NOT NULL,
            target_status TEXT NOT NULL
        );

        CREATE INDEX idx_scope_entity ON scope(entity_id);

        CREATE TABLE topic_posting (
            topic_tag TEXT NOT NULL,
            entity_id INTEGER NOT NULL REFERENCES venue(entity_id),
            PRIMARY KEY (topic_tag, entity_id)
        ) WITHOUT ROWID;

        CREATE INDEX idx_topic_entity ON topic_posting(entity_id, topic_tag);

        CREATE TABLE term_posting (
            term TEXT NOT NULL,
            entity_id INTEGER NOT NULL REFERENCES venue(entity_id),
            source INTEGER NOT NULL CHECK (source IN (0, 1)),
            PRIMARY KEY (term, entity_id, source)
        ) WITHOUT ROWID;

        CREATE INDEX idx_term_entity ON term_posting(entity_id, term, source);

        CREATE TABLE vector_embedding (
            entity_id INTEGER PRIMARY KEY REFERENCES venue(entity_id),
            provider_fingerprint TEXT NOT NULL,
            model TEXT NOT NULL,
            dimensions INTEGER NOT NULL CHECK (dimensions > 0),
            text_hash TEXT NOT NULL,
            vector BLOB NOT NULL,
            sign_bits BLOB NOT NULL
        );

        CREATE INDEX idx_vector_provider
            ON vector_embedding(provider_fingerprint, dimensions, entity_id);

        CREATE VIRTUAL TABLE venue_fts USING fts5(
            entity_id UNINDEXED,
            name,
            abbreviation,
            area,
            taxonomy_scope,
            curated_scope,
            curated_topics,
            official_scope,
            tokenize = "unicode61 remove_diacritics 2 tokenchars '+#.'"
        );
        """
    )
    dynamic_columns = []
    for column in record_columns:
        data_type = "INTEGER" if column == "row_id" else "TEXT"
        constraint = " PRIMARY KEY" if column == "row_id" else " NOT NULL"
        dynamic_columns.append(f'"{column}" {data_type}{constraint}')
    connection.execute(
        "CREATE TABLE venue_record ("
        "entity_id INTEGER NOT NULL REFERENCES venue(entity_id), "
        + ", ".join(dynamic_columns)
        + ")"
    )
    connection.execute(
        "CREATE INDEX idx_venue_record_entity "
        "ON venue_record(entity_id, row_id)"
    )


def _populate_index(
    connection: sqlite3.Connection,
    records: Sequence[Any],
    groups: Sequence[Sequence[Any]],
    *,
    record_columns: Sequence[str],
    tokenize: Callable[[str], Mapping[str, int]],
    normalize_alias: Callable[[str | None], str],
    display_name_for_group: Callable[[Sequence[Any]], str],
    matching_document_for_group: Callable[[Sequence[Any]], Mapping[str, str]],
    digest: str,
) -> None:
    metadata = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "source_digest": digest,
        "record_columns": json.dumps(list(record_columns), ensure_ascii=False),
        "built_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "record_count": str(len(records)),
        "entity_count": str(len(groups)),
        "vector_count": "0",
    }
    connection.executemany(
        "INSERT INTO index_meta(key, value) VALUES (?, ?)", metadata.items()
    )

    record_placeholders = ", ".join("?" for _ in range(len(record_columns) + 1))
    record_column_sql = ", ".join(
        ["entity_id", *(f'"{column}"' for column in record_columns)]
    )
    record_sql = (
        f"INSERT INTO venue_record({record_column_sql}) "
        f"VALUES ({record_placeholders})"
    )

    for group in sorted(groups, key=lambda current: min(row.row_id for row in current)):
        if not group:
            continue
        entity_id = min(int(record.row_id) for record in group)
        record_types = {record.record_type for record in group}
        if len(record_types) != 1:
            raise SearchIndexError(
                f"entity {entity_id} mixes record types: {sorted(record_types)}"
            )
        abbreviations = _unique_text(record.abbreviation for record in group)
        topic_tags = _unique_text(
            topic_tag
            for record in group
            if record.curated_scope_status == "approved"
            for topic_tag in _split_terms(record.curated_topic_tags)
        )
        target_statuses = _unique_text(
            record.curated_target_status
            for record in group
            if record.curated_scope_status == "approved"
            and record.curated_target_status
        )
        document = matching_document_for_group(group)
        semantic_text = _semantic_text(
            document,
            has_reviewed_scope=bool(topic_tags),
        )
        connection.execute(
            """
            INSERT INTO venue(
                entity_id, canonical_name, record_type, abbreviation,
                has_reviewed_scope, target_status, semantic_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entity_id,
                display_name_for_group(group),
                next(iter(record_types)),
                abbreviations[0] if abbreviations else "",
                int(bool(topic_tags)),
                ";".join(target_statuses),
                semantic_text,
            ),
        )

        record_rows = []
        for record in group:
            values = []
            for column in record_columns:
                value = getattr(record, column)
                values.append(int(value) if column == "row_id" else str(value or ""))
            record_rows.append((entity_id, *values))
            connection.execute(
                """
                INSERT INTO ranking(
                    entity_id, row_id, dataset, version_year, level,
                    record_type, area, area_en, taxonomy_scope
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entity_id,
                    record.row_id,
                    record.dataset,
                    record.version_year,
                    record.level,
                    record.record_type,
                    record.area,
                    record.area_en,
                    record.taxonomy_scope,
                ),
            )
        connection.executemany(record_sql, record_rows)

        aliases: set[tuple[str, str, str]] = set()
        for record in group:
            if record.name:
                aliases.add(("name", record.name, normalize_alias(record.name)))
            if record.abbreviation:
                normalized = re.sub(r"[^a-z0-9]+", "", record.abbreviation.casefold())
                aliases.add(("abbreviation", record.abbreviation, normalized))
            for value in (record.issn, record.eissn):
                normalized = re.sub(r"[^0-9x]+", "", value.casefold())
                if normalized:
                    aliases.add(("issn", value, normalized))
        connection.executemany(
            """
            INSERT OR IGNORE INTO venue_alias(
                entity_id, alias_kind, alias, normalized_alias
            ) VALUES (?, ?, ?, ?)
            """,
            (
                (entity_id, kind, alias, normalized)
                for kind, alias, normalized in aliases
                if normalized
            ),
        )

        seen_scope_ids: set[str] = set()
        for record in group:
            if (
                record.curated_scope_status != "approved"
                or not record.curated_scope_id
                or record.curated_scope_id in seen_scope_ids
            ):
                continue
            seen_scope_ids.add(record.curated_scope_id)
            connection.execute(
                """
                INSERT INTO scope(
                    scope_id, entity_id, summary, topics_zh, topics_en,
                    topic_tags, article_types, accepts_original_research,
                    submission_mode, scope_context, scope_year, out_of_scope,
                    source_type, review_status, secondary_source_urls,
                    target_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.curated_scope_id,
                    entity_id,
                    record.curated_scope,
                    record.curated_topics_zh,
                    record.curated_topics_en,
                    record.curated_topic_tags,
                    record.curated_article_types,
                    record.curated_accepts_original_research,
                    record.curated_submission_mode,
                    record.curated_scope_context,
                    record.curated_scope_year,
                    record.curated_out_of_scope,
                    record.curated_scope_basis,
                    record.curated_scope_status,
                    record.curated_secondary_source_urls,
                    record.curated_target_status,
                ),
            )
        connection.executemany(
            "INSERT INTO topic_posting(topic_tag, entity_id) VALUES (?, ?)",
            ((topic_tag, entity_id) for topic_tag in topic_tags),
        )

        tokenized_document = {
            field_name: sorted(tokenize(document.get(field_name, "")))
            for field_name in FTS_COLUMNS_WITH_OFFICIAL
        }
        connection.execute(
            """
            INSERT INTO venue_fts(
                entity_id, name, abbreviation, area, taxonomy_scope,
                curated_scope, curated_topics, official_scope
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entity_id,
                " ".join(tokenized_document["name"]),
                " ".join(tokenized_document["abbreviation"]),
                " ".join(tokenized_document["area"]),
                " ".join(tokenized_document["taxonomy_scope"]),
                " ".join(tokenized_document["curated_scope"]),
                " ".join(tokenized_document["curated_topics"]),
                " ".join(tokenized_document["official_scope"]),
            ),
        )
        default_terms = set().union(
            *(set(tokenized_document[field]) for field in FTS_COLUMNS_WITHOUT_OFFICIAL)
        )
        official_terms = set(tokenized_document["official_scope"])
        connection.executemany(
            "INSERT INTO term_posting(term, entity_id, source) VALUES (?, ?, ?)",
            (
                *((term, entity_id, 0) for term in sorted(default_terms)),
                *((term, entity_id, 1) for term in sorted(official_terms)),
            ),
        )

    connection.commit()
    connection.execute("ANALYZE")
    connection.execute("PRAGMA optimize")
    connection.commit()


def _read_only_connection(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _target_predicate(
    targets: Sequence[tuple[str, str]], *, alias: str = ""
) -> tuple[str, list[str]]:
    prefix = f"{alias}." if alias else ""
    conditions = []
    parameters: list[str] = []
    for dataset, level in targets:
        conditions.append(f"({prefix}dataset = ? AND {prefix}level = ?)")
        parameters.extend((dataset, level))
    return " OR ".join(conditions) or "0", parameters


def _fts_quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _fts_column_query(columns: Sequence[str], tokens: Sequence[str]) -> str:
    column_filter = " ".join(columns)
    token_query = " OR ".join(_fts_quote(token) for token in tokens)
    return f"{{{column_filter}}} : ({token_query})"


def _split_terms(value: str | None) -> list[str]:
    return [
        term.strip()
        for term in re.split(r"[;；|]+", value or "")
        if term.strip()
    ]


def _unique_text(values: Iterable[str]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _semantic_text(
    document: Mapping[str, str], *, has_reviewed_scope: bool
) -> str:
    if has_reviewed_scope:
        fields_to_embed = (
            "name",
            "abbreviation",
            "area",
            "taxonomy_scope",
            "curated_scope",
            "curated_topics",
        )
    else:
        # Unreviewed entries often share only a broad taxonomy.  Excluding the
        # venue name lets those identical descriptions share one cached vector.
        fields_to_embed = ("area", "taxonomy_scope")
    parts = _unique_text(document.get(field, "") for field in fields_to_embed)
    if not parts:
        parts = _unique_text((document.get("name", ""), document.get("abbreviation", "")))
    return " ".join(" ".join(parts).split())


def _normalize_vector(values: Sequence[float]) -> list[float]:
    if not values:
        raise SearchIndexError("query vector cannot be empty")
    normalized: list[float] = []
    squared_norm = 0.0
    for value in values:
        number = float(value)
        if not math.isfinite(number):
            raise SearchIndexError("query vector contains a non-finite value")
        normalized.append(number)
        squared_norm += number * number
    if squared_norm <= 0 or not math.isfinite(squared_norm):
        raise SearchIndexError("query vector has zero norm")
    scale = 1.0 / math.sqrt(squared_norm)
    return [value * scale for value in normalized]


def _sign_integer(values: Sequence[float]) -> int:
    result = 0
    for index, value in enumerate(values):
        if value >= 0:
            result |= 1 << index
    return result


def _unpack_float32(blob: bytes, dimensions: int) -> list[float]:
    expected_size = dimensions * 4
    if len(blob) != expected_size:
        raise SearchIndexError(
            f"stored vector has {len(blob)} bytes; expected {expected_size}"
        )
    values = array.array("f")
    values.frombytes(blob)
    if sys.byteorder != "little":
        values.byteswap()
    return list(values)


def _chunks(values: Sequence[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield list(values[start : start + size])
