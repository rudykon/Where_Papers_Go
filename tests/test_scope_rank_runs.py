from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from research.data import (
    ResearchDataError,
    build_run_binding,
    load_jsonl_corpus,
    load_recent_journal_dataset,
    sha256_file,
    write_run,
)
from research.scope_rank_runs import (
    PairwiseLinearRanker,
    _calibration_partition,
    _variant_specs,
    build_query_representation,
    build_scope_rank_suite,
)
from research.types import ScoredDocument


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


class ScopeRankFormalUnitTests(unittest.TestCase):
    def test_query_representation_ignores_all_gold_fields(self) -> None:
        from research.types import Query

        query = Query(
            "q",
            "graph learning for clinical imaging",
            "2026-01-01",
            title="graph learning",
            abstract="clinical imaging",
        )
        first = build_query_representation(
            query,
            {
                "article_type": "journal-article",
                "language": "en",
                "gold_journal_id": "v1",
                "gold_journal_name": "First",
                "gold_jcr_quartile": "Q1",
                "broad_field": "Medicine",
            },
        )
        second = build_query_representation(
            query,
            {
                "article_type": "journal-article",
                "language": "en",
                "gold_journal_id": "v9",
                "gold_journal_name": "Poison",
                "gold_jcr_quartile": "Q4",
                "broad_field": "Economics",
            },
        )
        self.assertEqual(first, second)
        self.assertNotIn(
            "gold_jcr_quartile", " ".join(first.representation_source_fields)
        )

    def test_pairwise_ranker_is_deterministic_and_orders_positive(self) -> None:
        pairs = [
            ({"dense": 1.0, "graph": 0.8}, {"dense": 0.1, "graph": 0.2}),
            ({"dense": 0.9, "graph": 1.0}, {"dense": 0.2, "graph": 0.1}),
        ]
        kwargs = {
            "training_query_count": 2,
            "skipped_query_count": 0,
            "epochs": 40,
            "learning_rate": 0.2,
            "l2": 0.001,
        }
        first = PairwiseLinearRanker(("dense", "graph")).fit(pairs, **kwargs)
        second = PairwiseLinearRanker(("dense", "graph")).fit(pairs, **kwargs)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertGreater(
            first.predict({"dense": 0.9, "graph": 0.9}),
            first.predict({"dense": 0.1, "graph": 0.1}),
        )

    def test_train_calibration_partition_and_ablation_names_are_frozen(self) -> None:
        query_ids = tuple(f"q{index:03d}" for index in range(50))
        fit, calibration = _calibration_partition(
            query_ids, salt="test", denominator=5
        )
        self.assertFalse(set(fit) & set(calibration))
        self.assertEqual(set((*fit, *calibration)), set(query_ids))
        names = [spec.name for spec in _variant_specs()]
        self.assertEqual(len(names), 12)
        self.assertEqual(len(set(names)), 12)
        self.assertIn("scope_rank_full", names)
        self.assertIn("scope_rank_replace_rrf", names)
        self.assertIn("scope_rank_replace_linear", names)


