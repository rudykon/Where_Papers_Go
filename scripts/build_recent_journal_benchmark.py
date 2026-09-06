#!/usr/bin/env python3
"""Build a recent-paper journal recommendation benchmark from Crossref metadata.

The builder starts from the project's JCR Q1--Q4 journal catalog, stratifies
known journals by broad field and quartile, and then retrieves recent articles
from Crossref's official ``/journals/{ISSN}/works`` endpoint.  A returned work
is accepted only when its own ISSN metadata maps unambiguously back to exactly
one internal journal.  This keeps the published journal as a natural label
without relying on fuzzy name matching.

Generated data is intended for evaluation, not redistribution without checking
the copyright terms that apply to individual abstracts.

For a future formal acquisition, pass an append-only request ledger together
with ``--require-complete-acquisition-evidence``.  That mode refuses redirects,
reopens the immutable raw-response cache, reconstructs every accepted row, and
publishes a content-addressed provenance bundle without replacing prior output.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import html
import http.client
import json
import os
import re
import socket
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# Support both ``python -m scripts.build_recent_journal_benchmark`` and direct
# execution from an arbitrary working directory.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from where_paper_go.paths import DATA_DIR, PROJECT_ROOT
from where_paper_go.recommender import (
    DATA_FILES,
    CURATED_SCOPE_FILE,
    VenueCandidate,
    build_candidates,
    load_records,
    normalize_name,
    normalize_space,
    parse_targets,
    valid_issn_token,
)


CROSSREF_API = "https://api.crossref.org"
PERMANENT_HTTP_ERROR_STATUSES = frozenset({400, 404, 405, 410, 422})
MAX_CROSSREF_RESPONSE_BYTES = 128 * 1024 * 1024
MAX_REQUEST_LEDGER_BYTES = 16 * 1024 * 1024
MAX_BUDGET_BINDING_BYTES = 64 * 1024
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "benchmark_artifacts" / "recent_journals"
DEFAULT_BUDGET_REGISTRY_DIR = (
    PROJECT_ROOT / "benchmark_artifacts" / ".crossref_request_budget_registry"
)
DEFAULT_CONTACT = "rudykon@users.noreply.github.com"
DEFAULT_SEED = "where-papers-go-recent-journals-v1"
DEFAULT_SAMPLE_SIZE = 500
QUARTILES = ("Q1", "Q2", "Q3", "Q4")
QUARTILE_ORDER = {value: index for index, value in enumerate(QUARTILES)}

BROAD_FIELD_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "computer_engineering",
        (
            "COMPUTER SCIENCE",
            "ENGINEERING",
            "TELECOMMUNICATIONS",
            "AUTOMATION & CONTROL",
            "ROBOTICS",
            "TRANSPORTATION SCIENCE & TECHNOLOGY",
            "INSTRUMENTS & INSTRUMENTATION",
            "ERGONOMICS",
        ),
    ),
    (
        "mathematics_statistics",
        (
            "MATHEMATICS",
            "STATISTICS & PROBABILITY",
            "OPERATIONS RESEARCH",
            "LOGIC",
            "SOCIAL SCIENCES, MATHEMATICAL METHODS",
        ),
    ),
    (
        "physical_chemical_materials",
        (
            "PHYSICS",
            "CHEMISTRY",
            "MATERIALS SCIENCE",
            "ASTRONOMY",
            "OPTICS",
            "ACOUSTICS",
            "CRYSTALLOGRAPHY",
            "ELECTROCHEMISTRY",
            "NANOSCIENCE",
            "NUCLEAR SCIENCE",
            "SPECTROSCOPY",
            "THERMODYNAMICS",
            "MECHANICS",
            "POLYMER SCIENCE",
        ),
    ),
    (
        "clinical_medicine",
        (
            "MEDICINE",
            "SURGERY",
            "ONCOLOGY",
            "NEUROLOGY",
            "PSYCHIATRY",
            "CARDIAC",
            "CARDIOVASCULAR",
            "DENTISTRY",
            "PHARMACOLOGY",
            "RADIOLOGY",
            "HEMATOLOGY",
            "IMMUNOLOGY",
            "ENDOCRINOLOGY",
            "GASTROENTEROLOGY",
            "HEPATOLOGY",
            "PEDIATRICS",
            "OBSTETRICS",
            "GYNECOLOGY",
            "UROLOGY",
            "NEPHROLOGY",
            "OPHTHALMOLOGY",
            "ANESTHESIOLOGY",
            "DERMATOLOGY",
            "RHEUMATOLOGY",
            "RESPIRATORY SYSTEM",
            "PATHOLOGY",
            "ALLERGY",
            "NUTRITION & DIETETICS",
            "PUBLIC, ENVIRONMENTAL & OCCUPATIONAL HEALTH",
            "HEALTH CARE SCIENCES",
            "HEALTH POLICY",
            "NURSING",
            "REHABILITATION",
            "SPORT SCIENCES",
            "SUBSTANCE ABUSE",
            "PRIMARY HEALTH CARE",
            "OTORHINOLARYNGOLOGY",
            "AUDIOLOGY",
            "TRANSPLANTATION",
            "ANDROLOGY",
        ),
    ),
    (
        "life_sciences",
        (
            "BIOLOGY",
            "BIOCHEMISTRY",
            "BIOTECHNOLOGY",
            "CELL ",
            "CELL BIOLOGY",
            "GENETICS",
            "MICROBIOLOGY",
            "NEUROSCIENCES",
            "PHYSIOLOGY",
            "BIOPHYSICS",
            "ANATOMY",
            "DEVELOPMENTAL BIOLOGY",
            "EVOLUTIONARY BIOLOGY",
            "PARASITOLOGY",
            "VIROLOGY",
            "MYCOLOGY",
            "TOXICOLOGY",
            "REPRODUCTIVE BIOLOGY",
            "MATHEMATICAL & COMPUTATIONAL BIOLOGY",
            "BIOCHEMICAL RESEARCH METHODS",
        ),
    ),
    (
        "earth_environment_agriculture",
        (
            "ENVIRONMENTAL",
            "ECOLOGY",
            "GEOGRAPHY, PHYSICAL",
            "GEOSCIENCES",
            "GEOLOGY",
            "GEOCHEMISTRY",
            "GEOPHYSICS",
            "METEOROLOGY",
            "ATMOSPHERIC",
            "OCEANOGRAPHY",
            "PALEONTOLOGY",
            "MINERALOGY",
            "REMOTE SENSING",
            "WATER RESOURCES",
            "AGRICULTURE",
            "AGRONOMY",
            "PLANT SCIENCES",
            "SOIL SCIENCE",
            "FORESTRY",
            "FISHERIES",
            "MARINE & FRESHWATER BIOLOGY",
            "LIMNOLOGY",
            "VETERINARY",
            "FOOD SCIENCE",
            "HORTICULTURE",
            "ENTOMOLOGY",
            "ZOOLOGY",
            "ORNITHOLOGY",
            "BIODIVERSITY CONSERVATION",
        ),
    ),
    (
        "social_sciences",
        (
            "ECONOMICS",
            "EDUCATION",
            "BUSINESS",
            "MANAGEMENT",
            "LAW",
            "COMMUNICATION",
            "POLITICAL SCIENCE",
            "INTERNATIONAL RELATIONS",
            "SOCIOLOGY",
            "PSYCHOLOGY",
            "ANTHROPOLOGY",
            "SOCIAL SCIENCES",
            "SOCIAL WORK",
            "PUBLIC ADMINISTRATION",
            "CRIMINOLOGY",
            "DEMOGRAPHY",
            "DEVELOPMENT STUDIES",
            "URBAN STUDIES",
            "WOMENS STUDIES",
            "ETHNIC STUDIES",
            "FAMILY STUDIES",
            "INDUSTRIAL RELATIONS",
            "HOSPITALITY",
            "INFORMATION SCIENCE & LIBRARY SCIENCE",
            "AREA STUDIES",
            "GEOGRAPHY",
        ),
    ),
    (
        "arts_humanities",
        (
            "HISTORY",
            "PHILOSOPHY",
            "LINGUISTICS",
            "LANGUAGE",
            "LITERATURE",
            "CULTURAL STUDIES",
            "ETHICS",
        ),
    ),
)
BROAD_FIELDS = tuple(field for field, _patterns in BROAD_FIELD_RULES) + (
    "multidisciplinary_other",
)

NOTICE_TITLE_RE = re.compile(
    r"^(?:"
    r"author correction|publisher correction|correction(?:\s+to)?|corrigendum|"
    r"erratum|retraction(?:\s+notice)?|expression of concern|editorial|"
    r"letter to (?:the )?editor|reply to|response to|comment on|addendum"
    r")\b",
    re.IGNORECASE,
)
NOTICE_RELATIONS = {
    "is-correction-of",
    "is-retraction-of",
    "is-update-of",
    "is-expression-of-concern-for",
}


@dataclass(frozen=True)
class JournalVenue:
    """A strict JCR journal identity available to the recommender."""

    venue_id: str
    entity_id: int
    name: str
    quartile: str
    category: str
    broad_field: str
    issns: tuple[str, ...]
    lookup_issn: str


@dataclass(frozen=True)
class BuildWindow:
    from_date: date
    until_date: date


@dataclass(frozen=True)
class CrossrefResponseEvidence:
    """Content-addressed identity of one full cached Crossref response."""

    request_url_sha256: str
    cache_relative_path: str
    response_sha256: str
    response_bytes: int
    observed_via: str
    request_descriptor: dict[str, Any]


@dataclass(frozen=True)
class CrossrefItemEvidence:
    """One item plus its exact position in a full response cache object."""

    item: dict[str, Any]
    item_index: int
    response: CrossrefResponseEvidence


class _RejectCrossrefRedirects(urllib.request.HTTPRedirectHandler):
    """Reject redirects so urllib cannot open an unreserved second socket."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        del req, fp, msg, headers, newurl
        raise ValueError(
            "Crossref redirect refused: every network hop requires a separate "
            f"pre-socket reservation (HTTP {code})"
        )


class _JatsTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.casefold().split(":")[-1] in {"p", "title", "sec", "br", "li"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold().split(":")[-1] in {"p", "title", "sec", "li"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def jats_to_text(value: Any) -> str:
    """Convert the JATS fragments returned by Crossref into plain text."""

