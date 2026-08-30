from __future__ import annotations

import io
import ipaddress
import json
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request

from where_paper_go import web_app, web_security
from where_paper_go.web_security import (
    SlidingWindowRateLimiter,
    WebSecurityConfig,
    audit_record,
    client_ip,
    configured_secret_values,
    redact_sensitive_text,
)


class WebSecurityTests(TestCase):
    @staticmethod
    def _read_closed_socket(client: socket.socket) -> bytes:
        chunks: list[bytes] = []
        while True:
            block = client.recv(4096)
            if not block:
                return b"".join(chunks)
            chunks.append(block)

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

    def test_api_token_symlink_is_rejected_without_following_it(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "real-api.token"
            target.write_text("c" * 40, encoding="utf-8")
            target.chmod(0o600)
            symlink = root / "api.token"
            symlink.symlink_to(target)
            with patch.dict(
                os.environ,
                {"WPG_API_TOKEN_FILE": str(symlink)},
                clear=True,
            ):
                with self.assertRaisesRegex(ValueError, "WPG_API_TOKEN_FILE"):
                    WebSecurityConfig.from_environment()

    def test_api_token_file_has_a_hard_size_limit(self) -> None:
        with TemporaryDirectory() as directory:
            token_path = Path(directory) / "api.token"
            token_path.write_bytes(b"d" * (web_security._MAX_API_TOKEN_FILE_BYTES + 1))
            token_path.chmod(0o600)
            with patch.dict(
                os.environ,
                {"WPG_API_TOKEN_FILE": str(token_path)},
                clear=True,
            ):
                with self.assertRaisesRegex(ValueError, "must not exceed"):
                    WebSecurityConfig.from_environment()

    def test_api_token_file_must_be_owned_by_current_user(self) -> None:
        with TemporaryDirectory() as directory:
            token_path = Path(directory) / "api.token"
            token_path.write_text("e" * 40, encoding="utf-8")
            token_path.chmod(0o600)
            real_fstat = os.fstat

            def foreign_owner(descriptor: int) -> SimpleNamespace:
                observed = real_fstat(descriptor)
                return SimpleNamespace(
                    st_mode=observed.st_mode,
                    st_uid=observed.st_uid + 1,
                    st_dev=observed.st_dev,
                    st_ino=observed.st_ino,
                    st_size=observed.st_size,
                    st_mtime_ns=observed.st_mtime_ns,
                    st_ctime_ns=observed.st_ctime_ns,
                )

            with patch.dict(
                os.environ,
                {"WPG_API_TOKEN_FILE": str(token_path)},
                clear=True,
            ), patch.object(web_security.os, "fstat", side_effect=foreign_owner):
                with self.assertRaisesRegex(ValueError, "owned by the current user"):
                    WebSecurityConfig.from_environment()

    def test_api_token_file_identity_drift_is_rejected(self) -> None:
        for changed_field in ("st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"):
            with self.subTest(changed_field=changed_field), TemporaryDirectory() as directory:
                token_path = Path(directory) / "api.token"
                token_path.write_text("f" * 40, encoding="utf-8")
                token_path.chmod(0o600)
                real_fstat = os.fstat
                call_count = 0

                def drifting_identity(descriptor: int) -> os.stat_result | SimpleNamespace:
                    nonlocal call_count
                    observed = real_fstat(descriptor)
                    call_count += 1
                    if call_count == 1:
                        return observed
                    values = {
                        "st_mode": observed.st_mode,
                        "st_uid": observed.st_uid,
                        "st_dev": observed.st_dev,
                        "st_ino": observed.st_ino,
                        "st_size": observed.st_size,
                        "st_mtime_ns": observed.st_mtime_ns,
                        "st_ctime_ns": observed.st_ctime_ns,
                    }
                    values[changed_field] += 1
                    return SimpleNamespace(**values)

                with patch.dict(
                    os.environ,
                    {"WPG_API_TOKEN_FILE": str(token_path)},
                    clear=True,
                ), patch.object(
                    web_security.os, "fstat", side_effect=drifting_identity
                ):
                    with self.assertRaisesRegex(ValueError, "changed while being read"):
                        WebSecurityConfig.from_environment()

    def test_client_allowlist_defaults_to_loopback_and_validates_environment(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            default = WebSecurityConfig.from_environment()
        self.assertTrue(default.client_allowed("127.0.0.1"))
        self.assertTrue(default.client_allowed("::1"))
        self.assertTrue(default.client_allowed("::ffff:127.0.0.1"))
        self.assertFalse(default.client_allowed("172.22.13.155"))
        self.assertFalse(default.client_allowed("not-an-address"))

        with patch.dict(
            os.environ,
            {"WPG_ALLOWED_CLIENT_CIDRS": "127.0.0.0/8,172.22.13.155/24"},
            clear=True,
        ):
            configured = WebSecurityConfig.from_environment()
        self.assertEqual(
            configured.allowed_client_cidrs,
            (
                ipaddress.ip_network("127.0.0.0/8"),
                ipaddress.ip_network("172.22.13.0/24"),
            ),
        )
        self.assertTrue(configured.client_allowed("::ffff:172.22.13.155"))

        for invalid in ("", "not-a-cidr"):
            with self.subTest(invalid=invalid):
                with patch.dict(
                    os.environ,
                    {"WPG_ALLOWED_CLIENT_CIDRS": invalid},
                    clear=True,
                ):
                    with self.assertRaisesRegex(ValueError, "WPG_ALLOWED_CLIENT_CIDRS"):
                        WebSecurityConfig.from_environment()

    def test_sliding_window_rate_limit_has_deterministic_retry_after(self) -> None:
        limiter = SlidingWindowRateLimiter(2, 10)
        self.assertEqual(limiter.allow("client", now=100), (True, 0))
        self.assertEqual(limiter.allow("client", now=101), (True, 0))
        self.assertEqual(limiter.allow("client", now=102), (False, 8))
        self.assertEqual(limiter.allow("client", now=111), (True, 0))

    def test_rate_limit_client_table_is_a_hard_bound(self) -> None:
        limiter = SlidingWindowRateLimiter(2, 10, max_clients=2)
        self.assertEqual(limiter.allow("first", now=100), (True, 0))
        self.assertEqual(limiter.allow("second", now=101), (True, 0))
        self.assertEqual(limiter.allow("third", now=102), (False, 8))
        self.assertLessEqual(len(limiter._events), 2)
        self.assertEqual(limiter.allow("third", now=111), (True, 0))
        self.assertLessEqual(len(limiter._events), 2)

    def test_forwarded_address_is_used_only_for_a_trusted_proxy(self) -> None:
        trusted = WebSecurityConfig(trust_proxy_headers=True)
        headers = {"X-Forwarded-For": "203.0.113.7, 127.0.0.1"}
        self.assertEqual(client_ip("127.0.0.1", headers, trusted), "203.0.113.7")
        self.assertEqual(client_ip("192.0.2.8", headers, trusted), "192.0.2.8")
        self.assertEqual(
            client_ip("127.0.0.1", headers, WebSecurityConfig()), "127.0.0.1"
        )
        self.assertEqual(
            client_ip("::ffff:127.0.0.1", {}, WebSecurityConfig()), "127.0.0.1"
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

    def test_http_allowlist_rejects_direct_peer_before_any_endpoint(self) -> None:
        denied_url = self._serve(
            WebSecurityConfig(
                allowed_client_cidrs=(ipaddress.ip_network("192.0.2.0/24"),),
                audit_enabled=False,
            )
        )
        denied = urllib.parse.urlparse(denied_url)
        with socket.create_connection((denied.hostname, denied.port), timeout=5) as client:
            # The pre-thread perimeter rejects even a client that sends no
            # request bytes; slow headers and unsupported methods cannot enter
            # BaseHTTPRequestHandler.
            self.assertEqual(client.recv(1), b"")

        allowed_url = self._serve(
            WebSecurityConfig(
                trust_proxy_headers=True,
                allowed_client_cidrs=(ipaddress.ip_network("127.0.0.0/8"),),
                audit_enabled=False,
            )
        )
        with urllib.request.urlopen(
            urllib.request.Request(
                allowed_url + "/api/health/live",
                headers={"X-Forwarded-For": "198.51.100.77"},
            ),
            timeout=5,
        ) as response:
            self.assertEqual(response.status, 200)

    def test_oversized_body_is_rejected_before_body_read_or_worker_use(self) -> None:
        base_url = self._serve(
            WebSecurityConfig(
                request_body_limit=32,
                rate_limit_requests=100,
                audit_enabled=False,
            )
        )
        parsed = urllib.parse.urlparse(base_url)
        with patch.object(web_app, "_run_search") as run_search:
            with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as client:
                client.sendall(
                    b"POST /api/search HTTP/1.1\r\n"
                    b"Host: localhost\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 1000000\r\n"
                    b"Connection: keep-alive\r\n\r\n"
                )
                response = self._read_closed_socket(client)

        self.assertIn(b" 413 ", response.split(b"\r\n", 1)[0])
        self.assertIn(b"Connection: close", response)
        run_search.assert_not_called()

    def test_slow_incomplete_body_times_out_without_worker_use(self) -> None:
        base_url = self._serve(
            WebSecurityConfig(
                request_read_timeout_seconds=1,
                rate_limit_requests=100,
                audit_enabled=False,
            )
        )
        parsed = urllib.parse.urlparse(base_url)
        with patch.object(web_app, "_run_search") as run_search:
            with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as client:
                client.sendall(
                    b"POST /api/search HTTP/1.1\r\n"
                    b"Host: localhost\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 100\r\n"
                    b"Connection: close\r\n\r\n{"
                )
                response = self._read_closed_socket(client)

        self.assertIn(b" 408 ", response.split(b"\r\n", 1)[0])
        self.assertIn(b"Connection: close", response)
        run_search.assert_not_called()

    def test_short_body_after_client_half_close_is_rejected_without_worker_use(self) -> None:
        base_url = self._serve(
            WebSecurityConfig(rate_limit_requests=100, audit_enabled=False)
        )
        parsed = urllib.parse.urlparse(base_url)
        body = b'{"query":"short but valid"}'
        with patch.object(web_app, "_run_search") as run_search:
            with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as client:
                client.sendall(
                    b"POST /api/search HTTP/1.1\r\n"
                    b"Host: localhost\r\n"
                    b"Content-Type: application/json\r\n"
                    + f"Content-Length: {len(body) + 5}\r\n".encode("ascii")
                    + b"Connection: keep-alive\r\n\r\n"
                    + body
                )
                client.shutdown(socket.SHUT_WR)
                response = self._read_closed_socket(client)

        self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])
        self.assertIn(b"Connection: close", response)
        run_search.assert_not_called()

    def test_concurrency_saturation_returns_503_without_queueing(self) -> None:
        base_url = self._serve(
            WebSecurityConfig(
                max_concurrent_searches=1,
                rate_limit_requests=100,
                audit_enabled=False,
            )
        )
        entered = threading.Event()
        release = threading.Event()
        first_result: list[int | BaseException] = []

        def blocked_search(_body):
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return 200, {"results": []}

        def send_first() -> None:
            try:
                request = urllib.request.Request(
                    base_url + "/api/search",
                    data=json.dumps({"query": "first"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    first_result.append(response.status)
            except BaseException as exc:  # surfaced in the parent test thread
                first_result.append(exc)

        with patch.object(web_app, "_run_search", side_effect=blocked_search) as run_search:
            thread = threading.Thread(target=send_first)
            thread.start()
            try:
                self.assertTrue(entered.wait(timeout=5))
                second = urllib.request.Request(
                    base_url + "/api/search",
                    data=json.dumps({"query": "second"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(second, timeout=5)
                try:
                    self.assertEqual(caught.exception.code, 503)
                    self.assertEqual(caught.exception.headers["Retry-After"], "5")
                    self.assertEqual(caught.exception.headers["Connection"], "close")
                finally:
                    caught.exception.close()
            finally:
                release.set()
                thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(first_result, [200])
        self.assertEqual(run_search.call_count, 1)

    def test_ambiguous_http_body_framing_is_rejected_and_closed(self) -> None:
        base_url = self._serve(
            WebSecurityConfig(rate_limit_requests=100, audit_enabled=False)
        )
        parsed = urllib.parse.urlparse(base_url)
        requests = (
            (
                b"POST /api/search HTTP/1.1\r\n"
                b"Host: localhost\r\nTransfer-Encoding: chunked\r\n"
                b"Connection: keep-alive\r\n\r\n0\r\n\r\n"
            ),
            (
                b"POST /api/search HTTP/1.1\r\n"
                b"Host: localhost\r\nContent-Length: 2\r\nContent-Length: 3\r\n"
                b"Connection: keep-alive\r\n\r\n{}"
            ),
        )
        with patch.object(web_app, "_run_search") as run_search:
            for request in requests:
                with self.subTest(request=request.split(b"\r\n", 1)[0]):
                    with socket.create_connection(
                        (parsed.hostname, parsed.port), timeout=5
                    ) as client:
                        client.sendall(request)
                        response = self._read_closed_socket(client)
                    self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])
                    self.assertIn(b"Connection: close", response)
        run_search.assert_not_called()

    def test_expect_continue_is_rejected_before_body_for_auth_and_size(self) -> None:
        configurations = (
            (
                WebSecurityConfig(
                    api_token="d" * 40,
                    require_api_auth=True,
                    rate_limit_requests=100,
                    audit_enabled=False,
                ),
                401,
            ),
            (
                WebSecurityConfig(
                    request_body_limit=32,
                    rate_limit_requests=100,
                    audit_enabled=False,
                ),
                413,
            ),
        )
        with patch.object(web_app, "_run_search") as run_search:
            for config, expected in configurations:
                with self.subTest(expected=expected):
                    parsed = urllib.parse.urlparse(self._serve(config))
                    with socket.create_connection(
                        (parsed.hostname, parsed.port), timeout=5
                    ) as client:
                        client.sendall(
                            b"POST /api/search HTTP/1.1\r\n"
                            b"Host: localhost\r\n"
                            b"Content-Type: application/json\r\n"
                            b"Content-Length: 1000000\r\n"
                            b"Expect: 100-continue\r\n"
                            b"Connection: keep-alive\r\n\r\n"
                        )
                        response = self._read_closed_socket(client)
                    status = response.split(b"\r\n", 1)[0]
                    self.assertIn(f" {expected} ".encode("ascii"), status)
                    self.assertNotIn(b" 100 ", response)
                    self.assertIn(b"Connection: close", response)
        run_search.assert_not_called()

    def test_connection_thread_limit_rejects_second_slow_header(self) -> None:
        base_url = self._serve(
            WebSecurityConfig(
                max_concurrent_connections=1,
                request_read_timeout_seconds=5,
                audit_enabled=False,
            )
        )
        parsed = urllib.parse.urlparse(base_url)
        first = socket.create_connection((parsed.hostname, parsed.port), timeout=5)
        self.addCleanup(first.close)
        first.sendall(b"GET /api/health/live HTTP/1.1\r\nHost: local")
        time.sleep(0.1)
        with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as second:
            second.sendall(
                b"GET /api/health/live HTTP/1.1\r\nHost: localhost\r\n\r\n"
            )
            self.assertEqual(second.recv(1), b"")
        first.close()

    def test_ipv6_loopback_listener_matches_allowlist_when_available(self) -> None:
        try:
            server = web_app.VenueHTTPServer(
                ("::1", 0),
                web_app.VenueHandler,
                WebSecurityConfig(audit_enabled=False),
            )
        except OSError:
            self.skipTest("host has no IPv6 loopback listener")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with socket.create_connection(
                ("::1", server.server_address[1]), timeout=5
            ) as client:
                client.sendall(
                    b"GET /api/health/live HTTP/1.1\r\n"
                    b"Host: [::1]\r\nConnection: close\r\n\r\n"
                )
                response = self._read_closed_socket(client)
            self.assertIn(b" 200 ", response.split(b"\r\n", 1)[0])
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    __import__("unittest").main()
