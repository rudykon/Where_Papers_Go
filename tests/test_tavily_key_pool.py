from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from where_paper_go.tavily_pool import (
    TavilyKeyPool,
    TavilyKeyPoolStateError,
    TavilyKeyPoolUnavailable,
    configured_tavily_keys,
)


def fake_keys(count: int) -> list[str]:
    return [f"tvly-fictitious-pool-key-{index:02d}" for index in range(count)]


class TavilyKeyPoolTests(TestCase):
    def test_twenty_keys_have_20000_capacity_and_no_plaintext_state(self) -> None:
        keys = fake_keys(20)
        with TemporaryDirectory() as directory:
            state_file = Path(directory) / "pool.json"
            pool = TavilyKeyPool(keys, quota_per_key=1000, state_file=state_file)
            summary = pool.summary()
            state_text = state_file.read_text(encoding="utf-8")

        self.assertEqual(summary["key_count"], 20)
        self.assertEqual(summary["total_capacity"], 20_000)
        self.assertEqual(summary["remaining"], 20_000)
        self.assertFalse(any(key in state_text for key in keys))

    def test_round_robin_distributes_evenly_and_survives_restart(self) -> None:
        keys = fake_keys(4)
        with TemporaryDirectory() as directory:
            state_file = Path(directory) / "pool.json"
            pool = TavilyKeyPool(keys, quota_per_key=10, state_file=state_file)
            positions = []
            for _ in range(8):
                lease = pool.acquire()
                positions.append(lease.position)
                pool.report_success(lease)
            restarted = TavilyKeyPool(keys, quota_per_key=10, state_file=state_file)
            next_lease = restarted.acquire()

        self.assertEqual(positions, [0, 1, 2, 3, 0, 1, 2, 3])
        self.assertEqual(next_lease.position, 0)

    def test_local_quota_is_a_hard_limit(self) -> None:
        with TemporaryDirectory() as directory:
            pool = TavilyKeyPool(
                fake_keys(2), quota_per_key=2, state_file=Path(directory) / "pool.json"
            )
            leases = [pool.acquire() for _ in range(4)]
            with self.assertRaises(TavilyKeyPoolUnavailable) as caught:
                pool.acquire()

        self.assertEqual([lease.position for lease in leases], [0, 1, 0, 1])
        self.assertTrue(caught.exception.exhausted)

    def test_429_cools_then_restores_a_key(self) -> None:
        now = [100.0]
        with TemporaryDirectory() as directory:
            pool = TavilyKeyPool(
                fake_keys(2),
                quota_per_key=10,
                state_file=Path(directory) / "pool.json",
                rate_limit_cooldown_seconds=60,
                clock=lambda: now[0],
            )
            first = pool.acquire()
            pool.report_failure(first, http_status=429, retry_after_seconds=60, event="http_429")
            second = pool.acquire()
            self.assertEqual(second.position, 1)
            pool.report_success(second)
            now[0] = 161.0
            third = pool.acquire()

        self.assertEqual(first.position, 0)
        self.assertEqual(third.position, 0)

    def test_432_is_persistently_exhausted(self) -> None:
        keys = fake_keys(2)
        with TemporaryDirectory() as directory:
            state_file = Path(directory) / "pool.json"
            pool = TavilyKeyPool(keys, quota_per_key=10, state_file=state_file)
            first = pool.acquire()
            pool.report_failure(first, http_status=432, event="http_432")
            restarted = TavilyKeyPool(keys, quota_per_key=10, state_file=state_file)
            positions = [restarted.acquire().position for _ in range(3)]
            state_text = state_file.read_text(encoding="utf-8")

        self.assertEqual(first.position, 0)
        self.assertEqual(positions, [1, 1, 1])
        self.assertFalse(any(key in state_text for key in keys))

    def test_concurrent_instances_never_oversubscribe(self) -> None:
        keys = fake_keys(3)
        with TemporaryDirectory() as directory:
            state_file = Path(directory) / "pool.json"
            pools = [
                TavilyKeyPool(keys, quota_per_key=7, state_file=state_file)
                for _ in range(2)
            ]

            def reserve(index: int) -> int | None:
                try:
                    return pools[index % 2].acquire().position
                except TavilyKeyPoolUnavailable:
                    return None

            with ThreadPoolExecutor(max_workers=16) as executor:
                positions = list(executor.map(reserve, range(64)))
            successful = [position for position in positions if position is not None]
            summary = pools[0].summary()

        self.assertEqual(len(successful), 21)
        self.assertEqual({position: successful.count(position) for position in range(3)}, {0: 7, 1: 7, 2: 7})
        self.assertEqual(summary["remaining"], 0)

    def test_transport_retry_consumes_an_additional_unit(self) -> None:
        with TemporaryDirectory() as directory:
            pool = TavilyKeyPool(
                fake_keys(1), quota_per_key=3, state_file=Path(directory) / "pool.json"
            )
            lease = pool.acquire()
            pool.reserve_transport_retry(lease)
            pool.report_success(lease)
            summary = pool.summary()

        self.assertEqual(summary["used"], 2)
        self.assertEqual(summary["remaining"], 1)

    def test_corrupt_primary_recovers_from_current_backup_and_both_bad_fail_closed(self) -> None:
        with TemporaryDirectory() as directory:
            state_file = Path(directory) / "pool.json"
            pool = TavilyKeyPool(
                fake_keys(1), quota_per_key=3, state_file=state_file
            )
            pool.acquire()
            expected = pool.summary()["used"]
            state_file.write_text("not json", encoding="utf-8")
            recovered = pool.summary()
            self.assertEqual(recovered["used"], expected)
            state_file.write_text("not json", encoding="utf-8")
            pool.backup_file.write_text("also not json", encoding="utf-8")
            with self.assertRaises(TavilyKeyPoolStateError):
                pool.summary()

    def test_new_api_keys_field_is_authoritative_and_legacy_aliases_work(self) -> None:
        self.assertEqual(
            configured_tavily_keys(
                {"api_keys": ["new-a", "new-b", "new-a"], "api_key": "old"}
            ),
            ["new-a", "new-b"],
        )
        self.assertEqual(
            configured_tavily_keys(
                {"api_key": ["old-a", "old-b"], "api_key2": "old-b"}
            ),
            ["old-a", "old-b"],
        )

    def test_state_schema_contains_only_fingerprints_and_counters(self) -> None:
        keys = fake_keys(2)
        with TemporaryDirectory() as directory:
            state_file = Path(directory) / "pool.json"
            pool = TavilyKeyPool(keys, state_file=state_file)
            pool.acquire()
            payload = json.loads(state_file.read_text(encoding="utf-8"))
            serialized = json.dumps(payload)

        self.assertEqual(len(payload["keys"]), 2)
        self.assertTrue(all(len(fingerprint) == 64 for fingerprint in payload["keys"]))
        self.assertFalse(any(key in serialized for key in keys))


if __name__ == "__main__":
    import unittest

    unittest.main()
