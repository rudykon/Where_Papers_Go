from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import urllib.error
from unittest import TestCase
from unittest.mock import patch

from where_paper_go.enrichment import search_web


class LLMNativeSearchTests(TestCase):
    def _config(self) -> dict[str, object]:
        return {
            "provider": "llm_native",
            "_llm_config": {
                "base_url": "https://llm.example/v1",
                "model": "search-model",
                "api_key": "test",
            },
        }

    def test_model_generated_urls_without_annotations_are_rejected(self) -> None:
        response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "results": [
                                    {
                                        "title": "placeholder",
                                        "url": "https://example.org/1234567",
                                    }
                                ]
                            }
                        ),
                        "annotations": None,
                    }
                }
            ]
        }
        with TemporaryDirectory() as directory, patch(
            "where_paper_go.enrichment.http_request",
            return_value=(200, {}, json.dumps(response).encode()),
        ):
            results = search_web(
                "wireless link adaptation",
                self._config(),
                Path(directory),
                10,
                5,
            )
        self.assertEqual(results, [])

    def test_provider_url_annotation_is_accepted(self) -> None:
        response = {
            "choices": [
                {
                    "message": {
                        "content": "{}",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url_citation": {
                                    "title": "Official wireless scope",
                                    "url": "https://www.example.edu/scope",
                                    "snippet": "link adaptation",
                                },
                            }
                        ],
                    }
                }
            ]
        }
        with TemporaryDirectory() as directory, patch(
            "where_paper_go.enrichment.http_request",
            return_value=(200, {}, json.dumps(response).encode()),
        ):
            results = search_web(
                "wireless link adaptation",
                self._config(),
                Path(directory),
                10,
                5,
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://www.example.edu/scope")


class TavilySearchTests(TestCase):
    def test_uses_bearer_auth_and_quality_search_defaults(self) -> None:
        response = {
            "results": [
                {
                    "title": "Official wireless scope",
                    "url": "https://www.example.edu/scope",
                    "content": "link adaptation and wireless networks",
                }
            ]
        }
        config = {
            "provider": "tavily",
            "api_key": "tvly-test-key",
            "endpoint": "https://api.tavily.com/search",
            "search_depth": "advanced",
            "max_results": 8,
        }
        with TemporaryDirectory() as directory, patch(
            "where_paper_go.enrichment.http_request",
            return_value=(200, {}, json.dumps(response).encode()),
        ) as request:
            config["key_pool_state_file"] = str(Path(directory) / "pool.json")
            results = search_web(
                "wireless link adaptation",
                config,
                Path(directory),
                10,
                5,
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://www.example.edu/scope")
        call = request.call_args.kwargs
        self.assertEqual(call["headers"]["Authorization"], "Bearer tvly-test-key")
        payload = json.loads(call["body"].decode())
        self.assertNotIn("api_key", payload)
        self.assertEqual(payload["search_depth"], "advanced")
        self.assertEqual(payload["max_results"], 5)

    def test_missing_key_is_reported(self) -> None:
        with TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                search_web(
                    "wireless link adaptation",
                    {"provider": "tavily"},
                    Path(directory),
                    10,
                    5,
                    raise_on_error=True,
                )

    def test_proxy_failure_retries_tavily_direct(self) -> None:
        response = {
            "results": [
                {
                    "title": "Direct result",
                    "url": "https://www.example.edu/scope",
                    "content": "scope evidence",
                }
            ]
        }
        config = {
            "provider": "tavily",
            "api_key": "tvly-test-key",
            "direct_fallback": True,
        }
        with TemporaryDirectory() as directory, patch(
            "where_paper_go.enrichment.http_request",
            side_effect=[
                urllib.error.URLError("proxy unavailable"),
                (200, {}, json.dumps(response).encode()),
            ],
        ) as request:
            config["key_pool_state_file"] = str(Path(directory) / "pool.json")
            results = search_web(
                "wireless link adaptation",
                config,
                Path(directory),
                10,
                5,
            )
            state = json.loads((Path(directory) / "pool.json").read_text(encoding="utf-8"))
        self.assertEqual(len(results), 1)
        self.assertEqual(request.call_count, 2)
        self.assertNotIn("proxy", request.call_args_list[0].kwargs)
        self.assertIsNone(request.call_args_list[1].kwargs["proxy"])
        self.assertEqual(sum(item["used"] for item in state["keys"].values()), 2)

    def test_tavily_falls_back_to_second_api_key(self) -> None:
        response = {
            "results": [
                {
                    "title": "Backup result",
                    "url": "https://www.example.edu/backup-scope",
                    "content": "scope evidence",
                }
            ]
        }
        config = {
            "provider": "tavily",
            "api_key": "tvly-primary-key",
            "api_key2": "tvly-backup-key",
            "proxy": "direct",
        }
        first_error = urllib.error.HTTPError(
            "https://api.tavily.com/search", 432, "plan limit", {}, None
        )
        with TemporaryDirectory() as directory, patch(
            "where_paper_go.enrichment.http_request",
            side_effect=[first_error, (200, {}, json.dumps(response).encode())],
        ) as request:
            config["key_pool_state_file"] = str(Path(directory) / "pool.json")
            results = search_web(
                "wireless link adaptation",
                config,
                Path(directory),
                10,
                5,
            )
            state = json.loads((Path(directory) / "pool.json").read_text(encoding="utf-8"))
        first_error.close()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://www.example.edu/backup-scope")
        self.assertEqual(
            request.call_args_list[0].kwargs["headers"]["Authorization"],
            "Bearer tvly-primary-key",
        )
        self.assertEqual(
            request.call_args_list[1].kwargs["headers"]["Authorization"],
            "Bearer tvly-backup-key",
        )
        self.assertIn("exhausted", {item["status"] for item in state["keys"].values()})
        self.assertNotIn("tvly-primary-key", json.dumps(state))
        self.assertNotIn("tvly-backup-key", json.dumps(state))

    def test_tavily_empty_success_uses_one_key_and_is_cached(self) -> None:
        config = {
            "provider": "tavily",
            "api_keys": ["tvly-primary-key", "tvly-backup-key"],
            "proxy": "direct",
        }
        with TemporaryDirectory() as directory, patch(
            "where_paper_go.enrichment.http_request",
            return_value=(200, {}, json.dumps({"results": []}).encode()),
        ) as request:
            config["key_pool_state_file"] = str(Path(directory) / "pool.json")
            first = search_web(
                "wireless link adaptation",
                config,
                Path(directory),
                10,
                5,
            )
            second = search_web(
                "wireless link adaptation",
                config,
                Path(directory),
                10,
                5,
            )
            state = json.loads((Path(directory) / "pool.json").read_text(encoding="utf-8"))
        self.assertEqual(first, [])
        self.assertEqual(second, [])
        self.assertEqual(request.call_count, 1)
        self.assertEqual(sum(item["used"] for item in state["keys"].values()), 1)

    def test_tavily_sequential_queries_rotate_api_keys(self) -> None:
        response = {
            "results": [
                {
                    "title": "Official scope",
                    "url": "https://www.example.edu/scope",
                    "content": "scope evidence",
                }
            ]
        }
        config = {
            "provider": "tavily",
            "api_keys": ["tvly-key-a", "tvly-key-b"],
            "proxy": "direct",
        }
        with TemporaryDirectory() as directory, patch(
            "where_paper_go.enrichment.http_request",
            return_value=(200, {}, json.dumps(response).encode()),
        ) as request:
            config["key_pool_state_file"] = str(Path(directory) / "pool.json")
            search_web("query one", config, Path(directory), 10, 5)
            search_web("query two", config, Path(directory), 10, 5)

        authorizations = [
            call.kwargs["headers"]["Authorization"] for call in request.call_args_list
        ]
        self.assertEqual(authorizations, ["Bearer tvly-key-a", "Bearer tvly-key-b"])
