from __future__ import annotations

import asyncio
import json
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

from where_paper_go import lightrag as venue_lightrag
from where_paper_go.lightrag import (
    LightRAGRuntimeError,
    recall_from_lightrag_data,
    validate_lightrag_workspace,
)


class LightRAGRecallTests(unittest.TestCase):
    def test_custom_kg_import_keeps_event_loop_awake_and_finalizes(self) -> None:
        heartbeat_seen = []

        class FakeRag:
            async def initialize_storages(self):
                return None

            async def ainsert_custom_kg(self, _custom_kg):
                heartbeat_seen.extend(
                    task.get_name() == "lightrag-import-heartbeat"
                    for task in asyncio.all_tasks()
                )

            async def finalize_storages(self):
                return None

        class FakeAdapter:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        adapter = FakeAdapter()
        components = (
            FakeRag(),
            SimpleNamespace(fingerprint="provider"),
            SimpleNamespace(model="llm"),
            adapter,
        )
        with patch.object(
            venue_lightrag, "_runtime_components", return_value=components
        ):
            result = asyncio.run(
                venue_lightrag._import_async(
                    {"chunks": [], "entities": [], "relationships": []},
                    Path("workspace"),
                    None,
                    None,
                )
            )

        self.assertEqual(result, ("provider", "llm"))
        self.assertIn(True, heartbeat_seen)
        self.assertTrue(adapter.closed)

    def test_structured_mix_result_maps_only_allowed_venue_ids(self) -> None:
        recall = recall_from_lightrag_data(
            {
                "entities": [
                    {"entity_name": "VENUE::17::MobiCom", "description": "wireless"},
                    {"entity_name": "TOPIC::wireless_mobile", "description": "topic"},
                    {"entity_name": "VENUE::99::Outside", "description": "outside"},
                ],
                "relationships": [
                    {
                        "src_id": "VENUE::23::SIGCOMM",
                        "tgt_id": "TOPIC::network_arch_protocols",
                        "description": "accepts topic",
                    }
                ],
                "chunks": [{"content": "VENUE::17::MobiCom mobile systems"}],
            },
            allowed_entity_ids=[17, 23],
        )
        self.assertEqual(recall.entity_ids, (17, 23))
        self.assertNotIn(99, recall.scores)
        self.assertIn("entity_vector", recall.channels[17])
        self.assertIn("chunk_vector", recall.channels[17])
        self.assertIn("relationship", recall.channels[23])
        self.assertGreater(recall.scores[17], 0)

    def test_workspace_is_fail_closed_when_manifest_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(LightRAGRuntimeError, "尚未构建"):
                validate_lightrag_workspace(
                    Path(temporary_directory), Path("graph.json.gz"), "fingerprint"
                )

    def test_workspace_manifest_binds_graph_and_embedding(self) -> None:
        class FakeGraph:
            def __init__(self, _path):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def metadata(self):
                return {"source_digest": "source", "semantic_digest": "semantic"}

        with tempfile.TemporaryDirectory() as temporary_directory:
            working_dir = Path(temporary_directory)
            manifest = {
                "manifest_schema": venue_lightrag.MANIFEST_SCHEMA_VERSION,
                "source_digest": "source",
                "semantic_digest": "semantic",
                "embedding_provider_fingerprint": "fingerprint",
                "query_mode": "mix",
            }
            (working_dir / venue_lightrag.MANIFEST_FILE).write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            for name in (
                "graph_chunk_entity_relation.graphml",
                "vdb_entities.json",
                "vdb_relationships.json",
                "vdb_chunks.json",
            ):
                (working_dir / name).write_text("{}", encoding="utf-8")
            with patch.object(venue_lightrag, "VenueGraphIndex", FakeGraph):
                self.assertEqual(
                    validate_lightrag_workspace(
                        working_dir, Path("graph.json.gz"), "fingerprint"
                    )["query_mode"],
                    "mix",
                )
                with self.assertRaisesRegex(LightRAGRuntimeError, "模型不一致"):
                    validate_lightrag_workspace(
                        working_dir, Path("graph.json.gz"), "different"
                    )

    def test_real_lightrag_mix_import_and_query_use_local_file_stores(self) -> None:
        class FakeProvider:
            fingerprint = "fake-provider"
            model = "fake-embedding"
            batch_size = 8

            def __init__(self, _config):
                pass

            def prepare_text(self, text):
                return " ".join(str(text).split())

            def embed(self, texts):
                vectors = []
                for text in texts:
                    seed = sum(ord(character) for character in text)
                    vector = [float(((seed + index * 17) % 23) + 1) for index in range(8)]
                    vectors.append(vector)
                return vectors

        class FakeGraph:
            def __init__(self, _path):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def validate(self):
                return None

            def metadata(self):
                return {"source_digest": "source", "semantic_digest": "semantic"}

            def to_lightrag_custom_kg(self):
                return {
                    "chunks": [
                        {
                            "content": "mobile wireless networking systems",
                            "source_id": "graph-node:venue:7",
                            "file_path": "graph",
                        }
                    ],
                    "entities": [
                        {
                            "entity_name": "VENUE::7::MobiCom",
                            "entity_type": "venue",
                            "description": "mobile wireless networking systems",
                            "source_id": "graph-node:venue:7",
                            "file_path": "graph",
                        },
                        {
                            "entity_name": "TOPIC::wireless_mobile",
                            "entity_type": "topic",
                            "description": "wireless mobile",
                            "source_id": "graph-node:venue:7",
                            "file_path": "graph",
                        },
                    ],
                    "relationships": [
                        {
                            "src_id": "VENUE::7::MobiCom",
                            "tgt_id": "TOPIC::wireless_mobile",
                            "description": "accepts topic",
                            "keywords": "accepts topic",
                            "weight": 1.0,
                            "source_id": "graph-node:venue:7",
                            "file_path": "graph",
                        }
                    ],
                }

        embedding_config = SimpleNamespace(
            dimensions=8,
            max_chars=1024,
            model="fake-embedding",
            batch_size=8,
        )
        api_config = {
            "llm": {
                "base_url": "https://example.invalid/v1",
                "model": "fake-llm",
                "timeout": 1,
            },
            "search": {"provider": "fake"},
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            working_dir = root / "lightrag"
            graph_path = root / "graph.json.gz"
            cache_path = root / "embedding-cache.json.gz"
            with (
                patch.object(venue_lightrag, "VenueGraphIndex", FakeGraph),
                patch.object(
                    venue_lightrag,
                    "load_embedding_config",
                    return_value=embedding_config,
                ),
                patch.object(
                    venue_lightrag,
                    "load_api_assistant_config",
                    return_value=api_config,
                ),
                patch.object(
                    venue_lightrag,
                    "OpenAICompatibleEmbeddingProvider",
                    FakeProvider,
                ),
            ):
                manifest = venue_lightrag.import_lightrag_graph(
                    graph_path, working_dir, embedding_cache=cache_path
                )
                recall = venue_lightrag.query_lightrag(
                    "mobile wireless",
                    working_dir,
                    graph_path,
                    embedding_cache=cache_path,
                    high_level_keywords=["wireless networks"],
                    low_level_keywords=["mobile", "wireless"],
                    allowed_entity_ids=[7],
                    top_k=10,
                    chunk_top_k=10,
                )
            self.assertEqual(manifest["query_mode"], "mix")
            self.assertEqual(recall.entity_ids, (7,))
            self.assertTrue((working_dir / "vdb_entities.json").exists())
            self.assertTrue((working_dir / "graph_chunk_entity_relation.graphml").exists())


if __name__ == "__main__":
    unittest.main()
