#!/usr/bin/env python3
"""Merge a failed-request Tavily retry run over the original 500-case report."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.evaluate_recent_journals import (  # noqa: E402
    TRACKS,
    build_summary,
    load_dataset,
    render_markdown,
    summarize_records,
    stratified_summary,
)
from scripts.merge_recent_journal_evaluation import _error_family  # noqa: E402


def _read_raw(directory: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("**/raw.jsonl")):
        with path.open(encoding="utf-8") as handle:
            rows.extend(json.loads(line) for line in handle if line.strip())
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--retry-dir", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--preliminary-k", type=int, default=40)
    args = parser.parse_args()

    baseline = _read_raw(args.baseline_dir.resolve())
    retry = _read_raw(args.retry_dir.resolve())
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    for row in baseline:
        selected[(str(row.get("case_id")), str(row.get("track")))] = row
    baseline_count = len(selected)
    retry_keys: set[tuple[str, str]] = set()
    for row in retry:
        key = (str(row.get("case_id")), str(row.get("track")))
        selected[key] = row
        retry_keys.add(key)

    cases = load_dataset(args.dataset.resolve())
    expected_ids = [case.case_id for case in cases]
    run_ids = sorted({str(row.get("run_id")) for row in retry if row.get("run_id")})
    if not run_ids:
        raise SystemExit("retry output contains no run_id")
    run_id = f"tavily-retry-{run_ids[-1]}"
    records = []
    for row in selected.values():
        normalized = dict(row)
        normalized["run_id"] = run_id
        records.append(normalized)

    summary = build_summary(
        records,
        run_id=run_id,
        dataset=args.dataset,
        dataset_sha256=hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        expected_case_count=len(cases),
        tracks=TRACKS,
        preliminary_k=args.preliminary_k,
        interrupted=False,
        expected_case_ids=expected_ids,
    )
    summary.update(
        {
            "evaluation_mode": "baseline_plus_failed_tavily_retry",
            "baseline_unique_case_tracks": baseline_count,
            "retry_records": len(retry),
            "retry_unique_case_tracks": len(retry_keys),
            "retry_replaced_case_tracks": len(retry_keys),
            "retry_successes": sum(1 for row in retry if row.get("status") == "ok"),
            "retry_errors": sum(1 for row in retry if row.get("status") != "ok"),
            "error_families": dict(
                Counter(
                    _error_family(row.get("error"))
                    for row in records
                    if row.get("status") != "ok"
                )
            ),
        }
    )
    for track in TRACKS:
        rows = [row for row in records if row.get("track") == track]
        successful = [row for row in rows if row.get("status") == "ok"]
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

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "summary.md").write_text(render_markdown(summary), encoding="utf-8")
    (output / "raw.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in records),
        encoding="utf-8",
    )
    print(json.dumps({
        "run_id": run_id,
        "records": len(records),
        "retry_records": len(retry),
        "retry_successes": summary["retry_successes"],
        "retry_errors": summary["retry_errors"],
        "summary": str(output / "summary.json"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
