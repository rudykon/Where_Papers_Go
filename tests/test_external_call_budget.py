from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from where_paper_go import external_call_budget as budget_module
from where_paper_go.external_call_budget import (
    BUDGET_ENV,
    LEDGER_ENV,
    RUN_ID_ENV,
    ExternalCallBudgetError,
    ExternalCallBudgetExceeded,
    external_call_ledger_status,
    initialize_external_call_ledger,
    reserve_external_call,
)
from where_paper_go.enrichment import http_request, http_stream_request
from where_paper_go.embeddings import EmbeddingConfig, OpenAICompatibleEmbeddingProvider


class ExternalCallBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.ledger = Path(self.temporary.name) / "ledger.jsonl"
        initialize_external_call_ledger(
            self.ledger, budget=3, run_id="unit-test-run"
        )
        self.highwater = self.ledger.with_name(
            self.ledger.name + budget_module.LEDGER_HIGHWATER_SUFFIX
        )
        self.binding = self.ledger.with_name(
            self.ledger.name + budget_module.LEDGER_BINDING_SUFFIX
        )
        self.environment = {
            LEDGER_ENV: str(self.ledger),
            BUDGET_ENV: "3",
            RUN_ID_ENV: "unit-test-run",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_concurrent_attempts_cannot_exceed_durable_budget(self) -> None:
        def reserve(_index: int) -> str:
            try:
                reserve_external_call(
                    "search", "https://example.invalid/search?api_key=secret&q=private"
                )
                return "reserved"
            except ExternalCallBudgetExceeded:
                return "blocked"

        with mock.patch.dict(os.environ, self.environment, clear=False):
            with ThreadPoolExecutor(max_workers=12) as executor:
                outcomes = list(executor.map(reserve, range(20)))
        self.assertEqual(outcomes.count("reserved"), 3)
        self.assertEqual(outcomes.count("blocked"), 17)
        status = external_call_ledger_status(self.ledger)
        self.assertEqual(status["used"], 3)
        self.assertTrue(status["continuity_verified"])
        self.assertEqual(self.ledger.read_bytes(), self.highwater.read_bytes())
        text = self.ledger.read_text(encoding="utf-8")
        self.assertNotIn("secret", text)
        self.assertNotIn("private", text)
        records = [json.loads(line) for line in text.splitlines()[1:]]
        self.assertEqual(
            {record["endpoint"] for record in records},
            {"https://example.invalid"},
        )

    def test_independent_processes_cannot_exceed_durable_budget(self) -> None:
        ledger = Path(self.temporary.name) / "process-ledger.jsonl"
        process_budget = 7
        run_id = "process-concurrency-test"
        initialize_external_call_ledger(
            ledger, budget=process_budget, run_id=run_id
        )
        environment = {
            **os.environ,
            LEDGER_ENV: str(ledger),
            BUDGET_ENV: str(process_budget),
            RUN_ID_ENV: run_id,
        }
        program = (
            "from where_paper_go.external_call_budget import "
            "ExternalCallBudgetExceeded,reserve_external_call\n"
            "try:\n"
            " reserve_external_call('process-test','https://example.invalid/test')\n"
            "except ExternalCallBudgetExceeded:\n"
            " raise SystemExit(3)\n"
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", program],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            for _index in range(20)
        ]
        return_codes: list[int] = []
        for process in processes:
            _stdout, stderr = process.communicate(timeout=15)
            if process.returncode not in (0, 3):
                self.fail(
                    f"reservation subprocess failed ({process.returncode}): "
                    f"{stderr.decode('utf-8', errors='replace')}"
                )
            return_codes.append(int(process.returncode))
        self.assertEqual(return_codes.count(0), process_budget)
        self.assertEqual(return_codes.count(3), 20 - process_budget)
        self.assertEqual(external_call_ledger_status(ledger)["used"], process_budget)

    def test_initialization_publishes_private_highwater_and_immutable_binding(
        self,
    ) -> None:
        self.assertEqual(self.ledger.read_bytes(), self.highwater.read_bytes())
        self.assertEqual(stat.S_IMODE(self.ledger.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.highwater.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.binding.stat().st_mode), 0o400)
        binding = json.loads(self.binding.read_text(encoding="utf-8"))
        self.assertEqual(binding["artifact_type"], "external_call_ledger_binding")
        self.assertEqual(binding["run_id"], "unit-test-run")
        self.assertEqual(binding["budget"], 3)
        self.assertEqual(binding["ledger_device"], self.ledger.stat().st_dev)
        self.assertEqual(binding["ledger_inode"], self.ledger.stat().st_ino)
        self.assertEqual(binding["highwater_device"], self.highwater.stat().st_dev)
        self.assertEqual(binding["highwater_inode"], self.highwater.stat().st_ino)
        self.assertEqual(
            binding["ledger_path_sha256"],
            hashlib.sha256(str(self.ledger.resolve()).encode()).hexdigest(),
        )

    def test_invalid_identity_never_creates_partial_continuity_files(self) -> None:
        for index, invalid_budget in enumerate((0, -1, True)):
            candidate = Path(self.temporary.name) / f"invalid-budget-{index}.jsonl"
            with self.subTest(budget=invalid_budget), self.assertRaises(
                ExternalCallBudgetError
            ):
                initialize_external_call_ledger(
                    candidate,
                    budget=invalid_budget,
                    run_id="invalid-budget",
                )
            self.assertFalse(candidate.exists())
        candidate = Path(self.temporary.name) / "invalid-run-id.jsonl"
        with self.assertRaises(ExternalCallBudgetError):
            initialize_external_call_ledger(candidate, budget=1, run_id="  ")
        self.assertFalse(candidate.exists())

    def test_partial_environment_fails_closed(self) -> None:
        with mock.patch.dict(
            os.environ,
            {LEDGER_ENV: str(self.ledger)},
            clear=True,
        ), self.assertRaisesRegex(ExternalCallBudgetError, "incomplete"):
            reserve_external_call("llm", "https://example.invalid/chat")

    def test_limiter_is_dormant_outside_an_explicit_budgeted_worker(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(
                reserve_external_call("http", "https://example.invalid/unbudgeted")
            )
        self.assertEqual(external_call_ledger_status(self.ledger)["used"], 0)

    def test_existing_ledger_is_never_overwritten(self) -> None:
        before = {
            path: path.read_bytes()
            for path in (self.ledger, self.highwater, self.binding)
        }
        with self.assertRaises(FileExistsError):
            initialize_external_call_ledger(
                self.ledger, budget=99, run_id="replacement"
            )
        self.assertEqual(
            {path: path.read_bytes() for path in before},
            before,
        )

    def test_http_transport_is_not_opened_after_budget_exhaustion(self) -> None:
        with mock.patch.dict(os.environ, self.environment, clear=False):
            for _index in range(3):
                reserve_external_call("test", "https://example.invalid/preflight")
            with mock.patch(
                "where_paper_go.enrichment.urllib.request.urlopen"
            ) as urlopen, self.assertRaises(ExternalCallBudgetExceeded):
                http_request(
                    "https://example.invalid/live?authorization=secret",
                    external_call_kind="search",
                )
        urlopen.assert_not_called()

    def test_same_length_in_place_tamper_fails_before_transport(
        self,
    ) -> None:
        with mock.patch.dict(os.environ, self.environment, clear=False):
            self.assertEqual(
                reserve_external_call("test", "https://example.invalid/preflight"),
                1,
            )

        before = self.ledger.stat()
        payload = self.ledger.read_bytes()
        marker = b'"ordinal":1'
        replacement = b'"ordinal":9'
        self.assertEqual(len(marker), len(replacement))
        self.assertEqual(payload.count(marker), 1)
        offset = payload.index(marker)
        with self.ledger.open("r+b") as handle:
            handle.seek(offset)
            handle.write(replacement)
            handle.flush()
            os.fsync(handle.fileno())
        after = self.ledger.stat()
        self.assertEqual(
            (after.st_dev, after.st_ino, after.st_size),
            (before.st_dev, before.st_ino, before.st_size),
        )
        tampered = self.ledger.read_bytes()

        with mock.patch.dict(os.environ, self.environment, clear=False):
            with mock.patch(
                "where_paper_go.enrichment.urllib.request.urlopen"
            ) as urlopen, self.assertRaisesRegex(
                ExternalCallBudgetError, "rolled back|non-sequential ordinal"
            ):
                http_request(
                    "https://example.invalid/live",
                    external_call_kind="search",
                )
        urlopen.assert_not_called()
        self.assertEqual(self.ledger.read_bytes(), tampered)

    def test_torn_tail_is_rejected_by_status_and_before_transport(self) -> None:
        with mock.patch.dict(os.environ, self.environment, clear=False):
            reserve_external_call("test", "https://example.invalid/first")
        payload = self.ledger.read_bytes()
        with self.ledger.open("r+b") as handle:
            handle.truncate(len(payload) - 7)
            handle.flush()
            os.fsync(handle.fileno())

        with self.assertRaisesRegex(ExternalCallBudgetError, "rolled back|diverged"):
            external_call_ledger_status(self.ledger)
        with mock.patch.dict(os.environ, self.environment, clear=False):
            with mock.patch(
                "where_paper_go.enrichment.urllib.request.urlopen"
            ) as urlopen, self.assertRaisesRegex(
                ExternalCallBudgetError, "rolled back|diverged"
            ):
                http_request(
                    "https://example.invalid/blocked",
                    external_call_kind="search",
                )
        urlopen.assert_not_called()

    def test_matching_partial_tail_still_fails_jsonl_continuity(self) -> None:
        with mock.patch.dict(os.environ, self.environment, clear=False):
            reserve_external_call("test", "https://example.invalid/first")
        for path in (self.ledger, self.highwater):
            payload = path.read_bytes()
            with path.open("r+b") as handle:
                handle.truncate(len(payload) - 7)
                handle.flush()
                os.fsync(handle.fileno())

        with self.assertRaisesRegex(ExternalCallBudgetError, "truncated final"):
            external_call_ledger_status(self.ledger)

    def test_valid_older_prefix_cannot_restore_spent_allowance(self) -> None:
        with mock.patch.dict(os.environ, self.environment, clear=False):
            self.assertEqual(
                reserve_external_call("test", "https://example.invalid/first"), 1
            )
            valid_prefix = self.ledger.read_bytes()
            self.assertEqual(
                reserve_external_call("test", "https://example.invalid/second"), 2
            )
        original_inode = self.ledger.stat().st_ino
        with self.ledger.open("r+b") as handle:
            handle.seek(0)
            handle.write(valid_prefix)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
        self.assertEqual(self.ledger.stat().st_ino, original_inode)

        with mock.patch.dict(os.environ, self.environment, clear=False):
            with mock.patch(
                "where_paper_go.enrichment.urllib.request.urlopen"
            ) as urlopen, self.assertRaisesRegex(
                ExternalCallBudgetError, "rolled back|diverged"
            ):
                http_request(
                    "https://example.invalid/would-restore-slot",
                    external_call_kind="search",
                )
        urlopen.assert_not_called()
        self.assertEqual(self.ledger.read_bytes(), valid_prefix)
        self.assertEqual(len(self.highwater.read_text().splitlines()), 3)

    def test_highwater_rollback_is_also_rejected(self) -> None:
        with mock.patch.dict(os.environ, self.environment, clear=False):
            reserve_external_call("test", "https://example.invalid/first")
            valid_prefix = self.highwater.read_bytes()
            reserve_external_call("test", "https://example.invalid/second")
        with self.highwater.open("r+b") as handle:
            handle.seek(0)
            handle.write(valid_prefix)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())

        with self.assertRaisesRegex(ExternalCallBudgetError, "rolled back|diverged"):
            external_call_ledger_status(self.ledger)

    def test_coordinated_same_permission_rollback_is_explicitly_out_of_scope(
        self,
    ) -> None:
        with mock.patch.dict(os.environ, self.environment, clear=False):
            reserve_external_call("test", "https://example.invalid/first")
            valid_prefix = self.ledger.read_bytes()
            reserve_external_call("test", "https://example.invalid/second")
        binding_before = self.binding.read_bytes()
        inode_before = {
            path: path.stat().st_ino for path in (self.ledger, self.highwater)
        }
        for path in (self.ledger, self.highwater):
            with path.open("r+b") as handle:
                handle.seek(0)
                handle.write(valid_prefix)
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())

        # This is deliberately documented as outside the local threat model:
        # a same-permission principal can coordinate both writable copies while
        # preserving their bound inodes and need not touch the 0400 binding.
        status = external_call_ledger_status(self.ledger)
        self.assertEqual(status["used"], 1)
        self.assertEqual(status["remaining"], 2)
        self.assertTrue(status["continuity_verified"])
        self.assertEqual(self.binding.read_bytes(), binding_before)
        self.assertEqual(
            {path: path.stat().st_ino for path in (self.ledger, self.highwater)},
            inode_before,
        )

    def test_identical_content_inode_replacement_is_rejected_by_binding(self) -> None:
        replacement = self.ledger.with_name("replacement-ledger.jsonl")
        replacement.write_bytes(self.ledger.read_bytes())
        replacement.chmod(0o600)
        original_inode = self.ledger.stat().st_ino
        os.replace(replacement, self.ledger)
        self.assertNotEqual(self.ledger.stat().st_ino, original_inode)

        with mock.patch.dict(os.environ, self.environment, clear=False):
            with mock.patch(
                "where_paper_go.enrichment.urllib.request.urlopen"
            ) as urlopen, self.assertRaisesRegex(
                ExternalCallBudgetError, "binding mismatch"
            ):
                http_request(
                    "https://example.invalid/replaced",
                    external_call_kind="search",
                )
        urlopen.assert_not_called()

    def test_matching_budget_rewrite_is_rejected_by_immutable_binding(self) -> None:
        for path in (self.ledger, self.highwater):
            payload = path.read_bytes()
            self.assertEqual(payload.count(b'"budget":3'), 1)
            rewritten = payload.replace(b'"budget":3', b'"budget":9')
            with path.open("r+b") as handle:
                handle.seek(0)
                handle.write(rewritten)
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())

        with self.assertRaisesRegex(ExternalCallBudgetError, "binding mismatch"):
            external_call_ledger_status(self.ledger)

    def test_missing_highwater_fails_before_transport(self) -> None:
        self.highwater.unlink()
        with mock.patch.dict(os.environ, self.environment, clear=False):
            with mock.patch(
                "where_paper_go.enrichment.urllib.request.urlopen"
            ) as urlopen, self.assertRaisesRegex(
                ExternalCallBudgetError, "continuity files are unavailable"
            ):
                http_request(
                    "https://example.invalid/missing-highwater",
                    external_call_kind="search",
                )
        urlopen.assert_not_called()

    def test_missing_or_writable_binding_fails_closed(self) -> None:
        self.binding.chmod(0o600)
        with self.assertRaisesRegex(ExternalCallBudgetError, "mode 0400"):
            external_call_ledger_status(self.ledger)

        other = Path(self.temporary.name) / "missing-binding-ledger.jsonl"
        initialize_external_call_ledger(other, budget=1, run_id="missing-binding")
        other_binding = other.with_name(
            other.name + budget_module.LEDGER_BINDING_SUFFIX
        )
        other_binding.unlink()
        environment = {
            LEDGER_ENV: str(other),
            BUDGET_ENV: "1",
            RUN_ID_ENV: "missing-binding",
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            with mock.patch(
                "where_paper_go.enrichment.urllib.request.urlopen"
            ) as urlopen, self.assertRaisesRegex(
                ExternalCallBudgetError, "binding is unavailable"
            ):
                http_request(
                    "https://example.invalid/missing-binding",
                    external_call_kind="search",
                )
        urlopen.assert_not_called()

    def test_crash_after_highwater_write_is_permanently_fail_closed(self) -> None:
        real_append = budget_module._append_durable
        write_count = 0

        def fail_after_first_write(descriptor: int, payload: bytes) -> None:
            nonlocal write_count
            real_append(descriptor, payload)
            write_count += 1
            if write_count == 1:
                raise OSError("injected crash after high-water fsync")

        with mock.patch.dict(os.environ, self.environment, clear=False):
            with mock.patch.object(
                budget_module,
                "_append_durable",
                side_effect=fail_after_first_write,
            ), mock.patch(
                "where_paper_go.enrichment.urllib.request.urlopen"
            ) as urlopen, self.assertRaisesRegex(
                ExternalCallBudgetError, "could not be persisted"
            ):
                http_request(
                    "https://example.invalid/crash",
                    external_call_kind="search",
                )
        urlopen.assert_not_called()
        self.assertNotEqual(self.ledger.read_bytes(), self.highwater.read_bytes())
        with self.assertRaisesRegex(ExternalCallBudgetError, "rolled back|diverged"):
            external_call_ledger_status(self.ledger)

    def test_embedding_transport_uses_the_same_hard_ledger(self) -> None:
        config = EmbeddingConfig(
            provider="openai_compatible",
            base_url="https://embedding.invalid/v1",
            api_key="unit-test-placeholder",
            model="test",
            endpoint="https://embedding.invalid/v1/embeddings",
            dimensions=8,
            send_dimensions=True,
            timeout=1,
            batch_size=1,
            max_chars=100,
            max_retries=0,
            headers={},
            extra_body={},
        )
        provider = OpenAICompatibleEmbeddingProvider(config)
        with mock.patch.dict(os.environ, self.environment, clear=False):
            for _index in range(3):
                reserve_external_call("test", "https://example.invalid/preflight")
            with mock.patch(
                "where_paper_go.embeddings.urllib.request.urlopen"
            ) as urlopen, self.assertRaises(ExternalCallBudgetExceeded):
                provider.embed(["bounded query"])
        urlopen.assert_not_called()

    def test_streaming_transport_is_not_opened_after_exhaustion(self) -> None:
        with mock.patch.dict(os.environ, self.environment, clear=False):
            for _index in range(3):
                reserve_external_call("test", "https://example.invalid/preflight")
            with mock.patch(
                "where_paper_go.enrichment.urllib.request.urlopen"
            ) as urlopen, self.assertRaises(ExternalCallBudgetExceeded):
                http_stream_request(
                    "https://example.invalid/chat",
                    external_call_kind="llm",
                )
        urlopen.assert_not_called()

    def test_non_private_or_symlink_ledger_fails_closed(self) -> None:
        self.ledger.chmod(0o640)
        with mock.patch.dict(
            os.environ, self.environment, clear=False
        ), self.assertRaisesRegex(ExternalCallBudgetError, "private regular"):
            reserve_external_call("llm", "https://example.invalid/chat")

        self.ledger.chmod(0o600)
        link = self.ledger.with_name("ledger-link.jsonl")
        link.symlink_to(self.ledger)
        linked_environment = {**self.environment, LEDGER_ENV: str(link)}
        with mock.patch.dict(
            os.environ, linked_environment, clear=False
        ), self.assertRaisesRegex(ExternalCallBudgetError, "unavailable"):
            reserve_external_call("llm", "https://example.invalid/chat")

    def test_fifo_substitutions_fail_closed_without_blocking(self) -> None:
        program = (
            "from pathlib import Path\n"
            "import sys\n"
            "from where_paper_go.external_call_budget import "
            "ExternalCallBudgetError,external_call_ledger_status\n"
            "try:\n"
            " external_call_ledger_status(Path(sys.argv[1]))\n"
            "except ExternalCallBudgetError:\n"
            " raise SystemExit(0)\n"
            "raise SystemExit(9)\n"
        )

        cases: list[Path] = []
        ledger_fifo = Path(self.temporary.name) / "ledger-fifo.jsonl"
        os.mkfifo(ledger_fifo, 0o600)
        cases.append(ledger_fifo)

        highwater_ledger = Path(self.temporary.name) / "highwater-fifo.jsonl"
        initialize_external_call_ledger(
            highwater_ledger, budget=1, run_id="highwater-fifo"
        )
        highwater_fifo = highwater_ledger.with_name(
            highwater_ledger.name + budget_module.LEDGER_HIGHWATER_SUFFIX
        )
        highwater_fifo.unlink()
        os.mkfifo(highwater_fifo, 0o600)
        cases.append(highwater_ledger)

        binding_ledger = Path(self.temporary.name) / "binding-fifo.jsonl"
        initialize_external_call_ledger(
            binding_ledger, budget=1, run_id="binding-fifo"
        )
        binding_fifo = binding_ledger.with_name(
            binding_ledger.name + budget_module.LEDGER_BINDING_SUFFIX
        )
        binding_fifo.unlink()
        os.mkfifo(binding_fifo, 0o400)
        cases.append(binding_ledger)

        for ledger in cases:
            with self.subTest(ledger=ledger.name):
                completed = subprocess.run(
                    [sys.executable, "-c", program, str(ledger)],
                    cwd=Path(__file__).resolve().parents[1],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=2,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stderr.decode("utf-8", errors="replace"),
                )


if __name__ == "__main__":
    unittest.main()
