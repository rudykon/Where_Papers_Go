from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from where_paper_go.recommender import (
    VenueCandidate,
    VenueRecord,
    build_candidates_from_groups,
    normalize_name,
    parse_targets,
    rank_candidates_indexed,
    tokenize,
)
from where_paper_go.search_index import (
    SOURCE_FILE_NAMES,
    SearchIndexError,
    VenueSearchIndex,
    build_index,
    inspect_index,
    source_digest,
)
from where_paper_go.embeddings import (
    EmbeddingConfigurationError,
    build_vector_index,
    load_embedding_config,
)


class FakeEmbeddingProvider:
    model = "fake-multilingual-v1"
    fingerprint = "fake-provider-fingerprint"
    batch_size = 2

    def prepare_text(self, text: str) -> str:
        return " ".join(text.split())

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            normalized = text.casefold()
            if any(term in normalized for term in ("无线", "wireless", "untethered")):
                vectors.append([1, 1, 1, 1, -1, -1, -1, -1])
            elif any(term in normalized for term in ("机器学习", "machine learning")):
                vectors.append([1, -1, 1, -1, 1, -1, 1, -1])
            else:
                vectors.append([1, 1, -1, -1, 1, 1, -1, -1])
        return vectors


def make_record(
    row_id: int,
    *,
    dataset: str,
    level: str,
    name: str,
    abbreviation: str,
    area: str,
    curated_scope_id: str = "",
    curated_scope: str = "",
    curated_topics_zh: str = "",
    curated_topics_en: str = "",
    curated_topic_tags: str = "",
    official_scope: str = "",
    official_scope_status: str = "",
) -> VenueRecord:
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
        official_scope=official_scope,
        official_scope_url="https://example.com/scope" if official_scope else "",
        official_scope_status=official_scope_status,
        official_scope_confidence="high" if official_scope else "",
        curated_scope_id=curated_scope_id,
        curated_scope=curated_scope,
        curated_topics_zh=curated_topics_zh,
        curated_topics_en=curated_topics_en,
        curated_topic_tags=curated_topic_tags,
        curated_article_types="original_research" if curated_scope_id else "",
        curated_accepts_original_research="yes" if curated_scope_id else "",
        curated_submission_mode="open" if curated_scope_id else "",
        curated_scope_context="main_track" if curated_scope_id else "",
        curated_scope_year="2026" if curated_scope_id else "",
        curated_out_of_scope="缺少本方向研究贡献的工作不匹配。" if curated_scope_id else "",
        curated_scope_basis="official_cfp" if curated_scope_id else "",
        curated_scope_status="approved" if curated_scope_id else "",
        curated_secondary_source_urls="",
        curated_target_status="active_target" if curated_scope_id else "",
        top="",
        impact_factor="",
    )


class PersistentSearchIndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.data_dir = Path(cls.temporary.name) / "data"
        cls.data_dir.mkdir()
        for name in SOURCE_FILE_NAMES:
            (cls.data_dir / name).write_text(f"fixture:{name}\n", encoding="utf-8")

        infocom_ccf = make_record(
            0,
            dataset="ccf",
            level="A",
            name="IEEE International Conference on Computer Communications",
            abbreviation="INFOCOM",
            area="计算机网络",
            curated_scope_id="scope-infocom",
            curated_scope="无线网络、边缘计算与资源分配。",
            curated_topics_zh="无线网络；边缘计算；资源分配",
            curated_topics_en="wireless network;edge computing;resource allocation",
            curated_topic_tags="wireless_mobile;resource_allocation_scheduling",
        )
        infocom_th = make_record(
            1,
            dataset="th_cpl",
            level="A",
            name="IEEE International Conference on Computer Communications",
            abbreviation="INFOCOM",
            area="Computer Networks",
        )
        icml = make_record(
            2,
            dataset="ccf",
            level="A",
            name="International Conference on Machine Learning",
            abbreviation="ICML",
            area="人工智能",
            curated_scope_id="scope-icml",
            curated_scope="机器学习的理论、算法与系统。",
            curated_topics_zh="机器学习；深度学习",
            curated_topics_en="machine learning;deep learning;C++ systems",
            curated_topic_tags="machine_learning;deep_representation",
            official_scope="量子通信专题候选文本",
            official_scope_status="ok",
        )
        cls.records = [infocom_ccf, infocom_th, icml]
        cls.groups = [[infocom_ccf, infocom_th], [icml]]
        cls.index_path = cls.data_dir / "venue_index.sqlite3"
        cls.digest = source_digest(cls.data_dir)
        build_index(
            cls.index_path,
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

    def test_index_is_fresh_and_loads_complete_entity_groups(self) -> None:
        freshness = inspect_index(
            self.index_path,
            self.data_dir,
            expected_digest=self.digest,
        )
        self.assertTrue(freshness.fresh)
        with VenueSearchIndex(self.index_path) as index:
            groups = index.load_groups_for_targets([("ccf", "A")])
        self.assertEqual(len(groups), 2)
        infocom = next(group for entity_id, group in groups if entity_id == 0)
        self.assertEqual(len(infocom), 2)
        self.assertEqual({row["dataset"] for row in infocom}, {"ccf", "th_cpl"})

    def test_fts_and_controlled_topics_recall_then_preserve_ranking(self) -> None:
        candidates = build_candidates_from_groups(
            self.groups,
            parse_targets(["CCF-A", "THCPL-A"]),
        )
        with VenueSearchIndex(self.index_path) as index:
            ranked = rank_candidates_indexed(
                candidates,
                "无线网络中的资源分配",
                index,
            )
        self.assertEqual(ranked[0].abbreviation, "INFOCOM")
        self.assertIn("fts5_bm25_recall", ranked[0].matched_fields)
        self.assertEqual(
            ranked[0].matched_ranking_labels,
            ["CCF-A（2026）", "TH-CPL-A（2026）"],
        )

    def test_technical_tokens_and_official_scope_opt_in(self) -> None:
        with VenueSearchIndex(self.index_path) as index:
            cplusplus = index.recall(
                allowed_entity_ids=[0, 2],
                query_tokens=tokenize("C++"),
                topic_tags=[],
                include_official_scope=False,
            )
            without_official = index.recall(
                allowed_entity_ids=[0, 2],
                query_tokens=tokenize("量子通信"),
                topic_tags=[],
                include_official_scope=False,
            )
            with_official = index.recall(
                allowed_entity_ids=[0, 2],
                query_tokens=tokenize("量子通信"),
                topic_tags=[],
                include_official_scope=True,
            )
        self.assertEqual(cplusplus.entity_ids, [2])
        self.assertNotIn(2, without_official.entity_ids)
        self.assertIn(2, with_official.entity_ids)

    def test_source_change_marks_index_stale(self) -> None:
        path = self.data_dir / SOURCE_FILE_NAMES[0]
        original = path.read_bytes()
        try:
            path.write_bytes(original + b"changed\n")
            freshness = inspect_index(self.index_path, self.data_dir)
            self.assertFalse(freshness.fresh)
            self.assertEqual(freshness.reason, "source_changed")
        finally:
            path.write_bytes(original)

    def test_vector_recall_finds_semantic_match_without_lexical_overlap(self) -> None:
        provider = FakeEmbeddingProvider()
        result = build_vector_index(
            self.index_path,
            provider,
            self.data_dir / ".embedding_cache.sqlite3",
        )
        self.assertEqual(result.entity_count, 2)
        self.assertEqual(result.dimensions, 8)

        # Keep this phrase outside the curated concept rules so the match is
        # attributable to vector semantics alone.
        query = "untethered connectivity planning"
        query_vector = provider.embed([query])[0]
        candidates = build_candidates_from_groups(
            self.groups,
            parse_targets(["CCF-A", "THCPL-A"]),
        )
        with VenueSearchIndex(self.index_path) as index:
            lexical_only = rank_candidates_indexed(candidates, query, index)
            with patch.object(
                index,
                "_replace_vector_shortlist",
                side_effect=AssertionError("default vector recall must be exact"),
            ):
                recalled = index.vector_recall(
                    allowed_entity_ids=[0, 2],
                    query_vector=query_vector,
                    provider_fingerprint=provider.fingerprint,
                    min_similarity=0.35,
                )
            approximate = index.vector_recall(
                allowed_entity_ids=[0, 2],
                query_vector=query_vector,
                provider_fingerprint=provider.fingerprint,
                min_similarity=0.35,
                approximate=True,
            )
            ranked = rank_candidates_indexed(
                candidates,
                query,
                index,
                query_vector=query_vector,
                vector_provider_fingerprint=provider.fingerprint,
                vector_min_similarity=0.35,
            )

        self.assertEqual(lexical_only, [])
        self.assertEqual(recalled.entity_ids, [0])
        self.assertEqual(approximate.entity_ids, [0])
        self.assertGreater(recalled.similarities[0], 0.99)
        self.assertEqual(ranked[0].abbreviation, "INFOCOM")
        self.assertIn("semantic_vector", ranked[0].matched_fields)
        self.assertGreater(ranked[0].semantic_similarity or 0.0, 0.99)

        with VenueSearchIndex(self.index_path) as index:
            with self.assertRaises(SearchIndexError):
                index.vector_recall(
                    allowed_entity_ids=[0, 2],
                    query_vector=[1.0, 0.0],
                    provider_fingerprint=provider.fingerprint,
                )

    def test_api_topic_tag_can_recall_without_rule_or_lexical_overlap(self) -> None:
        candidates = build_candidates_from_groups(
            self.groups, parse_targets(["CCF-A", "THCPL-A"])
        )
        with VenueSearchIndex(self.index_path) as index:
            ranked = rank_candidates_indexed(
                candidates,
                "untethered connectivity planning",
                index,
                additional_query_concepts=[
                    ("wireless_mobile", "无线与移动网络")
                ],
            )
        self.assertTrue(ranked)
        self.assertEqual(ranked[0].abbreviation, "INFOCOM")
        self.assertIn("无线与移动网络", ranked[0].matched_concepts)


class EmbeddingConfigurationTests(unittest.TestCase):
    def test_requires_independent_embedding_model_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "api.json"
            config_path.write_text(
                json.dumps(
                    {
                        "llm": {
                            "base_url": "https://example.com/v1",
                            "api_key": "secret",
                            "model": "chat-only",
                        }
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(EmbeddingConfigurationError):
                load_embedding_config(config_path)

            config_path.write_text(
                json.dumps(
                    {
                        "embedding": {
                            "base_url": "https://example.com/v1",
                            "api_key": "secret",
                            "model": "embedding-v1",
                            "dimensions": 8,
                        }
                    }
                ),
                encoding="utf-8",
            )
            config = load_embedding_config(config_path)
            self.assertEqual(config.endpoint, "https://example.com/v1/embeddings")
            self.assertEqual(config.model, "embedding-v1")
            self.assertTrue(config.send_dimensions)
            self.assertNotIn("secret", config.fingerprint)

            payload = json.loads(config_path.read_text(encoding="utf-8"))
            payload["embedding"]["send_dimensions"] = False
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertFalse(load_embedding_config(config_path).send_dimensions)


if __name__ == "__main__":
    unittest.main()
