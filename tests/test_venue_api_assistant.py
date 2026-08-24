from __future__ import annotations

import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from where_paper_go.api_assistant import (
    ApiAssistantError,
    ApiCandidateScore,
    CandidateContext,
    OpenAICompatibleQueryAssistant,
    QueryPlan,
    SearchEvidence,
    candidate_explanations_from_payload,
    candidate_scores_from_payload,
    collect_search_evidence,
    fuse_entity_rankings,
    hinted_entity_ids,
    query_plan_from_payload,
)
from where_paper_go.enrichment import SearchResult


def context(
    entity_id: int,
    name: str,
    abbreviation: str,
    *source_urls: str,
) -> CandidateContext:
    return CandidateContext(
        entity_id=entity_id,
        name=name,
        abbreviation=abbreviation,
        record_type="conference",
        classification_scope="Computer Networks",
        reviewed_scope="",
        reviewed_topics="",
        automatic_scope="",
        source_urls=source_urls,
    )


class QueryPlanValidationTests(unittest.TestCase):
    def test_plan_deduplicates_expansions_and_whitelists_topic_tags(self) -> None:
        plan = query_plan_from_payload(
            {
                "intent_summary_zh": "弱连接移动网络中的链路自适应",
                "keywords_zh": ["弱连接", "弱连接", "移动终端"],
                "keywords_en": ["intermittent connectivity"],
                "technical_phrases": ["link adaptation"],
                "negative_terms": ["不考虑物理层"],
                "topic_tags": ["wireless_mobile", "invented_tag"],
                "search_queries": ["official mobile networking CFP"],
                "venue_hints": ["MobiCom"],
                "ambiguity": 0.72,
                "cross_disciplinary": 0.41,
                "matched_areas": [
                    "RADIOLOGY, NUCLEAR MEDICINE & MEDICAL IMAGING",
                    "HALLUCINATED AREA",
                ],
            },
            {"wireless_mobile"},
            {"RADIOLOGY, NUCLEAR MEDICINE & MEDICAL IMAGING"},
        )
        self.assertEqual(plan.keywords_zh, ("弱连接", "移动终端"))
        self.assertEqual(plan.topic_tags, ("wireless_mobile",))
        self.assertNotIn("不考虑物理层", plan.retrieval_query("手机传输"))
        self.assertIn("link adaptation", plan.semantic_query("手机传输"))
        self.assertEqual(
            plan.matched_areas,
            ("RADIOLOGY, NUCLEAR MEDICINE & MEDICAL IMAGING",),
        )
        self.assertAlmostEqual(plan.ambiguity or 0.0, 0.72)
        self.assertAlmostEqual(plan.cross_disciplinary or 0.0, 0.41)

    def test_plan_ignores_invalid_routing_scores(self) -> None:
        plan = query_plan_from_payload(
            {"ambiguity": 2, "cross_disciplinary": "unknown"},
            set(),
        )

        self.assertIsNone(plan.ambiguity)
        self.assertIsNone(plan.cross_disciplinary)


