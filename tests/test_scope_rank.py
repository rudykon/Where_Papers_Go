from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from where_paper_go.scope_rank import (
    AdaptiveBudgetAllocator,
    ChannelObservation,
    LinearSoftmaxFusion,
    QueryProfile,
    SelectiveCalibrator,
    TrainingExample,
    allocate_channel_budgets,
)


def healthy_channels() -> list[ChannelObservation]:
    return [
        ChannelObservation(name, confidence=0.8, coverage=0.8, freshness=0.8)
        for name in (
            "combined",
            "semantic_vector",
            "lightrag_mix",
            "property_graph",
            "llm_area_route",
            "search_hint",
        )
    ]


class QueryProfileTests(unittest.TestCase):
    def test_fallback_profile_detects_language_length_and_cross_domain(self) -> None:
        profile = QueryProfile.from_text(
            "\u8de8\u533b\u5b66\u5f71\u50cf\u4e0e machine learning \u7684\u901a\u7528\u6a21\u578b"
        )

        self.assertEqual(profile.language, "mixed")
        self.assertGreater(profile.token_count, 4)
        self.assertGreaterEqual(profile.cross_disciplinary, 0.65)
        self.assertGreater(profile.language_complexity, 0.9)

    def test_model_supplied_profile_values_override_fallback(self) -> None:
        profile = QueryProfile.from_text(
            "machine learning",
            ambiguity=0.91,
            cross_disciplinary=0.72,
            language="other",
        )

        self.assertAlmostEqual(profile.ambiguity, 0.91)
        self.assertAlmostEqual(profile.cross_disciplinary, 0.72)
        self.assertEqual(profile.language, "other")

    def test_invalid_profile_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            QueryProfile.from_text("   ")
        with self.assertRaises(ValueError):
            QueryProfile.from_text("topic", ambiguity=1.1)
        with self.assertRaises(ValueError):
            QueryProfile.from_text("topic", language="auto")


class AdaptiveBudgetTests(unittest.TestCase):
    def test_budget_is_complete_and_every_healthy_channel_is_represented(self) -> None:
        profile = QueryProfile.from_text(
            "uncertain cross-domain biomedical graph learning",
            ambiguity=0.8,
            cross_disciplinary=0.9,
            language="en",
        )
        allocation = AdaptiveBudgetAllocator().allocate(
            profile, healthy_channels(), total_budget=40
        )

        self.assertEqual(sum(allocation.quotas.values()), 40)
        self.assertEqual(allocation.unallocated, 0)
        self.assertTrue(all(value >= 1 for value in allocation.quotas.values()))

    def test_routes_change_for_precise_and_fuzzy_cross_domain_queries(self) -> None:
        allocator = AdaptiveBudgetAllocator(minimum_per_available=0)
        exact = QueryProfile.from_text(
            "SIGIR neural ranking",
            ambiguity=0.05,
            cross_disciplinary=0.0,
            language="en",
        )
        fuzzy = QueryProfile.from_text(
            "\u533b\u5b66 knowledge graph \u76f8\u5173\u7684\u8de8\u5b66\u79d1\u901a\u7528\u65b9\u6cd5",
            ambiguity=0.95,
            cross_disciplinary=1.0,
            language="mixed",
        )

        exact_quota = allocator.allocate(exact, healthy_channels(), total_budget=60).quotas
        fuzzy_quota = allocator.allocate(fuzzy, healthy_channels(), total_budget=60).quotas

        self.assertGreater(exact_quota["combined"], fuzzy_quota["combined"])
        self.assertGreater(fuzzy_quota["llm_area_route"], exact_quota["llm_area_route"])
        self.assertGreater(fuzzy_quota["lightrag_mix"], exact_quota["lightrag_mix"])

    def test_stale_low_coverage_channel_loses_budget(self) -> None:
        profile = QueryProfile.from_text(
            "broad interdisciplinary topic",
            ambiguity=0.8,
            cross_disciplinary=0.8,
            language="en",
        )
        channels = healthy_channels()
        fresh = allocate_channel_budgets(profile, channels, total_budget=120)
        degraded = [
            ChannelObservation(
                channel.name,
                confidence=0.05 if channel.name == "search_hint" else channel.confidence,
                coverage=0.01 if channel.name == "search_hint" else channel.coverage,
                freshness=0.0 if channel.name == "search_hint" else channel.freshness,
            )
            for channel in channels
        ]
        stale = allocate_channel_budgets(profile, degraded, total_budget=120)

        self.assertLess(stale["search_hint"], fresh["search_hint"])
        self.assertEqual(sum(stale.values()), 120)

    def test_unavailable_and_capacity_limits_are_auditable(self) -> None:
        profile = QueryProfile.from_text("graph retrieval")
        observations = [
            ChannelObservation("combined", 1.0, 1.0, 1.0, capacity=2),
            ChannelObservation("semantic_vector", 1.0, 1.0, 1.0, capacity=3),
            ChannelObservation("search_hint", 1.0, 1.0, 1.0, available=False),
        ]
        allocation = AdaptiveBudgetAllocator().allocate(
            profile, observations, total_budget=10
        )

        self.assertEqual(allocation.quotas["combined"], 2)
        self.assertEqual(allocation.quotas["semantic_vector"], 3)
        self.assertEqual(allocation.quotas["search_hint"], 0)
        self.assertEqual(allocation.allocated, 5)
        self.assertEqual(allocation.unallocated, 5)

    def test_ties_are_deterministic_and_duplicate_names_fail(self) -> None:
        profile = QueryProfile.from_text("topic")
        observations = [
            ChannelObservation("z", 0.5, 0.5, 0.5),
            ChannelObservation("a", 0.5, 0.5, 0.5),
        ]
        allocation = AdaptiveBudgetAllocator(minimum_per_available=0).allocate(
            profile, observations, total_budget=1
        )
        self.assertEqual(allocation.quotas, {"z": 0, "a": 1})

        with self.assertRaises(ValueError):
            AdaptiveBudgetAllocator().allocate(
                profile, observations + [observations[0]], total_budget=2
            )


