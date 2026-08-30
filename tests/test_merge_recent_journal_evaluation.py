from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import stat
import tempfile
import unittest

from scripts import merge_recent_journal_evaluation as merger


class LegacyRecentJournalMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.dataset = self.root / "dataset.jsonl"
        self.dataset.write_text(
            json.dumps(
                {
                    "case_id": "case-1",
                    "doi": "10.1/legacy-test",
                    "title": "Legacy diagnostic test",
                    "abstract": "A synthetic abstract used only by a local unit test.",
                    "published_date": "2026-01-01",
                    "gold_journal_name": "Journal of Tests",
                    "gold_issns": ["1234-5678"],
                    "gold_jcr_quartile": "Q1",
                    "primary_field": "TEST",
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def record(track: str, *, status: str = "ok") -> dict[str, object]:
        record: dict[str, object] = {
            "schema_version": "legacy-test",
            "run_id": "legacy-run",
            "case_id": "case-1",
            "track": track,
            "status": status,
            "catalog_covered": True,
            "gold_jcr_quartile": "Q1",
            "primary_field": "TEST",
        }
        if status == "ok":
            record.update(
                {
                    "final_gold_rank": 1,
                    "preliminary_gold_rank": 1,
                    "recall_pool_gold_rank": 1,
                    "latency_ms": 10,
                    "leakage": {},
                }
            )
        else:
            record["error"] = "synthetic legacy failure"
        return record

    def write_shard(
        self, input_dir: Path, records: list[dict[str, object]]
    ) -> None:
        shard = input_dir / "shard-00"
        shard.mkdir(parents=True)
        (shard / "raw.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

    def argv(self, input_dir: Path, output_dir: Path) -> list[str]:
        return [
            "--input-dir",
            str(input_dir),
            "--dataset",
            str(self.dataset),
            "--output-dir",
            str(output_dir),
        ]

    def test_modern_evaluator_markers_are_rejected_without_output(self) -> None:
        marker_builders = {
            "run_manifest.json": lambda root: (root / "run_manifest.json").write_text(
                json.dumps({"evaluation_mode": "formal_500_full_denominator"}),
                encoding="utf-8",
            ),
            "raw_segments": lambda root: (root / "raw_segments").mkdir(),
            "closeout": lambda root: (
                root / "closeout.generation-000001.json"
            ).write_text("{}\n", encoding="utf-8"),
        }
        for name, build_marker in marker_builders.items():
            with self.subTest(marker=name):
                input_dir = self.root / f"modern-{name}"
                input_dir.mkdir()
                self.write_shard(
                    input_dir,
                    [self.record("title_abstract"), self.record("abstract_only")],
                )
                build_marker(input_dir)
                output = self.root / f"output-{name}"
                with self.assertRaisesRegex(
                    SystemExit, "refusing modern/formal evaluator input"
                ):
                    merger.main(self.argv(input_dir, output))
                self.assertFalse(output.exists())

    def test_nested_formal_marker_is_also_rejected(self) -> None:
        input_dir = self.root / "nested-modern"
        self.write_shard(
            input_dir,
            [self.record("title_abstract"), self.record("abstract_only")],
        )
        (input_dir / "shard-00" / "run_manifest.json").write_text(
            "{}\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(SystemExit, "refusing modern/formal"):
            merger.main(self.argv(input_dir, self.root / "nested-output"))

    def test_complete_legacy_merge_is_explicitly_nonformal(self) -> None:
        input_dir = self.root / "legacy-complete"
        output = self.root / "legacy-report"
        self.write_shard(
            input_dir,
            [self.record("title_abstract"), self.record("abstract_only")],
        )
        rendered = StringIO()
        with redirect_stdout(rendered):
            self.assertEqual(merger.main(self.argv(input_dir, output)), 0)

        summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        console = json.loads(rendered.getvalue())
        self.assertEqual(summary["evaluation_mode"], merger.LEGACY_MODE)
        self.assertFalse(summary["formal_full_denominator"])
        self.assertIn("must not be reported as a formal", summary["claim_status"])
        self.assertEqual(summary["execution_outcomes"]["ok"], 2)
        self.assertEqual(summary["exit_code"], 0)
        self.assertFalse(console["formal_full_denominator"])
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((output / "summary.json").stat().st_mode), 0o600)
        self.assertIn(
            "LEGACY DIAGNOSTIC ONLY — NOT A FORMAL EVALUATION",
            (output / "summary.md").read_text(encoding="utf-8"),
        )

    def test_missing_and_error_diagnostics_return_nonzero(self) -> None:
        missing_input = self.root / "legacy-missing"
        missing_output = self.root / "missing-report"
        self.write_shard(missing_input, [self.record("title_abstract")])
        with redirect_stdout(StringIO()):
            self.assertEqual(merger.main(self.argv(missing_input, missing_output)), 4)
        missing_summary = json.loads(
            (missing_output / "summary.json").read_text(encoding="utf-8")
        )
        self.assertEqual(missing_summary["execution_outcomes"]["missing"], 1)

        error_input = self.root / "legacy-error"
        error_output = self.root / "error-report"
        self.write_shard(
            error_input,
            [self.record("title_abstract"), self.record("abstract_only", status="error")],
        )
        with redirect_stdout(StringIO()):
            self.assertEqual(merger.main(self.argv(error_input, error_output)), 3)
        error_summary = json.loads(
            (error_output / "summary.json").read_text(encoding="utf-8")
        )
        self.assertEqual(error_summary["execution_outcomes"]["error"], 1)

    def test_existing_output_is_never_overwritten(self) -> None:
        input_dir = self.root / "legacy-existing"
        output = self.root / "existing-report"
        self.write_shard(
            input_dir,
            [self.record("title_abstract"), self.record("abstract_only")],
        )
        output.mkdir()
        marker = output / "summary.json"
        marker.write_text("preserve-me\n", encoding="utf-8")

        with self.assertRaisesRegex(SystemExit, "refusing overwrite"):
            merger.main(self.argv(input_dir, output))
        self.assertEqual(marker.read_text(encoding="utf-8"), "preserve-me\n")


if __name__ == "__main__":
    unittest.main()
