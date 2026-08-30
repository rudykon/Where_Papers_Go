#!/usr/bin/env python3
"""Run one fixed closeout unittest suite and emit aggregate evidence only.

The test-ID digest is SHA-256 over this byte sequence::

    b"where-papers-go-closeout-test-ids-v1\\0" +
    b"\\0".join(sorted(unique_test_ids_as_utf8)) + b"\\0"

Neither test IDs nor failure details are written to stdout or the report.
"""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
import json
import os
from pathlib import Path
import site
import stat
import subprocess
import sys
from typing import Any
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GUARD_DIRECTORY = PROJECT_ROOT / "scripts" / "closeout_offline_guard"
GUARD_FILE = GUARD_DIRECTORY / "sitecustomize.py"
AUDIT_ENV = "WPG_CLOSEOUT_NETWORK_AUDIT"
ACTIVE_ENV = "WPG_CLOSEOUT_OFFLINE_GUARD_ACTIVE"
BOOTSTRAP_ENV = "WPG_CLOSEOUT_RUNNER_BOOTSTRAPPED"
ARTIFACT_TYPE = "where_papers_go_closeout_test_report"
MODEL_MODULES = ("tests.test_model_runs", "tests.test_local_model_runtime")
ID_HASH_DOMAIN = b"where-papers-go-closeout-test-ids-v1\0"
EMPTY_ID_SHA256 = hashlib.sha256(ID_HASH_DOMAIN).hexdigest()
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

REPORT_KEYS = {
    "schema_version",
    "artifact_type",
    "guard_active",
    "total",
    "passed",
    "skipped",
    "failures",
    "errors",
    "expected_failures",
    "unexpected_successes",
    "test_id_count",
    "test_id_sha256",
}


def _trusted_user_site() -> tuple[Path, ...]:
    """Expose installed dependencies without enabling user-site ``.pth`` files."""

    if sys.prefix != sys.base_prefix:
        return ()
    try:
        raw_paths = site.getusersitepackages()
    except (AttributeError, OSError):
        return ()
    if isinstance(raw_paths, str):
        candidates = (raw_paths,)
    else:
        candidates = tuple(raw_paths)
    trusted: list[Path] = []
    for raw_path in candidates:
        try:
            path = Path(raw_path)
            if not path.is_absolute() or path.resolve() != path:
                continue
            metadata = path.lstat()
        except (OSError, TypeError, ValueError):
            continue
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & stat.S_IWOTH
            or (metadata.st_mode & stat.S_IWGRP and metadata.st_gid != os.getegid())
        ):
            continue
        trusted.append(path)
    return tuple(trusted)


TRUSTED_PYTHONPATH_ENTRIES = (
    GUARD_DIRECTORY,
    PROJECT_ROOT,
    *_trusted_user_site(),
)


def _empty_report(*, guard_active: bool) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_type": ARTIFACT_TYPE,
        "guard_active": guard_active,
        "total": 1,
        "passed": 0,
        "skipped": 0,
        "failures": 0,
        "errors": 1,
        "expected_failures": 0,
        "unexpected_successes": 0,
        "test_id_count": 0,
        "test_id_sha256": EMPTY_ID_SHA256,
    }


def _child_python_arguments(program: str) -> list[str]:
    arguments = [sys.executable, "-s"]
    if sys.version_info >= (3, 11):
        arguments.append("-P")
    arguments.extend(("-c", program))
    return arguments


def _sanitized_environment(audit_path: str) -> dict[str, str]:
    source = os.environ
    environment: dict[str, str] = {}
    for name in ("HOME", "USER", "LOGNAME", "SHELL"):
        value = source.get(name)
        if value:
            environment[name] = value
    trusted_pythonpath = os.pathsep.join(
        os.fspath(path) for path in TRUSTED_PYTHONPATH_ENTRIES
    )
    environment.update(
        {
            AUDIT_ENV: audit_path,
            BOOTSTRAP_ENV: "1",
            "PYTHONPATH": trusted_pythonpath,
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "PYTHONUTF8": "1",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
            "TMPDIR": "/tmp",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "NO_PROXY": "localhost,127.0.0.0/8,::1",
            "no_proxy": "localhost,127.0.0.0/8,::1",
        }
    )
    environment.pop(ACTIVE_ENV, None)
    return environment


