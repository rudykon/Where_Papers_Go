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
    write_run,
)
from research.reranker_runs import (
    LocalBGECrossEncoderProvider,
    build_cross_encoder_run,
)
from research.types import ScoredDocument


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


class FakeCrossEncoder:
    model = "fake/reranker"
    model_repo = "fake/reranker"
    model_revision = "0123456789abcdef0123456789abcdef01234567"
    fingerprint = hashlib.sha256(b"fake-reranker-v1").hexdigest()
    batch_size = 2
    max_length = 512
    device = "cpu"
    asset_record = {
        "model": {
            "repo": model_repo,
            "revision": model_revision,
            "directory": {"tree_sha256": "c" * 64},
        }
    }

    def __init__(self) -> None:
        self.scored_pair_count = 0

    def prepare_pair(self, query: str, passage: str) -> tuple[str, str]:
        return " ".join(query.split()), " ".join(passage.split())

    def score_pairs(self, pairs: list[tuple[str, str]]) -> list[float]:
        self.scored_pair_count += len(pairs)
        output = []
        for query, passage in pairs:
            query_tokens = set(query.casefold().split())
            passage_tokens = set(passage.casefold().split())
            output.append(float(len(query_tokens & passage_tokens)))
        return output


class CrossEncoderRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.dataset = self.root / "dataset.jsonl"
        self.profiles = self.root / "profiles.jsonl"
        self.reference = self.root / "reference.json"
        self.first_stage = self.root / "first-stage.jsonl"
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
                    "abstract": "clinical diagnosis",
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
                            "text": "neural graph evidence",
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
                            "text": "clinical cancer diagnosis",
                            "weight": 1.0,
                            "temporal_eligible": True,
                        }
                    ],
                },
            ],
        )
        self.bundle = load_recent_journal_dataset(self.dataset)
        reference_binding = build_run_binding(
            dataset_path=self.dataset,
            profiles_path=self.profiles,
            query_ids=("q1", "q2"),
            candidate_ids=("v1", "v2"),
            configuration={"reference": "unit"},
        )
        self.reference.write_text(
            json.dumps({"binding": reference_binding}, sort_keys=True),
            encoding="utf-8",
        )
        first_stage_config = {"builder": "unit-first-stage", "top_k": 2}
        first_stage_binding = build_run_binding(
            dataset_path=self.dataset,
            profiles_path=self.profiles,
            query_ids=("q1", "q2"),
            candidate_ids=("v1", "v2"),
            configuration=first_stage_config,
        )
        write_run(
            self.first_stage,
            {
                "q1": [ScoredDocument("v2", 2.0), ScoredDocument("v1", 1.0)],
                "q2": [ScoredDocument("v1", 2.0), ScoredDocument("v2", 1.0)],
            },
            binding=first_stage_binding,
            query_ids=("q1", "q2"),
            candidate_ids=("v1", "v2"),
            top_k=2,
            method={
                "name": "unit_first_stage",
                "kind": "lightrag_mix",
                "implementation_revision": "unit-first-stage-v1",
            },
            command=("python", "-m", "research", "unit-first-stage"),
            working_directory=self.root,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_cross_encoder_reranks_every_candidate_and_resumes_cache(self) -> None:
        provider = FakeCrossEncoder()
        cache = self.root / "pairs.sqlite3"
        output = self.root / "cross.jsonl"
        manifest = build_cross_encoder_run(
            provider=provider,
            bundle=self.bundle,
            dataset_path=self.dataset,
            profiles_path=self.profiles,
            reference_manifest_path=self.reference,
            first_stage_run_path=self.first_stage,
            cache_path=cache,
            output_path=output,
            candidate_pool=2,
            top_k=2,
            generation_command=("python", "-m", "research", "unit-cross"),
        )
        self.assertEqual(provider.scored_pair_count, 4)
        self.assertEqual(manifest["coverage_details"]["pair_count"], 4)
        self.assertEqual(manifest["coverage_details"]["newly_scored_pair_count"], 4)
        self.assertTrue(manifest["execution"]["search_free"])
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

        resumed = FakeCrossEncoder()
        resumed_manifest = build_cross_encoder_run(
            provider=resumed,
            bundle=self.bundle,
            dataset_path=self.dataset,
            profiles_path=self.profiles,
            reference_manifest_path=self.reference,
            first_stage_run_path=self.first_stage,
            cache_path=cache,
            output_path=self.root / "cross-resumed.jsonl",
            candidate_pool=2,
            top_k=2,
            generation_command=("python", "-m", "research", "unit-resume"),
        )
        self.assertEqual(resumed.scored_pair_count, 0)
        self.assertEqual(
            resumed_manifest["coverage_details"]["cached_pair_count"], 4
        )

    def test_overwrite_and_invalid_depth_fail_closed(self) -> None:
        provider = FakeCrossEncoder()
        output = self.root / "frozen.jsonl"
        arguments = {
            "provider": provider,
            "bundle": self.bundle,
            "dataset_path": self.dataset,
            "profiles_path": self.profiles,
            "reference_manifest_path": self.reference,
            "first_stage_run_path": self.first_stage,
            "cache_path": self.root / "cache.sqlite3",
            "output_path": output,
            "candidate_pool": 2,
            "top_k": 2,
            "generation_command": ("python", "-m", "research", "unit"),
        }
        build_cross_encoder_run(**arguments)
        with self.assertRaisesRegex(ResearchDataError, "refusing to overwrite"):
            build_cross_encoder_run(**arguments)
        with self.assertRaisesRegex(ResearchDataError, "candidate_pool"):
            build_cross_encoder_run(
                **{
                    **arguments,
                    "output_path": self.root / "other.jsonl",
                    "candidate_pool": 1,
                    "top_k": 2,
                }
            )

    def test_local_provider_hashes_assets_before_lazy_model_load(self) -> None:
        model_dir = self.root / "model"
        model_dir.mkdir()
        (model_dir / "config.json").write_text("{}\n", encoding="utf-8")
        provider = LocalBGECrossEncoderProvider(
            model_dir=model_dir,
            model_repo="BAAI/bge-reranker-v2-m3",
            model_revision="a" * 40,
            device="cpu",
        )
        fp32_provider = LocalBGECrossEncoderProvider(
            model_dir=model_dir,
            model_repo="BAAI/bge-reranker-v2-m3",
            model_revision="a" * 40,
            device="cpu",
            fp16=False,
        )
        self.assertEqual(
            provider.prepare_pair(" query ", " passage "), ("query", "passage")
        )
        self.assertEqual(len(provider.fingerprint), 64)
        self.assertNotEqual(provider.fingerprint, fp32_provider.fingerprint)


if __name__ == "__main__":
    unittest.main()
