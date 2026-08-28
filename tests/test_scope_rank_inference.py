from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.data import ResearchDataError, sha256_file
from research.scope_rank_inference import load_frozen_pairwise_ranker
from research.scope_rank_runs import VariantSpec, _feature_names


class FrozenRankerLoadingTests(unittest.TestCase):
    def test_model_is_loaded_without_fit_and_validates_schema(self) -> None:
        features = _feature_names(VariantSpec("scope_rank_full"))
        payload = {
            "schema_version": 1,
            "model_type": "pairwise_linear_logistic",
            "feature_names": list(features),
            "scales": [1.0] * len(features),
            "weights": [0.0] * len(features),
            "training_report": {
                "feature_count": len(features),
                "training_query_count": 8,
                "skipped_query_count": 2,
                "pair_count": 16,
                "epochs": 3,
                "learning_rate": 0.1,
                "l2": 0.01,
                "final_loss": 0.5,
            },
        }
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "model.json"
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            ranker = load_frozen_pairwise_ranker(
                path,
                expected_sha256=sha256_file(path),
                expected_features=features,
            )
            self.assertTrue(ranker.fitted)
            self.assertEqual(ranker.report.training_query_count, 8)
            self.assertEqual(ranker.predict({name: 0.0 for name in features}), 0.5)

            with self.assertRaisesRegex(ResearchDataError, "SHA-256"):
                load_frozen_pairwise_ranker(
                    path,
                    expected_sha256="0" * 64,
                    expected_features=features,
                )


if __name__ == "__main__":
    unittest.main()
