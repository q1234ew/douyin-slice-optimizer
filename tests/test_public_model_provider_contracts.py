from __future__ import annotations

import socket
import unittest
from dataclasses import replace
from decimal import Decimal
from unittest.mock import patch

from dso.providers import (
    PUBLIC_MODEL_PROVIDER_CONTRACT_VERSION,
    FakeProvider,
    ProviderCallMetrics,
    ProviderDecisionStatus,
    ProviderDataPermissionRecord,
    ProviderExecutionPolicy,
    ProviderInputSize,
    ProviderLifecycleStatus,
    ProviderModelRef,
    ProviderRegistry,
    ProviderRegistryError,
    ProviderRequest,
    PublicModelProvider,
    stable_json_sha256,
)


class PublicModelProviderContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = FakeProvider()
        self.identity = self.provider.descriptor.identity

    def _request(self, *, request_id: str = "req-1", parameters: dict | None = None) -> ProviderRequest:
        return ProviderRequest(
            request_id=request_id,
            request_type="structured_analysis",
            target=self.identity,
            content_sha256=stable_json_sha256({"content": "授权素材摘要"}),
            input_size=ProviderInputSize(
                video_seconds=8.0,
                audio_seconds=6.5,
                frame_count=3,
                image_count=3,
                text_characters=6,
                input_tokens=12,
                request_bytes=128,
            ),
            payload={"summary": "授权素材摘要", "frames": ["frame-1", "frame-2"]},
            parameters=parameters or {"temperature": 0},
        )

    def test_contract_has_vendor_neutral_versioned_identity_and_safe_defaults(self) -> None:
        request = self._request()

        self.assertEqual(request.contract_version, PUBLIC_MODEL_PROVIDER_CONTRACT_VERSION)
        self.assertEqual(request.target.provider_id, "fake")
        self.assertEqual(request.target.model_id, "fake-deterministic-v1")
        self.assertEqual(request.target.api_version, "fake-api.v1")
        self.assertEqual(request.target.prompt_version, "fake-prompt.v1")
        self.assertFalse(request.execution_policy.public_api_enabled)
        self.assertFalse(request.execution_policy.budget_authorized)
        self.assertFalse(request.data_permission.allowed_to_leave_local)
        self.assertEqual(
            self.provider.descriptor.lifecycle_status,
            ProviderLifecycleStatus.RESEARCH_ONLY,
        )

    def test_input_size_and_external_permission_are_strictly_validated(self) -> None:
        with self.assertRaisesRegex(ValueError, "audio_seconds"):
            ProviderInputSize(audio_seconds=-0.1)
        with self.assertRaisesRegex(ValueError, "frame_count"):
            ProviderInputSize(frame_count=True)
        with self.assertRaisesRegex(ValueError, "authorization_basis"):
            ProviderDataPermissionRecord(
                allowed_to_leave_local=True,
                authorization_basis="local_only",
                redaction_strategy="representative_frames_only",
                retention_days=0,
            )
        with self.assertRaisesRegex(ValueError, "retention_days"):
            ProviderDataPermissionRecord(
                allowed_to_leave_local=True,
                authorization_basis="owner_authorized",
                redaction_strategy="representative_frames_only",
                retention_days=None,
            )

    def test_request_rejects_wrong_contract_hash_and_non_json_payload(self) -> None:
        with self.assertRaisesRegex(ValueError, "contract_version"):
            replace(self._request(), contract_version="vendor-specific.v1")
        with self.assertRaisesRegex(ValueError, "content_sha256"):
            replace(self._request(), content_sha256="not-a-digest")
        with self.assertRaisesRegex(ValueError, "non-JSON"):
            replace(self._request(), payload={"secret": object()})

    def test_cache_key_changes_with_model_prompt_content_or_parameters(self) -> None:
        base = self._request()
        same_content_new_id = self._request(request_id="req-2")
        changed_parameters = self._request(parameters={"temperature": 1})
        changed_prompt = replace(
            base,
            target=replace(base.target, prompt_version="fake-prompt.v2"),
        )

        self.assertEqual(base.cache_key, same_content_new_id.cache_key)
        self.assertNotEqual(base.cache_key, changed_parameters.cache_key)
        self.assertNotEqual(base.cache_key, changed_prompt.cache_key)

    def test_fake_provider_is_deterministic_network_free_and_zero_cost(self) -> None:
        first_request = self._request(request_id="req-first")
        second_request = self._request(request_id="req-second")
        with patch.object(socket, "create_connection") as connect:
            first = self.provider.invoke(first_request)
            second = self.provider.invoke(second_request)

        connect.assert_not_called()
        self.assertEqual(first.output, second.output)
        self.assertFalse(first.metrics.cache_hit)
        self.assertTrue(second.metrics.cache_hit)
        self.assertEqual(first.metrics.network_request_count, 0)
        self.assertEqual(first.metrics.output_tokens, 0)
        self.assertEqual(first.metrics.retry_count, 0)
        self.assertEqual(first.metrics.rate_limit_count, 0)
        self.assertEqual(first.metrics.estimated_cost, Decimal("0"))
        self.assertEqual(first.metrics.cost_currency, "CNY")
        self.assertEqual(first.metrics.input_size, first_request.input_size)
        self.assertEqual(first.target, first_request.target)
        self.assertEqual(first.data_permission, first_request.data_permission)
        self.assertEqual(first.lifecycle_status, ProviderLifecycleStatus.RESEARCH_ONLY)
        self.assertEqual(first.decision.decision_status, ProviderDecisionStatus.SHADOW_ONLY)
        self.assertIn("test-only", first.decision.final_adoption_reason)

    def test_fake_provider_refuses_wrong_target_and_unsupported_request_type(self) -> None:
        wrong_target = replace(
            self._request(),
            target=ProviderModelRef(
                provider_id="other",
                model_id="fake-deterministic-v1",
                api_version="fake-api.v1",
                prompt_version="fake-prompt.v1",
            ),
        )
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.provider.invoke(wrong_target)

        unsupported = replace(self._request(), request_type="video_generation")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.provider.invoke(unsupported)

    def test_metrics_record_cost_latency_retry_rate_limit_cache_and_errors(self) -> None:
        metrics = ProviderCallMetrics(
            input_size=ProviderInputSize(text_characters=10, input_tokens=4),
            output_tokens=8,
            latency_ms=123.5,
            retry_count=1,
            rate_limit_count=1,
            request_count=2,
            network_request_count=2,
            cache_hit=False,
            estimated_cost=Decimal("0.0123"),
            cost_currency="CNY",
            error_code="rate_limit_recovered",
            error_message="one bounded retry",
        )

        self.assertEqual(metrics.output_tokens, 8)
        self.assertEqual(metrics.estimated_cost, Decimal("0.0123"))
        self.assertEqual(metrics.cost_currency, "CNY")
        self.assertEqual(metrics.error_code, "rate_limit_recovered")
        with self.assertRaisesRegex(ValueError, "estimated_cost"):
            replace(metrics, estimated_cost=Decimal("-0.01"))

    def test_registry_is_explicit_and_rejects_duplicate_provider_ids(self) -> None:
        registry = ProviderRegistry()
        self.assertIsInstance(self.provider, PublicModelProvider)

        registry.register(self.provider)

        self.assertIs(registry.resolve("fake"), self.provider)
        self.assertEqual(registry.descriptors(), (self.provider.descriptor,))
        with self.assertRaisesRegex(ProviderRegistryError, "already registered"):
            registry.register(FakeProvider())
        self.assertIs(registry.unregister("fake"), self.provider)
        with self.assertRaisesRegex(ProviderRegistryError, "not registered"):
            registry.resolve("fake")

    def test_execution_policy_rejects_invalid_timeout_and_retry_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "timeout_seconds"):
            ProviderExecutionPolicy(timeout_seconds=0)
        with self.assertRaisesRegex(ValueError, "max_retries"):
            ProviderExecutionPolicy(max_retries=True)


if __name__ == "__main__":
    unittest.main()
