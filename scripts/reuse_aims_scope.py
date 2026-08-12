#!/usr/bin/env python3
"""Reuse completed official aims/scope enrichment across normalized CSV files."""

from __future__ import annotations

import argparse
import csv
import re
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from where_paper_go.paths import DATA_DIR, PROJECT_ROOT


ROOT = PROJECT_ROOT

OUTPUT_COLUMNS = [
    "收稿方向_官网摘取",
    "收稿方向_来源URL",
    "收稿方向_证据",
    "收稿方向_置信度",
    "收稿方向_状态",
    "收稿方向_更新时间",
]


def normalize_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_title(value: str | None) -> str:
    value = normalize_space(value).lower()
    value = value.replace("&", " and ")
    value = re.sub(r"\b(the|acm|ieee|ifip|sigplan|sigops|sigda|sigbed|sigcomm)\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return normalize_space(value)


def normalize_abbr(value: str | None) -> str:
    value = normalize_space(value).lower()
    value = re.sub(r"[（(].*?[）)]", "", value)
    value = re.sub(r"[^a-z0-9]+", "", value)
    return value


def normalize_issn(value: str | None) -> str:
    value = normalize_space(value).lower()
    value = re.sub(r"[^0-9x]", "", value)
    return value


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_output_columns(fieldnames: list[str]) -> list[str]:
    fields = list(fieldnames)
    for column in OUTPUT_COLUMNS:
        if column not in fields:
            fields.append(column)
    return fields


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return ensure_output_columns(reader.fieldnames or []), list(reader)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> Path:
    backup_dir = path.parent / "_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{path.name}.bak-reuse-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    shutil.copy2(path, backup)
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)
    return backup


def row_keys(row: dict[str, str]) -> set[str]:
    keys: set[str] = set()
    record_type = row.get("record_type", "")
    name = normalize_title(row.get("name"))
    abbr = normalize_abbr(row.get("abbreviation"))
    if record_type == "journal":
        for issn in [normalize_issn(row.get("issn")), normalize_issn(row.get("eissn"))]:
            if issn:
                keys.add(f"journal:issn:{issn}")
        if name:
            keys.add(f"journal:name:{name}")
    elif record_type == "conference":
        if name:
            keys.add(f"conference:name:{name}")
        if abbr:
            keys.add(f"conference:abbr:{abbr}")
    return keys


def build_index(source_paths: list[Path]) -> dict[str, dict[str, str]]:
    buckets: dict[str, list[dict[str, str]]] = defaultdict(list)
    for path in source_paths:
        _fields, rows = read_csv(path)
        for row in rows:
            if normalize_space(row.get("收稿方向_状态")) != "ok":
                continue
            if not normalize_space(row.get("收稿方向_官网摘取")):
                continue
            for key in row_keys(row):
                buckets[key].append(row)

    index: dict[str, dict[str, str]] = {}
    for key, rows in buckets.items():
        urls = {normalize_space(row.get("收稿方向_来源URL")) for row in rows}
        summaries = {normalize_space(row.get("收稿方向_官网摘取")) for row in rows}
        if len(urls) == 1 and len(summaries) == 1:
            index[key] = rows[0]
    return index


def reusable_source(row: dict[str, str], index: dict[str, dict[str, str]]) -> dict[str, str] | None:
    matches = [index[key] for key in row_keys(row) if key in index]
    if not matches:
        return None
    urls = {normalize_space(row.get("收稿方向_来源URL")) for row in matches}
    summaries = {normalize_space(row.get("收稿方向_官网摘取")) for row in matches}
    if len(urls) == 1 and len(summaries) == 1:
        return matches[0]
    return None


def reuse_file(path: Path, index: dict[str, dict[str, str]], overwrite: bool) -> tuple[int, int, Path | None]:
    fieldnames, rows = read_csv(path)
    changed = 0
    selected = 0
    for row in rows:
        if not overwrite and (
            normalize_space(row.get("收稿方向_状态")) or normalize_space(row.get("收稿方向_官网摘取"))
        ):
            continue
        selected += 1
        source = reusable_source(row, index)
        if not source:
            continue
        for column in OUTPUT_COLUMNS:
            row[column] = source.get(column, "")
        row["收稿方向_更新时间"] = now_iso()
        changed += 1

    if not changed:
        return selected, changed, None
    return selected, changed, write_csv(path, fieldnames, rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", nargs="+", type=Path, required=True)
    parser.add_argument("--targets", nargs="+", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    index = build_index(args.sources)
    print(f"source_index_keys={len(index)}")
    for target in args.targets:
        selected, changed, backup = reuse_file(target, index, overwrite=args.overwrite)
        backup_text = backup.name if backup else ""
        print(f"{target}: selected={selected}, reused={changed}, backup={backup_text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