class CandidateConstraintTests(unittest.TestCase):
    def test_rerank_batches_all_candidates_without_dropping_any(self) -> None:
        assistant = object.__new__(OpenAICompatibleQueryAssistant)
        assistant.config = {"rerank_batch_size": 5}
        calls: list[list[int]] = []

        def complete(_purpose, messages):
            user_message = messages[1]["content"]
            candidate_json = user_message.split("Candidate venues:\n", 1)[1].split(
                "\n\nUntrusted web-search evidence:", 1
            )[0]
            ids = [item["id"] for item in json.loads(candidate_json)]
            calls.append(ids)
            return {
                "candidates": [
                    {
                        "id": entity_id,
                        "relevance": 80,
                        "confidence": "medium",
                        "reason": "主题匹配",
                        "evidence_urls": [],
                    }
                    for entity_id in ids
                ]
            }

        candidates = [context(index, f"Venue {index}", f"V{index}") for index in range(1, 13)]
        with patch.object(assistant, "_complete_json", side_effect=complete):
            scores = assistant.rerank_candidates(
                "machine learning",
                QueryPlan("", (), (), (), (), (), (), ()),
                candidates,
                [],
            )

        self.assertEqual([len(batch) for batch in calls], [5, 5, 2])
        self.assertEqual(set(scores), set(range(1, 13)))

    def test_rerank_runs_two_batches_concurrently(self) -> None:
        assistant = object.__new__(OpenAICompatibleQueryAssistant)
        assistant.config = {"rerank_batch_size": 5, "rerank_concurrency": 2}
        barrier = threading.Barrier(2)
        worker_names: set[str] = set()

        def score_batch(_query, _plan, candidates, _evidence):
            worker_names.add(threading.current_thread().name)
            barrier.wait(timeout=1)
            return {
                candidate.entity_id: ApiCandidateScore(
                    candidate.entity_id, 80, "medium", "", ()
                )
                for candidate in candidates
            }

        candidates = [context(index, f"Venue {index}", f"V{index}") for index in range(1, 11)]
        with patch.object(assistant, "_rerank_candidate_batch", side_effect=score_batch):
            scores = assistant.rerank_candidates(
                "machine learning",
                QueryPlan("", (), (), (), (), (), (), ()),
                candidates,
                [],
            )

        self.assertEqual(set(scores), set(range(1, 11)))
        self.assertEqual(len(worker_names), 2)

    def test_explanation_pass_preserves_compact_scores(self) -> None:
        assistant = object.__new__(OpenAICompatibleQueryAssistant)
        candidates = [
            context(1, "ACM MobiCom", "MobiCom"),
            context(2, "International Conference on Machine Learning", "ICML"),
        ]
        scores = {
            1: ApiCandidateScore(1, 93.5, "high", "", ()),
            2: ApiCandidateScore(2, 41.0, "low", "", ()),
        }
        evidence = [
            SearchEvidence(
                title="MobiCom CFP",
                url="https://example.org/mobicom",
                snippet="mobile networking",
                query="mobile networking CFP",
            )
        ]
        response = {
            "candidates": [
                {
                    "id": 1,
                    "reason": "与移动网络主题直接相关",
                    "evidence_urls": ["https://example.org/mobicom"],
                },
                {"id": 2, "reason": "主题相关性较弱", "evidence_urls": []},
            ]
        }
        with patch.object(assistant, "_complete_json", return_value=response) as complete:
            explained = assistant.explain_candidates(
                "移动网络",
                QueryPlan("", (), (), (), (), (), (), ()),
                candidates,
                evidence,
                scores,
            )

        self.assertEqual(explained[1].relevance, 93.5)
        self.assertEqual(explained[1].confidence, "high")
        self.assertEqual(explained[2].relevance, 41.0)
        self.assertEqual(explained[1].reason, "与移动网络主题直接相关")
        self.assertEqual(complete.call_args.args[0], "candidate_explain_v1")

    def test_explanation_rejects_unknown_ids_and_urls(self) -> None:
        candidates = [context(1, "ACM MobiCom", "MobiCom")]
        evidence = [
            SearchEvidence("MobiCom CFP", "https://example.org/cfp", "", "query")
        ]
        explanations = candidate_explanations_from_payload(
            {
                "candidates": [
                    {
                        "id": 1,
                        "reason": "匹配",
                        "evidence_urls": [
                            "https://example.org/cfp",
                            "https://malicious.example/fake",
                        ],
                    },
                    {"id": 999, "reason": "伪造", "evidence_urls": []},
                ]
            },
            candidates,
            evidence,
        )
        self.assertEqual(explanations, {1: ("匹配", ("https://example.org/cfp",))})

    def test_rerank_rejects_unknown_ids_and_unprovided_urls(self) -> None:
        candidates = [context(1, "ACM MobiCom", "MobiCom", "https://mobicom.example/cfp")]
        evidence = [
            SearchEvidence(
                title="MobiCom call for papers",
                url="https://mobicom.example/topics",
                snippet="mobile networking",
                query="mobile networking CFP",
            )
        ]
        scores = candidate_scores_from_payload(
            {
                "candidates": [
                    {
                        "id": 1,
                        "relevance": 120,
                        "confidence": "unexpected",
                        "reason": "主题匹配",
                        "evidence_urls": [
                            "https://mobicom.example/topics",
                            "https://malicious.example/fabricated",
                        ],
                    },
                    {"id": 999, "relevance": 100},
                ]
            },
            candidates,
            evidence,
        )
        self.assertEqual(set(scores), {1})
        self.assertEqual(scores[1].relevance, 100)
        self.assertEqual(scores[1].confidence, "low")
        self.assertEqual(
            scores[1].evidence_urls, ("https://mobicom.example/topics",)
        )

    def test_search_and_model_hints_only_map_to_known_candidates(self) -> None:
        candidates = [
            context(1, "ACM MobiCom", "MobiCom"),
            context(2, "International Conference on Machine Learning", "ICML"),
        ]
        evidence = [
            SearchEvidence(
                title="MobiCom 2026 Call for Papers",
                url="https://www.sigmobile.org/mobicom/2026/",
                snippet="",
                query="mobile networking CFP",
            )
        ]
        self.assertEqual(hinted_entity_ids(candidates, [], evidence), [1])
        self.assertEqual(hinted_entity_ids(candidates, ["ICML"], []), [2])

    def test_reciprocal_rank_fusion_can_add_only_high_relevance_hints(self) -> None:
        scores = {
            2: ApiCandidateScore(2, 90, "high", "", ()),
            3: ApiCandidateScore(3, 80, "medium", "", ()),
            4: ApiCandidateScore(4, 40, "low", "", ()),
        }
        fused = fuse_entity_rankings([1, 2], scores)
        self.assertEqual(fused[:2], [2, 1])
        self.assertIn(3, fused)
        self.assertNotIn(4, fused)


