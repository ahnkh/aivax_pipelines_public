"""SLM 버전 adapter와 레거시 정책 경계의 계약 테스트."""

from __future__ import annotations

import asyncio

from src.adapters.inspection_slm.mtm_cpu_v1_adapter import MtmCpuV1SlmAdapter
from src.adapters.inspection_slm.mtm_gpu_v2_adapter import MtmGpuV2SlmAdapter
from src.adapters.inspection_slm.wins_gpu_v3_adapter import WinsGpuV3SlmAdapter
from src.adapters.inspection_slm.wins_gpu_v4_adapter import WinsGpuV4SlmAdapter
from src.domain.inspection_slm.models import SlmInspectionRequest


class StubSlmClient:
    def __init__(self, response: dict | None) -> None:
        self.response = response
        self.payloads: list[dict] = []

    async def post(self, payload: dict) -> dict | None:
        self.payloads.append(payload)
        return self.response

    async def aclose(self) -> None:
        return None


def response(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def test_model_adapters_preserve_version_specific_payloads_and_detection() -> None:
    mtm_client = StubSlmClient(response("unsafe"))
    mtm = asyncio.run(
        MtmCpuV1SlmAdapter(mtm_client).inspect(
            SlmInspectionRequest("prompt"),
        )
    )

    assert mtm.unsafe
    assert mtm.use_default_policy_metadata
    assert mtm_client.payloads[0]["model"] == "cipherguard01"
    assert mtm_client.payloads[0]["max_tokens"] == 2048

    gpu = asyncio.run(
        MtmGpuV2SlmAdapter(
            StubSlmClient(
                response(
                    '{"detected_items": [{"category": "개인정보", "confidence": 0.9}]}',
                )
            )
        ).inspect(SlmInspectionRequest("prompt"))
    )

    assert gpu.unsafe
    assert gpu.use_default_policy_metadata

    wins3 = asyncio.run(
        WinsGpuV3SlmAdapter(
            StubSlmClient(
                response(
                    '{"has_pii": false, "is_abuse": true, "items": []}',
                )
            )
        ).inspect(SlmInspectionRequest("prompt"))
    )

    assert wins3.policy_name == "악성/욕설/유해 문장 탐지"
    assert wins3.policy_category == ""
    assert wins3.force_block_without_policy

    wins4 = asyncio.run(
        WinsGpuV4SlmAdapter(
            StubSlmClient(
                response(
                    '{"has_pii": true, "is_abuse": false, "guardrail": "NONE", '
                    '"items": [{"type": "PASSWORD", "value": "secret"}]}',
                )
            )
        ).inspect(SlmInspectionRequest("prompt"))
    )

    assert wins4.unsafe
    assert wins4.use_default_policy_metadata
    assert wins4.evidence == [
        {
            "kind": "pii",
            "type": "PASSWORD",
            "value": "secret",
        }
    ]


def test_wins_v4_extracts_guardrail_evidence() -> None:
    result = asyncio.run(
        WinsGpuV4SlmAdapter(
            StubSlmClient(
                response(
                    '{"has_pii": false, "is_abuse": false, '
                    '"guardrail": "PROMPT_INJECTION", "items": []}',
                )
            )
        ).inspect(SlmInspectionRequest("prompt"))
    )

    assert result.unsafe
    assert result.use_default_policy_metadata
    assert result.evidence == [
        {
            "kind": "guardrail",
            "type": "PROMPT_INJECTION",
            "value": "",
        }
    ]


def test_wins_v4_extracts_abuse_evidence() -> None:
    result = asyncio.run(
        WinsGpuV4SlmAdapter(
            StubSlmClient(
                response(
                    '{"has_pii": false, "is_abuse": true, '
                    '"guardrail": "NONE", "items": []}',
                )
            )
        ).inspect(SlmInspectionRequest("prompt"))
    )

    assert result.unsafe
    assert result.evidence == [
        {
            "kind": "abuse",
            "type": None,
            "value": "",
        }
    ]


def test_wins_v4_extracts_all_detection_kinds() -> None:
    result = asyncio.run(
        WinsGpuV4SlmAdapter(
            StubSlmClient(
                response(
                    '{"has_pii": true, "is_abuse": true, '
                    '"guardrail": "PROMPT_INJECTION", '
                    '"items": [{"type": "EMAIL", "value": "user@example.com"}]}',
                )
            )
        ).inspect(SlmInspectionRequest("prompt"))
    )

    assert result.unsafe
    assert result.evidence == [
        {
            "kind": "pii",
            "type": "EMAIL",
            "value": "user@example.com",
        },
        {
            "kind": "guardrail",
            "type": "PROMPT_INJECTION",
            "value": "",
        },
        {
            "kind": "abuse",
            "type": None,
            "value": "",
        },
    ]


def test_wins_v4_marks_has_pii_without_items_unsafe(caplog) -> None:
    result = asyncio.run(
        WinsGpuV4SlmAdapter(
            StubSlmClient(
                response(
                    '{"has_pii": true, "is_abuse": false, '
                    '"guardrail": "NONE", "items": []}',
                )
            )
        ).inspect(SlmInspectionRequest("prompt"))
    )

    assert result.unsafe
    assert result.evidence == []
    assert "valid pii items are missing" in caplog.text


def test_wins_v4_keeps_items_when_has_pii_is_false(caplog) -> None:
    result = asyncio.run(
        WinsGpuV4SlmAdapter(
            StubSlmClient(
                response(
                    '{"has_pii": false, "is_abuse": false, "guardrail": "NONE", '
                    '"items": [{"type": "EMAIL", "value": "user@example.com"}]}',
                )
            )
        ).inspect(SlmInspectionRequest("prompt"))
    )

    assert result.unsafe
    assert result.evidence == [
        {
            "kind": "pii",
            "type": "EMAIL",
            "value": "user@example.com",
        }
    ]
    assert "has_pii is false but pii items exist" in caplog.text


def test_wins_v4_logs_invalid_response_field_types(caplog) -> None:
    result = asyncio.run(
        WinsGpuV4SlmAdapter(
            StubSlmClient(
                response(
                    '{"has_pii": "true", "is_abuse": 0, '
                    '"guardrail": null, "items": {}}',
                )
            )
        ).inspect(SlmInspectionRequest("prompt"))
    )

    assert not result.unsafe
    assert result.evidence == []
    assert "field=has_pii" in caplog.text
    assert "field=is_abuse" in caplog.text
    assert "field=guardrail" in caplog.text
    assert "field=items" in caplog.text


def test_wins_v4_logs_invalid_json_content(caplog) -> None:
    result = asyncio.run(
        WinsGpuV4SlmAdapter(
            StubSlmClient(
                response(
                    "invalid json",
                )
            )
        ).inspect(SlmInspectionRequest("prompt"))
    )

    assert not result.unsafe
    assert result.evidence == []
    assert "invalid slm response content" in caplog.text


def test_transport_failure_is_explicitly_unavailable() -> None:
    result = asyncio.run(
        WinsGpuV4SlmAdapter(StubSlmClient(None)).inspect(
            SlmInspectionRequest("prompt"),
        )
    )

    assert result.unavailable
    assert not result.unsafe
