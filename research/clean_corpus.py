"""Causally clean, offline rebuilds from completed acquisition shards.

The acquisition directory is immutable input.  This module never contacts
Crossref, OpenAlex, Tavily, or publisher pages.  It rekeys stored evidence,
separates production material from paper-research material, optionally invokes
PCL with the already stored temporal subset, validates the full corpus, and
publishes the derived directory with one atomic rename.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import json
import os
from pathlib import Path
import time
from typing import Any, Iterable, Mapping, Sequence
import urllib.error

from .data import ResearchDataError, parse_iso_date, sha256_file
from .historical_builder import (
    CollectionPolicy,
    HistoricalCollectionError,
    PrototypeSynthesizer,
    SCHEMA_VERSION,
    VenueSeed,
    _atomic_compact_json,
    _atomic_json,
    _canonical_issn,
    _write_jsonl,
    build_venue_profile,
    git_code_state,
    merge_paper_evidence,
    now_iso,
    paper_evidence_id,
    stable_digest,
)


CLEAN_BUILD_VERSION = "causal-research-corpus-v1"


def _evidence_date(row: Mapping[str, Any]) -> str:
    return str(row.get("publication_date") or row.get("valid_at") or "").strip()[:10]


def _scope_evidence_id(venue_id: str, row: Mapping[str, Any]) -> str:
    if str(row.get("source") or "") == "jcr_2025":
        return f"official-scope:{venue_id}:catalog"
    return (
        f"official-scope:{venue_id}:"
        + stable_digest(
            row.get("source"),
            row.get("url"),
            row.get("text"),
            row.get("valid_at"),
        )[:32]
    )


def canonicalize_venue_evidence(
    venue: VenueSeed,
    evidence: Sequence[Mapping[str, Any]],
    *,
    cutoff: str,
    paper_only: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return production rows, temporal research rows, and ID crosswalk rows."""

    cutoff_date = parse_iso_date(cutoff, field_name="cutoff")
    paper_rows: list[dict[str, Any]] = []
    other_rows: list[dict[str, Any]] = []
    crosswalk: list[dict[str, Any]] = []
    for raw in evidence:
        row = dict(raw)
        row["venue_id"] = venue.venue_id
        if row.get("kind") == "paper":
            paper_rows.append(row)
        else:
            other_rows.append(row)

    merged_papers = merge_paper_evidence(paper_rows)
    paper_new_ids = {
        paper_evidence_id(
            venue.venue_id,
            doi=row.get("doi"),
            title=row.get("title"),
            published=row.get("publication_date"),
        )
        for row in paper_rows
    }
    if len(paper_new_ids) != len(merged_papers):
        # A mismatch means at least one old row cannot be mapped one-to-one to
        # the canonical merge identity.  Do not publish an ambiguous crosswalk.
        merged_ids = {str(row.get("evidence_id") or "") for row in merged_papers}
        if paper_new_ids != merged_ids:
            raise HistoricalCollectionError(
                f"paper identity merge mismatch for {venue.venue_id}"
            )
    for row in paper_rows:
        old_id = str(row.get("evidence_id") or "")
        new_id = paper_evidence_id(
            venue.venue_id,
            doi=row.get("doi"),
            title=row.get("title"),
            published=row.get("publication_date"),
        )
        crosswalk.append(
            {
                "venue_id": venue.venue_id,
                "old_evidence_id": old_id,
                "new_evidence_id": new_id,
                "kind": "paper",
                "status": "exact",
            }
        )

    canonical_other: list[dict[str, Any]] = []
    for row in other_rows:
        old_id = str(row.get("evidence_id") or "")
        kind = str(row.get("kind") or "")
        if kind == "catalog":
            new_id = f"catalog:{venue.venue_id}"
        elif kind == "official_scope":
            new_id = _scope_evidence_id(venue.venue_id, row)
        else:
            new_id = (
                f"evidence:{venue.venue_id}:{kind or 'unknown'}:"
                + stable_digest(old_id, row.get("source"), row.get("content_sha256"))[:32]
            )
        row["evidence_id"] = new_id
        canonical_other.append(row)
        crosswalk.append(
            {
                "venue_id": venue.venue_id,
                "old_evidence_id": old_id,
                "new_evidence_id": new_id,
                "kind": kind,
                "status": "exact",
            }
        )

    production = sorted(
        [*canonical_other, *merged_papers],
        key=lambda row: str(row.get("evidence_id") or ""),
    )
    research: list[dict[str, Any]] = []
    for row in production:
        if row.get("temporal_eligible") is not True:
            continue
        evidence_date = _evidence_date(row)
        if not evidence_date:
            raise HistoricalCollectionError(
                f"temporal evidence has no date: {venue.venue_id}/"
                f"{row.get('evidence_id')}"
            )
        if parse_iso_date(evidence_date, field_name="research evidence date") > cutoff_date:
            raise HistoricalCollectionError(
                f"temporal evidence postdates cutoff: {venue.venue_id}/"
                f"{row.get('evidence_id')}"
            )
        if paper_only and row.get("kind") not in {"catalog", "paper"}:
            continue
        research.append(row)

    crosswalk.sort(
        key=lambda row: (
            str(row["venue_id"]),
            str(row["old_evidence_id"]),
            str(row["new_evidence_id"]),
        )
    )
    old_targets: dict[str, set[str]] = {}
    for row in crosswalk:
        old_targets.setdefault(str(row["old_evidence_id"]), set()).add(
            str(row["new_evidence_id"])
        )
    ambiguous = {
        old_id: targets for old_id, targets in old_targets.items() if len(targets) != 1
    }
    if ambiguous:
        raise HistoricalCollectionError(
            f"ambiguous evidence identity crosswalk for {venue.venue_id}: "
            f"{sorted(ambiguous)[:3]}"
        )
    production_ids = [str(row.get("evidence_id") or "") for row in production]
    if not all(production_ids) or len(production_ids) != len(set(production_ids)):
        raise HistoricalCollectionError(
            f"duplicate canonical evidence identity for {venue.venue_id}"
        )
    return production, research, crosswalk


