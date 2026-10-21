#!/usr/bin/env python3
"""Build the public GitHub Pages catalog from explicitly allowed repository data.

This is an offline, standard-library-only export. It reuses the backend's entity
grouping and reviewed scope validation without initializing any retrieval model,
provider, cache, graph, or API configuration.
"""

from __future__ import annotations

import argparse
import csv
import ipaddress
import json
import re
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from where_paper_go import recommender  # noqa: E402


STATIC_FILES = (
    "index.html",
    "assets/styles.css",
    "assets/app.js",
    "assets/catalog.js",
    "assets/search-worker.js",
    "assets/search-engine.js",
    "assets/favicon.svg",
)
GENERATED_FILES = ("assets/catalog.json", ".nojekyll")
SOURCE_LABELS = {"ccf": "CCF", "th_cpl": "TH-CPL", "cas": "中科院", "jcr": "JCR"}
RANK_PREFIXES = {"ccf": "CCF", "th_cpl": "THCPL", "cas": "CAS", "jcr": "JCR"}
PUBLIC_VENUE_FIELDS = frozenset(
    {
        "id",
        "name",
        "abbreviation",
        "publishers",
        "issns",
        "record_type",
        "ranks",
        "source_ids",
        "areas",
        "scope",
        "keywords",
        "exclusions",
        "url",
        "scope_url",
        "scope_year",
        "article_types",
        "submission_mode",
        "reviewed",
    }
)


def public_url(value: str) -> str:
    """Keep only public HTTP(S) links, never credentials or local paths."""

    value = value.strip()
    if any(ord(char) < 32 or char == "\\" for char in value):
        return ""
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").casefold()
        # Browsers decode percent escapes and normalize Unicode hostnames before
        # interpreting IP literals. Do not allow escaped hosts; apply IDNA before
        # the public-address check so fullwidth digits and Unicode dots cannot
        # disguise a local address.
        if "%" in hostname or any(char.isspace() for char in hostname):
            return ""
        hostname = hostname.encode("idna").decode("ascii").rstrip(".")
        if (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or hostname == "localhost"
            or hostname.endswith((".localhost", ".local"))
        ):
            return ""
        parsed.port  # Validate malformed ports as well.
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            last_label = hostname.rsplit(".", 1)[-1]
            # WHATWG URL parsing accepts abbreviated, octal, hexadecimal and
            # integer IPv4 forms (e.g. 127.1 or 0177.0.0.1). Only canonical
            # literals accepted by ipaddress above are eligible for export.
            if (
                "." not in hostname
                or last_label.isdecimal()
                or last_label.startswith("0x")
            ):
                return ""
        else:
            if not address.is_global:
                return ""
    except (ValueError, UnicodeError):
        return ""
    return value


def normalize_issns(*values: str) -> list[str]:
    """Extract valid ISSN spellings from source fields, preserving source order."""

    tokens = (
        match.group().replace("-", "").upper()
        for value in values
        for match in re.finditer(
            r"(?<![0-9A-Za-z-])[0-9]{4}-?[0-9]{3}[0-9Xx](?![0-9A-Za-z-])", value
        )
    )
    return list(dict.fromkeys(f"{token[:4]}-{token[4:]}" for token in tokens))


def _record_metadata(data_dir: Path) -> dict[int, dict[str, object]]:
    """Read only public URL, publisher and ISSN fields in load_records order."""

    result: dict[int, dict[str, object]] = {}
    row_id = 0
    for filename in recommender.DATA_FILES:
        with (data_dir / filename).open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                result[row_id] = {
                    "url": public_url(row.get("url") or ""),
                    "publisher": recommender.normalize_space(row.get("publisher")),
                    "issns": normalize_issns(
                        row.get("issn") or "",
                        row.get("eissn") or "",
                        row.get("issn_eissn") or "",
                    ),
                }
                row_id += 1
    return result


