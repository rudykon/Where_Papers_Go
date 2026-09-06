from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from research.data import ResearchDataError, sha256_file
from research.model_assets import (
    ASSET_MANIFEST_NAME,
    load_model_asset_config,
    materialize_model_assets,
)


class ModelAssetAcquisitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = self.root / "assets.json"
        revision = "0123456789abcdef0123456789abcdef01234567"
        self.config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "assets": [
                        {
                            "name": "unit_model",
                            "repo_id": "unit/model",
                            "revision": revision,
                            "source_url": (
                                "https://huggingface.co/unit/model/tree/" + revision
                            ),
                            "include": ["config.json", "model.safetensors"],
                            "required_files": [
                                "config.json",
                                "model.safetensors",
                            ],
                            "estimated_download_bytes": 20,
                        }
                    ],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.hf = self.root / "hf"
        self.hf.write_text(
            f"""#!{sys.executable}
import json
import os
from pathlib import Path
import sys
import time

arguments = sys.argv[1:]
if arguments == ["--version"]:
    print("hf version 1.12.2")
    raise SystemExit(0)
if not arguments or arguments[0] != "download":
    raise SystemExit(2)
if "--dry-run" in arguments:
    if os.environ.get("UNIT_HF_TIMEOUT") == "1":
        time.sleep(1)
    weight_size = "-" if os.environ.get("UNIT_HF_CACHE_WEIGHT") == "1" else "12.0"
    print(json.dumps([
        {{"file": "config.json", "size": "8.0"}},
        {{"file": "model.safetensors", "size": weight_size}},
    ]))
    raise SystemExit(0)
if os.environ.get("UNIT_HF_FAIL_DOWNLOAD") == "1":
    print("deliberate failure hf_abcdefghijklmnopqrstuv", file=sys.stderr)
    raise SystemExit(9)
local_dir = Path(arguments[arguments.index("--local-dir") + 1])
local_dir.mkdir(parents=True, exist_ok=True)
(local_dir / "config.json").write_text("{{}}\\n", encoding="utf-8")
(local_dir / "model.safetensors").write_bytes(b"unit-weights")
print(json.dumps({{"local_dir": str(local_dir)}}))
""",
            encoding="utf-8",
        )
        self.hf.chmod(0o755)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_dry_run_records_cache_cost_and_disk_without_downloading(self) -> None:
        output = self.root / "models"
        audit = materialize_model_assets(
            config_path=self.config,
            output_root=output,
            hf_cli=self.hf,
            generation_command=("python", "-m", "research", "unit-plan"),
        )
        self.assertEqual(audit["status"], "dry_run_complete")
        self.assertEqual(audit["preflight"]["planned_download_bytes"], 20)
        self.assertEqual(audit["preflight"]["cache_coverage_bytes"], 0)
        self.assertFalse(audit["assets"][0]["dry_run"]["summary"]["fallback_estimate_used"])
        self.assertEqual(audit["assets"][0]["dry_run"]["summary"]["file_count"], 2)
        self.assertEqual(audit["cost_and_quota"]["known_provider_api_cost_usd"], 0.0)
        self.assertIn("model-asset-acquisition-v1@", audit["implementation"]["revision"])
        self.assertIn("python", audit["runtime"])
        self.assertTrue(Path(audit["audit_path"]).is_file())
        self.assertEqual(sha256_file(Path(audit["audit_path"])), audit["audit_sha256"])
        specs, _record = load_model_asset_config(self.config)
        self.assertFalse((output / specs[0].directory_name).exists())

    def test_dry_run_understands_hf_cli_cached_file_marker(self) -> None:
        output = self.root / "cached-plan"
        original = os.environ.get("UNIT_HF_CACHE_WEIGHT")
        os.environ["UNIT_HF_CACHE_WEIGHT"] = "1"
        try:
            audit = materialize_model_assets(
                config_path=self.config,
                output_root=output,
                hf_cli=self.hf,
            )
        finally:
            if original is None:
                os.environ.pop("UNIT_HF_CACHE_WEIGHT", None)
            else:
                os.environ["UNIT_HF_CACHE_WEIGHT"] = original
        summary = audit["assets"][0]["dry_run"]["summary"]
        self.assertEqual(summary["cached_file_count"], 1)
        self.assertEqual(summary["planned_download_file_count"], 1)
        self.assertEqual(summary["planned_download_bytes"], 8)

    def test_execute_requires_authorization_and_atomically_publishes(self) -> None:
        output = self.root / "models"
        with self.assertRaisesRegex(ResearchDataError, "authorization"):
            materialize_model_assets(
                config_path=self.config,
                output_root=output,
                hf_cli=self.hf,
                execute=True,
            )
        audit = materialize_model_assets(
            config_path=self.config,
            output_root=output,
            hf_cli=self.hf,
            execute=True,
            authorization_reference="unit-test-explicit-authorization",
            generation_command=("python", "-m", "research", "unit-fetch"),
        )
        self.assertEqual(audit["status"], "complete")
        specs, _record = load_model_asset_config(self.config)
        target = output / specs[0].directory_name
        self.assertTrue((target / "model.safetensors").is_file())
        self.assertTrue((target / ASSET_MANIFEST_NAME).is_file())
        self.assertFalse(list(output.glob(".*.building-*")))

        reused = materialize_model_assets(
            config_path=self.config,
            output_root=output,
            hf_cli=self.hf,
        )
        self.assertEqual(reused["preflight"]["validated_existing_asset_count"], 1)
        self.assertEqual(reused["assets"][0]["status"], "validated_existing")
        self.assertEqual(reused["preflight"]["planned_download_bytes"], 0)

        (target / "model.safetensors").write_bytes(b"corrupted")
        with self.assertRaisesRegex(ResearchDataError, "payload hash mismatch"):
            materialize_model_assets(
                config_path=self.config,
                output_root=output,
                hf_cli=self.hf,
            )
        self.assertEqual((target / "model.safetensors").read_bytes(), b"corrupted")

    def test_failed_download_preserves_shadow_and_audit(self) -> None:
        output = self.root / "failed-models"
        original = os.environ.get("UNIT_HF_FAIL_DOWNLOAD")
        os.environ["UNIT_HF_FAIL_DOWNLOAD"] = "1"
        try:
            with self.assertRaisesRegex(ResearchDataError, "shadow preserved"):
                materialize_model_assets(
                    config_path=self.config,
                    output_root=output,
                    hf_cli=self.hf,
                    execute=True,
                    authorization_reference="unit-test-explicit-authorization",
                )
        finally:
            if original is None:
                os.environ.pop("UNIT_HF_FAIL_DOWNLOAD", None)
            else:
                os.environ["UNIT_HF_FAIL_DOWNLOAD"] = original
        shadows = list(output.glob(".*.building-*"))
        self.assertEqual(len(shadows), 1)
        self.assertTrue((shadows[0] / "DOWNLOAD_FAILED.json").is_file())
        audits = list((output / "_acquisition_audits").glob("*.json"))
        self.assertEqual(len(audits), 1)
        combined_records = (
            (shadows[0] / "DOWNLOAD_FAILED.json").read_text(encoding="utf-8")
            + audits[0].read_text(encoding="utf-8")
        )
        self.assertNotIn("hf_abcdefghijklmnopqrstuv", combined_records)
        self.assertIn("[REDACTED]", combined_records)

    def test_dry_run_missing_required_file_fails_before_download(self) -> None:
        raw = json.loads(self.config.read_text(encoding="utf-8"))
        raw["assets"][0]["include"].append("missing.bin")
        raw["assets"][0]["required_files"].append("missing.bin")
        self.config.write_text(json.dumps(raw), encoding="utf-8")
        output = self.root / "missing-models"
        with self.assertRaisesRegex(ResearchDataError, "omitted required files"):
            materialize_model_assets(
                config_path=self.config,
                output_root=output,
                hf_cli=self.hf,
            )
        self.assertFalse(list(output.glob(".*.building-*")))
        audits = list((output / "_acquisition_audits").glob("*.json"))
        self.assertEqual(len(audits), 1)
        audit = json.loads(audits[0].read_text(encoding="utf-8"))
        self.assertEqual(audit["status"], "failed_before_download")
        self.assertEqual(
            audit["assets"][0]["dry_run"]["missing_required_files"],
            ["missing.bin"],
        )

    def test_dry_run_timeout_is_bounded_and_audited(self) -> None:
        output = self.root / "timeout-models"
        original = os.environ.get("UNIT_HF_TIMEOUT")
        os.environ["UNIT_HF_TIMEOUT"] = "1"
        try:
            with self.assertRaisesRegex(ResearchDataError, "dry-run failed"):
                materialize_model_assets(
                    config_path=self.config,
                    output_root=output,
                    hf_cli=self.hf,
                    dry_run_timeout_seconds=0.05,
                )
        finally:
            if original is None:
                os.environ.pop("UNIT_HF_TIMEOUT", None)
            else:
                os.environ["UNIT_HF_TIMEOUT"] = original
        audits = list((output / "_acquisition_audits").glob("*.json"))
        self.assertEqual(len(audits), 1)
        audit = json.loads(audits[0].read_text(encoding="utf-8"))
        dry_run = audit["assets"][0]["dry_run"]
        self.assertEqual(dry_run["returncode"], 124)
        self.assertTrue(dry_run["timed_out"])
        self.assertIn("timed out", dry_run["stderr"])

    def test_config_rejects_unpinned_or_traversing_assets(self) -> None:
        raw = json.loads(self.config.read_text(encoding="utf-8"))
        raw["assets"][0]["revision"] = "main"
        self.config.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(ResearchDataError, "exactly pinned"):
            load_model_asset_config(self.config)

    def test_adapter_overlay_lock_has_exact_wheel_hashes(self) -> None:
        lock = (
            Path(__file__).resolve().parents[1]
            / "research"
            / "configs"
            / "m3_adapter_overlay_requirements.txt"
        )
        requirements = {
            line.split(" --hash=sha256:", 1)[0]: line.split(
                " --hash=sha256:", 1
            )[1]
            for line in lock.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        }
        self.assertEqual(
            requirements,
            {
                "adapters==1.3.0": (
                    "c0620b1b98df4af3876aa1e053ba43d0a19d82e62fc6a3ebc67d158183e07919"
                ),
                "huggingface-hub==0.36.2": (
                    "48f0c8eac16145dfce371e9d2d7772854a4f591bcb56c9cf548accf531d54270"
                ),
                "transformers==4.57.6": (
                    "4c9e9de11333ddfe5114bc872c9f370509198acf0b87a832a0ab9458e2bd0550"
                ),
            },
        )


if __name__ == "__main__":
    unittest.main()
