from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from scripts.normalize_tavily_keys import (
    ConfigMigrationError,
    main,
    normalize_config_text,
    normalize_file,
)


def fake_keys(count: int = 20) -> list[str]:
    return [f"tvly-dev-fictitious-key-{number:02d}" for number in range(1, count + 1)]


def malformed_config(keys: list[str]) -> str:
    base = {
        "llm": {"provider": "example"},
        "search": {
            "provider": "tavily",
            "api_key": "tvly-old-exhausted-primary",
            "api_key2": "tvly-old-exhausted-backup",
            "endpoint": "https://api.tavily.com/search",
        },
    }
    raw = json.dumps(base, indent=2)
    numbered = "\n".join(f"{number}. {key}" for number, key in enumerate(keys, start=1))
    return raw + "\n" + numbered + "\n"


def malformed_merged_config(existing: list[str], new: list[str]) -> str:
    base = {
        "search": {
            "provider": "tavily",
            "api_keys": existing,
            "quota_per_key": 1000,
        }
    }
    raw = json.dumps(base, indent=2)
    numbered = "\n".join(
        f"{number}. 卡密：{key}" for number, key in enumerate(new, start=1)
    )
    return raw + "\n" + numbered + "\n"


class NormalizeTavilyKeysTests(TestCase):
    def test_imports_only_numbered_keys_and_sets_pool_defaults(self) -> None:
        keys = fake_keys()
        payload = normalize_config_text(malformed_config(keys))
        search = payload["search"]

        self.assertEqual(search["api_keys"], keys)
        self.assertNotIn("api_key", search)
        self.assertNotIn("api_key2", search)
        self.assertEqual(search["quota_per_key"], 1000)
        self.assertEqual(
            search["key_pool_state_file"], "data/.tavily_key_pool_state.json"
        )
        self.assertEqual(search["max_key_attempts"], 3)
        self.assertEqual(search["rate_limit_cooldown_seconds"], 3600)
        self.assertEqual(search["transient_cooldown_seconds"], 60)
        self.assertIs(search["retry_empty_results"], False)
        self.assertEqual(search["endpoint"], "https://api.tavily.com/search")

    def test_legal_api_keys_configuration_is_idempotent(self) -> None:
        keys = fake_keys()
        initial = {
            "search": {
                "provider": "tavily",
                "api_keys": keys,
                "api_key": "tvly-old-exhausted-primary",
            }
        }
        once = normalize_config_text(json.dumps(initial))
        twice = normalize_config_text(json.dumps(once))
        self.assertEqual(once, twice)
        self.assertEqual(twice["search"]["api_keys"], keys)
        self.assertNotIn("api_key", twice["search"])

    def test_rejects_wrong_count_without_disclosing_keys(self) -> None:
        keys = fake_keys(19)
        with self.assertRaises(ConfigMigrationError) as caught:
            normalize_config_text(malformed_config(keys))
        message = str(caught.exception)
        self.assertIn("found 19", message)
        self.assertFalse(any(key in message for key in keys))

    def test_rejects_duplicates_without_disclosing_key(self) -> None:
        keys = fake_keys()
        keys[-1] = keys[0]
        with self.assertRaises(ConfigMigrationError) as caught:
            normalize_config_text(malformed_config(keys))
        message = str(caught.exception)
        self.assertIn("duplicate", message)
        self.assertFalse(any(key in message for key in keys))

    def test_rejects_invalid_format_without_disclosing_value(self) -> None:
        keys = fake_keys()
        invalid_value = "not-a-tavily-credential"
        keys[6] = invalid_value
        with self.assertRaises(ConfigMigrationError) as caught:
            normalize_config_text(malformed_config(keys))
        message = str(caught.exception)
        self.assertIn("position 7", message)
        self.assertIn("tvly format", message)
        self.assertNotIn(invalid_value, message)

    def test_requires_consecutive_numbering(self) -> None:
        text = malformed_config(fake_keys()).replace(
            "7. tvly-dev-fictitious-key-07", "21. tvly-dev-fictitious-key-07"
        )
        with self.assertRaises(ConfigMigrationError) as caught:
            normalize_config_text(text)
        self.assertIn("ordered consecutively", str(caught.exception))

    def test_accepts_numbered_lines_with_chinese_key_label(self) -> None:
        keys = fake_keys()
        text = malformed_config(keys)
        for number, key in enumerate(keys, start=1):
            text = text.replace(f"{number}. {key}", f"{number}. 卡密：{key}")
        payload = normalize_config_text(text)
        self.assertEqual(payload["search"]["api_keys"], keys)

    def test_direct_proxy_mode_is_explicit_and_idempotent(self) -> None:
        keys = fake_keys()
        once = normalize_config_text(malformed_config(keys), proxy_mode="direct")
        twice = normalize_config_text(json.dumps(once), proxy_mode="direct")
        self.assertEqual(once, twice)
        self.assertEqual(twice["search"]["proxy"], "direct")

    def test_merges_a_new_numbered_batch_after_existing_keys(self) -> None:
        existing = fake_keys()
        new = [f"tvly-second-fictitious-key-{number:02d}" for number in range(1, 21)]
        payload = normalize_config_text(
            malformed_merged_config(existing, new),
            merge_existing=True,
            expected_total_count=40,
        )
        self.assertEqual(payload["search"]["api_keys"], existing + new)
        repeated = normalize_config_text(
            json.dumps(payload),
            merge_existing=True,
            expected_total_count=40,
        )
        self.assertEqual(repeated, payload)

    def test_merge_rejects_overlap_without_disclosing_key(self) -> None:
        existing = fake_keys()
        new = [f"tvly-second-fictitious-key-{number:02d}" for number in range(1, 21)]
        new[-1] = existing[0]
        with self.assertRaises(ConfigMigrationError) as caught:
            normalize_config_text(
                malformed_merged_config(existing, new),
                merge_existing=True,
                expected_total_count=40,
            )
        message = str(caught.exception)
        self.assertIn("overlaps 1", message)
        self.assertFalse(any(key in message for key in existing + new))

    def test_merge_accepts_two_numbered_blocks_of_ten(self) -> None:
        existing = fake_keys()
        new = [f"tvly-second-fictitious-key-{number:02d}" for number in range(1, 21)]
        text = malformed_merged_config(existing, new)
        for number, key in enumerate(new[10:], start=11):
            text = text.replace(f"{number}. 卡密：{key}", f"{number - 10}. 卡密：{key}")
        payload = normalize_config_text(
            text,
            merge_existing=True,
            expected_total_count=40,
        )
        self.assertEqual(payload["search"]["api_keys"], existing + new)

    def test_atomic_file_rewrite_sets_mode_0600_and_cli_redacts_keys(self) -> None:
        keys = fake_keys()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "api.json"
            path.write_text(malformed_config(keys), encoding="utf-8")
            os.chmod(path, 0o664)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = main([str(path)])

            self.assertEqual(result, 0)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["search"]["api_keys"], keys)
            output = stdout.getvalue() + stderr.getvalue()
            self.assertFalse(any(key in output for key in keys))

    def test_atomic_replace_failure_keeps_original_and_redacts_error(self) -> None:
        keys = fake_keys()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "api.json"
            original = malformed_config(keys)
            path.write_text(original, encoding="utf-8")
            with patch(
                "scripts.normalize_tavily_keys.os.replace",
                side_effect=OSError("simulated failure"),
            ), self.assertRaises(ConfigMigrationError) as caught:
                normalize_file(path)

            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertFalse(any(key in str(caught.exception) for key in keys))
            self.assertEqual(list(path.parent.glob(".api.json.*.tmp")), [])


if __name__ == "__main__":
    import unittest

    unittest.main()
