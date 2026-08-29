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
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import html
import json
import os
import re
import socket
import sys
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
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "benchmark_artifacts" / "recent_journals"
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
        if (
            record.get("schema_version") != 1
            or record.get("event") != "attempt_reserved"
            or record.get("sequence") != expected_sequence
            or record.get("budget_id") != budget_id
            or len(str(record.get("request_url_sha256") or "")) != 64
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
    if not path.is_file():
        raise ValueError(f"Crossref request ledger is not a file: {path}")
    with path.open(encoding="utf-8") as handle:
        return _parse_request_ledger_lines(
            handle,
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
    ) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.mailto = mailto
        self.timeout = timeout
        self.retries = retries
        self.request_interval = request_interval
        self.refresh_cache = refresh_cache
        self.max_network_requests = max_network_requests
        self.request_ledger = request_ledger
        self.request_budget_id = request_budget_id
        if self.request_ledger is not None and not self.request_budget_id:
            raise ValueError("Crossref request ledger requires a budget ID")
        ledger_rows = _read_request_ledger(
            self.request_ledger,
            budget_id=self.request_budget_id,
        )
        if len(ledger_rows) > self.max_network_requests:
            raise ValueError(
                "Crossref request ledger already exceeds its budget: "
                f"{len(ledger_rows)}/{self.max_network_requests}"
            )
        handlers: list[Any] = [] if use_environment_proxy else [urllib.request.ProxyHandler({})]
        self.opener = urllib.request.build_opener(*handlers)
        self.last_request_at = 0.0
        self.network_requests = 0
        self.cumulative_network_requests = len(ledger_rows)
        self.cache_hits = 0
        self._request_lock = threading.Lock()

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}.json"

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

        self.request_ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.request_ledger.open("a+", encoding="utf-8", newline="\n") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                rows = _parse_request_ledger_lines(
                    handle,
                    path=self.request_ledger,
                    budget_id=self.request_budget_id,
                )
                used = len(rows)
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
                    "request_url_sha256": hashlib.sha256(
                        url.encode("utf-8")
                    ).hexdigest(),
                    "recorded_after_fact": False,
                }
                handle.seek(0, os.SEEK_END)
                handle.write(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        sort_keys=True,
                        allow_nan=False,
                    )
                    + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
                self.network_requests += 1
                self.cumulative_network_requests = sequence
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def get_json(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        query = dict(params)
        query["mailto"] = self.mailto
        url = CROSSREF_API + path + "?" + urllib.parse.urlencode(query)
        cache_path = self._cache_path(url)
        if cache_path.exists() and not self.refresh_cache:
            with self._request_lock:
                self.cache_hits += 1
            with cache_path.open(encoding="utf-8") as handle:
                cached = json.load(handle)
            if not isinstance(cached, dict):
                raise ValueError(f"invalid cached Crossref response: {cache_path}")
            return cached

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
                    payload = json.load(response)
                if not isinstance(payload, dict):
                    raise ValueError("Crossref returned a non-object JSON response")
                temporary = cache_path.with_suffix(".tmp")
                with temporary.open("w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                temporary.replace(cache_path)
                return payload
            except urllib.error.HTTPError as error:
                if error.code not in retryable or attempt >= self.retries:
                    raise
                retry_after = error.headers.get("Retry-After") if error.headers else None
                try:
                    delay = min(120.0, max(0.0, float(retry_after)))
                except (TypeError, ValueError):
                    delay = min(30.0, 2.0**attempt)
                time.sleep(delay)
            except (urllib.error.URLError, socket.timeout, TimeoutError):
                if attempt >= self.retries:
                    raise
                time.sleep(min(30.0, 2.0**attempt))
        raise RuntimeError("unreachable Crossref retry state")

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
    query = dict(params)
    query["mailto"] = mailto
    return CROSSREF_API + path + "?" + urllib.parse.urlencode(query)


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
    cache_paths = [
        cache_dir / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}.json"
        for url in known_urls
    ]
    known_cache_hits = sum(path.is_file() for path in cache_paths)
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
        },
        "cache": {
            "path": str(cache_dir.resolve()),
            "directory_exists": cache_dir.is_dir(),
            "existing_json_files": (
                sum(1 for path in cache_dir.glob("*.json") if path.is_file())
                if cache_dir.is_dir()
                else 0
            ),
            "known_request_url_count": len(known_urls),
            "known_cache_hit_count": known_cache_hits,
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


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> str:
    temporary = path.with_suffix(path.suffix + ".tmp")
    digest = hashlib.sha256()
    with temporary.open("wb") as handle:
        for row in rows:
            line = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            handle.write(line)
            digest.update(line)
    temporary.replace(path)
    return digest.hexdigest()


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def build_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    window = resolve_window(args.from_date, args.until_date)
    fields = _csv_set(args.fields, BROAD_FIELDS, "--fields")
    quartiles = _csv_set(args.quartiles.upper(), QUARTILES, "--quartiles")
    args.output_dir.mkdir(parents=True, exist_ok=True)

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
        items, next_cursor = client.works_page(
            window=window,
            rows=args.bulk_rows,
            cursor=cursor,
        )
        bulk_pages_scanned += 1
        bulk_items_scanned += len(items)
        for item in items:
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
                bulk_records[stratum][venue.venue_id].append(record)
        if not next_cursor or next_cursor == cursor or not items:
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

        def fetch_fallback(venue: JournalVenue) -> tuple[JournalVenue, list[dict[str, Any]], urllib.error.HTTPError | None]:
            try:
                return venue, client.journal_works(
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
            for venue, items, http_error in fallback_results:
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
                if not items:
                    rejection_counts["no_recent_abstract_items"] += 1
                accepted: list[dict[str, Any]] = []
                for item in items:
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
                        accepted.append(record)
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
    dataset_path = args.output_dir / "dataset.jsonl"
    catalog_files = [*DATA_FILES, CURATED_SCOPE_FILE]
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "builder": "scripts/build_recent_journal_benchmark.py",
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
                    "path": str(args.request_ledger.resolve()),
                    "sha256": _sha256_file(args.request_ledger),
                    "attempt_records": client.cumulative_network_requests,
                    "budget_id": args.request_budget_id,
                    "append_only": True,
                }
                if args.request_ledger is not None
                else None
            ),
            "cache_dir": str(client.cache_dir.resolve()),
            "cache_hits": client.cache_hits,
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
    if records and not manifest["dataset"]["complete"] and not args.allow_incomplete:
        raise ValueError(
            "benchmark is incomplete; increase --journal-attempt-multiplier or "
            "pass --allow-incomplete to keep a partial dataset"
        )
    if not records:
        raise ValueError("benchmark contains no accepted records")
    dataset_sha256 = _write_jsonl(dataset_path, records)
    manifest["dataset"]["sha256"] = dataset_sha256
    _write_json(args.output_dir / "manifest.json", manifest)
    return manifest


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
