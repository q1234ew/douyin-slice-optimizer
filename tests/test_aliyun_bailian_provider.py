from __future__ import annotations

import base64
from decimal import Decimal
import gzip
import json

import httpx
import pytest

from dso.providers.aliyun_bailian import (
    BAILIAN_EMBEDDING_MODEL,
    BAILIAN_PRIMARY_JUDGE_MODEL,
    BAILIAN_RERANK_MODEL,
    AliyunBailianProvider,
    BailianConfigurationError,
    estimate_bailian_cost,
    estimate_bailian_research_cost,
    validate_bailian_base_url,
)
from dso.providers.budget import BudgetGuard, BudgetLimits, Money
from dso.providers.cache import FileResponseCache
from dso.providers.contracts import (
    ProviderBillingStatus,
    ProviderCallStatus,
    ProviderDataPermissionRecord,
    ProviderExecutionPolicy,
    ProviderInputSize,
    ProviderRequest,
    stable_json_sha256,
)
from dso.providers.ledger import PublicModelLedger
from dso.providers.policy import (
    DataPermission,
    PublicModelPolicy,
    SecretEnvRef,
    UploadLevel,
)
from dso.providers.registry import ProviderRegistry
from dso.providers.runner import PublicModelRunner
from dso.providers.service import build_aliyun_bailian_runtime, public_model_status


BASE_URL = "https://workspace-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
SECRET = "test-bailian-secret-that-must-not-persist"


def _permission_record() -> ProviderDataPermissionRecord:
    return ProviderDataPermissionRecord(
        allowed_to_leave_local=True,
        authorization_basis="synthetic_fixture_only",
        redaction_strategy="fixture_contains_no_business_data",
        retention_days=30,
        retention_policy_reference="test-contract://bailian-retention.v1",
    )


def _policy() -> PublicModelPolicy:
    return PublicModelPolicy(
        provider="aliyun_bailian",
        enabled=True,
        secret=SecretEnvRef("DSO_BAILIAN_API_KEY"),
        budget_configured=True,
        data_permission=DataPermission(
            may_leave_local=True,
            authorization_basis="synthetic_fixture_only",
            allowed_upload_levels=frozenset({UploadLevel.STRUCTURED_SUMMARY}),
            redaction_strategy="fixture_contains_no_business_data",
            retention_days=30,
            retention_policy_reference="test-contract://bailian-retention.v1",
        ),
    )


def _request(
    provider: AliyunBailianProvider,
    *,
    request_id: str = "bailian-test-1",
    retries: int = 0,
    payload: dict | None = None,
    input_size: ProviderInputSize | None = None,
) -> ProviderRequest:
    data = payload or {"summary": "这是完全合成的候选摘要。"}
    return ProviderRequest(
        request_id=request_id,
        request_type=(
            "representative_frame_analysis" if "frames" in data else "structured_analysis"
        ),
        target=provider.descriptor.identity,
        content_sha256=stable_json_sha256({"fixture": data}),
        input_size=input_size
        or ProviderInputSize(
            text_characters=13,
            input_tokens=1000,
            request_bytes=256,
        ),
        data_permission=_permission_record(),
        execution_policy=ProviderExecutionPolicy(
            public_api_enabled=True,
            budget_authorized=True,
            timeout_seconds=5,
            max_retries=retries,
        ),
        payload=data,
        parameters={"estimated_output_tokens": 500},
    )


def _response_payload(*, model: str, content: dict | None = None) -> dict:
    output = content or {
        "label": "candidate",
        "score": 0.72,
        "confidence": 0.81,
        "reasons": ["synthetic evidence"],
        "abstain": False,
    }
    return {
        "id": "cmpl-safe-1",
        "model": model,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(output)},
            }
        ],
        "usage": {
            "prompt_tokens": 1200,
            "completion_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 200},
        },
    }


def _provider_with_handler(
    monkeypatch: pytest.MonkeyPatch,
    handler,
    *,
    model_id: str = "qwen3.5-flash-2026-02-23",
) -> tuple[AliyunBailianProvider, httpx.Client]:
    monkeypatch.setenv("DSO_BAILIAN_API_KEY", SECRET)
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    provider = AliyunBailianProvider(
        model_id=model_id,
        base_url=BASE_URL,
        client=client,
        sleeper=lambda _: None,
    )
    return provider, client


