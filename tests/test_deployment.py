from __future__ import annotations

from argparse import Namespace
import json
from pathlib import Path
import os
import stat
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from scripts import manage_deployment
from where_paper_go.tavily_pool import TavilyKeyPool


class DeploymentManifestTests(TestCase):
    @staticmethod
    def _runtime_shadow(root: Path, *, data_dir: Path | None = None) -> Path:
        runtime = root / "runtime-root" / "generations" / "generation-test"
        lightrag = runtime / "lightrag_storage"
        lightrag.mkdir(parents=True, mode=0o700)
        files = []
        for name in manage_deployment.RUNTIME_LIGHTRAG_FILES:
            path = lightrag / name
            path.write_bytes((name + "\n").encode("utf-8"))
            path.chmod(0o600)
            files.append(
                {
                    "runtime_path": f"lightrag_storage/{name}",
                    "bytes": path.stat().st_size,
                    "sha256": manage_deployment.sha256_file(path),
                }
            )
        (runtime / "api_cache").mkdir(mode=0o700)
        manifest = runtime / manage_deployment.RUNTIME_MANIFEST
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "artifact_type": "where_papers_go_runtime_shadow",
                    "source_data_dir": str(
                        (data_dir or manage_deployment.PROJECT_ROOT / "data").resolve()
                    ),
                    "source_binding_sha256": "0" * 64,
                    "protected_sources_never_replaced": True,
                    "files": files,
                }
            ),
            encoding="utf-8",
        )
        manifest.chmod(0o400)
        runtime.chmod(0o700)
        runtime.parent.chmod(0o700)
        runtime.parent.parent.chmod(0o700)
        return runtime

    @staticmethod
    def _shared_state(root: Path) -> tuple[Path, Path]:
        shared = root / "runtime-root" / "shared"
        shared.mkdir(parents=True, mode=0o700, exist_ok=True)
        shared.chmod(0o700)
        shared.parent.chmod(0o700)
        config = root / "api-config.json"
        config.write_text(
            json.dumps(
                {
                    "search": {
                        "provider": "tavily",
                        "api_keys": ["unit-test-placeholder-key"],
                        "quota_per_key": 10,
                    }
                }
            ),
            encoding="utf-8",
        )
        TavilyKeyPool(
            ["unit-test-placeholder-key"],
            quota_per_key=10,
            state_file=shared / manage_deployment.TAVILY_STATE_NAME,
        ).summary()
        return shared, config

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
            "ReadOnlyPaths=@@DATA_DIR@@",
            "ReadWritePaths=@@RUNTIME_DIR@@",
            "ReadWritePaths=@@SHARED_STATE_DIR@@",
            "ReadOnlyPaths=@@RUNTIME_DIR@@/runtime-shadow-manifest.json",
            "Environment=WPG_TAVILY_STATE_FILE=@@SHARED_STATE_DIR@@/",
            "Environment=WPG_RUNTIME_MANIFEST_SHA256=@@RUNTIME_MANIFEST_SHA256@@",
            "Environment=WPG_DATA_DIR=@@DATA_DIR@@",
            "Environment=WPG_API_CONFIG=@@CONFIG_PATH@@",
            "Environment=WPG_MAX_CONCURRENT_CONNECTIONS=64",
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
            root = Path(directory)
            output = root / "where-papers-go.service"
            runtime = self._runtime_shadow(root)
            shared, config = self._shared_state(root)
            args = Namespace(
                template=manage_deployment.SYSTEMD_TEMPLATE,
                project_root=manage_deployment.PROJECT_ROOT,
                python=Path(sys.executable),
                data_dir=manage_deployment.PROJECT_ROOT / "data",
                api_config=config,
                environment_file="%h/.config/where-papers-go/runtime.env",
                runtime_dir=runtime,
                shared_state_dir=shared,
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
            restore_backup = Path(restored["backup"])
            self.assertTrue(restore_backup.is_file())
            self.assertEqual(restore_backup.read_text(encoding="utf-8"), rendered)
            self.assertEqual(output.stat().st_mode & 0o777, 0o644)

    def test_atomic_install_replace_failure_keeps_active_predecessor(self) -> None:
        with TemporaryDirectory() as directory:
            output = Path(directory) / "active.conf"
            output.write_bytes(b"active predecessor\n")
            output.chmod(0o640)
            with patch.object(
                manage_deployment.os,
                "replace",
                side_effect=OSError("simulated final namespace switch failure"),
            ), self.assertRaisesRegex(OSError, "simulated final"):
                manage_deployment.atomic_install(
                    output, b"candidate replacement\n", mode=0o600
                )

            self.assertEqual(output.read_bytes(), b"active predecessor\n")
            self.assertEqual(output.stat().st_mode & 0o777, 0o640)
            backups = list(Path(directory).glob("active.conf.backup-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), b"active predecessor\n")
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o640)
            self.assertEqual(list(Path(directory).glob(".active.conf.*.tmp")), [])

    def test_atomic_install_fsyncs_backup_before_replace_and_new_name_after(self) -> None:
        with TemporaryDirectory() as directory:
            output = Path(directory) / "active.conf"
            output.write_bytes(b"predecessor")
            events: list[str] = []
            real_replace = os.replace

            def replace(source, target):
                events.append("replace")
                return real_replace(source, target)

            with (
                patch.object(
                    manage_deployment,
                    "_fsync_directory",
                    side_effect=lambda _path: events.append("fsync"),
                ),
                patch.object(manage_deployment.os, "replace", side_effect=replace),
            ):
                manage_deployment.atomic_install(output, b"replacement", mode=0o600)

            self.assertEqual(events, ["fsync", "replace", "fsync"])

    def test_atomic_install_fsyncs_requested_mode_and_unchanged_mode(self) -> None:
        with TemporaryDirectory() as directory:
            output = Path(directory) / "active.conf"
            fsync_calls: list[int] = []
            real_fsync = os.fsync

            def fsync(descriptor: int) -> None:
                fsync_calls.append(descriptor)
                real_fsync(descriptor)

            with patch.object(manage_deployment.os, "fsync", side_effect=fsync):
                manage_deployment.atomic_install(output, b"payload", mode=0o640)
                first_count = len(fsync_calls)
                manage_deployment.atomic_install(output, b"payload", mode=0o600)

            self.assertGreaterEqual(first_count, 2)
            self.assertGreater(len(fsync_calls), first_count)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_atomic_install_copy_fallback_preserves_predecessor_mode(self) -> None:
        with TemporaryDirectory() as directory:
            output = Path(directory) / "active.conf"
            output.write_bytes(b"predecessor")
            output.chmod(0o640)
            with patch.object(
                manage_deployment.os,
                "link",
                side_effect=OSError("simulated hard-link unavailability"),
            ):
                backup = manage_deployment.atomic_install(
                    output, b"replacement", mode=0o600
                )

            self.assertIsNotNone(backup)
            assert backup is not None
            self.assertEqual(backup.read_bytes(), b"predecessor")
            self.assertEqual(backup.stat().st_mode & 0o777, 0o640)
            self.assertEqual(output.read_bytes(), b"replacement")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_atomic_install_surfaces_directory_fsync_failure(self) -> None:
        with TemporaryDirectory() as directory:
            output = Path(directory) / "active.conf"
            output.write_bytes(b"predecessor")
            with (
                patch.object(
                    manage_deployment,
                    "_fsync_directory",
                    side_effect=[None, OSError("simulated directory fsync failure")],
                ),
                self.assertRaisesRegex(OSError, "directory fsync"),
            ):
                manage_deployment.atomic_install(output, b"replacement", mode=0o600)

            self.assertEqual(output.read_bytes(), b"replacement")
            backups = list(Path(directory).glob("active.conf.backup-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), b"predecessor")

    def test_atomic_install_refuses_dangling_symlink(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "missing-target"
            output = root / "active.conf"
            output.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "non-regular"):
                manage_deployment.atomic_install(output, b"replacement", mode=0o600)
            self.assertTrue(output.is_symlink())
            self.assertFalse(target.exists())

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
            '"upstream_request_id":"$upstream_http_x_request_id"',
            'proxy_set_header Authorization ""',
            "Strict-Transport-Security",
            "proxy_buffering off",
        ):
            self.assertIn(value, payload)
        self.assertNotIn("$request_body", payload)
        self.assertNotIn("$http_authorization", payload)
        self.assertIn("proxy_set_header X-Forwarded-For $remote_addr;", payload)
        self.assertIn("return 301 https://papers.example.org$request_uri;", payload)
        self.assertNotIn("return 301 https://$host", payload)
        self.assertNotIn("$proxy_add_x_forwarded_for", payload)
        self.assertNotIn("@@", payload)

    def test_nginx_renderer_rejects_non_hostname_redirect_targets(self) -> None:
        base = {
            "template": manage_deployment.NGINX_TEMPLATE,
            "output": Path("/tmp/not-written.conf"),
            "tls_certificate": Path("/absolute/cert.pem"),
            "tls_certificate_key": Path("/absolute/key.pem"),
            "htpasswd": Path("/absolute/htpasswd"),
            "upstream_port": 8001,
            "apply": False,
        }
        for server_name in (
            "attacker.example good.example",
            "$host",
            "bad/name",
            "测试.example",
        ):
            with self.subTest(server_name=server_name), self.assertRaisesRegex(
                ValueError, "server-name"
            ):
                manage_deployment.render_nginx(
                    Namespace(server_name=server_name, **base)
                )

    def test_runtime_environment_is_private_nonsecret_and_atomic(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "runtime.env"
            runtime = self._runtime_shadow(root)
            shared, config = self._shared_state(root)
            args = Namespace(
                output=output,
                host="0.0.0.0",
                port=8001,
                data_dir=manage_deployment.PROJECT_ROOT / "data",
                api_config=config,
                runtime_dir=runtime,
                shared_state_dir=shared,
                rate_limit_requests=6,
                rate_limit_window_seconds=60,
                max_concurrent_connections=64,
                max_concurrent_searches=2,
                request_body_limit=200_000,
                request_read_timeout=30,
                audit_log=True,
                allowed_client_cidrs="127.0.0.0/8,172.22.13.155/24",
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
            self.assertIn("WPG_MAX_CONCURRENT_CONNECTIONS=64", text)
            self.assertIn(f"WPG_LIGHTRAG_WORKING_DIR={runtime / 'lightrag_storage'}", text)
            self.assertIn(
                f"WPG_TAVILY_STATE_FILE={shared / manage_deployment.TAVILY_STATE_NAME}",
                text,
            )
            self.assertIn(f"WPG_RUNTIME_GENERATION={runtime}", text)
            self.assertIn("WPG_STRICT_GRAPH_READ_ONLY=1", text)
            self.assertIn("WPG_REQUIRE_RUNTIME_SHADOW=1", text)
            self.assertIn(
                "WPG_ALLOWED_CLIENT_CIDRS=127.0.0.0/8,172.22.13.0/24", text
            )
            self.assertIn("WPG_AUDIT_LOG=1", text)
            self.assertNotIn("api_key", text.casefold())
            self.assertNotIn("token", text.casefold())

            args.host = "192.0.2.5"
            with self.assertRaisesRegex(ValueError, "loopback health gate"):
                manage_deployment.render_environment(args)

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
            "runtime": {
                "process_ready": True,
                "bindings_current": True,
                "write_isolated": True,
                "tavily_state_shared": True,
                "ready": True,
                "worker_bindings": {"exact_match": True},
                "runtime_manifest": {
                    "ready": True,
                    "sha256_matched": True,
                    "path_bound": True,
                },
            },
            "config": {
                "ready": True,
                "search_quota_audit": {
                    "ready": True,
                    "replicated_revision": True,
                    "configuration_current": True,
                },
            },
        }
        self.assertEqual(manage_deployment.validate_health_payload(healthy), [])

        for top_level, field in (
            ("graph", "exists"),
            ("vectors", "exists"),
            ("lightrag", "manifest_exists"),
            ("runtime", "bindings_current"),
            ("runtime", "write_isolated"),
            ("config", "ready"),
        ):
            broken = json.loads(json.dumps(healthy))
            broken[top_level][field] = False
            self.assertTrue(
                manage_deployment.validate_health_payload(broken),
                msg=f"{top_level}.{field} must fail closed",
            )

    def test_prepare_runtime_is_private_atomic_and_preserves_prior_generation(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "source-data"
            lightrag = data / "lightrag_storage"
            lightrag.mkdir(parents=True)
            for index, name in enumerate(manage_deployment.RUNTIME_LIGHTRAG_FILES):
                (lightrag / name).write_bytes(f"seed-{index}\n".encode())
            (data / ".query_embedding_cache.json.gz").write_bytes(b"query-seed")
            (data / ".embedding_cache.json.gz").write_bytes(b"rag-seed")
            api = data / ".query_api_cache" / "nested"
            api.mkdir(parents=True)
            (api / "cache.json").write_bytes(b"api-seed")
            runtime_root = root / "runtime-root"
            config = root / "api-config.json"
            config.write_text(
                json.dumps(
                    {
                        "search": {
                            "provider": "tavily",
                            "api_keys": ["unit-test-placeholder-key"],
                            "quota_per_key": 10,
                        }
                    }
                ),
                encoding="utf-8",
            )
            args = Namespace(
                data_dir=data,
                runtime_root=runtime_root,
                shared_state_dir=None,
                api_config=config,
                apply=False,
            )
            dry = manage_deployment.prepare_runtime(args)
            self.assertEqual(dry["status"], "dry-run")
            self.assertFalse(runtime_root.exists())

            source_hashes = {
                path.relative_to(data).as_posix(): manage_deployment.sha256_file(path)
                for path in data.rglob("*")
                if path.is_file()
            }
            args.apply = True
            first = manage_deployment.prepare_runtime(args)
            generation = Path(first["generation"])
            self.assertEqual(first["status"], "built-not-active")
            self.assertFalse(os.path.lexists(runtime_root / "current"))
            self.assertEqual(stat.S_IMODE(generation.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((generation / manage_deployment.RUNTIME_MANIFEST).stat().st_mode),
                0o400,
            )
            self.assertEqual(
                (generation / "api_cache" / "nested" / "cache.json").read_bytes(),
                b"api-seed",
            )
            for path in generation.rglob("*"):
                if path.is_file() and path.name != manage_deployment.RUNTIME_MANIFEST:
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(
                source_hashes,
                {
                    path.relative_to(data).as_posix(): manage_deployment.sha256_file(path)
                    for path in data.rglob("*")
                    if path.is_file()
                },
            )

            second = manage_deployment.prepare_runtime(args)
            self.assertNotEqual(second["generation"], first["generation"])
            self.assertTrue(Path(first["generation"]).is_dir())
            shared = manage_deployment.prepare_shared_state(
                Namespace(
                    data_dir=data,
                    runtime_root=runtime_root,
                    shared_state_dir=None,
                    api_config=config,
                    apply=True,
                )
            )
            self.assertEqual(shared["status"], "installed")
            activation = manage_deployment.activate_runtime(
                Namespace(
                    generation=generation,
                    runtime_root=runtime_root,
                    data_dir=data,
                    expected_manifest_sha256=first["manifest_sha256"],
                    expected_current="none",
                    apply=True,
                )
            )
            self.assertEqual(activation["status"], "activated")
            self.assertEqual((runtime_root / "current").resolve(), generation)

            current = runtime_root / "current"
            environment = manage_deployment.render_environment(
                Namespace(
                    output=root / "runtime.env",
                    host="127.0.0.1",
                    port=8001,
                    data_dir=data,
                    api_config=config,
                    runtime_dir=current,
                    shared_state_dir=runtime_root / "shared",
                    rate_limit_requests=6,
                    rate_limit_window_seconds=60,
                    max_concurrent_connections=64,
                    max_concurrent_searches=2,
                    request_body_limit=200_000,
                    request_read_timeout=30,
                    audit_log=True,
                    allowed_client_cidrs="127.0.0.0/8,::1/128",
                    trust_proxy=False,
                    trusted_proxy_cidrs="127.0.0.0/8,::1/128",
                    require_api_auth=False,
                    api_token_file=None,
                    apply=False,
                )
            )
            self.assertEqual(environment["status"], "dry-run")
            systemd = manage_deployment.render_systemd(
                Namespace(
                    template=manage_deployment.SYSTEMD_TEMPLATE,
                    project_root=manage_deployment.PROJECT_ROOT,
                    python=Path(sys.executable),
                    data_dir=data,
                    api_config=config,
                    environment_file="%h/.config/where-papers-go/runtime.env",
                    runtime_dir=current,
                    shared_state_dir=runtime_root / "shared",
                    output=root / "where-papers-go.service",
                    apply=False,
                )
            )
            self.assertEqual(systemd["status"], "dry-run")

            second_activation = manage_deployment.activate_runtime(
                Namespace(
                    generation=Path(second["generation"]),
                    runtime_root=runtime_root,
                    data_dir=data,
                    expected_manifest_sha256=second["manifest_sha256"],
                    expected_current=os.readlink(runtime_root / "current"),
                    apply=True,
                )
            )
            backup = Path(second_activation["previous_pointer_backup"])
            self.assertTrue(backup.is_symlink())
            self.assertEqual(backup.resolve(), Path(first["generation"]))


if __name__ == "__main__":
    __import__("unittest").main()
