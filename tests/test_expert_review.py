from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.data import ResearchDataError, sha256_file
from research.expert_review import (
    ExpertReviewStore,
    build_conflict_report,
    export_expert_review,
    fleiss_kappa,
)


def _package(root: Path) -> Path:
    package = root / "package"
    package.mkdir()
    item = {
        "review_id": "R-1",
        "query_alias": "Q001",
        "candidate_alias": "Q001-C01",
        "query": {"title": "Query", "abstract": "Abstract", "user_constraints": {}},
        "candidate": {"name": "Venue"},
        "explanation": {"available": False},
    }
    public = package / "review_items.public.jsonl"
    public.write_text(json.dumps(item) + "\n", encoding="utf-8")
    for expert in ("expert_a", "expert_b", "expert_c"):
        (package / f"assignment.{expert}.json").write_text(
            json.dumps(
                {"schema_version": 1, "expert_id": expert, "review_ids": ["R-1"]}
            )
            + "\n",
            encoding="utf-8",
        )
    manifest = {
        "schema_version": 1,
        "artifact_type": "three_expert_blind_review_package",
        "experts": ["expert_a", "expert_b", "expert_c"],
        "outputs": {
            "public_items": {
                "path": str(public.resolve()),
                "sha256": sha256_file(public),
                "bytes": public.stat().st_size,
            }
        },
    }
    (package / "manifest.json").write_text(
        json.dumps(manifest) + "\n", encoding="utf-8"
    )
    return package


def _annotation(relevance: int = 2) -> dict[str, object]:
    return {
        "review_id": "R-1",
        "relevance": relevance,
        "submission_fit": 2,
        "constraint_violation": "none",
        "explanation_quality": "not_available",
        "notes": "",
    }


class AgreementTests(unittest.TestCase):
    def test_fleiss_kappa_is_one_for_perfect_agreement(self) -> None:
        self.assertEqual(
            fleiss_kappa([[0, 0, 0], [1, 1, 1]], categories=(0, 1)),
            1.0,
        )


class ExpertStoreTests(unittest.TestCase):
    def test_progress_conflicts_and_immutable_export_use_real_inputs(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = _package(root)
            state = root / "state"
            stores = {
                expert: ExpertReviewStore(package, state, expert)
                for expert in ("expert_a", "expert_b", "expert_c")
            }
            stores["expert_a"].save(_annotation(3))
            stores["expert_a"].save(_annotation(2), phase="conflict_review")
            audit = [
                json.loads(line)
                for line in stores["expert_a"].audit_path.read_text().splitlines()
            ]
            self.assertEqual(len(audit), 2)
            self.assertEqual(audit[1]["previous_event_sha256"], audit[0]["event_sha256"])
            self.assertEqual(stores["expert_a"].progress()["completed"], 1)

            with self.assertRaisesRegex(ResearchDataError, "incomplete"):
                export_expert_review(
                    package,
                    state,
                    root / "premature",
                    generation_command=("python", "-m", "research"),
                )
            stores["expert_b"].save(_annotation(1))
            stores["expert_c"].save(_annotation(2))
            conflicts = build_conflict_report(package, state)
            self.assertEqual(conflicts["complete_triplet_count"], 1)
            self.assertEqual(conflicts["conflict_count"], 1)
            self.assertFalse(conflicts["method_sources_exposed"])

            output = root / "export"
            manifest = export_expert_review(
                package,
                state,
                output,
                generation_command=("python", "-m", "research"),
            )
            self.assertEqual(manifest["status"], "human_evaluation_complete")
            self.assertEqual(manifest["real_annotation_count"], 3)
            self.assertEqual(manifest["synthetic_annotation_count"], 0)
            self.assertTrue((output / "anonymous_annotations.raw.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
