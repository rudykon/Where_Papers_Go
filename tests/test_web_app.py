from __future__ import annotations

from http import HTTPStatus
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from where_paper_go import web_app


class WebAppTests(TestCase):
    def test_health_reports_built_runtime_without_secrets(self) -> None:
        payload = web_app._health_payload()
        self.assertIn(payload["status"], {"ready", "incomplete"})
        self.assertEqual(payload["lightrag"]["mode"], "mix")
        self.assertEqual(payload["lightrag"]["embedding_model"], "bge-m3")
        self.assertEqual(payload["lightrag"]["dimensions"], 1024)
        self.assertNotIn("api_key", json.dumps(payload, ensure_ascii=False))

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