class LinearSoftmaxFusionTests(unittest.TestCase):
    def training_rows(self) -> list[TrainingExample]:
        return [
            TrainingExample({"semantic": 0.95, "graph": 0.90, "lexical": 0.70}, 1),
            TrainingExample({"semantic": 0.85, "graph": 0.75, "lexical": 0.80}, 1),
            TrainingExample({"semantic": 0.15, "graph": 0.20, "lexical": 0.30}, 0),
            TrainingExample({"semantic": 0.25, "graph": 0.10, "lexical": 0.20}, 0),
        ]

    def test_pointwise_model_learns_and_is_deterministic(self) -> None:
        first = LinearSoftmaxFusion().fit(self.training_rows())
        second = LinearSoftmaxFusion().fit(self.training_rows())

        positive = first.predict({"semantic": 0.9, "graph": 0.8, "lexical": 0.7})
        negative = first.predict({"semantic": 0.1, "graph": 0.2, "lexical": 0.1})
        self.assertGreater(positive, 0.9)
        self.assertLess(negative, 0.1)
        self.assertEqual(first.feature_names, ("graph", "lexical", "semantic"))
        self.assertEqual(first.weights, second.weights)
        self.assertEqual(first.bias, second.bias)

    def test_missing_features_are_supported_and_ranking_is_stable(self) -> None:
        model = LinearSoftmaxFusion(["semantic", "graph"]).fit(
            [({"semantic": 1.0, "graph": 1.0}, 1), ({}, 0)]
        )
        ranking = model.rank(
            {
                "venue-b": {"semantic": 0.5},
                "venue-a": {"semantic": 0.5},
                "venue-c": {"semantic": 1.0, "graph": 1.0},
            }
        )

        self.assertEqual(ranking[0][0], "venue-c")
        self.assertEqual([value[0] for value in ranking[1:]], ["venue-a", "venue-b"])

    def test_json_round_trip_preserves_predictions(self) -> None:
        model = LinearSoftmaxFusion().fit(self.training_rows(), epochs=80)
        features = {"semantic": 0.77, "graph": 0.66, "lexical": 0.55}

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scope-rank.json"
            model.save(path)
            restored = LinearSoftmaxFusion.load(path)
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["model_type"], "linear_binary_softmax")
        self.assertAlmostEqual(model.predict(features), restored.predict(features), places=14)

    def test_invalid_training_data_and_unfitted_prediction_fail(self) -> None:
        with self.assertRaises(RuntimeError):
            LinearSoftmaxFusion(["x"]).predict({"x": 1.0})
        with self.assertRaises(ValueError):
            LinearSoftmaxFusion().fit([])
        with self.assertRaises(ValueError):
            TrainingExample({"x": math.inf}, 1)
        with self.assertRaises(ValueError):
            TrainingExample({"x": 1.0}, 0.5)


class SelectiveCalibrationTests(unittest.TestCase):
    def test_fit_selects_precision_safe_threshold_and_accepts_strong_result(self) -> None:
        calibrator = SelectiveCalibrator(
            target_precision=1.0,
            min_confidence=0.5,
        ).fit([0.95, 0.85, 0.70, 0.20], [1, 1, 0, 0])

        accepted = calibrator.decide(0.95)
        rejected = calibrator.decide(0.70)

        self.assertFalse(accepted.abstain)
        self.assertIsNone(accepted.reason)
        self.assertTrue(rejected.abstain)
        self.assertEqual(rejected.reason, "below_calibrated_relevance_threshold")
        self.assertAlmostEqual(accepted.confidence + accepted.uncertainty, 1.0)

    def test_weak_evidence_and_channel_disagreement_have_explicit_reasons(self) -> None:
        calibrator = SelectiveCalibrator(
            min_confidence=0.0,
            min_evidence_coverage=0.3,
            min_channel_agreement=0.4,
        )

        no_evidence = calibrator.decide(0.99, evidence_coverage=0.1)
        disagreement = calibrator.decide(
            0.99, evidence_coverage=1.0, channel_agreement=0.1
        )

        self.assertEqual(no_evidence.reason, "insufficient_evidence_coverage")
        self.assertEqual(disagreement.reason, "insufficient_channel_agreement")

    def test_no_precision_safe_result_forces_abstention(self) -> None:
        calibrator = SelectiveCalibrator(target_precision=0.9).fit(
            [0.9, 0.8, 0.7], [0, 0, 0]
        )

        decision = calibrator.decide(1.0)
        self.assertTrue(decision.abstain)
        self.assertEqual(decision.reason, "below_calibrated_relevance_threshold")

    def test_equal_score_group_is_not_partially_accepted(self) -> None:
        calibrator = SelectiveCalibrator(
            target_precision=0.75,
            min_confidence=0.0,
        ).fit([0.9, 0.8, 0.8], [1, 1, 0])

        self.assertFalse(calibrator.decide(0.9).abstain)
        self.assertTrue(calibrator.decide(0.8).abstain)


if __name__ == "__main__":
    unittest.main()
