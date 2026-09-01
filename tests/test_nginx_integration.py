from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import ssl
import subprocess
from tempfile import TemporaryDirectory
import threading
import time
from unittest import TestCase
import urllib.error
import urllib.request

from scripts import manage_deployment


class _BackendHandler(BaseHTTPRequestHandler):
    authorization_seen: str | None = None
    forwarded_for_seen: str | None = None
    forwarded_seen: str | None = None
    proxy_authorization_seen: str | None = None
    forwarded_host_seen: str | None = None
    request_id_seen: str | None = None
    post_requests_seen = 0
    held_searches = 0
    held_searches_ready = threading.Event()
    release_held_searches = threading.Event()
    state_lock = threading.Lock()

    def _record_forwarded_headers(self) -> None:
        type(self).authorization_seen = self.headers.get("Authorization")
        type(self).forwarded_for_seen = self.headers.get("X-Forwarded-For")
        type(self).forwarded_seen = self.headers.get("Forwarded")
        type(self).proxy_authorization_seen = self.headers.get(
            "Proxy-Authorization"
        )
        type(self).forwarded_host_seen = self.headers.get("X-Forwarded-Host")
        type(self).request_id_seen = self.headers.get("X-Request-ID")

    def _send_payload(self) -> None:
        body = json.dumps({"proxied": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-ID", "backend-test-request-id")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib HTTP server API
        self._record_forwarded_headers()
        self._send_payload()

    def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP server API
        self._record_forwarded_headers()
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        hold = self.headers.get("X-WPG-Test-Hold") == "1"
        with type(self).state_lock:
            type(self).post_requests_seen += 1
            if hold:
                type(self).held_searches += 1
                if type(self).held_searches >= 2:
                    type(self).held_searches_ready.set()
        if hold:
            type(self).release_held_searches.wait(timeout=15)
        self._send_payload()

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class NginxIntegrationTests(TestCase):
    """Exercise the checked-in TLS/auth proxy when an Nginx binary is present."""

    @staticmethod
    def _unused_port() -> int:
        with socket.socket() as candidate:
            candidate.bind(("127.0.0.1", 0))
            return int(candidate.getsockname()[1])

    def test_nginx_syntax_tls_auth_and_proxy_redaction(self) -> None:
        template = manage_deployment.NGINX_TEMPLATE.read_text(encoding="utf-8")
        for required in (
            "limit_req_zone $binary_remote_addr zone=wpg_search:10m rate=6r/m;",
            "limit_conn_zone $binary_remote_addr zone=wpg_search_connections:10m;",
            '"remote_user":"$remote_user"',
            "auth_basic_user_file @@HTPASSWD@@;",
            "client_max_body_size 200000;",
            "client_header_timeout 15s;",
            "client_body_timeout 30s;",
            "keepalive_timeout 30s;",
            "limit_conn wpg_search_connections 2;",
            "limit_conn_status 429;",
            "proxy_set_header Host @@SERVER_NAME@@;",
            'proxy_set_header Forwarded "";',
            'proxy_set_header Proxy-Authorization "";',
            'proxy_set_header X-Forwarded-Host "";',
            'proxy_set_header Authorization "Bearer @@BACKEND_API_TOKEN@@";',
        ):
            self.assertIn(required, template)

        nginx = os.environ.get("WPG_NGINX_BIN")
        openssl = shutil.which("openssl")
        if not nginx:
            self.skipTest(
                "set WPG_NGINX_BIN to opt into the isolated Nginx integration"
            )
        if not openssl:
            self.skipTest("openssl is required to create an isolated test certificate")

        backend = ThreadingHTTPServer(("127.0.0.1", 0), _BackendHandler)
        backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
        backend_thread.start()
        self.addCleanup(backend.server_close)
        self.addCleanup(backend_thread.join, 5)
        self.addCleanup(backend.shutdown)
        _BackendHandler.authorization_seen = None
        _BackendHandler.forwarded_for_seen = None
        _BackendHandler.forwarded_seen = None
        _BackendHandler.proxy_authorization_seen = None
        _BackendHandler.forwarded_host_seen = None
        _BackendHandler.request_id_seen = None
        _BackendHandler.post_requests_seen = 0
        _BackendHandler.held_searches = 0
        _BackendHandler.held_searches_ready = threading.Event()
        _BackendHandler.release_held_searches = threading.Event()

        with TemporaryDirectory() as directory:
            root = Path(directory)
            certificate = root / "certificate.pem"
            private_key = root / "private-key.pem"
            subprocess.run(
                [
                    openssl,
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    str(private_key),
                    "-out",
                    str(certificate),
                    "-days",
                    "1",
                    "-subj",
                    "/CN=localhost",
                    "-addext",
                    "subjectAltName=DNS:localhost",
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            password = subprocess.run(
                [openssl, "passwd", "-apr1", "-salt", "wpgtest", "test-password"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            htpasswd = root / "htpasswd"
            htpasswd.write_text(f"test-user:{password}\n", encoding="utf-8")
            backend_api_token = "nginx-proxy-test-token-0123456789abcdef"

            http_port = self._unused_port()
            https_port = self._unused_port()
            rendered = manage_deployment.render_template(
                manage_deployment.NGINX_TEMPLATE,
                {
                    "UPSTREAM_PORT": str(backend.server_address[1]),
                    "SERVER_NAME": "localhost",
                    "TLS_CERTIFICATE": certificate,
                    "TLS_CERTIFICATE_KEY": private_key,
                    "HTPASSWD": htpasswd,
                    "BACKEND_API_TOKEN": backend_api_token,
                },
            ).decode("utf-8")
            # Isolate the concurrent-connection assertion below from the
            # separate request-rate bucket. The exact checked-in rate/burst
            # contract is asserted above before this test-only adjustment.
            rendered = rendered.replace("rate=6r/m;", "rate=1000r/s;")
            rendered = rendered.replace("burst=2 nodelay;", "burst=100 nodelay;")
            rendered = rendered.replace("listen 80;", f"listen 127.0.0.1:{http_port};")
            rendered = rendered.replace("listen [::]:80;", "")
            rendered = rendered.replace(
                "listen 443 ssl http2;",
                f"listen 127.0.0.1:{https_port} ssl http2;",
            )
            rendered = rendered.replace("listen [::]:443 ssl http2;", "")
            rendered = rendered.replace(
                "return 301 https://localhost$request_uri;",
                f"return 301 https://localhost:{https_port}$request_uri;",
            )
            rendered = rendered.replace(
                "/var/log/nginx/where-papers-go.access.json",
                str(root / "access.json"),
            )
            rendered = rendered.replace(
                "/var/log/nginx/where-papers-go.error.log",
                str(root / "proxy-error.log"),
            )
            config = root / "nginx.conf"
            config.write_text(
                "worker_processes 1;\n"
                f"pid {root / 'nginx.pid'};\n"
                f"error_log {root / 'nginx-error.log'} notice;\n"
                "events { worker_connections 64; }\n"
                "http {\n"
                + rendered
                + "\n}\n",
                encoding="utf-8",
            )

            subprocess.run(
                [nginx, "-p", str(root), "-c", str(config), "-t"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            process = subprocess.Popen(
                [nginx, "-p", str(root), "-c", str(config), "-g", "daemon off;"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        with socket.create_connection(("127.0.0.1", https_port), timeout=0.2):
                            break
                    except OSError:
                        if process.poll() is not None:
                            stderr = process.stderr.read() if process.stderr else ""
                            self.fail(f"Nginx exited before readiness: {stderr[-2000:]}")
                        time.sleep(0.05)
                else:
                    self.fail("Nginx did not open its isolated TLS listener")

                connection = http.client.HTTPConnection("127.0.0.1", http_port, timeout=5)
                connection.request(
                    "GET",
                    "/api/health/live",
                    headers={"Host": "attacker.example"},
                )
                redirect = connection.getresponse()
                self.assertEqual(redirect.status, 301)
                self.assertEqual(
                    redirect.headers["Location"],
                    f"https://localhost:{https_port}/api/health/live",
                )
                redirect.read()
                connection.close()

                context = ssl.create_default_context(cafile=str(certificate))
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
                )
                protected_paths = (
                    "/",
                    "/api/health/live",
                    "/api/health/ready",
                    "/api/health",
                )
                for protected_path in protected_paths:
                    url = f"https://localhost:{https_port}{protected_path}"
                    with self.assertRaises(urllib.error.HTTPError) as denied:
                        opener.open(url, timeout=5)
                    try:
                        self.assertEqual(denied.exception.code, 401)
                    finally:
                        denied.exception.close()

                credential = base64.b64encode(b"test-user:test-password").decode("ascii")
                wrong_credential = base64.b64encode(
                    b"test-user:wrong-password"
                ).decode("ascii")
                wrong_request = urllib.request.Request(
                    f"https://localhost:{https_port}/api/health/ready",
                    headers={"Authorization": "Basic " + wrong_credential},
                )
                with self.assertRaises(urllib.error.HTTPError) as wrong_denied:
                    opener.open(wrong_request, timeout=5)
                try:
                    self.assertEqual(wrong_denied.exception.code, 401)
                finally:
                    wrong_denied.exception.close()

                payload = None
                for protected_path in protected_paths:
                    request = urllib.request.Request(
                        f"https://localhost:{https_port}{protected_path}",
                        headers={
                            "Authorization": "Basic " + credential,
                            "Host": "localhost",
                            "X-Forwarded-For": "203.0.113.77",
                            "Forwarded": "for=203.0.113.77",
                            "Proxy-Authorization": "Basic forged",
                            "X-Forwarded-Host": "attacker.example",
                            "X-Request-ID": "client-forged-request-id",
                        },
                    )
                    with opener.open(request, timeout=5) as response:
                        payload = json.load(response)
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
                        self.assertEqual(
                            response.headers["X-Request-ID"],
                            "backend-test-request-id",
                        )
                    self.assertEqual(payload, {"proxied": True})
                self.assertEqual(
                    _BackendHandler.authorization_seen,
                    "Bearer " + backend_api_token,
                )
                self.assertEqual(_BackendHandler.forwarded_for_seen, "127.0.0.1")
                self.assertIsNone(_BackendHandler.forwarded_seen)
                self.assertIsNone(_BackendHandler.proxy_authorization_seen)
                self.assertIsNone(_BackendHandler.forwarded_host_seen)
                self.assertIsNotNone(_BackendHandler.request_id_seen)
                self.assertNotEqual(
                    _BackendHandler.request_id_seen, "client-forged-request-id"
                )

                access_log = root / "access.json"
                deadline = time.monotonic() + 5
                records = []
                while time.monotonic() < deadline:
                    if access_log.is_file():
                        records = [
                            json.loads(line)
                            for line in access_log.read_text(encoding="utf-8").splitlines()
                            if line.strip()
                        ]
                        if any(record.get("status") == 200 for record in records):
                            break
                    time.sleep(0.05)
                successful = [record for record in records if record.get("status") == 200]
                self.assertEqual(len(successful), len(protected_paths))
                self.assertEqual(
                    successful[-1]["request_id"], _BackendHandler.request_id_seen
                )
                self.assertEqual(
                    successful[-1]["upstream_request_id"],
                    "backend-test-request-id",
                )
                self.assertTrue(
                    all(record["remote_user"] == "test-user" for record in successful)
                )

                oversized = urllib.request.Request(
                    f"https://localhost:{https_port}/api/search",
                    data=b"x" * 200_001,
                    headers={
                        "Authorization": "Basic " + credential,
                        "Content-Type": "application/octet-stream",
                        "Host": "localhost",
                    },
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as too_large:
                    opener.open(oversized, timeout=5)
                try:
                    self.assertEqual(too_large.exception.code, 413)
                finally:
                    too_large.exception.close()
                self.assertEqual(_BackendHandler.post_requests_seen, 0)

                held_results: list[int | BaseException] = []

                def held_search() -> None:
                    held_opener = urllib.request.build_opener(
                        urllib.request.ProxyHandler({}),
                        urllib.request.HTTPSHandler(context=context),
                    )
                    held_request = urllib.request.Request(
                        f"https://localhost:{https_port}/api/search",
                        data=b"{}",
                        headers={
                            "Authorization": "Basic " + credential,
                            "Content-Type": "application/json",
                            "Host": "localhost",
                            "X-WPG-Test-Hold": "1",
                        },
                        method="POST",
                    )
                    try:
                        with held_opener.open(held_request, timeout=20) as response:
                            response.read()
                            held_results.append(response.status)
                    except BaseException as exc:
                        held_results.append(exc)

                held_threads = [
                    threading.Thread(target=held_search, daemon=True)
                    for _index in range(2)
                ]
                for thread in held_threads:
                    thread.start()
                try:
                    self.assertTrue(
                        _BackendHandler.held_searches_ready.wait(timeout=5),
                        "two proxied Search requests did not reach the backend",
                    )
                    saturated = urllib.request.Request(
                        f"https://localhost:{https_port}/api/search",
                        data=b"{}",
                        headers={
                            "Authorization": "Basic " + credential,
                            "Content-Type": "application/json",
                            "Host": "localhost",
                        },
                        method="POST",
                    )
                    with self.assertRaises(urllib.error.HTTPError) as rejected:
                        opener.open(saturated, timeout=5)
                    try:
                        self.assertEqual(rejected.exception.code, 429)
                    finally:
                        rejected.exception.close()
                finally:
                    _BackendHandler.release_held_searches.set()
                    for thread in held_threads:
                        thread.join(timeout=10)
                self.assertTrue(all(not thread.is_alive() for thread in held_threads))
                self.assertEqual(held_results, [200, 200])
                self.assertEqual(_BackendHandler.post_requests_seen, 2)
            finally:
                _BackendHandler.release_held_searches.set()
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)

            # Start a fresh master with the checked-in 6r/m, burst=2 request
            # bucket. Serial requests cannot hit the concurrent-connection
            # limit, so the fourth immediate request must be rejected by
            # limit_req after the initial request plus two burst requests.
            rate_root = root / "fixed-rate"
            rate_root.mkdir()
            rate_http_port = self._unused_port()
            rate_https_port = self._unused_port()
            rate_rendered = manage_deployment.render_template(
                manage_deployment.NGINX_TEMPLATE,
                {
                    "UPSTREAM_PORT": str(backend.server_address[1]),
                    "SERVER_NAME": "localhost",
                    "TLS_CERTIFICATE": certificate,
                    "TLS_CERTIFICATE_KEY": private_key,
                    "HTPASSWD": htpasswd,
                    "BACKEND_API_TOKEN": backend_api_token,
                },
            ).decode("utf-8")
            self.assertIn(
                "limit_req_zone $binary_remote_addr "
                "zone=wpg_search:10m rate=6r/m;",
                rate_rendered,
            )
            self.assertIn(
                "limit_req zone=wpg_search burst=2 nodelay;",
                rate_rendered,
            )
            rate_rendered = rate_rendered.replace(
                "listen 80;", f"listen 127.0.0.1:{rate_http_port};"
            )
            rate_rendered = rate_rendered.replace("listen [::]:80;", "")
            rate_rendered = rate_rendered.replace(
                "listen 443 ssl http2;",
                f"listen 127.0.0.1:{rate_https_port} ssl http2;",
            )
            rate_rendered = rate_rendered.replace(
                "listen [::]:443 ssl http2;", ""
            )
            rate_rendered = rate_rendered.replace(
                "return 301 https://localhost$request_uri;",
                f"return 301 https://localhost:{rate_https_port}$request_uri;",
            )
            rate_access_log = rate_root / "access.json"
            rate_error_log = rate_root / "proxy-error.log"
            rate_rendered = rate_rendered.replace(
                "/var/log/nginx/where-papers-go.access.json",
                str(rate_access_log),
            )
            rate_rendered = rate_rendered.replace(
                "/var/log/nginx/where-papers-go.error.log",
                str(rate_error_log),
            )
            rate_config = rate_root / "nginx.conf"
            rate_config.write_text(
                "worker_processes 1;\n"
                f"pid {rate_root / 'nginx.pid'};\n"
                f"error_log {rate_root / 'nginx-error.log'} notice;\n"
                "events { worker_connections 64; }\n"
                "http {\n"
                + rate_rendered
                + "\n}\n",
                encoding="utf-8",
            )

            subprocess.run(
                [nginx, "-p", str(rate_root), "-c", str(rate_config), "-t"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            rate_process = subprocess.Popen(
                [
                    nginx,
                    "-p",
                    str(rate_root),
                    "-c",
                    str(rate_config),
                    "-g",
                    "daemon off;",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        with socket.create_connection(
                            ("127.0.0.1", rate_https_port), timeout=0.2
                        ):
                            break
                    except OSError:
                        if rate_process.poll() is not None:
                            stderr = (
                                rate_process.stderr.read()
                                if rate_process.stderr
                                else ""
                            )
                            self.fail(
                                "fixed-rate Nginx exited before readiness: "
                                f"{stderr[-2000:]}"
                            )
                        time.sleep(0.05)
                else:
                    self.fail("fixed-rate Nginx did not open its TLS listener")

                rate_context = ssl.create_default_context(
                    cafile=str(certificate)
                )
                rate_opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({}),
                    urllib.request.HTTPSHandler(context=rate_context),
                )
                rate_url = (
                    f"https://localhost:{rate_https_port}/api/search"
                )
                with _BackendHandler.state_lock:
                    posts_before_rate_test = _BackendHandler.post_requests_seen
                rate_statuses: list[int] = []
                for _index in range(4):
                    rate_request = urllib.request.Request(
                        rate_url,
                        data=b"{}",
                        headers={
                            "Authorization": "Basic " + credential,
                            "Content-Type": "application/json",
                            "Host": "localhost",
                        },
                        method="POST",
                    )
                    try:
                        with rate_opener.open(
                            rate_request, timeout=5
                        ) as response:
                            response.read()
                            rate_statuses.append(response.status)
                    except urllib.error.HTTPError as exc:
                        try:
                            exc.read()
                            rate_statuses.append(exc.code)
                        finally:
                            exc.close()
                self.assertEqual(rate_statuses, [200, 200, 200, 429])
                with _BackendHandler.state_lock:
                    posts_after_rate_test = _BackendHandler.post_requests_seen
                self.assertEqual(
                    posts_after_rate_test - posts_before_rate_test,
                    3,
                )

                deadline = time.monotonic() + 5
                rate_records = []
                rate_error_text = ""
                while time.monotonic() < deadline:
                    if rate_access_log.is_file():
                        rate_records = [
                            json.loads(line)
                            for line in rate_access_log.read_text(
                                encoding="utf-8"
                            ).splitlines()
                            if line.strip()
                        ]
                    if rate_error_log.is_file():
                        rate_error_text = rate_error_log.read_text(
                            encoding="utf-8"
                        )
                    if (
                        [record.get("status") for record in rate_records]
                        == rate_statuses
                        and "limiting requests" in rate_error_text
                        and "zone \"wpg_search\"" in rate_error_text
                    ):
                        break
                    time.sleep(0.05)
                self.assertEqual(
                    [record.get("status") for record in rate_records],
                    [200, 200, 200, 429],
                )
                self.assertIn("limiting requests", rate_error_text)
                self.assertIn('zone "wpg_search"', rate_error_text)
            finally:
                if rate_process.poll() is None:
                    os.killpg(rate_process.pid, signal.SIGTERM)
                try:
                    rate_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(rate_process.pid, signal.SIGKILL)
                    rate_process.wait(timeout=5)


if __name__ == "__main__":
    __import__("unittest").main()
