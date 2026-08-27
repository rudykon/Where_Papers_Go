from __future__ import annotations

import csv
import json
import math
from pathlib import Path
import tempfile
import unittest

from research.baselines import BM25Baseline, TfidfBaseline, tokenize
from research.cache_builder import build_cached_corpus, jats_to_text
from research.cli import evaluate_config
from research.data import (
    ResearchDataError,
    build_run_binding,
    canonical_json_sha256,
    exclude_query_identities_from_prototypes,
    load_evidence_concat_corpus,
    load_recent_journal_dataset,
    load_score_run,
    sha256_file,
    temporal_split,
    write_run,
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

    def test_prototype_tfidf_idf_uses_expanded_unit_population(self) -> None:
        corpus = [
            VenueDocument(
                "v1",
                "fallback",
                metadata={
                    "prototypes": [
                        {"text": "common alpha"},
                        {"text": "common beta"},
                    ]
                },
            ),
            VenueDocument(
                "v2",
                "fallback",
                metadata={"prototypes": [{"text": "gamma"}]},
            ),
        ]
        baseline = TfidfBaseline(use_prototypes=True).fit(corpus)
        self.assertAlmostEqual(baseline._idf["common"], math.log(4 / 3) + 1)

    def test_evidence_concat_excludes_validation_identity_without_rewriting_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profiles = root / "profiles.jsonl"
            evidence = root / "evidence.jsonl"
            _write_jsonl(
                profiles,
                [
                    {
                        "venue_id": "v1",
                        "name": "Venue One",
                        "snapshot_date": "2026-01-31",
                        "metadata": {"subject": "computer science"},
                    },
                    {
                        "venue_id": "v2",
                        "name": "Venue Two",
                        "snapshot_date": "2026-01-31",
                        "metadata": {},
                    },
                ],
            )
            _write_jsonl(
                evidence,
                [
                    {
                        "kind": "paper",
                        "venue_id": "v1",
                        "doi": "10.1/safe",
                        "title": "Safe historical paper title",
                        "abstract": "safe evidence",
                    },
                    {
                        "kind": "paper",
                        "venue_id": "v1",
                        "doi": "10.1/leaked",
                        "title": "Distinctive validation paper identity title",
                        "abstract": "must not enter the active view",
                    },
                ],
            )
            corpus, report = load_evidence_concat_corpus(
                profiles,
                evidence,
                excluded_queries=(
                    Query(
                        "doi:10.1/leaked",
                        "query",
                        "2026-02-01",
                        title="Distinctive validation paper identity title",
                        doi="10.1/leaked",
                    ),
                ),
            )
        self.assertEqual(len(corpus), 2)
        self.assertIn("Safe historical paper title", corpus[0].text)
        self.assertNotIn("Distinctive validation paper identity title", corpus[0].text)
        self.assertEqual(report["excluded_count"], 1)
        self.assertEqual(report["matched_query_ids"], ["doi:10.1/leaked"])

    def test_prototype_identity_exclusion_drops_the_whole_citing_unit(self) -> None:
        corpus, report = exclude_query_identities_from_prototypes(
            [
                VenueDocument(
                    "v1",
                    "fallback",
                    metadata={
                        "prototypes": [
                            {
                                "text": "safe prototype",
                                "source_ids": ["paper:v1:doi:10.1/safe"],
                            },
                            {
                                "text": "contaminated prototype",
                                "source_ids": ["paper:v1:doi:10.1/leaked"],
                            },
                        ]
                    },
                )
            ],
            excluded_queries=(
                Query(
                    "doi:10.1/leaked",
                    "query",
                    "2026-02-01",
                    title="Distinctive validation paper identity title",
                    doi="10.1/leaked",
                ),
            ),
        )
        prototypes = corpus[0].metadata["prototypes"]
        self.assertEqual([item["text"] for item in prototypes], ["safe prototype"])
        self.assertEqual(report["excluded_count"], 1)
        self.assertEqual(report["affected_venue_count"], 1)

    def test_imported_score_formats(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "scores.jsonl"
            dataset = root / "dataset.jsonl"
            profiles = root / "profiles.jsonl"
            dataset.write_text("dataset\n", encoding="utf-8")
            profiles.write_text("profiles\n", encoding="utf-8")
            configuration = {"builder": "unit-test", "top_k": 2}
            binding = build_run_binding(
                dataset_path=dataset,
                profiles_path=profiles,
                query_ids=("q1", "q2"),
                candidate_ids=("v1", "v2"),
                configuration=configuration,
            )
            method = {
                "name": "test",
                "kind": "vector",
                "provider_fingerprint": "provider-v1",
            }
            write_run(
                path,
                {
                    "q1": [
                        ScoredDocument("v2", 0.9),
                        ScoredDocument("v1", 0.4),
                    ],
                    "q2": [ScoredDocument("v1", 0.8)],
                },
                binding=binding,
                query_ids=("q1", "q2"),
                candidate_ids=("v1", "v2"),
                top_k=2,
                method=method,
                command=("python", "-m", "research", "test"),
                working_directory=root,
            )
            # Exercise both supported on-disk row forms under one valid,
            # content-addressed sidecar.
            _write_jsonl(
                path,
                [
                    {"query_id": "q1", "venue_id": "v2", "rank": 1, "score": 0.9},
                    {"query_id": "q1", "venue_id": "v1", "rank": 2, "score": 0.4},
                    {"query_id": "q2", "scores": {"v1": 0.8}},
                ],
            )
            manifest_path = path.with_suffix(path.suffix + ".manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["output"]["sha256"] = sha256_file(path)
            manifest["output"]["bytes"] = path.stat().st_size
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            run = load_score_run(
                path,
                expected_query_ids=("q1", "q2"),
                candidate_ids=("v1", "v2"),
                expected_binding=binding,
                expected_manifest_sha256=sha256_file(manifest_path),
                expected_configuration_sha256=canonical_json_sha256(configuration),
                expected_method_identity={"provider_fingerprint": "provider-v1"},
            )
        self.assertEqual([item.doc_id for item in run["q1"]], ["v2", "v1"])
        self.assertEqual(run["q2"][0].score, 0.8)


class FrozenRunContractTests(unittest.TestCase):
    def test_frozen_run_binding_includes_every_active_corpus_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset.jsonl"
            profiles = root / "profiles.jsonl"
            evidence = root / "evidence.jsonl"
            other_evidence = root / "other-evidence.jsonl"
            path = root / "run.jsonl"
            dataset.write_text("dataset\n", encoding="utf-8")
            profiles.write_text("profiles\n", encoding="utf-8")
            evidence.write_text("evidence\n", encoding="utf-8")
            other_evidence.write_text("changed evidence\n", encoding="utf-8")
            configuration = {"builder": "multi-input-test"}
            binding = build_run_binding(
                dataset_path=dataset,
                profiles_path=profiles,
                query_ids=("q1",),
                candidate_ids=("v1",),
                configuration=configuration,
                additional_input_paths=(evidence,),
            )
            write_run(
                path,
                {"q1": [ScoredDocument("v1", 1.0)]},
                binding=binding,
                query_ids=("q1",),
                candidate_ids=("v1",),
                top_k=1,
                method={
                    "name": "bound",
                    "kind": "test",
                    "implementation_revision": "test@1",
                },
                command=("python", "-m", "research", "test"),
                working_directory=root,
            )
            manifest_path = path.with_suffix(path.suffix + ".manifest.json")
            mismatched_binding = build_run_binding(
                dataset_path=dataset,
                profiles_path=profiles,
                query_ids=("q1",),
                candidate_ids=("v1",),
                configuration=configuration,
                additional_input_paths=(other_evidence,),
            )
            with self.assertRaisesRegex(ResearchDataError, "additional_inputs"):
                load_score_run(
                    path,
                    expected_query_ids=("q1",),
                    candidate_ids=("v1",),
                    expected_binding=mismatched_binding,
                    expected_manifest_sha256=sha256_file(manifest_path),
                    expected_configuration_sha256=canonical_json_sha256(
                        configuration
                    ),
                    expected_method_identity={"implementation_revision": "test@1"},
                )

    def test_frozen_run_contract_rejects_incomplete_or_invalid_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset.jsonl"
            profiles = root / "profiles.jsonl"
            path = root / "run.jsonl"
            dataset.write_text("dataset\n", encoding="utf-8")
            profiles.write_text("profiles\n", encoding="utf-8")
            config = {"builder": "contract-test", "top_k": 2}
            binding = build_run_binding(
                dataset_path=dataset,
                profiles_path=profiles,
                query_ids=("q1", "q2"),
                candidate_ids=("v1", "v2"),
                configuration=config,
            )
            method = {
                "name": "strict",
                "kind": "model",
                "model_revision": "model@0123456789abcdef",
            }
            valid = {
                "q1": [ScoredDocument("v1", 1.0)],
                "q2": [],
            }

            def freeze() -> Path:
                write_run(
                    path,
                    valid,
                    binding=binding,
                    query_ids=("q1", "q2"),
                    candidate_ids=("v1", "v2"),
                    top_k=2,
                    method=method,
                    command=("python", "-m", "research", "contract-test"),
                    working_directory=root,
                )
                return path.with_suffix(path.suffix + ".manifest.json")

            for bad_run in (
                {"q1": valid["q1"]},
                {**valid, "q3": []},
                {"q1": [ScoredDocument("unknown", 1.0)], "q2": []},
                {"q1": [ScoredDocument("v1", float("inf"))], "q2": []},
                {
                    "q1": [
                        ScoredDocument("v1", 1.0),
                        ScoredDocument("v1", 0.5),
                    ],
                    "q2": [],
                },
            ):
                with self.subTest(bad_run=bad_run), self.assertRaises(ResearchDataError):
                    write_run(
                        path,
                        bad_run,
                        binding=binding,
                        query_ids=("q1", "q2"),
                        candidate_ids=("v1", "v2"),
                        top_k=2,
                        method=method,
                        command=("python", "-m", "research", "contract-test"),
                        working_directory=root,
                    )

            manifest_path = freeze()
            with self.assertRaises(ResearchDataError):
                load_score_run(
                    path,
                    expected_query_ids=("q1", "q2"),
                    candidate_ids=("v1", "v2"),
                    expected_binding=binding,
                    expected_manifest_sha256="0" * 64,
                    expected_configuration_sha256=canonical_json_sha256(config),
                    expected_method_identity={
                        "model_revision": "model@0123456789abcdef"
                    },
                )

            manifest_sha = sha256_file(manifest_path)
            for wrong_config, wrong_identity in (
                ("f" * 64, {"model_revision": "model@0123456789abcdef"}),
                (
                    canonical_json_sha256(config),
                    {"model_revision": "model@wrong"},
                ),
            ):
                with self.subTest(
                    wrong_config=wrong_config, wrong_identity=wrong_identity
                ), self.assertRaises(ResearchDataError):
                    load_score_run(
                        path,
                        expected_query_ids=("q1", "q2"),
                        candidate_ids=("v1", "v2"),
                        expected_binding=binding,
                        expected_manifest_sha256=manifest_sha,
                        expected_configuration_sha256=wrong_config,
                        expected_method_identity=wrong_identity,
                    )

            for malformed_rows in (
                [
                    {"query_id": "q1", "venue_id": "v1", "rank": 1, "score": "inf"},
                    {"query_id": "q2", "scores": {}},
                ],
                [{"query_id": "q1", "venue_id": "v1", "rank": 1, "score": 1.0}],
                [
                    {"query_id": "q1", "venue_id": "v1", "rank": 1, "score": 1.0},
                    {"query_id": "q1", "venue_id": "v1", "rank": 2, "score": 0.5},
                    {"query_id": "q2", "scores": {}},
                ],
            ):
                manifest_path = freeze()
                _write_jsonl(path, malformed_rows)
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["output"]["sha256"] = sha256_file(path)
                manifest["output"]["bytes"] = path.stat().st_size
                manifest_path.write_text(
                    json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                with self.subTest(rows=malformed_rows), self.assertRaises(
                    ResearchDataError
                ):
                    load_score_run(
                        path,
                        expected_query_ids=("q1", "q2"),
                        candidate_ids=("v1", "v2"),
                        expected_binding=binding,
                        expected_manifest_sha256=sha256_file(manifest_path),
                        expected_configuration_sha256=canonical_json_sha256(config),
                        expected_method_identity={
                            "model_revision": "model@0123456789abcdef"
                        },
                    )


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
            prototype_scoped = [
                VenueDocument(
                    "v1",
                    "catalog name only",
                    snapshot_date="2025-12-31",
                    metadata={
                        "source_dois": ["10.1/test"],
                        "source_titles": ["New cancer imaging paper"],
                        "prototypes": [
                            {
                                "label": "Historical graph methods",
                                "text": "historical graph retrieval methods",
                                "source_ids": [
                                    "paper:v1:doi:10.1/historical"
                                ],
                                "source_max_date": "2025-12-31",
                                "temporal_eligible": True,
                            }
                        ],
                    },
                )
            ]
            scoped_audit = audit_leakage(
                bundle,
                prototype_scoped,
                split,
                corpus_views=("prototypes",),
            )
            self.assertTrue(scoped_audit["passed"])
            self.assertEqual(scoped_audit["schema_version"], 3)
            self.assertEqual(scoped_audit["audited_corpus_views"], ["prototypes"])
            self.assertTrue(
                any(
                    item["kind"] == "evaluation_identity_in_unindexed_metadata_doi"
                    for item in scoped_audit["findings"]
                )
            )
            active_source = [
                VenueDocument(
                    "v1",
                    "catalog name only",
                    snapshot_date="2025-12-31",
                    metadata={
                        "prototypes": [
                            {
                                "label": "Leaked test-derived prototype",
                                "text": "derived clinical retrieval topic",
                                "source_ids": ["paper:v1:doi:10.1/test"],
                                "source_max_date": "2025-12-31",
                                "temporal_eligible": True,
                            }
                        ]
                    },
                )
            ]
            active_audit = audit_leakage(
                bundle,
                active_source,
                split,
                corpus_views=("prototypes",),
            )
            self.assertFalse(active_audit["passed"])
            self.assertTrue(
                any(
                    item["kind"] == "evaluation_identity_in_corpus_doi"
                    for item in active_audit["findings"]
                )
            )
            synthetic_audit = {
                "findings": [
                    {"kind": "gold_venue_mentioned_in_query", "query_id": "test"},
                    {"kind": "cross_split_duplicate_title", "query_id": "dev"},
                ]
            }
            self.assertEqual(identity_unsafe_query_ids(synthetic_audit), ("test",))

    def test_abstract_near_duplicate_and_publication_version_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dataset = Path(temporary) / "dataset.jsonl"
            shared = [f"token{index}" for index in range(50)]
            near_train = " ".join(shared)
            near_test = " ".join([*shared[:-1], "changed-final-token"])
            _write_jsonl(
                dataset,
                [
                    {
                        "paper_id": "near-train",
                        "doi": "10.1000/near-train",
                        "title": "Training title for abstract comparison",
                        "abstract": near_train,
                        "publication_date": "2026-01-10",
                        "gold_journal_id": "v1",
                    },
                    {
                        "paper_id": "version-train",
                        "doi": "10.1000/versioned-work.v1",
                        "title": "Early preprint title",
                        "abstract": "short early preprint summary",
                        "publication_date": "2026-01-11",
                        "gold_journal_id": "v1",
                    },
                    {
                        "paper_id": "near-test",
                        "doi": "10.1000/near-test",
                        "title": "Different evaluation title",
                        "abstract": near_test,
                        "publication_date": "2026-03-10",
                        "gold_journal_id": "v1",
                    },
                    {
                        "paper_id": "version-test",
                        "doi": "10.1000/versioned-work.v2",
                        "title": "Substantially revised journal title",
                        "abstract": "different final publication summary",
                        "publication_date": "2026-03-11",
                        "gold_journal_id": "v1",
                    },
                ],
            )
            bundle = load_recent_journal_dataset(dataset)
            split = temporal_split(
                bundle.queries,
                train_end="2026-01-31",
                validation_end="2026-02-28",
                test_end="2026-03-31",
            )
            audit = audit_leakage(
                bundle,
                [VenueDocument("v1", "clean", snapshot_date="2025-12-31")],
                split,
            )
            kinds = {finding["kind"] for finding in audit["findings"]}
            self.assertIn("cross_split_near_duplicate_abstract", kinds)
            self.assertIn("cross_split_publication_version", kinds)
            self.assertFalse(audit["passed"])

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
            self.assertEqual(report["schema_version"], 2)
            self.assertEqual(report["frozen_run_contract"]["query_count"], 3)
            self.assertEqual(
                report["frozen_run_contract"]["candidate_universe_count"], 2
            )
            self.assertIn("shell_command", report["reproduction"])
            for relative in (
                "manifest.json",
                "leakage_audit.json",
                "metrics.json",
                "runs/bm25.jsonl",
                "runs/bm25.jsonl.manifest.json",
                "runs/learned.jsonl",
                "runs/learned.jsonl.manifest.json",
            ):
                self.assertTrue((output / relative).is_file(), relative)
            manifest = json.loads(
                (output / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(
                manifest["frozen_runs"]["bm25"]["coverage"]["query_count"], 3
            )
            self.assertTrue(
                manifest["frozen_runs"]["bm25"]["coverage"]
                ["complete_query_coverage"]
            )
            self.assertIn("dependencies", manifest["runtime"])
            self.assertIn("hardware", manifest["runtime"])


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
