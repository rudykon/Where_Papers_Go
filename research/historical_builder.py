"""Build a frozen, multi-source historical venue corpus.

This module is deliberately separate from :mod:`research.cache_builder`.
The latter turns the recent-paper benchmark cache into evaluation queries;
this module acquires *candidate-side* evidence for every catalog venue.  A
test paper is therefore never needed to decide which venue is collected.

The network-facing classes are small and injectable so unit tests can use
frozen fixtures.  The generated corpus remains an offline input to the
research evaluator.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import csv
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import fcntl
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Iterable, Mapping, Protocol, Sequence
import urllib.error
import urllib.parse
import urllib.request

from scripts.build_recent_journal_benchmark import classify_broad_field
from scripts.enrich_journal_scope_catalog import load_scope_entities
from where_paper_go import enrichment

from .pcl_retry import PCLRetryOutcome, PCLRetryPolicy, PCLRetryQueue

from .cache_builder import (
    NOTICE_RELATIONS,
    NOTICE_TITLE_RE,
    _first_text,
    _item_issns,
    crossref_publication_date,
    jats_to_text,
)
from .data import (
    ResearchDataError,
    _jcr_document_id,
    _valid_issn_token,
    normalize_doi,
    normalize_text,
    parse_iso_date,
    sha256_file,
)


SCHEMA_VERSION = 2
DEFAULT_SEED = "where-papers-go-historical-venues-v1"
DEFAULT_USER_AGENT = "WherePapersGo-HistoricalCorpus/1.0"
TRANSIENT_HTTP_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
PCL_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "GLM-5.2": 1_048_576,
    "DeepSeek-V4-Pro": 409_600,
    "DeepSeek-V4-Flash-0731": 524_288,
    "Qwen3.6-35B": 262_144,
}
# The provider has not supplied a MiniMax-M3 context limit.  Do not represent
# a guessed value as a model capability; use a deliberately small input cap
# until an explicit override is configured.
PCL_DEFAULT_INPUT_TOKEN_CAP = 49_152
PCL_UNKNOWN_MODEL_INPUT_TOKEN_CAP = 32_768


class HistoricalCollectionError(RuntimeError):
    """Raised when a corpus-level invariant cannot be satisfied."""


@dataclass(frozen=True)
class VenueSeed:
    venue_id: str
    name: str
    issns: tuple[str, ...]
    quartile: str
    subject: str
    subject_en: str
    broad_field: str
    publisher: str = ""
    homepage: str = ""
    catalog_scope: str = ""
    official_scope: str = ""
    official_scope_url: str = ""
    official_scope_evidence: str = ""
    official_scope_updated_at: str = ""
    online_entity_id: int | None = None
    identity_status: str = "unmapped"

    def to_queue_row(self, position: int) -> dict[str, Any]:
        return {
            "position": position,
            "venue_id": self.venue_id,
            "name": self.name,
            "issns": list(self.issns),
            "quartile": self.quartile,
            "subject": self.subject,
            "broad_field": self.broad_field,
            "online_entity_id": self.online_entity_id,
            "identity_status": self.identity_status,
        }


@dataclass(frozen=True)
class CollectionPolicy:
    history_start: str
    cutoff: str
    max_papers_per_venue: int = 50
    min_papers_before_fallback: int = 20
    max_pages_per_source: int = 5
    openalex_mode: str = "fallback"
    scope_mode: str = "fallback"
    max_prototypes: int = 8
    max_pcl_evidence: int = 32

    def validate(self) -> None:
        start = parse_iso_date(self.history_start, field_name="history_start")
        cutoff = parse_iso_date(self.cutoff, field_name="cutoff")
        if start > cutoff:
            raise ResearchDataError("history_start must not be later than cutoff")
        if self.max_papers_per_venue < 1:
            raise ResearchDataError("max_papers_per_venue must be positive")
        if not 1 <= self.min_papers_before_fallback <= self.max_papers_per_venue:
            raise ResearchDataError(
                "min_papers_before_fallback must be within max_papers_per_venue"
            )
        if (
            self.max_pages_per_source < 1
            or self.max_prototypes < 1
            or self.max_pcl_evidence < 1
        ):
            raise ResearchDataError(
                "source pages, prototype, and PCL evidence limits must be positive"
            )
        if self.openalex_mode not in {"always", "fallback", "off"}:
            raise ResearchDataError("openalex_mode must be always, fallback, or off")
        if self.scope_mode not in {"always", "fallback", "off"}:
            raise ResearchDataError("scope_mode must be always, fallback, or off")


class EvidenceSource(Protocol):
    name: str

    def fetch(self, venue: VenueSeed, policy: CollectionPolicy) -> list[dict[str, Any]]:
        """Return normalized evidence records for one canonical venue."""


class PrototypeSynthesizer(Protocol):
    model: str
    provider_identity: Mapping[str, Any]

    def synthesize(
        self,
        venue: VenueSeed,
        evidence: Sequence[Mapping[str, Any]],
        policy: CollectionPolicy,
    ) -> tuple[list[dict[str, Any]], str]:
        """Return grounded prototypes and a status string."""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stable_digest(*values: object) -> str:
    payload = "\x1f".join(str(value) for value in values).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _atomic_compact_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    temporary.replace(path)


def _append_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _canonical_issn(value: object) -> str:
    token = _valid_issn_token(value)
    return f"{token[:4]}-{token[4:]}" if token else ""


def _longest(values: Iterable[object]) -> str:
    normalized = {" ".join(str(value or "").split()) for value in values}
    return max(normalized - {""}, key=lambda value: (len(value), value), default="")


def _timestamp_date(value: object) -> str:
    raw = str(value or "").strip()
    match = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    return match.group(1) if match else ""


def load_venue_seeds(jcr_csv: Path, *, data_dir: Path | None = None) -> list[VenueSeed]:
    """Load the exact JCR Q1--Q4 identity universe and an ISSN crosswalk.

    The online graph crosswalk is exact-ISSN-only.  Ambiguous ownership is
    quarantined rather than resolved with a fuzzy journal-name match.
    """

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    try:
        handle = jcr_csv.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise ResearchDataError(f"cannot open JCR catalog: {jcr_csv}") from exc
    with handle:
        for row in csv.DictReader(handle):
            if str(row.get("level") or "").upper() not in {"Q1", "Q2", "Q3", "Q4"}:
                continue
            venue_id = _jcr_document_id(row)
            if venue_id:
                grouped[venue_id].append(dict(row))
    if not grouped:
        raise ResearchDataError(f"JCR catalog has no canonical Q1--Q4 venues: {jcr_csv}")

    entity_by_issn: dict[str, set[int]] = defaultdict(set)
    if data_dir is not None:
        for entity in load_scope_entities(data_dir):
            for value in entity.issns:
                token = _valid_issn_token(value)
                if token:
                    entity_by_issn[token].add(entity.entity_id)

    seeds: list[VenueSeed] = []
    for venue_id, rows in grouped.items():
        tokens = tuple(
            sorted(
                {
                    token
                    for row in rows
                    for token in (
                        _valid_issn_token(row.get("issn")),
                        _valid_issn_token(row.get("eissn")),
                    )
                    if token
                }
            )
        )
        owners = {owner for token in tokens for owner in entity_by_issn.get(token, ())}
        online_entity_id = next(iter(owners)) if len(owners) == 1 else None
        identity_status = (
            "exact_issn" if len(owners) == 1 else "ambiguous" if owners else "unmapped"
        )
        subject = _longest(row.get("area") for row in rows)
        subject_en = _longest(row.get("area_en") for row in rows)
        seeds.append(
            VenueSeed(
                venue_id=venue_id,
                name=_longest(row.get("name") for row in rows),
                issns=tokens,
                quartile=_longest(row.get("level") for row in rows).upper(),
                subject=subject,
                subject_en=subject_en,
                broad_field=classify_broad_field(" ".join((subject, subject_en))),
                publisher=_longest(row.get("publisher") for row in rows),
                homepage=_longest(row.get("url") for row in rows),
                catalog_scope=_longest(row.get("收稿方向") for row in rows),
                official_scope=_longest(row.get("收稿方向_官网摘取") for row in rows),
                official_scope_url=_longest(row.get("收稿方向_来源URL") for row in rows),
                official_scope_evidence=_longest(row.get("收稿方向_证据") for row in rows),
                official_scope_updated_at=max(
                    (str(row.get("收稿方向_更新时间") or "") for row in rows),
                    default="",
                ),
                online_entity_id=online_entity_id,
                identity_status=identity_status,
            )
        )
    seeds.sort(key=lambda venue: venue.venue_id)
    return seeds


class _RateLimiter:
    def __init__(self, interval: float) -> None:
        self.interval = max(0.0, float(interval))
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            delay = self.interval - (time.monotonic() - self._last)
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


class CachedJsonClient:
    """Small cache-first JSON client that never persists authorization data."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        timeout: int = 45,
        retries: int = 3,
        request_interval: float = 0.1,
        user_agent: str = DEFAULT_USER_AGENT,
        use_environment_proxy: bool = False,
    ) -> None:
        self.cache_dir = cache_dir
        self.timeout = max(1, int(timeout))
        self.retries = max(0, int(retries))
        self.user_agent = user_agent
        self.use_environment_proxy = bool(use_environment_proxy)
        self.rate_limiter = _RateLimiter(request_interval)

    def get(
        self,
        source: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        max_bytes: int = 25_000_000,
    ) -> Mapping[str, Any]:
        cache_key = stable_digest(source, url)
        path = self.cache_dir / source / f"{cache_key}.json"
        if path.is_file():
            cached = json.loads(path.read_text(encoding="utf-8"))
            payload = cached.get("payload") if isinstance(cached, Mapping) else None
            if isinstance(payload, Mapping):
                return payload
        safe_headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
            **{str(key): str(value) for key, value in (headers or {}).items()},
        }
        last_error: BaseException | None = None
        for attempt in range(self.retries + 1):
            self.rate_limiter.wait()
            request = urllib.request.Request(url, headers=safe_headers)
            try:
                open_url = urllib.request.urlopen
                if not self.use_environment_proxy:
                    open_url = urllib.request.build_opener(
                        urllib.request.ProxyHandler({})
                    ).open
                with open_url(request, timeout=self.timeout) as response:
                    raw = response.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise HistoricalCollectionError(f"{source} response exceeded max_bytes")
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, Mapping):
                    raise HistoricalCollectionError(f"{source} response is not an object")
                _atomic_json(
                    path,
                    {
                        "source": source,
                        "url": url,
                        "retrieved_at": now_iso(),
                        "payload": payload,
                    },
                )
                return payload
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in TRANSIENT_HTTP_STATUS:
                    raise
            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
                UnicodeError,
                json.JSONDecodeError,
            ) as exc:
                last_error = exc
            if attempt < self.retries:
                time.sleep(min(8.0, 0.5 * (2**attempt)))
        detail = " ".join(str(last_error or "unknown").split())[:240]
        raise HistoricalCollectionError(
            f"{source} request failed: "
            f"{type(last_error).__name__ if last_error else 'unknown'}"
            + (f" ({detail})" if detail else "")
        ) from last_error


