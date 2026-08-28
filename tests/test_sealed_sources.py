from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.data import load_blind_query_dataset
from research.sealed_sources import (
    build_sealed_lexical_run,
    build_sealed_reference_binding,
)


class SealedSourceRunTests(unittest.TestCase):
    def test_reference_and_bm25_run_use_only_blind_queries(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "queries.blind.jsonl"
            dataset.write_text(
                json.dumps(
                    {
                        "paper_id": "q1",
                        "title": "quantum graph",
                        "abstract": "network methods",
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
            profiles = root / "profiles.jsonl"
            profiles.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in (
                        {
                            "venue_id": "v1",
                            "name": "Physics",
                            "snapshot_date": "2026-03-31",
                            "prototypes": [
                                {"text": "quantum graph network", "temporal_eligible": True}
                            ],
                        },
                        {
                            "venue_id": "v2",
                            "name": "Medicine",
                            "snapshot_date": "2026-03-31",
                            "prototypes": [
                                {"text": "clinical oncology", "temporal_eligible": True}
                            ],
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            bundle = load_blind_query_dataset(dataset)
            reference = root / "reference.json"
            reference_manifest = build_sealed_reference_binding(
                bundle=bundle,
                dataset_path=dataset,
                profiles_path=profiles,
                output_path=reference,
                profile_cutoff="2026-03-31",
                generation_command=("python", "test"),
            )
            self.assertFalse(reference_manifest["label_boundary"]["qrels_present"])
            output = root / "bm25.jsonl"
            manifest = build_sealed_lexical_run(
                bundle=bundle,
                dataset_path=dataset,
                profiles_path=profiles,
                reference_manifest_path=reference,
                output_path=output,
                method_name="bm25",
                method_type="bm25",
                top_k=2,
                generation_command=("python", "test"),
            )
            first = json.loads(output.read_text().splitlines()[0])
            self.assertEqual(first["venue_id"], "v1")
            self.assertFalse(manifest["label_boundary"]["labels_accessed"])


if __name__ == "__main__":
    unittest.main()
