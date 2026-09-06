"""Frozen data loading, temporal splitting, and reproducibility manifests."""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import lru_cache
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import unicodedata
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .types import Qrels, Query, Run, ScoredDocument, VenueDocument, sort_ranking


class ResearchDataError(ValueError):
    """Raised when an offline input is incomplete or ambiguous."""


RUN_MANIFEST_SCHEMA_VERSION = 2


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


BLIND_QUERY_ALLOWED_FIELDS = frozenset(
    {
        "abstract",
        "article_type",
        "language",
        "paper_id",
        "publication_date",
        "publication_date_precision",
        "title",
        "user_constraints",
    }
)
BLIND_QUERY_LABEL_FIELDS = frozenset(
    {
        "broad_field",
        "gold_container_title",
        "gold_entity_id",
        "gold_issns",
        "gold_jcr_category",
        "gold_jcr_quartile",
        "gold_journal_id",
        "gold_journal_name",
        "journal_name",
        "label",
        "primary_field",
        "relevance",
        "split",
        "venue_id",
    }
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_sha256(value: Any) -> str:
    """Hash a JSON value without whitespace or key-order ambiguity."""

    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ResearchDataError("value is not canonical finite JSON") from exc
    return hashlib.sha256(payload).hexdigest()


def ordered_ids_sha256(values: Sequence[str]) -> str:
    """Fingerprint an ordered ID sequence with unambiguous JSON framing."""

    return canonical_json_sha256([str(value) for value in values])


def _file_record(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    return {
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "bytes": resolved.stat().st_size,
    }


def _checked_ids(
    values: Sequence[str], *, label: str, sort_values: bool = False
) -> tuple[str, ...]:
    identifiers = tuple(str(value).strip() for value in values)
    if not identifiers or any(not value for value in identifiers):
        raise ResearchDataError(f"{label} must contain non-empty IDs")
    if len(set(identifiers)) != len(identifiers):
        raise ResearchDataError(f"{label} contains duplicate IDs")
    return tuple(sorted(identifiers)) if sort_values else identifiers


@lru_cache(maxsize=1)
def _environment_snapshot() -> dict[str, Any]:
    dependencies: list[dict[str, str]] = []
    try:
        for distribution in importlib.metadata.distributions():
            name = str(distribution.metadata.get("Name") or "").strip()
            if name:
                dependencies.append(
                    {"name": name, "version": str(distribution.version)}
                )
    except Exception:  # pragma: no cover - damaged package metadata is unusual
        dependencies = []
    dependencies.sort(key=lambda item: (item["name"].casefold(), item["version"]))

    cpu_model = platform.processor().strip()
    cpuinfo = Path("/proc/cpuinfo")
    if not cpu_model and cpuinfo.is_file():
        try:
            for line in cpuinfo.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.casefold().startswith("model name") and ":" in line:
                    cpu_model = line.split(":", 1)[1].strip()
                    break
        except OSError:
            pass
    memory_bytes: int | None = None
    try:
        memory_bytes = int(os.sysconf("SC_PAGE_SIZE")) * int(
            os.sysconf("SC_PHYS_PAGES")
        )
    except (OSError, ValueError):
        pass

    gpus: list[dict[str, str]] = []
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            completed = subprocess.run(
                (
                    nvidia_smi,
                    "--query-gpu=name,uuid,driver_version",
                    "--format=csv,noheader,nounits",
                ),
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if completed.returncode == 0:
                for line in completed.stdout.splitlines():
                    fields = [field.strip() for field in line.split(",", 2)]
                    if len(fields) == 3:
                        gpus.append(
                            {
                                "name": fields[0],
                                "uuid": fields[1],
                                "driver_version": fields[2],
                            }
                        )
        except (OSError, subprocess.SubprocessError):
            pass

    return {
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "executable": str(Path(sys.executable).resolve()),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
        },
        "dependencies": dependencies,
        "hardware": {
            "cpu_model": cpu_model or "unknown",
            "logical_cpu_count": os.cpu_count(),
            "physical_memory_bytes": memory_bytes,
            "gpus": gpus,
        },
    }


def _git_code_state() -> dict[str, Any]:
    repository = Path(__file__).resolve().parents[1]

    def run(*arguments: str) -> bytes:
        try:
            return subprocess.check_output(
                ("git", *arguments),
                cwd=repository,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return b""

    commit = run("rev-parse", "HEAD").decode("utf-8", "replace").strip()
    status = run("status", "--porcelain=v1", "--untracked-files=all")
    diff = run("diff", "--binary", "HEAD")
    return {
        "commit": commit,
        "dirty": bool(status.strip()),
        "status_sha256": hashlib.sha256(status).hexdigest(),
        "tracked_diff_sha256": hashlib.sha256(diff).hexdigest(),
    }


def runtime_provenance() -> dict[str, Any]:
    """Capture code, Python/dependency, and hardware provenance."""

    return {"code": _git_code_state(), **_environment_snapshot()}


def build_run_binding(
    *,
    dataset_path: Path,
    profiles_path: Path,
    query_ids: Sequence[str],
    candidate_ids: Sequence[str],
    configuration: Mapping[str, Any],
    configuration_path: Path | None = None,
    additional_input_paths: Sequence[Path] = (),
) -> dict[str, Any]:
    """Build the immutable input/configuration binding for a frozen run."""

    ordered_queries = _checked_ids(query_ids, label="query_ids")
    ordered_candidates = _checked_ids(
        candidate_ids, label="candidate_ids", sort_values=True
    )
    config_record: dict[str, Any] = {
        "canonical_sha256": canonical_json_sha256(configuration)
    }
    if configuration_path is not None:
        config_record["source"] = _file_record(configuration_path)
    binding = {
        "dataset": _file_record(dataset_path),
        "queries": {
            "count": len(ordered_queries),
            "ordered_ids_sha256": ordered_ids_sha256(ordered_queries),
        },
        "profiles": _file_record(profiles_path),
        "candidates": {
            "count": len(ordered_candidates),
            "ordering": "lexicographic",
            "ordered_ids_sha256": ordered_ids_sha256(ordered_candidates),
        },
        "configuration": config_record,
    }
    if additional_input_paths:
        resolved: list[Path] = []
        seen_paths: set[Path] = set()
        for path in additional_input_paths:
            item = path.resolve()
            if item in seen_paths:
                continue
            seen_paths.add(item)
            resolved.append(item)
        binding["additional_inputs"] = [_file_record(path) for path in resolved]
    return binding


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


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def _iter_jsonl(path: Path) -> Iterator[tuple[int, Mapping[str, Any]]]:
    """Stream strict JSONL records and retain their physical line numbers."""

    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise ResearchDataError(f"cannot open JSONL file: {path}") from exc
    seen = False
    with handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line, parse_constant=_reject_json_constant)
            except (json.JSONDecodeError, ValueError) as exc:
                message = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
                raise ResearchDataError(
                    f"{path}:{line_number}: invalid JSON: {message}"
                ) from exc
            if not isinstance(payload, Mapping):
                raise ResearchDataError(f"{path}:{line_number}: expected an object")
            seen = True
            yield line_number, payload
    if not seen:
        raise ResearchDataError(f"JSONL file contains no records: {path}")


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
    for line_number, row in _iter_jsonl(path):
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


def load_blind_query_dataset(
    path: Path,
    *,
    query_fields: Sequence[str] = ("title", "abstract"),
) -> DatasetBundle:
    """Load a physically label-free query file for pre-commit inference.

    The accepted schema is intentionally closed.  This prevents a newly added
    metadata column from silently carrying a gold venue, field, quartile, or
    split cue into a sealed-test prediction process.
    """

    requested_fields = tuple(str(value).strip() for value in query_fields)
    if (
        not requested_fields
        or any(not value for value in requested_fields)
        or not set(requested_fields) <= BLIND_QUERY_ALLOWED_FIELDS
    ):
        raise ResearchDataError("blind query_fields are empty or outside the safe schema")
    queries: list[Query] = []
    source_rows: dict[str, Mapping[str, Any]] = {}
    for line_number, row in _iter_jsonl(path):
        forbidden = sorted(set(row) & BLIND_QUERY_LABEL_FIELDS)
        unknown = sorted(set(row) - BLIND_QUERY_ALLOWED_FIELDS)
        if forbidden:
            raise ResearchDataError(
                f"{path}:{line_number}: blind query contains label fields: {forbidden}"
            )
        if unknown:
            raise ResearchDataError(
                f"{path}:{line_number}: blind query contains unapproved fields: {unknown}"
            )
        query_id = str(row.get("paper_id") or "").strip()
        if not query_id:
            raise ResearchDataError(f"{path}:{line_number}: missing paper_id")
        if query_id in source_rows:
            raise ResearchDataError(
                f"{path}:{line_number}: duplicate query ID {query_id!r}"
            )
        publication_date = str(row.get("publication_date") or "").strip()[:10]
        parse_iso_date(publication_date, field_name="publication date")
        title = str(row.get("title") or "").strip()
        abstract = str(row.get("abstract") or "").strip()
        parts = [str(row.get(field) or "").strip() for field in requested_fields]
        text = "\n".join(part for part in parts if part)
        if not text:
            raise ResearchDataError(f"{path}:{line_number}: empty query text")
        constraints = row.get("user_constraints")
        if constraints is not None and not isinstance(constraints, Mapping):
            raise ResearchDataError(
                f"{path}:{line_number}: user_constraints must be an object"
            )
        queries.append(
            Query(
                query_id=query_id,
                text=text,
                publication_date=publication_date,
                title=title,
                abstract=abstract,
                doi="",
                gold_venue_name="",
                metadata={"language": row.get("language") or "unknown"},
            )
        )
        source_rows[query_id] = row
    queries.sort(key=lambda item: (item.publication_date, item.query_id))
    return DatasetBundle(tuple(queries), {}, source_rows)


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
    for line_number, row in _iter_jsonl(path):
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


_EMBEDDED_DOI_RE = re.compile(r"10\.\d{1,9}/[-._;()/:a-z0-9]+", re.I)


def _identity_maps(
    queries: Sequence[Query],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    doi_queries: dict[str, set[str]] = defaultdict(set)
    title_queries: dict[str, set[str]] = defaultdict(set)
    for query in queries:
        if query.doi:
            doi_queries[normalize_doi(query.doi)].add(query.query_id)
        normalized_title = normalize_text(query.title)
        cjk_count = len(re.findall(r"[\u3400-\u9fff]", normalized_title))
        if len(normalized_title) >= 24 and (
            len(normalized_title.split()) >= 4 or cjk_count >= 8
        ):
            title_queries[normalized_title].add(query.query_id)
    return dict(doi_queries), dict(title_queries)


def _title_anchor_index(
    title_queries: Mapping[str, set[str]],
) -> tuple[
    dict[str, list[tuple[str, set[str]]]],
    dict[str, list[tuple[str, set[str]]]],
]:
    token_index: dict[str, list[tuple[str, set[str]]]] = defaultdict(list)
    substring_index: dict[str, list[tuple[str, set[str]]]] = defaultdict(list)
    for title, query_ids in title_queries.items():
        tokens = title.split()
        if len(tokens) == 1 and re.search(r"[\u3400-\u9fff]", title):
            substring_index[title[:8]].append((title, query_ids))
            continue
        anchor = max(tokens, key=lambda value: (len(value), value)) if tokens else title
        token_index[anchor].append((title, query_ids))
    return dict(token_index), dict(substring_index)


def _contained_title_query_ids(
    normalized_text: str,
    anchor_indexes: tuple[
        Mapping[str, Sequence[tuple[str, set[str]]]],
        Mapping[str, Sequence[tuple[str, set[str]]]],
    ],
) -> set[str]:
    token_index, substring_index = anchor_indexes
    matches: set[str] = set()
    for token in set(normalized_text.split()):
        for title, query_ids in token_index.get(token, ()):
            if title in normalized_text:
                matches.update(query_ids)
    for anchor, candidates in substring_index.items():
        if anchor not in normalized_text:
            continue
        for title, query_ids in candidates:
            if title in normalized_text:
                matches.update(query_ids)
    return matches


def _identity_exclusion_report(
    *,
    queries: Sequence[Query],
    excluded_kind: str,
    excluded_count: int,
    affected_venues: set[str],
    matched_query_ids: set[str],
    active_count: int,
) -> dict[str, Any]:
    ordered_query_ids = tuple(query.query_id for query in queries)
    return {
        "schema_version": 1,
        "policy": (
            "remove validation/test paper identities from the active corpus "
            "view before retrieval; the source corpus remains immutable"
        ),
        "target_query_count": len(ordered_query_ids),
        "target_query_ids_sha256": ordered_ids_sha256(ordered_query_ids),
        "excluded_kind": excluded_kind,
        "excluded_count": excluded_count,
        "active_count": active_count,
        "affected_venue_count": len(affected_venues),
        "matched_query_count": len(matched_query_ids),
        "matched_query_ids": sorted(matched_query_ids),
    }


def load_evidence_concat_corpus(
    profiles_path: Path,
    evidence_path: Path,
    *,
    excluded_queries: Sequence[Query],
    id_field: str = "venue_id",
    snapshot_field: str = "snapshot_date",
) -> tuple[list[VenueDocument], dict[str, Any]]:
    """Build a paper-concat view without rewriting the frozen source corpus.

    Validation/test identities are removed at evidence-row granularity before
    concatenation.  This is the temporal protocol's explicit test-paper
    exclusion, not a score- or label-dependent filter.
    """

    profile_rows: dict[str, tuple[str, str, dict[str, Any]]] = {}
    for line_number, row in _iter_jsonl(profiles_path):
        venue_id = str(row.get(id_field) or "").strip()
        if not venue_id or venue_id in profile_rows:
            raise ResearchDataError(
                f"{profiles_path}:{line_number}: missing or duplicate {id_field}"
            )
        snapshot_date = str(row.get(snapshot_field) or "").strip()[:10]
        parse_iso_date(snapshot_date, field_name="corpus snapshot date")
        name = " ".join(str(row.get("name") or "").split())
        if not name:
            raise ResearchDataError(f"{profiles_path}:{line_number}: empty venue name")
        metadata = (
            dict(row.get("metadata") or {})
            if isinstance(row.get("metadata"), Mapping)
            else {}
        )
        metadata.pop("prototypes", None)
        metadata["text_fields"] = [
            "name",
            "metadata.subject",
            "paper.title",
            "paper.abstract",
        ]
        profile_rows[venue_id] = (name, snapshot_date, metadata)
    if not profile_rows:
        raise ResearchDataError(f"profile corpus is empty: {profiles_path}")

    doi_queries, title_queries = _identity_maps(excluded_queries)
    title_anchor_index = _title_anchor_index(title_queries)
    evidence_parts: dict[str, list[str]] = defaultdict(list)
    evidence_counts: Counter[str] = Counter()
    excluded_counts: Counter[str] = Counter()
    excluded_count = 0
    affected_venues: set[str] = set()
    matched_query_ids: set[str] = set()
    for line_number, row in _iter_jsonl(evidence_path):
        if str(row.get("kind") or "") != "paper":
            continue
        venue_id = str(row.get("venue_id") or "").strip()
        if venue_id not in profile_rows:
            raise ResearchDataError(
                f"{evidence_path}:{line_number}: unknown venue_id {venue_id!r}"
            )
        if row.get("temporal_eligible", True) is False:
            continue
        publication_date = str(row.get("publication_date") or "").strip()[:10]
        if publication_date:
            source_date = parse_iso_date(
                publication_date, field_name="evidence publication date"
            )
            snapshot_date = parse_iso_date(
                profile_rows[venue_id][1], field_name="corpus snapshot date"
            )
            if source_date > snapshot_date:
                raise ResearchDataError(
                    f"{evidence_path}:{line_number}: evidence postdates snapshot"
                )
        doi = normalize_doi(row.get("doi"))
        title = " ".join(str(row.get("title") or "").split())
        abstract = " ".join(str(row.get("abstract") or "").split())
        normalized_text = normalize_text("\n".join((title, abstract)))
        matched = set(doi_queries.get(doi, ()))
        matched.update(_contained_title_query_ids(normalized_text, title_anchor_index))
        if matched:
            excluded_count += 1
            excluded_counts[venue_id] += 1
            affected_venues.add(venue_id)
            matched_query_ids.update(matched)
            continue
        text = "\n".join(value for value in (title, abstract) if value)
        if not text:
            continue
        evidence_parts[venue_id].append(text)
        evidence_counts[venue_id] += 1

    documents: list[VenueDocument] = []
    for venue_id in sorted(profile_rows):
        name, snapshot_date, metadata = profile_rows[venue_id]
        subject = " ".join(str(metadata.get("subject") or "").split())
        parts = [name]
        if subject and subject.casefold() != name.casefold():
            parts.append(subject)
        parts.extend(evidence_parts.get(venue_id, ()))
        metadata["active_paper_count"] = evidence_counts[venue_id]
        metadata["identity_excluded_paper_count"] = excluded_counts[venue_id]
        documents.append(
            VenueDocument(
                doc_id=venue_id,
                text="\n\n".join(parts),
                name=name,
                snapshot_date=snapshot_date,
                metadata=metadata,
            )
        )
    active_count = sum(evidence_counts.values())
    report = _identity_exclusion_report(
        queries=excluded_queries,
        excluded_kind="paper_evidence_row",
        excluded_count=excluded_count,
        affected_venues=affected_venues,
        matched_query_ids=matched_query_ids,
        active_count=active_count,
    )
    report["candidate_count"] = len(documents)
    return documents, report


def exclude_query_identities_from_prototypes(
    corpus: Sequence[VenueDocument],
    *,
    excluded_queries: Sequence[Query],
) -> tuple[list[VenueDocument], dict[str, Any]]:
    """Drop an entire prototype when it cites a validation/test identity."""

    doi_queries, title_queries = _identity_maps(excluded_queries)
    title_anchor_index = _title_anchor_index(title_queries)
    output: list[VenueDocument] = []
    excluded_count = 0
    active_count = 0
    affected_venues: set[str] = set()
    matched_query_ids: set[str] = set()
    for document in corpus:
        metadata = dict(document.metadata)
        raw = metadata.get("prototypes")
        prototypes = raw if isinstance(raw, list) else []
        kept: list[dict[str, Any]] = []
        removed_here = 0
        for prototype in prototypes:
            if not isinstance(prototype, Mapping):
                continue
            if prototype.get("temporal_eligible", True) is False:
                kept.append(dict(prototype))
                continue
            matched: set[str] = set()
            raw_source_ids = prototype.get("source_ids") or ()
            source_ids = (
                (raw_source_ids,)
                if isinstance(raw_source_ids, str)
                else raw_source_ids
            )
            for source_id in source_ids:
                for value in _EMBEDDED_DOI_RE.findall(str(source_id or "")):
                    matched.update(doi_queries.get(normalize_doi(value), ()))
            normalized_text = normalize_text(
                "\n".join(
                    (
                        str(prototype.get("label") or ""),
                        str(prototype.get("text") or ""),
                    )
                )
            )
            matched.update(
                _contained_title_query_ids(normalized_text, title_anchor_index)
            )
            if matched:
                removed_here += 1
                excluded_count += 1
                matched_query_ids.update(matched)
                continue
            kept.append(dict(prototype))
        if removed_here:
            affected_venues.add(document.doc_id)
        active_count += sum(
            prototype.get("temporal_eligible", True) is not False
            for prototype in kept
        )
        metadata["prototypes"] = kept
        metadata["identity_excluded_prototype_count"] = removed_here
        output.append(
            VenueDocument(
                doc_id=document.doc_id,
                text=document.text,
                name=document.name,
                snapshot_date=document.snapshot_date,
                metadata=metadata,
            )
        )
    report = _identity_exclusion_report(
        queries=excluded_queries,
        excluded_kind="prototype_unit",
        excluded_count=excluded_count,
        affected_venues=affected_venues,
        matched_query_ids=matched_query_ids,
        active_count=active_count,
    )
    report["candidate_count"] = len(output)
    return output, report


def load_score_run(
    path: Path,
    *,
    expected_query_ids: Sequence[str],
    candidate_ids: Sequence[str],
    expected_binding: Mapping[str, Any],
    expected_manifest_sha256: str,
    expected_configuration_sha256: str,
    expected_method_identity: Mapping[str, str],
    manifest_path: Path | None = None,
    query_field: str = "query_id",
    document_field: str = "venue_id",
    score_field: str = "score",
    top_k: int | None = None,
) -> Run:
    """Import a fully bound frozen vector/graph/model score run.

    Supported JSONL rows are either ``{query_id, venue_id, score}`` or
    ``{query_id, scores: {venue_id: score}}``.  The sidecar manifest, full
    query coverage, candidate universe, finite scores, input fingerprints, and
    exact method identity are mandatory and are checked before scores are used.
    """

    queries = _checked_ids(expected_query_ids, label="expected_query_ids")
    candidates = _checked_ids(
        candidate_ids, label="candidate_ids", sort_values=True
    )
    manifest_path = manifest_path or path.with_suffix(path.suffix + ".manifest.json")
    if not expected_manifest_sha256:
        raise ResearchDataError("frozen run import requires manifest_sha256")
    if sha256_file(manifest_path) != expected_manifest_sha256:
        raise ResearchDataError("frozen run manifest SHA-256 mismatch")
    manifest = _read_json_object(manifest_path)
    _validate_run_manifest(
        manifest,
        run_path=path,
        expected_binding=expected_binding,
        expected_query_ids=queries,
        candidate_ids=candidates,
        expected_configuration_sha256=expected_configuration_sha256,
        expected_method_identity=expected_method_identity,
    )

    scores: dict[str, dict[str, float]] = {}
    formats: dict[str, str] = {}
    for line_number, row in _iter_jsonl(path):
        query_id = str(row.get(query_field) or "").strip()
        if not query_id:
            raise ResearchDataError(f"{path}:{line_number}: missing {query_field}")
        if isinstance(row.get("scores"), Mapping):
            if query_id in scores:
                raise ResearchDataError(
                    f"{path}:{line_number}: duplicate score block for {query_id!r}"
                )
            bucket: dict[str, float] = {}
            scores[query_id] = bucket
            formats[query_id] = "mapping"
            values = row["scores"]
            for raw_doc_id, raw_score in values.items():
                doc_id = str(raw_doc_id).strip()
                if not doc_id:
                    raise ResearchDataError(
                        f"{path}:{line_number}: empty {document_field}"
                    )
                if doc_id in bucket:
                    raise ResearchDataError(
                        f"{path}:{line_number}: duplicate candidate {doc_id!r}"
                    )
                bucket[doc_id] = _finite_score(
                    raw_score, path=path, line_number=line_number, field=score_field
                )
            continue
        if query_id in formats and formats[query_id] != "rows":
            raise ResearchDataError(
                f"{path}:{line_number}: mixed score formats for {query_id!r}"
            )
        formats[query_id] = "rows"
        bucket = scores.setdefault(query_id, {})
        doc_id = str(row.get(document_field) or "").strip()
        if not doc_id:
            raise ResearchDataError(f"{path}:{line_number}: missing {document_field}")
        if doc_id in bucket:
            raise ResearchDataError(
                f"{path}:{line_number}: duplicate candidate {doc_id!r} for {query_id!r}"
            )
        if score_field not in row:
            raise ResearchDataError(f"{path}:{line_number}: missing {score_field}")
        bucket[doc_id] = _finite_score(
            row[score_field], path=path, line_number=line_number, field=score_field
        )
        if "rank" in row:
            raw_rank = row["rank"]
            try:
                rank = int(raw_rank)
            except (TypeError, ValueError) as exc:
                raise ResearchDataError(f"{path}:{line_number}: invalid rank") from exc
            if isinstance(raw_rank, bool) or rank != len(bucket):
                raise ResearchDataError(
                    f"{path}:{line_number}: rank is not contiguous for {query_id!r}"
                )

    if tuple(scores) != queries:
        missing = sorted(set(queries) - set(scores))
        extra = sorted(set(scores) - set(queries))
        raise ResearchDataError(
            "frozen run query coverage/order mismatch: "
            f"missing={missing[:5]}, extra={extra[:5]}"
        )
    run = {query_id: sort_ranking(scores[query_id]) for query_id in queries}
    declared_top_k = int(manifest["coverage"]["top_k"])
    stats = _validate_run_contents(
        run,
        query_ids=queries,
        candidate_ids=candidates,
        top_k=declared_top_k,
        require_order=True,
    )
    for key, value in stats.items():
        if manifest["coverage"].get(key) != value:
            raise ResearchDataError(
                f"frozen run manifest coverage mismatch for {key!r}"
            )
    if top_k is not None:
        if top_k < 0 or top_k > declared_top_k:
            raise ResearchDataError(
                f"requested top_k={top_k} exceeds frozen depth {declared_top_k}"
            )
        return {
            query_id: list(ranking[:top_k]) for query_id, ranking in run.items()
        }
    return run


def _finite_score(
    value: Any, *, path: Path, line_number: int, field: str
) -> float:
    if isinstance(value, bool):
        raise ResearchDataError(f"{path}:{line_number}: invalid {field}")
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise ResearchDataError(f"{path}:{line_number}: invalid {field}") from exc
    if not math.isfinite(score):
        raise ResearchDataError(f"{path}:{line_number}: non-finite {field}")
    return score


def _read_json_object(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ResearchDataError(f"cannot read strict JSON object: {path}") from exc
    if not isinstance(payload, Mapping):
        raise ResearchDataError(f"expected a JSON object: {path}")
    return payload


def _binding_value(binding: Mapping[str, Any], *keys: str) -> Any:
    value: Any = binding
    for key in keys:
        if not isinstance(value, Mapping) or key not in value:
            raise ResearchDataError(
                "frozen run binding is missing " + ".".join(keys)
            )
        value = value[key]
    return value


def _validate_binding_matches(
    actual: Mapping[str, Any], expected: Mapping[str, Any]
) -> None:
    fields = (
        ("dataset", "sha256"),
        ("dataset", "bytes"),
        ("queries", "count"),
        ("queries", "ordered_ids_sha256"),
        ("profiles", "sha256"),
        ("profiles", "bytes"),
        ("candidates", "count"),
        ("candidates", "ordering"),
        ("candidates", "ordered_ids_sha256"),
    )
    for keys in fields:
        if _binding_value(actual, *keys) != _binding_value(expected, *keys):
            raise ResearchDataError(
                "frozen run binding mismatch for " + ".".join(keys)
            )
    def additional_records(binding: Mapping[str, Any]) -> list[tuple[Any, Any]]:
        value = binding.get("additional_inputs", [])
        if not isinstance(value, list) or not all(
            isinstance(item, Mapping) for item in value
        ):
            raise ResearchDataError(
                "frozen run binding has invalid additional_inputs"
            )
        return [(item.get("sha256"), item.get("bytes")) for item in value]

    if additional_records(actual) != additional_records(expected):
        raise ResearchDataError("frozen run binding mismatch for additional_inputs")


def _validate_method_identity(method: Mapping[str, Any]) -> None:
    identity_fields = (
        "model_revision",
        "provider_fingerprint",
        "implementation_revision",
    )
    if not any(str(method.get(field) or "").strip() for field in identity_fields):
        raise ResearchDataError(
            "frozen run method requires an exact model revision, provider "
            "fingerprint, or implementation revision"
        )


def _validate_run_manifest(
    manifest: Mapping[str, Any],
    *,
    run_path: Path,
    expected_binding: Mapping[str, Any],
    expected_query_ids: Sequence[str],
    candidate_ids: Sequence[str],
    expected_configuration_sha256: str,
    expected_method_identity: Mapping[str, str],
) -> None:
    if manifest.get("schema_version") != RUN_MANIFEST_SCHEMA_VERSION:
        raise ResearchDataError("unsupported frozen run manifest schema")
    if manifest.get("artifact_type") != "frozen_score_run":
        raise ResearchDataError("manifest is not a frozen score run")
    binding = manifest.get("binding")
    method = manifest.get("method")
    runtime = manifest.get("runtime")
    generation = manifest.get("generation")
    output = manifest.get("output")
    coverage = manifest.get("coverage")
    if not all(
        isinstance(value, Mapping)
        for value in (binding, method, runtime, generation, output, coverage)
    ):
        raise ResearchDataError("frozen run manifest is structurally incomplete")
    assert isinstance(binding, Mapping)
    assert isinstance(method, Mapping)
    assert isinstance(runtime, Mapping)
    assert isinstance(generation, Mapping)
    assert isinstance(output, Mapping)
    assert isinstance(coverage, Mapping)
    _validate_binding_matches(binding, expected_binding)
    if (
        _binding_value(binding, "configuration", "canonical_sha256")
        != expected_configuration_sha256
    ):
        raise ResearchDataError("frozen run generation configuration mismatch")
    _validate_method_identity(method)
    if not expected_method_identity:
        raise ResearchDataError("expected method identity is required")
    for key, value in expected_method_identity.items():
        if key not in {
            "model_revision",
            "provider_fingerprint",
            "implementation_revision",
        }:
            raise ResearchDataError(f"unsupported method identity field {key!r}")
        if method.get(key) != value:
            raise ResearchDataError(f"frozen run method identity mismatch for {key!r}")

    code = runtime.get("code")
    python = runtime.get("python")
    if (
        not isinstance(code, Mapping)
        or not str(code.get("commit") or "")
        or not isinstance(code.get("dirty"), bool)
        or not isinstance(python, Mapping)
        or not str(python.get("version") or "")
        or not isinstance(runtime.get("dependencies"), list)
        or not isinstance(runtime.get("hardware"), Mapping)
    ):
        raise ResearchDataError("frozen run runtime provenance is incomplete")
    command = generation.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(value, str) and value for value in command)
        or not str(generation.get("working_directory") or "")
    ):
        raise ResearchDataError("frozen run reproduction command is incomplete")
    if output.get("sha256") != sha256_file(run_path):
        raise ResearchDataError("frozen score file SHA-256 mismatch")
    if output.get("bytes") != run_path.stat().st_size:
        raise ResearchDataError("frozen score file size mismatch")
    if coverage.get("complete_query_coverage") is not True:
        raise ResearchDataError("frozen run declares incomplete query coverage")
    if coverage.get("query_count") != len(expected_query_ids):
        raise ResearchDataError("frozen run query count mismatch")
    if coverage.get("candidate_universe_count") != len(candidate_ids):
        raise ResearchDataError("frozen run candidate universe count mismatch")
    try:
        depth = int(coverage.get("top_k"))
    except (TypeError, ValueError) as exc:
        raise ResearchDataError("frozen run has invalid top_k coverage") from exc
    if depth < 1:
        raise ResearchDataError("frozen run has invalid top_k coverage")


def _validate_run_contents(
    run: Run,
    *,
    query_ids: Sequence[str],
    candidate_ids: Sequence[str],
    top_k: int,
    require_order: bool,
) -> dict[str, Any]:
    queries = tuple(query_ids)
    query_set = set(queries)
    candidates = set(candidate_ids)
    if set(run) != query_set or (require_order and tuple(run) != queries):
        missing = sorted(query_set - set(run))
        extra = sorted(set(run) - query_set)
        raise ResearchDataError(
            "run query coverage/order mismatch: "
            f"missing={missing[:5]}, extra={extra[:5]}"
        )
    entry_count = 0
    empty_count = 0
    scored_candidates: set[str] = set()
    for query_id in queries:
        ranking = run[query_id]
        if len(ranking) > top_k:
            raise ResearchDataError(
                f"run ranking for {query_id!r} exceeds frozen top_k={top_k}"
            )
        seen: set[str] = set()
        canonical: list[ScoredDocument] = []
        for item in ranking:
            doc_id = str(item.doc_id).strip()
            try:
                score = float(item.score)
            except (TypeError, ValueError) as exc:
                raise ResearchDataError(
                    f"run contains an invalid score for {query_id!r}/{doc_id!r}"
                ) from exc
            if not doc_id or not math.isfinite(score):
                raise ResearchDataError(
                    f"run contains an empty ID or non-finite score for {query_id!r}"
                )
            if doc_id in seen:
                raise ResearchDataError(
                    f"run contains duplicate candidate {doc_id!r} for {query_id!r}"
                )
            if doc_id not in candidates:
                raise ResearchDataError(
                    f"run contains candidate {doc_id!r} outside the frozen universe"
                )
            seen.add(doc_id)
            scored_candidates.add(doc_id)
            canonical.append(ScoredDocument(doc_id, score))
        expected = sorted(canonical, key=lambda item: (-item.score, item.doc_id))
        if canonical != expected:
            raise ResearchDataError(
                f"run ranking for {query_id!r} is not deterministically sorted"
            )
        entry_count += len(ranking)
        empty_count += not ranking
    return {
        "query_count": len(queries),
        "candidate_universe_count": len(candidates),
        "complete_query_coverage": True,
        "empty_ranking_count": empty_count,
        "ranking_entry_count": entry_count,
        "scored_candidate_id_count": len(scored_candidates),
        "top_k": top_k,
    }


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    try:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ) + "\n"
    except (TypeError, ValueError) as exc:
        raise ResearchDataError("manifest contains non-JSON or non-finite data") from exc
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)


