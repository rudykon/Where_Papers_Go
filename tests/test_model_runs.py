from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from research.data import (
    ResearchDataError,
    build_run_binding,
    load_recent_journal_dataset,
    load_score_run,
    sha256_file,
)
from research.model_runs import (
    TITLE_ABSTRACT_SEPARATOR,
    LocalScientificEncoderProvider,
    build_scientific_encoder_run,
    load_scientific_prototypes,
)


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


class FakeScientificProvider:
    model = "fake/scientific"
    model_repo = "fake/scientific"
    model_revision = "0123456789abcdef0123456789abcdef01234567"
    protocol = "scincl"
    fingerprint = hashlib.sha256(b"fake-scientific-v1").hexdigest()
    batch_size = 2
    max_length = 512
    device = "cpu"
    asset_record = {
        "model": {
            "repo": model_repo,
            "revision": model_revision,
            "directory": {"tree_sha256": "a" * 64},
        }
    }

    def __init__(self) -> None:
        self.embedded_text_count = 0

    def prepare_text(self, text: str) -> str:
        if TITLE_ABSTRACT_SEPARATOR not in text:
            raise ResearchDataError("missing scientific separator")
        title, abstract = text.split(TITLE_ABSTRACT_SEPARATOR, 1)
        return (
            " ".join(title.split())
            + TITLE_ABSTRACT_SEPARATOR
            + " ".join(abstract.split())
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.embedded_text_count += len(texts)
        vectors = []
        for text in texts:
            lowered = text.casefold()
            vector = [
                float("graph" in lowered),
                float("cancer" in lowered),
                float("retrieval" in lowered),
                float("imaging" in lowered),
                float("evidence" in lowered),
                0.1,
                0.2,
                0.3,
            ]
            vectors.append(vector)
        return vectors


class ScientificModelRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.dataset = self.root / "dataset.jsonl"
        self.profiles = self.root / "profiles.jsonl"
        self.reference = self.root / "reference.json"
        _write_jsonl(
            self.dataset,
            [
                {
                    "paper_id": "q1",
                    "publication_date": "2026-04-01",
                    "title": "graph retrieval",
                    "abstract": "neural evidence",
                    "gold_journal_id": "v1",
                },
                {
                    "paper_id": "q2",
                    "publication_date": "2026-04-02",
                    "title": "cancer imaging",
                    "abstract": "clinical evidence",
                    "gold_journal_id": "v2",
                },
            ],
        )
        _write_jsonl(
            self.profiles,
            [
                {
                    "venue_id": "v1",
                    "name": "Graph Venue",
                    "snapshot_date": "2026-03-31",
                    "prototypes": [
                        {
                            "prototype_id": "v1:p0",
                            "label": "Graph retrieval",
                            "text": "neural graph retrieval evidence",
                            "weight": 1.0,
                            "temporal_eligible": True,
                        }
                    ],
                },
                {
                    "venue_id": "v2",
                    "name": "Medical Venue",
                    "snapshot_date": "2026-03-31",
                    "prototypes": [
                        {
                            "prototype_id": "v2:p0",
                            "label": "Cancer imaging",
                            "text": "clinical cancer imaging evidence",
                            "weight": 1.0,
                            "temporal_eligible": True,
                        }
                    ],
                },
            ],
        )
        self.bundle = load_recent_journal_dataset(self.dataset)
        binding = build_run_binding(
            dataset_path=self.dataset,
            profiles_path=self.profiles,
            query_ids=("q1", "q2"),
            candidate_ids=("v1", "v2"),
            configuration={"reference": "unit"},
        )
        self.reference.write_text(
            json.dumps({"binding": binding}, sort_keys=True), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_builder_uses_title_abstract_protocol_and_resumable_cache(self) -> None:
        provider = FakeScientificProvider()
        cache = self.root / "embeddings.sqlite3"
        output = self.root / "scincl.jsonl"
        manifest = build_scientific_encoder_run(
            provider=provider,
            bundle=self.bundle,
            dataset_path=self.dataset,
            profiles_path=self.profiles,
            reference_manifest_path=self.reference,
            cache_path=cache,
            output_path=output,
            top_k=2,
            query_batch_size=2,
            prototype_chunk_size=2,
            generation_command=("python", "-m", "research", "unit-scientific"),
        )
        self.assertEqual(provider.embedded_text_count, 4)
        self.assertEqual(manifest["coverage_details"]["embedded_text_count"], 4)
        self.assertEqual(manifest["coverage_details"]["cached_text_count"], 0)
        self.assertEqual(manifest["official_input_protocol"]["max_length"], 512)
        self.assertTrue(manifest["execution"]["local_files_only"])
        manifest_path = output.with_suffix(output.suffix + ".manifest.json")
        loaded = load_score_run(
            output,
            expected_query_ids=("q1", "q2"),
            candidate_ids=("v1", "v2"),
            expected_binding=manifest["binding"],
            expected_manifest_sha256=sha256_file(manifest_path),
            expected_configuration_sha256=manifest["binding"]["configuration"][
                "canonical_sha256"
            ],
            expected_method_identity={
                "provider_fingerprint": provider.fingerprint
            },
        )
        self.assertEqual(loaded["q1"][0].doc_id, "v1")
        self.assertEqual(loaded["q2"][0].doc_id, "v2")

        second_provider = FakeScientificProvider()
        second_manifest = build_scientific_encoder_run(
            provider=second_provider,
            bundle=self.bundle,
            dataset_path=self.dataset,
            profiles_path=self.profiles,
            reference_manifest_path=self.reference,
            cache_path=cache,
            output_path=self.root / "scincl-resumed.jsonl",
            top_k=2,
            query_batch_size=2,
            prototype_chunk_size=2,
            generation_command=("python", "-m", "research", "unit-resume"),
        )
        self.assertEqual(second_provider.embedded_text_count, 0)
        self.assertEqual(second_manifest["coverage_details"]["embedded_text_count"], 0)
        self.assertEqual(second_manifest["coverage_details"]["cached_text_count"], 4)

    def test_profile_mapping_and_output_overwrite_are_fail_closed(self) -> None:
        units, venues = load_scientific_prototypes(self.profiles)
        self.assertEqual(venues, ("v1", "v2"))
        self.assertEqual(units[0].title, "Graph retrieval")
        self.assertIn(TITLE_ABSTRACT_SEPARATOR, units[0].model_input)
        provider = FakeScientificProvider()
        output = self.root / "frozen.jsonl"
        arguments = {
            "provider": provider,
            "bundle": self.bundle,
            "dataset_path": self.dataset,
            "profiles_path": self.profiles,
            "reference_manifest_path": self.reference,
            "cache_path": self.root / "cache.sqlite3",
            "output_path": output,
            "top_k": 2,
            "generation_command": ("python", "-m", "research", "unit"),
        }
        build_scientific_encoder_run(**arguments)
        with self.assertRaisesRegex(ResearchDataError, "refusing to overwrite"):
            build_scientific_encoder_run(**arguments)

    def test_local_provider_requires_pinned_assets_without_loading_torch(self) -> None:
        model_dir = self.root / "model"
        adapter_dir = self.root / "adapter"
        model_dir.mkdir()
        adapter_dir.mkdir()
        (model_dir / "config.json").write_text("{}\n", encoding="utf-8")
        (adapter_dir / "adapter_config.json").write_text("{}\n", encoding="utf-8")
        provider = LocalScientificEncoderProvider(
            protocol="specter2",
            model_dir=model_dir,
            model_repo="allenai/specter2_base",
            model_revision="a" * 40,
            adapter_dir=adapter_dir,
            adapter_repo="allenai/specter2",
            adapter_revision="b" * 40,
            device="cpu",
        )
        fp32_provider = LocalScientificEncoderProvider(
            protocol="specter2",
            model_dir=model_dir,
            model_repo="allenai/specter2_base",
            model_revision="a" * 40,
            adapter_dir=adapter_dir,
            adapter_repo="allenai/specter2",
            adapter_revision="b" * 40,
            device="cpu",
            fp16=False,
        )
        prepared = provider.prepare_text(
            " A title " + TITLE_ABSTRACT_SEPARATOR + " An abstract "
        )
        self.assertEqual(
            prepared, "A title" + TITLE_ABSTRACT_SEPARATOR + "An abstract"
        )
        self.assertEqual(len(provider.fingerprint), 64)
        self.assertNotEqual(provider.fingerprint, fp32_provider.fingerprint)
        with self.assertRaisesRegex(ResearchDataError, "proximity adapter"):
            LocalScientificEncoderProvider(
                protocol="specter2",
                model_dir=model_dir,
                model_repo="allenai/specter2_base",
                model_revision="a" * 40,
            )


if __name__ == "__main__":
    unittest.main()
