"""SafeGuard v1.0 JSON 응답을 기존 AIVAX SLM 탐지 형식으로 변환한다."""

import json
import logging
from typing import Any

from src.adapters.inspection_slm.http_slm_client import HttpSlmClient
from src.adapters.inspection_slm.response import assistant_content, json_object
from src.domain.inspection_slm.errors import SlmInspectionError
from src.domain.inspection_slm.models import (
    SlmInspectionRequest,
    SlmInspectionResult,
    SlmRiskCode,
)
from src.domain.inspection_slm.ports import SlmInspectionPort

_LOGGER = logging.getLogger("khan-python")


class WinsSafeguardV1_0SlmAdapter(SlmInspectionPort):
    """탐지만 정규화하며, 최종 조치는 기존 AIVAX 정책에 맡긴다."""

    def __init__(self, client: HttpSlmClient) -> None:
        self._client = client

    async def inspect(self, request: SlmInspectionRequest) -> SlmInspectionResult:
        payload = {
            "messages": [{"role": "user", "content": request.prompt}],
            "max_tokens": 3000,
        }
        response = await self._client.post(payload)
        if response is None:
            return SlmInspectionResult(unavailable=True)

        def log_error(message: str, *args: object) -> None:
            _LOGGER.error(
                message + " request_body=%s response_body=%s",
                *args,
                json.dumps(payload, ensure_ascii=False),
                json.dumps(response, ensure_ascii=False),
                stacklevel=2,
            )

        content = assistant_content(response)
        parsed = json_object(content)
        if parsed is None:
            reason = (
                "choices[0].message.content는 문자열이어야 합니다."
                if content is None
                else "assistant content는 JSON 객체여야 합니다."
            )
            log_error(
                "invalid safeguard v1.0 response content: %s (content_length=%d)",
                reason,
                len(content or ""),
            )
            return SlmInspectionResult(
                raw_content=content,
                unavailable=True,
                error=SlmInspectionError.invalid_response(),
            )

        try:
            risk = self._validate_response(parsed)
        except ValueError as error:
            log_error("invalid safeguard v1.0 response: %s", error)
            return SlmInspectionResult(
                raw_content=content,
                unavailable=True,
                error=SlmInspectionError.invalid_response(),
            )

        # 자격증명도 현재 정책 계약의 kind=pii, type별 규칙으로 처리한다.
        evidence: list[dict[str, Any]] = [
            {"kind": "pii", "type": item["type"], "value": item["value"]}
            for item in parsed["items"]
        ]
        if (parsed["pii"] or parsed["secret"]) and not evidence:
            log_error(
                "safeguard v1.0 reports pii or secret but valid items are missing"
            )

        if parsed["gr"] != "NONE":
            evidence.append({"kind": "guardrail", "type": parsed["gr"], "value": ""})
        if parsed["abuse"]:
            evidence.append({"kind": "abuse", "type": None, "value": ""})

        # risk는 관측값으로 보존하고, 최종 조치는 AIVAX 정책에 맡긴다.
        return SlmInspectionResult(
            raw_content=content,
            unsafe=parsed["pii"] or parsed["secret"] or bool(evidence),
            evidence=evidence,
            use_default_policy_metadata=True,
            risk=risk,
        )

    @staticmethod
    def _validate_response(parsed: dict[str, Any]) -> SlmRiskCode:
        for field in ("pii", "secret", "abuse"):
            if not isinstance(parsed.get(field), bool):
                raise ValueError(f"{field}는 boolean이어야 합니다.")

        try:
            risk = SlmRiskCode(parsed.get("risk"))
        except (TypeError, ValueError) as error:
            raise ValueError("risk는 P, L, M, H 중 하나여야 합니다.") from error

        guardrail = parsed.get("gr")
        if not isinstance(guardrail, str) or not guardrail.strip():
            raise ValueError("gr은 비어 있지 않은 문자열이어야 합니다.")

        items = parsed.get("items")
        if not isinstance(items, list):
            raise ValueError("items는 배열이어야 합니다.")

        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"items[{index}]는 JSON 객체여야 합니다.")
            if not isinstance(item.get("type"), str) or not item["type"].strip():
                raise ValueError(
                    f"items[{index}].type은 비어 있지 않은 문자열이어야 합니다.",
                )
            if not isinstance(item.get("value"), str) or not item["value"]:
                raise ValueError(
                    f"items[{index}].value는 비어 있지 않은 문자열이어야 합니다.",
                )

        return risk
