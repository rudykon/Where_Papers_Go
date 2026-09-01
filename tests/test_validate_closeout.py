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
    ) -> validate_closeout.DeploymentEvidence:
        return validate_closeout.DeploymentEvidence(
            active=True,
            enabled=True,
            ready=True,
            bindings_current=True,
            lightrag_store_hashes_verified=proof,
            listener_scope="all_interfaces_ipv4",
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
        )

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
                return_value={
                    name: (
                        "2" * 64 if name == "offline_guard_sha256" else "1" * 64
                    )
                    for name in validate_closeout.TRACKED_IMPLEMENTATION_FILES
                },
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
                "already has a successful v3 closeout",
            ):
                validate_closeout.create_closeout(
                    input_path=self.input_path,
                    project_root=self.root,
                    output_root=self.output_root,
                )

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
        self.assertEqual(payload["deployment"]["main_pid"], 4321)
        self.assertTrue(payload["publication"]["same_head_replay_supported"])
        self.assertEqual(
            hashlib.sha256((target / "summary.json").read_bytes()).hexdigest(),
            digest,
        )

    def test_deployment_probe_requires_true_lightrag_and_one_process_identity(self) -> None:
        systemd = "\n".join(
            (
                "ActiveState=active",
                "SubState=running",
                "UnitFileState=enabled",
                "MainPID=1234",
                "NRestarts=1",
                "Result=success",
                "NeedDaemonReload=no",
                "InvocationID=" + "8" * 32,
                "ControlGroup=/user.slice/user-1000.slice/user@1000.service/app.slice/where-papers-go.service",
                "ExecMainStartTimestampMonotonic=123456",
            )
        )
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
                    "bindings_current",
                    "runtime_contract",
                    "source_identity",
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
        release = self.root / "source-releases" / ("release-" + "f" * 64)
        release.mkdir(parents=True)
        release.chmod(0o555)
        process = {
            "pid": 1234,
            "start_ticks": 9876,
            "cwd": str(release),
            "command": ["/usr/bin/python3", "-m", "where_paper_go.web_app"],
            "environment": {
                "WPG_HOST": "0.0.0.0",
                "WPG_PORT": "8765",
                "WPG_SOURCE_HEAD": self.head,
                "WPG_SOURCE_TREE": "9" * 40,
                "WPG_SOURCE_MANIFEST": str(
                    release / "source-release-manifest.json"
                ),
                "WPG_SOURCE_MANIFEST_SHA256": "f" * 64,
            },
            "host": "0.0.0.0",
            "port": 8765,
        }
        manifest_snapshot = validate_closeout.FileSnapshot(
            1, 2, 3, 0o400, 4, 5, "f" * 64
        )
        with (
            patch.object(validate_closeout, "_systemctl_show", return_value=systemd),
            patch.object(validate_closeout, "_process_snapshot", return_value=process),
            patch.object(
                validate_closeout,
                "_ss_listeners",
                return_value=(
                    'LISTEN 0 128 0.0.0.0:8765 0.0.0.0:* '
                    'users:(("python3",pid=1234,fd=3))\n'
                ),
            ),
            patch.object(
                validate_closeout,
                "_fetch_loopback_health",
                return_value=json.dumps(base_health).encode(),
            ),
            patch.object(
                validate_closeout,
                "_inspect_regular_file",
                return_value=(None, manifest_snapshot),
            ),
            patch.object(
                validate_closeout,
                "_expected_source_manifest_sha256",
                return_value="f" * 64,
            ),
        ):
            new = validate_closeout._deployment_state(
                mutable_project, self.git_state()
            )
            process["environment"]["WPG_SOURCE_MANIFEST_SHA256"] = "e" * 64
            base_health["source"]["manifest_sha256"] = "e" * 64
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
        self.assertEqual(new.listener_scope, "all_interfaces_ipv4")
        self.assertEqual(new.source_head, self.head)
        self.assertEqual(new.lightrag_manifest_sha256, "b" * 64)
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
                'LISTEN 0 128 0.0.0.0:8765 0.0.0.0:* '
                'users:(("python3",pid=9999,fd=3))\n',
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
