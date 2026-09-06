from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from research.data import (
    ResearchDataError,
    canonical_json_sha256,
    load_blind_query_dataset,
)
from research.sealed_test import (
    _failed_partial_output,
    split_labeled_dataset,
    verify_method_freeze,
)


def _labeled_row(identifier: str, day: int) -> dict[str, object]:
    return {
        "paper_id": identifier,
        "doi": identifier.removeprefix("doi:"),
        "title": "Frozen future query",
        "abstract": "A sufficiently detailed abstract for label-boundary testing.",
        "publication_date": f"2026-07-{day:02d}",
        "publication_date_precision": "day",
        "language": "en",
        "article_type": "journal-article",
        "gold_journal_id": "jcr-deadbeefdeadbeef",
        "gold_entity_id": 123,
        "gold_journal_name": "Synthetic Journal",
        "gold_container_title": "Synthetic Journal",
        "gold_issns": ["0007-9235"],
        "gold_jcr_quartile": "Q1",
        "gold_jcr_category": "ONCOLOGY",
        "broad_field": "clinical_medicine",
        "source": "crossref",
        "source_url": "https://doi.org/10.1/example",
    }


class SealedDatasetTests(unittest.TestCase):
    def test_failed_partial_dataset_is_restricted_and_inventoried(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / ".future.building"
            failed = root / "future.failed-audit"
            staging.mkdir()
            dataset = staging / "dataset.jsonl"
            dataset.write_text(json.dumps(_labeled_row("doi:10.1/a", 1)) + "\n")
            os.chmod(dataset, 0o644)
            manifest = {
                "dataset": {
                    "record_count": 1,
                    "complete": False,
                    "sha256": "a" * 64,
                }
            }
            (staging / "manifest.json").write_text(
                json.dumps(manifest) + "\n", encoding="utf-8"
            )

            report = _failed_partial_output(staging, failed)

            self.assertTrue(report["present"])
            self.assertFalse(report["accepted_as_formal_denominator"])
            self.assertEqual(report["dataset_summary"]["record_count"], 1)
            self.assertEqual(os.stat(dataset).st_mode & 0o777, 0o600)
            self.assertEqual(
                report["artifacts"]["dataset.jsonl"]["path"],
                str((failed / "dataset.jsonl").resolve()),
            )

    def test_split_physically_removes_every_label_field(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.jsonl"
            rows = [_labeled_row("doi:10.1/b", 2), _labeled_row("doi:10.1/a", 1)]
            source.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            blind = root / "queries.blind.jsonl"
            labels = root / "labels.sealed.jsonl"
            report = split_labeled_dataset(
                source,
                blind_path=blind,
                labels_path=labels,
                development_cutoff="2026-06-30",
                window_start="2026-07-01",
                window_end="2026-07-31",
            )
            self.assertEqual(report["record_count"], 2)
            self.assertFalse(report["label_values_returned_or_printed"])
            self.assertEqual(os.stat(labels).st_mode & 0o777, 0o600)
            blind_rows = [json.loads(line) for line in blind.read_text().splitlines()]
            self.assertNotIn("gold_journal_id", blind_rows[0])
            self.assertNotIn("broad_field", blind_rows[0])
            self.assertEqual(blind_rows[0]["user_constraints"], {})

            bundle = load_blind_query_dataset(blind)
            self.assertEqual([query.query_id for query in bundle.queries], ["doi:10.1/a", "doi:10.1/b"])
            self.assertEqual(bundle.qrels, {})
            self.assertTrue(all(not query.gold_venue_name for query in bundle.queries))

    def test_blind_loader_rejects_new_or_label_columns(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "queries.jsonl"
            row = {
                "paper_id": "doi:10.1/a",
                "title": "Title",
                "abstract": "Abstract",
                "publication_date": "2026-07-01",
                "publication_date_precision": "day",
                "language": "en",
                "article_type": "journal-article",
                "user_constraints": {},
                "gold_journal_id": "leak",
            }
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ResearchDataError, "label fields"):
                load_blind_query_dataset(path)

    def test_freeze_verifies_method_hyperparameters_and_source_protocol(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "artifact.json"
            artifact.write_text("{}\n", encoding="utf-8")
            method = {"top_k": 100, "profile_cutoff": "2026-03-31"}
            freeze = {
                "status": "frozen_before_future_data_acquisition",
                "commits": {"method": "a" * 40},
                "artifacts": [
                    {
                        "name": "artifact",
                        "path": artifact.name,
                        "sha256": "ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356",
                    }
                ],
                "candidates": {
                    "count": 20087,
                    "ordered_ids_sha256": "b" * 64,
                },
                "method_family": ["lightrag", "scope_rank_full"],
                "method_hyperparameters": method,
                "method_hyperparameters_canonical_sha256": canonical_json_sha256(
                    method
                ),
                "source_protocol": {"bm25": {"k1": 1.2}},
                "metrics": {"primary": "ndcg@10"},
                "statistics": {
                    "comparison_family": "all_methods_unordered_pairs"
                },
            }
            with patch("research.sealed_test._git_object_exists", return_value=True):
                verified = verify_method_freeze(root / "freeze.json", freeze)
            self.assertEqual(
                verified["method_hyperparameters_sha256"],
                canonical_json_sha256(method),
            )
            self.assertEqual(verified["method_family"], freeze["method_family"])
            self.assertEqual(len(verified["source_protocol_sha256"]), 64)

            freeze["method_hyperparameters_canonical_sha256"] = "0" * 64
            with patch("research.sealed_test._git_object_exists", return_value=True):
                with self.assertRaisesRegex(
                    ResearchDataError, "hyperparameters are not frozen"
                ):
                    verify_method_freeze(root / "freeze.json", freeze)


if __name__ == "__main__":
    unittest.main()
