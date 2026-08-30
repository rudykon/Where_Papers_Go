from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import time
from unittest import TestCase
import urllib.error
import urllib.parse
import urllib.request


class HostSystemdIntegrationTests(TestCase):
    """Opt-in destructive-to-process, recoverable test of the installed user unit."""

    unit = os.environ.get("WPG_SYSTEMD_TEST_UNIT", "where-papers-go.service")
    health_url = os.environ.get(
        "WPG_SYSTEMD_TEST_HEALTH_URL", "http://127.0.0.1:8001/api/health"
    )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @classmethod
    def _systemctl_value(cls, property_name: str) -> str:
        return subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                cls.unit,
                "--property",
                property_name,
                "--value",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()

    @classmethod
    def _ready_health(cls) -> dict[str, object] | None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(cls.health_url, timeout=2) as response:
                payload = json.load(response)
        except (OSError, ValueError, urllib.error.URLError):
            return None
        if not isinstance(payload, dict) or payload.get("ready") is not True:
            return None
        return payload

    @classmethod
    def _recover_service(cls) -> None:
        if cls._ready_health() is not None:
            return
        subprocess.run(
            ["systemctl", "--user", "restart", cls.unit],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            if cls._ready_health() is not None:
                return
            time.sleep(1)
        raise AssertionError("installed service did not recover ready health")

    def test_main_process_sigkill_is_automatically_restarted_and_ready(self) -> None:
        if os.environ.get("WPG_RUN_HOST_SYSTEMD_TESTS") != "1":
            self.skipTest("set WPG_RUN_HOST_SYSTEMD_TESTS=1 for the recoverable host test")
        self.addCleanup(self._recover_service)

        required_environment = (
            "WPG_EXPECTED_SYSTEMD_UNIT_SHA256",
            "WPG_EXPECTED_VECTOR_SHA256",
            "WPG_EXPECTED_LIGHTRAG_SHA256",
            "WPG_EXPECTED_HOST",
            "WPG_EXPECTED_PORT",
            "WPG_EXPECTED_DATA_DIR",
            "WPG_EXPECTED_API_CONFIG",
            "WPG_EXPECTED_ALLOWED_CLIENT_CIDRS",
            "WPG_EXPECTED_TRUST_PROXY_HEADERS",
            "WPG_EXPECTED_TRUSTED_PROXY_CIDRS",
            "WPG_EXPECTED_REQUIRE_API_AUTH",
            "WPG_EXPECTED_RUNTIME_GENERATION",
            "WPG_EXPECTED_RUNTIME_MANIFEST",
            "WPG_EXPECTED_RUNTIME_MANIFEST_SHA256",
            "WPG_EXPECTED_TAVILY_STATE_FILE",
            "WPG_EXPECTED_STRICT_GRAPH_READ_ONLY",
            "WPG_EXPECTED_REQUIRE_RUNTIME_SHADOW",
        )
        missing = [name for name in required_environment if not os.environ.get(name)]
        self.assertEqual(missing, [], msg="missing host-test identity bindings")
        fragment = Path(self._systemctl_value("FragmentPath")).resolve()
        expected_fragment = Path(
            os.environ.get(
                "WPG_EXPECTED_SYSTEMD_FRAGMENT",
                str(Path.home() / ".config/systemd/user/where-papers-go.service"),
            )
        ).resolve()
        self.assertEqual(fragment, expected_fragment)
        self.assertEqual(
            self._sha256(fragment), os.environ["WPG_EXPECTED_SYSTEMD_UNIT_SHA256"]
        )
        self.assertEqual(self._systemctl_value("UnitFileState"), "enabled")
        self.assertEqual(self._systemctl_value("NeedDaemonReload"), "no")
        runtime_environment = Path(
            os.environ.get(
                "WPG_SYSTEMD_RUNTIME_ENV",
                str(Path.home() / ".config/where-papers-go/runtime.env"),
            )
        )
        runtime_info = runtime_environment.lstat()
        self.assertTrue(stat.S_ISREG(runtime_info.st_mode))
        self.assertFalse(runtime_environment.is_symlink())
        self.assertEqual(stat.S_IMODE(runtime_info.st_mode), 0o600)
        self.assertEqual(runtime_info.st_uid, os.getuid())
        nonsecret_values = {}
        checked_names = {
            "WPG_HOST",
            "WPG_PORT",
            "WPG_DATA_DIR",
            "WPG_API_CONFIG",
            "WPG_ALLOWED_CLIENT_CIDRS",
            "WPG_TRUST_PROXY_HEADERS",
            "WPG_TRUSTED_PROXY_CIDRS",
            "WPG_REQUIRE_API_AUTH",
            "WPG_RUNTIME_GENERATION",
            "WPG_RUNTIME_MANIFEST",
            "WPG_RUNTIME_MANIFEST_SHA256",
            "WPG_TAVILY_STATE_FILE",
            "WPG_STRICT_GRAPH_READ_ONLY",
            "WPG_REQUIRE_RUNTIME_SHADOW",
        }
        for line in runtime_environment.read_text(encoding="utf-8").splitlines():
            name, separator, value = line.partition("=")
            if separator and name in checked_names:
                nonsecret_values[name] = value
        for name in checked_names:
            expected_name = "WPG_EXPECTED_" + name.removeprefix("WPG_")
            self.assertEqual(nonsecret_values.get(name), os.environ[expected_name])
        health = urllib.parse.urlsplit(self.health_url)
        self.assertEqual(health.hostname, "127.0.0.1")
        self.assertEqual(str(health.port), os.environ["WPG_EXPECTED_PORT"])
        data_dir = Path(nonsecret_values["WPG_DATA_DIR"]).resolve()
        api_config = Path(nonsecret_values["WPG_API_CONFIG"]).resolve()
        self.assertEqual(data_dir, Path(os.environ["WPG_EXPECTED_DATA_DIR"]).resolve())
        self.assertEqual(
            api_config, Path(os.environ["WPG_EXPECTED_API_CONFIG"]).resolve()
        )
        runtime_generation = Path(
            nonsecret_values["WPG_RUNTIME_GENERATION"]
        ).resolve()
        runtime_manifest = Path(nonsecret_values["WPG_RUNTIME_MANIFEST"])
        tavily_state = Path(nonsecret_values["WPG_TAVILY_STATE_FILE"])
        self.assertEqual(
            runtime_generation,
            Path(os.environ["WPG_EXPECTED_RUNTIME_GENERATION"]).resolve(),
        )
        self.assertEqual(runtime_manifest.parent.resolve(), runtime_generation)
        self.assertEqual(
            nonsecret_values["WPG_RUNTIME_MANIFEST_SHA256"],
            os.environ["WPG_EXPECTED_RUNTIME_MANIFEST_SHA256"],
        )
        self.assertEqual(
            tavily_state.resolve(),
            Path(os.environ["WPG_EXPECTED_TAVILY_STATE_FILE"]).resolve(),
        )
        generation_info = runtime_generation.lstat()
        self.assertTrue(stat.S_ISDIR(generation_info.st_mode))
        self.assertFalse(runtime_generation.is_symlink())
        self.assertEqual(stat.S_IMODE(generation_info.st_mode) & 0o077, 0)
        manifest_info = runtime_manifest.lstat()
        self.assertTrue(stat.S_ISREG(manifest_info.st_mode))
        self.assertFalse(runtime_manifest.is_symlink())
        self.assertEqual(stat.S_IMODE(manifest_info.st_mode), 0o400)
        self.assertEqual(
            self._sha256(runtime_manifest),
            os.environ["WPG_EXPECTED_RUNTIME_MANIFEST_SHA256"],
        )
        self.assertFalse(tavily_state.is_relative_to(runtime_generation))
        self.assertFalse(tavily_state.is_relative_to(data_dir))
        tavily_copies = (
            tavily_state,
            tavily_state.with_name(tavily_state.name + ".bak"),
            tavily_state.with_name(tavily_state.name + ".lock"),
        )
        for tavily_copy in tavily_copies:
            copy_info = tavily_copy.lstat()
            self.assertTrue(stat.S_ISREG(copy_info.st_mode))
            self.assertFalse(tavily_copy.is_symlink())
            self.assertEqual(stat.S_IMODE(copy_info.st_mode), 0o600)
        self.assertEqual(
            self._sha256(tavily_copies[0]), self._sha256(tavily_copies[1])
        )
        config_info = api_config.lstat()
        self.assertTrue(stat.S_ISREG(config_info.st_mode))
        self.assertFalse(api_config.is_symlink())
        self.assertEqual(config_info.st_uid, os.getuid())
        self.assertEqual(stat.S_IMODE(config_info.st_mode) & 0o077, 0)
        vector_path = data_dir / "venue_graph_vectors.json.gz"
        lightrag_path = data_dir / "lightrag_storage/venue_import_manifest.json"
        self.assertEqual(
            self._sha256(vector_path), os.environ["WPG_EXPECTED_VECTOR_SHA256"]
        )
        self.assertEqual(
            self._sha256(lightrag_path), os.environ["WPG_EXPECTED_LIGHTRAG_SHA256"]
        )

        self.assertEqual(self._systemctl_value("ActiveState"), "active")
        self.assertEqual(self._systemctl_value("SubState"), "running")
        self.assertEqual(self._systemctl_value("Restart"), "on-failure")
        original_pid = int(self._systemctl_value("MainPID"))
        original_restarts = int(self._systemctl_value("NRestarts"))
        self.assertGreater(original_pid, 1)
        active_environment = {
            name.decode("utf-8"): value.decode("utf-8")
            for entry in Path(f"/proc/{original_pid}/environ").read_bytes().split(b"\0")
            if entry
            for name, separator, value in (entry.partition(b"="),)
            if separator and name.decode("utf-8") in checked_names
        }
        for name in checked_names:
            self.assertEqual(active_environment.get(name), nonsecret_values[name])
        initial_health = self._ready_health()
        self.assertIsNotNone(initial_health)
        assert initial_health is not None
        self.assertTrue(all(initial_health["checks"].values()))

        subprocess.run(
            [
                "systemctl",
                "--user",
                "kill",
                "--kill-who=main",
                "--signal=KILL",
                self.unit,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        deadline = time.monotonic() + 240
        payload: dict[str, object] | None = None
        replacement_pid = 0
        while time.monotonic() < deadline:
            try:
                replacement_pid = int(self._systemctl_value("MainPID") or 0)
            except (OSError, ValueError, subprocess.CalledProcessError):
                replacement_pid = 0
            if replacement_pid not in {0, original_pid}:
                payload = self._ready_health()
                if payload is not None:
                    break
            time.sleep(1)

        self.assertNotIn(replacement_pid, {0, original_pid})
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertTrue(payload["runtime"]["process_ready"])
        self.assertTrue(payload["runtime"]["bindings_current"])
        self.assertTrue(payload["runtime"]["write_isolated"])
        self.assertTrue(payload["runtime"]["tavily_state_shared"])
        self.assertTrue(payload["runtime"]["runtime_manifest"]["ready"])
        self.assertTrue(payload["config"]["search_quota_audit"]["ready"])
        self.assertTrue(all(payload["checks"].values()))
        restarted_environment = {
            name.decode("utf-8"): value.decode("utf-8")
            for entry in Path(f"/proc/{replacement_pid}/environ").read_bytes().split(b"\0")
            if entry
            for name, separator, value in (entry.partition(b"="),)
            if separator and name.decode("utf-8") in checked_names
        }
        for name in checked_names:
            self.assertEqual(restarted_environment.get(name), nonsecret_values[name])
        self.assertEqual(
            self._sha256(fragment), os.environ["WPG_EXPECTED_SYSTEMD_UNIT_SHA256"]
        )
        self.assertEqual(
            self._sha256(vector_path), os.environ["WPG_EXPECTED_VECTOR_SHA256"]
        )
        self.assertEqual(
            self._sha256(lightrag_path), os.environ["WPG_EXPECTED_LIGHTRAG_SHA256"]
        )
        self.assertGreaterEqual(
            int(self._systemctl_value("NRestarts")), original_restarts + 1
        )


if __name__ == "__main__":
    __import__("unittest").main()
