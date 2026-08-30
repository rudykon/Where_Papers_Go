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
        class SyntheticPassingTests(unittest.TestCase):
            def test_first(self) -> None:
                pass

            def test_second(self) -> None:
                pass

            def test_skipped_subtest_is_one_parent_outcome(self) -> None:
                with self.subTest(value="synthetic"):
                    self.skipTest("synthetic subtest skip")

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(
            SyntheticPassingTests
        )
        with patch.object(run_closeout_tests, "_load_suite", return_value=suite):
            report = run_closeout_tests._run_suite("full", _HealthyGuard())

        self.assertEqual(set(report), run_closeout_tests.REPORT_KEYS)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(
            report["artifact_type"],
            "where_papers_go_closeout_test_report",
        )
        self.assertTrue(report["guard_active"])
        self.assertEqual(report["total"], 3)
        self.assertEqual(report["passed"], 2)
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(report["test_id_count"], 3)
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
