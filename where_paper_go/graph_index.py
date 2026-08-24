#!/usr/bin/env python3
"""File-persisted property graph for venue retrieval.

This backend intentionally uses no relational database and no Neo4j server.
The graph is built deterministically from the validated CSV/TSV sources, saved
as one atomically replaced gzip-compressed property-graph snapshot, and loaded
into adjacency/inverted indexes for queries.

The snapshot is also suitable for deterministic export to LightRAG's
``insert_custom_kg`` format. LightRAG is mandatory for topical semantic
retrieval, while hard ranking filters never depend on an LLM-generated edge.
"""

from __future__ import annotations

import array
import base64
from collections import Counter, defaultdict
from dataclasses import dataclass, fields
from datetime import datetime, timezone
import gzip
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Callable, Iterable, Mapping, Sequence

try:
    import numpy as _numpy
except ImportError:  # pragma: no cover - the scalar path remains supported.
    _numpy = None


GRAPH_SCHEMA_VERSION = "2"
GRAPH_VECTOR_SCHEMA_VERSION = "1"
DEFAULT_GRAPH_FILE = "venue_graph.json.gz"
SOURCE_FILE_NAMES = (
    "ccf_conferences_2026.csv",
    "th_cpl_partition_2019.csv",
    "cas_partition_2025.csv",
    "jcr_partition_2025.csv",
    "curated_venue_scopes.tsv",
)
SEARCH_FIELDS_WITHOUT_AUTOMATIC = (
    "name",
    "abbreviation",
    "area",
    "taxonomy_scope",
    "curated_scope",
    "curated_topics",
)
SEARCH_FIELDS_WITH_AUTOMATIC = (
    *SEARCH_FIELDS_WITHOUT_AUTOMATIC,
    "official_scope",
)
FIELD_WEIGHTS = {
    "name": 2.5,
    "abbreviation": 4.0,
    "area": 3.5,
    "taxonomy_scope": 2.5,
    "curated_scope": 5.0,
    "curated_topics": 5.5,
    "official_scope": 1.0,
}
GRAPH_TOPIC_MIN_STRENGTH = 0.50
GRAPH_TOPIC_MAX_NEIGHBORS = 5


class GraphIndexError(RuntimeError):
    """Raised when a graph snapshot is missing, stale, or inconsistent."""


@dataclass(frozen=True)
class GraphFreshness:
    fresh: bool
    reason: str
    source_digest: str


@dataclass(frozen=True)
class GraphBuildResult:
    path: Path
    source_digest: str
    record_count: int
    entity_count: int
    node_count: int
    edge_count: int


@dataclass(frozen=True)
class GraphRecallResult:
    entity_ids: list[int]
    lexical_scores: Mapping[int, float]
    total_documents: int
    reviewed_documents: int
    document_frequency: Mapping[str, int]
    concept_document_frequency: Mapping[str, int]
    graph_scores: Mapping[int, float]
    graph_paths: Mapping[int, tuple[str, ...]]


@dataclass(frozen=True)
class GraphVectorRecallResult:
    entity_ids: list[int]
    similarities: Mapping[int, float]
    model: str
    dimensions: int


@dataclass(frozen=True)
class GraphVectorBuildResult:
    path: Path
    entity_count: int
    dimensions: int
    model: str
    provider_fingerprint: str


def default_graph_path(data_dir: Path) -> Path:
    return data_dir / DEFAULT_GRAPH_FILE


def vector_path_for_graph(graph_path: Path) -> Path:
    """Return the deterministic vector sidecar path for a graph snapshot."""

    name = graph_path.name
    if name.endswith(".json.gz"):
        name = name[: -len(".json.gz")] + "_vectors.json.gz"
    else:
        name += "_vectors.json.gz"
    return graph_path.with_name(name)


def graph_source_digest(data_dir: Path) -> str:
    digest = hashlib.sha256()
    digest.update(f"graph-schema:{GRAPH_SCHEMA_VERSION}\n".encode())
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


def inspect_graph(
    graph_path: Path,
    data_dir: Path,
    *,
    expected_digest: str | None = None,
) -> GraphFreshness:
    digest = expected_digest or graph_source_digest(data_dir)
    if not graph_path.exists():
        return GraphFreshness(False, "graph_missing", digest)
    try:
        metadata = _read_graph_metadata(graph_path)
    except (OSError, EOFError, UnicodeError, json.JSONDecodeError, GraphIndexError):
        return GraphFreshness(False, "graph_unreadable", digest)
    if metadata.get("schema_version") != GRAPH_SCHEMA_VERSION:
        return GraphFreshness(False, "schema_changed", digest)
    if metadata.get("source_digest") != digest:
        return GraphFreshness(False, "source_changed", digest)
    return GraphFreshness(True, "fresh", digest)