def _evidence_id(source: str, identifier: str, title: str, published: str) -> str:
    identity = identifier or stable_digest(normalize_text(title), published)[:24]
    return f"{source}:{identity}"


def _paper_record(
    *,
    source: str,
    venue: VenueSeed,
    identifier: str,
    doi: str,
    title: str,
    abstract: str,
    published: str,
    date_precision: str,
    url: str,
    keywords: Sequence[str] = (),
    source_record_id: str = "",
) -> dict[str, Any]:
    return {
        "evidence_id": _evidence_id(source, identifier or doi, title, published),
        "venue_id": venue.venue_id,
        "kind": "paper",
        "source": source,
        "source_record_id": source_record_id or identifier,
        "doi": normalize_doi(doi),
        "title": " ".join(title.split()),
        "abstract": " ".join(abstract.split()),
        "publication_date": published,
        "publication_date_precision": date_precision,
        "url": url,
        "issns": [_canonical_issn(value) for value in venue.issns],
        "keywords": sorted({" ".join(str(value).split()) for value in keywords if str(value).strip()}),
        "retrieved_at": now_iso(),
        "temporal_eligible": True,
        "content_sha256": stable_digest(title, abstract),
        "license": "source_metadata_terms_apply",
    }


class CrossrefHistoricalSource:
    name = "crossref"

    def __init__(self, client: CachedJsonClient, *, mailto: str = "") -> None:
        self.client = client
        self.mailto = mailto.strip()

    def fetch(self, venue: VenueSeed, policy: CollectionPolicy) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for token in venue.issns:
            cursor = "*"
            for _page in range(policy.max_pages_per_source):
                filters = ",".join(
                    (
                        f"from-pub-date:{policy.history_start}",
                        f"until-pub-date:{policy.cutoff}",
                        "type:journal-article",
                    )
                )
                params = {
                    "filter": filters,
                    "rows": str(min(100, max(50, policy.max_papers_per_venue * 2))),
                    "sort": "published",
                    "order": "desc",
                    "cursor": cursor,
                }
                if self.mailto:
                    params["mailto"] = self.mailto
                url = (
                    "https://api.crossref.org/journals/"
                    + urllib.parse.quote(_canonical_issn(token), safe="")
                    + "/works?"
                    + urllib.parse.urlencode(params)
                )
                payload = self.client.get(self.name, url)
                message = payload.get("message")
                items = message.get("items") if isinstance(message, Mapping) else None
                if not isinstance(items, list) or not items:
                    break
                for item in items:
                    if not isinstance(item, Mapping):
                        continue
                    title = jats_to_text(_first_text(item.get("title")))
                    relation = item.get("relation")
                    if (
                        not title
                        or str(item.get("type") or "") != "journal-article"
                        or NOTICE_TITLE_RE.search(title)
                        or item.get("update-to")
                        or (
                            isinstance(relation, Mapping)
                            and NOTICE_RELATIONS.intersection(relation)
                        )
                    ):
                        continue
                    item_tokens = set(_item_issns(item))
                    if item_tokens and not item_tokens.intersection(venue.issns):
                        continue
                    published_info = crossref_publication_date(item)
                    if published_info is None:
                        continue
                    published_date, precision = published_info
                    # A year-only date in the cutoff year could fall after T.
                    if precision == "year" and published_date.year == parse_iso_date(
                        policy.cutoff, field_name="cutoff"
                    ).year:
                        continue
                    if not (
                        parse_iso_date(policy.history_start, field_name="history_start")
                        <= published_date
                        <= parse_iso_date(policy.cutoff, field_name="cutoff")
                    ):
                        continue
                    doi = normalize_doi(item.get("DOI"))
                    identity = doi or stable_digest(normalize_text(title), published_date.year)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    records.append(
                        _paper_record(
                            source=self.name,
                            venue=venue,
                            identifier=identity,
                            doi=doi,
                            title=title,
                            abstract=jats_to_text(item.get("abstract")),
                            published=published_date.isoformat(),
                            date_precision=precision,
                            url=("https://doi.org/" + doi) if doi else str(item.get("URL") or ""),
                            keywords=tuple(item.get("subject") or ()),
                            source_record_id=doi,
                        )
                    )
                if len(records) >= policy.max_papers_per_venue:
                    break
                next_cursor = str(message.get("next-cursor") or "") if isinstance(message, Mapping) else ""
                if not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
            if len(records) >= policy.max_papers_per_venue:
                break
        records.sort(
            key=lambda row: (str(row.get("publication_date") or ""), str(row["evidence_id"])),
            reverse=True,
        )
        return records[: policy.max_papers_per_venue]


