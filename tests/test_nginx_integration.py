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
    authenticated_user_seen: str | None = None
    internal_token_seen: str | None = None
    internal_client_addr_seen: str | None = None
    post_requests_seen = 0
    paths_seen: list[str] = []
    request_bodies_seen: list[bytes] = []
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
        type(self).authenticated_user_seen = self.headers.get(
            "X-WPG-Authenticated-User"
        )
        type(self).internal_token_seen = self.headers.get("X-WPG-Internal-Token")
        type(self).internal_client_addr_seen = self.headers.get(
            "X-WPG-Client-Addr"
        )
        with type(self).state_lock:
            type(self).paths_seen.append(self.path)

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
        request_body = self.rfile.read(length)
        hold = self.headers.get("X-WPG-Test-Hold") == "1"
        with type(self).state_lock:
            type(self).post_requests_seen += 1
            type(self).request_bodies_seen.append(request_body)
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
            "limit_req_zone $binary_remote_addr zone=wpg_auth_attempts:10m rate=30r/m;",
            "limit_req_zone $binary_remote_addr zone=wpg_search:10m rate=6r/m;",
            "limit_req_zone $http_x_wpg_authenticated_user zone=wpg_search_user:10m rate=6r/m;",
            "limit_conn_zone $binary_remote_addr zone=wpg_auth_connections:10m;",
            "limit_conn_zone $binary_remote_addr zone=wpg_search_connections:10m;",
            "limit_conn_zone $http_x_wpg_authenticated_user zone=wpg_search_user_connections:10m;",
            '"remote_user":"$remote_user"',
            "auth_basic_user_file @@HTPASSWD@@;",
            "client_max_body_size 200000;",
            "client_header_timeout 15s;",
            "client_body_timeout 30s;",
            "keepalive_timeout 30s;",
            "limit_req zone=wpg_auth_attempts burst=20 nodelay;",
            "limit_req zone=wpg_search burst=2 nodelay;",
            "limit_req zone=wpg_search_user burst=2 nodelay;",
            "limit_conn wpg_auth_connections 16;",
            "limit_conn wpg_search_connections 2;",
            "limit_conn wpg_search_user_connections 2;",
            "limit_conn_status 429;",
            "proxy_set_header Host @@SERVER_NAME@@;",
            'proxy_set_header Forwarded "";',
            'proxy_set_header Proxy-Authorization "";',
            'proxy_set_header X-Forwarded-Host "";',
            'proxy_set_header Authorization "Bearer @@BACKEND_API_TOKEN@@";',
        ):
            self.assertIn(required, template)
        tls_server_prefix = template.split("listen 443 ssl http2;", 1)[1].split(
            "location = /api/search {", 1
        )[0]
        self.assertIn(
            "limit_req zone=wpg_auth_attempts burst=20 nodelay;",
            tls_server_prefix,
        )
        self.assertIn(
            "limit_conn wpg_auth_connections 16;", tls_server_prefix
        )
        for path in ("/api/search", "/api/search/stream"):
            outer_location = template.split(f"location = {path} {{", 1)[1].split(
                "\n    }", 1
            )[0]
            self.assertIn('auth_basic "Where Papers Go";', outer_location)
            self.assertIn("auth_basic_user_file @@HTPASSWD@@;", outer_location)
            self.assertIn(
                "limit_req zone=wpg_auth_attempts burst=20 nodelay;",
                outer_location,
            )
            self.assertIn(
                "limit_req zone=wpg_search burst=2 nodelay;", outer_location
            )
            self.assertIn(
                "limit_conn wpg_auth_connections 16;", outer_location
            )
            self.assertIn(
                "limit_conn wpg_search_connections 2;", outer_location
            )
            self.assertEqual(outer_location.count("limit_req zone="), 2)
            self.assertEqual(outer_location.count("limit_conn "), 2)
            self.assertNotIn("zone=wpg_search_user", outer_location)
            self.assertIn("limit_req_status 429;", outer_location)
            self.assertIn("limit_conn_status 429;", outer_location)
            self.assertIn(
                "proxy_pass http://where_papers_go_authenticated_gate;",
                outer_location,
            )
            self.assertIn(
                "proxy_set_header X-WPG-Authenticated-User $remote_user;",
                outer_location,
            )
            self.assertIn(
                'proxy_set_header X-WPG-Internal-Token "@@BACKEND_API_TOKEN@@";',
                outer_location,
            )
            self.assertIn('proxy_set_header Authorization "";', outer_location)
        internal_server = template.split(
            "listen 127.0.0.1:@@AUTHENTICATED_GATE_PORT@@;", 1
        )[1]
        self.assertIn("access_log off;", internal_server)
        internal_location = internal_server.split(
            "location ~ ^/api/search(?:/stream)?$ {", 1
        )[1].split("\n    }", 1)[0]
        self.assertIn(
            'if ($http_x_wpg_internal_token != "@@BACKEND_API_TOKEN@@") { return 403; }',
            internal_location,
        )
        self.assertIn(
            'if ($http_x_wpg_authenticated_user = "") { return 403; }',
            internal_location,
        )
        self.assertEqual(internal_location.count("limit_req zone="), 1)
        self.assertEqual(internal_location.count("limit_conn "), 1)
        self.assertIn(
            "limit_req zone=wpg_search_user burst=2 nodelay;", internal_location
        )
        self.assertIn(
            "limit_conn wpg_search_user_connections 2;", internal_location
        )
        self.assertNotIn("zone=wpg_search burst", internal_location)
        self.assertNotIn("limit_conn wpg_search_connections", internal_location)
        self.assertIn("limit_req_status 429;", internal_location)
        self.assertIn("limit_conn_status 429;", internal_location)
        self.assertIn(
            "proxy_pass http://where_papers_go_backend;", internal_location
        )
        for header in (
            "X-WPG-Authenticated-User",
            "X-WPG-Client-Addr",
            "X-WPG-Internal-Token",
        ):
            self.assertIn(f'proxy_set_header {header} "";', internal_location)

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
        _BackendHandler.authenticated_user_seen = None
        _BackendHandler.internal_token_seen = None
        _BackendHandler.internal_client_addr_seen = None
        _BackendHandler.post_requests_seen = 0
        _BackendHandler.paths_seen = []
        _BackendHandler.request_bodies_seen = []
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
            test_users = (
                "test-user",
                "ip-conn-1",
                "ip-conn-2",
                "ip-conn-3",
                "user-conn",
                "ip-rate-1",
                "ip-rate-2",
                "ip-rate-3",
                "ip-rate-4",
                "user-rate",
            )
            htpasswd.write_text(
                "".join(f"{username}:{password}\n" for username in test_users),
                encoding="utf-8",
            )
            backend_api_token = "nginx-proxy-test-token-0123456789abcdef"
            two_hop_body = b'{"two_hop_probe":"distinct-nonempty-body"}'

            def basic_credential(
                username: str, password_value: str = "test-password"
            ) -> str:
                encoded = base64.b64encode(
                    f"{username}:{password_value}".encode("ascii")
                ).decode("ascii")
                return "Basic " + encoded

            def post_search(
                *,
                port: int,
                tls_context: ssl.SSLContext,
                path: str,
                username: str,
                password_value: str = "test-password",
                source_ip: str = "127.0.0.1",
                hold: bool = False,
                body: bytes = b"{}",
                timeout: float = 5,
            ) -> int:
                connection = http.client.HTTPSConnection(
                    "localhost",
                    port,
                    timeout=timeout,
                    context=tls_context,
                    source_address=(source_ip, 0),
                )
                headers = {
                    "Authorization": basic_credential(username, password_value),
                    "Content-Type": "application/json",
                    "Host": "localhost",
                    "X-WPG-Authenticated-User": "client-forged-user",
                    "X-WPG-Client-Addr": "203.0.113.77",
                    "X-WPG-Internal-Token": "client-forged-token",
                }
                if hold:
                    headers["X-WPG-Test-Hold"] = "1"
                try:
                    connection.request("POST", path, body=body, headers=headers)
                    response = connection.getresponse()
                    try:
                        response.read()
                        return response.status
                    finally:
                        response.close()
                finally:
                    connection.close()

            def get_path_status(
                *,
                port: int,
                tls_context: ssl.SSLContext,
                path: str,
                username: str,
                password_value: str,
                source_ip: str,
            ) -> int:
                connection = http.client.HTTPSConnection(
                    "localhost",
                    port,
                    timeout=5,
                    context=tls_context,
                    source_address=(source_ip, 0),
                )
                try:
                    connection.request(
                        "GET",
                        path,
                        headers={
                            "Authorization": basic_credential(
                                username, password_value
                            ),
                            "Host": "localhost",
                        },
                    )
                    response = connection.getresponse()
                    try:
                        response.read()
                        return response.status
                    finally:
                        response.close()
                finally:
                    connection.close()

            http_port = self._unused_port()
            https_port = self._unused_port()
            while https_port == http_port:
                https_port = self._unused_port()
            authenticated_gate_port = self._unused_port()
            while authenticated_gate_port in {
                http_port,
                https_port,
                backend.server_address[1],
            }:
                authenticated_gate_port = self._unused_port()
            rendered = manage_deployment.render_template(
                manage_deployment.NGINX_TEMPLATE,
                {
                    "UPSTREAM_PORT": str(backend.server_address[1]),
                    "AUTHENTICATED_GATE_PORT": str(authenticated_gate_port),
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
                # The second listener is reachable only on loopback, but local
                # reachability alone grants nothing: spoofed identity without
                # the private handoff token fails in REWRITE before user limits.
                gate_connection = http.client.HTTPConnection(
                    "127.0.0.1", authenticated_gate_port, timeout=5
                )
                try:
                    gate_connection.request(
                        "POST",
                        "/api/search",
                        body=b"{}",
                        headers={
                            "Host": "wpg-authenticated-gate",
                            "Content-Type": "application/json",
                            "X-WPG-Authenticated-User": "user-rate",
                            "X-WPG-Client-Addr": "203.0.113.77",
                            "X-WPG-Internal-Token": "client-forged-token",
                        },
                    )
                    gate_denied = gate_connection.getresponse()
                    try:
                        self.assertEqual(gate_denied.status, 403)
                        gate_denied.read()
                    finally:
                        gate_denied.close()
                finally:
                    gate_connection.close()
                self.assertEqual(_BackendHandler.post_requests_seen, 0)
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

                credential = basic_credential("test-user").removeprefix("Basic ")
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

                def exercise_connection_limit(
                    *,
                    path: str,
                    held_clients: tuple[tuple[str, str], tuple[str, str]],
                    rejected_client: tuple[str, str],
                ) -> None:
                    _BackendHandler.held_searches = 0
                    _BackendHandler.held_searches_ready = threading.Event()
                    _BackendHandler.release_held_searches = threading.Event()
                    with _BackendHandler.state_lock:
                        posts_before = _BackendHandler.post_requests_seen
                    held_results: list[int | BaseException | None] = [None, None]

                    def held_search(
                        index: int, username: str, source_ip: str
                    ) -> None:
                        try:
                            held_results[index] = post_search(
                                port=https_port,
                                tls_context=context,
                                path=path,
                                username=username,
                                source_ip=source_ip,
                                hold=True,
                                body=(
                                    two_hop_body
                                    if path == "/api/search" and index == 0
                                    else b"{}"
                                ),
                                timeout=20,
                            )
                        except BaseException as exc:
                            held_results[index] = exc

                    held_threads = [
                        threading.Thread(
                            target=held_search,
                            args=(index, username, source_ip),
                            daemon=True,
                        )
                        for index, (username, source_ip) in enumerate(held_clients)
                    ]
                    for thread in held_threads:
                        thread.start()
                    try:
                        self.assertTrue(
                            _BackendHandler.held_searches_ready.wait(timeout=5),
                            "two proxied Search requests did not reach the backend",
                        )
                        rejected_username, rejected_source_ip = rejected_client
                        self.assertEqual(
                            post_search(
                                port=https_port,
                                tls_context=context,
                                path=path,
                                username=rejected_username,
                                source_ip=rejected_source_ip,
                            ),
                            429,
                        )
                    finally:
                        _BackendHandler.release_held_searches.set()
                        for thread in held_threads:
                            thread.join(timeout=10)
                    self.assertTrue(
                        all(not thread.is_alive() for thread in held_threads)
                    )
                    self.assertEqual(held_results, [200, 200])
                    with _BackendHandler.state_lock:
                        posts_after = _BackendHandler.post_requests_seen
                    self.assertEqual(posts_after - posts_before, 2)

                # Distinct valid users share one source address, so only the
                # preserved address-keyed connection zone can reject the third.
                exercise_connection_limit(
                    path="/api/search",
                    held_clients=(
                        ("ip-conn-1", "127.0.0.1"),
                        ("ip-conn-2", "127.0.0.1"),
                    ),
                    rejected_client=("ip-conn-3", "127.0.0.1"),
                )
                # One valid user connects from distinct loopback addresses, so
                # only the new username-keyed zone can reject the third stream.
                exercise_connection_limit(
                    path="/api/search/stream",
                    held_clients=(
                        ("user-conn", "127.0.0.2"),
                        ("user-conn", "127.0.0.3"),
                    ),
                    rejected_client=("user-conn", "127.0.0.4"),
                )
                connection_error_log = root / "proxy-error.log"
                deadline = time.monotonic() + 5
                connection_error_text = ""
                while time.monotonic() < deadline:
                    if connection_error_log.is_file():
                        connection_error_text = connection_error_log.read_text(
                            encoding="utf-8"
                        )
                    if all(
                        f'zone "{zone}"' in connection_error_text
                        for zone in (
                            "wpg_search_connections",
                            "wpg_search_user_connections",
                        )
                    ):
                        break
                    time.sleep(0.05)
                self.assertIn(
                    'zone "wpg_search_connections"', connection_error_text
                )
                self.assertIn(
                    'zone "wpg_search_user_connections"', connection_error_text
                )
                self.assertEqual(_BackendHandler.post_requests_seen, 4)
                with _BackendHandler.state_lock:
                    bodies_seen = list(_BackendHandler.request_bodies_seen)
                self.assertEqual(bodies_seen.count(two_hop_body), 1)
                self.assertIsNone(_BackendHandler.authenticated_user_seen)
                self.assertIsNone(_BackendHandler.internal_token_seen)
                self.assertIsNone(_BackendHandler.internal_client_addr_seen)
                self.assertNotEqual(
                    _BackendHandler.forwarded_for_seen, "203.0.113.77"
                )
            finally:
                _BackendHandler.release_held_searches.set()
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)

            # Start a fresh master with both checked-in 6r/m, burst=2 request
            # buckets. Serial requests cannot hit either connection limit.
            rate_root = root / "fixed-rate"
            rate_root.mkdir()
            rate_http_port = self._unused_port()
            rate_https_port = self._unused_port()
            while rate_https_port == rate_http_port:
                rate_https_port = self._unused_port()
            rate_authenticated_gate_port = self._unused_port()
            while rate_authenticated_gate_port in {
                rate_http_port,
                rate_https_port,
                backend.server_address[1],
            }:
                rate_authenticated_gate_port = self._unused_port()
            rate_rendered = manage_deployment.render_template(
                manage_deployment.NGINX_TEMPLATE,
                {
                    "UPSTREAM_PORT": str(backend.server_address[1]),
                    "AUTHENTICATED_GATE_PORT": str(rate_authenticated_gate_port),
                    "SERVER_NAME": "localhost",
                    "TLS_CERTIFICATE": certificate,
                    "TLS_CERTIFICATE_KEY": private_key,
                    "HTPASSWD": htpasswd,
                    "BACKEND_API_TOKEN": backend_api_token,
                },
            ).decode("utf-8")
            self.assertIn(
                "limit_req_zone $binary_remote_addr "
                "zone=wpg_auth_attempts:10m rate=30r/m;",
                rate_rendered,
            )
            self.assertIn(
                "limit_req zone=wpg_auth_attempts burst=20 nodelay;",
                rate_rendered,
            )
            self.assertIn(
                "limit_req_zone $binary_remote_addr "
                "zone=wpg_search:10m rate=6r/m;",
                rate_rendered,
            )
            self.assertIn(
                "limit_req zone=wpg_search burst=2 nodelay;",
                rate_rendered,
            )
            self.assertIn(
                "limit_req_zone $http_x_wpg_authenticated_user "
                "zone=wpg_search_user:10m rate=6r/m;",
                rate_rendered,
            )
            self.assertIn(
                "limit_req zone=wpg_search_user burst=2 nodelay;",
                rate_rendered,
            )
            # Keep the checked-in Search buckets exact while shortening only
            # the global all-path auth-attempt proof to seven requests.
            rate_rendered = rate_rendered.replace(
                "zone=wpg_auth_attempts:10m rate=30r/m;",
                "zone=wpg_auth_attempts:10m rate=1r/m;",
            )
            rate_rendered = rate_rendered.replace(
                "limit_req zone=wpg_auth_attempts burst=20 nodelay;",
                "limit_req zone=wpg_auth_attempts burst=5 nodelay;",
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
                with _BackendHandler.state_lock:
                    posts_before_rate_test = _BackendHandler.post_requests_seen
                # Health and a path that would otherwise be 404 share the
                # server-level pre-authentication IP bucket. Wrong passwords
                # never reach the backend and the seventh rapid attempt is 429.
                auth_attempt_statuses = [
                    get_path_status(
                        port=rate_https_port,
                        tls_context=rate_context,
                        path=(
                            "/api/health/ready"
                            if index % 2
                            else "/would-otherwise-be-404"
                        ),
                        username="test-user",
                        password_value="wrong-password",
                        source_ip="127.0.0.40",
                    )
                    for index in range(7)
                ]
                self.assertEqual(auth_attempt_statuses, [401] * 6 + [429])
                # Distinct claimed valid users with wrong passwords share one
                # address. The outer IP bucket must cap Basic password work;
                # the fourth request is rejected before authentication.
                brute_force_statuses = [
                    post_search(
                        port=rate_https_port,
                        tls_context=rate_context,
                        path="/api/search",
                        username=f"ip-rate-{index}",
                        password_value="wrong-password",
                        source_ip="127.0.0.30",
                    )
                    for index in range(1, 5)
                ]
                self.assertEqual(brute_force_statuses, [401, 401, 401, 429])
                # Four distinct users share one address. Every username bucket
                # is fresh, so the fourth ordinary Search proves the IP bucket.
                ip_rate_statuses = [
                    post_search(
                        port=rate_https_port,
                        tls_context=rate_context,
                        path="/api/search",
                        username=f"ip-rate-{index}",
                    )
                    for index in range(1, 5)
                ]
                self.assertEqual(ip_rate_statuses, [200, 200, 200, 429])
                # Wrong passwords for a real username must finish in the outer
                # ACCESS phase. Distinct IPs isolate this assertion from the IP
                # bucket; none may reach or poison the internal user-rate bucket.
                unauthenticated_statuses = [
                    post_search(
                        port=rate_https_port,
                        tls_context=rate_context,
                        path="/api/search/stream",
                        username="user-rate",
                        password_value="wrong-password",
                        source_ip=f"127.0.0.{index}",
                    )
                    for index in range(20, 24)
                ]
                self.assertEqual(unauthenticated_statuses, [401, 401, 401, 401])
                # One user then uses four fresh source addresses. Every address
                # bucket is fresh, so the fourth stream proves the user bucket.
                user_rate_statuses = [
                    post_search(
                        port=rate_https_port,
                        tls_context=rate_context,
                        path="/api/search/stream?handoff=preserved",
                        username="user-rate",
                        source_ip=f"127.0.0.{index}",
                    )
                    for index in range(10, 14)
                ]
                self.assertEqual(user_rate_statuses, [200, 200, 200, 429])
                rate_statuses = (
                    auth_attempt_statuses
                    + brute_force_statuses
                    + ip_rate_statuses
                    + unauthenticated_statuses
                    + user_rate_statuses
                )
                with _BackendHandler.state_lock:
                    posts_after_rate_test = _BackendHandler.post_requests_seen
                self.assertEqual(
                    posts_after_rate_test - posts_before_rate_test,
                    6,
                )
                with _BackendHandler.state_lock:
                    proxied_paths = list(_BackendHandler.paths_seen)
                self.assertIn("/api/search", proxied_paths)
                self.assertIn("/api/search/stream", proxied_paths)
                self.assertEqual(
                    proxied_paths.count(
                        "/api/search/stream?handoff=preserved"
                    ),
                    3,
                )
                self.assertIsNone(_BackendHandler.authenticated_user_seen)
                self.assertIsNone(_BackendHandler.internal_token_seen)
                self.assertIsNone(_BackendHandler.internal_client_addr_seen)

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
                        and "zone \"wpg_auth_attempts\"" in rate_error_text
                        and "zone \"wpg_search\"" in rate_error_text
                        and "zone \"wpg_search_user\"" in rate_error_text
                    ):
                        break
                    time.sleep(0.05)
                self.assertEqual(
                    [record.get("status") for record in rate_records],
                    rate_statuses,
                )
                self.assertIn("limiting requests", rate_error_text)
                self.assertIn('zone "wpg_auth_attempts"', rate_error_text)
                self.assertIn('zone "wpg_search"', rate_error_text)
                self.assertIn('zone "wpg_search_user"', rate_error_text)
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
