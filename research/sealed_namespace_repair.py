"""Audited post-access repair for a sealed venue-ID namespace mismatch.

This module is deliberately separate from :mod:`research.sealed_evaluation`.
The original evaluator and its first-access audit remain immutable.  A repair
is permitted only when a catalog-wide, label-free, exact-ISSN crosswalk was
frozen before any metric computation.  The repair changes qrel document IDs
only; queries, candidates, runs, methods, metrics, and statistics stay frozen.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
from itertools import combinations
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any, Mapping, Sequence
import uuid

import numpy as np

from .data import (
    BLIND_QUERY_ALLOWED_FIELDS,
    BLIND_QUERY_LABEL_FIELDS,
    DatasetBundle,
    ResearchDataError,
    TemporalSplit,
    build_run_binding,
    canonical_json_sha256,
    load_jsonl_corpus,
    load_score_run,
    ordered_ids_sha256,
    parse_iso_date,
    sha256_file,
)
from .leakage import audit_leakage
from .metrics import evaluate_run, stratified_metrics
from .reporting import build_query_strata, summarize_strata
from .statistics import adjust_p_values, paired_bootstrap_ci, paired_permutation_test
from .types import Query, Run, VenueDocument


REPAIR_STATUS = "frozen_after_first_label_access_before_metric_computation"
AUTHORIZATION_STATUS = "explicit_second_label_access_authorized"
FIRST_FAILURE_STATUS = "first_attempt_failed_closed_after_label_access_before_metrics"
FIRST_FAILURE_MESSAGE = "sealed labels contain out-of-candidate gold venues"
MAPPING_METHOD = "exact_issn_unique_owner"
GLOBAL_REPAIR_SENTINEL_ROOT = (
    Path(__file__).resolve().parents[1]
    / "benchmark_artifacts"
    / ".sealed_namespace_repair_once"
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CODE_BUNDLE_PATHS = frozenset(
    {
        "research/cli.py",
        "research/data.py",
        "research/leakage.py",
        "research/metrics.py",
        "research/reporting.py",
        "research/sealed_namespace_repair.py",
        "research/statistics.py",
        "research/types.py",
    }
)


@dataclass(frozen=True)
class RepairPreflight:
    repair_config_path: Path
    repair_config: Mapping[str, Any]
    repair_config_artifact: Mapping[str, Any]
    original_config_path: Path
    original_config: Mapping[str, Any]
    original_output_dir: Path
    label_access_audit_path: Path
    label_path: Path
    label_sha256: str
    blind_path: Path
    blind_bundle: DatasetBundle
    sealed_manifest_path: Path
    sealed_manifest: Mapping[str, Any]
    commitment_path: Path
    commitment: Mapping[str, Any]
    profiles_path: Path
    corpus: tuple[VenueDocument, ...]
    candidate_ids: tuple[str, ...]
    query_ids: tuple[str, ...]
    runs: Mapping[str, Run]
    method_order: tuple[str, ...]
    method_records: Mapping[str, Any]
    namespace_mapping: Mapping[str, str]
    namespace_manifest_path: Path
    namespace_manifest: Mapping[str, Any]
    authorization_path: Path | None
    authorization: Mapping[str, Any]
    authorization_artifact: Mapping[str, Any] | None
    repair_identity: str
    repair_start_path: Path


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchDataError(f"{label} must be an object")
    return value


def _read_object(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResearchDataError(f"cannot read {label}: {path}") from exc
    return _mapping(value, label)


def _read_bytes_stable(
    path: Path,
    label: str,
    *,
    expected_sha256: str | None = None,
    required_mode: int | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Read and hash one immutable byte stream from a single file descriptor."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ResearchDataError(f"cannot open {label}: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ResearchDataError(f"{label} is not a regular file")
        if required_mode is not None and stat.S_IMODE(before.st_mode) != required_mode:
            raise ResearchDataError(f"{label} must have mode {required_mode:04o}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ResearchDataError(f"{label} changed while it was being read")
    finally:
        os.close(descriptor)
    content = b"".join(chunks)
    digest = hashlib.sha256(content).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise ResearchDataError(f"{label} SHA-256 mismatch")
    artifact = {
        "path": str(path.resolve()),
        "sha256": digest,
        "bytes": len(content),
        "device": before.st_dev,
        "inode": before.st_ino,
        "mode": oct(stat.S_IMODE(before.st_mode)),
    }
    return content, artifact


def _read_object_stable(
    path: Path,
    label: str,
    *,
    expected_sha256: str | None = None,
    required_mode: int | None = None,
) -> tuple[Mapping[str, Any], dict[str, Any]]:
    """Hash and parse one JSON object from the same stable byte stream."""

    content, artifact = _read_bytes_stable(
        path,
        label,
        expected_sha256=expected_sha256,
        required_mode=required_mode,
    )
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ResearchDataError(f"cannot parse {label}: {path}") from exc
    return _mapping(value, label), artifact


def _resolve(config_path: Path, value: Any) -> Path:
    path = Path(str(value or ""))
    if not str(path):
        raise ResearchDataError("namespace-repair configuration contains an empty path")
    return path if path.is_absolute() else (config_path.parent / path).resolve()


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_directory_noreplace(source: Path, target: Path) -> None:
    """Atomically publish a directory while refusing even an empty target."""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise ResearchDataError("atomic no-replace directory publication is unavailable")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(target),
        1,
    )
    if result != 0:
        error = ctypes.get_errno()
        if error in (errno.EEXIST, errno.ENOTEMPTY):
            raise ResearchDataError(
                f"output appeared during atomic publication and was not overwritten: {target}"
            )
        if error in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise ResearchDataError(
                "atomic no-replace directory publication is unsupported"
            )
        raise OSError(error, os.strerror(error), str(target))
    _fsync_directory(target.parent)


def _publish_readonly_json_once(path: Path, payload: Any) -> None:
    """Atomically create a permanent sentinel without an overwrite race."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                )
                + "\n"
            )
            os.fchmod(handle.fileno(), 0o444)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise ResearchDataError(
                "namespace repair has already started and is permanently one-shot"
            ) from exc
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        if path.parent.exists():
            _fsync_directory(path.parent)


