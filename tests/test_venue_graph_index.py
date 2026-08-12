from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from where_paper_go.embeddings import build_graph_vector_index
from where_paper_go.graph_index import (
    SOURCE_FILE_NAMES,
    GraphIndexError,
    VenueGraphIndex,
    build_graph,
    export_lightrag_custom_kg,
    graph_source_digest,
    inspect_graph,
    vector_path_for_graph,
)
from where_paper_go.recommender import (
    VenueCandidate,
    VenueRecord,
    build_candidates_from_groups,
    normalize_name,
    parse_targets,
    rank_candidates_indexed,
    tokenize,
)


class FakeEmbeddingProvider:
    model = "fake-multilingual-v1"
    fingerprint = "fake-graph-provider"
    batch_size = 2

    def prepare_text(self, text: str) -> str:
        return " ".join(text.split())

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            normalized = text.casefold()
            if any(term in normalized for term in ("无线", "wireless", "untethered")):
                vectors.append([1, 1, 1, 1, -1, -1, -1, -1])
            elif any(term in normalized for term in ("边缘", "edge")):
                vectors.append([1, 1, 1, -1, -1, -1, -1, -1])
            else:
                vectors.append([1, -1, 1, -1, 1, -1, 1, -1])
        return vectors


def make_record(
    row_id: int,
    *,
    dataset: str,
    level: str,
    name: str,
    abbreviation: str,
    area: str,
    topic_tags: str = "",
    scope_id: str = "",
    scope: str = "",
) -> VenueRecord:
    reviewed = bool(scope_id)
    return VenueRecord(
        row_id=row_id,
        dataset=dataset,
        source="test",
        source_file="test.csv",
        version_year="2026",
        record_type="conference",
        name=name,
        abbreviation=abbreviation,
        issn="",
        eissn="",
        area=area,
        area_en="",
        level=level,
        taxonomy_scope=area,
        official_scope="",
        official_scope_url="",
        official_scope_status="",
        official_scope_confidence="",
        curated_scope_id=scope_id,
        curated_scope=scope,
        curated_topics_zh=scope,
        curated_topics_en="",
        curated_topic_tags=topic_tags,
        curated_article_types="original_research" if reviewed else "",
        curated_accepts_original_research="yes" if reviewed else "",
        curated_submission_mode="open" if reviewed else "",
        curated_scope_context="main_track" if reviewed else "",
        curated_scope_year="2026" if reviewed else "",
        curated_out_of_scope="",
        curated_scope_basis="official_cfp" if reviewed else "",
        curated_scope_status="approved" if reviewed else "",
        curated_secondary_source_urls="https://example.com/cfp" if reviewed else "",
        curated_target_status="active_target" if reviewed else "",
        top="",
        impact_factor="",
    )


class PropertyGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.data_dir = Path(cls.temporary.name) / "data"
        cls.data_dir.mkdir()
        for name in SOURCE_FILE_NAMES:
            (cls.data_dir / name).write_text(f"fixture:{name}\n", encoding="utf-8")

        wireless_ccf = make_record(
            0,
            dataset="ccf",
            level="A",
            name="Wireless Systems Conference",
            abbreviation="WSC",
            area="无线网络",
            topic_tags="wireless_mobile;network_arch_protocols",
            scope_id="scope-wireless",
            scope="无线网络与网络协议",
        )
        wireless_th = make_record(
            1,
            dataset="th_cpl",
            level="A",
            name="Wireless Systems Conference",
            abbreviation="WSC",
            area="Computer Networks",
        )
        edge = make_record(
            2,
            dataset="ccf",
            level="A",
            name="Edge Systems Conference",
            abbreviation="ESC",
            area="边缘系统",
            topic_tags="network_arch_protocols;edge_networking",
            scope_id="scope-edge",
            scope="边缘网络与网络协议",
        )
        machine_learning = make_record(
            3,
            dataset="ccf",
            level="B",
            name="Learning Conference",
            abbreviation="LC",
            area="机器学习",
            topic_tags="machine_learning",
            scope_id="scope-ml",
            scope="机器学习",
        )
        cls.records = [wireless_ccf, wireless_th, edge, machine_learning]
        cls.groups = [[wireless_ccf, wireless_th], [edge], [machine_learning]]
        cls.graph_path = cls.data_dir / "venue_graph.json.gz"
        cls.digest = graph_source_digest(cls.data_dir)
        build_graph(
            cls.graph_path,
            cls.data_dir,
            cls.records,
            cls.groups,
            tokenize=tokenize,
            normalize_alias=normalize_name,
            display_name_for_group=lambda group: VenueCandidate(
                list(group), list(group)
            ).name,
            matching_document_for_group=lambda group: VenueCandidate(
                list(group), list(group)
            ).matching_document(True),
            expected_digest=cls.digest,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_graph_is_fresh_and_hard_ranking_traversal_restores_groups(self) -> None:
        self.assertTrue(
            inspect_graph(
                self.graph_path, self.data_dir, expected_digest=self.digest
            ).fresh
        )
        with VenueGraphIndex(self.graph_path) as graph:
            graph.validate()
            groups = graph.load_groups_for_targets([("ccf", "A")])
            summary = graph.graph_summary()
        self.assertEqual([entity_id for entity_id, _rows in groups], [0, 2])
        self.assertEqual(len(groups[0][1]), 2)
        self.assertIn("ACCEPTS_TOPIC", summary["relation_types"])
        self.assertIn("RANKED_IN", summary["relation_types"])

    def test_related_topic_path_expands_a_fuzzy_theme(self) -> None:
        candidates = build_candidates_from_groups(
            self.groups, parse_targets(["CCF-A"])
        )
        with VenueGraphIndex(self.graph_path) as graph:
            ranked = rank_candidates_indexed(candidates, "手机信号时好时坏", graph)
        edge = next(candidate for candidate in ranked if candidate.abbreviation == "ESC")
        self.assertGreater(edge.graph_relevance or 0.0, 0.0)
        self.assertIn("RELATED_TOPIC", edge.graph_path)
        self.assertIn("knowledge_graph_path", edge.matched_fields)

    def test_file_vectors_recall_semantics_and_create_no_sqlite_database(self) -> None:
        provider = FakeEmbeddingProvider()
        cache_path = self.data_dir / ".embedding_cache.json.gz"
        result = build_graph_vector_index(
            self.graph_path,
            provider,
            cache_path,
            force=True,
        )
        self.assertEqual(result.entity_count, 3)
        query_vector = provider.embed(["untethered connectivity"])[0]
        with VenueGraphIndex(self.graph_path) as graph:
            recall = graph.vector_recall(
                allowed_entity_ids=[0, 2],
                query_vector=query_vector,
                provider_fingerprint=provider.fingerprint,
                min_similarity=0.35,
            )
            with self.assertRaises(GraphIndexError):
                graph.vector_recall(
                    allowed_entity_ids=[0, 2],
                    query_vector=query_vector,
                    provider_fingerprint="wrong-provider",
                )
        self.assertEqual(recall.entity_ids[0], 0)
        self.assertTrue(cache_path.exists())
        self.assertTrue(vector_path_for_graph(self.graph_path).exists())
        self.assertFalse(any(self.data_dir.glob("*.sqlite*")))

    def test_lightrag_custom_kg_export_is_referentially_complete(self) -> None:
        output = self.data_dir / "lightrag_custom_kg.json"
        counts = export_lightrag_custom_kg(self.graph_path, output)
        payload = json.loads(output.read_text(encoding="utf-8"))
        names = {item["entity_name"] for item in payload["entities"]}
        self.assertEqual(len(names), len(payload["entities"]))
        self.assertEqual(counts["entities"], len(payload["entities"]))
        self.assertTrue(payload["relationships"])
        for relation in payload["relationships"]:
            self.assertIn(relation["src_id"], names)
            self.assertIn(relation["tgt_id"], names)

    def test_source_change_invalidates_graph_and_vectors(self) -> None:
        source = self.data_dir / SOURCE_FILE_NAMES[0]
        original = source.read_bytes()
        try:
            source.write_bytes(original + b"changed\n")
            freshness = inspect_graph(self.graph_path, self.data_dir)
            self.assertFalse(freshness.fresh)
            self.assertEqual(freshness.reason, "source_changed")
        finally:
            source.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
