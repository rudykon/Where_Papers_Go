from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import unittest

from research.data import (
    ResearchDataError,
    load_recent_journal_dataset,
    ordered_ids_sha256,
    sha256_file,
)
from research.scope_rank_selective import (
    evaluate_scope_rank_selective,
    selective_metrics,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = (
    PROJECT_ROOT
    / "research"
    / "configs"
    / "scope_rank_selective_evaluation_v2.json"
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


class ScopeRankSelectiveUnitTests(unittest.TestCase):
    def test_selective_metrics_handle_zero_and_partial_coverage(self) -> None:
        rows = [
            {
                "abstain": False,
                "correct": True,
                "calibrated_score": 0.8,
                "reason": None,
            },
            {
                "abstain": False,
                "correct": False,
                "calibrated_score": 0.6,
                "reason": None,
            },
            {
                "abstain": True,
                "correct": True,
                "calibrated_score": 0.4,
                "reason": "below_threshold",
            },
            {
                "abstain": True,
                "correct": False,
                "calibrated_score": 0.2,
                "reason": "below_threshold",
            },
        ]
        metrics = selective_metrics(rows, bins=5, confidence=0.95)
        self.assertEqual(metrics["query_count"], 4)
        self.assertEqual(metrics["accepted_count"], 2)
        self.assertEqual(metrics["coverage"], 0.5)
        self.assertEqual(metrics["selective_precision"], 0.5)
        self.assertEqual(metrics["selective_risk"], 0.5)
        self.assertEqual(metrics["correct_acceptance_recall"], 0.5)

        all_abstained = selective_metrics(
            [{**row, "abstain": True, "reason": "closed"} for row in rows],
            bins=5,
            confidence=0.95,
        )
        self.assertEqual(all_abstained["coverage"], 0.0)
        self.assertIsNone(all_abstained["selective_precision"])
        self.assertIsNone(all_abstained["selective_precision_wilson_ci"])

    def test_frozen_selective_config_binds_every_input(self) -> None:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertTrue(config["offline_only"])
        self.assertEqual(
            config["evaluation_status"], "exposed_development_not_sealed"
        )
        for field in (
            "suite_manifest_sha256",
            "decisions_sha256",
            "leakage_audit_sha256",
        ):
            self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", config[field]))
        self.assertEqual(config["calibration_bins"], 10)
        self.assertEqual(config["confidence"], 0.95)


class ScopeRankSelectiveIntegrationTests(unittest.TestCase):
    def test_evaluation_is_bound_posthoc_and_preserves_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset_path = root / "dataset.jsonl"
            rows = []
            for index, publication_date in enumerate(
                (
                    "2026-01-01",
                    "2026-02-01",
                    "2026-04-01",
                    "2026-05-01",
                    "2026-06-01",
                    "2026-06-02",
                )
            ):
                rows.append(
                    {
                        "paper_id": f"q{index}",
                        "title": f"unique paper title number {index}",
                        "abstract": f"independent abstract evidence {index}",
                        "publication_date": publication_date,
                        "gold_journal_id": "v1" if index % 2 == 0 else "v2",
                    }
                )
            dataset_path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            bundle = load_recent_journal_dataset(dataset_path)
            query_ids = tuple(query.query_id for query in bundle.queries)
            variants = (
                "scope_rank_full",
                "scope_rank_ablate_calibration",
                "scope_rank_ablate_constraint_features",
            )
            decisions_path = root / "decisions.jsonl"
            decisions = []
            for variant in variants:
                for offset, query_id in enumerate(query_ids):
                    gold = next(iter(bundle.qrels[query_id]))
                    correct = offset % 2 == 0
                    abstain = variant != "scope_rank_ablate_calibration"
                    decisions.append(
                        {
                            "variant": variant,
                            "query_id": query_id,
                            "top_candidate_id": gold if correct else "wrong",
                            "calibrated_score": 0.8 if correct else 0.2,
                            "confidence": 0.7 if correct else 0.1,
                            "abstain": abstain,
                            "reason": "below_calibrated_relevance_threshold"
                            if abstain
                            else None,
                        }
                    )
            decisions_path.write_text(
                "".join(json.dumps(row) + "\n" for row in decisions),
                encoding="utf-8",
            )
            full_run_path = root / "full.jsonl"
            constraint_run_path = root / "constraint.jsonl"
            full_run_path.write_text(
                json.dumps(
                    {
                        "query_id": "q0",
                        "rank": 1,
                        "score": 0.9,
                        "venue_id": "v1",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            constraint_run_path.write_text(
                json.dumps(
                    {
                        "query_id": "q0",
                        "rank": 1,
                        "score": 0.9000000001,
                        "venue_id": "v1",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            leakage_path = root / "leakage.json"
            _write_json(leakage_path, {"passed": True, "findings": []})
            suite_path = root / "suite.json"
            suite = {
                "artifact_type": "scope_rank_suite",
                "status": "complete_exposed_development_not_sealed",
                "binding": {
                    "dataset": {"sha256": sha256_file(dataset_path)},
                    "queries": {
                        "count": len(query_ids),
                        "ordered_ids_sha256": ordered_ids_sha256(query_ids),
                    },
                },
                "outputs": {
                    "decisions": {"sha256": sha256_file(decisions_path)}
                },
                "leakage_audit": {"sha256": sha256_file(leakage_path)},
                "variants": {
                    variant: {
                        "run": {
                            "path": str(
                                constraint_run_path
                                if variant
                                == "scope_rank_ablate_constraint_features"
                                else full_run_path
                            ),
                            "sha256": sha256_file(
                                constraint_run_path
                                if variant
                                == "scope_rank_ablate_constraint_features"
                                else full_run_path
                            ),
                        },
                        "calibration": {
                            "enabled": variant != "scope_rank_ablate_calibration"
                        },
                        "selective_output": {},
                    }
                    for variant in variants
                },
            }
            _write_json(suite_path, suite)
            output_dir = root / "output"
            config_path = root / "config.json"
            config = {
                "schema_version": 1,
                "offline_only": True,
                "evaluation_status": "exposed_development_not_sealed",
                "output_dir": str(output_dir),
                "suite_manifest": str(suite_path),
                "suite_manifest_sha256": sha256_file(suite_path),
                "decisions": str(decisions_path),
                "decisions_sha256": sha256_file(decisions_path),
                "leakage_audit": str(leakage_path),
                "leakage_audit_sha256": sha256_file(leakage_path),
                "dataset": {"path": str(dataset_path)},
                "temporal_split": {
                    "start": "2026-01-01",
                    "train_end": "2026-03-31",
                    "validation_end": "2026-05-31",
                    "test_end": "2026-06-30",
                },
                "calibration_bins": 5,
                "confidence": 0.95,
            }
            _write_json(config_path, config)

            manifest = evaluate_scope_rank_selective(
                config_path,
                generation_command=("python", "-m", "research"),
            )
            metrics = json.loads(
                (output_dir / "metrics.json").read_text(encoding="utf-8")
            )
            full = metrics["methods"]["scope_rank_full"]["primary_test"]
            no_calibration = metrics["methods"][
                "scope_rank_ablate_calibration"
            ]["primary_test"]
            self.assertEqual(manifest["coverage"]["variant_count"], 3)
            self.assertEqual(manifest["coverage"]["decision_count"], 18)
            self.assertEqual(full["coverage"], 0.0)
            self.assertIsNone(full["selective_precision"])
            self.assertEqual(no_calibration["coverage"], 1.0)
            self.assertEqual(no_calibration["selective_precision"], 0.5)
            self.assertTrue(
                metrics["frozen_ablation_checks"][
                    "calibration_ablation_exact_score_run_equal"
                ]
            )
            self.assertFalse(
                metrics["frozen_ablation_checks"][
                    "constraint_feature_ablation_exact_score_run_equal"
                ]
            )
            self.assertTrue(
                metrics["frozen_ablation_checks"][
                    "constraint_feature_ablation_rank_order_equal"
                ]
            )

            with self.assertRaisesRegex(ResearchDataError, "will not be overwritten"):
                evaluate_scope_rank_selective(
                    config_path,
                    generation_command=("python", "-m", "research"),
                )
            config["output_dir"] = str(root / "tampered")
            config["decisions_sha256"] = "0" * 64
            _write_json(config_path, config)
            with self.assertRaisesRegex(ResearchDataError, "decisions SHA-256"):
                evaluate_scope_rank_selective(
                    config_path,
                    generation_command=("python", "-m", "research"),
                )
            self.assertFalse((root / "tampered").exists())
            self.assertEqual(len(list(root.glob("tampered.failed-*"))), 1)


if __name__ == "__main__":
    unittest.main()
