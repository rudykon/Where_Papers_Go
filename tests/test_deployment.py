from __future__ import annotations

from argparse import Namespace
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import os
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import zipfile

from scripts import manage_deployment, monitor_operations
from where_paper_go import deployment_identity
from where_paper_go.tavily_pool import TavilyKeyPool


class DeploymentManifestTests(TestCase):
    def test_source_git_queries_ignore_host_overrides(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"value\n", stderr=b""
        )
        with (
            patch.dict(
                os.environ,
                {
                    "GIT_DIR": "/tmp/attacker",
                    "GIT_WORK_TREE": "/tmp/other",
                    "GIT_CONFIG_GLOBAL": "/tmp/config",
                    "PATH": "/tmp/bin",
                },
            ),
            patch.object(
                manage_deployment.subprocess,
                "run",
                return_value=completed,
            ) as run,
        ):
            self.assertEqual(
                manage_deployment._git_output(Path("/project"), "rev-parse", "HEAD"),
                b"value\n",
            )
        arguments = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertEqual(arguments[0], "/usr/bin/git")
        self.assertNotIn("GIT_DIR", environment)
        self.assertNotIn("GIT_WORK_TREE", environment)
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertEqual(environment["PATH"], "/usr/bin:/bin")

    @staticmethod
    def _source_release(root: Path) -> tuple[Path, str, dict[str, object]]:
        head = (
            manage_deployment._git_output(
                manage_deployment.PROJECT_ROOT,
                "rev-parse",
                "--verify",
                "HEAD^{commit}",
            )
            .decode("ascii")
            .strip()
            .casefold()
        )
        tree = (
            manage_deployment._git_output(
                manage_deployment.PROJECT_ROOT,
                "rev-parse",
                "--verify",
                "HEAD^{tree}",
            )
            .decode("ascii")
            .strip()
            .casefold()
        )
        contents = {
            manage_deployment.MONITOR_RENDERER_RELATIVE.as_posix(): (
                manage_deployment.PROJECT_ROOT
                / manage_deployment.MONITOR_RENDERER_RELATIVE
            ).read_bytes(),
            "where_paper_go/web_app.py": b"# immutable web entrypoint\n",
            manage_deployment.MONITOR_POLICY_RELATIVE.as_posix(): (
                manage_deployment.PROJECT_ROOT
                / manage_deployment.MONITOR_POLICY_RELATIVE
            ).read_bytes(),
            manage_deployment.MONITOR_SYSTEMD_SERVICE_RELATIVE.as_posix(): (
                manage_deployment.PROJECT_ROOT
                / manage_deployment.MONITOR_SYSTEMD_SERVICE_RELATIVE
            ).read_bytes(),
            manage_deployment.MONITOR_SYSTEMD_TIMER_RELATIVE.as_posix(): (
                manage_deployment.PROJECT_ROOT
                / manage_deployment.MONITOR_SYSTEMD_TIMER_RELATIVE
            ).read_bytes(),
            manage_deployment.MONITOR_SCRIPT_RELATIVE.as_posix(): (
                manage_deployment.PROJECT_ROOT
                / manage_deployment.MONITOR_SCRIPT_RELATIVE
            ).read_bytes(),
            manage_deployment.SYSTEMD_TEMPLATE.relative_to(
                manage_deployment.PROJECT_ROOT
            ).as_posix(): manage_deployment.SYSTEMD_TEMPLATE.read_bytes(),
        }
        rows = [
            {
                "path": name,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "mode": "0444",
            }
            for name, payload in sorted(contents.items())
        ]
        binding = hashlib.sha256(
            json.dumps(
                {"source_head": head, "source_tree": tree, "files": rows},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        manifest: dict[str, object] = {
            "schema_version": 1,
            "artifact_type": deployment_identity.SOURCE_ARTIFACT_TYPE,
            "source_head": head,
            "source_tree": tree,
            "source_binding_sha256": binding,
            "file_count": len(rows),
            "files": rows,
            "immutable_files": True,
            "forbidden_entries_excluded": True,
        }
        manifest_payload = (
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()
        release = root / "source-releases" / f"release-{manifest_sha256}"
        for name, payload in contents.items():
            path = release / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            path.chmod(0o444)
        source_manifest = release / deployment_identity.SOURCE_MANIFEST_FILE
        source_manifest.write_bytes(manifest_payload)
        source_manifest.chmod(0o400)
        for directory in sorted(
            [release, *(path for path in release.rglob("*") if path.is_dir())],
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            directory.chmod(0o555)
        return release, manifest_sha256, manifest

    @staticmethod
    def _runtime_shadow(
        root: Path, *, data_dir: Path | None = None, marker: str = ""
    ) -> Path:
        runtime = root / "runtime-root" / "generations" / "generation-test"
        lightrag = runtime / "lightrag_storage"
        lightrag.mkdir(parents=True, mode=0o700)
        files = []
        for name in manage_deployment.RUNTIME_LIGHTRAG_FILES:
            path = lightrag / name
            path.write_bytes((name + marker + "\n").encode("utf-8"))
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
            "Environment=WPG_REQUIRE_API_AUTH=1",
            "Environment=WPG_API_TOKEN_FILE=@@API_TOKEN_FILE@@",
            "Environment=WPG_TRUST_PROXY_HEADERS=1",
            "ExecStartPost=",
            "--token-file @@API_TOKEN_FILE@@",
            "--expect-process-pid ${MAINPID}",
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
            "Environment=WPG_PYTHON_RUNTIME=@@PYTHON_RUNTIME@@",
            "Environment=WPG_PYTHON_RUNTIME_MANIFEST=@@PYTHON_RUNTIME_MANIFEST@@",
            "Environment=WPG_PYTHON_RUNTIME_MANIFEST_SHA256=@@PYTHON_RUNTIME_MANIFEST_SHA256@@",
            "Environment=WPG_PYTHON_RUNTIME_TREE_SHA256=@@PYTHON_RUNTIME_TREE_SHA256@@",
            "WorkingDirectory=@@SOURCE_RELEASE@@",
            "UnsetEnvironment=GCONV_PATH GLIBC_TUNABLES LD_ASSUME_KERNEL LD_AUDIT",
            "ExecStart=/usr/bin/env -u GCONV_PATH -u GLIBC_TUNABLES "
            "-u LD_ASSUME_KERNEL -u LD_HWCAP_MASK "
            "-u PYTHONHOME -u PYTHONPLATLIBDIR "
            "-u LD_PRELOAD -u LD_LIBRARY_PATH -u LD_AUDIT "
            "-u OPENSSL_CONF -u OPENSSL_CONF_INCLUDE -u OPENSSL_ENGINES "
            "-u OPENSSL_MODULES -u SSLKEYLOGFILE -u SSL_CERT_FILE "
            "-u SSL_CERT_DIR -u REQUESTS_CA_BUNDLE -u CURL_CA_BUNDLE "
            "-u AWS_CA_BUNDLE -u GRPC_DEFAULT_SSL_ROOTS_FILE_PATH "
            "-u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u NO_PROXY "
            "-u http_proxy -u https_proxy -u all_proxy -u no_proxy "
            "-u PYTHONINSPECT -u PYTHONSTARTUP -u PYTHONWARNINGS "
            "-u PYTHONBREAKPOINT PATH=/usr/bin:/bin "
            "PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONSAFEPATH=1 "
            "PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1 UV_OFFLINE=1 "
            "HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 "
            "PYTHONPATH=@@SOURCE_RELEASE@@:@@PYTHON_IMPORT_PATH@@",
            "WPG_SOURCE_HEAD=@@SOURCE_HEAD@@",
            "WPG_SOURCE_TREE=@@SOURCE_TREE@@",
            "WPG_SOURCE_MANIFEST=@@SOURCE_MANIFEST@@",
            "WPG_SOURCE_MANIFEST_SHA256=@@SOURCE_MANIFEST_SHA256@@",
            "WPG_PYTHON_RUNTIME_MANIFEST_SHA256=@@PYTHON_RUNTIME_MANIFEST_SHA256@@",
            "ReadOnlyPaths=@@SOURCE_RELEASE@@",
            "ReadOnlyPaths=@@PYTHON_RUNTIME@@",
            "Environment=WPG_DATA_DIR=@@DATA_DIR@@",
            "Environment=WPG_API_CONFIG=@@CONFIG_PATH@@",
            "Environment=WPG_MAX_CONCURRENT_CONNECTIONS=64",
            "UMask=0077",
            "KillSignal=SIGINT",
        )
        for value in required:
            self.assertIn(value, text)
        self.assertNotIn("EnvironmentFile=", text)
        self.assertEqual(
            text.count("PYTHONPATH=@@SOURCE_RELEASE@@:@@PYTHON_IMPORT_PATH@@"),
            2,
        )
        command_tokens = [
            line.split()
            for line in text.splitlines()
            if line.startswith(("ExecStart=", "ExecStartPost="))
        ]
        for protected_name in (
            "GCONV_PATH",
            "GLIBC_TUNABLES",
            "LD_ASSUME_KERNEL",
            "LD_HWCAP_MASK",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "OPENSSL_CONF",
            "OPENSSL_CONF_INCLUDE",
            "OPENSSL_ENGINES",
            "OPENSSL_MODULES",
            "SSLKEYLOGFILE",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "REQUESTS_CA_BUNDLE",
            "CURL_CA_BUNDLE",
            "AWS_CA_BUNDLE",
            "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
            "PYTHONINSPECT",
            "PYTHONSTARTUP",
            "PYTHONWARNINGS",
            "PYTHONBREAKPOINT",
        ):
            self.assertIn(protected_name, text.split("UnsetEnvironment=", 1)[1])
            self.assertEqual(
                sum(
                    1
                    for tokens in command_tokens
                    for index, token in enumerate(tokens[:-1])
                    if token == "-u" and tokens[index + 1] == protected_name
                ),
                2,
            )
        for offline_binding in (
            "PIP_NO_INDEX=1",
            "PIP_DISABLE_PIP_VERSION_CHECK=1",
            "UV_OFFLINE=1",
            "HF_HUB_OFFLINE=1",
            "TRANSFORMERS_OFFLINE=1",
        ):
            self.assertEqual(text.count(offline_binding), 2)
        self.assertEqual(
            text.count(
                "--expect-sha256 @@PYTHON_RUNTIME_MANIFEST@@="
                "@@PYTHON_RUNTIME_MANIFEST_SHA256@@"
            ),
            1,
        )
        self.assertEqual(text.count("WPG_SOURCE_HEAD=@@SOURCE_HEAD@@"), 2)
        self.assertNotIn("Environment=PYTHONPATH=", text)
        self.assertNotIn("Environment=WPG_SOURCE_HEAD=", text)
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

        monitor_service = (
            manage_deployment.PROJECT_ROOT
            / manage_deployment.MONITOR_SYSTEMD_SERVICE_RELATIVE
        ).read_text(encoding="utf-8")
        main_unset = next(
            line for line in text.splitlines() if line.startswith("UnsetEnvironment=")
        )
        monitor_unset = next(
            line
            for line in monitor_service.splitlines()
            if line.startswith("UnsetEnvironment=")
        )
        self.assertEqual(monitor_unset, main_unset)
        self.assertEqual(
            tuple(monitor_unset.removeprefix("UnsetEnvironment=").split()),
            manage_deployment.EXEC_BOUNDARY_UNSET_ENVIRONMENT,
        )
        self.assertLess(
            monitor_service.index(monitor_unset),
            monitor_service.index("ExecStart=/usr/bin/env -i "),
        )
        for earlier, later in (
            ("GCONV_PATH", "GLIBC_TUNABLES"),
            ("LD_AUDIT", "LD_PRELOAD"),
            ("OPENSSL_CONF", "SSLKEYLOGFILE"),
            ("HTTP_PROXY", "http_proxy"),
            ("PYTHONBREAKPOINT", "PYTHONHOME"),
            ("PYTHONPATH", "PYTHONWARNINGS"),
        ):
            self.assertLess(
                manage_deployment.EXEC_BOUNDARY_UNSET_ENVIRONMENT.index(earlier),
                manage_deployment.EXEC_BOUNDARY_UNSET_ENVIRONMENT.index(later),
            )
        for value in (
            "Type=oneshot",
            "After=where-papers-go.service",
            "-m scripts.monitor_operations",
            "--source-manifest @@SOURCE_MANIFEST@@",
            "--python-runtime-manifest @@PYTHON_RUNTIME_MANIFEST@@",
            "--runtime-manifest @@RUNTIME_MANIFEST@@",
            "--expected-policy-sha256 @@MONITOR_POLICY_SHA256@@",
            "--expected-python-executable-sha256 @@PYTHON_EXECUTABLE_SHA256@@",
            "--expected-store-binding-sha256 @@LIGHTRAG_STORE_BINDING_SHA256@@",
            "SuccessExitStatus=2",
            "Restart=no",
            "TimeoutStartSec=300",
            "ProtectSystem=strict",
            "ProtectHome=read-only",
            "ReadWritePaths=@@MONITOR_STATE_DIR@@",
            "ReadOnlyPaths=@@SOURCE_RELEASE@@",
            "ReadOnlyPaths=@@PYTHON_RUNTIME@@",
            "ReadOnlyPaths=@@RUNTIME_MANIFEST@@",
        ):
            self.assertIn(value, monitor_service)
        monitor_exec = next(
            line
            for line in monitor_service.splitlines()
            if line.startswith("ExecStart=")
        )
        self.assertNotIn("/api/search", monitor_exec)
        self.assertNotIn("@@MONITOR_HEALTH_URL@@", monitor_service)
        monitor_timer = (
            manage_deployment.PROJECT_ROOT
            / manage_deployment.MONITOR_SYSTEMD_TIMER_RELATIVE
        ).read_text(encoding="utf-8")
        for value in (
            "OnCalendar=*-*-* *:*:00",
            "Persistent=true",
            "Unit=where-papers-go-monitor.service",
            "WantedBy=timers.target",
        ):
            self.assertIn(value, monitor_timer)
        monitor_policy = json.loads(
            (
                manage_deployment.PROJECT_ROOT
                / manage_deployment.MONITOR_POLICY_RELATIVE
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            monitor_policy["service"],
            {
                "unit": manage_deployment.MONITORED_SERVICE_NAME,
                "health_url": manage_deployment.MONITOR_HEALTH_URL,
                "health_timeout_seconds": 60,
            },
        )
        self.assertIsNone(
            monitor_policy["thresholds"]["search_latency_warning_ms"]
        )
        self.assertIsNone(
            monitor_policy["thresholds"]["search_latency_critical_ms"]
        )

        for invalid_json in (b'{"value":1,"value":2}', b'{"value":NaN}'):
            with self.assertRaisesRegex(
                monitor_operations.MonitorError, "STRICT_JSON_INVALID"
            ):
                monitor_operations.parse_json_bytes(
                    invalid_json, label="STRICT"
                )

        with TemporaryDirectory() as module_directory:
            module_root = Path(module_directory)
            source_module_root = module_root / "source"
            python_module_root = module_root / "python-runtime"
            source_module_payloads = {
                "scripts/manage_deployment.py": b"manage helper\n",
                "scripts/monitor_operations.py": b"monitor helper\n",
                "where_paper_go/deployment_identity.py": b"identity helper\n",
            }
            python_module_payloads = {
                "lib/python3.14/json/__init__.py": b"stdlib helper\n"
            }

            def module_inventory(
                root: Path, payloads: dict[str, bytes]
            ) -> dict[str, dict[str, object]]:
                inventory: dict[str, dict[str, object]] = {}
                for relative, payload in payloads.items():
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(payload)
                    path.chmod(0o444)
                    inventory[relative] = {
                        "path": relative,
                        "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "mode": "0444",
                    }
                return inventory

            source_module_inventory = module_inventory(
                source_module_root, source_module_payloads
            )
            python_module_inventory = module_inventory(
                python_module_root, python_module_payloads
            )
            loaded_modules = [
                *(
                    Namespace(__file__=str(source_module_root / relative))
                    for relative in source_module_payloads
                ),
                *(
                    Namespace(__file__=str(python_module_root / relative))
                    for relative in python_module_payloads
                ),
                Namespace(__file__="<frozen importlib._bootstrap>"),
            ]
            module_proof = monitor_operations._verify_loaded_module_files(
                source_root=source_module_root,
                python_runtime=python_module_root,
                source_inventory=source_module_inventory,
                python_inventory=python_module_inventory,
                modules=loaded_modules,
            )
            self.assertEqual(module_proof["loaded_module_file_count"], 4)
            self.assertEqual(module_proof["source_module_file_count"], 3)
            self.assertEqual(
                module_proof["python_runtime_module_file_count"], 1
            )
            self.assertRegex(
                module_proof["loaded_module_binding_sha256"],
                r"^[0-9a-f]{64}$",
            )
            wrong_hash_inventory = {
                name: dict(row) for name, row in source_module_inventory.items()
            }
            wrong_hash_inventory["scripts/manage_deployment.py"][
                "sha256"
            ] = "0" * 64
            with self.assertRaisesRegex(
                monitor_operations.MonitorError,
                "LOADED_MODULE_FILE_MISMATCH",
            ):
                monitor_operations._verify_loaded_module_files(
                    source_root=source_module_root,
                    python_runtime=python_module_root,
                    source_inventory=wrong_hash_inventory,
                    python_inventory=python_module_inventory,
                    modules=loaded_modules,
                )
            missing_row_inventory = dict(source_module_inventory)
            del missing_row_inventory["scripts/manage_deployment.py"]
            with self.assertRaisesRegex(
                monitor_operations.MonitorError,
                "LOADED_MODULE_MANIFEST_MISSING",
            ):
                monitor_operations._verify_loaded_module_files(
                    source_root=source_module_root,
                    python_runtime=python_module_root,
                    source_inventory=missing_row_inventory,
                    python_inventory=python_module_inventory,
                    modules=loaded_modules,
                )
            outside_module = module_root / "mutable-outside.py"
            outside_module.write_bytes(b"outside\n")
            outside_module.chmod(0o444)
            with self.assertRaisesRegex(
                monitor_operations.MonitorError,
                "MONITOR_IMPORT_ROOT_INVALID",
            ):
                monitor_operations._verify_loaded_module_files(
                    source_root=source_module_root,
                    python_runtime=python_module_root,
                    source_inventory=source_module_inventory,
                    python_inventory=python_module_inventory,
                    modules=[
                        *loaded_modules,
                        Namespace(__file__=str(outside_module)),
                    ],
                )

            writable_core = source_module_root / "scripts/manage_deployment.py"
            writable_core.chmod(0o644)
            with self.assertRaisesRegex(
                monitor_operations.MonitorError, "LOADED_MODULE_FILE_UNSAFE"
            ):
                monitor_operations._verify_loaded_module_files(
                    source_root=source_module_root,
                    python_runtime=python_module_root,
                    source_inventory=source_module_inventory,
                    python_inventory=python_module_inventory,
                    modules=loaded_modules,
                )
            writable_core.chmod(0o444)

            writable_manifest = module_root / "manifest.json"
            writable_manifest.write_bytes(b"{}\n")
            writable_manifest.chmod(0o600)
            with self.assertRaisesRegex(
                monitor_operations.MonitorError,
                "TEST_MANIFEST_FILE_UNSAFE",
            ):
                monitor_operations._read_stable_file(
                    writable_manifest,
                    label="TEST_MANIFEST",
                    max_bytes=1024,
                    exact_mode=0o400,
                )

            policy_payload = (
                manage_deployment.PROJECT_ROOT
                / manage_deployment.MONITOR_POLICY_RELATIVE
            ).read_bytes()
            writable_policy = module_root / "policy.json"
            writable_policy.write_bytes(policy_payload)
            for writable_mode in (0o600, 0o420, 0o402):
                with self.subTest(writable_policy_mode=oct(writable_mode)):
                    writable_policy.chmod(writable_mode)
                    with self.assertRaisesRegex(
                        monitor_operations.MonitorError, "POLICY_FILE_UNSAFE"
                    ):
                        monitor_operations.load_policy(
                            writable_policy,
                            hashlib.sha256(policy_payload).hexdigest(),
                        )

        proof_expected = {
            "source_head": "1" * 40,
            "source_tree": "2" * 40,
            "source_manifest_sha256": "3" * 64,
            "python_runtime_manifest_sha256": "4" * 64,
            "python_runtime_tree_sha256": "5" * 64,
            "python_executable_sha256": "6" * 64,
            "runtime_manifest_sha256": "7" * 64,
            "store_binding_sha256": "8" * 64,
        }
        proof_local = {
            "lightrag_six_files": {"manifest_binding_verified": True}
        }
        proof_runtime = {
            "bindings_current": False,
            "lightrag_store_verification": {
                "required": True,
                "verified": True,
                "file_count": 6,
                "manifest_sha256": proof_expected[
                    "runtime_manifest_sha256"
                ],
                "store_binding_sha256": proof_expected[
                    "store_binding_sha256"
                ],
            },
        }
        stale_binding_proofs = monitor_operations._proofs(
            {"payload": {"runtime": proof_runtime}},
            proof_expected,
            proof_local,
        )
        self.assertFalse(
            stale_binding_proofs["lightrag_six_files"]["verified"]
        )
        proof_runtime["bindings_current"] = True
        current_binding_proofs = monitor_operations._proofs(
            {"payload": {"runtime": proof_runtime}},
            proof_expected,
            proof_local,
        )
        self.assertTrue(
            current_binding_proofs["lightrag_six_files"]["verified"]
        )

        def canonical_audit(
            outcome: str,
            *,
            path: str = "/api/search",
            outer_status: int = 200,
            outer_duration_ms: int = 25,
            terminal_status: int | None = 200,
            terminal_elapsed_ms: int | None = 20,
            disconnected: bool = False,
        ) -> dict[str, object]:
            return {
                "audit_schema_version": 2,
                "event": "http_request",
                "method": "POST",
                "path": path,
                "request_id": "a" * 32,
                "status": outer_status,
                "duration_ms": outer_duration_ms,
                "recommendation_outcome": outcome,
                "terminal_status": terminal_status,
                "terminal_elapsed_ms": terminal_elapsed_ms,
                "client_disconnected": disconnected,
            }

        complete = monitor_operations._search_sample(
            canonical_audit("complete", terminal_status=201),
            fallback_id="complete-fallback",
        )
        self.assertIsNotNone(complete)
        assert complete is not None
        self.assertEqual(
            complete[2],
            {
                "duration_ms": 20,
                "failed": False,
                "status": 201,
                "endpoint": "search",
            },
        )
        stream_error_event = canonical_audit(
            "error",
            path="/api/search/stream",
            outer_status=200,
            outer_duration_ms=400,
            terminal_status=503,
            terminal_elapsed_ms=333,
        )
        stream_error = monitor_operations._search_sample(
            stream_error_event, fallback_id="error-fallback"
        )
        self.assertIsNotNone(stream_error)
        assert stream_error is not None
        self.assertEqual(stream_error[2]["status"], 503)
        self.assertTrue(stream_error[2]["failed"])
        self.assertEqual(stream_error[2]["duration_ms"], 333)
        incomplete = monitor_operations._search_sample(
            canonical_audit(
                "incomplete",
                path="/api/search/stream",
                terminal_status=None,
                terminal_elapsed_ms=None,
                disconnected=True,
            ),
            fallback_id="incomplete-fallback",
        )
        self.assertIsNotNone(incomplete)
        assert incomplete is not None
        self.assertEqual(incomplete[2]["status"], 0)
        self.assertTrue(incomplete[2]["failed"])
        self.assertIsNone(
            monitor_operations._search_sample(
                canonical_audit(
                    "not_applicable",
                    terminal_status=None,
                    terminal_elapsed_ms=None,
                ),
                fallback_id="not-applicable-fallback",
            )
        )

        self.assertIsNone(
            monitor_operations._journal_message_payload(
                json.dumps(stream_error_event), max_bytes=4096
            )
        )
        self.assertIsNone(
            monitor_operations._journal_message_payload(
                "ordinary-library-message:" + "x" * 100_000,
                max_bytes=16,
            )
        )
        with self.assertRaisesRegex(
            monitor_operations.MonitorError, "JOURNAL_MESSAGE_TOO_LARGE"
        ):
            monitor_operations._journal_message_payload(
                "[audit] " + "x" * 100_000,
                max_bytes=16,
            )
        with self.assertRaisesRegex(
            monitor_operations.MonitorError, "JOURNAL_AUDIT_JSON_INVALID"
        ):
            monitor_operations._journal_message_payload(
                "[audit] {malformed", max_bytes=4096
            )
        journal_row = json.dumps(
            {
                "__CURSOR": "entry-cursor",
                "MESSAGE": "[audit] "
                + json.dumps(stream_error_event, separators=(",", ":")),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        journal_payload = journal_row + b"\n-- cursor: final-cursor\n"
        journal_samples, journal_cursor, journal_entries = (
            monitor_operations.parse_journal_json(
                journal_payload, max_entries=2, max_message_bytes=4096
            )
        )
        self.assertEqual(journal_cursor, "final-cursor")
        self.assertEqual(journal_entries, 1)
        self.assertEqual(journal_samples[0]["status"], 503)
        large_non_audit_row = json.dumps(
            {
                "__CURSOR": "large-ordinary-cursor",
                "MESSAGE": "ordinary:" + "x" * 100_000,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        ignored_samples, ignored_cursor, ignored_entries = (
            monitor_operations.parse_journal_json(
                large_non_audit_row + b"\n-- cursor: ignored-cursor\n",
                max_entries=2,
                max_message_bytes=16,
            )
        )
        self.assertEqual(ignored_samples, [])
        self.assertEqual(ignored_cursor, "ignored-cursor")
        self.assertEqual(ignored_entries, 1)
        with self.assertRaisesRegex(
            monitor_operations.MonitorError, "JOURNAL_CURSOR_MISSING"
        ):
            monitor_operations.parse_journal_json(
                journal_row + b"\n",
                max_entries=2,
                max_message_bytes=4096,
            )
        with self.assertRaisesRegex(
            monitor_operations.MonitorError, "JOURNAL_ENTRY_LIMIT_EXCEEDED"
        ):
            monitor_operations.parse_journal_json(
                journal_row
                + b"\n"
                + journal_row
                + b"\n-- cursor: overflow-cursor\n",
                max_entries=1,
                max_message_bytes=4096,
            )

        histogram_samples = [
            {
                "duration_ms": duration,
                "failed": False,
                "status": 200,
                "endpoint": "search",
            }
            for duration in (999, 1000, 1001, 3000, 3001)
        ]
        histogram = monitor_operations.aggregate_search_metrics(
            histogram_samples
        )
        self.assertEqual(
            [row["upper_bound_ms"] for row in histogram["latency_histogram"]],
            [*monitor_operations.HISTOGRAM_UPPER_BOUNDS_MS, None],
        )
        self.assertEqual(
            [row["count"] for row in histogram["latency_histogram"][:3]],
            [2, 2, 1],
        )
        self.assertEqual(histogram["latency_p95_upper_bound_ms"], 5000)
        self.assertFalse(histogram["latency_p95_overflow"])
        self.assertEqual(histogram["latency_sample_count"], 5)
        self.assertEqual(histogram["latency_mean_ms"], 1800.2)
        self.assertEqual(histogram["successful_latency_sample_count"], 5)
        self.assertEqual(histogram["successful_latency_mean_ms"], 1800.2)
        terminal_failures = monitor_operations.aggregate_search_metrics(
            [stream_error[2], incomplete[2]]
        )
        self.assertEqual(terminal_failures["terminal_error_count"], 2)
        self.assertEqual(
            terminal_failures["terminal_errors_by_status_class"]["5xx"], 1
        )
        self.assertEqual(
            terminal_failures["hard_timeout_or_incomplete_count"], 1
        )
        self.assertEqual(terminal_failures["latency_sample_count"], 1)
        self.assertEqual(terminal_failures["latency_mean_ms"], 333.0)
        self.assertEqual(
            terminal_failures["successful_latency_sample_count"], 0
        )
        self.assertIsNone(terminal_failures["successful_latency_mean_ms"])
        all_terminal_outcomes = monitor_operations.aggregate_search_metrics(
            [complete[2], stream_error[2], incomplete[2]]
        )
        self.assertEqual(all_terminal_outcomes["terminal_count"], 3)
        self.assertEqual(all_terminal_outcomes["latency_sample_count"], 2)
        self.assertEqual(all_terminal_outcomes["latency_mean_ms"], 176.5)
        self.assertEqual(
            all_terminal_outcomes["successful_latency_sample_count"], 1
        )
        self.assertEqual(
            all_terminal_outcomes["successful_latency_mean_ms"], 20.0
        )
        empty_metrics = monitor_operations.aggregate_search_metrics([])
        self.assertEqual(empty_metrics["latency_sample_count"], 0)
        self.assertIsNone(empty_metrics["latency_mean_ms"])
        self.assertEqual(empty_metrics["successful_latency_sample_count"], 0)
        self.assertIsNone(empty_metrics["successful_latency_mean_ms"])

        with (
            patch.object(
                monitor_operations,
                "_run_command",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=b"unused", stderr=b""
                ),
            ) as journal_command,
            patch.object(
                monitor_operations,
                "parse_journal_json",
                return_value=(
                    [],
                    "oldest-batch-cursor",
                    int(monitor_policy["journal"]["max_entries"]),
                ),
            ),
        ):
            first_batch = monitor_operations.collect_journal(
                monitor_policy,
                cursor=None,
                boot_changed=False,
                now=datetime(2026, 9, 2, tzinfo=timezone.utc),
                disabled=False,
            )
            first_command = journal_command.call_args.args[0]
            self.assertIn(
                f"--lines=+{monitor_policy['journal']['max_entries']}",
                first_command,
            )
            self.assertIn(
                f"--grep={monitor_operations.FIXED_JOURNAL_GREP}",
                first_command,
            )
            self.assertNotIn("--reverse", first_command)
            self.assertTrue(first_batch["backlog_possible"])
            self.assertEqual(first_batch["cursor"], "oldest-batch-cursor")
            self.assertTrue(first_batch["replay_possible"])
            self.assertFalse(first_batch["recovery_eligible"])
            cross_boot_batch = monitor_operations.collect_journal(
                monitor_policy,
                cursor="oldest-batch-cursor",
                boot_changed=True,
                now=datetime(2026, 9, 2, tzinfo=timezone.utc),
                disabled=False,
            )
            cross_boot_command = journal_command.call_args.args[0]
            self.assertIn(
                "--after-cursor=oldest-batch-cursor", cross_boot_command
            )
            self.assertFalse(
                any(value.startswith("--since=") for value in cross_boot_command)
            )
            self.assertTrue(cross_boot_batch["cross_boot_cursor_used"])
            self.assertFalse(cross_boot_batch["cursor_fallback_used"])
            self.assertFalse(cross_boot_batch["replay_possible"])
            self.assertTrue(cross_boot_batch["recovery_eligible"])

        with patch.object(
            monitor_operations,
            "_run_command",
            side_effect=(
                subprocess.CompletedProcess(
                    args=[],
                    returncode=1,
                    stdout=b"",
                    stderr=b"Failed to seek to cursor: Invalid argument\n",
                ),
                subprocess.CompletedProcess(
                    args=[],
                    returncode=1,
                    stdout=b"-- cursor: fallback-cursor\n",
                    stderr=b"",
                ),
            ),
        ) as fallback_commands:
            fallback_batch = monitor_operations.collect_journal(
                monitor_policy,
                cursor="vacuumed-cursor",
                boot_changed=True,
                now=datetime(2026, 9, 2, tzinfo=timezone.utc),
                disabled=False,
            )
            self.assertEqual(fallback_commands.call_count, 2)
            first_fallback_command = fallback_commands.call_args_list[0].args[0]
            second_fallback_command = fallback_commands.call_args_list[1].args[0]
            self.assertIn(
                "--after-cursor=vacuumed-cursor", first_fallback_command
            )
            self.assertTrue(
                any(value.startswith("--since=") for value in second_fallback_command)
            )
            self.assertTrue(fallback_batch["cursor_fallback_used"])
            self.assertTrue(fallback_batch["replay_possible"])
            self.assertFalse(fallback_batch["recovery_eligible"])
            self.assertFalse(fallback_batch["cross_boot_cursor_used"])
            self.assertEqual(fallback_batch["cursor"], "fallback-cursor")

        with patch.object(
            monitor_operations,
            "_run_command",
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout=b"-- cursor: no-match-cursor\n",
                stderr=b"",
            ),
        ):
            no_match_batch = monitor_operations.collect_journal(
                monitor_policy,
                cursor="oldest-batch-cursor",
                boot_changed=False,
                now=datetime(2026, 9, 2, tzinfo=timezone.utc),
                disabled=False,
            )
            self.assertTrue(no_match_batch["available"])
            self.assertEqual(no_match_batch["entries_read"], 0)
            self.assertEqual(no_match_batch["cursor"], "no-match-cursor")
            self.assertEqual(
                no_match_batch["metrics"]["terminal_count"], 0
            )
        with patch.object(
            monitor_operations,
            "_run_command",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=1, stdout=b"", stderr=b""
            ),
        ) as non_explicit_cursor_failure:
            with self.assertRaisesRegex(
                monitor_operations.MonitorError, "JOURNAL_CURSOR_MISSING"
            ):
                monitor_operations.collect_journal(
                    monitor_policy,
                    cursor="no-match-cursor",
                    boot_changed=False,
                    now=datetime(2026, 9, 2, tzinfo=timezone.utc),
                    disabled=False,
                )
            self.assertEqual(non_explicit_cursor_failure.call_count, 1)

        quota_delta, quota_regressed = monitor_operations._quota_delta(
            {"available": True, "state_revision": 11, "used": 150},
            {"quota_revision": 10, "quota_used": 100},
        )
        self.assertEqual((quota_delta, quota_regressed), (50, False))
        quota_delta, quota_regressed = monitor_operations._quota_delta(
            {"available": True, "state_revision": 9, "used": 90},
            {"quota_revision": 10, "quota_used": 100},
        )
        self.assertEqual((quota_delta, quota_regressed), (0, True))

        condition_systemd = {
            "active_state": "active",
            "sub_state": "running",
            "result": "success",
            "main_pid": 1234,
            "invocation_id": "1" * 32,
            "uptime_seconds": 10_000.0,
            "need_daemon_reload": False,
        }
        unavailable_health = {
            "available": False,
            "payload": None,
            "validation_failures": [],
        }
        empty_search_conditions = monitor_operations.build_conditions(
            policy=monitor_policy,
            systemd=condition_systemd,
            restart_delta=0,
            health=unavailable_health,
            proofs={},
            journal={
                "available": True,
                "recovery_eligible": True,
                "metrics": monitor_operations.aggregate_search_metrics([]),
            },
            quota={"available": False},
            quota_delta=0,
            quota_regressed=False,
        )
        self.assertIsNone(
            empty_search_conditions["SEARCH_ERROR_RATE_HIGH"]
        )
        self.assertIsNone(empty_search_conditions["SEARCH_HARD_TIMEOUT"])

        search_alert_states = monitor_operations.initial_state(
            policy_sha256="a" * 64, binding_sha256="b" * 64
        )["alert_states"]
        search_trigger_conditions: dict[str, str | bool | None] = {
            code: False for code in monitor_operations.ALERT_CODES
        }
        search_trigger_conditions["SEARCH_ERROR_RATE_HIGH"] = "warning"
        search_trigger_conditions["SEARCH_HARD_TIMEOUT"] = "critical"
        search_alert_time = datetime(2026, 9, 2, tzinfo=timezone.utc)
        search_alert_states, _events = (
            monitor_operations.evaluate_alert_transitions(
                search_alert_states,
                search_trigger_conditions,
                now=search_alert_time,
                repeat_seconds=21600,
                recovery_observations=2,
                next_revision=1,
            )
        )
        empty_transition_conditions = dict(search_trigger_conditions)
        empty_transition_conditions["SEARCH_ERROR_RATE_HIGH"] = (
            empty_search_conditions["SEARCH_ERROR_RATE_HIGH"]
        )
        empty_transition_conditions["SEARCH_HARD_TIMEOUT"] = (
            empty_search_conditions["SEARCH_HARD_TIMEOUT"]
        )
        for revision in (2, 3):
            search_alert_states, empty_events = (
                monitor_operations.evaluate_alert_transitions(
                    search_alert_states,
                    empty_transition_conditions,
                    now=search_alert_time + timedelta(seconds=revision * 60),
                    repeat_seconds=21600,
                    recovery_observations=2,
                    next_revision=revision,
                )
            )
            self.assertEqual(empty_events, [])
            for code in (
                "SEARCH_ERROR_RATE_HIGH",
                "SEARCH_HARD_TIMEOUT",
            ):
                self.assertTrue(search_alert_states[code]["active"])
                self.assertEqual(
                    search_alert_states[code]["recovery_streak"], 0
                )

        replay_failure_metrics = monitor_operations.aggregate_search_metrics(
            [
                {
                    "duration_ms": 900_000,
                    "failed": True,
                    "status": 503,
                    "endpoint": "search",
                }
            ]
        )
        replay_conditions = monitor_operations.build_conditions(
            policy=monitor_policy,
            systemd=condition_systemd,
            restart_delta=0,
            health=unavailable_health,
            proofs={},
            journal={
                "available": True,
                "replay_possible": True,
                "recovery_eligible": False,
                "metrics": replay_failure_metrics,
            },
            quota={"available": False},
            quota_delta=0,
            quota_regressed=False,
        )
        self.assertIsNone(replay_conditions["SEARCH_ERROR_RATE_HIGH"])
        self.assertIsNone(replay_conditions["SEARCH_HARD_TIMEOUT"])
        replay_transition_conditions = dict(search_trigger_conditions)
        replay_transition_conditions["SEARCH_ERROR_RATE_HIGH"] = None
        replay_transition_conditions["SEARCH_HARD_TIMEOUT"] = None
        replay_states, replay_events = (
            monitor_operations.evaluate_alert_transitions(
                search_alert_states,
                replay_transition_conditions,
                now=search_alert_time + timedelta(seconds=240),
                repeat_seconds=21600,
                recovery_observations=2,
                next_revision=4,
            )
        )
        self.assertEqual(replay_events, [])
        for code in ("SEARCH_ERROR_RATE_HIGH", "SEARCH_HARD_TIMEOUT"):
            self.assertTrue(replay_states[code]["active"])
            self.assertEqual(replay_states[code]["recovery_streak"], 0)

        alert_states = monitor_operations.initial_state(
            policy_sha256="a" * 64, binding_sha256="b" * 64
        )["alert_states"]
        clear_conditions: dict[str, str | bool | None] = {
            code: False for code in monitor_operations.ALERT_CODES
        }
        alert_code = "SERVICE_RESTART_DELTA"
        alert_time = datetime(2026, 9, 2, tzinfo=timezone.utc)
        warning_conditions = dict(clear_conditions)
        warning_conditions[alert_code] = "warning"
        alert_states, events = monitor_operations.evaluate_alert_transitions(
            alert_states,
            warning_conditions,
            now=alert_time,
            repeat_seconds=21600,
            recovery_observations=2,
            next_revision=1,
        )
        self.assertEqual(
            [(event["kind"], event["severity"]) for event in events],
            [("first", "warning")],
        )
        critical_conditions = dict(clear_conditions)
        critical_conditions[alert_code] = "critical"
        alert_states, events = monitor_operations.evaluate_alert_transitions(
            alert_states,
            critical_conditions,
            now=alert_time + timedelta(seconds=60),
            repeat_seconds=21600,
            recovery_observations=2,
            next_revision=2,
        )
        self.assertEqual(
            [(event["kind"], event["severity"]) for event in events],
            [("escalation", "critical")],
        )
        alert_states, events = monitor_operations.evaluate_alert_transitions(
            alert_states,
            critical_conditions,
            now=alert_time + timedelta(seconds=21660),
            repeat_seconds=21600,
            recovery_observations=2,
            next_revision=3,
        )
        self.assertEqual([event["kind"] for event in events], ["repeat"])
        alert_states, events = monitor_operations.evaluate_alert_transitions(
            alert_states,
            clear_conditions,
            now=alert_time + timedelta(seconds=21720),
            repeat_seconds=21600,
            recovery_observations=2,
            next_revision=4,
        )
        self.assertEqual(events, [])
        self.assertEqual(alert_states[alert_code]["recovery_streak"], 1)
        alert_states, events = monitor_operations.evaluate_alert_transitions(
            alert_states,
            clear_conditions,
            now=alert_time + timedelta(seconds=21780),
            repeat_seconds=21600,
            recovery_observations=2,
            next_revision=5,
        )
        self.assertEqual([event["kind"] for event in events], ["recovery"])
        self.assertFalse(alert_states[alert_code]["active"])

    def test_systemd_render_is_dry_run_then_backs_up_existing_file(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.enterContext(
                patch.object(
                    manage_deployment.pwd,
                    "getpwuid",
                    return_value=Namespace(pw_dir=str(root)),
                )
            )
            output = root / "where-papers-go.service"
            runtime = self._runtime_shadow(root)
            shared, config = self._shared_state(root)
            source_release, source_manifest_sha256, source_manifest = (
                self._source_release(root)
            )
            source_head = str(source_manifest["source_head"])
            source_tree = str(source_manifest["source_tree"])
            real_git_output = manage_deployment._git_output
            current_renderer_payload = (
                manage_deployment.PROJECT_ROOT
                / manage_deployment.MONITOR_RENDERER_RELATIVE
            ).read_bytes()
            renderer_object = (
                f"{source_head}:"
                + manage_deployment.MONITOR_RENDERER_RELATIVE.as_posix()
            )

            def committed_renderer_git(
                project: Path, *arguments: str
            ) -> bytes:
                if arguments == ("cat-file", "-s", renderer_object):
                    return f"{len(current_renderer_payload)}\n".encode("ascii")
                if arguments == ("cat-file", "blob", renderer_object):
                    return current_renderer_payload
                return real_git_output(project, *arguments)

            self.enterContext(
                patch.object(
                    manage_deployment,
                    "_git_output",
                    side_effect=committed_renderer_git,
                )
            )
            python_runtime = root / "python-runtime-immutable"
            python = python_runtime / "bin" / "python"
            import_path = python_runtime / "lib" / "site-packages"
            import_path.mkdir(parents=True)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_bytes(b"synthetic executable")
            python.chmod(0o555)
            python_manifest = python_runtime / manage_deployment.PYTHON_RUNTIME_MANIFEST
            python_manifest.write_text("{}\n", encoding="ascii")
            python_manifest.chmod(0o400)
            python_manifest_sha256 = "a" * 64
            api_token_file = root / manage_deployment.BACKEND_API_TOKEN_RELATIVE
            api_token_file.parent.mkdir(parents=True)
            (root / ".config").chmod(0o700)
            api_token_file.parent.chmod(0o700)
            api_token_file.write_text("t" * 48 + "\n", encoding="ascii")
            api_token_file.chmod(0o600)
            runtime_identity = {
                "runtime": str(python_runtime),
                "manifest": str(
                    python_runtime / manage_deployment.PYTHON_RUNTIME_MANIFEST
                ),
                "manifest_sha256": python_manifest_sha256,
                "runtime_tree_sha256": "b" * 64,
                "python_executable": str(python),
                "python_executable_sha256": manage_deployment.sha256_file(python),
                "import_paths": [str(import_path)],
            }
            runtime_release_probe = self.enterContext(
                patch.object(
                    manage_deployment,
                    "validate_python_runtime_release",
                    return_value=runtime_identity,
                )
            )
            runtime_probe = self.enterContext(
                patch.object(manage_deployment, "_validate_python_runtime")
            )
            args = Namespace(
                template=manage_deployment.SYSTEMD_TEMPLATE,
                source_release=source_release,
                expected_source_manifest_sha256=source_manifest_sha256,
                python_runtime=python_runtime,
                expected_python_runtime_manifest_sha256=python_manifest_sha256,
                data_dir=manage_deployment.PROJECT_ROOT / "data",
                api_config=config,
                api_token_file=api_token_file,
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
            self.assertIn(str(source_release), rendered)
            self.assertIn(f"WPG_SOURCE_HEAD={source_head}", rendered)
            self.assertIn(f"{python} -S -P -B -m where_paper_go.web_app", rendered)
            self.assertIn(
                f"WPG_PYTHON_RUNTIME_MANIFEST_SHA256={python_manifest_sha256}",
                rendered,
            )
            self.assertNotIn("@@", rendered)
            self.assertGreaterEqual(runtime_probe.call_count, 1)
            self.assertGreaterEqual(runtime_release_probe.call_count, 1)

            unchanged = manage_deployment.render_systemd(args)
            self.assertIsNone(unchanged["backup"])

            monitor_state_base = root / manage_deployment.MONITOR_STATE_RELATIVE
            monitor_service_output = root / manage_deployment.MONITOR_SERVICE_NAME
            monitor_timer_output = root / manage_deployment.MONITOR_TIMER_NAME
            expected_runtime_manifest_sha256 = (
                manage_deployment._runtime_manifest_sha256(runtime)
            )
            monitor_args = Namespace(
                source_release=source_release,
                expected_source_manifest_sha256=source_manifest_sha256,
                python_runtime=python_runtime,
                expected_python_runtime_manifest_sha256=python_manifest_sha256,
                runtime_dir=runtime,
                expected_runtime_manifest_sha256=(
                    expected_runtime_manifest_sha256
                ),
                api_token_file=api_token_file,
                state_dir=monitor_state_base,
                service_output=monitor_service_output,
                timer_output=monitor_timer_output,
                apply=False,
            )
            wrong_runtime_args = Namespace(**vars(monitor_args))
            wrong_runtime_args.expected_runtime_manifest_sha256 = "f" * 64
            with self.assertRaisesRegex(ValueError, "runtime manifest SHA-256"):
                manage_deployment.render_monitor_systemd(wrong_runtime_args)
            wrong_state_args = Namespace(**vars(monitor_args))
            wrong_state_args.state_dir = root / "attacker-selected-monitor-state"
            with self.assertRaisesRegex(ValueError, "fixed canonical passwd-home"):
                manage_deployment.render_monitor_systemd(wrong_state_args)

            selected_renderer = (
                source_release / manage_deployment.MONITOR_RENDERER_RELATIVE
            )
            clean_checkout_binding = (
                manage_deployment._monitor_renderer_checkout_binding(
                    selected_renderer,
                    expected_head=source_head,
                    expected_tree=source_tree,
                )
            )
            self.assertEqual(clean_checkout_binding["head"], source_head)
            self.assertEqual(clean_checkout_binding["tree"], source_tree)
            self.assertEqual(
                clean_checkout_binding["renderer_sha256"],
                hashlib.sha256(current_renderer_payload).hexdigest(),
            )
            for mismatched_head, mismatched_tree in (
                (
                    ("0" if source_head[0] != "0" else "1")
                    + source_head[1:],
                    source_tree,
                ),
                (
                    source_head,
                    ("0" if source_tree[0] != "0" else "1")
                    + source_tree[1:],
                ),
            ):
                with self.assertRaisesRegex(ValueError, "HEAD/tree differs"):
                    manage_deployment._monitor_renderer_checkout_binding(
                        selected_renderer,
                        expected_head=mismatched_head,
                        expected_tree=mismatched_tree,
                    )

            def drifted_renderer_git(
                project: Path, *arguments: str
            ) -> bytes:
                if arguments == ("cat-file", "blob", renderer_object):
                    return b"x" * len(current_renderer_payload)
                return committed_renderer_git(project, *arguments)

            with (
                patch.object(
                    manage_deployment,
                    "_git_output",
                    side_effect=drifted_renderer_git,
                ),
                self.assertRaisesRegex(ValueError, "uncommitted.*drift"),
            ):
                manage_deployment._monitor_renderer_checkout_binding(
                    selected_renderer,
                    expected_head=source_head,
                    expected_tree=source_tree,
                )

            drifted_selected_renderer = root / "drifted-manage-deployment.py"
            drifted_selected_renderer.write_bytes(
                current_renderer_payload + b"# selected drift\n"
            )
            drifted_selected_renderer.chmod(0o444)
            with self.assertRaisesRegex(
                ValueError, "selected source renderer differs"
            ):
                manage_deployment._monitor_renderer_checkout_binding(
                    drifted_selected_renderer,
                    expected_head=source_head,
                    expected_tree=source_tree,
                )

            monitor_dry_run = manage_deployment.render_monitor_systemd(
                monitor_args
            )
            self.assertEqual(monitor_dry_run["status"], "dry-run")
            self.assertFalse(monitor_state_base.exists())
            self.assertFalse(monitor_service_output.exists())
            self.assertFalse(monitor_timer_output.exists())
            self.assertTrue(monitor_dry_run["renderer_checkout_bound"])
            self.assertEqual(
                monitor_dry_run["renderer_sha256"],
                hashlib.sha256(current_renderer_payload).hexdigest(),
            )
            self.assertEqual(monitor_dry_run["source_head"], source_head)
            self.assertEqual(monitor_dry_run["source_tree"], source_tree)
            self.assertEqual(monitor_dry_run["lightrag_store_file_count"], 6)
            self.assertRegex(
                monitor_dry_run["lightrag_store_binding_sha256"],
                r"^[0-9a-f]{64}$",
            )
            monitor_state = Path(monitor_dry_run["monitor_state_dir"])
            self.assertEqual(monitor_state.parent, monitor_state_base)
            self.assertEqual(
                monitor_state.name,
                monitor_dry_run["monitor_state_namespace_sha256"],
            )
            expected_monitor_binding = {
                "source_head": source_head,
                "source_tree": source_tree,
                "source_manifest_sha256": source_manifest_sha256,
                "python_runtime_manifest_sha256": python_manifest_sha256,
                "python_runtime_tree_sha256": "b" * 64,
                "python_executable_sha256": manage_deployment.sha256_file(python),
                "runtime_manifest_sha256": expected_runtime_manifest_sha256,
                "store_binding_sha256": monitor_dry_run[
                    "lightrag_store_binding_sha256"
                ],
            }
            self.assertEqual(
                monitor_dry_run["deployment_binding_sha256"],
                monitor_operations._binding_sha256(expected_monitor_binding),
            )
            self.assertEqual(
                monitor_state.name,
                monitor_operations._state_namespace_sha256(
                    policy_sha256=monitor_dry_run["policy_sha256"],
                    binding_sha256=monitor_dry_run[
                        "deployment_binding_sha256"
                    ],
                ),
            )
            self.assertNotEqual(
                monitor_state.name,
                manage_deployment._monitor_state_namespace_sha256(
                    policy_sha256=(
                        ("0" if monitor_dry_run["policy_sha256"][0] != "0" else "1")
                        + monitor_dry_run["policy_sha256"][1:]
                    ),
                    deployment_binding_sha256=monitor_dry_run[
                        "deployment_binding_sha256"
                    ],
                ),
            )
            self.assertNotEqual(
                monitor_state.name,
                manage_deployment._monitor_state_namespace_sha256(
                    policy_sha256=monitor_dry_run["policy_sha256"],
                    deployment_binding_sha256=(
                        (
                            "0"
                            if monitor_dry_run["deployment_binding_sha256"][0]
                            != "0"
                            else "1"
                        )
                        + monitor_dry_run["deployment_binding_sha256"][1:]
                    ),
                ),
            )
            monitor_service_candidate = monitor_dry_run["service"]["content"]
            self.assertIn(
                f"--expected-source-head {source_head}",
                monitor_service_candidate,
            )
            self.assertIn(
                "--expected-runtime-manifest-sha256 "
                + expected_runtime_manifest_sha256,
                monitor_service_candidate,
            )
            self.assertIn(
                "--expected-store-binding-sha256 "
                + monitor_dry_run["lightrag_store_binding_sha256"],
                monitor_service_candidate,
            )
            self.assertIn(
                f"--state {monitor_state}/operations-monitor-v1.json",
                monitor_service_candidate,
            )
            self.assertIn(
                f"ReadWritePaths={monitor_state}", monitor_service_candidate
            )
            self.assertNotIn(
                f"ReadWritePaths={monitor_state_base}\n", monitor_service_candidate
            )
            self.assertNotIn("@@", monitor_service_candidate)
            self.assertNotIn("t" * 48, monitor_service_candidate)
            rendered_unset = next(
                line
                for line in monitor_service_candidate.splitlines()
                if line.startswith("UnsetEnvironment=")
            )
            self.assertEqual(
                tuple(rendered_unset.removeprefix("UnsetEnvironment=").split()),
                manage_deployment.EXEC_BOUNDARY_UNSET_ENVIRONMENT,
            )
            self.assertLess(
                monitor_service_candidate.index(rendered_unset),
                monitor_service_candidate.index("ExecStart=/usr/bin/env -i "),
            )
            self.assertEqual(
                monitor_service_candidate.count("TimeoutStartSec=300"), 1
            )
            self.assertEqual(
                hashlib.sha256(monitor_service_candidate.encode("utf-8")).hexdigest(),
                monitor_dry_run["service"]["sha256"],
            )

            monitor_service_output.write_text(
                "preserved monitor service\n", encoding="utf-8"
            )
            monitor_timer_output.write_text(
                "preserved monitor timer\n", encoding="utf-8"
            )
            monitor_args.apply = True
            monitor_installed = manage_deployment.render_monitor_systemd(
                monitor_args
            )
            self.assertEqual(monitor_installed["status"], "installed")
            self.assertEqual(
                stat.S_IMODE(monitor_state_base.stat().st_mode), 0o700
            )
            self.assertEqual(stat.S_IMODE(monitor_state.stat().st_mode), 0o700)
            self.assertEqual(
                Path(monitor_installed["service"]["backup"]).read_text(
                    encoding="utf-8"
                ),
                "preserved monitor service\n",
            )
            self.assertEqual(
                Path(monitor_installed["timer"]["backup"]).read_text(
                    encoding="utf-8"
                ),
                "preserved monitor timer\n",
            )
            self.assertEqual(
                monitor_service_output.read_text(encoding="utf-8"),
                monitor_service_candidate,
            )
            self.assertEqual(
                monitor_timer_output.read_text(encoding="utf-8"),
                monitor_installed["timer"]["content"],
            )
            monitor_verify = subprocess.run(
                [
                    "/usr/bin/systemd-analyze",
                    "verify",
                    str(monitor_service_output),
                    str(monitor_timer_output),
                ],
                cwd=root,
                env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(
                monitor_verify.returncode,
                0,
                monitor_verify.stdout + monitor_verify.stderr,
            )
            self.assertFalse(monitor_installed["manager_reloaded"])
            self.assertFalse(monitor_installed["units_enabled"])
            self.assertFalse(monitor_installed["units_started"])
            monitor_unchanged = manage_deployment.render_monitor_systemd(
                monitor_args
            )
            self.assertIsNone(monitor_unchanged["service"]["backup"])
            self.assertIsNone(monitor_unchanged["timer"]["backup"])

            predecessor_state = monitor_state / "operations-monitor-v1.json"
            predecessor_state.write_text(
                "preserved predecessor monitor state\n", encoding="utf-8"
            )
            predecessor_state.chmod(0o600)
            successor_runtime = self._runtime_shadow(
                root / "successor-runtime", marker="-successor"
            )
            successor_args = Namespace(**vars(monitor_args))
            successor_args.runtime_dir = successor_runtime
            successor_args.expected_runtime_manifest_sha256 = (
                manage_deployment._runtime_manifest_sha256(successor_runtime)
            )
            successor_installed = manage_deployment.render_monitor_systemd(
                successor_args
            )
            successor_state = Path(successor_installed["monitor_state_dir"])
            self.assertNotEqual(successor_state, monitor_state)
            self.assertEqual(successor_state.parent, monitor_state_base)
            self.assertTrue(successor_state.is_dir())
            self.assertEqual(stat.S_IMODE(successor_state.stat().st_mode), 0o700)
            self.assertEqual(
                predecessor_state.read_text(encoding="utf-8"),
                "preserved predecessor monitor state\n",
            )

            core_policy_payload = (
                manage_deployment.PROJECT_ROOT
                / manage_deployment.MONITOR_POLICY_RELATIVE
            ).read_bytes()
            core_policy_sha256 = hashlib.sha256(core_policy_payload).hexdigest()
            core_policy = monitor_operations.validate_policy(
                monitor_operations.parse_json_bytes(
                    core_policy_payload, label="POLICY"
                )
            )
            renderer_store_binding = monitor_dry_run[
                "lightrag_store_binding_sha256"
            ]
            core_store_binding = (
                ("0" if renderer_store_binding[0] != "0" else "1")
                + renderer_store_binding[1:]
            )
            core_args = Namespace(
                policy=(
                    manage_deployment.PROJECT_ROOT
                    / manage_deployment.MONITOR_POLICY_RELATIVE
                ),
                state=root / "placeholder-state",
                token_file=api_token_file,
                source_manifest=(
                    source_release / deployment_identity.SOURCE_MANIFEST_FILE
                ),
                python_runtime_manifest=python_manifest,
                runtime_manifest=runtime / manage_deployment.RUNTIME_MANIFEST,
                expected_policy_sha256=core_policy_sha256,
                # Keep the real selected Git identity.  Vary a non-Git proof
                # only so this mocked core state cannot reuse the renderer
                # fixture's intentionally corrupted predecessor state above.
                expected_source_head=source_head,
                expected_source_tree=source_tree,
                expected_source_manifest_sha256=source_manifest_sha256,
                expected_python_runtime_manifest_sha256=(
                    python_manifest_sha256
                ),
                expected_python_runtime_tree_sha256="b" * 64,
                expected_python_executable_sha256=(
                    manage_deployment.sha256_file(python)
                ),
                expected_runtime_manifest_sha256=(
                    expected_runtime_manifest_sha256
                ),
                expected_store_binding_sha256=core_store_binding,
                no_journal=False,
                apply=False,
            )
            expected_core_binding = monitor_operations._binding_sha256(
                monitor_operations._expected_bindings(core_args)
            )
            core_namespace = monitor_operations._state_namespace_sha256(
                policy_sha256=core_policy_sha256,
                binding_sha256=expected_core_binding,
            )
            core_state = (
                monitor_state_base
                / core_namespace
                / monitor_operations.STATE_FILENAME
            )
            core_args.state = core_state
            healthy_systemd = {
                "load_state": "loaded",
                "active_state": "active",
                "sub_state": "running",
                "result": "success",
                "main_pid": 1234,
                "invocation_id": "1" * 32,
                "nrestarts": 0,
                "active_enter_monotonic_us": 1,
                "need_daemon_reload": False,
                "uptime_seconds": 10_000.0,
                "boot_uptime_seconds": 20_000.0,
            }
            healthy_health = {
                "available": True,
                "http_status": 200,
                "error_code": None,
                "payload": {"ready": True},
                "validation_failures": [],
            }
            verified_proofs = {
                section: {"verified": True}
                for section in (
                    "source",
                    "python_runtime",
                    "worker",
                    "runtime_manifest",
                    "lightrag_six_files",
                )
            }
            healthy_quota = {
                "available": True,
                "state_revision": 7,
                "used": 100,
                "remaining": 900,
                "total_capacity": 1000,
                "status_counts": {
                    "active": 1,
                    "cooldown": 0,
                    "exhausted": 0,
                    "invalid": 0,
                },
            }
            healthy_journal = {
                "available": True,
                "reason": None,
                "cursor": "core-cursor",
                "entries_read": 0,
                "metrics": monitor_operations.aggregate_search_metrics([]),
            }
            with (
                patch.object(
                    monitor_operations,
                    "load_policy",
                    return_value=(core_policy, core_policy_sha256),
                ),
                patch.object(
                    monitor_operations, "_read_token", return_value="x" * 48
                ),
                patch.object(
                    monitor_operations,
                    "collect_local_proofs",
                    return_value={"verified": True},
                ) as local_proof_probe,
                patch.object(
                    monitor_operations,
                    "_read_boot_id",
                    return_value="11111111-1111-1111-1111-111111111111",
                ),
                patch.object(
                    monitor_operations,
                    "collect_systemd",
                    return_value=healthy_systemd,
                ),
                patch.object(
                    monitor_operations,
                    "collect_health",
                    return_value=healthy_health,
                ) as health_probe,
                patch.object(
                    monitor_operations,
                    "_proofs",
                    return_value=verified_proofs,
                ),
                patch.object(
                    monitor_operations,
                    "_sanitized_quota",
                    return_value=healthy_quota,
                ),
                patch.object(
                    monitor_operations,
                    "collect_journal",
                    return_value=healthy_journal,
                ) as journal_probe,
            ):
                core_dry_report, core_dry_status = (
                    monitor_operations.run_observation(core_args)
                )
                self.assertEqual(core_dry_status, 0)
                self.assertEqual(core_dry_report["mode"], "dry-run")
                self.assertEqual(core_dry_report["provider_calls"], 0)
                self.assertFalse(core_state.parent.exists())
                self.assertEqual(
                    core_dry_report["proof_refresh"]["mode"], "full"
                )
                self.assertTrue(
                    core_dry_report["proof_refresh"][
                        "full_proof_performed_this_run"
                    ]
                )
                self.assertFalse(
                    core_dry_report["proof_refresh"][
                        "full_proof_persisted_this_run"
                    ]
                )
                self.assertTrue(local_proof_probe.call_args.kwargs["full"])

                core_args.apply = True
                core_apply_report, core_apply_status = (
                    monitor_operations.run_observation(core_args)
                )
                self.assertEqual(core_apply_status, 0)
                self.assertTrue(core_apply_report["state"]["applied"])
                self.assertTrue(
                    core_apply_report["proof_refresh"][
                        "full_proof_persisted_this_run"
                    ]
                )
                self.assertEqual(stat.S_IMODE(core_state.stat().st_mode), 0o600)
                first_state_payload = core_state.read_bytes()
                first_state = monitor_operations.validate_state(
                    monitor_operations.parse_json_bytes(
                        first_state_payload, label="STATE"
                    ),
                    pending_limit=core_policy["limits"]["pending_events"],
                )
                self.assertEqual(first_state["revision"], 1)
                self.assertEqual(first_state["journal_cursor"], "core-cursor")
                self.assertIsNotNone(first_state["last_full_proof_at"])
                self.assertEqual(
                    core_state,
                    monitor_operations._fixed_state_path(
                        core_state,
                        policy_sha256=core_policy_sha256,
                        binding_sha256=expected_core_binding,
                        create_directories=False,
                    ),
                )
                self.assertEqual(
                    first_state["binding_sha256"], expected_core_binding
                )

                alternate_policy_sha256 = (
                    ("0" if core_policy_sha256[0] != "0" else "1")
                    + core_policy_sha256[1:]
                )
                wrong_core_args = Namespace(**vars(core_args))
                wrong_core_args.state = (
                    root
                    / "attacker-selected-monitor-state"
                    / core_namespace
                    / monitor_operations.STATE_FILENAME
                )
                with self.assertRaisesRegex(
                    monitor_operations.MonitorError, "STATE_PATH_UNSAFE"
                ):
                    monitor_operations.run_observation(wrong_core_args)

                tampered_state = dict(first_state)
                tampered_state["policy_sha256"] = alternate_policy_sha256
                core_state.write_bytes(
                    monitor_operations._canonical_json(tampered_state) + b"\n"
                )
                with self.assertRaisesRegex(
                    monitor_operations.MonitorError,
                    "STATE_POLICY_SHA256_MISMATCH",
                ):
                    monitor_operations._load_state(
                        core_state,
                        policy=core_policy,
                        policy_sha256=core_policy_sha256,
                        binding_sha256=expected_core_binding,
                    )
                alternate_binding = (
                    ("0" if expected_core_binding[0] != "0" else "1")
                    + expected_core_binding[1:]
                )
                tampered_state = dict(first_state)
                tampered_state["binding_sha256"] = alternate_binding
                core_state.write_bytes(
                    monitor_operations._canonical_json(tampered_state) + b"\n"
                )
                with self.assertRaisesRegex(
                    monitor_operations.MonitorError,
                    "STATE_BINDING_SHA256_MISMATCH",
                ):
                    monitor_operations._load_state(
                        core_state,
                        policy=core_policy,
                        policy_sha256=core_policy_sha256,
                        binding_sha256=expected_core_binding,
                    )
                core_state.write_bytes(first_state_payload)

                for journal_failure in (
                    "JOURNAL_CURSOR_MISSING",
                    "JOURNAL_ENTRY_LIMIT_EXCEEDED",
                ):
                    journal_probe.side_effect = monitor_operations.MonitorError(
                        journal_failure
                    )
                    with self.assertRaisesRegex(
                        monitor_operations.MonitorError, journal_failure
                    ):
                        monitor_operations.run_observation(core_args)
                    self.assertFalse(
                        local_proof_probe.call_args.kwargs["full"]
                    )
                    self.assertEqual(core_state.read_bytes(), first_state_payload)
                journal_probe.side_effect = None
                journal_probe.return_value = healthy_journal

                for revision, used, expected_regressed in (
                    (6, 90, True),
                    (8, 95, True),
                    (9, 100, False),
                    (10, 100, False),
                ):
                    healthy_quota.update(
                        {
                            "state_revision": revision,
                            "used": used,
                            "remaining": 1000 - used,
                        }
                    )
                    quota_report, _quota_status = (
                        monitor_operations.run_observation(core_args)
                    )
                    self.assertEqual(
                        quota_report["tavily_quota"]["counter_regressed"],
                        expected_regressed,
                    )
                    persisted_quota_state = monitor_operations.validate_state(
                        monitor_operations.parse_json_bytes(
                            core_state.read_bytes(), label="STATE"
                        ),
                        pending_limit=core_policy["limits"]["pending_events"],
                    )
                    self.assertGreaterEqual(
                        persisted_quota_state["baseline"]["quota_revision"],
                        7,
                    )
                    self.assertEqual(
                        persisted_quota_state["baseline"]["quota_used"], 100
                    )
                    self.assertFalse(
                        local_proof_probe.call_args.kwargs["full"]
                    )
                self.assertEqual(
                    [
                        event["kind"]
                        for event in quota_report["alerts"]["events"]
                        if event["code"]
                        == "TAVILY_QUOTA_COUNTER_REGRESSION"
                    ],
                    ["recovery"],
                )

                aged_state = monitor_operations.validate_state(
                    monitor_operations.parse_json_bytes(
                        core_state.read_bytes(), label="STATE"
                    ),
                    pending_limit=core_policy["limits"]["pending_events"],
                )
                aged_state["last_full_proof_at"] = "2000-01-01T00:00:00Z"
                core_state.write_bytes(
                    monitor_operations._canonical_json(aged_state) + b"\n"
                )
                aged_state_info = core_state.stat()

                health_probe.return_value = {
                    "available": False,
                    "http_status": None,
                    "error_code": "HEALTH_UNREACHABLE",
                    "payload": None,
                    "validation_failures": [],
                }
                core_alert_report, core_alert_status = (
                    monitor_operations.run_observation(core_args)
                )
                self.assertEqual(core_alert_status, 2)
                self.assertEqual(
                    core_alert_report["proof_refresh"]["mode"], "full"
                )
                self.assertTrue(local_proof_probe.call_args.kwargs["full"])
                self.assertEqual(
                    core_alert_report["proof_refresh"][
                        "full_proof_age_seconds"
                    ],
                    0.0,
                )
                self.assertEqual(
                    [event["code"] for event in core_alert_report["alerts"]["events"]],
                    ["HEALTH_UNAVAILABLE"],
                )
                self.assertNotEqual(
                    core_state.stat().st_ino, aged_state_info.st_ino
                )
                self.assertEqual(stat.S_IMODE(core_state.stat().st_mode), 0o600)

            monitor_cli_args = [
                "--policy",
                str(core_args.policy),
                "--state",
                str(core_state),
                "--token-file",
                str(core_args.token_file),
                "--source-manifest",
                str(core_args.source_manifest),
                "--python-runtime-manifest",
                str(core_args.python_runtime_manifest),
                "--runtime-manifest",
                str(core_args.runtime_manifest),
                "--expected-policy-sha256",
                core_policy_sha256,
                "--expected-source-head",
                core_args.expected_source_head,
                "--expected-source-tree",
                core_args.expected_source_tree,
                "--expected-source-manifest-sha256",
                core_args.expected_source_manifest_sha256,
                "--expected-python-runtime-manifest-sha256",
                core_args.expected_python_runtime_manifest_sha256,
                "--expected-python-runtime-tree-sha256",
                core_args.expected_python_runtime_tree_sha256,
                "--expected-python-executable-sha256",
                core_args.expected_python_executable_sha256,
                "--expected-runtime-manifest-sha256",
                core_args.expected_runtime_manifest_sha256,
                "--expected-store-binding-sha256",
                core_args.expected_store_binding_sha256,
            ]
            state_argument = monitor_cli_args.index("--state")
            monitor_cli_without_state = [
                *monitor_cli_args[:state_argument],
                *monitor_cli_args[state_argument + 2 :],
            ]
            with (
                patch.object(monitor_operations.sys, "stderr"),
                self.assertRaises(SystemExit) as missing_state,
            ):
                monitor_operations._parser().parse_args(
                    monitor_cli_without_state
                )
            self.assertEqual(missing_state.exception.code, 3)
            saved_umask = os.umask(0o077)
            os.umask(saved_umask)
            try:
                for expected_status in (0, 2):
                    with (
                        patch.object(
                            monitor_operations,
                            "run_observation",
                            return_value=({"status": "ok"}, expected_status),
                        ),
                        patch.object(
                            monitor_operations, "_print_json"
                        ) as print_probe,
                    ):
                        self.assertEqual(
                            monitor_operations.main(monitor_cli_args),
                            expected_status,
                        )
                        print_probe.assert_called_once_with({"status": "ok"})
                with (
                    patch.object(
                        monitor_operations,
                        "run_observation",
                        side_effect=monitor_operations.MonitorError(
                            "TEST_MONITOR_FAILURE"
                        ),
                    ),
                    patch.object(
                        monitor_operations, "_print_json"
                    ) as error_print_probe,
                ):
                    self.assertEqual(
                        monitor_operations.main(monitor_cli_args), 3
                    )
                    error_report = error_print_probe.call_args.args[0]
                    self.assertEqual(
                        error_report["error_code"], "TEST_MONITOR_FAILURE"
                    )
                    self.assertEqual(error_report["provider_calls"], 0)
                    self.assertFalse(error_report["state_applied"])
            finally:
                os.umask(saved_umask)

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
                "AUTHENTICATED_GATE_PORT": "18002",
                "SERVER_NAME": "papers.example.org",
                "TLS_CERTIFICATE": "/etc/ssl/papers/fullchain.pem",
                "TLS_CERTIFICATE_KEY": "/etc/ssl/papers/privkey.pem",
                "HTPASSWD": "/etc/nginx/where-papers-go.htpasswd",
                "BACKEND_API_TOKEN": "b" * 40,
            },
        ).decode("utf-8")
        for value in (
            "listen 443 ssl",
            "ssl_protocols TLSv1.2 TLSv1.3",
            "auth_basic_user_file",
            "limit_req_zone $binary_remote_addr zone=wpg_auth_attempts:10m rate=30r/m;",
            "limit_req_zone $binary_remote_addr zone=wpg_search:10m rate=6r/m;",
            "limit_req_zone $http_x_wpg_authenticated_user zone=wpg_search_user:10m rate=6r/m;",
            "limit_conn_zone $binary_remote_addr zone=wpg_auth_connections:10m;",
            "limit_conn_zone $binary_remote_addr zone=wpg_search_connections:10m;",
            "limit_conn_zone $http_x_wpg_authenticated_user zone=wpg_search_user_connections:10m;",
            "limit_req zone=wpg_auth_attempts burst=20 nodelay;",
            "limit_req zone=wpg_search burst=2 nodelay;",
            "limit_req zone=wpg_search_user burst=2 nodelay;",
            "limit_conn wpg_auth_connections 16;",
            "limit_conn wpg_search_connections 2;",
            "limit_conn wpg_search_user_connections 2;",
            "limit_req_status 429;",
            "limit_conn_status 429;",
            "access_log /var/log/nginx/where-papers-go.access.json wpg_json",
            '"upstream_request_id":"$upstream_http_x_request_id"',
            'proxy_set_header Authorization "Bearer ' + "b" * 40 + '"',
            "proxy_set_header Host papers.example.org;",
            'proxy_set_header Forwarded "";',
            'proxy_set_header Proxy-Authorization "";',
            'proxy_set_header X-Forwarded-Host "";',
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
        self.assertNotIn("proxy_set_header Host $host", payload)
        tls_server_prefix = payload.split("listen 443 ssl http2;", 1)[1].split(
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
            outer_location = payload.split(f"location = {path} {{", 1)[1].split(
                "\n    }", 1
            )[0]
            self.assertIn('auth_basic "Where Papers Go";', outer_location)
            self.assertIn("auth_basic_user_file", outer_location)
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
                'proxy_set_header X-WPG-Internal-Token "' + "b" * 40 + '";',
                outer_location,
            )
            self.assertIn('proxy_set_header Authorization "";', outer_location)
        internal_server = payload.split(
            "listen 127.0.0.1:18002;", 1
        )[1]
        self.assertIn("access_log off;", internal_server)
        internal_location = internal_server.split(
            "location ~ ^/api/search(?:/stream)?$ {", 1
        )[1].split("\n    }", 1)[0]
        self.assertIn(
            'if ($http_x_wpg_internal_token != "' + "b" * 40 + '") { return 403; }',
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
        self.assertNotIn("@@", payload)

    def test_nginx_renderer_rejects_non_hostname_redirect_targets(self) -> None:
        base = {
            "template": manage_deployment.NGINX_TEMPLATE,
            "output": Path("/tmp/not-written.conf"),
            "tls_certificate": Path("/absolute/cert.pem"),
            "tls_certificate_key": Path("/absolute/key.pem"),
            "htpasswd": Path("/absolute/htpasswd"),
            "backend_api_token_file": Path("/absolute/backend.token"),
            "upstream_port": 8001,
            "authenticated_gate_port": 18002,
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

        for upstream_port in (0, 65_536, True, "8001"):
            with self.subTest(upstream_port=upstream_port), self.assertRaisesRegex(
                ValueError, "upstream-port"
            ):
                manage_deployment.render_nginx(
                    Namespace(
                        server_name="papers.example.org",
                        **{**base, "upstream_port": upstream_port},
                    )
                )

        for gate_port in (
            0,
            1,
            1023,
            65_536,
            True,
            "18002",
            80,
            443,
            8001,
            8765,
        ):
            with self.subTest(gate_port=gate_port), self.assertRaisesRegex(
                ValueError, "authenticated-gate-port"
            ):
                manage_deployment.render_nginx(
                    Namespace(
                        server_name="papers.example.org",
                        **{**base, "authenticated_gate_port": gate_port},
                    )
                )

        for field, unsafe in (
            ("tls_certificate", Path("/tmp/cert;ssl off")),
            ("tls_certificate_key", Path("/tmp/key#comment")),
            ("htpasswd", Path("/tmp/users; auth_basic off; #")),
        ):
            values = dict(base)
            values[field] = unsafe
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "safe absolute Nginx path"
            ):
                manage_deployment.render_nginx(
                    Namespace(server_name="papers.example.org", **values)
                )

        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.enterContext(
                patch.object(
                    manage_deployment.pwd,
                    "getpwuid",
                    return_value=Namespace(pw_dir=str(root)),
                )
            )
            certificate = root / "certificate.pem"
            private_key = root / "private-key.pem"
            htpasswd = root / "htpasswd"
            backend_token = root / manage_deployment.BACKEND_API_TOKEN_RELATIVE
            backend_token.parent.mkdir(parents=True)
            (root / ".config").chmod(0o700)
            backend_token.parent.chmod(0o700)
            certificate.write_text("not a certificate\n", encoding="utf-8")
            private_key.write_text("not a key\n", encoding="utf-8")
            htpasswd.write_text("user:plaintext-password\n", encoding="utf-8")
            backend_token.write_text("b" * 40 + "\n", encoding="utf-8")
            certificate.chmod(0o644)
            private_key.chmod(0o600)
            htpasswd.chmod(0o640)
            backend_token.chmod(0o600)
            alternate_token = root / "alternate-backend.token"
            alternate_token.write_text("a" * 40 + "\n", encoding="ascii")
            alternate_token.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "fixed passwd-home"):
                manage_deployment.render_nginx(
                    Namespace(
                        server_name="papers.example.org",
                        **{
                            **base,
                            "tls_certificate": certificate,
                            "tls_certificate_key": private_key,
                            "htpasswd": htpasswd,
                            "backend_api_token_file": alternate_token,
                        },
                    )
                )
            with self.assertRaisesRegex(ValueError, "plaintext entry"):
                manage_deployment.render_nginx(
                    Namespace(
                        server_name="papers.example.org",
                        **{
                            **base,
                            "tls_certificate": certificate,
                            "tls_certificate_key": private_key,
                            "htpasswd": htpasswd,
                            "backend_api_token_file": backend_token,
                            "apply": True,
                        },
                    )
                )

            candidate = root / "where-papers-go.conf.candidate"
            deferred = manage_deployment.render_nginx(
                Namespace(
                    server_name="papers.example.org",
                    **{
                        **base,
                        "output": candidate,
                        "tls_certificate": certificate,
                        "tls_certificate_key": private_key,
                        "htpasswd": htpasswd,
                        "backend_api_token_file": backend_token,
                        "apply": True,
                        "defer_privileged_input_validation": True,
                    },
                )
            )
            self.assertEqual(candidate.stat().st_mode & 0o777, 0o600)
            self.assertFalse(deferred["privileged_inputs_validated"])
            self.assertEqual(deferred["authenticated_gate_port"], 18002)
            self.assertNotIn("b" * 40, json.dumps(deferred, sort_keys=True))

            # A same-mode, same-size pathname swap after the fd read must not
            # satisfy the privileged-input stability contract.
            stable_input = root / "stable.htpasswd"
            stable_input.write_text(
                "user:$6$salt$" + "x" * 86 + "\n", encoding="ascii"
            )
            stable_input.chmod(0o640)
            opened_snapshot = stable_input.lstat()

            def swap_after_fd_read(
                path: Path, *, max_bytes: int
            ) -> tuple[bytes, os.stat_result]:
                self.assertEqual(path, stable_input)
                self.assertGreater(max_bytes, 0)
                replacement = root / "replacement.htpasswd"
                replacement.write_bytes(stable_input.read_bytes())
                replacement.chmod(0o640)
                os.replace(replacement, stable_input)
                return stable_input.read_bytes(), opened_snapshot

            with (
                patch.object(
                    manage_deployment,
                    "_stable_regular_bytes",
                    side_effect=swap_after_fd_read,
                ),
                self.assertRaisesRegex(ValueError, "changed while inspected"),
            ):
                manage_deployment._validated_nginx_input_file(
                    stable_input, kind="htpasswd"
                )

            stable_token = root / "stable-backend.token"
            stable_token.write_text("z" * 48 + "\n", encoding="ascii")
            stable_token.chmod(0o600)
            token_snapshot = stable_token.lstat()

            def swap_token_after_fd_read(
                path: Path, *, max_bytes: int
            ) -> tuple[bytes, os.stat_result]:
                self.assertEqual(path, stable_token)
                self.assertGreater(max_bytes, 0)
                replacement = root / "replacement-backend.token"
                replacement.write_bytes(stable_token.read_bytes())
                replacement.chmod(0o600)
                os.replace(replacement, stable_token)
                return stable_token.read_bytes(), token_snapshot

            with (
                patch.object(
                    manage_deployment,
                    "_stable_regular_bytes",
                    side_effect=swap_token_after_fd_read,
                ),
                self.assertRaisesRegex(ValueError, "changed while inspected"),
            ):
                manage_deployment._read_private_bearer_token(
                    stable_token, label="backend API token file"
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

            args.host = "0.0.0.0"
            args.trust_proxy = True
            with self.assertRaisesRegex(ValueError, "--host=127.0.0.1"):
                manage_deployment.render_environment(args)

            args.host = "127.0.0.1"
            token_path = root / "backend.token"
            token_path.write_text("b" * 40 + "\n", encoding="utf-8")
            token_path.chmod(0o600)
            args.require_api_auth = True
            args.api_token_file = token_path
            with self.assertRaisesRegex(ValueError, "allowed-client-cidrs"):
                manage_deployment.render_environment(args)

            args.allowed_client_cidrs = "127.0.0.0/8,::1/128"
            args.trusted_proxy_cidrs = "127.0.0.0/8,172.22.13.0/24"
            with self.assertRaisesRegex(ValueError, "trusted-proxy-cidrs"):
                manage_deployment.render_environment(args)

    @patch.dict(os.environ, {}, clear=True)
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
                    "actual_sha256": "3" * 64,
                },
                "lightrag_store_verification": {
                    "required": True,
                    "verified": True,
                    "file_count": 6,
                    "manifest_sha256": "3" * 64,
                    "store_binding_sha256": "4" * 64,
                },
                "worker_process": {
                    "exact": True,
                    "pid": os.getpid() + 1,
                    "start_ticks": 1234,
                    "executable_sha256": "8" * 64,
                    "proc_exe_verified": True,
                    "interpreter": {
                        "argv_exact": True,
                        "no_site": True,
                        "safe_path": True,
                        "dont_write_bytecode": True,
                    },
                    "source": {
                        "head": "1" * 40,
                        "tree": "2" * 40,
                        "manifest_sha256": "5" * 64,
                        "files_verified": True,
                    },
                    "python_runtime": {
                        "manifest_sha256": "6" * 64,
                        "runtime_tree_sha256": "7" * 64,
                        "python_executable_sha256": "8" * 64,
                        "python_version": "3.14.5",
                        "python_soabi": "cpython-314-x86_64-linux-gnu",
                        "python_platform": "linux-x86_64",
                        "wheel_count": 59,
                        "elf_audit_sha256": "9" * 64,
                        "system_library_count": 10,
                        "system_directory_count": 4,
                        "files_verified": True,
                        "proc_exe_matches": True,
                        "system_abi_stat_verified": True,
                    },
                },
            },
            "source": {
                "ready": True,
                "head": "1" * 40,
                "tree": "2" * 40,
                "manifest_sha256": "5" * 64,
                "files_verified": True,
                "file_count": 2,
                "process_pid": os.getpid(),
                "process_start_ticks": deployment_identity.process_start_ticks(),
            },
            "python_runtime": {
                "ready": True,
                "manifest_sha256": "6" * 64,
                "runtime_tree_sha256": "7" * 64,
                "python_executable_sha256": "8" * 64,
                "python_version": "3.14.5",
                "python_soabi": "cpython-314-x86_64-linux-gnu",
                "python_platform": "linux-x86_64",
                "wheel_count": 59,
                "elf_audit_sha256": "9" * 64,
                "system_library_count": 10,
                "system_directory_count": 4,
                "system_abi_stat_verified": True,
                "files_verified": True,
                "proc_exe_matches": True,
                "process_pid": os.getpid(),
                "process_start_ticks": deployment_identity.process_start_ticks(),
            },
            "checks": {
                "lightrag_store_hashes": True,
                "source_identity": True,
                "python_runtime_identity": True,
                "worker_process_identity": True,
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
        with patch.object(
            manage_deployment, "_python_runtime_process_failures", return_value=[]
        ), patch.object(
            manage_deployment, "_worker_process_health_failures", return_value=[]
        ):
            self.assertEqual(manage_deployment.validate_health_payload(healthy), [])
        self.assertTrue(
            manage_deployment.validate_health_payload(
                healthy, expected_process_pid=os.getpid() + 1
            )
        )
        with patch.dict(
            os.environ,
            {
                deployment_identity.SOURCE_HEAD_ENV: "9" * 40,
                "WPG_RUNTIME_MANIFEST_SHA256": "8" * 64,
            },
            clear=False,
        ):
            failures = manage_deployment.validate_health_payload(healthy)
        self.assertTrue(
            any("source health identity" in failure for failure in failures)
        )
        self.assertTrue(
            any("runtime manifest health identity" in failure for failure in failures)
        )
        with TemporaryDirectory() as directory:
            source_release, source_sha256, source_manifest = self._source_release(
                Path(directory)
            )
            locally_bound = json.loads(json.dumps(healthy))
            locally_bound["source"].update(
                {
                    "head": source_manifest["source_head"],
                    "tree": source_manifest["source_tree"],
                    "manifest_sha256": source_sha256,
                    "file_count": source_manifest["file_count"],
                }
            )
            with patch.dict(
                os.environ,
                {
                    deployment_identity.SOURCE_HEAD_ENV: str(
                        source_manifest["source_head"]
                    ),
                    deployment_identity.SOURCE_TREE_ENV: str(
                        source_manifest["source_tree"]
                    ),
                    deployment_identity.SOURCE_MANIFEST_ENV: str(
                        source_release / deployment_identity.SOURCE_MANIFEST_FILE
                    ),
                    deployment_identity.SOURCE_MANIFEST_SHA256_ENV: source_sha256,
                    "WPG_RUNTIME_MANIFEST_SHA256": "3" * 64,
                },
                clear=False,
            ):
                with patch.object(
                    manage_deployment,
                    "_python_runtime_process_failures",
                    return_value=[],
                ), patch.object(
                    manage_deployment,
                    "_worker_process_health_failures",
                    return_value=[],
                ):
                    self.assertEqual(
                        manage_deployment.validate_health_payload(locally_bound), []
                    )

        with TemporaryDirectory() as directory:
            python_runtime = Path(directory) / f"python-runtime-{'a' * 64}"
            python_manifest = (
                python_runtime / manage_deployment.PYTHON_RUNTIME_MANIFEST
            )
            python_executable = python_runtime / "bin" / "python3.14"
            python_identity = {
                "runtime": str(python_runtime),
                "manifest": str(python_manifest),
                "manifest_sha256": "a" * 64,
                "runtime_tree_sha256": "b" * 64,
                "python_executable": str(python_executable),
                "python_executable_sha256": "c" * 64,
                "python_version": "3.14.5",
                "python_soabi": "cpython-314-x86_64-linux-gnu",
                "python_platform": "linux-x86_64",
                "wheel_count": 59,
                "elf_audit_sha256": "d" * 64,
                "system_library_count": 10,
                "system_directory_count": 4,
            }
            python_environment = {
                manage_deployment.PYTHON_RUNTIME_ENV: str(python_runtime),
                manage_deployment.PYTHON_RUNTIME_MANIFEST_ENV: str(python_manifest),
                manage_deployment.PYTHON_RUNTIME_MANIFEST_SHA256_ENV: "a" * 64,
                manage_deployment.PYTHON_RUNTIME_TREE_SHA256_ENV: "b" * 64,
            }
            healthy["python_runtime"] = {
                "ready": True,
                "manifest_sha256": "a" * 64,
                "runtime_tree_sha256": "b" * 64,
                "python_executable_sha256": "c" * 64,
                "python_version": "3.14.5",
                "python_soabi": "cpython-314-x86_64-linux-gnu",
                "python_platform": "linux-x86_64",
                "wheel_count": 59,
                "elf_audit_sha256": "d" * 64,
                "system_library_count": 10,
                "system_directory_count": 4,
                "system_abi_stat_verified": True,
                "files_verified": True,
                "proc_exe_matches": True,
                "process_pid": os.getpid(),
                "process_start_ticks": deployment_identity.process_start_ticks(),
            }
            with (
                patch.dict(os.environ, python_environment, clear=False),
                patch.object(
                    manage_deployment,
                    "validate_python_runtime_release",
                    return_value=python_identity,
                ) as runtime_validation,
                patch.object(
                    manage_deployment,
                    "_process_executable_identity",
                    return_value=(str(python_executable), "c" * 64),
                ),
                patch.object(
                    manage_deployment,
                    "_read_process_environment",
                    return_value=python_environment,
                ),
                patch.object(
                    manage_deployment,
                    "_worker_process_health_failures",
                    return_value=[],
                ),
            ):
                self.assertEqual(
                    manage_deployment.validate_health_payload(healthy), []
                )
                runtime_validation.assert_called_once()
            with (
                patch.dict(os.environ, python_environment, clear=False),
                patch.object(
                    manage_deployment,
                    "validate_python_runtime_release",
                    return_value=python_identity,
                ),
                patch.object(
                    manage_deployment,
                    "_process_executable_identity",
                    return_value=(str(python_executable), "d" * 64),
                ),
                patch.object(
                    manage_deployment,
                    "_read_process_environment",
                    return_value=python_environment,
                ),
                patch.object(
                    manage_deployment,
                    "_worker_process_health_failures",
                    return_value=[],
                ),
            ):
                self.assertTrue(
                    any(
                        "process executable" in failure
                        for failure in manage_deployment.validate_health_payload(
                            healthy
                        )
                    )
                )

        for top_level, field in (
            ("graph", "exists"),
            ("vectors", "exists"),
            ("lightrag", "manifest_exists"),
            ("runtime", "bindings_current"),
            ("runtime", "write_isolated"),
            ("config", "ready"),
            ("checks", "python_runtime_identity"),
            ("checks", "worker_process_identity"),
        ):
            broken = json.loads(json.dumps(healthy))
            broken[top_level][field] = False
            self.assertTrue(
                manage_deployment.validate_health_payload(broken),
                msg=f"{top_level}.{field} must fail closed",
            )

        for path, value in (
            (("runtime", "lightrag_store_verification", "required"), False),
            (("runtime", "lightrag_store_verification", "verified"), False),
            (("runtime", "lightrag_store_verification", "file_count"), 5),
            (("runtime", "lightrag_store_verification", "manifest_sha256"), "x" * 64),
            (("runtime", "lightrag_store_verification", "store_binding_sha256"), "x" * 64),
            (("source", "ready"), False),
            (("source", "manifest_sha256"), "x" * 64),
            (("source", "process_pid"), 0),
            (("source", "process_start_ticks"), 0),
        ):
            broken = json.loads(json.dumps(healthy))
            target = broken
            for component in path[:-1]:
                target = target[component]
            target[path[-1]] = value
            self.assertTrue(
                manage_deployment.validate_health_payload(broken),
                msg=".".join(path) + " must fail closed",
            )

    def test_source_release_is_git_bound_content_addressed_atomic_and_read_only(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()

            def git(*arguments: str) -> None:
                subprocess.run(
                    ["git", "-C", str(project), *arguments],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

            git("init", "-q")
            git("config", "user.name", "Unit Test")
            git("config", "user.email", "unit@example.invalid")
            (project / "app.py").write_text("committed = True\n", encoding="utf-8")
            executable = project / "run.sh"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            git("add", "app.py", "run.sh")
            git("commit", "-q", "-m", "source fixture")
            (project / "app.py").write_text("dirty = True\n", encoding="utf-8")
            ignored = project / "__pycache__"
            ignored.mkdir()
            (ignored / "app.cpython.pyc").write_bytes(b"not-release-input")

            release_root = root / "published"
            args = Namespace(
                project_root=project,
                release_root=release_root,
                apply=False,
            )
            dry = manage_deployment.prepare_source_release(args)
            self.assertEqual(dry["status"], "dry-run")
            self.assertFalse(release_root.exists())

            args.apply = True
            built = manage_deployment.prepare_source_release(args)
            release = Path(built["release"])
            manifest = Path(built["manifest"])
            self.assertEqual(built["status"], "built")
            self.assertEqual(release.name, f"release-{built['manifest_sha256']}")
            self.assertEqual((release / "app.py").read_text(), "committed = True\n")
            self.assertFalse((release / ".git").exists())
            self.assertFalse((release / "__pycache__").exists())
            self.assertEqual(stat.S_IMODE(release.stat().st_mode), 0o555)
            self.assertEqual(stat.S_IMODE((release / "app.py").stat().st_mode), 0o444)
            self.assertEqual(stat.S_IMODE((release / "run.sh").stat().st_mode), 0o555)
            self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o400)
            self.assertEqual(
                list((release_root / "releases").glob(".*.building")), []
            )
            identity = deployment_identity.validate_source_release(
                manifest,
                expected_head=built["head"],
                expected_tree=built["tree"],
                expected_manifest_sha256=built["manifest_sha256"],
            )
            self.assertTrue(identity["files_verified"])
            status = deployment_identity.source_identity_status(
                {
                    deployment_identity.SOURCE_HEAD_ENV: str(built["head"]),
                    deployment_identity.SOURCE_TREE_ENV: str(built["tree"]),
                    deployment_identity.SOURCE_MANIFEST_ENV: str(manifest),
                    deployment_identity.SOURCE_MANIFEST_SHA256_ENV: str(
                        built["manifest_sha256"]
                    ),
                }
            )
            self.assertTrue(status["ready"])
            self.assertEqual(status["process_pid"], os.getpid())
            self.assertGreater(int(status["process_start_ticks"]), 0)
            wrong_head_status = deployment_identity.source_identity_status(
                {
                    deployment_identity.SOURCE_HEAD_ENV: "9" * 40,
                    deployment_identity.SOURCE_TREE_ENV: str(built["tree"]),
                    deployment_identity.SOURCE_MANIFEST_ENV: str(manifest),
                    deployment_identity.SOURCE_MANIFEST_SHA256_ENV: str(
                        built["manifest_sha256"]
                    ),
                }
            )
            self.assertFalse(wrong_head_status["ready"])

            reused = manage_deployment.prepare_source_release(args)
            self.assertEqual(reused["status"], "already-built")
            (release / "app.py").chmod(0o644)
            with self.assertRaisesRegex(
                deployment_identity.SourceIdentityError, "mode"
            ):
                deployment_identity.validate_source_release(
                    manifest,
                    expected_manifest_sha256=built["manifest_sha256"],
                )
            (release / "app.py").chmod(0o444)
            with self.assertRaisesRegex(
                deployment_identity.SourceIdentityError, "SHA-256"
            ):
                deployment_identity.validate_source_release(
                    manifest,
                    expected_manifest_sha256="f" * 64,
                )
            release.chmod(0o755)
            extra = release / "unlisted.py"
            extra.write_bytes(b"unlisted\n")
            extra.chmod(0o444)
            release.chmod(0o555)
            with self.assertRaisesRegex(
                deployment_identity.SourceIdentityError, "tree"
            ):
                deployment_identity.validate_source_release(
                    manifest,
                    expected_manifest_sha256=built["manifest_sha256"],
                )
            release.chmod(0o755)
            extra.unlink()
            release.chmod(0o555)
            self.assertFalse(
                deployment_identity.source_identity_status(
                    {
                        deployment_identity.SOURCE_HEAD_ENV: str(built["head"]),
                        deployment_identity.SOURCE_TREE_ENV: str(built["tree"]),
                        deployment_identity.SOURCE_MANIFEST_ENV: str(manifest),
                        deployment_identity.SOURCE_MANIFEST_SHA256_ENV: str(
                            built["manifest_sha256"]
                        ),
                    }
                )["ready"]
            )

            # Keep the public test ID stable while exercising the companion
            # immutable Python-runtime publisher and validator end to end.
            tracked_lock_payload = (
                manage_deployment.PRODUCTION_PYTHON_RUNTIME_LOCK.read_bytes()
            )
            self.assertEqual(
                hashlib.sha256(tracked_lock_payload).hexdigest(),
                manage_deployment.PRODUCTION_PYTHON_RUNTIME_LOCK_SHA256,
            )
            self.assertEqual(
                manage_deployment._validate_selected_wheel_lock(
                    tracked_lock_payload
                )["wheel_count"],
                59,
            )
            source_prefix = root / "python-prefix"
            python = source_prefix / "bin" / "python3.14"
            purelib = source_prefix / "lib" / "python3.14" / "site-packages"
            lib_dynload = source_prefix / "lib" / "python3.14" / "lib-dynload"
            python.parent.mkdir(parents=True)
            purelib.mkdir(parents=True)
            lib_dynload.mkdir()
            python.write_bytes(b"#!/bin/sh\nexit 0\n")
            python.chmod(0o755)
            native_probe = source_prefix / "bin" / "native-probe"
            native_probe.write_bytes(Path("/bin/true").read_bytes())
            native_probe.chmod(0o755)
            path_bound_library = source_prefix / "lib" / "path-bound.so"
            path_bound_library.parent.mkdir(exist_ok=True)
            path_bound_library.write_bytes(b"path-bound fixture\n")
            self.assertEqual(
                manage_deployment._canonical_elf_needed_path(
                    "$ORIGIN/../lib/path-bound.so",
                    elf_path=native_probe,
                    runtime_root=source_prefix,
                ),
                "$RUNTIME/lib/path-bound.so",
            )
            with self.assertRaisesRegex(ValueError, "not \\$ORIGIN-relative"):
                manage_deployment._canonical_elf_needed_path(
                    str(path_bound_library),
                    elf_path=native_probe,
                    runtime_root=source_prefix,
                )
            unsafe_system_component = root / "unsafe-system-component"
            unsafe_system_component.mkdir(mode=0o777)
            unsafe_system_component.chmod(0o777)
            with self.assertRaisesRegex(ValueError, "root-controlled"):
                manage_deployment._system_path_component_identity(
                    unsafe_system_component,
                    directory=True,
                )
            with self.assertRaisesRegex(ValueError, "root-controlled"):
                manage_deployment._validate_opened_system_library(
                    Namespace(st_uid=1234, st_mode=stat.S_IFREG | 0o755),
                    expected_mode=0o755,
                )
            package = purelib / "pinned_package.py"
            package_payload = b"PINNED = True\n"
            package.write_bytes(package_payload)
            dist_info = purelib / "pinned_package-1.0.dist-info"
            dist_info.mkdir()
            metadata_payload = (
                b"Metadata-Version: 2.1\n"
                b"Name: pinned-package\n"
                b"Version: 1.0\n\n"
            )
            wheel_metadata_payload = (
                b"Wheel-Version: 1.0\n"
                b"Generator: unit-test\n"
                b"Root-Is-Purelib: true\n"
                b"Tag: py3-none-any\n\n"
            )
            entry_points_payload = (
                b"[console_scripts]\n"
                b"pinned-cli = pinned_package:main\n"
            )
            installer_payload = b"pip\n"
            requested_payload = b""
            generated_script_payload = b"#!/runtime/python\n"

            def record_row(path: str, payload: bytes) -> str:
                digest = base64.urlsafe_b64encode(
                    hashlib.sha256(payload).digest()
                ).decode("ascii").rstrip("=")
                return f"{path},sha256={digest},{len(payload)}"

            for name, payload in (
                ("METADATA", metadata_payload),
                ("WHEEL", wheel_metadata_payload),
                ("entry_points.txt", entry_points_payload),
                ("INSTALLER", installer_payload),
                ("REQUESTED", requested_payload),
            ):
                (dist_info / name).write_bytes(payload)
            installed_record_payload = (
                "\n".join(
                    (
                        record_row("pinned_package.py", package_payload),
                        record_row(
                            "pinned_package-1.0.dist-info/METADATA",
                            metadata_payload,
                        ),
                        record_row(
                            "pinned_package-1.0.dist-info/WHEEL",
                            wheel_metadata_payload,
                        ),
                        record_row(
                            "pinned_package-1.0.dist-info/entry_points.txt",
                            entry_points_payload,
                        ),
                        record_row(
                            "pinned_package-1.0.dist-info/INSTALLER",
                            installer_payload,
                        ),
                        record_row(
                            "pinned_package-1.0.dist-info/REQUESTED",
                            requested_payload,
                        ),
                        record_row("../../bin/pinned-cli", generated_script_payload),
                        "pinned_package-1.0.dist-info/RECORD,,",
                    )
                )
                + "\n"
            ).encode()
            (dist_info / "RECORD").write_bytes(installed_record_payload)
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            wheel = wheelhouse / "pinned_package-1.0-py3-none-any.whl"
            wheel_record_payload = (
                "\n".join(
                    (
                        record_row("pinned_package.py", package_payload),
                        record_row(
                            "pinned_package-1.0.dist-info/METADATA",
                            metadata_payload,
                        ),
                        record_row(
                            "pinned_package-1.0.dist-info/WHEEL",
                            wheel_metadata_payload,
                        ),
                        record_row(
                            "pinned_package-1.0.dist-info/entry_points.txt",
                            entry_points_payload,
                        ),
                        "pinned_package-1.0.dist-info/RECORD,,",
                    )
                )
                + "\n"
            ).encode()
            with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("pinned_package.py", package_payload)
                archive.writestr(
                    "pinned_package-1.0.dist-info/METADATA", metadata_payload
                )
                archive.writestr(
                    "pinned_package-1.0.dist-info/WHEEL", wheel_metadata_payload
                )
                archive.writestr(
                    "pinned_package-1.0.dist-info/entry_points.txt",
                    entry_points_payload,
                )
                archive.writestr(
                    "pinned_package-1.0.dist-info/RECORD", wheel_record_payload
                )
            wheel_sha256 = hashlib.sha256(wheel.read_bytes()).hexdigest()
            dependency_lock = root / "selected-wheel-lock.json"

            def runtime_layout_probe(
                runtime_root: Path, executable_relative: str
            ) -> dict[str, object]:
                executable = runtime_root / executable_relative
                executable_sha256 = hashlib.sha256(executable.read_bytes()).hexdigest()
                return {
                    "implementation": "CPython",
                    "version": "3.14.0",
                    "version_info": {
                        "major": 3,
                        "minor": 14,
                        "micro": 0,
                        "releaselevel": "final",
                        "serial": 0,
                    },
                    "cache_tag": "cpython-314",
                    "soabi": "cpython-314-x86_64-linux-gnu",
                    "platform": "linux-x86_64",
                    "proc_exe_sha256": executable_sha256,
                    "stdlib": "lib/python3.14",
                    "platstdlib": "lib/python3.14",
                    "purelib": "lib/python3.14/site-packages",
                    "platlib": "lib/python3.14/site-packages",
                    "isolated_sys_path": [
                        "lib/python3.14",
                        "lib/python3.14/lib-dynload",
                    ],
                }

            runtime_args = Namespace(
                source_prefix=source_prefix,
                python_relative_path=Path("bin/python3.14"),
                dependency_lock=dependency_lock,
                wheelhouse=wheelhouse,
                release_root=root / "runtime-published",
                apply=False,
            )
            def synthetic_system_component(
                path: Path, *, directory: bool
            ) -> dict[str, object]:
                info = path.lstat()
                self.assertEqual(stat.S_ISDIR(info.st_mode), directory)
                return {
                    "path": str(path),
                    "owner_uid": 0,
                    "mode": f"{stat.S_IMODE(info.st_mode):04o}",
                }

            with (
                patch.object(
                    manage_deployment,
                    "_probe_python_runtime_layout",
                    side_effect=runtime_layout_probe,
                ),
                patch.object(
                    manage_deployment,
                    "_system_path_component_identity",
                    side_effect=synthetic_system_component,
                ),
                patch.object(
                    manage_deployment,
                    "_validate_opened_system_library",
                ),
            ):
                lock_args = Namespace(
                    source_prefix=source_prefix,
                    python_relative_path=Path("bin/python3.14"),
                    wheelhouse=wheelhouse,
                    output=dependency_lock,
                    apply=False,
                )
                lock_dry = manage_deployment.prepare_python_runtime_lock(lock_args)
                self.assertEqual(lock_dry["status"], "dry-run")
                self.assertFalse(dependency_lock.exists())
                lock_args.apply = True
                lock_built = manage_deployment.prepare_python_runtime_lock(lock_args)
                self.assertEqual(lock_built["status"], "built")
                self.assertEqual(stat.S_IMODE(dependency_lock.stat().st_mode), 0o400)
                selected_lock = json.loads(
                    dependency_lock.read_text(encoding="utf-8")
                )
                self.assertEqual(
                    selected_lock["artifact_type"],
                    manage_deployment.PYTHON_RUNTIME_LOCK_ARTIFACT_TYPE,
                )
                self.assertEqual(selected_lock["python"]["platform"], "linux-x86_64")
                self.assertEqual(selected_lock["wheels"][0]["name"], "pinned-package")
                self.assertEqual(selected_lock["wheels"][0]["version"], "1.0")
                self.assertEqual(selected_lock["wheels"][0]["bytes"], wheel.stat().st_size)
                self.assertEqual(selected_lock["wheels"][0]["sha256"], wheel_sha256)
                self.assertEqual(
                    manage_deployment.prepare_python_runtime_lock(lock_args)["status"],
                    "already-built",
                )
                incompatible_lock = json.loads(json.dumps(selected_lock))
                incompatible_lock["wheels"][0]["tags"] = [
                    "cp313-cp313-win_amd64"
                ]
                incompatible_payload = (
                    json.dumps(
                        incompatible_lock,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode()
                with self.assertRaisesRegex(ValueError, "no tag compatible"):
                    manage_deployment._validate_selected_wheel_lock(
                        incompatible_payload,
                        expected_python=selected_lock["python"],
                    )

                mismatched_lock = json.loads(json.dumps(selected_lock))
                mismatched_lock["python"]["platform"] = "other-platform"
                mismatched_lock_path = root / "mismatched-selected-wheel-lock.json"
                mismatched_lock_path.write_text(
                    json.dumps(mismatched_lock, sort_keys=True, separators=(",", ":"))
                    + "\n",
                    encoding="utf-8",
                )
                mismatched_args = Namespace(
                    **{
                        **vars(runtime_args),
                        "dependency_lock": mismatched_lock_path,
                    }
                )
                with self.assertRaisesRegex(ValueError, "platform identity"):
                    manage_deployment.prepare_python_runtime(mismatched_args)

                uv_lock_path = root / "uv.lock"
                uv_lock_path.write_text(
                    'version = 1\nrevision = 3\nrequires-python = ">=3.10"\n',
                    encoding="utf-8",
                )
                uv_lock_args = Namespace(
                    **{
                        **vars(runtime_args),
                        "dependency_lock": uv_lock_path,
                    }
                )
                with self.assertRaisesRegex(ValueError, "not uv.lock"):
                    manage_deployment.prepare_python_runtime(uv_lock_args)

                absolute_binding = purelib / "absolute-launcher"
                absolute_binding.write_text(
                    "#!/tmp/previous-prefix/bin/python3.14\n",
                    encoding="utf-8",
                )
                absolute_binding.chmod(0o755)
                with self.assertRaisesRegex(ValueError, "absolute shebang"):
                    manage_deployment.prepare_python_runtime(runtime_args)
                absolute_binding.unlink()

                unrecorded_package = purelib / "unrecorded_package.py"
                unrecorded_package.write_bytes(b"UNLOCKED = True\n")
                with self.assertRaisesRegex(ValueError, "unrecorded files"):
                    manage_deployment.prepare_python_runtime(runtime_args)
                unrecorded_package.unlink()

                runtime_dry = manage_deployment.prepare_python_runtime(runtime_args)
                self.assertEqual(runtime_dry["status"], "dry-run")
                self.assertFalse(runtime_args.release_root.exists())

                runtime_args.apply = True
                runtime_built = manage_deployment.prepare_python_runtime(runtime_args)
                runtime_release = Path(runtime_built["runtime"])
                runtime_manifest = Path(runtime_built["manifest"])
                self.assertEqual(runtime_built["status"], "built")
                self.assertEqual(
                    runtime_release.name,
                    f"python-runtime-{runtime_built['manifest_sha256']}",
                )
                self.assertEqual(stat.S_IMODE(runtime_release.stat().st_mode), 0o555)
                self.assertEqual(stat.S_IMODE(runtime_manifest.stat().st_mode), 0o400)
                self.assertEqual(
                    stat.S_IMODE((runtime_release / "bin" / "python3.14").stat().st_mode),
                    0o555,
                )
                self.assertEqual(
                    stat.S_IMODE(
                        (
                            runtime_release / package.relative_to(source_prefix)
                        ).stat().st_mode
                    ),
                    0o444,
                )
                self.assertEqual(
                    list(
                        (runtime_args.release_root / "python-runtimes").glob(
                            ".*.building"
                        )
                    ),
                    [],
                )
                runtime_document = json.loads(runtime_manifest.read_text(encoding="utf-8"))
                self.assertEqual(
                    runtime_document["python"]["executable_sha256"],
                    runtime_document["python"]["proc_exe_sha256"],
                )
                self.assertEqual(runtime_document["python"]["version"], "3.14.0")
                self.assertEqual(runtime_document["elf_audit"]["file_count"], 1)
                self.assertGreater(
                    runtime_document["elf_audit"]["system_library_count"], 0
                )
                self.assertEqual(
                    runtime_document["elf_audit"]["system_library_count"],
                    len(runtime_document["elf_audit"]["system_libraries"]),
                )
                for system_library in runtime_document["elf_audit"][
                    "system_libraries"
                ]:
                    self.assertEqual(
                        set(system_library),
                        {
                            "path",
                            "bytes",
                            "mode",
                            "owner_uid",
                            "sha256",
                            "trusted_directories_sha256",
                        },
                    )
                    self.assertTrue(Path(system_library["path"]).is_absolute())
                    self.assertEqual(system_library["owner_uid"], 0)
                    self.assertEqual(len(system_library["sha256"]), 64)
                self.assertGreater(
                    runtime_document["elf_audit"]["system_directory_count"], 0
                )
                for directory_identity in runtime_document["elf_audit"][
                    "system_directories"
                ]:
                    self.assertEqual(directory_identity["owner_uid"], 0)
                    self.assertFalse(int(directory_identity["mode"], 8) & 0o022)
                self.assertEqual(runtime_built["elf_file_count"], 1)
                self.assertEqual(
                    runtime_built["elf_audit_sha256"],
                    runtime_document["elf_audit"]["binding_sha256"],
                )
                self.assertEqual(
                    runtime_document["elf_audit"]["files"][0]["path"],
                    "bin/native-probe",
                )
                installed = runtime_document["installed_distributions"]
                self.assertEqual(installed["distribution_count"], 1)
                self.assertEqual(installed["omitted_entry_point_count"], 1)
                self.assertTrue(installed["record_files_verified"])
                self.assertEqual(
                    installed["distributions"][0]["normalized_name"],
                    "pinned-package",
                )
                self.assertEqual(
                    installed["distributions"][0]["omitted_entry_points"][0][
                        "runtime_path"
                    ],
                    "bin/pinned-cli",
                )
                self.assertEqual(
                    runtime_built["installed_distributions_sha256"],
                    installed["binding_sha256"],
                )
                self.assertEqual(
                    runtime_document["dependency_lock"]["sha256"],
                    hashlib.sha256(dependency_lock.read_bytes()).hexdigest(),
                )
                self.assertEqual(runtime_document["wheels"][0]["sha256"], wheel_sha256)
                self.assertEqual(
                    runtime_document["wheels"][0]["normalized_name"],
                    "pinned-package",
                )
                runtime_identity = manage_deployment.validate_python_runtime_release(
                    runtime_manifest,
                    expected_manifest_sha256=runtime_built["manifest_sha256"],
                )
                self.assertTrue(runtime_identity["files_verified"])
                self.assertEqual(runtime_identity["elf_file_count"], 1)
                self.assertEqual(
                    runtime_identity["runtime_tree_sha256"],
                    runtime_built["runtime_tree_sha256"],
                )
                system_library_paths = {
                    Path(row["path"]).resolve()
                    for row in runtime_document["elf_audit"]["system_libraries"]
                }
                stable_regular_file = manage_deployment._stable_regular_file

                def drift_system_library(path: Path, **kwargs: object):
                    size, digest, info = stable_regular_file(path, **kwargs)
                    if path.resolve() in system_library_paths:
                        digest = "f" * 64 if digest != "f" * 64 else "e" * 64
                    return size, digest, info

                with (
                    patch.object(
                        manage_deployment,
                        "_stable_regular_file",
                        side_effect=drift_system_library,
                    ),
                    self.assertRaisesRegex(ValueError, "ELF loader identity"),
                ):
                    manage_deployment.validate_python_runtime_release(
                        runtime_manifest,
                        expected_manifest_sha256=runtime_built["manifest_sha256"],
                    )

                runtime_reused = manage_deployment.prepare_python_runtime(runtime_args)
                self.assertEqual(runtime_reused["status"], "already-built")
                published_package = runtime_release / package.relative_to(source_prefix)
                published_package.chmod(0o644)
                with self.assertRaisesRegex(ValueError, "mode"):
                    manage_deployment.validate_python_runtime_release(
                        runtime_manifest,
                        expected_manifest_sha256=runtime_built["manifest_sha256"],
                    )
                published_package.chmod(0o444)

    def test_source_release_rename_failure_never_publishes_partial_release(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            subprocess.run(
                ["git", "-C", str(project), "init", "-q"], check=True
            )
            subprocess.run(
                ["git", "-C", str(project), "config", "user.name", "Unit Test"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(project),
                    "config",
                    "user.email",
                    "unit@example.invalid",
                ],
                check=True,
            )
            (project / "app.py").write_text("committed = True\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(project), "add", "app.py"], check=True
            )
            subprocess.run(
                ["git", "-C", str(project), "commit", "-q", "-m", "fixture"],
                check=True,
            )
            release_root = root / "published"
            args = Namespace(
                project_root=project,
                release_root=release_root,
                apply=True,
            )
            with (
                patch.object(
                    manage_deployment,
                    "atomic_rename_noreplace",
                    side_effect=OSError("simulated atomic rename failure"),
                ),
                self.assertRaisesRegex(OSError, "atomic rename"),
            ):
                manage_deployment.prepare_source_release(args)
            releases = release_root / "releases"
            self.assertEqual(list(releases.glob("release-*")), [])
            self.assertEqual(len(list(releases.glob(".*.building"))), 1)

    def test_atomic_rename_noreplace_never_replaces_existing_target(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            published = root / "published"
            first.write_bytes(b"first")
            deployment_identity.atomic_rename_noreplace(first, published)
            self.assertFalse(first.exists())
            self.assertEqual(published.read_bytes(), b"first")

            contender = root / "contender"
            contender.write_bytes(b"contender")
            with self.assertRaises(FileExistsError):
                deployment_identity.atomic_rename_noreplace(contender, published)
            self.assertEqual(contender.read_bytes(), b"contender")
            self.assertEqual(published.read_bytes(), b"first")

    def test_prepare_runtime_is_private_atomic_and_preserves_prior_generation(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.enterContext(
                patch.object(
                    manage_deployment.pwd,
                    "getpwuid",
                    return_value=Namespace(pw_dir=str(root)),
                )
            )
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
            source_release, source_manifest_sha256, _source_manifest = (
                self._source_release(root)
            )
            python_runtime = root / "python-runtime-immutable"
            python = python_runtime / "bin" / "python"
            import_path = python_runtime / "lib" / "site-packages"
            import_path.mkdir(parents=True)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_bytes(b"synthetic executable")
            python.chmod(0o555)
            python_manifest_sha256 = "a" * 64
            api_token_file = root / manage_deployment.BACKEND_API_TOKEN_RELATIVE
            api_token_file.parent.mkdir(parents=True)
            (root / ".config").chmod(0o700)
            api_token_file.parent.chmod(0o700)
            api_token_file.write_text("t" * 48 + "\n", encoding="ascii")
            api_token_file.chmod(0o600)
            runtime_release_probe = self.enterContext(
                patch.object(
                    manage_deployment,
                    "validate_python_runtime_release",
                    return_value={
                        "runtime": str(python_runtime),
                        "manifest": str(
                            python_runtime
                            / manage_deployment.PYTHON_RUNTIME_MANIFEST
                        ),
                        "manifest_sha256": python_manifest_sha256,
                        "runtime_tree_sha256": "b" * 64,
                        "python_executable": str(python),
                        "import_paths": [str(import_path)],
                    },
                )
            )
            runtime_probe = self.enterContext(
                patch.object(manage_deployment, "_validate_python_runtime")
            )
            systemd = manage_deployment.render_systemd(
                Namespace(
                    template=manage_deployment.SYSTEMD_TEMPLATE,
                    source_release=source_release,
                    expected_source_manifest_sha256=source_manifest_sha256,
                    python_runtime=python_runtime,
                    expected_python_runtime_manifest_sha256=python_manifest_sha256,
                    data_dir=data,
                    api_config=config,
                    api_token_file=api_token_file,
                    runtime_dir=current,
                    shared_state_dir=runtime_root / "shared",
                    output=root / "where-papers-go.service",
                    apply=False,
                )
            )
            self.assertEqual(systemd["status"], "dry-run")
            runtime_probe.assert_called_once()
            runtime_release_probe.assert_called_once()

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