def _bootstrap_guard() -> bool:
    if os.environ.get(BOOTSTRAP_ENV) == "1":
        return True
    raw_audit_path = os.environ.get(AUDIT_ENV, "")
    if not raw_audit_path or "\x00" in raw_audit_path:
        return False
    audit_path = Path(raw_audit_path)
    if not audit_path.is_absolute():
        return False
    arguments = [sys.executable, "-s"]
    if sys.version_info >= (3, 11):
        arguments.append("-P")
    arguments.extend((os.fspath(Path(__file__).resolve()), *sys.argv[1:]))
    try:
        os.execve(
            sys.executable,
            arguments,
            _sanitized_environment(os.fspath(audit_path)),
        )
    except OSError:
        return False
    return False


def _expected_guard_module() -> Any | None:
    module = sys.modules.get("sitecustomize")
    if module is None:
        return None
    try:
        loaded_path = Path(module.__file__).resolve()
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    if loaded_path != GUARD_FILE.resolve():
        return None
    if getattr(module, "GUARD_IMPLEMENTATION_VERSION", None) != 1:
        return None
    try:
        if not bool(module.guard_self_check()):
            return None
    except BaseException:
        return None
    return module


def _child_inherits_guard() -> bool:
    program = (
        "import pathlib,sitecustomize,sys;"
        "expected=pathlib.Path(sys.argv[1]).resolve();"
        "actual=pathlib.Path(sitecustomize.__file__).resolve();"
        "raise SystemExit(0 if actual==expected and "
        "sitecustomize.guard_self_check() else 9)"
    )
    arguments = _child_python_arguments(program)
    arguments.append(os.fspath(GUARD_FILE.resolve()))
    try:
        completed = subprocess.run(
            arguments,
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _parse_arguments(arguments: list[str]) -> tuple[str, Path | None] | None:
    suite_name: str | None = None
    output_path: Path | None = None
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option not in {"--suite", "--output"} or index + 1 >= len(arguments):
            return None
        value = arguments[index + 1]
        index += 2
        if option == "--suite":
            if suite_name is not None or value not in {"full", "model-focused"}:
                return None
            suite_name = value
        else:
            if output_path is not None or not value or "\x00" in value:
                return None
            output_path = Path(value).resolve(strict=False)
    if suite_name is None:
        return None
    return suite_name, output_path


def _open_report(path: Path) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_CLOEXEC | _O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("closeout report is not a regular file")
        os.fchmod(descriptor, 0o600)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _silence_process_output() -> tuple[int, int]:
    sys.stdout.flush()
    sys.stderr.flush()
    saved_stdout = os.dup(1)
    devnull = os.open(os.devnull, os.O_WRONLY | _O_CLOEXEC)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    return saved_stdout, devnull


def _flatten_suite(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    flattened: list[unittest.TestCase] = []

    def visit(value: Any) -> None:
        if isinstance(value, unittest.TestSuite):
            for child in value:
                visit(child)
            return
        identifier = getattr(value, "id", None)
        if not callable(identifier):
            raise TypeError("discovered unittest object has no test ID")
        flattened.append(value)

    visit(suite)
    return flattened


def _test_id_digest(test_ids: Iterable[str]) -> str:
    digest = hashlib.sha256()
    digest.update(ID_HASH_DOMAIN)
    for identifier in sorted(test_ids):
        digest.update(identifier.encode("utf-8", errors="strict"))
        digest.update(b"\0")
    return digest.hexdigest()


class AggregateTestResult(unittest.TestResult):
    """Count one final outcome per test without retaining failure details."""

    _PRECEDENCE = {
        "passed": 0,
        "skipped": 1,
        "expected_failures": 2,
        "unexpected_successes": 3,
        "failures": 4,
        "errors": 5,
    }

    def __init__(self) -> None:
        super().__init__()
        self.executed_ids: list[str] = []
        self._active: dict[int, tuple[str, str]] = {}
        self.counts = {name: 0 for name in self._PRECEDENCE}
        self.fixture_outcomes = 0

    def startTest(self, test: unittest.TestCase) -> None:  # noqa: N802
        super().startTest(test)
        identifier = test.id()
        self.executed_ids.append(identifier)
        self._active[id(test)] = (identifier, "passed")

    def _mark(self, test: Any, outcome: str) -> None:
        key = id(test)
        current = self._active.get(key)
        if current is None:
            # Python 3.14 reports skipTest() inside a subTest through the
            # _SubTest wrapper.  Collapse it into the one discovered parent
            # test so aggregate totals remain one final outcome per test ID.
            parent = getattr(test, "test_case", None)
            key = id(parent)
            current = self._active.get(key)
        if current is None:
            self.counts[outcome] += 1
            self.fixture_outcomes += 1
            return
        identifier, previous = current
        if self._PRECEDENCE[outcome] >= self._PRECEDENCE[previous]:
            self._active[key] = (identifier, outcome)

    def stopTest(self, test: unittest.TestCase) -> None:  # noqa: N802
        current = self._active.pop(id(test), None)
        if current is not None:
            self.counts[current[1]] += 1
        super().stopTest(test)

    def addSuccess(self, test: unittest.TestCase) -> None:  # noqa: N802
        self._mark(test, "passed")

    def addFailure(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        del err
        self._mark(test, "failures")

    def addError(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        del err
        self._mark(test, "errors")

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:  # noqa: N802
        del reason
        self._mark(test, "skipped")

    def addExpectedFailure(  # noqa: N802
        self, test: unittest.TestCase, err: Any
    ) -> None:
        del err
        self._mark(test, "expected_failures")

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:  # noqa: N802
        self._mark(test, "unexpected_successes")

    def addSubTest(self, test: unittest.TestCase, subtest: Any, err: Any) -> None:  # noqa: N802
        del subtest
        if err is None:
            return
        exception_type = err[0]
        try:
            is_failure = issubclass(exception_type, test.failureException)
        except TypeError:
            is_failure = False
        self._mark(test, "failures" if is_failure else "errors")

    def close_interrupted_tests(self) -> None:
        for _identifier, _outcome in tuple(self._active.values()):
            self.counts["errors"] += 1
        self._active.clear()

    def aggregate(self) -> dict[str, int]:
        return dict(self.counts)


def _load_suite(name: str) -> unittest.TestSuite:
    loader = unittest.TestLoader()
    if name == "full":
        return loader.discover(
            start_dir=os.fspath(PROJECT_ROOT / "tests"),
            pattern="test_*.py",
        )
    if name == "model-focused":
        return loader.loadTestsFromNames(MODEL_MODULES)
    raise ValueError("unsupported fixed closeout suite")


def _invalidate_counts(counts: dict[str, int]) -> None:
    if counts["failures"] or counts["errors"] or counts["unexpected_successes"]:
        return
    for source in ("passed", "skipped", "expected_failures"):
        if counts[source]:
            counts[source] -= 1
            counts["errors"] += 1
            return
    counts["errors"] = 1


def _audit_snapshot(module: Any) -> tuple[str, int, int, int, int, int] | None:
    try:
        snapshot = module.audit_snapshot()
    except BaseException:
        return None
    if (
        not isinstance(snapshot, tuple)
        or len(snapshot) != 6
        or not isinstance(snapshot[0], str)
        or any(isinstance(value, bool) or not isinstance(value, int) for value in snapshot[1:])
    ):
        return None
    return snapshot


def _audit_is_pristine_and_unchanged(
    initial: tuple[str, int, int, int, int, int] | None,
    final: tuple[str, int, int, int, int, int] | None,
) -> bool:
    """Require the same path/inode, zero bytes, and unchanged mtime/ctime."""

    return bool(
        initial is not None
        and final is not None
        and initial == final
        and final[3] == 0
    )


def _run_suite(name: str, guard_module: Any) -> dict[str, Any]:
    discovered_ids: list[str] = []
    result = AggregateTestResult()
    try:
        integrity_ok = bool(guard_module.guard_self_check())
    except BaseException:
        integrity_ok = False
    try:
        suite = _load_suite(name)
        cases = _flatten_suite(suite)
        discovered_ids = [case.id() for case in cases]
        if any(
            not isinstance(identifier, str)
            or not identifier
            or "\0" in identifier
            for identifier in discovered_ids
        ):
            raise ValueError("invalid discovered unittest ID")
        if len(discovered_ids) != len(set(discovered_ids)):
            raise ValueError("duplicate discovered unittest ID")
        suite.run(result)
    except BaseException:
        integrity_ok = False
    finally:
        result.close_interrupted_tests()

    if tuple(result.executed_ids) != tuple(discovered_ids):
        integrity_ok = False
    if result.fixture_outcomes:
        integrity_ok = False
    try:
        final_guard_active = bool(guard_module.guard_self_check())
    except BaseException:
        final_guard_active = False
    if not final_guard_active:
        integrity_ok = False
    counts = result.aggregate()
    if not integrity_ok:
        _invalidate_counts(counts)
    total = sum(counts.values())
    if total == 0:
        counts["errors"] = 1
        total = 1
    report: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": ARTIFACT_TYPE,
        "guard_active": True,
        "total": total,
        "passed": counts["passed"],
        "skipped": counts["skipped"],
        "failures": counts["failures"],
        "errors": counts["errors"],
        "expected_failures": counts["expected_failures"],
        "unexpected_successes": counts["unexpected_successes"],
        "test_id_count": len(discovered_ids),
        "test_id_sha256": _test_id_digest(discovered_ids),
    }
    return report


def _encode_report(report: dict[str, Any]) -> bytes:
    if set(report) != REPORT_KEYS:
        raise ValueError("internal closeout report key mismatch")
    return (
        json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def _write_all(descriptor: int, payload: bytes, *, synchronize: bool) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("could not write closeout report")
        view = view[written:]
    if synchronize:
        os.fsync(descriptor)


def main() -> int:
    if not _bootstrap_guard():
        payload = _encode_report(_empty_report(guard_active=False))
        try:
            os.write(1, payload)
        except OSError:
            return 2
        return 2

    parsed = _parse_arguments(sys.argv[1:])
    guard_module = _expected_guard_module()
    guard_active = guard_module is not None
    report_fd: int | None = None
    if parsed is not None and parsed[1] is not None:
        try:
            report_fd = _open_report(parsed[1])
        except OSError:
            parsed = None

    try:
        saved_stdout, devnull = _silence_process_output()
    except OSError:
        if report_fd is not None:
            os.close(report_fd)
        return 2

    target_fd = report_fd if report_fd is not None else saved_stdout
    report = _empty_report(guard_active=guard_active)
    initial_audit: tuple[str, int, int, int, int, int] | None = None
    try:
        if parsed is not None and guard_module is not None:
            initial_audit = _audit_snapshot(guard_module)
            audit_is_pristine = _audit_is_pristine_and_unchanged(
                initial_audit, initial_audit
            )
            if audit_is_pristine and _child_inherits_guard():
                report = _run_suite(parsed[0], guard_module)
            else:
                report = _empty_report(guard_active=guard_active)

            final_audit = _audit_snapshot(guard_module)
            audit_unchanged = _audit_is_pristine_and_unchanged(
                initial_audit, final_audit
            )
            if not audit_unchanged:
                counts = {
                    key: int(report[key])
                    for key in (
                        "passed",
                        "skipped",
                        "failures",
                        "errors",
                        "expected_failures",
                        "unexpected_successes",
                    )
                }
                _invalidate_counts(counts)
                report.update(counts)
                report["total"] = sum(counts.values())
        payload = _encode_report(report)
        _write_all(target_fd, payload, synchronize=report_fd is not None)
    except BaseException:
        return_code = 2
    else:
        return_code = 0
        if (
            report["guard_active"] is not True
            or report["failures"] != 0
            or report["errors"] != 0
            or report["expected_failures"] != 0
            or report["unexpected_successes"] != 0
        ):
            return_code = 1
    finally:
        for descriptor in (report_fd, saved_stdout, devnull):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