def build_catalog(data_dir: Path = ROOT / "data") -> dict[str, object]:
    """Return a deterministic public schema, excluding automatic scope fields."""

    records = recommender.load_records(data_dir)
    scopes = {
        scope.scope_id: scope
        for scope in recommender.load_curated_scopes(data_dir).values()
    }
    record_metadata = _record_metadata(data_dir)
    targets = [
        recommender.TargetSpec(dataset, level)
        for dataset, level in sorted({record.target_key for record in records})
    ]
    candidates = recommender.build_candidates(records, targets, include_inactive=False)
    venues = []
    for candidate in candidates:
        reviewed_scopes = [
            scopes[scope_id]
            for scope_id in dict.fromkeys(
                record.curated_scope_id
                for record in candidate.records
                if record.curated_scope_status == "approved" and record.curated_scope_id
            )
        ]
        # The existing loader fails closed on multiple active scopes per entity.
        scope = reviewed_scopes[0] if reviewed_scopes else None
        rank_keys = sorted(
            {record.target_key for record in candidate.records},
            key=lambda key: (recommender.DATASET_ORDER[key[0]], key[1]),
        )
        venues.append(
            {
                "id": f"venue:{min(record.row_id for record in candidate.records)}",
                "name": candidate.name,
                "abbreviation": candidate.abbreviation,
                "publishers": list(
                    dict.fromkeys(
                        record_metadata[record.row_id]["publisher"]
                        for record in candidate.records
                        if record_metadata[record.row_id]["publisher"]
                    )
                ),
                "issns": list(
                    dict.fromkeys(
                        issn
                        for record in candidate.records
                        for issn in record_metadata[record.row_id]["issns"]
                    )
                ),
                "record_type": candidate.record_type,
                "ranks": [
                    f"{RANK_PREFIXES[dataset]}-{level}" for dataset, level in rank_keys
                ],
                "source_ids": list(dict.fromkeys(dataset for dataset, _ in rank_keys)),
                "areas": candidate.areas,
                "scope": " ".join(candidate.curated_scopes),
                "keywords": candidate.curated_topics,
                "exclusions": " ".join(candidate.curated_out_of_scope),
                "url": next(
                    (
                        record_metadata[record.row_id]["url"]
                        for record in candidate.records
                        if record_metadata[record.row_id]["url"]
                    ),
                    "",
                ),
                "scope_url": public_url(scope.source_url) if scope else "",
                "scope_year": scope.scope_year if scope else "",
                "article_types": candidate.curated_article_types,
                "submission_mode": scope.submission_mode if scope else "",
                "reviewed": bool(scope),
            }
        )

    source_counts = Counter((record.dataset, record.version_year) for record in records)
    return {
        "schema_version": 1,
        "sources": [
            {
                "id": dataset,
                "label": SOURCE_LABELS[dataset],
                "year": year,
                "records": count,
            }
            for (dataset, year), count in sorted(
                source_counts.items(),
                key=lambda item: (recommender.DATASET_ORDER[item[0][0]], item[0][1]),
            )
        ],
        "venues": venues,
        "stats": {
            "venues": len(venues),
            "reviewed": sum(venue["reviewed"] for venue in venues),
        },
    }


def _check_existing_output(output_dir: Path) -> None:
    """Refuse to overwrite unrelated files or follow links in a build directory."""

    if output_dir.is_symlink():
        raise ValueError("The Pages output directory must not be a symbolic link")
    if not output_dir.exists():
        return
    if not output_dir.is_dir():
        raise ValueError("The Pages output path must be a directory")
    allowed_files = set(STATIC_FILES + GENERATED_FILES)
    for path in output_dir.rglob("*"):
        relative = path.relative_to(output_dir).as_posix()
        if path.is_symlink() or (
            relative not in allowed_files and not (relative == "assets" and path.is_dir())
        ):
            raise ValueError(f"Unexpected existing Pages output entry: {relative}")


def build_site(
    output_dir: Path = ROOT / "dist" / "github-pages",
    *,
    website_dir: Path = ROOT / "website",
    data_dir: Path = ROOT / "data",
) -> dict[str, object]:
    """Copy exactly the reviewed static files and generated safe catalog."""

    if website_dir.is_symlink():
        raise ValueError("The website source directory must not be a symbolic link")
    website_dir = website_dir.resolve()
    for name in STATIC_FILES:
        source = website_dir / name
        relative_parents = source.relative_to(website_dir).parents
        if source.is_symlink() or any(
            (website_dir / parent).is_symlink() for parent in relative_parents
        ):
            raise ValueError(f"Website assets must not be symbolic links: {name}")
        if not source.is_file():
            raise FileNotFoundError(f"Missing required website asset: {name}")
    output_dir = output_dir.absolute()
    if output_dir == website_dir or output_dir in website_dir.parents:
        raise ValueError("The Pages output must not replace the website source")
    _check_existing_output(output_dir)
    catalog = build_catalog(data_dir)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".pages-build-", dir=output_dir.parent) as temp:
        staging = Path(temp) / "site"
        (staging / "assets").mkdir(parents=True)
        for name in STATIC_FILES:
            shutil.copyfile(website_dir / name, staging / name)
        (staging / "assets" / "catalog.json").write_text(
            json.dumps(catalog, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        (staging / ".nojekyll").touch()
        if output_dir.exists():
            shutil.rmtree(output_dir)
        staging.replace(output_dir)
    return catalog["stats"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist" / "github-pages",
        help="Output directory (default: dist/github-pages)",
    )
    args = parser.parse_args()
    stats = build_site(args.output)
    print(
        f"Built GitHub Pages: {stats['venues']:,} venues, "
        f"{stats['reviewed']:,} reviewed scopes."
    )


if __name__ == "__main__":
    main()
