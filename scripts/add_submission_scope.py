#!/usr/bin/env python3
"""Add/update the 收稿方向 column for normalized data CSV files.

The current data files already share a normalized schema. This script derives a
full-coverage submission-scope field from local structured classifications:
CCF/TH-CPL research areas, CAS subject areas, and JCR categories.
"""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

from where_paper_go.paths import DATA_DIR, PROJECT_ROOT


ROOT = PROJECT_ROOT
TARGETS = [
    DATA_DIR / "ccf_conferences_2026.csv",
    DATA_DIR / "th_cpl_partition_2019.csv",
    DATA_DIR / "cas_partition_2025.csv",
    DATA_DIR / "jcr_partition_2025.csv",
]


def clean(value: str | None) -> str:
    return " ".join((value or "").split())


def join_scope(parts: list[str]) -> str:
    seen = set()
    result = []
    for part in parts:
        value = clean(part)
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return "；".join(result)


def derive_scope(row: dict[str, str]) -> str:
    dataset = row.get("dataset", "")
    if dataset == "ccf":
        return join_scope([row.get("area", "")])
    if dataset == "th_cpl":
        return join_scope([row.get("area", ""), row.get("area_en", "")])
    if dataset == "cas":
        parts = []
        if row.get("cas_major_area"):
            parts.append(f"中科院大类：{row['cas_major_area']}")
        for i in range(1, 7):
            subject = row.get(f"cas_subject_{i}", "")
            if subject:
                parts.append(f"中科院小类：{subject}")
        return join_scope(parts)
    if dataset == "jcr":
        return join_scope(
            [
                f"JCR类别：{row.get(f'jcr_category_{i}', '')}"
                for i in range(1, 7)
                if row.get(f"jcr_category_{i}", "")
            ]
        )
    return join_scope([row.get("area", ""), row.get("area_en", "")])


def insert_scope_field(fieldnames: list[str]) -> list[str]:
    fields = [field for field in fieldnames if field != "收稿方向"]
    if "name" in fields:
        fields.insert(fields.index("name") + 1, "收稿方向")
    else:
        fields.append("收稿方向")
    return fields


def update_csv(path: Path) -> tuple[int, int, list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = reader.fieldnames or []

    output_fields = insert_scope_field(fieldnames)
    non_empty = 0
    for row in rows:
        scope = derive_scope(row)
        row["收稿方向"] = scope
        if scope:
            non_empty += 1

    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_fields)
        writer.writeheader()
        writer.writerows(rows)

    return len(rows), non_empty, output_fields


def update_manifest(fieldnames: list[str], stats: dict[str, dict[str, int]]) -> None:
    path = DATA_DIR / "manifest.json"
    manifest = {}
    if path.exists():
        manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["updated_at"] = date.today().isoformat()
    schema = manifest.setdefault("schema", {})
    schema["encoding"] = "UTF-8 with BOM"
    schema["delimiter"] = ","
    schema["field_count"] = len(fieldnames)
    schema["fields"] = fieldnames
    schema["submission_scope"] = (
        "The 收稿方向 column is derived from local structured classifications: "
        "CCF/TH-CPL research areas, CAS major/minor subject areas, and JCR categories. "
        "It is not copied from publisher aims-and-scope pages."
    )
    for item in manifest.get("outputs", []):
        file_name = Path(item.get("file", "")).name
        if file_name in stats:
            item["records"] = stats[file_name]["records"]
            item["submission_scope_non_empty"] = stats[file_name]["scope_non_empty"]
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def update_readme(stats: dict[str, dict[str, int]]) -> None:
    lines = [
        "# Data Files",
        "",
        "These CSV files are normalized from local workspace sources only.",
        "",
        "All four CSV files use the same header, UTF-8 BOM encoding, and comma delimiter. The `收稿方向` column is derived from local structured classifications: CCF/TH-CPL research areas, CAS major/minor subject areas, and JCR categories. It is not copied from publisher aims-and-scope pages.",
        "",
        "| File | Records | 收稿方向 Non-empty |",
        "| --- | ---: | ---: |",
    ]
    for path in TARGETS:
        stat = stats[path.name]
        lines.append(f"| `{path.name}` | {stat['records']} | {stat['scope_non_empty']} |")
    lines.extend(
        [
            "",
            "Scope derivation rules:",
            "",
            "- CCF: `area`.",
            "- TH-CPL: `area` plus `area_en`.",
            "- CAS: `cas_major_area` plus all non-empty `cas_subject_*` fields.",
            "- JCR: all non-empty `jcr_category_*` fields.",
        ]
    )
    (DATA_DIR / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    stats: dict[str, dict[str, int]] = {}
    common_fields: list[str] | None = None
    for path in TARGETS:
        records, non_empty, fields = update_csv(path)
        stats[path.name] = {"records": records, "scope_non_empty": non_empty}
        common_fields = fields if common_fields is None else common_fields
        print(f"{path}: {records} rows, {non_empty} scopes")

    if common_fields is None:
        return 0
    update_manifest(common_fields, stats)
    update_readme(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
