"""Leakage audit for temporal venue-retrieval experiments."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
import hashlib
import re
from typing import Any, Mapping, Sequence

from .data import DatasetBundle, TemporalSplit, normalize_doi, normalize_text, parse_iso_date
from .types import Query, VenueDocument


DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", re.I)


def _content_hash(query: Query) -> str:
    return hashlib.sha256(normalize_text(query.text).encode("utf-8")).hexdigest()


def _distinctive_title(value: str) -> bool:
    """Reject generic headings such as Introduction or Issue Information."""

    normalized = normalize_text(value)
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", normalized))
    return len(normalized) >= 24 and (len(normalized.split()) >= 4 or cjk_count >= 8)


def audit_leakage(
    bundle: DatasetBundle,
    corpus: Sequence[VenueDocument],
    split: TemporalSplit,
    *,
    evaluation_splits: Sequence[str] = ("validation", "test"),
) -> dict[str, Any]:
    """Audit direct identity, temporal, duplicate, and label-in-query leakage."""

    query_by_id = {query.query_id: query for query in bundle.queries}
    split_map: dict[str, str] = {}
    for split_name, query_ids in split.as_dict().items():
        for query_id in query_ids:
            split_map[query_id] = split_name
    findings: list[dict[str, Any]] = []

    def add(kind: str, severity: str, **details: Any) -> None:
        findings.append({"kind": kind, "severity": severity, **details})

    # Cross-split duplicates can make a test paper effectively part of train.
    for field_name, key_function in (
        ("doi", lambda query: query.doi),
        ("title", lambda query: normalize_text(query.title)),
        ("content", _content_hash),
    ):
        owners: dict[str, list[str]] = defaultdict(list)
        for query in bundle.queries:
            value = key_function(query)
            if value:
                owners[str(value)].append(query.query_id)
        for value, query_ids in owners.items():
            owner_splits = {split_map.get(query_id, "excluded") for query_id in query_ids}
            if len(owner_splits) > 1:
                severity = (
                    "warning"
                    if field_name == "title" and not _distinctive_title(value)
                    else "critical"
                )
                add(
                    "cross_split_duplicate_" + field_name,
                    severity,
                    value=value,
                    query_ids=query_ids,
                    splits=sorted(owner_splits),
                )

    target_query_ids = [
        query_id
        for split_name in evaluation_splits
        for query_id in getattr(split, split_name)
    ]
    target_queries = [query_by_id[query_id] for query_id in target_query_ids]
    if target_queries:
        earliest_target = min(
            parse_iso_date(query.publication_date, field_name="publication date")
            for query in target_queries
        )
    else:
        earliest_target = date.max

    corpus_dois: set[str] = set()
    corpus_titles: set[str] = set()
    corpus_hashes: set[str] = set()
    source_query_ids: set[str] = set()
    missing_snapshot = 0
    postdated_documents: list[str] = []
    postdated_sources: list[str] = []
    for document in corpus:
        if not document.snapshot_date:
            missing_snapshot += 1
        else:
            snapshot = parse_iso_date(document.snapshot_date, field_name="corpus snapshot date")
            if snapshot >= earliest_target:
                postdated_documents.append(document.doc_id)
        metadata = document.metadata
        source_max_dates = [str(metadata.get("source_max_date") or "").strip()[:10]]
        prototypes = metadata.get("prototypes")
        if isinstance(prototypes, Sequence) and not isinstance(prototypes, (str, bytes)):
            for prototype in prototypes:
                if not isinstance(prototype, Mapping):
                    continue
                # Current production scope may coexist in schema v2, but it is
                # not part of the frozen evaluation text or prototype index.
                if prototype.get("temporal_eligible", True) is False:
                    continue
                source_max_dates.append(
                    str(prototype.get("source_max_date") or "").strip()[:10]
                )
                for source_id in prototype.get("source_ids") or ():
                    value = str(source_id or "")
                    if value.startswith("doi:") and normalize_doi(value[4:]):
                        corpus_dois.add(normalize_doi(value[4:]))
        for source_max_date in source_max_dates:
            if not source_max_date:
                continue
            source_date = parse_iso_date(source_max_date, field_name="corpus source_max_date")
            if source_date >= earliest_target:
                postdated_sources.append(document.doc_id)
            if document.snapshot_date and source_date > parse_iso_date(
                document.snapshot_date, field_name="corpus snapshot date"
            ):
                postdated_sources.append(document.doc_id)
        doi_values = metadata.get("source_dois") or [metadata.get("source_doi")]
        if isinstance(doi_values, str):
            doi_values = [doi_values]
        for value in doi_values if isinstance(doi_values, Sequence) else ():
            if normalized := normalize_doi(value):
                corpus_dois.add(normalized)
        title_values = metadata.get("source_titles") or [metadata.get("source_title")]
        if isinstance(title_values, str):
            title_values = [title_values]
        for value in title_values if isinstance(title_values, Sequence) else ():
            if normalized := normalize_text(value):
                corpus_titles.add(normalized)
        hash_values = metadata.get("content_sha256") or metadata.get("source_content_sha256")
        if isinstance(hash_values, str) and hash_values:
            corpus_hashes.add(hash_values)
        query_values = metadata.get("source_query_ids") or [metadata.get("source_query_id")]
        if isinstance(query_values, str):
            query_values = [query_values]
        for value in query_values if isinstance(query_values, Sequence) else ():
            if value:
                source_query_ids.add(str(value))
        corpus_dois.update(normalize_doi(value) for value in DOI_RE.findall(document.text))

    if missing_snapshot:
        add("missing_corpus_snapshot", "critical", document_count=missing_snapshot)
    if postdated_documents:
        add(
            "corpus_snapshot_not_before_evaluation",
            "critical",
            earliest_evaluation_date=earliest_target.isoformat(),
            document_count=len(postdated_documents),
            examples=postdated_documents[:20],
        )
    if postdated_sources:
        unique_sources = sorted(set(postdated_sources))
        add(
            "corpus_source_content_postdates_boundary",
            "critical",
            earliest_evaluation_date=earliest_target.isoformat(),
            document_count=len(unique_sources),
            examples=unique_sources[:20],
        )

    direct_counts: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    for query in target_queries:
        checks = {
            "doi": bool(query.doi and query.doi in corpus_dois),
            "title": bool(
                _distinctive_title(query.title)
                and normalize_text(query.title) in corpus_titles
            ),
            "content_hash": _content_hash(query) in corpus_hashes,
            "source_query_id": query.query_id in source_query_ids,
        }
        for kind, matched in checks.items():
            if matched:
                direct_counts[kind] += 1
                if len(examples[kind]) < 20:
                    examples[kind].append(query.query_id)
        gold = normalize_text(query.gold_venue_name)
        query_text = normalize_text(query.text)
        if len(gold) >= 8 and gold in query_text:
            add(
                "gold_venue_mentioned_in_query",
                "warning",
                query_id=query.query_id,
                gold_venue_name=query.gold_venue_name,
            )
    for kind, count in direct_counts.items():
        add(
            "evaluation_identity_in_corpus_" + kind,
            "critical",
            query_count=count,
            examples=examples[kind],
        )

    severity_counts = Counter(finding["severity"] for finding in findings)
    return {
        "schema_version": 1,
        "audited_evaluation_splits": list(evaluation_splits),
        "audited_query_count": len(target_queries),
        "corpus_document_count": len(corpus),
        "passed": severity_counts["critical"] == 0,
        "severity_counts": dict(sorted(severity_counts.items())),
        "findings": findings,
        "notes": [
            "Candidate venue names and frozen scope/category text are permitted inputs, not label leakage.",
            "The audit cannot prove that a pretrained embedding model never saw a paper; imported runs must disclose model/version and training cutoff separately.",
        ],
    }


def identity_unsafe_query_ids(audit: Mapping[str, Any]) -> tuple[str, ...]:
    """Return queries with an explicit gold-venue identity cue.

    This drives a conservative sensitivity analysis.  It never changes the
    full-test denominator or the audit pass/fail decision.
    """

    query_ids = {
        str(finding.get("query_id"))
        for finding in audit.get("findings", ())
        if isinstance(finding, Mapping)
        and finding.get("kind") == "gold_venue_mentioned_in_query"
        and finding.get("query_id")
    }
    return tuple(sorted(query_ids))