def test_bailian_base_url_requires_beijing_workspace_host() -> None:
    assert validate_bailian_base_url(BASE_URL + "/") == BASE_URL
    for invalid in (
        "http://workspace-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        BASE_URL + "?redirect=1",
        "https://127.0.0.1/compatible-mode/v1",
    ):
        with pytest.raises(BailianConfigurationError):
            validate_bailian_base_url(invalid)


def test_bailian_cost_uses_fixed_snapshot_tiers_and_retry_reservation() -> None:
    cost = estimate_bailian_cost(
        model_id="qwen3.5-flash-2026-02-23",
        input_tokens=3000,
        output_tokens=500,
    )
    retried = estimate_bailian_cost(
        model_id="qwen3.5-flash-2026-02-23",
        input_tokens=3000,
        output_tokens=500,
        attempts=2,
    )

    assert cost.amount == Decimal("0.0016")
    assert retried.amount == Decimal("0.0032")
    assert cost.currency == "CNY"


def test_bailian_success_sends_frozen_safe_request_and_parses_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=_response_payload(model="qwen3.5-flash-2026-02-23"),
            headers={"x-request-id": "req-safe-123"},
        )

    provider, client = _provider_with_handler(monkeypatch, handler)
    try:
        result = provider.invoke(_request(provider))
    finally:
        client.close()

    assert result.status == ProviderCallStatus.SUCCEEDED
    assert result.output["label"] == "candidate"
    assert result.metrics.input_size.input_tokens == 1200
    assert result.metrics.output_tokens == 100
    assert result.metrics.provider_cached_input_tokens == 200
    assert result.metrics.estimated_cost == Decimal("0.00044")
    assert result.metrics.billing_status == ProviderBillingStatus.USAGE_ESTIMATED
    assert result.metrics.provider_request_id == "req-safe-123"
    assert len(result.metrics.attempts) == 1
    assert captured["url"] == BASE_URL + "/chat/completions"
    assert captured["authorization"] == f"Bearer {SECRET}"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "qwen3.5-flash-2026-02-23"
    assert body["stream"] is False
    assert body["enable_thinking"] is False
    assert body["response_format"] == {"type": "json_object"}
    assert body["temperature"] == 0
    assert body["seed"] == 1234
    assert "n" not in body
    assert "max_tokens" not in body
    assert "tools" not in body
    assert "web_search" not in json.dumps(body)


def test_bailian_invalid_schema_retries_once_and_accounts_each_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        content = (
            {"label": "missing-required-fields"}
            if call_count == 1
            else None
        )
        return httpx.Response(
            200,
            json=_response_payload(
                model="qwen3.5-flash-2026-02-23",
                content=content,
            ),
            headers={"x-request-id": f"attempt-{call_count}"},
        )

    provider, client = _provider_with_handler(monkeypatch, handler)
    try:
        result = provider.invoke(_request(provider, retries=1))
    finally:
        client.close()

    assert result.status == ProviderCallStatus.SUCCEEDED
    assert call_count == 2
    assert result.metrics.network_request_count == 2
    assert result.metrics.retry_count == 1
    assert result.metrics.input_size.input_tokens == 2400
    assert result.metrics.output_tokens == 200
    assert result.metrics.estimated_cost == Decimal("0.00088")
    assert [item.provider_request_id for item in result.metrics.attempts] == [
        "attempt-1",
        "attempt-2",
    ]


def test_bailian_classifies_remote_disconnect_and_retries_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.RemoteProtocolError(
                "synthetic upstream disconnected; must not be persisted",
                request=request,
            )
        return httpx.Response(
            200,
            json=_response_payload(model="qwen3.5-flash-2026-02-23"),
            headers={"x-request-id": "req-recovered-1"},
        )

    provider, client = _provider_with_handler(monkeypatch, handler)
    try:
        result = provider.invoke(_request(provider, retries=1))
    finally:
        client.close()

    assert result.status == ProviderCallStatus.SUCCEEDED
    assert result.metrics.network_request_count == 2
    assert result.metrics.retry_count == 1
    assert result.metrics.billing_status == ProviderBillingStatus.UNKNOWN
    assert result.metrics.attempts[0].error_code == "network_remote_protocol_error"
    assert result.metrics.attempts[1].error_code == ""
    assert result.metrics.provider_request_id == "req-recovered-1"
    assert "synthetic upstream disconnected" not in result.metrics.error_message


