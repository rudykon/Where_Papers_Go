from __future__ import annotations

from http import HTTPStatus
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from where_paper_go import web_app


class WebAppTests(TestCase):
    def test_frontend_declares_brand_assets(self) -> None:
        favicon_path = web_app.WEB_DIR / "favicon.png"
        brand_path = web_app.WEB_DIR / "brand-mark.png"
        html = (web_app.WEB_DIR / "index.html").read_text(encoding="utf-8")

        self.assertTrue(favicon_path.is_file())
        self.assertTrue(brand_path.is_file())
        self.assertIn('rel="icon" href="/favicon.png"', html)
        self.assertIn('class="brand-mark" src="/favicon.png"', html)
        self.assertEqual(web_app.mimetypes.guess_type(favicon_path.name)[0], "image/png")
        self.assertEqual(favicon_path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(brand_path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_frontend_discards_preliminary_results_after_terminal_failure(self) -> None:
        javascript = (web_app.WEB_DIR / "app.js").read_text(encoding="utf-8")

        self.assertIn('state.payload = null;', javascript)
        self.assertIn('state.resultStatus = "error";', javascript)
        self.assertNotIn("partial_error", javascript)
        self.assertNotIn("preliminary_failed", javascript)

    def test_health_reports_built_runtime_without_secrets(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            (data_dir / "venue_graph.json.gz").touch()
            (data_dir / "venue_graph_vectors.json.gz").touch()
            lightrag_dir = data_dir / "lightrag_storage"
            lightrag_dir.mkdir()
            (lightrag_dir / "venue_import_manifest.json").write_text(
                json.dumps(
                    {
                        "query_mode": "mix",
                        "embedding_model": "bge-m3",
                        "embedding_dimensions": 1024,
                        "counts": {"venues": 1},
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(web_app, "DATA_DIR", data_dir),
                patch.object(
                    web_app,
                    "_runtime_status",
                    return_value={
                        "persistent_worker": True,
                        "process_ready": True,
                        "bindings_current": True,
                        "ready": True,
                        "preload_ms": 1,
                    },
                ),
                patch.object(web_app, "_config_status", return_value={"ready": True}),
            ):
                payload = web_app._health_payload()

        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["lightrag"]["mode"], "mix")
        self.assertEqual(payload["lightrag"]["embedding_model"], "bge-m3")
        self.assertEqual(payload["lightrag"]["dimensions"], 1024)
        self.assertTrue(payload["checks"]["bindings_current"])
        self.assertNotIn(str(data_dir), json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("api_key", json.dumps(payload, ensure_ascii=False))

    def test_health_fails_closed_when_worker_dies_or_bindings_change(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            (data_dir / "venue_graph.json.gz").touch()
            (data_dir / "venue_graph_vectors.json.gz").touch()
            lightrag_dir = data_dir / "lightrag_storage"
            lightrag_dir.mkdir()
            (lightrag_dir / "venue_import_manifest.json").write_text(
                json.dumps({"query_mode": "mix", "counts": {}}),
                encoding="utf-8",
            )
            with (
                patch.object(web_app, "DATA_DIR", data_dir),
                patch.object(web_app, "_config_status", return_value={"ready": True}),
                patch.object(
                    web_app,
                    "_runtime_status",
                    return_value={
                        "persistent_worker": True,
                        "process_ready": False,
                        "bindings_current": False,
                        "ready": False,
                        "preload_ms": 1,
                    },
                ),
            ):
                payload = web_app._health_payload()

        self.assertFalse(payload["ready"])
        self.assertEqual(payload["status"], "incomplete")
        self.assertFalse(payload["checks"]["worker"])
        self.assertFalse(payload["checks"]["bindings_current"])

    def test_config_status_recognizes_tavily_key_pool_without_exposing_keys(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "llmapi.json"
            config_path.write_text(
                json.dumps(
                    {
                        "search": {
                            "provider": "tavily",
                            "api_keys": ["tvly-test-a", "tvly-test-b"],
                            "quota_per_key": 1000,
                        }
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(web_app, "DEFAULT_CONFIG", config_path),
                patch.dict(
                    os.environ,
                    {
                        web_app.TAVILY_STATE_FILE_ENV: str(
                            Path(temp_dir) / "missing-state.json"
                        )
                    },
                    clear=False,
                ),
            ):
                payload = web_app._config_status()

        self.assertTrue(payload["search_key_configured"])
        self.assertEqual(payload["search_key_count"], 2)
        self.assertEqual(payload["search_total_quota"], 2000)
        self.assertFalse(payload["search_quota_audit"]["ready"])
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("tvly-test-a", serialized)
        self.assertNotIn("tvly-test-b", serialized)

    def test_config_status_requires_read_only_replicated_tavily_audit(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "llmapi.json"
            state_file = root / "shared" / "pool.json"
            config = {
                "llm": {
                    "model": "synthetic-llm",
                    "base_url": "https://invalid.example.test/v1",
                },
                "embedding": {
                    "model": "synthetic-embedding",
                    "base_url": "https://invalid.example.test/v1",
                },
                "search": {
                    "provider": "tavily",
                    "api_keys": ["tvly-synthetic-a", "tvly-synthetic-b"],
                    "quota_per_key": 7,
                },
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with patch.dict(
                os.environ,
                {web_app.TAVILY_STATE_FILE_ENV: str(state_file)},
                clear=False,
            ):
                web_app.TavilyKeyPool.from_config(config["search"]).summary()
                primary_before = state_file.read_bytes()
                backup_before = state_file.with_name(state_file.name + ".bak").read_bytes()
                with patch.object(web_app, "DEFAULT_CONFIG", config_path):
                    payload = web_app._config_status()
                    backup_file = state_file.with_name(state_file.name + ".bak")
                    newer_backup = json.loads(backup_file.read_text(encoding="utf-8"))
                    newer_backup["state_revision"] += 1
                    backup_file.write_text(json.dumps(newer_backup), encoding="utf-8")
                    degraded_backup_before = backup_file.read_bytes()
                    degraded = web_app._config_status()

            audit = payload["search_quota_audit"]
            self.assertTrue(payload["ready"])
            self.assertTrue(audit["ready"])
            self.assertTrue(audit["configuration_current"])
            self.assertTrue(audit["replicated_revision"])
            self.assertEqual(
                audit["copies"]["primary"]["revision"], audit["state_revision"]
            )
            self.assertEqual(state_file.read_bytes(), primary_before)
            self.assertEqual(
                audit["copies"]["backup"]["sha256"],
                hashlib.sha256(backup_before).hexdigest(),
            )
            serialized = json.dumps(payload, sort_keys=True)
            self.assertNotIn("tvly-synthetic-a", serialized)
            self.assertNotIn(str(state_file), serialized)
            self.assertFalse(degraded["ready"])
            self.assertFalse(degraded["search_quota_audit"]["ready"])
            self.assertFalse(
                degraded["search_quota_audit"]["replicated_revision"]
            )
            self.assertEqual(backup_file.read_bytes(), degraded_backup_before)

    def test_options_expose_targets_and_record_types(self) -> None:
        payload = web_app._options_payload()
        values = {item["value"] for item in payload["targets"]}
        self.assertIn("CCF-A", values)
        self.assertIn("JCR-Q1", values)
        self.assertEqual(
            {item["value"] for item in payload["record_types"]},
            {"all", "conference", "journal"},
        )
        self.assertGreater(payload["counts"]["records"], 0)

    def test_runtime_status_binds_every_write_path_and_immutable_manifest(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "protected-data"
            data_dir.mkdir()
            generation = root / "runtime" / "generations" / "generation-test"
            generation.mkdir(parents=True)
            os.chmod(generation, 0o700)
            manifest_path = generation / web_app.RUNTIME_MANIFEST_FILE
            manifest_payload = (
                json.dumps(
                    {
                        "schema_version": 1,
                        "artifact_type": "where_papers_go_runtime_shadow",
                        "source_data_dir": str(data_dir.resolve()),
                        "source_binding_sha256": "1" * 64,
                        "files": [],
                        "write_boundary": "runtime_generation_only",
                        "protected_sources_never_replaced": True,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
            manifest_path.write_bytes(manifest_payload)
            os.chmod(manifest_path, 0o400)
            api_cache = generation / "api_cache"
            environment = {
                "WPG_REQUIRE_RUNTIME_SHADOW": "1",
                "WPG_STRICT_GRAPH_READ_ONLY": "1",
                web_app.RUNTIME_GENERATION_ENV: str(generation),
                web_app.RUNTIME_MANIFEST_ENV: str(manifest_path),
                web_app.RUNTIME_MANIFEST_SHA256_ENV: hashlib.sha256(
                    manifest_payload
                ).hexdigest(),
                web_app.recommender.API_CACHE_DIR_ENV: str(api_cache),
                "WPG_RESULT_CACHE_DIR": str(api_cache / "result"),
                web_app.recommender.QUERY_EMBEDDING_CACHE_ENV: str(
                    generation / "query.json.gz"
                ),
                web_app.recommender.LIGHTRAG_EMBEDDING_CACHE_ENV: str(
                    generation / "lightrag.json.gz"
                ),
                "WPG_LIGHTRAG_WORKING_DIR": str(generation / "lightrag_storage"),
                "WPG_GRAPH_PATH": str(data_dir / "venue_graph.json.gz"),
                web_app.TAVILY_STATE_FILE_ENV: str(
                    root / "runtime" / "shared" / "tavily-state.json"
                ),
            }
            with (
                patch.dict(os.environ, environment, clear=True),
                patch.object(web_app, "DATA_DIR", data_dir),
            ):
                expected = web_app._expected_worker_bindings()
                search_runtime = Mock(
                    process_ready=True,
                    bindings_current=True,
                    ready=True,
                    preload_ms=1,
                    runtime_bindings=expected,
                )
                with patch.object(web_app, "_SEARCH_RUNTIME", search_runtime):
                    status = web_app._runtime_status()

            self.assertTrue(status["ready"])
            self.assertTrue(status["write_isolated"])
            self.assertTrue(status["tavily_state_shared"])
            self.assertTrue(status["runtime_manifest"]["ready"])
            self.assertTrue(status["runtime_manifest"]["sha256_matched"])
            self.assertTrue(status["worker_bindings"]["exact_match"])
            self.assertTrue(
                all(
                    binding["generation_bound"]
                    and binding["outside_protected_sources"]
                    for binding in status["write_bindings"].values()
                )
            )
            serialized = json.dumps(status, sort_keys=True)
            self.assertNotIn(str(generation), serialized)
            self.assertNotIn(str(data_dir), serialized)

    def test_runtime_status_fails_closed_on_unbound_result_or_manifest_hash(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            data_dir.mkdir()
            generation = root / "generation"
            generation.mkdir()
            os.chmod(generation, 0o700)
            manifest = generation / web_app.RUNTIME_MANIFEST_FILE
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "artifact_type": "where_papers_go_runtime_shadow",
                        "source_data_dir": str(data_dir.resolve()),
                        "source_binding_sha256": "1" * 64,
                        "files": [],
                        "write_boundary": "runtime_generation_only",
                        "protected_sources_never_replaced": True,
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(manifest, 0o400)
            environment = {
                "WPG_REQUIRE_RUNTIME_SHADOW": "1",
                "WPG_STRICT_GRAPH_READ_ONLY": "1",
                web_app.RUNTIME_GENERATION_ENV: str(generation),
                web_app.RUNTIME_MANIFEST_ENV: str(manifest),
                web_app.RUNTIME_MANIFEST_SHA256_ENV: "0" * 64,
                web_app.recommender.API_CACHE_DIR_ENV: str(generation / "api"),
                "WPG_RESULT_CACHE_DIR": str(root / "escaped-result"),
                web_app.recommender.QUERY_EMBEDDING_CACHE_ENV: str(
                    generation / "query.gz"
                ),
                web_app.recommender.LIGHTRAG_EMBEDDING_CACHE_ENV: str(
                    generation / "lightrag.gz"
                ),
                "WPG_LIGHTRAG_WORKING_DIR": str(generation / "lightrag"),
                "WPG_GRAPH_PATH": str(data_dir / "graph.json.gz"),
                web_app.TAVILY_STATE_FILE_ENV: str(root / "shared" / "state.json"),
            }
            with (
                patch.dict(os.environ, environment, clear=True),
                patch.object(web_app, "DATA_DIR", data_dir),
            ):
                expected = web_app._expected_worker_bindings()
                search_runtime = Mock(
                    process_ready=True,
                    bindings_current=True,
                    ready=True,
                    preload_ms=1,
                    runtime_bindings=expected,
                )
                with patch.object(web_app, "_SEARCH_RUNTIME", search_runtime):
                    status = web_app._runtime_status()

            self.assertFalse(status["write_isolated"])
            self.assertFalse(status["runtime_manifest"]["sha256_matched"])
            self.assertFalse(status["runtime_manifest"]["ready"])
            self.assertFalse(status["ready"])

    def test_search_command_keeps_one_backend_contract(self) -> None:
        command = web_app._search_command(
            {
                "query": "无线网络资源调度",
                "targets": ["CCF-A", "JCR-Q1"],
                "scopes": ["无线网络"],
                "limit": 7,
            }
        )
        self.assertIn("--api-config", command)
        self.assertEqual(command[:3], [web_app.sys.executable, "-m", "where_paper_go.recommender"])
        self.assertEqual(web_app._recommender_argv(command)[0], "--query")
        self.assertEqual(command[command.index("--limit") + 1], "7")
        self.assertEqual(command.count("--target"), 2)
        self.assertIn("--scope", command)
        self.assertIn("--match-official-scope", command)

    def test_search_api_failure_is_reported_as_service_unavailable(self) -> None:
        completed = __import__("subprocess").CompletedProcess(
            args=[],
            returncode=2,
            stdout="",
            stderr="venue_recommender.py: error: Search API 未提供可用网页证据",
        )
        with (
            patch.object(web_app, "_load_result_cache", return_value=None),
            patch.object(web_app._SEARCH_RUNTIME, "run", return_value=completed),
        ):
            status, payload = web_app._run_search({"query": "无线网络", "targets": ["CCF-A"]})
        self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertTrue(payload["retryable"])
        self.assertIn("Search API", payload["detail"])

    def test_worker_timeout_returns_no_payload_and_does_not_cache(self) -> None:
        timeout = __import__("subprocess").TimeoutExpired([], 900)
        with (
            patch.object(web_app, "_load_result_cache", return_value=None),
            patch.object(web_app, "_store_result_cache") as store,
            patch.object(web_app._SEARCH_RUNTIME, "run", side_effect=timeout),
        ):
            status, payload = web_app._run_search(
                {"query": "wireless systems", "targets": ["CCF-A"]}
            )

        self.assertEqual(status, HTTPStatus.GATEWAY_TIMEOUT)
        self.assertEqual(payload["error"], "检索超时")
        self.assertNotIn("results", payload)
        store.assert_not_called()

    def test_worker_crash_is_retryable_and_redacts_internal_credentials(self) -> None:
        with (
            patch.object(web_app, "_load_result_cache", return_value=None),
            patch.object(
                web_app._SEARCH_RUNTIME,
                "run",
                side_effect=RuntimeError(
                    "Authorization: Bearer super-secret-worker-token"
                ),
            ),
            patch.object(
                web_app,
                "configured_secret_values",
                return_value=("super-secret-worker-token",),
            ),
        ):
            status, payload = web_app._run_search(
                {"query": "wireless systems", "targets": ["CCF-A"]}
            )

        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertTrue(payload["retryable"])
        self.assertNotIn("super-secret-worker-token", serialized)
        self.assertIn("[REDACTED]", serialized)

    def test_binding_change_during_request_discards_result_and_never_caches(self) -> None:
        completed = __import__("subprocess").CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"results": [{"name": "stale result"}]}),
            stderr="",
        )
        old_stamp = [("graph", 1, 1, 1, 1, 10)]
        new_stamp = [("graph", 1, 2, 1, 2, 10)]
        with (
            patch.object(web_app, "_result_dependency_stamp", side_effect=[old_stamp, new_stamp]),
            patch.object(web_app, "_load_result_cache", return_value=None),
            patch.object(web_app._SEARCH_RUNTIME, "run", return_value=completed),
            patch.object(web_app, "_store_result_cache") as store,
        ):
            status, payload = web_app._run_search(
                {"query": "wireless systems", "targets": ["CCF-A"]}
            )

        self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertTrue(payload["retryable"])
        self.assertNotIn("results", payload)
        store.assert_not_called()

    def test_worker_discards_mid_request_response_after_binding_change(self) -> None:
        manager = web_app.RetrievalWorkerManager()
        process = Mock()
        process.poll.return_value = None
        process.stdin = Mock()
        process.wait.return_value = 0
        manager._process = process
        old_stamp = [("graph", 1, 1, 1, 1, 10)]
        new_stamp = [("graph", 1, 2, 1, 2, 10)]
        manager._dependency_stamp = old_stamp
        command = [
            web_app.sys.executable,
            "-m",
            "where_paper_go.recommender",
            "--query",
            "wireless systems",
        ]
        response = json.dumps(
            {"request_id": "placeholder", "returncode": 0, "stdout": "{}"}
        )

        def response_with_request_id(_timeout: float) -> str:
            request = json.loads(process.stdin.write.call_args.args[0])
            return json.dumps({**json.loads(response), "request_id": request["request_id"]})

        with (
            patch.object(manager, "start"),
            patch.object(manager, "_readline", side_effect=response_with_request_id),
            patch.object(
                web_app,
                "_result_dependency_stamp",
                side_effect=[old_stamp, new_stamp],
            ),
            self.assertRaisesRegex(RuntimeError, "通信失败"),
        ):
            manager._round_trip(command, 5)

        self.assertIsNone(manager._process)
        process.terminate.assert_called_once()
        manager.close()

    def test_dependency_stamp_detects_same_size_preserved_mtime_replacement(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            graph = data_dir / "venue_graph.json.gz"
            graph.write_bytes(b"first")
            original_mtime = graph.stat().st_mtime_ns
            with (
                patch.object(web_app, "DATA_DIR", data_dir),
                patch.object(web_app, "DEFAULT_CONFIG", data_dir / "missing-config"),
            ):
                before = web_app._result_dependency_stamp()
                replacement = data_dir / "replacement"
                replacement.write_bytes(b"other")
                __import__("os").utime(
                    replacement, ns=(original_mtime, original_mtime)
                )
                __import__("os").replace(replacement, graph)
                __import__("os").utime(graph, ns=(original_mtime, original_mtime))
                after = web_app._result_dependency_stamp()

        self.assertNotEqual(before, after)

    def test_dependency_stamp_covers_lightrag_storage_atomic_replacement(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            storage = data_dir / "lightrag_storage"
            storage.mkdir()
            entities = storage / "vdb_entities.json"
            entities.write_bytes(b"first")
            original_mtime = entities.stat().st_mtime_ns
            with (
                patch.object(web_app, "DATA_DIR", data_dir),
                patch.object(web_app, "DEFAULT_CONFIG", data_dir / "missing-config"),
            ):
                before = web_app._result_dependency_stamp()
                replacement = storage / "replacement"
                replacement.write_bytes(b"other")
                __import__("os").utime(
                    replacement, ns=(original_mtime, original_mtime)
                )
                __import__("os").replace(replacement, entities)
                __import__("os").utime(
                    entities, ns=(original_mtime, original_mtime)
                )
                after = web_app._result_dependency_stamp()

        self.assertNotEqual(before, after)

    def test_stale_dependency_stamp_makes_worker_unready(self) -> None:
        manager = web_app.RetrievalWorkerManager()

        class AliveProcess:
            def poll(self):
                return None

        manager._process = AliveProcess()
        manager._dependency_stamp = [("graph", 1, 1)]
        with patch.object(
            web_app,
            "_result_dependency_stamp",
            return_value=[("graph", 2, 1)],
        ):
            self.assertTrue(manager.process_ready)
            self.assertFalse(manager.bindings_current)
            self.assertFalse(manager.ready)
        manager._process = None
        manager.close()

    def test_dead_worker_is_reaped_and_restarted_from_fresh_bindings(self) -> None:
        manager = web_app.RetrievalWorkerManager()
        first = Mock()
        first.poll.return_value = None
        first.stdin = Mock()
        first.wait.return_value = 0
        second = Mock()
        second.poll.return_value = None
        second.stdin = Mock()
        second.wait.return_value = 0
        expected_bindings = {"graph_path": "/synthetic/graph"}
        ready_line = json.dumps(
            {
                "ready": True,
                "preload_ms": 7,
                "bindings": expected_bindings,
            }
        )
        stamp = [("graph", 1, 1)]
        with (
            patch.object(web_app, "_result_dependency_stamp", return_value=stamp),
            patch.object(
                web_app, "_expected_worker_bindings", return_value=expected_bindings
            ),
            patch.object(web_app.subprocess, "Popen", side_effect=[first, second]) as popen,
            patch.object(manager, "_readline", side_effect=[ready_line, ready_line]),
        ):
            manager.start()
            self.assertTrue(manager.ready)
            first.poll.return_value = 1
            manager.start()
            self.assertTrue(manager.ready)
            self.assertEqual(popen.call_count, 2)
            first.wait.assert_called()
        manager.close()

    def test_worker_ready_protocol_rejects_missing_extra_or_changed_binding(self) -> None:
        expected = {
            "graph_path": "/synthetic/graph",
            "lightrag_working_dir": "/synthetic/rag",
            "api_cache_dir": "/synthetic/api",
            "query_embedding_cache": "/synthetic/query",
            "lightrag_embedding_cache": "/synthetic/lightrag",
        }
        bad_bindings = (
            {key: value for key, value in expected.items() if key != "graph_path"},
            {**expected, "unexpected": "/synthetic/extra"},
            {**expected, "graph_path": "/synthetic/other-graph"},
        )
        for bindings in bad_bindings:
            with self.subTest(bindings=sorted(bindings)):
                manager = web_app.RetrievalWorkerManager()
                process = Mock()
                process.poll.return_value = None
                process.stdin = Mock()
                process.wait.return_value = 0
                ready_line = json.dumps(
                    {"ready": True, "preload_ms": 1, "bindings": bindings}
                )
                with (
                    patch.object(
                        web_app,
                        "_result_dependency_stamp",
                        return_value=[("graph", 1, 1)],
                    ),
                    patch.object(
                        web_app, "_expected_worker_bindings", return_value=expected
                    ),
                    patch.object(web_app.subprocess, "Popen", return_value=process),
                    patch.object(manager, "_readline", return_value=ready_line),
                    self.assertRaisesRegex(RuntimeError, "绑定与父进程不一致"),
                ):
                    manager.start()
                self.assertIsNone(manager._process)
                process.terminate.assert_called_once()
                manager.close()

    def test_invalid_worker_protocol_is_discarded_without_partial_result(self) -> None:
        manager = web_app.RetrievalWorkerManager()
        process = Mock()
        process.poll.return_value = None
        process.stdin = Mock()
        process.wait.return_value = 0
        with (
            patch.object(
                web_app,
                "_result_dependency_stamp",
                return_value=[("graph", 1, 1)],
            ),
            patch.object(web_app.subprocess, "Popen", return_value=process),
            patch.object(manager, "_readline", return_value="not-json\n"),
        ):
            with self.assertRaisesRegex(RuntimeError, "启动协议无效"):
                manager.start()
        self.assertIsNone(manager._process)
        process.terminate.assert_called_once()
        manager.close()

    def test_timed_out_worker_is_disposed_before_next_request(self) -> None:
        manager = web_app.RetrievalWorkerManager()
        process = Mock()
        process.poll.return_value = None
        process.stdin = Mock()
        process.wait.return_value = 0
        manager._process = process
        stamp = [("graph", 1, 1, 1, 1, 10)]
        manager._dependency_stamp = stamp
        command = [
            web_app.sys.executable,
            "-m",
            "where_paper_go.recommender",
            "--query",
            "wireless systems",
        ]
        timeout = __import__("subprocess").TimeoutExpired(command, 1)
        with (
            patch.object(manager, "start"),
            patch.object(manager, "_readline", side_effect=timeout),
            patch.object(web_app, "_result_dependency_stamp", return_value=stamp),
            self.assertRaises(__import__("subprocess").TimeoutExpired),
        ):
            manager._round_trip(command, 1)

        self.assertIsNone(manager._process)
        process.terminate.assert_called_once()
        manager.close()

    def test_stream_relays_progress_before_final_payload(self) -> None:
        completed = __import__("subprocess").CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {"total": 1, "displayed": 1, "results": [{"name": "MobiCom"}]},
                ensure_ascii=False,
            ),
            stderr="",
        )

        def fake_stream(_command, on_event, timeout=900):
            self.assertEqual(timeout, 900)
            on_event({"type": "progress", "stage": "llm", "status": "running"})
            on_event(
                {
                    "type": "results",
                    "phase": "preliminary",
                    "payload": {"total": 1, "displayed": 1, "results": []},
                }
            )
            return completed

        events = []
        with (
            patch.object(web_app, "_load_result_cache", return_value=None),
            patch.object(web_app, "_store_result_cache"),
            patch.object(web_app._SEARCH_RUNTIME, "stream", side_effect=fake_stream),
        ):
            web_app._run_search_stream(
                {"query": "无线网络", "targets": ["CCF-A"]}, events.append
            )

        self.assertEqual(
            [event["type"] for event in events],
            ["accepted", "progress", "results", "complete"],
        )
        self.assertEqual(events[-1]["payload"]["results"][0]["name"], "MobiCom")

    def test_stream_failure_has_no_complete_event_or_cached_partial_result(self) -> None:
        failed = __import__("subprocess").CompletedProcess(
            args=[],
            returncode=2,
            stdout="",
            stderr="Search API 未提供可用网页证据",
        )

        def fake_stream(_command, on_event, timeout=900):
            self.assertEqual(timeout, 900)
            on_event(
                {
                    "type": "results",
                    "phase": "preliminary",
                    "payload": {"results": [{"name": "Local-only candidate"}]},
                }
            )
            return failed

        events = []
        with (
            patch.object(web_app, "_load_result_cache", return_value=None),
            patch.object(web_app, "_store_result_cache") as store,
            patch.object(web_app._SEARCH_RUNTIME, "stream", side_effect=fake_stream),
        ):
            web_app._run_search_stream(
                {"query": "wireless systems", "targets": ["CCF-A"]},
                events.append,
            )

        self.assertEqual(
            [event["type"] for event in events],
            ["accepted", "results", "error"],
        )
        self.assertEqual(events[-1]["status"], HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertNotIn("complete", {event["type"] for event in events})
        store.assert_not_called()

    def test_complete_result_cache_hits_and_invalidates_with_dependencies(self) -> None:
        completed = __import__("subprocess").CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {"total": 1, "displayed": 1, "results": [{"name": "MobiCom"}]},
                ensure_ascii=False,
            ),
            stderr="",
        )
        dependency_version = [[("graph", 1, 100)]]
        body = {"query": "cache-specific wireless query", "targets": ["CCF-A"]}
        with TemporaryDirectory() as directory:
            with (
                patch.object(web_app, "RESULT_CACHE_DIR", Path(directory)),
                patch.object(
                    web_app,
                    "_result_dependency_stamp",
                    side_effect=lambda: dependency_version[0],
                ),
                patch.object(web_app._SEARCH_RUNTIME, "run", return_value=completed) as run,
            ):
                first_status, first = web_app._run_search(body)
                second_status, second = web_app._run_search(body)
                dependency_version[0] = [("graph", 2, 100)]
                third_status, third = web_app._run_search(body)

        self.assertEqual((first_status, second_status, third_status), (200, 200, 200))
        self.assertFalse(first["result_cache"]["hit"])
        self.assertTrue(second["result_cache"]["hit"])
        self.assertFalse(third["result_cache"]["hit"])
        self.assertEqual(run.call_count, 2)

    def test_stream_cache_hit_skips_worker(self) -> None:
        body = {"query": "cached stream query", "targets": ["CCF-A"]}
        completed = __import__("subprocess").CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"total": 0, "displayed": 0, "results": []}),
            stderr="",
        )
        with TemporaryDirectory() as directory:
            with (
                patch.object(web_app, "RESULT_CACHE_DIR", Path(directory)),
                patch.object(web_app, "_result_dependency_stamp", return_value=[("graph", 1, 1)]),
                patch.object(web_app._SEARCH_RUNTIME, "run", return_value=completed),
            ):
                status, _payload = web_app._run_search(body)
                self.assertEqual(status, 200)
                events = []
                with patch.object(web_app._SEARCH_RUNTIME, "stream") as stream:
                    web_app._run_search_stream(body, events.append)

        stream.assert_not_called()
        self.assertEqual([event["type"] for event in events], ["accepted", "complete"])
        self.assertTrue(events[-1]["payload"]["result_cache"]["hit"])