def write_run(
    path: Path,
    run: Run,
    *,
    binding: Mapping[str, Any],
    query_ids: Sequence[str],
    candidate_ids: Sequence[str],
    top_k: int,
    method: Mapping[str, Any],
    command: Sequence[str],
    working_directory: Path,
    runtime: Mapping[str, Any] | None = None,
    additional_manifest_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically freeze a run and its mandatory reproducibility sidecar."""

    queries = _checked_ids(query_ids, label="query_ids")
    candidates = _checked_ids(
        candidate_ids, label="candidate_ids", sort_values=True
    )
    if top_k < 1:
        raise ResearchDataError("frozen run top_k must be positive")
    _validate_binding_matches(binding, binding)
    if _binding_value(binding, "queries", "ordered_ids_sha256") != ordered_ids_sha256(
        queries
    ):
        raise ResearchDataError("run query IDs do not match the supplied binding")
    if _binding_value(
        binding, "candidates", "ordered_ids_sha256"
    ) != ordered_ids_sha256(candidates):
        raise ResearchDataError("run candidate IDs do not match the supplied binding")
    _validate_method_identity(method)
    if not command or not all(str(value) for value in command):
        raise ResearchDataError("frozen run requires a reproduction command")
    stats = _validate_run_contents(
        run,
        query_ids=queries,
        candidate_ids=candidates,
        top_k=top_k,
        require_order=False,
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for query_id in queries:
            if not run[query_id]:
                handle.write(
                    json.dumps(
                        {"query_id": query_id, "scores": {}},
                        ensure_ascii=False,
                        sort_keys=True,
                        allow_nan=False,
                    )
                    + "\n"
                )
                continue
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
                        allow_nan=False,
                    )
                    + "\n"
                )
    temporary.replace(path)

    manifest: dict[str, Any] = {
        "schema_version": RUN_MANIFEST_SCHEMA_VERSION,
        "artifact_type": "frozen_score_run",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "binding": dict(binding),
        "method": dict(method),
        "runtime": dict(runtime or runtime_provenance()),
        "generation": {
            "command": [str(value) for value in command],
            "working_directory": str(working_directory.resolve()),
        },
        "coverage": stats,
        "output": {
            "path": str(path.resolve()),
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        },
    }
    reserved = set(manifest)
    for key, value in (additional_manifest_fields or {}).items():
        if key in reserved:
            raise ResearchDataError(f"additional manifest field {key!r} is reserved")
        manifest[key] = value
    _validate_run_manifest(
        manifest,
        run_path=path,
        expected_binding=binding,
        expected_query_ids=queries,
        candidate_ids=candidates,
        expected_configuration_sha256=str(
            _binding_value(binding, "configuration", "canonical_sha256")
        ),
        expected_method_identity={
            key: str(method[key])
            for key in (
                "model_revision",
                "provider_fingerprint",
                "implementation_revision",
            )
            if method.get(key)
        },
    )
    manifest_path = path.with_suffix(path.suffix + ".manifest.json")
    _atomic_json(manifest_path, manifest)
    return manifest


def build_data_manifest(
    *,
    config_path: Path,
    dataset_path: Path,
    corpus_path: Path,
    bundle: DatasetBundle,
    corpus: Sequence[VenueDocument],
    split: TemporalSplit,
    config: Mapping[str, Any],
    binding: Mapping[str, Any],
    runtime: Mapping[str, Any],
    reproduction: Mapping[str, Any],
    frozen_runs: Mapping[str, Any],
    outputs: Mapping[str, Any],
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
    input_files = {str(path.resolve()): _file_record(path) for path in paths}
    return {
        "schema_version": 2,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "offline_only": True,
        "inputs": input_files,
        "binding": dict(binding),
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
        "configuration": {
            "path": str(config_path.resolve()),
            "source_sha256": sha256_file(config_path),
            "canonical_sha256": canonical_json_sha256(config),
            "value": config,
        },
        "runtime": dict(runtime),
        "reproduction": dict(reproduction),
        "frozen_runs": dict(frozen_runs),
        "outputs": dict(outputs),
    }