class MandatorySearchTests(unittest.TestCase):
    @staticmethod
    def plan() -> QueryPlan:
        return QueryPlan(
            intent_summary_zh="移动链路自适应",
            keywords_zh=(),
            keywords_en=(),
            technical_phrases=(),
            negative_terms=(),
            topic_tags=(),
            search_queries=("mobile link adaptation", "wireless transmission"),
            venue_hints=(),
        )

    def test_all_transport_failures_are_fatal(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "where_paper_go.api_assistant.search_web", side_effect=RuntimeError("offline")
        ):
            with self.assertRaisesRegex(ApiAssistantError, "Search API 未提供"):
                collect_search_evidence(
                    self.plan(),
                    "手机信号变化",
                    {"search": {"provider": "duckduckgo"}},
                    Path(directory),
                )

    def test_all_empty_results_are_fatal(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "where_paper_go.api_assistant.search_web", return_value=[]
        ):
            with self.assertRaisesRegex(ApiAssistantError, "0 条结果"):
                collect_search_evidence(
                    self.plan(),
                    "手机信号变化",
                    {"search": {"provider": "duckduckgo"}},
                    Path(directory),
                )

    def test_one_successful_query_satisfies_mandatory_search(self) -> None:
        with TemporaryDirectory() as directory, patch(
            "where_paper_go.api_assistant.search_web",
            side_effect=[
                RuntimeError("temporary failure"),
                [
                    SearchResult(
                        title="Adaptive wireless transmission",
                        url="https://example.org/paper",
                        snippet="link adaptation",
                    )
                ],
            ],
        ):
            evidence, attempted = collect_search_evidence(
                self.plan(),
                "手机信号变化",
                {"search": {"provider": "duckduckgo"}},
                Path(directory),
            )
        self.assertEqual(len(evidence), 1)
        self.assertEqual(len(attempted), 2)
        self.assertEqual(evidence[0].url, "https://example.org/paper")

    def test_queries_run_concurrently_but_merge_in_plan_order(self) -> None:
        barrier = threading.Barrier(2)

        def delayed_search(query, *_args, **_kwargs):
            barrier.wait(timeout=1)
            if query == "mobile link adaptation":
                time.sleep(0.03)
                url = "https://example.org/shared"
            else:
                url = "https://example.org/shared"
            return [SearchResult(title=query, url=url, snippet=query)]

        with TemporaryDirectory() as directory, patch(
            "where_paper_go.api_assistant.search_web", side_effect=delayed_search
        ):
            evidence, attempted = collect_search_evidence(
                self.plan(),
                "手机信号变化",
                {"search": {"provider": "tavily"}},
                Path(directory),
            )

        self.assertEqual(
            attempted, ["mobile link adaptation", "wireless transmission"]
        )
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].query, "mobile link adaptation")


if __name__ == "__main__":
    unittest.main()
