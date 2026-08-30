from __future__ import annotations

from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal
import hashlib
from io import StringIO
import json
import os
import shutil
import stat
import unittest
from pathlib import Path
import tempfile
from unittest import mock

import scripts.evaluate_recent_journals as evaluator
import scripts.build_recent_journal_benchmark as benchmark_builder

from scripts.evaluate_recent_journals import (
    audit_search_leakage,
    case_from_payload,
    gold_rank,
    normalized_title_similarity,
    prediction_matches_gold,
    ranking_metrics,
    build_summary,
    summarize_records,
)


class FormalAcquisitionCompatibilityTests(unittest.TestCase):
    def test_builder_bundle_passes_evaluator_exact_schema_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            venue = benchmark_builder.JournalVenue(
                venue_id="fixture-journal",
                entity_id=100,
                name="Fixture Journal",
                quartile="Q1",
                category="ONCOLOGY",
                broad_field="clinical_medicine",
                issns=("00079235",),
                lookup_issn="0007-9235",
            )
            window = benchmark_builder.BuildWindow(
                date(2026, 1, 1), date(2026, 4, 30)
            )
            item = {
                "DOI": "10.1234/evaluator-compatibility",
                "type": "journal-article",
                "title": ["Cross-component acquisition evidence compatibility"],
                "abstract": "<jats:p>" + ("Detailed methods and findings. " * 20) + "</jats:p>",
                "ISSN": ["0007-9235"],
                "container-title": ["Fixture Journal"],
                "published-online": {"date-parts": [[2026, 2, 3]]},
                "language": "en",
            }
            response_bytes = json.dumps(
                {"message": {"items": [item]}},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")

            class FakeOpener:
                def open(self, _request, *, timeout):
                    del timeout
                    return __import__("io").BytesIO(response_bytes)

            ledger = root / "request-ledger.jsonl"
            client = benchmark_builder.CrossrefClient(
                cache_dir=root / "cache",
                mailto="test@example.org",
                timeout=5.0,
                retries=0,
                request_interval=0.0,
                use_environment_proxy=False,
                refresh_cache=False,
                max_network_requests=2,
                request_ledger=ledger,
                request_budget_id="evaluator-compatibility-budget",
                require_private_storage=True,
                budget_registry_dir=root / "budget-registry",
            )
            client.opener = FakeOpener()
            payload, response_evidence = client.get_json_with_evidence(
                "/works",
                {
                    "cursor": "*",
                    "filter": (
                        "from-pub-date:2026-01-01,until-pub-date:2026-04-30,"
                        "type:journal-article,has-abstract:true"
                    ),
                    "rows": "1",
                },
            )
            issn_index = benchmark_builder.build_issn_index([venue])
            record, status = benchmark_builder.prepare_crossref_record(
                payload["message"]["items"][0],
                issn_index=issn_index,
                expected_venue=venue,
                window=window,
                min_abstract_chars=100,
            )
            self.assertEqual(status, "ok")
            assert record is not None
            record_with_evidence = benchmark_builder._record_with_item_evidence(
                record,
                benchmark_builder.CrossrefItemEvidence(
                    payload["message"]["items"][0], 0, response_evidence
                ),
            )
            records, provenance, leaves, tree = (
                benchmark_builder._verify_acquisition_evidence(
                    [record_with_evidence],
                    cache_dir=client.cache_dir,
                    venues=[venue],
                    issn_index=issn_index,
                    window=window,
                    min_abstract_chars=100,
                    request_ledger=ledger,
                    request_budget_id="evaluator-compatibility-budget",
                    hard_http_attempt_ceiling=2,
                    budget_binding_path=client.budget_binding_path,
                    budget_registry_claim_path=client.budget_registry_claim_path,
                    request_highwater_path=client.request_highwater_path,
                    global_usage_path=client.global_usage_path,
                    mailto="test@example.org",
                    bulk_rows=1,
                    rows_per_journal=1,
                    require_complete=True,
                )
            )
            output = root / "formal-bundle"
            manifest = {
                "schema_version": 1,
                "builder": "scripts/build_recent_journal_benchmark.py",
                "builder_source": tree["builder_source"],
                "configuration": {
                    "mailto": "test@example.org",
                    "bulk_rows": 1,
                    "rows_per_journal": 1,
                },
                "dataset": {
                    "path": "dataset.jsonl",
                    "record_count": 1,
                    "sha256": "pending",
                    "complete": True,
                },
            }
            benchmark_builder._finalize_benchmark_outputs(
                output,
                records,
                manifest,
                allow_incomplete=False,
                provenance_rows=provenance,
                cache_evidence_leaves=leaves,
                cache_evidence_tree=tree,
            )
            builder_payload = json.loads(
                (output / "manifest.json").read_text(encoding="utf-8")
            )
            with mock.patch.object(
                benchmark_builder, "load_jcr_venues", return_value=([venue], {})
            ):
                audit = evaluator._validate_formal_acquisition_evidence(
                    builder_payload,
                    builder_manifest=output / "manifest.json",
                    dataset=output / "dataset.jsonl",
                    dataset_sha256=evaluator._file_sha256(output / "dataset.jsonl"),
                    raw_rows=records,
                    expected_count=1,
                    acquisition_window=window,
                    min_abstract_chars=100,
                )
            self.assertEqual(audit["provenance_record_count"], 1)
            self.assertEqual(audit["http_attempt_prefix_count"], 1)
            self.assertIn("crossref_request_highwater", audit["source_files"])
            self.assertIn("crossref_request_global_usage", audit["source_files"])
            acquisition_sources, verified_sources = (
                evaluator._formal_acquisition_source_bindings(
                    {"acquisition_evidence": audit}
                )
            )
            closeout_plan, closeout_sources = evaluator._source_evidence_plan(
                output / "dataset.jsonl",
                builder_manifest=output / "manifest.json",
                authorization_grant=None,
                additional_sources=acquisition_sources,
            )
            evaluator._assert_formal_acquisition_sources_match_plan(
                verified_sources, acquisition_sources, closeout_plan
            )
            evaluator._assert_source_evidence_sources_match_plan(
                closeout_plan, closeout_sources
            )
            closeout_root = root / "evaluator-closeout"
            (closeout_root / evaluator.SOURCE_EVIDENCE_DIR).mkdir(
                parents=True, mode=0o700
            )
            for name, source in closeout_sources.items():
                evaluator._clone_bound_source_evidence_file(
                    name,
                    source,
                    closeout_root / closeout_plan[name]["path"],
                    closeout_plan[name],
                )
            closeout_snapshot = evaluator._source_evidence_integrity_snapshot(
                closeout_root, closeout_plan
            )
            self.assertEqual(set(closeout_snapshot), set(closeout_plan))

            unexpected_nested_field = json.loads(json.dumps(builder_payload))
            unexpected_nested_field["acquisition_evidence"]["provenance"][
                "unexpected"
            ] = "must-fail-closed"
            with mock.patch.object(
                benchmark_builder, "load_jcr_venues", return_value=([venue], {})
            ), self.assertRaisesRegex(
                evaluator.EvaluationError, "nested binding schema is not exact"
            ):
                evaluator._validate_formal_acquisition_evidence(
                    unexpected_nested_field,
                    builder_manifest=output / "manifest.json",
                    dataset=output / "dataset.jsonl",
                    dataset_sha256=evaluator._file_sha256(output / "dataset.jsonl"),
                    raw_rows=records,
                    expected_count=1,
                    acquisition_window=window,
                    min_abstract_chars=100,
                )
            os.rename(
                output / "request-ledger-highwater-prefix.jsonl",
                output / "request-ledger-highwater-prefix.preserved-missing.jsonl",
            )
            with mock.patch.object(
                benchmark_builder, "load_jcr_venues", return_value=([venue], {})
            ), self.assertRaisesRegex(
                evaluator.EvaluationError, "formal evidence is missing/unsafe"
            ):
                evaluator._validate_formal_acquisition_evidence(
                    builder_payload,
                    builder_manifest=output / "manifest.json",
                    dataset=output / "dataset.jsonl",
                    dataset_sha256=evaluator._file_sha256(output / "dataset.jsonl"),
                    raw_rows=records,
                    expected_count=1,
                    acquisition_window=window,
                    min_abstract_chars=100,
                )

    def test_verified_acquisition_source_replacement_fails_plan_and_clone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset.jsonl"
            dataset.write_bytes(b'{"fixture":true}\n')
            source = root / "verified-source.json"
            original = b'{"evidence":"original"}\n'
            replacement = b'{"evidence":"tampered"}\n'
            self.assertEqual(len(original), len(replacement))
            source.write_bytes(original)
            digest = hashlib.sha256(original).hexdigest()
            sources, verified = evaluator._formal_acquisition_source_bindings(
                {
                    "acquisition_evidence": {
                        "source_files": {
                            "crossref_fixture": {
                                "path": str(source.absolute()),
                                "sha256": digest,
                                "bytes": len(original),
                            }
                        }
                    }
                }
            )
            plan, _plan_sources = evaluator._source_evidence_plan(
                dataset,
                builder_manifest=None,
                authorization_grant=None,
                additional_sources=sources,
            )
            evaluator._assert_formal_acquisition_sources_match_plan(
                verified, sources, plan
            )

            staged_replacement = root / "replacement.json"
            staged_replacement.write_bytes(replacement)
            os.replace(staged_replacement, source)
            replaced_plan, _ = evaluator._source_evidence_plan(
                dataset,
                builder_manifest=None,
                authorization_grant=None,
                additional_sources=sources,
            )
            with self.assertRaisesRegex(
                evaluator.EvaluationError, "no longer matches verified binding"
            ):
                evaluator._assert_formal_acquisition_sources_match_plan(
                    verified, sources, replaced_plan
                )

            source.write_bytes(original)
            destination = root / "cloned-source.json"
            real_clone = evaluator._clone_private_regular_file

            def clone_then_replace(clone_source: Path, clone_destination: Path) -> None:
                real_clone(clone_source, clone_destination)
                post_clone_replacement = root / "post-clone-replacement.json"
                post_clone_replacement.write_bytes(replacement)
                os.replace(post_clone_replacement, clone_source)

            with mock.patch.object(
                evaluator,
                "_clone_private_regular_file",
                side_effect=clone_then_replace,
            ), self.assertRaisesRegex(
                evaluator.EvaluationError, "source evidence drifted"
            ):
                evaluator._clone_bound_source_evidence_file(
                    "crossref_fixture",
                    source,
                    destination,
                    plan["crossref_fixture"],
                )


class GoldMatchingTests(unittest.TestCase):
    def test_builder_schema_maps_broad_field(self) -> None:
        case = case_from_payload(
            {
                "paper_id": "doi:10.1/example",
                "doi": "10.1/example",
                "title": "A title",
                "abstract": "A sufficiently informative abstract.",
                "gold_entity_id": 4,
                "gold_journal_name": "Journal of Tests",
                "gold_issns": ["1234-5678"],
                "gold_jcr_quartile": "Q2",
                "broad_field": "clinical_medicine",
            },
            1,
        )
        self.assertEqual(case.case_id, "doi:10.1/example")
        self.assertEqual(case.primary_field, "clinical_medicine")

    def test_entity_id_is_preferred_over_same_name(self) -> None:
        self.assertFalse(
            prediction_matches_gold(
                {"entity_id": 8, "name": "Journal of Tests"},
                7,
                "Journal of Tests",
            )
        )

    def test_missing_entity_ids_never_fall_back_to_name(self) -> None:
        self.assertFalse(
            prediction_matches_gold(
                {"name": "Signal & Image Journal"},
                None,
                "Signal and Image Journal",
            )
        )
        self.assertFalse(
            prediction_matches_gold(
                {"name": "Gold"},
                7,
                "Gold",
            )
        )

    def test_gold_rank_uses_first_match(self) -> None:
        predictions = [
            {"entity_id": 1, "name": "Other"},
            {"entity_id": 7, "name": "Gold"},
            {"entity_id": 7, "name": "Gold"},
        ]
        self.assertEqual(gold_rank(predictions, 7, "Gold"), 2)


class MetricTests(unittest.TestCase):
    def test_ranking_metrics_count_missing_as_misses(self) -> None:
        metrics = ranking_metrics([1, 3, 8, None])
        self.assertEqual(metrics["hits_at_1"], 1)
        self.assertEqual(metrics["hit_at_3"], 0.5)
        self.assertEqual(metrics["hit_at_10"], 0.75)
        self.assertAlmostEqual(metrics["mrr_at_10"], (1 + 1 / 3 + 1 / 8) / 4)

    def test_summary_keeps_errors_and_uncovered_cases_in_denominator(self) -> None:
        records = [
            {
                "status": "ok",
                "catalog_covered": True,
                "final_gold_rank": 1,
                "preliminary_gold_rank": 2,
                "recall_pool_gold_rank": 1,
                "latency_ms": 100,
                "preliminary_latency_ms": 40,
                "leakage": {
                    "any_leak": False,
                    "article_leak": False,
                    "gold_journal_mentioned": False,
                },
            },
            {
                "status": "error",
                "catalog_covered": True,
                "final_gold_rank": None,
                "preliminary_gold_rank": None,
                "recall_pool_gold_rank": None,
            },
            {
                "status": "ok",
                "catalog_covered": False,
                "final_gold_rank": None,
                "preliminary_gold_rank": None,
                "recall_pool_gold_rank": None,
                "latency_ms": 300,
                "leakage": {
                    "any_leak": True,
                    "article_leak": True,
                    "gold_journal_mentioned": False,
                },
            },
        ]
        summary = summarize_records(records)
        self.assertEqual(summary["catalog_covered"], 2)
        self.assertEqual(summary["errors"], 1)
        self.assertAlmostEqual(summary["final"]["hit_at_1"], 1 / 3)
        self.assertAlmostEqual(
            summary["coverage_conditioned"]["final"]["hit_at_1"], 1 / 2
        )
        self.assertAlmostEqual(summary["recall_pool"]["hit_at_40"], 1 / 3)
        self.assertEqual(summary["no_search_leak"]["case_count"], 1)
        self.assertEqual(summary["no_search_leak"]["final"]["hit_at_1"], 1.0)
        self.assertAlmostEqual(
            summary["search_leakage_safe_lower_bound"]["final"]["hit_at_1"],
            1 / 3,
        )
        self.assertEqual(summary["latency_ms"]["median"], 200)

    def test_partial_summary_counts_missing_case_tracks_as_errors(self) -> None:
        summary = build_summary(
            [],
            run_id="test",
            dataset=Path("dataset.jsonl"),
            dataset_sha256="abc",
            expected_case_count=2,
            tracks=("abstract_only",),
            preliminary_k=40,
            interrupted=True,
            expected_case_ids=("a", "b"),
        )
        result = summary["track_results"]["abstract_only"]
        self.assertEqual(result["case_count"], 2)
        self.assertEqual(result["errors"], 2)

class LeakageAuditTests(unittest.TestCase):
    def test_title_similarity_is_normalized(self) -> None:
        self.assertGreaterEqual(
            normalized_title_similarity(
                "Graph-based Learning: A Study", "Graph Based Learning -- A Study"
            ),
            0.95,
        )

    def test_audit_detects_doi_title_and_journal(self) -> None:
        evidence = [
            {
                "title": "Graph-based Learning: A Study",
                "url": "https://doi.org/10.1000/test.7",
                "snippet": "Published in Journal of Graph Tests",
                "query": "graph learning journal",
            }
        ]
        audit = audit_search_leakage(
            evidence,
            doi="10.1000/TEST.7",
            gold_journal_name="Journal of Graph Tests",
            paper_title="Graph Based Learning - A Study",
        )
        self.assertTrue(audit["article_leak"])
        self.assertTrue(audit["gold_journal_mentioned"])
        self.assertEqual(audit["reason_counts"], {"doi": 1, "title": 1, "gold_journal": 1})

    def test_topical_evidence_without_identity_is_not_a_leak(self) -> None:
        audit = audit_search_leakage(
            [
                {
                    "title": "Aims and scope for machine learning methods",
                    "url": "https://example.org/scope",
                    "snippet": "Graph learning and optimization",
                }
            ],
            doi="10.1000/test.7",
            gold_journal_name="Journal of Graph Tests",
            paper_title="Graph Based Learning - A Study",
        )
        self.assertFalse(audit["any_leak"])


class EvaluatorExecutionSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.dataset = self.root / "dataset.jsonl"
        self.dataset.write_text(
            json.dumps(
                {
                    "case_id": "case-1",
                    "doi": "10.1/example",
                    "title": "A safe evaluator test",
                    "abstract": "An abstract used only by the local unit test.",
                    "gold_journal_name": "Journal of Tests",
                    "gold_issns": ["1234-5678"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.api_config = self.root / "api.json"
        self.api_config.write_text(
            json.dumps(
                {
                    "llm": {
                        "provider": "openai_compatible",
                        "base_url": "https://llm.invalid/v1",
                        "model": "test",
                        "max_retries": 0,
                    },
                    "embedding": {
                        "provider": "openai_compatible",
                        "base_url": "https://embedding.invalid/v1",
                        "model": "test",
                        "max_retries": 0,
                    },
                    "search": {
                        "provider": "brave",
                        "api_key": "unit-test-placeholder",
                    },
                }
            ),
            encoding="utf-8",
        )
        self.api_cache_seed = self.root / "api-cache-seed"
        (self.api_cache_seed / "llm").mkdir(parents=True)
        (self.api_cache_seed / "search").mkdir()
        (self.api_cache_seed / "llm" / "seed.json").write_text(
            '{"seed":true}\n', encoding="utf-8"
        )
        self.query_cache_seed = self.root / "query-seed.json.gz"
        self.query_cache_seed.write_bytes(b"synthetic-query-seed")
        self.lightrag_cache_seed = self.root / "lightrag-seed.json.gz"
        self.lightrag_cache_seed.write_bytes(b"synthetic-lightrag-seed")
        self.lightrag_working_dir_seed = self.root / "lightrag-workspace-seed"
        self.lightrag_working_dir_seed.mkdir()
        for name in evaluator.LIGHTRAG_WORKSPACE_FILES:
            (self.lightrag_working_dir_seed / name).write_text(
                json.dumps({"synthetic": name}) + "\n", encoding="utf-8"
            )
        self.registry = self.root / "authorization-registry"
        self.registry_patch = mock.patch.object(
            evaluator, "DEFAULT_AUTHORIZATION_REGISTRY_DIR", self.registry
        )
        self.registry_patch.start()

    def tearDown(self) -> None:
        self.registry_patch.stop()
        self.temporary.cleanup()

    def argv(self, output: Path, *extra: str) -> list[str]:
        return [
            "--dataset",
            str(self.dataset),
            "--output-dir",
            str(output),
            "--api-config",
            str(self.api_config),
            "--track",
            "abstract_only",
            "--api-cache-seed-dir",
            str(self.api_cache_seed),
            "--query-embedding-cache-seed",
            str(self.query_cache_seed),
            "--lightrag-embedding-cache-seed",
            str(self.lightrag_cache_seed),
            "--lightrag-working-dir-seed",
            str(self.lightrag_working_dir_seed),
            *extra,
        ]

    def reviewed_digest(self, output: Path, *controls: str) -> str:
        rendered = StringIO()
        with redirect_stdout(rendered):
            self.assertEqual(
                evaluator.main(self.argv(output, "--dry-run", *controls)), 0
            )
        return str(json.loads(rendered.getvalue())["reviewed_plan_digest"])

    @staticmethod
    def live_controls(reference: str = "unit-test-authorization") -> tuple[str, ...]:
        return (
            "--authorization-reference",
            reference,
            "--external-call-budget",
            "2",
            "--external-attempt-cost-ceiling-usd",
            "0",
            "--authorized-max-cost-usd",
            "0",
        )

    def local_only_patches(self):
        return (
            mock.patch.object(
                evaluator, "resolve_gold_entity_ids", side_effect=lambda cases: cases
            ),
            mock.patch.object(
                evaluator,
                "make_run_id",
                side_effect=lambda dataset, *_args, **_kwargs: (
                    "unit-test-run",
                    evaluator._file_sha256(Path(dataset)),
                ),
            ),
        )

    @staticmethod
    def fake_worker_class():
        class FakeWorker:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.preload_ms = 0
                self.process = None

            def start(self):
                return None

            def close(self):
                return None

        return FakeWorker

    def configure_synthetic_tavily_pool(self, *, quota_per_key: int = 2):
        from where_paper_go.tavily_pool import TavilyKeyPool

        state_file = self.root / "tavily-state.json"
        pool = TavilyKeyPool(
            ["tvly-synthetic-unit-test-key"],
            quota_per_key=quota_per_key,
            state_file=state_file,
        )
        pool.summary()
        config = json.loads(self.api_config.read_text(encoding="utf-8"))
        config["search"] = {
            "provider": "tavily",
            "api_keys": ["tvly-synthetic-unit-test-key"],
            "quota_per_key": quota_per_key,
            "key_pool_state_file": str(state_file),
        }
        self.api_config.write_text(json.dumps(config), encoding="utf-8")
        return pool, state_file

    def test_dry_run_is_read_only_and_never_constructs_worker(self) -> None:
        output = self.root / "new-output"
        patches = self.local_only_patches()
        with patches[0], patches[1], mock.patch.object(
            evaluator.PersistentWorker,
            "__init__",
            side_effect=AssertionError("dry-run constructed worker"),
        ), mock.patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("dry-run opened HTTP transport"),
        ):
            rendered = StringIO()
            with redirect_stdout(rendered):
                result = evaluator.main(self.argv(output, "--dry-run"))
        plan = json.loads(rendered.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(plan["network_calls_made"], 0)
        self.assertFalse(plan["live_clients_instantiated"])
        self.assertEqual(plan["output"]["pending_case_tracks"], 1)
        self.assertRegex(plan["selection_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(plan["api_config_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(plan["reviewed_plan_digest"], r"^[0-9a-f]{64}$")
        self.assertIn("diagnostic_nonformal", plan["claim_status"])
        self.assertFalse(output.exists())
        self.assertFalse(self.registry.exists())

    def test_existing_output_requires_explicit_resume(self) -> None:
        output = self.root / "existing"
        output.mkdir()
        patches = self.local_only_patches()
        with patches[0], patches[1], self.assertRaisesRegex(
            SystemExit, "refusing overwrite"
        ):
            evaluator.main(self.argv(output, "--dry-run"))

    def test_live_requires_authorization_budget_and_cost_bounds_before_writes(self) -> None:
        output = self.root / "unauthorized"
        patches = self.local_only_patches()
        with patches[0], patches[1], mock.patch.object(
            evaluator.PersistentWorker,
            "__init__",
            side_effect=AssertionError("unauthorized run constructed worker"),
        ), self.assertRaisesRegex(SystemExit, "live evaluation refused"):
            evaluator.main(self.argv(output))
        self.assertFalse(output.exists())

    def test_tavily_shared_usage_is_monotonic_across_resume_closeouts(self) -> None:
        pool, _state_file = self.configure_synthetic_tavily_pool()
        output = self.root / "tavily-monotonic-output"
        controls = self.live_controls("quota-monotonic-20260830")
        FakeWorker = self.fake_worker_class()

        def fake_error(_worker, case, track, *, run_id, **_kwargs):
            return {
                "schema_version": evaluator.SCHEMA_VERSION,
                "run_id": run_id,
                "case_id": case.case_id,
                "track": track,
                **case.public_metadata(),
                "status": "error",
                "error": "synthetic no-network result",
            }

        patches = self.local_only_patches()
        with patches[0], patches[1], mock.patch.object(
            evaluator, "PersistentWorker", FakeWorker
        ), mock.patch.object(evaluator, "evaluate_case", side_effect=fake_error):
            digest = self.reviewed_digest(output, *controls)
            live = (*controls, "--reviewed-plan-digest", digest)
            self.assertEqual(evaluator.main(self.argv(output, *live)), 3)

        first = json.loads(
            (output / "closeout.generation-000001.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(first["shared_external_quota_final"]["used"], 0)

        pool.acquire()
        patches = self.local_only_patches()
        with patches[0], patches[1], mock.patch.object(
            evaluator, "PersistentWorker", FakeWorker
        ):
            self.assertEqual(
                evaluator.main(self.argv(output, "--resume", *live)),
                3,
            )
        second = json.loads(
            (output / "closeout.generation-000002.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(second["shared_external_quota_final"]["used"], 1)
        self.assertGreaterEqual(
            second["shared_external_quota_final"]["state_revision"],
            first["shared_external_quota_final"]["state_revision"],
        )

    def test_tavily_valid_newer_usage_rollback_is_rejected_on_resume(self) -> None:
        pool, state_file = self.configure_synthetic_tavily_pool()
        pool.acquire()
        output = self.root / "tavily-rollback-output"
        controls = self.live_controls("quota-rollback-20260830")
        FakeWorker = self.fake_worker_class()

        def fake_error(_worker, case, track, *, run_id, **_kwargs):
            return {
                "schema_version": evaluator.SCHEMA_VERSION,
                "run_id": run_id,
                "case_id": case.case_id,
                "track": track,
                **case.public_metadata(),
                "status": "error",
                "error": "synthetic no-network result",
            }

        patches = self.local_only_patches()
        with patches[0], patches[1], mock.patch.object(
            evaluator, "PersistentWorker", FakeWorker
        ), mock.patch.object(evaluator, "evaluate_case", side_effect=fake_error):
            digest = self.reviewed_digest(output, *controls)
            live = (*controls, "--reviewed-plan-digest", digest)
            self.assertEqual(evaluator.main(self.argv(output, *live)), 3)

        rolled_back = json.loads(state_file.read_text(encoding="utf-8"))
        rolled_back["state_revision"] += 1
        for record in rolled_back["keys"].values():
            record["used"] = 0
            record["status"] = "active"
            record["last_event"] = "configured"
        encoded = json.dumps(rolled_back, sort_keys=True) + "\n"
        for path in (
            state_file,
            state_file.with_name(state_file.name + ".bak"),
        ):
            path.write_text(encoded, encoding="utf-8")
            path.chmod(0o600)

        patches = self.local_only_patches()
        with patches[0], patches[1], self.assertRaisesRegex(
            SystemExit, "Tavily quota used moved backwards"
        ):
            evaluator.main(
                self.argv(
                    output,
                    "--resume",
                    "--dry-run",
                    *live,
                )
            )
        self.assertFalse(
            (output / evaluator.GENERATION_DIR / "generation-000002.jsonl").exists()
        )

    def test_tavily_degraded_single_copy_is_never_live_ready(self) -> None:
        _pool, state_file = self.configure_synthetic_tavily_pool()
        backup = state_file.with_name(state_file.name + ".bak")
        backup.unlink()
        primary_before = state_file.read_bytes()
        output = self.root / "tavily-degraded-output"
        controls = self.live_controls("quota-degraded-20260830")

        patches = self.local_only_patches()
        with patches[0], patches[1]:
            digest = self.reviewed_digest(output, *controls)
        rendered = StringIO()
        patches = self.local_only_patches()
        with patches[0], patches[1], redirect_stdout(rendered):
            self.assertEqual(
                evaluator.main(
                    self.argv(
                        output,
                        "--dry-run",
                        *controls,
                        "--reviewed-plan-digest",
                        digest,
                    )
                ),
                0,
            )
        plan = json.loads(rendered.getvalue())
        self.assertEqual(
            plan["quota"]["current_observation"]["state_status"],
            "readable_degraded_fail_closed",
        )
        self.assertFalse(plan["live_control_ready"])
        self.assertTrue(
            any(
                "Tavily shared quota ledger" in reason
                for reason in plan["live_missing_or_invalid_controls"]
            )
        )
        self.assertEqual(state_file.read_bytes(), primary_before)
        self.assertFalse(backup.exists())
        self.assertFalse(output.exists())
        self.assertFalse(self.registry.exists())

    def test_source_evidence_drift_between_plan_and_claim_fails_closed(self) -> None:
        output = self.root / "source-evidence-drift-output"
        controls = self.live_controls("source-drift-20260830")
        patches = self.local_only_patches()
        with patches[0], patches[1]:
            digest = self.reviewed_digest(output, *controls)

        real_source_evidence_plan = evaluator._source_evidence_plan
        calls = 0

        def drift_on_second_snapshot(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.dataset.write_bytes(self.dataset.read_bytes() + b"\n")
            return real_source_evidence_plan(*args, **kwargs)

        patches = self.local_only_patches()
        with patches[0], patches[1], mock.patch.object(
            evaluator,
            "_source_evidence_plan",
            side_effect=drift_on_second_snapshot,
        ), self.assertRaisesRegex(
            SystemExit, "source evidence drifted after plan review"
        ):
            evaluator.main(
                self.argv(
                    output,
                    *controls,
                    "--reviewed-plan-digest",
                    digest,
                )
            )
        self.assertEqual(calls, 2)
        self.assertFalse(output.exists())
        self.assertFalse(self.registry.exists())

    def test_run_manifest_mutation_never_publishes_a_closeout(self) -> None:
        output = self.root / "run-manifest-drift-output"
        controls = self.live_controls("manifest-drift-20260830")
        FakeWorker = self.fake_worker_class()
        patches = self.local_only_patches()
        with patches[0], patches[1]:
            digest = self.reviewed_digest(output, *controls)

        def mutate_manifest(_worker, case, track, *, run_id, **_kwargs):
            manifest_path = output / evaluator.RUN_MANIFEST_FILE
            manifest_path.chmod(0o600)
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["synthetic_mid_run_mutation"] = True
            manifest_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            manifest_path.chmod(0o444)
            return {
                "schema_version": evaluator.SCHEMA_VERSION,
                "run_id": run_id,
                "case_id": case.case_id,
                "track": track,
                **case.public_metadata(),
                "status": "ok",
                "catalog_covered": True,
                "final_gold_rank": 1,
                "preliminary_gold_rank": 1,
                "recall_pool_gold_rank": 1,
                "latency_ms": 1,
                "preliminary_latency_ms": 1,
                "leakage": {},
            }

        patches = self.local_only_patches()
        try:
            with patches[0], patches[1], mock.patch.object(
                evaluator, "PersistentWorker", FakeWorker
            ), mock.patch.object(
                evaluator, "evaluate_case", side_effect=mutate_manifest
            ):
                result = evaluator.main(
                    self.argv(
                        output,
                        *controls,
                        "--reviewed-plan-digest",
                        digest,
                    )
                )
        except (evaluator.EvaluationError, SystemExit) as exc:
            self.assertIn("run manifest drifted", str(exc))
        else:
            self.assertEqual(result, 3)

        self.assertFalse((output / "closeout.generation-000001.json").exists())
        self.assertEqual(list(self.registry.glob("*.anchor.json")), [])
        self.assertTrue(
            json.loads(
                (output / evaluator.RUN_MANIFEST_FILE).read_text(encoding="utf-8")
            )["synthetic_mid_run_mutation"]
        )

    def test_authorization_reference_rejects_credential_shaped_values(self) -> None:
        for value in (
            "Bearer secret-value",
            "token=secret-value",
            "api_key: secret-value",
            "sk-proj-A1b2C3d4E5f6G7h8I9j0K1l2",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature12345678",
            "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0",
        ):
            with self.subTest(value=value), self.assertRaisesRegex(
                SystemExit, "non-secret"
            ):
                evaluator.main(
                    self.argv(
                        self.root / ("rejected-" + str(len(value))),
                        "--dry-run",
                        "--authorization-reference",
                        value,
                    )
                )

    def test_fresh_live_control_files_are_exclusive_and_resume_is_verified(self) -> None:
        output = self.root / "controlled"

        class FakeWorker:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.preload_ms = 0
                self.process = None

            def start(self):
                return None

            def close(self):
                return None

        def fake_evaluate(_worker, case, track, *, run_id, **_kwargs):
            return {
                "schema_version": evaluator.SCHEMA_VERSION,
                "run_id": run_id,
                "case_id": case.case_id,
                "track": track,
                "status": "error",
                "catalog_covered": False,
                "error": "synthetic no-network unit-test result",
            }

        controls = (
            "--authorization-reference",
            "unit-test-authorization",
            "--external-call-budget",
            "2",
            "--external-attempt-cost-ceiling-usd",
            "0",
            "--authorized-max-cost-usd",
            "0",
        )
        patches = self.local_only_patches()
        with patches[0], patches[1], mock.patch.object(
            evaluator, "PersistentWorker", FakeWorker
        ), mock.patch.object(evaluator, "evaluate_case", side_effect=fake_evaluate):
            digest = self.reviewed_digest(output, *controls)
            self.assertEqual(
                evaluator.main(
                    self.argv(output, *controls, "--reviewed-plan-digest", digest)
                ),
                3,
            )
        manifest = json.loads((output / evaluator.RUN_MANIFEST_FILE).read_text())
        self.assertEqual(manifest["external_call_budget"], 2)
        self.assertEqual(manifest["maximum_estimated_cost_usd"], "0")
        self.assertEqual(manifest["approved_plan_digest"], digest)
        self.assertEqual(
            manifest["authorization_registry"]["registry_identity"],
            evaluator.external_call_ledger_status(
                Path(manifest["authorization_registry"]["global_ledger"])
            )["run_id"],
        )
        self.assertEqual(
            (output / evaluator.RUN_MANIFEST_FILE).stat().st_mode & 0o777,
            0o444,
        )
        ledger = Path(manifest["authorization_registry"]["global_ledger"])
        self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)
        self.assertFalse((output / "external_call_ledger.jsonl").exists())
        self.assertEqual(
            stat.S_IMODE((output / evaluator.RUN_MANIFEST_FILE).stat().st_mode),
            0o444,
        )
        self.assertEqual(
            evaluator.external_call_ledger_status(ledger)["used"],
            0,
        )
        self.assertTrue((output / "raw_segments" / "generation-000001.jsonl").is_file())
        self.assertTrue((output / "summary.generation-000001.json").is_file())
        self.assertTrue((output / "closeout.generation-000001.json").is_file())
        self.assertTrue((output / "runtime_cache" / "api_config.snapshot.json").is_file())
        runtime_query = output / "runtime_cache" / "query_embedding_cache.json.gz"
        runtime_lightrag = output / "runtime_cache" / "lightrag_embedding_cache.json.gz"
        self.assertEqual(runtime_query.read_bytes(), self.query_cache_seed.read_bytes())
        self.assertEqual(runtime_lightrag.read_bytes(), self.lightrag_cache_seed.read_bytes())
        self.assertNotEqual(runtime_query.stat().st_ino, self.query_cache_seed.stat().st_ino)
        self.assertNotEqual(runtime_lightrag.stat().st_ino, self.lightrag_cache_seed.stat().st_ino)
        patches = self.local_only_patches()
        with patches[0], patches[1], mock.patch.object(
            evaluator, "PersistentWorker", FakeWorker
        ):
            self.assertEqual(
                evaluator.main(
                    self.argv(
                        output,
                        "--resume",
                        *controls,
                        "--reviewed-plan-digest",
                        digest,
                    )
                ),
                3,
            )
        self.assertTrue((output / "summary.generation-000002.json").is_file())

        patches = self.local_only_patches()
        with patches[0], patches[1], self.assertRaisesRegex(
            SystemExit, "reviewed-plan-digest does not match"
        ):
            evaluator.main(
                self.argv(
                    output,
                    "--resume",
                    "--authorization-reference",
                    "different-authorization",
                    "--external-call-budget",
                    "2",
                    "--external-attempt-cost-ceiling-usd",
                    "0",
                    "--authorized-max-cost-usd",
                    "0",
                    "--reviewed-plan-digest",
                    digest,
                )
            )

        runtime_query.write_bytes(b"runtime-only-mutation")
        self.assertEqual(self.query_cache_seed.read_bytes(), b"synthetic-query-seed")
        patches = self.local_only_patches()
        with patches[0], patches[1], self.assertRaisesRegex(
            SystemExit, "runtime cache drifted"
        ):
            evaluator.main(
                self.argv(
                    output,
                    "--resume",
                    *controls,
                    "--reviewed-plan-digest",
                    digest,
                )
            )

    def test_worker_receives_exact_limiter_environment(self) -> None:
        ledger = self.root / "ledger.jsonl"
        verified: list[bool] = []
        evaluator.initialize_external_call_ledger(
            ledger, budget=7, run_id="unit-test-run"
        )
        worker = evaluator.PersistentWorker(
            external_call_ledger=ledger,
            external_call_budget=7,
            run_id="unit-test-run",
            api_cache_dir=self.api_cache_seed,
            query_embedding_cache=self.query_cache_seed,
            lightrag_embedding_cache=self.lightrag_cache_seed,
            lightrag_working_dir=self.lightrag_working_dir_seed,
            graph_path=self.root / "venue_graph.json.gz",
            api_config_snapshot=self.api_config,
            verify_bindings=lambda: verified.append(True),
        )
        fake_process = mock.Mock()
        with mock.patch.object(
            evaluator.subprocess, "Popen", return_value=fake_process
        ) as popen, mock.patch.object(
            worker, "_read_message", return_value={"ready": True, "preload_ms": 1}
        ):
            worker.start()
        environment = popen.call_args.kwargs["env"]
        self.assertEqual(environment[evaluator.LEDGER_ENV], str(ledger.resolve()))
        self.assertEqual(environment[evaluator.BUDGET_ENV], "7")
        self.assertEqual(environment[evaluator.RUN_ID_ENV], "unit-test-run")
        self.assertEqual(environment["WPG_API_CACHE_DIR"], str(self.api_cache_seed))
        self.assertEqual(
            environment["WPG_QUERY_EMBEDDING_CACHE"], str(self.query_cache_seed)
        )
        self.assertEqual(
            environment["WPG_LIGHTRAG_EMBEDDING_CACHE"],
            str(self.lightrag_cache_seed),
        )
        self.assertEqual(
            environment["WPG_LIGHTRAG_WORKING_DIR"],
            str(self.lightrag_working_dir_seed),
        )
        self.assertEqual(
            environment["WPG_GRAPH_PATH"],
            str((self.root / "venue_graph.json.gz").resolve()),
        )
        self.assertEqual(environment["WPG_STRICT_GRAPH_READ_ONLY"], "1")
        self.assertEqual(environment["WPG_API_CONFIG"], str(self.api_config))
        deterministic_environment = {
            "PYTHONHASHSEED": "0",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
        }
        self.assertEqual(
            {name: environment[name] for name in deterministic_environment},
            deterministic_environment,
        )
        self.assertEqual(
            evaluator._dependency_environment_snapshot()[
                "worker_determinism_environment"
            ],
            deterministic_environment,
        )
        self.assertEqual(verified, [True])
        worker.process = None

    def _write_authorization_grant(
        self,
        name: str,
        *,
        authorization_reference: str,
        reviewed_plan_digest: str,
        output: Path,
        budget: int,
        attempt_cost: Decimal,
        authorized_cost: Decimal,
        payload_overrides: dict[str, object] | None = None,
    ) -> Path:
        payload: dict[str, object] = {
            "schema_version": evaluator.AUTHORIZATION_GRANT_SCHEMA_VERSION,
            "status": "explicit_external_api_authorization",
            "authorization_reference_sha256": hashlib.sha256(
                authorization_reference.encode("utf-8")
            ).hexdigest(),
            "reviewed_plan_digest": reviewed_plan_digest,
            "output_identity_sha256": hashlib.sha256(
                str(output.resolve()).encode("utf-8")
            ).hexdigest(),
            "evaluation_mode": evaluator.FORMAL_MODE,
            "external_call_budget": budget,
            "external_attempt_cost_ceiling_usd": str(attempt_cost),
            "authorized_max_cost_usd": str(authorized_cost),
            "maximum_estimated_cost_usd": str(attempt_cost * budget),
        }
        payload.update(payload_overrides or {})
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(0o444)
        return path

    def test_formal_authorization_grant_binds_registry_and_manifest(self) -> None:
        reference = "formal-grant-unit-audit-2026-08-30"
        reference_sha256 = hashlib.sha256(reference.encode("utf-8")).hexdigest()
        reviewed_digest = "d" * 64
        output = self.root / "formal-grant-output"
        budget = 15
        attempt_cost = Decimal("0")
        authorized_cost = Decimal("0")
        grant_path = self._write_authorization_grant(
            "formal-grant.json",
            authorization_reference=reference,
            reviewed_plan_digest=reviewed_digest,
            output=output,
            budget=budget,
            attempt_cost=attempt_cost,
            authorized_cost=authorized_cost,
        )

        binding = evaluator._authorization_grant_binding(
            grant_path,
            authorization_reference_sha256=reference_sha256,
            reviewed_plan_digest=reviewed_digest,
            output_dir=output,
            evaluation_mode=evaluator.FORMAL_MODE,
            external_call_budget=budget,
            attempt_cost_ceiling_usd=attempt_cost,
            authorized_max_cost_usd=authorized_cost,
        )
        self.assertEqual(binding["sha256"], evaluator._file_sha256(grant_path))
        self.assertEqual(binding["validation"]["reviewed_plan_digest"], reviewed_digest)
        self.assertNotIn(reference, json.dumps(binding, sort_keys=True))

        registry_binding, _ledger = evaluator._claim_or_verify_authorization_registry(
            self.registry,
            authorization_reference=reference,
            approved_plan_digest=reviewed_digest,
            budget=budget,
            attempt_cost_ceiling_usd=attempt_cost,
            authorized_max_cost_usd=authorized_cost,
            authorization_grant=binding,
            retry_errors=False,
        )
        registry_entry = json.loads(
            Path(registry_binding["registry_entry"]).read_text(encoding="utf-8")
        )
        self.assertEqual(registry_entry["authorization_grant_sha256"], binding["sha256"])
        self.assertNotIn(reference, json.dumps(registry_entry, sort_keys=True))

        cases = evaluator.load_dataset(self.dataset)
        manifest = evaluator._run_manifest_payload(
            run_id="formal-grant-unit-run",
            dataset=self.dataset,
            dataset_sha256=evaluator._file_sha256(self.dataset),
            cases=cases,
            tracks=("abstract_only",),
            api_config=self.api_config,
            authorization_reference_sha256=reference_sha256,
            external_call_budget=budget,
            attempt_cost_ceiling_usd=attempt_cost,
            authorized_max_cost_usd=authorized_cost,
            evaluation_mode=evaluator.FORMAL_MODE,
            approved_plan_digest=reviewed_digest,
            runtime_bindings={},
            cache_seeds={},
            authorization_registry=registry_binding,
            builder_manifest={},
            authorization_grant=binding,
            retry_errors=False,
        )
        self.assertEqual(manifest["authorization_grant"]["sha256"], binding["sha256"])
        self.assertNotIn(reference, json.dumps(manifest, sort_keys=True))

    def test_formal_authorization_grant_rejects_wrong_plan_bindings_and_symlink(
        self,
    ) -> None:
        reference = "formal-grant-negative-audit-2026-08-30"
        reference_sha256 = hashlib.sha256(reference.encode("utf-8")).hexdigest()
        reviewed_digest = "e" * 64
        output = self.root / "formal-negative-output"
        budget = 15
        attempt_cost = Decimal("0")
        authorized_cost = Decimal("0")
        mutations = {
            "output": {
                "output_identity_sha256": "0" * 64,
            },
            "digest": {
                "reviewed_plan_digest": "f" * 64,
            },
            "budget": {
                "external_call_budget": budget + 1,
            },
            "mode": {
                "evaluation_mode": evaluator.DIAGNOSTIC_MODE,
            },
        }
        for name, override in mutations.items():
            with self.subTest(binding=name):
                path = self._write_authorization_grant(
                    f"wrong-{name}.json",
                    authorization_reference=reference,
                    reviewed_plan_digest=reviewed_digest,
                    output=output,
                    budget=budget,
                    attempt_cost=attempt_cost,
                    authorized_cost=authorized_cost,
                    payload_overrides=override,
                )
                with self.assertRaisesRegex(
                    evaluator.EvaluationError,
                    "does not bind this exact plan/output/budget",
                ):
                    evaluator._authorization_grant_binding(
                        path,
                        authorization_reference_sha256=reference_sha256,
                        reviewed_plan_digest=reviewed_digest,
                        output_dir=output,
                        evaluation_mode=evaluator.FORMAL_MODE,
                        external_call_budget=budget,
                        attempt_cost_ceiling_usd=attempt_cost,
                        authorized_max_cost_usd=authorized_cost,
                    )

        target = self._write_authorization_grant(
            "real-grant.json",
            authorization_reference=reference,
            reviewed_plan_digest=reviewed_digest,
            output=output,
            budget=budget,
            attempt_cost=attempt_cost,
            authorized_cost=authorized_cost,
        )
        symlink = self.root / "grant-symlink.json"
        symlink.symlink_to(target)
        with self.assertRaisesRegex(
            evaluator.EvaluationError, "must be a regular file"
        ):
            evaluator._authorization_grant_binding(
                symlink,
                authorization_reference_sha256=reference_sha256,
                reviewed_plan_digest=reviewed_digest,
                output_dir=output,
                evaluation_mode=evaluator.FORMAL_MODE,
                external_call_budget=budget,
                attempt_cost_ceiling_usd=attempt_cost,
                authorized_max_cost_usd=authorized_cost,
            )

    def test_formal_authorization_grant_rejects_extra_fields(self) -> None:
        reference = "formal-grant-extra-field-audit-2026-08-30"
        reviewed_digest = "c" * 64
        output = self.root / "formal-extra-field-output"
        grant = self._write_authorization_grant(
            "formal-extra-field-grant.json",
            authorization_reference=reference,
            reviewed_plan_digest=reviewed_digest,
            output=output,
            budget=15,
            attempt_cost=Decimal("0"),
            authorized_cost=Decimal("0"),
            payload_overrides={
                "unexpected_api_key": "synthetic-value-must-not-be-copied"
            },
        )
        with self.assertRaisesRegex(
            evaluator.EvaluationError,
            "does not bind this exact plan/output/budget",
        ):
            evaluator._authorization_grant_binding(
                grant,
                authorization_reference_sha256=hashlib.sha256(
                    reference.encode("utf-8")
                ).hexdigest(),
                reviewed_plan_digest=reviewed_digest,
                output_dir=output,
                evaluation_mode=evaluator.FORMAL_MODE,
                external_call_budget=15,
                attempt_cost_ceiling_usd=Decimal("0"),
                authorized_max_cost_usd=Decimal("0"),
            )
        self.assertFalse(output.exists())
        self.assertFalse(self.registry.exists())

    def test_raw_authorization_reference_is_absent_from_all_run_outputs(self) -> None:
        output = self.root / "redacted-reference-output"
        reference = "audit-ref-redaction-3029"
        controls = self.live_controls(reference)
        FakeWorker = self.fake_worker_class()

        def fake_error(_worker, case, track, *, run_id, **_kwargs):
            return {
                "schema_version": evaluator.SCHEMA_VERSION,
                "run_id": run_id,
                "case_id": case.case_id,
                "track": track,
                **case.public_metadata(),
                "status": "error",
                "error": "synthetic no-network result",
            }

        patches = self.local_only_patches()
        rendered = StringIO()
        with patches[0], patches[1], redirect_stdout(rendered):
            self.assertEqual(
                evaluator.main(self.argv(output, "--dry-run", *controls)), 0
            )
        plan_text = rendered.getvalue()
        digest = json.loads(plan_text)["reviewed_plan_digest"]
        self.assertNotIn(reference, plan_text)

        patches = self.local_only_patches()
        with patches[0], patches[1], mock.patch.object(
            evaluator, "PersistentWorker", FakeWorker
        ), mock.patch.object(evaluator, "evaluate_case", side_effect=fake_error):
            self.assertEqual(
                evaluator.main(
                    self.argv(
                        output,
                        *controls,
                        "--reviewed-plan-digest",
                        digest,
                    )
                ),
                3,
            )
        for name in (
            evaluator.RUN_MANIFEST_FILE,
            "summary.generation-000001.json",
            "closeout.generation-000001.json",
        ):
            with self.subTest(artifact=name):
                self.assertNotIn(reference, (output / name).read_text(encoding="utf-8"))

    def test_global_registry_cannot_be_reset_by_new_output_or_copied_resume(self) -> None:
        first = self.root / "first-output"
        second = self.root / "second-output"
        copied = self.root / "copied-output"
        controls = self.live_controls("global-plan-audit-2026-08-30")
        FakeWorker = self.fake_worker_class()

        def fake_error(_worker, case, track, *, run_id, **_kwargs):
            return {
                "schema_version": evaluator.SCHEMA_VERSION,
                "run_id": run_id,
                "case_id": case.case_id,
                "track": track,
                **case.public_metadata(),
                "status": "error",
                "error": "synthetic",
            }

        patches = self.local_only_patches()
        with patches[0], patches[1], mock.patch.object(
            evaluator, "PersistentWorker", FakeWorker
        ), mock.patch.object(evaluator, "evaluate_case", side_effect=fake_error):
            digest = self.reviewed_digest(first, *controls)
            live = (*controls, "--reviewed-plan-digest", digest)
            self.assertEqual(evaluator.main(self.argv(first, *live)), 3)
            self.assertEqual(evaluator.main(self.argv(second, *live)), 3)

        first_manifest = json.loads((first / evaluator.RUN_MANIFEST_FILE).read_text())
        second_manifest = json.loads((second / evaluator.RUN_MANIFEST_FILE).read_text())
        self.assertEqual(
            first_manifest["authorization_registry"],
            second_manifest["authorization_registry"],
        )
        ledger = Path(first_manifest["authorization_registry"]["global_ledger"])
        self.assertEqual(len(list(self.registry.glob("*.external_call_ledger.jsonl"))), 1)
        self.assertEqual(evaluator.external_call_ledger_status(ledger)["used"], 0)

        shutil.copytree(first, copied)
        patches = self.local_only_patches()
        with patches[0], patches[1], mock.patch.object(
            evaluator, "PersistentWorker", FakeWorker
        ):
            self.assertEqual(
                evaluator.main(self.argv(copied, "--resume", *live)), 3
            )
        self.assertTrue(
            (copied / evaluator.GENERATION_DIR / "generation-000002.jsonl").is_file()
        )
        self.assertEqual(evaluator.external_call_ledger_status(ledger)["used"], 0)

        shard = self.root / "changed-shard-output"
        patches = self.local_only_patches()
        with patches[0], patches[1], mock.patch.object(
            evaluator, "PersistentWorker", FakeWorker
        ):
            shard_digest = self.reviewed_digest(
                shard, *controls, "--track", "title_abstract"
            )
            with self.assertRaisesRegex(
                SystemExit, "already bound to a different approved plan"
            ):
                evaluator.main(
                    self.argv(
                        shard,
                        *controls,
                        "--track",
                        "title_abstract",
                        "--reviewed-plan-digest",
                        shard_digest,
                    )
                )
        self.assertFalse(shard.exists())
        self.assertEqual(len(list(self.registry.glob("*.external_call_ledger.jsonl"))), 1)

    def test_resume_anchors_one_unclean_tail_before_new_generation(self) -> None:
        output = self.root / "tail-output"
        controls = self.live_controls("tail-audit-2026-08-30")
        FakeWorker = self.fake_worker_class()

        def fake_error(_worker, case, track, *, run_id, **_kwargs):
            return {
                "schema_version": evaluator.SCHEMA_VERSION,
                "run_id": run_id,
                "case_id": case.case_id,
                "track": track,
                **case.public_metadata(),
                "status": "error",
                "error": "synthetic",
            }

        patches = self.local_only_patches()
        with patches[0], patches[1], mock.patch.object(
            evaluator, "PersistentWorker", FakeWorker
        ), mock.patch.object(evaluator, "evaluate_case", side_effect=fake_error):
            digest = self.reviewed_digest(output, *controls)
            live = (*controls, "--reviewed-plan-digest", digest)
            self.assertEqual(evaluator.main(self.argv(output, *live)), 3)
        closed_segment = output / evaluator.GENERATION_DIR / "generation-000001.jsonl"
        self.assertEqual(stat.S_IMODE(closed_segment.stat().st_mode), 0o400)
        with self.assertRaises(PermissionError):
            closed_segment.open("ab")
        generation, segment = evaluator._create_generation_segment(output)
        self.assertEqual(generation, 2)
        with segment.open("ab") as handle:
            handle.write(b'{"crash_tail":')
            handle.flush()
            os.fsync(handle.fileno())
        before = segment.read_bytes()

        patches = self.local_only_patches()
        with patches[0], patches[1], mock.patch.object(
            evaluator, "PersistentWorker", FakeWorker
        ):
            self.assertEqual(
                evaluator.main(self.argv(output, "--resume", *live)), 3
            )
        self.assertEqual(segment.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(segment.stat().st_mode), 0o400)
        self.assertTrue((output / "summary.generation-000001.json").exists())
        self.assertFalse((output / "summary.generation-000002.json").exists())
        self.assertTrue((output / "summary.generation-000003.json").exists())
        closeout = json.loads(
            (output / "closeout.generation-000002.json").read_text()
        )
        self.assertEqual(closeout["closure_kind"], "recovered_unclean")
        self.assertEqual(closeout["exit_code"], 3)
        current_audit = closeout["current_segment"]
        self.assertEqual(current_audit["path"], "raw_segments/generation-000002.jsonl")
        self.assertTrue(current_audit["incomplete_tail_ignored"])
        self.assertEqual(current_audit["incomplete_tail_bytes"], len(b'{"crash_tail":'))
        self.assertEqual(current_audit["sha256"], evaluator._file_sha256(segment))
        third = json.loads(
            (output / "closeout.generation-000003.json").read_text()
        )
        self.assertEqual(
            third["previous_closeout_sha256"],
            evaluator._file_sha256(output / "closeout.generation-000002.json"),
        )

    def test_retry_outcomes_report_ever_failed_and_recovered_exactly(self) -> None:
        output = self.root / "recovered-output"
        controls = self.live_controls("recovery-audit-2026-08-30")
        FakeWorker = self.fake_worker_class()

        def outcome(status: str):
            def fake(_worker, case, track, *, run_id, **_kwargs):
                return {
                    "schema_version": evaluator.SCHEMA_VERSION,
                    "run_id": run_id,
                    "case_id": case.case_id,
                    "track": track,
                    **case.public_metadata(),
                    "status": status,
                    "error": "synthetic" if status == "error" else None,
                    "leakage": {},
                }

            return fake

        patches = self.local_only_patches()
        with patches[0], patches[1], mock.patch.object(
            evaluator, "PersistentWorker", FakeWorker
        ), mock.patch.object(evaluator, "evaluate_case", side_effect=outcome("error")):
            digest = self.reviewed_digest(output, *controls, "--retry-errors")
            live = (
                *controls,
                "--retry-errors",
                "--reviewed-plan-digest",
                digest,
            )
            self.assertEqual(evaluator.main(self.argv(output, *live)), 3)
        patches = self.local_only_patches()
        with patches[0], patches[1], mock.patch.object(
            evaluator, "PersistentWorker", FakeWorker
        ), mock.patch.object(evaluator, "evaluate_case", side_effect=outcome("ok")):
            self.assertEqual(
                evaluator.main(
                    self.argv(output, "--resume", *live)
                ),
                0,
            )
        summary = json.loads(
            (output / "summary.generation-000002.json").read_text()
        )
        self.assertEqual(
            summary["execution_outcomes"],
            {
                "attempt_records": 2,
                "attempted": 1,
                "ok": 1,
                "error": 0,
                "missing": 0,
                "ever_failed": 1,
                "recovered": 1,
            },
        )

    def test_missing_exit_four_and_inherits_expected_stratum_metadata(self) -> None:
        output = self.root / "missing-output"
        controls = self.live_controls("missing-audit-2026-08-30")
        FakeWorker = self.fake_worker_class()
        patches = self.local_only_patches()
        with patches[0], patches[1], mock.patch.object(
            evaluator, "PersistentWorker", FakeWorker
        ), mock.patch.object(
            evaluator,
            "evaluate_case",
            side_effect=evaluator.EvaluationError("synthetic fail closed"),
        ):
            digest = self.reviewed_digest(output, *controls)
            self.assertEqual(
                evaluator.main(
                    self.argv(
                        output,
                        *controls,
                        "--reviewed-plan-digest",
                        digest,
                    )
                ),
                4,
            )
        summary = json.loads(
            (output / "summary.generation-000001.json").read_text()
        )
        self.assertEqual(summary["execution_outcomes"]["missing"], 1)
        self.assertIn(
            "UNKNOWN",
            summary["track_results"]["abstract_only"]["by_quartile"],
        )

    def test_formal_500_mode_requires_complete_builder_and_exact_denominator(self) -> None:
        formal_dataset = self.root / "formal-500.jsonl"
        fields = [
            "arts_humanities",
            "clinical_medicine",
            "computer_engineering",
            "earth_environment_agriculture",
            "life_sciences",
            "mathematics_statistics",
            "multidisciplinary_other",
            "physical_chemical_materials",
            "social_sciences",
        ]
        strata: dict[str, dict[str, object]] = {}
        rows: list[dict[str, object]] = []
        index = 0
        for stratum_index, (field, quartile) in enumerate(
            (field, quartile)
            for field in fields
            for quartile in ("Q1", "Q2", "Q3", "Q4")
        ):
            target = 14 if stratum_index < 32 else 13
            strata[f"{field}/{quartile}"] = {
                "accepted": target,
                "target": target,
                "complete": True,
            }
            for _ in range(target):
                doi = f"10.1234/formal-{index:03d}"
                rows.append(
                    {
                        "case_id": f"doi:{doi}",
                        "doi": doi,
                        "title": f"Formal title {index}",
                        "abstract": "A" * 320,
                        "published_date": "2026-03-15",
                        "gold_entity_id": index + 1,
                        "gold_journal_name": "Journal of Tests",
                        "gold_issns": ["0378-5955"],
                        "gold_journal_id": f"journal-{index:03d}",
                        "gold_jcr_quartile": quartile,
                        "broad_field": field,
                        "article_type": "journal-article",
                        "source": "crossref",
                        "publication_date_precision": "day",
                        "source_url": f"https://doi.org/{doi}",
                    }
                )
                index += 1
        with formal_dataset.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        formal_dataset.chmod(0o444)
        builder = self.root / "builder-manifest.json"
        builder.write_text(
            json.dumps(
                {
                    "builder": "scripts/build_recent_journal_benchmark.py",
                    "schema_version": 1,
                    "configuration": {
                        "sample_size": 500,
                        "min_abstract_chars": 300,
                        "max_papers_per_journal": 1,
                        "seed": "where-papers-go-recent-journals-v1",
                        "samples_per_stratum": 10,
                        "fields": fields,
                        "quartiles": ["Q1", "Q2", "Q3", "Q4"],
                    },
                    "coverage": {
                        "accepted_records": 500,
                        "target_records": 500,
                        "complete_strata": 36,
                        "covered_strata": 36,
                        "targeted_strata": 36,
                    },
                    "dataset": {
                        "complete": True,
                        "record_count": 500,
                        "sha256": evaluator._file_sha256(formal_dataset),
                        "path": formal_dataset.name,
                        "format": "JSON Lines",
                        "model_input_fields": ["title", "abstract"],
                        "label_fields": [
                            "gold_journal_id",
                            "gold_entity_id",
                            "gold_journal_name",
                            "gold_issns",
                            "gold_jcr_quartile",
                            "gold_jcr_category",
                        ],
                    },
                    "source": {
                        "name": "Crossref REST API",
                        "base_url": "https://api.crossref.org",
                        "filters": {
                            "from_pub_date": "2026-01-01",
                            "has_abstract": True,
                            "type": "journal-article",
                            "until_pub_date": "2026-06-30",
                        },
                    },
                    "internal_catalog": {
                        "eligible_q1_q4_journals": 20087,
                        "source_files_sha256": {
                            name: evaluator._file_sha256(evaluator.DATA_DIR / name)
                            for name in (
                                *evaluator.DATA_FILES,
                                evaluator.CURATED_SCOPE_FILE,
                            )
                        },
                    },
                    "strata": strata,
                }
            ),
            encoding="utf-8",
        )
        builder.chmod(0o444)

        def formal_argv(output: Path, *extra: str) -> list[str]:
            return [
                "--dataset",
                str(formal_dataset),
                "--output-dir",
                str(output),
                "--api-config",
                str(self.api_config),
                "--evaluation-mode",
                evaluator.FORMAL_MODE,
                "--builder-manifest",
                str(builder),
                "--api-cache-seed-dir",
                str(self.api_cache_seed),
                "--query-embedding-cache-seed",
                str(self.query_cache_seed),
                "--lightrag-embedding-cache-seed",
                str(self.lightrag_cache_seed),
                "--lightrag-working-dir-seed",
                str(self.lightrag_working_dir_seed),
                *extra,
            ]

        patches = self.local_only_patches()
        with patches[0], patches[1], mock.patch.object(
            evaluator,
            "_validate_formal_acquisition_evidence",
            return_value={
                "status": "synthetic-unit-evidence",
                "source_files": {
                    "synthetic_formal_evidence": {
                        "path": str(builder),
                        "sha256": evaluator._file_sha256(builder),
                        "bytes": builder.stat().st_size,
                    }
                },
            },
        ):
            rendered = StringIO()
            with redirect_stdout(rendered):
                self.assertEqual(
                    evaluator.main(formal_argv(self.root / "formal-output", "--dry-run")),
                    0,
                )
        plan = json.loads(rendered.getvalue())
        self.assertEqual(plan["case_count"], 500)
        self.assertEqual(plan["case_track_count"], 1000)
        self.assertEqual(plan["tracks"], list(evaluator.TRACKS))
        self.assertIn("formal 500-paper", plan["claim_status"])
        self.assertEqual(plan["builder_manifest"]["sha256"], evaluator._file_sha256(builder))

        patches = self.local_only_patches()
        with patches[0], patches[1], self.assertRaisesRegex(
            SystemExit, "formal_500_full_denominator forbids"
        ):
            evaluator.main(
                formal_argv(
                    self.root / "formal-forbidden",
                    "--dry-run",
                    "--max-cases",
                    "499",
                )
            )

        broken = json.loads(builder.read_text())
        broken["dataset"]["complete"] = False
        builder.chmod(0o644)
        builder.write_text(json.dumps(broken), encoding="utf-8")
        builder.chmod(0o444)
        patches = self.local_only_patches()
        with patches[0], patches[1], self.assertRaisesRegex(
            SystemExit, "formal builder manifest is incomplete"
        ):
            evaluator.main(formal_argv(self.root / "formal-broken", "--dry-run"))

    def test_keyboard_interrupt_returns_130_without_repairing_segment(self) -> None:
        output = self.root / "interrupt-output"
        controls = self.live_controls("interrupt-audit-2026-08-30")
        FakeWorker = self.fake_worker_class()
        patches = self.local_only_patches()
        with patches[0], patches[1], mock.patch.object(
            evaluator, "PersistentWorker", FakeWorker
        ), mock.patch.object(
            evaluator, "evaluate_case", side_effect=KeyboardInterrupt
        ):
            digest = self.reviewed_digest(output, *controls)
            self.assertEqual(
                evaluator.main(
                    self.argv(
                        output,
                        *controls,
                        "--reviewed-plan-digest",
                        digest,
                    )
                ),
                130,
            )
        closeout = json.loads(
            (output / "closeout.generation-000001.json").read_text()
        )
        self.assertEqual(closeout["exit_code"], 130)
        self.assertTrue(closeout["interrupted"])
        self.assertEqual(closeout["current_segment"]["bytes"], 0)

    def test_api_config_drift_rejects_result_and_stops_mixed_run(self) -> None:
        output = self.root / "binding-drift-output"
        controls = self.live_controls("binding-drift-audit-2026-08-30")
        FakeWorker = self.fake_worker_class()

        def drift_after_provider(_worker, case, track, *, run_id, **_kwargs):
            payload = json.loads(self.api_config.read_text())
            payload["llm"]["model"] = "changed-mid-run"
            self.api_config.write_text(json.dumps(payload), encoding="utf-8")
            return {
                "schema_version": evaluator.SCHEMA_VERSION,
                "run_id": run_id,
                "case_id": case.case_id,
                "track": track,
                **case.public_metadata(),
                "status": "ok",
                "leakage": {},
                "final_payload": {"large": "must-not-reach-summary" * 100},
            }

        patches = self.local_only_patches()
        with patches[0], patches[1], mock.patch.object(
            evaluator, "PersistentWorker", FakeWorker
        ), mock.patch.object(
            evaluator, "evaluate_case", side_effect=drift_after_provider
        ):
            digest = self.reviewed_digest(output, *controls)
            self.assertEqual(
                evaluator.main(
                    self.argv(
                        output,
                        *controls,
                        "--reviewed-plan-digest",
                        digest,
                    )
                ),
                3,
            )
        raw = (
            output / evaluator.GENERATION_DIR / "generation-000001.jsonl"
        ).read_text()
        self.assertIn("result rejected after binding drift", raw)
        summary_text = (output / "summary.generation-000001.json").read_text()
        self.assertNotIn("must-not-reach-summary", summary_text)

    def test_recommender_argv_freezes_all_runtime_cache_bindings(self) -> None:
        argv = evaluator.build_recommender_argv(
            "safe query",
            api_config=self.api_config,
            preliminary_k=40,
            api_timeout=20,
            api_cache_dir=self.api_cache_seed,
            query_embedding_cache=self.query_cache_seed,
            lightrag_embedding_cache=self.lightrag_cache_seed,
            lightrag_working_dir=self.lightrag_working_dir_seed,
            graph_path=self.root / "venue_graph.json.gz",
        )
        self.assertEqual(argv[argv.index("--api-cache-dir") + 1], str(self.api_cache_seed))
        self.assertEqual(
            argv[argv.index("--query-embedding-cache") + 1],
            str(self.query_cache_seed),
        )
        self.assertEqual(
            argv[argv.index("--lightrag-embedding-cache") + 1],
            str(self.lightrag_cache_seed),
        )
        self.assertEqual(
            argv[argv.index("--lightrag-working-dir") + 1],
            str(self.lightrag_working_dir_seed),
        )
        self.assertEqual(
            argv[argv.index("--graph") + 1],
            str((self.root / "venue_graph.json.gz").resolve()),
        )

    def test_output_lock_refuses_concurrent_resume(self) -> None:
        output = self.root / "lock-output"
        output.mkdir()
        descriptor = evaluator._acquire_output_lock(output)
        try:
            with self.assertRaisesRegex(
                evaluator.EvaluationError, "another evaluator"
            ):
                evaluator._acquire_output_lock(output)
        finally:
            os.close(descriptor)


if __name__ == "__main__":
    unittest.main()
