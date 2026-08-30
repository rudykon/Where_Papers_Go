from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from where_paper_go.tavily_pool import (
    TAVILY_STATE_FILE_ENV,
    TavilyKeyPool,
    TavilyKeyPoolConfigError,
    TavilyKeyPoolStateError,
    TavilyKeyPoolUnavailable,
    configured_tavily_keys,
)


def fake_keys(count: int) -> list[str]:
    return [f"tvly-fictitious-pool-key-{index:02d}" for index in range(count)]


class TavilyKeyPoolTests(TestCase):
    def test_environment_state_file_override_is_authoritative(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            configured = root / "configured.json"
            overridden = root / "shared" / "pool.json"
            with patch.dict(
                os.environ,
                {TAVILY_STATE_FILE_ENV: str(overridden)},
                clear=False,
            ):
                pool = TavilyKeyPool.from_config(
                    {
                        "api_keys": fake_keys(1),
                        "key_pool_state_file": str(configured),
                    }
                )
                pool.summary()

            self.assertEqual(pool.state_file, overridden)
            self.assertTrue(overridden.is_file())
            self.assertFalse(configured.exists())

    def test_environment_state_file_override_must_be_absolute(self) -> None:
        with (
            patch.dict(
                os.environ,
                {TAVILY_STATE_FILE_ENV: "relative/pool.json"},
                clear=False,
            ),
            self.assertRaisesRegex(TavilyKeyPoolConfigError, TAVILY_STATE_FILE_ENV),
        ):
            TavilyKeyPool.from_config({"api_keys": fake_keys(1)})

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

    def test_state_revision_is_monotonic_and_both_copies_are_identical(self) -> None:
        with TemporaryDirectory() as directory:
            state_file = Path(directory) / "pool.json"
            pool = TavilyKeyPool(fake_keys(1), quota_per_key=3, state_file=state_file)

            pool.summary()
            first = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(first["state_revision"], 1)
            self.assertEqual(state_file.read_bytes(), pool.backup_file.read_bytes())

            lease = pool.acquire()
            second = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(second["state_revision"], 2)
            self.assertEqual(state_file.read_bytes(), pool.backup_file.read_bytes())

            pool.report_success(lease)
            third = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(third["state_revision"], 3)
            self.assertEqual(state_file.read_bytes(), pool.backup_file.read_bytes())

    def test_highest_valid_revision_wins_and_equal_revision_conflict_fails(self) -> None:
        with TemporaryDirectory() as directory:
            state_file = Path(directory) / "pool.json"
            pool = TavilyKeyPool(fake_keys(1), quota_per_key=3, state_file=state_file)
            pool.summary()
            primary = json.loads(state_file.read_text(encoding="utf-8"))
            backup = json.loads(pool.backup_file.read_text(encoding="utf-8"))
            fingerprint = next(iter(backup["keys"]))
            backup["state_revision"] = 2
            backup["keys"][fingerprint]["used"] = 1
            pool.backup_file.write_text(json.dumps(backup), encoding="utf-8")

            before_primary = state_file.read_bytes()
            before_backup = pool.backup_file.read_bytes()
            snapshot = pool.audit_snapshot()
            self.assertEqual(snapshot["state_revision"], 2)
            self.assertEqual(snapshot["used"], 1)
            self.assertEqual(state_file.read_bytes(), before_primary)
            self.assertEqual(pool.backup_file.read_bytes(), before_backup)

            primary["state_revision"] = 2
            primary["keys"][fingerprint]["used"] = 0
            state_file.write_text(json.dumps(primary), encoding="utf-8")
            with self.assertRaisesRegex(TavilyKeyPoolStateError, "conflict"):
                pool.audit_snapshot()

    def test_legacy_state_without_revision_is_revision_zero(self) -> None:
        with TemporaryDirectory() as directory:
            state_file = Path(directory) / "pool.json"
            pool = TavilyKeyPool(fake_keys(1), quota_per_key=2, state_file=state_file)
            pool.summary()
            legacy = json.loads(state_file.read_text(encoding="utf-8"))
            legacy.pop("state_revision")
            serialized = json.dumps(legacy)
            state_file.write_text(serialized, encoding="utf-8")
            pool.backup_file.write_text(serialized, encoding="utf-8")

            self.assertEqual(pool.audit_snapshot()["state_revision"], 0)
            pool.acquire()
            upgraded = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(upgraded["state_revision"], 1)
            self.assertEqual(
                sum(record["used"] for record in upgraded["keys"].values()), 1
            )

    def test_quota_decrease_preserves_cumulative_usage_without_self_corruption(self) -> None:
        keys = fake_keys(1)
        with TemporaryDirectory() as directory:
            state_file = Path(directory) / "pool.json"
            original = TavilyKeyPool(keys, quota_per_key=2, state_file=state_file)
            original.acquire()
            original.acquire()

            reduced = TavilyKeyPool(keys, quota_per_key=1, state_file=state_file)
            summary = reduced.summary()
            migrated = json.loads(state_file.read_text(encoding="utf-8"))
            record = next(iter(migrated["keys"].values()))
            self.assertEqual(summary["used"], 1)
            self.assertEqual(summary["remaining"], 0)
            self.assertEqual(record["used"], 2)
            self.assertEqual(record["limit"], 1)
            self.assertEqual(record["status"], "exhausted")

            restarted = TavilyKeyPool(keys, quota_per_key=1, state_file=state_file)
            self.assertEqual(restarted.audit_snapshot()["remaining"], 0)
            restarted.summary()
            with self.assertRaises(TavilyKeyPoolUnavailable):
                restarted.acquire()
            after_restart = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(
                next(iter(after_restart["keys"].values()))["used"], 2
            )

    def test_revisioned_state_can_add_new_key_without_resetting_old_key(self) -> None:
        old_key, new_key = fake_keys(2)
        with TemporaryDirectory() as directory:
            state_file = Path(directory) / "pool.json"
            original = TavilyKeyPool(
                [old_key], quota_per_key=3, state_file=state_file
            )
            original.acquire()
            original.acquire()
            before = json.loads(state_file.read_text(encoding="utf-8"))
            before_revision = before["state_revision"]

            expanded = TavilyKeyPool(
                [old_key, new_key], quota_per_key=3, state_file=state_file
            )
            expanded.summary()
            migrated = json.loads(state_file.read_text(encoding="utf-8"))
            old_fingerprint = hashlib.sha256(old_key.encode()).hexdigest()
            new_fingerprint = hashlib.sha256(new_key.encode()).hexdigest()
            self.assertGreater(migrated["state_revision"], before_revision)
            self.assertEqual(migrated["keys"][old_fingerprint]["used"], 2)
            self.assertEqual(migrated["keys"][new_fingerprint]["used"], 0)
            self.assertEqual(migrated["order"], [old_fingerprint, new_fingerprint])

            restarted = TavilyKeyPool(
                [old_key, new_key], quota_per_key=3, state_file=state_file
            )
            self.assertEqual(restarted.audit_snapshot()["used"], 2)

    def test_crash_after_backup_publish_does_not_restore_old_quota(self) -> None:
        with TemporaryDirectory() as directory:
            state_file = Path(directory) / "pool.json"
            keys = fake_keys(1)
            pool = TavilyKeyPool(keys, quota_per_key=1, state_file=state_file)
            pool.summary()
            original_publish = pool._publish_state_file

            def fail_before_primary(path: Path, payload: bytes) -> None:
                if path == pool.state_file:
                    raise OSError("injected crash before primary publish")
                original_publish(path, payload)

            with patch.object(pool, "_publish_state_file", side_effect=fail_before_primary):
                with self.assertRaisesRegex(OSError, "injected crash"):
                    pool.acquire()

            primary = json.loads(state_file.read_text(encoding="utf-8"))
            backup = json.loads(pool.backup_file.read_text(encoding="utf-8"))
            self.assertEqual(primary["state_revision"], 1)
            self.assertEqual(backup["state_revision"], 2)
            self.assertEqual(
                sum(record["used"] for record in backup["keys"].values()), 1
            )
            restarted = TavilyKeyPool(keys, quota_per_key=1, state_file=state_file)
            self.assertEqual(restarted.audit_snapshot()["used"], 1)
            with self.assertRaises(TavilyKeyPoolUnavailable):
                restarted.acquire()

    def test_each_state_copy_is_file_and_directory_fsynced(self) -> None:
        with TemporaryDirectory() as directory:
            pool = TavilyKeyPool(
                fake_keys(1), state_file=Path(directory) / "pool.json"
            )
            pool.summary()
            with (
                patch.object(
                    pool, "_fsync_directory", wraps=pool._fsync_directory
                ) as directory_fsync,
                patch(
                    "where_paper_go.tavily_pool.os.fsync", wraps=os.fsync
                ) as fsync,
                patch(
                    "where_paper_go.tavily_pool.os.replace", wraps=os.replace
                ) as replace,
            ):
                pool.acquire()

            self.assertEqual(replace.call_count, 2)
            self.assertEqual(
                [call.args[1] for call in replace.call_args_list],
                [pool.backup_file, pool.state_file],
            )
            self.assertEqual(directory_fsync.call_count, 2)
            self.assertGreaterEqual(fsync.call_count, 4)

    def test_parseable_current_or_newer_record_corruption_fails_closed(self) -> None:
        corruptions = {
            "used": lambda record: record.__setitem__("used", "zero"),
            "successes": lambda record: record.__setitem__("successes", -1),
            "failures": lambda record: record.__setitem__("failures", None),
            "empty_results": lambda record: record.__setitem__(
                "empty_results", True
            ),
            "position": lambda record: record.__setitem__("position", 9),
            "limit": lambda record: record.__setitem__("limit", 0),
            "status": lambda record: record.__setitem__("status", "unknown"),
            "cooldown_until": lambda record: record.__setitem__(
                "cooldown_until", "later"
            ),
        }
        for name, corrupt in corruptions.items():
            with self.subTest(field=name), TemporaryDirectory() as directory:
                state_file = Path(directory) / "pool.json"
                pool = TavilyKeyPool(
                    fake_keys(1), quota_per_key=3, state_file=state_file
                )
                pool.summary()
                newer = json.loads(pool.backup_file.read_text(encoding="utf-8"))
                newer["state_revision"] += 1
                record = next(iter(newer["keys"].values()))
                corrupt(record)
                pool.backup_file.write_text(json.dumps(newer), encoding="utf-8")
                primary_before = state_file.read_bytes()
                backup_before = pool.backup_file.read_bytes()

                with self.assertRaisesRegex(
                    TavilyKeyPoolStateError, "corrupt current-or-newer"
                ):
                    pool.audit_snapshot()
                with self.assertRaises(TavilyKeyPoolStateError):
                    pool.summary()
                self.assertEqual(state_file.read_bytes(), primary_before)
                self.assertEqual(pool.backup_file.read_bytes(), backup_before)

    def test_parseable_newer_state_missing_configured_record_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            state_file = Path(directory) / "pool.json"
            pool = TavilyKeyPool(fake_keys(1), state_file=state_file)
            pool.summary()
            newer = json.loads(pool.backup_file.read_text(encoding="utf-8"))
            newer["state_revision"] += 1
            newer["keys"].clear()
            pool.backup_file.write_text(json.dumps(newer), encoding="utf-8")

            with self.assertRaisesRegex(
                TavilyKeyPoolStateError, "corrupt current-or-newer"
            ):
                pool.audit_snapshot()
            with self.assertRaises(TavilyKeyPoolStateError):
                pool.acquire()

    def test_audit_snapshot_is_read_only_and_contains_no_key_fingerprints(self) -> None:
        keys = fake_keys(2)
        with TemporaryDirectory() as directory:
            state_file = Path(directory) / "pool.json"
            pool = TavilyKeyPool(keys, quota_per_key=4, state_file=state_file)
            pool.acquire()
            primary_before = state_file.read_bytes()
            backup_before = pool.backup_file.read_bytes()
            primary_mtime = state_file.stat().st_mtime_ns
            backup_mtime = pool.backup_file.stat().st_mtime_ns

            snapshot = pool.audit_snapshot()
            serialized = json.dumps(snapshot, sort_keys=True)

            self.assertEqual(state_file.read_bytes(), primary_before)
            self.assertEqual(pool.backup_file.read_bytes(), backup_before)
            self.assertEqual(state_file.stat().st_mtime_ns, primary_mtime)
            self.assertEqual(pool.backup_file.stat().st_mtime_ns, backup_mtime)
            self.assertEqual(snapshot["used"], 1)
            self.assertTrue(snapshot["configuration_current"])
            self.assertNotIn("keys", snapshot)
            self.assertNotIn("order", snapshot)
            self.assertNotIn("fingerprint", serialized)
            self.assertFalse(any(key in serialized for key in keys))
            self.assertFalse(
                any(hashlib.sha256(key.encode()).hexdigest() in serialized for key in keys)
            )
            for name, expected in (
                ("primary", primary_before),
                ("backup", backup_before),
            ):
                metadata = snapshot["copies"][name]
                self.assertEqual(metadata["sha256"], hashlib.sha256(expected).hexdigest())
                self.assertEqual(metadata["bytes"], len(expected))
                self.assertEqual(metadata["mode"], "0600")
            reordered = TavilyKeyPool(
                list(reversed(keys)), quota_per_key=4, state_file=state_file
            )
            self.assertEqual(
                reordered.audit_snapshot()["configured_keyset_sha256"],
                snapshot["configured_keyset_sha256"],
            )

    def test_audit_reports_configuration_drift_without_rewriting_state(self) -> None:
        with TemporaryDirectory() as directory:
            state_file = Path(directory) / "pool.json"
            original = TavilyKeyPool(
                fake_keys(1), quota_per_key=3, state_file=state_file
            )
            original.summary()
            primary_before = state_file.read_bytes()
            backup_before = original.backup_file.read_bytes()

            changed = TavilyKeyPool(
                fake_keys(2), quota_per_key=4, state_file=state_file
            )
            snapshot = changed.audit_snapshot()

            self.assertFalse(snapshot["configuration_current"])
            self.assertEqual(state_file.read_bytes(), primary_before)
            self.assertEqual(original.backup_file.read_bytes(), backup_before)

    def test_audit_requires_existing_private_regular_nofollow_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state_file = root / "pool.json"
            pool = TavilyKeyPool(fake_keys(1), state_file=state_file)
            with self.assertRaises(TavilyKeyPoolStateError):
                pool.audit_snapshot()
            self.assertFalse(state_file.exists())
            self.assertFalse(pool.lock_file.exists())

            pool.lock_file.write_text("", encoding="utf-8")
            os.chmod(pool.lock_file, 0o600)
            with self.assertRaisesRegex(TavilyKeyPoolStateError, "missing"):
                pool.audit_snapshot()
            with self.assertRaisesRegex(TavilyKeyPoolStateError, "missing"):
                pool.summary()
            self.assertFalse(state_file.exists())
            self.assertFalse(pool.backup_file.exists())

            pool.lock_file.unlink()
            pool.summary()
            os.chmod(pool.backup_file, 0o640)
            with self.assertRaisesRegex(TavilyKeyPoolStateError, "mode"):
                pool.audit_snapshot()
            os.chmod(pool.backup_file, 0o600)

            backup_payload = pool.backup_file.read_bytes()
            pool.backup_file.unlink()
            target = root / "backup-target.json"
            target.write_bytes(backup_payload)
            os.chmod(target, 0o600)
            pool.backup_file.symlink_to(target)
            with self.assertRaises(TavilyKeyPoolStateError):
                pool.audit_snapshot()

        with TemporaryDirectory() as directory:
            root = Path(directory)
            pool = TavilyKeyPool(fake_keys(1), state_file=root / "pool.json")
            pool.summary()
            pool.lock_file.unlink()
            lock_target = root / "lock-target"
            lock_target.write_text("", encoding="utf-8")
            os.chmod(lock_target, 0o600)
            pool.lock_file.symlink_to(lock_target)
            with self.assertRaises(TavilyKeyPoolStateError):
                pool.audit_snapshot()


if __name__ == "__main__":
    import unittest

    unittest.main()
