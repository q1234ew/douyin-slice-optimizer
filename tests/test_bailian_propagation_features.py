from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from dso.learning.bailian_propagation_features import (
    NEUTRAL_FEATURE_SUMMARY,
    build_propagation_feature_request,
    evaluate_propagation_feature_results,
)
from dso.providers.contracts import ProviderDataPermissionRecord, ProviderModelRef


def _features(*, high: bool) -> dict:
    return {
        "content_form": "performance",
        "hook": {
            "onset_seconds": 0.5,
            "modality": "audio_visual",
            "strength": "high" if high else "medium",
            "evidence": "音画同时开始。",
        },
        "audio": {
            "music": True,
            "singing": True,
            "speech": False,
            "audience_reaction": high,
            "energy": "high" if high else "medium",
            "energy_change": "rising" if high else "flat",
            "vocal_clarity": "high",
            "evidence": "可听到演唱和伴奏。",
        },
        "visual": {
            "primary_scene": "stage",
            "face_prominence": "high",
            "motion": "medium",
            "cut_density": "high" if high else "low",
            "text_density": "low",
            "evidence": "人物近景和舞台画面。",
        },
        "narrative": {
            "arc": "build" if high else "flat",
            "context_dependency": "low",
            "novelty": "medium" if high else "low",
            "emotional_intensity": "high" if high else "medium",
            "payoff_present": high,
            "payoff_seconds": 7.0 if high else None,
            "evidence": "结尾有情绪释放。" if high else "全程变化有限。",
        },
        "timeline": [
            {
                "start_seconds": 0,
                "end_seconds": 8,
                "visual_event": "舞台表演。",
                "audio_event": "演唱和伴奏。",
            }
        ],
        "limitations": [],
        "confidence": 0.85,
        "abstain": False,
    }


def _result(sample_id: str, label: str, *, success: bool) -> dict:
    high = label == "high"
    return {
        "sample_id": sample_id,
        "source_pair_id": "pair-a",
        "platform_item_id": f"item-{sample_id}",
        "performance_label": label,
        "normalized_reward": 95.0 if high else 5.0,
        "reward_proxy": 45.0 if high else 8.0,
        "visible_engagement": {
            "likes": 1000 if high else 10,
            "comments": 100 if high else 1,
            "favorites": 80 if high else 1,
            "shares": 50 if high else 0,
        },
        "views": None,
        "follows": None,
        "provider_status": "shadow_succeeded" if success else "fallback_local",
        "provider_output": _features(high=high) if success else {},
        "network_request_count": 1,
        "usage_estimated_cost_cny": "0.1",
        "latency_ms": 1000,
    }


class BailianPropagationFeatureTest(unittest.TestCase):
    def test_output_token_limit_changes_request_cache_identity(self) -> None:
        target = ProviderModelRef(
            provider_id="aliyun_bailian",
            model_id="qwen3.5-omni-plus-2026-03-15",
            api_version="test-api",
            prompt_version="test-prompt",
        )
        runtime = SimpleNamespace(
            provider=SimpleNamespace(descriptor=SimpleNamespace(identity=target)),
            data_permission=ProviderDataPermissionRecord(),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            proxy = Path(temp_dir) / "clip.mp4"
            proxy.write_bytes(b"synthetic-proxy")
            kwargs = {
                "runtime": runtime,
                "clip": {"sample_id": "sample-1"},
                "proxy_path": proxy,
                "proxy_info": {"duration_seconds": 10.0, "audio_seconds": 10.0},
                "batch_id": "batch-1",
            }
            default = build_propagation_feature_request(**kwargs, output_tokens=1200)
            recovery = build_propagation_feature_request(**kwargs, output_tokens=1800)

        self.assertNotEqual(default.content_sha256, recovery.content_sha256)
        self.assertEqual(recovery.parameters["estimated_output_tokens"], 1800)

    def test_neutral_summary_excludes_outcome_values(self) -> None:
        lowered = NEUTRAL_FEATURE_SUMMARY.lower()
        for forbidden in ("high", "low", "reward_proxy", "tianci", "traffic_potential_score"):
            self.assertNotIn(forbidden, lowered)

    def test_feature_evaluation_keeps_missing_rate_denominators_explicit(self) -> None:
        report = evaluate_propagation_feature_results(
            [
                _result("high", "high", success=True),
                _result("low", "low", success=True),
                _result("failed", "low", success=False),
            ]
        )

        self.assertEqual(report["schema_valid_rate"], 0.6667)
        self.assertEqual(report["comparable_pair_count"], 1)
        self.assertEqual(
            report["outcome_availability"]["visible_engagement_heat"]["coverage"],
            1.0,
        )
        self.assertEqual(report["outcome_availability"]["share_rate"]["coverage"], 0.0)
        self.assertEqual(
            report["outcome_availability"]["follow_conversion_rate"]["coverage"],
            0.0,
        )
        self.assertEqual(report["outcome_availability"]["watch_quality"]["coverage"], 0.0)
        self.assertEqual(
            report["feature_distributions_by_heat_label"]["audience_reaction"]["high"],
            {"True": 1},
        )
        self.assertNotIn(
            "traffic_potential_score",
            report["feature_outcome_rows"][0]["features"],
        )
        share_rate = report["feature_outcome_rows"][0]["outcomes"]["share_rate"]
        self.assertEqual(share_rate["status"], "unavailable_missing_views")
        self.assertIsNone(share_rate["value"])
        associations = report["exploratory_feature_outcome_associations"]
        self.assertEqual(associations["status"], "low_confidence_exploratory_only")
        self.assertFalse(associations["causal_claim_allowed"])
        hook_high = next(
            item
            for item in associations["top_prevalence_differences"]
            if item["feature"] == "hook_strength" and item["value"] == "high"
        )
        self.assertEqual(hook_high["prevalence_delta"], 1.0)
        self.assertEqual(associations["comparable_pair_count"], 1)
        self.assertEqual(report["promotion_gate"]["status"], "research_only")


if __name__ == "__main__":
    unittest.main()
