from __future__ import annotations

from decimal import Decimal
import unittest

from dso.providers.evaluation import (
    SHADOW_EVALUATION_CONTRACT_VERSION,
    ShadowObservation,
    aggregate_shadow_observations,
)


class PublicModelShadowEvaluationTests(unittest.TestCase):
    def test_empty_report_remains_research_only_and_marks_metrics_missing(self) -> None:
        report = aggregate_shadow_observations([])

        self.assertEqual(report.contract_version, SHADOW_EVALUATION_CONTRACT_VERSION)
        self.assertEqual(report.status, "research_only")
        self.assertFalse(report.production_weight_changed)
        self.assertEqual(report.sample_count, 0)
        self.assertEqual(report.coverage_rate, 0.0)
        self.assertEqual(report.failure_rate, 0.0)
        self.assertEqual(report.cache_hit_rate, 0.0)
        self.assertIsNone(report.baseline_severe_error_rate)
        self.assertIsNone(report.provider_severe_error_rate)
        self.assertIsNone(report.average_quality_delta)
        self.assertIsNone(report.latency_p50_ms)
        self.assertIsNone(report.latency_p95_ms)
        self.assertEqual(report.total_cost, 0.0)
        self.assertIsNone(report.effective_candidate_cost)
        self.assertIsNone(report.currency)

    def test_aggregate_reports_quality_reliability_latency_and_cost(self) -> None:
        report = aggregate_shadow_observations(
            [
                ShadowObservation(
                    candidate_id="win",
                    success=True,
                    baseline_quality=0.5,
                    provider_quality=0.7,
                    baseline_severe_error=False,
                    provider_severe_error=False,
                    latency_ms=100,
                    cost="0.01",
                    currency="cny",
                ),
                ShadowObservation(
                    candidate_id="tie",
                    success=True,
                    baseline_quality=0.8,
                    provider_quality=0.8,
                    baseline_severe_error=False,
                    provider_severe_error=True,
                    latency_ms=200,
                    cost=Decimal("0.02"),
                    currency="CNY",
                ),
                ShadowObservation(
                    candidate_id="loss",
                    success=True,
                    baseline_quality=0.9,
                    provider_quality=0.4,
                    baseline_severe_error=True,
                    provider_severe_error=False,
                    latency_ms=400,
                    cost="0.03",
                    currency="CNY",
                ),
                ShadowObservation(
                    candidate_id="failed",
                    success=False,
                    latency_ms=800,
                    cost="0.04",
                    currency="CNY",
                    failure_reason="timeout",
                ),
            ]
        )

        self.assertEqual(report.sample_count, 4)
        self.assertEqual(report.successful_count, 3)
        self.assertEqual(report.failed_count, 1)
        self.assertEqual(report.evaluated_count, 3)
        self.assertEqual(report.coverage_rate, 0.75)
        self.assertEqual((report.win_count, report.tie_count, report.loss_count), (1, 1, 1))
        self.assertEqual(report.severe_error_evaluated_count, 3)
        self.assertEqual(report.baseline_severe_error_count, 1)
        self.assertEqual(report.provider_severe_error_count, 1)
        self.assertAlmostEqual(report.baseline_severe_error_rate or 0, 1 / 3)
        self.assertAlmostEqual(report.provider_severe_error_rate or 0, 1 / 3)
        self.assertAlmostEqual(report.average_quality_delta or 0, -0.1)
        self.assertEqual(report.latency_sample_count, 4)
        self.assertEqual(report.latency_p50_ms, 300.0)
        self.assertAlmostEqual(report.latency_p95_ms or 0, 740.0)
        self.assertEqual(report.failure_rate, 0.25)
        self.assertEqual(report.total_cost, 0.1)
        self.assertAlmostEqual(report.effective_candidate_cost or 0, 0.1 / 3)
        self.assertEqual(report.currency, "CNY")
        self.assertEqual(report.to_dict()["status"], "research_only")

    def test_cache_hit_is_covered_without_adding_provider_cost(self) -> None:
        report = aggregate_shadow_observations(
            [
                ShadowObservation(
                    candidate_id="cached",
                    success=True,
                    baseline_quality=0.6,
                    provider_quality=0.6,
                    baseline_severe_error=False,
                    provider_severe_error=False,
                    latency_ms=8,
                    cache_hit=True,
                    cost=0,
                    currency="CNY",
                )
            ]
        )

        self.assertEqual(report.evaluated_count, 1)
        self.assertEqual(report.coverage_rate, 1.0)
        self.assertEqual(report.tie_count, 1)
        self.assertEqual(report.cache_hit_count, 1)
        self.assertEqual(report.cache_hit_rate, 1.0)
        self.assertEqual(report.latency_p50_ms, 8.0)
        self.assertEqual(report.latency_p95_ms, 8.0)
        self.assertEqual(report.total_cost, 0.0)
        self.assertEqual(report.effective_candidate_cost, 0.0)

    def test_success_with_missing_quality_is_not_coerced_into_coverage(self) -> None:
        report = aggregate_shadow_observations(
            [
                ShadowObservation(
                    candidate_id="schema_partial",
                    success=True,
                    provider_quality=0.7,
                    latency_ms=10,
                )
            ]
        )

        self.assertEqual(report.successful_count, 1)
        self.assertEqual(report.evaluated_count, 0)
        self.assertEqual(report.coverage_rate, 0.0)
        self.assertEqual((report.win_count, report.tie_count, report.loss_count), (0, 0, 0))
        self.assertIsNone(report.average_quality_delta)
        self.assertIsNone(report.effective_candidate_cost)

    def test_mixed_currencies_are_rejected_without_implicit_conversion(self) -> None:
        observations = [
            ShadowObservation(
                candidate_id="cny",
                success=False,
                cost="0.1",
                currency="CNY",
            ),
            ShadowObservation(
                candidate_id="usd",
                success=False,
                cost="0.1",
                currency="USD",
            ),
        ]

        with self.assertRaisesRegex(ValueError, "mixed currencies"):
            aggregate_shadow_observations(observations)

    def test_observation_and_tolerance_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "currency is required"):
            ShadowObservation(candidate_id="cost", success=False, cost="0.1")
        with self.assertRaisesRegex(ValueError, "latency_ms must be non-negative"):
            ShadowObservation(candidate_id="latency", success=False, latency_ms=-1)
        with self.assertRaisesRegex(ValueError, "candidate_id"):
            ShadowObservation(candidate_id=" ", success=False)
        with self.assertRaisesRegex(ValueError, "tie_tolerance"):
            aggregate_shadow_observations([], tie_tolerance=-1)


if __name__ == "__main__":
    unittest.main()
