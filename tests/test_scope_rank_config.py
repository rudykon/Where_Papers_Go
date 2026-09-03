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
    / "scope_rank_exposed_development_v1.json"
)


class ScopeRankFrozenConfigTests(unittest.TestCase):
    def test_formal_config_freezes_sources_boundaries_and_ablations(self) -> None:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        channels = config["channels"]
        method = config["method"]

        self.assertEqual(config["schema_version"], 1)
        self.assertTrue(config["offline_only"])
        self.assertTrue(config["fail_on_critical_leakage"])
        self.assertEqual(
            config["evaluation_status"], "exposed_development_not_sealed"
        )
        self.assertNotIn("sealed", Path(config["output_dir"]).name)
        self.assertRegex(config["reference_manifest_sha256"], r"^[0-9a-f]{64}$")

        expected_channels = {
            "bm25",
            "bge_m3",
            "specter2",
            "scincl",
            "property_graph",
            "lightrag",
            "cross_encoder",
        }
        self.assertEqual({row["name"] for row in channels}, expected_channels)
        self.assertEqual(len(channels), len(expected_channels))
        for row in channels:
            for field in (
                "run_sha256",
                "manifest_sha256",
                "generation_config_sha256",
            ):
                self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", row[field]))
            identities = [
                row.get(field)
                for field in (
                    "model_revision",
                    "provider_fingerprint",
                    "implementation_revision",
                )
                if row.get(field)
            ]
            self.assertTrue(identities)

        recall_channels = expected_channels - {"cross_encoder"} | {"subject_route"}
        self.assertEqual(set(method["fixed_quotas"]), recall_channels)
        self.assertEqual(sum(method["fixed_quotas"].values()), 350)
        self.assertEqual(method["total_recall_budget"], 350)
        self.assertEqual(method["source_depth"], 100)
        self.assertEqual(method["top_k"], 100)
        self.assertEqual(
            method["profile_cutoff"], config["temporal_split"]["train_end"]
        )
        self.assertGreaterEqual(method["calibration_denominator"], 2)
        self.assertGreater(method["hard_negatives"], 0)
        self.assertGreater(method["epochs"], 0)


if __name__ == "__main__":
    unittest.main()
