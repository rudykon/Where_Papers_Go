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
    request_id_seen: str | None = None

    def do_GET(self) -> None:  # noqa: N802 - stdlib HTTP server API
        type(self).authorization_seen = self.headers.get("Authorization")
        type(self).forwarded_for_seen = self.headers.get("X-Forwarded-For")
        type(self).request_id_seen = self.headers.get("X-Request-ID")
        body = json.dumps({"proxied": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-ID", "backend-test-request-id")
        self.end_headers()
        self.wfile.write(body)

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
        nginx = os.environ.get("WPG_NGINX_BIN") or shutil.which("nginx")
        openssl = shutil.which("openssl")
        if not nginx:
            self.skipTest("Nginx is not installed; set WPG_NGINX_BIN after admin install")
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
        _BackendHandler.request_id_seen = None

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
                },
            ).decode("utf-8")
            rendered = rendered.replace("listen 80;", f"listen 127.0.0.1:{http_port};")
            rendered = rendered.replace("listen [::]:80;", "")
            rendered = rendered.replace(
                "listen 443 ssl;", f"listen 127.0.0.1:{https_port} ssl;"
            )
            rendered = rendered.replace("listen [::]:443 ssl;", "")
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

                context = ssl._create_unverified_context()
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
                )
                url = f"https://127.0.0.1:{https_port}/api/health/live"
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    opener.open(url, timeout=5)
                try:
                    self.assertEqual(denied.exception.code, 401)
                finally:
                    denied.exception.close()

                credential = base64.b64encode(b"test-user:test-password").decode("ascii")
                request = urllib.request.Request(
                    url,
                    headers={
                        "Authorization": "Basic " + credential,
                        "Host": "localhost",
                        "X-Forwarded-For": "203.0.113.77",
                        "X-Request-ID": "client-forged-request-id",
                    },
                )
                with opener.open(request, timeout=5) as response:
                    payload = json.load(response)
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["X-Frame-Options"], "DENY")
                    self.assertEqual(
                        response.headers["X-Request-ID"], "backend-test-request-id"
                    )
                self.assertEqual(payload, {"proxied": True})
                self.assertIsNone(_BackendHandler.authorization_seen)
                self.assertEqual(_BackendHandler.forwarded_for_seen, "127.0.0.1")
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
                self.assertEqual(len(successful), 1)
                self.assertEqual(
                    successful[0]["request_id"], _BackendHandler.request_id_seen
                )
                self.assertEqual(
                    successful[0]["upstream_request_id"],
                    "backend-test-request-id",
                )
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)


if __name__ == "__main__":
    __import__("unittest").main()