    if not isinstance(value, str) or not value.strip():
        return ""
    parser = _JatsTextExtractor()
    try:
        parser.feed(value)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", value)
    text = normalize_space(html.unescape(text))
    return re.sub(r"^(?:abstract|summary)\s*[:.—-]?\s*", "", text, flags=re.I)


def classify_broad_field(category: str) -> str:
    normalized = normalize_space(category).upper()
    for field, patterns in BROAD_FIELD_RULES:
        if any(
            re.search(
                rf"(?<![A-Z0-9]){re.escape(pattern)}(?![A-Z0-9])",
                normalized,
            )
            for pattern in patterns
        ):
            return field
    return "multidisciplinary_other"


def _stable_digest(*parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _format_issn(token: str) -> str:
    return f"{token[:4]}-{token[4:]}"


def _candidate_to_venue(candidate: VenueCandidate) -> JournalVenue | None:
    jcr_rows = [
        record
        for record in candidate.matched_records
        if record.dataset == "jcr" and record.level in QUARTILES
    ]
    quartiles = {record.level for record in jcr_rows}
    if len(quartiles) != 1:
        return None
    tokens = sorted(
        {
            token
            for record in candidate.records
            for token in (
                valid_issn_token(record.issn),
                valid_issn_token(record.eissn),
            )
            if token
        }
    )
    if not tokens:
        return None
    category = normalize_space(jcr_rows[0].area) or "UNCLASSIFIED"
    venue_id = "jcr-" + _stable_digest(*tokens)[:16]
    return JournalVenue(
        venue_id=venue_id,
        entity_id=min(record.row_id for record in candidate.records),
        name=candidate.name,
        quartile=next(iter(quartiles)),
        category=category,
        broad_field=classify_broad_field(category),
        issns=tuple(tokens),
        lookup_issn=_format_issn(tokens[0]),
    )


def load_jcr_venues(data_dir: Path = DATA_DIR) -> tuple[list[JournalVenue], set[str]]:
    """Load strict Q1--Q4 entities through the recommender's canonical API."""

    targets = parse_targets([f"JCR-{quartile}" for quartile in QUARTILES])
    candidates = build_candidates(
        load_records(data_dir),
        targets,
        record_type="journal",
    )
    venues = [venue for candidate in candidates if (venue := _candidate_to_venue(candidate))]
    owners: dict[str, set[str]] = defaultdict(set)
    for venue in venues:
        for token in venue.issns:
            owners[token].add(venue.venue_id)
    ambiguous = {token for token, venue_ids in owners.items() if len(venue_ids) != 1}
    if ambiguous:
        venues = [venue for venue in venues if not ambiguous.intersection(venue.issns)]
    return venues, ambiguous


def build_issn_index(
    venues: Iterable[JournalVenue],
) -> dict[str, tuple[JournalVenue, ...]]:
    owners: dict[str, dict[str, JournalVenue]] = defaultdict(dict)
    for venue in venues:
        for token in venue.issns:
            valid = valid_issn_token(token)
            if valid:
                owners[valid][venue.venue_id] = venue
    return {
        token: tuple(sorted(values.values(), key=lambda venue: venue.venue_id))
        for token, values in owners.items()
    }


def crossref_item_issns(item: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[Any] = []
    raw = item.get("ISSN")
    values.extend(raw if isinstance(raw, list) else [raw])
    typed = item.get("issn-type")
    if isinstance(typed, list):
        values.extend(
            entry.get("value") for entry in typed if isinstance(entry, Mapping)
        )
    return tuple(sorted({valid_issn_token(str(value or "")) for value in values} - {""}))


def resolve_item_venue(
    item: Mapping[str, Any],
    issn_index: Mapping[str, tuple[JournalVenue, ...]],
) -> tuple[JournalVenue | None, str]:
    tokens = crossref_item_issns(item)
    if not tokens:
        return None, "unmapped_issn"
    owners: dict[str, JournalVenue] = {}
    for token in tokens:
        matches = issn_index.get(token, ())
        if len(matches) > 1:
            return None, "ambiguous_issn"
        if matches:
            owners[matches[0].venue_id] = matches[0]
    if not owners:
        return None, "unmapped_issn"
    if len(owners) > 1:
        return None, "ambiguous_issn"
    return next(iter(owners.values())), "ok"


def _first_text(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    return normalize_space(str(value or ""))


def _date_from_parts(value: Any) -> tuple[date, str] | None:
    if not isinstance(value, Mapping):
        return None
    parts_list = value.get("date-parts")
    if not isinstance(parts_list, list) or not parts_list or not parts_list[0]:
        return None
    parts = parts_list[0]
    if not isinstance(parts, list):
        return None
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        day = int(parts[2]) if len(parts) > 2 else 1
        precision = "day" if len(parts) > 2 else "month" if len(parts) > 1 else "year"
        return date(year, month, day), precision
    except (TypeError, ValueError, IndexError):
        return None


def crossref_publication_date(item: Mapping[str, Any]) -> tuple[date, str] | None:
    """Prefer the most precise Crossref date, then the earliest such date."""

    dates = [
        parsed
        for key in ("published-online", "published-print", "published", "issued")
        if (parsed := _date_from_parts(item.get(key))) is not None
    ]
    precision_order = {"day": 0, "month": 1, "year": 2}
    return min(dates, key=lambda value: (precision_order[value[1]], value[0])) if dates else None


def is_non_article_notice(item: Mapping[str, Any], title: str) -> bool:
    if normalize_space(str(item.get("type") or "")) != "journal-article":
        return True
    if NOTICE_TITLE_RE.search(title):
        return True
    if item.get("update-to"):
        return True
    relation = item.get("relation")
    return isinstance(relation, Mapping) and bool(NOTICE_RELATIONS.intersection(relation))


def prepare_crossref_record(
    item: Mapping[str, Any],
    *,
    issn_index: Mapping[str, tuple[JournalVenue, ...]],
    expected_venue: JournalVenue,
    window: BuildWindow,
    min_abstract_chars: int,
) -> tuple[dict[str, Any] | None, str]:
    """Validate and normalize one Crossref work without any network access."""

    title = jats_to_text(_first_text(item.get("title")))
    if not title:
        return None, "missing_title"
    if is_non_article_notice(item, title):
        return None, "non_article_notice"
    abstract = jats_to_text(item.get("abstract"))
    if len(abstract) < min_abstract_chars:
        return None, "short_abstract"
    published_info = crossref_publication_date(item)
    if published_info is None:
        return None, "outside_date_window"
    published, date_precision = published_info
    if date_precision == "year":
        return None, "imprecise_publication_date"
    if not (window.from_date <= published <= window.until_date):
        return None, "outside_date_window"
    venue, status = resolve_item_venue(item, issn_index)
    if venue is None:
        return None, status
    if venue.venue_id != expected_venue.venue_id:
        return None, "mismatched_journal"
    doi = normalize_space(str(item.get("DOI") or "")).casefold()
    if not doi:
        return None, "missing_doi"
    container_title = _first_text(item.get("container-title"))
    return (
        {
            "paper_id": "doi:" + doi,
            "doi": doi,
            "title": title,
            "abstract": abstract,
            "publication_date": published.isoformat(),
            "publication_date_precision": date_precision,
            "language": normalize_space(str(item.get("language") or "")),
            "article_type": "journal-article",
            "gold_journal_id": venue.venue_id,
            "gold_entity_id": venue.entity_id,
            "gold_journal_name": venue.name,
            "gold_container_title": container_title,
            "gold_issns": [_format_issn(token) for token in venue.issns],
            "gold_jcr_quartile": venue.quartile,
            "gold_jcr_category": venue.category,
            "broad_field": venue.broad_field,
            "source": "crossref",
            "source_url": "https://doi.org/" + urllib.parse.quote(doi, safe="/"),
        },
        "ok",
    )


def stratified_journal_order(
    venues: Iterable[JournalVenue],
    *,
    fields: set[str],
    quartiles: set[str],
    seed: str,
) -> dict[tuple[str, str], list[JournalVenue]]:
    strata: dict[tuple[str, str], list[JournalVenue]] = defaultdict(list)
    for venue in venues:
        if venue.broad_field in fields and venue.quartile in quartiles:
            strata[(venue.broad_field, venue.quartile)].append(venue)
    for key, values in strata.items():
        values.sort(
            key=lambda venue: (
                _stable_digest(seed, key[0], key[1], venue.venue_id),
                venue.venue_id,
            )
        )
    return dict(strata)


def allocate_stratum_targets(
    strata: Iterable[tuple[str, str]],
    *,
    sample_size: int | None,
    samples_per_stratum: int,
) -> dict[tuple[str, str], int]:
    """Allocate a total sample evenly across fields and quartiles.

    The diagonal remainder order covers every selected field before returning
    to it and rotates quartiles across fields.  It therefore remains balanced
    even for a pilot smaller than the total number of strata.
    """

    keys = set(strata)
    if not keys:
        return {}
    if sample_size is None:
        return {key: samples_per_stratum for key in keys}
    base, remainder = divmod(sample_size, len(keys))
    targets = {key: base for key in keys}
    fields = sorted({field for field, _quartile in keys})
    quartiles = sorted(
        {quartile for _field, quartile in keys},
        key=lambda value: QUARTILE_ORDER[value],
    )
    remainder_order: list[tuple[str, str]] = []
    for diagonal in range(len(quartiles)):
        for field_index, field in enumerate(fields):
            key = (field, quartiles[(field_index + diagonal) % len(quartiles)])
            if key in keys:
                remainder_order.append(key)
    remainder_order.extend(sorted(keys - set(remainder_order)))
    for key in remainder_order[:remainder]:
        targets[key] += 1
    return targets


def select_records_for_journal(
    records: Iterable[dict[str, Any]],
    *,
    limit: int,
    seed: str,
    venue_id: str,
) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for record in records:
        key = normalize_space(str(record.get("doi") or record.get("paper_id") or "")).casefold()
        if key:
            unique.setdefault(key, record)
    ordered = sorted(
        unique.values(),
        key=lambda record: (
            _stable_digest(seed, venue_id, record.get("doi", "")),
            str(record.get("doi", "")),
        ),
    )
    return ordered[:limit]


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _secure_read_regular_file(
    path: Path,
    *,
    max_bytes: int,
    shared_lock: bool = False,
) -> tuple[bytes, os.stat_result]:
    """Read one owned regular file without following links or accepting races."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    locked = False
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
            raise ValueError(f"evidence source is not an owned regular file: {path}")
        if before.st_size > max_bytes:
            raise ValueError(f"evidence source exceeds {max_bytes} bytes: {path}")
        if shared_lock:
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            locked = True
            before = os.fstat(descriptor)
            if before.st_size > max_bytes:
                raise ValueError(f"evidence source exceeds {max_bytes} bytes: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"evidence source exceeds {max_bytes} bytes: {path}")
        after = os.fstat(descriptor)
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
        raw = b"".join(chunks)
        if identity_before != identity_after or len(raw) != before.st_size:
            raise ValueError(f"evidence source changed while being read: {path}")
        return raw, before
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _ensure_private_directory(path: Path, *, require_private: bool = False) -> None:
    """Create a leaf directory privately and reject symlinked/non-private roots."""

    existed = path.exists()
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise ValueError(f"private artifact directory is not a real directory: {path}")
    if not existed:
        os.chmod(path, 0o700)
        _fsync_directory(path.parent)
    elif require_private and stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError(
            f"formal acquisition directory must have mode 0700: {path} "
            f"(observed {stat.S_IMODE(info.st_mode):04o})"
        )


def _publish_new_bytes(path: Path, payload: bytes, *, mode: int) -> str:
    """Durably publish bytes without ever replacing an existing path."""

    _ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise ValueError(f"refusing to overwrite existing artifact: {path}") from exc
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        _fsync_directory(path.parent)
    return hashlib.sha256(payload).hexdigest()


def _publish_immutable_cache_bytes(path: Path, payload: bytes) -> str:
    """Publish a private raw response cache, accepting only identical races."""

    expected_sha256 = hashlib.sha256(payload).hexdigest()
    try:
        return _publish_new_bytes(path, payload, mode=0o400)
    except ValueError as error:
        if "refusing to overwrite existing artifact" not in str(error):
            raise
        observed, _info = _secure_read_regular_file(
            path, max_bytes=MAX_CROSSREF_RESPONSE_BYTES
        )
        if hashlib.sha256(observed).hexdigest() != expected_sha256:
            raise ValueError(
                f"immutable Crossref cache collision or content change: {path}"
            ) from error
        return expected_sha256


def _load_json_object_bytes(raw: bytes, *, source: Path | str) -> dict[str, Any]:
    if len(raw) > MAX_CROSSREF_RESPONSE_BYTES:
        raise ValueError(
            f"Crossref response exceeds {MAX_CROSSREF_RESPONSE_BYTES} bytes: {source}"
        )

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number {value!r} in {source}")

    try:
        payload = json.loads(raw, parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid Crossref JSON object: {source}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Crossref returned a non-object JSON response: {source}")
    return payload


def _read_bounded_response(response: Any, *, source: str) -> bytes:
    raw = response.read(MAX_CROSSREF_RESPONSE_BYTES + 1)
    if len(raw) > MAX_CROSSREF_RESPONSE_BYTES:
        raise ValueError(
            f"Crossref response exceeds {MAX_CROSSREF_RESPONSE_BYTES} bytes: {source}"
        )
    return raw


def _ledger_budget_binding_path(ledger: Path) -> Path:
    return ledger.with_name(ledger.name + ".budget.json")


def _ledger_highwater_path(ledger: Path) -> Path:
    return ledger.with_name(ledger.name + ".highwater.jsonl")


def _budget_registry_claim_path(registry_dir: Path, budget_id: str) -> Path:
    return registry_dir / (hashlib.sha256(budget_id.encode("utf-8")).hexdigest() + ".json")


def _budget_registry_usage_path(registry_dir: Path, budget_id: str) -> Path:
    return registry_dir / (
        hashlib.sha256(budget_id.encode("utf-8")).hexdigest() + ".attempts.jsonl"
    )


def _budget_binding_payload(
    *, budget_id: str, hard_ceiling: int, ledger: Path, highwater: Path
) -> dict[str, Any]:
    _raw, ledger_info = _secure_read_regular_file(
        ledger, max_bytes=MAX_REQUEST_LEDGER_BYTES, shared_lock=True
    )
    _highwater_raw, highwater_info = _secure_read_regular_file(
        highwater, max_bytes=MAX_REQUEST_LEDGER_BYTES, shared_lock=True
    )
    return _budget_binding_payload_from_identity(
        budget_id=budget_id,
        hard_ceiling=hard_ceiling,
        ledger=ledger,
        ledger_info=ledger_info,
        highwater=highwater,
        highwater_info=highwater_info,
    )


def _budget_binding_payload_from_identity(
    *,
    budget_id: str,
    hard_ceiling: int,
    ledger: Path,
    ledger_info: os.stat_result,
    highwater: Path,
    highwater_info: os.stat_result,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_type": "crossref_http_attempt_budget_binding",
        "budget_id": budget_id,
        "hard_http_attempt_ceiling": hard_ceiling,
        "ledger_path_sha256": hashlib.sha256(
            str(ledger.resolve()).encode("utf-8")
        ).hexdigest(),
        "ledger_device": ledger_info.st_dev,
        "ledger_inode": ledger_info.st_ino,
        "highwater_path_sha256": hashlib.sha256(
            str(highwater.resolve()).encode("utf-8")
        ).hexdigest(),
        "highwater_device": highwater_info.st_dev,
        "highwater_inode": highwater_info.st_ino,
    }


def _global_budget_claim_payload(
    base: Mapping[str, Any], *, usage_path: Path
) -> dict[str, Any]:
    _raw, usage_info = _secure_read_regular_file(
        usage_path, max_bytes=MAX_REQUEST_LEDGER_BYTES, shared_lock=True
    )
    return _global_budget_claim_payload_from_identity(
        base, usage_path=usage_path, usage_info=usage_info
    )


def _global_budget_claim_payload_from_identity(
    base: Mapping[str, Any], *, usage_path: Path, usage_info: os.stat_result
) -> dict[str, Any]:
    claim = dict(base)
    claim["artifact_type"] = "crossref_global_http_attempt_budget_claim"
    claim["global_usage_path_sha256"] = hashlib.sha256(
        str(usage_path.resolve()).encode("utf-8")
    ).hexdigest()
    claim["global_usage_device"] = usage_info.st_dev
    claim["global_usage_inode"] = usage_info.st_ino
    return claim


def _read_and_validate_budget_binding(
    path: Path,
    *,
    expected: Mapping[str, Any],
) -> dict[str, Any]:
    raw, info = _secure_read_regular_file(
        path, max_bytes=MAX_BUDGET_BINDING_BYTES
    )
    payload = _load_json_object_bytes(raw, source=path)
    if payload != expected:
        raise ValueError(
            "Crossref budget binding mismatch; refusing to change the cumulative "
            f"hard ceiling, budget ID, or ledger identity: {path}"
        )
    if stat.S_IMODE(info.st_mode) != 0o400:
        raise ValueError(f"Crossref budget binding must have mode 0400: {path}")
    return payload


def _initialize_or_verify_budget_binding(
    ledger: Path,
    *,
    highwater: Path,
    budget_id: str,
    hard_ceiling: int,
    existing_rows: Sequence[Mapping[str, Any]],
    existing_highwater_rows: Sequence[Mapping[str, Any]],
    require_bound: bool,
    registry_dir: Path | None,
) -> tuple[Path | None, Path | None, Path, Path | None]:
    """Bind a new ledger to one immutable ceiling before its first reservation."""

    binding_path = _ledger_budget_binding_path(ledger)
    binding_was_present = binding_path.exists()
    expected = _budget_binding_payload(
        budget_id=budget_id,
        hard_ceiling=hard_ceiling,
        ledger=ledger,
        highwater=highwater,
    )
    if binding_path.exists():
        _read_and_validate_budget_binding(binding_path, expected=expected)
    elif existing_rows:
        raise ValueError(
            "Crossref acquisition refuses a pre-existing nonempty ledger without "
            "a pre-attempt immutable hard-ceiling binding; it cannot be adopted "
            "retroactively without expanding or fabricating authorization"
        )
    else:
        _publish_immutable_cache_bytes(
            binding_path, _canonical_json_bytes(expected) + b"\n"
        )
        _read_and_validate_budget_binding(binding_path, expected=expected)
    if list(existing_rows) != list(existing_highwater_rows):
        raise ValueError(
            "Crossref request ledger rolled back or diverged from its independent "
            "high-water anchor"
        )

    claim_path: Path | None = None
    usage_path: Path | None = None
    if registry_dir is not None:
        _ensure_private_directory(registry_dir, require_private=require_bound)
        claim_path = _budget_registry_claim_path(registry_dir, budget_id)
        usage_path = _budget_registry_usage_path(registry_dir, budget_id)
        if claim_path.exists() and not usage_path.exists():
            raise ValueError(
                "Crossref global usage anchor is missing behind its immutable claim"
            )
        if not usage_path.exists():
            _publish_new_bytes(usage_path, b"", mode=0o600)
        usage_rows = _read_request_ledger(usage_path, budget_id=budget_id)
        if list(usage_rows) != list(existing_rows):
            raise ValueError(
                "Crossref request ledger rolled back or diverged from the global "
                "append-only usage anchor"
            )
        claim = _global_budget_claim_payload(expected, usage_path=usage_path)
        if claim_path.exists():
            _read_and_validate_budget_binding(claim_path, expected=claim)
        elif binding_was_present:
            raise ValueError(
                "Crossref global budget claim is missing behind an existing local "
                "binding; refusing to recreate a pre-attempt claim after the fact"
            )
        else:
            _publish_immutable_cache_bytes(
                claim_path, _canonical_json_bytes(claim) + b"\n"
            )
            _read_and_validate_budget_binding(claim_path, expected=claim)
    elif require_bound:
        raise ValueError(
            "formal Crossref acquisition requires a fixed global budget registry"
        )
    return binding_path, claim_path, highwater, usage_path


def _parse_request_ledger_lines(
    lines: Iterable[str],
    *,
    path: Path,
    budget_id: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"invalid Crossref request ledger JSON: {path}:{line_number}"
            ) from exc
        if not isinstance(record, dict):
            raise ValueError(
                f"invalid Crossref request ledger record: {path}:{line_number}"
            )
        expected_sequence = len(rows) + 1
        expected_keys = {
            "schema_version",
            "event",
            "sequence",
            "budget_id",
            "reserved_at",
            "request_url_sha256",
            "recorded_after_fact",
        }
        reserved_at = record.get("reserved_at")
        try:
            parsed_reserved_at = datetime.fromisoformat(str(reserved_at))
        except ValueError:
            parsed_reserved_at = None
        if (
            set(record) != expected_keys
            or record.get("schema_version") != 1
            or record.get("event") != "attempt_reserved"
            or record.get("sequence") != expected_sequence
            or record.get("budget_id") != budget_id
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(record.get("request_url_sha256") or "")
            )
            or record.get("recorded_after_fact") is not False
            or parsed_reserved_at is None
            or parsed_reserved_at.tzinfo is None
            or parsed_reserved_at.utcoffset() != timedelta(0)
        ):
            raise ValueError(
                f"Crossref request ledger continuity mismatch: {path}:{line_number}"
            )
        rows.append(record)
    return rows


def _read_request_ledger(
    path: Path | None,
    *,
    budget_id: str,
) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    raw, _info = _secure_read_regular_file(
        path, max_bytes=MAX_REQUEST_LEDGER_BYTES, shared_lock=True
    )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"invalid UTF-8 Crossref request ledger: {path}") from exc
    return _parse_request_ledger_lines(
        text.splitlines(keepends=True),
        path=path,
        budget_id=budget_id,
    )


class CrossrefClient:
    def __init__(
        self,
        *,
        cache_dir: Path,
        mailto: str,
        timeout: float,
        retries: int,
        request_interval: float,
        use_environment_proxy: bool,
        refresh_cache: bool,
        max_network_requests: int,
        request_ledger: Path | None = None,
        request_budget_id: str = "",
        require_private_storage: bool = False,
        budget_registry_dir: Path | None = None,
    ) -> None:
        self.cache_dir = cache_dir
        _ensure_private_directory(
            self.cache_dir, require_private=require_private_storage
        )
        self.mailto = mailto
        self.timeout = timeout
        self.retries = retries
        self.request_interval = request_interval
        self.refresh_cache = refresh_cache
        self.max_network_requests = max_network_requests
        self.request_ledger = request_ledger
        self.request_budget_id = request_budget_id
        self.require_private_storage = require_private_storage
        self.budget_registry_dir = budget_registry_dir
        if self.max_network_requests <= 0:
            raise ValueError("Crossref network request hard ceiling must be positive")
        if self.request_ledger is not None and not self.request_budget_id:
            raise ValueError("Crossref request ledger requires a budget ID")
        if self.request_ledger is not None and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}", self.request_budget_id
        ):
            raise ValueError("Crossref request budget ID has an unsafe format")
        if self.request_ledger is not None:
            _ensure_private_directory(
                self.request_ledger.parent,
                require_private=require_private_storage,
            )
            binding_path = _ledger_budget_binding_path(self.request_ledger)
            highwater_path = _ledger_highwater_path(self.request_ledger)
            claim_path = (
                _budget_registry_claim_path(
                    budget_registry_dir, self.request_budget_id
                )
                if budget_registry_dir is not None
                else None
            )
            if not self.request_ledger.exists() and (
                binding_path.exists()
                or highwater_path.exists()
                or (claim_path is not None and claim_path.exists())
            ):
                raise ValueError(
                    "Crossref request ledger is missing behind an immutable budget "
                    "binding; refusing to reset the cumulative attempt count"
                )
            if not self.request_ledger.exists():
                _publish_new_bytes(self.request_ledger, b"", mode=0o600)
            elif not binding_path.exists() and self.request_ledger.lstat().st_size:
                raise ValueError(
                    "Crossref acquisition refuses a pre-existing nonempty ledger "
                    "without a pre-attempt immutable hard-ceiling binding; it cannot "
                    "be adopted retroactively without expanding or fabricating "
                    "authorization"
                )
            if binding_path.exists() and not highwater_path.exists():
                raise ValueError(
                    "Crossref high-water anchor is missing behind its immutable "
                    "budget binding"
                )
            if not highwater_path.exists():
                _publish_new_bytes(highwater_path, b"", mode=0o600)
            for private_path in (self.request_ledger, highwater_path):
                ledger_info = private_path.lstat()
                if (
                    require_private_storage
                    and stat.S_IMODE(ledger_info.st_mode) != 0o600
                ):
                    raise ValueError(
                        "formal Crossref request ledger must have mode 0600: "
                        f"{private_path}"
                    )
        ledger_rows = _read_request_ledger(
            self.request_ledger,
            budget_id=self.request_budget_id,
        )
        self.budget_binding_path: Path | None = None
        self.budget_registry_claim_path: Path | None = None
        self.request_highwater_path: Path | None = None
        self.global_usage_path: Path | None = None
        if self.request_ledger is not None:
            highwater_rows = _read_request_ledger(
                _ledger_highwater_path(self.request_ledger),
                budget_id=self.request_budget_id,
            )
            (
                self.budget_binding_path,
                self.budget_registry_claim_path,
                self.request_highwater_path,
                self.global_usage_path,
            ) = _initialize_or_verify_budget_binding(
                self.request_ledger,
                highwater=_ledger_highwater_path(self.request_ledger),
                budget_id=self.request_budget_id,
                hard_ceiling=self.max_network_requests,
                existing_rows=ledger_rows,
                existing_highwater_rows=highwater_rows,
                require_bound=require_private_storage,
                registry_dir=budget_registry_dir,
            )
        if len(ledger_rows) > self.max_network_requests:
            raise ValueError(
                "Crossref request ledger already exceeds its budget: "
                f"{len(ledger_rows)}/{self.max_network_requests}"
            )
        handlers: list[Any] = [_RejectCrossrefRedirects()]
        if not use_environment_proxy:
            handlers.append(urllib.request.ProxyHandler({}))
        self.opener = urllib.request.build_opener(*handlers)
        self.last_request_at = 0.0
        self.network_requests = 0
        self.cumulative_network_requests = len(ledger_rows)
        self.cache_hits = 0
        self.response_cache_hits = 0
        self.permanent_error_cache_hits = 0
        self._request_lock = threading.Lock()

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}.json"

    def _permanent_error_cache_path(self, url: str) -> Path:
        return self._cache_path(url).with_suffix(".error.json")

    @staticmethod
    def _write_cache_object(path: Path, payload: Mapping[str, Any]) -> None:
        _publish_immutable_cache_bytes(path, _canonical_json_bytes(payload) + b"\n")

    def _reserve_network_request(self, url: str) -> None:
        """Consume one cumulative attempt before opening the network socket."""

        if self.request_ledger is None:
            if self.network_requests >= self.max_network_requests:
                raise ValueError(
                    "Crossref network request budget exhausted before request: "
                    f"{self.network_requests}/{self.max_network_requests}"
                )
            self.network_requests += 1
            self.cumulative_network_requests = self.network_requests
            return

        if self.request_highwater_path is None or self.budget_binding_path is None:
            raise ValueError("Crossref request ledger has no durable high-water binding")
        reservation_paths = [self.request_ledger, self.request_highwater_path]
        if self.global_usage_path is not None:
            reservation_paths.append(self.global_usage_path)
        reservation_paths = sorted(set(reservation_paths), key=lambda path: str(path.resolve()))
        handles: dict[Path, Any] = {}
        opened_infos: dict[Path, os.stat_result] = {}
        try:
            for path in reservation_paths:
                flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(path, flags)
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_size > MAX_REQUEST_LEDGER_BYTES
                    or stat.S_IMODE(info.st_mode) != 0o600
                ):
                    os.close(descriptor)
                    raise ValueError(f"unsafe Crossref reservation anchor: {path}")
                handle = os.fdopen(descriptor, "a+", encoding="utf-8", newline="\n")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                handles[path] = handle
                opened_infos[path] = info

            rows_by_path: dict[Path, list[dict[str, Any]]] = {}
            for path, handle in handles.items():
                handle.seek(0)
                rows_by_path[path] = _parse_request_ledger_lines(
                    handle, path=path, budget_id=self.request_budget_id
                )
            authoritative_rows = rows_by_path[
                self.global_usage_path
                if self.global_usage_path is not None
                else self.request_highwater_path
            ]
            if any(rows != authoritative_rows for rows in rows_by_path.values()):
                raise ValueError(
                    "Crossref reservation ledger rolled back or diverged from its "
                    "independent high-water/global anchor"
                )

            base_binding = _budget_binding_payload_from_identity(
                budget_id=self.request_budget_id,
                hard_ceiling=self.max_network_requests,
                ledger=self.request_ledger,
                ledger_info=opened_infos[self.request_ledger],
                highwater=self.request_highwater_path,
                highwater_info=opened_infos[self.request_highwater_path],
            )
            _read_and_validate_budget_binding(
                self.budget_binding_path, expected=base_binding
            )
            if self.global_usage_path is not None:
                if self.budget_registry_claim_path is None:
                    raise ValueError("global usage anchor lacks an immutable claim")
                claim = _global_budget_claim_payload_from_identity(
                    base_binding,
                    usage_path=self.global_usage_path,
                    usage_info=opened_infos[self.global_usage_path],
                )
                _read_and_validate_budget_binding(
                    self.budget_registry_claim_path, expected=claim
                )

            used = len(authoritative_rows)
            if used >= self.max_network_requests:
                raise ValueError(
                    "Crossref cumulative network request budget exhausted before "
                    f"request: {used}/{self.max_network_requests}"
                )
            sequence = used + 1
            record = {
                "schema_version": 1,
                "event": "attempt_reserved",
                "sequence": sequence,
                "budget_id": self.request_budget_id,
                "reserved_at": datetime.now(timezone.utc).isoformat(),
                "request_url_sha256": hashlib.sha256(url.encode("utf-8")).hexdigest(),
                "recorded_after_fact": False,
            }
            line = (
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                )
                + "\n"
            )
            durability_order = [
                path
                for path in (
                    self.global_usage_path,
                    self.request_highwater_path,
                    self.request_ledger,
                )
                if path is not None
            ]
            for write_number, path in enumerate(durability_order, 1):
                handle = handles[path]
                handle.seek(0, os.SEEK_END)
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
                after = os.fstat(handle.fileno())
                before = opened_infos[path]
                if (
                    (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
                    or after.st_size > MAX_REQUEST_LEDGER_BYTES
                ):
                    raise ValueError(
                        f"Crossref reservation anchor changed while appending: {path}"
                    )
                if getattr(self, "_reservation_fault_after_writes", None) == write_number:
                    raise RuntimeError(
                        "injected crash after durable Crossref reservation anchor write"
                    )
            for parent in {path.parent for path in durability_order}:
                _fsync_directory(parent)
            self.network_requests += 1
            self.cumulative_network_requests = sequence
        finally:
            for path in reversed(reservation_paths):
                handle = handles.get(path)
                if handle is not None:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    finally:
                        handle.close()

    def get_json_with_evidence(
        self, path: str, params: Mapping[str, str]
    ) -> tuple[dict[str, Any], CrossrefResponseEvidence]:
        request_descriptor = _crossref_request_descriptor(
            path, params, mailto=self.mailto
        )
        url = _crossref_url_from_descriptor(request_descriptor)
        request_url_sha256 = hashlib.sha256(url.encode("utf-8")).hexdigest()
        cache_path = self._cache_path(url)
        permanent_error_cache_path = self._permanent_error_cache_path(url)
        if cache_path.exists() and not self.refresh_cache:
            with self._request_lock:
                self.cache_hits += 1
                self.response_cache_hits += 1
            raw, _cache_info = _secure_read_regular_file(
                cache_path, max_bytes=MAX_CROSSREF_RESPONSE_BYTES
            )
            cached = _load_json_object_bytes(raw, source=cache_path)
            return cached, CrossrefResponseEvidence(
                request_url_sha256=request_url_sha256,
                cache_relative_path=cache_path.name,
                response_sha256=hashlib.sha256(raw).hexdigest(),
                response_bytes=len(raw),
                observed_via="cache",
                request_descriptor=request_descriptor,
            )
        if permanent_error_cache_path.exists() and not self.refresh_cache:
            raw_error, _error_info = _secure_read_regular_file(
                permanent_error_cache_path,
                max_bytes=MAX_BUDGET_BINDING_BYTES,
            )
            cached_error = _load_json_object_bytes(
                raw_error,
                source=permanent_error_cache_path,
            )
            expected_url_sha256 = request_url_sha256
            if (
                not isinstance(cached_error, dict)
                or cached_error.get("schema_version") != 1
                or cached_error.get("artifact_type")
                != "crossref_permanent_http_error"
                or cached_error.get("request_url_sha256") != expected_url_sha256
                or cached_error.get("status") not in PERMANENT_HTTP_ERROR_STATUSES
                or not isinstance(cached_error.get("reason"), str)
            ):
                raise ValueError(
                    "invalid cached permanent Crossref error: "
                    f"{permanent_error_cache_path}"
                )
            with self._request_lock:
                self.cache_hits += 1
                self.permanent_error_cache_hits += 1
            raise urllib.error.HTTPError(
                url,
                int(cached_error["status"]),
                str(cached_error["reason"]),
                None,
                None,
            )

        retryable = {429, 500, 502, 503, 504}
        for attempt in range(self.retries + 1):
            with self._request_lock:
                elapsed = time.monotonic() - self.last_request_at
                if elapsed < self.request_interval:
                    time.sleep(self.request_interval - elapsed)
                self.last_request_at = time.monotonic()
                self._reserve_network_request(url)
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": (
                        "WherePapersGoBenchmark/1.0 "
                        f"(https://github.com/rudykon/where_papers_go; mailto:{self.mailto})"
                    ),
                },
            )
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    final_url = response.geturl() if hasattr(response, "geturl") else url
                    if final_url != url:
                        raise ValueError(
                            "Crossref response URL changed; redirect hops are forbidden "
                            "without an independent pre-socket reservation"
                        )
                    raw = _read_bounded_response(response, source=url)
                payload = _load_json_object_bytes(raw, source=url)
                response_sha256 = _publish_immutable_cache_bytes(cache_path, raw)
                return payload, CrossrefResponseEvidence(
                    request_url_sha256=request_url_sha256,
                    cache_relative_path=cache_path.name,
                    response_sha256=response_sha256,
                    response_bytes=len(raw),
                    observed_via="network",
                    request_descriptor=request_descriptor,
                )
            except urllib.error.HTTPError as error:
                if error.code in PERMANENT_HTTP_ERROR_STATUSES:
                    self._write_cache_object(
                        permanent_error_cache_path,
                        {
                            "schema_version": 1,
                            "artifact_type": "crossref_permanent_http_error",
                            "recorded_at": datetime.now(timezone.utc).isoformat(),
                            "request_url_sha256": request_url_sha256,
                            "status": error.code,
                            "reason": str(error.reason or "HTTP error"),
                        },
                    )
                    raise
                if error.code not in retryable or attempt >= self.retries:
                    raise
                retry_after = error.headers.get("Retry-After") if error.headers else None
                try:
                    delay = min(120.0, max(0.0, float(retry_after)))
                except (TypeError, ValueError):
                    delay = min(30.0, 2.0**attempt)
                time.sleep(delay)
            except (
                urllib.error.URLError,
                socket.timeout,
                TimeoutError,
                http.client.IncompleteRead,
            ):
                if attempt >= self.retries:
                    raise
                time.sleep(min(30.0, 2.0**attempt))
        raise RuntimeError("unreachable Crossref retry state")

    def get_json(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        payload, _evidence = self.get_json_with_evidence(path, params)
        return payload

    def journal_works(
        self,
        issn: str,
        *,
        window: BuildWindow,
        rows: int,
    ) -> list[dict[str, Any]]:
        filters = ",".join(
            (
                f"from-pub-date:{window.from_date.isoformat()}",
                f"until-pub-date:{window.until_date.isoformat()}",
                "type:journal-article",
                "has-abstract:true",
            )
        )
        payload = self.get_json(
            "/journals/" + urllib.parse.quote(issn, safe="") + "/works",
            {
                "filter": filters,
                "rows": str(rows),
                "sort": "published",
                "order": "desc",
            },
        )
        message = payload.get("message")
        items = message.get("items") if isinstance(message, Mapping) else None
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def journal_works_with_evidence(
        self,
        issn: str,
        *,
        window: BuildWindow,
        rows: int,
    ) -> list[CrossrefItemEvidence]:
        filters = ",".join(
            (
                f"from-pub-date:{window.from_date.isoformat()}",
                f"until-pub-date:{window.until_date.isoformat()}",
                "type:journal-article",
                "has-abstract:true",
            )
        )
        payload, response_evidence = self.get_json_with_evidence(
            "/journals/" + urllib.parse.quote(issn, safe="") + "/works",
            {
                "filter": filters,
                "rows": str(rows),
                "sort": "published",
                "order": "desc",
            },
        )
        message = payload.get("message")
        items = message.get("items") if isinstance(message, Mapping) else None
        if not isinstance(items, list):
            return []
        return [
            CrossrefItemEvidence(item, index, response_evidence)
            for index, item in enumerate(items)
            if isinstance(item, dict)
        ]

    def works_page(
        self,
        *,
        window: BuildWindow,
        rows: int,
        cursor: str = "*",
    ) -> tuple[list[dict[str, Any]], str]:
        """Read one cached cursor page from the global Crossref works stream."""

        filters = ",".join(
            (
                f"from-pub-date:{window.from_date.isoformat()}",
                f"until-pub-date:{window.until_date.isoformat()}",
                "type:journal-article",
                "has-abstract:true",
            )
        )
        payload = self.get_json(
            "/works",
            {
                "filter": filters,
                "rows": str(rows),
                "cursor": cursor,
            },
        )
        message = payload.get("message")
        if not isinstance(message, Mapping):
            return [], ""
        items = message.get("items")
        next_cursor = normalize_space(str(message.get("next-cursor") or ""))
        return (
            [item for item in items if isinstance(item, dict)]
            if isinstance(items, list)
            else [],
            next_cursor,
        )

    def works_page_with_evidence(
        self,
        *,
        window: BuildWindow,
        rows: int,
        cursor: str = "*",
    ) -> tuple[list[CrossrefItemEvidence], str]:
        filters = ",".join(
            (
                f"from-pub-date:{window.from_date.isoformat()}",
                f"until-pub-date:{window.until_date.isoformat()}",
                "type:journal-article",
                "has-abstract:true",
            )
        )
        payload, response_evidence = self.get_json_with_evidence(
            "/works",
            {
                "filter": filters,
                "rows": str(rows),
                "cursor": cursor,
            },
        )
        message = payload.get("message")
        if not isinstance(message, Mapping):
            return [], ""
        items = message.get("items")
        next_cursor = normalize_space(str(message.get("next-cursor") or ""))
        if not isinstance(items, list):
            return [], next_cursor
        return (
            [
                CrossrefItemEvidence(item, index, response_evidence)
                for index, item in enumerate(items)
                if isinstance(item, dict)
            ],
            next_cursor,
        )


def _record_with_item_evidence(
    record: Mapping[str, Any], item_evidence: CrossrefItemEvidence
) -> dict[str, Any]:
    result = dict(record)
    result["_crossref_acquisition_evidence"] = {
        "request_url_sha256": item_evidence.response.request_url_sha256,
        "cache_relative_path": item_evidence.response.cache_relative_path,
        "response_sha256": item_evidence.response.response_sha256,
        "response_bytes": item_evidence.response.response_bytes,
        "observed_via": item_evidence.response.observed_via,
        "request_descriptor": item_evidence.response.request_descriptor,
        "item_index": item_evidence.item_index,
        "canonical_item_sha256": hashlib.sha256(
            _canonical_json_bytes(item_evidence.item)
        ).hexdigest(),
    }
    return result


def _snapshot_request_ledger(
    path: Path | None,
    *,
    budget_id: str,
    hard_ceiling: int,
    budget_binding_path: Path | None,
    budget_registry_claim_path: Path | None,
    highwater_path: Path | None,
    global_usage_path: Path | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if path is None or highwater_path is None:
        return [], None
    if not path.exists() or not highwater_path.exists():
        return [], None

    def read_rows(source: Path) -> tuple[bytes, os.stat_result, list[dict[str, Any]]]:
        raw_value, info_value = _secure_read_regular_file(
            source, max_bytes=MAX_REQUEST_LEDGER_BYTES, shared_lock=True
        )
        try:
            text_value = raw_value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"invalid UTF-8 Crossref request ledger: {source}") from exc
        rows_value = _parse_request_ledger_lines(
            text_value.splitlines(keepends=True), path=source, budget_id=budget_id
        )
        return raw_value, info_value, rows_value

    raw, info, rows = read_rows(path)
    highwater_raw, highwater_info, highwater_rows = read_rows(highwater_path)
    global_raw: bytes | None = None
    global_info: os.stat_result | None = None
    global_rows: list[dict[str, Any]] | None = None
    if global_usage_path is not None:
        global_raw, global_info, global_rows = read_rows(global_usage_path)
    if highwater_rows != rows or (global_rows is not None and global_rows != rows):
        raise ValueError(
            "Crossref request ledger rolled back or diverged from its high-water/global "
            "anchor before evidence snapshot"
        )
    expected_binding = _budget_binding_payload_from_identity(
        budget_id=budget_id,
        hard_ceiling=hard_ceiling,
        ledger=path,
        ledger_info=info,
        highwater=highwater_path,
        highwater_info=highwater_info,
    )

    def bind_budget_file(
        binding_path: Path | None,
        *,
        expected: Mapping[str, Any],
        persisted_path: str,
    ) -> dict[str, Any] | None:
        if binding_path is None:
            return None
        _read_and_validate_budget_binding(binding_path, expected=expected)
        binding_info = binding_path.lstat()
        return {
            "path": persisted_path,
            "_live_path": str(binding_path.resolve()),
            "sha256": _sha256_file(binding_path),
            "bytes": binding_info.st_size,
            "mode": f"{stat.S_IMODE(binding_info.st_mode):04o}",
            **expected,
        }

    global_claim = (
        _global_budget_claim_payload_from_identity(
            expected_binding,
            usage_path=global_usage_path,
            usage_info=global_info,
        )
        if global_usage_path is not None and global_info is not None
        else None
    )

    def usage_binding(
        *,
        source: Path,
        raw_value: bytes,
        info_value: os.stat_result,
        persisted_path: str,
    ) -> dict[str, Any]:
        return {
            "path": persisted_path,
            "_live_path": str(source.resolve()),
            "sha256": hashlib.sha256(raw_value).hexdigest(),
            "bytes": len(raw_value),
            "attempt_records": len(rows),
            "mode": f"{stat.S_IMODE(info_value.st_mode):04o}",
        }

    return rows, {
        "path": "request-ledger-prefix.jsonl",
        "_live_path": str(path.resolve()),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "attempt_records": len(rows),
        "budget_id": budget_id,
        "append_only_reservations": True,
        "mode": f"{stat.S_IMODE(info.st_mode):04o}",
        "hard_http_attempt_ceiling": hard_ceiling,
        "source_ledger_device": info.st_dev,
        "source_ledger_inode": info.st_ino,
        "highwater": usage_binding(
            source=highwater_path,
            raw_value=highwater_raw,
            info_value=highwater_info,
            persisted_path="request-ledger-highwater-prefix.jsonl",
        ),
        "global_usage": (
            usage_binding(
                source=global_usage_path,
                raw_value=global_raw,
                info_value=global_info,
                persisted_path="request-ledger-global-prefix.jsonl",
            )
            if global_usage_path is not None
            and global_raw is not None
            and global_info is not None
            else None
        ),
        "budget_binding": bind_budget_file(
            budget_binding_path,
            expected=expected_binding,
            persisted_path="request-budget-binding.json",
        ),
        "global_budget_claim": bind_budget_file(
            budget_registry_claim_path,
            expected=global_claim or {},
            persisted_path="request-budget-global-claim.json",
        ),
    }


def _merkle_root(leaves: Sequence[Mapping[str, Any]]) -> str:
    level = [hashlib.sha256(_canonical_json_bytes(leaf)).digest() for leaf in leaves]
    if not level:
        return hashlib.sha256(b"").hexdigest()
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [
            hashlib.sha256(b"\x01" + level[index] + level[index + 1]).digest()
            for index in range(0, len(level), 2)
        ]
    return level[0].hex()


def _validate_crossref_request_descriptor(
    descriptor: Any,
    *,
    expected_mailto: str,
    window: BuildWindow,
    expected_bulk_rows: int,
    expected_journal_rows: int,
) -> tuple[dict[str, Any], str]:
    if not isinstance(descriptor, Mapping) or set(descriptor) != {
        "schema_version",
        "base_url",
        "path",
        "query",
    }:
        raise ValueError("invalid Crossref request descriptor schema")
    path = descriptor.get("path")
    query = descriptor.get("query")
    if (
        descriptor.get("schema_version") != 1
        or descriptor.get("base_url") != CROSSREF_API
        or not isinstance(path, str)
        or not isinstance(query, Mapping)
        or not all(isinstance(key, str) and isinstance(value, str) for key, value in query.items())
        or query.get("mailto") != expected_mailto
        or query.get("filter") != _crossref_filters(window)
    ):
        raise ValueError("Crossref request descriptor is not the fixed official protocol")
    if path == "/works":
        if set(query) != {"cursor", "filter", "mailto", "rows"}:
            raise ValueError("invalid Crossref cursor request descriptor")
        if not query.get("cursor"):
            raise ValueError("Crossref cursor request lacks a cursor")
        expected_rows = expected_bulk_rows
    elif re.fullmatch(r"/journals/[0-9Xx-]{8,9}/works", path):
        if set(query) != {"filter", "mailto", "order", "rows", "sort"}:
            raise ValueError("invalid Crossref journal request descriptor")
        if query.get("sort") != "published" or query.get("order") != "desc":
            raise ValueError("Crossref journal request descriptor changed sorting")
        if not valid_issn_token(path.split("/")[2]):
            raise ValueError("Crossref journal request descriptor has an invalid ISSN")
        expected_rows = expected_journal_rows
    else:
        raise ValueError("Crossref request descriptor uses an unsupported endpoint")
    try:
        rows = int(str(query.get("rows") or ""))
    except ValueError as exc:
        raise ValueError("Crossref request descriptor has invalid rows") from exc
    if (
        not 1 <= rows <= 1000
        or str(rows) != query.get("rows")
        or rows != expected_rows
    ):
        raise ValueError("Crossref request descriptor rows are outside the fixed protocol")
    normalized = {
        "schema_version": 1,
        "base_url": CROSSREF_API,
        "path": path,
        "query": dict(sorted(query.items())),
    }
    return normalized, hashlib.sha256(
        _crossref_url_from_descriptor(normalized).encode("utf-8")
    ).hexdigest()


def _verify_acquisition_evidence(
    records: Sequence[Mapping[str, Any]],
    *,
    cache_dir: Path,
    venues: Sequence[JournalVenue],
    issn_index: Mapping[str, tuple[JournalVenue, ...]],
    window: BuildWindow,
    min_abstract_chars: int,
    request_ledger: Path | None,
    request_budget_id: str,
    hard_http_attempt_ceiling: int,
    budget_binding_path: Path | None,
    budget_registry_claim_path: Path | None,
    request_highwater_path: Path | None,
    global_usage_path: Path | None,
    mailto: str,
    bulk_rows: int,
    rows_per_journal: int,
    require_complete: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Reopen raw caches and reconstruct every accepted dataset row."""

    cache_info = cache_dir.lstat()
    if cache_dir.is_symlink() or not stat.S_ISDIR(cache_info.st_mode):
        raise ValueError(f"Crossref cache root is not a real directory: {cache_dir}")
    venue_by_id = {venue.venue_id: venue for venue in venues}
    ledger_rows, ledger_binding = _snapshot_request_ledger(
        request_ledger,
        budget_id=request_budget_id,
        hard_ceiling=hard_http_attempt_ceiling,
        budget_binding_path=budget_binding_path,
        budget_registry_claim_path=budget_registry_claim_path,
        highwater_path=request_highwater_path,
        global_usage_path=global_usage_path,
    )
    ledger_sequences: dict[str, list[int]] = defaultdict(list)
    for row in ledger_rows:
        ledger_sequences[str(row["request_url_sha256"])].append(int(row["sequence"]))

    cache_objects: dict[str, tuple[bytes, dict[str, Any], os.stat_result]] = {}
    cache_request_descriptors: dict[str, dict[str, Any]] = {}
    clean_records: list[dict[str, Any]] = []
    provenance_rows: list[dict[str, Any]] = []
    for dataset_index, record_with_evidence in enumerate(records):
        evidence = record_with_evidence.get("_crossref_acquisition_evidence")
        if not isinstance(evidence, Mapping):
            raise ValueError(
                f"accepted record lacks Crossref response evidence at index {dataset_index}"
            )
        clean_record = {
            key: value
            for key, value in record_with_evidence.items()
            if key != "_crossref_acquisition_evidence"
        }
        request_sha = str(evidence.get("request_url_sha256") or "")
        request_descriptor, descriptor_url_sha = _validate_crossref_request_descriptor(
            evidence.get("request_descriptor"),
            expected_mailto=mailto,
            window=window,
            expected_bulk_rows=bulk_rows,
            expected_journal_rows=rows_per_journal,
        )
        if descriptor_url_sha != request_sha:
            raise ValueError(
                f"Crossref request descriptor hash mismatch at dataset index {dataset_index}"
            )
        relative_name = str(evidence.get("cache_relative_path") or "")
        if (
            not re.fullmatch(r"[0-9a-f]{64}\.json", relative_name)
            or relative_name != f"{request_sha}.json"
        ):
            raise ValueError(
                f"invalid Crossref cache evidence path at dataset index {dataset_index}"
            )
        if relative_name not in cache_objects:
            cache_path = cache_dir / relative_name
            raw, info = _secure_read_regular_file(
                cache_path, max_bytes=MAX_CROSSREF_RESPONSE_BYTES
            )
            payload = _load_json_object_bytes(raw, source=cache_path)
            cache_objects[relative_name] = raw, payload, info
            cache_request_descriptors[relative_name] = request_descriptor
        elif cache_request_descriptors[relative_name] != request_descriptor:
            raise ValueError(
                f"Crossref cache leaf has conflicting request descriptors: {relative_name}"
            )
        raw, payload, _cache_stat = cache_objects[relative_name]
        response_sha = hashlib.sha256(raw).hexdigest()
        if (
            response_sha != evidence.get("response_sha256")
            or len(raw) != evidence.get("response_bytes")
        ):
            raise ValueError(
                f"Crossref response cache hash/size mismatch at dataset index {dataset_index}"
            )
        message = payload.get("message")
        items = message.get("items") if isinstance(message, Mapping) else None
        item_index = evidence.get("item_index")
        if (
            not isinstance(items, list)
            or not isinstance(item_index, int)
            or item_index < 0
            or item_index >= len(items)
            or not isinstance(items[item_index], dict)
        ):
            raise ValueError(
                f"Crossref item position mismatch at dataset index {dataset_index}"
            )
        item = items[item_index]
        canonical_item_sha256 = hashlib.sha256(
            _canonical_json_bytes(item)
        ).hexdigest()
        if canonical_item_sha256 != evidence.get("canonical_item_sha256"):
            raise ValueError(
                f"Crossref item hash mismatch at dataset index {dataset_index}"
            )
        observed_via = evidence.get("observed_via")
        if observed_via not in {"network", "cache"}:
            raise ValueError(
                f"invalid Crossref observation mode at dataset index {dataset_index}"
            )
        expected_venue = venue_by_id.get(str(clean_record.get("gold_journal_id") or ""))
        if expected_venue is None:
            raise ValueError(
                f"accepted record references an unknown venue at index {dataset_index}"
            )
        rebuilt, status = prepare_crossref_record(
            item,
            issn_index=issn_index,
            expected_venue=expected_venue,
            window=window,
            min_abstract_chars=min_abstract_chars,
        )
        if status != "ok" or rebuilt != clean_record:
            raise ValueError(
                "Crossref cache replay did not reconstruct the accepted dataset row "
                f"at index {dataset_index} (status={status})"
            )
        clean_records.append(clean_record)
        provenance_rows.append(
            {
                "schema_version": 1,
                "artifact_type": "crossref_accepted_record_provenance",
                "dataset_index": dataset_index,
                "paper_id": clean_record["paper_id"],
                "request_url_sha256": request_sha,
                "request_descriptor_sha256": hashlib.sha256(
                    _canonical_json_bytes(request_descriptor)
                ).hexdigest(),
                "cache_relative_path": "raw_cache/" + relative_name,
                "response_sha256": response_sha,
                "response_bytes": len(raw),
                "item_index": item_index,
                "canonical_item_sha256": canonical_item_sha256,
                "observed_via": observed_via,
                "prepared_record_sha256": hashlib.sha256(
                    _canonical_json_bytes(clean_record)
                ).hexdigest(),
                "ledger_sequences": ledger_sequences.get(request_sha, []),
            }
        )

    cache_leaves: list[dict[str, Any]] = []
    for relative_name in sorted(cache_objects):
        raw, _payload, info = cache_objects[relative_name]
        request_sha = relative_name.removesuffix(".json")
        cache_leaves.append(
            {
                "schema_version": 1,
                "artifact_type": "crossref_response_cache_leaf",
                "cache_relative_path": "raw_cache/" + relative_name,
                "request_url_sha256": request_sha,
                "request_descriptor": cache_request_descriptors[relative_name],
                "request_descriptor_sha256": hashlib.sha256(
                    _canonical_json_bytes(cache_request_descriptors[relative_name])
                ).hexdigest(),
                "response_sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
                "mode": f"{stat.S_IMODE(info.st_mode):04o}",
                "ledger_sequences": ledger_sequences.get(request_sha, []),
            }
        )

    cache_private = stat.S_IMODE(cache_info.st_mode) == 0o700
    leaves_private_immutable = bool(cache_leaves) and all(
        leaf["mode"] == "0400" for leaf in cache_leaves
    )
    all_leaves_ledger_bound = bool(cache_leaves) and all(
        leaf["ledger_sequences"] for leaf in cache_leaves
    )
    usage_anchors_bound = bool(
        ledger_binding
        and isinstance(ledger_binding.get("highwater"), Mapping)
        and isinstance(ledger_binding.get("global_usage"), Mapping)
        and ledger_binding["highwater"].get("sha256")
        == ledger_binding.get("sha256")
        and ledger_binding["global_usage"].get("sha256")
        == ledger_binding.get("sha256")
        and ledger_binding["highwater"].get("attempt_records")
        == ledger_binding.get("attempt_records")
        and ledger_binding["global_usage"].get("attempt_records")
        == ledger_binding.get("attempt_records")
    )
    ledger_private_appendable = bool(
        usage_anchors_bound
        and ledger_binding
        and ledger_binding.get("mode") == "0600"
        and ledger_binding["highwater"].get("mode") == "0600"
        and ledger_binding["global_usage"].get("mode") == "0600"
    )
    budget_ceiling_bound = bool(
        ledger_binding
        and ledger_binding.get("hard_http_attempt_ceiling")
        == hard_http_attempt_ceiling
        and ledger_binding.get("budget_binding")
        and ledger_binding.get("global_budget_claim")
        and usage_anchors_bound
    )
    complete = bool(clean_records) and bool(ledger_binding) and all(
        (
            cache_private,
            leaves_private_immutable,
            all_leaves_ledger_bound,
            ledger_private_appendable,
            budget_ceiling_bound,
        )
    )
    tree_manifest = {
        "schema_version": 1,
        "artifact_type": "crossref_acquisition_evidence_tree",
        "cache_root": "raw_cache",
        "_live_cache_root": str(cache_dir.resolve()),
        "cache_root_mode": f"{stat.S_IMODE(cache_info.st_mode):04o}",
        "leaf_count": len(cache_leaves),
        "merkle_sha256": _merkle_root(cache_leaves),
        "accepted_record_count": len(clean_records),
        "provenance_replay_verified": len(clean_records),
        "ledger": ledger_binding,
        "builder_source": {
            "path": "scripts/build_recent_journal_benchmark.py",
            "sha256": _sha256_file(Path(__file__).resolve()),
        },
        "redirect_policy": "fail_closed_no_redirect_hops",
        "every_used_response_bound_to_reservation": all_leaves_ledger_bound,
        "ledger_private_appendable": ledger_private_appendable,
        "reservation_usage_anchors_bound": usage_anchors_bound,
        "budget_ceiling_bound": budget_ceiling_bound,
        "complete": complete,
        "assurance_scope": (
            "locally replayable accepted-row provenance and used successful Crossref "
            "responses with pre-socket reservation prefixes under operator discipline; "
            "excludes unselected/failed response completeness and is not cryptographic "
            "attestation by Crossref"
        ),
    }
    if require_complete and not complete:
        reasons = []
        if not clean_records:
            reasons.append("no accepted records")
        if ledger_binding is None:
            reasons.append("missing request ledger")
        elif not ledger_private_appendable:
            reasons.append("request ledger is not mode 0600")
        if not budget_ceiling_bound:
            reasons.append("hard HTTP-attempt ceiling lacks immutable local/global bindings")
        if not all_leaves_ledger_bound:
            reasons.append("one or more used responses lack a ledger reservation")
        if not cache_private:
            reasons.append("cache directory is not mode 0700")
        if not leaves_private_immutable:
            reasons.append("used cache leaves are not mode 0400")
        raise ValueError(
            "formal Crossref acquisition evidence is incomplete: " + "; ".join(reasons)
        )
    return clean_records, provenance_rows, cache_leaves, tree_manifest


def select_bulk_records(
    venue_records: Mapping[str, Sequence[dict[str, Any]]],
    *,
    limit: int,
    max_papers_per_journal: int,
    seed: str,
    stratum: tuple[str, str],
) -> list[dict[str, Any]]:
    """Deterministically sample a stratum while enforcing the per-journal cap."""

    selected: list[dict[str, Any]] = []
    venue_ids = sorted(
        venue_records,
        key=lambda venue_id: (
            _stable_digest(seed, stratum[0], stratum[1], venue_id),
            venue_id,
        ),
    )
    for venue_id in venue_ids:
        selected.extend(
            select_records_for_journal(
                venue_records[venue_id],
                limit=min(max_papers_per_journal, limit - len(selected)),
                seed=seed,
                venue_id=venue_id,
            )
        )
        if len(selected) >= limit:
            break
    return selected


def default_date_window(today: date | None = None) -> BuildWindow:
    today = today or date.today()
    until = today - timedelta(days=90)
    return BuildWindow(from_date=until - timedelta(days=365), until_date=until)


def _parse_date(value: str, option: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{option} must be YYYY-MM-DD, got {value!r}") from error


def resolve_window(from_date: str | None, until_date: str | None) -> BuildWindow:
    fallback = default_date_window()
    start = _parse_date(from_date, "--from-date") if from_date else fallback.from_date
    end = _parse_date(until_date, "--until-date") if until_date else fallback.until_date
    if start > end:
        raise ValueError("--from-date must not be later than --until-date")
    return BuildWindow(start, end)


def _csv_set(value: str, allowed: Iterable[str], option: str) -> set[str]:
    selected = {normalize_space(item) for item in value.split(",") if normalize_space(item)}
    unknown = selected - set(allowed)
    if unknown:
        raise ValueError(f"{option} contains unsupported values: {', '.join(sorted(unknown))}")
    if not selected:
        raise ValueError(f"{option} must not be empty")
    return selected


def _crossref_request_url(
    path: str, params: Mapping[str, str], *, mailto: str
) -> str:
    return _crossref_url_from_descriptor(
        _crossref_request_descriptor(path, params, mailto=mailto)
    )


def _crossref_request_descriptor(
    path: str, params: Mapping[str, str], *, mailto: str
) -> dict[str, Any]:
    query = {str(key): str(value) for key, value in params.items()}
    query["mailto"] = mailto
    return {
        "schema_version": 1,
        "base_url": CROSSREF_API,
        "path": path,
        "query": dict(sorted(query.items())),
    }


def _crossref_url_from_descriptor(descriptor: Mapping[str, Any]) -> str:
    query = descriptor.get("query")
    if not isinstance(query, Mapping):
        raise ValueError("Crossref request descriptor has no query mapping")
    return (
        str(descriptor.get("base_url") or "")
        + str(descriptor.get("path") or "")
        + "?"
        + urllib.parse.urlencode(sorted((str(key), str(value)) for key, value in query.items()))
    )


def _crossref_filters(window: BuildWindow) -> str:
    return ",".join(
        (
            f"from-pub-date:{window.from_date.isoformat()}",
            f"until-pub-date:{window.until_date.isoformat()}",
            "type:journal-article",
            "has-abstract:true",
        )
    )


def plan_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    """Return a zero-network request/cache/cost bound for a benchmark build."""

    window = resolve_window(args.from_date, args.until_date)
    fields = _csv_set(args.fields, BROAD_FIELDS, "--fields")
    quartiles = _csv_set(args.quartiles.upper(), QUARTILES, "--quartiles")
    venues, catalog_ambiguous = load_jcr_venues(args.data_dir)
    strata = stratified_journal_order(
        venues,
        fields=fields,
        quartiles=quartiles,
        seed=args.seed,
    )
    expected_strata = [
        (field, quartile)
        for field in sorted(fields)
        for quartile in QUARTILES
        if quartile in quartiles
    ]
    missing_strata = [key for key in expected_strata if not strata.get(key)]
    if missing_strata:
        raise ValueError(
            "internal JCR catalog has no journals for strata: "
            + ", ".join(f"{field}/{quartile}" for field, quartile in missing_strata)
        )
    targets = allocate_stratum_targets(
        expected_strata,
        sample_size=args.sample_size,
        samples_per_stratum=args.samples_per_stratum,
    )
    cache_dir = args.cache_dir or (args.output_dir / "crossref_cache")
    known_urls: list[str] = []
    known_urls.append(
        _crossref_request_url(
            "/works",
            {
                "filter": _crossref_filters(window),
                "rows": str(args.bulk_rows),
                "cursor": "*",
            },
            mailto=args.mailto,
        )
    )
    fallback_request_cap = 0
    fallback_by_stratum: dict[str, int] = {}
    for field, quartile in expected_strata:
        target = targets[(field, quartile)]
        needed_journals = (
            target + args.max_papers_per_journal - 1
        ) // args.max_papers_per_journal
        attempt_cap = max(
            needed_journals,
            needed_journals * args.journal_attempt_multiplier,
        )
        selected = strata[(field, quartile)][:attempt_cap]
        fallback_by_stratum[f"{field}/{quartile}"] = len(selected)
        fallback_request_cap += len(selected)
        for venue in selected:
            known_urls.append(
                _crossref_request_url(
                    "/journals/"
                    + urllib.parse.quote(venue.lookup_issn, safe="")
                    + "/works",
                    {
                        "filter": _crossref_filters(window),
                        "rows": str(args.rows_per_journal),
                        "sort": "published",
                        "order": "desc",
                    },
                    mailto=args.mailto,
                )
            )
    response_cache_paths = [
        cache_dir / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}.json"
        for url in known_urls
    ]
    permanent_error_cache_paths = [
        path.with_suffix(".error.json") for path in response_cache_paths
    ]
    known_response_cache_hits = sum(path.is_file() for path in response_cache_paths)
    known_permanent_error_cache_hits = sum(
        path.is_file() for path in permanent_error_cache_paths
    )
    known_cache_hits = sum(
        response_path.is_file() or error_path.is_file()
        for response_path, error_path in zip(
            response_cache_paths, permanent_error_cache_paths, strict=True
        )
    )
    ledger_rows = _read_request_ledger(
        args.request_ledger,
        budget_id=args.request_budget_id,
    )
    attempts_used = len(ledger_rows)
    if attempts_used > args.max_network_requests:
        raise ValueError(
            "Crossref request ledger already exceeds its budget: "
            f"{attempts_used}/{args.max_network_requests}"
        )
    maximum_logical_requests = args.bulk_pages + fallback_request_cap
    uncapped_http_attempts = maximum_logical_requests * (args.retries + 1)
    budget_binding_path = (
        _ledger_budget_binding_path(args.request_ledger)
        if args.request_ledger is not None
        else None
    )
    highwater_path = (
        _ledger_highwater_path(args.request_ledger)
        if args.request_ledger is not None
        else None
    )
    registry_claim_path = (
        _budget_registry_claim_path(
            args.request_budget_registry_dir, args.request_budget_id
        )
        if args.request_ledger is not None
        else None
    )
    registry_usage_path = (
        _budget_registry_usage_path(
            args.request_budget_registry_dir, args.request_budget_id
        )
        if args.request_ledger is not None
        else None
    )
    highwater_rows = (
        _read_request_ledger(highwater_path, budget_id=args.request_budget_id)
        if highwater_path is not None and highwater_path.exists()
        else []
    )
    global_usage_rows = (
        _read_request_ledger(registry_usage_path, budget_id=args.request_budget_id)
        if registry_usage_path is not None and registry_usage_path.exists()
        else []
    )
    if (
        (highwater_path is not None and highwater_path.exists() and highwater_rows != ledger_rows)
        or (
            registry_usage_path is not None
            and registry_usage_path.exists()
            and global_usage_rows != ledger_rows
        )
    ):
        raise ValueError("Crossref request ledger diverged from its high-water anchor")
    expected_budget_binding = (
        _budget_binding_payload(
            budget_id=args.request_budget_id,
            hard_ceiling=args.max_network_requests,
            ledger=args.request_ledger,
            highwater=highwater_path,
        )
        if args.request_ledger is not None
        and args.request_ledger.is_file()
        and highwater_path is not None
        and highwater_path.is_file()
        else None
    )
    if budget_binding_path is not None and budget_binding_path.exists():
        if expected_budget_binding is None:
            raise ValueError(
                "Crossref request ledger is missing behind its budget binding"
            )
        _read_and_validate_budget_binding(
            budget_binding_path, expected=expected_budget_binding
        )
    if registry_claim_path is not None and registry_claim_path.exists():
        if expected_budget_binding is None:
            raise ValueError(
                "Crossref request ledger is missing behind its global budget claim"
            )
        if registry_usage_path is None or not registry_usage_path.is_file():
            raise ValueError("Crossref global usage anchor is missing behind its claim")
        expected_claim = _global_budget_claim_payload(
            expected_budget_binding, usage_path=registry_usage_path
        )
        _read_and_validate_budget_binding(registry_claim_path, expected=expected_claim)
    formal_budget_preflight_ready = bool(
        args.request_ledger is not None
        and (
            not ledger_rows
            or (
                budget_binding_path is not None
                and budget_binding_path.exists()
                and highwater_path is not None
                and highwater_path.exists()
                and registry_claim_path is not None
                and registry_claim_path.exists()
                and registry_usage_path is not None
                and registry_usage_path.exists()
            )
        )
    )
    return {
        "schema_version": 1,
        "artifact_type": "crossref_benchmark_zero_network_plan",
        "network_performed": False,
        "window": {
            "from": window.from_date.isoformat(),
            "until": window.until_date.isoformat(),
        },
        "selection": {
            "sample_size": args.sample_size,
            "targeted_strata": sum(value > 0 for value in targets.values()),
            "target_records": sum(targets.values()),
            "eligible_journals": len(venues),
            "ambiguous_issn_count": len(catalog_ambiguous),
        },
        "request_bound": {
            "bulk_logical_requests": args.bulk_pages,
            "fallback_logical_requests": fallback_request_cap,
            "fallback_by_stratum": fallback_by_stratum,
            "maximum_logical_requests": maximum_logical_requests,
            "maximum_http_attempts_without_budget_cap": uncapped_http_attempts,
            "configured_http_attempt_cap": args.max_network_requests,
            "cumulative_http_attempts_already_used": attempts_used,
            "cumulative_http_attempts_remaining": (
                args.max_network_requests - attempts_used
            ),
            "retries_per_logical_request": args.retries,
            "unknown_cursor_urls_after_first_page": max(0, args.bulk_pages - 1),
        },
        "request_ledger": {
            "path": (
                str(args.request_ledger.resolve())
                if args.request_ledger is not None
                else None
            ),
            "budget_id": args.request_budget_id or None,
            "exists": bool(
                args.request_ledger is not None and args.request_ledger.is_file()
            ),
            "attempt_records": attempts_used,
            "append_only": args.request_ledger is not None,
            "sha256": (
                _sha256_file(args.request_ledger)
                if args.request_ledger is not None and args.request_ledger.is_file()
                else None
            ),
            "hard_http_attempt_ceiling": args.max_network_requests,
            "budget_binding_path": (
                str(budget_binding_path.resolve())
                if budget_binding_path is not None
                else None
            ),
            "budget_binding_exists": bool(
                budget_binding_path is not None and budget_binding_path.is_file()
            ),
            "global_budget_claim_path": (
                str(registry_claim_path.resolve())
                if registry_claim_path is not None
                else None
            ),
            "global_budget_claim_exists": bool(
                registry_claim_path is not None and registry_claim_path.is_file()
            ),
            "formal_budget_preflight_ready": formal_budget_preflight_ready,
        },
        "cache": {
            "path": str(cache_dir.resolve()),
            "directory_exists": cache_dir.is_dir(),
            "existing_response_files": (
                sum(
                    1
                    for path in cache_dir.glob("*.json")
                    if path.is_file() and not path.name.endswith(".error.json")
                )
                if cache_dir.is_dir()
                else 0
            ),
            "existing_permanent_error_files": (
                sum(
                    1
                    for path in cache_dir.glob("*.error.json")
                    if path.is_file()
                )
                if cache_dir.is_dir()
                else 0
            ),
            "known_request_url_count": len(known_urls),
            "known_cache_hit_count": known_cache_hits,
            "known_response_cache_hit_count": known_response_cache_hits,
            "known_permanent_error_cache_hit_count": (
                known_permanent_error_cache_hits
            ),
            "known_cache_coverage": known_cache_hits / len(known_urls),
            "note": (
                "Only the first global cursor URL is knowable without reading a "
                "response; later cursor-page cache keys remain deliberately unknown."
            ),
        },
        "external_cost": {
            "estimated_charge_usd": 0.0,
            "basis": (
                "No paid API key, billing account, Search, LLM, or embedding "
                "provider is configured by this Crossref-only builder."
            ),
        },
        "acquisition_evidence": {
            "required": args.require_complete_acquisition_evidence,
            "request_ledger_configured": args.request_ledger is not None,
            "raw_cache_replay_before_publication": True,
            "redirect_policy": "fail_closed_no_redirect_hops",
            "final_artifact_overwrite_allowed": False,
            "planned_schema_version": 1,
        },
        "output": {
            "path": str(args.output_dir.resolve()),
            "already_exists": args.output_dir.exists(),
        },
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(
        (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        for row in rows
    )


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> str:
    return _publish_new_bytes(path, _jsonl_bytes(rows), mode=0o444)


def _write_json(path: Path, payload: Mapping[str, Any]) -> str:
    raw = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return _publish_new_bytes(path, raw, mode=0o444)


def _assert_new_output_targets(output_dir: Path, *, include_evidence: bool) -> None:
    names = ["dataset.jsonl", "manifest.json"]
    if include_evidence:
        names.extend(
            [
                "provenance.jsonl",
                "cache_evidence.jsonl",
                "cache_evidence_manifest.json",
                "raw_cache",
                "request-ledger-prefix.jsonl",
                "request-ledger-highwater-prefix.jsonl",
                "request-ledger-global-prefix.jsonl",
                "request-budget-binding.json",
                "request-budget-global-claim.json",
            ]
        )
    collisions = [str(output_dir / name) for name in names if (output_dir / name).exists()]
    if collisions:
        raise ValueError(
            "refusing to overwrite existing benchmark artifacts: " + ", ".join(collisions)
        )


def _recheck_acquisition_evidence_sources(
    cache_evidence_leaves: Sequence[Mapping[str, Any]],
    tree: Mapping[str, Any],
) -> None:
    """Close the verify/publish gap for cache, ledger, and builder source."""

    cache_root_value = tree.get("_live_cache_root")
    if not isinstance(cache_root_value, str) or not cache_root_value:
        raise ValueError("Crossref evidence tree lacks its cache root")
    cache_root = Path(cache_root_value)
    root_info = cache_root.lstat()
    if cache_root.is_symlink() or not stat.S_ISDIR(root_info.st_mode):
        raise ValueError("Crossref evidence cache root changed before publication")
    if f"{stat.S_IMODE(root_info.st_mode):04o}" != tree.get("cache_root_mode"):
        raise ValueError("Crossref evidence cache root mode changed before publication")
    for leaf in cache_evidence_leaves:
        relative_name = str(leaf.get("cache_relative_path") or "")
        relative = Path(relative_name)
        if (
            relative.parts[:1] != ("raw_cache",)
            or len(relative.parts) != 2
            or not re.fullmatch(r"[0-9a-f]{64}\.json", relative.name)
        ):
            raise ValueError("invalid Crossref cache leaf in evidence bundle")
        path = cache_root / relative.name
        raw, info = _secure_read_regular_file(
            path, max_bytes=MAX_CROSSREF_RESPONSE_BYTES
        )
        if (
            info.st_size != leaf.get("bytes")
            or hashlib.sha256(raw).hexdigest() != leaf.get("response_sha256")
            or f"{stat.S_IMODE(info.st_mode):04o}" != leaf.get("mode")
        ):
            raise ValueError(f"Crossref cache leaf changed before publication: {path}")

    ledger = tree.get("ledger")
    if not isinstance(ledger, Mapping):
        if tree.get("complete"):
            raise ValueError("complete Crossref evidence tree lacks a ledger binding")
    else:
        ledger_path = Path(str(ledger.get("_live_path") or ""))
        ledger_raw, info = _secure_read_regular_file(
            ledger_path, max_bytes=MAX_REQUEST_LEDGER_BYTES, shared_lock=True
        )
        if (
            info.st_size != ledger.get("bytes")
            or hashlib.sha256(ledger_raw).hexdigest() != ledger.get("sha256")
            or f"{stat.S_IMODE(info.st_mode):04o}" != ledger.get("mode")
        ):
            raise ValueError("Crossref request ledger changed before publication")
        for usage_name in ("highwater", "global_usage"):
            usage = ledger.get(usage_name)
            if not isinstance(usage, Mapping):
                if tree.get("complete"):
                    raise ValueError(f"complete Crossref evidence lacks {usage_name}")
                continue
            usage_path = Path(str(usage.get("_live_path") or ""))
            usage_raw, usage_info = _secure_read_regular_file(
                usage_path, max_bytes=MAX_REQUEST_LEDGER_BYTES, shared_lock=True
            )
            if (
                usage_info.st_size != usage.get("bytes")
                or hashlib.sha256(usage_raw).hexdigest() != usage.get("sha256")
                or f"{stat.S_IMODE(usage_info.st_mode):04o}" != usage.get("mode")
                or usage_raw != ledger_raw
            ):
                raise ValueError(
                    f"Crossref {usage_name} changed before publication"
                )
        for binding_name in ("budget_binding", "global_budget_claim"):
            binding = ledger.get(binding_name)
            if not isinstance(binding, Mapping):
                if tree.get("complete"):
                    raise ValueError(
                        f"complete Crossref evidence lacks {binding_name}"
                    )
                continue
            binding_path = Path(str(binding.get("_live_path") or ""))
            binding_raw, binding_info = _secure_read_regular_file(
                binding_path, max_bytes=MAX_BUDGET_BINDING_BYTES
            )
            if (
                binding_info.st_size != binding.get("bytes")
                or hashlib.sha256(binding_raw).hexdigest() != binding.get("sha256")
                or f"{stat.S_IMODE(binding_info.st_mode):04o}"
                != binding.get("mode")
            ):
                raise ValueError(f"Crossref {binding_name} changed before publication")

    builder = tree.get("builder_source")
    if not isinstance(builder, Mapping):
        raise ValueError("Crossref evidence tree lacks the builder source binding")
    if _sha256_file(Path(__file__).resolve()) != builder.get("sha256"):
        raise ValueError("benchmark builder source changed before publication")


def _without_internal_fields(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _without_internal_fields(item)
            for key, item in value.items()
            if not str(key).startswith("_live_")
        }
    if isinstance(value, list):
        return [_without_internal_fields(item) for item in value]
    return value


def _snapshot_acquisition_sources(
    output_dir: Path,
    cache_evidence_leaves: Sequence[Mapping[str, Any]],
    tree: Mapping[str, Any],
) -> dict[str, Any]:
    """Copy the verified ledger prefix and used raw leaves into the new bundle."""

    live_cache_root = Path(str(tree["_live_cache_root"]))
    raw_cache_dir = output_dir / "raw_cache"
    _ensure_private_directory(raw_cache_dir, require_private=True)
    for leaf in cache_evidence_leaves:
        relative = Path(str(leaf["cache_relative_path"]))
        source = live_cache_root / relative.name
        raw, _source_info = _secure_read_regular_file(
            source, max_bytes=MAX_CROSSREF_RESPONSE_BYTES
        )
        if (
            len(raw) != leaf.get("bytes")
            or hashlib.sha256(raw).hexdigest() != leaf.get("response_sha256")
        ):
            raise ValueError(f"Crossref cache leaf changed during snapshot: {source}")
        _publish_new_bytes(output_dir / relative, raw, mode=0o400)

    ledger = tree.get("ledger")
    if isinstance(ledger, Mapping):
        source = Path(str(ledger["_live_path"]))
        raw, _ledger_info = _secure_read_regular_file(
            source, max_bytes=MAX_REQUEST_LEDGER_BYTES, shared_lock=True
        )
        if (
            len(raw) != ledger.get("bytes")
            or hashlib.sha256(raw).hexdigest() != ledger.get("sha256")
        ):
            raise ValueError("Crossref request ledger changed during snapshot")
        _publish_new_bytes(output_dir / str(ledger["path"]), raw, mode=0o444)
        for usage_name in ("highwater", "global_usage"):
            usage = ledger.get(usage_name)
            if not isinstance(usage, Mapping):
                continue
            source = Path(str(usage["_live_path"]))
            usage_raw, _usage_info = _secure_read_regular_file(
                source, max_bytes=MAX_REQUEST_LEDGER_BYTES, shared_lock=True
            )
            if (
                len(usage_raw) != usage.get("bytes")
                or hashlib.sha256(usage_raw).hexdigest() != usage.get("sha256")
                or usage_raw != raw
            ):
                raise ValueError(f"Crossref {usage_name} changed during snapshot")
            _publish_new_bytes(
                output_dir / str(usage["path"]), usage_raw, mode=0o444
            )
        for binding_name in ("budget_binding", "global_budget_claim"):
            binding = ledger.get(binding_name)
            if not isinstance(binding, Mapping):
                continue
            source = Path(str(binding["_live_path"]))
            raw, _binding_info = _secure_read_regular_file(
                source, max_bytes=MAX_BUDGET_BINDING_BYTES
            )
            if (
                len(raw) != binding.get("bytes")
                or hashlib.sha256(raw).hexdigest() != binding.get("sha256")
            ):
                raise ValueError(f"Crossref {binding_name} changed during snapshot")
            _publish_new_bytes(output_dir / str(binding["path"]), raw, mode=0o444)
    persisted = _without_internal_fields(tree)
    persisted_ledger = persisted.get("ledger")
    if isinstance(persisted_ledger, dict):
        persisted_ledger["source_mode"] = persisted_ledger.get("mode")
        persisted_ledger["mode"] = "0444"
        persisted_ledger["immutable_prefix_snapshot"] = True
        for usage_name in ("highwater", "global_usage"):
            usage = persisted_ledger.get(usage_name)
            if isinstance(usage, dict):
                usage["source_mode"] = usage.get("mode")
                usage["mode"] = "0444"
                usage["immutable_prefix_snapshot"] = True
        for binding_name in ("budget_binding", "global_budget_claim"):
            binding = persisted_ledger.get(binding_name)
            if isinstance(binding, dict):
                binding["source_mode"] = binding.get("mode")
                binding["mode"] = "0444"
                binding["immutable_snapshot"] = True
    return persisted


def _finalize_benchmark_outputs(
    output_dir: Path,
    records: Sequence[Mapping[str, Any]],
    manifest: dict[str, Any],
    *,
    allow_incomplete: bool,
    provenance_rows: Sequence[Mapping[str, Any]] | None = None,
    cache_evidence_leaves: Sequence[Mapping[str, Any]] | None = None,
    cache_evidence_tree: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist even an incomplete build before enforcing its publication gate."""

    include_evidence = any(
        value is not None
        for value in (provenance_rows, cache_evidence_leaves, cache_evidence_tree)
    )
    if include_evidence and any(
        value is None
        for value in (provenance_rows, cache_evidence_leaves, cache_evidence_tree)
    ):
        raise ValueError("Crossref evidence outputs must be finalized as one bundle")
    if include_evidence:
        assert cache_evidence_leaves is not None
        assert cache_evidence_tree is not None
        _recheck_acquisition_evidence_sources(
            cache_evidence_leaves, cache_evidence_tree
        )
    _assert_new_output_targets(output_dir, include_evidence=include_evidence)
    _ensure_private_directory(output_dir)
    persisted_evidence_tree: dict[str, Any] | None = None
    if include_evidence:
        assert cache_evidence_leaves is not None
        assert cache_evidence_tree is not None
        persisted_evidence_tree = _snapshot_acquisition_sources(
            output_dir, cache_evidence_leaves, cache_evidence_tree
        )
    dataset_path = output_dir / "dataset.jsonl"
    dataset_sha256 = _write_jsonl(dataset_path, records)
    manifest["dataset"]["sha256"] = dataset_sha256
    if include_evidence:
        assert provenance_rows is not None
        assert cache_evidence_leaves is not None
        assert cache_evidence_tree is not None
        assert persisted_evidence_tree is not None
        provenance_sha256 = _write_jsonl(
            output_dir / "provenance.jsonl", provenance_rows
        )
        cache_leaves_sha256 = _write_jsonl(
            output_dir / "cache_evidence.jsonl", cache_evidence_leaves
        )
        tree = persisted_evidence_tree
        tree["dataset"] = {
            "path": "dataset.jsonl",
            "sha256": dataset_sha256,
            "record_count": len(records),
        }
        tree["provenance"] = {
            "path": "provenance.jsonl",
            "sha256": provenance_sha256,
            "record_count": len(provenance_rows),
            "schema_version": 1,
        }
        tree["cache_leaves"] = {
            "path": "cache_evidence.jsonl",
            "sha256": cache_leaves_sha256,
            "record_count": len(cache_evidence_leaves),
            "schema_version": 1,
        }
        tree_sha256 = _write_json(
            output_dir / "cache_evidence_manifest.json", tree
        )
        manifest["acquisition_evidence"] = {
            "schema_version": 1,
            "artifact_type": "crossref_acquisition_evidence_bundle",
            "complete": bool(tree.get("complete")),
            "dataset_record_count": len(records),
            "provenance": tree["provenance"],
            "cache_leaves": tree["cache_leaves"],
            "cache_tree": {
                "path": "cache_evidence_manifest.json",
                "sha256": tree_sha256,
                "schema_version": 1,
                "merkle_sha256": tree["merkle_sha256"],
            },
            "ledger": tree.get("ledger"),
            "builder_source": tree["builder_source"],
            "redirect_policy": tree["redirect_policy"],
            "assurance_scope": tree["assurance_scope"],
        }
    _write_json(output_dir / "manifest.json", manifest)
    if not manifest["dataset"]["complete"] and not allow_incomplete:
        raise ValueError(
            "benchmark is incomplete; increase --journal-attempt-multiplier or "
            "pass --allow-incomplete to keep a partial dataset"
        )
    if not records:
        raise ValueError("benchmark contains no accepted records")
    return manifest


def build_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    window = resolve_window(args.from_date, args.until_date)
    fields = _csv_set(args.fields, BROAD_FIELDS, "--fields")
    quartiles = _csv_set(args.quartiles.upper(), QUARTILES, "--quartiles")
    _assert_new_output_targets(args.output_dir, include_evidence=True)
    _ensure_private_directory(
        args.output_dir,
        require_private=args.require_complete_acquisition_evidence,
    )

    venues, catalog_ambiguous = load_jcr_venues(args.data_dir)
    issn_index = build_issn_index(venues)
    strata = stratified_journal_order(
        venues,
        fields=fields,
        quartiles=quartiles,
        seed=args.seed,
    )
    expected_strata = [(field, quartile) for field in sorted(fields) for quartile in QUARTILES if quartile in quartiles]
    missing_strata = [key for key in expected_strata if not strata.get(key)]
    if missing_strata:
        raise ValueError(
            "internal JCR catalog has no journals for strata: "
            + ", ".join(f"{field}/{quartile}" for field, quartile in missing_strata)
        )

    client = CrossrefClient(
        cache_dir=args.cache_dir or (args.output_dir / "crossref_cache"),
        mailto=args.mailto,
        timeout=args.timeout,
        retries=args.retries,
        request_interval=args.request_interval,
        use_environment_proxy=args.use_environment_proxy,
        refresh_cache=args.refresh_cache,
        max_network_requests=args.max_network_requests,
        request_ledger=args.request_ledger,
        request_budget_id=args.request_budget_id,
        require_private_storage=args.require_complete_acquisition_evidence,
        budget_registry_dir=(
            args.request_budget_registry_dir
            if args.require_complete_acquisition_evidence
            else None
        ),
    )
    rejection_counts: Counter[str] = Counter()
    request_failures: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    global_dois: set[str] = set()
    global_titles: set[str] = set()
    stratum_stats: dict[str, dict[str, Any]] = {}

    stratum_targets = allocate_stratum_targets(
        expected_strata,
        sample_size=args.sample_size,
        samples_per_stratum=args.samples_per_stratum,
    )
    bulk_records: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {
        key: defaultdict(list) for key in expected_strata
    }
    bulk_pages_scanned = 0
    bulk_items_scanned = 0
    cursor = "*"
    for _page_number in range(args.bulk_pages):
        item_evidence_rows, next_cursor = client.works_page_with_evidence(
            window=window,
            rows=args.bulk_rows,
            cursor=cursor,
        )
        bulk_pages_scanned += 1
        bulk_items_scanned += len(item_evidence_rows)
        for item_evidence in item_evidence_rows:
            item = item_evidence.item
            venue, resolution = resolve_item_venue(item, issn_index)
            if venue is None:
                rejection_counts["bulk_" + resolution] += 1
                continue
            stratum = (venue.broad_field, venue.quartile)
            if stratum not in bulk_records:
                continue
            record, status = prepare_crossref_record(
                item,
                issn_index=issn_index,
                expected_venue=venue,
                window=window,
                min_abstract_chars=args.min_abstract_chars,
            )
            if record is None:
                rejection_counts[status] += 1
            else:
                bulk_records[stratum][venue.venue_id].append(
                    _record_with_item_evidence(record, item_evidence)
                )
        if not next_cursor or next_cursor == cursor or not item_evidence_rows:
            break
        cursor = next_cursor

    for field, quartile in expected_strata:
        key = f"{field}/{quartile}"
        stratum_target = stratum_targets[(field, quartile)]
        bulk_selected = select_bulk_records(
            bulk_records[(field, quartile)],
            limit=stratum_target,
            max_papers_per_journal=args.max_papers_per_journal,
            seed=args.seed,
            stratum=(field, quartile),
        )
        accepted_for_stratum = 0
        attempted_journals = 0
        successful_journals = 0
        successful_venue_ids: set[str] = set()
        for record in bulk_selected:
            doi = str(record["doi"])
            title_key = normalize_name(str(record["title"]))
            if doi in global_dois or title_key in global_titles:
                rejection_counts["bulk_duplicate"] += 1
                continue
            global_dois.add(doi)
            global_titles.add(title_key)
            records.append(record)
            accepted_for_stratum += 1
            successful_venue_ids.add(str(record["gold_journal_id"]))
        successful_journals = len(successful_venue_ids)
        needed_journals = (
            max(0, stratum_target - accepted_for_stratum)
            + args.max_papers_per_journal
            - 1
        ) // args.max_papers_per_journal
        attempt_cap = max(
            needed_journals,
            needed_journals * args.journal_attempt_multiplier,
        )
        fallback_venues = [
            venue
            for venue in strata[(field, quartile)][:attempt_cap]
            if venue.venue_id not in successful_venue_ids
        ]

        def fetch_fallback(
            venue: JournalVenue,
        ) -> tuple[
            JournalVenue,
            list[CrossrefItemEvidence],
            urllib.error.HTTPError | None,
        ]:
            try:
                return venue, client.journal_works_with_evidence(
                    venue.lookup_issn,
                    window=window,
                    rows=args.rows_per_journal,
                ), None
            except urllib.error.HTTPError as error:
                return venue, [], error

        for offset in range(0, len(fallback_venues), args.journal_workers):
            if accepted_for_stratum >= stratum_target:
                break
            batch = fallback_venues[offset : offset + args.journal_workers]
            with ThreadPoolExecutor(max_workers=args.journal_workers) as executor:
                fallback_results = list(executor.map(fetch_fallback, batch))
            for venue, item_evidence_rows, http_error in fallback_results:
                if accepted_for_stratum >= stratum_target:
                    break
                attempted_journals += 1
                if http_error is not None:
                    if http_error.code != 404:
                        raise ValueError(
                            f"Crossref request failed for {venue.lookup_issn}: {http_error}"
                        ) from http_error
                    rejection_counts["crossref_journal_not_found"] += 1
                    request_failures.append(
                        {
                            "issn": venue.lookup_issn,
                            "status": 404,
                            "reason": "journal endpoint not found",
                        }
                    )
                    print(f"warning: {venue.lookup_issn}: {http_error}", file=sys.stderr)
                    continue
                if not item_evidence_rows:
                    rejection_counts["no_recent_abstract_items"] += 1
                accepted: list[dict[str, Any]] = []
                for item_evidence in item_evidence_rows:
                    item = item_evidence.item
                    record, status = prepare_crossref_record(
                        item,
                        issn_index=issn_index,
                        expected_venue=venue,
                        window=window,
                        min_abstract_chars=args.min_abstract_chars,
                    )
                    if record is None:
                        rejection_counts[status] += 1
                    else:
                        accepted.append(
                            _record_with_item_evidence(record, item_evidence)
                        )
                remaining = stratum_target - accepted_for_stratum
                chosen = select_records_for_journal(
                    accepted,
                    limit=min(args.max_papers_per_journal, remaining),
                    seed=args.seed,
                    venue_id=venue.venue_id,
                )
                added = 0
                for record in chosen:
                    doi = str(record["doi"])
                    title_key = normalize_name(str(record["title"]))
                    if doi in global_dois:
                        rejection_counts["duplicate_doi"] += 1
                        continue
                    if title_key in global_titles:
                        rejection_counts["duplicate_title"] += 1
                        continue
                    global_dois.add(doi)
                    global_titles.add(title_key)
                    records.append(record)
                    accepted_for_stratum += 1
                    added += 1
                if added:
                    successful_journals += 1
                    successful_venue_ids.add(venue.venue_id)
        stratum_stats[key] = {
            "target": stratum_target,
            "accepted": accepted_for_stratum,
            "attempted_journals": attempted_journals,
            "successful_journals": successful_journals,
            "catalog_journals": len(strata[(field, quartile)]),
            "complete": accepted_for_stratum == stratum_target,
        }
        print(
            f"{key}: {accepted_for_stratum}/{stratum_target} "
            f"papers from {successful_journals}/{attempted_journals} journals",
            file=sys.stderr,
        )

    records.sort(
        key=lambda record: (
            record["broad_field"],
            QUARTILE_ORDER[record["gold_jcr_quartile"]],
            record["gold_journal_id"],
            record["publication_date"],
            record["doi"],
        )
    )
    (
        records,
        provenance_rows,
        cache_evidence_leaves,
        cache_evidence_tree,
    ) = _verify_acquisition_evidence(
        records,
        cache_dir=client.cache_dir,
        venues=venues,
        issn_index=issn_index,
        window=window,
        min_abstract_chars=args.min_abstract_chars,
        request_ledger=args.request_ledger,
        request_budget_id=args.request_budget_id,
        hard_http_attempt_ceiling=args.max_network_requests,
        budget_binding_path=client.budget_binding_path,
        budget_registry_claim_path=client.budget_registry_claim_path,
        request_highwater_path=client.request_highwater_path,
        global_usage_path=client.global_usage_path,
        mailto=args.mailto,
        bulk_rows=args.bulk_rows,
        rows_per_journal=args.rows_per_journal,
        require_complete=args.require_complete_acquisition_evidence,
    )
    dataset_path = args.output_dir / "dataset.jsonl"
    catalog_files = [*DATA_FILES, CURATED_SCOPE_FILE]
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "builder": "scripts/build_recent_journal_benchmark.py",
        "builder_source": {
            "path": "scripts/build_recent_journal_benchmark.py",
            "sha256": _sha256_file(Path(__file__).resolve()),
        },
            "source": {
            "name": "Crossref REST API",
            "base_url": CROSSREF_API,
            "endpoint_pattern": "/journals/{ISSN}/works",
            "filters": {
                "from_pub_date": window.from_date.isoformat(),
                "until_pub_date": window.until_date.isoformat(),
                "type": "journal-article",
                "has_abstract": True,
            },
            "selection": (
                "seeded per-journal sampling from a recent global cursor stream, "
                "with deterministic journal-endpoint fallback for underfilled strata; "
                "not a uniform sample of all papers in the date window"
            ),
            "network_requests": client.network_requests,
            "network_requests_this_run": client.network_requests,
            "network_requests_cumulative": client.cumulative_network_requests,
            "network_request_budget_remaining": (
                args.max_network_requests - client.cumulative_network_requests
            ),
            "network_request_budget": args.max_network_requests,
            "network_request_ledger": (
                {
                    "path": cache_evidence_tree["ledger"]["path"],
                    "sha256": cache_evidence_tree["ledger"]["sha256"],
                    "attempt_records": cache_evidence_tree["ledger"][
                        "attempt_records"
                    ],
                    "budget_id": cache_evidence_tree["ledger"]["budget_id"],
                    "append_only": True,
                }
                if cache_evidence_tree["ledger"] is not None
                else None
            ),
            "cache_dir": "raw_cache",
            "cache_is_run_local_snapshot": True,
            "cache_hits": client.cache_hits,
            "response_cache_hits": client.response_cache_hits,
            "permanent_error_cache_hits": client.permanent_error_cache_hits,
            "permanent_request_failures": request_failures,
            "bulk_scan": {
                "endpoint": "/works",
                "pages": bulk_pages_scanned,
                "items": bulk_items_scanned,
                "rows_per_page": args.bulk_rows,
                "requested_pages": args.bulk_pages,
            },
        },
        "internal_catalog": {
            "data_dir": str(args.data_dir.resolve()),
            "source_files_sha256": {
                name: _sha256_file(args.data_dir / name)
                for name in catalog_files
            },
            "eligible_q1_q4_journals": len(venues),
            "ambiguous_issns_rejected": sorted(_format_issn(token) for token in catalog_ambiguous),
        },
        "configuration": {
            "fields": sorted(fields),
            "quartiles": sorted(quartiles, key=QUARTILE_ORDER.get),
            "sample_size": args.sample_size,
            "samples_per_stratum": args.samples_per_stratum,
            "max_papers_per_journal": args.max_papers_per_journal,
            "journal_attempt_multiplier": args.journal_attempt_multiplier,
            "journal_workers": args.journal_workers,
            "rows_per_journal": args.rows_per_journal,
            "bulk_rows": args.bulk_rows,
            "bulk_pages": args.bulk_pages,
            "min_abstract_chars": args.min_abstract_chars,
            "seed": args.seed,
            "environment_proxy_used": args.use_environment_proxy,
            "max_network_requests": args.max_network_requests,
            "request_budget_id": args.request_budget_id or None,
            "require_complete_acquisition_evidence": (
                args.require_complete_acquisition_evidence
            ),
            "mailto": args.mailto,
        },
        "dataset": {
            "path": dataset_path.name,
            "format": "JSON Lines",
            "record_count": len(records),
            "sha256": "pending",
            "label_fields": [
                "gold_journal_id",
                "gold_entity_id",
                "gold_journal_name",
                "gold_issns",
                "gold_jcr_quartile",
                "gold_jcr_category",
            ],
            "model_input_fields": ["title", "abstract"],
            "complete": all(stat["complete"] for stat in stratum_stats.values()),
        },
        "strata": stratum_stats,
        "coverage": {
            "target_records": sum(stat["target"] for stat in stratum_stats.values()),
            "accepted_records": len(records),
            "record_completion_rate": (
                len(records) / sum(stat["target"] for stat in stratum_stats.values())
                if sum(stat["target"] for stat in stratum_stats.values())
                else 1.0
            ),
            "targeted_strata": sum(stat["target"] > 0 for stat in stratum_stats.values()),
            "covered_strata": sum(stat["accepted"] > 0 for stat in stratum_stats.values()),
            "complete_strata": sum(
                stat["target"] > 0 and stat["complete"]
                for stat in stratum_stats.values()
            ),
            "attempted_journals": sum(stat["attempted_journals"] for stat in stratum_stats.values()),
            "successful_journals": sum(stat["successful_journals"] for stat in stratum_stats.values()),
        },
        "rejections": dict(sorted(rejection_counts.items())),
        "notice": (
            "Crossref metadata is open, but abstract copyright may remain with its "
            "rightsholder. Check applicable terms before redistributing dataset.jsonl."
        ),
    }
    return _finalize_benchmark_outputs(
        args.output_dir,
        records,
        manifest,
        allow_incomplete=args.allow_incomplete,
        provenance_rows=provenance_rows,
        cache_evidence_leaves=cache_evidence_leaves,
        cache_evidence_tree=cache_evidence_tree,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Crossref response cache; default: OUTPUT_DIR/crossref_cache.",
    )
    parser.add_argument("--from-date", default=None, help="YYYY-MM-DD; default is 455 days ago")
    parser.add_argument("--until-date", default=None, help="YYYY-MM-DD; default is 90 days ago")
    parser.add_argument("--fields", default=",".join(BROAD_FIELDS))
    parser.add_argument("--quartiles", default=",".join(QUARTILES))
    parser.add_argument(
        "--sample-size",
        type=int,
        default=DEFAULT_SAMPLE_SIZE,
        help="Total records, balanced across selected strata; default: 500.",
    )
    parser.add_argument("--samples-per-stratum", type=int, default=10)
    parser.add_argument("--max-papers-per-journal", type=int, default=1)
    parser.add_argument("--journal-attempt-multiplier", type=int, default=12)
    parser.add_argument("--journal-workers", type=int, default=8)
    parser.add_argument("--bulk-pages", type=int, default=8)
    parser.add_argument("--bulk-rows", type=int, default=1000)
    parser.add_argument("--rows-per-journal", type=int, default=20)
    parser.add_argument("--min-abstract-chars", type=int, default=300)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument("--mailto", default=os.getenv("CROSSREF_MAILTO", DEFAULT_CONTACT))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--request-interval", type=float, default=0.12)
    parser.add_argument(
        "--max-network-requests",
        type=int,
        default=1000,
        help=(
            "Hard cap on HTTP attempts, including retries; the build fails closed "
            "before attempt 1001 by default."
        ),
    )
    parser.add_argument(
        "--request-ledger",
        type=Path,
        default=None,
        help=(
            "Optional append-only JSONL ledger that makes the HTTP-attempt cap "
            "cumulative across failed or resumed builds."
        ),
    )
    parser.add_argument(
        "--request-budget-id",
        default="",
        help="Stable identifier required when --request-ledger is used.",
    )
    parser.add_argument(
        "--request-budget-registry-dir",
        type=Path,
        default=DEFAULT_BUDGET_REGISTRY_DIR,
        help=(
            "Ignored global registry used by formal acquisition to bind a budget ID "
            "to one ledger identity and immutable hard ceiling."
        ),
    )
    parser.add_argument(
        "--require-complete-acquisition-evidence",
        action="store_true",
        help=(
            "Fail closed unless every accepted row replays from a private immutable "
            "raw cache leaf that is bound to the append-only request ledger."
        ),
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Print a zero-network cache/request/cost plan and do not create outputs.",
    )
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="Exit successfully with fewer records than requested.",
    )
    parser.add_argument(
        "--use-environment-proxy",
        action="store_true",
        help="Honor HTTP(S)_PROXY. By default the builder bypasses environment proxies.",
    )
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    positive = (
        "samples_per_stratum",
        "max_papers_per_journal",
        "journal_attempt_multiplier",
        "journal_workers",
        "bulk_pages",
        "bulk_rows",
        "rows_per_journal",
        "min_abstract_chars",
    )
    for name in positive:
        if getattr(args, name) <= 0:
            raise ValueError(f"--{name.replace('_', '-')} must be positive")
    if args.sample_size is not None and args.sample_size <= 0:
        raise ValueError("--sample-size must be positive")
    if args.rows_per_journal > 1000 or args.bulk_rows > 1000:
        raise ValueError("Crossref rows values must not exceed the 1000-row limit")
    if args.retries < 0 or args.timeout <= 0 or args.request_interval < 0:
        raise ValueError("retry, timeout, and request interval values are invalid")
    if args.max_network_requests <= 0:
        raise ValueError("--max-network-requests must be positive")
    if args.request_ledger is not None and not args.request_budget_id.strip():
        raise ValueError("--request-ledger requires --request-budget-id")
    if args.request_ledger is not None and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}", args.request_budget_id
    ):
        raise ValueError("--request-budget-id has an unsafe format")
    if args.require_complete_acquisition_evidence and args.request_ledger is None:
        raise ValueError(
            "--require-complete-acquisition-evidence requires --request-ledger"
        )
    if "@" not in args.mailto:
        raise ValueError("--mailto must be a valid contact email address")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _validate_args(args)
        manifest = plan_benchmark(args) if args.plan_only else build_benchmark(args)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
