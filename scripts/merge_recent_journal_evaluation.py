#!/usr/bin/env python3
"""Merge legacy pre-control shards into a diagnostic, non-formal report.

This utility exists only to preserve the historical ``shard-*/raw.jsonl``
workflow.  It cannot validate the immutable manifests, runtime bindings,
authorization ledger, or versioned closeouts used by the current evaluator and
therefore refuses every modern run layout.  Use ``evaluate_recent_journals``
directly for any formal 500-paper execution or resume.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Sequence

# When invoked as ``python scripts/merge_...py``, Python puts ``scripts/``
# rather than the project root on sys.path.  Add the root so the evaluator can
# be imported both as a module and as a standalone script.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.evaluate_recent_journals import (  # noqa: E402
    DEFAULT_PRELIMINARY_K,
    TRACKS,
    build_summary,
    load_dataset,
    render_markdown,
    summarize_records,
    stratified_summary,
)


LEGACY_MODE = "legacy_parallel_shards_diagnostic_nonformal"
RUN_MANIFEST_FILE = "run_manifest.json"
GENERATION_DIR = "raw_segments"
_CLOSEOUT_PATTERN = re.compile(r"closeout\.generation-\d{6}\.json")


class LegacyMergeError(RuntimeError):
    """The input cannot be represented by the legacy diagnostic contract."""


def _error_family(error: Any) -> str:
    text = str(error or "unknown").lower()
    if "search api 未提供" in text or "no usable" in text:
        return "search_no_evidence"
    if "http 429" in text or "rate" in text or "too many" in text:
        return "rate_limit"
    if "http 5" in text or "server error" in text:
        return "upstream_5xx"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "llm" in text:
        return "llm_error"
    return "other"


def _modern_markers(input_dir: Path) -> list[Path]:
    markers: set[Path] = set()
    if input_dir.name == GENERATION_DIR:
        markers.add(input_dir)
    try:
        for candidate in input_dir.rglob("*"):
            if (
                candidate.name == RUN_MANIFEST_FILE
                or candidate.name == GENERATION_DIR
                or _CLOSEOUT_PATTERN.fullmatch(candidate.name)
            ):
                markers.add(candidate)
    except OSError as exc:
        raise LegacyMergeError("cannot audit legacy input markers") from exc
    return sorted(markers, key=lambda path: path.as_posix())


def _validate_legacy_input(input_dir: Path) -> list[Path]:
    try:
        info = input_dir.lstat()
    except OSError as exc:
        raise LegacyMergeError(f"legacy input directory is unavailable: {input_dir}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise LegacyMergeError("legacy input must be a real directory")
    markers = _modern_markers(input_dir)
    if markers:
        rendered = ", ".join(str(path.relative_to(input_dir)) for path in markers[:5])
        raise LegacyMergeError(
            "refusing modern/formal evaluator input; use its versioned closeouts "
            f"directly instead of the legacy merger (markers: {rendered})"
        )
    paths = sorted(input_dir.glob("shard-*/raw.jsonl"))
    if not paths:
        raise LegacyMergeError("no legacy shard-*/raw.jsonl inputs found")
    for path in paths:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise LegacyMergeError(f"legacy shard input must be a regular file: {path}")
    return paths


def _read_records(paths: Sequence[Path]) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for path in paths:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise LegacyMergeError(
                        f"invalid JSON in {path} line {line_number}"
                    ) from exc
                if not isinstance(record, dict):
                    raise LegacyMergeError(
                        f"non-object record in {path} line {line_number}"
                    )
                if not str(record.get("run_id") or "").strip():
                    raise LegacyMergeError(
                        f"record without run_id in {path} line {line_number}"
                    )
                if not str(record.get("case_id") or "").strip():
                    raise LegacyMergeError(
                        f"record without case_id in {path} line {line_number}"
                    )
                if record.get("track") not in TRACKS:
                    raise LegacyMergeError(
                        f"record with invalid track in {path} line {line_number}"
                    )
                if record.get("status") not in {"ok", "error"}:
                    raise LegacyMergeError(
                        f"record with invalid status in {path} line {line_number}"
                    )
                key = (str(record.get("case_id")), str(record.get("track")))
                # Historical shards were occasionally rerun in place.  Keep
                # their latest-wins behavior, but the output remains non-formal.
                latest[key] = record
    return list(latest.values())


def _write_exclusive(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir",
        type=Path,
        required=True,
        help="Legacy shard root only; modern/formal evaluator outputs are rejected.",
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="A new directory for a diagnostic-only report; never overwritten.",
    )
    parser.add_argument("--preliminary-k", type=int, default=DEFAULT_PRELIMINARY_K)
    args = parser.parse_args(argv)

    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    try:
        shard_paths = _validate_legacy_input(input_dir)
    except (LegacyMergeError, OSError, UnicodeError) as exc:
        raise SystemExit(str(exc)) from exc
    if output_dir.exists():
        raise SystemExit(
            f"legacy diagnostic output already exists; refusing overwrite: {output_dir}"
        )
    try:
        records = _read_records(shard_paths)
        cases = load_dataset(args.dataset.resolve())
    except (LegacyMergeError, OSError, UnicodeError) as exc:
        raise SystemExit(str(exc)) from exc
    run_ids = sorted({str(record.get("run_id")) for record in records if record.get("run_id")})
    if len(run_ids) != 1:
        raise SystemExit(f"expected exactly one run_id, found {run_ids}")
    run_id = run_ids[0]
    expected_keys = {(case.case_id, track) for case in cases for track in TRACKS}
    actual_keys = {
        (str(record.get("case_id")), str(record.get("track"))) for record in records
    }
    unexpected = sorted(actual_keys - expected_keys)
    if unexpected:
        raise SystemExit(f"legacy shards contain unexpected case-track keys: {unexpected[:5]}")
    summary = build_summary(
        records,
        run_id=run_id,
        dataset=args.dataset,
        dataset_sha256=hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        expected_case_count=len(cases),
        tracks=TRACKS,
        preliminary_k=args.preliminary_k,
        interrupted=False,
        expected_case_ids=[case.case_id for case in cases],
    )
    summary["evaluation_mode"] = LEGACY_MODE
    summary["formal_full_denominator"] = False
    summary["claim_status"] = (
        "legacy diagnostic only; must not be reported as a formal 500-paper evaluation"
    )
    summary["shard_count"] = len(shard_paths)
    summary["unique_case_tracks_from_shards"] = len(records)
    summary["error_families"] = dict(
        Counter(
            _error_family(record.get("error"))
            for record in records
            if record.get("status") != "ok"
        )
    )
    for track in TRACKS:
        rows = [record for record in records if record.get("track") == track]
        successful = [record for record in rows if record.get("status") == "ok"]
        result = summary["track_results"][track]
        result["success_conditioned"] = summarize_records(
            successful, preliminary_k=args.preliminary_k
        )
        result["success_conditioned_by_quartile"] = stratified_summary(
            successful, "gold_jcr_quartile", preliminary_k=args.preliminary_k
        )
        result["success_conditioned_by_field"] = stratified_summary(
            successful, "primary_field", preliminary_k=args.preliminary_k
        )

    outcomes = summary["execution_outcomes"]
    if outcomes["missing"]:
        exit_code = 4
    elif outcomes["error"]:
        exit_code = 3
    else:
        exit_code = 0
    summary["exit_code"] = exit_code

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        output_dir.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise SystemExit(
            f"legacy diagnostic output already exists; refusing overwrite: {output_dir}"
        ) from exc
    _write_exclusive(
        output_dir / "summary.json",
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
    )
    _write_exclusive(
        output_dir / "summary.md",
        "# LEGACY DIAGNOSTIC ONLY — NOT A FORMAL EVALUATION\n\n"
        + "This report was merged from historical pre-control shards and must not "
        "be cited as a formal 500-paper result.\n\n"
        + render_markdown(summary),
    )
    print(json.dumps({
        "run_id": run_id,
        "records": len(records),
        "expected": len(cases) * len(TRACKS),
        "evaluation_mode": LEGACY_MODE,
        "formal_full_denominator": False,
        "exit_code": exit_code,
        "error_families": summary["error_families"],
        "summary": str(output_dir / "summary.json"),
    }, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
