from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.data import ResearchDataError, load_blind_query_dataset
from research.sealed_test import split_labeled_dataset


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


if __name__ == "__main__":
    unittest.main()
