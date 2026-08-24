"""Frozen data loading, temporal splitting, and reproducibility manifests."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from typing import Any, Iterable, Mapping, Sequence

from .types import Qrels, Query, Run, ScoredDocument, VenueDocument, sort_ranking


class ResearchDataError(ValueError):
    """Raised when an offline input is incomplete or ambiguous."""


@dataclass(frozen=True)
class DatasetBundle:
    queries: tuple[Query, ...]
    qrels: Qrels
    source_rows: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class TemporalSplit:
    train: tuple[str, ...]
    validation: tuple[str, ...]
    test: tuple[str, ...]
    excluded: tuple[str, ...]

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "train": list(self.train),
            "validation": list(self.validation),
            "test": list(self.test),
            "excluded": list(self.excluded),
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("&", " and ")
    # Keep every Unicode letter, number, and combining mark.  The previous
    # ASCII+CJK whitelist erased Korean, Cyrillic, Persian, and Japanese kana,
    # which made unrelated title-only papers collapse onto the same identity.
    normalized = "".join(
        character
        if unicodedata.category(character)[0] in {"L", "M", "N"}
        else " "
        for character in text
    )
    return " ".join(normalized.split())


def normalize_doi(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    text = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", text)
    return text.rstrip(".,;)")


def parse_iso_date(value: object, *, field_name: str) -> date:
    raw = str(value or "").strip()
    try:
        return date.fromisoformat(raw[:10])
    except (TypeError, ValueError) as exc:
        raise ResearchDataError(f"invalid or missing {field_name}: {raw!r}") from exc


def _read_jsonl(path: Path) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise ResearchDataError(f"cannot open JSONL file: {path}") from exc
    with handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ResearchDataError(
                    f"{path}:{line_number}: invalid JSON: {exc.msg}"
                ) from exc
            if not isinstance(payload, Mapping):
                raise ResearchDataError(f"{path}:{line_number}: expected an object")
            rows.append(payload)
    if not rows:
        raise ResearchDataError(f"JSONL file contains no records: {path}")
    return rows


def _first(row: Mapping[str, Any], fields: Sequence[str], default: object = "") -> object:
    for field in fields:
        value = row.get(field)
        if value not in (None, ""):
            return value
    return default


def load_recent_journal_dataset(
    path: Path,
    *,
    query_fields: Sequence[str] = ("title", "abstract"),
    id_fields: Sequence[str] = ("paper_id", "case_id", "id"),
    date_fields: Sequence[str] = ("publication_date", "published_date"),
    relevance_field: str = "gold_journal_id",
) -> DatasetBundle:
    """Load the existing recent-journals schema or an equivalent JSONL file.

    Every record remains in the denominator.  A relevance target is retained
    even when it is absent from the frozen candidate corpus, so catalog misses
    cannot silently disappear from reported metrics.
    """

    queries: list[Query] = []
    qrels: Qrels = {}
    source_rows: dict[str, Mapping[str, Any]] = {}
    for line_number, row in enumerate(_read_jsonl(path), 1):
        query_id = str(_first(row, id_fields)).strip()
        if not query_id:
            raise ResearchDataError(f"{path}:{line_number}: missing query ID")
        if query_id in source_rows:
            raise ResearchDataError(f"{path}:{line_number}: duplicate query ID {query_id!r}")
        publication_date = str(_first(row, date_fields)).strip()[:10]
        parse_iso_date(publication_date, field_name="publication date")
        title = str(row.get("title") or "").strip()
        abstract = str(row.get("abstract") or "").strip()
        parts = [str(row.get(field) or "").strip() for field in query_fields]
        text = "\n".join(part for part in parts if part)
        if not text:
            raise ResearchDataError(f"{path}:{line_number}: empty query text")
        relevance_id = str(row.get(relevance_field) or "").strip()
        if not relevance_id:
            # Entity ID is supported for compatible custom datasets, but the
            # stable journal ID is preferred for the bundled benchmark.
            entity_id = row.get("gold_entity_id")
            relevance_id = f"entity:{entity_id}" if entity_id not in (None, "") else ""
        if not relevance_id:
            raise ResearchDataError(f"{path}:{line_number}: missing relevance label")
        query = Query(
            query_id=query_id,
            text=text,
            publication_date=publication_date,
            title=title,
            abstract=abstract,
            doi=normalize_doi(row.get("doi")),
            gold_venue_name=str(row.get("gold_journal_name") or row.get("journal_name") or "").strip(),
            metadata={
                "field": row.get("broad_field") or row.get("primary_field") or "unknown",
                "quartile": row.get("gold_jcr_quartile") or "unknown",
                "language": row.get("language") or "unknown",
            },
        )
        queries.append(query)
        qrels[query_id] = {relevance_id: 1.0}
        source_rows[query_id] = row
    queries.sort(key=lambda item: (item.publication_date, item.query_id))
    return DatasetBundle(tuple(queries), qrels, source_rows)


def temporal_split(
    queries: Sequence[Query],
    *,
    train_end: str,
    validation_end: str,
    test_end: str,
    start: str | None = None,
) -> TemporalSplit:
    start_date = parse_iso_date(start, field_name="split start") if start else None
    train_date = parse_iso_date(train_end, field_name="train_end")
    validation_date = parse_iso_date(validation_end, field_name="validation_end")
    test_date = parse_iso_date(test_end, field_name="test_end")
    if not train_date < validation_date < test_date:
        raise ResearchDataError("split boundaries must satisfy train_end < validation_end < test_end")
    train: list[str] = []
    validation: list[str] = []
    test: list[str] = []
    excluded: list[str] = []
    for query in queries:
        query_date = parse_iso_date(query.publication_date, field_name="publication date")
        if start_date and query_date < start_date:
            excluded.append(query.query_id)
        elif query_date <= train_date:
            train.append(query.query_id)
        elif query_date <= validation_date:
            validation.append(query.query_id)
        elif query_date <= test_date:
            test.append(query.query_id)
        else:
            excluded.append(query.query_id)
    if not train or not test:
        raise ResearchDataError("temporal split must contain at least one train and one test query")
    return TemporalSplit(tuple(train), tuple(validation), tuple(test), tuple(excluded))


def _valid_issn_token(value: object) -> str:
    raw = str(value or "").strip().upper()
    if not re.fullmatch(r"\d{4}-?\d{3}[\dX]", raw):
        return ""
    token = raw.replace("-", "")
    total = sum(int(token[index]) * (8 - index) for index in range(7))
    check = (11 - total % 11) % 11
    expected = "X" if check == 10 else str(check)
    return token if token[-1] == expected else ""


def _jcr_document_id(row: Mapping[str, Any]) -> str:
    tokens = sorted(
        token
        for token in {
            _valid_issn_token(row.get("issn")),
            _valid_issn_token(row.get("eissn")),
        }
        if token
    )
    if not tokens:
        return ""
    digest = hashlib.sha256("\x1f".join(tokens).encode("utf-8")).hexdigest()
    return "jcr-" + digest[:16]


def load_jcr_corpus(
    path: Path,
    *,
    snapshot_date: str,
    text_fields: Sequence[str],
    allowed_levels: Sequence[str] = ("Q1", "Q2", "Q3", "Q4"),
) -> list[VenueDocument]:
    """Load a frozen JCR CSV without importing production code.

    The caller explicitly whitelists text fields.  This is deliberate: the
    paper configuration excludes fields enriched after the test papers were
    published.
    """

    parse_iso_date(snapshot_date, field_name="corpus snapshot_date")
    allowed = {value.upper() for value in allowed_levels}
    documents: dict[str, VenueDocument] = {}
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise ResearchDataError(f"cannot open JCR corpus: {path}") from exc
    with handle:
        reader = csv.DictReader(handle)
        missing_fields = [field for field in text_fields if field not in (reader.fieldnames or [])]
        if missing_fields:
            raise ResearchDataError(f"JCR corpus is missing text fields: {missing_fields}")
        for row in reader:
            level = str(row.get("level") or "").upper()
            if allowed and level not in allowed:
                continue
            doc_id = _jcr_document_id(row)
            if not doc_id:
                continue
            values: list[str] = []
            for field in text_fields:
                value = " ".join(str(row.get(field) or "").split())
                if value and value not in values:
                    values.append(value)
            if not values:
                continue
            document = VenueDocument(
                doc_id=doc_id,
                text="\n".join(values),
                name=str(row.get("name") or "").strip(),
                snapshot_date=snapshot_date,
                metadata={
                    "dataset": "jcr",
                    "version_year": row.get("version_year") or "",
                    "level": level,
                    "issn": row.get("issn") or "",
                    "eissn": row.get("eissn") or "",
                    "text_fields": list(text_fields),
                    "content_origin": "static_venue_metadata",
                },
            )
            # Identical ISSN identities should not diverge.  Merge their
            # whitelisted metadata deterministically if duplicate rows exist.
            previous = documents.get(doc_id)
            if previous is None:
                documents[doc_id] = document
            elif document.text not in previous.text:
                merged = "\n".join(sorted({previous.text, document.text}))
                documents[doc_id] = VenueDocument(
                    doc_id=doc_id,
                    text=merged,
                    name=previous.name or document.name,
                    snapshot_date=snapshot_date,
                    metadata=previous.metadata,
                )
    if not documents:
        raise ResearchDataError(f"JCR corpus contains no eligible documents: {path}")
    return [documents[key] for key in sorted(documents)]


def load_jsonl_corpus(
    path: Path,
    *,
    id_field: str = "venue_id",
    text_fields: Sequence[str] = ("name", "scope"),
    snapshot_field: str = "snapshot_date",
    default_snapshot_date: str = "",
) -> list[VenueDocument]:
    """Load a generic frozen venue corpus with provenance metadata."""

    documents: list[VenueDocument] = []
    seen: set[str] = set()
    for line_number, row in enumerate(_read_jsonl(path), 1):
        doc_id = str(row.get(id_field) or "").strip()
        if not doc_id or doc_id in seen:
            raise ResearchDataError(f"{path}:{line_number}: missing or duplicate {id_field}")
        snapshot_date = str(row.get(snapshot_field) or default_snapshot_date).strip()[:10]
        parse_iso_date(snapshot_date, field_name="corpus snapshot date")
        parts = [str(row.get(field) or "").strip() for field in text_fields]
        text = "\n".join(part for part in parts if part)
        if not text:
            raise ResearchDataError(f"{path}:{line_number}: empty venue text")
        metadata = dict(row.get("metadata") or {}) if isinstance(row.get("metadata"), Mapping) else {}
        if isinstance(row.get("prototypes"), list):
            # Preserve structured multi-prototype text for prototype-aware
            # retrievers while keeping ``document.text`` compatible with the
            # original one-document-per-venue baselines.
            metadata["prototypes"] = [
                dict(value) for value in row["prototypes"] if isinstance(value, Mapping)
            ]
        for field in ("source_doi", "source_title", "source_date", "source_query_id", "content_sha256"):
            if field in row:
                metadata[field] = row[field]
        metadata["text_fields"] = list(text_fields)
        documents.append(
            VenueDocument(
                doc_id=doc_id,
                text=text,
                name=str(row.get("name") or "").strip(),
                snapshot_date=snapshot_date,
                metadata=metadata,
            )
        )
        seen.add(doc_id)
    documents.sort(key=lambda item: item.doc_id)
    return documents


def load_score_run(
    path: Path,
    *,
    query_field: str = "query_id",
    document_field: str = "venue_id",
    score_field: str = "score",
    top_k: int | None = None,
) -> Run:
    """Import frozen vector/graph/model scores without loading the model.

    Supported JSONL rows are either ``{query_id, venue_id, score}`` or
    ``{query_id, scores: {venue_id: score}}``.
    """

    scores: dict[str, dict[str, float]] = {}
    for line_number, row in enumerate(_read_jsonl(path), 1):
        query_id = str(row.get(query_field) or "").strip()
        if not query_id:
            raise ResearchDataError(f"{path}:{line_number}: missing {query_field}")
        bucket = scores.setdefault(query_id, {})
        if isinstance(row.get("scores"), Mapping):
            values = row["scores"]
            for doc_id, score in values.items():
                bucket[str(doc_id)] = float(score)
            continue
        doc_id = str(row.get(document_field) or "").strip()
        if not doc_id:
            raise ResearchDataError(f"{path}:{line_number}: missing {document_field}")
        try:
            bucket[doc_id] = float(row[score_field])
        except (KeyError, TypeError, ValueError) as exc:
            raise ResearchDataError(f"{path}:{line_number}: invalid {score_field}") from exc
    return {query_id: sort_ranking(values, top_k) for query_id, values in scores.items()}


def write_run(path: Path, run: Run) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for query_id in sorted(run):
            for rank, item in enumerate(run[query_id], 1):
                handle.write(
                    json.dumps(
                        {
                            "query_id": query_id,
                            "venue_id": item.doc_id,
                            "rank": rank,
                            "score": item.score,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )


def build_data_manifest(
    *,
    dataset_path: Path,
    corpus_path: Path,
    bundle: DatasetBundle,
    corpus: Sequence[VenueDocument],
    split: TemporalSplit,
    config: Mapping[str, Any],
    additional_inputs: Iterable[Path] = (),
) -> dict[str, Any]:
    query_by_id = {query.query_id: query for query in bundle.queries}
    corpus_ids = {doc.doc_id for doc in corpus}
    relevant_ids = {
        doc_id
        for query_relevance in bundle.qrels.values()
        for doc_id, gain in query_relevance.items()
        if gain > 0
    }

    def date_range(query_ids: Sequence[str]) -> dict[str, str | None]:
        values = sorted(query_by_id[query_id].publication_date for query_id in query_ids)
        return {"from": values[0] if values else None, "until": values[-1] if values else None}

    paths = [dataset_path, corpus_path, *additional_inputs]
    input_files = {
        str(path): {"sha256": sha256_file(path), "bytes": path.stat().st_size}
        for path in paths
    }
    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "offline_only": True,
        "inputs": input_files,
        "dataset": {
            "query_count": len(bundle.queries),
            "relevance_target_count": len(relevant_ids),
            "candidate_covered_targets": len(relevant_ids & corpus_ids),
            "candidate_target_coverage": (
                len(relevant_ids & corpus_ids) / len(relevant_ids) if relevant_ids else 0.0
            ),
        },
        "corpus": {
            "document_count": len(corpus),
            "snapshot_dates": sorted({doc.snapshot_date for doc in corpus}),
        },
        "temporal_split": {
            "counts": {
                "train": len(split.train),
                "validation": len(split.validation),
                "test": len(split.test),
                "excluded": len(split.excluded),
            },
            "date_ranges": {
                "train": date_range(split.train),
                "validation": date_range(split.validation),
                "test": date_range(split.test),
                "excluded": date_range(split.excluded),
            },
            "query_ids_sha256": {
                name: hashlib.sha256("\n".join(values).encode("utf-8")).hexdigest()
                for name, values in split.as_dict().items()
            },
        },
        "configuration": config,
    }
