from __future__ import annotations

from decimal import Decimal
from threading import RLock
from typing import Any

from dso.providers.contracts import (
    ProviderCallMetrics,
    ProviderCallStatus,
    ProviderDecisionEvidence,
    ProviderDecisionStatus,
    ProviderDescriptor,
    ProviderLifecycleStatus,
    ProviderModelRef,
    ProviderRequest,
    ProviderResult,
    stable_json_sha256,
)


class FakeProvider:
    """Deterministic, network-free provider for contract and fallback testing."""

    def __init__(self) -> None:
        self._descriptor = ProviderDescriptor(
            identity=ProviderModelRef(
                provider_id="fake",
                model_id="fake-deterministic-v1",
                api_version="fake-api.v1",
                prompt_version="fake-prompt.v1",
            ),
            lifecycle_status=ProviderLifecycleStatus.RESEARCH_ONLY,
            request_types=("structured_analysis", "text_analysis"),
            uses_public_network=False,
            description="Network-free deterministic test provider; never production evidence.",
        )
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = RLock()

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def invoke(self, request: ProviderRequest) -> ProviderResult:
        if request.target != self.descriptor.identity:
            raise ValueError("request target does not match FakeProvider identity")
        if request.request_type not in self.descriptor.request_types:
            raise ValueError(f"unsupported fake request_type {request.request_type!r}")

        result_digest = stable_json_sha256(
            {
                "request_type": request.request_type,
                "content_sha256": request.content_sha256,
                "prompt_version": request.target.prompt_version,
                "payload": request.payload,
                "parameters": request.parameters,
            }
        )
        output = {
            "result_sha256": result_digest,
            "label": "fake_shadow_result",
            "score": int(result_digest[:8], 16) / 0xFFFFFFFF,
        }
        with self._lock:
            cache_hit = request.cache_key in self._cache
            if cache_hit:
                output = dict(self._cache[request.cache_key])
            else:
                self._cache[request.cache_key] = dict(output)

        metrics = ProviderCallMetrics(
            input_size=request.input_size,
            output_tokens=0,
            latency_ms=0.0,
            retry_count=0,
            rate_limit_count=0,
            request_count=1,
            network_request_count=0,
            cache_hit=cache_hit,
            estimated_cost=Decimal("0"),
            cost_currency="CNY",
        )
        return ProviderResult(
            request_id=request.request_id,
            request_type=request.request_type,
            target=request.target,
            status=ProviderCallStatus.SUCCEEDED,
            output=output,
            metrics=metrics,
            data_permission=request.data_permission,
            lifecycle_status=self.descriptor.lifecycle_status,
            decision=ProviderDecisionEvidence(
                api_result=output,
                decision_status=ProviderDecisionStatus.SHADOW_ONLY,
                final_adoption_reason="fake provider is test-only and cannot be production evidence",
            ),
        )