def test_bailian_persists_safe_transport_category_without_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "synthetic secret-bearing diagnostic must not persist",
            request=request,
        )

    provider, client = _provider_with_handler(monkeypatch, handler)
    try:
        result = provider.invoke(_request(provider))
    finally:
        client.close()

    assert result.status == ProviderCallStatus.FAILED
    assert result.metrics.network_request_count == 1
    assert result.metrics.error_code == "network_connect_error"
    assert result.metrics.attempts[0].error_code == "network_connect_error"
    assert "synthetic secret-bearing" not in result.metrics.error_message


def test_bailian_429_retries_but_quota_and_auth_do_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rate_calls = 0

    def rate_handler(request: httpx.Request) -> httpx.Response:
        nonlocal rate_calls
        rate_calls += 1
        if rate_calls == 1:
            return httpx.Response(
                429,
                json={"code": "rate_limit_reached", "message": "do not persist"},
                headers={"retry-after": "0"},
            )
        return httpx.Response(
            200,
            json=_response_payload(model="qwen3.5-flash-2026-02-23"),
        )

    provider, client = _provider_with_handler(monkeypatch, rate_handler)
    try:
        recovered = provider.invoke(_request(provider, retries=1))
    finally:
        client.close()

    assert recovered.status == ProviderCallStatus.SUCCEEDED
    assert recovered.metrics.rate_limit_count == 1
    assert recovered.metrics.billing_status == ProviderBillingStatus.UNKNOWN
    assert rate_calls == 2

    for status_code, code, expected in (
        (401, "invalid_api_key", ProviderCallStatus.DENIED),
        (429, "quota_exhausted", ProviderCallStatus.DENIED),
    ):
        calls = 0

        def deny_handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(status_code, json={"code": code})

        denied_provider, denied_client = _provider_with_handler(monkeypatch, deny_handler)
        try:
            denied = denied_provider.invoke(_request(denied_provider, retries=1))
        finally:
            denied_client.close()
        assert denied.status == expected
        assert denied.metrics.network_request_count == 1
        assert calls == 1


def test_bailian_success_without_usage_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = _response_payload(model="qwen3.5-flash-2026-02-23")
        payload.pop("usage")
        return httpx.Response(200, json=payload)

    provider, client = _provider_with_handler(monkeypatch, handler)
    try:
        result = provider.invoke(_request(provider))
    finally:
        client.close()

    assert result.status == ProviderCallStatus.FAILED
    assert result.metrics.billing_status == ProviderBillingStatus.UNKNOWN
    assert result.metrics.error_code == "invalid_provider_response"


def test_bailian_response_body_is_bounded_before_json_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 1_100_000)

    provider, client = _provider_with_handler(monkeypatch, handler)
    try:
        result = provider.invoke(_request(provider))
    finally:
        client.close()

    assert result.status == ProviderCallStatus.FAILED
    assert result.metrics.error_code == "response_too_large"
    assert result.metrics.response_bytes == 1_000_001


def test_bailian_compressed_response_is_not_decoded_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(
        _response_payload(model="qwen3.5-flash-2026-02-23")
    ).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=gzip.compress(payload),
            headers={
                "content-encoding": "gzip",
                "content-length": str(len(payload)),
                "transfer-encoding": "chunked",
                "x-request-id": "req-gzip-1",
            },
        )

    provider, client = _provider_with_handler(monkeypatch, handler)
    try:
        result = provider.invoke(_request(provider))
    finally:
        client.close()

    assert result.status == ProviderCallStatus.SUCCEEDED
    assert result.output["label"] == "candidate"
    assert result.metrics.provider_request_id == "req-gzip-1"
    assert result.metrics.error_code == ""


