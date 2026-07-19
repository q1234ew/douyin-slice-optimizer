from __future__ import annotations

import unittest

from dso.learning.bailian_complete_clip_batch import (
    NEUTRAL_CLIP_SUMMARY,
    evaluate_complete_clip_results,
)


def _result(
    sample_id: str,
    pair_id: str,
    label: str,
    score: float,
    role: str,
    *,
    abstain: bool = False,
) -> dict:
    return {
        "sample_id": sample_id,
        "source_pair_id": pair_id,
        "performance_label": label,
        "normalized_reward": 95.0 if label == "high" else 5.0,
        "reward_proxy": 40.0 if label == "high" else 10.0,
        "visible_engagement": {"likes": 100 if label == "high" else 10},
        "diagnostic_role": role,
        "provider_status": "shadow_succeeded",
        "network_request_count": 1,
        "usage_estimated_cost_cny": "0.1",
        "latency_ms": 1000,
        "provider_output": {
            "traffic_potential_score": score,
            "abstain": abstain,
            "audio_characteristics": ["music"],
            "timeline": [{"audio_event": "music"}],
        },
    }


class BailianCompleteClipBatchTest(unittest.TestCase):
    def test_neutral_complete_clip_summary_excludes_outcome_labels(self) -> None:
        lowered = NEUTRAL_CLIP_SUMMARY.lower()
        for forbidden in ("high", "mid", "low", "v2.4", "tianci", "music_variety"):
            self.assertNotIn(forbidden, lowered)

    def test_complete_clip_evaluation_reveals_labels_after_outputs(self) -> None:
        report = evaluate_complete_clip_results(
            [
                _result("high-a", "pair-a", "high", 0.8, "failure_cloud_wrong_v2_4_correct"),
                _result("low-a", "pair-a", "low", 0.4, "failure_cloud_wrong_v2_4_correct"),
                _result("low-b", "pair-b", "low", 0.7, "failure_both_wrong"),
                _result("high-b", "pair-b", "high", 0.6, "failure_both_wrong"),
            ]
        )

        self.assertTrue(report["labels_revealed_after_all_provider_outputs"])
        self.assertEqual(report["pair_count"], 2)
        self.assertEqual(report["pair_accuracy"], 0.5)
        self.assertEqual(report["v2_4_accuracy_on_comparable_pairs"], 0.5)
        self.assertEqual(report["audio_evidence_coverage"], 1.0)
        self.assertEqual(
            report["pair_results"][0]["outcome_evidence"]["high-a"]["reward_proxy"],
            40.0,
        )
        self.assertEqual(report["promotion_gate"]["status"], "research_only")

    def test_complete_clip_evaluation_accepts_manifest_outcome_contract(self) -> None:
        report = evaluate_complete_clip_results(
            [
                _result("high", "pair", "high", 0.8, "visible_engagement_proxy_pair"),
                _result("low", "pair", "low", 0.3, "visible_engagement_proxy_pair"),
            ],
            outcome_target="visible_engagement_reward_proxy_v2_not_views",
            promotion_gate_reason="Outcome-enriched diagnostic only.",
        )

        self.assertEqual(report["outcome_target"], "visible_engagement_reward_proxy_v2_not_views")
        self.assertEqual(report["promotion_gate"]["reason"], "Outcome-enriched diagnostic only.")

    def test_complete_clip_evaluation_treats_abstention_as_no_selection(self) -> None:
        report = evaluate_complete_clip_results(
            [
                _result("high", "pair", "high", 0.9, "control_both_correct", abstain=True),
                _result("low", "pair", "low", 0.1, "control_both_correct"),
            ]
        )

        self.assertEqual(report["pair_accuracy"], 0.0)
        self.assertEqual(report["pair_results"][0]["omni_selected_sample_id"], "abstain")
        self.assertEqual(report["abstain_count"], 1)
