from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from where_paper_go import recommender
from where_paper_go import worker


class WorkerCacheBindingTests(unittest.TestCase):
    def test_default_effective_embedding_caches_are_absolute_and_injected(
        self,
    ) -> None:
        with patch.dict("os.environ", {}, clear=True):
            bindings = worker._worker_cache_bindings()
        self.assertTrue(bindings.query_embedding_cache.is_absolute())
        self.assertTrue(bindings.lightrag_embedding_cache.is_absolute())
        self.assertTrue(bindings.api_cache_dir.is_absolute())
        self.assertTrue(bindings.lightrag_working_dir.is_absolute())
        self.assertTrue(bindings.graph_path.is_absolute())
        bound = worker._bind_cache_argv(["--target", "CCF-A"], bindings)
        api_index = bound.index("--api-cache-dir")
        query_index = bound.index("--query-embedding-cache")
        lightrag_index = bound.index("--lightrag-embedding-cache")
        working_index = bound.index("--lightrag-working-dir")
        graph_index = bound.index("--graph")
        self.assertEqual(
            Path(bound[query_index + 1]), bindings.query_embedding_cache
        )
        self.assertEqual(
            Path(bound[lightrag_index + 1]), bindings.lightrag_embedding_cache
        )
        self.assertEqual(Path(bound[api_index + 1]), bindings.api_cache_dir)
        self.assertEqual(
            Path(bound[working_index + 1]), bindings.lightrag_working_dir
        )
        self.assertEqual(Path(bound[graph_index + 1]), bindings.graph_path)

    def test_environment_bindings_are_injected_into_every_query_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api_cache = root / "api"
            query_cache = root / "query.json.gz"
            lightrag_cache = root / "lightrag.json.gz"
            lightrag_working_dir = root / "lightrag-workspace"
            graph_path = root / "venue_graph.json.gz"
            environment = {
                recommender.API_CACHE_DIR_ENV: str(api_cache),
                recommender.QUERY_EMBEDDING_CACHE_ENV: str(query_cache),
                recommender.LIGHTRAG_EMBEDDING_CACHE_ENV: str(lightrag_cache),
                worker.LIGHTRAG_WORKING_DIR_ENV: str(lightrag_working_dir),
                worker.GRAPH_PATH_ENV: str(graph_path),
            }
            with patch.dict("os.environ", environment, clear=False):
                bindings = worker._worker_cache_bindings()

            original = ["--target", "CCF-A", "--query", "wireless systems"]
            bound = worker._bind_cache_argv(original, bindings)

            self.assertEqual(
                bound[-10:],
                [
                    "--api-cache-dir",
                    str(api_cache.resolve()),
                    "--query-embedding-cache",
                    str(query_cache.resolve()),
                    "--lightrag-embedding-cache",
                    str(lightrag_cache.resolve()),
                    "--lightrag-working-dir",
                    str(lightrag_working_dir.resolve()),
                    "--graph",
                    str(graph_path.resolve()),
                ],
            )
            self.assertEqual(
                original, ["--target", "CCF-A", "--query", "wireless systems"]
            )

    def test_query_cannot_change_the_preloaded_lightrag_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bindings = worker.WorkerCacheBindings(
                api_cache_dir=root / "api",
                query_embedding_cache=root / "query.json.gz",
                lightrag_embedding_cache=root / "preloaded.json.gz",
                lightrag_working_dir=root / "rag",
                graph_path=root / "venue_graph.json.gz",
            )
            with self.assertRaisesRegex(ValueError, "startup binding"):
                worker._bind_cache_argv(
                    [
                        "--target",
                        "CCF-A",
                        "--lightrag-embedding-cache",
                        str(root / "other.json.gz"),
                    ],
                    bindings,
                )

    def test_worker_rejects_shared_query_and_lightrag_write_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            shared = Path(temporary) / "shared.json.gz"
            environment = {
                recommender.QUERY_EMBEDDING_CACHE_ENV: str(shared),
                recommender.LIGHTRAG_EMBEDDING_CACHE_ENV: str(shared),
            }
            with (
                patch.dict("os.environ", environment, clear=False),
                self.assertRaisesRegex(ValueError, "different files"),
            ):
                worker._worker_cache_bindings()

    def test_only_lightrag_environment_cannot_collide_with_query_default(
        self,
    ) -> None:
        default_query = worker.default_query_embedding_cache_path(
            worker.DATA_DIR
        ).resolve()
        environment = {
            recommender.LIGHTRAG_EMBEDDING_CACHE_ENV: str(default_query),
        }
        with (
            patch.dict("os.environ", environment, clear=True),
            self.assertRaisesRegex(ValueError, "different files"),
        ):
            worker._worker_cache_bindings()

    def test_only_query_environment_cannot_collide_with_lightrag_default(
        self,
    ) -> None:
        default_lightrag = worker.default_graph_embedding_cache_path(
            worker.DATA_DIR
        ).resolve()
        environment = {
            recommender.QUERY_EMBEDDING_CACHE_ENV: str(default_lightrag),
        }
        with (
            patch.dict("os.environ", environment, clear=True),
            self.assertRaisesRegex(ValueError, "different files"),
        ):
            worker._worker_cache_bindings()

    def test_embedding_environment_cannot_collide_with_default_api_namespace(
        self,
    ) -> None:
        default_api = (worker.DATA_DIR / ".query_api_cache").resolve()
        for name in (
            recommender.QUERY_EMBEDDING_CACHE_ENV,
            recommender.LIGHTRAG_EMBEDDING_CACHE_ENV,
        ):
            with self.subTest(name=name), patch.dict(
                "os.environ", {name: str(default_api / "shared.json.gz")}, clear=True
            ), self.assertRaisesRegex(ValueError, "must not contain"):
                worker._worker_cache_bindings()

    def test_preload_and_query_use_the_same_explicit_lightrag_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph_path = root / "venue_graph.json.gz"
            lightrag_cache = root / "lightrag.json.gz"
            bindings = worker.WorkerCacheBindings(
                api_cache_dir=root / "api",
                query_embedding_cache=root / "query.json.gz",
                lightrag_embedding_cache=lightrag_cache,
                lightrag_working_dir=root / "rag",
                graph_path=graph_path,
            )

            class FakeGraph:
                def preload_vectors(self) -> None:
                    return None

            with (
                patch.object(worker, "default_graph_path", return_value=graph_path),
                patch.object(
                    recommender,
                    "open_persistent_graph",
                    return_value=(FakeGraph(), False, ""),
                ),
                patch.object(worker.lightrag, "preload_persistent_runtime") as preload,
            ):
                worker._preload(bindings)

            self.assertEqual(preload.call_args.args[3], lightrag_cache)
            self.assertEqual(preload.call_args.args[0], root / "rag")

            captured: list[str] = []

            def fake_main(argv, *, event_callback=None):
                del event_callback
                captured.extend(argv)
                return 0

            with patch.object(recommender, "main", side_effect=fake_main):
                result = worker._run_search(
                    ["--target", "CCF-A"], cache_bindings=bindings
                )
            self.assertEqual(result["returncode"], 0)
            option_index = captured.index("--lightrag-embedding-cache")
            self.assertEqual(captured[option_index + 1], str(lightrag_cache))
            working_index = captured.index("--lightrag-working-dir")
            self.assertEqual(captured[working_index + 1], str(root / "rag"))

    def test_lightrag_working_dir_cannot_overlap_write_caches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {
                worker.LIGHTRAG_WORKING_DIR_ENV: str(root / "rag"),
                recommender.QUERY_EMBEDDING_CACHE_ENV: str(root / "rag" / "query.gz"),
            }
            with (
                patch.dict("os.environ", environment, clear=True),
                self.assertRaisesRegex(ValueError, "working directory"),
            ):
                worker._worker_cache_bindings()

    def test_required_runtime_shadow_rejects_protected_data_defaults(self) -> None:
        with patch.dict(
            "os.environ", {worker.REQUIRE_RUNTIME_SHADOW_ENV: "1"}, clear=True
        ), self.assertRaisesRegex(ValueError, "outside protected sources"):
            worker._worker_cache_bindings()

    def test_required_runtime_shadow_accepts_disjoint_private_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {
                worker.REQUIRE_RUNTIME_SHADOW_ENV: "1",
                worker.STRICT_GRAPH_READ_ONLY_ENV: "1",
                worker.RUNTIME_GENERATION_ENV: str(root),
                recommender.API_CACHE_DIR_ENV: str(root / "api"),
                recommender.QUERY_EMBEDDING_CACHE_ENV: str(root / "query.gz"),
                recommender.LIGHTRAG_EMBEDDING_CACHE_ENV: str(root / "lightrag.gz"),
                worker.LIGHTRAG_WORKING_DIR_ENV: str(root / "lightrag"),
                worker.GRAPH_PATH_ENV: str(worker.DATA_DIR / "venue_graph.json.gz"),
            }
            with patch.dict("os.environ", environment, clear=True):
                bindings = worker._worker_cache_bindings()
            self.assertEqual(bindings.api_cache_dir, (root / "api").resolve())

    def test_required_runtime_shadow_rejects_write_path_outside_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generation = root / "generation"
            generation.mkdir()
            generation.chmod(0o700)
            environment = {
                worker.REQUIRE_RUNTIME_SHADOW_ENV: "1",
                worker.STRICT_GRAPH_READ_ONLY_ENV: "1",
                worker.RUNTIME_GENERATION_ENV: str(generation),
                recommender.API_CACHE_DIR_ENV: str(generation / "api"),
                recommender.QUERY_EMBEDDING_CACHE_ENV: str(root / "escaped-query.gz"),
                recommender.LIGHTRAG_EMBEDDING_CACHE_ENV: str(
                    generation / "lightrag.gz"
                ),
                worker.LIGHTRAG_WORKING_DIR_ENV: str(generation / "lightrag"),
                worker.GRAPH_PATH_ENV: str(worker.DATA_DIR / "venue_graph.json.gz"),
            }
            with (
                patch.dict("os.environ", environment, clear=True),
                self.assertRaisesRegex(ValueError, "belong to one generation"),
            ):
                worker._worker_cache_bindings()


if __name__ == "__main__":
    unittest.main()