def test_bailian_representative_frames_are_local_jpeg_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Minimal JPEG structure with a 1x1 SOF0 marker; decoding pixels is not needed
    # for the adapter's MIME/dimension boundary validation.
    jpeg = bytes.fromhex("ffd8ffc0000b080001000101011100ffd9")
    encoded = base64.b64encode(jpeg).decode("ascii")
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=_response_payload(model="qwen3-vl-flash-2026-01-22"),
        )

    provider, client = _provider_with_handler(
        monkeypatch,
        handler,
        model_id="qwen3-vl-flash-2026-01-22",
    )
    request = _request(
        provider,
        payload={
            "summary": "合成视觉摘要",
            "frames": [
                {"role": "hook", "mime_type": "image/jpeg", "data_base64": encoded}
            ],
        },
        input_size=ProviderInputSize(
            frame_count=1,
            image_count=1,
            text_characters=8,
            input_tokens=1000,
            request_bytes=512,
        ),
    )
    try:
        result = provider.invoke(request)
    finally:
        client.close()

    assert result.status == ProviderCallStatus.SUCCEEDED
    body = captured["body"]
    assert isinstance(body, dict)
    user_content = body["messages"][1]["content"]
    assert any(part.get("type") == "image_url" for part in user_content)
    assert "http://" not in json.dumps(user_content)
    assert "https://" not in json.dumps(user_content)


def test_bailian_multimodal_embedding_uses_workspace_endpoint_and_zero_output_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jpeg = bytes.fromhex("ffd8ffc0000b080001000101011100ffd9")
    encoded = base64.b64encode(jpeg).decode("ascii")
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "request_id": "emb-safe-1",
                "output": {
                    "embeddings": [
                        {"index": 0, "type": "fusion", "embedding": [0.0] * 2560}
                    ]
                },
                "usage": {"input_tokens": 20, "image_tokens": 1000, "total_tokens": 1020},
            },
        )

    provider, client = _provider_with_handler(
        monkeypatch,
        handler,
        model_id=BAILIAN_EMBEDDING_MODEL,
    )
    request = ProviderRequest(
        request_id="embedding-test",
        request_type="multimodal_embedding",
        target=provider.descriptor.identity,
        content_sha256=stable_json_sha256({"fixture": "embedding"}),
        input_size=ProviderInputSize(
            frame_count=1,
            image_count=1,
            text_characters=4,
            input_tokens=1200,
            request_bytes=512,
        ),
        data_permission=_permission_record(),
        execution_policy=ProviderExecutionPolicy(
            public_api_enabled=True,
            budget_authorized=True,
            timeout_seconds=5,
        ),
        payload={
            "summary": "合成摘要",
            "frames": [{"role": "hook", "mime_type": "image/jpeg", "data_base64": encoded}],
        },
        parameters={
            "dimension": 2560,
            "enable_fusion": True,
            "instruct": "检索相似短视频",
            "estimated_image_tokens": 1000,
        },
    )
    preflight = provider.preflight_request(request)
    assert captured == {}
    assert preflight["serialized_request_bytes"] > 0
    assert preflight["frame_count"] == 1
    assert preflight["network_request_count"] == 0
    assert preflight["secret_resolved"] is False
    try:
        result = provider.invoke(request)
    finally:
        client.close()

    assert result.status == ProviderCallStatus.SUCCEEDED
    assert result.metrics.output_tokens == 0
    assert result.metrics.input_size.input_tokens == 1020
    assert result.metrics.estimated_cost == Decimal("0.001814")
    assert result.output["dimension"] == 2560
    assert len(result.output["embeddings"][0]["embedding"]) == 2560
    assert captured["url"] == (
        "https://workspace-123.cn-beijing.maas.aliyuncs.com/api/v1/services/"
        "embeddings/multimodal-embedding/multimodal-embedding"
    )
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["parameters"] == {
        "dimension": 2560,
        "enable_fusion": True,
        "instruct": "检索相似短视频",
    }
    assert body["input"]["contents"][1]["image"].startswith("data:image/jpeg;base64,")


