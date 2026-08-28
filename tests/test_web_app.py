from __future__ import annotations

from http import HTTPStatus
import json
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
            with patch.object(web_app, "DEFAULT_CONFIG", config_path):
                payload = web_app._config_status()

        self.assertTrue(payload["search_key_configured"])
        self.assertEqual(payload["search_key_count"], 2)
        self.assertEqual(payload["search_total_quota"], 2000)
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("tvly-test-a", serialized)
        self.assertNotIn("tvly-test-b", serialized)

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
        ready_line = json.dumps({"ready": True, "preload_ms": 7})
        stamp = [("graph", 1, 1)]
        with (
            patch.object(web_app, "_result_dependency_stamp", return_value=stamp),
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