def _artifact(path: Path, *, published_path: Path | None = None) -> dict[str, Any]:
    return {
        "path": str((published_path or path).resolve()),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _verified_artifact(
    owner_path: Path,
    record: Mapping[str, Any],
    label: str,
    *,
    required_mode: int | None = None,
) -> Path:
    path = _resolve(owner_path, record.get("path"))
    expected = str(record.get("sha256") or "")
    if not path.is_file() or len(expected) != 64 or sha256_file(path) != expected:
        raise ResearchDataError(f"{label} SHA-256 mismatch")
    if "bytes" in record and path.stat().st_size != int(record.get("bytes", -1)):
        raise ResearchDataError(f"{label} byte-size mismatch")
    if required_mode is not None and path.stat().st_mode & 0o777 != required_mode:
        raise ResearchDataError(f"{label} must have mode {required_mode:04o}")
    return path


def _require_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise ResearchDataError(f"namespace-repair mismatch for {label}")


def _repair_identity(label_sha256: str) -> str:
    """Return an output-independent one-shot identity for one label vault."""

    return canonical_json_sha256(
        {
            "schema_version": 1,
            "artifact_type": "sealed_namespace_repair_once_identity",
            "sealed_label_sha256": label_sha256,
        }
    )


def _git_state() -> dict[str, Any]:
    """Return the exact tracked code state used for the authorized execution."""

    try:
        commit = subprocess.check_output(
            ("git", "rev-parse", "HEAD"),
            cwd=REPOSITORY_ROOT,
            stderr=subprocess.DEVNULL,
        ).decode("ascii").strip()
        status = subprocess.check_output(
            ("git", "status", "--porcelain=v1", "--untracked-files=all"),
            cwd=REPOSITORY_ROOT,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError, UnicodeError) as exc:
        raise ResearchDataError("cannot verify namespace-repair Git state") from exc
    return {
        "commit": commit,
        "tracked_worktree_clean": not bool(status.strip()),
        "status_sha256": hashlib.sha256(status).hexdigest(),
    }


def _runtime_versions() -> dict[str, str]:
    return {
        "python_version": sys.version,
        "numpy_version": np.__version__,
    }


def _validate_code_bundle(config_path: Path, config: Mapping[str, Any]) -> str:
    raw_bundle = config.get("code_bundle")
    if not isinstance(raw_bundle, list):
        raise ResearchDataError("namespace repair code_bundle must be an array")
    verified: dict[str, str] = {}
    for index, raw in enumerate(raw_bundle):
        record = _mapping(raw, f"code_bundle[{index}]")
        relative = str(record.get("repository_relative_path") or "")
        if relative in verified or relative not in CODE_BUNDLE_PATHS:
            raise ResearchDataError("namespace repair code_bundle is duplicated or unexpected")
        path = _verified_artifact(
            config_path, record, f"code_bundle[{relative}]"
        )
        try:
            actual_relative = path.relative_to(REPOSITORY_ROOT).as_posix()
        except ValueError as exc:
            raise ResearchDataError("namespace repair code_bundle escaped the repository") from exc
        _require_equal(actual_relative, relative, "code bundle path")
        verified[relative] = sha256_file(path)
    _require_equal(set(verified), set(CODE_BUNDLE_PATHS), "complete code bundle")
    fingerprint = canonical_json_sha256(
        [
            {"repository_relative_path": path, "sha256": verified[path]}
            for path in sorted(verified)
        ]
    )
    _require_equal(
        config.get("code_bundle_sha256"), fingerprint, "code bundle fingerprint"
    )
    return fingerprint


def _committed_pairs(commitment: Mapping[str, Any]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for section_name in ("sources", "variants"):
        section = _mapping(commitment.get(section_name), section_name)
        for record_value in section.values():
            record = _mapping(record_value, f"{section_name}[]")
            run = _mapping(record.get("run"), "committed run")
            manifest = _mapping(record.get("manifest"), "committed manifest")
            pair = (str(run.get("sha256") or ""), str(manifest.get("sha256") or ""))
            if any(len(value) != 64 for value in pair):
                raise ResearchDataError("prediction commitment contains invalid hashes")
            pairs.add(pair)
    return pairs


def _method_identity(sidecar: Mapping[str, Any]) -> dict[str, str]:
    method = _mapping(sidecar.get("method"), "run method")
    identity = {
        key: str(method[key])
        for key in (
            "model_revision",
            "provider_fingerprint",
            "implementation_revision",
        )
        if str(method.get(key) or "").strip()
    }
    if not identity:
        raise ResearchDataError("sealed run has no exact method identity")
    return identity


def load_namespace_mapping(
    path: Path,
    *,
    candidate_ids: Sequence[str],
    expected: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, int]]:
    """Load a catalog-wide bijection without consulting a sealed label file."""

    mapping: dict[str, str] = {}
    targets: set[str] = set()
    identity_count = 0
    remapped_count = 0
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise ResearchDataError(f"cannot open namespace crosswalk: {path}") from exc
    with handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ResearchDataError(
                    f"{path}:{line_number}: invalid namespace crosswalk JSON"
                ) from exc
            if not isinstance(row, Mapping):
                raise ResearchDataError(
                    f"{path}:{line_number}: expected a namespace mapping object"
                )
            source = str(row.get("source_venue_id") or "").strip()
            target = str(row.get("target_venue_id") or "").strip()
            method = str(row.get("mapping_method") or "")
            if not source or not target or method != MAPPING_METHOD:
                raise ResearchDataError("namespace crosswalk contains an invalid record")
            if source in mapping:
                raise ResearchDataError("namespace crosswalk contains a duplicate source")
            if target in targets:
                raise ResearchDataError("namespace crosswalk is not one-to-one")
            mapping[source] = target
            targets.add(target)
            if source == target:
                identity_count += 1
            else:
                remapped_count += 1
    candidate_set = set(candidate_ids)
    if len(candidate_set) != len(candidate_ids):
        raise ResearchDataError("frozen candidate IDs are duplicated")
    counts = {
        "source_count": len(mapping),
        "target_count": len(targets),
        "mapped_count": len(mapping),
        "distinct_target_count": len(targets),
        "identity_count": identity_count,
        "remapped_count": remapped_count,
        "unmapped_count": 0,
        "ambiguous_count": 0,
        "collision_count": len(mapping) - len(targets),
    }
    for name, actual in counts.items():
        if name in expected:
            _require_equal(actual, int(expected[name]), "crosswalk." + name)
    if targets != candidate_set:
        raise ResearchDataError(
            "namespace crosswalk targets do not exactly equal the frozen candidates"
        )
    return mapping, counts


def translate_bundle_qrels(
    bundle: DatasetBundle,
    *,
    namespace_mapping: Mapping[str, str],
    candidate_ids: Sequence[str],
) -> tuple[DatasetBundle, dict[str, int]]:
    """Translate one positive qrel per query and return aggregate-only audit data."""

    candidate_set = set(candidate_ids)
    translated: dict[str, dict[str, float]] = {}
    identity_count = 0
    remapped_count = 0
    for query in bundle.queries:
        relevance = bundle.qrels.get(query.query_id)
        if not isinstance(relevance, Mapping) or len(relevance) != 1:
            raise ResearchDataError(
                "sealed namespace repair requires exactly one qrel per query"
            )
        source, gain = next(iter(relevance.items()))
        if float(gain) <= 0:
            raise ResearchDataError("sealed namespace repair requires a positive qrel")
        target = namespace_mapping.get(str(source))
        if target is None:
            raise ResearchDataError(
                "sealed namespace repair has an unmapped gold identifier"
            )
        if target not in candidate_set:
            raise ResearchDataError(
                "sealed namespace repair resolved outside the frozen candidates"
            )
        translated[query.query_id] = {target: float(gain)}
        if target == source:
            identity_count += 1
        else:
            remapped_count += 1
    query_ids = tuple(query.query_id for query in bundle.queries)
    if len(translated) != len(query_ids) or set(translated) != set(query_ids):
        raise ResearchDataError("namespace repair changed the committed denominator")
    audit = {
        "query_count": len(query_ids),
        "mapped_query_count": len(translated),
        "identity_query_count": identity_count,
        "remapped_query_count": remapped_count,
        "unmapped_query_count": 0,
        "ambiguous_query_count": 0,
        "dropped_query_count": 0,
    }
    return DatasetBundle(bundle.queries, translated, bundle.source_rows), audit


def _load_blind_bundle_from_stable_file(
    path: Path, *, expected_sha256: str
) -> tuple[DatasetBundle, Mapping[str, Any]]:
    content, artifact = _read_bytes_stable(
        path,
        "blind query artifact",
        expected_sha256=expected_sha256,
    )
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise ResearchDataError("blind query artifact is not UTF-8") from exc
    queries: list[Query] = []
    source_rows: dict[str, Mapping[str, Any]] = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ResearchDataError(
                f"blind query artifact line {line_number} is invalid JSON"
            ) from exc
        if not isinstance(row, Mapping):
            raise ResearchDataError("blind query artifact contains a non-object row")
        forbidden = sorted(set(row) & BLIND_QUERY_LABEL_FIELDS)
        unknown = sorted(set(row) - BLIND_QUERY_ALLOWED_FIELDS)
        if forbidden or unknown:
            raise ResearchDataError("blind query artifact violates the closed schema")
        query_id = str(row.get("paper_id") or "").strip()
        if not query_id or query_id in source_rows:
            raise ResearchDataError("blind query artifact has an invalid query identity")
        publication_date = str(row.get("publication_date") or "").strip()[:10]
        parse_iso_date(publication_date, field_name="publication date")
        title = str(row.get("title") or "").strip()
        abstract = str(row.get("abstract") or "").strip()
        text_value = "\n".join(value for value in (title, abstract) if value)
        if not text_value:
            raise ResearchDataError("blind query artifact contains empty query text")
        constraints = row.get("user_constraints")
        if constraints is not None and not isinstance(constraints, Mapping):
            raise ResearchDataError("blind user_constraints must be an object")
        queries.append(
            Query(
                query_id=query_id,
                text=text_value,
                publication_date=publication_date,
                title=title,
                abstract=abstract,
                metadata={"language": row.get("language") or "unknown"},
            )
        )
        source_rows[query_id] = row
    queries.sort(key=lambda item: (item.publication_date, item.query_id))
    return DatasetBundle(tuple(queries), {}, source_rows), artifact


def _unseal_labels_from_stable_file(
    *,
    blind: DatasetBundle,
    label_path: Path,
    expected_label_sha256: str,
    expected_query_count: int,
) -> tuple[DatasetBundle, Mapping[str, Any]]:
    """Hash and parse the authorized label vault through one stable descriptor."""

    content, label_artifact = _read_bytes_stable(
        label_path,
        "sealed label vault",
        expected_sha256=expected_label_sha256,
        required_mode=0o600,
    )
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise ResearchDataError("sealed label vault is not UTF-8") from exc
    labels: dict[str, Mapping[str, Any]] = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ResearchDataError(
                f"sealed label vault line {line_number} is invalid JSON"
            ) from exc
        if not isinstance(row, Mapping):
            raise ResearchDataError("sealed label vault contains a non-object row")
        query_id = str(row.get("paper_id") or "").strip()
        gold = str(row.get("gold_journal_id") or "").strip()
        if not query_id or not gold or query_id in labels:
            raise ResearchDataError(
                "sealed label vault contains an empty or duplicate identity"
            )
        labels[query_id] = row
    query_ids = tuple(query.query_id for query in blind.queries)
    if (
        len(query_ids) != expected_query_count
        or len(labels) != expected_query_count
        or set(labels) != set(query_ids)
    ):
        raise ResearchDataError("sealed labels do not match the committed denominator")
    queries: list[Query] = []
    qrels: dict[str, dict[str, float]] = {}
    for query in blind.queries:
        row = labels[query.query_id]
        gold = str(row["gold_journal_id"])
        queries.append(
            Query(
                query_id=query.query_id,
                text=query.text,
                publication_date=query.publication_date,
                title=query.title,
                abstract=query.abstract,
                doi=str(row.get("doi") or ""),
                gold_venue_name=str(row.get("gold_journal_name") or ""),
                metadata={
                    "field": row.get("broad_field") or "unknown",
                    "quartile": row.get("gold_jcr_quartile") or "unknown",
                    "language": query.metadata.get("language") or "unknown",
                },
            )
        )
        qrels[query.query_id] = {gold: 1.0}
    return DatasetBundle(tuple(queries), qrels, blind.source_rows), label_artifact


def preflight_namespace_repair(
    config_path: Path,
    *,
    authorization_path: Path | None = None,
) -> RepairPreflight:
    """Validate every repair input before opening the already-unsealed labels."""

    config_path = config_path.resolve()
    config, repair_config_artifact = _read_object_stable(
        config_path, "namespace-repair configuration"
    )
    _require_equal(config.get("schema_version"), 1, "config schema_version")
    _require_equal(config.get("status"), REPAIR_STATUS, "config status")
    _require_equal(config.get("offline_only"), True, "offline_only")
    _require_equal(config.get("search_free"), True, "search_free")
    if "output_dir" in config:
        raise ResearchDataError(
            "namespace repair must inherit the original output directory"
        )

    implementation = _mapping(config.get("implementation"), "implementation")
    implementation_path = _verified_artifact(
        config_path, implementation, "repair implementation"
    )
    _require_equal(
        implementation_path,
        Path(__file__).resolve(),
        "executed repair implementation path",
    )
    code_bundle_sha256 = _validate_code_bundle(config_path, config)

    original_record = _mapping(config.get("original_evaluation"), "original_evaluation")
    original_config_record = _mapping(original_record.get("config"), "original config")
    original_config_path = _verified_artifact(
        config_path, original_config_record, "original evaluation config"
    )
    original_config = _read_object(original_config_path, "original evaluation config")
    _require_equal(original_config.get("schema_version"), 1, "original schema")
    _require_equal(
        original_config.get("status"),
        "predictions_committed_before_label_unseal",
        "original status",
    )
    _require_equal(original_config.get("offline_only"), True, "original offline_only")
    _require_equal(original_config.get("search_free"), True, "original search_free")
    preflight_record = _mapping(
        original_record.get("label_free_preflight"), "label-free preflight"
    )
    preflight_path = _verified_artifact(
        config_path, preflight_record, "original label-free preflight"
    )
    preflight = _read_object(preflight_path, "original label-free preflight")
    _require_equal(preflight.get("status"), "ready_for_single_label_access", "preflight status")
    _require_equal(preflight.get("label_content_parsed"), False, "preflight label boundary")
    preflight_config = _mapping(preflight.get("config"), "preflight config")
    _require_equal(
        preflight_config.get("sha256"),
        sha256_file(original_config_path),
        "preflight config hash",
    )

    first_attempt = _mapping(config.get("first_attempt"), "first_attempt")
    failure_record = _mapping(first_attempt.get("record"), "first-attempt record")
    failure_path = _verified_artifact(
        config_path,
        failure_record,
        "first-attempt failure record",
        required_mode=0o444,
    )
    failure = _read_object(failure_path, "first-attempt failure record")
    _require_equal(
        failure.get("artifact_type"),
        "sealed_evaluation_first_attempt_failure_record",
        "first failure artifact type",
    )
    _require_equal(failure.get("status"), FIRST_FAILURE_STATUS, "first failure status")
    failure_detail = _mapping(failure.get("failure"), "first failure detail")
    _require_equal(failure_detail.get("message"), FIRST_FAILURE_MESSAGE, "first failure message")
    _require_equal(failure_detail.get("label_access_completed"), True, "first label access")
    _require_equal(failure_detail.get("metrics_computed"), False, "first metrics state")
    _require_equal(
        failure_detail.get("evaluation_output_published"),
        False,
        "first output state",
    )
    access_record = _mapping(
        first_attempt.get("label_access_audit"), "first label-access audit"
    )
    label_access_audit_path = _verified_artifact(
        config_path,
        access_record,
        "first label-access audit",
        required_mode=0o444,
    )
    access_audit = _read_object(label_access_audit_path, "first label-access audit")
    _require_equal(access_audit.get("schema_version"), 1, "label-access schema")
    _require_equal(
        access_audit.get("artifact_type"),
        "sealed_label_access_audit",
        "label-access artifact type",
    )
    _require_equal(
        access_audit.get("predictions_committed_before_access"),
        True,
        "pre-access predictions",
    )
    _require_equal(access_audit.get("refit_after_access"), False, "refit boundary")
    _require_equal(
        access_audit.get("hyperparameter_change_after_access"),
        False,
        "hyperparameter boundary",
    )
    _require_equal(
        access_audit.get("reason"),
        "post-commit metric evaluation",
        "first label-access reason",
    )

    output_dir = _resolve(original_config_path, original_config.get("output_dir"))
    if output_dir.exists():
        raise ResearchDataError(
            f"namespace-repair output exists and will not be overwritten: {output_dir}"
        )
    prior_audits = sorted(output_dir.parent.glob(output_dir.name + ".label-access-*.json"))
    if prior_audits != [label_access_audit_path]:
        raise ResearchDataError(
            "namespace repair requires exactly the committed first label-access audit"
        )
    sealed_config = _mapping(original_config.get("sealed_test"), "sealed_test")
    sealed_manifest_path = _resolve(original_config_path, sealed_config.get("manifest"))
    if sha256_file(sealed_manifest_path) != str(sealed_config.get("manifest_sha256") or ""):
        raise ResearchDataError("sealed-test manifest changed after first access")
    sealed_manifest = _read_object(sealed_manifest_path, "sealed-test manifest")
    _require_equal(
        sealed_manifest.get("artifact_type"),
        "future_sealed_test",
        "sealed-test artifact type",
    )
    _require_equal(
        sealed_manifest.get("status"),
        "labels_sealed_predictions_pending",
        "sealed-test status",
    )
    dataset_record = _mapping(sealed_manifest.get("dataset"), "sealed dataset")
    blind_record = _mapping(dataset_record.get("blind_queries"), "blind queries")
    label_record = _mapping(dataset_record.get("sealed_labels"), "sealed labels")
    blind_path = Path(str(blind_record.get("path") or ""))
    label_path = Path(str(label_record.get("path") or ""))
    if (
        not blind_path.is_file()
        or sha256_file(blind_path) != str(blind_record.get("sha256") or "")
    ):
        raise ResearchDataError("blind query artifact changed after first access")
    expected_label_hash = str(label_record.get("sha256") or "")
    if (
        not label_path.is_file()
        or len(expected_label_hash) != 64
        or sha256_file(label_path) != expected_label_hash
    ):
        raise ResearchDataError("sealed label vault changed after first access")
    if label_path.stat().st_mode & 0o777 != 0o600:
        raise ResearchDataError("sealed label vault must retain mode 0600")
    repair_identity = _repair_identity(expected_label_hash)
    repair_start_path = GLOBAL_REPAIR_SENTINEL_ROOT / (repair_identity + ".json")
    if repair_start_path.exists():
        raise ResearchDataError(
            "namespace repair has already started for this label vault and is "
            "permanently one-shot"
        )
    access_label = _mapping(access_audit.get("label_file"), "accessed label file")
    _require_equal(
        Path(str(access_label.get("path") or "")).resolve(),
        label_path.resolve(),
        "accessed label path",
    )
    _require_equal(access_label.get("sha256"), expected_label_hash, "accessed label hash")
    _require_equal(
        int(access_label.get("bytes", -1)),
        label_path.stat().st_size,
        "accessed label bytes",
    )
    _require_equal(
        int(access_label.get("record_count", -1)),
        int(dataset_record.get("record_count", -2)),
        "accessed label denominator",
    )
    failure_artifacts = _mapping(failure.get("artifacts"), "first failure artifacts")
    _require_equal(
        _mapping(
            failure_artifacts.get("evaluation_config"), "failure evaluation config"
        ).get("sha256"),
        sha256_file(original_config_path),
        "failure evaluation config",
    )
    _require_equal(
        _mapping(
            failure_artifacts.get("label_free_preflight"), "failure preflight"
        ).get("sha256"),
        sha256_file(preflight_path),
        "failure preflight",
    )
    _require_equal(
        _mapping(
            failure_artifacts.get("label_access_audit"),
            "failure label-access audit",
        ).get("sha256"),
        sha256_file(label_access_audit_path),
        "failure label-access audit",
    )
    _require_equal(
        _mapping(
            failure_artifacts.get("sealed_labels"), "failure sealed labels"
        ).get("sha256"),
        expected_label_hash,
        "failure sealed labels",
    )

    blind, _blind_artifact = _load_blind_bundle_from_stable_file(
        blind_path,
        expected_sha256=str(blind_record.get("sha256") or ""),
    )
    query_ids = tuple(query.query_id for query in blind.queries)
    expected_query_count = int(dataset_record.get("record_count", -1))
    _require_equal(len(query_ids), expected_query_count, "query denominator")

    commitment_config = _mapping(
        original_config.get("prediction_commitment"), "prediction_commitment"
    )
    commitment_path = _resolve(original_config_path, commitment_config.get("path"))
    expected_commitment_hash = str(commitment_config.get("sha256") or "")
    if (
        not commitment_path.is_file()
        or len(expected_commitment_hash) != 64
        or sha256_file(commitment_path) != expected_commitment_hash
    ):
        raise ResearchDataError("prediction commitment changed after first access")
    commitment = _read_object(commitment_path, "prediction commitment")
    access_commitment = _mapping(
        access_audit.get("prediction_commitment"), "accessed prediction commitment"
    )
    _require_equal(
        access_commitment.get("sha256"),
        sha256_file(commitment_path),
        "accessed prediction commitment",
    )
    _require_equal(
        _mapping(
            failure_artifacts.get("sealed_test_manifest"),
            "failure sealed-test manifest",
        ).get("sha256"),
        sha256_file(sealed_manifest_path),
        "failure sealed-test manifest",
    )
    _require_equal(
        _mapping(
            failure_artifacts.get("prediction_commitment"),
            "failure prediction commitment",
        ).get("sha256"),
        sha256_file(commitment_path),
        "failure prediction commitment",
    )
    _require_equal(
        commitment.get("status"),
        "predictions_committed_before_label_access",
        "prediction commitment status",
    )
    committed_label = _mapping(
        commitment.get("label_vault_commitment"), "committed label vault"
    )
    _require_equal(committed_label.get("sha256"), expected_label_hash, "committed labels")
    _require_equal(committed_label.get("content_parsed"), False, "commitment label boundary")
    _require_equal(commitment.get("query_count"), len(query_ids), "committed denominator")
    _require_equal(
        commitment.get("query_ids_sha256"),
        ordered_ids_sha256(query_ids),
        "committed query ordering",
    )

    corpus_config = _mapping(original_config.get("corpus"), "corpus")
    profiles_path = _resolve(original_config_path, corpus_config.get("path"))
    corpus = tuple(
        load_jsonl_corpus(
            profiles_path,
            id_field=str(corpus_config.get("id_field") or "venue_id"),
            text_fields=tuple(corpus_config.get("text_fields") or ("name",)),
            snapshot_field=str(corpus_config.get("snapshot_field") or "snapshot_date"),
        )
    )
    candidate_ids = tuple(document.doc_id for document in corpus)
    sealed_freeze = _mapping(sealed_manifest.get("method_freeze"), "sealed freeze")
    frozen_candidates = _mapping(sealed_freeze.get("candidates"), "frozen candidates")
    _require_equal(len(candidate_ids), int(frozen_candidates.get("count", -1)), "candidates")
    _require_equal(
        ordered_ids_sha256(candidate_ids),
        frozen_candidates.get("ordered_ids_sha256"),
        "candidate ordering",
    )
    _require_equal(sha256_file(profiles_path), frozen_candidates.get("profiles_sha256"), "profiles")
    _require_equal(
        _mapping(
            failure_artifacts.get("frozen_candidate_profiles"),
            "failure frozen profiles",
        ).get("sha256"),
        sha256_file(profiles_path),
        "failure frozen profiles",
    )
    _require_equal(commitment.get("candidate_count"), len(candidate_ids), "committed candidates")
    _require_equal(
        commitment.get("candidate_ids_sha256"),
        ordered_ids_sha256(candidate_ids),
        "committed candidate ordering",
    )

    freeze_config_record = _mapping(sealed_manifest.get("config"), "freeze config")
    freeze_config_path = Path(str(freeze_config_record.get("path") or ""))
    if (
        not freeze_config_path.is_file()
        or sha256_file(freeze_config_path) != str(freeze_config_record.get("sha256") or "")
    ):
        raise ResearchDataError("sealed method-freeze config changed")
    freeze_config = _read_object(freeze_config_path, "sealed method-freeze config")
    method_freeze = _mapping(freeze_config.get("method_freeze"), "method freeze")
    for name in ("method_hyperparameters", "metrics", "statistics"):
        _require_equal(
            canonical_json_sha256(_mapping(method_freeze.get(name), name)),
            sealed_freeze.get(name + "_sha256"),
            "frozen " + name,
        )
    frozen_family = tuple(str(value) for value in method_freeze.get("method_family", ()))
    _require_equal(
        tuple(str(value) for value in sealed_freeze.get("method_family", ())),
        frozen_family,
        "sealed method family",
    )

    expected_binding = build_run_binding(
        dataset_path=blind_path,
        profiles_path=profiles_path,
        query_ids=query_ids,
        candidate_ids=candidate_ids,
        configuration=original_config,
        configuration_path=original_config_path,
    )
    committed_pairs = _committed_pairs(commitment)
    raw_methods = original_config.get("methods")
    if not isinstance(raw_methods, list) or len(raw_methods) < 2:
        raise ResearchDataError("original sealed evaluation requires at least two methods")
    method_order = tuple(
        str(_mapping(value, "methods[]").get("name") or "") for value in raw_methods
    )
    if any(not name for name in method_order) or len(set(method_order)) != len(
        method_order
    ):
        raise ResearchDataError("original sealed method names must be non-empty and unique")
    _require_equal(method_order, frozen_family, "ordered method family")
    runs: dict[str, Run] = {}
    method_records: dict[str, Any] = {}
    for raw in raw_methods:
        method_config = _mapping(raw, "methods[]")
        name = str(method_config.get("name") or "")
        run_path = _resolve(original_config_path, method_config.get("path"))
        manifest_path = _resolve(original_config_path, method_config.get("manifest_path"))
        run_hash = str(method_config.get("run_sha256") or "")
        manifest_hash = str(method_config.get("manifest_sha256") or "")
        if (
            sha256_file(run_path) != run_hash
            or sha256_file(manifest_path) != manifest_hash
            or (run_hash, manifest_hash) not in committed_pairs
        ):
            raise ResearchDataError(f"method {name!r} changed after prediction commitment")
        sidecar = _read_object(manifest_path, name + " sidecar")
        method_record = _mapping(sidecar.get("method"), name + " method")
        _require_equal(method_record.get("name"), name, name + " method identity")
        binding = _mapping(sidecar.get("binding"), name + " binding")
        generation = _mapping(binding.get("configuration"), name + " generation")
        runs[name] = load_score_run(
            run_path,
            expected_query_ids=query_ids,
            candidate_ids=candidate_ids,
            expected_binding=expected_binding,
            expected_manifest_sha256=manifest_hash,
            expected_configuration_sha256=str(generation.get("canonical_sha256") or ""),
            expected_method_identity=_method_identity(sidecar),
            manifest_path=manifest_path,
        )
        execution = _mapping(sidecar.get("execution"), name + " execution")
        _require_equal(execution.get("failed_query_count"), 0, name + " failures")
        _require_equal(execution.get("external_api_calls"), 0, name + " API calls")
        _require_equal(execution.get("search_free"), True, name + " Search boundary")
        sidecar_coverage = _mapping(sidecar.get("coverage"), name + " coverage")
        _require_equal(
            int(sidecar_coverage.get("top_k", -1)),
            int(
                _mapping(method_freeze.get("metrics"), "frozen metrics").get(
                    "retrieval_depth", -2
                )
            ),
            name + " retrieval depth",
        )
        method_records[name] = {
            "run": _artifact(run_path),
            "manifest": _artifact(manifest_path),
            "method": sidecar["method"],
            "execution": sidecar.get("execution"),
        }

    metrics_freeze = _mapping(method_freeze.get("metrics"), "frozen metrics")
    evaluation = _mapping(original_config.get("evaluation"), "evaluation")
    _require_equal(
        tuple(int(value) for value in evaluation.get("cutoffs", ())),
        tuple(int(value) for value in metrics_freeze.get("cutoffs", ())),
        "metric cutoffs",
    )
    statistics = _mapping(original_config.get("statistics"), "statistics")
    frozen_statistics = _mapping(method_freeze.get("statistics"), "frozen statistics")
    for name in (
        "comparison_family",
        "metric",
        "bootstrap_iterations",
        "permutation_iterations",
        "confidence",
        "seed",
    ):
        _require_equal(statistics.get(name), frozen_statistics.get(name), "statistics." + name)
    _require_equal(statistics.get("metric"), metrics_freeze.get("primary"), "primary metric")
    _require_equal(
        frozen_statistics.get("multiple_comparison_corrections"),
        ["holm_family_wise", "benjamini_hochberg_fdr"],
        "multiple-comparison corrections",
    )
    _require_equal(
        metrics_freeze.get("denominator_policy"),
        "all_300_queries_no_failure_removal",
        "denominator policy",
    )

    crosswalk_config = _mapping(config.get("namespace_crosswalk"), "namespace_crosswalk")
    crosswalk_record = _mapping(crosswalk_config.get("mapping"), "namespace mapping")
    crosswalk_path = _verified_artifact(
        config_path,
        crosswalk_record,
        "namespace mapping",
        required_mode=0o444,
    )
    crosswalk_manifest_record = _mapping(
        crosswalk_config.get("manifest"), "namespace manifest"
    )
    crosswalk_manifest_path = _verified_artifact(
        config_path,
        crosswalk_manifest_record,
        "namespace manifest",
        required_mode=0o444,
    )
    crosswalk_manifest = _read_object(crosswalk_manifest_path, "namespace manifest")
    _require_equal(
        crosswalk_manifest.get("artifact_type"),
        "sealed_venue_namespace_crosswalk",
        "crosswalk artifact type",
    )
    _require_equal(
        crosswalk_manifest.get("status"),
        "complete_label_free_exact_issn_bijection",
        "crosswalk status",
    )
    label_boundary = _mapping(
        crosswalk_manifest.get("label_boundary"), "crosswalk label boundary"
    )
    _require_equal(label_boundary.get("label_input_configured"), False, "crosswalk label input")
    _require_equal(label_boundary.get("label_files_opened"), 0, "crosswalk label files")
    _require_equal(label_boundary.get("label_content_parsed"), False, "crosswalk label parse")
    matching_policy = _mapping(
        crosswalk_manifest.get("matching_policy"), "crosswalk matching policy"
    )
    _require_equal(matching_policy.get("method"), MAPPING_METHOD, "crosswalk method")
    _require_equal(matching_policy.get("checksum_valid_issn_required"), True, "ISSN checksums")
    _require_equal(matching_policy.get("fuzzy_matching"), False, "fuzzy matching")
    _require_equal(matching_policy.get("journal_names_emitted"), False, "name matching")
    crosswalk_implementation = _mapping(
        crosswalk_manifest.get("implementation"), "crosswalk implementation"
    )
    _verified_artifact(
        crosswalk_manifest_path,
        crosswalk_implementation,
        "crosswalk implementation",
    )
    crosswalk_source = _mapping(crosswalk_manifest.get("source"), "crosswalk source")
    raw_source_artifacts = crosswalk_source.get("artifacts")
    if not isinstance(raw_source_artifacts, list) or not raw_source_artifacts:
        raise ResearchDataError("crosswalk source artifacts are missing")
    for index, raw_artifact in enumerate(raw_source_artifacts):
        _verified_artifact(
            crosswalk_manifest_path,
            _mapping(raw_artifact, f"crosswalk source artifacts[{index}]"),
            f"crosswalk source artifacts[{index}]",
        )
    crosswalk_target = _mapping(crosswalk_manifest.get("target"), "crosswalk target")
    _verified_artifact(
        crosswalk_manifest_path,
        _mapping(crosswalk_target.get("artifact"), "crosswalk target artifact"),
        "crosswalk target artifact",
    )
    _require_equal(
        crosswalk_source.get("namespace_sha256"),
        crosswalk_config.get("source_namespace_sha256"),
        "source namespace hash",
    )
    _require_equal(
        crosswalk_target.get("namespace_sha256"),
        crosswalk_config.get("target_namespace_sha256"),
        "target namespace hash",
    )
    expected_crosswalk = _mapping(crosswalk_config.get("expected"), "crosswalk expected")
    namespace_mapping, namespace_counts = load_namespace_mapping(
        crosswalk_path,
        candidate_ids=candidate_ids,
        expected=expected_crosswalk,
    )
    coverage = _mapping(crosswalk_manifest.get("counts"), "crosswalk coverage")
    manifest_counts = {
        "source_count": int(coverage.get("source", -1)),
        "target_count": int(coverage.get("target", -1)),
        "mapped_count": int(coverage.get("mapped", -1)),
        "distinct_target_count": int(coverage.get("distinct_target", -1)),
        "identity_count": int(coverage.get("identity", -1)),
        "remapped_count": int(coverage.get("remapped", -1)),
        "ambiguous_count": int(coverage.get("ambiguous", -1)),
        "collision_count": int(coverage.get("collision", -1)),
    }
    for name, actual in namespace_counts.items():
        if name in manifest_counts:
            _require_equal(manifest_counts[name], actual, "crosswalk manifest " + name)
    _require_equal(int(coverage.get("source_unmapped", -1)), 0, "source unmapped")
    _require_equal(int(coverage.get("target_unmapped", -1)), 0, "target unmapped")
    output_record = _mapping(
        crosswalk_manifest.get("mapping_artifact"), "crosswalk output"
    )
    _require_equal(
        Path(str(output_record.get("path") or "")).resolve(),
        crosswalk_path.resolve(),
        "crosswalk output path",
    )
    _require_equal(output_record.get("sha256"), sha256_file(crosswalk_path), "crosswalk output hash")
    _require_equal(
        int(output_record.get("bytes", -1)),
        crosswalk_path.stat().st_size,
        "crosswalk output bytes",
    )
    _require_equal(
        int(output_record.get("record_count", -1)),
        len(namespace_mapping),
        "crosswalk output record count",
    )
    crosswalk_expectations = _mapping(
        crosswalk_manifest.get("expectations"), "crosswalk expectations"
    )
    _require_equal(
        crosswalk_expectations.get("source_namespace_sha256"),
        crosswalk_config.get("source_namespace_sha256"),
        "expected source namespace",
    )
    _require_equal(
        crosswalk_expectations.get("target_namespace_sha256"),
        crosswalk_config.get("target_namespace_sha256"),
        "expected target namespace",
    )
    for manifest_name, config_name in (
        ("source_count", "source_count"),
        ("target_count", "target_count"),
        ("identity_count", "identity_count"),
        ("remap_count", "remapped_count"),
    ):
        _require_equal(
            int(crosswalk_expectations.get(manifest_name, -1)),
            int(expected_crosswalk.get(config_name, -2)),
            "crosswalk expectation " + manifest_name,
        )

    authorization: Mapping[str, Any] = {}
    authorization_artifact: Mapping[str, Any] | None = None
    resolved_authorization_path: Path | None = None
    if authorization_path is not None:
        resolved_authorization_path = authorization_path.resolve()
        authorization, authorization_artifact = _read_object_stable(
            resolved_authorization_path,
            "repair authorization",
            required_mode=0o444,
        )
        _require_equal(authorization.get("schema_version"), 1, "authorization schema")
        _require_equal(
            authorization.get("status"), AUTHORIZATION_STATUS, "authorization status"
        )
        _require_equal(
            authorization.get("repair_config_sha256"),
            repair_config_artifact["sha256"],
            "authorized repair config",
        )
        _require_equal(
            authorization.get("original_evaluation_config_sha256"),
            sha256_file(original_config_path),
            "authorized original evaluation config",
        )
        _require_equal(
            authorization.get("sealed_label_sha256"),
            expected_label_hash,
            "authorized label vault",
        )
        _require_equal(
            authorization.get("output_dir"),
            str(output_dir.resolve()),
            "authorized output directory",
        )
        _require_equal(
            authorization.get("repair_identity"),
            repair_identity,
            "authorized repair identity",
        )
        _require_equal(
            authorization.get("prediction_commitment_sha256"),
            sha256_file(commitment_path),
            "authorized prediction commitment",
        )
        _require_equal(
            authorization.get("namespace_crosswalk_sha256"),
            sha256_file(crosswalk_path),
            "authorized namespace crosswalk",
        )
        _require_equal(
            authorization.get("code_bundle_sha256"),
            code_bundle_sha256,
            "authorized code bundle",
        )
        runtime_versions = _runtime_versions()
        _require_equal(
            authorization.get("python_version"),
            runtime_versions["python_version"],
            "authorized Python version",
        )
        _require_equal(
            authorization.get("numpy_version"),
            runtime_versions["numpy_version"],
            "authorized NumPy version",
        )
        git_state = _git_state()
        _require_equal(
            authorization.get("runtime_git_commit"),
            git_state["commit"],
            "authorized runtime Git commit",
        )
        _require_equal(
            authorization.get("tracked_worktree_clean_required"),
            True,
            "authorized clean-worktree boundary",
        )
        _require_equal(
            git_state["tracked_worktree_clean"],
            True,
            "runtime tracked worktree cleanliness",
        )
        scope = _mapping(authorization.get("scope"), "authorization scope")
        _require_equal(scope.get("semantic_label_reads"), 1, "authorized semantic reads")
        _require_equal(scope.get("query_denominator"), len(query_ids), "authorized denominator")
        _require_equal(scope.get("search_calls"), 0, "authorized Search calls")
        _require_equal(scope.get("llm_calls"), 0, "authorized LLM calls")
        _require_equal(scope.get("embedding_calls"), 0, "authorized embedding calls")
        _require_equal(
            scope.get("estimated_external_cost_usd"), 0, "authorized external cost"
        )
        _require_equal(
            scope.get("allow_method_or_statistic_changes"),
            False,
            "method-change boundary",
        )
        if not str(authorization.get("authorization_reference") or "").strip():
            raise ResearchDataError("repair authorization has no user reference")

    return RepairPreflight(
        repair_config_path=config_path,
        repair_config=config,
        repair_config_artifact=repair_config_artifact,
        original_config_path=original_config_path,
        original_config=original_config,
        original_output_dir=output_dir,
        label_access_audit_path=label_access_audit_path,
        label_path=label_path,
        label_sha256=expected_label_hash,
        blind_path=blind_path,
        blind_bundle=blind,
        sealed_manifest_path=sealed_manifest_path,
        sealed_manifest=sealed_manifest,
        commitment_path=commitment_path,
        commitment=commitment,
        profiles_path=profiles_path,
        corpus=corpus,
        candidate_ids=candidate_ids,
        query_ids=query_ids,
        runs=runs,
        method_order=method_order,
        method_records=method_records,
        namespace_mapping=namespace_mapping,
        namespace_manifest_path=crosswalk_manifest_path,
        namespace_manifest=crosswalk_manifest,
        authorization_path=resolved_authorization_path,
        authorization=authorization,
        authorization_artifact=authorization_artifact,
        repair_identity=repair_identity,
        repair_start_path=repair_start_path,
    )


def namespace_repair_readiness(config_path: Path) -> dict[str, Any]:
    """Return an aggregate, label-content-free readiness report."""

    preflight = preflight_namespace_repair(config_path)
    crosswalk_counts = _mapping(
        preflight.namespace_manifest.get("counts"), "crosswalk counts"
    )
    statistics = _mapping(
        preflight.original_config.get("statistics"), "statistics"
    )
    evaluation = _mapping(preflight.original_config.get("evaluation"), "evaluation")
    return {
        "schema_version": 1,
        "artifact_type": "sealed_namespace_repair_label_free_preflight",
        "status": "ready_for_explicit_second_label_access_authorization",
        "label_content_parsed": False,
        "repair_config": dict(preflight.repair_config_artifact),
        "original_evaluation_config": _artifact(preflight.original_config_path),
        "first_label_access_audit": _artifact(
            preflight.label_access_audit_path
        ),
        "sealed_label_vault": {
            "path": str(preflight.label_path.resolve()),
            "sha256": preflight.label_sha256,
            "bytes": preflight.label_path.stat().st_size,
            "private_mode": oct(preflight.label_path.stat().st_mode & 0o777),
            "content_parsed": False,
        },
        "namespace_crosswalk_manifest": _artifact(
            preflight.namespace_manifest_path
        ),
        "namespace_crosswalk_coverage": dict(crosswalk_counts),
        "coverage": {
            "query_count": len(preflight.query_ids),
            "candidate_count": len(preflight.candidate_ids),
            "method_count": len(preflight.method_order),
            "method_order": list(preflight.method_order),
        },
        "protocol": {
            "cutoffs": list(evaluation.get("cutoffs", ())),
            "metric": statistics.get("metric"),
            "bootstrap_iterations": statistics.get("bootstrap_iterations"),
            "permutation_iterations": statistics.get("permutation_iterations"),
            "confidence": statistics.get("confidence"),
            "seed": statistics.get("seed"),
            "comparison_family": statistics.get("comparison_family"),
            "qrel_change_only": True,
            "mapping_method": MAPPING_METHOD,
            "runtime_versions": _runtime_versions(),
        },
        "output_dir": str(preflight.original_output_dir.resolve()),
        "repair_identity": preflight.repair_identity,
        "global_repair_start_path": str(preflight.repair_start_path.resolve()),
        "ordinary_evaluator_retry_permitted": False,
        "repair_start_audit_present": False,
        "second_semantic_label_read_authorized": False,
        "explicit_user_authorization_required": True,
        "pristine_single_pass_sealed_test": False,
    }


def evaluate_post_access_namespace_repair(
    config_path: Path,
    *,
    authorization_path: Path,
    generation_command: Sequence[str],
) -> dict[str, Any]:
    """Run the one authorized post-access repair exactly once.

    All label-independent checks complete before the immutable repair-start
    sentinel is published.  The sentinel is written before the second semantic
    label read and permanently prevents another repair attempt, even on crash.
    """

    preflight = preflight_namespace_repair(
        config_path,
        authorization_path=authorization_path,
    )
    if preflight.authorization_path is None:  # pragma: no cover - API contract
        raise ResearchDataError("namespace repair requires explicit authorization")
    if preflight.authorization_artifact is None:  # pragma: no cover - API contract
        raise ResearchDataError("namespace repair authorization was not stably read")
    started_at = datetime.now(timezone.utc).isoformat()
    repair_start = {
        "schema_version": 1,
        "artifact_type": "sealed_namespace_repair_start_audit",
        "status": "started_one_shot_post_access_namespace_repair",
        "started_at": started_at,
        "access_sequence": 2,
        "repair_identity": preflight.repair_identity,
        "reason": "deterministic catalog-wide venue-ID namespace translation",
        "original_label_access_audit": _artifact(preflight.label_access_audit_path),
        "repair_config": dict(preflight.repair_config_artifact),
        "authorization": dict(preflight.authorization_artifact),
        "sealed_label_vault": {
            "path": str(preflight.label_path.resolve()),
            "sha256": preflight.label_sha256,
            "bytes": preflight.label_path.stat().st_size,
        },
        "namespace_crosswalk_manifest": _artifact(
            preflight.namespace_manifest_path
        ),
        "predictions_changed": False,
        "methods_changed": False,
        "statistics_changed": False,
        "candidate_universe_changed": False,
        "denominator_policy_changed": False,
        "frozen_after_first_access_before_metric_computation": True,
        "pristine_single_pass_sealed_test": False,
    }
    _publish_readonly_json_once(preflight.repair_start_path, repair_start)

    output_dir = preflight.original_output_dir
    staging: Path | None = None
    stage = "repair_staging_creation"
    mapping_audit: dict[str, Any] | None = None
    metrics_computed = False
    try:
        staging = output_dir.with_name(
            "."
            + output_dir.name
            + ".namespace-repair-building-"
            + uuid.uuid4().hex[:12]
        )
        staging.mkdir()
        stage = "second_semantic_label_read"
        unsealed_bundle, stable_label_artifact = _unseal_labels_from_stable_file(
            blind=preflight.blind_bundle,
            label_path=preflight.label_path,
            expected_label_sha256=preflight.label_sha256,
            expected_query_count=len(preflight.query_ids),
        )
        stage = "catalog_wide_qrel_namespace_translation"
        bundle, mapping_counts = translate_bundle_qrels(
            unsealed_bundle,
            namespace_mapping=preflight.namespace_mapping,
            candidate_ids=preflight.candidate_ids,
        )
        if mapping_counts["query_count"] != len(preflight.query_ids):
            raise ResearchDataError("namespace repair changed the 300-query denominator")
        mapping_audit = {
            "schema_version": 1,
            "artifact_type": "sealed_qrel_namespace_mapping_audit",
            "status": "complete_full_denominator_exact_mapping",
            "mapping_method": MAPPING_METHOD,
            "query_level_mapping_values_disclosed": False,
            "catalog_crosswalk_manifest": _artifact(
                preflight.namespace_manifest_path
            ),
            "stable_label_read": dict(stable_label_artifact),
            "coverage": mapping_counts,
            "invariants": {
                "query_text_changed": False,
                "query_order_changed": False,
                "gain_changed": False,
                "candidate_universe_changed": False,
                "prediction_changed": False,
                "method_changed": False,
                "statistical_protocol_changed": False,
                "fuzzy_or_name_fallback_used": False,
            },
        }
        mapping_audit_path = staging / "namespace_mapping_audit.json"
        _atomic_json(mapping_audit_path, mapping_audit)

        stage = "critical_leakage_audit"
        split = TemporalSplit(
            train=(),
            validation=(),
            test=preflight.query_ids,
            excluded=(),
        )
        leakage = audit_leakage(
            bundle,
            preflight.corpus,
            split,
            evaluation_splits=("test",),
            corpus_views=("document", "prototypes"),
        )
        leakage_path = staging / "leakage_audit.json"
        _atomic_json(leakage_path, leakage)
        if not leakage["passed"]:
            raise ResearchDataError(
                "critical leakage found after namespace repair; metrics were not computed"
            )

        stage = "frozen_metric_computation"
        evaluation_config = _mapping(
            preflight.original_config.get("evaluation"), "evaluation"
        )
        cutoffs = tuple(
            int(value)
            for value in evaluation_config.get("cutoffs", (1, 3, 5, 10, 20, 50))
        )
        evaluations = {
            name: evaluate_run(
                preflight.runs[name],
                bundle.qrels,
                query_ids=preflight.query_ids,
                ks=cutoffs,
            )
            for name in preflight.method_order
        }
        metrics_computed = True
        queries_by_id = {query.query_id: query for query in bundle.queries}
        strata = build_query_strata(
            query_ids=preflight.query_ids,
            qrels=bundle.qrels,
            queries=queries_by_id,
            corpus=preflight.corpus,
        )
        strata_summary = summarize_strata(
            strata, query_count=len(preflight.query_ids)
        )
        method_strata = {
            name: {
                dimension: stratified_metrics(evaluations[name], assignments)
                for dimension, assignments in strata.items()
            }
            for name in preflight.method_order
        }

        statistics_config = _mapping(
            preflight.original_config.get("statistics"), "statistics"
        )
        metric = str(statistics_config.get("metric") or "ndcg@10")
        bootstrap_iterations = int(
            statistics_config.get("bootstrap_iterations", 2000)
        )
        permutation_iterations = int(
            statistics_config.get("permutation_iterations", 2000)
        )
        confidence = float(statistics_config.get("confidence", 0.95))
        seed = int(statistics_config.get("seed", 20260828))
        if statistics_config.get("comparison_family") != "all_methods_unordered_pairs":
            raise ResearchDataError(
                "sealed comparison family must retain every frozen method pair"
            )
        comparisons_payload: dict[str, Any] = {}
        raw_p: dict[str, float] = {}
        for pair_index, (left, right) in enumerate(
            combinations(preflight.method_order, 2)
        ):
            identity = left + "__vs__" + right
            bootstrap = paired_bootstrap_ci(
                evaluations[left]["per_query"],
                evaluations[right]["per_query"],
                metric=metric,
                iterations=bootstrap_iterations,
                confidence=confidence,
                seed=seed + pair_index,
            )
            permutation = paired_permutation_test(
                evaluations[left]["per_query"],
                evaluations[right]["per_query"],
                metric=metric,
                iterations=permutation_iterations,
                seed=seed + pair_index,
            )
            comparisons_payload[identity] = {
                "left": left,
                "right": right,
                "bootstrap": bootstrap,
                "permutation": permutation,
            }
            raw_p[identity] = float(permutation["two_sided_p_value"])
        corrections = adjust_p_values(raw_p)
        for identity, adjusted in corrections.items():
            comparisons_payload[identity]["multiple_comparison_correction"] = adjusted

        stage = "atomic_artifact_publication"
        metrics = {
            "schema_version": 1,
            "artifact_type": "post_access_namespace_repaired_sealed_retrieval_metrics",
            "status": "complete_full_denominator",
            "pristine_single_pass_sealed_test": False,
            "primary_metric": metric,
            "query_count": len(preflight.query_ids),
            "candidate_count": len(preflight.candidate_ids),
            "method_order": list(preflight.method_order),
            "methods": {
                name: {
                    "aggregate": evaluations[name]["aggregate"],
                    "per_query": evaluations[name]["per_query"],
                    "stratified": method_strata[name],
                }
                for name in preflight.method_order
            },
            "strata": strata_summary,
            "paired_comparisons": comparisons_payload,
            "statistics": {
                "metric": metric,
                "bootstrap_iterations": bootstrap_iterations,
                "permutation_iterations": permutation_iterations,
                "confidence": confidence,
                "seed": seed,
                "comparison_family": "all_methods_unordered_pairs",
                "pair_count": len(comparisons_payload),
                "corrections": ["Holm family-wise", "Benjamini-Hochberg FDR"],
            },
        }
        metrics_path = staging / "metrics.json"
        _atomic_json(metrics_path, metrics)

        first_attempt = _mapping(
            preflight.repair_config.get("first_attempt"), "first_attempt"
        )
        failure_record = _mapping(first_attempt.get("record"), "failure record")
        failure_path = _resolve(preflight.repair_config_path, failure_record.get("path"))
        manifest = {
            "schema_version": 1,
            "artifact_type": "future_sealed_test_namespace_repaired_evaluation",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "complete_post_access_namespace_repaired_evaluation",
            "pristine_single_pass_sealed_test": False,
            "original_first_attempt": _artifact(failure_path),
            "original_evaluation_config": _artifact(
                preflight.original_config_path
            ),
            "repair_config": dict(preflight.repair_config_artifact),
            "sealed_test_manifest": _artifact(preflight.sealed_manifest_path),
            "prediction_commitment": _artifact(preflight.commitment_path),
            "first_label_access_audit": _artifact(
                preflight.label_access_audit_path
            ),
            "repair_start_audit": _artifact(preflight.repair_start_path),
            "authorization": dict(preflight.authorization_artifact),
            "namespace_crosswalk_manifest": _artifact(
                preflight.namespace_manifest_path
            ),
            "namespace_mapping_audit": _artifact(
                mapping_audit_path,
                published_path=output_dir / mapping_audit_path.name,
            ),
            "leakage_audit": _artifact(
                leakage_path,
                published_path=output_dir / leakage_path.name,
            ),
            "metrics": _artifact(
                metrics_path,
                published_path=output_dir / metrics_path.name,
            ),
            "methods": preflight.method_records,
            "coverage": {
                "query_count": len(preflight.query_ids),
                "full_denominator_retained": True,
                "failed_query_count": 0,
                "dropped_query_count": 0,
                "unmapped_query_count": 0,
                "ambiguous_query_count": 0,
                "method_count": len(preflight.method_order),
                "paired_comparison_count": len(comparisons_payload),
                "critical_leakage_count": int(
                    leakage["severity_counts"].get("critical", 0)
                ),
            },
            "change_audit": {
                "qrel_identifier_namespace_translated": True,
                "query_text_changed": False,
                "query_order_changed": False,
                "gain_changed": False,
                "predictions_changed": False,
                "methods_changed": False,
                "hyperparameters_changed": False,
                "statistics_changed": False,
                "candidate_universe_changed": False,
                "denominator_changed": False,
                "refit_after_access": False,
            },
            "generation": {
                "command": [str(value) for value in generation_command],
                "working_directory": str(Path.cwd().resolve()),
                "search_calls": 0,
                "llm_calls": 0,
                "embedding_calls": 0,
                "estimated_external_cost_usd": 0,
                "runtime_versions": _runtime_versions(),
            },
            "claim_boundary": (
                "This is an audited post-access, deterministic namespace-repaired "
                "future evaluation, not a pristine single-pass sealed test. The first "
                "attempt failed after label access and before metrics. Claims must retain "
                "the full denominator and follow all corrected null, negative, and "
                "positive comparisons without selection."
            ),
        }
        manifest_path = staging / "manifest.json"
        _atomic_json(manifest_path, manifest)
        _rename_directory_noreplace(staging, output_dir)
        return {
            **manifest,
            "manifest": _artifact(output_dir / "manifest.json"),
        }
    except Exception as exc:
        try:
            output_published = bool(
                staging is not None
                and not staging.exists()
                and output_dir.is_dir()
                and (output_dir / "manifest.json").is_file()
            )
            if staging is None or not staging.exists():
                staging = GLOBAL_REPAIR_SENTINEL_ROOT / (
                    ".failure-building-" + uuid.uuid4().hex
                )
                staging.mkdir(parents=True)
            failure_payload = {
                "schema_version": 1,
                "artifact_type": "sealed_namespace_repair_failure",
                "status": (
                    "published_with_durability_sync_failure"
                    if output_published
                    else "failed_closed_after_repair_start"
                ),
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "stage": stage,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "metrics_computed": metrics_computed,
                "output_published": output_published,
                "repair_start_audit": _artifact(preflight.repair_start_path),
                "full_denominator_required": len(preflight.query_ids),
                "retry_permitted": False,
            }
            _atomic_json(staging / "failure.json", failure_payload)
            if mapping_audit is not None and not (
                staging / "namespace_mapping_audit.json"
            ).exists():
                _atomic_json(
                    staging / "namespace_mapping_audit.json", mapping_audit
                )
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            failed = output_dir.with_name(
                output_dir.name
                + f".namespace-repair-failed-{timestamp}-{uuid.uuid4().hex[:8]}"
            )
            try:
                _rename_directory_noreplace(staging, failed)
            except (OSError, ResearchDataError):
                fallback = GLOBAL_REPAIR_SENTINEL_ROOT / (
                    preflight.repair_identity
                    + f".failed-{timestamp}-{uuid.uuid4().hex[:8]}"
                )
                _rename_directory_noreplace(staging, fallback)
        except Exception as preservation_error:
            raise ResearchDataError(
                "namespace repair failed and its post-sentinel failure artifact "
                f"could not be published: {type(preservation_error).__name__}"
            ) from exc
        raise


__all__ = [
    "AUTHORIZATION_STATUS",
    "FIRST_FAILURE_MESSAGE",
    "FIRST_FAILURE_STATUS",
    "MAPPING_METHOD",
    "REPAIR_STATUS",
    "RepairPreflight",
    "evaluate_post_access_namespace_repair",
    "load_namespace_mapping",
    "namespace_repair_readiness",
    "preflight_namespace_repair",
    "translate_bundle_qrels",
]
