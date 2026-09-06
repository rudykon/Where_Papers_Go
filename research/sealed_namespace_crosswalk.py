"""Build a label-free exact-ISSN venue namespace crosswalk.

The future benchmark builder and the frozen research corpus use independently
derived venue identifiers.  This module reconciles those identifiers using
only their already-public JCR ISSNs.  It deliberately has no label-vault input
and rejects any path that could be a sealed-label artifact.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping, Sequence
import uuid

from scripts.build_recent_journal_benchmark import load_jcr_venues
from where_paper_go.paths import DATA_DIR
from where_paper_go.recommender import (
    CURATED_SCOPE_FILE,
    DATA_FILES,
    valid_issn_token,
)

from .data import ResearchDataError, canonical_json_sha256, sha256_file


FORMAL_VENUE_COUNT = 20_087
MAPPING_METHOD = "exact_issn_unique_owner"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SOURCE_FILENAMES = (*DATA_FILES, CURATED_SCOPE_FILE)


def _require_sha256(value: str, label: str) -> str:
    normalized = str(value or "").strip()
    if not _SHA256_RE.fullmatch(normalized):
        raise ResearchDataError(f"{label} must be a lowercase SHA-256 digest")
    return normalized


def _require_count(value: int, label: str) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ResearchDataError(f"{label} must be an integer") from exc
    if count < 0:
        raise ResearchDataError(f"{label} must be non-negative")
    return count


def _forbid_sealed_label_path(path: Path, label: str) -> None:
    if any(part.casefold().endswith("labels.sealed.jsonl") for part in path.parts):
        raise ResearchDataError(f"{label} must not reference a sealed-label artifact")


def _normalize_issns(raw_values: Any, label: str) -> tuple[str, ...]:
    if not isinstance(raw_values, (list, tuple)) or not raw_values:
        raise ResearchDataError(f"{label} must contain at least one ISSN")
    tokens: list[str] = []
    for raw in raw_values:
        if not isinstance(raw, str):
            raise ResearchDataError(f"{label} contains a non-string ISSN")
        token = valid_issn_token(raw)
        if not token:
            raise ResearchDataError(f"{label} contains a checksum-invalid ISSN")
        tokens.append(token)
    if len(tokens) != len(set(tokens)):
        raise ResearchDataError(f"{label} contains duplicate normalized ISSNs")
    return tuple(sorted(tokens))


def _source_artifacts(data_dir: Path) -> tuple[list[dict[str, Any]], str]:
    artifacts: list[dict[str, Any]] = []
    fingerprint_rows: list[dict[str, Any]] = []
    for filename in _SOURCE_FILENAMES:
        path = (data_dir / filename).resolve()
        _forbid_sealed_label_path(path, "source data")
        if not path.is_file():
            raise ResearchDataError(f"source data artifact does not exist: {path}")
        digest = sha256_file(path)
        size = path.stat().st_size
        artifacts.append(
            {
                "path": str(path),
                "sha256": digest,
                "bytes": size,
            }
        )
        fingerprint_rows.append(
            {"filename": filename, "sha256": digest, "bytes": size}
        )
    return artifacts, canonical_json_sha256(fingerprint_rows)


def _namespace_sha256(rows: Sequence[Mapping[str, Any]]) -> str:
    return canonical_json_sha256(
        [
            {
                "venue_id": str(row["venue_id"]),
                "issns": list(row["issns"]),
            }
            for row in rows
        ]
    )


def _validate_unique_owners(
    rows: Sequence[Mapping[str, Any]], *, label: str
) -> tuple[dict[str, str], int]:
    owners: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        venue_id = str(row["venue_id"])
        for token in row["issns"]:
            owners[str(token)].add(venue_id)
    collision_count = sum(1 for venue_ids in owners.values() if len(venue_ids) != 1)
    if collision_count:
        raise ResearchDataError(
            f"{label} has {collision_count} ISSNs with non-unique venue owners"
        )
    return {token: next(iter(venue_ids)) for token, venue_ids in owners.items()}, 0


def _load_source_namespace(data_dir: Path) -> tuple[list[dict[str, Any]], int]:
    try:
        venues, ambiguous = load_jcr_venues(data_dir)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ResearchDataError("cannot load the source JCR namespace") from exc
    if ambiguous:
        raise ResearchDataError(
            f"source loader reported {len(ambiguous)} ambiguous ISSNs"
        )
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for venue in venues:
        venue_id = str(getattr(venue, "venue_id", "") or "").strip()
        if not venue_id:
            raise ResearchDataError("source namespace contains an empty venue ID")
        if venue_id in seen_ids:
            raise ResearchDataError("source namespace contains duplicate venue IDs")
        seen_ids.add(venue_id)
        rows.append(
            {
                "venue_id": venue_id,
                "issns": _normalize_issns(
                    getattr(venue, "issns", None), "source namespace row"
                ),
            }
        )
    rows.sort(key=lambda row: str(row["venue_id"]))
    _validate_unique_owners(rows, label="source namespace")
    return rows, 0


def _load_target_namespace(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    raise ResearchDataError(
                        f"target identity crosswalk has a blank row at line {line_number}"
                    )
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ResearchDataError(
                        f"target identity crosswalk has invalid JSON at line {line_number}"
                    ) from exc
                if not isinstance(raw, Mapping):
                    raise ResearchDataError(
                        f"target identity crosswalk row {line_number} is not an object"
                    )
                if raw.get("status") != "exact_issn":
                    raise ResearchDataError(
                        "target identity crosswalk contains a non-exact-ISSN row"
                    )
                venue_id = str(raw.get("venue_id") or "").strip()
                if not venue_id:
                    raise ResearchDataError(
                        "target identity crosswalk contains an empty venue ID"
                    )
                if venue_id in seen_ids:
                    raise ResearchDataError(
                        "target identity crosswalk contains duplicate venue IDs"
                    )
                seen_ids.add(venue_id)
                rows.append(
                    {
                        "venue_id": venue_id,
                        "issns": _normalize_issns(
                            raw.get("issns"), "target identity crosswalk row"
                        ),
                    }
                )
    except ResearchDataError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ResearchDataError(f"cannot read target identity crosswalk: {path}") from exc
    if not rows:
        raise ResearchDataError("target identity crosswalk is empty")
    rows.sort(key=lambda row: str(row["venue_id"]))
    _validate_unique_owners(rows, label="target namespace")
    return rows


def _write_new(path: Path, content: str) -> None:
    """Publish a complete file atomically without replacing an existing path."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    except FileExistsError as exc:
        raise ResearchDataError(f"output already exists: {path}") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _jsonl_content(rows: Iterable[Mapping[str, Any]]) -> str:
    return "".join(
        json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
        for row in rows
    )