def _openalex_abstract(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    positions: list[tuple[int, str]] = []
    for token, raw_positions in value.items():
        if not isinstance(raw_positions, list):
            continue
        for position in raw_positions:
            try:
                positions.append((int(position), str(token)))
            except (TypeError, ValueError):
                continue
    return " ".join(token for _position, token in sorted(positions))


class OpenAlexHistoricalSource:
    name = "openalex"

    def __init__(self, client: CachedJsonClient, *, mailto: str = "") -> None:
        self.client = client
        self.mailto = mailto.strip()

    def _source_id(self, venue: VenueSeed) -> str:
        for token in venue.issns:
            url = "https://api.openalex.org/sources/issn:" + urllib.parse.quote(
                _canonical_issn(token), safe=":-"
            )
            if self.mailto:
                url += "?" + urllib.parse.urlencode({"mailto": self.mailto})
            try:
                payload = self.client.get("openalex-source", url)
            except (HistoricalCollectionError, urllib.error.HTTPError):
                continue
            source_id = str(payload.get("id") or "").rstrip("/").split("/")[-1]
            if re.fullmatch(r"S\d+", source_id):
                return source_id
        return ""

    def fetch(self, venue: VenueSeed, policy: CollectionPolicy) -> list[dict[str, Any]]:
        source_id = self._source_id(venue)
        if not source_id:
            return []
        cursor = "*"
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _page in range(policy.max_pages_per_source):
            filters = ",".join(
                (
                    f"primary_location.source.id:{source_id}",
                    f"from_publication_date:{policy.history_start}",
                    f"to_publication_date:{policy.cutoff}",
                    "type:article",
                )
            )
            params = {
                "filter": filters,
                "per-page": str(min(100, max(50, policy.max_papers_per_venue * 2))),
                "cursor": cursor,
                "sort": "publication_date:desc",
            }
            if self.mailto:
                params["mailto"] = self.mailto
            url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
            payload = self.client.get(self.name, url)
            items = payload.get("results")
            if not isinstance(items, list) or not items:
                break
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                title = " ".join(str(item.get("display_name") or item.get("title") or "").split())
                published = str(item.get("publication_date") or "")[:10]
                if not title or not published:
                    continue
                try:
                    published_date = parse_iso_date(published, field_name="OpenAlex publication_date")
                except ResearchDataError:
                    continue
                if not (
                    parse_iso_date(policy.history_start, field_name="history_start")
                    <= published_date
                    <= parse_iso_date(policy.cutoff, field_name="cutoff")
                ):
                    continue
                doi = normalize_doi(item.get("doi"))
                openalex_id = str(item.get("id") or "").rstrip("/").split("/")[-1]
                identity = doi or openalex_id or stable_digest(normalize_text(title), published[:4])
                if identity in seen:
                    continue
                seen.add(identity)
                keyword_values = []
                for field_name in ("keywords", "topics", "concepts"):
                    raw_values = item.get(field_name)
                    if isinstance(raw_values, list):
                        keyword_values.extend(
                            str(value.get("display_name") or "")
                            for value in raw_values
                            if isinstance(value, Mapping)
                        )
                records.append(
                    _paper_record(
                        source=self.name,
                        venue=venue,
                        identifier=identity,
                        doi=doi,
                        title=title,
                        abstract=_openalex_abstract(item.get("abstract_inverted_index")),
                        published=published_date.isoformat(),
                        date_precision="day",
                        url=("https://doi.org/" + doi) if doi else str(item.get("id") or ""),
                        keywords=keyword_values,
                        source_record_id=openalex_id,
                    )
                )
            if len(records) >= policy.max_papers_per_venue:
                break
            meta = payload.get("meta")
            next_cursor = str(meta.get("next_cursor") or "") if isinstance(meta, Mapping) else ""
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        records.sort(
            key=lambda row: (str(row.get("publication_date") or ""), str(row["evidence_id"])),
            reverse=True,
        )
        return records[: policy.max_papers_per_venue]


def catalog_evidence(venue: VenueSeed, cutoff: str) -> list[dict[str, Any]]:
    # The mutable production ``收稿方向`` column is intentionally excluded
    # from the temporal profile.  Only the frozen catalog identity/category
    # fields are treated as available at T.
    values = [venue.name, venue.subject, venue.subject_en]
    text = "\n".join(value for value in values if value)
    records = [
        {
            "evidence_id": f"catalog:{venue.venue_id}",
            "venue_id": venue.venue_id,
            "kind": "catalog",
            "source": "jcr_2025",
            "title": venue.name,
            "text": text,
            "url": venue.homepage,
            "valid_at": cutoff,
            "retrieved_at": now_iso(),
            "temporal_eligible": True,
            "content_sha256": stable_digest(text),
            "license": "catalog_source_terms_apply",
        }
    ]
    if venue.official_scope:
        valid_at = _timestamp_date(venue.official_scope_updated_at)
        records.append(
            {
                "evidence_id": f"official-scope:{venue.venue_id}:catalog",
                "venue_id": venue.venue_id,
                "kind": "official_scope",
                "source": "catalog_official_scope",
                "title": venue.name + " aims and scope",
                "text": venue.official_scope,
                "evidence": venue.official_scope_evidence,
                "url": venue.official_scope_url,
                "valid_at": valid_at,
                "retrieved_at": venue.official_scope_updated_at or now_iso(),
                "temporal_eligible": bool(valid_at and valid_at <= cutoff),
                "content_sha256": stable_digest(venue.official_scope),
                "license": "publisher_page_terms_apply",
            }
        )
    return records


class OfficialScopeSearchSource:
    """Search official pages and let the configured PCL model extract scope."""

    name = "search_official_scope"

    def __init__(
        self,
        api_config: Mapping[str, Any],
        cache_dir: Path,
        *,
        timeout: int = 35,
        max_search_queries: int = 1,
        llm_semaphore: threading.Semaphore | None = None,
    ) -> None:
        self.api_config = dict(api_config)
        self.search_config = enrichment.search_config(dict(api_config))
        self.cache_dir = cache_dir
        self.llm_semaphore = llm_semaphore
        self.args = argparse.Namespace(
            max_search_queries=max_search_queries,
            cache_dir=cache_dir,
            timeout=timeout,
            search_results=5,
            max_html_bytes=1_000_000,
            max_pages=3,
            skip_journal_homepage_lookup=False,
            max_chars_per_page=8_000,
            allow_untrusted_domains=False,
            llm_semaphore=llm_semaphore,
        )

    def fetch(self, venue: VenueSeed, policy: CollectionPolicy) -> list[dict[str, Any]]:
        if not self.search_config.get("provider"):
            return []
        row = {
            "dataset": "jcr",
            "record_type": "journal",
            "name": venue.name,
            "issn": _canonical_issn(venue.issns[0]) if venue.issns else "",
            "eissn": _canonical_issn(venue.issns[1]) if len(venue.issns) > 1 else "",
            "url": venue.homepage,
            "收稿方向": venue.catalog_scope or venue.subject,
        }
        _index, status, result, error = enrichment.enrich_row(
            0,
            row,
            self.args,
            self.api_config,
            self.search_config,
        )
        source_name = f"search:{self.search_config.get('provider')}"
        if status.startswith("error:"):
            # Bibliographic corpus construction should remain resumable when
            # a paid Search API is temporarily exhausted.  Try the canonical
            # publisher/homepage path directly and still let PCL validate it.
            pages = enrichment.candidate_pages(
                row,
                [],
                self.cache_dir,
                timeout=self.args.timeout,
                max_bytes=self.args.max_html_bytes,
                max_pages=self.args.max_pages,
                use_journal_homepage_lookup=True,
            )
            if pages:
                if self.llm_semaphore is None:
                    result = enrichment.call_llm(
                        row,
                        pages,
                        self.api_config,
                        self.cache_dir,
                        timeout=self.args.timeout,
                        max_chars_per_page=self.args.max_chars_per_page,
                    )
                else:
                    with self.llm_semaphore:
                        result = enrichment.call_llm(
                            row,
                            pages,
                            self.api_config,
                            self.cache_dir,
                            timeout=self.args.timeout,
                            max_chars_per_page=self.args.max_chars_per_page,
                        )
                status = "ok" if result.get("is_relevant") else "not_relevant"
                source_name = "direct_official_page:pcl"
        if status != "ok" or not isinstance(result, Mapping):
            if error and status.startswith("error:"):
                raise HistoricalCollectionError(error)
            return []
        scope = " ".join(str(result.get("scope_summary") or "").split())
        if not scope:
            return []
        retrieved_at = now_iso()
        retrieved_date = retrieved_at[:10]
        return [
            {
                "evidence_id": f"official-scope:{venue.venue_id}:search:{stable_digest(scope)[:12]}",
                "venue_id": venue.venue_id,
                "kind": "official_scope",
                "source": source_name,
                "title": venue.name + " aims and scope",
                "text": scope,
                "keywords": [
                    " ".join(str(value).split())
                    for value in (result.get("scope_keywords") or [])
                    if str(value).strip()
                ],
                "evidence": " ".join(str(result.get("evidence") or "").split()),
                "url": str(result.get("source_url") or ""),
                "valid_at": retrieved_date,
                "retrieved_at": retrieved_at,
                # A page fetched after T is useful to production, but it must
                # not silently enter the temporal paper benchmark.
                "temporal_eligible": retrieved_date <= policy.cutoff,
                "content_sha256": stable_digest(scope),
                "license": "publisher_page_terms_apply",
            }
        ]


class PCLPrototypeClient:
    """Grounded prototype synthesis with model-aware PCL failover."""

    PROMPT_VERSION = "grounded-prototypes-v3-model-pool"

    def __init__(
        self,
        api_config: Mapping[str, Any],
        cache_dir: Path,
        *,
        max_output_tokens: int | None = None,
        request_semaphore: threading.Semaphore | None = None,
        models: Sequence[str] | None = None,
        model_context_windows: Mapping[str, int] | None = None,
        model_max_output_tokens: Mapping[str, int] | None = None,
        model_fallbacks: int | None = None,
    ) -> None:
        llm = enrichment.llm_config(dict(api_config))
        base_url = str(
            llm.get("base_url") or llm.get("api_base") or llm.get("endpoint") or ""
        ).strip()
        configured_primary = str(llm.get("model") or "").strip()
        configured_models: object = (
            models
            if models is not None
            else llm.get("pcl_models") or llm.get("models")
        )
        if not configured_models:
            configured_models = [configured_primary]
        if isinstance(configured_models, str):
            configured_models = [configured_models]
        normalized_models: list[str] = []
        if isinstance(configured_models, Sequence):
            for raw in configured_models:
                for value in str(raw).split(","):
                    name = value.strip()
                    if name and name not in normalized_models:
                        normalized_models.append(name)
        if not base_url or not normalized_models:
            raise HistoricalCollectionError("PCL LLM requires base_url and at least one model")
        self.models = tuple(normalized_models)
        self.model = self.models[0]
        hostname = (urllib.parse.urlparse(base_url).hostname or "").casefold()
        if not (hostname == "pcl.ac.cn" or hostname.endswith(".pcl.ac.cn")):
            raise HistoricalCollectionError(
                "historical collection requires the configured PCL API endpoint"
            )
        self.endpoint = str(llm.get("chat_completions_url") or "").strip()
        if not self.endpoint:
            self.endpoint = base_url.rstrip("/") + "/chat/completions"
        self.llm_config = llm
        configured_max_tokens = llm.get(
            "max_output_tokens", llm.get("max_tokens", 2048)
        )
        try:
            configured_base_tokens = int(configured_max_tokens)
            hard_max_output_tokens = int(
                max_output_tokens if max_output_tokens is not None else 16_384
            )
        except (TypeError, ValueError) as exc:
            raise HistoricalCollectionError("PCL max output tokens must be an integer") from exc
        if not 64 <= configured_base_tokens <= 16_384:
            raise HistoricalCollectionError(
                "PCL max output tokens must be between 64 and 16384"
            )
        if not 64 <= hard_max_output_tokens <= 16_384:
            raise HistoricalCollectionError(
                "PCL hard max output tokens must be between 64 and 16384"
            )
        self.hard_max_output_tokens = hard_max_output_tokens
        self.max_output_tokens = min(configured_base_tokens, hard_max_output_tokens)

        fallback_value = (
            model_fallbacks
            if model_fallbacks is not None
            else llm.get("model_fallbacks", 2)
        )
        try:
            self.model_fallbacks = int(fallback_value)
        except (TypeError, ValueError) as exc:
            raise HistoricalCollectionError("PCL model_fallbacks must be an integer") from exc
        if self.model_fallbacks < 0:
            raise HistoricalCollectionError("PCL model_fallbacks must be non-negative")
        self.model_fallbacks = min(self.model_fallbacks, len(self.models) - 1)

        output_overrides: dict[str, int] = {}
        for source in (llm.get("model_max_output_tokens"), model_max_output_tokens):
            if not isinstance(source, Mapping):
                continue
            for name, raw_value in source.items():
                try:
                    value = int(raw_value)
                except (TypeError, ValueError) as exc:
                    raise HistoricalCollectionError(
                        f"invalid PCL output limit for {name}"
                    ) from exc
                if not 64 <= value <= 16_384:
                    raise HistoricalCollectionError(
                        f"PCL output limit must be between 64 and 16384 for {name}"
                    )
                output_overrides[str(name)] = min(value, self.hard_max_output_tokens)
        self.model_output_tokens = {
            name: output_overrides.get(name, self.max_output_tokens)
            for name in self.models
        }

        context_overrides: dict[str, int] = {}
        for source in (llm.get("model_context_windows"), model_context_windows):
            if not isinstance(source, Mapping):
                continue
            for name, raw_value in source.items():
                if raw_value is None:
                    continue
                try:
                    value = int(raw_value)
                except (TypeError, ValueError) as exc:
                    raise HistoricalCollectionError(
                        f"invalid PCL context window for {name}"
                    ) from exc
                if value <= self.max_output_tokens + 8_192:
                    raise HistoricalCollectionError(
                        f"PCL context window is too small for {name}"
                    )
                context_overrides[str(name)] = value
        self.context_windows: dict[str, int | None] = {
            name: context_overrides.get(name, PCL_MODEL_CONTEXT_WINDOWS.get(name))
            for name in self.models
        }
        try:
            known_cap = int(
                llm.get("model_input_token_cap", PCL_DEFAULT_INPUT_TOKEN_CAP)
            )
            unknown_cap = int(
                llm.get(
                    "unknown_model_input_token_cap",
                    PCL_UNKNOWN_MODEL_INPUT_TOKEN_CAP,
                )
            )
        except (TypeError, ValueError) as exc:
            raise HistoricalCollectionError("PCL input caps must be integers") from exc
        if known_cap < 1_024 or unknown_cap < 1_024:
            raise HistoricalCollectionError("PCL input caps must be at least 1024")
        self.model_input_caps: dict[str, int] = {}
        for name, context_window in self.context_windows.items():
            if context_window is None:
                self.model_input_caps[name] = unknown_cap
            else:
                if context_window <= self.model_output_tokens[name] + 8_192:
                    raise HistoricalCollectionError(
                        f"PCL context window is too small for {name}"
                    )
                self.model_input_caps[name] = min(
                    known_cap,
                    context_window - self.model_output_tokens[name] - 8_192,
                )

        self.cache_dir = cache_dir
        self.request_semaphore = request_semaphore
        self._selector_lock = threading.Lock()
        self._selector_cursor = 0
        self._model_cooldown_until = {name: 0.0 for name in self.models}
        self._thread_state = threading.local()
        self._audit_lock = threading.Lock()
        self._audit_path = cache_dir / "pcl_model_attempts.jsonl"
        self.provider_identity = {
            "provider": "pcl_openai_compatible_model_pool",
            "endpoint_host": urllib.parse.urlparse(self.endpoint).hostname or "",
            "model": self.model,
            "models": [
                {
                    "name": name,
                    "context_window": self.context_windows[name],
                    "input_token_cap": self.model_input_caps[name],
                    "max_output_tokens": self.model_output_tokens[name],
                }
                for name in self.models
            ],
            "selection": "round_robin_with_failure_cooldown",
            "model_fallbacks": self.model_fallbacks,
            "hard_max_output_tokens": self.hard_max_output_tokens,
        }

    @property
    def last_model(self) -> str:
        return str(getattr(self._thread_state, "last_model", self.model))

    def _model_route(self) -> tuple[str, ...]:
        with self._selector_lock:
            start = self._selector_cursor
            self._selector_cursor = (self._selector_cursor + 1) % len(self.models)
            now = time.monotonic()
            ordered = [
                self.models[(start + offset) % len(self.models)]
                for offset in range(len(self.models))
            ]
            available = [
                model
                for model in ordered
                if self._model_cooldown_until[model] <= now
            ]
            cooling = [model for model in ordered if model not in available]
        count = min(len(self.models), self.model_fallbacks + 1)
        return tuple([*available, *cooling][:count])

    def _mark_model_success(self, model: str) -> None:
        with self._selector_lock:
            self._model_cooldown_until[model] = 0.0

    def _mark_model_failure(self, model: str, status: str) -> None:
        normalized = status.casefold()
        delay = 30.0 if (
            "timeout" in normalized
            or "transport" in normalized
            or normalized.startswith("http_404")
            or normalized.startswith("http_429")
            or normalized.startswith("http_5")
        ) else 10.0
        with self._selector_lock:
            self._model_cooldown_until[model] = max(
                self._model_cooldown_until[model],
                time.monotonic() + delay,
            )

    @staticmethod
    def _prompt_size(messages: Sequence[Mapping[str, Any]]) -> int:
        # UTF-8 bytes form a conservative upper bound for tokenizer units and
        # avoid claiming one tokenizer across five different model families.
        return len(
            json.dumps(
                messages,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )

    @staticmethod
    def _evidence_block(row: Mapping[str, Any]) -> str:
        evidence_id = str(row.get("evidence_id") or "")
        content = str(row.get("abstract") or row.get("text") or row.get("title") or "")
        return "\n".join(
            (
                f"[Evidence {evidence_id}]",
                f"kind={row.get('kind', '')}; source={row.get('source', '')}; date={row.get('publication_date') or row.get('valid_at') or ''}",
                f"title={' '.join(str(row.get('title') or '').split())[:320]}",
                f"content={' '.join(content.split())[:900]}",
            )
        )

    def _messages_for_model(
        self,
        model: str,
        venue: VenueSeed,
        ranked: Sequence[Mapping[str, Any]],
        policy: CollectionPolicy,
        output_tokens: int,
    ) -> tuple[list[dict[str, str]], dict[str, Mapping[str, Any]], int]:
        def build(selected: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
            ids = [str(row.get("evidence_id") or "") for row in selected]
            blocks = [self._evidence_block(row) for row in selected]
            return [
                {
                    "role": "system",
                    "content": (
                        "You build grounded journal topic prototypes. Return strict JSON only. "
                        "Use only the supplied evidence; never invent papers, scope, dates, IDs, "
                        "or venue facts. Every prototype must cite one or more exact evidence_ids."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Journal: {venue.name}\nISSNs: {', '.join(_canonical_issn(v) for v in venue.issns)}\n"
                        f"Catalog subject: {venue.subject}; {venue.subject_en}\n\n"
                        + "\n\n".join(blocks)
                        + "\n\nAllowed evidence_ids (copy these strings exactly): "
                        + ", ".join(ids)
                        + "\n\nReturn {\"prototypes\":[...]} with 2-"
                        + str(policy.max_prototypes)
                        + " diverse prototypes. Each item must contain label, summary, keywords "
                        "(array), evidence_ids (array of exact IDs), and confidence "
                        "(high|medium|low). Keep summary concise and retrieval-oriented."
                    ),
                },
            ]

        cap = self.model_input_caps[model]
        context_window = self.context_windows[model]
        if context_window is not None:
            cap = min(cap, context_window - output_tokens - 8_192)
        selected: list[Mapping[str, Any]] = []
        for row in ranked:
            if not str(row.get("evidence_id") or ""):
                continue
            candidate = [*selected, row]
            candidate_messages = build(candidate)
            if self._prompt_size(candidate_messages) <= cap:
                selected = candidate
        if not selected:
            raise HistoricalCollectionError(
                f"PCL prompt cannot fit one evidence record within {model} input cap"
            )
        messages = build(selected)
        evidence_by_id = {
            str(row.get("evidence_id") or ""): row for row in selected
        }
        return messages, evidence_by_id, self._prompt_size(messages)

    @staticmethod
    def _message_text(data: Any) -> tuple[str, str, int]:
        choices = data.get("choices") if isinstance(data, Mapping) else None
        if not isinstance(choices, list) or not choices:
            return "", "", 0
        choice = choices[0] if isinstance(choices[0], Mapping) else {}
        finish_reason = str(choice.get("finish_reason") or "")
        message = choice.get("message") if isinstance(choice, Mapping) else None
        if not isinstance(message, Mapping):
            return "", finish_reason, len(choices)
        content = message.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, Mapping):
                    value = part.get("text") or part.get("content") or ""
                    parts.append(str(value))
                elif part is not None:
                    parts.append(str(part))
            text = "\n".join(parts)
        else:
            text = str(content or "")
        if not text.strip():
            text = str(message.get("reasoning_content") or "")
        return text, finish_reason, len(choices)

    @staticmethod
    def _parse_prototypes(
        candidate: Any,
        *,
        venue: VenueSeed,
        policy: CollectionPolicy,
        model: str,
        evidence_by_id: Mapping[str, Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        raw_prototypes = (
            candidate.get("prototypes") if isinstance(candidate, Mapping) else None
        )
        if not isinstance(raw_prototypes, list):
            return []
        valid_ids = set(evidence_by_id)
        parsed: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_prototypes[: policy.max_prototypes]):
            if not isinstance(raw, Mapping):
                continue
            cited = [
                str(value)
                for value in (raw.get("evidence_ids") or [])
                if str(value) in valid_ids
            ]
            if not cited:
                continue
            label = " ".join(str(raw.get("label") or "").split())
            summary = " ".join(str(raw.get("summary") or "").split())
            keywords = [
                " ".join(str(value).split())
                for value in (raw.get("keywords") or [])
                if str(value).strip()
            ][:16]
            text = ". ".join(
                value for value in (label, summary, "; ".join(keywords)) if value
            )
            if not text:
                continue
            cited_rows = [evidence_by_id[value] for value in cited]
            source_dates = [
                str(row.get("publication_date") or row.get("valid_at") or "")
                for row in cited_rows
                if row.get("publication_date") or row.get("valid_at")
            ]
            parsed.append(
                {
                    "prototype_id": f"{venue.venue_id}:pcl:{index}",
                    "kind": "historical_topic",
                    "label": label,
                    "text": text,
                    "keywords": keywords,
                    "weight": 1.0,
                    "confidence": str(raw.get("confidence") or "medium"),
                    "source_ids": cited,
                    "source_max_date": max(source_dates, default=""),
                    "temporal_eligible": all(
                        bool(row.get("temporal_eligible")) for row in cited_rows
                    ),
                    "derived_by": "pcl_llm",
                    "model": model,
                }
            )
        return parsed

    def _audit_model_attempt(self, **fields: Any) -> None:
        row = {"recorded_at": now_iso(), **fields}
        with self._audit_lock:
            _append_jsonl(self._audit_path, [row])

    def synthesize(
        self,
        venue: VenueSeed,
        evidence: Sequence[Mapping[str, Any]],
        policy: CollectionPolicy,
    ) -> tuple[list[dict[str, Any]], str]:
        ranked = sorted(
            evidence,
            key=lambda row: (
                {"official_scope": 3, "catalog": 2, "paper": 1}.get(
                    str(row.get("kind") or ""), 0
                ),
                bool(row.get("abstract")),
                str(row.get("publication_date") or row.get("valid_at") or ""),
                str(row.get("evidence_id") or ""),
            ),
            reverse=True,
        )[: policy.max_pcl_evidence]
        semantic_status = ""
        semantic_model = ""
        last_exception: BaseException | None = None
        truncated = False
        route = self._model_route()
        for route_index, model in enumerate(route):
            self._thread_state.last_model = model
            configured_output_tokens = self.model_output_tokens[model]
            output_tokens = (
                min(
                    self.hard_max_output_tokens,
                    max(configured_output_tokens, 3_072),
                )
                if truncated
                else configured_output_tokens
            )
            messages, evidence_by_id, prompt_size = self._messages_for_model(
                model, venue, ranked, policy, output_tokens
            )
            cache_key = stable_digest(
                self.PROMPT_VERSION,
                self.endpoint,
                model,
                output_tokens,
                bool(self.llm_config.get("json_mode")),
                json.dumps(messages, ensure_ascii=False),
            )
            path = self.cache_dir / "pcl_prototypes" / f"{cache_key}.json"
            cached_response: Any = None
            if path.is_file():
                try:
                    cached = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    cached = None
                cached_response = (
                    cached.get("result") if isinstance(cached, Mapping) else None
                )
            cached_prototypes = self._parse_prototypes(
                cached_response,
                venue=venue,
                policy=policy,
                model=model,
                evidence_by_id=evidence_by_id,
            )
            if cached_prototypes:
                self._mark_model_success(model)
                self._audit_model_attempt(
                    venue_id=venue.venue_id,
                    model=model,
                    route_index=route_index,
                    status="cache_hit",
                    prompt_token_upper_bound=prompt_size,
                    evidence_count=len(evidence_by_id),
                    output_tokens=output_tokens,
                    duration_seconds=0.0,
                )
                return cached_prototypes, "ok"

            payload: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": self.llm_config.get("temperature", 0),
                "max_tokens": output_tokens,
            }
            if self.llm_config.get("json_mode"):
                payload["response_format"] = {"type": "json_object"}
            request_kwargs = {
                "payload": payload,
                "config": self.llm_config,
                "headers": enrichment.api_headers(self.llm_config),
                "timeout": int(self.llm_config.get("timeout", 60)),
                "max_bytes": min(
                    1_000_000,
                    max(65_536, int(self.llm_config.get("max_response_bytes", 262_144))),
                ),
            }
            started = time.monotonic()
            content = b""
            try:
                if self.request_semaphore is None:
                    _status, _headers, content = enrichment.openai_chat_request(
                        self.endpoint, **request_kwargs
                    )
                else:
                    with self.request_semaphore:
                        started = time.monotonic()
                        _status, _headers, content = enrichment.openai_chat_request(
                            self.endpoint, **request_kwargs
                        )
            except urllib.error.HTTPError as exc:
                duration = time.monotonic() - started
                self._audit_model_attempt(
                    venue_id=venue.venue_id,
                    model=model,
                    route_index=route_index,
                    status="http_error",
                    http_status=int(exc.code),
                    error_type=type(exc).__name__,
                    prompt_token_upper_bound=prompt_size,
                    evidence_count=len(evidence_by_id),
                    output_tokens=output_tokens,
                    duration_seconds=round(duration, 3),
                )
                if int(exc.code) == 401:
                    raise
                self._mark_model_failure(model, f"http_{int(exc.code)}")
                last_exception = exc
                continue
            except Exception as exc:  # noqa: BLE001 - next model is the fallback boundary.
                duration = time.monotonic() - started
                self._audit_model_attempt(
                    venue_id=venue.venue_id,
                    model=model,
                    route_index=route_index,
                    status="transport_error",
                    error_type=type(exc).__name__,
                    prompt_token_upper_bound=prompt_size,
                    evidence_count=len(evidence_by_id),
                    output_tokens=output_tokens,
                    duration_seconds=round(duration, 3),
                )
                self._mark_model_failure(model, f"transport:{type(exc).__name__}")
                last_exception = exc
                continue

            duration = time.monotonic() - started
            finish_reason = ""
            choices_count = 0
            content_text = ""
            response: Any = None
            parse_stage = "body_json"
            try:
                data = json.loads(content.decode("utf-8"))
                content_text, finish_reason, choices_count = self._message_text(data)
                parse_stage = "message_json"
                response = enrichment.extract_json_object(content_text)
            except (UnicodeError, json.JSONDecodeError, ValueError, TypeError, AttributeError, IndexError):
                response = None
            prototypes = self._parse_prototypes(
                response,
                venue=venue,
                policy=policy,
                model=model,
                evidence_by_id=evidence_by_id,
            )
            terminal_reason = finish_reason.casefold()
            if prototypes and terminal_reason not in {"length", "content_filter"}:
                self._mark_model_success(model)
                _atomic_json(
                    path,
                    {
                        "result": response,
                        "cached_at": now_iso(),
                        "prompt_version": self.PROMPT_VERSION,
                        "model": model,
                        "max_output_tokens": output_tokens,
                    },
                )
                self._audit_model_attempt(
                    venue_id=venue.venue_id,
                    model=model,
                    route_index=route_index,
                    status="ok",
                    finish_reason=finish_reason,
                    choices_count=choices_count,
                    response_bytes=len(content),
                    streamed=_headers.get("x-wpg-streamed") == "1",
                    stream_events=int(_headers.get("x-wpg-stream-events", 0)),
                    stream_wire_bytes=int(
                        _headers.get("x-wpg-stream-wire-bytes", len(content))
                    ),
                    content_chars=len(content_text),
                    body_sha256=hashlib.sha256(content).hexdigest(),
                    parse_stage="grounded",
                    prompt_token_upper_bound=prompt_size,
                    evidence_count=len(evidence_by_id),
                    output_tokens=output_tokens,
                    duration_seconds=round(duration, 3),
                )
                return prototypes, "ok"

            if terminal_reason == "length":
                attempt_status = "truncated_response"
                truncated = True
                semantic_status = "invalid_response"
                semantic_model = model
            elif terminal_reason == "content_filter":
                attempt_status = "invalid_response"
                semantic_status = "invalid_response"
                semantic_model = model
            elif isinstance(response, Mapping) and isinstance(
                response.get("prototypes"), list
            ):
                attempt_status = "ungrounded_response"
                semantic_status = "ungrounded_response"
                semantic_model = model
            else:
                attempt_status = "invalid_response"
                if not semantic_status:
                    semantic_status = "invalid_response"
                    semantic_model = model
            self._mark_model_failure(model, attempt_status)
            self._audit_model_attempt(
                venue_id=venue.venue_id,
                model=model,
                route_index=route_index,
                status=attempt_status,
                finish_reason=finish_reason,
                choices_count=choices_count,
                response_bytes=len(content),
                streamed=_headers.get("x-wpg-streamed") == "1",
                stream_events=int(_headers.get("x-wpg-stream-events", 0)),
                stream_wire_bytes=int(
                    _headers.get("x-wpg-stream-wire-bytes", len(content))
                ),
                content_chars=len(content_text),
                body_sha256=hashlib.sha256(content).hexdigest(),
                parse_stage=parse_stage,
                prompt_token_upper_bound=prompt_size,
                evidence_count=len(evidence_by_id),
                output_tokens=output_tokens,
                duration_seconds=round(duration, 3),
            )

        if semantic_status:
            self._thread_state.last_model = semantic_model
            return [], semantic_status
        if last_exception is not None:
            raise last_exception
        return [], "invalid_response"


def merge_paper_evidence(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Merge source records by DOI, otherwise by normalized title and year."""

    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in records:
        if row.get("kind") != "paper":
            continue
        doi = normalize_doi(row.get("doi"))
        title = normalize_text(row.get("title"))
        year = str(row.get("publication_date") or "")[:4]
        key = "doi:" + doi if doi else "title:" + stable_digest(title, year)
        grouped[key].append(row)
    merged: list[dict[str, Any]] = []
    for key, values in grouped.items():
        best = max(
            values,
            key=lambda row: (
                len(str(row.get("abstract") or "")),
                len(str(row.get("title") or "")),
                str(row.get("source") or ""),
            ),
        )
        row = dict(best)
        row["evidence_id"] = key
        row["sources"] = sorted({str(value.get("source") or "") for value in values} - {""})
        row["source_evidence_ids"] = sorted(
            {str(value.get("evidence_id") or "") for value in values} - {""}
        )
        row["keywords"] = sorted(
            {
                str(keyword)
                for value in values
                for keyword in (value.get("keywords") or [])
                if str(keyword).strip()
            }
        )
        merged.append(row)
    merged.sort(
        key=lambda row: (str(row.get("publication_date") or ""), str(row["evidence_id"])),
        reverse=True,
    )
    return merged


def _fallback_prototypes(
    venue: VenueSeed,
    papers: Sequence[Mapping[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if not papers:
        return []
    buckets: list[list[Mapping[str, Any]]] = [[] for _ in range(min(4, limit, len(papers)))]
    for row in papers:
        bucket = int(stable_digest(row.get("evidence_id"))[:8], 16) % len(buckets)
        buckets[bucket].append(row)
    prototypes: list[dict[str, Any]] = []
    for index, bucket in enumerate(buckets):
        if not bucket:
            continue
        selected = sorted(
            bucket,
            key=lambda row: (str(row.get("publication_date") or ""), str(row.get("evidence_id") or "")),
            reverse=True,
        )[:8]
        text = "\n".join(
            f"{row.get('title', '')}. {str(row.get('abstract') or '')[:500]}".strip()
            for row in selected
        )
        prototypes.append(
            {
                "prototype_id": f"{venue.venue_id}:history:{index}",
                "kind": "historical_evidence_cluster",
                "label": f"Historical topic {index + 1}",
                "text": text,
                "keywords": [],
                "weight": 0.9,
                "confidence": "evidence_only",
                "source_ids": [str(row["evidence_id"]) for row in selected],
                "source_max_date": max(
                    (str(row.get("publication_date") or "") for row in selected),
                    default="",
                ),
                "temporal_eligible": True,
                "derived_by": "deterministic_fallback",
                "model": "",
            }
        )
    return prototypes


def build_venue_profile(
    venue: VenueSeed,
    evidence: Sequence[Mapping[str, Any]],
    llm_prototypes: Sequence[Mapping[str, Any]],
    *,
    cutoff: str,
    pcl_status: str,
    pcl_model: str,
    max_prototypes: int,
    collection_status: str,
    source_errors: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    papers = [dict(row) for row in evidence if row.get("kind") == "paper"]
    temporal_papers = [row for row in papers if row.get("temporal_eligible")]
    abstract_count = sum(bool(str(row.get("abstract") or "").strip()) for row in temporal_papers)
    title_only_count = len(temporal_papers) - abstract_count
    scope_rows = [row for row in evidence if row.get("kind") == "official_scope"]
    temporal_scope = [row for row in scope_rows if row.get("temporal_eligible")]

    catalog_text = "\n".join(
        value
        for value in (venue.name, venue.subject, venue.subject_en)
        if value
    )
    prototypes: list[dict[str, Any]] = [
        {
            "prototype_id": f"{venue.venue_id}:static",
            "kind": "static",
            "label": "Catalog identity and subject",
            "text": catalog_text,
            "keywords": [value for value in (venue.subject, venue.subject_en) if value],
            "weight": 0.35,
            "confidence": "catalog",
            "source_ids": [f"catalog:{venue.venue_id}"],
            "source_max_date": cutoff,
            "temporal_eligible": True,
            "derived_by": "deterministic",
            "model": "",
        }
    ]
    for index, row in enumerate(scope_rows):
        scope_text = str(row.get("text") or "").strip()
        if scope_text:
            prototypes.append(
                {
                    "prototype_id": f"{venue.venue_id}:scope:{index}",
                    "kind": "official_scope",
                    "label": "Official aims and scope",
                    "text": scope_text,
                    "keywords": list(row.get("keywords") or ()),
                    "weight": 0.9,
                    "confidence": "official_evidence",
                    "source_ids": [str(row.get("evidence_id") or "")],
                    "source_max_date": str(row.get("valid_at") or ""),
                    "temporal_eligible": bool(row.get("temporal_eligible")),
                    "derived_by": "official_source",
                    "model": "",
                }
            )
    grounded = [dict(row) for row in llm_prototypes if row.get("source_ids")]
    if not grounded:
        grounded = _fallback_prototypes(
            venue,
            temporal_papers,
            limit=max(1, max_prototypes - len(prototypes)),
        )
    prototypes.extend(grounded)
    # Static is mandatory. Prefer diverse topical/scope prototypes within cap.
    prototypes = prototypes[:max_prototypes]
    temporal_prototypes = [row for row in prototypes if row.get("temporal_eligible")]
    profile_text = "\n\n".join(str(row.get("text") or "") for row in temporal_prototypes)
    production_profile_text = "\n\n".join(str(row.get("text") or "") for row in prototypes)

    paper_count = len(temporal_papers)
    if abstract_count >= 10:
        evidence_grade = "A"
    elif paper_count >= 10:
        evidence_grade = "B"
    elif paper_count or temporal_scope:
        evidence_grade = "C"
    else:
        evidence_grade = "D"
    profile_tier = "warm" if paper_count >= 5 else "few-shot" if paper_count else "cold"
    source_dois = sorted({normalize_doi(row.get("doi")) for row in temporal_papers} - {""})
    source_titles = sorted({str(row.get("title") or "") for row in temporal_papers} - {""})
    source_dates = sorted(
        {str(row.get("publication_date") or "") for row in temporal_papers} - {""}
    )
    source_types = sorted(
        {
            source
            for row in evidence
            for source in (
                list(row.get("sources") or ())
                if isinstance(row.get("sources"), list)
                else [str(row.get("source") or "")]
            )
            if source
        }
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "venue_id": venue.venue_id,
        "name": venue.name,
        "profile_text": profile_text,
        "production_profile_text": production_profile_text,
        "snapshot_date": cutoff,
        "prototypes": prototypes,
        "metadata": {
            "content_origin": "multi_source_frozen_historical_profiles",
            "collection_status": collection_status,
            "history_paper_count": paper_count,
            "paper_count": paper_count,
            "abstract_paper_count": abstract_count,
            "title_only_paper_count": title_only_count,
            "official_scope_count": len(scope_rows),
            "temporal_official_scope_count": len(temporal_scope),
            "prototype_count": len(prototypes),
            "temporal_prototype_count": len(temporal_prototypes),
            "profile_tier": profile_tier,
            "evidence_grade": evidence_grade,
            "profile_level": evidence_grade,
            "source_types": source_types,
            "source_dois": source_dois,
            "source_titles": source_titles,
            "source_dates": source_dates,
            "source_max_date": max(source_dates, default=""),
            "jcr_quartile": venue.quartile,
            "level": venue.quartile,
            "subject": venue.subject,
            "broad_field": venue.broad_field,
            "issns": [_canonical_issn(value) for value in venue.issns],
            "online_entity_id": venue.online_entity_id,
            "identity_status": venue.identity_status,
            "pcl_status": pcl_status,
            "pcl_model": pcl_model,
            "source_errors": dict(source_errors or {}),
        },
    }


def collect_venue_evidence(
    venue: VenueSeed,
    *,
    policy: CollectionPolicy,
    crossref: EvidenceSource | None,
    openalex: EvidenceSource | None,
    scope_search: EvidenceSource | None,
    pcl_model: str = "",
) -> dict[str, Any]:
    """Collect and persistable-normalize evidence without waiting for PCL."""

    evidence = catalog_evidence(venue, policy.cutoff)
    source_errors: dict[str, str] = {}

    def fetch(source: EvidenceSource | None) -> list[dict[str, Any]]:
        if source is None:
            return []
        try:
            return source.fetch(venue, policy)
        except Exception as exc:  # noqa: BLE001 - long jobs preserve per-source errors.
            source_errors[source.name] = f"{type(exc).__name__}: {' '.join(str(exc).split())[:180]}"
            return []

    paper_rows = fetch(crossref)
    if openalex is not None and (
        policy.openalex_mode == "always"
        or (
            policy.openalex_mode == "fallback"
            and len(paper_rows) < policy.min_papers_before_fallback
        )
    ):
        paper_rows.extend(fetch(openalex))
    papers = merge_paper_evidence(paper_rows)[: policy.max_papers_per_venue]
    evidence.extend(papers)

    has_official_scope = any(
        row.get("kind") == "official_scope" and str(row.get("text") or "").strip()
        for row in evidence
    )
    if scope_search is not None and (
        policy.scope_mode == "always"
        or (policy.scope_mode == "fallback" and not has_official_scope)
    ):
        evidence.extend(fetch(scope_search))

    profile = build_venue_profile(
        venue,
        evidence,
        (),
        cutoff=policy.cutoff,
        pcl_status="queued",
        pcl_model=pcl_model,
        max_prototypes=policy.max_prototypes,
        collection_status="partial",
        source_errors=source_errors,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "venue": venue.to_queue_row(0),
        "status": "partial",
        "collected_at": now_iso(),
        "evidence": evidence,
        "profile": profile,
        "source_errors": source_errors,
        "pcl_status": "queued",
        "pcl_queued_at": now_iso(),
    }


def finalize_venue_pcl(
    shard: Mapping[str, Any],
    venue: VenueSeed,
    *,
    policy: CollectionPolicy,
    prototypes: Sequence[Mapping[str, Any]],
    pcl_status: str,
    pcl_model: str,
    pcl_error: str = "",
) -> dict[str, Any]:
    """Rebuild one shard after a PCL attempt while preserving source evidence."""

    evidence = [
        dict(row)
        for row in (shard.get("evidence") or [])
        if isinstance(row, Mapping)
    ]
    source_errors = {
        str(key): str(value)
        for key, value in dict(shard.get("source_errors") or {}).items()
    }
    if pcl_status == "ok":
        source_errors.pop("pcl", None)
    else:
        source_errors["pcl"] = " ".join(
            str(pcl_error or f"PCLStatus: {pcl_status}").split()
        )[:180]
    status = "complete" if not source_errors and pcl_status == "ok" else "partial"
    profile = build_venue_profile(
        venue,
        evidence,
        prototypes,
        cutoff=policy.cutoff,
        pcl_status=pcl_status,
        pcl_model=pcl_model,
        max_prototypes=policy.max_prototypes,
        collection_status=status,
        source_errors=source_errors,
    )
    result = dict(shard)
    result.update(
        {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "profile": profile,
            "source_errors": source_errors,
            "pcl_status": pcl_status,
            "pcl_updated_at": now_iso(),
        }
    )
    return result


def process_venue(
    venue: VenueSeed,
    *,
    policy: CollectionPolicy,
    crossref: EvidenceSource | None,
    openalex: EvidenceSource | None,
    scope_search: EvidenceSource | None,
    pcl: PrototypeSynthesizer,
) -> dict[str, Any]:
    """Compatibility wrapper for callers that require synchronous PCL work."""

    shard = collect_venue_evidence(
        venue,
        policy=policy,
        crossref=crossref,
        openalex=openalex,
        scope_search=scope_search,
        pcl_model=pcl.model,
    )

    try:
        prototypes, pcl_status = pcl.synthesize(
            venue,
            shard["evidence"],
            policy,
        )
    except Exception as exc:  # noqa: BLE001 - deterministic fallback remains available.
        actual_model = str(getattr(pcl, "last_model", pcl.model))
        return finalize_venue_pcl(
            shard,
            venue,
            policy=policy,
            prototypes=(),
            pcl_status=f"error:{type(exc).__name__}",
            pcl_model=actual_model,
            pcl_error=f"{type(exc).__name__}: {' '.join(str(exc).split())[:180]}",
        )
    actual_model = str(getattr(pcl, "last_model", pcl.model))
    return finalize_venue_pcl(
        shard,
        venue,
        policy=policy,
        prototypes=prototypes,
        pcl_status=pcl_status,
        pcl_model=actual_model,
    )


def _static_pending_profile(venue: VenueSeed, policy: CollectionPolicy) -> dict[str, Any]:
    return build_venue_profile(
        venue,
        catalog_evidence(venue, policy.cutoff),
        (),
        cutoff=policy.cutoff,
        pcl_status="pending",
        pcl_model="",
        max_prototypes=policy.max_prototypes,
        collection_status="pending",
    )


def assemble_historical_corpus(
    *,
    venues: Sequence[VenueSeed],
    policy: CollectionPolicy,
    output_dir: Path,
    jcr_csv: Path,
    provider_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Assemble all venue shards while retaining the full candidate universe."""

    shard_dir = output_dir / "venues"
    profiles: list[dict[str, Any]] = []
    evidence_rows: list[dict[str, Any]] = []
    prototype_rows: list[dict[str, Any]] = []
    identity_rows: list[dict[str, Any]] = []
    statuses: Counter[str] = Counter()
    tiers: Counter[str] = Counter()
    grades: Counter[str] = Counter()
    source_venues: dict[str, set[str]] = defaultdict(set)
    pcl_statuses: Counter[str] = Counter()

    for venue in venues:
        shard_path = shard_dir / f"{venue.venue_id}.json"
        if shard_path.is_file():
            shard = json.loads(shard_path.read_text(encoding="utf-8"))
            profile = shard.get("profile") if isinstance(shard, Mapping) else None
            evidence = shard.get("evidence") if isinstance(shard, Mapping) else None
            if not isinstance(profile, Mapping) or not isinstance(evidence, list):
                raise HistoricalCollectionError(f"invalid venue shard: {shard_path}")
            profile_row = dict(profile)
            statuses[str(shard.get("status") or "unknown")] += 1
            pcl_statuses[str(shard.get("pcl_status") or "unknown").split(":", 1)[0]] += 1
            for row in evidence:
                if isinstance(row, Mapping):
                    evidence_row = dict(row)
                    evidence_rows.append(evidence_row)
                    sources = evidence_row.get("sources")
                    source_values = sources if isinstance(sources, list) else [evidence_row.get("source")]
                    for source in source_values:
                        if source:
                            source_venues[str(source)].add(venue.venue_id)
        else:
            profile_row = _static_pending_profile(venue, policy)
            statuses["pending"] += 1
            pcl_statuses["pending"] += 1
            evidence_rows.extend(catalog_evidence(venue, policy.cutoff))
            source_venues["jcr_2025"].add(venue.venue_id)
        profiles.append(profile_row)
        metadata = profile_row.get("metadata") if isinstance(profile_row.get("metadata"), Mapping) else {}
        tiers[str(metadata.get("profile_tier") or "unknown")] += 1
        grades[str(metadata.get("evidence_grade") or "unknown")] += 1
        for prototype in profile_row.get("prototypes") or ():
            if isinstance(prototype, Mapping):
                prototype_rows.append({"venue_id": venue.venue_id, **dict(prototype)})
        identity_rows.append(
            {
                "venue_id": venue.venue_id,
                "online_entity_id": venue.online_entity_id,
                "issns": [_canonical_issn(value) for value in venue.issns],
                "status": venue.identity_status,
            }
        )

    profiles.sort(key=lambda row: str(row.get("venue_id") or ""))
    evidence_rows.sort(
        key=lambda row: (str(row.get("venue_id") or ""), str(row.get("evidence_id") or ""))
    )
    prototype_rows.sort(
        key=lambda row: (str(row.get("venue_id") or ""), str(row.get("prototype_id") or ""))
    )
    identity_rows.sort(key=lambda row: str(row.get("venue_id") or ""))
    profiles_path = output_dir / "venue_profiles.train.jsonl"
    evidence_path = output_dir / "evidence.jsonl"
    prototypes_path = output_dir / "prototypes.jsonl"
    identity_path = output_dir / "venue_identity_crosswalk.jsonl"
    lightrag_path = output_dir / "lightrag_custom_kg.json"
    _write_jsonl(profiles_path, profiles)
    _write_jsonl(evidence_path, evidence_rows)
    _write_jsonl(prototypes_path, prototype_rows)
    _write_jsonl(identity_path, identity_rows)

    venue_by_id = {venue.venue_id: venue for venue in venues}
    kg_chunks: list[dict[str, Any]] = []
    kg_entities: list[dict[str, Any]] = []
    kg_relationships: list[dict[str, Any]] = []
    for profile in profiles:
        venue_id = str(profile.get("venue_id") or "")
        venue = venue_by_id[venue_id]
        if venue.online_entity_id is None or venue.identity_status != "exact_issn":
            continue
        venue_name = f"VENUE::{venue.online_entity_id}::{venue.name}"
        venue_source = f"historical-venue:{venue_id}"
        venue_description = " | ".join(
            value for value in (venue.name, venue.subject, venue.quartile) if value
        )
        kg_chunks.append(
            {
                "content": venue_description,
                "source_id": venue_source,
                "file_path": str(profiles_path),
            }
        )
        kg_entities.append(
            {
                "entity_name": venue_name,
                "entity_type": "venue",
                "description": venue_description,
                "source_id": venue_source,
                "file_path": str(profiles_path),
            }
        )
        for prototype in profile.get("prototypes") or ():
            if (
                not isinstance(prototype, Mapping)
                or prototype.get("temporal_eligible", True) is False
            ):
                continue
            prototype_id = str(prototype.get("prototype_id") or "")
            text = str(prototype.get("text") or "").strip()
            if not prototype_id or not text:
                continue
            prototype_name = "PROTOTYPE::" + prototype_id
            source_id = "historical-prototype:" + prototype_id
            kg_chunks.append(
                {"content": text, "source_id": source_id, "file_path": str(profiles_path)}
            )
            kg_entities.append(
                {
                    "entity_name": prototype_name,
                    "entity_type": "venue_prototype",
                    "description": text,
                    "source_id": source_id,
                    "file_path": str(profiles_path),
                }
            )
            kg_relationships.append(
                {
                    "src_id": venue_name,
                    "tgt_id": prototype_name,
                    "description": (
                        "Venue has a frozen topic prototype derived from: "
                        + ", ".join(str(value) for value in prototype.get("source_ids") or ())
                    ),
                    "keywords": "HAS_PROTOTYPE DERIVED_FROM",
                    "weight": float(prototype.get("weight", 1.0)),
                    "source_id": source_id,
                    "file_path": str(profiles_path),
                }
            )
    _atomic_compact_json(
        lightrag_path,
        {
            "chunks": kg_chunks,
            "entities": kg_entities,
            "relationships": kg_relationships,
        },
    )

    history_counts = [
        int((row.get("metadata") or {}).get("history_paper_count") or 0)
        for row in profiles
    ]
    coverage = {
        "catalog_venues": len(venues),
        "collection_status": dict(sorted(statuses.items())),
        "venues_with_history": sum(value > 0 for value in history_counts),
        "venues_with_5_history_papers": sum(value >= 5 for value in history_counts),
        "venues_with_10_history_papers": sum(value >= 10 for value in history_counts),
        "history_coverage": (
            sum(value > 0 for value in history_counts) / len(venues) if venues else 0.0
        ),
        "profile_tiers": dict(sorted(tiers.items())),
        "evidence_grades": dict(sorted(grades.items())),
        "source_venue_coverage": {
            source: len(venue_ids) for source, venue_ids in sorted(source_venues.items())
        },
        "pcl_status": dict(sorted(pcl_statuses.items())),
        "paper_evidence_records": sum(row.get("kind") == "paper" for row in evidence_rows),
        "title_only_paper_records": sum(
            row.get("kind") == "paper" and not str(row.get("abstract") or "").strip()
            for row in evidence_rows
        ),
        "prototype_records": len(prototype_rows),
        "lightrag_mapped_venues": sum(
            venue.online_entity_id is not None and venue.identity_status == "exact_issn"
            for venue in venues
        ),
        "lightrag_entities": len(kg_entities),
        "lightrag_relationships": len(kg_relationships),
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_iso(),
        "purpose": "candidate-side historical venue profiles; evaluation queries are separate",
        "boundaries": {
            "history_start": policy.history_start,
            "cutoff": policy.cutoff,
        },
        "policy": {
            "max_papers_per_venue": policy.max_papers_per_venue,
            "min_papers_before_fallback": policy.min_papers_before_fallback,
            "max_pages_per_source": policy.max_pages_per_source,
            "openalex_mode": policy.openalex_mode,
            "scope_mode": policy.scope_mode,
            "max_prototypes": policy.max_prototypes,
            "abstract_required_for_history": False,
            "identity_mapping": "exact_validated_issn_only",
            "test_gold_priority": False,
        },
        "pcl": dict(provider_identity),
        "coverage": coverage,
        "inputs": {
            "jcr_csv": str(jcr_csv),
            "jcr_csv_sha256": sha256_file(jcr_csv),
        },
        "outputs": {
            "profiles": {"path": profiles_path.name, "sha256": sha256_file(profiles_path)},
            "evidence": {"path": evidence_path.name, "sha256": sha256_file(evidence_path)},
            "prototypes": {"path": prototypes_path.name, "sha256": sha256_file(prototypes_path)},
            "identity_crosswalk": {"path": identity_path.name, "sha256": sha256_file(identity_path)},
            "lightrag_custom_kg": {
                "path": lightrag_path.name,
                "sha256": sha256_file(lightrag_path),
            },
        },
    }
    _atomic_json(output_dir / "manifest.json", manifest)
    return manifest


def stable_collection_queue(venues: Sequence[VenueSeed], seed: str) -> list[VenueSeed]:
    """Gold-independent ordering, interleaved across subject and quartile."""

    buckets: dict[tuple[str, str], list[VenueSeed]] = defaultdict(list)
    for venue in venues:
        buckets[(venue.broad_field, venue.quartile)].append(venue)
    for key, values in buckets.items():
        values.sort(key=lambda venue: (stable_digest(seed, venue.venue_id), venue.venue_id))
    queue: list[VenueSeed] = []
    keys = sorted(buckets)
    while keys:
        next_keys: list[tuple[str, str]] = []
        for key in keys:
            if buckets[key]:
                queue.append(buckets[key].pop(0))
            if buckets[key]:
                next_keys.append(key)
        keys = next_keys
    return queue


def is_pcl_retry_candidate(status: object) -> bool:
    """Return whether an evidence-complete shard is eligible for PCL-only work."""

    normalized = str(status or "").strip()
    return normalized == "queued" or normalized in {
        "invalid_response",
        "ungrounded_response",
    } or normalized.startswith("error:")


def _retryable_pcl_exception(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        # The PCL gateway has returned intermittent 404s for the same endpoint
        # between successful calls.  After model-level failover is exhausted,
        # give that routing failure one queue-level retry instead of making it
        # a permanent venue failure.
        return int(exc.code) == 404 or int(exc.code) in TRANSIENT_HTTP_STATUS
    return isinstance(
        exc,
        (
            TimeoutError,
            urllib.error.URLError,
            ConnectionError,
            OSError,
            json.JSONDecodeError,
        ),
    )


def retry_pcl_shard(
    *,
    venue: VenueSeed,
    shard_path: Path,
    policy: CollectionPolicy,
    pcl: PrototypeSynthesizer,
) -> PCLRetryOutcome:
    """Run exactly one PCL-only attempt using evidence already stored on disk."""

    try:
        shard = json.loads(shard_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return PCLRetryOutcome(
            ok=False,
            status=f"error:{type(exc).__name__}",
            error=f"{type(exc).__name__}: {' '.join(str(exc).split())[:180]}",
            retryable=False,
        )
    evidence = shard.get("evidence") if isinstance(shard, Mapping) else None
    if not isinstance(evidence, list):
        return PCLRetryOutcome(
            ok=False,
            status="error:InvalidShard",
            error="InvalidShard: missing evidence list",
            retryable=False,
        )
    try:
        prototypes, pcl_status = pcl.synthesize(venue, evidence, policy)
    except Exception as exc:  # noqa: BLE001 - classify before the queue decides.
        pcl_status = f"error:{type(exc).__name__}"
        error = f"{type(exc).__name__}: {' '.join(str(exc).split())[:180]}"
        actual_model = str(getattr(pcl, "last_model", pcl.model))
        repaired = finalize_venue_pcl(
            shard,
            venue,
            policy=policy,
            prototypes=(),
            pcl_status=pcl_status,
            pcl_model=actual_model,
            pcl_error=error,
        )
        _atomic_json(shard_path, repaired)
        return PCLRetryOutcome(
            ok=False,
            status=pcl_status,
            error=error,
            retryable=_retryable_pcl_exception(exc),
        )

    if pcl_status == "ok" and prototypes:
        actual_model = str(getattr(pcl, "last_model", pcl.model))
        repaired = finalize_venue_pcl(
            shard,
            venue,
            policy=policy,
            prototypes=prototypes,
            pcl_status="ok",
            pcl_model=actual_model,
        )
        _atomic_json(shard_path, repaired)
        return PCLRetryOutcome(ok=True, status="ok", retryable=False)

    failed_status = pcl_status if pcl_status != "ok" else "ungrounded_response"
    error = f"PCLStatus: {failed_status}"
    actual_model = str(getattr(pcl, "last_model", pcl.model))
    repaired = finalize_venue_pcl(
        shard,
        venue,
        policy=policy,
        prototypes=(),
        pcl_status=failed_status,
        pcl_model=actual_model,
        pcl_error=error,
    )
    _atomic_json(shard_path, repaired)
    return PCLRetryOutcome(
        ok=False,
        status=failed_status,
        error=error,
        retryable=failed_status in {"invalid_response", "ungrounded_response"},
    )


def _exclusive_collection_lock(function):
    """Prevent two corpus collectors from writing one output directory."""

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any):
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        lock_path = output_dir / ".collector.lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                handle.seek(0)
                owner = " ".join(handle.read(240).split()) or "unknown owner"
                raise HistoricalCollectionError(
                    f"historical collector already holds {lock_path}: {owner}"
                ) from exc
            handle.seek(0)
            handle.truncate()
            handle.write(
                json.dumps(
                    {"pid": os.getpid(), "started_at": now_iso()},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
            handle.flush()
            try:
                return function(*args, **kwargs)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    return wrapped


@_exclusive_collection_lock
def run_historical_collection(
    *,
    venues: Sequence[VenueSeed],
    policy: CollectionPolicy,
    output_dir: Path,
    jcr_csv: Path,
    crossref: EvidenceSource | None,
    openalex: EvidenceSource | None,
    scope_search: EvidenceSource | None,
    pcl: PrototypeSynthesizer,
    batch_size: int = 100,
    workers: int = 2,
    max_batches: int = 0,
    smoke_limit: int = 0,
    smoke_venue_id: str = "",
    seed: str = DEFAULT_SEED,
    retry_partial: bool = False,
    pcl_retry_policy: PCLRetryPolicy | None = None,
    retry_pcl_exhausted: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    policy.validate()
    retry_policy = pcl_retry_policy or PCLRetryPolicy()
    try:
        retry_policy.validate()
    except ValueError as exc:
        raise ResearchDataError(str(exc)) from exc
    if not 50 <= batch_size <= 100:
        raise ResearchDataError("batch_size must be between 50 and 100")
    if workers < 1 or max_batches < 0 or not 0 <= smoke_limit <= 10:
        raise ResearchDataError(
            "workers must be positive; max_batches non-negative; smoke_limit within 0..10"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    shard_dir = output_dir / "venues"
    shard_dir.mkdir(parents=True, exist_ok=True)
    queue = stable_collection_queue(venues, seed)
    _write_jsonl(
        output_dir / "queue.jsonl",
        (venue.to_queue_row(index) for index, venue in enumerate(queue, 1)),
    )
    _write_jsonl(
        output_dir / "venue_identity_crosswalk.jsonl",
        (
            {
                "venue_id": venue.venue_id,
                "online_entity_id": venue.online_entity_id,
                "issns": [_canonical_issn(value) for value in venue.issns],
                "status": venue.identity_status,
            }
            for venue in sorted(venues, key=lambda item: item.venue_id)
        ),
    )
    if dry_run:
        pending = len(queue)
        return {
            "status": "dry_run",
            "catalog_venues": len(venues),
            "pending": pending,
            "batch_size": batch_size,
            "total_batches": (pending + batch_size - 1) // batch_size,
            "pcl": dict(pcl.provider_identity),
        }

    def should_process(venue: VenueSeed) -> bool:
        path = shard_dir / f"{venue.venue_id}.json"
        if not path.is_file():
            return True
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return True
        # A runner-level failure may contain catalog evidence only and is not
        # eligible for PCL-only repair.  It must re-enter full acquisition.
        if str(payload.get("status") or "") == "failed" or str(
            payload.get("pcl_status") or ""
        ) == "not_run":
            return True
        if not retry_partial:
            return False
        return str(payload.get("status") or "") != "complete"

    pending_queue = [venue for venue in queue if should_process(venue)]
    recovery_queue: Sequence[VenueSeed] = queue
    action_limit: int | None = None
    if smoke_venue_id:
        target = next(
            (venue for venue in queue if venue.venue_id == smoke_venue_id),
            None,
        )
        if target is None:
            raise ResearchDataError(
                f"smoke venue is absent: {smoke_venue_id}"
            )
        pending_queue = [target] if should_process(target) else []
        recovery_queue = [target]
        action_limit = 1
    if max_batches:
        batch_limit = max_batches * batch_size
        action_limit = (
            min(action_limit, batch_limit) if action_limit is not None else batch_limit
        )
    if smoke_limit:
        action_limit = (
            min(action_limit, smoke_limit) if action_limit is not None else smoke_limit
        )
    if action_limit is not None:
        pending_queue = pending_queue[:action_limit]
        recovery_limit: int | None = max(0, action_limit - len(pending_queue))
    else:
        recovery_limit = None
    stop_path = output_dir / "STOP"
    attempt_path = output_dir / "attempts.jsonl"
    processed_ids = {path.stem for path in shard_dir.glob("jcr-*.json")}
    venue_by_id = {venue.venue_id: venue for venue in venues}

    def handle_pcl(venue_id: str) -> PCLRetryOutcome:
        venue = venue_by_id.get(venue_id)
        if venue is None:
            return PCLRetryOutcome(
                ok=False,
                status="error:UnknownVenue",
                error="UnknownVenue: queue ID is absent from the canonical catalog",
                retryable=False,
            )
        return retry_pcl_shard(
            venue=venue,
            shard_path=shard_dir / f"{venue_id}.json",
            policy=policy,
            pcl=pcl,
        )

    pcl_queue = PCLRetryQueue(output_dir, handle_pcl, retry_policy)
    batches_completed = 0
    attempted = 0
    try:
        pending_ids = {venue.venue_id for venue in pending_queue}
        # Repair old PCL-only failures without touching Crossref/OpenAlex/Tavily.
        # queued shards resume their first pass; historical failures enter pass two.
        recoveries_scheduled = 0
        for venue in recovery_queue:
            if stop_path.exists():
                break
            if (
                recovery_limit is not None
                and recoveries_scheduled >= recovery_limit
            ):
                break
            if venue.venue_id in pending_ids:
                continue
            shard_path = shard_dir / f"{venue.venue_id}.json"
            if not shard_path.is_file():
                continue
            try:
                shard = json.loads(shard_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            pcl_status = str(shard.get("pcl_status") or "")
            if is_pcl_retry_candidate(pcl_status):
                if pcl_queue.enqueue(
                    venue.venue_id,
                    second_pass=pcl_status != "queued",
                    origin=(
                        "existing_failure"
                        if pcl_status != "queued"
                        else "restart_recovery"
                    ),
                    force=retry_pcl_exhausted,
                ):
                    recoveries_scheduled += 1

        for offset in range(0, len(pending_queue), batch_size):
            if stop_path.exists():
                break
            batch = pending_queue[offset : offset + batch_size]
            attempts: list[dict[str, Any]] = []
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(
                        collect_venue_evidence,
                        venue,
                        policy=policy,
                        crossref=crossref,
                        openalex=openalex,
                        scope_search=scope_search,
                        pcl_model=pcl.model,
                    ): venue
                    for venue in batch
                }
                for future in as_completed(futures):
                    venue = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:  # noqa: BLE001 - preserve the rest of the batch.
                        result = {
                            "schema_version": SCHEMA_VERSION,
                            "venue": venue.to_queue_row(0),
                            "status": "failed",
                            "collected_at": now_iso(),
                            "evidence": catalog_evidence(venue, policy.cutoff),
                            "profile": _static_pending_profile(venue, policy),
                            "source_errors": {
                                "runner": f"{type(exc).__name__}: {' '.join(str(exc).split())[:180]}"
                            },
                            "pcl_status": "not_run",
                        }
                        result["profile"]["metadata"]["collection_status"] = "failed"
                    shard_path = shard_dir / f"{venue.venue_id}.json"
                    _atomic_json(shard_path, result)
                    processed_ids.add(venue.venue_id)
                    if result.get("pcl_status") == "queued":
                        pcl_queue.enqueue(
                            venue.venue_id,
                            origin="new_evidence",
                            force=True,
                        )
                    attempts.append(
                        {
                            "attempted_at": now_iso(),
                            "venue_id": venue.venue_id,
                            "status": result.get("status"),
                            "pcl_status": result.get("pcl_status"),
                            "source_errors": result.get("source_errors") or {},
                        }
                    )
                    attempted += 1
            _append_jsonl(
                attempt_path,
                sorted(attempts, key=lambda row: str(row["venue_id"])),
            )
            batches_completed += 1
            pcl_queue.raise_if_failed()
            processed_shards = len(processed_ids)
            _atomic_json(
                output_dir / "runner_state.json",
                {
                    "status": "stopped" if stop_path.exists() else "running",
                    "updated_at": now_iso(),
                    "batches_completed_this_run": batches_completed,
                    "attempted_this_run": attempted,
                    "remaining_after_selection": max(0, len(pending_queue) - attempted),
                    "processed_catalog_venues": processed_shards,
                    "remaining_catalog_venues": len(venues) - processed_shards,
                    "pcl_retry": pcl_queue.snapshot(),
                },
            )
    except BaseException as exc:
        try:
            pcl_queue.close(drain=False)
        except BaseException:
            # Preserve the acquisition exception; queue/shard logs remain the
            # restart source of truth even if shutdown persistence also fails.
            pass
        if isinstance(exc, RuntimeError) and str(exc).startswith("PCL retry worker"):
            raise HistoricalCollectionError(str(exc)) from exc
        raise

    try:
        queue_state = pcl_queue.close(drain=not stop_path.exists())
    except RuntimeError as exc:
        raise HistoricalCollectionError(str(exc)) from exc
    manifest = assemble_historical_corpus(
        venues=venues,
        policy=policy,
        output_dir=output_dir,
        jcr_csv=jcr_csv,
        provider_identity=pcl.provider_identity,
    )
    remaining = sum(
        not (shard_dir / f"{venue.venue_id}.json").is_file() for venue in venues
    )
    pcl_unresolved = 0
    for venue in venues:
        shard_path = shard_dir / f"{venue.venue_id}.json"
        if not shard_path.is_file():
            continue
        try:
            shard = json.loads(shard_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pcl_unresolved += 1
            continue
        if str(shard.get("pcl_status") or "") != "ok":
            pcl_unresolved += 1
    final_status = (
        "stopped"
        if stop_path.exists()
        else "complete"
        if not remaining and not pcl_unresolved
        else "partial"
    )
    _atomic_json(
        output_dir / "runner_state.json",
        {
            "status": final_status,
            "updated_at": now_iso(),
            "batches_completed_this_run": batches_completed,
            "attempted_this_run": attempted,
            "remaining_catalog_venues": remaining,
            "pcl_unresolved": pcl_unresolved,
            "pcl_retry": queue_state,
            "coverage": manifest["coverage"],
        },
    )
    return manifest
