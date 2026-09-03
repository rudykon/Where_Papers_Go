from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from scripts import validate_closeout


class AggregateCloseoutValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output_root = self.root / "benchmark_artifacts"
        self.output_root.mkdir()
        self.head = "a" * 40
        self.branch = "agent/aggregate-only-closeout-20260831"
        self.input_path = self.root / "closeout-request.json"
        self.artifact_hashes: dict[str, str] = {}
        for name, relative in validate_closeout.REQUIRED_ARTIFACTS.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                (validate_closeout.PROJECT_ROOT / relative).read_bytes()
            )
            path.chmod(0o444)
            self.artifact_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "artifact_type": validate_closeout.REQUEST_ARTIFACT_TYPE,
            "expected_head": self.head,
            "expected_branch": self.branch,
            "artifacts": dict(self.artifact_hashes),
        }

    def write_request(self, value: dict[str, object] | None = None) -> None:
        if self.input_path.exists():
            self.input_path.chmod(0o600)
        self.input_path.write_text(
            json.dumps(value or self.request(), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.input_path.chmod(0o444)

    def git_state(
        self,
        *,
        head: str | None = None,
        branch: str | None = None,
        clean: bool = True,
    ) -> validate_closeout.GitState:
        return validate_closeout.GitState(
            head=head or self.head,
            tree="9" * 40,
            branch=branch or self.branch,
            worktree_clean=clean,
        )

    def deployment(
        self,
        *,
        proof: bool = True,
        pid: int = 1234,
        start_ticks: int = 9876,
        invocation_id: str = "8" * 32,
        boot_id: str = "11111111-1111-4111-8111-111111111111",
        uptime_seconds: float = 10_000.0,
        quota_revision: int = 7,
        quota_used: int = 3,
    ) -> validate_closeout.DeploymentEvidence:
        python_runtime = self.python_runtime()
        worker_process = self.worker_process(main_pid=pid)
        return validate_closeout.DeploymentEvidence(
            active=True,
            enabled=True,
            ready=True,
            bindings_current=True,
            lightrag_store_hashes_verified=proof,
            listener_scope="loopback_only",
            backend_port=8001,
            main_pid=pid,
            nrestarts=1,
            process_start_ticks=start_ticks,
            systemd_invocation_id=invocation_id,
            source_head=self.head,
            source_tree="9" * 40,
            source_manifest_sha256="f" * 64,
            source_release="/srv/releases/release-" + "f" * 64,
            source_files_verified=True,
            lightrag_file_count=6,
            lightrag_manifest_sha256="b" * 64,
            lightrag_store_binding_sha256="c" * 64,
            systemd_snapshot_sha256="d" * 64,
            process_snapshot_sha256="6" * 64,
            health_snapshot_sha256="e" * 64,
            listener_snapshot_sha256="a" * 64,
            host_boot={
                "boot_id": boot_id,
                "machine_id_sha256": validate_closeout._machine_id_sha256(
                    "a" * 32
                ),
                "uptime_seconds": uptime_seconds,
                "linger": True,
            },
            shared_quota=self.shared_quota(
                revision=quota_revision, used=quota_used
            ),
            python_runtime=python_runtime,
            worker_process=worker_process,
        )

    @staticmethod
    def shared_quota(*, revision: int = 7, used: int = 3) -> dict[str, object]:
        total = 20
        copy_hash = hashlib.sha256(f"quota-{revision}-{used}".encode()).hexdigest()
        candidate = {
            "present": True,
            "valid": True,
            "revision": revision,
            "sha256": copy_hash,
            "bytes": 4096,
            "mode": "0600",
        }
        return {
            "ready": True,
            "state_revision": revision,
            "configuration_current": True,
            "replicated_revision": True,
            "used": used,
            "remaining": total - used,
            "total_capacity": total,
            "configured_keyset_sha256": "4" * 64,
            "copies": {
                "primary": dict(candidate),
                "backup": dict(candidate),
            },
        }

    def host_front_door_evidence(
        self,
        deployment: validate_closeout.DeploymentEvidence,
        *,
        tracked_implementation: dict[str, str],
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "artifact_type": (
                "where_papers_go_administrator_attested_host_front_door"
            ),
            "recorded_at": "2026-08-31T12:00:30.000000Z",
            "source_head": self.head,
            "source_tree": "9" * 40,
            "boot_id": deployment.host_boot["boot_id"],
            "machine_id_sha256": deployment.host_boot["machine_id_sha256"],
            "nginx": {
                "active": True,
                "enabled": True,
                "binary_path": "/usr/sbin/nginx",
                "binary_sha256": "5" * 64,
                "template_sha256": tracked_implementation[
                    "nginx_template_sha256"
                ],
                "renderer_sha256": tracked_implementation[
                    "deployment_manager_sha256"
                ],
                "main_pid": 2468,
                "systemd_invocation_id": "a" * 32,
                "process_executable_sha256": "5" * 64,
                "version": "nginx/1.24.0",
                "server_name": "papers.example.org",
                "upstream_port": deployment.backend_port,
                "authenticated_gate_port": 18002,
                "listener_scope": "loopback_only",
                "active_config_sha256": "6" * 64,
                "rendered_config_sha256": "6" * 64,
                "configuration_tested": True,
                "certificate_private_key_match": True,
            },
            "tls": {
                "server_name": "papers.example.org",
                "certificate_sha256": "7" * 64,
                "subject_alt_name_match": True,
                "chain_trusted": True,
                "currently_valid": True,
                "not_before": "2026-08-01T00:00:00.000000Z",
                "not_after": "2027-08-01T00:00:00.000000Z",
            },
            "firewall": {
                "manager": "nftables",
                "ruleset_sha256": "8" * 64,
                "backend_port": deployment.backend_port,
                "backend_port_denied": True,
                "authenticated_gate_port": 18002,
                "authenticated_gate_port_denied": True,
                "legacy_port_8765_denied": True,
                "front_door_ports_allowed": [80, 443],
            },
        }

    def lan_front_door_evidence(
        self,
        deployment: validate_closeout.DeploymentEvidence,
        *,
        postboot_challenge_sha256: str,
    ) -> dict[str, object]:
        quota = deployment.as_dict()["shared_quota"]
        return {
            "schema_version": 1,
            "artifact_type": (
                "where_papers_go_administrator_attested_lan_front_door"
            ),
            "recorded_at": "2026-08-31T12:00:45.000000Z",
            "source_head": self.head,
            "source_tree": "9" * 40,
            "boot_id": deployment.host_boot["boot_id"],
            "machine_id_sha256": deployment.host_boot[
                "machine_id_sha256"
            ],
            "postboot_challenge_sha256": postboot_challenge_sha256,
            "source": {
                "machine_id_sha256": validate_closeout._machine_id_sha256(
                    "b" * 32
                ),
                "ip": "172.22.13.156",
                "lan_cidr": "172.22.13.0/24",
            },
            "target": {
                "server_name": "papers.example.org",
                "ip": "172.22.13.155",
                "backend_port": deployment.backend_port,
            },
            "tls": {
                "server_name": "papers.example.org",
                "certificate_sha256": "7" * 64,
                "subject_alt_name_match": True,
                "chain_trusted": True,
                "currently_valid": True,
            },
            "http": {
                "redirect_status": 301,
                "redirect_location": (
                    "https://papers.example.org/api/health/ready"
                ),
                "unauthenticated_status": 401,
                "authenticated_ui_status": 200,
                "authenticated_ready_status": 200,
                "authenticated_detailed_health_status": 200,
                "ready_body": True,
                "detailed_health_ready": True,
                "rate_limited_status": 429,
            },
            "direct_backend": {
                "backend_port": deployment.backend_port,
                "backend_connect_succeeded": False,
                "authenticated_gate_port": 18002,
                "authenticated_gate_connect_succeeded": False,
                "legacy_8765_connect_succeeded": False,
            },
            "provider_guard": {
                "provider_workflows_requested": 0,
                "valid_search_requests_submitted": 0,
                "quota_before": quota,
                "quota_after": quota,
                "quota_unchanged": True,
            },
        }

    def external_evidence(
        self, name: str, public: dict[str, object]
    ) -> validate_closeout.ExternalEvidence:
        payload = json.dumps(public, sort_keys=True).encode()
        return validate_closeout.ExternalEvidence(
            path=self.root / name,
            snapshot=validate_closeout.FileSnapshot(
                1,
                2 if name.startswith("host") else 3,
                len(payload),
                0o444,
                4,
                5,
                hashlib.sha256(payload).hexdigest(),
            ),
            public=public,
        )

    @staticmethod
    def python_runtime(*, home: Path | None = None) -> dict[str, object]:
        manifest_sha256 = "4" * 64
        runtime_root = (
            home / validate_closeout.PYTHON_RUNTIME_ROOT_RELATIVE
            if home is not None
            else Path("/srv/python-runtimes")
        )
        runtime = str(runtime_root / ("python-runtime-" + manifest_sha256))
        import_path = runtime + "/lib/python3.14/site-packages"
        return {
            "runtime": runtime,
            "manifest": runtime + "/python-runtime-manifest.json",
            "manifest_sha256": manifest_sha256,
            "runtime_tree_sha256": "5" * 64,
            "python_executable": runtime + "/bin/python3.14",
            "python_executable_sha256": "6" * 64,
            "python_version": "3.14.5",
            "python_soabi": "cpython-314-x86_64-linux-gnu",
            "python_platform": "linux-x86_64",
            "import_paths": [import_path],
            "elf_audit_sha256": "7" * 64,
            "elf_file_count": 31,
            "system_library_count": 10,
            "system_directory_count": 3,
            "installed_distributions_sha256": "8" * 64,
            "installed_distribution_count": 59,
            "installed_record_entry_count": 4200,
            "omitted_entry_point_count": 7,
            "dependency_lock_sha256": (
                "f5057fc74abe9390884d4fe5a3ab77d01c2aa599ac50bf36d7bacd745c4d0f8b"
            ),
            "wheel_count": 59,
            "system_abi_stat_verified": True,
            "files_verified": True,
        }

    def worker_process(
        self,
        *,
        main_pid: int = 1234,
        worker_pid: int | None = None,
        home: Path | None = None,
    ) -> dict[str, object]:
        python_runtime = self.python_runtime(home=home)
        selected_worker_pid = worker_pid or (
            4321 if main_pid == 1234 else main_pid + 10_000
        )
        return {
            "exact": True,
            "pid": selected_worker_pid,
            "parent_pid": main_pid,
            "start_ticks": 8765,
            "executable_sha256": python_runtime[
                "python_executable_sha256"
            ],
            "proc_exe_verified": True,
            "interpreter": {
                "argv_exact": True,
                "no_site": True,
                "safe_path": True,
                "dont_write_bytecode": True,
            },
            "source": {
                "head": self.head,
                "tree": "9" * 40,
                "manifest_sha256": "f" * 64,
                "files_verified": True,
            },
            "python_runtime": {
                "manifest_sha256": python_runtime["manifest_sha256"],
                "runtime_tree_sha256": python_runtime["runtime_tree_sha256"],
                "python_executable_sha256": python_runtime[
                    "python_executable_sha256"
                ],
                "python_version": python_runtime["python_version"],
                "python_soabi": python_runtime["python_soabi"],
                "python_platform": python_runtime["python_platform"],
                "wheel_count": python_runtime["wheel_count"],
                "elf_audit_sha256": python_runtime["elf_audit_sha256"],
                "system_library_count": python_runtime[
                    "system_library_count"
                ],
                "system_directory_count": python_runtime[
                    "system_directory_count"
                ],
                "files_verified": True,
                "proc_exe_matches": True,
                "system_abi_stat_verified": True,
            },
            "python_executable": python_runtime["python_executable"],
            "process_snapshot_sha256": "0" * 64,
        }

    def fake_test_evidence(
        self,
        _project_root: Path,
        workspace: Path,
        *,
        total: int = validate_closeout.FULL_TEST_COUNT,
        prefix: str = "full",
        model_focused: bool = False,
    ) -> validate_closeout.TestEvidence:
        report_path = workspace / f"{prefix}-unittest-report.json"
        skipped = 0 if model_focused else 2
        report = {
            "schema_version": 2,
            "artifact_type": validate_closeout.TEST_REPORT_ARTIFACT_TYPE,
            "guard_active": True,
            "total": total,
            "passed": total - skipped,
            "skipped": skipped,
            "failures": 0,
            "errors": 0,
            "expected_failures": 0,
            "unexpected_successes": 0,
            "test_id_count": total,
            "test_id_sha256": (
                validate_closeout.MODEL_FOCUSED_TEST_ID_SHA256
                if model_focused
                else validate_closeout.FULL_TEST_ID_SHA256
            ),
            "skipped_test_id_count": skipped,
            "skipped_test_id_sha256": validate_closeout._skipped_test_id_digest(
                ()
                if model_focused
                else validate_closeout.FULL_ALLOWED_SKIP_TEST_IDS[:skipped]
            ),
            "skip_allowlist_sha256": (
                validate_closeout.MODEL_SKIP_ALLOWLIST_SHA256
                if model_focused
                else validate_closeout.FULL_SKIP_ALLOWLIST_SHA256
            ),
        }
        report_path.write_text(json.dumps(report) + "\n", encoding="utf-8")
        report_path.chmod(0o600)
        audit_path = workspace / f"{prefix}-nonloopback-socket-attempts.log"
        audit_path.write_bytes(b"")
        audit_path.chmod(0o600)
        _unused, report_snapshot = validate_closeout._inspect_regular_file(
            report_path, capture=False, expected_mode=0o600
        )
        _unused, audit_snapshot = validate_closeout._inspect_regular_file(
            audit_path, capture=False, expected_mode=0o600
        )
        public = validate_closeout._validate_test_report(
            report,
            expected_total=(
                validate_closeout.MODEL_FOCUSED_TEST_COUNT
                if model_focused
                else None
            ),
        )
        public.update(
            {
                "report_sha256": report_snapshot.sha256,
                "offline_guard_audit_sha256": audit_snapshot.sha256,
            }
        )
        if model_focused:
            public["model_runtime_interpreter_sha256"] = "9" * 64
        return validate_closeout.TestEvidence(
            public=public,
            report_path=report_path,
            report_snapshot=report_snapshot,
            audit_path=audit_path,
            audit_snapshot=audit_snapshot,
        )

    def fake_model_test_evidence(
        self, project_root: Path, workspace: Path
    ) -> validate_closeout.TestEvidence:
        return self.fake_test_evidence(
            project_root,
            workspace,
            total=validate_closeout.MODEL_FOCUSED_TEST_COUNT,
            prefix="model",
            model_focused=True,
        )

    def create_patches(self, **overrides):
        values = {
            "_git_state": patch.object(
                validate_closeout, "_git_state", return_value=self.git_state()
            ),
            "_verify_tracked_helpers": patch.object(
                validate_closeout,
                "_verify_tracked_helpers",
                return_value=self.tracked_implementation(),
            ),
            "_run_full_test_suite": patch.object(
                validate_closeout,
                "_run_full_test_suite",
                side_effect=self.fake_test_evidence,
            ),
            "_run_model_focused_suite": patch.object(
                validate_closeout,
                "_run_model_focused_suite",
                side_effect=self.fake_model_test_evidence,
            ),
            "_deployment_state": patch.object(
                validate_closeout,
                "_deployment_state",
                return_value=self.deployment(),
            ),
            "_utc_stamp": patch.object(
                validate_closeout,
                "_utc_stamp",
                return_value=(
                    "2026-08-31T12:00:00.000000Z",
                    "20260831T120000000000Z",
                ),
            ),
        }
        values.update(overrides)
        return tuple(values.values())

    def tracked_implementation(self) -> dict[str, str]:
        return {
            name: (
                "2" * 64
                if name == "offline_guard_sha256"
                else (
                    self.python_runtime()["dependency_lock_sha256"]
                    if name == "selected_wheel_lock_sha256"
                    else "1" * 64
                )
            )
            for name in validate_closeout.TRACKED_IMPLEMENTATION_FILES
        }

    def create(self):
        with self._stack(self.create_patches()):
            return validate_closeout.create_closeout(
                input_path=self.input_path,
                project_root=self.root,
                output_root=self.output_root,
            )

    @staticmethod
    def _stack(patches):
        class Stack:
            def __enter__(self):
                for item in patches:
                    item.start()

            def __exit__(self, exc_type, exc_value, traceback):
                for item in reversed(patches):
                    item.stop()

        return Stack()

    def test_publishes_fixed_aggregate_record_and_preserves_legacy(self) -> None:
        legacy = self.root / validate_closeout.REQUIRED_ARTIFACTS[
            "legacy_closeout_summary"
        ]
        legacy_hash = hashlib.sha256(legacy.read_bytes()).hexdigest()
        self.write_request()
        target, payload, summary_hash = self.create()

        summary_path = target / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        self.assertEqual(summary, payload)
        self.assertEqual(
            validate_closeout.OUTPUT_PREFIX,
            "final_delivery_validation_v5_",
        )
        self.assertEqual(
            validate_closeout.LEGACY_OUTPUT_PREFIX,
            "final_delivery_validation_v4_",
        )
        self.assertEqual(summary["schema_version"], 6)
        self.assertEqual(set(summary), validate_closeout.OUTPUT_KEYS)
        self.assertEqual(summary["git"]["head"], self.head)
        self.assertEqual(summary["git"]["tree"], "9" * 40)
        self.assertEqual(summary["git"]["branch"], self.branch)
        self.assertEqual(
            summary["tests"][validate_closeout.FULL_TEST_KEY]["total"],
            validate_closeout.FULL_TEST_COUNT,
        )
        self.assertEqual(
            summary["tests"][validate_closeout.MODEL_TEST_KEY]["total"], 6
        )
        self.assertEqual(summary["tests"]["official_weight_inference_tests"], 0)
        self.assertEqual(
            set(summary["tracked_implementation"]),
            set(validate_closeout.TRACKED_IMPLEMENTATION_FILES),
        )
        self.assertEqual(
            set(summary["critical_artifacts"]),
            set(validate_closeout.REQUIRED_ARTIFACTS),
        )
        self.assertTrue(summary["deployment"]["lightrag_store_hashes_verified"])
        self.assertTrue(summary["deployment"]["host_boot"]["linger"])
        self.assertTrue(summary["deployment"]["shared_quota"]["ready"])
        self.assertEqual(summary["deployment"]["backend_port"], 8001)
        self.assertTrue(summary["deployment"]["python_runtime"]["files_verified"])
        self.assertEqual(
            set(summary["deployment"]["python_runtime"]),
            validate_closeout.PYTHON_RUNTIME_OUTPUT_KEYS,
        )
        self.assertTrue(summary["deployment"]["worker_process"]["exact"])
        self.assertEqual(
            summary["external_calls"][
                "guard_observed_nonloopback_socket_attempts"
            ],
            0,
        )
        self.assertFalse(summary["excluded_actions"]["live_formal500_executed"])
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o555)
        self.assertEqual(os.stat(summary_path).st_mode & 0o777, 0o444)
        self.assertEqual(hashlib.sha256(summary_path.read_bytes()).hexdigest(), summary_hash)
        self.assertEqual(hashlib.sha256(legacy.read_bytes()).hexdigest(), legacy_hash)

    def test_request_cannot_self_report_tests_deployment_or_free_fields(self) -> None:
        for field, value in (
            ("tests", {"full_unittest": {"total": 1, "passed": 1}}),
            ("deployment", {"ready": True}),
            ("external_calls", {"total": 0}),
            ("free_identifier", "per-query-value"),
        ):
            with self.subTest(field=field):
                request = self.request()
                request[field] = value
                with self.assertRaisesRegex(
                    validate_closeout.CloseoutValidationError,
                    "closeout request keys mismatch",
                ):
                    validate_closeout._validate_request(request)

        request = self.request()
        request["artifacts"]["free_artifact_name"] = "0" * 64
        with self.assertRaisesRegex(
            validate_closeout.CloseoutValidationError,
            "request artifacts keys mismatch",
        ):
            validate_closeout._validate_request(request)

        request = self.request()
        request["artifacts"]["future_dataset_manifest"] = "0" * 64
        with self.assertRaisesRegex(
            validate_closeout.CloseoutValidationError, "fixed known SHA-256"
        ):
            validate_closeout._validate_request(request)

    def test_forged_one_of_one_runner_result_is_rejected(self) -> None:
        report = {
            "schema_version": 2,
            "artifact_type": validate_closeout.TEST_REPORT_ARTIFACT_TYPE,
            "guard_active": True,
            "total": 1,
            "passed": 1,
            "skipped": 0,
            "failures": 0,
            "errors": 0,
            "expected_failures": 0,
            "unexpected_successes": 0,
            "test_id_count": 1,
            "test_id_sha256": "f" * 64,
            "skipped_test_id_count": 0,
            "skipped_test_id_sha256": validate_closeout._skipped_test_id_digest(()),
            "skip_allowlist_sha256": validate_closeout.FULL_SKIP_ALLOWLIST_SHA256,
        }
        with self.assertRaisesRegex(
            validate_closeout.CloseoutValidationError, "exactly 489"
        ):
            validate_closeout._validate_test_report(report)

    def test_full_report_requires_fixed_test_id_fingerprint(self) -> None:
        report = {
            "schema_version": 2,
            "artifact_type": validate_closeout.TEST_REPORT_ARTIFACT_TYPE,
            "guard_active": True,
            "total": validate_closeout.FULL_TEST_COUNT,
            "passed": validate_closeout.FULL_TEST_COUNT,
            "skipped": 0,
            "failures": 0,
            "errors": 0,
            "expected_failures": 0,
            "unexpected_successes": 0,
            "test_id_count": validate_closeout.FULL_TEST_COUNT,
            "test_id_sha256": "f" * 64,
            "skipped_test_id_count": 0,
            "skipped_test_id_sha256": validate_closeout._skipped_test_id_digest(()),
            "skip_allowlist_sha256": validate_closeout.FULL_SKIP_ALLOWLIST_SHA256,
        }
        with self.assertRaisesRegex(
            validate_closeout.CloseoutValidationError, "fingerprint"
        ):
            validate_closeout._validate_test_report(report)
        report.update(
            passed=0,
            skipped=validate_closeout.FULL_TEST_COUNT,
            test_id_sha256=validate_closeout.FULL_TEST_ID_SHA256,
            skipped_test_id_count=validate_closeout.FULL_TEST_COUNT,
            skipped_test_id_sha256="e" * 64,
        )
        with self.assertRaisesRegex(
            validate_closeout.CloseoutValidationError, "fixed allowlist"
        ):
            validate_closeout._validate_test_report(report)

    def test_model_focused_report_requires_exact_six_of_six(self) -> None:
        report = {
            "schema_version": 2,
            "artifact_type": validate_closeout.TEST_REPORT_ARTIFACT_TYPE,
            "guard_active": True,
            "total": 5,
            "passed": 5,
            "skipped": 0,
            "failures": 0,
            "errors": 0,
            "expected_failures": 0,
            "unexpected_successes": 0,
            "test_id_count": 5,
            "test_id_sha256": "f" * 64,
            "skipped_test_id_count": 0,
            "skipped_test_id_sha256": validate_closeout.MODEL_SKIPPED_TEST_ID_SHA256,
            "skip_allowlist_sha256": validate_closeout.MODEL_SKIP_ALLOWLIST_SHA256,
        }
        with self.assertRaisesRegex(
            validate_closeout.CloseoutValidationError, "exactly 6"
        ):
            validate_closeout._validate_test_report(report, expected_total=6)
        report.update(total=6, passed=6, test_id_count=6)
        with self.assertRaisesRegex(
            validate_closeout.CloseoutValidationError, "fingerprint"
        ):
            validate_closeout._validate_test_report(report, expected_total=6)

    def test_main_detached_and_branch_mismatch_are_rejected(self) -> None:
        for value in (
            "main",
            "agent/../main",
            "HEAD",
            "feature/test",
            "agent/different",
        ):
            with self.subTest(branch=value):
                with self.assertRaises(validate_closeout.CloseoutValidationError):
                    validate_closeout._validate_branch(value)

        self.write_request()
        patches = self.create_patches(
            _git_state=patch.object(
                validate_closeout,
                "_git_state",
                return_value=self.git_state(branch="agent/different"),
            )
        )
        with self._stack(patches):
            with self.assertRaisesRegex(
                validate_closeout.CloseoutValidationError,
                "does not match current HEAD/branch",
            ):
                validate_closeout.create_closeout(
                    input_path=self.input_path,
                    project_root=self.root,
                    output_root=self.output_root,
                )

    def test_input_and_artifacts_require_exact_read_only_mode(self) -> None:
        self.write_request()
        self.input_path.chmod(0o644)
        with self.assertRaisesRegex(
            validate_closeout.CloseoutValidationError, "mode mismatch"
        ):
            validate_closeout.create_closeout(
                input_path=self.input_path,
                project_root=self.root,
                output_root=self.output_root,
            )

        self.write_request()
        name = next(iter(validate_closeout.REQUIRED_ARTIFACTS))
        artifact = self.root / validate_closeout.REQUIRED_ARTIFACTS[name]
        artifact.chmod(0o644)
        with self._stack(self.create_patches()):
            with self.assertRaisesRegex(
                validate_closeout.CloseoutValidationError, "mode mismatch"
            ):
                validate_closeout.create_closeout(
                    input_path=self.input_path,
                    project_root=self.root,
                    output_root=self.output_root,
                )

    def test_symlink_and_lstat_open_replacement_are_rejected(self) -> None:
        regular = self.root / "regular.json"
        regular.write_bytes(b"aggregate\n")
        regular.chmod(0o444)
        link = self.root / "link.json"
        link.symlink_to(regular)
        with self.assertRaisesRegex(
            validate_closeout.CloseoutValidationError, "non-symlink"
        ):
            validate_closeout._inspect_regular_file(
                link, capture=False, expected_mode=0o444
            )

        replacement = self.root / "replacement.json"
        original_open = os.open
        replaced = False

        def replace_before_open(path, flags, *args):
            nonlocal replaced
            candidate = Path(path)
            if candidate == regular and not replaced:
                replaced = True
                regular.rename(self.root / "original.json")
                replacement.write_bytes(b"aggregate\n")
                replacement.chmod(0o444)
                replacement.rename(regular)
            return original_open(path, flags, *args)

        with patch.object(validate_closeout.os, "open", side_effect=replace_before_open):
            with self.assertRaisesRegex(
                validate_closeout.CloseoutValidationError,
                "replaced between lstat and open",
            ):
                validate_closeout._inspect_regular_file(
                    regular, capture=False, expected_mode=0o444
                )

    def test_path_replacement_after_read_is_rejected(self) -> None:
        path = self.root / "after-read.json"
        path.write_bytes(b"aggregate\n")
        path.chmod(0o444)
        original_read = os.read
        replaced = False

        def replace_after_read(descriptor, size):
            nonlocal replaced
            data = original_read(descriptor, size)
            if data and not replaced:
                replaced = True
                path.rename(self.root / "after-read.original")
                path.write_bytes(b"aggregate\n")
                path.chmod(0o444)
            return data

        with patch.object(validate_closeout.os, "read", side_effect=replace_after_read):
            with self.assertRaisesRegex(
                validate_closeout.CloseoutValidationError,
                "changed while being read|path was replaced after read",
            ):
                validate_closeout._inspect_regular_file(
                    path, capture=False, expected_mode=0o444
                )

    def test_identical_artifact_replacement_between_checks_fails_closed(self) -> None:
        self.write_request()
        original_verify = validate_closeout._verify_artifacts
        calls = 0

        def replace_after_first(project_root, expected):
            nonlocal calls
            evidence = original_verify(project_root, expected)
            calls += 1
            if calls == 1:
                name = next(iter(validate_closeout.REQUIRED_ARTIFACTS))
                path = self.root / validate_closeout.REQUIRED_ARTIFACTS[name]
                data = path.read_bytes()
                path.rename(path.with_suffix(".original"))
                path.write_bytes(data)
                path.chmod(0o444)
            return evidence

        patches = self.create_patches(
            _verify_artifacts=patch.object(
                validate_closeout,
                "_verify_artifacts",
                side_effect=replace_after_first,
            )
        )
        with self._stack(patches):
            with self.assertRaisesRegex(
                validate_closeout.CloseoutValidationError,
                "critical artifacts changed",
            ):
                validate_closeout.create_closeout(
                    input_path=self.input_path,
                    project_root=self.root,
                    output_root=self.output_root,
                )

    def test_post_publish_deployment_drift_is_preserved_as_failed(self) -> None:
        self.write_request()
        patches = self.create_patches(
            _deployment_state=patch.object(
                validate_closeout,
                "_deployment_state",
                side_effect=[self.deployment(), self.deployment(pid=9999)],
            )
        )
        with self._stack(patches):
            with self.assertRaisesRegex(
                validate_closeout.CloseoutValidationError,
                "deployment changed before closeout publication",
            ):
                validate_closeout.create_closeout(
                    input_path=self.input_path,
                    project_root=self.root,
                    output_root=self.output_root,
                )
        self.assertEqual(
            list(self.output_root.glob(f"{validate_closeout.OUTPUT_PREFIX}*")), []
        )
        failed = list(self.output_root.glob(".*.failed-*"))
        self.assertEqual(len(failed), 1)
        self.assertTrue((failed[0] / "summary.json").is_file())

    def test_same_head_success_cannot_be_replayed_with_new_timestamp(self) -> None:
        legacy_directory = self.output_root / (
            validate_closeout.LEGACY_OUTPUT_PREFIX
            + "20260831T115900000000Z-"
            + self.head[:12]
        )
        legacy_directory.mkdir()
        legacy_summary = legacy_directory / "summary.json"
        legacy_summary.write_text(
            json.dumps(
                {
                    "schema_version": 5,
                    "artifact_type": validate_closeout.OUTPUT_ARTIFACT_TYPE,
                    "git": {"head": self.head},
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        legacy_summary.chmod(0o444)
        legacy_directory.chmod(0o555)
        legacy_bytes = legacy_summary.read_bytes()
        self.write_request()
        with self._stack(self.create_patches()):
            validate_closeout.create_closeout(
                input_path=self.input_path,
                project_root=self.root,
                output_root=self.output_root,
            )

        second_patches = self.create_patches(
            _run_full_test_suite=patch.object(
                validate_closeout,
                "_run_full_test_suite",
                side_effect=AssertionError("runner must not execute during replay"),
            ),
            _run_model_focused_suite=patch.object(
                validate_closeout,
                "_run_model_focused_suite",
                side_effect=AssertionError("runner must not execute during replay"),
            ),
        )
        with self._stack(second_patches):
            with self.assertRaisesRegex(
                validate_closeout.CloseoutValidationError,
                "already has a successful v5 closeout",
            ):
                validate_closeout.create_closeout(
                    input_path=self.input_path,
                    project_root=self.root,
                    output_root=self.output_root,
                )
        self.assertEqual(legacy_summary.read_bytes(), legacy_bytes)

    def test_hidden_building_is_checked_before_atomic_publication(self) -> None:
        name = (
            validate_closeout.OUTPUT_PREFIX
            + "20260831T120000000000Z-"
            + self.head[:12]
        )
        target = self.output_root / name
        observations: list[str] = []

        def final_checks() -> None:
            self.assertFalse(target.exists())
            building = list(self.output_root.glob(f".{name}.building-*"))
            self.assertEqual(len(building), 1)
            self.assertEqual(stat.S_IMODE(building[0].stat().st_mode), 0o555)
            self.assertEqual(
                stat.S_IMODE((building[0] / "summary.json").stat().st_mode),
                0o444,
            )
            observations.append("checked")

        published, _digest = validate_closeout._write_new_directory(
            self.output_root,
            name,
            {"aggregate": True},
            final_checks=final_checks,
        )
        self.assertEqual(observations, ["checked"])
        self.assertEqual(published, target)
        self.assertTrue((target / "summary.json").is_file())
        self.assertEqual(list(self.output_root.glob(".*.building-*")), [])

    def test_same_head_post_deployment_reproof_is_independent_and_immutable(self) -> None:
        self.write_request()
        base_target, _payload, _digest = self.create()
        base_summary = base_target / "summary.json"
        base_bytes = base_summary.read_bytes()
        legacy_directory = self.output_root / (
            validate_closeout.LEGACY_OUTPUT_PREFIX
            + "20260831T115900000000Z-"
            + self.head[:12]
        )
        legacy_directory.mkdir()
        legacy_summary = legacy_directory / "summary.json"
        legacy_summary.write_text('{"schema_version":5}\n', encoding="utf-8")
        legacy_summary.chmod(0o444)
        legacy_directory.chmod(0o555)
        with self.assertRaisesRegex(
            validate_closeout.CloseoutValidationError,
            "canonical v5/schema-6 closeout",
        ):
            validate_closeout._load_base_closeout(
                legacy_summary, output_root=self.output_root
            )
        updated = self.deployment(
            pid=4321,
            start_ticks=5555,
            invocation_id="7" * 32,
        )
        patches = self.create_patches(
            _deployment_state=patch.object(
                validate_closeout, "_deployment_state", return_value=updated
            ),
            _utc_stamp=patch.object(
                validate_closeout,
                "_utc_stamp",
                return_value=(
                    "2026-08-31T12:01:00.000000Z",
                    "20260831T120100000000Z",
                ),
            ),
        )
        with self._stack(patches):
            target, payload, digest = validate_closeout.create_deployment_reproof(
                base_summary_path=base_summary,
                project_root=self.root,
                output_root=self.output_root,
            )
        self.assertEqual(base_summary.read_bytes(), base_bytes)
        self.assertTrue(target.name.startswith(validate_closeout.REPROOF_PREFIX))
        self.assertEqual(set(payload), validate_closeout.REPROOF_OUTPUT_KEYS)
        self.assertEqual(
            set(payload["base_closeout"]), validate_closeout.REPROOF_BASE_KEYS
        )
        self.assertEqual(payload["git"]["head"], self.head)
        self.assertEqual(payload["schema_version"], 3)
        self.assertEqual(
            validate_closeout.REPROOF_PREFIX,
            "final_delivery_deployment_reproof_v3_",
        )
        self.assertEqual(
            validate_closeout.LEGACY_REPROOF_PREFIX,
            "final_delivery_deployment_reproof_v2_",
        )
        self.assertEqual(payload["deployment"]["main_pid"], 4321)
        self.assertTrue(payload["publication"]["same_head_replay_supported"])
        self.assertEqual(
            hashlib.sha256((target / "summary.json").read_bytes()).hexdigest(),
            digest,
        )
        second_updated = self.deployment(
            pid=5678,
            start_ticks=6666,
            invocation_id="6" * 32,
        )
        second_patches = self.create_patches(
            _deployment_state=patch.object(
                validate_closeout,
                "_deployment_state",
                return_value=second_updated,
            ),
            _utc_stamp=patch.object(
                validate_closeout,
                "_utc_stamp",
                return_value=(
                    "2026-08-31T12:02:00.000000Z",
                    "20260831T120200000000Z",
                ),
            ),
        )
        with self._stack(second_patches):
            second_target, second_payload, _second_digest = (
                validate_closeout.create_deployment_reproof(
                    base_summary_path=base_summary,
                    project_root=self.root,
                    output_root=self.output_root,
                )
            )
        self.assertNotEqual(second_target, target)
        self.assertEqual(second_payload["git"]["head"], self.head)
        self.assertEqual(second_payload["deployment"]["main_pid"], 5678)
        self.assertEqual(base_summary.read_bytes(), base_bytes)
        self.assertTrue(target.is_dir())
        self.assertTrue(second_target.is_dir())

        strict_deployment = self.deployment(
            pid=6789,
            start_ticks=7777,
            invocation_id="5" * 32,
            boot_id="22222222-2222-4222-8222-222222222222",
            uptime_seconds=90.0,
            quota_revision=8,
            quota_used=3,
        )
        tracked_implementation = self.tracked_implementation()
        host_raw = self.host_front_door_evidence(
            strict_deployment,
            tracked_implementation=tracked_implementation,
        )
        host_public = validate_closeout._validate_host_front_door_evidence(
            host_raw,
            git_state=self.git_state(),
            deployment=strict_deployment,
            tracked_implementation=tracked_implementation,
        )
        host_record = self.external_evidence("host.json", host_public)
        postboot_challenge = validate_closeout._postboot_challenge_sha256(
            base_summary_sha256=_digest,
            deployment=strict_deployment,
            host_evidence_sha256=host_record.snapshot.sha256,
        )
        lan_raw = self.lan_front_door_evidence(
            strict_deployment,
            postboot_challenge_sha256=postboot_challenge,
        )
        lan_public = validate_closeout._validate_lan_front_door_evidence(
            lan_raw,
            git_state=self.git_state(),
            deployment=strict_deployment,
            host_evidence=host_public,
            postboot_challenge_sha256=postboot_challenge,
        )
        lan_record = self.external_evidence("lan.json", lan_public)
        strict_patches = self.create_patches(
            _deployment_state=patch.object(
                validate_closeout,
                "_deployment_state",
                return_value=strict_deployment,
            ),
            _load_host_acceptance_evidence=patch.object(
                validate_closeout,
                "_load_host_acceptance_evidence",
                return_value=host_record,
            ),
            _load_lan_acceptance_evidence=patch.object(
                validate_closeout,
                "_load_lan_acceptance_evidence",
                return_value=lan_record,
            ),
            _utc_stamp=patch.object(
                validate_closeout,
                "_utc_stamp",
                return_value=(
                    "2026-08-31T12:01:00.000000Z",
                    "20260831T120100000000Z",
                ),
            ),
        )
        with self._stack(strict_patches):
            strict_target, strict_payload, strict_digest = (
                validate_closeout.create_post_reboot_reproof(
                    base_summary_path=base_summary,
                    host_front_door_evidence_path=host_record.path,
                    lan_front_door_evidence_path=lan_record.path,
                    project_root=self.root,
                    output_root=self.output_root,
                )
            )
        self.assertTrue(
            strict_target.name.startswith(validate_closeout.STRICT_REPROOF_PREFIX)
        )
        self.assertEqual(strict_payload["schema_version"], 1)
        self.assertEqual(
            strict_payload["artifact_type"],
            validate_closeout.STRICT_REPROOF_ARTIFACT_TYPE,
        )
        self.assertEqual(
            strict_payload["status"],
            "administrator_attested_lan_front_door_complete",
        )
        self.assertEqual(
            strict_payload["reproof_kind"],
            "administrator_attested_lan_front_door",
        )
        self.assertEqual(
            set(strict_payload), validate_closeout.STRICT_REPROOF_OUTPUT_KEYS
        )
        self.assertTrue(strict_payload["restart_transition"]["boot_id_changed"])
        self.assertTrue(strict_payload["restart_transition"]["quota_non_regression"])
        self.assertEqual(
            strict_payload["front_door"]["lan"]["http"]["rate_limited_status"],
            429,
        )
        self.assertEqual(
            strict_payload["front_door"]["postboot_challenge_sha256"],
            postboot_challenge,
        )
        self.assertEqual(
            hashlib.sha256((strict_target / "summary.json").read_bytes()).hexdigest(),
            strict_digest,
        )
        strict_serialized = json.dumps(strict_payload, sort_keys=True)
        for secret in (
            "test-password",
            "Authorization: Basic",
            "nginx-proxy-test-token",
            "PRIVATE KEY-----",
        ):
            self.assertNotIn(secret, strict_serialized)
        self.assertEqual(base_summary.read_bytes(), base_bytes)

        transition_failures = (
            self.deployment(
                pid=6789,
                start_ticks=7777,
                invocation_id="5" * 32,
            ),
            self.deployment(
                pid=1234,
                start_ticks=7777,
                invocation_id="5" * 32,
                boot_id="22222222-2222-4222-8222-222222222222",
            ),
            self.deployment(
                pid=6789,
                start_ticks=7777,
                invocation_id="5" * 32,
                boot_id="22222222-2222-4222-8222-222222222222",
                quota_revision=8,
                quota_used=2,
            ),
        )
        different_host = self.deployment(
            pid=6789,
            start_ticks=7777,
            invocation_id="5" * 32,
            boot_id="22222222-2222-4222-8222-222222222222",
            quota_revision=8,
        )
        different_host.host_boot["machine_id_sha256"] = "e" * 64
        for invalid_deployment in (*transition_failures, different_host):
            with self.subTest(invalid_transition=invalid_deployment.host_boot):
                with self.assertRaisesRegex(
                    validate_closeout.CloseoutValidationError,
                    "strict post-reboot transition",
                ):
                    validate_closeout._post_reboot_transition(
                        base_deployment=_payload["deployment"],
                        current=invalid_deployment,
                    )

        same_revision_changed = self.shared_quota(revision=7, used=3)
        same_revision_changed["copies"]["primary"]["sha256"] = "e" * 64
        same_revision_changed["copies"]["backup"]["sha256"] = "e" * 64
        self.assertTrue(
            validate_closeout._quota_non_regressed(
                self.shared_quota(revision=7, used=2),
                self.shared_quota(revision=7, used=2),
            )
        )
        self.assertFalse(
            validate_closeout._quota_non_regressed(
                self.shared_quota(revision=7, used=2),
                same_revision_changed,
            )
        )
        self.assertTrue(
            validate_closeout._quota_non_regressed(
                self.shared_quota(revision=7, used=2),
                self.shared_quota(revision=8, used=3),
            )
        )

        invalid_lan_documents = []
        for path, value in (
            (("http", "redirect_status"), 302),
            (("http", "unauthenticated_status"), 200),
            (("http", "rate_limited_status"), 200),
            (("tls", "chain_trusted"), False),
            (("direct_backend", "backend_connect_succeeded"), True),
            (("direct_backend", "authenticated_gate_connect_succeeded"), True),
            (("direct_backend", "authenticated_gate_port"), 18003),
            (("provider_guard", "provider_workflows_requested"), 1),
            (("source_head",), "e" * 40),
            (("source_tree",), "e" * 40),
            (("boot_id",), "11111111-1111-4111-8111-111111111111"),
            (("machine_id_sha256",), "e" * 64),
            (
                ("source", "machine_id_sha256"),
                strict_deployment.host_boot["machine_id_sha256"],
            ),
            (("postboot_challenge_sha256",), "d" * 64),
            (("recorded_at",), "2026-08-31T12:00:29.000000Z"),
        ):
            candidate = json.loads(json.dumps(lan_raw))
            if len(path) == 1:
                candidate[path[0]] = value
            else:
                candidate[path[0]][path[1]] = value
            invalid_lan_documents.append(candidate)
        for invalid_lan in invalid_lan_documents:
            with self.subTest(invalid_lan=invalid_lan):
                with self.assertRaises(validate_closeout.CloseoutValidationError):
                    validate_closeout._validate_lan_front_door_evidence(
                        invalid_lan,
                        git_state=self.git_state(),
                        deployment=strict_deployment,
                        host_evidence=host_public,
                        postboot_challenge_sha256=postboot_challenge,
                    )

        invalid_host_documents = []
        for path, value in (
            (("nginx", "active_config_sha256"), "a" * 64),
            (("nginx", "template_sha256"), "a" * 64),
            (("nginx", "renderer_sha256"), "a" * 64),
            (
                ("nginx", "authenticated_gate_port"),
                strict_deployment.backend_port,
            ),
            (("nginx", "authenticated_gate_port"), 1023),
            (("nginx", "authenticated_gate_port"), 8765),
            (("nginx", "listener_scope"), "lan"),
            (("tls", "currently_valid"), False),
            (("firewall", "backend_port_denied"), False),
            (("firewall", "authenticated_gate_port_denied"), False),
        ):
            candidate = json.loads(json.dumps(host_raw))
            candidate[path[0]][path[1]] = value
            invalid_host_documents.append(candidate)
        for invalid_host in invalid_host_documents:
            with self.subTest(invalid_host=invalid_host):
                with self.assertRaises(validate_closeout.CloseoutValidationError):
                    validate_closeout._validate_host_front_door_evidence(
                        invalid_host,
                        git_state=self.git_state(),
                        deployment=strict_deployment,
                        tracked_implementation=tracked_implementation,
                    )

        if os.geteuid() != 0:
            untrusted = self.root / "untrusted-front-door.json"
            untrusted.write_text("{}\n", encoding="utf-8")
            untrusted.chmod(0o444)
            with self.assertRaisesRegex(
                validate_closeout.CloseoutValidationError, "root-owned"
            ):
                validate_closeout._load_root_owned_evidence(
                    untrusted, validator=lambda value: dict(value)
                )

        later_uptime = self.deployment(uptime_seconds=10_001.0)
        self.assertTrue(
            validate_closeout._deployment_observations_match(
                self.deployment(), later_uptime
            )
        )
        self.assertFalse(
            validate_closeout._deployment_observations_match(
                later_uptime, self.deployment()
            )
        )

        drifted_host_record = validate_closeout.ExternalEvidence(
            path=host_record.path,
            snapshot=validate_closeout.FileSnapshot(
                host_record.snapshot.device,
                host_record.snapshot.inode,
                host_record.snapshot.size,
                host_record.snapshot.mode,
                host_record.snapshot.mtime_ns + 1,
                host_record.snapshot.ctime_ns + 1,
                host_record.snapshot.sha256,
            ),
            public=host_record.public,
        )
        drift_patches = self.create_patches(
            _deployment_state=patch.object(
                validate_closeout,
                "_deployment_state",
                return_value=strict_deployment,
            ),
            _load_host_acceptance_evidence=patch.object(
                validate_closeout,
                "_load_host_acceptance_evidence",
                side_effect=[host_record, drifted_host_record],
            ),
            _load_lan_acceptance_evidence=patch.object(
                validate_closeout,
                "_load_lan_acceptance_evidence",
                return_value=lan_record,
            ),
            _utc_stamp=patch.object(
                validate_closeout,
                "_utc_stamp",
                return_value=(
                    "2026-08-31T12:02:00.000000Z",
                    "20260831T120200000000Z",
                ),
            ),
        )
        with self._stack(drift_patches):
            with self.assertRaisesRegex(
                validate_closeout.CloseoutValidationError,
                "front-door evidence changed",
            ):
                validate_closeout.create_post_reboot_reproof(
                    base_summary_path=base_summary,
                    host_front_door_evidence_path=host_record.path,
                    lan_front_door_evidence_path=lan_record.path,
                    project_root=self.root,
                    output_root=self.output_root,
                )
        self.assertFalse(
            (
                self.output_root
                / (
                    validate_closeout.STRICT_REPROOF_PREFIX
                    + "20260831T120200000000Z-"
                    + self.head[:12]
                )
            ).exists()
        )
        self.assertTrue(
            any(
                path.name.startswith(
                    "."
                    + validate_closeout.STRICT_REPROOF_PREFIX
                    + "20260831T120200000000Z-"
                )
                for path in self.output_root.glob(".*.failed-*")
            )
        )

    def test_deployment_probe_requires_true_lightrag_and_one_process_identity(self) -> None:
        test_home = self.root / "passwd-home"
        test_home.mkdir(mode=0o700)
        test_home.chmod(0o700)
        fragment = test_home / validate_closeout.SYSTEMD_FRAGMENT_RELATIVE
        source_root = test_home / validate_closeout.SOURCE_RELEASE_ROOT_RELATIVE
        runtime_root = test_home / validate_closeout.PYTHON_RUNTIME_ROOT_RELATIVE
        fragment.parent.mkdir(parents=True)
        source_root.mkdir(parents=True)
        runtime_root.mkdir(parents=True)
        for private_parent in (
            test_home / ".local",
            test_home / ".local/lib",
            test_home / ".local/lib/where-papers-go",
        ):
            private_parent.chmod(0o700)
        source_root.chmod(0o700)
        runtime_root.chmod(0o700)
        api_token_file = test_home / ".config/where-papers-go/backend.token"
        api_token_file.parent.mkdir(parents=True)
        (test_home / ".config").chmod(0o700)
        api_token_file.parent.chmod(0o700)
        api_token_file.write_text("t" * 48 + "\n", encoding="ascii")
        api_token_file.chmod(0o600)
        base_health = {
            "status": "ready",
            "ready": True,
            "backend": validate_closeout.EXPECTED_BACKEND,
            "checks": {
                name: True
                for name in (
                    "graph",
                    "vectors",
                    "lightrag_manifest",
                    "api_config",
                    "search_quota_audit",
                    "worker",
                    "worker_process_identity",
                    "bindings_current",
                    "runtime_contract",
                    "source_identity",
                    "python_runtime_identity",
                )
            },
            "runtime": {
                "persistent_worker": True,
                "process_ready": True,
                "bindings_current": True,
                "ready": True,
                "runtime_manifest": {
                    "ready": True,
                    "actual_sha256": "b" * 64,
                },
            },
            "source": {
                "ready": True,
                "head": self.head,
                "tree": "9" * 40,
                "manifest_sha256": "f" * 64,
                "files_verified": True,
                "file_count": 185,
                "process_pid": 1234,
                "process_start_ticks": 9876,
            },
            "config": {
                "search_quota_audit": {
                    **self.shared_quota(),
                    "required": True,
                    "status_counts": {"available": 2},
                }
            },
        }
        python_identity = self.python_runtime(home=test_home)
        base_health["python_runtime"] = {
            "ready": True,
            "manifest_sha256": python_identity["manifest_sha256"],
            "runtime_tree_sha256": python_identity["runtime_tree_sha256"],
            "python_executable_sha256": python_identity[
                "python_executable_sha256"
            ],
            "python_version": python_identity["python_version"],
            "python_soabi": python_identity["python_soabi"],
            "python_platform": python_identity["python_platform"],
            "wheel_count": python_identity["wheel_count"],
            "elf_audit_sha256": python_identity["elf_audit_sha256"],
            "system_library_count": python_identity["system_library_count"],
            "system_directory_count": python_identity[
                "system_directory_count"
            ],
            "system_abi_stat_verified": True,
            "files_verified": True,
            "proc_exe_matches": True,
            "process_pid": 1234,
            "process_start_ticks": 9876,
        }
        worker_health = self.worker_process(
            main_pid=1234, worker_pid=4321, home=test_home
        )
        base_health["runtime"]["worker_process"] = {
            name: worker_health[name]
            for name in validate_closeout.HEALTH_WORKER_PROCESS_KEYS
        }
        with self.assertRaisesRegex(
            validate_closeout.CloseoutValidationError, "six-file LightRAG"
        ):
            validate_closeout._parse_health_snapshot(
                json.dumps(base_health).encode()
            )
        base_health["checks"]["lightrag_store_hashes"] = True
        base_health["runtime"]["lightrag_store_verification"] = {
            "required": True,
            "verified": True,
            "file_count": 6,
            "manifest_sha256": "b" * 64,
            "store_binding_sha256": "c" * 64,
        }
        mutable_project = self.root / "mutable-project"
        mutable_project.mkdir()
        tracked_template = mutable_project / validate_closeout.SYSTEMD_TEMPLATE_PATH
        tracked_template.parent.mkdir(parents=True)
        tracked_template.write_bytes(
            (validate_closeout.PROJECT_ROOT / validate_closeout.SYSTEMD_TEMPLATE_PATH).read_bytes()
        )
        tracked_template.chmod(0o644)
        release = source_root / ("release-" + "f" * 64)
        release.mkdir()
        release.chmod(0o555)
        Path(str(python_identity["runtime"])).mkdir()
        data_dir = mutable_project / "data"
        data_dir.mkdir()
        api_config = mutable_project / "llmapi.json"
        api_config.write_text("{}\n", encoding="utf-8")
        generation = self.root / "state" / "generations" / "generation-1"
        generation.mkdir(parents=True)
        shared_state = self.root / "state" / "shared"
        shared_state.mkdir()
        process_environment = {
            "PATH": "/usr/bin:/bin",
            "WPG_HOST": "127.0.0.1",
            "WPG_PORT": "8001",
            "WPG_DATA_DIR": str(data_dir),
            "WPG_API_CONFIG": str(api_config),
            "WPG_API_CACHE_DIR": str(generation / "api_cache"),
            "WPG_RESULT_CACHE_DIR": str(
                generation / "api_cache" / "result"
            ),
            "WPG_QUERY_EMBEDDING_CACHE": str(
                generation / "query_embedding_cache.json.gz"
            ),
            "WPG_LIGHTRAG_EMBEDDING_CACHE": str(
                generation / "lightrag_embedding_cache.json.gz"
            ),
            "WPG_LIGHTRAG_WORKING_DIR": str(
                generation / "lightrag_storage"
            ),
            "WPG_GRAPH_PATH": str(data_dir / "venue_graph.json.gz"),
            "WPG_TAVILY_STATE_FILE": str(
                shared_state / ".tavily_key_pool_state.json"
            ),
            "WPG_RUNTIME_GENERATION": str(generation),
            "WPG_RUNTIME_MANIFEST": str(
                generation / "runtime-shadow-manifest.json"
            ),
            "WPG_RUNTIME_MANIFEST_SHA256": "b" * 64,
            "WPG_STRICT_GRAPH_READ_ONLY": "1",
            "WPG_REQUIRE_RUNTIME_SHADOW": "1",
            "WPG_RATE_LIMIT_REQUESTS": "6",
            "WPG_RATE_LIMIT_WINDOW_SECONDS": "60",
            "WPG_MAX_CONCURRENT_CONNECTIONS": "64",
            "WPG_MAX_CONCURRENT_SEARCHES": "2",
            "WPG_REQUEST_BODY_LIMIT": "200000",
            "WPG_REQUEST_READ_TIMEOUT": "30",
            "WPG_ALLOWED_CLIENT_CIDRS": "127.0.0.0/8,::1/128",
            "WPG_TRUST_PROXY_HEADERS": "1",
            "WPG_TRUSTED_PROXY_CIDRS": "127.0.0.0/8,::1/128",
            "WPG_REQUIRE_API_AUTH": "1",
            "WPG_API_TOKEN_FILE": str(api_token_file),
            "WPG_AUDIT_LOG": "1",
            "WPG_SOURCE_HEAD": self.head,
            "WPG_SOURCE_TREE": "9" * 40,
            "WPG_SOURCE_MANIFEST": str(
                release / "source-release-manifest.json"
            ),
            "WPG_SOURCE_MANIFEST_SHA256": "f" * 64,
            "WPG_PYTHON_RUNTIME": python_identity["runtime"],
            "WPG_PYTHON_RUNTIME_MANIFEST": python_identity["manifest"],
            "WPG_PYTHON_RUNTIME_MANIFEST_SHA256": python_identity[
                "manifest_sha256"
            ],
            "WPG_PYTHON_RUNTIME_TREE_SHA256": python_identity[
                "runtime_tree_sha256"
            ],
            "PYTHONPATH": os.pathsep.join(
                [str(release), *python_identity["import_paths"]]
            ),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "UV_OFFLINE": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
        encoded_environment = b"\0".join(
            f"{name}={value}".encode() for name, value in process_environment.items()
        )
        self.assertEqual(
            validate_closeout._parse_process_environment(encoded_environment),
            process_environment,
        )
        unauthenticated_environment = dict(process_environment)
        unauthenticated_environment["WPG_REQUIRE_API_AUTH"] = "0"
        with self.assertRaisesRegex(
            validate_closeout.CloseoutValidationError,
            "security environment differs: WPG_REQUIRE_API_AUTH",
        ):
            validate_closeout._parse_process_environment(
                b"\0".join(
                    f"{name}={value}".encode()
                    for name, value in unauthenticated_environment.items()
                )
            )
        for forbidden in (
            "PYTHONHOME",
            "PYTHONPLATLIBDIR",
            "PYTHON_UNKNOWN_INJECTION",
            "LD_PRELOAD",
            "LD_UNKNOWN_INJECTION",
            "OPENSSL_CONF",
            "HTTPS_PROXY",
            "REQUESTS_CA_BUNDLE",
        ):
            with self.assertRaisesRegex(
                validate_closeout.CloseoutValidationError,
                "forbidden systemd MainPID environment variable",
            ):
                validate_closeout._parse_process_environment(
                    encoded_environment + f"\0{forbidden}=/tmp/poison".encode()
                )
        with patch.object(
            validate_closeout.http.client, "HTTPConnection"
        ) as connection_type:
            connection = connection_type.return_value
            response = connection.getresponse.return_value
            response.status = 200
            response.read.return_value = b"{}"
            self.assertEqual(
                validate_closeout._fetch_loopback_health(
                    8765, bearer_token="t" * 48
                ),
                b"{}",
            )
        request_call = connection.request.call_args
        self.assertEqual(request_call.args[:2], ("GET", "/api/health"))
        self.assertEqual(
            request_call.kwargs["headers"]["Authorization"],
            "Bearer " + "t" * 48,
        )
        process = {
            "pid": 1234,
            "start_ticks": 9876,
            "cwd": str(release),
            "command": [
                python_identity["python_executable"],
                "-S",
                "-P",
                "-B",
                "-m",
                "where_paper_go.web_app",
            ],
            "executable": python_identity["python_executable"],
            "executable_sha256": python_identity["python_executable_sha256"],
            "no_new_privileges": True,
            "environment": dict(process_environment),
            "host": "127.0.0.1",
            "port": 8001,
        }
        rendered_unit = validate_closeout._render_expected_systemd_unit(
            tracked_template.read_bytes(),
            {
                "SOURCE_RELEASE": str(release),
                "SOURCE_HEAD": self.head,
                "SOURCE_TREE": "9" * 40,
                "SOURCE_MANIFEST": str(
                    release / "source-release-manifest.json"
                ),
                "SOURCE_MANIFEST_SHA256": "f" * 64,
                "PYTHON": str(python_identity["python_executable"]),
                "PYTHON_RUNTIME": str(python_identity["runtime"]),
                "PYTHON_RUNTIME_MANIFEST": str(python_identity["manifest"]),
                "PYTHON_RUNTIME_MANIFEST_SHA256": str(
                    python_identity["manifest_sha256"]
                ),
                "PYTHON_RUNTIME_TREE_SHA256": str(
                    python_identity["runtime_tree_sha256"]
                ),
                "PYTHON_IMPORT_PATH": os.pathsep.join(
                    str(value) for value in python_identity["import_paths"]
                ),
                "DATA_DIR": str(data_dir),
                "CONFIG_PATH": str(api_config),
                "API_TOKEN_FILE": str(api_token_file),
                "RUNTIME_DIR": str(generation),
                "SHARED_STATE_DIR": str(shared_state),
                "RUNTIME_MANIFEST_SHA256": "b" * 64,
            },
        )
        fragment.write_bytes(rendered_unit)
        fragment.chmod(0o644)
        rendered_environment = validate_closeout._rendered_unit_values(
            rendered_unit, "Environment"
        )
        rendered_unset = validate_closeout._rendered_unit_values(
            rendered_unit, "UnsetEnvironment"
        )
        rendered_exec = validate_closeout._rendered_unit_values(
            rendered_unit, "ExecStart"
        )
        self.assertEqual(len(rendered_unset), 1)
        self.assertEqual(len(rendered_exec), 1)
        systemd = "\n".join(
            (
                "ActiveState=active",
                "SubState=running",
                "UnitFileState=enabled",
                f"FragmentPath={fragment}",
                "DropInPaths=",
                "MainPID=1234",
                "NRestarts=1",
                "Result=success",
                "NeedDaemonReload=no",
                "InvocationID=" + "8" * 32,
                "ControlGroup=/user.slice/user-1000.slice/"
                "user@1000.service/app.slice/where-papers-go.service",
                "ExecMainStartTimestampMonotonic=123456",
                "NoNewPrivileges=yes",
                "PrivateTmp=yes",
                "ProtectSystem=strict",
                "ProtectHome=read-only",
                f"WorkingDirectory={release}",
                "ReadOnlyPaths="
                + " ".join(
                    validate_closeout._rendered_unit_values(
                        rendered_unit, "ReadOnlyPaths"
                    )
                ),
                "ReadWritePaths="
                + " ".join(
                    validate_closeout._rendered_unit_values(
                        rendered_unit, "ReadWritePaths"
                    )
                ),
                "Environment=" + " ".join(rendered_environment),
                "UnsetEnvironment=" + rendered_unset[0],
                "ExecStart={ path=/usr/bin/env ; argv[]="
                + rendered_exec[0]
                + " ; ignore_errors=no ; }",
            )
        )
        worker_pid = worker_health["pid"]

        def proc_stat(*, parent_pid: int, start_ticks: int) -> bytes:
            fields = ["S", str(parent_pid), *("0" for _ in range(17)), str(start_ticks)]
            return f"{worker_pid} (worker name) {' '.join(fields)}\n".encode()

        worker_stat = proc_stat(parent_pid=1234, start_ticks=8765)
        main_stat = proc_stat(parent_pid=1, start_ticks=9876)
        main_status = b"Name:\tpython\nNoNewPrivs:\t1\n"
        main_cmdline = b"\0".join(
            str(value).encode() for value in (*process["command"], "")
        )
        worker_cmdline = b"\0".join(
            str(value).encode()
            for value in (
                python_identity["python_executable"],
                "-S",
                "-P",
                "-B",
                "-m",
                "where_paper_go.worker",
                "",
            )
        )

        def read_worker_proc(path: Path, *, maximum_bytes: int) -> bytes:
            del maximum_bytes
            if path.parent.name == str(worker_pid) and path.name == "stat":
                return worker_stat
            if path.parent.name == "1234" and path.name == "stat":
                return main_stat
            if path.parent.name == "1234" and path.name == "status":
                return main_status
            if path.parent.name == str(worker_pid) and path.name == "cmdline":
                return worker_cmdline
            if path.parent.name == str(worker_pid) and path.name == "environ":
                return encoded_environment
            raise AssertionError(f"unexpected proc path: {path}")

        original_realpath = os.path.realpath

        def proc_realpath(path: object, *args: object, **kwargs: object) -> str:
            if str(path) in {f"/proc/{worker_pid}/cwd", "/proc/1234/cwd"}:
                return str(release)
            return original_realpath(path, *args, **kwargs)

        def read_main_proc(path: Path, *, maximum_bytes: int) -> bytes:
            del maximum_bytes
            if path.parent.name == "1234" and path.name == "stat":
                return main_stat
            if path.parent.name == "1234" and path.name == "status":
                return main_status
            if path.parent.name == "1234" and path.name == "cmdline":
                return main_cmdline
            if path.parent.name == "1234" and path.name == "environ":
                return encoded_environment
            raise AssertionError(f"unexpected main proc path: {path}")

        with (
            patch.object(
                validate_closeout,
                "_read_proc_bytes",
                side_effect=read_main_proc,
            ),
            patch.object(
                validate_closeout,
                "_process_executable_identity",
                return_value=(
                    python_identity["python_executable"],
                    python_identity["python_executable_sha256"],
                ),
            ) as process_executable_identity,
            patch.object(
                validate_closeout.os.path,
                "realpath",
                side_effect=proc_realpath,
            ),
        ):
            main_snapshot = validate_closeout._process_snapshot(1234)
        process_executable_identity.assert_called_once_with(1234)
        self.assertEqual(main_snapshot, process)

        def read_main_without_no_new_privs(
            path: Path, *, maximum_bytes: int
        ) -> bytes:
            if path.parent.name == "1234" and path.name == "status":
                return b"Name:\tpython\nNoNewPrivs:\t0\n"
            return read_main_proc(path, maximum_bytes=maximum_bytes)

        with (
            patch.object(
                validate_closeout,
                "_read_proc_bytes",
                side_effect=read_main_without_no_new_privs,
            ),
            patch.object(
                validate_closeout,
                "_process_executable_identity",
                return_value=(
                    python_identity["python_executable"],
                    python_identity["python_executable_sha256"],
                ),
            ),
            patch.object(
                validate_closeout.os.path,
                "realpath",
                side_effect=proc_realpath,
            ),
            self.assertRaisesRegex(
                validate_closeout.CloseoutValidationError, "NoNewPrivs"
            ),
        ):
            validate_closeout._process_snapshot(1234)

        with (
            patch.object(
                validate_closeout,
                "_read_proc_bytes",
                side_effect=read_worker_proc,
            ),
            patch.object(
                validate_closeout,
                "_process_executable_identity",
                return_value=(
                    python_identity["python_executable"],
                    python_identity["python_executable_sha256"],
                ),
            ),
            patch.object(
                validate_closeout.os.path,
                "realpath",
                side_effect=proc_realpath,
            ),
        ):
            worker_snapshot = validate_closeout._worker_process_snapshot(
                main_process=process,
                health_worker={
                    name: worker_health[name]
                    for name in validate_closeout.HEALTH_WORKER_PROCESS_KEYS
                },
                python_runtime=python_identity,
            )
        self.assertEqual(worker_snapshot["parent_pid"], 1234)
        self.assertEqual(worker_snapshot["start_ticks"], 8765)

        bad_worker_stat = proc_stat(parent_pid=9999, start_ticks=8765)

        def read_wrong_parent(path: Path, *, maximum_bytes: int) -> bytes:
            if path.parent.name == str(worker_pid) and path.name == "stat":
                return bad_worker_stat
            return read_worker_proc(path, maximum_bytes=maximum_bytes)

        with (
            patch.object(
                validate_closeout,
                "_read_proc_bytes",
                side_effect=read_wrong_parent,
            ),
            self.assertRaisesRegex(
                validate_closeout.CloseoutValidationError, "parent PID"
            ),
        ):
            validate_closeout._worker_process_snapshot(
                main_process=process,
                health_worker={
                    name: worker_health[name]
                    for name in validate_closeout.HEALTH_WORKER_PROCESS_KEYS
                },
                python_runtime=python_identity,
            )
        manifest_snapshot = validate_closeout.FileSnapshot(
            1, 2, 3, 0o400, 4, 5, "f" * 64
        )
        validator_identity = {
            name: python_identity[name]
            for name in validate_closeout.PYTHON_RUNTIME_VALIDATOR_KEYS
        }
        original_inspect_regular_file = validate_closeout._inspect_regular_file

        def inspect_deployment_file(
            path: Path, **kwargs: object
        ) -> tuple[bytes | None, validate_closeout.FileSnapshot]:
            if path in {tracked_template, fragment}:
                return original_inspect_regular_file(path, **kwargs)
            return None, manifest_snapshot

        health_bearer_tokens: list[str] = []

        def fetch_health(_port: int, *, bearer_token: str) -> bytes:
            health_bearer_tokens.append(bearer_token)
            return json.dumps(base_health).encode()

        with (
            patch.object(
                validate_closeout,
                "_current_user_home",
                return_value=test_home,
            ),
            patch.object(validate_closeout, "_systemctl_show", return_value=systemd),
            patch.object(validate_closeout, "_process_snapshot", return_value=process),
            patch.object(
                validate_closeout,
                "_ss_listeners",
                return_value=(
                    'LISTEN 0 128 127.0.0.1:8001 0.0.0.0:* '
                    'users:(("python3",pid=1234,fd=3))\n'
                ),
            ),
            patch.object(
                validate_closeout,
                "_fetch_loopback_health",
                side_effect=fetch_health,
            ),
            patch.object(
                validate_closeout,
                "_inspect_regular_file",
                side_effect=inspect_deployment_file,
            ),
            patch.object(
                validate_closeout,
                "_expected_source_manifest_sha256",
                return_value="f" * 64,
            ),
            patch.object(
                validate_closeout,
                "validate_python_runtime_release",
                return_value=validator_identity,
            ),
            patch.object(
                validate_closeout,
                "_selected_wheel_lock_sha256",
                return_value=python_identity["dependency_lock_sha256"],
            ),
            patch.object(
                validate_closeout,
                "_worker_process_snapshot",
                return_value=worker_snapshot,
            ),
            patch.object(
                validate_closeout,
                "_host_boot_state",
                return_value={
                    "boot_id": "11111111-1111-4111-8111-111111111111",
                    "machine_id_sha256": "3" * 64,
                    "uptime_seconds": 10_000.0,
                    "linger": True,
                },
            ),
        ):
            new = validate_closeout._deployment_state(
                mutable_project, self.git_state()
            )
            self.assertEqual(health_bearer_tokens[0], "t" * 48)

            weakened_unit = rendered_unit
            for weakened_line in (
                b"NoNewPrivileges=yes\n",
                b"PrivateTmp=yes\n",
                b"ProtectSystem=strict\n",
                ("ReadOnlyPaths=" + str(release) + "\n").encode(),
            ):
                weakened_unit = weakened_unit.replace(weakened_line, b"")
            fragment.write_bytes(weakened_unit)
            try:
                with self.assertRaisesRegex(
                    validate_closeout.CloseoutValidationError,
                    "fragment bytes",
                ):
                    validate_closeout._deployment_state(
                        mutable_project, self.git_state()
                    )
            finally:
                fragment.write_bytes(rendered_unit)

            wrong_fragment_systemd = systemd.replace(
                f"FragmentPath={fragment}",
                "FragmentPath=/tmp/where-papers-go.service",
            )
            with self.assertRaisesRegex(
                validate_closeout.CloseoutValidationError,
                "FragmentPath",
            ):
                validate_closeout._parse_systemd_snapshot(
                    wrong_fragment_systemd
                )
            drop_in_systemd = systemd.replace(
                "DropInPaths=",
                "DropInPaths=/tmp/weakening.conf",
            )
            with self.assertRaisesRegex(
                validate_closeout.CloseoutValidationError,
                "drop-ins",
            ):
                validate_closeout._parse_systemd_snapshot(drop_in_systemd)
            weakened_properties = systemd.replace(
                "NoNewPrivileges=yes", "NoNewPrivileges=no"
            )
            with self.assertRaisesRegex(
                validate_closeout.CloseoutValidationError,
                "hardening properties",
            ):
                validate_closeout._parse_systemd_snapshot(
                    weakened_properties
                )

            with self.assertRaisesRegex(
                validate_closeout.CloseoutValidationError,
                "source release.*fixed persistent root",
            ):
                validate_closeout._required_persistent_install_roots(
                    source_release=Path("/tmp/release-" + "f" * 64),
                    python_runtime=Path(str(python_identity["runtime"])),
                )
            with self.assertRaisesRegex(
                validate_closeout.CloseoutValidationError,
                "Python runtime.*fixed persistent root",
            ):
                validate_closeout._required_persistent_install_roots(
                    source_release=release,
                    python_runtime=Path(
                        "/tmp/python-runtime-" + "4" * 64
                    ),
                )

            with self.assertRaisesRegex(
                validate_closeout.CloseoutValidationError,
                "fixed passwd-home token path",
            ):
                validate_closeout._read_closeout_api_token(
                    "/tmp/backend.token"
                )

            source_root.chmod(0o755)
            try:
                with self.assertRaisesRegex(
                    validate_closeout.CloseoutValidationError, "mode 0700"
                ):
                    validate_closeout._required_persistent_install_roots(
                        source_release=release,
                        python_runtime=Path(str(python_identity["runtime"])),
                    )
            finally:
                source_root.chmod(0o700)

            api_token_file.parent.chmod(0o755)
            try:
                with self.assertRaisesRegex(
                    validate_closeout.CloseoutValidationError, "mode 0700"
                ):
                    validate_closeout._read_closeout_api_token(
                        str(api_token_file)
                    )
            finally:
                api_token_file.parent.chmod(0o700)

            api_token_file.chmod(0o644)
            try:
                with self.assertRaisesRegex(
                    validate_closeout.CloseoutValidationError,
                    "nlink=1 mode 0600",
                ):
                    validate_closeout._deployment_state(
                        mutable_project, self.git_state()
                    )
            finally:
                api_token_file.chmod(0o600)
            token_link = api_token_file.with_name("api-token-hardlink")
            os.link(api_token_file, token_link)
            try:
                with self.assertRaisesRegex(
                    validate_closeout.CloseoutValidationError,
                    "nlink=1 mode 0600",
                ):
                    validate_closeout._deployment_state(
                        mutable_project, self.git_state()
                    )
            finally:
                token_link.unlink()
            api_token_file.write_text(
                "t" * 48 + "\n\n", encoding="ascii"
            )
            try:
                with self.assertRaisesRegex(
                    validate_closeout.CloseoutValidationError,
                    "32..256 character safe token",
                ):
                    validate_closeout._deployment_state(
                        mutable_project, self.git_state()
                    )
            finally:
                api_token_file.write_text(
                    "t" * 48 + "\n", encoding="ascii"
                )

            base_health["checks"]["worker_process_identity"] = False
            with self.assertRaisesRegex(
                validate_closeout.CloseoutValidationError,
                "mandatory loopback health check",
            ):
                validate_closeout._deployment_state(
                    mutable_project, self.git_state()
                )
            base_health["checks"]["worker_process_identity"] = True
            base_health["runtime"]["worker_process"]["source"]["tree"] = (
                "a" * 40
            )
            with self.assertRaisesRegex(
                validate_closeout.CloseoutValidationError,
                "worker and parent source identities disagree",
            ):
                validate_closeout._deployment_state(
                    mutable_project, self.git_state()
                )
            base_health["runtime"]["worker_process"]["source"]["tree"] = (
                "9" * 40
            )
            base_health["python_runtime"]["manifest_sha256"] = "a" * 64
            base_health["runtime"]["worker_process"]["python_runtime"][
                "manifest_sha256"
            ] = "a" * 64
            with self.assertRaisesRegex(
                validate_closeout.CloseoutValidationError,
                "health, process, and independent Python runtime",
            ):
                validate_closeout._deployment_state(
                    mutable_project, self.git_state()
                )
            base_health["python_runtime"]["manifest_sha256"] = python_identity[
                "manifest_sha256"
            ]
            base_health["runtime"]["worker_process"]["python_runtime"][
                "manifest_sha256"
            ] = python_identity["manifest_sha256"]
            validator_identity["dependency_lock_sha256"] = "a" * 64
            with self.assertRaisesRegex(
                validate_closeout.CloseoutValidationError,
                "tracked selected-wheel lock",
            ):
                validate_closeout._deployment_state(
                    mutable_project, self.git_state()
                )
            validator_identity["dependency_lock_sha256"] = (
                "f5057fc74abe9390884d4fe5a3ab77d01c2aa599ac50bf36d7bacd745c4d0f8b"
            )
            process["environment"]["WPG_PYTHON_RUNTIME_TREE_SHA256"] = "a" * 64
            with self.assertRaisesRegex(
                validate_closeout.CloseoutValidationError,
                "process environment disagrees",
            ):
                validate_closeout._deployment_state(
                    mutable_project, self.git_state()
                )
            process["environment"]["WPG_PYTHON_RUNTIME_TREE_SHA256"] = (
                python_identity["runtime_tree_sha256"]
            )
            process["executable_sha256"] = "a" * 64
            with self.assertRaisesRegex(
                validate_closeout.CloseoutValidationError,
                "process executable/flags disagree",
            ):
                validate_closeout._deployment_state(
                    mutable_project, self.git_state()
                )
            process["executable_sha256"] = python_identity[
                "python_executable_sha256"
            ]
            process["environment"]["WPG_SOURCE_MANIFEST_SHA256"] = "e" * 64
            with self.assertRaisesRegex(
                validate_closeout.CloseoutValidationError,
                "current Git objects",
            ):
                validate_closeout._deployment_state(
                    mutable_project, self.git_state()
                )
        self.assertTrue(new.lightrag_store_hashes_verified)
        self.assertEqual(new.main_pid, 1234)
        self.assertEqual(new.process_start_ticks, 9876)
        self.assertEqual(new.listener_scope, "loopback_only")
        self.assertEqual(new.backend_port, 8001)
        self.assertTrue(new.host_boot["linger"])
        self.assertEqual(new.shared_quota["state_revision"], 7)
        self.assertEqual(new.shared_quota["copies"]["primary"]["mode"], "0600")
        self.assertEqual(new.source_head, self.head)
        self.assertEqual(new.lightrag_manifest_sha256, "b" * 64)
        self.assertEqual(new.python_runtime, python_identity)
        self.assertEqual(new.worker_process["pid"], worker_pid)
        self.assertEqual(new.worker_process["parent_pid"], 1234)
        fixed_host_values = {
            validate_closeout.BOOT_ID_PATH: (
                "11111111-1111-4111-8111-111111111111"
            ),
            validate_closeout.MACHINE_ID_PATH: "a" * 32,
            validate_closeout.UPTIME_PATH: "123.45 67.89",
        }
        with (
            patch.object(
                validate_closeout,
                "_read_fixed_ascii",
                side_effect=lambda path, maximum_bytes: fixed_host_values[path],
            ),
            patch.object(validate_closeout, "_linger_enabled", return_value=True),
        ):
            boot_state = validate_closeout._host_boot_state()
        self.assertEqual(boot_state["uptime_seconds"], 123.45)
        self.assertEqual(len(boot_state["machine_id_sha256"]), 64)
        self.assertNotEqual(boot_state["machine_id_sha256"], "a" * 32)
        with self.assertRaisesRegex(
            validate_closeout.CloseoutValidationError, "Linger=yes"
        ):
            validate_closeout._validate_host_boot_public(
                {**boot_state, "linger": False}, context="test host boot"
            )
        base_health["runtime"]["lightrag_store_verification"][
            "manifest_sha256"
        ] = None
        with self.assertRaisesRegex(
            validate_closeout.CloseoutValidationError,
            "six-file LightRAG",
        ):
            validate_closeout._parse_health_snapshot(
                json.dumps(base_health).encode()
            )
        with self.assertRaisesRegex(
            validate_closeout.CloseoutValidationError, "owner"
        ):
            validate_closeout._parse_listener_snapshot(
                'LISTEN 0 128 127.0.0.1:8765 0.0.0.0:* '
                'users:(("python3",pid=9999,fd=3))\n',
                expected_host="127.0.0.1",
                expected_port=8765,
                expected_pid=1234,
            )
        with self.assertRaisesRegex(
            validate_closeout.CloseoutValidationError, "wildcard listener"
        ):
            validate_closeout._parse_listener_snapshot(
                'LISTEN 0 128 0.0.0.0:8765 0.0.0.0:* '
                'users:(("python3",pid=1234,fd=3))\n',
                expected_host="0.0.0.0",
                expected_port=8765,
                expected_pid=1234,
            )

    def test_git_environment_ignores_repository_override_variables(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GIT_DIR": "/tmp/attacker",
                "GIT_WORK_TREE": "/tmp/other",
                "GIT_CONFIG_GLOBAL": "/tmp/config",
                "PATH": "/tmp/bin",
            },
        ):
            environment = validate_closeout._git_environment()
        self.assertNotIn("GIT_DIR", environment)
        self.assertNotIn("GIT_WORK_TREE", environment)
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertEqual(environment["PATH"], "/usr/bin:/bin")


if __name__ == "__main__":
    unittest.main()