def build_sealed_namespace_crosswalk(
    *,
    data_dir: Path,
    target_identity_path: Path,
    mapping_output_path: Path,
    manifest_output_path: Path,
    expected_source_namespace_sha256: str,
    expected_target_file_sha256: str,
    expected_target_namespace_sha256: str,
    expected_source_count: int,
    expected_target_count: int,
    expected_identity_count: int,
    expected_remap_count: int,
    generation_command: Sequence[str],
) -> dict[str, Any]:
    """Validate and publish a complete one-to-one exact-ISSN ID crosswalk.

    All expectations are mandatory so the builder cannot silently accept a
    different namespace or a partial reconciliation.
    """

    data_dir = Path(data_dir).resolve()
    target_identity_path = Path(target_identity_path).resolve()
    mapping_output_path = Path(mapping_output_path).resolve()
    manifest_output_path = Path(manifest_output_path).resolve()
    for path, label in (
        (target_identity_path, "target identity crosswalk"),
        (mapping_output_path, "mapping output"),
        (manifest_output_path, "manifest output"),
    ):
        _forbid_sealed_label_path(path, label)
    if target_identity_path.name != "venue_identity_crosswalk.jsonl":
        raise ResearchDataError(
            "target identity artifact must be named venue_identity_crosswalk.jsonl"
        )
    if len({target_identity_path, mapping_output_path, manifest_output_path}) != 3:
        raise ResearchDataError("input and output paths must be distinct")
    if mapping_output_path.exists() or manifest_output_path.exists():
        raise ResearchDataError("namespace crosswalk output already exists")
    if not target_identity_path.is_file():
        raise ResearchDataError(
            f"target identity crosswalk does not exist: {target_identity_path}"
        )

    expected_source_hash = _require_sha256(
        expected_source_namespace_sha256, "expected source namespace hash"
    )
    expected_target_file_hash = _require_sha256(
        expected_target_file_sha256, "expected target file hash"
    )
    expected_target_namespace_hash = _require_sha256(
        expected_target_namespace_sha256, "expected target namespace hash"
    )
    source_expected = _require_count(expected_source_count, "expected source count")
    target_expected = _require_count(expected_target_count, "expected target count")
    identity_expected = _require_count(
        expected_identity_count, "expected identity count"
    )
    remap_expected = _require_count(expected_remap_count, "expected remap count")
    if source_expected != target_expected:
        raise ResearchDataError(
            "expected source and target counts must agree for a bijection"
        )
    if identity_expected + remap_expected != source_expected:
        raise ResearchDataError(
            "expected identity and remap counts must sum to the source count"
        )
    command = [str(value) for value in generation_command]
    if not command or any(not value for value in command):
        raise ResearchDataError("generation command must contain non-empty arguments")

    actual_target_file_hash = sha256_file(target_identity_path)
    if actual_target_file_hash != expected_target_file_hash:
        raise ResearchDataError("target identity crosswalk SHA-256 mismatch")
    source_artifacts, source_artifacts_sha256 = _source_artifacts(data_dir)
    source_rows, source_ambiguous_count = _load_source_namespace(data_dir)
    target_rows = _load_target_namespace(target_identity_path)

    source_namespace_hash = _namespace_sha256(source_rows)
    target_namespace_hash = _namespace_sha256(target_rows)
    if source_namespace_hash != expected_source_hash:
        raise ResearchDataError("source namespace SHA-256 mismatch")
    if target_namespace_hash != expected_target_namespace_hash:
        raise ResearchDataError("target namespace SHA-256 mismatch")
    if len(source_rows) != source_expected:
        raise ResearchDataError("source namespace count mismatch")
    if len(target_rows) != target_expected:
        raise ResearchDataError("target namespace count mismatch")

    target_owner, target_collision_count = _validate_unique_owners(
        target_rows, label="target namespace"
    )
    target_issns = {
        str(row["venue_id"]): frozenset(str(token) for token in row["issns"])
        for row in target_rows
    }
    mappings: list[dict[str, str]] = []
    unmatched_count = 0
    ambiguous_count = source_ambiguous_count
    for source in source_rows:
        candidate_targets = {
            target_owner[token]
            for token in source["issns"]
            if token in target_owner
        }
        if not candidate_targets:
            unmatched_count += 1
            continue
        if len(candidate_targets) != 1:
            ambiguous_count += 1
            continue
        target_id = next(iter(candidate_targets))
        if not set(source["issns"]).intersection(target_issns[target_id]):
            raise ResearchDataError("exact-ISSN mapping lost its match evidence")
        mappings.append(
            {
                "source_venue_id": str(source["venue_id"]),
                "target_venue_id": target_id,
                "mapping_method": MAPPING_METHOD,
            }
        )

    target_counts = Counter(row["target_venue_id"] for row in mappings)
    collision_count = target_collision_count + sum(
        1 for count in target_counts.values() if count != 1
    )
    distinct_target_count = len(target_counts)
    target_unmapped_count = len(target_rows) - distinct_target_count
    identity_count = sum(
        row["source_venue_id"] == row["target_venue_id"] for row in mappings
    )
    remap_count = len(mappings) - identity_count
    if unmatched_count or target_unmapped_count or ambiguous_count or collision_count:
        raise ResearchDataError(
            "venue namespace reconciliation is not a complete unambiguous bijection: "
            f"source_unmapped={unmatched_count}, target_unmapped={target_unmapped_count}, "
            f"ambiguous={ambiguous_count}, collisions={collision_count}"
        )
    if len(mappings) != source_expected or distinct_target_count != target_expected:
        raise ResearchDataError("venue namespace mapping coverage mismatch")
    if identity_count != identity_expected:
        raise ResearchDataError("venue namespace identity count mismatch")
    if remap_count != remap_expected:
        raise ResearchDataError("venue namespace remap count mismatch")

    mappings.sort(key=lambda row: row["source_venue_id"])
    _write_new(mapping_output_path, _jsonl_content(mappings))
    mapping_record = {
        "path": str(mapping_output_path),
        "sha256": sha256_file(mapping_output_path),
        "bytes": mapping_output_path.stat().st_size,
        "record_count": len(mappings),
    }
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": "sealed_venue_namespace_crosswalk",
        "status": "complete_label_free_exact_issn_bijection",
        "implementation": {
            "path": str(Path(__file__).resolve()),
            "sha256": sha256_file(Path(__file__).resolve()),
            "bytes": Path(__file__).resolve().stat().st_size,
        },
        "label_boundary": {
            "label_input_configured": False,
            "label_files_opened": 0,
            "label_content_parsed": False,
        },
        "matching_policy": {
            "source_loader": (
                "scripts.build_recent_journal_benchmark.load_jcr_venues"
            ),
            "method": MAPPING_METHOD,
            "target_required_status": "exact_issn",
            "issn_validator": "where_paper_go.recommender.valid_issn_token",
            "checksum_valid_issn_required": True,
            "fuzzy_matching": False,
            "journal_names_emitted": False,
        },
        "counts": {
            "source": len(source_rows),
            "target": len(target_rows),
            "mapped": len(mappings),
            "distinct_target": distinct_target_count,
            "identity": identity_count,
            "remapped": remap_count,
            "source_unmapped": unmatched_count,
            "target_unmapped": target_unmapped_count,
            "ambiguous": ambiguous_count,
            "collision": collision_count,
        },
        "source": {
            "data_dir": str(data_dir),
            "namespace_sha256": source_namespace_hash,
            "artifacts_sha256": source_artifacts_sha256,
            "artifacts": source_artifacts,
            "issn_count": sum(len(row["issns"]) for row in source_rows),
        },
        "target": {
            "artifact": {
                "path": str(target_identity_path),
                "sha256": actual_target_file_hash,
                "bytes": target_identity_path.stat().st_size,
            },
            "namespace_sha256": target_namespace_hash,
            "issn_count": sum(len(row["issns"]) for row in target_rows),
        },
        "expectations": {
            "source_namespace_sha256": expected_source_hash,
            "target_file_sha256": expected_target_file_hash,
            "target_namespace_sha256": expected_target_namespace_hash,
            "source_count": source_expected,
            "target_count": target_expected,
            "identity_count": identity_expected,
            "remap_count": remap_expected,
        },
        "mapping_artifact": mapping_record,
        "generation": {"command": command},
    }
    _write_new(
        manifest_output_path,
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
    )
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a label-free exact-ISSN venue namespace crosswalk."
    )
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--target-identity", type=Path, required=True)
    parser.add_argument("--mapping-output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument("--expected-source-namespace-sha256", required=True)
    parser.add_argument("--expected-target-file-sha256", required=True)
    parser.add_argument("--expected-target-namespace-sha256", required=True)
    parser.add_argument("--expected-source-count", type=int, required=True)
    parser.add_argument("--expected-target-count", type=int, required=True)
    parser.add_argument("--expected-identity-count", type=int, required=True)
    parser.add_argument("--expected-remap-count", type=int, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(raw_argv)
    command = [sys.executable, "-m", "research.sealed_namespace_crosswalk", *raw_argv]
    try:
        manifest = build_sealed_namespace_crosswalk(
            data_dir=args.data_dir,
            target_identity_path=args.target_identity,
            mapping_output_path=args.mapping_output,
            manifest_output_path=args.manifest_output,
            expected_source_namespace_sha256=args.expected_source_namespace_sha256,
            expected_target_file_sha256=args.expected_target_file_sha256,
            expected_target_namespace_sha256=args.expected_target_namespace_sha256,
            expected_source_count=args.expected_source_count,
            expected_target_count=args.expected_target_count,
            expected_identity_count=args.expected_identity_count,
            expected_remap_count=args.expected_remap_count,
            generation_command=command,
        )
    except ResearchDataError as exc:
        print(f"research namespace crosswalk error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