def build_graph(
    graph_path: Path,
    data_dir: Path,
    records: Sequence[Any],
    groups: Sequence[Sequence[Any]],
    *,
    tokenize: Callable[[str], Mapping[str, int]],
    normalize_alias: Callable[[str | None], str],
    display_name_for_group: Callable[[Sequence[Any]], str],
    matching_document_for_group: Callable[[Sequence[Any]], Mapping[str, str]],
    expected_digest: str | None = None,
) -> GraphBuildResult:
    """Build and atomically replace a complete property-graph snapshot."""

    if not records:
        raise GraphIndexError("cannot build a graph without venue records")
    graph_path = graph_path.resolve()
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    digest = expected_digest or graph_source_digest(data_dir)
    record_columns = [field.name for field in fields(type(records[0]))]

    record_nodes: list[dict[str, Any]] = []
    venue_nodes: list[dict[str, Any]] = []
    scope_nodes: list[dict[str, Any]] = []
    topic_nodes: dict[str, dict[str, Any]] = {}
    ranking_nodes: dict[str, dict[str, Any]] = {}
    area_nodes: dict[str, dict[str, Any]] = {}
    source_nodes: dict[str, dict[str, Any]] = {}
    edges: list[list[Any]] = []
    topic_frequency: Counter[str] = Counter()
    topic_pairs: Counter[tuple[str, str]] = Counter()
    seen_record_ids: set[int] = set()
    seen_scope_ids: set[str] = set()
    scope_owners: dict[str, int] = {}

    for group in sorted(groups, key=lambda current: min(row.row_id for row in current)):
        if not group:
            continue
        entity_id = min(int(record.row_id) for record in group)
        venue_node_id = f"venue:{entity_id}"
        record_types = {str(record.record_type) for record in group}
        if len(record_types) != 1:
            raise GraphIndexError(
                f"entity {entity_id} mixes record types: {sorted(record_types)}"
            )
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
        document = {
            field: str(value or "")
            for field, value in matching_document_for_group(group).items()
        }
        token_fields = {
            field: sorted(tokenize(document.get(field, "")))
            for field in SEARCH_FIELDS_WITH_AUTOMATIC
        }
        aliases: list[dict[str, str]] = []
        alias_keys: set[tuple[str, str]] = set()
        for record in group:
            alias_values = (
                ("name", record.name, normalize_alias(record.name)),
                (
                    "abbreviation",
                    record.abbreviation,
                    re.sub(r"[^a-z0-9]+", "", record.abbreviation.casefold()),
                ),
                (
                    "issn",
                    record.issn,
                    re.sub(r"[^0-9x]+", "", record.issn.casefold()),
                ),
                (
                    "issn",
                    record.eissn,
                    re.sub(r"[^0-9x]+", "", record.eissn.casefold()),
                ),
            )
            for kind, value, normalized in alias_values:
                key = (kind, normalized)
                if not value or not normalized or key in alias_keys:
                    continue
                alias_keys.add(key)
                aliases.append(
                    {"kind": kind, "value": str(value), "normalized": normalized}
                )

        record_ids: list[int] = []
        for record in sorted(group, key=lambda item: item.row_id):
            row_id = int(record.row_id)
            if row_id in seen_record_ids:
                raise GraphIndexError(f"record node {row_id} is duplicated")
            seen_record_ids.add(row_id)
            record_ids.append(row_id)
            properties = {
                column: int(getattr(record, column))
                if column == "row_id"
                else str(getattr(record, column) or "")
                for column in record_columns
            }
            record_nodes.append(
                {
                    "id": f"record:{row_id}",
                    "type": "ranking_record",
                    "properties": properties,
                }
            )
            edges.append(
                [venue_node_id, "HAS_RANKING_RECORD", f"record:{row_id}", {}]
            )
            ranking_id = f"ranking:{record.dataset}:{record.level}"
            ranking_nodes.setdefault(
                ranking_id,
                {
                    "id": ranking_id,
                    "type": "ranking",
                    "properties": {
                        "dataset": str(record.dataset),
                        "level": str(record.level),
                    },
                },
            )
            edges.append(
                [
                    f"record:{row_id}",
                    "RANKED_IN",
                    ranking_id,
                    {"version_year": str(record.version_year)},
                ]
            )
            area_value = str(record.area or record.taxonomy_scope or "").strip()
            if area_value:
                area_id = "area:" + hashlib.sha256(
                    area_value.casefold().encode("utf-8")
                ).hexdigest()[:20]
                area_nodes.setdefault(
                    area_id,
                    {
                        "id": area_id,
                        "type": "area",
                        "properties": {"name": area_value},
                    },
                )
                edges.append([f"record:{row_id}", "CLASSIFIED_AS", area_id, {}])

        for topic_tag in topic_tags:
            topic_id = f"topic:{topic_tag}"
            topic_nodes.setdefault(
                topic_id,
                {
                    "id": topic_id,
                    "type": "topic",
                    "properties": {"tag": topic_tag},
                },
            )
            edges.append(
                [
                    venue_node_id,
                    "ACCEPTS_TOPIC",
                    topic_id,
                    {"review_status": "approved", "confidence": 1.0},
                ]
            )
            topic_frequency[topic_tag] += 1
        for index, left in enumerate(sorted(topic_tags)):
            for right in sorted(topic_tags)[index + 1 :]:
                topic_pairs[(left, right)] += 1

        for record in group:
            if (
                record.curated_scope_status != "approved"
                or not record.curated_scope_id
            ):
                continue
            previous_owner = scope_owners.setdefault(record.curated_scope_id, entity_id)
            if previous_owner != entity_id:
                raise GraphIndexError(
                    f"scope {record.curated_scope_id} belongs to multiple venues"
                )
            if record.curated_scope_id in seen_scope_ids:
                continue
            seen_scope_ids.add(record.curated_scope_id)
            scope_id = f"scope:{record.curated_scope_id}"
            scope_properties = {
                "scope_id": str(record.curated_scope_id),
                "summary": str(record.curated_scope),
                "topics_zh": str(record.curated_topics_zh),
                "topics_en": str(record.curated_topics_en),
                "topic_tags": str(record.curated_topic_tags),
                "article_types": str(record.curated_article_types),
                "accepts_original_research": str(
                    record.curated_accepts_original_research
                ),
                "submission_mode": str(record.curated_submission_mode),
                "scope_context": str(record.curated_scope_context),
                "scope_year": str(record.curated_scope_year),
                "out_of_scope": str(record.curated_out_of_scope),
                "source_type": str(record.curated_scope_basis),
                "review_status": str(record.curated_scope_status),
                "secondary_source_urls": str(
                    record.curated_secondary_source_urls
                ),
                "target_status": str(record.curated_target_status),
            }
            scope_nodes.append(
                {"id": scope_id, "type": "submission_scope", "properties": scope_properties}
            )
            edges.append(
                [venue_node_id, "HAS_SUBMISSION_SCOPE", scope_id, {"confidence": 1.0}]
            )
            for url in _split_terms(record.curated_secondary_source_urls):
                source_id = "source:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
                source_nodes.setdefault(
                    source_id,
                    {
                        "id": source_id,
                        "type": "evidence_source",
                        "properties": {"url": url},
                    },
                )
                edges.append([scope_id, "SUPPORTED_BY", source_id, {}])

        semantic_text = _semantic_text(document, has_reviewed_scope=bool(topic_tags))
        venue_nodes.append(
            {
                "id": venue_node_id,
                "type": "venue",
                "properties": {
                    "entity_id": entity_id,
                    "canonical_name": display_name_for_group(group),
                    "record_type": next(iter(record_types)),
                    "abbreviation": next(
                        (record.abbreviation for record in group if record.abbreviation),
                        "",
                    ),
                    "record_ids": record_ids,
                    "has_reviewed_scope": bool(topic_tags),
                    "target_status": target_statuses,
                    "topic_tags": topic_tags,
                    "aliases": aliases,
                    "document": document,
                    "token_fields": token_fields,
                    "semantic_text": semantic_text,
                },
            }
        )

    for (left, right), count in sorted(topic_pairs.items()):
        denominator = math.sqrt(topic_frequency[left] * topic_frequency[right])
        strength = count / denominator if denominator else 0.0
        properties = {"count": count, "strength": round(strength, 8)}
        edges.append([f"topic:{left}", "RELATED_TOPIC", f"topic:{right}", properties])
        edges.append([f"topic:{right}", "RELATED_TOPIC", f"topic:{left}", properties])

    nodes = [
        *venue_nodes,
        *record_nodes,
        *scope_nodes,
        *sorted(topic_nodes.values(), key=lambda item: item["id"]),
        *sorted(ranking_nodes.values(), key=lambda item: item["id"]),
        *sorted(area_nodes.values(), key=lambda item: item["id"]),
        *sorted(source_nodes.values(), key=lambda item: item["id"]),
    ]
    metadata = {
        "schema_version": GRAPH_SCHEMA_VERSION,
        "source_digest": digest,
        "semantic_digest": _semantic_documents_digest(venue_nodes),
        "built_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "record_columns": record_columns,
        "record_count": len(records),
        "entity_count": len(venue_nodes),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "vector_count": 0,
        "storage": "gzip_property_graph",
    }
    payload = {"metadata": metadata, "nodes": nodes, "edges": edges}

    temporary = tempfile.NamedTemporaryFile(
        prefix=f".{graph_path.name}.",
        suffix=".tmp",
        dir=graph_path.parent,
        delete=False,
    )
    temporary_path = Path(temporary.name)
    temporary.close()
    try:
        with gzip.open(temporary_path, "wt", encoding="utf-8", compresslevel=6) as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        with VenueGraphIndex(temporary_path) as generated:
            generated.validate()
        os.replace(temporary_path, graph_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    return GraphBuildResult(
        path=graph_path,
        source_digest=digest,
        record_count=len(records),
        entity_count=len(venue_nodes),
        node_count=len(nodes),
        edge_count=len(edges),
    )


class VenueGraphIndex:
    """Read/query facade over an immutable property-graph snapshot."""

    def __init__(self, path: Path, *, vector_path: Path | None = None):
        self.path = path.resolve()
        self.vector_path = (vector_path or vector_path_for_graph(self.path)).resolve()
        self._vector_payload: dict[str, Any] | None = None
        self._decoded_vectors: dict[int, tuple[str, list[float], bytes]] | None = None
        self._vector_matrix: Any | None = None
        self._vector_entity_order: list[int] | None = None
        self._vector_row_by_entity: dict[int, int] | None = None
        try:
            with gzip.open(self.path, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, EOFError, UnicodeError, json.JSONDecodeError) as exc:
            raise GraphIndexError(f"cannot read graph snapshot: {self.path}") from exc
        if not isinstance(payload, dict):
            raise GraphIndexError("graph snapshot root is not an object")
        self._payload = payload
        self._metadata = dict(payload.get("metadata") or {})
        if self._metadata.get("schema_version") != GRAPH_SCHEMA_VERSION:
            raise GraphIndexError("graph schema version is incompatible")
        self.record_columns = tuple(self._metadata.get("record_columns") or ())
        if not self.record_columns:
            raise GraphIndexError("graph record metadata is invalid")

        nodes = payload.get("nodes")
        edges = payload.get("edges")
        if not isinstance(nodes, list) or not isinstance(edges, list):
            raise GraphIndexError("graph nodes/edges are invalid")
        self.nodes: dict[str, dict[str, Any]] = {}
        for node in nodes:
            if not isinstance(node, dict) or not isinstance(node.get("id"), str):
                raise GraphIndexError("graph contains an invalid node")
            if node["id"] in self.nodes:
                raise GraphIndexError(f"duplicate graph node: {node['id']}")
            self.nodes[node["id"]] = node
        self.edges: list[tuple[str, str, str, Mapping[str, Any]]] = []
        self.outgoing: dict[str, list[tuple[str, str, Mapping[str, Any]]]] = defaultdict(list)
        self.incoming: dict[str, list[tuple[str, str, Mapping[str, Any]]]] = defaultdict(list)
        for edge in edges:
            if not isinstance(edge, list) or len(edge) != 4:
                raise GraphIndexError("graph contains an invalid edge")
            source, relation, target, properties = edge
            if source not in self.nodes or target not in self.nodes:
                raise GraphIndexError(f"edge references an unknown node: {edge[:3]}")
            if not isinstance(relation, str) or not isinstance(properties, dict):
                raise GraphIndexError("graph edge relation/properties are invalid")
            normalized = (source, relation, target, properties)
            self.edges.append(normalized)
            self.outgoing[source].append((relation, target, properties))
            self.incoming[target].append((relation, source, properties))

        self.venue_nodes: dict[int, dict[str, Any]] = {}
        self.record_nodes: dict[int, dict[str, Any]] = {}
        for node in nodes:
            properties = node.get("properties") or {}
            if node.get("type") == "venue":
                self.venue_nodes[int(properties["entity_id"])] = node
            elif node.get("type") == "ranking_record":
                self.record_nodes[int(properties["row_id"])] = node

        self.entity_record_ids: dict[int, list[int]] = {}
        self.ranking_to_entities: dict[tuple[str, str], set[int]] = defaultdict(set)
        self.topic_to_entities: dict[str, set[int]] = defaultdict(set)
        self.topic_neighbors: dict[str, dict[str, float]] = defaultdict(dict)
        self.term_default: dict[str, set[int]] = defaultdict(set)
        self.term_automatic: dict[str, set[int]] = defaultdict(set)
        self.entity_token_fields: dict[int, dict[str, set[str]]] = {}
        self.reviewed_entities: set[int] = set()
        self.alias_to_entities: dict[str, set[int]] = defaultdict(set)

        record_to_entity: dict[int, int] = {}
        for entity_id, node in self.venue_nodes.items():
            properties = node["properties"]
            self.entity_record_ids[entity_id] = []
            for alias in properties.get("aliases", []):
                normalized = str(alias.get("normalized") or "")
                if normalized:
                    self.alias_to_entities[normalized].add(entity_id)
            token_fields = {
                field: set(str(value) for value in values)
                for field, values in (properties.get("token_fields") or {}).items()
            }
            self.entity_token_fields[entity_id] = token_fields
            for field in SEARCH_FIELDS_WITHOUT_AUTOMATIC:
                for token in token_fields.get(field, set()):
                    self.term_default[token].add(entity_id)
            for token in token_fields.get("official_scope", set()):
                self.term_automatic[token].add(entity_id)

        for source, relation, target, properties in self.edges:
            if relation == "HAS_RANKING_RECORD":
                entity_id = int(self.nodes[source]["properties"]["entity_id"])
                row_id = int(self.nodes[target]["properties"]["row_id"])
                previous = record_to_entity.setdefault(row_id, entity_id)
                if previous != entity_id:
                    raise GraphIndexError(f"record {row_id} has multiple venue owners")
                self.entity_record_ids[entity_id].append(row_id)
            elif relation == "ACCEPTS_TOPIC":
                entity_id = int(self.nodes[source]["properties"]["entity_id"])
                topic = str(self.nodes[target]["properties"]["tag"])
                self.topic_to_entities[topic].add(entity_id)
                self.reviewed_entities.add(entity_id)
            elif relation == "RELATED_TOPIC":
                left = str(self.nodes[source]["properties"]["tag"])
                right = str(self.nodes[target]["properties"]["tag"])
                self.topic_neighbors[left][right] = float(
                    properties.get("strength", 0.0)
                )

        for source, relation, target, _properties in self.edges:
            if relation != "RANKED_IN":
                continue
            row_id = int(self.nodes[source]["properties"]["row_id"])
            entity_id = record_to_entity.get(row_id)
            if entity_id is None:
                raise GraphIndexError(f"record {row_id} has no venue edge")
            ranking = self.nodes[target]["properties"]
            self.ranking_to_entities[
                (str(ranking["dataset"]), str(ranking["level"]))
            ].add(entity_id)

        for entity_id, record_ids in self.entity_record_ids.items():
            record_ids.sort()
            declared = sorted(
                int(value)
                for value in self.venue_nodes[entity_id]["properties"].get(
                    "record_ids", []
                )
            )
            if declared != record_ids:
                raise GraphIndexError(
                    f"venue {entity_id} record properties disagree with graph edges"
                )

    def close(self) -> None:
        return None

    def __enter__(self) -> "VenueGraphIndex":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def metadata(self) -> dict[str, str]:
        return {key: str(value) for key, value in self._metadata.items()}

    def validate(self) -> None:
        record_count = int(self._metadata.get("record_count", -1))
        entity_count = int(self._metadata.get("entity_count", -1))
        node_count = int(self._metadata.get("node_count", -1))
        edge_count = int(self._metadata.get("edge_count", -1))
        if record_count != len(self.record_nodes):
            raise GraphIndexError("graph record count does not match metadata")
        if entity_count != len(self.venue_nodes):
            raise GraphIndexError("graph entity count does not match metadata")
        if node_count != len(self.nodes) or edge_count != len(self.edges):
            raise GraphIndexError("graph node/edge count does not match metadata")
        linked_records = {
            row_id for record_ids in self.entity_record_ids.values() for row_id in record_ids
        }
        if linked_records != set(self.record_nodes):
            raise GraphIndexError("graph contains unlinked or multiply linked records")

    def load_groups_for_targets(
        self, targets: Sequence[tuple[str, str]]
    ) -> list[tuple[int, list[dict[str, Any]]]]:
        entity_ids: set[int] = set()
        for dataset, level in targets:
            entity_ids.update(self.ranking_to_entities.get((dataset, level), set()))
        grouped: list[tuple[int, list[dict[str, Any]]]] = []
        for entity_id in sorted(entity_ids):
            records = []
            for row_id in sorted(self.entity_record_ids[entity_id]):
                properties = self.record_nodes[row_id]["properties"]
                records.append(
                    {column: properties.get(column, "") for column in self.record_columns}
                )
            grouped.append((entity_id, records))
        return grouped

    def recall(
        self,
        *,
        allowed_entity_ids: Sequence[int],
        query_tokens: Iterable[str],
        topic_tags: Iterable[str],
        include_official_scope: bool,
        lexical_limit: int | None = None,
    ) -> GraphRecallResult:
        allowed = set(int(value) for value in allowed_entity_ids) & set(self.venue_nodes)
        tokens = _unique_text(query_tokens)
        topics = _unique_text(topic_tags)
        if not allowed:
            return GraphRecallResult([], {}, 0, 0, {}, {}, {}, {})

        document_frequency: dict[str, int] = {}
        lexical_entities: set[int] = set()
        for token in tokens:
            posting = set(self.term_default.get(token, set()))
            if include_official_scope:
                posting.update(self.term_automatic.get(token, set()))
            matches = posting & allowed
            document_frequency[token] = len(matches)
            lexical_entities.update(matches)

        lexical_scores: dict[int, float] = {}
        for entity_id in lexical_entities:
            token_fields = self.entity_token_fields[entity_id]
            score = 0.0
            for token in tokens:
                best = max(
                    (
                        FIELD_WEIGHTS[field]
                        for field in (
                            SEARCH_FIELDS_WITH_AUTOMATIC
                            if include_official_scope
                            else SEARCH_FIELDS_WITHOUT_AUTOMATIC
                        )
                        if token in token_fields.get(field, set())
                    ),
                    default=0.0,
                )
                if best:
                    frequency = document_frequency.get(token, 0)
                    score += best * (math.log((len(allowed) + 1) / (frequency + 1)) + 1.0)
            lexical_scores[entity_id] = score
        lexical_order = sorted(
            lexical_scores,
            key=lambda entity_id: (-lexical_scores[entity_id], entity_id),
        )
        if lexical_limit is not None:
            lexical_order = lexical_order[: max(1, int(lexical_limit))]
            lexical_scores = {
                entity_id: lexical_scores[entity_id] for entity_id in lexical_order
            }

        concept_document_frequency: dict[str, int] = {}
        direct_topic_entities: set[int] = set()
        for topic in topics:
            matches = self.topic_to_entities.get(topic, set()) & allowed
            concept_document_frequency[topic] = len(matches)
            direct_topic_entities.update(matches)

        graph_scores: dict[int, float] = {}
        graph_paths: dict[int, tuple[str, ...]] = {}
        for seed in topics:
            neighbors = sorted(
                self.topic_neighbors.get(seed, {}).items(),
                key=lambda item: (-item[1], item[0]),
            )[:GRAPH_TOPIC_MAX_NEIGHBORS]
            for related, strength in neighbors:
                if strength < GRAPH_TOPIC_MIN_STRENGTH or related in topics:
                    continue
                for entity_id in self.topic_to_entities.get(related, set()) & allowed:
                    if entity_id in direct_topic_entities:
                        continue
                    score = 0.45 * strength
                    if score <= graph_scores.get(entity_id, 0.0):
                        continue
                    graph_scores[entity_id] = score
                    graph_paths[entity_id] = (
                        f"topic:{seed}",
                        "RELATED_TOPIC",
                        f"topic:{related}",
                        "ACCEPTS_TOPIC^-1",
                        f"venue:{entity_id}",
                    )

        recalled = list(lexical_order)
        seen = set(recalled)
        for entity_id in sorted(direct_topic_entities | set(graph_scores)):
            if entity_id not in seen:
                seen.add(entity_id)
                recalled.append(entity_id)
        return GraphRecallResult(
            entity_ids=recalled,
            lexical_scores=lexical_scores,
            total_documents=len(allowed),
            reviewed_documents=len(allowed & self.reviewed_entities),
            document_frequency=document_frequency,
            concept_document_frequency=concept_document_frequency,
            graph_scores=graph_scores,
            graph_paths=graph_paths,
        )

    def vector_metadata(self) -> dict[str, str]:
        payload = self._load_vector_payload(required=False)
        if payload is None:
            return {}
        return {
            key: str(value)
            for key, value in payload["metadata"].items()
            if key
            in {
                "vector_provider_fingerprint",
                "vector_model",
                "vector_dimensions",
                "vector_count",
                "vector_unique_text_count",
                "built_at",
            }
        }

    def preload_vectors(self) -> None:
        """Load and decode the immutable exact-vector sidecar into memory."""

        payload = self._load_vector_payload(required=True)
        assert payload is not None
        decoded = self._decode_vectors(payload)
        if _numpy is not None:
            self._numpy_vector_matrix(decoded)

    def vector_recall(
        self,
        *,
        allowed_entity_ids: Sequence[int],
        query_vector: Sequence[float],
        provider_fingerprint: str,
        limit: int = 500,
        min_similarity: float = 0.35,
        approximate: bool = False,
    ) -> GraphVectorRecallResult:
        if limit < 1:
            raise ValueError("vector recall limit must be positive")
        if not -1.0 <= min_similarity <= 1.0:
            raise ValueError("minimum vector similarity must be in [-1, 1]")
        payload = self._load_vector_payload(required=True)
        assert payload is not None
        metadata = payload["metadata"]
        if metadata["vector_provider_fingerprint"] != provider_fingerprint:
            raise GraphIndexError("embedding provider does not match graph vectors")
        dimensions = int(metadata["vector_dimensions"])
        normalized_query = _normalize_vector(query_vector)
        if len(normalized_query) != dimensions:
            raise GraphIndexError(
                f"query vector dimensions differ: expected {dimensions}, "
                f"got {len(normalized_query)}"
            )
        decoded = self._decode_vectors(payload)
        allowed = set(int(value) for value in allowed_entity_ids)
        entity_ids = [entity_id for entity_id in decoded if entity_id in allowed]
        if approximate and len(entity_ids) > limit:
            query_sign = _sign_bits(normalized_query)
            shortlist_size = min(len(entity_ids), max(limit, limit * 8))
            entity_ids = heapq.nsmallest(
                shortlist_size,
                entity_ids,
                key=lambda entity_id: _hamming_distance(
                    query_sign, decoded[entity_id][2]
                ),
            )
        scores: list[tuple[float, int]] = []
        if _numpy is not None and not approximate:
            matrix, row_by_entity = self._numpy_vector_matrix(decoded)
            row_ids = [row_by_entity[entity_id] for entity_id in entity_ids]
            query_array = _numpy.asarray(normalized_query, dtype=_numpy.float32)
            if len(row_ids) == matrix.shape[0] and all(
                row_id == index for index, row_id in enumerate(row_ids)
            ):
                # The normal all-catalog path can use the immutable matrix
                # directly.  NumPy advanced indexing would otherwise allocate
                # and copy roughly 80 MiB for every query at the current scale.
                similarities = matrix @ query_array
            elif len(row_ids) * 4 >= matrix.shape[0]:
                # Large filtered catalogs (notably JCR Q1--Q4) are cheaper to
                # score as one full matrix-vector product and then select a
                # tiny score vector.  Indexing matrix[row_ids] would copy most
                # of the 80 MiB matrix for every request.
                similarities = (matrix @ query_array)[row_ids]
            else:
                similarities = matrix[row_ids] @ query_array
            for entity_id, raw_similarity in zip(entity_ids, similarities.tolist()):
                similarity = max(-1.0, min(1.0, float(raw_similarity)))
                if similarity >= min_similarity:
                    scores.append((similarity, entity_id))
        else:
            for entity_id in entity_ids:
                _text_hash, vector, _sign_bits_blob = decoded[entity_id]
                similarity = sum(
                    query_value * stored_value
                    for query_value, stored_value in zip(normalized_query, vector)
                )
                similarity = max(-1.0, min(1.0, similarity))
                if similarity >= min_similarity:
                    scores.append((similarity, entity_id))
        scores.sort(key=lambda item: (-item[0], item[1]))
        scores = scores[:limit]
        return GraphVectorRecallResult(
            entity_ids=[entity_id for _similarity, entity_id in scores],
            similarities={entity_id: similarity for similarity, entity_id in scores},
            model=str(metadata["vector_model"]),
            dimensions=dimensions,
        )

    def semantic_documents(self) -> dict[int, str]:
        """Return graph venue-node text used for mandatory topical embeddings."""

        return {
            entity_id: str(node["properties"].get("semantic_text") or "")
            for entity_id, node in self.venue_nodes.items()
            if str(node["properties"].get("semantic_text") or "").strip()
        }

    def _load_vector_payload(self, *, required: bool) -> dict[str, Any] | None:
        if self._vector_payload is not None:
            return self._vector_payload
        if not self.vector_path.exists():
            if required:
                raise GraphIndexError(
                    "graph vectors are not built; run python3 -m scripts.build_graph --with-vectors"
                )
            return None
        try:
            with gzip.open(self.vector_path, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, EOFError, UnicodeError, json.JSONDecodeError) as exc:
            raise GraphIndexError(
                f"cannot read graph vector snapshot: {self.vector_path}"
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("vectors"), list):
            raise GraphIndexError("graph vector snapshot is invalid")
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            raise GraphIndexError("graph vector metadata is invalid")
        if metadata.get("schema_version") != GRAPH_VECTOR_SCHEMA_VERSION:
            raise GraphIndexError("graph vector schema version is incompatible")
        if metadata.get("graph_source_digest") != self._metadata.get("source_digest"):
            raise GraphIndexError("graph vectors are stale for the current source graph")
        if metadata.get("graph_semantic_digest") != self._metadata.get(
            "semantic_digest"
        ):
            raise GraphIndexError("graph vectors are stale for current semantic text")
        try:
            dimensions = int(metadata["vector_dimensions"])
            vector_count = int(metadata["vector_count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GraphIndexError("graph vector metadata is incomplete") from exc
        if dimensions < 8 or vector_count != len(payload["vectors"]):
            raise GraphIndexError("graph vector counts or dimensions are invalid")
        self._vector_payload = payload
        return payload

    def _decode_vectors(
        self, payload: Mapping[str, Any]
    ) -> dict[int, tuple[str, list[float], bytes]]:
        if self._decoded_vectors is not None:
            return self._decoded_vectors
        dimensions = int(payload["metadata"]["vector_dimensions"])
        decoded: dict[int, tuple[str, list[float], bytes]] = {}
        for row in payload["vectors"]:
            if not isinstance(row, list) or len(row) != 4:
                raise GraphIndexError("graph vector row is invalid")
            try:
                entity_id = int(row[0])
                text_hash = str(row[1])
                blob = base64.b64decode(str(row[2]), validate=True)
                sign_bits = base64.b64decode(str(row[3]), validate=True)
            except (TypeError, ValueError) as exc:
                raise GraphIndexError("graph vector encoding is invalid") from exc
            if entity_id not in self.venue_nodes or entity_id in decoded:
                raise GraphIndexError("graph vector entity is unknown or duplicated")
            if len(sign_bits) != (dimensions + 7) // 8:
                raise GraphIndexError("graph vector sign bits have invalid dimensions")
            vector = _unpack_float32(blob, dimensions)
            decoded[entity_id] = (text_hash, vector, sign_bits)
        self._decoded_vectors = decoded
        return decoded

    def _numpy_vector_matrix(
        self,
        decoded: Mapping[int, tuple[str, list[float], bytes]],
    ) -> tuple[Any, Mapping[int, int]]:
        """Return one immutable float32 matrix for fast exact cosine scans.

        The persisted representation and scalar fallback remain unchanged.
        Building the matrix once turns every later full-catalog query into a
        single BLAS matrix-vector product without changing the candidate set.
        """

        if _numpy is None:  # pragma: no cover - guarded by the caller.
            raise GraphIndexError("NumPy vector acceleration is unavailable")
        if (
            self._vector_matrix is None
            or self._vector_entity_order is None
            or self._vector_row_by_entity is None
        ):
            entity_order = list(decoded)
            matrix = _numpy.asarray(
                [decoded[entity_id][1] for entity_id in entity_order],
                dtype=_numpy.float32,
            )
            if matrix.ndim != 2 or matrix.shape[0] != len(entity_order):
                raise GraphIndexError("graph vector matrix is invalid")
            matrix.setflags(write=False)
            self._vector_matrix = matrix
            self._vector_entity_order = entity_order
            self._vector_row_by_entity = {
                entity_id: row for row, entity_id in enumerate(entity_order)
            }
        return self._vector_matrix, self._vector_row_by_entity

    def neighbors(
        self,
        node_id: str,
        *,
        relations: set[str] | None = None,
    ) -> list[tuple[str, str, Mapping[str, Any]]]:
        values = self.outgoing.get(node_id, [])
        if relations is None:
            return list(values)
        return [item for item in values if item[0] in relations]

    def graph_summary(self) -> dict[str, Any]:
        node_types = Counter(node.get("type", "unknown") for node in self.nodes.values())
        relation_types = Counter(relation for _source, relation, _target, _props in self.edges)
        return {
            "path": str(self.path),
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "vectors": int(self.vector_metadata().get("vector_count", "0")),
            "node_types": dict(sorted(node_types.items())),
            "relation_types": dict(sorted(relation_types.items())),
        }

    def to_lightrag_custom_kg(self) -> dict[str, list[dict[str, Any]]]:
        """Export deterministic venue/topic/ranking relations for LightRAG."""

        chunks: list[dict[str, Any]] = []
        entities: list[dict[str, Any]] = []
        relationships: list[dict[str, Any]] = []
        included_nodes = {
            node_id: node
            for node_id, node in self.nodes.items()
            if node.get("type") in {"venue", "topic", "ranking", "submission_scope"}
        }
        for node_id, node in sorted(included_nodes.items()):
            properties = node.get("properties") or {}
            entity_name = _lightrag_name(node_id, properties)
            description = _node_description(node, properties)
            source_id = f"graph-node:{node_id}"
            chunks.append(
                {
                    "content": description,
                    "source_id": source_id,
                    "file_path": str(self.path),
                }
            )
            entities.append(
                {
                    "entity_name": entity_name,
                    "entity_type": str(node.get("type") or "entity"),
                    "description": description,
                    "source_id": source_id,
                    "file_path": str(self.path),
                }
            )
        entity_names = [entity["entity_name"] for entity in entities]
        if len(entity_names) != len(set(entity_names)):
            raise GraphIndexError("LightRAG export contains colliding entity names")
        allowed_relations = {
            "ACCEPTS_TOPIC",
            "HAS_SUBMISSION_SCOPE",
            "RELATED_TOPIC",
        }
        for source, relation, target, properties in self.edges:
            if (
                relation not in allowed_relations
                or source not in included_nodes
                or target not in included_nodes
            ):
                continue
            source_properties = included_nodes[source].get("properties") or {}
            target_properties = included_nodes[target].get("properties") or {}
            source_id = f"graph-edge:{source}:{relation}:{target}"
            relationships.append(
                {
                    "src_id": _lightrag_name(source, source_properties),
                    "tgt_id": _lightrag_name(target, target_properties),
                    "description": _edge_description(relation, properties),
                    "keywords": relation.casefold().replace("_", " "),
                    "weight": float(properties.get("confidence", properties.get("strength", 1.0))),
                    "source_id": source_id,
                    "file_path": str(self.path),
                }
            )
        return {
            "chunks": chunks,
            "entities": entities,
            "relationships": relationships,
        }


def write_graph_vectors(
    graph_path: Path,
    vector_path: Path,
    *,
    provider_fingerprint: str,
    model: str,
    dimensions: int,
    unique_text_count: int,
    vectors: Mapping[int, tuple[str, bytes]],
) -> GraphVectorBuildResult:
    """Atomically persist normalized venue-node vectors without a database."""

    if dimensions < 8 or not vectors:
        raise GraphIndexError("graph vectors require data and at least 8 dimensions")
    with VenueGraphIndex(graph_path) as graph:
        expected_entities = set(graph.semantic_documents())
        source_digest = str(graph._metadata["source_digest"])
        semantic_digest = str(graph._metadata["semantic_digest"])
    if set(vectors) != expected_entities:
        missing = len(expected_entities - set(vectors))
        extra = len(set(vectors) - expected_entities)
        raise GraphIndexError(
            f"graph vector coverage differs from semantic nodes: missing={missing}, extra={extra}"
        )

    rows: list[list[Any]] = []
    for entity_id, (text_hash, blob) in sorted(vectors.items()):
        normalized = _unpack_float32(blob, dimensions)
        squared_norm = sum(value * value for value in normalized)
        if not 0.999 <= squared_norm <= 1.001:
            raise GraphIndexError(f"graph vector {entity_id} is not L2 normalized")
        rows.append(
            [
                int(entity_id),
                str(text_hash),
                base64.b64encode(blob).decode("ascii"),
                base64.b64encode(_sign_bits(normalized)).decode("ascii"),
            ]
        )
    metadata = {
        "schema_version": GRAPH_VECTOR_SCHEMA_VERSION,
        "graph_source_digest": source_digest,
        "graph_semantic_digest": semantic_digest,
        "built_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "vector_provider_fingerprint": provider_fingerprint,
        "vector_model": model,
        "vector_dimensions": dimensions,
        "vector_count": len(rows),
        "vector_unique_text_count": int(unique_text_count),
        "storage": "gzip_graph_vector_sidecar",
    }
    payload = {"metadata": metadata, "vectors": rows}
    vector_path = vector_path.resolve()
    vector_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(
        prefix=f".{vector_path.name}.",
        suffix=".tmp",
        dir=vector_path.parent,
        delete=False,
    )
    temporary_path = Path(temporary.name)
    temporary.close()
    try:
        with gzip.open(temporary_path, "wt", encoding="utf-8", compresslevel=6) as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        with VenueGraphIndex(graph_path, vector_path=temporary_path) as generated:
            loaded = generated._load_vector_payload(required=True)
            assert loaded is not None
            generated._decode_vectors(loaded)
        os.replace(temporary_path, vector_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return GraphVectorBuildResult(
        path=vector_path,
        entity_count=len(rows),
        dimensions=dimensions,
        model=model,
        provider_fingerprint=provider_fingerprint,
    )


def export_lightrag_custom_kg(graph_path: Path, output_path: Path) -> dict[str, int]:
    with VenueGraphIndex(graph_path) as graph:
        custom_kg = graph.to_lightrag_custom_kg()
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        dir=output_path.parent,
        mode="w",
        encoding="utf-8",
        delete=False,
    )
    temporary_path = Path(temporary.name)
    try:
        with temporary:
            json.dump(custom_kg, temporary, ensure_ascii=False, separators=(",", ":"))
            temporary.write("\n")
        os.replace(temporary_path, output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return {key: len(values) for key, values in custom_kg.items()}


def _semantic_text(document: Mapping[str, str], *, has_reviewed_scope: bool) -> str:
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
        fields_to_embed = ("area", "taxonomy_scope")
    parts = _unique_text(document.get(field, "") for field in fields_to_embed)
    if not parts:
        parts = _unique_text((document.get("name", ""), document.get("abbreviation", "")))
    return " ".join(" ".join(parts).split())


def _semantic_documents_digest(venue_nodes: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for node in sorted(
        venue_nodes,
        key=lambda value: int(value["properties"]["entity_id"]),
    ):
        properties = node["properties"]
        digest.update(str(properties["entity_id"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(properties.get("semantic_text") or "").encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _read_graph_metadata(path: Path) -> dict[str, Any]:
    """Read only the leading metadata object, without materializing all nodes."""

    decoder = json.JSONDecoder()
    buffer = ""
    start: int | None = None
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        while len(buffer) <= 1024 * 1024:
            chunk = handle.read(4096)
            if not chunk:
                break
            buffer += chunk
            if start is None:
                match = re.match(r'\s*\{\s*"metadata"\s*:\s*', buffer)
                if match:
                    start = match.end()
                elif len(buffer) >= 4096:
                    raise GraphIndexError("graph metadata is not the first property")
            if start is None:
                continue
            try:
                metadata, _end = decoder.raw_decode(buffer, start)
            except json.JSONDecodeError:
                continue
            if not isinstance(metadata, dict):
                raise GraphIndexError("graph metadata is not an object")
            return metadata
    raise GraphIndexError("graph metadata exceeds the supported prefix size")


def _split_terms(value: str | None) -> list[str]:
    return [term.strip() for term in re.split(r"[;；|]+", value or "") if term.strip()]


def _unique_text(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _lightrag_name(node_id: str, properties: Mapping[str, Any]) -> str:
    if node_id.startswith("venue:"):
        # LightRAG canonicalizes and merges equal entity names.  Keep the graph
        # node ID in the name so distinct same-title journals cannot collapse.
        return "VENUE::" + node_id.removeprefix("venue:") + "::" + str(
            properties.get("canonical_name") or node_id
        )
    if node_id.startswith("topic:"):
        return "TOPIC::" + str(properties.get("tag") or node_id.removeprefix("topic:"))
    if node_id.startswith("ranking:"):
        return "RANKING::" + ":".join(
            (str(properties.get("dataset") or ""), str(properties.get("level") or ""))
        )
    if node_id.startswith("scope:"):
        return "SCOPE::" + str(properties.get("scope_id") or node_id.removeprefix("scope:"))
    return node_id


def _node_description(node: Mapping[str, Any], properties: Mapping[str, Any]) -> str:
    node_type = str(node.get("type") or "entity")
    if node_type == "venue":
        document = properties.get("document") or {}
        return " ".join(
            _unique_text(
                (
                    properties.get("canonical_name", ""),
                    properties.get("abbreviation", ""),
                    document.get("area", ""),
                    document.get("taxonomy_scope", ""),
                    document.get("curated_scope", ""),
                    document.get("curated_topics", ""),
                )
            )
        )
    if node_type == "submission_scope":
        return " ".join(
            _unique_text(
                (
                    properties.get("summary", ""),
                    properties.get("topics_zh", ""),
                    properties.get("topics_en", ""),
                    properties.get("out_of_scope", ""),
                )
            )
        )
    return json.dumps(dict(properties), ensure_ascii=False, sort_keys=True)


def _edge_description(relation: str, properties: Mapping[str, Any]) -> str:
    if relation == "ACCEPTS_TOPIC":
        return "The venue accepts research on this reviewed topic."
    if relation == "HAS_SUBMISSION_SCOPE":
        return "The venue has this reviewed submission scope."
    if relation == "RELATED_TOPIC":
        return (
            "The topics co-occur in reviewed venue scopes; "
            f"normalized strength={properties.get('strength', 0)}."
        )
    return relation.replace("_", " ").lower()


def _normalize_vector(values: Sequence[float]) -> list[float]:
    if not values:
        raise GraphIndexError("query vector cannot be empty")
    vector: list[float] = []
    squared_norm = 0.0
    for value in values:
        number = float(value)
        if not math.isfinite(number):
            raise GraphIndexError("query vector contains a non-finite value")
        vector.append(number)
        squared_norm += number * number
    if squared_norm <= 0.0 or not math.isfinite(squared_norm):
        raise GraphIndexError("query vector has zero norm")
    inverse_norm = 1.0 / math.sqrt(squared_norm)
    return [number * inverse_norm for number in vector]


def _unpack_float32(blob: bytes, dimensions: int) -> list[float]:
    expected = dimensions * 4
    if len(blob) != expected:
        raise GraphIndexError(
            f"invalid graph vector size: expected {expected}, got {len(blob)}"
        )
    values = array.array("f")
    values.frombytes(blob)
    if sys.byteorder != "little":
        values.byteswap()
    result = list(values)
    if any(not math.isfinite(value) for value in result):
        raise GraphIndexError("graph vector contains a non-finite value")
    return result


def _sign_bits(values: Sequence[float]) -> bytes:
    result = 0
    for index, value in enumerate(values):
        if value >= 0:
            result |= 1 << index
    return result.to_bytes((len(values) + 7) // 8, byteorder="little")


def _hamming_distance(left: bytes, right: bytes) -> int:
    if len(left) != len(right):
        raise GraphIndexError("vector sign-bit dimensions differ")
    return sum((left_value ^ right_value).bit_count() for left_value, right_value in zip(left, right))
