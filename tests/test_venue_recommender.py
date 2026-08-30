from __future__ import annotations

import csv
import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from where_paper_go import api_assistant as venue_api_assistant
from where_paper_go import embeddings as venue_embeddings
from where_paper_go import lightrag as venue_lightrag
from where_paper_go import recommender as venue_recommender
from where_paper_go.api_assistant import ApiCandidateScore, QueryPlan, SearchEvidence
from where_paper_go.graph_index import GraphVectorRecallResult, VenueGraphIndex
from where_paper_go.lightrag import LightRAGRecall
from where_paper_go.recommender import (
    MULTICHANNEL_RECALL_WEIGHTS,
    _allocate_adaptive_multichannel_quotas,
    _allocate_multichannel_quotas,
    _article_intents,
    _matches_search_filter,
    area_summary,
    build_parser,
    build_candidates,
    candidate_to_dict,
    detect_query_concepts,
    load_curated_scopes,
    load_records,
    parse_target,
    parse_targets,
    rank_candidates,
    tokenize,
    valid_issn_token,
    write_csv_output,
)


class TargetParsingTests(unittest.TestCase):
    def test_default_multichannel_pool_reserves_all_recall_sources(self) -> None:
        self.assertEqual(
            _allocate_multichannel_quotas(40),
            {
                "combined": 12,
                "semantic_vector": 8,
                "lightrag_mix": 6,
                "property_graph": 6,
                "llm_area_route": 4,
                "search_hint": 4,
            },
        )

    def test_llm_routing_scores_change_adaptive_recall_budget(self) -> None:
        channel_ids = {
            name: list(range(100)) for name, _weight in MULTICHANNEL_RECALL_WEIGHTS
        }
        precise, _precise_metadata = _allocate_adaptive_multichannel_quotas(
            "same input",
            channel_ids,
            limit=40,
            ambiguity=0.0,
            cross_disciplinary=0.0,
        )
        fuzzy, fuzzy_metadata = _allocate_adaptive_multichannel_quotas(
            "same input",
            channel_ids,
            limit=40,
            ambiguity=1.0,
            cross_disciplinary=1.0,
        )

        self.assertEqual(sum(precise.values()), 40)
        self.assertEqual(sum(fuzzy.values()), 40)
        self.assertGreater(precise["combined"], fuzzy["combined"])
        self.assertGreater(fuzzy["lightrag_mix"], precise["lightrag_mix"])
        self.assertEqual(fuzzy_metadata["mode"], "scope_rank_adaptive")

    def test_quality_first_retrieval_defaults(self) -> None:
        args = build_parser().parse_args(["--target", "CCF-A"])
        self.assertEqual(args.candidate_pool, 0)
        self.assertEqual(args.vector_limit, 500)
        self.assertFalse(args.approximate_vector_search)
        self.assertTrue(args.api_assisted_search)
        self.assertTrue(args.vector_search)
        self.assertEqual(args.lightrag_top_k, 200)
        self.assertEqual(args.api_candidate_limit, 40)
        self.assertFalse(args.fixed_recall_budget)
        self.assertIsNone(
            venue_recommender.rank_candidates_indexed.__kwdefaults__["lexical_limit"]
        )

    def test_run_local_cache_paths_can_be_bound_by_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {
                venue_recommender.API_CACHE_DIR_ENV: str(root / "api"),
                venue_recommender.QUERY_EMBEDDING_CACHE_ENV: str(
                    root / "query.json.gz"
                ),
                venue_recommender.LIGHTRAG_EMBEDDING_CACHE_ENV: str(
                    root / "lightrag.json.gz"
                ),
            }
            with patch.dict("os.environ", environment, clear=False):
                args = build_parser().parse_args(["--target", "CCF-A"])
            self.assertEqual(args.api_cache_dir, root / "api")
            self.assertEqual(args.query_embedding_cache, root / "query.json.gz")
            self.assertEqual(
                args.lightrag_embedding_cache, root / "lightrag.json.gz"
            )

    def test_topic_query_rejects_shared_embedding_write_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            shared = Path(temporary) / "shared.json.gz"
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                venue_recommender.main(
                    [
                        "--target",
                        "CCF-A",
                        "--query",
                        "wireless systems",
                        "--query-embedding-cache",
                        str(shared),
                        "--lightrag-embedding-cache",
                        str(shared),
                    ]
                )
            self.assertEqual(raised.exception.code, 2)

    def test_only_explicit_lightrag_cache_cannot_collide_with_query_default(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            default_query = venue_embeddings.default_query_embedding_cache_path(
                data_dir
            )
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                venue_recommender.main(
                    [
                        "--target",
                        "CCF-A",
                        "--query",
                        "wireless systems",
                        "--data-dir",
                        str(data_dir),
                        "--lightrag-embedding-cache",
                        str(default_query),
                    ]
                )
            self.assertEqual(raised.exception.code, 2)

    def test_only_explicit_query_cache_cannot_collide_with_lightrag_default(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            default_lightrag = venue_embeddings.default_graph_embedding_cache_path(
                data_dir
            )
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                venue_recommender.main(
                    [
                        "--target",
                        "CCF-A",
                        "--query",
                        "wireless systems",
                        "--data-dir",
                        str(data_dir),
                        "--query-embedding-cache",
                        str(default_lightrag),
                    ]
                )
            self.assertEqual(raised.exception.code, 2)

    def test_embedding_cache_cannot_collide_with_effective_api_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            default_api = data_dir / ".query_api_cache"
            for option in (
                "--query-embedding-cache",
                "--lightrag-embedding-cache",
            ):
                with (
                    self.subTest(option=option),
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    venue_recommender.main(
                        [
                            "--target",
                            "CCF-A",
                            "--query",
                            "wireless systems",
                            "--data-dir",
                            str(data_dir),
                            option,
                            str(default_api / "shared.json.gz"),
                        ]
                    )
                self.assertEqual(raised.exception.code, 2)

    def test_common_target_spellings(self) -> None:
        cases = {
            "CCFA": ("ccf", "A"),
            "ccf:A": ("ccf", "A"),
            "CCF-A": ("ccf", "A"),
            "THCPL-A": ("th_cpl", "A"),
            "TH-CPL A": ("th_cpl", "A"),
            "中科院1区": ("cas", "1"),
            "中科院大类1区": ("cas", "1"),
            "中科院一区": ("cas", "1"),
            "CAS-1": ("cas", "1"),
            "JCR-Q1": ("jcr", "Q1"),
            "JCR主类别Q1": ("jcr", "Q1"),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(parse_target(value).key, expected)

    def test_chinese_or_expression_and_deduplication(self) -> None:
        targets = parse_targets(["CCFA或者THCPL-A或者中科院1区", "CCF:A"])
        self.assertEqual(
            [target.key for target in targets],
            [("ccf", "A"), ("th_cpl", "A"), ("cas", "1")],
        )

    def test_better_than_expression_expands_levels(self) -> None:
        self.assertEqual(
            [target.key for target in parse_targets(["CCF-B及以上", "中科院2区及以上"])],
            [("ccf", "A"), ("ccf", "B"), ("cas", "1"), ("cas", "2")],
        )

    def test_invalid_target_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_target("中科院Q1")

    def test_missing_identifiers_are_not_entity_keys(self) -> None:
        self.assertEqual(valid_issn_token("N/A"), "")
        self.assertEqual(valid_issn_token("0030-211X"), "")
        self.assertEqual(valid_issn_token("0007-9235"), "00079235")

    def test_topic_query_rejects_all_offline_database_fallbacks(self) -> None:
        for option in ("--no-graph", "--no-index"):
            with self.subTest(option=option), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    venue_recommender.main(
                        ["--target", "CCF-A", "--query", "wireless systems", option]
                    )
                self.assertEqual(raised.exception.code, 2)


class ApiAssistedSearchIntegrationTests(unittest.TestCase):
    def test_cli_pipeline_expands_collects_evidence_and_reranks_known_ids(self) -> None:
        cache_directory = tempfile.TemporaryDirectory()
        self.addCleanup(cache_directory.cleanup)
        cache_root = Path(cache_directory.name)
        api_write_cache = cache_root / "api"
        query_write_cache = cache_root / "query.json.gz"
        lightrag_write_cache = cache_root / "lightrag.json.gz"
        observed_api_caches: list[Path] = []
        observed_query_caches: list[Path] = []
        observed_lightrag_caches: list[Path] = []

        class FakeEmbeddingProvider:
            fingerprint = "fake-embedding-fingerprint"
            model = "fake-embedding-model"

            def __init__(self, _config):
                pass

        class FakeAssistant:
            model = "fake-query-model"

            def __init__(self, _config, _cache_dir):
                observed_api_caches.append(_cache_dir)

            def plan_query(
                self,
                _query,
                _topic_labels,
                *,
                area_filters=(),
                available_areas=(),
            ):
                return QueryPlan(
                    intent_summary_zh="弱连接移动网络的链路自适应",
                    keywords_zh=("移动网络",),
                    keywords_en=("intermittent connectivity",),
                    technical_phrases=("link adaptation",),
                    negative_terms=(),
                    topic_tags=("wireless_mobile",),
                    search_queries=("official mobile networking CFP",),
                    venue_hints=("MobiCom",),
                )

            def rerank_candidates(self, _query, _plan, candidates, _evidence):
                return {
                    candidate.entity_id: ApiCandidateScore(
                        entity_id=candidate.entity_id,
                        relevance=100.0 if candidate.abbreviation == "MobiCom" else 20.0,
                        confidence="high" if candidate.abbreviation == "MobiCom" else "low",
                        reason="移动与无线网络主题直接匹配",
                        evidence_urls=("https://example.com/mobicom-cfp",)
                        if candidate.abbreviation == "MobiCom"
                        else (),
                    )
                    for candidate in candidates
                }

        evidence = [
            SearchEvidence(
                title="MobiCom Call for Papers",
                url="https://example.com/mobicom-cfp",
                snippet="mobile and wireless networking",
                query="official mobile networking CFP",
            )
        ]
        pipeline_barrier = threading.Barrier(3)

        def collect_evidence_concurrently(*args, **_kwargs):
            observed_api_caches.append(args[3])
            pipeline_barrier.wait(timeout=2)
            return evidence, ["official mobile networking CFP"]

        def embed_concurrently(*args, **_kwargs):
            observed_query_caches.append(args[2])
            pipeline_barrier.wait(timeout=2)
            return [1.0] + [0.0] * 1023

        def query_lightrag_concurrently(*args, **_kwargs):
            observed_lightrag_caches.append(args[4])
            pipeline_barrier.wait(timeout=2)
            return LightRAGRecall(
                entity_ids=(),
                scores={},
                channels={},
                entity_count=2,
                relationship_count=1,
                chunk_count=1,
            )

        stdout = io.StringIO()
        stderr = io.StringIO()
        events = []
        with (
            patch.object(
                venue_api_assistant,
                "load_api_assistant_config",
                return_value={
                    "llm": {"base_url": "https://example.com/v1", "model": "fake"},
                    "search": {"provider": "fake"},
                },
            ),
            patch.object(
                venue_api_assistant,
                "OpenAICompatibleQueryAssistant",
                FakeAssistant,
            ),
            patch.object(
                venue_api_assistant,
                "collect_search_evidence",
                side_effect=collect_evidence_concurrently,
            ),
            patch.object(
                venue_embeddings,
                "OpenAICompatibleEmbeddingProvider",
                FakeEmbeddingProvider,
            ),
            patch.object(
                venue_embeddings,
                "load_embedding_config",
                return_value=object(),
            ),
            patch.object(
                venue_embeddings,
                "embed_query_graph",
                side_effect=embed_concurrently,
            ),
            patch.object(
                VenueGraphIndex,
                "vector_metadata",
                return_value={
                    "vector_provider_fingerprint": "fake-embedding-fingerprint",
                    "vector_model": "fake-embedding-model",
                    "vector_dimensions": "1024",
                },
            ),
            patch.object(
                VenueGraphIndex,
                "vector_recall",
                return_value=GraphVectorRecallResult(
                    entity_ids=[],
                    similarities={},
                    model="fake-embedding-model",
                    dimensions=1024,
                ),
            ),
            patch.object(
                venue_lightrag,
                "query_lightrag",
                side_effect=query_lightrag_concurrently,
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = venue_recommender.main(
                [
                    "--target",
                    "CCF-A",
                    "--query",
                    "手机在信号时好时坏时自动调整传输策略",
                    "--api-assisted-search",
                    "--api-candidate-limit",
                    "10",
                    "--api-cache-dir",
                    str(api_write_cache),
                    "--query-embedding-cache",
                    str(query_write_cache),
                    "--lightrag-embedding-cache",
                    str(lightrag_write_cache),
                    "--limit",
                    "3",
                    "--format",
                    "json",
                ],
                event_callback=events.append,
            )
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["api_assisted_search"]["status"], "ok")
        self.assertEqual(payload["lightrag"]["mode"], "mix")
        self.assertTrue(payload["vector_search"]["enabled"])
        self.assertEqual(payload["api_assisted_search"]["search_result_count"], 1)
        self.assertEqual(payload["results"][0]["abbreviation"], "MobiCom")
        self.assertEqual(payload["results"][0]["api_relevance"], 100.0)
        self.assertIn("llm_api_rerank", payload["results"][0]["matched_fields"])
        event_types = [event["type"] for event in events]
        self.assertIn("results", event_types)
        preliminary = next(event for event in events if event["type"] == "results")
        self.assertEqual(preliminary["phase"], "preliminary")
        self.assertEqual(preliminary["payload"]["streaming_phase"], "preliminary")
        self.assertEqual(
            [
                event["stage"]
                for event in events
                if event["type"] == "progress" and event["status"] == "done"
            ],
            ["llm", "vector", "graph", "search"],
        )
        self.assertEqual(observed_api_caches, [api_write_cache, api_write_cache])
        self.assertEqual(observed_query_caches, [query_write_cache])
        self.assertEqual(observed_lightrag_caches, [lightrag_write_cache])
        self.assertEqual(
            len({query_write_cache.resolve(), lightrag_write_cache.resolve()}), 2
        )


class CuratedScopeValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        source = Path(__file__).resolve().parents[1] / "data" / "curated_venue_scopes.tsv"
        with source.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            cls.fieldnames = list(reader.fieldnames or [])
            cls.valid_row = next(reader)

    def _load_single_row(self, **changes: str):
        row = {**self.valid_row, **changes}
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "curated_venue_scopes.tsv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=self.fieldnames, delimiter="\t")
                writer.writeheader()
                writer.writerow(row)
            return load_curated_scopes(Path(temporary_directory))

    def test_non_active_review_rows_are_valid_but_not_loaded(self) -> None:
        self.assertEqual(
            self._load_single_row(
                review_status="in_review",
                source_url="",
                evidence="",
                reviewed_at="",
            ),
            {},
        )

    def test_invalid_controlled_article_type_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "article_types"):
            self._load_single_row(article_types="original_research;survey_reveiw")

    def test_invalid_controlled_topic_tag_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "topic_tags"):
            self._load_single_row(topic_tags="parallel_hpc;parallel_hcp")

    def test_invalid_review_date_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "reviewed_at"):
            self._load_single_row(reviewed_at="2026-02-30")

    def test_missing_target_status_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "来源或审核信息"):
            self._load_single_row(target_status="")

    def test_missing_review_notes_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "来源或审核信息"):
            self._load_single_row(review_notes="")

    def test_target_status_and_submission_semantics_are_consistent(self) -> None:
        with self.assertRaisesRegex(ValueError, "历史合并目标"):
            self._load_single_row(target_status="historical_merged")
        with self.assertRaisesRegex(ValueError, "刊系占位项"):
            self._load_single_row(target_status="family_non_actionable")

    def test_article_intent_requires_document_type_language(self) -> None:
        self.assertEqual(
            _article_intents(
                "We review prior work and propose a new machine learning system with experiments."
            ),
            (False, False),
        )
        self.assertEqual(
            _article_intents(
                "We present a new network protocol with extensive experiments."
            ),
            (True, False),
        )
        self.assertEqual(
            _article_intents("本文提出一种新的方法并进行实验评估。"),
            (True, False),
        )
        self.assertEqual(
            _article_intents(
                "We conduct a literature review and then propose and evaluate a new ML system."
            ),
            (False, False),
        )
        self.assertEqual(
            _article_intents("A systematic review of machine learning systems."),
            (False, True),
        )
        self.assertEqual(_article_intents("不是研究论文，是综述论文"), (False, True))
        self.assertEqual(_article_intents("不写实验论文，准备综述教程"), (False, True))
        self.assertEqual(_article_intents("systematization of knowledge for secure systems"), (False, True))
        self.assertEqual(
            _article_intents("Not a review paper; this is original research."),
            (True, False),
        )

    def test_search_filter_preserves_technical_tokens_and_ascii_boundaries(self) -> None:
        self.assertTrue(_matches_search_filter("C++ programming language", "C++"))
        self.assertTrue(_matches_search_filter("AI for networking", "AI"))
        self.assertFalse(_matches_search_filter("fairness and repair", "AI"))
        self.assertEqual(tokenize("网络"), {"网络": 1})

    def test_new_fine_grained_concepts_are_bilingual(self) -> None:
        concepts = dict(
            detect_query_concepts(
                "FPGA 可重构计算、进化算法、模糊控制、bioinformatics、"
                "industrial informatics, autonomous vehicles, and biohybrid cyborg robots"
            )
        )
        self.assertTrue(
            {
                "fpga_reconfigurable_computing",
                "evolutionary_computation",
                "fuzzy_systems",
                "bioinformatics_computational_biology",
                "industrial_informatics_manufacturing",
                "autonomous_vehicles",
                "bionics_biohybrid_systems",
            }
            <= set(concepts)
        )

    def test_fuzzy_topic_paraphrases_map_to_high_confidence_concepts(self) -> None:
        cases = {
            "手机在信号时好时坏时自动调整传输策略": {"wireless_mobile"},
            "戴头显探索三维空间并改进手势操作": {"vr_ar", "hci_ux"},
            "protecting location traces without revealing individual movements": {
                "privacy_anonymity"
            },
            "从观察数据判断治疗方案是否导致改善": {"probabilistic_causal"},
            "自动发现内存越界并生成修复补丁": {
                "testing_analysis",
                "software_security",
            },
            "数千块GPU集群进行分布式训练和数据交换": {
                "parallel_hpc",
                "ai_systems",
            },
            "让模型同时看图片听语音并回答问题": {"multimodal"},
            "固态硬盘掉电后恢复文件目录": {"storage_filesystems"},
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                detected = {topic for topic, _label in detect_query_concepts(query)}
                self.assertTrue(expected <= detected)

        self.assertNotIn(
            "privacy_anonymity",
            {topic for topic, _label in detect_query_concepts("显示用户当前位置")},
        )


class RealDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.records = load_records()

    def test_dataset_size_and_level_counts(self) -> None:
        self.assertEqual(len(self.records), 45_207)
        expected = {
            ("ccf", "A"): 58,
            ("th_cpl", "A"): 117,
            ("cas", "1"): 1_451,
            ("jcr", "Q1"): 4_518,
        }
        for key, count in expected.items():
            with self.subTest(key=key):
                self.assertEqual(sum(record.target_key == key for record in self.records), count)

    def test_reviewed_scope_catalog_has_expected_first_batch(self) -> None:
        scopes = load_curated_scopes()
        self.assertEqual(len(scopes), 160)
        ubicomp = next(scope for scope in scopes.values() if scope.match_abbreviation == "UbiComp")
        self.assertEqual(ubicomp.scope_context, "journal_first")
        self.assertEqual(
            sum(
                bool(record.curated_scope)
                for record in self.records
                if record.target_key == ("ccf", "A")
            ),
            58,
        )
        expected_candidate_coverage = {
            "CCF-A": 58,
            "THCPL-A": 117,
            "中科院1区": 53,
        }
        for target, expected in expected_candidate_coverage.items():
            with self.subTest(target=target):
                self.assertEqual(
                    len(
                        build_candidates(
                            self.records,
                            parse_targets([target]),
                            reviewed_scope_only=True,
                        )
                    ),
                    expected - (3 if target == "THCPL-A" else 0),
                )
                if target == "THCPL-A":
                    self.assertEqual(
                        len(
                            build_candidates(
                                self.records,
                                parse_targets([target]),
                                reviewed_scope_only=True,
                                include_inactive=True,
                            )
                        ),
                        expected,
                    )

    def test_review_and_proposal_only_outlets_are_explicit_constraints(self) -> None:
        scopes = {
            scope.match_name.casefold(): scope for scope in load_curated_scopes().values()
        }
        expected_modes = {
            "Annual Review of Control Robotics and Autonomous Systems": "invited_only",
            "Computer Science Review": "open",
            "Foundations and Trends in Information Retrieval": "proposal_first",
            "Foundations and Trends in Machine Learning": "proposal_first",
            "Foundations and Trends in Systems and Control": "proposal_first",
            "IEEE Communications Surveys and Tutorials": "open",
        }
        for name, mode in expected_modes.items():
            with self.subTest(name=name):
                scope = scopes[name.casefold()]
                self.assertEqual(scope.accepts_original_research, "no")
                self.assertIn("survey_review", scope.article_types.split(";"))
                self.assertNotIn("original_research", scope.article_types.split(";"))
                self.assertEqual(scope.submission_mode, mode)

        self.assertEqual(
            scopes["artificial intelligence review"].accepts_original_research,
            "yes",
        )

        robotics = rank_candidates(
            build_candidates(
                self.records,
                parse_targets(["CCF-A", "THCPL-A", "中科院1区"]),
                reviewed_scope_only=True,
            ),
            "机器人控制与自主系统",
        )
        by_name = {candidate.name: candidate for candidate in robotics}
        annual_review = by_name[
            "Annual Review of Control Robotics and Autonomous Systems"
        ]
        self.assertGreater(
            by_name["IEEE International Conference on Robotics and Automation"].score,
            annual_review.score,
        )
        self.assertIn("invited_only_target", annual_review.matched_fields)

    def test_hybrid_and_family_targets_keep_submission_semantics(self) -> None:
        scopes = {
            scope.match_name.casefold(): scope for scope in load_curated_scopes().values()
        }
        for name in (
            "International Conference on Measurement and Modeling of Computer Systems",
            "International Conference on Cryptographic Hardware and Embedded Systems",
            "ACM Symposium on Principles of Database Systems",
        ):
            self.assertEqual(scopes[name.casefold()].scope_context, "journal_first")
        self.assertEqual(
            scopes[
                "International conference on Intelligent Systems for Molecular Biology".casefold()
            ].scope_context,
            "journal_proceedings",
        )
        self.assertEqual(
            scopes[
                "International Conference on Information Processing in Sensor Networks".casefold()
            ].submission_mode,
            "retired_merged",
        )
        for name in ("Science China", "中国科学"):
            scope = scopes[name.casefold()]
            self.assertEqual(scope.scope_context, "journal_family")
            self.assertEqual(scope.submission_mode, "varies_by_series")
            self.assertEqual(scope.target_status, "family_non_actionable")

        ipsn = scopes[
            "International Conference on Information Processing in Sensor Networks".casefold()
        ]
        self.assertEqual(ipsn.target_status, "historical_merged")

    def test_inactive_targets_are_excluded_from_area_summary_by_default(self) -> None:
        targets = parse_targets(["THCPL-A"])
        default_rows = area_summary(self.records, targets)
        audit_rows = area_summary(self.records, targets, include_inactive=True)
        self.assertLess(sum(row["count"] for row in default_rows), sum(row["count"] for row in audit_rows))

    def test_th_cpl_a_type_counts(self) -> None:
        records = [record for record in self.records if record.target_key == ("th_cpl", "A")]
        self.assertEqual(sum(record.record_type == "journal" for record in records), 40)
        self.assertEqual(sum(record.record_type == "conference" for record in records), 77)

    def test_exact_area_filters_match_known_lists(self) -> None:
        ccf_a = parse_targets(["CCF-A"])
        artificial_intelligence = build_candidates(
            self.records,
            ccf_a,
            area_filters=["人工智能"],
        )
        computer_networks = build_candidates(
            self.records,
            ccf_a,
            area_filters=["计算机网络"],
        )
        self.assertEqual(len(artificial_intelligence), 7)
        self.assertEqual(len(computer_networks), 4)
        self.assertEqual(
            {candidate.abbreviation for candidate in computer_networks},
            {"SIGCOMM", "MobiCom", "INFOCOM", "NSDI"},
        )

    def test_area_filter_does_not_leak_from_an_unrequested_ranking(self) -> None:
        candidates = build_candidates(
            self.records,
            parse_targets(["CCF-A"]),
            area_filters=["高性能计算"],
        )
        self.assertEqual(candidates, [])

    def test_cross_list_result_keeps_all_ranking_badges(self) -> None:
        candidates = build_candidates(self.records, parse_targets(["CCF-A", "THCPL-A"]))
        infocom = [candidate for candidate in candidates if candidate.abbreviation == "INFOCOM"]
        self.assertEqual(len(infocom), 1)
        self.assertIn("CCF-A（2026）", infocom[0].matched_ranking_labels)
        self.assertIn("TH-CPL-A（2019）", infocom[0].matched_ranking_labels)

    def test_renamed_conferences_merge_only_through_explicit_aliases(self) -> None:
        candidates = build_candidates(self.records, parse_targets(["CCF-A", "THCPL-A"]))
        atc = [candidate for candidate in candidates if "ATC" in candidate.abbreviation]
        software_fse = [
            candidate
            for candidate in candidates
            if candidate.abbreviation == "FSE"
            and "Software Engineering" in candidate.name
        ]
        self.assertEqual(len(atc), 1)
        self.assertEqual(len(software_fse), 1)
        for candidate in (atc[0], software_fse[0]):
            self.assertIn("CCF-A（2026）", candidate.matched_ranking_labels)
            self.assertIn("TH-CPL-A（2019）", candidate.matched_ranking_labels)

    def test_reviewed_journal_scope_propagates_across_safe_alias(self) -> None:
        candidates = build_candidates(
            self.records,
            parse_targets(["THCPL-A", "中科院1区"]),
            reviewed_scope_only=True,
        )
        jsac = [candidate for candidate in candidates if candidate.abbreviation == "JSAC"]
        tpami = [candidate for candidate in candidates if candidate.abbreviation == "TPAMI"]
        self.assertEqual(len(jsac), 1)
        self.assertEqual(len(tpami), 1)
        for candidate in (jsac[0], tpami[0]):
            self.assertTrue(candidate.curated_scopes)
            self.assertIn("TH-CPL-A（2019）", candidate.matched_ranking_labels)
            self.assertIn("中科院1区（2025）", candidate.matched_ranking_labels)

    def test_explicit_journal_lineages_merge_and_propagate_scope(self) -> None:
        candidates = build_candidates(
            self.records,
            parse_targets(["THCPL-A", "中科院2区", "JCR-Q1"]),
            reviewed_scope_only=True,
        )
        expected = {
            "The VLDB Journal": "vldb_journal",
            "IEEE Transactions on Audio, Speech and Language Processing": "taslp",
            "IEEE Transactions on Networking": "ton",
        }
        for display_name, lineage in expected.items():
            with self.subTest(display_name=display_name):
                matching = [
                    candidate
                    for candidate in candidates
                    if candidate.name == display_name
                    and any(
                        record.record_type == "journal"
                        and lineage == venue_recommender.journal_lineage_name(record.name)
                        for record in candidate.records
                    )
                ]
                self.assertEqual(len(matching), 1)
                candidate = matching[0]
                self.assertTrue(candidate.curated_scopes)
                self.assertIn("TH-CPL-A（2019）", candidate.matched_ranking_labels)

    def test_negative_scope_constraints_filter_explicitly_excluded_queries(self) -> None:
        candidates = build_candidates(self.records, parse_targets(["THCPL-A"]))
        cases = {
            "纯湿实验蛋白质组学，不涉及计算方法或生物信息学": {"ISMB", "RECOMB"},
            "只把现成FPGA作为运行平台，没有体系结构或综合贡献": {"FPGA"},
            "通用机器学习应用，没有嵌入式感知或物联网贡献": {"SenSys"},
        }
        for query, excluded in cases.items():
            with self.subTest(query=query):
                ranked_names = {candidate.abbreviation for candidate in rank_candidates(candidates, query)}
                self.assertTrue(excluded.isdisjoint(ranked_names))

    def test_superseded_automatic_scope_is_hidden_from_candidate(self) -> None:
        candidates = build_candidates(self.records, parse_targets(["THCPL-A"]))
        rss = next(candidate for candidate in candidates if candidate.abbreviation == "RSS")
        self.assertEqual(rss.official_scope_candidates, [])
        for record in self.records:
            if record.name in {
                "ACM The Workshop on Hot Topics in Networks",
                "The Workshop on Hot Topics in Networks",
                "Congress on Evolutionary Computation",
            }:
                self.assertEqual(record.official_scope_status, "superseded")

    def test_secondary_sources_and_non_actionable_status_are_serialized(self) -> None:
        scopes = list(load_curated_scopes().values())
        taslp = next(scope for scope in scopes if "Audio Speech" in scope.match_name)
        self.assertIn("overview-articles", taslp.secondary_source_urls)
        self.assertGreaterEqual(sum(bool(scope.secondary_source_urls) for scope in scopes), 24)
        self.assertIn(
            "systematization_of_knowledge",
            next(scope for scope in scopes if scope.scope_id == "th-cpl-2019-ches-main-2026").article_types,
        )
        jiii = next(scope for scope in scopes if scope.match_name == "Journal of Industrial Information Integration")
        self.assertIn("survey_review", jiii.article_types)

    def test_colliding_abbreviation_does_not_merge_entities(self) -> None:
        candidates = build_candidates(self.records, parse_targets(["CCF-A", "CCF-B"]))
        fse = [candidate for candidate in candidates if candidate.abbreviation == "FSE"]
        self.assertEqual(len(fse), 2)
        self.assertNotEqual(fse[0].name, fse[1].name)

    def test_topic_ranking_prioritizes_the_exact_classification(self) -> None:
        candidates = build_candidates(self.records, parse_targets(["CCF-A"]))
        ranked = rank_candidates(candidates, "计算机网络")
        self.assertEqual(
            {candidate.abbreviation for candidate in ranked[:4]},
            {"SIGCOMM", "MobiCom", "INFOCOM", "NSDI"},
        )

    def test_scope_filter_only_uses_reviewed_fine_scope(self) -> None:
        candidates = build_candidates(
            self.records,
            parse_targets(["CCF-A"]),
            scope_filters=["无线网络"],
        )
        self.assertEqual(
            {candidate.abbreviation for candidate in candidates},
            {"SIGCOMM", "MobiCom", "INFOCOM"},
        )

    def test_project_topic_prefers_wireless_submission_targets(self) -> None:
        candidates = build_candidates(
            self.records,
            parse_targets(["CCF-A", "THCPL-A", "中科院1区"]),
        )
        ranked = rank_candidates(
            candidates,
            "截止期约束的联合波束与资源分配 无线边缘网络",
        )
        self.assertEqual(
            [candidate.abbreviation for candidate in ranked[:2]],
            ["TWC", "INFOCOM"],
        )
        self.assertGreater(
            next(candidate.score for candidate in ranked if candidate.abbreviation == "INFOCOM"),
            next(candidate.score for candidate in ranked if candidate.abbreviation == "RTSS"),
        )

    def test_controlled_l2_topics_recover_cross_language_scope(self) -> None:
        candidates = build_candidates(self.records, parse_targets(["CCF-A"]))
        storage = rank_candidates(candidates, "文件系统与存储可靠性")
        self.assertTrue({"FAST", "OSDI", "SOSP"} <= {
            candidate.abbreviation for candidate in storage[:5]
        })

        language_models = rank_candidates(candidates, "大语言模型与自然语言处理")
        self.assertTrue({"ACL", "ICLR", "ICML", "NeurIPS"} <= {
            candidate.abbreviation for candidate in language_models[:5]
        })

    def test_new_gap_scopes_recover_specialized_submission_targets(self) -> None:
        candidates = build_candidates(
            self.records,
            parse_targets(["CCF-A", "THCPL-A", "中科院1区"]),
            reviewed_scope_only=True,
        )
        expected_top_abbreviations = {
            "FPGA 可重构计算与高层综合": "FPGA",
            "进化算法与多目标优化": "TEC",
            "模糊控制系统": "TFS",
            "自动驾驶车辆的多传感器融合与控制": "",
            "情感计算与多模态情绪识别": "TAC",
        }
        for query, abbreviation in expected_top_abbreviations.items():
            with self.subTest(query=query):
                ranked = rank_candidates(candidates, query)
                if abbreviation:
                    self.assertEqual(ranked[0].abbreviation, abbreviation)
                else:
                    self.assertEqual(
                        ranked[0].name,
                        "IEEE Transactions on Intelligent Vehicles",
                    )

        bioinformatics = rank_candidates(candidates, "生物信息学 蛋白质组学 计算方法")
        self.assertEqual(
            {candidate.abbreviation for candidate in bioinformatics[:2]},
            {"ISMB", "RECOMB"},
        )
        manufacturing = rank_candidates(candidates, "工业物联网与智能制造")
        self.assertEqual(
            {candidate.name for candidate in manufacturing[:3]},
            {
                "COMPUTERS IN INDUSTRY",
                "IEEE Transactions on Industrial Informatics",
                "Journal of Industrial Information Integration",
            },
        )

    def test_review_only_journal_is_excluded_for_original_paper_intent(self) -> None:
        candidates = build_candidates(self.records, parse_targets(["中科院1区"]))
        original = rank_candidates(candidates, "原创实验论文，不写综述，计算机科学")
        reviews = rank_candidates(candidates, "计算机科学综述和教程文章")
        review_only_names = {
            "ACM COMPUTING SURVEYS",
            "Annual Review of Control Robotics and Autonomous Systems",
            "Computer Science Review",
            "Foundations and Trends in Information Retrieval",
            "Foundations and Trends in Machine Learning",
            "Foundations and Trends in Systems and Control",
            "IEEE Communications Surveys and Tutorials",
        }
        self.assertTrue(review_only_names.isdisjoint(candidate.name for candidate in original))
        self.assertTrue(
            {"ACM COMPUTING SURVEYS", "Computer Science Review"}
            <= {candidate.name for candidate in reviews[:5]}
        )

    def test_sok_intent_is_distinct_from_ordinary_review_intent(self) -> None:
        candidates = build_candidates(self.records, parse_targets(["THCPL-A"]))
        ordinary = {candidate.abbreviation for candidate in rank_candidates(candidates, "密码学综述")}
        sok = {candidate.abbreviation for candidate in rank_candidates(candidates, "密码学 systematization of knowledge")}
        self.assertNotIn("CHES", ordinary)
        self.assertIn("CHES", sok)

    def test_article_type_intent_handles_negation_and_is_not_a_topic_boost(self) -> None:
        ccf_a = build_candidates(self.records, parse_targets(["CCF-A"]))
        self.assertEqual(rank_candidates(ccf_a, "论文"), [])

        cas_one = build_candidates(self.records, parse_targets(["中科院1区"]))
        self.assertEqual(rank_candidates(cas_one, "论文"), [])
        review = rank_candidates(
            cas_one,
            "不是原创研究，是综述论文，系统总结机器学习方法",
        )
        review_names = {candidate.name for candidate in review}
        self.assertIn("ACM COMPUTING SURVEYS", review_names)
        self.assertNotIn(
            "IEEE TRANSACTIONS ON PATTERN ANALYSIS AND MACHINE INTELLIGENCE",
            review_names,
        )

    def test_unreviewed_automatic_scope_is_opt_in_and_boundaries_are_explanatory(self) -> None:
        candidates = build_candidates(self.records, parse_targets(["CCF-A"]))
        rtss = next(candidate for candidate in candidates if candidate.abbreviation == "RTSS")
        self.assertEqual(rtss.matching_document(False)["official_scope"], "")
        self.assertTrue(rtss.matching_document(True)["official_scope"])

        ranked = rank_candidates(candidates, "截止期约束的无线资源分配")
        ranked_rtss = next(candidate for candidate in ranked if candidate.abbreviation == "RTSS")
        self.assertNotIn("out_of_scope_penalty", ranked_rtss.matched_fields)
        self.assertTrue(ranked_rtss.curated_out_of_scope)

    def test_structured_output_keeps_reviewed_scope_audit_fields(self) -> None:
        candidate = build_candidates(
            self.records,
            parse_targets(["CCF-A"]),
            reviewed_scope_only=True,
        )[0]
        payload = candidate_to_dict(candidate)
        for key in (
            "entity_id",
            "reviewed_scope_entries",
            "reviewed_scope_out_of_scope",
            "reviewed_scope_basis",
            "reviewed_scope_secondary_sources",
            "reviewed_scope_target_status",
            "reviewed_scope_contexts",
            "reviewed_scope_years",
            "matched_concepts",
        ):
            self.assertIn(key, payload)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            write_csv_output([candidate])
        header = next(csv.reader(io.StringIO(output.getvalue())))
        self.assertIn("reviewed_scope_out_of_scope", header)
        self.assertIn("reviewed_scope_basis", header)
        self.assertIn("reviewed_scope_secondary_sources", header)
        self.assertIn("reviewed_scope_target_status", header)
        self.assertIn("matched_concepts", header)
        self.assertIn("official_scope_notice", header)


if __name__ == "__main__":
    unittest.main()
