from __future__ import annotations

from decimal import Decimal
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    TestClient = None

from dso.providers.budget import Money
from dso.providers.cache import FileResponseCache
from dso.providers.contracts import (
    ProviderDataPermissionRecord,
    ProviderDescriptor,
    ProviderExecutionPolicy,
    ProviderInputSize,
    ProviderLifecycleStatus,
    ProviderModelRef,
    ProviderRequest,
    stable_json_sha256,
)
from dso.providers.ledger import PublicModelLedger
from dso.providers.policy import PublicModelPolicy, UploadLevel
from dso.providers.registry import ProviderRegistry
from dso.providers.runner import PublicModelRunner
from dso.providers.service import public_model_status, run_fake_provider_smoke


class _NetworkProviderThatMustNotRun:
    def __init__(self) -> None:
        self.invoked = False
        self._descriptor = ProviderDescriptor(
            identity=ProviderModelRef(
                provider_id="network-test",
                model_id="network-test-model",
                api_version="network-test-api.v1",
                prompt_version="network-test-prompt.v1",
            ),
            lifecycle_status=ProviderLifecycleStatus.RESEARCH_ONLY,
            request_types=("structured_analysis",),
            uses_public_network=True,
            description="Test provider that must be denied before invoke.",
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def invoke(self, request: ProviderRequest):  # pragma: no cover - denial is the assertion
        self.invoked = True
        raise AssertionError("network provider must not be invoked while policy is disabled")


class PublicModelRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(
            os.environ,
            {
                "DSO_ROOT": str(self.root),
                "DSO_PUBLIC_MODEL_API_ENABLED": "",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_status_is_fail_closed_and_only_registers_network_free_fake(self) -> None:
        status = public_model_status()

        self.assertEqual(status["status"], "disabled")
        self.assertFalse(status["public_api_enabled"])
        self.assertFalse(status["network_calls_allowed"])
        self.assertEqual(status["network_provider_count"], 0)
        self.assertEqual(status["registered_providers"][0]["provider"], "fake")
        self.assertFalse(status["registered_providers"][0]["uses_public_network"])
        self.assertFalse(status["production_weight_changed"])
        self.assertFalse(status["writes_manual_gold"])

    def test_fake_smoke_is_cached_zero_cost_and_keeps_input_local(self) -> None:
        private_text = "local-only-provider-smoke-text"
        result = run_fake_provider_smoke(text=private_text, repeat=2, batch_id="smoke-1")

        self.assertEqual(result["status"], "simulated")
        self.assertEqual(result["network_request_count"], 0)
        self.assertEqual(result["public_api_call_count"], 0)
        self.assertEqual(result["estimated_cost"], "0")
        self.assertFalse(result["data_left_local"])
        self.assertEqual(result["cache_hit_count"], 1)
        self.assertEqual(result["outcomes"][0]["status"], "shadow_succeeded")
        self.assertEqual(result["outcomes"][1]["status"], "shadow_cached")
        self.assertEqual(
            result["outcomes"][0]["final_output"],
            result["outcomes"][0]["local_baseline"],
        )
        self.assertFalse(result["production_weight_changed"])

        persisted = b"".join(path.read_bytes() for path in self.root.rglob("*.*"))
        self.assertNotIn(private_text.encode(), persisted)

    def test_network_provider_is_denied_before_invoke_and_falls_back_local(self) -> None:
        provider = _NetworkProviderThatMustNotRun()
        registry = ProviderRegistry()
        registry.register(provider)
        ledger = PublicModelLedger(self.root / "ledger.sqlite3")
        runner = PublicModelRunner(
            registry=registry,
            cache=FileResponseCache(self.root / "cache"),
            ledger=ledger,
            policy=PublicModelPolicy(),
            budget_guard=None,
        )
        request = ProviderRequest(
            request_id="network-denied-1",
            request_type="structured_analysis",
            target=provider.descriptor.identity,
            content_sha256=stable_json_sha256({"sample": 1}),
            input_size=ProviderInputSize(text_characters=5, request_bytes=5),
            data_permission=ProviderDataPermissionRecord(
                allowed_to_leave_local=True,
                authorization_basis="owned_media",
                redaction_strategy="structured_summary_only",
                retention_days=0,
            ),
            execution_policy=ProviderExecutionPolicy(
                public_api_enabled=True,
                budget_authorized=True,
            ),
            payload={"summary": "local"},
        )

        outcome = runner.execute(
            request,
            estimated_cost=Money(Decimal("0.10"), "CNY"),
            upload_level=UploadLevel.STRUCTURED_SUMMARY,
            batch_id="network-denied",
            local_baseline={"score": 0.42},
        )

        self.assertFalse(provider.invoked)
        self.assertEqual(outcome.status, "fallback_local")
        self.assertEqual(outcome.policy_code, "public_models_disabled")
        self.assertEqual(outcome.final_output, {"score": 0.42})
        self.assertEqual(outcome.network_request_count, 0)
        self.assertFalse(outcome.production_weight_changed)
        self.assertEqual(ledger.count(), 1)
        self.assertEqual(next(ledger.iter_entries())["status"], "policy_rejected")

    @unittest.skipIf(TestClient is None, "FastAPI/TestClient dependencies are not installed")
    def test_api_exposes_disabled_status_and_network_free_smoke(self) -> None:
        from dso.api.main import app

        with TestClient(app) as client:
            status_response = client.get("/providers/status")
            smoke_response = client.post(
                "/providers/fake-smoke",
                json={"text": "api-local-only", "repeat": 2, "batch_id": "api-smoke"},
            )
            invalid_response = client.post(
                "/providers/fake-smoke",
                json={"repeat": 10},
            )

        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["status"], "disabled")
        self.assertEqual(smoke_response.status_code, 200)
        self.assertEqual(smoke_response.json()["network_request_count"], 0)
        self.assertEqual(smoke_response.json()["estimated_cost"], "0")
        self.assertEqual(invalid_response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