def test_bailian_multimodal_rerank_maps_indexes_back_to_sample_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "request_id": "rerank-safe-1",
                "output": {
                    "results": [
                        {"index": 1, "relevance_score": 0.91},
                        {"index": 0, "relevance_score": 0.42},
                    ]
                },
                "usage": {"total_tokens": 300},
            },
        )

    provider, client = _provider_with_handler(
        monkeypatch,
        handler,
        model_id=BAILIAN_RERANK_MODEL,
    )
    documents = [
        {"sample_id": "sample-a", "text": "舞台演唱"},
        {"sample_id": "sample-b", "text": "情绪回报"},
    ]
    request = ProviderRequest(
        request_id="rerank-test",
        request_type="multimodal_rerank",
        target=provider.descriptor.identity,
        content_sha256=stable_json_sha256({"fixture": "rerank"}),
        input_size=ProviderInputSize(text_characters=20, input_tokens=400, request_bytes=128),
        data_permission=_permission_record(),
        execution_policy=ProviderExecutionPolicy(
            public_api_enabled=True,
            budget_authorized=True,
            timeout_seconds=5,
        ),
        payload={"query": {"text": "高价值华语音乐短视频"}, "documents": documents},
        parameters={
            "top_n": 2,
            "return_documents": False,
            "instruct": "按传播价值相似性重排",
            "estimated_image_tokens": 0,
        },
    )
    try:
        result = provider.invoke(request)
    finally:
        client.close()

    assert result.status == ProviderCallStatus.SUCCEEDED
    assert result.metrics.estimated_cost == Decimal("0.00021")
    assert provider.estimate_max_cost(request).amount > result.metrics.estimated_cost
    assert [item["sample_id"] for item in result.output["results"]] == ["sample-b", "sample-a"]
    assert captured["url"] == (
        "https://workspace-123.cn-beijing.maas.aliyuncs.com/api/v1/services/"
        "rerank/text-rerank/text-rerank"
    )
    body = captured["body"]
    assert isinstance(body, dict)
    assert all("sample_id" not in item for item in body["input"]["documents"])


def test_bailian_pairwise_judge_has_frozen_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = {
        "choice": "left",
        "confidence": 0.83,
        "reasons": ["开头更清晰"],
        "risk_flags": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_response_payload(model=BAILIAN_PRIMARY_JUDGE_MODEL, content=output),
        )

    provider, client = _provider_with_handler(
        monkeypatch,
        handler,
        model_id=BAILIAN_PRIMARY_JUDGE_MODEL,
    )
    request = ProviderRequest(
        request_id="judge-test",
        request_type="pairwise_judge",
        target=provider.descriptor.identity,
        content_sha256=stable_json_sha256({"fixture": "judge"}),
        input_size=ProviderInputSize(text_characters=20, input_tokens=1000, request_bytes=128),
        data_permission=_permission_record(),
        execution_policy=ProviderExecutionPolicy(
            public_api_enabled=True,
            budget_authorized=True,
            timeout_seconds=5,
        ),
        payload={
            "left": {"summary": "左侧合成候选"},
            "right": {"summary": "右侧合成候选"},
            "context": "冻结对照",
        },
        parameters={"estimated_output_tokens": 500},
    )
    try:
        result = provider.invoke(request)
    finally:
        client.close()

    assert result.status == ProviderCallStatus.SUCCEEDED
    assert result.output == output
    assert result.target.prompt_version == "dso-cloud-pairwise-judge.v1"


def test_bailian_research_cost_is_input_only_and_modality_aware() -> None:
    cost = estimate_bailian_research_cost(
        request_type="multimodal_embedding",
        input_tokens=1200,
        image_tokens=1000,
        attempts=2,
    )
    assert cost.amount == Decimal("0.00388")


def test_runner_caches_before_budget_and_persists_actual_usage_and_attempts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_response_payload(model="qwen3.5-flash-2026-02-23"),
            headers={"x-request-id": "req-ledger-1"},
        )

    provider, client = _provider_with_handler(monkeypatch, handler)
    registry = ProviderRegistry()
    registry.register(provider)
    ledger = PublicModelLedger(tmp_path / "ledger.sqlite3")
    budget = BudgetGuard(
        BudgetLimits(
            per_request=Money(Decimal("0.10"), "CNY"),
            per_batch=Money(Decimal("1"), "CNY"),
            per_day=Money(Decimal("2"), "CNY"),
        ),
        batch_id="bailian-batch",
    )
    runner = PublicModelRunner(
        registry=registry,
        cache=FileResponseCache(tmp_path / "cache"),
        ledger=ledger,
        policy=_policy(),
        budget_guard=budget,
    )
    request = _request(provider)
    reservation = provider.estimate_max_cost(request)
    try:
        first = runner.execute(
            request,
            estimated_cost=reservation,
            upload_level=UploadLevel.STRUCTURED_SUMMARY,
            batch_id="bailian-batch",
            local_baseline={"score": 0.4},
        )
        after_first = budget.snapshot().batch_spent.amount
        second = runner.execute(
            _request(provider, request_id="bailian-test-2"),
            estimated_cost=reservation,
            upload_level=UploadLevel.STRUCTURED_SUMMARY,
            batch_id="bailian-batch",
            local_baseline={"score": 0.4},
        )
    finally:
        client.close()

    assert first.status == "shadow_succeeded"
    assert first.preflight_reserved_cost == str(reservation.amount)
    assert first.usage_estimated_cost == "0.00044"
    assert first.final_output == {"score": 0.4}
    assert after_first == Decimal("0.00044")
    assert second.status == "shadow_cached"
    assert budget.snapshot().batch_spent.amount == after_first
    entries = list(ledger.iter_entries())
    assert entries[0]["input_tokens"] == 1200
    assert entries[0]["provider_cached_input_tokens"] == 200
    assert entries[0]["provider_request_id"] == "req-ledger-1"
    assert entries[0]["usage_estimated_cost"] == Decimal("0.00044")
    assert entries[1]["status"] == "cache_hit"
    attempts = list(ledger.iter_attempts(call_id=str(entries[0]["call_id"])))
    assert len(attempts) == 1
    assert attempts[0]["provider_request_id"] == "req-ledger-1"
    persisted = (tmp_path / "ledger.sqlite3").read_bytes()
    assert SECRET.encode() not in persisted
    assert "完全合成的候选摘要".encode() not in persisted


