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

from scripts import run_closeout_tests


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
            "cf862e1b9db067771bace29fc33381c603c7aca542e624b398a2808856713018",
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
