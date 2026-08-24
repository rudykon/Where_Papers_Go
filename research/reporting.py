"""Transparent test-set stratification for offline retrieval reports.

The primary benchmark denominator is never changed here.  Every test query is
assigned to exactly one bucket for each dimension, including explicit
``unknown`` and ``out-of-catalog`` buckets when metadata is incomplete.
"""

from __future__ import annotations

from collections import Counter
import math
from typing import Any, Callable, Mapping, Sequence

from .types import Qrels, Query, VenueDocument


HISTORY_STATUS_POLICY = {
    "warm": "history_paper_count >= 5",
    "few-shot": "1 <= history_paper_count <= 4",
    "cold": "history_paper_count == 0",
    "unknown": "gold venue exists but its history count is unavailable",
    "out-of-catalog": "the gold venue is absent from the frozen corpus",
}

STRATIFICATION_POLICY = {
    "primary_denominator": "the complete frozen test split",
    "missing_metadata": (
        "retain the query in an explicit unknown or out-of-catalog bucket; "
        "never remove it from the primary denominator"
    ),
    "history_status": HISTORY_STATUS_POLICY,
    "history_count_precedence": [
        "metadata.history_paper_count",
        "metadata.paper_count",
        "len(metadata.source_dois)",
        "metadata.profile_tier (fallback only)",
    ],
    "profile_level_precedence": [
        "metadata.evidence_grade",
        "metadata.profile_level",
        "metadata.profile_grade",
    ],
    "subject_precedence": [
        "query.metadata.field",
        "query.metadata.subject",
        "gold venue metadata.broad_field/field/subject/area_en/area",
    ],
    "jcr_quartile_precedence": [
        "query.metadata.quartile",
        "gold venue metadata.jcr_quartile/quartile/level",
    ],
    "multiple_gold_venues": (
        "use the shared value when all gold venues agree; otherwise assign mixed"
    ),
}


def _clean_label(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        labels = sorted({_clean_label(item) for item in value} - {"", "unknown"})
        if not labels:
            return ""
        return labels[0] if len(labels) == 1 else " | ".join(labels)
    if isinstance(value, Mapping):
        for key in ("code", "name", "label", "value"):
            if key in value:
                return _clean_label(value[key])
        return ""
    label = " ".join(str(value or "").split())
    return "" if label.casefold() in {"", "unknown", "none", "null", "n/a"} else label


def _first_label(metadata: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        label = _clean_label(metadata.get(key))
        if label:
            return label
    return ""


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        return None
    return int(number)


def _history_count(metadata: Mapping[str, Any]) -> int | None:
    for key in ("history_paper_count", "paper_count"):
        count = _non_negative_int(metadata.get(key))
        if count is not None:
            return count
    source_dois = metadata.get("source_dois")
    if isinstance(source_dois, (list, tuple, set)):
        return len(source_dois)
    # A static-only JCR record is a genuine cold-start profile, even though
    # older corpus schemas did not write an explicit zero count.
    if str(metadata.get("content_origin") or "") == "static_venue_metadata":
        return 0
    return None


def _normalize_history_tier(value: Any) -> str:
    token = _clean_label(value).casefold().replace("_", "-").replace(" ", "-")
    aliases = {
        "warm": "warm",
        "few-shot": "few-shot",
        "fewshot": "few-shot",
        "cold": "cold",
        "cold-start": "cold",
        "coldstart": "cold",
    }
    return aliases.get(token, "")


def _history_status(document: VenueDocument) -> str:
    count = _history_count(document.metadata)
    if count is not None:
        if count >= 5:
            return "warm"
        if count >= 1:
            return "few-shot"
        return "cold"
    return _normalize_history_tier(document.metadata.get("profile_tier")) or "unknown"


def _profile_level(document: VenueDocument) -> str:
    level = _first_label(
        document.metadata,
        ("evidence_grade", "profile_level", "profile_grade"),
    )
    return level.upper() if len(level) == 1 and level.isalpha() else level or "unknown"


def _quartile_label(value: str) -> str:
    if value in {"unknown", "out-of-catalog", "mixed", "unlabeled"}:
        return value
    return value.upper()


def _combine_gold_values(
    gold_ids: Sequence[str],
    corpus_by_id: Mapping[str, VenueDocument],
    value_for_document: Callable[[VenueDocument], str],
) -> str:
    if not gold_ids:
        return "unlabeled"
    values: list[str] = []
    missing = False
    for venue_id in gold_ids:
        document = corpus_by_id.get(venue_id)
        if document is None:
            missing = True
        else:
            values.append(str(value_for_document(document) or "unknown"))
    unique = set(values)
    if missing:
        unique.add("out-of-catalog")
    if len(unique) == 1:
        return next(iter(unique))
    return "mixed"


def _gold_metadata_label(
    gold_ids: Sequence[str],
    corpus_by_id: Mapping[str, VenueDocument],
    keys: Sequence[str],
) -> str:
    return _combine_gold_values(
        gold_ids,
        corpus_by_id,
        lambda document: _first_label(document.metadata, keys) or "unknown",
    )


def build_query_strata(
    *,
    query_ids: Sequence[str],
    qrels: Qrels,
    queries: Mapping[str, Query],
    corpus: Sequence[VenueDocument],
) -> dict[str, dict[str, str]]:
    """Assign every requested query to one bucket in each report dimension."""

    corpus_by_id = {document.doc_id: document for document in corpus}
    strata = {
        "history_status": {},
        "profile_level": {},
        "subject": {},
        "jcr_quartile": {},
    }
    for query_id in query_ids:
        query = queries.get(query_id)
        gold_ids = sorted(
            venue_id
            for venue_id, gain in qrels.get(query_id, {}).items()
            if float(gain) > 0
        )
        strata["history_status"][query_id] = _combine_gold_values(
            gold_ids, corpus_by_id, _history_status
        )
        strata["profile_level"][query_id] = _combine_gold_values(
            gold_ids, corpus_by_id, _profile_level
        )

        query_metadata = query.metadata if query is not None else {}
        subject = _first_label(query_metadata, ("field", "subject", "broad_field"))
        if not subject:
            subject = _gold_metadata_label(
                gold_ids,
                corpus_by_id,
                ("broad_field", "field", "subject", "area_en", "area"),
            )
        strata["subject"][query_id] = subject or "unknown"

        quartile = _first_label(query_metadata, ("quartile", "jcr_quartile"))
        if not quartile:
            quartile = _gold_metadata_label(
                gold_ids,
                corpus_by_id,
                ("jcr_quartile", "quartile", "level"),
            )
        strata["jcr_quartile"][query_id] = (
            _quartile_label(quartile) if quartile else "unknown"
        )
    return strata


def summarize_strata(
    strata: Mapping[str, Mapping[str, str]], *, query_count: int
) -> dict[str, dict[str, Any]]:
    """Return transparent counts and verify each dimension keeps the denominator."""

    summary: dict[str, dict[str, Any]] = {}
    for dimension, assignments in strata.items():
        counts = Counter(str(group) for group in assignments.values())
        assigned = sum(counts.values())
        if assigned != query_count:
            raise ValueError(
                f"stratum {dimension!r} assigns {assigned} queries; expected {query_count}"
            )
        summary[dimension] = {
            "query_count": assigned,
            "groups": dict(sorted(counts.items())),
        }
    return summary
