from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = (
    PROJECT_ROOT / "research" / "configs" / "m3_all_strong_baselines_unified_v2.json"
)


class M3UnifiedConfigTests(unittest.TestCase):
    def test_all_methods_share_one_complete_pairwise_family(self) -> None:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        methods = [
            *(row["name"] for row in config["baselines"]),
            *(row["name"] for row in config["imported_runs"]),
            *(row["name"] for row in config["fusions"]),
        ]
        family = config["statistics"]["comparison_family"]

        self.assertEqual(config["evaluation_status"], "exposed_development_not_sealed")
        self.assertTrue(config["offline_only"])
        self.assertTrue(config["fail_on_critical_leakage"])
        self.assertEqual(len(methods), 11)
        self.assertEqual(len(set(methods)), len(methods))
        self.assertEqual(family["type"], "all_methods_unordered_pairs")
        self.assertEqual(family["method_order"], methods)
        self.assertEqual(len(methods) * (len(methods) - 1) // 2, 55)

        known = set(methods)
        for fusion in config["fusions"]:
            self.assertTrue(fusion["sources"])
            self.assertLessEqual(set(fusion["sources"]), known)
        for imported in config["imported_runs"]:
            self.assertRegex(imported["manifest_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(
                imported["generation_config_sha256"], r"^[0-9a-f]{64}$"
            )
            identities = {
                key
                for key in (
                    "model_revision",
                    "provider_fingerprint",
                    "implementation_revision",
                )
                if imported.get(key)
            }
            self.assertTrue(identities)
            for key in identities:
                value = imported[key]
                if key == "provider_fingerprint":
                    self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", value))


if __name__ == "__main__":
    unittest.main()