def test_runner_falls_back_when_actual_usage_exceeds_reservation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_response_payload(model="qwen3.5-flash-2026-02-23"),
        )

    provider, client = _provider_with_handler(monkeypatch, handler)
    registry = ProviderRegistry()
    registry.register(provider)
    budget = BudgetGuard(
        BudgetLimits(
            per_request=Money(Decimal("1"), "CNY"),
            per_batch=Money(Decimal("2"), "CNY"),
            per_day=Money(Decimal("3"), "CNY"),
        ),
        batch_id="under-reserved",
    )
    runner = PublicModelRunner(
        registry=registry,
        cache=FileResponseCache(tmp_path / "cache"),
        ledger=PublicModelLedger(tmp_path / "ledger.sqlite3"),
        policy=_policy(),
        budget_guard=budget,
    )
    try:
        outcome = runner.execute(
            _request(provider),
            estimated_cost=Money(Decimal("0.0001"), "CNY"),
            upload_level=UploadLevel.STRUCTURED_SUMMARY,
            batch_id="under-reserved",
            local_baseline={"score": 0.4},
        )
    finally:
        client.close()

    assert outcome.status == "fallback_local"
    assert outcome.policy_code == "budget_reservation_actual_exceeded"
    assert outcome.usage_estimated_cost == "0.00044"
    assert outcome.final_output == {"score": 0.4}
    assert budget.snapshot().batch_spent.amount == Decimal("0.00044")


