from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

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
        self.assertEqual(external_call_ledger_status(self.ledger)["used"], 3)
        text = self.ledger.read_text(encoding="utf-8")
        self.assertNotIn("secret", text)
        self.assertNotIn("private", text)
        records = [json.loads(line) for line in text.splitlines()[1:]]
        self.assertEqual(
            {record["endpoint"] for record in records},
            {"https://example.invalid"},
        )

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
        before = self.ledger.read_bytes()
        with self.assertRaises(FileExistsError):
            initialize_external_call_ledger(
                self.ledger, budget=99, run_id="replacement"
            )
        self.assertEqual(self.ledger.read_bytes(), before)

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

    def test_same_length_in_place_tamper_invalidates_cache_before_transport(
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
        # Make the cache-identity transition deterministic even on a filesystem
        # whose automatic timestamp update has coarse resolution.
        changed = self.ledger.stat()
        os.utime(
            self.ledger,
            ns=(changed.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
        )
        after = self.ledger.stat()
        self.assertEqual(
            (after.st_dev, after.st_ino, after.st_size),
            (before.st_dev, before.st_ino, before.st_size),
        )
        self.assertNotEqual(
            (after.st_mtime_ns, after.st_ctime_ns),
            (before.st_mtime_ns, before.st_ctime_ns),
        )
        tampered = self.ledger.read_bytes()

        with mock.patch.dict(os.environ, self.environment, clear=False):
            with mock.patch(
                "where_paper_go.enrichment.urllib.request.urlopen"
            ) as urlopen, self.assertRaisesRegex(
                ExternalCallBudgetError, "non-sequential ordinal"
            ):
                http_request(
                    "https://example.invalid/live",
                    external_call_kind="search",
                )
        urlopen.assert_not_called()
        self.assertEqual(self.ledger.read_bytes(), tampered)

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


if __name__ == "__main__":
    unittest.main()
