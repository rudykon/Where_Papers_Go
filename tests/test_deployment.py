from __future__ import annotations

from argparse import Namespace
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase

from scripts import manage_deployment


class DeploymentManifestTests(TestCase):
    def test_systemd_manifest_is_restartable_hardened_and_health_gated(self) -> None:
        text = manage_deployment.SYSTEMD_TEMPLATE.read_text(encoding="utf-8")
        required = (
            "Restart=on-failure",
            "WantedBy=default.target",
            "EnvironmentFile=@@ENV_FILE@@",
            "ExecStartPost=",
            "/api/health",
            "NoNewPrivileges=yes",
            "ProtectSystem=strict",
            "ProtectHome=read-only",
            "ReadWritePaths=@@DATA_DIR@@",
            "Environment=WPG_DATA_DIR=@@DATA_DIR@@",
            "Environment=WPG_API_CONFIG=@@CONFIG_PATH@@",
            "UMask=0077",
            "KillSignal=SIGINT",
        )
        for value in required:
            self.assertIn(value, text)
        self.assertNotIn("api_key", text.casefold())
        self.assertNotIn("llmapi.json", text)
        self.assertNotIn("CapabilityBoundingSet", text)
        self.assertNotIn("ProtectHostname", text)
        for incompatible in (
            "PrivateDevices",
            "ProtectClock",
            "ProtectKernelLogs",
            "ProtectKernelModules",
        ):
            self.assertNotIn(incompatible, text)

    def test_systemd_render_is_dry_run_then_backs_up_existing_file(self) -> None:
        with TemporaryDirectory() as directory:
            output = Path(directory) / "where-papers-go.service"
            args = Namespace(
                template=manage_deployment.SYSTEMD_TEMPLATE,
                project_root=manage_deployment.PROJECT_ROOT,
                python=Path(sys.executable),
                data_dir=manage_deployment.PROJECT_ROOT / "data",
                api_config=manage_deployment.PROJECT_ROOT / "llmapi.json",
                environment_file="%h/.config/where-papers-go/runtime.env",
                output=output,
                apply=False,
            )
            dry_run = manage_deployment.render_systemd(args)
            self.assertEqual(dry_run["status"], "dry-run")
            self.assertTrue(dry_run["changed"])
            self.assertFalse(output.exists())

            output.write_text("preserved predecessor\n", encoding="utf-8")
            args.apply = True
            installed = manage_deployment.render_systemd(args)
            self.assertEqual(installed["status"], "installed")
            backup = Path(installed["backup"])
            self.assertEqual(backup.read_text(encoding="utf-8"), "preserved predecessor\n")
            rendered = output.read_text(encoding="utf-8")
            self.assertIn(str(manage_deployment.PROJECT_ROOT), rendered)
            self.assertIn(str(Path(sys.executable).resolve()), rendered)
            self.assertNotIn("@@", rendered)

            unchanged = manage_deployment.render_systemd(args)
            self.assertIsNone(unchanged["backup"])

            restored = manage_deployment.restore_file(
                Namespace(
                    source=backup,
                    output=output,
                    mode=0o644,
                    apply=True,
                )
            )
            self.assertEqual(restored["status"], "installed")
            self.assertEqual(output.read_text(encoding="utf-8"), "preserved predecessor\n")
            self.assertTrue(Path(restored["backup"]).is_file())

    def test_nginx_template_requires_tls_auth_limits_and_redacted_audit(self) -> None:
        payload = manage_deployment.render_template(
            manage_deployment.NGINX_TEMPLATE,
            {
                "UPSTREAM_PORT": "8001",
                "SERVER_NAME": "papers.example.org",
                "TLS_CERTIFICATE": "/etc/ssl/papers/fullchain.pem",
                "TLS_CERTIFICATE_KEY": "/etc/ssl/papers/privkey.pem",
                "HTPASSWD": "/etc/nginx/where-papers-go.htpasswd",
            },
        ).decode("utf-8")
        for value in (
            "listen 443 ssl",
            "ssl_protocols TLSv1.2 TLSv1.3",
            "auth_basic_user_file",
            "limit_req zone=wpg_search",
            "access_log /var/log/nginx/where-papers-go.access.json wpg_json",
            'proxy_set_header Authorization ""',
            "Strict-Transport-Security",
            "proxy_buffering off",
        ):
            self.assertIn(value, payload)
        self.assertNotIn("$request_body", payload)
        self.assertNotIn("$http_authorization", payload)
        self.assertNotIn("@@", payload)

    def test_runtime_environment_is_private_nonsecret_and_atomic(self) -> None:
        with TemporaryDirectory() as directory:
            output = Path(directory) / "runtime.env"
            args = Namespace(
                output=output,
                host="0.0.0.0",
                port=8001,
                data_dir=manage_deployment.PROJECT_ROOT / "data",
                api_config=manage_deployment.PROJECT_ROOT / "llmapi.json",
                rate_limit_requests=6,
                rate_limit_window_seconds=60,
                max_concurrent_searches=2,
                request_body_limit=200_000,
                request_read_timeout=30,
                audit_log=True,
                trust_proxy=False,
                trusted_proxy_cidrs="127.0.0.0/8,::1/128",
                require_api_auth=False,
                api_token_file=None,
                apply=True,
            )
            result = manage_deployment.render_environment(args)

            self.assertEqual(result["status"], "installed")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            text = output.read_text(encoding="utf-8")
            self.assertIn("WPG_HOST=0.0.0.0", text)
            self.assertIn("WPG_AUDIT_LOG=1", text)
            self.assertNotIn("api_key", text.casefold())
            self.assertNotIn("token", text.casefold())

    def test_health_contract_fails_each_missing_mandatory_layer(self) -> None:
        healthy = {
            "status": "ready",
            "ready": True,
            "backend": manage_deployment.EXPECTED_BACKEND,
            "graph": {"exists": True},
            "vectors": {"exists": True},
            "lightrag": {
                "exists": True,
                "manifest_exists": True,
                "mode": "mix",
            },
            "runtime": {"process_ready": True, "bindings_current": True},
            "config": {"ready": True},
        }
        self.assertEqual(manage_deployment.validate_health_payload(healthy), [])

        for top_level, field in (
            ("graph", "exists"),
            ("vectors", "exists"),
            ("lightrag", "manifest_exists"),
            ("runtime", "bindings_current"),
            ("config", "ready"),
        ):
            broken = json.loads(json.dumps(healthy))
            broken[top_level][field] = False
            self.assertTrue(
                manage_deployment.validate_health_payload(broken),
                msg=f"{top_level}.{field} must fail closed",
            )


if __name__ == "__main__":
    __import__("unittest").main()
