from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile
import unittest

from research.baselines import BM25Baseline, TfidfBaseline, tokenize
from research.cache_builder import build_cached_corpus, jats_to_text
from research.cli import evaluate_config
from research.data import (
    load_recent_journal_dataset,
    load_score_run,
    temporal_split,
)
from research.fusion import LearnedLinearFusion, rrf_fuse
from research.leakage import audit_leakage, identity_unsafe_query_ids
from research.metrics import evaluate_run
from research.reporting import build_query_strata, summarize_strata
from research.statistics import paired_bootstrap_ci, paired_permutation_test
from research.types import Query, ScoredDocument, VenueDocument


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


class LexicalBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.corpus = [
            VenueDocument("v-graph", "graph neural networks and information retrieval"),
            VenueDocument("v-medical", "clinical medicine cancer imaging diagnosis"),
            VenueDocument("v-chinese", "医学影像跨模态诊断"),
        ]
        self.queries = [
            Query("q1", "neural graph retrieval", "2026-06-01"),
            Query("q2", "跨模态医学影像", "2026-06-02"),
        ]

    def test_tokenizer_supports_cjk_bigrams(self) -> None:
        tokens = tokenize("医学影像 Graph-based C++")
        self.assertIn("医学", tokens)
        self.assertIn("graph-based", tokens)
        self.assertIn("c++", tokens)

    def test_bm25_and_tfidf_share_run_interface(self) -> None:
        for baseline in (BM25Baseline(), TfidfBaseline()):
            run = baseline.fit(self.corpus).run(self.queries, top_k=3)
            self.assertEqual(run["q1"][0].doc_id, "v-graph")
            self.assertEqual(run["q2"][0].doc_id, "v-chinese")

    def test_imported_score_formats(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "scores.jsonl"
            _write_jsonl(
                path,
                [
                    {"query_id": "q1", "venue_id": "v1", "score": 0.4},
                    {"query_id": "q1", "venue_id": "v2", "score": 0.9},
                    {"query_id": "q2", "scores": {"v1": 0.8}},
                ],
            )
            run = load_score_run(path)
        self.assertEqual([item.doc_id for item in run["q1"]], ["v2", "v1"])
        self.assertEqual(run["q2"][0].score, 0.8)


class FusionMetricAndStatisticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run_a = {
            "q1": [ScoredDocument("gold1", 2.0), ScoredDocument("other", 1.0)],
            "q2": [ScoredDocument("other", 2.0), ScoredDocument("gold2", 1.0)],
            "q3": [ScoredDocument("gold3", 2.0)],
        }
        self.run_b = {
            "q1": [ScoredDocument("other", 2.0), ScoredDocument("gold1", 1.0)],
            "q2": [ScoredDocument("gold2", 2.0), ScoredDocument("other", 1.0)],
            "q3": [ScoredDocument("other", 2.0), ScoredDocument("gold3", 1.0)],
        }
        self.qrels = {
            "q1": {"gold1": 1.0},
            "q2": {"gold2": 1.0},
            "q3": {"gold3": 1.0},
        }

    def test_rrf_and_learned_fusion(self) -> None:
        fused = rrf_fuse({"a": self.run_a, "b": self.run_b}, top_k=2)
        self.assertEqual(set(fused), set(self.qrels))
        learner = LearnedLinearFusion(epochs=20, hard_negatives=1).fit(
            {"a": self.run_a, "b": self.run_b}, self.qrels, ["q1", "q2", "q3"]
        )
        learned = learner.run(
            {"a": self.run_a, "b": self.run_b}, query_ids=["q1", "q2", "q3"], top_k=2
        )
        self.assertEqual(len(learned["q1"]), 2)
        self.assertIsNotNone(learner.report)
        self.assertGreater(learner.report.training_pairs, 0)  # type: ignore[union-attr]

    def test_all_requested_metrics_keep_misses_in_denominator(self) -> None:
        result = evaluate_run(
            self.run_a,
            self.qrels,
            query_ids=["q1", "q2", "q3", "missing"],
            ks=(1, 2),
        )
        aggregate = result["aggregate"]
        self.assertEqual(result["query_count"], 4)
        self.assertAlmostEqual(aggregate["hit@1"], 0.5)
        for key in ("recall@2", "hit@2", "mrr@2", "ndcg@2", "map@2"):
            self.assertIn(key, aggregate)

    def test_paired_bootstrap_and_permutation_are_deterministic(self) -> None:
        left = evaluate_run(self.run_a, self.qrels, query_ids=list(self.qrels), ks=(1,))["per_query"]
        right = evaluate_run(self.run_b, self.qrels, query_ids=list(self.qrels), ks=(1,))["per_query"]
        first = paired_bootstrap_ci(left, right, metric="hit@1", iterations=200, seed=7)
        second = paired_bootstrap_ci(left, right, metric="hit@1", iterations=200, seed=7)
        self.assertEqual(first, second)
        permutation = paired_permutation_test(
            left, right, metric="hit@1", iterations=200, seed=7
        )
        self.assertGreaterEqual(permutation["two_sided_p_value"], 0.0)
        self.assertLessEqual(permutation["two_sided_p_value"], 1.0)


class StratifiedReportingTests(unittest.TestCase):
    def test_gold_profile_strata_keep_every_query_in_the_denominator(self) -> None:
        queries = {
            "q-warm": Query(
                "q-warm",
                "graph retrieval",
                "2026-06-01",
                metadata={"field": "Computer Science", "quartile": "Q1"},
            ),
            "q-few": Query(
                "q-few", "clinical imaging", "2026-06-02", metadata={}
            ),
            "q-cold": Query(
                "q-cold", "algebra", "2026-06-03", metadata={}
            ),
            "q-missing": Query(
                "q-missing", "unknown target", "2026-06-04", metadata={}
            ),
        }
        qrels = {
            "q-warm": {"v-warm": 1.0},
            "q-few": {"v-few": 1.0},
            "q-cold": {"v-cold": 1.0},
            "q-missing": {"v-outside": 1.0},
        }
        corpus = [
            VenueDocument(
                "v-warm",
                "graph",
                metadata={
                    "history_paper_count": 5,
                    "evidence_grade": "a",
                    "broad_field": "Engineering",
                    "jcr_quartile": "Q2",
                },
            ),
            VenueDocument(
                "v-few",
                "medicine",
                metadata={
                    "paper_count": "4",
                    "profile_level": "B",
                    "field": "Medicine",
                    "level": "Q2",
                },
            ),
            VenueDocument(
                "v-cold",
                "mathematics",
                metadata={
                    "source_dois": [],
                    "profile_grade": "D",
                    "area_en": "Mathematics",
                    "quartile": "Q4",
                },
            ),
        ]
        query_ids = tuple(queries)
        strata = build_query_strata(
            query_ids=query_ids,
            qrels=qrels,
            queries=queries,
            corpus=corpus,
        )

        self.assertEqual(strata["history_status"]["q-warm"], "warm")
        self.assertEqual(strata["history_status"]["q-few"], "few-shot")
        self.assertEqual(strata["history_status"]["q-cold"], "cold")
        self.assertEqual(
            strata["history_status"]["q-missing"], "out-of-catalog"
        )
        self.assertEqual(strata["profile_level"]["q-warm"], "A")
        self.assertEqual(strata["subject"]["q-warm"], "Computer Science")
        self.assertEqual(strata["subject"]["q-few"], "Medicine")
        self.assertEqual(strata["jcr_quartile"]["q-warm"], "Q1")
        self.assertEqual(strata["jcr_quartile"]["q-cold"], "Q4")
        self.assertEqual(
            strata["jcr_quartile"]["q-missing"], "out-of-catalog"
        )

        summary = summarize_strata(strata, query_count=4)
        for dimension in summary.values():
            self.assertEqual(dimension["query_count"], 4)
            self.assertEqual(sum(dimension["groups"].values()), 4)


class TemporalDataAndLeakageTests(unittest.TestCase):
    def _dataset(self, path: Path) -> None:
        _write_jsonl(
            path,
            [
                {
                    "paper_id": "train",
                    "doi": "10.1/train",
                    "title": "Old graph paper",
                    "abstract": "graph methods",
                    "publication_date": "2026-01-10",
                    "gold_journal_id": "v1",
                    "gold_journal_name": "Graph Venue",
                },
                {
                    "paper_id": "dev",
                    "doi": "10.1/dev",
                    "title": "Development medical paper",
                    "abstract": "medical imaging",
                    "publication_date": "2026-02-10",
                    "gold_journal_id": "v2",
                    "gold_journal_name": "Medical Venue",
                },
                {
                    "paper_id": "test",
                    "doi": "10.1/test",
                    "title": "New cancer imaging paper",
                    "abstract": "clinical cancer diagnosis",
                    "publication_date": "2026-03-10",
                    "gold_journal_id": "v2",
                    "gold_journal_name": "Medical Venue",
                },
            ],
        )

    def test_temporal_split_and_direct_leak_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dataset = Path(temporary) / "dataset.jsonl"
            self._dataset(dataset)
            bundle = load_recent_journal_dataset(dataset)
            split = temporal_split(
                bundle.queries,
                train_end="2026-01-31",
                validation_end="2026-02-28",
                test_end="2026-03-31",
            )
            clean = [VenueDocument("v1", "graph", snapshot_date="2025-12-31")]
            self.assertTrue(audit_leakage(bundle, clean, split)["passed"])
            leaked = [
                VenueDocument(
                    "v1",
                    "graph",
                    snapshot_date="2025-12-31",
                    metadata={"source_dois": ["10.1/test"]},
                )
            ]
            audit = audit_leakage(bundle, leaked, split)
            self.assertFalse(audit["passed"])
            self.assertTrue(
                any(item["kind"].endswith("doi") for item in audit["findings"])
            )
            synthetic_audit = {
                "findings": [
                    {"kind": "gold_venue_mentioned_in_query", "query_id": "test"},
                    {"kind": "cross_split_duplicate_title", "query_id": "dev"},
                ]
            }
            self.assertEqual(identity_unsafe_query_ids(synthetic_audit), ("test",))

    def test_end_to_end_config_writes_manifest_audit_runs_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset.jsonl"
            corpus = root / "corpus.jsonl"
            output = root / "output"
            config_path = root / "config.json"
            self._dataset(dataset)
            _write_jsonl(
                corpus,
                [
                    {
                        "venue_id": "v1",
                        "name": "Computing Journal",
                        "scope": "graph methods neural retrieval",
                        "snapshot_date": "2025-12-31",
                        "metadata": {
                            "history_paper_count": 7,
                            "evidence_grade": "A",
                            "broad_field": "Computer Science",
                            "jcr_quartile": "Q1",
                        },
                    },
                    {
                        "venue_id": "v2",
                        "name": "Clinical Journal",
                        "scope": "clinical cancer diagnosis medical imaging",
                        "snapshot_date": "2025-12-31",
                        "metadata": {
                            "history_paper_count": 3,
                            "evidence_grade": "B",
                            "broad_field": "Medicine",
                            "jcr_quartile": "Q2",
                        },
                    },
                ],
            )
            config = {
                "offline_only": True,
                "output_dir": str(output),
                "dataset": {"path": str(dataset), "query_fields": ["title", "abstract"]},
                "corpus": {
                    "type": "jsonl",
                    "path": str(corpus),
                    "text_fields": ["name", "scope"],
                },
                "temporal_split": {
                    "train_end": "2026-01-31",
                    "validation_end": "2026-02-28",
                    "test_end": "2026-03-31",
                },
                "baselines": [
                    {"type": "bm25", "name": "bm25"},
                    {"type": "tfidf", "name": "tfidf"},
                ],
                "fusions": [
                    {"type": "rrf", "name": "rrf", "sources": ["bm25", "tfidf"]},
                    {
                        "type": "learned_linear",
                        "name": "learned",
                        "sources": ["bm25", "tfidf"],
                        "epochs": 10,
                        "hard_negatives": 1,
                    },
                ],
                "evaluation": {"retrieval_depth": 2, "cutoffs": [1, 2]},
                "statistics": {
                    "bootstrap_iterations": 100,
                    "permutation_iterations": 100,
                    "comparisons": [{"left": "rrf", "right": "bm25", "metric": "hit@1"}],
                },
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")
            report = evaluate_config(config_path)
            self.assertEqual(report["methods"]["bm25"]["query_count"], 1)
            self.assertEqual(report["primary_evaluation"]["query_count"], 1)
            self.assertEqual(
                report["methods"]["bm25"]["by_history_status"]["few-shot"][
                    "query_count"
                ],
                1,
            )
            self.assertEqual(
                report["methods"]["bm25"]["by_profile_level"]["B"][
                    "query_count"
                ],
                1,
            )
            self.assertEqual(
                report["methods"]["bm25"]["by_subject"]["Medicine"][
                    "query_count"
                ],
                1,
            )
            self.assertEqual(
                report["methods"]["bm25"]["by_jcr_quartile"]["Q2"][
                    "query_count"
                ],
                1,
            )
            self.assertEqual(
                report["stratification"]["summary"]["history_status"][
                    "query_count"
                ],
                report["primary_evaluation"]["query_count"],
            )
            self.assertEqual(report["identity_safe_test"]["full_query_count"], 1)
            self.assertEqual(report["methods"]["bm25"]["identity_safe"]["query_count"], 1)
            for relative in (
                "manifest.json",
                "leakage_audit.json",
                "metrics.json",
                "runs/bm25.jsonl",
                "runs/learned.jsonl",
            ):
                self.assertTrue((output / relative).is_file(), relative)


class CachedCorpusBuilderTests(unittest.TestCase):
    def test_jats_cleanup(self) -> None:
        self.assertEqual(jats_to_text("<jats:p>Abstract: useful &amp; clean</jats:p>"), "useful & clean")

    def test_builder_keeps_full_candidate_space_and_uses_train_papers_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache_dir = root / "cache"
            output_dir = root / "output"
            cache_dir.mkdir()
            jcr = root / "jcr.csv"
            fields = ["dataset", "version_year", "name", "issn", "eissn", "area", "area_en", "level"]
            with jcr.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(
                    [
                        {
                            "dataset": "jcr",
                            "version_year": "2025",
                            "name": "Cancer Journal",
                            "issn": "0007-9235",
                            "area": "ONCOLOGY",
                            "area_en": "ONCOLOGY",
                            "level": "Q1",
                        },
                        {
                            "dataset": "jcr",
                            "version_year": "2025",
                            "name": "Cell Journal",
                            "issn": "1471-0072",
                            "area": "CELL BIOLOGY",
                            "area_en": "CELL BIOLOGY",
                            "level": "Q1",
                        },
                    ]
                )

            def item(doi: str, title: str, month: int) -> dict[str, object]:
                return {
                    "DOI": doi,
                    "title": [title],
                    "abstract": "<jats:p>Detailed cancer imaging abstract with enough text.</jats:p>",
                    "type": "journal-article",
                    "ISSN": ["0007-9235"],
                    "published-online": {"date-parts": [[2026, month, 15]]},
                }

            payload = {
                "status": "ok",
                "message": {
                    "items": [
                        item("10.1000/train", "Train paper", 1),
                        item("10.1000/dev", "Development paper", 2),
                        item("10.1000/test", "Test paper", 3),
                    ]
                },
            }
            (cache_dir / "one.json").write_text(json.dumps(payload), encoding="utf-8")
            manifest = build_cached_corpus(
                cache_dir=cache_dir,
                jcr_csv=jcr,
                output_dir=output_dir,
                train_end="2026-01-31",
                dev_end="2026-02-28",
                test_end="2026-03-31",
                min_abstract_chars=10,
            )
            profiles = [
                json.loads(line)
                for line in (output_dir / "venue_profiles.train.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(profiles), 2)
            self.assertEqual(manifest["coverage"]["train_profile_venues"], 2)
            self.assertEqual(manifest["coverage"]["venues_with_train_history"], 1)
            with_history = next(row for row in profiles if row["name"] == "Cancer Journal")
            without_history = next(row for row in profiles if row["name"] == "Cell Journal")
            self.assertIn("Train paper", with_history["profile_text"])
            self.assertNotIn("Development paper", with_history["profile_text"])
            self.assertNotIn("Test paper", with_history["profile_text"])
            self.assertEqual(without_history["metadata"]["paper_count"], 0)


if __name__ == "__main__":
    unittest.main()
