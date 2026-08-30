from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from research.data import ResearchDataError, canonical_json_sha256, sha256_file
from research.sealed_namespace_crosswalk import (
    MAPPING_METHOD,
    build_sealed_namespace_crosswalk,
)
from scripts.build_recent_journal_benchmark import JournalVenue
from where_paper_go.recommender import CURATED_SCOPE_FILE, DATA_FILES


def _venue(venue_id: str, *issns: str) -> JournalVenue:
    return JournalVenue(
        venue_id=venue_id,
        entity_id=1,
        name="A name that must never be emitted",
        quartile="Q1",
        category="TEST",
        broad_field="test",
        issns=tuple(issns),
        lookup_issn="1016-3328",
    )


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


class SealedNamespaceCrosswalkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        for filename in (*DATA_FILES, CURATED_SCOPE_FILE):
            (self.data_dir / filename).write_text(
                f"fixed test input: {filename}\n", encoding="utf-8"
            )
        self.target = self.root / "venue_identity_crosswalk.jsonl"
        self.target_rows = [
            {
                "venue_id": "target-remap",
                "online_entity_id": 2,
                "status": "exact_issn",
                "issns": ["0254-0584"],
            },
            {
                "venue_id": "venue-same",
                "online_entity_id": 1,
                "status": "exact_issn",
                "issns": ["1016-3328"],
            },
        ]
        _write_jsonl(self.target, self.target_rows)
        self.source_venues = [
            _venue("venue-same", "10163328"),
            _venue("source-remap", "02540584"),
        ]
        self.mapping = self.root / "mapping.jsonl"
        self.manifest = self.root / "manifest.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _kwargs(self) -> dict[str, object]:
        source_namespace = [
            {"venue_id": "source-remap", "issns": ["02540584"]},
            {"venue_id": "venue-same", "issns": ["10163328"]},
        ]
        target_namespace = [
            {"venue_id": "target-remap", "issns": ["02540584"]},
            {"venue_id": "venue-same", "issns": ["10163328"]},
        ]
        return {
            "data_dir": self.data_dir,
            "target_identity_path": self.target,
            "mapping_output_path": self.mapping,
            "manifest_output_path": self.manifest,
            "expected_source_namespace_sha256": canonical_json_sha256(
                source_namespace
            ),
            "expected_target_file_sha256": sha256_file(self.target),
            "expected_target_namespace_sha256": canonical_json_sha256(
                target_namespace
            ),
            "expected_source_count": 2,
            "expected_target_count": 2,
            "expected_identity_count": 1,
            "expected_remap_count": 1,
            "generation_command": ["python", "-m", "test-crosswalk"],
        }

    def _build(self, **overrides: object) -> dict[str, object]:
        kwargs = self._kwargs()
        kwargs.update(overrides)
        with patch(
            "research.sealed_namespace_crosswalk.load_jcr_venues",
            return_value=(self.source_venues, set()),
        ) as loader:
            manifest = build_sealed_namespace_crosswalk(**kwargs)
        loader.assert_called_once_with(self.data_dir.resolve())
        return manifest

    def test_builds_complete_exact_issn_bijection_without_names(self) -> None:
        manifest = self._build()

        rows = [json.loads(line) for line in self.mapping.read_text().splitlines()]
        self.assertEqual(
            rows,
            [
                {
                    "mapping_method": MAPPING_METHOD,
                    "source_venue_id": "source-remap",
                    "target_venue_id": "target-remap",
                },
                {
                    "mapping_method": MAPPING_METHOD,
                    "source_venue_id": "venue-same",
                    "target_venue_id": "venue-same",
                },
            ],
        )
        self.assertNotIn("A name that must never be emitted", self.mapping.read_text())
        self.assertEqual(
            manifest["counts"],
            {
                "source": 2,
                "target": 2,
                "mapped": 2,
                "distinct_target": 2,
                "identity": 1,
                "remapped": 1,
                "source_unmapped": 0,
                "target_unmapped": 0,
                "ambiguous": 0,
                "collision": 0,
            },
        )
        self.assertFalse(manifest["label_boundary"]["label_content_parsed"])
        self.assertFalse(manifest["matching_policy"]["journal_names_emitted"])
        self.assertEqual(
            manifest["mapping_artifact"]["sha256"], sha256_file(self.mapping)
        )
        self.assertEqual(
            manifest["mapping_artifact"]["bytes"], self.mapping.stat().st_size
        )
        self.assertEqual(
            manifest["implementation"]["sha256"],
            sha256_file(Path("research/sealed_namespace_crosswalk.py")),
        )
        self.assertEqual(
            json.loads(self.manifest.read_text())["generation"]["command"],
            ["python", "-m", "test-crosswalk"],
        )

    def test_all_frozen_expectations_fail_closed_before_publication(self) -> None:
        cases = {
            "source_hash": {"expected_source_namespace_sha256": "0" * 64},
            "target_file_hash": {"expected_target_file_sha256": "0" * 64},
            "target_namespace_hash": {
                "expected_target_namespace_sha256": "0" * 64
            },
            "source_count": {
                "expected_source_count": 3,
                "expected_target_count": 3,
                "expected_identity_count": 1,
                "expected_remap_count": 2,
            },
            "target_count": {"expected_target_count": 3},
            "identity_count": {
                "expected_identity_count": 0,
                "expected_remap_count": 2,
            },
            "remap_count": {
                "expected_identity_count": 2,
                "expected_remap_count": 0,
            },
        }
        for label, overrides in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(ResearchDataError):
                    self._build(**overrides)
                self.assertFalse(self.mapping.exists())
                self.assertFalse(self.manifest.exists())

    def test_rejects_checksum_invalid_target_issn(self) -> None:
        self.target_rows[0]["issns"] = ["0254-0585"]
        _write_jsonl(self.target, self.target_rows)
        with self.assertRaisesRegex(ResearchDataError, "checksum-invalid"):
            self._build(expected_target_file_sha256=sha256_file(self.target))
        self.assertFalse(self.mapping.exists())
        self.assertFalse(self.manifest.exists())

    def test_rejects_non_unique_exact_issn_owner(self) -> None:
        self.target_rows[0]["issns"] = ["1016-3328"]
        _write_jsonl(self.target, self.target_rows)
        with self.assertRaisesRegex(ResearchDataError, "non-unique venue owners"):
            self._build(expected_target_file_sha256=sha256_file(self.target))
        self.assertFalse(self.mapping.exists())
        self.assertFalse(self.manifest.exists())

    def test_refuses_to_open_a_sealed_label_path(self) -> None:
        sealed = self.root / "labels.sealed.jsonl"
        sealed.write_text("deliberately not JSON\n", encoding="utf-8")
        with self.assertRaisesRegex(ResearchDataError, "sealed-label"):
            self._build(target_identity_path=sealed)
        self.assertFalse(self.mapping.exists())
        self.assertFalse(self.manifest.exists())


if __name__ == "__main__":
    unittest.main()
