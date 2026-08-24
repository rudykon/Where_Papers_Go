"""Leakage audit for temporal venue-retrieval experiments."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
import hashlib
import math
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from .data import DatasetBundle, TemporalSplit, normalize_doi, normalize_text, parse_iso_date
from .types import Query, VenueDocument


DOI_RE = re.compile(r"10\.\d{1,9}/[-._;()/:a-z0-9]+", re.I)
ARXIV_RE = re.compile(r"(?:arxiv\s*:\s*)?(\d{4}\.\d{4,5})(?:v\d+)?", re.I)

AbstractSimilarityHook = Callable[[str, str], float]
PublicationVersionHook = Callable[[Query, Mapping[str, Any]], Iterable[str]]

_CORPUS_VIEWS = frozenset({"document", "metadata_sources", "prototypes"})


def _content_hash(query: Query) -> str:
    return hashlib.sha256(normalize_text(query.text).encode("utf-8")).hexdigest()


def _record_dois(
    value: object,
    dois: set[str],
    version_tokens: set[str],
) -> None:
    """Extract DOI identities even when embedded in typed evidence IDs."""

    raw = str(value or "")
    matches = DOI_RE.findall(raw)
    direct = normalize_doi(raw)
    # Preserve support for synthetic/local DOI-like fixtures while avoiding
    # treating a typed evidence ID as one giant DOI.
    if (
        not matches
        and direct.startswith("10.")
        and "/" in direct
        and ":doi:" not in direct
    ):
        matches = [direct]
    for match in matches:
        normalized = normalize_doi(match)
        if not normalized:
            continue
        dois.add(normalized)
        if parsed := _version_token(normalized):
            version_tokens.add(parsed[0])


def _distinctive_title(value: str) -> bool:
    """Reject generic headings such as Introduction or Issue Information."""

    normalized = normalize_text(value)
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", normalized))
    return len(normalized) >= 24 and (len(normalized.split()) >= 4 or cjk_count >= 8)


def _abstract_shingles(value: str) -> frozenset[str]:
    normalized = normalize_text(value)
    tokens = normalized.split()
    if len(tokens) >= 12:
        return frozenset(
            "w:" + " ".join(tokens[index : index + 5])
            for index in range(len(tokens) - 4)
        )
    compact = "".join(tokens)
    if len(compact) >= 80:
        return frozenset(
            "c:" + compact[index : index + 18]
            for index in range(len(compact) - 17)
        )
    return frozenset()


def _default_abstract_similarity(left: str, right: str) -> float:
    left_shingles = _abstract_shingles(left)
    right_shingles = _abstract_shingles(right)
    if not left_shingles or not right_shingles:
        return 0.0
    overlap = len(left_shingles & right_shingles)
    return max(
        overlap / len(left_shingles | right_shingles),
        overlap / min(len(left_shingles), len(right_shingles)),
    )


_VERSION_FIELDS = {
    "arxiv_id",
    "doi",
    "is_preprint_of",
    "is_version_of",
    "journal_doi",
    "preprint_doi",
    "publication_version_ids",
    "published_as",
    "related_doi",
    "related_dois",
    "relation",
    "relations",
    "source_version_ids",
    "version_of",
}


def _flatten_version_values(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for nested in value.values():
            yield from _flatten_version_values(nested)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for nested in value:
            yield from _flatten_version_values(nested)
    elif value not in (None, ""):
        yield str(value)


def _version_token(value: str) -> tuple[str, bool] | None:
    raw = str(value or "").strip()
    doi_match = DOI_RE.search(raw)
    doi = normalize_doi(doi_match.group(0) if doi_match else raw)
    if doi.startswith("10.") and "/" in doi:
        root = re.sub(r"(?:[._-](?:v|version)\d+)$", "", doi, flags=re.I)
        return "doi-version:" + root, root != doi
    arxiv_match = ARXIV_RE.search(raw)
    if arxiv_match:
        explicit = bool(re.search(r"v\d+", raw, re.I))
        return "arxiv-version:" + arxiv_match.group(1).casefold(), explicit
    return None


def _publication_version_tokens(
    query: Query,
    row: Mapping[str, Any],
    hook: PublicationVersionHook | None,
) -> dict[str, bool]:
    values: list[tuple[str, bool]] = []
    if query.doi:
        values.append((query.doi, False))
    for key, value in row.items():
        normalized_key = str(key).casefold()
        if normalized_key in _VERSION_FIELDS and normalized_key != "doi":
            values.extend((item, True) for item in _flatten_version_values(value))
    if hook is not None:
        values.extend((str(item), True) for item in hook(query, row))
    tokens: dict[str, bool] = {}
    for value, relation_explicit in values:
        parsed = _version_token(value)
        if parsed is None:
            continue
        token, marker_explicit = parsed
        tokens[token] = tokens.get(token, False) or relation_explicit or marker_explicit
    return tokens


def audit_leakage(
    bundle: DatasetBundle,
    corpus: Sequence[VenueDocument],
    split: TemporalSplit,
    *,
    evaluation_splits: Sequence[str] = ("validation", "test"),
    corpus_views: Sequence[str] = (
        "document",
        "metadata_sources",
        "prototypes",
    ),
    abstract_near_duplicate_threshold: float = 0.9,
    abstract_similarity_hook: AbstractSimilarityHook | None = None,
    publication_version_hook: PublicationVersionHook | None = None,
) -> dict[str, Any]:
    """Audit identity, temporal, near-duplicate, and publication-version leakage."""

    normalized_corpus_views = tuple(dict.fromkeys(str(value) for value in corpus_views))
    unknown_corpus_views = set(normalized_corpus_views) - _CORPUS_VIEWS
    if not normalized_corpus_views or unknown_corpus_views:
        raise ValueError(
            "corpus_views must be a non-empty subset of "
            f"{sorted(_CORPUS_VIEWS)}; unknown={sorted(unknown_corpus_views)}"
        )
    if not 0.0 < abstract_near_duplicate_threshold <= 1.0:
        raise ValueError("abstract near-duplicate threshold must be in (0, 1]")

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

    audited_target_ids = {
        query_id
        for split_name in evaluation_splits
        for query_id in getattr(split, split_name)
    }

    # Candidate generation is deliberately conservative and deterministic.
    # A true high-overlap pair shares many rare shingles, so selecting the 20
    # rarest shingles per abstract avoids a quadratic default audit.  A custom
    # hook opts into exhaustive cross-split pairs for future sealed datasets.
    abstract_queries = [
        query
        for query in bundle.queries
        if (
            bool(query.abstract.strip())
            if abstract_similarity_hook is not None
            else bool(_abstract_shingles(query.abstract))
        )
    ]
    abstract_pairs: set[tuple[str, str]] = set()
    if abstract_similarity_hook is not None:
        for left_index, left in enumerate(abstract_queries):
            for right in abstract_queries[left_index + 1 :]:
                if split_map.get(left.query_id) == split_map.get(right.query_id):
                    continue
                if not ({left.query_id, right.query_id} & audited_target_ids):
                    continue
                abstract_pairs.add(tuple(sorted((left.query_id, right.query_id))))
    else:
        shingles = {
            query.query_id: _abstract_shingles(query.abstract)
            for query in abstract_queries
        }
        frequencies = Counter(
            shingle for values in shingles.values() for shingle in values
        )
        owners: dict[str, list[str]] = defaultdict(list)
        for query_id, values in shingles.items():
            for shingle in sorted(values, key=lambda item: (frequencies[item], item))[
                :20
            ]:
                if frequencies[shingle] <= 100:
                    owners[shingle].append(query_id)
        for query_ids in owners.values():
            for left_index, left_id in enumerate(query_ids):
                for right_id in query_ids[left_index + 1 :]:
                    if split_map.get(left_id) == split_map.get(right_id):
                        continue
                    if not ({left_id, right_id} & audited_target_ids):
                        continue
                    abstract_pairs.add(tuple(sorted((left_id, right_id))))

    near_duplicate_examples: list[dict[str, Any]] = []
    near_duplicate_count = 0
    similarity = abstract_similarity_hook or _default_abstract_similarity
    for left_id, right_id in sorted(abstract_pairs):
        score = float(
            similarity(
                query_by_id[left_id].abstract,
                query_by_id[right_id].abstract,
            )
        )
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise ValueError("abstract similarity hook must return a finite value in [0, 1]")
        if score < abstract_near_duplicate_threshold:
            continue
        near_duplicate_count += 1
        if len(near_duplicate_examples) < 20:
            near_duplicate_examples.append(
                {
                    "query_ids": [left_id, right_id],
                    "splits": sorted(
                        {split_map.get(left_id), split_map.get(right_id)}
                    ),
                    "similarity": score,
                }
            )
    if near_duplicate_count:
        add(
            "cross_split_near_duplicate_abstract",
            "critical",
            pair_count=near_duplicate_count,
            threshold=abstract_near_duplicate_threshold,
            examples=near_duplicate_examples,
        )

    version_tokens_by_query = {
        query.query_id: _publication_version_tokens(
            query,
            bundle.source_rows.get(query.query_id, {}),
            publication_version_hook,
        )
        for query in bundle.queries
    }
    version_owners: dict[str, list[tuple[str, bool]]] = defaultdict(list)
    for query_id, tokens in version_tokens_by_query.items():
        for token, explicit in tokens.items():
            version_owners[token].append((query_id, explicit))
    version_examples: list[dict[str, Any]] = []
    version_group_count = 0
    for token, owners in sorted(version_owners.items()):
        owner_ids = sorted({query_id for query_id, _explicit in owners})
        owner_splits = {split_map.get(query_id, "excluded") for query_id in owner_ids}
        if len(owner_splits) <= 1 or not (set(owner_ids) & audited_target_ids):
            continue
        distinct_dois = {query_by_id[query_id].doi for query_id in owner_ids}
        if len(distinct_dois) <= 1 and not any(explicit for _query_id, explicit in owners):
            continue
        version_group_count += 1
        if len(version_examples) < 20:
            version_examples.append(
                {
                    "version_identity": token,
                    "query_ids": owner_ids,
                    "splits": sorted(owner_splits),
                }
            )
    if version_group_count:
        add(
            "cross_split_publication_version",
            "critical",
            identity_count=version_group_count,
            examples=version_examples,
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
    corpus_version_tokens: set[str] = set()
    corpus_titles: set[str] = set()
    corpus_texts: set[str] = set()
    corpus_hashes: set[str] = set()
    source_query_ids: set[str] = set()
    unindexed_metadata_dois: set[str] = set()
    unindexed_metadata_titles: set[str] = set()
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
        source_max_dates: list[str] = []
        if "document" in normalized_corpus_views:
            normalized_document_text = normalize_text(document.text)
            if normalized_document_text:
                corpus_texts.add(normalized_document_text)
            for value in DOI_RE.findall(document.text):
                _record_dois(value, corpus_dois, corpus_version_tokens)
        if "metadata_sources" in normalized_corpus_views:
            source_max_dates.append(
                str(metadata.get("source_max_date") or "").strip()[:10]
            )
        prototypes = metadata.get("prototypes")
        active_prototype_text_found = False
        if isinstance(prototypes, Sequence) and not isinstance(prototypes, (str, bytes)):
            for prototype in prototypes:
                if not isinstance(prototype, Mapping):
                    continue
                # Current production scope may coexist in schema v2, but it is
                # not part of the frozen evaluation text or prototype index.
                if prototype.get("temporal_eligible", True) is False:
                    continue
                if "prototypes" not in normalized_corpus_views:
                    continue
                prototype_text = normalize_text(prototype.get("text"))
                if not prototype_text:
                    continue
                active_prototype_text_found = True
                corpus_texts.add(prototype_text)
                if label := normalize_text(prototype.get("label")):
                    corpus_titles.add(label)
                source_max_dates.append(
                    str(prototype.get("source_max_date") or "").strip()[:10]
                )
                for source_id in prototype.get("source_ids") or ():
                    _record_dois(source_id, corpus_dois, corpus_version_tokens)
                    source_id_text = str(source_id or "").strip()
                    if source_id_text:
                        source_query_ids.add(source_id_text)
        # Prototype-aware retrievers fall back to document.text when a venue
        # has no eligible prototype; mirror that exact behavior in the audit.
        if "prototypes" in normalized_corpus_views and not active_prototype_text_found:
            normalized_document_text = normalize_text(document.text)
            if normalized_document_text:
                corpus_texts.add(normalized_document_text)
            for value in DOI_RE.findall(document.text):
                _record_dois(value, corpus_dois, corpus_version_tokens)
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
                if "metadata_sources" in normalized_corpus_views:
                    _record_dois(normalized, corpus_dois, corpus_version_tokens)
                else:
                    unindexed_metadata_dois.add(normalized)
        title_values = metadata.get("source_titles") or [metadata.get("source_title")]
        if isinstance(title_values, str):
            title_values = [title_values]
        for value in title_values if isinstance(title_values, Sequence) else ():
            if normalized := normalize_text(value):
                if "metadata_sources" in normalized_corpus_views:
                    corpus_titles.add(normalized)
                else:
                    unindexed_metadata_titles.add(normalized)
        hash_values = metadata.get("content_sha256") or metadata.get("source_content_sha256")
        if (
            "metadata_sources" in normalized_corpus_views
            and isinstance(hash_values, str)
            and hash_values
        ):
            corpus_hashes.add(hash_values)
        query_values = metadata.get("source_query_ids") or [metadata.get("source_query_id")]
        if isinstance(query_values, str):
            query_values = [query_values]
        for value in query_values if isinstance(query_values, Sequence) else ():
            if value and "metadata_sources" in normalized_corpus_views:
                source_query_ids.add(str(value))

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
    unindexed_counts: Counter[str] = Counter()
    unindexed_examples: dict[str, list[str]] = defaultdict(list)
    for query in target_queries:
        normalized_title = normalize_text(query.title)
        title_in_effective_text = bool(
            _distinctive_title(query.title)
            and any(normalized_title in text for text in corpus_texts)
        )
        version_match = bool(
            query.doi not in corpus_dois
            and set(version_tokens_by_query.get(query.query_id, {}))
            & corpus_version_tokens
        )
        checks = {
            "doi": bool(query.doi and query.doi in corpus_dois),
            "title": bool(
                _distinctive_title(query.title)
                and (normalized_title in corpus_titles or title_in_effective_text)
            ),
            "content_hash": _content_hash(query) in corpus_hashes,
            "source_query_id": query.query_id in source_query_ids,
            "publication_version": version_match,
        }
        for kind, matched in checks.items():
            if matched:
                direct_counts[kind] += 1
                if len(examples[kind]) < 20:
                    examples[kind].append(query.query_id)
        inactive_checks = {
            "doi": bool(query.doi and query.doi in unindexed_metadata_dois),
            "title": bool(
                _distinctive_title(query.title)
                and normalized_title in unindexed_metadata_titles
            ),
        }
        for kind, matched in inactive_checks.items():
            if matched and not checks[kind]:
                unindexed_counts[kind] += 1
                if len(unindexed_examples[kind]) < 20:
                    unindexed_examples[kind].append(query.query_id)
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
    for kind, count in unindexed_counts.items():
        add(
            "evaluation_identity_in_unindexed_metadata_" + kind,
            "warning",
            query_count=count,
            examples=unindexed_examples[kind],
        )

    severity_counts = Counter(finding["severity"] for finding in findings)
    return {
        "schema_version": 3,
        "audited_evaluation_splits": list(evaluation_splits),
        "audited_corpus_views": list(normalized_corpus_views),
        "audited_query_count": len(target_queries),
        "corpus_document_count": len(corpus),
        "passed": severity_counts["critical"] == 0,
        "severity_counts": dict(sorted(severity_counts.items())),
        "findings": findings,
        "notes": [
            "Candidate venue names and frozen scope/category text are permitted inputs, not label leakage.",
            "Abstract near-duplicate detection uses deterministic rare-shingle candidate generation unless an explicit similarity hook is supplied.",
            "Publication-version identities combine versioned DOI/arXiv roots with optional dataset-specific relation hooks.",
            "Only configured retrieval views are critical corpus inputs; identity overlap in retained but unindexed provenance metadata is reported separately as a warning.",
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
