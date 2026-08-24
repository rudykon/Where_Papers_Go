#!/usr/bin/env python3
"""Normalize a Tavily key pool without disclosing any key material.

This one-time migration understands the accidental configuration shape where
numbered, unquoted ``tvly-...`` lines were pasted next to an otherwise valid
JSON document.  When such lines exist they are the *only* keys imported; legacy
single-key aliases are treated as exhausted and removed.  An already-valid
``search.api_keys`` configuration is also accepted, making repeated runs safe.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any, Sequence


DEFAULT_EXPECTED_COUNT = 20
DEFAULT_QUOTA_PER_KEY = 1_000
DEFAULT_POOL_STATE_FILE = "data/.tavily_key_pool_state.json"
DEFAULT_MAX_KEY_ATTEMPTS = 3
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 3_600
DEFAULT_TRANSIENT_COOLDOWN_SECONDS = 60

# The value capture is deliberately broad so a malformed numbered value is
# reported as a format error rather than leaking through a JSON parser error.
NUMBERED_VALUE_RE = re.compile(
    r"^\s*(?P<number>\d{1,3})\s*(?:[.)、:：-])\s*"
    r"(?:(?:卡密|key|api[ _-]?key)\s*[:：]\s*)?"
    r"(?P<value>\S+)\s*$",
    re.IGNORECASE,
)
TAVILY_KEY_RE = re.compile(r"^tvly-[A-Za-z0-9][A-Za-z0-9_-]{7,}$")
LEGACY_KEY_ALIASES = (
    "api_key",
    "api_key2",
    "api_key_2",
    "key",
    "backup_api_key",
    "fallback_api_key",
)


class ConfigMigrationError(ValueError):
    """A sanitized migration failure that never contains a credential."""


def _safe_json_loads(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        # Do not include ``exc.doc`` or the source line: either can contain a
        # credential.  Location alone is sufficient to repair unrelated JSON.
        raise ConfigMigrationError(
            f"configuration is not valid JSON (line {exc.lineno}, column {exc.colno})"
        ) from None
    if not isinstance(payload, dict):
        raise ConfigMigrationError("configuration root must be a JSON object")
    return payload


def _extract_numbered_values(text: str) -> tuple[str, list[tuple[int, str]]]:
    """Return JSON text with numbered raw values removed and their values."""

    kept_lines: list[str] = []
    numbered: list[tuple[int, str]] = []
    for line in text.splitlines(keepends=True):
        match = NUMBERED_VALUE_RE.fullmatch(line.rstrip("\r\n"))
        if match is None:
            kept_lines.append(line)
            continue
        value = match.group("value").rstrip(",")
        numbered.append((int(match.group("number")), value))

    sanitized = "".join(kept_lines)
    # A comma immediately before a closing delimiter is the common residue
    # when the raw list was pasted after the last property in ``search``.
    sanitized = re.sub(r",(?=\s*[}\]])", "", sanitized)
    return sanitized, numbered


def _validate_keys(
    numbered_or_keys: Sequence[tuple[int, str]], *, expected_count: int
) -> list[str]:
    if expected_count < 1:
        raise ConfigMigrationError("expected count must be a positive integer")
    if len(numbered_or_keys) != expected_count:
        raise ConfigMigrationError(
            f"found {len(numbered_or_keys)} Tavily keys; expected {expected_count}"
        )

    keys: list[str] = []
    seen: set[str] = set()
    for position, (_number, key) in enumerate(numbered_or_keys, start=1):
        if TAVILY_KEY_RE.fullmatch(key) is None:
            raise ConfigMigrationError(
                f"Tavily key at position {position} does not match the required tvly format"
            )
        if key in seen:
            raise ConfigMigrationError(
                f"duplicate Tavily key at position {position}; all keys must be unique"
            )
        seen.add(key)
        keys.append(key)
    return keys


def normalize_config_text(
    text: str,
    *,
    expected_count: int = DEFAULT_EXPECTED_COUNT,
    merge_existing: bool = False,
    expected_total_count: int | None = None,
    proxy_mode: str = "keep",
) -> dict[str, Any]:
    """Normalize malformed numbered keys or a legal ``api_keys`` pool."""

    json_text, numbered = _extract_numbered_values(text)
    payload = _safe_json_loads(json_text)
    search = payload.get("search")
    if not isinstance(search, dict):
        raise ConfigMigrationError("configuration must contain a search object")

    if numbered:
        new_keys = _validate_keys(numbered, expected_count=expected_count)
        actual_numbers = [number for number, _key in numbered]
        next_number = 1
        for number in actual_numbers:
            if number == next_number:
                next_number += 1
                continue
            # Pasted exports may be split into several numbered blocks, such
            # as 1--10 followed by another 1--10 block.
            if number == 1 and next_number > 1:
                next_number = 2
                continue
            raise ConfigMigrationError(
                "numbered Tavily keys must be ordered consecutively in groups starting at 1"
            )
        if merge_existing:
            configured = search.get("api_keys")
            if not isinstance(configured, list) or not configured:
                raise ConfigMigrationError(
                    "merge requested but search.api_keys is not a non-empty array"
                )
            if any(not isinstance(value, str) for value in configured):
                raise ConfigMigrationError("every existing search.api_keys entry must be a string")
            existing_keys = _validate_keys(
                [
                    (position, value.strip())
                    for position, value in enumerate(configured, start=1)
                ],
                expected_count=len(configured),
            )
            overlap_count = len(set(existing_keys) & set(new_keys))
            if overlap_count:
                raise ConfigMigrationError(
                    f"new Tavily batch overlaps {overlap_count} existing key(s)"
                )
            keys = existing_keys + new_keys
        else:
            keys = new_keys
    else:
        configured = search.get("api_keys")
        if not isinstance(configured, list):
            raise ConfigMigrationError(
                "no numbered Tavily keys found and search.api_keys is not an array"
            )
        if any(not isinstance(value, str) for value in configured):
            raise ConfigMigrationError("every search.api_keys entry must be a string")
        required_count = expected_total_count or expected_count
        keys = _validate_keys(
            [(position, value.strip()) for position, value in enumerate(configured, start=1)],
            expected_count=required_count,
        )

    if expected_total_count is not None and len(keys) != expected_total_count:
        raise ConfigMigrationError(
            f"normalized pool contains {len(keys)} keys; expected {expected_total_count}"
        )

    for alias in LEGACY_KEY_ALIASES:
        search.pop(alias, None)
    search["api_keys"] = keys
    search["quota_per_key"] = DEFAULT_QUOTA_PER_KEY
    search["key_pool_state_file"] = DEFAULT_POOL_STATE_FILE
    search["max_key_attempts"] = DEFAULT_MAX_KEY_ATTEMPTS
    search["rate_limit_cooldown_seconds"] = DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
    search["transient_cooldown_seconds"] = DEFAULT_TRANSIENT_COOLDOWN_SECONDS
    search["retry_empty_results"] = False
    if proxy_mode == "direct":
        search["proxy"] = "direct"
    elif proxy_mode == "env":
        search.pop("proxy", None)
    elif proxy_mode != "keep":
        raise ConfigMigrationError("proxy mode must be keep, direct, or env")
    return payload


def _atomic_write_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            os.chmod(handle.name, stat.S_IRUSR | stat.S_IWUSR)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        # Enforce private permissions even if the destination existed as 0664.
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def normalize_file(
    path: Path,
    *,
    expected_count: int = DEFAULT_EXPECTED_COUNT,
    merge_existing: bool = False,
    expected_total_count: int | None = None,
    proxy_mode: str = "keep",
) -> int:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigMigrationError(
            f"unable to read configuration ({type(exc).__name__})"
        ) from None
    payload = normalize_config_text(
        text,
        expected_count=expected_count,
        merge_existing=merge_existing,
        expected_total_count=expected_total_count,
        proxy_mode=proxy_mode,
    )
    try:
        _atomic_write_private_json(path, payload)
    except OSError as exc:
        raise ConfigMigrationError(
            f"unable to atomically write configuration ({type(exc).__name__})"
        ) from None
    return len(payload["search"]["api_keys"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="path to the API JSON configuration")
    parser.add_argument(
        "--expected-count",
        type=int,
        default=DEFAULT_EXPECTED_COUNT,
        help=f"required number of unique keys (default: {DEFAULT_EXPECTED_COUNT})",
    )
    parser.add_argument(
        "--merge-existing",
        action="store_true",
        help="append the numbered batch after the existing search.api_keys pool",
    )
    parser.add_argument(
        "--expected-total-count",
        type=int,
        default=None,
        help="optional required size of the normalized combined pool",
    )
    parser.add_argument(
        "--proxy",
        choices=("keep", "direct", "env"),
        default="keep",
        help="preserve proxy config, force direct HTTPS, or use environment proxy",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        count = normalize_file(
            args.config,
            expected_count=args.expected_count,
            merge_existing=args.merge_existing,
            expected_total_count=args.expected_total_count,
            proxy_mode=args.proxy,
        )
    except ConfigMigrationError as exc:
        print(f"Tavily key migration failed: {exc}", file=sys.stderr)
        return 2
    print(f"Normalized {count} Tavily keys; configuration permissions set to 0600.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
