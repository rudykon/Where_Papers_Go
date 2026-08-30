from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.data import (
    build_run_binding,
    canonical_json_sha256,
    ordered_ids_sha256,
    sha256_file,
    write_run,
)
from research.sealed_preflight import preflight_sealed_evaluation
from research.types import ScoredDocument


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


class SealedEvaluationPreflightTests(unittest.TestCase):
    def test_complete_preflight_never_parses_label_content(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            blind = root / "queries.blind.jsonl"
            blind.write_text(
                json.dumps(
                    {
                        "paper_id": "doi:10.1/a",
                        "title": "A future query",
                        "abstract": "A sufficiently descriptive abstract",
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
            labels.write_text("this is deliberately not JSON\n", encoding="utf-8")
            os.chmod(labels, 0o600)
            profiles = root / "profiles.jsonl"
            profiles.write_text(
                json.dumps(
                    {
                        "venue_id": "venue-a",
                        "name": "Venue A",
                        "snapshot_date": "2026-03-31",
                        "metadata": {},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            query_ids = ("doi:10.1/a",)
            candidate_ids = ("venue-a",)
            runtime = {
                "code": {"commit": "test", "dirty": False},
                "python": {"version": "test"},
                "dependencies": [],
                "hardware": {},
            }
            committed = {}
            for index, name in enumerate(("method_a", "method_b"), 1):
                run_path = root / f"{name}.jsonl"
                generation_config = {"method": name}
                binding = build_run_binding(
                    dataset_path=blind,
                    profiles_path=profiles,
                    query_ids=query_ids,
                    candidate_ids=candidate_ids,
                    configuration=generation_config,
                )
                manifest = write_run(
                    run_path,
                    {"doi:10.1/a": [ScoredDocument("venue-a", float(index))]},
                    binding=binding,
                    query_ids=query_ids,
                    candidate_ids=candidate_ids,
                    top_k=1,
                    method={
                        "name": name,
                        "implementation_revision": f"test-{name}",
                        "configuration_sha256": canonical_json_sha256(
                            generation_config
                        ),
                    },
                    command=("test", name),
                    working_directory=root,
                    runtime=runtime,
                    additional_manifest_fields={
                        "execution": {
                            "failed_query_count": 0,
                            "external_api_calls": 0,
                            "search_free": True,
                        }
                    },
                )
                sidecar = run_path.with_suffix(".jsonl.manifest.json")
                committed[name] = {
                    "run": manifest["output"],
                    "manifest": {
                        "path": str(sidecar.resolve()),
                        "sha256": sha256_file(sidecar),
                        "bytes": sidecar.stat().st_size,
                    },
                }

            method_freeze = {
                "method_family": ["method_a", "method_b"],
                "method_hyperparameters": {"frozen": True},
                "metrics": {
                    "primary": "ndcg@10",
                    "retrieval_depth": 1,
                    "cutoffs": [1],
                    "reported": ["ndcg"],
                    "denominator_policy": "all_300_queries_no_failure_removal",
                },
                "statistics": {
                    "comparison_family": "all_methods_unordered_pairs",
                    "metric": "ndcg@10",
                    "bootstrap_iterations": 2,
                    "permutation_iterations": 2,
                    "confidence": 0.95,
                    "seed": 7,
                    "multiple_comparison_corrections": [
                        "holm_family_wise",
                        "benjamini_hochberg_fdr",
                    ],
                },
            }
            freeze_config = root / "freeze.json"
            _write_json(freeze_config, {"method_freeze": method_freeze})
            commitment = root / "prediction_commitment.json"
            _write_json(
                commitment,
                {
                    "status": "predictions_committed_before_label_access",
                    "label_vault_commitment": {
                        "sha256": sha256_file(labels),
                        "content_parsed": False,
                    },
                    "query_count": 1,
                    "query_ids_sha256": ordered_ids_sha256(query_ids),
                    "candidate_count": 1,
                    "candidate_ids_sha256": ordered_ids_sha256(candidate_ids),
                    "sources": committed,
                    "variants": {},
                },
            )
            sealed_manifest = root / "sealed_manifest.json"
            _write_json(
                sealed_manifest,
                {
                    "artifact_type": "future_sealed_test",
                    "status": "labels_sealed_predictions_pending",
                    "config": {
                        "path": str(freeze_config.resolve()),
                        "sha256": sha256_file(freeze_config),
                        "bytes": freeze_config.stat().st_size,
                    },
                    "method_freeze": {
                        "method_family": method_freeze["method_family"],
                        "method_hyperparameters_sha256": canonical_json_sha256(
                            method_freeze["method_hyperparameters"]
                        ),
                        "metrics_sha256": canonical_json_sha256(
                            method_freeze["metrics"]
                        ),
                        "statistics_sha256": canonical_json_sha256(
                            method_freeze["statistics"]
                        ),
                        "candidates": {
                            "count": 1,
                            "ordered_ids_sha256": ordered_ids_sha256(candidate_ids),
                            "profiles_sha256": sha256_file(profiles),
                        },
                    },
                    "dataset": {
                        "record_count": 1,
                        "blind_queries": {
                            "path": str(blind.resolve()),
                            "sha256": sha256_file(blind),
                            "bytes": blind.stat().st_size,
                        },
                        "sealed_labels": {
                            "path": str(labels.resolve()),
                            "sha256": sha256_file(labels),
                            "bytes": labels.stat().st_size,
                        },
                    },
                },
            )
            evaluation = root / "evaluation.json"
            _write_json(
                evaluation,
                {
                    "schema_version": 1,
                    "status": "predictions_committed_before_label_unseal",
                    "offline_only": True,
                    "search_free": True,
                    "output_dir": str(root / "evaluation-output"),
                    "sealed_test": {
                        "manifest": str(sealed_manifest),
                        "manifest_sha256": sha256_file(sealed_manifest),
                    },
                    "prediction_commitment": {
                        "path": str(commitment),
                        "sha256": sha256_file(commitment),
                    },
                    "corpus": {
                        "path": str(profiles),
                        "id_field": "venue_id",
                        "text_fields": ["name"],
                        "snapshot_field": "snapshot_date",
                    },
                    "methods": [
                        {
                            "name": name,
                            "path": record["run"]["path"],
                            "run_sha256": record["run"]["sha256"],
                            "manifest_path": record["manifest"]["path"],
                            "manifest_sha256": record["manifest"]["sha256"],
                        }
                        for name, record in committed.items()
                    ],
                    "evaluation": {"cutoffs": [1]},
                    "statistics": {
                        key: value
                        for key, value in method_freeze["statistics"].items()
                        if key != "multiple_comparison_corrections"
                    },
                },
            )
            report = preflight_sealed_evaluation(evaluation)
            self.assertEqual(report["status"], "ready_for_single_label_access")
            self.assertFalse(report["label_content_parsed"])
            self.assertEqual(report["coverage"]["method_order"], ["method_a", "method_b"])


if __name__ == "__main__":
    unittest.main()
