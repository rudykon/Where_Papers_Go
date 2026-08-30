from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Callable, Iterator
import unittest
import urllib.error
from unittest import mock

from where_paper_go.api_assistant import (
    ApiAssistantError,
    OpenAICompatibleQueryAssistant,
)
from where_paper_go.embeddings import (
    EmbeddingConfig,
    EmbeddingError,
    OpenAICompatibleEmbeddingProvider,
)
from where_paper_go.enrichment import http_request, http_stream_request
from where_paper_go.external_call_budget import (
    BUDGET_ENV,
    LEDGER_ENV,
    RUN_ID_ENV,
    ExternalCallBudgetExceeded,
    external_call_ledger_status,
    initialize_external_call_ledger,
)


Response = tuple[int, dict[str, str], bytes]
Responder = Callable[[str, str, dict[str, str], bytes], Response]


class _RecordingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, responder: Responder):
        super().__init__(("127.0.0.1", 0), _RecordingHandler)
        self.responder = responder
        self.requests: list[dict[str, Any]] = []


class _RecordingHandler(BaseHTTPRequestHandler):
    server: _RecordingHTTPServer

    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        self._respond()

    def _respond(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        headers = {str(key).casefold(): str(value) for key, value in self.headers.items()}
        self.server.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": headers,
                "body": body,
            }
        )
        status, response_headers, response_body = self.server.responder(
            self.command, self.path, headers, body
        )
        self.send_response(status)
        for key, value in response_headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        if response_body:
            self.wfile.write(response_body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def _server(responder: Responder) -> Iterator[tuple[str, _RecordingHTTPServer]]:
    server = _RecordingHTTPServer(responder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class ExternalCallRedirectTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            probe = _RecordingHTTPServer(
                lambda _method, _path, _headers, _body: (200, {}, b"")
            )
        except PermissionError:
            self.skipTest("sandbox forbids loopback sockets; run on the host/CI")
        else:
            probe.server_close()
        self.temporary = tempfile.TemporaryDirectory()
        self.sequence = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @contextmanager
    def budget(
        self,
        attempts: int,
        *,
        environment: dict[str, str] | None = None,
    ) -> Iterator[Path]:
        self.sequence += 1
        ledger = Path(self.temporary.name) / f"ledger-{self.sequence}.jsonl"
        run_id = f"redirect-test-{self.sequence}"
        initialize_external_call_ledger(ledger, budget=attempts, run_id=run_id)
        values = {
            LEDGER_ENV: str(ledger),
            BUDGET_ENV: str(attempts),
            RUN_ID_ENV: run_id,
            "NO_PROXY": "*",
            "no_proxy": "*",
            **(environment or {}),
        }
        with mock.patch.dict(os.environ, values, clear=True):
            yield ledger

    def test_every_3xx_is_one_request_and_one_reservation(self) -> None:
        def respond(
            _method: str, path: str, _headers: dict[str, str], _body: bytes
        ) -> Response:
            if path == "/target":
                return 200, {"Content-Type": "text/plain"}, b"followed"
            code = int(path.rsplit("/", 1)[-1])
            return code, {"Location": "/target"}, b"redirect"

        with _server(respond) as (base_url, server), self.budget(100) as ledger:
            for code in range(300, 400):
                with self.subTest(code=code):
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        http_request(
                            f"{base_url}/status/{code}",
                            proxy=None,
                            external_call_kind="http",
                        )
                    self.assertEqual(raised.exception.code, code)
                    raised.exception.close()

        self.assertEqual(len(server.requests), 100)
        self.assertNotIn("/target", [request["path"] for request in server.requests])
        self.assertEqual(external_call_ledger_status(ledger)["used"], 100)

    def test_cross_origin_redirect_never_receives_authorization(self) -> None:
        def target_response(
            _method: str, _path: str, _headers: dict[str, str], _body: bytes
        ) -> Response:
            return 200, {}, b"target"

        with _server(target_response) as (target_url, target):
            def source_response(
                _method: str, _path: str, _headers: dict[str, str], _body: bytes
            ) -> Response:
                return 302, {"Location": f"{target_url}/capture"}, b""

            with _server(source_response) as (source_url, source), self.budget(1) as ledger:
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    http_request(
                        f"{source_url}/start",
                        headers={"Authorization": "Bearer source-secret"},
                        proxy=None,
                        external_call_kind="llm",
                    )
                self.assertEqual(raised.exception.code, 302)
                raised.exception.close()

        self.assertEqual(len(source.requests), 1)
        self.assertEqual(
            source.requests[0]["headers"].get("authorization"),
            "Bearer source-secret",
        )
        self.assertEqual(target.requests, [])
        self.assertEqual(external_call_ledger_status(ledger)["used"], 1)

    def test_redirect_loop_stops_after_first_request(self) -> None:
        def respond(
            _method: str, _path: str, _headers: dict[str, str], _body: bytes
        ) -> Response:
            return 301, {"Location": "/loop"}, b""

        with _server(respond) as (base_url, server), self.budget(1) as ledger:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                http_request(
                    f"{base_url}/loop",
                    proxy=None,
                    external_call_kind="search",
                )
            self.assertEqual(raised.exception.code, 301)
            raised.exception.close()

        self.assertEqual(len(server.requests), 1)
        self.assertEqual(external_call_ledger_status(ledger)["used"], 1)

    def test_stream_redirect_is_not_followed(self) -> None:
        def respond(
            _method: str, _path: str, _headers: dict[str, str], _body: bytes
        ) -> Response:
            return 307, {"Location": "/stream-target"}, b""

        with _server(respond) as (base_url, server), self.budget(1) as ledger:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                http_stream_request(
                    f"{base_url}/stream",
                    headers={"Authorization": "Bearer stream-secret"},
                    body=b"{}",
                    proxy=None,
                    external_call_kind="llm",
                )
            self.assertEqual(raised.exception.code, 307)
            raised.exception.close()

        self.assertEqual(len(server.requests), 1)
        self.assertEqual(server.requests[0]["method"], "POST")
        self.assertEqual(external_call_ledger_status(ledger)["used"], 1)

    def test_embedding_redirect_is_not_followed_or_retried(self) -> None:
        def respond(
            _method: str, _path: str, _headers: dict[str, str], _body: bytes
        ) -> Response:
            return 303, {"Location": "/embedding-target"}, b""

        with _server(respond) as (base_url, server), self.budget(1) as ledger:
            provider = OpenAICompatibleEmbeddingProvider(
                EmbeddingConfig(
                    provider="openai_compatible",
                    base_url=base_url,
                    api_key="embedding-secret",
                    model="test-model",
                    endpoint=f"{base_url}/embeddings",
                    dimensions=2,
                    send_dimensions=True,
                    timeout=2,
                    batch_size=1,
                    max_chars=100,
                    max_retries=0,
                    headers={},
                    extra_body={},
                )
            )
            with self.assertRaises(EmbeddingError):
                provider.embed(["blind query"])

        self.assertEqual(len(server.requests), 1)
        self.assertEqual(server.requests[0]["method"], "POST")
        self.assertEqual(
            server.requests[0]["headers"].get("authorization"),
            "Bearer embedding-secret",
        )
        self.assertEqual(external_call_ledger_status(ledger)["used"], 1)

    def test_api_assistant_redirect_is_not_followed_or_cached(self) -> None:
        def respond(
            _method: str, _path: str, _headers: dict[str, str], _body: bytes
        ) -> Response:
            return 308, {"Location": "/chat-target"}, b""

        cache_dir = Path(self.temporary.name) / "api-cache"
        with _server(respond) as (base_url, server), self.budget(1) as ledger:
            assistant = OpenAICompatibleQueryAssistant(
                {
                    "llm": {
                        "base_url": base_url,
                        "chat_completions_url": f"{base_url}/chat",
                        "api_key": "llm-secret",
                        "model": "test-model",
                        "timeout": 2,
                        "max_retries": 0,
                    }
                },
                cache_dir=cache_dir,
            )
            with self.assertRaises(ApiAssistantError):
                assistant.plan_query("local redirect test", {})

        self.assertEqual(len(server.requests), 1)
        self.assertEqual(server.requests[0]["method"], "POST")
        self.assertEqual(server.requests[0]["headers"].get("authorization"), "Bearer llm-secret")
        self.assertEqual(external_call_ledger_status(ledger)["used"], 1)
        self.assertEqual(list(cache_dir.rglob("*.json")), [])

    def test_explicit_and_environment_proxy_paths_cannot_follow(self) -> None:
        def target_response(
            _method: str, _path: str, _headers: dict[str, str], _body: bytes
        ) -> Response:
            return 200, {}, b"target"

        with _server(target_response) as (target_url, target):
            def proxy_response(
                _method: str, _path: str, _headers: dict[str, str], _body: bytes
            ) -> Response:
                return 302, {"Location": f"{target_url}/proxy-target"}, b""

            with _server(proxy_response) as (proxy_url, proxy):
                direct_proxy_environment = {"NO_PROXY": "", "no_proxy": ""}
                with self.budget(
                    1, environment=direct_proxy_environment
                ) as explicit_ledger:
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        http_request(
                            "http://origin.invalid/explicit",
                            headers={"Authorization": "Bearer proxy-secret"},
                            proxy=proxy_url,
                            external_call_kind="http",
                        )
                    raised.exception.close()

                proxy_environment = {
                    "http_proxy": proxy_url,
                    "HTTP_PROXY": proxy_url,
                    "NO_PROXY": "",
                    "no_proxy": "",
                }
                with self.budget(1, environment=proxy_environment) as environment_ledger:
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        http_request(
                            "http://origin.invalid/environment",
                            headers={"Authorization": "Bearer proxy-secret"},
                            external_call_kind="http",
                        )
                    raised.exception.close()

        self.assertEqual(len(proxy.requests), 2)
        self.assertEqual(target.requests, [])
        self.assertEqual(external_call_ledger_status(explicit_ledger)["used"], 1)
        self.assertEqual(external_call_ledger_status(environment_ledger)["used"], 1)

    def test_exhausted_budget_performs_zero_loopback_requests(self) -> None:
        def respond(
            _method: str, _path: str, _headers: dict[str, str], _body: bytes
        ) -> Response:
            return 200, {}, b"unexpected"

        with _server(respond) as (base_url, server), self.budget(1) as ledger:
            status, _headers, content = http_request(
                f"{base_url}/spend",
                proxy=None,
                external_call_kind="http",
            )
            self.assertEqual((status, content), (200, b"unexpected"))
            with self.assertRaises(ExternalCallBudgetExceeded):
                http_request(
                    f"{base_url}/blocked",
                    proxy=None,
                    external_call_kind="http",
                )

        self.assertEqual([request["path"] for request in server.requests], ["/spend"])
        self.assertEqual(external_call_ledger_status(ledger)["used"], 1)

    def test_unbudgeted_web_get_preserves_normal_redirect_behavior(self) -> None:
        def respond(
            _method: str, path: str, _headers: dict[str, str], _body: bytes
        ) -> Response:
            if path == "/start":
                return 302, {"Location": "/target"}, b""
            return 200, {"Content-Type": "text/plain"}, b"normal"

        with _server(respond) as (base_url, server), mock.patch.dict(
            os.environ,
            {"NO_PROXY": "*", "no_proxy": "*"},
            clear=True,
        ):
            status, _headers, content = http_request(f"{base_url}/start", proxy=None)

        self.assertEqual(status, 200)
        self.assertEqual(content, b"normal")
        self.assertEqual(
            [request["path"] for request in server.requests],
            ["/start", "/target"],
        )

    def test_low_level_transports_close_http_error_before_reraising(self) -> None:
        for transport in (http_request, http_stream_request):
            with self.subTest(transport=transport.__name__):
                error = urllib.error.HTTPError(
                    "http://example.invalid/start",
                    302,
                    "redirect",
                    {},
                    None,
                )
                error.close = mock.Mock()
                with (
                    mock.patch(
                        "where_paper_go.enrichment.prepare_external_call_urlopen",
                        return_value=mock.Mock(side_effect=error),
                    ),
                    self.assertRaises(urllib.error.HTTPError) as raised,
                ):
                    transport("http://example.invalid/start", proxy=None)
                self.assertIs(raised.exception, error)
                error.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
