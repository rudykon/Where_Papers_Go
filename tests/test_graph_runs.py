from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from research.data import (
    ResearchDataError,
    build_run_binding,
    canonical_json_sha256,
    load_recent_journal_dataset,
    load_score_run,
    sha256_file,
    write_run,
)
from research.graph_runs import (
    build_lightrag_mix_run,
    build_property_graph_run,
    load_frozen_edge_graph,
    mix_ranked_runs,
)
from research.types import ScoredDocument


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _record(path: Path) -> dict[str, object]:
    return {
        "path": path.name,
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


class GraphRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.dataset = self.root / "dataset.jsonl"
        self.profiles = self.root / "profiles.jsonl"
        self.prototypes = self.root / "prototypes.jsonl"
        self.evidence = self.root / "evidence.jsonl"
        self.corpus_manifest = self.root / "corpus-manifest.json"
        self.reference_manifest = self.root / "reference-manifest.json"
        _write_jsonl(
            self.dataset,
            [
                {
                    "paper_id": "q1",
                    "publication_date": "2026-04-01",
                    "title": "neural graph retrieval",
                    "abstract": "evidence propagation",
                    "gold_journal_id": "v1",
                },
                {
                    "paper_id": "q2",
                    "publication_date": "2026-04-02",
                    "title": "cancer imaging diagnosis",
                    "abstract": "clinical evidence",
                    "gold_journal_id": "v2",
                },
            ],
        )
        prototype_rows: list[dict[str, object]] = [
            {
                "prototype_id": "v1:static",
                "venue_id": "v1",
                "text": "graph journal",
                "source_ids": ["catalog:v1"],
                "weight": 0.35,
                "temporal_eligible": True,
            },
            {
                "prototype_id": "v1:pcl:0",
                "venue_id": "v1",
                "text": "neural graph retrieval evidence propagation",
                "source_ids": ["catalog:v1", "paper:v1:1"],
                "weight": 1.0,
                "temporal_eligible": True,
            },
            {
                "prototype_id": "v2:static",
                "venue_id": "v2",
                "text": "medical journal",
                "source_ids": ["catalog:v2"],
                "weight": 0.35,
                "temporal_eligible": True,
            },
            {
                "prototype_id": "v2:pcl:0",
                "venue_id": "v2",
                "text": "clinical cancer imaging diagnosis",
                "source_ids": ["catalog:v2", "paper:v2:1"],
                "weight": 1.0,
                "temporal_eligible": True,
            },
        ]
        _write_jsonl(self.prototypes, prototype_rows)
        profile_rows = []
        for venue_id, name in (("v1", "Graph Venue"), ("v2", "Medical Venue")):
            profile_rows.append(
                {
                    "venue_id": venue_id,
                    "name": name,
                    "snapshot_date": "2026-03-31",
                    "prototypes": [
                        {
                            key: value
                            for key, value in row.items()
                            if key != "venue_id"
                        }
                        for row in prototype_rows
                        if row["venue_id"] == venue_id
                    ],
                }
            )
        _write_jsonl(self.profiles, profile_rows)
        _write_jsonl(
            self.evidence,
            [
                {
                    "evidence_id": "catalog:v1",
                    "venue_id": "v1",
                    "kind": "catalog",
                    "valid_at": "2026-03-31",
                    "temporal_eligible": True,
                    "text": "graph journal",
                },
                {
                    "evidence_id": "paper:v1:1",
                    "venue_id": "v1",
                    "kind": "paper",
                    "valid_at": "2026-03-01",
                    "temporal_eligible": True,
                    "text": "neural graph retrieval and propagation",
                },
                {
                    "evidence_id": "catalog:v2",
                    "venue_id": "v2",
                    "kind": "catalog",
                    "valid_at": "2026-03-31",
                    "temporal_eligible": True,
                    "text": "medical journal",
                },
                {
                    "evidence_id": "paper:v2:1",
                    "venue_id": "v2",
                    "kind": "paper",
                    "valid_at": "2026-02-01",
                    "temporal_eligible": True,
                    "text": "clinical cancer imaging diagnosis",
                },
                {
                    "evidence_id": "unreferenced",
                    "venue_id": "v2",
                    "kind": "paper",
                    "valid_at": "2026-03-01",
                    "temporal_eligible": True,
                    "text": "must not enter the graph",
                },
            ],
        )
        self.corpus_manifest.write_text(
            json.dumps(
                {
                    "outputs": {
                        "profiles": _record(self.profiles),
                        "prototypes": _record(self.prototypes),
                        "research_evidence": _record(self.evidence),
                    },
                    "validation": {
                        "candidate_count": 2,
                        "profile_count": 2,
                        "candidate_profile_ids_match": True,
                        "missing_prototype_source_id_count": 0,
                        "ambiguous_prototype_source_id_count": 0,
                        "research_non_temporal_evidence_count": 0,
                        "research_post_cutoff_evidence_count": 0,
                        "unrelated_evidence_id_collision_count": 0,
                    },
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self.bundle = load_recent_journal_dataset(self.dataset)
        reference_binding = build_run_binding(
            dataset_path=self.dataset,
            profiles_path=self.profiles,
            query_ids=("q1", "q2"),
            candidate_ids=("v1", "v2"),
            configuration={"reference": "unit-test"},
        )
        self.reference_manifest.write_text(
            json.dumps({"binding": reference_binding}, sort_keys=True),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _build_graph(self) -> tuple[Path, dict[str, object]]:
        path = self.root / "property-graph.jsonl"
        manifest = build_property_graph_run(
            bundle=self.bundle,
            dataset_path=self.dataset,
            profiles_path=self.profiles,
            prototypes_path=self.prototypes,
            evidence_path=self.evidence,
            corpus_manifest_path=self.corpus_manifest,
            reference_manifest_path=self.reference_manifest,
            output_path=path,
            cutoff="2026-03-31",
            top_k=2,
            candidate_pool=2,
            generation_command=("python", "-m", "research", "unit-test"),
        )
        return path, manifest

    def test_property_graph_builder_validates_real_edges_and_freezes_run(self) -> None:
        path, manifest = self._build_graph()
        self.assertEqual(manifest["edge_audit"]["prototype_count"], 4)
        self.assertEqual(
            manifest["edge_audit"]["prototype_evidence_edge_count"], 6
        )
        self.assertEqual(manifest["edge_audit"]["unique_linked_evidence_count"], 4)
        self.assertEqual(manifest["edge_audit"]["paper_edge_count"], 2)
        self.assertTrue(manifest["edge_audit"]["candidate_coverage_complete"])
        self.assertTrue(manifest["execution"]["search_free"])
        self.assertEqual(manifest["execution"]["external_api_calls"], 0)
        manifest_path = path.with_suffix(path.suffix + ".manifest.json")
        loaded = load_score_run(
            path,
            expected_query_ids=("q1", "q2"),
            candidate_ids=("v1", "v2"),
            expected_binding=manifest["binding"],
            expected_manifest_sha256=sha256_file(manifest_path),
            expected_configuration_sha256=manifest["binding"]["configuration"][
                "canonical_sha256"
            ],
            expected_method_identity={
                "implementation_revision": manifest["method"][
                    "implementation_revision"
                ]
            },
        )
        self.assertEqual(loaded["q1"][0].doc_id, "v1")
        self.assertEqual(loaded["q2"][0].doc_id, "v2")
        with self.assertRaisesRegex(ResearchDataError, "refusing to overwrite"):
            self._build_graph()

    def test_graph_loader_rejects_missing_and_post_cutoff_evidence(self) -> None:
        rows = [
            json.loads(line)
            for line in self.evidence.read_text(encoding="utf-8").splitlines()
        ]
        missing_path = self.root / "missing.jsonl"
        _write_jsonl(
            missing_path,
            [row for row in rows if row["evidence_id"] != "paper:v1:1"],
        )
        with self.assertRaisesRegex(ResearchDataError, "missing evidence"):
            load_frozen_edge_graph(
                profiles_path=self.profiles,
                prototypes_path=self.prototypes,
                evidence_path=missing_path,
                cutoff="2026-03-31",
            )
        post_cutoff_path = self.root / "post-cutoff.jsonl"
        for row in rows:
            if row["evidence_id"] == "paper:v1:1":
                row["valid_at"] = "2026-04-01"
        _write_jsonl(post_cutoff_path, rows)
        with self.assertRaisesRegex(ResearchDataError, "post-cutoff"):
            load_frozen_edge_graph(
                profiles_path=self.profiles,
                prototypes_path=self.prototypes,
                evidence_path=post_cutoff_path,
                cutoff="2026-03-31",
            )

    def test_lightrag_mix_requires_audited_graph_and_vector_channels(self) -> None:
        graph_path, _graph_manifest = self._build_graph()
        vector_path = self.root / "vector.jsonl"
        vector_config = {"builder": "unit-vector", "top_k": 2}
        vector_binding = build_run_binding(
            dataset_path=self.dataset,
            profiles_path=self.profiles,
            query_ids=("q1", "q2"),
            candidate_ids=("v1", "v2"),
            configuration=vector_config,
        )
        write_run(
            vector_path,
            {
                "q1": [ScoredDocument("v1", 0.9), ScoredDocument("v2", 0.2)],
                "q2": [ScoredDocument("v2", 0.8), ScoredDocument("v1", 0.1)],
            },
            binding=vector_binding,
            query_ids=("q1", "q2"),
            candidate_ids=("v1", "v2"),
            top_k=2,
            method={
                "name": "unit_vector",
                "kind": "vector",
                "provider_fingerprint": "unit-vector-fingerprint",
                "configuration_sha256": canonical_json_sha256(vector_config),
            },
            command=("python", "-m", "research", "unit-vector"),
            working_directory=self.root,
        )
        mix_path = self.root / "mix.jsonl"
        manifest = build_lightrag_mix_run(
            bundle=self.bundle,
            dataset_path=self.dataset,
            profiles_path=self.profiles,
            reference_manifest_path=self.reference_manifest,
            property_graph_run_path=graph_path,
            vector_run_path=vector_path,
            output_path=mix_path,
            top_k=2,
            generation_command=("python", "-m", "research", "unit-mix"),
        )
        self.assertEqual(manifest["method"]["kind"], "lightrag_mix")
        self.assertTrue(
            manifest["lightrag_semantics"]["real_prototype_evidence_edges"]
        )
        self.assertTrue(manifest["lightrag_semantics"]["generative_answering"] is False)
        self.assertEqual(manifest["coverage"]["query_count"], 2)
        self.assertEqual(manifest["coverage"]["ranking_entry_count"], 4)

    def test_rank_mix_has_stable_ties(self) -> None:
        local = {"q": [ScoredDocument("b", 1.0), ScoredDocument("a", 0.5)]}
        global_run = {
            "q": [ScoredDocument("a", 1.0), ScoredDocument("b", 0.5)]
        }
        mixed = mix_ranked_runs(
            local,
            global_run,
            query_ids=("q",),
            top_k=2,
        )
        self.assertEqual([item.doc_id for item in mixed["q"]], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
