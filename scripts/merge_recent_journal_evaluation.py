#!/usr/bin/env python3
"""Merge parallel recent-journal benchmark shards into one auditable report."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any

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


def _read_records(input_dir: Path) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for path in sorted(input_dir.glob("shard-*/raw.jsonl")):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                key = (str(record.get("case_id")), str(record.get("track")))
                # Shards are disjoint, but latest-wins makes reruns safe.
                latest[key] = record
    return list(latest.values())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--preliminary-k", type=int, default=DEFAULT_PRELIMINARY_K)
    args = parser.parse_args()

    records = _read_records(args.input_dir.resolve())
    cases = load_dataset(args.dataset.resolve())
    run_ids = sorted({str(record.get("run_id")) for record in records if record.get("run_id")})
    if len(run_ids) != 1:
        raise SystemExit(f"expected exactly one run_id, found {run_ids}")
    run_id = run_ids[0]
    summary = build_summary(
        records,
        run_id=run_id,
        dataset=args.dataset,
        dataset_sha256=__import__("hashlib").sha256(args.dataset.read_bytes()).hexdigest(),
        expected_case_count=len(cases),
        tracks=TRACKS,
        preliminary_k=args.preliminary_k,
        interrupted=False,
        expected_case_ids=[case.case_id for case in cases],
    )
    summary["evaluation_mode"] = "parallel_shards_skip_explanations"
    summary["shard_count"] = len(list(args.input_dir.glob("shard-*/raw.jsonl")))
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

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "summary.md").write_text(render_markdown(summary), encoding="utf-8")
    print(json.dumps({
        "run_id": run_id,
        "records": len(records),
        "expected": len(cases) * len(TRACKS),
        "error_families": summary["error_families"],
        "summary": str(output_dir / "summary.json"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