def test_runner_releases_reservation_when_budget_batch_does_not_match(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("batch mismatch must be rejected before the network")

    provider, client = _provider_with_handler(monkeypatch, handler)
    registry = ProviderRegistry()
    registry.register(provider)
    budget = BudgetGuard(
        BudgetLimits(
            per_request=Money(Decimal("1"), "CNY"),
            per_batch=Money(Decimal("2"), "CNY"),
            per_day=Money(Decimal("3"), "CNY"),
        ),
        batch_id="guard-batch",
    )
    runner = PublicModelRunner(
        registry=registry,
        cache=FileResponseCache(tmp_path / "cache"),
        ledger=PublicModelLedger(tmp_path / "ledger.sqlite3"),
        policy=_policy(),
        budget_guard=budget,
    )
    try:
        outcome = runner.execute(
            _request(provider),
            estimated_cost=Money(Decimal("0.01"), "CNY"),
            upload_level=UploadLevel.STRUCTURED_SUMMARY,
            batch_id="runner-batch",
            local_baseline={"score": 0.4},
        )
    finally:
        client.close()

    assert outcome.policy_code == "budget_batch_mismatch"
    assert outcome.network_request_count == 0
    assert budget.snapshot().batch_spent.amount == Decimal("0")
    assert budget.snapshot().active_reservation_count == 0


def test_environment_runtime_requires_every_gate_and_never_exposes_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    configured = {
        "DSO_ROOT": str(tmp_path),
        "DSO_PUBLIC_MODEL_API_ENABLED": "1",
        "DSO_PUBLIC_MODEL_PROVIDER": "aliyun_bailian",
        "DSO_BAILIAN_MODEL_ID": "qwen3.5-flash-2026-02-23",
        "DSO_BAILIAN_BASE_URL": BASE_URL,
        "DSO_BAILIAN_API_KEY": SECRET,
        "DSO_PUBLIC_MODEL_BUDGET_PER_REQUEST_CNY": "0.10",
        "DSO_PUBLIC_MODEL_BUDGET_PER_BATCH_CNY": "1.00",
        "DSO_PUBLIC_MODEL_BUDGET_PER_DAY_CNY": "2.00",
        "DSO_BAILIAN_DATA_ALLOWED": "1",
        "DSO_BAILIAN_AUTHORIZATION_BASIS": "synthetic_fixture_only",
        "DSO_BAILIAN_REDACTION_STRATEGY": "fixture_contains_no_business_data",
        "DSO_BAILIAN_RETENTION_DAYS": "30",
        "DSO_BAILIAN_RETENTION_POLICY_REFERENCE": "test-contract://bailian-retention.v1",
        "DSO_BAILIAN_ALLOWED_UPLOAD_LEVELS": "structured_summary",
    }
    for name, value in configured.items():
        monkeypatch.setenv(name, value)

    status = public_model_status()
    runtime = build_aliyun_bailian_runtime(batch_id="configured-smoke")
    embedding_runtime = build_aliyun_bailian_runtime(
        batch_id="configured-embedding-smoke",
        model_id=BAILIAN_EMBEDDING_MODEL,
    )

    assert status["status"] == "ready_for_shadow"
    assert status["network_calls_allowed"] is True
    assert all(status["gates"].values())
    assert SECRET not in json.dumps(status)
    assert runtime.provider.configured is True
    assert runtime.data_permission.retention_days == 30
    assert runtime.allowed_upload_levels == frozenset({UploadLevel.STRUCTURED_SUMMARY})
    assert runtime.budget_guard.snapshot().daily_spent.amount == Decimal("0")
    assert embedding_runtime.provider.descriptor.identity.model_id == BAILIAN_EMBEDDING_MODEL
    assert embedding_runtime.provider.descriptor.request_types == ("multimodal_embedding",)


def test_environment_runtime_accepts_referenced_non_fixed_retention_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    configured = {
        "DSO_ROOT": str(tmp_path),
        "DSO_PUBLIC_MODEL_API_ENABLED": "1",
        "DSO_PUBLIC_MODEL_PROVIDER": "aliyun_bailian",
        "DSO_BAILIAN_MODEL_ID": "qwen3.5-flash-2026-02-23",
        "DSO_BAILIAN_BASE_URL": BASE_URL,
        "DSO_BAILIAN_API_KEY": SECRET,
        "DSO_PUBLIC_MODEL_BUDGET_PER_REQUEST_CNY": "0.10",
        "DSO_PUBLIC_MODEL_BUDGET_PER_BATCH_CNY": "1.00",
        "DSO_PUBLIC_MODEL_BUDGET_PER_DAY_CNY": "2.00",
        "DSO_BAILIAN_DATA_ALLOWED": "1",
        "DSO_BAILIAN_AUTHORIZATION_BASIS": "owner_authorized_research_frames",
        "DSO_BAILIAN_REDACTION_STRATEGY": "summary_and_representative_frames_only",
        "DSO_BAILIAN_RETENTION_DAYS": "provider_minimum_necessary",
        "DSO_BAILIAN_RETENTION_POLICY_REFERENCE": (
            "https://terms.alicdn.com/legal-agreement/terms/common_platform_service/"
            "20230728213935489/20230728213935489.html"
        ),
        "DSO_BAILIAN_ALLOWED_UPLOAD_LEVELS": (
            "structured_summary,representative_frames"
        ),
    }
    for name, value in configured.items():
        monkeypatch.setenv(name, value)

    status = public_model_status()
    runtime = build_aliyun_bailian_runtime(batch_id="policy-only-retention")

    assert status["status"] == "ready_for_shadow"
    assert status["retention_days_known"] is False
    assert status["gates"]["retention_policy_confirmed"] is True
    assert "bounded research-only shadow batch" in status["next_action"]
    assert runtime.data_permission.retention_days is None
    assert "terms.alicdn.com" in runtime.data_permission.retention_policy_reference
