from __future__ import annotations

import unittest
from pathlib import Path
import tempfile

from scripts.evaluate_recent_journals import (
    audit_search_leakage,
    case_from_payload,
    gold_rank,
    normalized_title_similarity,
    prediction_matches_gold,
    ranking_metrics,
    _repair_and_load_jsonl,
    build_summary,
    summarize_records,
)


class GoldMatchingTests(unittest.TestCase):
    def test_builder_schema_maps_broad_field(self) -> None:
        case = case_from_payload(
            {
                "paper_id": "doi:10.1/example",
                "doi": "10.1/example",
                "title": "A title",
                "abstract": "A sufficiently informative abstract.",
                "gold_entity_id": 4,
                "gold_journal_name": "Journal of Tests",
                "gold_issns": ["1234-5678"],
                "gold_jcr_quartile": "Q2",
                "broad_field": "clinical_medicine",
            },
            1,
        )
        self.assertEqual(case.case_id, "doi:10.1/example")
        self.assertEqual(case.primary_field, "clinical_medicine")

    def test_entity_id_is_preferred_over_same_name(self) -> None:
        self.assertFalse(
            prediction_matches_gold(
                {"entity_id": 8, "name": "Journal of Tests"},
                7,
                "Journal of Tests",
            )
        )

    def test_missing_entity_ids_never_fall_back_to_name(self) -> None:
        self.assertFalse(
            prediction_matches_gold(
                {"name": "Signal & Image Journal"},
                None,
                "Signal and Image Journal",
            )
        )
        self.assertFalse(
            prediction_matches_gold(
                {"name": "Gold"},
                7,
                "Gold",
            )
        )

    def test_gold_rank_uses_first_match(self) -> None:
        predictions = [
            {"entity_id": 1, "name": "Other"},
            {"entity_id": 7, "name": "Gold"},
            {"entity_id": 7, "name": "Gold"},
        ]
        self.assertEqual(gold_rank(predictions, 7, "Gold"), 2)


class MetricTests(unittest.TestCase):
    def test_ranking_metrics_count_missing_as_misses(self) -> None:
        metrics = ranking_metrics([1, 3, 8, None])
        self.assertEqual(metrics["hits_at_1"], 1)
        self.assertEqual(metrics["hit_at_3"], 0.5)
        self.assertEqual(metrics["hit_at_10"], 0.75)
        self.assertAlmostEqual(metrics["mrr_at_10"], (1 + 1 / 3 + 1 / 8) / 4)

    def test_summary_keeps_errors_and_uncovered_cases_in_denominator(self) -> None:
        records = [
            {
                "status": "ok",
                "catalog_covered": True,
                "final_gold_rank": 1,
                "preliminary_gold_rank": 2,
                "recall_pool_gold_rank": 1,
                "latency_ms": 100,
                "preliminary_latency_ms": 40,
                "leakage": {
                    "any_leak": False,
                    "article_leak": False,
                    "gold_journal_mentioned": False,
                },
            },
            {
                "status": "error",
                "catalog_covered": True,
                "final_gold_rank": None,
                "preliminary_gold_rank": None,
                "recall_pool_gold_rank": None,
            },
            {
                "status": "ok",
                "catalog_covered": False,
                "final_gold_rank": None,
                "preliminary_gold_rank": None,
                "recall_pool_gold_rank": None,
                "latency_ms": 300,
                "leakage": {
                    "any_leak": True,
                    "article_leak": True,
                    "gold_journal_mentioned": False,
                },
            },
        ]
        summary = summarize_records(records)
        self.assertEqual(summary["catalog_covered"], 2)
        self.assertEqual(summary["errors"], 1)
        self.assertAlmostEqual(summary["final"]["hit_at_1"], 1 / 3)
        self.assertAlmostEqual(
            summary["coverage_conditioned"]["final"]["hit_at_1"], 1 / 2
        )
        self.assertAlmostEqual(summary["recall_pool"]["hit_at_40"], 1 / 3)
        self.assertEqual(summary["no_search_leak"]["case_count"], 1)
        self.assertEqual(summary["no_search_leak"]["final"]["hit_at_1"], 1.0)
        self.assertAlmostEqual(
            summary["search_leakage_safe_lower_bound"]["final"]["hit_at_1"],
            1 / 3,
        )
        self.assertEqual(summary["latency_ms"]["median"], 200)

    def test_partial_summary_counts_missing_case_tracks_as_errors(self) -> None:
        summary = build_summary(
            [],
            run_id="test",
            dataset=Path("dataset.jsonl"),
            dataset_sha256="abc",
            expected_case_count=2,
            tracks=("abstract_only",),
            preliminary_k=40,
            interrupted=True,
            expected_case_ids=("a", "b"),
        )
        result = summary["track_results"]["abstract_only"]
        self.assertEqual(result["case_count"], 2)
        self.assertEqual(result["errors"], 2)

    def test_jsonl_repair_adds_missing_trailing_newline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "raw.jsonl"
            path.write_text('{"case_id":"a"}', encoding="utf-8")
            self.assertEqual(_repair_and_load_jsonl(path), [{"case_id": "a"}])
            self.assertTrue(path.read_bytes().endswith(b"\n"))


class LeakageAuditTests(unittest.TestCase):
    def test_title_similarity_is_normalized(self) -> None:
        self.assertGreaterEqual(
            normalized_title_similarity(
                "Graph-based Learning: A Study", "Graph Based Learning -- A Study"
            ),
            0.95,
        )

    def test_audit_detects_doi_title_and_journal(self) -> None:
        evidence = [
            {
                "title": "Graph-based Learning: A Study",
                "url": "https://doi.org/10.1000/test.7",
                "snippet": "Published in Journal of Graph Tests",
                "query": "graph learning journal",
            }
        ]
        audit = audit_search_leakage(
            evidence,
            doi="10.1000/TEST.7",
            gold_journal_name="Journal of Graph Tests",
            paper_title="Graph Based Learning - A Study",
        )
        self.assertTrue(audit["article_leak"])
        self.assertTrue(audit["gold_journal_mentioned"])
        self.assertEqual(audit["reason_counts"], {"doi": 1, "title": 1, "gold_journal": 1})

    def test_topical_evidence_without_identity_is_not_a_leak(self) -> None:
        audit = audit_search_leakage(
            [
                {
                    "title": "Aims and scope for machine learning methods",
                    "url": "https://example.org/scope",
                    "snippet": "Graph learning and optimization",
                }
            ],
            doi="10.1000/test.7",
            gold_journal_name="Journal of Graph Tests",
            paper_title="Graph Based Learning - A Study",
        )
        self.assertFalse(audit["any_leak"])


if __name__ == "__main__":
    unittest.main()