def _remap_prototypes(
    venue_id: str,
    prototypes: Sequence[Mapping[str, Any]],
    crosswalk: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    mapping = {
        str(row.get("old_evidence_id") or ""): str(row.get("new_evidence_id") or "")
        for row in crosswalk
    }
    remapped: list[dict[str, Any]] = []
    for raw in prototypes:
        prototype = dict(raw)
        source_ids = [
            mapping.get(str(value), str(value))
            for value in prototype.get("source_ids") or ()
            if str(value)
        ]
        prototype["source_ids"] = source_ids
        prototype.setdefault("prototype_id", f"{venue_id}:production:{len(remapped)}")
        remapped.append(prototype)
    return remapped


def _source_prototypes(profile: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    values = (
        profile.get("production_prototypes")
        or profile.get("prototypes")
        or ()
    )
    return [row for row in values if isinstance(row, Mapping)]


def _process_venue(
    venue: VenueSeed,
    *,
    source_dir: Path,
    policy: CollectionPolicy,
    mode: str,
    pcl: PrototypeSynthesizer | None,
    pcl_attempts: int,
    pcl_backoff_base: float,
    pcl_backoff_max: float,
) -> dict[str, Any]:
    shard_path = source_dir / "venues" / f"{venue.venue_id}.json"
    try:
        shard = json.loads(shard_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HistoricalCollectionError(f"invalid acquisition shard: {shard_path}") from exc
    if not isinstance(shard, Mapping) or not isinstance(shard.get("evidence"), list):
        raise HistoricalCollectionError(f"invalid acquisition shard: {shard_path}")
    source_profile = shard.get("profile")
    if not isinstance(source_profile, Mapping):
        raise HistoricalCollectionError(f"missing acquisition profile: {shard_path}")

    production_evidence, research_evidence, crosswalk = canonicalize_venue_evidence(
        venue,
        [row for row in shard["evidence"] if isinstance(row, Mapping)],
        cutoff=policy.cutoff,
        paper_only=mode == "deterministic",
    )
    production_prototypes = _remap_prototypes(
        venue.venue_id,
        _source_prototypes(source_profile),
        crosswalk,
    )

    llm_prototypes: Sequence[Mapping[str, Any]] = ()
    pcl_status = "not_run"
    pcl_model = ""
    if mode == "pcl":
        if pcl is None:
            raise HistoricalCollectionError("clean PCL rebuild requires a synthesizer")
        last_error: BaseException | None = None
        for attempt in range(1, pcl_attempts + 1):
            try:
                llm_prototypes, pcl_status = pcl.synthesize(
                    venue, research_evidence, policy
                )
                pcl_model = str(getattr(pcl, "last_model", pcl.model))
                if pcl_status == "ok" and llm_prototypes:
                    break
                last_error = HistoricalCollectionError(
                    f"clean PCL synthesis failed for {venue.venue_id}: {pcl_status}"
                )
            except urllib.error.HTTPError as exc:
                # Authentication is a shared configuration failure, not a
                # transient per-venue problem.  Stop the bounded queue now.
                if int(exc.code) == 401:
                    raise
                last_error = exc
            except Exception as exc:  # noqa: BLE001 - retry boundary is explicit.
                last_error = exc
            if attempt < pcl_attempts:
                delay = min(
                    pcl_backoff_max,
                    pcl_backoff_base * (2 ** (attempt - 1)),
                )
                if delay:
                    time.sleep(delay)
        else:
            assert last_error is not None
            raise HistoricalCollectionError(
                f"clean PCL synthesis exhausted {pcl_attempts} attempts for "
                f"{venue.venue_id}: {type(last_error).__name__}"
            ) from last_error

    source_metadata = source_profile.get("metadata")
    source_metadata = source_metadata if isinstance(source_metadata, Mapping) else {}
    research_profile = build_venue_profile(
        venue,
        research_evidence,
        llm_prototypes,
        cutoff=policy.cutoff,
        pcl_status=pcl_status,
        pcl_model=pcl_model,
        max_prototypes=policy.max_prototypes,
        collection_status=str(shard.get("status") or "unknown"),
        source_errors=(
            source_metadata.get("source_errors")
            if isinstance(source_metadata.get("source_errors"), Mapping)
            else {}
        ),
        research_scope_enabled=mode == "pcl",
    )
    production_profile = dict(source_profile)
    production_profile["prototypes"] = production_prototypes
    production_profile["production_prototypes"] = production_prototypes
    return {
        "schema_version": SCHEMA_VERSION,
        "build_version": CLEAN_BUILD_VERSION,
        "mode": mode,
        "venue_id": venue.venue_id,
        "production_evidence": production_evidence,
        "research_evidence": research_evidence,
        "evidence_identity_crosswalk": crosswalk,
        "production_profile": production_profile,
        "research_profile": research_profile,
        "production_prototypes": production_prototypes,
        "research_prototypes": research_profile["research_prototypes"],
    }


def _checkpoint_rows(
    checkpoints: Sequence[Path], field: str, *, venue_field: bool = False
) -> Iterable[Mapping[str, Any]]:
    for path in checkpoints:
        payload = json.loads(path.read_text(encoding="utf-8"))
        values = payload.get(field)
        if isinstance(values, Mapping):
            yield values
            continue
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, Mapping):
                if venue_field:
                    yield {"venue_id": payload["venue_id"], **dict(value)}
                else:
                    yield value


def _validate_checkpoints(
    checkpoints: Sequence[Path],
    venues: Sequence[VenueSeed],
    *,
    cutoff: str,
    mode: str,
) -> tuple[dict[str, Any], Counter[str]]:
    expected_ids = {venue.venue_id for venue in venues}
    profile_ids: set[str] = set()
    missing_sources = 0
    ambiguous_sources = 0
    post_cutoff = 0
    non_temporal = 0
    warm_few_without_paper = 0
    evidence_collisions = 0
    model_distribution: Counter[str] = Counter()
    cutoff_date = parse_iso_date(cutoff, field_name="cutoff")
    for path in checkpoints:
        payload = json.loads(path.read_text(encoding="utf-8"))
        venue_id = str(payload.get("venue_id") or "")
        if venue_id in profile_ids:
            raise HistoricalCollectionError(f"duplicate clean checkpoint: {venue_id}")
        profile_ids.add(venue_id)
        evidence = payload.get("research_evidence") or []
        evidence_by_id: dict[str, Mapping[str, Any]] = {}
        for row in evidence:
            if not isinstance(row, Mapping):
                continue
            evidence_id = str(row.get("evidence_id") or "")
            if not evidence_id:
                missing_sources += 1
                continue
            if evidence_id in evidence_by_id:
                evidence_collisions += 1
            evidence_by_id[evidence_id] = row
            if row.get("temporal_eligible") is not True:
                non_temporal += 1
            raw_date = _evidence_date(row)
            if not raw_date or parse_iso_date(
                raw_date, field_name="research evidence date"
            ) > cutoff_date:
                post_cutoff += 1
        profile = payload.get("research_profile") or {}
        prototypes = payload.get("research_prototypes") or []
        paper_ids = {
            evidence_id
            for evidence_id, row in evidence_by_id.items()
            if row.get("kind") == "paper"
        }
        paper_backed = False
        for prototype in prototypes:
            if not isinstance(prototype, Mapping):
                continue
            source_ids = [str(value) for value in prototype.get("source_ids") or ()]
            for source_id in source_ids:
                if source_id not in evidence_by_id:
                    missing_sources += 1
            if len(source_ids) != len(set(source_ids)):
                ambiguous_sources += 1
            paper_backed = paper_backed or bool(paper_ids.intersection(source_ids))
        metadata = profile.get("metadata") if isinstance(profile, Mapping) else {}
        tier = str((metadata or {}).get("profile_tier") or "")
        if tier in {"warm", "few-shot"} and not paper_backed:
            warm_few_without_paper += 1
        model = str((metadata or {}).get("pcl_model") or "")
        if model:
            model_distribution[model] += 1
        if mode == "pcl":
            generation = (metadata or {}).get("pcl_generation")
            if not isinstance(generation, Mapping):
                missing_sources += 1
            else:
                required = {
                    "model",
                    "prompt_version",
                    "prompt_sha256",
                    "parameters",
                    "parameters_sha256",
                    "input_evidence_sha256",
                    "code_state",
                }
                if required - set(generation):
                    missing_sources += 1
    validation = {
        "candidate_count": len(expected_ids),
        "profile_count": len(profile_ids),
        "candidate_profile_ids_match": profile_ids == expected_ids,
        "research_non_temporal_evidence_count": non_temporal,
        "research_post_cutoff_evidence_count": post_cutoff,
        "missing_prototype_source_id_count": missing_sources,
        "ambiguous_prototype_source_id_count": ambiguous_sources,
        "unrelated_evidence_id_collision_count": evidence_collisions,
        "warm_few_without_paper_backed_prototype_count": warm_few_without_paper,
    }
    if not validation["candidate_profile_ids_match"] or any(
        validation[key]
        for key in (
            "research_non_temporal_evidence_count",
            "research_post_cutoff_evidence_count",
            "missing_prototype_source_id_count",
            "ambiguous_prototype_source_id_count",
            "unrelated_evidence_id_collision_count",
            "warm_few_without_paper_backed_prototype_count",
        )
    ):
        raise HistoricalCollectionError(f"clean corpus validation failed: {validation}")
    return validation, model_distribution


def _build_kg(
    checkpoints: Sequence[Path],
    venues: Sequence[VenueSeed],
    profiles_path: Path,
) -> dict[str, Any]:
    venue_by_id = {venue.venue_id: venue for venue in venues}
    chunks: list[dict[str, Any]] = []
    entities: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    for path in checkpoints:
        payload = json.loads(path.read_text(encoding="utf-8"))
        venue = venue_by_id[str(payload["venue_id"])]
        if venue.online_entity_id is None or venue.identity_status != "exact_issn":
            continue
        venue_name = f"VENUE::{venue.online_entity_id}::{venue.name}"
        venue_source = f"historical-venue:{venue.venue_id}"
        description = " | ".join(
            value for value in (venue.name, venue.subject, venue.quartile) if value
        )
        chunks.append(
            {"content": description, "source_id": venue_source, "file_path": str(profiles_path)}
        )
        entities.append(
            {
                "entity_name": venue_name,
                "entity_type": "venue",
                "description": description,
                "source_id": venue_source,
                "file_path": str(profiles_path),
            }
        )
        for prototype in payload.get("research_prototypes") or ():
            if not isinstance(prototype, Mapping):
                continue
            prototype_id = str(prototype.get("prototype_id") or "")
            text = str(prototype.get("text") or "").strip()
            if not prototype_id or not text:
                continue
            prototype_name = "PROTOTYPE::" + prototype_id
            source_id = "historical-prototype:" + prototype_id
            chunks.append(
                {"content": text, "source_id": source_id, "file_path": str(profiles_path)}
            )
            entities.append(
                {
                    "entity_name": prototype_name,
                    "entity_type": "venue_prototype",
                    "description": text,
                    "source_id": source_id,
                    "file_path": str(profiles_path),
                }
            )
            relationships.append(
                {
                    "src_id": venue_name,
                    "tgt_id": prototype_name,
                    "description": "Venue has a causally frozen topic prototype derived from: "
                    + ", ".join(str(value) for value in prototype.get("source_ids") or ()),
                    "keywords": "HAS_PROTOTYPE DERIVED_FROM",
                    "weight": float(prototype.get("weight", 1.0)),
                    "source_id": source_id,
                    "file_path": str(profiles_path),
                }
            )
    return {"chunks": chunks, "entities": entities, "relationships": relationships}


def rebuild_clean_corpus(
    *,
    venues: Sequence[VenueSeed],
    policy: CollectionPolicy,
    source_dir: Path,
    output_dir: Path,
    jcr_csv: Path,
    mode: str,
    pcl: PrototypeSynthesizer | None = None,
    workers: int = 1,
    pcl_attempts: int = 1,
    pcl_backoff_base: float = 1.0,
    pcl_backoff_max: float = 30.0,
) -> dict[str, Any]:
    """Rebuild and atomically publish a deterministic or clean-PCL corpus."""

    policy.validate()
    if mode not in {"deterministic", "pcl"}:
        raise ResearchDataError("clean rebuild mode must be deterministic or pcl")
    if workers < 1:
        raise ResearchDataError("clean rebuild workers must be positive")
    if pcl_attempts < 1:
        raise ResearchDataError("clean PCL attempts must be positive")
    if pcl_backoff_base < 0 or pcl_backoff_max < 0:
        raise ResearchDataError("clean PCL backoff values must be non-negative")
    source_manifest = source_dir / "manifest.json"
    if not source_manifest.is_file():
        raise HistoricalCollectionError(f"missing source manifest: {source_manifest}")
    if output_dir.exists():
        manifest_path = output_dir / "manifest.json"
        if manifest_path.is_file():
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                existing.get("build_version") == CLEAN_BUILD_VERSION
                and existing.get("mode") == mode
                and existing.get("inputs", {}).get("source_manifest", {}).get("sha256")
                == sha256_file(source_manifest)
            ):
                return existing
        raise HistoricalCollectionError(f"clean output already exists: {output_dir}")

    building = output_dir.with_name(f".{output_dir.name}.building")
    building.mkdir(parents=True, exist_ok=True)
    state = {
        "build_version": CLEAN_BUILD_VERSION,
        "mode": mode,
        "source_manifest_sha256": sha256_file(source_manifest),
        "jcr_csv_sha256": sha256_file(jcr_csv),
        "venue_count": len(venues),
        "policy": {
            "history_start": policy.history_start,
            "cutoff": policy.cutoff,
            "max_prototypes": policy.max_prototypes,
            "max_pcl_evidence": policy.max_pcl_evidence,
            "pcl_attempts": pcl_attempts,
            "pcl_backoff_base": pcl_backoff_base,
            "pcl_backoff_max": pcl_backoff_max,
        },
        "code_state": git_code_state(),
        "pcl_provider": (
            dict(pcl.provider_identity) if pcl is not None else {"enabled": False}
        ),
    }
    state_path = building / "build_state.json"
    if state_path.is_file():
        existing_state = json.loads(state_path.read_text(encoding="utf-8"))
        if existing_state != state:
            raise HistoricalCollectionError(
                f"incompatible clean build state: {state_path}"
            )
    else:
        _atomic_json(state_path, state)

    checkpoint_dir = building / "clean_venues"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    def build_one(venue: VenueSeed) -> Path:
        checkpoint = checkpoint_dir / f"{venue.venue_id}.json"
        if checkpoint.is_file():
            return checkpoint
        payload = _process_venue(
            venue,
            source_dir=source_dir,
            policy=policy,
            mode=mode,
            pcl=pcl,
            pcl_attempts=pcl_attempts,
            pcl_backoff_base=pcl_backoff_base,
            pcl_backoff_max=pcl_backoff_max,
        )
        _atomic_json(checkpoint, payload)
        return checkpoint

    pending = [
        venue
        for venue in venues
        if not (checkpoint_dir / f"{venue.venue_id}.json").is_file()
    ]
    if workers == 1:
        for venue in pending:
            build_one(venue)
    else:
        # Keep only one task per worker in flight.  If a shared provider or
        # configuration error occurs, at most ``workers - 1`` other requests
        # can finish before the executor exits; thousands of doomed requests
        # are never pre-submitted.
        with ThreadPoolExecutor(max_workers=workers) as executor:
            iterator = iter(pending)
            futures: dict[Future[Path], VenueSeed] = {}

            def submit_next() -> bool:
                try:
                    venue = next(iterator)
                except StopIteration:
                    return False
                futures[executor.submit(build_one, venue)] = venue
                return True

            for _ in range(workers):
                if not submit_next():
                    break
            while futures:
                completed, _unfinished = wait(
                    futures, return_when=FIRST_COMPLETED
                )
                for future in completed:
                    futures.pop(future)
                    future.result()
                    submit_next()

    checkpoints = [
        checkpoint_dir / f"{venue.venue_id}.json"
        for venue in sorted(venues, key=lambda item: item.venue_id)
    ]
    validation, model_distribution = _validate_checkpoints(
        checkpoints,
        venues,
        cutoff=policy.cutoff,
        mode=mode,
    )

    paths = {
        "profiles": building / "venue_profiles.train.jsonl",
        "production_profiles": building / "production_profiles.jsonl",
        "research_evidence": building / "research_evidence.jsonl",
        "production_evidence": building / "production_evidence.jsonl",
        "prototypes": building / "prototypes.jsonl",
        "production_prototypes": building / "production_prototypes.jsonl",
        "evidence_identity_crosswalk": building / "evidence_identity_crosswalk.jsonl",
        "venue_identity_crosswalk": building / "venue_identity_crosswalk.jsonl",
        "pcl_generation": building / "pcl_generation.jsonl",
        "lightrag_custom_kg": building / "lightrag_custom_kg.json",
    }
    _write_jsonl(paths["profiles"], _checkpoint_rows(checkpoints, "research_profile"))
    _write_jsonl(
        paths["production_profiles"],
        _checkpoint_rows(checkpoints, "production_profile"),
    )
    _write_jsonl(
        paths["research_evidence"], _checkpoint_rows(checkpoints, "research_evidence")
    )
    _write_jsonl(
        paths["production_evidence"], _checkpoint_rows(checkpoints, "production_evidence")
    )
    _write_jsonl(
        paths["prototypes"],
        _checkpoint_rows(checkpoints, "research_prototypes", venue_field=True),
    )
    _write_jsonl(
        paths["production_prototypes"],
        _checkpoint_rows(checkpoints, "production_prototypes", venue_field=True),
    )
    _write_jsonl(
        paths["evidence_identity_crosswalk"],
        _checkpoint_rows(checkpoints, "evidence_identity_crosswalk"),
    )
    _write_jsonl(
        paths["venue_identity_crosswalk"],
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

    def generation_rows() -> Iterable[Mapping[str, Any]]:
        for checkpoint in checkpoints:
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            profile = payload.get("research_profile") or {}
            metadata = profile.get("metadata") if isinstance(profile, Mapping) else {}
            generation = (metadata or {}).get("pcl_generation")
            if isinstance(generation, Mapping) and generation:
                yield {"venue_id": payload["venue_id"], **dict(generation)}

    _write_jsonl(paths["pcl_generation"], generation_rows())
    kg = _build_kg(checkpoints, venues, paths["profiles"])
    _atomic_compact_json(paths["lightrag_custom_kg"], kg)

    profile_tiers: Counter[str] = Counter()
    paper_count = 0
    research_evidence_count = 0
    prototype_count = 0
    for checkpoint in checkpoints:
        payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        metadata = payload["research_profile"]["metadata"]
        profile_tiers[str(metadata.get("profile_tier") or "unknown")] += 1
        paper_count += sum(
            row.get("kind") == "paper" for row in payload["research_evidence"]
        )
        research_evidence_count += len(payload["research_evidence"])
        prototype_count += len(payload["research_prototypes"])

    output_manifest = {
        name: {"path": path.name, "sha256": sha256_file(path), "bytes": path.stat().st_size}
        for name, path in paths.items()
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "build_version": CLEAN_BUILD_VERSION,
        "created_at": now_iso(),
        "mode": mode,
        "purpose": "causally clean candidate-side paper research corpus",
        "boundaries": {"history_start": policy.history_start, "cutoff": policy.cutoff},
        "policy": {
            "network_acquisition": False,
            "stored_evidence_only": True,
            "research_evidence": (
                "catalog_and_paper_only"
                if mode == "deterministic"
                else "temporal_eligible_only"
            ),
            "max_prototypes": policy.max_prototypes,
            "max_pcl_evidence": policy.max_pcl_evidence,
            "test_gold_priority": False,
        },
        "inputs": {
            "source_manifest": {
                "path": str(source_manifest),
                "sha256": sha256_file(source_manifest),
            },
            "jcr_csv": {"path": str(jcr_csv), "sha256": sha256_file(jcr_csv)},
        },
        "code_state": git_code_state(),
        "pcl": dict(pcl.provider_identity) if pcl is not None else {"enabled": False},
        "observed_pcl_model_distribution": dict(sorted(model_distribution.items())),
        "coverage": {
            "catalog_venues": len(venues),
            "profile_tiers": dict(sorted(profile_tiers.items())),
            "research_evidence_records": research_evidence_count,
            "paper_evidence_records": paper_count,
            "prototype_records": prototype_count,
            "lightrag_entities": len(kg["entities"]),
            "lightrag_relationships": len(kg["relationships"]),
        },
        "validation": validation,
        "outputs": output_manifest,
    }
    _atomic_json(building / "manifest.json", manifest)
    os.replace(building, output_dir)
    return manifest
