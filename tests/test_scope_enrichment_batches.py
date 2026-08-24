from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.enrich_journal_scope_catalog import load_attempted_entity_ids
from scripts.run_scope_enrichment_batches import requeue_latest_attempts, unhealthy_batch


class BatchHealthTests(unittest.TestCase):
    def test_healthy_mixed_batch_does_not_trip_breaker(self) -> None:
        payload = {
            "selected": 50,
            "outcomes": {"ok": 19, "not_relevant": 24, "no_candidate_pages": 7},
        }
        self.assertEqual(unhealthy_batch(payload), "")

    def test_all_missing_pages_trips_breaker(self) -> None:
        payload = {"selected": 50, "outcomes": {"no_candidate_pages": 50}}
        self.assertIn("circuit breaker", unhealthy_batch(payload))

    def test_small_smoke_batch_does_not_trip_breaker(self) -> None:
        payload = {"selected": 2, "outcomes": {"no_candidate_pages": 2}}
        self.assertEqual(unhealthy_batch(payload), "")

    def test_latest_batch_can_be_requeued(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "attempts.jsonl"
            path.write_text(
                '{"entity_id": 1, "name": "A"}\n'
                '{"entity_id": 2, "name": "B"}\n',
                encoding="utf-8",
            )
            self.assertEqual(requeue_latest_attempts(path, 1, reason="outage"), [2])
            self.assertEqual(load_attempted_entity_ids(path), {1})


if __name__ == "__main__":
    unittest.main()
