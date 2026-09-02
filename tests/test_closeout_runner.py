from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import run_closeout_tests, validate_pr_gates


class _HealthyGuard:
    @staticmethod
    def guard_self_check() -> bool:
        return True


class CloseoutRunnerContractTests(unittest.TestCase):
    def test_synthetic_suite_emits_only_the_exact_aggregate_schema(self) -> None:
        class SyntheticCase(unittest.TestCase):
            def __init__(
                self,
                identifier: str,
                reason: str | None = None,
                *,
                subtest: bool = False,
            ) -> None:
                super().__init__("runTest")
                self.identifier = identifier
                self.reason = reason
                self.use_subtest = subtest

            def id(self) -> str:
                return self.identifier

            def runTest(self) -> None:
                if self.reason is None:
                    return
                if self.use_subtest:
                    with self.subTest(value="synthetic"):
                        self.skipTest(self.reason)
                    return
                self.skipTest(self.reason)

        def run_synthetic(
            suite_name: str,
            rows: tuple[tuple[str, str | None, bool], ...],
        ) -> dict[str, object]:
            suite = unittest.TestSuite(
                SyntheticCase(identifier, reason, subtest=subtest)
                for identifier, reason, subtest in rows
            )
            with patch.object(
                run_closeout_tests, "_load_suite", return_value=suite
            ):
                return run_closeout_tests._run_suite(
                    suite_name, _HealthyGuard()
                )

        allowed_parent_id = (
            run_closeout_tests.LOCAL_RUNTIME_SCIENTIFIC_TEST_ID
        )
        allowed_prefix_reason = (
            run_closeout_tests.LOCAL_RUNTIME_SKIP_REASON_PREFIX
            + "ModuleNotFoundError: No module named 'torch'"
        )
        report = run_synthetic(
            "full",
            (
                ("synthetic.first", None, False),
                ("synthetic.second", None, False),
                (allowed_parent_id, allowed_prefix_reason, False),
            ),
        )

        self.assertEqual(set(report), run_closeout_tests.REPORT_KEYS)
        self.assertEqual(
            report["schema_version"],
            run_closeout_tests.REPORT_SCHEMA_VERSION,
        )
        self.assertEqual(
            report["artifact_type"],
            "where_papers_go_closeout_test_report",
        )
        self.assertTrue(report["guard_active"])
        self.assertEqual(report["total"], 3)
        self.assertEqual(report["passed"], 2)
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(report["test_id_count"], 3)
        self.assertEqual(report["skipped_test_id_count"], 1)
        self.assertEqual(
            report["skipped_test_id_sha256"],
            run_closeout_tests._skipped_test_id_digest((allowed_parent_id,)),
        )
        self.assertNotEqual(
            report["skipped_test_id_sha256"],
            run_closeout_tests._test_id_digest((allowed_parent_id,)),
        )
        full_allowlist_digest = run_closeout_tests._skip_allowlist_digest(
            run_closeout_tests.FULL_SKIP_ALLOWLIST
        )
        self.assertEqual(
            full_allowlist_digest,
            "d970d6124c58fd064d3241a151dfc2001b2c841c003c2c2bba3dfecaf71a246b",
        )
        self.assertEqual(
            report["skip_allowlist_sha256"], full_allowlist_digest
        )
        self.assertEqual(
            len(
                {
                    run_closeout_tests.ID_HASH_DOMAIN,
                    run_closeout_tests.SKIPPED_TEST_ID_HASH_DOMAIN,
                    run_closeout_tests.SKIP_ALLOWLIST_HASH_DOMAIN,
                }
            ),
            3,
        )
        for field in (
            "failures",
            "errors",
            "expected_failures",
            "unexpected_successes",
        ):
            self.assertEqual(report[field], 0)
        self.assertRegex(report["test_id_sha256"], r"\A[0-9a-f]{64}\Z")
        encoded = run_closeout_tests._encode_report(report)
        self.assertEqual(json.loads(encoded), report)
        self.assertTrue(encoded.endswith(b"\n"))
        self.assertNotIn(allowed_parent_id.encode("ascii"), encoded)
        self.assertNotIn(allowed_prefix_reason.encode("ascii"), encoded)

        expected_allowlist = {
            run_closeout_tests.LOCAL_RUNTIME_SCIENTIFIC_TEST_ID: (
                (
                    run_closeout_tests.SKIP_REASON_PREFIX,
                    run_closeout_tests.LOCAL_RUNTIME_SKIP_REASON_PREFIX,
                ),
            ),
            run_closeout_tests.LOCAL_RUNTIME_CROSS_ENCODER_TEST_ID: (
                (
                    run_closeout_tests.SKIP_REASON_PREFIX,
                    run_closeout_tests.LOCAL_RUNTIME_SKIP_REASON_PREFIX,
                ),
            ),
            run_closeout_tests.NGINX_INTEGRATION_TEST_ID: (
                (
                    run_closeout_tests.SKIP_REASON_EXACT,
                    run_closeout_tests.NGINX_UNAVAILABLE_SKIP_REASON,
                ),
            ),
            run_closeout_tests.SYSTEMD_HOST_INTEGRATION_TEST_ID: (
                (
                    run_closeout_tests.SKIP_REASON_EXACT,
                    run_closeout_tests.SYSTEMD_HOST_OPT_IN_SKIP_REASON,
                ),
            ),
        }
        self.assertEqual(
            run_closeout_tests.SUITE_SKIP_ALLOWLISTS,
            {"full": expected_allowlist, "model-focused": {}},
        )

        allowed_report = run_synthetic(
            "full",
            (
                (
                    run_closeout_tests.LOCAL_RUNTIME_SCIENTIFIC_TEST_ID,
                    allowed_prefix_reason,
                    False,
                ),
                (
                    run_closeout_tests.LOCAL_RUNTIME_CROSS_ENCODER_TEST_ID,
                    run_closeout_tests.LOCAL_RUNTIME_SKIP_REASON_PREFIX
                    + "ImportError: synthetic optional runtime",
                    False,
                ),
                (
                    run_closeout_tests.NGINX_INTEGRATION_TEST_ID,
                    run_closeout_tests.NGINX_UNAVAILABLE_SKIP_REASON,
                    False,
                ),
                (
                    run_closeout_tests.SYSTEMD_HOST_INTEGRATION_TEST_ID,
                    run_closeout_tests.SYSTEMD_HOST_OPT_IN_SKIP_REASON,
                    False,
                ),
            ),
        )
        self.assertEqual(allowed_report["total"], 4)
        self.assertEqual(allowed_report["skipped"], 4)
        self.assertEqual(allowed_report["errors"], 0)
        self.assertEqual(allowed_report["skipped_test_id_count"], 4)
        self.assertEqual(
            allowed_report["skipped_test_id_sha256"],
            run_closeout_tests._skipped_test_id_digest(
                expected_allowlist
            ),
        )

        model_pass_report = run_synthetic(
            "model-focused", (("synthetic.model.pass", None, False),)
        )
        self.assertEqual(model_pass_report["passed"], 1)
        self.assertEqual(model_pass_report["skipped_test_id_count"], 0)
        self.assertEqual(
            model_pass_report["skipped_test_id_sha256"],
            run_closeout_tests.EMPTY_SKIPPED_TEST_ID_SHA256,
        )
        self.assertEqual(
            run_closeout_tests.EMPTY_SKIPPED_TEST_ID_SHA256,
            "a9f31e92ced15b4367d3e78d93aa1e820909a0fd49fb3495d0c36cce9817c3dc",
        )
        self.assertEqual(
            model_pass_report["skip_allowlist_sha256"],
            run_closeout_tests.EMPTY_SKIP_ALLOWLIST_SHA256,
        )
        self.assertEqual(
            run_closeout_tests.EMPTY_SKIP_ALLOWLIST_SHA256,
            "ecbbeafb099c4e91937fc5570d6dbf6ffdde3700e59245704340e92d8d558fed",
        )
        self.assertNotEqual(
            model_pass_report["skip_allowlist_sha256"],
            full_allowlist_digest,
        )

        fixed_full_report = dict(report)
        fixed_full_report.update(
            {
                "total": 489,
                "passed": 485,
                "skipped": 4,
                "failures": 0,
                "errors": 0,
                "expected_failures": 0,
                "unexpected_successes": 0,
                "test_id_count": 489,
                "test_id_sha256": validate_pr_gates.FULL_TEST_ID_SHA256,
                "skipped_test_id_count": 4,
                "skipped_test_id_sha256": (
                    validate_pr_gates.FULL_SKIPPED_TEST_ID_SHA256
                ),
                "skip_allowlist_sha256": (
                    validate_pr_gates.FULL_SKIP_ALLOWLIST_SHA256
                ),
            }
        )
        validated_full = validate_pr_gates._validate_test_report(
            fixed_full_report, suite="full"
        )
        self.assertEqual(
            (validated_full["total"], validated_full["passed"], validated_full["skipped"]),
            (489, 485, 4),
        )
        fixed_model_report = dict(model_pass_report)
        fixed_model_report.update(
            {
                "total": 6,
                "passed": 6,
                "skipped": 0,
                "failures": 0,
                "errors": 0,
                "expected_failures": 0,
                "unexpected_successes": 0,
                "test_id_count": 6,
                "test_id_sha256": validate_pr_gates.MODEL_TEST_ID_SHA256,
                "skipped_test_id_count": 0,
                "skipped_test_id_sha256": (
                    validate_pr_gates.MODEL_SKIPPED_TEST_ID_SHA256
                ),
                "skip_allowlist_sha256": (
                    validate_pr_gates.MODEL_SKIP_ALLOWLIST_SHA256
                ),
            }
        )
        validated_model = validate_pr_gates._validate_test_report(
            fixed_model_report, suite="model-focused"
        )
        self.assertEqual(
            (validated_model["total"], validated_model["passed"], validated_model["skipped"]),
            (6, 6, 0),
        )
        drifted_full_report = dict(fixed_full_report)
        drifted_full_report["passed"] = 484
        drifted_full_report["skipped"] = 5
        drifted_full_report["skipped_test_id_count"] = 5
        with self.assertRaisesRegex(
            validate_pr_gates.PrGateError, "fixed full report drifted"
        ):
            validate_pr_gates._validate_test_report(
                drifted_full_report, suite="full"
            )

        invalid_skips = (
            (
                "full",
                "synthetic.unknown_skip",
                "synthetic unknown reason",
                False,
            ),
            (
                "full",
                run_closeout_tests.LOCAL_RUNTIME_SCIENTIFIC_TEST_ID,
                "unexpected local runtime reason",
                False,
            ),
            (
                "full",
                run_closeout_tests.LOCAL_RUNTIME_SCIENTIFIC_TEST_ID,
                run_closeout_tests.LOCAL_RUNTIME_SKIP_REASON_PREFIX,
                False,
            ),
            (
                "full",
                run_closeout_tests.NGINX_INTEGRATION_TEST_ID,
                "openssl is required to create an isolated test certificate",
                False,
            ),
            (
                "full",
                run_closeout_tests.SYSTEMD_HOST_INTEGRATION_TEST_ID,
                run_closeout_tests.SYSTEMD_HOST_OPT_IN_SKIP_REASON + "!",
                True,
            ),
            (
                "full",
                run_closeout_tests.LOCAL_RUNTIME_SCIENTIFIC_TEST_ID,
                allowed_prefix_reason,
                True,
            ),
            (
                "model-focused",
                "tests."
                + run_closeout_tests.LOCAL_RUNTIME_SCIENTIFIC_TEST_ID,
                allowed_prefix_reason,
                False,
            ),
        )
        for suite_name, identifier, reason, subtest in invalid_skips:
            with self.subTest(
                suite_name=suite_name,
                identifier=identifier,
                subtest=subtest,
            ):
                invalid_report = run_synthetic(
                    suite_name, ((identifier, reason, subtest),)
                )
                self.assertEqual(invalid_report["total"], 1)
                self.assertEqual(invalid_report["passed"], 0)
                self.assertEqual(invalid_report["skipped"], 0)
                self.assertEqual(invalid_report["errors"], 1)
                self.assertEqual(
                    invalid_report["skipped_test_id_count"], 1
                )
                self.assertEqual(
                    invalid_report["skipped_test_id_sha256"],
                    run_closeout_tests._skipped_test_id_digest(
                        (identifier,)
                    ),
                )
                invalid_encoded = run_closeout_tests._encode_report(
                    invalid_report
                )
                self.assertNotIn(identifier.encode("ascii"), invalid_encoded)
                self.assertNotIn(reason.encode("ascii"), invalid_encoded)

        class FixtureSkippedCase(unittest.TestCase):
            @classmethod
            def setUpClass(cls) -> None:
                raise unittest.SkipTest("synthetic fixture skip")

            def test_never_runs(self) -> None:
                self.fail("fixture-skipped test unexpectedly ran")

        fixture_suite = unittest.TestSuite(
            (
                unittest.defaultTestLoader.loadTestsFromTestCase(
                    FixtureSkippedCase
                ),
                SyntheticCase("synthetic.fixture.peer"),
            )
        )
        with patch.object(
            run_closeout_tests,
            "_load_suite",
            return_value=fixture_suite,
        ):
            fixture_report = run_closeout_tests._run_suite(
                "full", _HealthyGuard()
            )
        self.assertEqual(fixture_report["total"], 2)
        self.assertEqual(fixture_report["passed"], 1)
        self.assertEqual(fixture_report["skipped"], 0)
        self.assertEqual(fixture_report["errors"], 1)
        self.assertEqual(fixture_report["skipped_test_id_count"], 0)

    def test_output_creation_is_private_and_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "report.json"
            descriptor = run_closeout_tests._open_report(output)
            try:
                metadata = os.fstat(descriptor)
                self.assertTrue(stat.S_ISREG(metadata.st_mode))
                self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
            finally:
                os.close(descriptor)

            with self.assertRaises(FileExistsError):
                run_closeout_tests._open_report(output)

    def test_runner_environment_drops_host_opt_ins_secrets_and_proxies(self) -> None:
        with patch.dict(
            os.environ,
            {
                "WPG_RUN_HOST_SYSTEMD_TESTS": "1",
                "WPG_SYSTEMD_TEST_UNIT": "production.service",
                "WPG_EXPECTED_HOST": "production.example",
                "WPG_NGINX_BIN": "/untrusted/nginx",
                "OPENAI_API_KEY": "secret",
                "HTTPS_PROXY": "http://proxy.example",
                "SSLKEYLOGFILE": "/tmp/keys",
                "PYTHONSTARTUP": "/tmp/inject.py",
            },
            clear=False,
        ):
            environment = run_closeout_tests._sanitized_environment(
                "/tmp/closeout-audit-test.log"
            )

        for name in (
            "WPG_RUN_HOST_SYSTEMD_TESTS",
            "WPG_SYSTEMD_TEST_UNIT",
            "WPG_EXPECTED_HOST",
            "WPG_NGINX_BIN",
            "OPENAI_API_KEY",
            "HTTPS_PROXY",
            "SSLKEYLOGFILE",
            "PYTHONSTARTUP",
        ):
            self.assertNotIn(name, environment)
        self.assertEqual(environment["HF_HUB_OFFLINE"], "1")
        self.assertEqual(environment["TRANSFORMERS_OFFLINE"], "1")
        self.assertEqual(environment["HF_DATASETS_OFFLINE"], "1")
        self.assertEqual(
            environment["PYTHONPATH"].split(os.pathsep),
            [
                os.fspath(path)
                for path in run_closeout_tests.TRUSTED_PYTHONPATH_ENTRIES
            ],
        )
        self.assertEqual(
            run_closeout_tests._parse_arguments(
                ["--suite", "full", "--output", "/tmp/report.json"]
            ),
            ("full", Path("/tmp/report.json")),
        )
        self.assertIsNone(
            run_closeout_tests._parse_arguments(
                ["--suite", "tests.test_closeout_runner"]
            )
        )
        self.assertIsNone(
            run_closeout_tests._parse_arguments(
                ["--suite", "full", "--output", "first", "--output", "second"]
            )
        )
        with patch.object(
            validate_pr_gates.importlib.util, "find_spec", return_value=None
        ), patch.dict(os.environ, {"WPG_NGINX_BIN": ""}, clear=False):
            with self.assertRaisesRegex(
                validate_pr_gates.PrGateError, "WPG_NGINX_BIN to be unset"
            ):
                validate_pr_gates._runtime_preflight("full")

        codeowners = (
            validate_pr_gates.PROJECT_ROOT / ".github" / "CODEOWNERS"
        ).read_text(encoding="utf-8")
        for protected_path in (
            "/.github/ @rudykon",
            "/scripts/validate_pr_gates.py @rudykon",
            "/scripts/run_closeout_tests.py @rudykon",
            "/scripts/run_linux_offline_gate.sh @rudykon",
            "/scripts/validate_closeout.py @rudykon",
            "/deploy/ @rudykon",
            "/uv.lock @rudykon",
        ):
            self.assertIn(protected_path, codeowners)

        workflow = (
            validate_pr_gates.PROJECT_ROOT
            / ".github"
            / "workflows"
            / "tests.yml"
        ).read_text(encoding="utf-8")
        for job in (
            "  fixed-static:",
            "  fixed-full:",
            "  fixed-retrieval:",
            "  fixed-model:",
            "  fixed-pr-gates:",
        ):
            self.assertIn(job, workflow)
        self.assertIn(
            "needs: [fixed-static, fixed-full, fixed-retrieval, fixed-model]",
            workflow,
        )
        self.assertEqual(workflow.count("enable-cache: false"), 5)
        self.assertNotIn("enable-cache: true", workflow)
        self.assertEqual(workflow.count("-I -S"), 10)
        self.assertNotIn("-m scripts.validate_pr_gates", workflow)
        self.assertNotIn("pip install -e", workflow)
        self.assertIn("terminal offline step", workflow)
        self.assertEqual(workflow.count('= "Python 3.12.3"'), 4)
        self.assertEqual(workflow.count("Reverify fixed inputs"), 3)
        self.assertEqual(workflow.count("--require-os-isolation"), 3)
        self.assertIn("--no-install-project --no-build", workflow)
        self.assertIn("--preview-features pylock --require-hashes --strict", workflow)
        self.assertIn("--no-deps --only-binary :all: --no-index", workflow)
        self.assertIn(".github/pylock.wpg-wheel-build.toml", workflow)
        self.assertIn("git -c core.hooksPath=/dev/null archive", workflow)
        self.assertIn("permissions: {}", workflow)
        self.assertIn("  push:\n    branches: [main]\n", workflow)

        model_lock = validate_pr_gates.MODEL_LOCK_PATH.read_bytes()
        self.assertEqual(
            validate_pr_gates._sha256(model_lock),
            validate_pr_gates.MODEL_LOCK_SHA256,
        )
        self.assertEqual(len(validate_pr_gates.MODEL_LOCK_VERSIONS), 25)
        wheel_build_lock = validate_pr_gates.WHEEL_BUILD_LOCK_PATH.read_bytes()
        self.assertEqual(
            validate_pr_gates._sha256(wheel_build_lock),
            validate_pr_gates.WHEEL_BUILD_LOCK_SHA256,
        )
        if sys.version_info >= (3, 11):
            self.assertEqual(
                validate_pr_gates._wheel_build_lock_versions(),
                validate_pr_gates.WHEEL_BUILD_LOCK_VERSIONS,
            )
        self.assertIn(
            "scripts/__init__.py",
            validate_pr_gates.REQUIRED_CRITICAL_PATHS,
        )
        self.assertEqual(
            validate_pr_gates._sha256(
                validate_pr_gates.RUN_CLOSEOUT_TESTS_PATH.read_bytes()
            ),
            validate_pr_gates.RUN_CLOSEOUT_TESTS_SHA256,
        )

        manifest = validate_pr_gates._load_manifest()
        validate_pr_gates._validate_manifest(manifest)
        self.assertEqual(validate_pr_gates._manifest_template(), manifest)
        self.assertEqual(
            manifest["logo_protection"],
            {
                "path": "docs/Where-Papers-Go.png",
                "git_blob_sha1": "42b021f7088e08c165fa615a8d3b7bd60af25fd1",
                "sha256": "80266c537c4a8251766e1d8e53c5a1e9def90e34080b76d8a3e00be770ba3b11",
            },
        )
        self.assertEqual(manifest["retrieval"]["case_count"], 7)
        canonical_cases, case_digest = validate_pr_gates._retrieval_definition()
        self.assertEqual(
            case_digest,
            validate_pr_gates.RETRIEVAL_CASE_DEFINITION_SHA256,
        )
        self.assertEqual(
            list(validate_pr_gates._credential_findings()),
            manifest["credential_scan"]["allowed_findings"],
        )
        self.assertEqual(
            list(validate_pr_gates._credential_findings(source="worktree")),
            manifest["credential_scan"]["allowed_findings"],
        )
        offline_wrapper = (
            validate_pr_gates.PROJECT_ROOT
            / "scripts"
            / "run_linux_offline_gate.sh"
        ).read_text(encoding="utf-8")
        for required_fragment in (
            "/usr/bin/unshare",
            "--propagation private",
            "/bin/bash --noprofile --norc -p",
            "--mount-proc",
            "--kill-child=KILL",
            "--clear-groups",
            "--no-new-privs",
            "--inh-caps=-all",
            "--ambient-caps=-all",
            "--bounding-set=-all",
            "wpg-run /run",
            "wpg-tmp /tmp",
            "cd /",
            "readonly_bind_exec \"$caller_home\"",
            "readonly_bind \"$project_root\"",
            "cd -- \"$project_root\"",
            "command is below the noexec checkout",
            "[[ ! -w scripts/validate_pr_gates.py ]]",
            "WPG_PR_RUNNER_TOOL_CACHE",
            'wpg-root "$command_path" "$@"',
            'wpg-unprivileged "$@"',
            "GITHUB_ENV+x",
            "OS-level offline gate retained propagating mounts",
            "OS-level offline gate privileged setup shell lost effective root",
        ):
            self.assertIn(required_fragment, offline_wrapper)
        self.assertNotIn("mount --make-rprivate /", offline_wrapper)
        for local_name, environment_name in (
            ("caller_uid", "WPG_PR_CALLER_UID"),
            ("caller_gid", "WPG_PR_CALLER_GID"),
            ("project_root", "WPG_PR_SANDBOX_ROOT"),
            ("runner_commands_dir", "WPG_PR_RUNNER_COMMANDS_DIR"),
            ("caller_home", "WPG_PR_CALLER_HOME"),
            ("runner_tool_cache", "WPG_PR_RUNNER_TOOL_CACHE"),
        ):
            self.assertEqual(
                offline_wrapper.count(
                    f'{local_name}="${{{environment_name}:?}}"'
                ),
                2,
            )
        self.assertNotIn('runner_commands_dir="$4"', offline_wrapper)
        self.assertNotIn("shift 6", offline_wrapper)
        self.assertNotIn("--init-groups", offline_wrapper)
        self.assertNotIn("iptables", offline_wrapper)

        def logo_git(arguments, *, binary=False):
            if arguments[:2] in {
                ("cat-file", "-e"),
                ("merge-base", "--is-ancestor"),
            }:
                return b"" if binary else ""
            if arguments[:2] == ("ls-tree", "-z"):
                return b""
            if arguments[:2] == ("rev-parse", "--verify"):
                return validate_pr_gates.LOGO_GIT_BLOB_SHA1
            if arguments[0] == "diff":
                return ""
            self.fail(f"unexpected git invocation: {arguments!r}")

        with patch.object(validate_pr_gates, "_git", side_effect=logo_git):
            validate_pr_gates._verify_logo_and_diff("base")

        wrong_logo = (
            b"100644 blob "
            + (b"0" * 40)
            + b"\tdocs/Where-Papers-Go.png\0"
        )
        with patch.object(
            validate_pr_gates,
            "_git",
            side_effect=lambda arguments, binary=False: (
                wrong_logo
                if arguments[:2] == ("ls-tree", "-z")
                else logo_git(arguments, binary=binary)
            ),
        ):
            with self.assertRaisesRegex(
                validate_pr_gates.PrGateError,
                "base may omit but may not replace",
            ):
                validate_pr_gates._verify_logo_and_diff("base")

        locked_status = """\
Pid:\t1
Uid:\t1001\t1001\t1001\t1001
Gid:\t1002\t1002\t1002\t1002
Groups:\t
CapInh:\t0000000000000000
CapPrm:\t0000000000000000
CapEff:\t0000000000000000
CapBnd:\t0000000000000000
CapAmb:\t0000000000000000
NoNewPrivs:\t1
"""
        self.assertTrue(
            validate_pr_gates._sandbox_status_is_unprivileged(
                locked_status, 1001, 1002
            )
        )
        self.assertFalse(
            validate_pr_gates._sandbox_status_is_unprivileged(
                locked_status.replace("NoNewPrivs:\t1", "NoNewPrivs:\t0"),
                1001,
                1002,
            )
        )
        project_root = os.fspath(validate_pr_gates.PROJECT_ROOT)
        caller_home = os.fspath(validate_pr_gates.PROJECT_ROOT.parents[1])
        runner_tool_cache = "/opt/hostedtoolcache"
        mountinfo = "\n".join(
            (
                "1 0 0:1 / /proc rw,nosuid,nodev,noexec - proc proc rw",
                "2 0 0:2 / /run rw,nosuid,nodev,noexec - tmpfs wpg-run rw",
                "3 0 0:3 / /tmp rw,nosuid,nodev - tmpfs wpg-tmp rw",
                "4 0 0:4 / /dev/shm rw,nosuid,nodev,noexec - tmpfs wpg-shm rw",
                f"5 0 0:5 / {project_root} ro,nosuid,nodev,noexec - ext4 /dev/root ro",
                f"6 0 0:6 / {caller_home} ro,nosuid,nodev - ext4 /dev/root ro",
                f"7 0 0:7 / {runner_tool_cache} ro,nosuid,nodev - ext4 /dev/root ro",
            )
        )
        self.assertTrue(
            validate_pr_gates._sandbox_mounts_are_private(
                mountinfo,
                project_root=project_root,
                caller_home=caller_home,
                runner_commands_dir="/nonexistent",
                runner_tool_cache=runner_tool_cache,
            )
        )
        self.assertFalse(
            validate_pr_gates._sandbox_mounts_are_private(
                mountinfo.replace("/tmp rw,nosuid,nodev", "/tmp rw,nosuid"),
                project_root=project_root,
                caller_home=caller_home,
                runner_commands_dir="/nonexistent",
                runner_tool_cache=runner_tool_cache,
            )
        )
        self.assertFalse(
            validate_pr_gates._sandbox_mounts_are_private(
                mountinfo.replace(
                    "/ /proc rw,nosuid,nodev,noexec - proc",
                    "/ /proc rw,nosuid,nodev,noexec shared:42 - proc",
                ),
                project_root=project_root,
                caller_home=caller_home,
                runner_commands_dir="/nonexistent",
                runner_tool_cache=runner_tool_cache,
            )
        )

        with patch.dict(
            os.environ,
            {
                validate_pr_gates.OS_OFFLINE_ENV: (
                    validate_pr_gates.OS_OFFLINE_TOKEN
                )
            },
        ), patch.object(
            validate_pr_gates, "_sandbox_attestation", return_value=True
        ):
            self.assertTrue(
                validate_pr_gates._os_network_isolation_active(required=True)
            )
        with patch.dict(
            os.environ,
            {
                validate_pr_gates.OS_OFFLINE_ENV: (
                    validate_pr_gates.OS_OFFLINE_TOKEN
                )
            },
        ), patch.object(
            validate_pr_gates, "_sandbox_attestation", return_value=False
        ):
            with self.assertRaisesRegex(
                validate_pr_gates.PrGateError, "sandbox isolation is inactive"
            ):
                validate_pr_gates._os_network_isolation_active(required=True)
        with patch.object(validate_pr_gates, "_verify_critical_files"), patch.object(
            validate_pr_gates, "_verify_logo_and_diff"
        ), patch.object(
            validate_pr_gates,
            "_model_lock_versions",
            return_value=dict(validate_pr_gates.MODEL_LOCK_VERSIONS),
        ), patch.object(
            validate_pr_gates,
            "_wheel_build_lock_versions",
            return_value=dict(validate_pr_gates.WHEEL_BUILD_LOCK_VERSIONS),
        ):
            static_result = validate_pr_gates._run_static_gate(
                "HEAD", validate_pr_gates.MANIFEST_PATH
            )
        self.assertEqual(static_result["status"], "passed")
        self.assertEqual(static_result["credential_findings"], 0)

        with tempfile.TemporaryDirectory() as temporary:
            graph = Path(temporary) / "venue-graph.json.gz"
            benchmark_cases = [
                {
                    "name": case["name"],
                    "query": case["query"],
                    "top_k": case["top_k"],
                    "expected": list(case["expected"]),
                    "result": list(case["expected"]),
                    "matched": list(case["expected"]),
                    "recall_at_k": 1.0,
                    "query_ms": 0.0,
                }
                for case in canonical_cases
            ]
            benchmark_result = {
                "graph": os.fspath(graph.resolve()),
                "graph_load_ms": 0.0,
                "peak_rss_kb": 1,
                "case_count": 7,
                "micro_recall_at_k": 1.0,
                "all_cases_full_recall": True,
                "query_ms": {"mean": 0.0, "median": 0.0, "max": 0.0},
                "cases": benchmark_cases,
            }
            validate_pr_gates._validate_retrieval_benchmark(
                benchmark_result, graph=graph
            )
            forged = dict(benchmark_result)
            forged["cases"] = [dict(row) for row in benchmark_cases]
            forged["cases"][0]["expected"] = []
            forged["cases"][0]["matched"] = []
            with self.assertRaisesRegex(
                validate_pr_gates.PrGateError, "canonical full recall"
            ):
                validate_pr_gates._validate_retrieval_benchmark(
                    forged, graph=graph
                )

            target = Path(temporary) / "target"
            target.write_bytes(b"synthetic")
            link = Path(temporary) / "tracked-link"
            link.symlink_to(target)
            with patch.object(
                validate_pr_gates, "PROJECT_ROOT", Path(temporary)
            ):
                with self.assertRaisesRegex(
                    validate_pr_gates.PrGateError, "not a regular file"
                ):
                    validate_pr_gates._tracked_bytes(
                        "tracked-link", "0" * 40, source="worktree"
                    )

    def test_guard_survives_environment_clear_and_audits_lowlevel_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "network-audit.log"
            descriptor = os.open(
                audit,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            os.close(descriptor)
            program = r'''
import os
import socket
import sitecustomize
import sys
import types
from unittest.mock import patch

if not sitecustomize.guard_self_check():
    raise SystemExit(10)
if not socket.getaddrinfo("localhost", 80):
    raise SystemExit(12)
class DisconnectedVerifiedLoopback:
    family = socket.AF_INET
    _closeout_peer_allowed = True
    def getpeername(self):
        raise OSError("peer already closed")
if not sitecustomize._socket_peer_is_allowed(DisconnectedVerifiedLoopback()):
    raise SystemExit(13)
blocked = 0
with patch.dict(os.environ, {}, clear=True):
    try:
        sys.audit("socket.getaddrinfo", "example.invalid", 443, 0, 0, 0)
    except sitecustomize.CloseoutOfflineNetworkError:
        blocked += 1
try:
    sys.audit(
        "socket.bind",
        types.SimpleNamespace(family=socket.AF_INET),
        ("198.51.100.10", 0),
    )
except sitecustomize.CloseoutOfflineNetworkError:
    blocked += 1
try:
    sys.audit(
        "socket.sendmsg",
        types.SimpleNamespace(family=socket.AF_INET),
        ("198.51.100.11", 9),
    )
except sitecustomize.CloseoutOfflineNetworkError:
    blocked += 1
if blocked != 3 or not sitecustomize.guard_self_check():
    raise SystemExit(11)
'''
            arguments = [sys.executable, "-s"]
            if sys.version_info >= (3, 11):
                arguments.append("-P")
            arguments.extend(("-c", program))
            completed = subprocess.run(
                arguments,
                cwd=run_closeout_tests.PROJECT_ROOT,
                env=run_closeout_tests._sanitized_environment(os.fspath(audit)),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )

            self.assertEqual(
                completed.returncode,
                0,
                completed.stderr.decode("utf-8", errors="replace"),
            )
            self.assertEqual(audit.read_bytes().splitlines(), [b"1", b"1", b"1"])

    def test_truncating_a_blocked_attempt_back_to_zero_invalidates_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            audit = Path(temporary) / "network-audit.log"
            descriptor = os.open(
                audit,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            os.close(descriptor)
            program = r'''
import os
import sitecustomize
import sys
from scripts import run_closeout_tests

initial = sitecustomize.audit_snapshot()
try:
    sys.audit("socket.getaddrinfo", "example.invalid", 443, 0, 0, 0)
except sitecustomize.CloseoutOfflineNetworkError:
    pass
else:
    raise SystemExit(20)
descriptor = os.open(sys.argv[1], os.O_WRONLY | os.O_TRUNC)
os.fsync(descriptor)
os.close(descriptor)
metadata = os.stat(sys.argv[1])
os.utime(
    sys.argv[1],
    ns=(metadata.st_atime_ns, initial[4] + 1_000_000_000),
)
final = sitecustomize.audit_snapshot()
if final[3] != 0:
    raise SystemExit(21)
if run_closeout_tests._audit_is_pristine_and_unchanged(initial, final):
    raise SystemExit(22)
'''
            arguments = [sys.executable, "-s"]
            if sys.version_info >= (3, 11):
                arguments.append("-P")
            arguments.extend(("-c", program, os.fspath(audit)))
            completed = subprocess.run(
                arguments,
                cwd=run_closeout_tests.PROJECT_ROOT,
                env=run_closeout_tests._sanitized_environment(os.fspath(audit)),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )

            self.assertEqual(
                completed.returncode,
                0,
                completed.stderr.decode("utf-8", errors="replace"),
            )
            self.assertEqual(audit.read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()
