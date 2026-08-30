from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from research.data import (
    DatasetBundle,
    ResearchDataError,
    build_run_binding,
    canonical_json_sha256,
    ordered_ids_sha256,
    sha256_file,
    write_run,
)
import research.sealed_namespace_repair as repair_module
from research.sealed_namespace_repair import (
    AUTHORIZATION_STATUS,
    FIRST_FAILURE_MESSAGE,
    FIRST_FAILURE_STATUS,
    MAPPING_METHOD,
    REPAIR_STATUS,
    evaluate_post_access_namespace_repair,
    load_namespace_mapping,
    namespace_repair_readiness,
    preflight_namespace_repair,
    translate_bundle_qrels,
)
from research.types import Query, ScoredDocument


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


class NamespaceRepairCoreTests(unittest.TestCase):
    def test_atomic_directory_publish_never_replaces_even_an_empty_target(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            target.mkdir()
            (source / "evidence.json").write_text("source\n", encoding="utf-8")
            (target / "preserved.txt").write_text("target\n", encoding="utf-8")
            with self.assertRaisesRegex(ResearchDataError, "not overwritten"):
                repair_module._rename_directory_noreplace(source, target)
            self.assertTrue(source.is_dir())
            self.assertTrue(target.is_dir())
            self.assertEqual(
                (target / "preserved.txt").read_text(encoding="utf-8"), "target\n"
            )
            (target / "preserved.txt").unlink()
            target.rmdir()
            repair_module._rename_directory_noreplace(source, target)
            self.assertFalse(source.exists())
            self.assertEqual(
                (target / "evidence.json").read_text(encoding="utf-8"), "source\n"
            )

    def test_catalog_mapping_translates_only_qrel_document_ids(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapping_path = root / "mapping.jsonl"
            _write_jsonl(
                mapping_path,
                [
                    {
                        "source_venue_id": "source-old",
                        "target_venue_id": "candidate-a",
                        "mapping_method": MAPPING_METHOD,
                    },
                    {
                        "source_venue_id": "candidate-b",
                        "target_venue_id": "candidate-b",
                        "mapping_method": MAPPING_METHOD,
                    },
                ],
            )
            mapping, counts = load_namespace_mapping(
                mapping_path,
                candidate_ids=("candidate-a", "candidate-b"),
                expected={
                    "source_count": 2,
                    "target_count": 2,
                    "identity_count": 1,
                    "remapped_count": 1,
                    "unmapped_count": 0,
                    "ambiguous_count": 0,
                    "collision_count": 0,
                },
            )
            query = Query(
                query_id="q1",
                text="unchanged text",
                publication_date="2026-07-01",
            )
            source_rows = {"q1": {"paper_id": "q1", "title": "unchanged"}}
            bundle = DatasetBundle(
                (query,), {"q1": {"source-old": 1.0}}, source_rows
            )
            translated, audit = translate_bundle_qrels(
                bundle,
                namespace_mapping=mapping,
                candidate_ids=("candidate-a", "candidate-b"),
            )
            self.assertIs(translated.queries, bundle.queries)
            self.assertIs(translated.source_rows, bundle.source_rows)
            self.assertEqual(translated.qrels, {"q1": {"candidate-a": 1.0}})
            self.assertEqual(audit["mapped_query_count"], 1)
            self.assertEqual(audit["remapped_query_count"], 1)
            self.assertEqual(counts["collision_count"], 0)

    def test_mapping_and_qrel_fail_closed_on_collision_or_missing_source(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            mapping_path = root / "mapping.jsonl"
            _write_jsonl(
                mapping_path,
                [
                    {
                        "source_venue_id": "source-a",
                        "target_venue_id": "candidate-a",
                        "mapping_method": MAPPING_METHOD,
                    },
                    {
                        "source_venue_id": "source-b",
                        "target_venue_id": "candidate-a",
                        "mapping_method": MAPPING_METHOD,
                    },
                ],
            )
            with self.assertRaisesRegex(ResearchDataError, "not one-to-one"):
                load_namespace_mapping(
                    mapping_path,
                    candidate_ids=("candidate-a", "candidate-b"),
                    expected={},
                )
            query = Query("q1", "text", "2026-07-01")
            bundle = DatasetBundle((query,), {"q1": {"missing": 1.0}}, {})
            with self.assertRaisesRegex(ResearchDataError, "unmapped gold"):
                translate_bundle_qrels(
                    bundle,
                    namespace_mapping={"source-a": "candidate-a"},
                    candidate_ids=("candidate-a",),
                )


class NamespaceRepairEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.sentinel_patcher = patch.object(
            repair_module,
            "GLOBAL_REPAIR_SENTINEL_ROOT",
            self.root / "global-repair-sentinels",
        )
        self.sentinel_patcher.start()
        self.git_state_patcher = patch.object(
            repair_module,
            "_git_state",
            return_value={
                "commit": "fixture-runtime-commit",
                "tracked_worktree_clean": True,
                "status_sha256": "0" * 64,
            },
        )
        self.git_state_patcher.start()

    def tearDown(self) -> None:
        self.git_state_patcher.stop()
        self.sentinel_patcher.stop()
        self.temporary.cleanup()

    def _fixture(self, *, gold_id: str = "source-old") -> tuple[Path, Path, Path]:
        root = self.root
        blind = root / "queries.blind.jsonl"
        _write_jsonl(
            blind,
            [
                {
                    "paper_id": "q1",
                    "title": "A future paper unrelated to the candidate name",
                    "abstract": "A sufficiently descriptive future abstract",
                    "publication_date": "2026-07-01",
                    "publication_date_precision": "day",
                    "language": "en",
                    "article_type": "journal-article",
                    "user_constraints": {},
                }
            ],
        )
        labels = root / "labels.sealed.jsonl"
        _write_jsonl(
            labels,
            [
                {
                    "paper_id": "q1",
                    "doi": "10.1/q1",
                    "gold_journal_id": gold_id,
                    "gold_journal_name": "Venue A",
                    "broad_field": "test",
                    "gold_jcr_quartile": "Q1",
                }
            ],
        )
        os.chmod(labels, 0o600)
        profiles = root / "profiles.jsonl"
        _write_jsonl(
            profiles,
            [
                {
                    "venue_id": "candidate-a",
                    "name": "Venue A",
                    "snapshot_date": "2026-03-31",
                    "metadata": {"field": "test", "quartile": "Q1"},
                }
            ],
        )
        query_ids = ("q1",)
        candidate_ids = ("candidate-a",)
        runtime = {
            "code": {"commit": "test", "dirty": False},
            "python": {"version": "test"},
            "dependencies": [],
            "hardware": {},
        }
        committed: dict[str, dict[str, object]] = {}
        for index, name in enumerate(("method_a", "method_b"), 1):
            run_path = root / f"{name}.jsonl"
            generation_config = {"method": name}
            binding = build_run_binding(
                dataset_path=blind,
                profiles_path=profiles,
                query_ids=query_ids,
                candidate_ids=candidate_ids,
                configuration=generation_config,
            )
            manifest = write_run(
                run_path,
                {"q1": [ScoredDocument("candidate-a", float(index))]},
                binding=binding,
                query_ids=query_ids,
                candidate_ids=candidate_ids,
                top_k=1,
                method={
                    "name": name,
                    "implementation_revision": "fixture-" + name,
                    "configuration_sha256": canonical_json_sha256(
                        generation_config
                    ),
                },
                command=("test", name),
                working_directory=root,
                runtime=runtime,
                additional_manifest_fields={
                    "execution": {
                        "failed_query_count": 0,
                        "external_api_calls": 0,
                        "search_free": True,
                    }
                },
            )
            sidecar = run_path.with_suffix(".jsonl.manifest.json")
            committed[name] = {
                "run": manifest["output"],
                "manifest": {
                    "path": str(sidecar),
                    "sha256": sha256_file(sidecar),
                    "bytes": sidecar.stat().st_size,
                },
            }

        method_freeze = {
            "method_family": ["method_a", "method_b"],
            "method_hyperparameters": {"frozen": True},
            "metrics": {
                "primary": "ndcg@1",
                "retrieval_depth": 1,
                "cutoffs": [1],
                "denominator_policy": "all_300_queries_no_failure_removal",
            },
            "statistics": {
                "comparison_family": "all_methods_unordered_pairs",
                "metric": "ndcg@1",
                "bootstrap_iterations": 100,
                "permutation_iterations": 100,
                "confidence": 0.95,
                "seed": 7,
                "multiple_comparison_corrections": [
                    "holm_family_wise",
                    "benjamini_hochberg_fdr",
                ],
            },
        }
        freeze_config = root / "freeze.json"
        _write_json(freeze_config, {"method_freeze": method_freeze})
        commitment = root / "prediction_commitment.json"
        _write_json(
            commitment,
            {
                "status": "predictions_committed_before_label_access",
                "label_vault_commitment": {
                    "sha256": sha256_file(labels),
                    "content_parsed": False,
                },
                "query_count": 1,
                "query_ids_sha256": ordered_ids_sha256(query_ids),
                "candidate_count": 1,
                "candidate_ids_sha256": ordered_ids_sha256(candidate_ids),
                "sources": committed,
                "variants": {},
            },
        )
        sealed_manifest = root / "sealed_manifest.json"
        _write_json(
            sealed_manifest,
            {
                "artifact_type": "future_sealed_test",
                "status": "labels_sealed_predictions_pending",
                "config": {
                    "path": str(freeze_config),
                    "sha256": sha256_file(freeze_config),
                    "bytes": freeze_config.stat().st_size,
                },
                "method_freeze": {
                    "method_family": method_freeze["method_family"],
                    "method_hyperparameters_sha256": canonical_json_sha256(
                        method_freeze["method_hyperparameters"]
                    ),
                    "metrics_sha256": canonical_json_sha256(
                        method_freeze["metrics"]
                    ),
                    "statistics_sha256": canonical_json_sha256(
                        method_freeze["statistics"]
                    ),
                    "candidates": {
                        "count": 1,
                        "ordered_ids_sha256": ordered_ids_sha256(candidate_ids),
                        "profiles_sha256": sha256_file(profiles),
                    },
                },
                "dataset": {
                    "record_count": 1,
                    "blind_queries": {
                        "path": str(blind),
                        "sha256": sha256_file(blind),
                        "bytes": blind.stat().st_size,
                    },
                    "sealed_labels": {
                        "path": str(labels),
                        "sha256": sha256_file(labels),
                        "bytes": labels.stat().st_size,
                    },
                },
            },
        )
        output = root / "evaluation"
        original_config = root / "evaluation.json"
        _write_json(
            original_config,
            {
                "schema_version": 1,
                "status": "predictions_committed_before_label_unseal",
                "offline_only": True,
                "search_free": True,
                "output_dir": str(output),
                "sealed_test": {
                    "manifest": str(sealed_manifest),
                    "manifest_sha256": sha256_file(sealed_manifest),
                },
                "prediction_commitment": {
                    "path": str(commitment),
                    "sha256": sha256_file(commitment),
                },
                "corpus": {
                    "path": str(profiles),
                    "id_field": "venue_id",
                    "text_fields": ["name"],
                    "snapshot_field": "snapshot_date",
                },
                "methods": [
                    {
                        "name": name,
                        "path": record["run"]["path"],
                        "run_sha256": record["run"]["sha256"],
                        "manifest_path": record["manifest"]["path"],
                        "manifest_sha256": record["manifest"]["sha256"],
                    }
                    for name, record in committed.items()
                ],
                "evaluation": {"cutoffs": [1]},
                "statistics": {
                    key: value
                    for key, value in method_freeze["statistics"].items()
                    if key != "multiple_comparison_corrections"
                },
            },
        )
        label_free_preflight = root / "preflight.json"
        _write_json(
            label_free_preflight,
            {
                "status": "ready_for_single_label_access",
                "label_content_parsed": False,
                "config": {"sha256": sha256_file(original_config)},
            },
        )
        access_audit = root / "evaluation.label-access-20260830T000000+0000.json"
        _write_json(
            access_audit,
            {
                "schema_version": 1,
                "artifact_type": "sealed_label_access_audit",
                "reason": "post-commit metric evaluation",
                "predictions_committed_before_access": True,
                "refit_after_access": False,
                "hyperparameter_change_after_access": False,
                "label_file": {
                    "path": str(labels),
                    "sha256": sha256_file(labels),
                    "bytes": labels.stat().st_size,
                    "record_count": 1,
                },
                "prediction_commitment": {
                    "sha256": sha256_file(commitment)
                },
            },
        )
        os.chmod(access_audit, 0o444)
        failure = root / "first_failure.json"
        _write_json(
            failure,
            {
                "artifact_type": "sealed_evaluation_first_attempt_failure_record",
                "status": FIRST_FAILURE_STATUS,
                "failure": {
                    "message": FIRST_FAILURE_MESSAGE,
                    "label_access_completed": True,
                    "metrics_computed": False,
                    "evaluation_output_published": False,
                },
                "artifacts": {
                    "evaluation_config": {
                        "sha256": sha256_file(original_config)
                    },
                    "label_free_preflight": {
                        "sha256": sha256_file(label_free_preflight)
                    },
                    "sealed_test_manifest": {
                        "sha256": sha256_file(sealed_manifest)
                    },
                    "prediction_commitment": {
                        "sha256": sha256_file(commitment)
                    },
                    "label_access_audit": {
                        "sha256": sha256_file(access_audit)
                    },
                    "sealed_labels": {"sha256": sha256_file(labels)},
                    "frozen_candidate_profiles": {
                        "sha256": sha256_file(profiles)
                    },
                },
            },
        )
        os.chmod(failure, 0o444)
        namespace_mapping = root / "namespace_mapping.jsonl"
        _write_jsonl(
            namespace_mapping,
            [
                {
                    "source_venue_id": "source-old",
                    "target_venue_id": "candidate-a",
                    "mapping_method": MAPPING_METHOD,
                }
            ],
        )
        namespace_manifest = root / "namespace_manifest.json"
        _write_json(
            namespace_manifest,
            {
                "artifact_type": "sealed_venue_namespace_crosswalk",
                "status": "complete_label_free_exact_issn_bijection",
                "implementation": {
                    "path": str(Path(repair_module.__file__).resolve()),
                    "sha256": sha256_file(Path(repair_module.__file__).resolve()),
                },
                "label_boundary": {
                    "label_input_configured": False,
                    "label_files_opened": 0,
                    "label_content_parsed": False,
                },
                "matching_policy": {
                    "method": MAPPING_METHOD,
                    "checksum_valid_issn_required": True,
                    "fuzzy_matching": False,
                    "journal_names_emitted": False,
                },
                "counts": {
                    "source": 1,
                    "target": 1,
                    "mapped": 1,
                    "distinct_target": 1,
                    "identity": 0,
                    "remapped": 1,
                    "source_unmapped": 0,
                    "target_unmapped": 0,
                    "ambiguous": 0,
                    "collision": 0,
                },
                "source": {
                    "namespace_sha256": "source-namespace-fixture",
                    "artifacts": [
                        {
                            "path": str(profiles),
                            "sha256": sha256_file(profiles),
                        }
                    ],
                },
                "target": {
                    "namespace_sha256": "target-namespace-fixture",
                    "artifact": {
                        "path": str(profiles),
                        "sha256": sha256_file(profiles),
                    },
                },
                "expectations": {
                    "source_namespace_sha256": "source-namespace-fixture",
                    "target_namespace_sha256": "target-namespace-fixture",
                    "source_count": 1,
                    "target_count": 1,
                    "identity_count": 0,
                    "remap_count": 1,
                },
                "mapping_artifact": {
                    "path": str(namespace_mapping),
                    "sha256": sha256_file(namespace_mapping),
                    "bytes": namespace_mapping.stat().st_size,
                    "record_count": 1,
                },
            },
        )
        os.chmod(namespace_mapping, 0o444)
        os.chmod(namespace_manifest, 0o444)
        implementation_path = Path(repair_module.__file__).resolve()
        code_bundle = [
            {
                "path": str(repair_module.REPOSITORY_ROOT / relative),
                "repository_relative_path": relative,
                "sha256": sha256_file(repair_module.REPOSITORY_ROOT / relative),
            }
            for relative in sorted(repair_module.CODE_BUNDLE_PATHS)
        ]
        code_bundle_sha256 = canonical_json_sha256(
            [
                {
                    "repository_relative_path": record[
                        "repository_relative_path"
                    ],
                    "sha256": record["sha256"],
                }
                for record in code_bundle
            ]
        )
        repair_config = root / "repair.json"
        _write_json(
            repair_config,
            {
                "schema_version": 1,
                "status": REPAIR_STATUS,
                "offline_only": True,
                "search_free": True,
                "implementation": {
                    "path": str(implementation_path),
                    "sha256": sha256_file(implementation_path),
                },
                "code_bundle": code_bundle,
                "code_bundle_sha256": code_bundle_sha256,
                "original_evaluation": {
                    "config": {
                        "path": str(original_config),
                        "sha256": sha256_file(original_config),
                    },
                    "label_free_preflight": {
                        "path": str(label_free_preflight),
                        "sha256": sha256_file(label_free_preflight),
                    },
                },
                "first_attempt": {
                    "record": {
                        "path": str(failure),
                        "sha256": sha256_file(failure),
                    },
                    "label_access_audit": {
                        "path": str(access_audit),
                        "sha256": sha256_file(access_audit),
                    },
                },
                "namespace_crosswalk": {
                    "source_namespace_sha256": "source-namespace-fixture",
                    "target_namespace_sha256": "target-namespace-fixture",
                    "mapping": {
                        "path": str(namespace_mapping),
                        "sha256": sha256_file(namespace_mapping),
                    },
                    "manifest": {
                        "path": str(namespace_manifest),
                        "sha256": sha256_file(namespace_manifest),
                    },
                    "expected": {
                        "source_count": 1,
                        "target_count": 1,
                        "mapped_count": 1,
                        "distinct_target_count": 1,
                        "identity_count": 0,
                        "remapped_count": 1,
                        "unmapped_count": 0,
                        "ambiguous_count": 0,
                        "collision_count": 0,
                    },
                },
            },
        )
        authorization = root / "authorization.json"
        _write_json(
            authorization,
            {
                "schema_version": 1,
                "status": AUTHORIZATION_STATUS,
                "repair_config_sha256": sha256_file(repair_config),
                "original_evaluation_config_sha256": sha256_file(
                    original_config
                ),
                "sealed_label_sha256": sha256_file(labels),
                "repair_identity": repair_module._repair_identity(
                    sha256_file(labels)
                ),
                "prediction_commitment_sha256": sha256_file(commitment),
                "namespace_crosswalk_sha256": sha256_file(namespace_mapping),
                "code_bundle_sha256": code_bundle_sha256,
                "runtime_git_commit": "fixture-runtime-commit",
                "tracked_worktree_clean_required": True,
                **repair_module._runtime_versions(),
                "output_dir": str(output.resolve()),
                "authorization_reference": "synthetic unit-test authorization",
                "scope": {
                    "semantic_label_reads": 1,
                    "query_denominator": 1,
                    "search_calls": 0,
                    "llm_calls": 0,
                    "embedding_calls": 0,
                    "estimated_external_cost_usd": 0,
                    "allow_method_or_statistic_changes": False,
                },
            },
        )
        os.chmod(authorization, 0o444)
        return repair_config, authorization, output

    def _clone_chain_with_new_output(
        self, repair_config: Path, *, output: Path
    ) -> tuple[Path, Path]:
        """Recreate the formerly exploitable caller-supplied evidence chain."""

        repair = json.loads(repair_config.read_text())
        original_path = Path(repair["original_evaluation"]["config"]["path"])
        original = json.loads(original_path.read_text())
        cloned_original = self.root / "cloned-evaluation.json"
        original["output_dir"] = str(output)
        _write_json(cloned_original, original)

        cloned_preflight = self.root / "cloned-preflight.json"
        preflight = json.loads(
            Path(
                repair["original_evaluation"]["label_free_preflight"]["path"]
            ).read_text()
        )
        preflight["config"]["sha256"] = sha256_file(cloned_original)
        _write_json(cloned_preflight, preflight)

        original_access = Path(
            repair["first_attempt"]["label_access_audit"]["path"]
        )
        cloned_access = self.root / "cloned-output.label-access-copy.json"
        cloned_access.write_bytes(original_access.read_bytes())
        os.chmod(cloned_access, 0o444)
        cloned_failure = self.root / "cloned-first-failure.json"
        failure = json.loads(
            Path(repair["first_attempt"]["record"]["path"]).read_text()
        )
        failure["artifacts"]["evaluation_config"]["sha256"] = sha256_file(
            cloned_original
        )
        failure["artifacts"]["label_access_audit"]["sha256"] = sha256_file(
            cloned_access
        )
        _write_json(cloned_failure, failure)
        os.chmod(cloned_failure, 0o444)

        repair["original_evaluation"]["config"] = {
            "path": str(cloned_original),
            "sha256": sha256_file(cloned_original),
        }
        repair["original_evaluation"]["label_free_preflight"] = {
            "path": str(cloned_preflight),
            "sha256": sha256_file(cloned_preflight),
        }
        repair["first_attempt"]["record"] = {
            "path": str(cloned_failure),
            "sha256": sha256_file(cloned_failure),
        }
        repair["first_attempt"]["label_access_audit"] = {
            "path": str(cloned_access),
            "sha256": sha256_file(cloned_access),
        }
        cloned_repair = self.root / "cloned-repair.json"
        _write_json(cloned_repair, repair)

        labels = self.root / "labels.sealed.jsonl"
        commitment = self.root / "prediction_commitment.json"
        crosswalk = Path(repair["namespace_crosswalk"]["mapping"]["path"])
        cloned_authorization = self.root / "cloned-authorization.json"
        _write_json(
            cloned_authorization,
            {
                "schema_version": 1,
                "status": AUTHORIZATION_STATUS,
                "repair_config_sha256": sha256_file(cloned_repair),
                "original_evaluation_config_sha256": sha256_file(
                    cloned_original
                ),
                "sealed_label_sha256": sha256_file(labels),
                "repair_identity": repair_module._repair_identity(
                    sha256_file(labels)
                ),
                "prediction_commitment_sha256": sha256_file(commitment),
                "namespace_crosswalk_sha256": sha256_file(crosswalk),
                "code_bundle_sha256": repair["code_bundle_sha256"],
                "runtime_git_commit": "fixture-runtime-commit",
                "tracked_worktree_clean_required": True,
                **repair_module._runtime_versions(),
                "output_dir": str(output.resolve()),
                "authorization_reference": "synthetic cloned-chain authorization",
                "scope": {
                    "semantic_label_reads": 1,
                    "query_denominator": 1,
                    "search_calls": 0,
                    "llm_calls": 0,
                    "embedding_calls": 0,
                    "estimated_external_cost_usd": 0,
                    "allow_method_or_statistic_changes": False,
                },
            },
        )
        os.chmod(cloned_authorization, 0o444)
        return cloned_repair, cloned_authorization

    def test_full_repair_is_explicitly_non_pristine_and_one_shot(self) -> None:
        config, authorization, output = self._fixture()
        with patch(
            "research.sealed_namespace_repair._unseal_labels_from_stable_file",
            side_effect=AssertionError("label parser must not run in preflight"),
        ):
            readiness = namespace_repair_readiness(config)
        self.assertFalse(readiness["label_content_parsed"])
        self.assertTrue(readiness["explicit_user_authorization_required"])
        self.assertFalse(readiness["second_semantic_label_read_authorized"])
        preflight = preflight_namespace_repair(config, authorization_path=authorization)
        self.assertFalse(preflight.repair_start_path.exists())
        real_unseal = repair_module._unseal_labels_from_stable_file

        def guarded_unseal(**kwargs: object):
            self.assertTrue(preflight.repair_start_path.is_file())
            self.assertEqual(
                preflight.repair_start_path.stat().st_mode & 0o777, 0o444
            )
            return real_unseal(**kwargs)

        with patch(
            "research.sealed_namespace_repair._unseal_labels_from_stable_file",
            side_effect=guarded_unseal,
        ):
            result = evaluate_post_access_namespace_repair(
                config,
                authorization_path=authorization,
                generation_command=("test", "repair"),
            )
        self.assertEqual(
            result["status"],
            "complete_post_access_namespace_repaired_evaluation",
        )
        self.assertFalse(result["pristine_single_pass_sealed_test"])
        self.assertEqual(result["coverage"]["query_count"], 1)
        mapping_audit = json.loads(
            (output / "namespace_mapping_audit.json").read_text()
        )
        self.assertEqual(mapping_audit["coverage"]["remapped_query_count"], 1)
        sentinel = preflight.repair_start_path
        self.assertEqual(sentinel.stat().st_mode & 0o777, 0o444)
        with self.assertRaisesRegex(ResearchDataError, "will not be overwritten"):
            evaluate_post_access_namespace_repair(
                config,
                authorization_path=authorization,
                generation_command=("test", "retry"),
            )
        cloned_config, cloned_authorization = self._clone_chain_with_new_output(
            config, output=self.root / "cloned-output"
        )
        with self.assertRaisesRegex(ResearchDataError, "this label vault"):
            evaluate_post_access_namespace_repair(
                cloned_config,
                authorization_path=cloned_authorization,
                generation_command=("test", "cloned-output-bypass"),
            )
        self.assertFalse((self.root / "cloned-output").exists())

    def test_unmapped_gold_fails_before_metrics_and_sentinel_blocks_retry(self) -> None:
        config, authorization, output = self._fixture(gold_id="not-in-crosswalk")
        with self.assertRaisesRegex(ResearchDataError, "unmapped gold"):
            evaluate_post_access_namespace_repair(
                config,
                authorization_path=authorization,
                generation_command=("test", "repair"),
            )
        self.assertFalse(output.exists())
        sentinel = (
            self.root
            / "global-repair-sentinels"
            / (repair_module._repair_identity(sha256_file(self.root / "labels.sealed.jsonl")) + ".json")
        )
        self.assertTrue(sentinel.exists())
        failed = list(self.root.glob("evaluation.namespace-repair-failed-*"))
        self.assertEqual(len(failed), 1)
        failure = json.loads((failed[0] / "failure.json").read_text())
        self.assertFalse(failure["metrics_computed"])
        self.assertFalse((failed[0] / "metrics.json").exists())
        with self.assertRaisesRegex(ResearchDataError, "permanently one-shot"):
            preflight_namespace_repair(config, authorization_path=authorization)

    def test_statistical_failure_records_in_memory_metrics_without_publication(self) -> None:
        config, authorization, output = self._fixture()
        with patch(
            "research.sealed_namespace_repair.paired_bootstrap_ci",
            side_effect=RuntimeError("synthetic statistics failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic statistics"):
                evaluate_post_access_namespace_repair(
                    config,
                    authorization_path=authorization,
                    generation_command=("test", "repair"),
                )
        self.assertFalse(output.exists())
        failed = list(self.root.glob("evaluation.namespace-repair-failed-*"))
        self.assertEqual(len(failed), 1)
        failure = json.loads((failed[0] / "failure.json").read_text())
        self.assertTrue(failure["metrics_computed"])
        self.assertFalse(failure["output_published"])
        self.assertEqual(failure["stage"], "frozen_metric_computation")
        self.assertFalse((failed[0] / "metrics.json").exists())

    def test_wrong_or_multiple_prior_access_audits_fail_before_sentinel(self) -> None:
        config, authorization, _output = self._fixture()
        expected = self.root / "evaluation.label-access-20260830T000000+0000.json"
        os.chmod(expected, 0o644)
        with self.assertRaisesRegex(ResearchDataError, "mode 0444"):
            preflight_namespace_repair(config, authorization_path=authorization)
        os.chmod(expected, 0o444)
        extra = self.root / "evaluation.label-access-extra.json"
        _write_json(extra, {"artifact_type": "sealed_label_access_audit"})
        os.chmod(extra, 0o444)
        with self.assertRaisesRegex(ResearchDataError, "exactly the committed"):
            preflight_namespace_repair(config, authorization_path=authorization)
        self.assertFalse(
            any((self.root / "global-repair-sentinels").glob("*.json"))
        )


if __name__ == "__main__":
    unittest.main()