class ScopeRankFormalIntegrationTests(unittest.TestCase):
    def test_suite_builds_full_method_all_ablations_and_audits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset_path = root / "dataset.jsonl"
            profiles_path = root / "profiles.jsonl"
            config_path = root / "scope.json"
            output_dir = root / "scope-suite"

            rows: list[dict[str, object]] = []
            start = date(2026, 1, 1)
            for index in range(12):
                if index < 8:
                    published = start + timedelta(days=index)
                elif index < 10:
                    published = date(2026, 4, 1 + index - 8)
                else:
                    published = date(2026, 6, 1 + index - 10)
                gold = "v1" if index % 2 == 0 else "v2"
                rows.append(
                    {
                        "paper_id": f"q{index:02d}",
                        "title": (
                            f"graph learning methods study {index}"
                            if gold == "v1"
                            else f"clinical imaging methods study {index}"
                        ),
                        "abstract": f"independent methods and evidence case {index}",
                        "publication_date": published.isoformat(),
                        "article_type": "journal-article",
                        "language": "en",
                        "gold_journal_id": gold,
                        "gold_journal_name": "poisoned label",
                        "gold_jcr_quartile": "Q4",
                    }
                )
            _write_jsonl(dataset_path, rows)
            _write_jsonl(
                profiles_path,
                [
                    {
                        "venue_id": venue_id,
                        "name": name,
                        "snapshot_date": "2026-03-31",
                        "profile_text": text,
                        "prototypes": [
                            {
                                "prototype_id": f"{venue_id}:p",
                                "text": text,
                                "source_ids": [f"paper:{venue_id}"],
                                "source_max_date": "2026-03-01",
                                "temporal_eligible": True,
                            }
                        ],
                        "metadata": {
                            "subject": text,
                            "broad_field": text,
                            "jcr_quartile": "Q1",
                            "level": "Q1",
                            "profile_level": "A",
                            "evidence_grade": "A",
                            "history_paper_count": 10,
                            "temporal_prototype_count": 1,
                            "temporal_official_scope_count": 0,
                        },
                    }
                    for venue_id, name, text in (
                        ("v1", "Graph Venue", "computer science graph learning"),
                        ("v2", "Clinical Venue", "medicine clinical imaging"),
                        ("v3", "Other Venue", "economics finance"),
                    )
                ],
            )
            bundle = load_recent_journal_dataset(dataset_path)
            corpus = load_jsonl_corpus(
                profiles_path, text_fields=("name",), snapshot_field="snapshot_date"
            )
            query_ids = tuple(query.query_id for query in bundle.queries)
            candidate_ids = tuple(document.doc_id for document in corpus)
            source_dir = root / "sources"
            source_dir.mkdir()
            channels = []
            for source_index, source in enumerate(
                (
                    "bm25",
                    "bge_m3",
                    "specter2",
                    "scincl",
                    "property_graph",
                    "lightrag",
                    "cross_encoder",
                )
            ):
                source_binding = build_run_binding(
                    dataset_path=dataset_path,
                    profiles_path=profiles_path,
                    query_ids=query_ids,
                    candidate_ids=candidate_ids,
                    configuration={"source": source},
                )
                run = {}
                for query_id in query_ids:
                    gold = next(iter(bundle.qrels[query_id]))
                    others = [value for value in candidate_ids if value != gold]
                    ranking_ids = [gold, *others]
                    if source_index % 2:
                        ranking_ids = [others[0], gold, *others[1:]]
                    run[query_id] = [
                        ScoredDocument(candidate_id, 3.0 - rank)
                        for rank, candidate_id in enumerate(ranking_ids)
                    ]
                run_path = source_dir / f"{source}.jsonl"
                identity = f"toy-{source}-v1"
                manifest = write_run(
                    run_path,
                    run,
                    binding=source_binding,
                    query_ids=query_ids,
                    candidate_ids=candidate_ids,
                    top_k=3,
                    method={
                        "name": source,
                        "kind": "toy",
                        "implementation": "tests.test_scope_rank_runs",
                        "implementation_revision": identity,
                        "configuration_sha256": source_binding["configuration"][
                            "canonical_sha256"
                        ],
                    },
                    command=("python", "-m", "unittest"),
                    working_directory=root,
                )
                manifest_path = run_path.with_suffix(".jsonl.manifest.json")
                channels.append(
                    {
                        "name": source,
                        "path": str(run_path),
                        "run_sha256": sha256_file(run_path),
                        "manifest_path": str(manifest_path),
                        "manifest_sha256": sha256_file(manifest_path),
                        "generation_config_sha256": manifest["method"][
                            "configuration_sha256"
                        ],
                        "implementation_revision": identity,
                    }
                )
            reference_path = root / "reference.json"
            reference_path.write_text(
                json.dumps({"binding": source_binding}) + "\n", encoding="utf-8"
            )
            config = {
                "schema_version": 1,
                "offline_only": True,
                "fail_on_critical_leakage": True,
                "evaluation_status": "exposed_development_not_sealed",
                "output_dir": str(output_dir),
                "reference_manifest": str(reference_path),
                "reference_manifest_sha256": sha256_file(reference_path),
                "dataset": {
                    "path": str(dataset_path),
                    "query_fields": ["title", "abstract"],
                },
                "corpus": {
                    "path": str(profiles_path),
                    "id_field": "venue_id",
                    "text_fields": ["name"],
                    "snapshot_field": "snapshot_date",
                },
                "temporal_split": {
                    "start": "2026-01-01",
                    "train_end": "2026-03-31",
                    "validation_end": "2026-05-31",
                    "test_end": "2026-06-30",
                },
                "channels": channels,
                "method": {
                    "source_depth": 3,
                    "top_k": 3,
                    "profile_cutoff": "2026-03-31",
                    "total_recall_budget": 7,
                    "fixed_quotas": {
                        "bm25": 1,
                        "bge_m3": 1,
                        "specter2": 1,
                        "scincl": 1,
                        "property_graph": 1,
                        "lightrag": 1,
                        "subject_route": 1,
                    },
                    "calibration_salt": "toy-scope-rank",
                    "calibration_denominator": 2,
                    "hard_negatives": 1,
                    "epochs": 5,
                    "learning_rate": 0.1,
                    "l2": 0.001,
                    "calibrator": {
                        "target_precision": 0.0,
                        "min_confidence": 0.0,
                        "min_evidence_coverage": 0.0,
                        "min_channel_agreement": 0.0,
                    },
                },
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")

            manifest = build_scope_rank_suite(
                config_path,
                generation_command=("python", "-m", "research", "build-scope-rank-suite"),
            )

            self.assertEqual(manifest["coverage"]["variant_count"], 12)
            self.assertEqual(manifest["coverage"]["ablation_count"], 11)
            self.assertEqual(manifest["coverage"]["decision_count"], 12 * 12)
            self.assertEqual(manifest["coverage"]["explanation_count"], 12 * 2)
            self.assertTrue(manifest["coverage"]["all_variants_complete"])
            self.assertEqual(manifest["leakage_audit"]["sha256"], sha256_file(
                output_dir / "leakage_audit.json"
            ))
            self.assertTrue(manifest["label_boundary"]["partitions_disjoint"])
            self.assertTrue(manifest["label_boundary"]["union_equals_temporal_train"])
            self.assertFalse(manifest["label_boundary"]["validation_labels_accessed"])
            self.assertFalse(manifest["label_boundary"]["test_labels_accessed"])
            self.assertEqual(len(manifest["variants"]), 12)
            for variant in manifest["variants"].values():
                self.assertEqual(variant["constraints"]["output_violation_count"], 0)
                self.assertFalse(variant["training"]["validation_labels_accessed"])
                self.assertFalse(variant["training"]["test_labels_accessed"])
            self.assertTrue((output_dir / "manifest.json").is_file())
            self.assertFalse(list(root.glob("scope-suite.failed-*")))

            manifest_sha256 = sha256_file(output_dir / "manifest.json")
            with self.assertRaisesRegex(ResearchDataError, "will not be overwritten"):
                build_scope_rank_suite(
                    config_path,
                    generation_command=(
                        "python",
                        "-m",
                        "research",
                        "build-scope-rank-suite",
                    ),
                )
            self.assertEqual(
                sha256_file(output_dir / "manifest.json"), manifest_sha256
            )

            failed_output = root / "scope-suite-tampered"
            config["output_dir"] = str(failed_output)
            config["channels"][0]["run_sha256"] = "0" * 64
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ResearchDataError, "source run SHA-256"):
                build_scope_rank_suite(
                    config_path,
                    generation_command=(
                        "python",
                        "-m",
                        "research",
                        "build-scope-rank-suite",
                    ),
                )
            self.assertFalse(failed_output.exists())
            self.assertEqual(len(list(root.glob("scope-suite-tampered.failed-*"))), 1)


if __name__ == "__main__":
    unittest.main()
