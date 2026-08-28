from __future__ import annotations

import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from unittest import TestCase
from unittest.mock import patch
import urllib.error
import urllib.request

from where_paper_go import web_app
from where_paper_go.web_security import (
    SlidingWindowRateLimiter,
    WebSecurityConfig,
    audit_record,
    client_ip,
    configured_secret_values,
    redact_sensitive_text,
)


class WebSecurityTests(TestCase):
    def _serve(self, config: WebSecurityConfig):
        try:
            server = web_app.VenueHTTPServer(
                ("127.0.0.1", 0), web_app.VenueHandler, config
            )
        except PermissionError:
            self.skipTest("sandbox forbids loopback sockets; run on the host/CI")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def test_required_auth_without_private_token_file_fails_closed(self) -> None:
        with patch.dict(os.environ, {"WPG_REQUIRE_API_AUTH": "1"}, clear=True):
            with self.assertRaisesRegex(ValueError, "WPG_API_TOKEN_FILE"):
                WebSecurityConfig.from_environment()

    def test_private_bearer_token_is_constant_contract_and_never_serialized(self) -> None:
        with TemporaryDirectory() as directory:
            token_path = Path(directory) / "api.token"
            token = "a" * 40
            token_path.write_text(token + "\n", encoding="utf-8")
            token_path.chmod(0o600)
            with patch.dict(
                os.environ,
                {
                    "WPG_REQUIRE_API_AUTH": "1",
                    "WPG_API_TOKEN_FILE": str(token_path),
                },
                clear=True,
            ):
                config = WebSecurityConfig.from_environment()

        self.assertTrue(config.authorize("Bearer " + token))
        self.assertFalse(config.authorize("Bearer wrong"))
        self.assertFalse(config.authorize(None))
        self.assertNotIn(token, repr(config))

    def test_group_readable_api_token_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            token_path = Path(directory) / "api.token"
            token_path.write_text("b" * 40, encoding="utf-8")
            token_path.chmod(0o640)
            with patch.dict(
                os.environ,
                {"WPG_API_TOKEN_FILE": str(token_path)},
                clear=True,
            ):
                with self.assertRaisesRegex(ValueError, "group/other"):
                    WebSecurityConfig.from_environment()

    def test_sliding_window_rate_limit_has_deterministic_retry_after(self) -> None:
        limiter = SlidingWindowRateLimiter(2, 10)
        self.assertEqual(limiter.allow("client", now=100), (True, 0))
        self.assertEqual(limiter.allow("client", now=101), (True, 0))
        self.assertEqual(limiter.allow("client", now=102), (False, 8))
        self.assertEqual(limiter.allow("client", now=111), (True, 0))

    def test_forwarded_address_is_used_only_for_a_trusted_proxy(self) -> None:
        trusted = WebSecurityConfig(trust_proxy_headers=True)
        headers = {"X-Forwarded-For": "203.0.113.7, 127.0.0.1"}
        self.assertEqual(client_ip("127.0.0.1", headers, trusted), "203.0.113.7")
        self.assertEqual(client_ip("192.0.2.8", headers, trusted), "192.0.2.8")
        self.assertEqual(
            client_ip("127.0.0.1", headers, WebSecurityConfig()), "127.0.0.1"
        )

    def test_configured_and_labeled_secrets_are_redacted(self) -> None:
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "llmapi.json"
            config_path.write_text(
                json.dumps(
                    {
                        "llm": {"api_key": "llm-secret-value"},
                        "search": {"api_keys": ["search-secret-value"]},
                    }
                ),
                encoding="utf-8",
            )
            secrets = configured_secret_values(config_path)

        raw = (
            "Authorization: Bearer llm-secret-value "
            "api_key=search-secret-value https://user:password@example.org"
        )
        redacted = redact_sensitive_text(raw, secrets)
        self.assertNotIn("llm-secret-value", redacted)
        self.assertNotIn("search-secret-value", redacted)
        self.assertNotIn("user:password", redacted)
        self.assertGreaterEqual(redacted.count("[REDACTED]"), 2)

    def test_audit_record_contains_no_body_or_authorization(self) -> None:
        record = audit_record(
            request_id="abc",
            method="POST",
            path="/api/search",
            status=429,
            client_ip="127.0.0.1",
        )
        payload = json.loads(record)
        self.assertEqual(payload["event"], "http_request")
        self.assertNotIn("query", payload)
        self.assertNotIn("authorization", payload)

    def test_live_endpoint_has_security_headers_without_python_banner(self) -> None:
        audit_stream = io.StringIO()
        with patch.object(web_app.sys, "stderr", audit_stream):
            base_url = self._serve(WebSecurityConfig(audit_enabled=True))
            with urllib.request.urlopen(
                base_url + "/api/health/live", timeout=5
            ) as response:
                payload = json.load(response)
                headers = response.headers
            time.sleep(0.05)

        self.assertTrue(payload["alive"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn("Python", headers["Server"])
        audit_lines = [
            line for line in audit_stream.getvalue().splitlines() if "http_request" in line
        ]
        self.assertEqual(len(audit_lines), 1)
        self.assertNotIn('"status":0', audit_lines[0])

    def test_server_can_bind_without_listening_until_preload_is_ready(self) -> None:
        try:
            server = web_app.VenueHTTPServer(
                ("127.0.0.1", 0),
                web_app.VenueHandler,
                WebSecurityConfig(audit_enabled=False),
                bind_and_activate=False,
            )
        except PermissionError:
            self.skipTest("sandbox forbids loopback sockets; run on the host/CI")
        self.addCleanup(server.server_close)
        server.server_bind()
        self.assertEqual(server.socket.getsockopt(__import__("socket").SOL_SOCKET, __import__("socket").SO_ACCEPTCONN), 0)
        server.server_activate()
        self.assertEqual(server.socket.getsockopt(__import__("socket").SOL_SOCKET, __import__("socket").SO_ACCEPTCONN), 1)

    def test_http_search_auth_and_rate_limit_fail_before_worker_use(self) -> None:
        token_config = WebSecurityConfig(
            api_token="c" * 40,
            require_api_auth=True,
            audit_enabled=False,
        )
        base_url = self._serve(token_config)
        request = urllib.request.Request(
            base_url + "/api/search",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        try:
            self.assertEqual(caught.exception.code, 401)
            self.assertEqual(
                caught.exception.headers["WWW-Authenticate"],
                'Bearer realm="where-papers-go"',
            )
        finally:
            caught.exception.close()

        limited_url = self._serve(
            WebSecurityConfig(rate_limit_requests=1, audit_enabled=False)
        )
        first = urllib.request.Request(
            limited_url + "/api/search",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as first_error:
            urllib.request.urlopen(first, timeout=5)
        try:
            self.assertEqual(first_error.exception.code, 400)
        finally:
            first_error.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as second_error:
            urllib.request.urlopen(first, timeout=5)
        try:
            self.assertEqual(second_error.exception.code, 429)
            self.assertEqual(second_error.exception.headers["Retry-After"], "60")
        finally:
            second_error.exception.close()


if __name__ == "__main__":
    __import__("unittest").main()
