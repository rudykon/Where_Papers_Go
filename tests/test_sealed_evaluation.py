from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.data import ResearchDataError, sha256_file
from research.sealed_evaluation import unseal_labels_after_prediction_commitment


class SealedLabelAccessTests(unittest.TestCase):
    def test_labels_join_only_against_exact_committed_queries(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            blind = root / "queries.blind.jsonl"
            blind.write_text(
                json.dumps(
                    {
                        "paper_id": "doi:10.1/a",
                        "title": "A query",
                        "abstract": "An abstract",
                        "publication_date": "2026-07-01",
                        "publication_date_precision": "day",
                        "language": "en",
                        "article_type": "journal-article",
                        "user_constraints": {},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            labels = root / "labels.sealed.jsonl"
            labels.write_text(
                json.dumps(
                    {
                        "paper_id": "doi:10.1/a",
                        "doi": "10.1/a",
                        "gold_journal_id": "jcr-a",
                        "gold_journal_name": "Journal A",
                        "broad_field": "computer_engineering",
                        "gold_jcr_quartile": "Q1",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            result = unseal_labels_after_prediction_commitment(
                blind_path=blind,
                label_path=labels,
                expected_label_sha256=sha256_file(labels),
                expected_query_count=1,
            )
            self.assertEqual(result.record_count, 1)
            self.assertEqual(result.bundle.qrels, {"doi:10.1/a": {"jcr-a": 1.0}})
            self.assertEqual(
                result.bundle.queries[0].metadata["field"],
                "computer_engineering",
            )

            with self.assertRaisesRegex(ResearchDataError, "commitment mismatch"):
                unseal_labels_after_prediction_commitment(
                    blind_path=blind,
                    label_path=labels,
                    expected_label_sha256="0" * 64,
                    expected_query_count=1,
                )


if __name__ == "__main__":
    unittest.main()
