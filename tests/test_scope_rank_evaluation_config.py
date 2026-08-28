from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = (
    PROJECT_ROOT
    / "research"
    / "configs"
    / "scope_rank_unified_evaluation_v1.json"
)


class ScopeRankEvaluationConfigTests(unittest.TestCase):
    def test_complete_pairwise_family_is_frozen_before_evaluation(self) -> None:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        methods = [row["name"] for row in config["imported_runs"]]
        family = config["statistics"]["comparison_family"]

        self.assertTrue(config["offline_only"])
        self.assertTrue(config["fail_on_critical_leakage"])
        self.assertEqual(
            config["evaluation_status"], "exposed_development_not_sealed"
        )
        self.assertEqual(config["baselines"], [])
        self.assertEqual(config["fusions"], [])
        self.assertEqual(len(methods), 13)
        self.assertEqual(len(set(methods)), 13)
        self.assertEqual(methods[0], "m3_lightrag_strongest")
        self.assertEqual(methods[1], "scope_rank_full")
        self.assertEqual(family["type"], "all_methods_unordered_pairs")
        self.assertEqual(family["metric"], "ndcg@10")
        self.assertEqual(family["method_order"], methods)
        self.assertEqual(len(methods) * (len(methods) - 1) // 2, 78)
        self.assertEqual(config["statistics"]["bootstrap_iterations"], 2000)
        self.assertEqual(config["statistics"]["permutation_iterations"], 2000)

        for row in config["imported_runs"]:
            self.assertEqual(row["corpus_view"], "prototypes")
            for field in (
                "run_sha256",
                "manifest_sha256",
                "generation_config_sha256",
            ):
                self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", row[field]))
            self.assertTrue(row["implementation_revision"])


if __name__ == "__main__":
    unittest.main()
