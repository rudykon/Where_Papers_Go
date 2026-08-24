"""Build a temporal paper/venue corpus from existing Crossref cache files.

There is deliberately no HTTP client in this module.  Missing cache data is a
reported coverage limitation, never a reason to contact Crossref.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from html.parser import HTMLParser
import html
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .data import (
    ResearchDataError,
    _valid_issn_token,
    load_jcr_corpus,
    normalize_doi,
    parse_iso_date,
    sha256_file,
)


NOTICE_TITLE_RE = re.compile(
    r"^(?:author correction|publisher correction|correction(?:\s+to)?|corrigendum|"
    r"erratum|retraction(?:\s+notice)?|expression of concern|editorial|"
    r"letter to (?:the )?editor|reply to|response to|comment on|addendum)\b",
    re.I,
)
NOTICE_RELATIONS = {
    "is-correction-of",
    "is-retraction-of",
    "is-update-of",
    "is-expression-of-concern-for",
}


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
    if not isinstance(value, str) or not value.strip():
        return ""
    parser = _JatsTextExtractor()
    try:
        parser.feed(value)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", value)
    text = " ".join(html.unescape(text).split())
    return re.sub(r"^(?:abstract|summary)\s*[:.—-]?\s*", "", text, flags=re.I)


def _first_text(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    return " ".join(str(value or "").split())


def _date_from_parts(value: Any) -> tuple[date, str] | None:
    if not isinstance(value, Mapping):
        return None
    parts_list = value.get("date-parts")
    if not isinstance(parts_list, list) or not parts_list or not isinstance(parts_list[0], list):
        return None
    parts = parts_list[0]
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        day = int(parts[2]) if len(parts) > 2 else 1
        precision = "day" if len(parts) > 2 else "month" if len(parts) > 1 else "year"
        return date(year, month, day), precision
    except (IndexError, TypeError, ValueError):
        return None


def crossref_publication_date(item: Mapping[str, Any]) -> tuple[date, str] | None:
    dates = [
        parsed
        for key in ("published-online", "published-print", "published", "issued")
        if (parsed := _date_from_parts(item.get(key))) is not None
    ]
    precision_order = {"day": 0, "month": 1, "year": 2}
    return min(dates, key=lambda value: (precision_order[value[1]], value[0])) if dates else None


def _item_issns(item: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[Any] = []
    raw = item.get("ISSN")
    values.extend(raw if isinstance(raw, list) else [raw])
    typed = item.get("issn-type")
    if isinstance(typed, list):
        values.extend(
            entry.get("value") for entry in typed if isinstance(entry, Mapping)
        )
    return tuple(sorted({_valid_issn_token(value) for value in values} - {""}))


def _split_name(
    published: date, *, start: date | None, train_end: date, dev_end: date, test_end: date
) -> str:
    if start and published < start:
        return "excluded"
    if published <= train_end:
        return "train"
    if published <= dev_end:
        return "validation"
    if published <= test_end:
        return "test"
    return "excluded"


def build_cached_corpus(
    *,
    cache_dir: Path,
    jcr_csv: Path,
    output_dir: Path,
    train_end: str,
    dev_end: str,
    test_end: str,
    start: str | None = None,
    min_abstract_chars: int = 100,
    max_train_papers_per_venue: int = 50,
) -> dict[str, Any]:
    """Convert cached Crossref responses into deduplicated temporal JSONL.

    Returns the written manifest.  Venue profiles contain train papers only;
    validation and test content can never enter retrieval documents.
    """

    if min_abstract_chars < 1 or max_train_papers_per_venue < 1:
        raise ValueError("minimum abstract length and profile cap must be positive")
    train_date = parse_iso_date(train_end, field_name="train_end")
    dev_date = parse_iso_date(dev_end, field_name="dev_end")
    test_date = parse_iso_date(test_end, field_name="test_end")
    start_date = parse_iso_date(start, field_name="start") if start else None
    if not train_date < dev_date < test_date:
        raise ResearchDataError("boundaries must satisfy train_end < dev_end < test_end")

    catalog = load_jcr_corpus(
        jcr_csv,
        snapshot_date=str(train_date),
        # Do not consume any scope field from the mutable production catalog.
        # Only the frozen 2025 identity/category metadata is allowed here.
        text_fields=("name", "area", "area_en"),
    )
    issn_owners: dict[str, set[str]] = defaultdict(set)
    catalog_by_id = {document.doc_id: document for document in catalog}
    for document in catalog:
        for key in ("issn", "eissn"):
            token = _valid_issn_token(document.metadata.get(key))
            if token:
                issn_owners[token].add(document.doc_id)

    rejection_counts: Counter[str] = Counter()
    by_doi: dict[str, dict[str, Any]] = {}
    cache_paths = sorted(cache_dir.glob("*.json"))
    if not cache_paths:
        raise ResearchDataError(f"no cached Crossref JSON files found under {cache_dir}")
    cache_bytes = 0
    raw_items = 0
    for cache_path in cache_paths:
        cache_bytes += cache_path.stat().st_size
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            rejection_counts["invalid_cache_file"] += 1
            continue
        message = payload.get("message") if isinstance(payload, Mapping) else None
        items = message.get("items") if isinstance(message, Mapping) else None
        if not isinstance(items, list):
            rejection_counts["missing_items"] += 1
            continue
        for item in items:
            if not isinstance(item, Mapping):
                rejection_counts["invalid_item"] += 1
                continue
            raw_items += 1
            title = jats_to_text(_first_text(item.get("title")))
            if not title:
                rejection_counts["missing_title"] += 1
                continue
            relation = item.get("relation")
            if (
                str(item.get("type") or "") != "journal-article"
                or NOTICE_TITLE_RE.search(title)
                or item.get("update-to")
                or (isinstance(relation, Mapping) and NOTICE_RELATIONS.intersection(relation))
            ):
                rejection_counts["non_article_notice"] += 1
                continue
            abstract = jats_to_text(item.get("abstract"))
            if len(abstract) < min_abstract_chars:
                rejection_counts["short_abstract"] += 1
                continue
            published_info = crossref_publication_date(item)
            if published_info is None or published_info[1] == "year":
                rejection_counts["missing_or_imprecise_date"] += 1
                continue
            published, precision = published_info
            split = _split_name(
                published,
                start=start_date,
                train_end=train_date,
                dev_end=dev_date,
                test_end=test_date,
            )
            if split == "excluded":
                rejection_counts["outside_date_window"] += 1
                continue
            owners = {
                owner
                for token in _item_issns(item)
                for owner in issn_owners.get(token, ())
            }
            if len(owners) != 1:
                rejection_counts["ambiguous_issn" if owners else "unmapped_issn"] += 1
                continue
            venue_id = next(iter(owners))
            doi = normalize_doi(item.get("DOI"))
            if not doi:
                rejection_counts["missing_doi"] += 1
                continue
            venue = catalog_by_id[venue_id]
            record = {
                "paper_id": "doi:" + doi,
                "doi": doi,
                "title": title,
                "abstract": abstract,
                "publication_date": published.isoformat(),
                "publication_date_precision": precision,
                "split": split,
                "gold_journal_id": venue_id,
                "gold_journal_name": venue.name,
                "gold_jcr_quartile": venue.metadata.get("level") or "unknown",
                "language": str(item.get("language") or ""),
                "article_type": "journal-article",
                "source": "crossref_cache",
                "source_url": "https://doi.org/" + doi,
            }
            previous = by_doi.get(doi)
            if previous is None or len(abstract) > len(str(previous.get("abstract") or "")):
                by_doi[doi] = record
            else:
                rejection_counts["duplicate_doi"] += 1

    records = sorted(
        by_doi.values(), key=lambda row: (row["publication_date"], row["doi"])
    )
    split_counts = Counter(str(record["split"]) for record in records)
    train_by_venue: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record["split"] == "train":
            train_by_venue[str(record["gold_journal_id"])].append(record)
    profiles: list[dict[str, Any]] = []
    venues_with_train_history = 0
    # Keep the exact full Q1--Q4 candidate universe.  A venue without cached
    # train papers receives a static metadata profile instead of disappearing.
    for venue_id in sorted(catalog_by_id):
        papers = sorted(
            train_by_venue.get(venue_id, ()),
            key=lambda row: (row["publication_date"], row["doi"]),
            reverse=True,
        )[:max_train_papers_per_venue]
        venue = catalog_by_id[venue_id]
        if papers:
            venues_with_train_history += 1
        paper_text = "\n\n".join(
            f"{paper['title']}. {paper['abstract']}" for paper in papers
        )
        profile_text = venue.text + (("\n\n" + paper_text) if paper_text else "")
        profiles.append(
            {
                "venue_id": venue_id,
                "name": venue.name,
                "profile_text": profile_text,
                "snapshot_date": train_end,
                "metadata": {
                    "content_origin": "frozen_metadata_plus_historical_train_papers",
                    "source_dois": [paper["doi"] for paper in papers],
                    "source_titles": [paper["title"] for paper in papers],
                    "source_max_date": (
                        max(paper["publication_date"] for paper in papers) if papers else ""
                    ),
                    "paper_count": len(papers),
                },
            }
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    papers_path = output_dir / "papers.jsonl"
    profiles_path = output_dir / "venue_profiles.train.jsonl"
    with papers_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    with profiles_path.open("w", encoding="utf-8", newline="\n") as handle:
        for profile in profiles:
            handle.write(json.dumps(profile, ensure_ascii=False, sort_keys=True) + "\n")
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "offline_only": True,
        "source": {
            "cache_dir": str(cache_dir),
            "cache_file_count": len(cache_paths),
            "cache_total_bytes": cache_bytes,
            "jcr_csv": str(jcr_csv),
            "jcr_csv_sha256": sha256_file(jcr_csv),
        },
        "boundaries": {
            "start": start,
            "train_end": train_end,
            "dev_end": dev_end,
            "test_end": test_end,
        },
        "configuration": {
            "min_abstract_chars": min_abstract_chars,
            "max_train_papers_per_venue": max_train_papers_per_venue,
            "identity_mapping": "exact_validated_issn",
            "deduplication": "normalized_doi_keep_longest_abstract",
        },
        "coverage": {
            "raw_crossref_items": raw_items,
            "accepted_unique_dois": len(records),
            "split_counts": dict(sorted(split_counts.items())),
            "train_profile_venues": len(profiles),
            "venues_with_train_history": venues_with_train_history,
            "train_history_coverage": (
                venues_with_train_history / len(profiles) if profiles else 0.0
            ),
        },
        "rejections": dict(sorted(rejection_counts.items())),
        "outputs": {
            "papers": {"path": papers_path.name, "sha256": sha256_file(papers_path)},
            "train_profiles": {
                "path": profiles_path.name,
                "sha256": sha256_file(profiles_path),
            },
        },
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest
