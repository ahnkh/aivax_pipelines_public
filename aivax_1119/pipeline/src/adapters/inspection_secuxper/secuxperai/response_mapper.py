"""SecuXperAI 필터 응답을 secuXper 도메인 결과로 변환한다."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from src.domain.inspection_secuxper.models import SecuxperInspectionResult


class SecuxperaiResponseMapper:
    """SecuXperAI 응답 계약을 검증하고 정규화한다."""

    _ACTIONS = {
        0: "accept",
        1: "block",
        2: "masking",
    }
    _DETAIL_ACTIONS = {
        1: "block",
        2: "masking",
    }

    def __init__(
        self,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._logger = logger or logging.getLogger("khan-python")

    def to_result(
        self,
        response: Mapping[str, Any],
        prompt: str,
    ) -> SecuxperInspectionResult:
        """외부 응답을 검증하고 공용 secuXper 결과로 변환한다."""
        external_id = self._required_text(response, "id")
        chat_id = self._required_text(response, "chat_id")
        action = self._action(response.get("filter_result"))

        filter_message = response.get("filter_message")
        if not isinstance(filter_message, Mapping):
            raise ValueError(
                "SecuXperAI filter_message는 JSON object여야 합니다.",
            )

        filtered_content = filter_message.get("content")
        if not isinstance(filtered_content, str):
            raise ValueError(
                "SecuXperAI filter_message.content는 문자열이어야 합니다.",
            )

        message = filter_message.get("message")
        if not isinstance(message, Mapping):
            raise ValueError(
                "SecuXperAI filter_message.message는 JSON object여야 합니다.",
            )

        filter_detail = response.get("filter_detail")
        if not isinstance(filter_detail, list):
            raise ValueError(
                "SecuXperAI filter_detail은 배열이어야 합니다.",
            )

        evidence: list[dict[str, object]] = []
        for index, detail in enumerate(filter_detail):
            try:
                evidence.extend(
                    self._detail_evidence(
                        detail,
                        prompt=prompt,
                    ),
                )
            except (TypeError, ValueError) as error:
                # 일부 상세 결과가 잘못되어도 최상위 action과 나머지
                # 정상적인 상세 결과는 계속 적용한다.
                self._logger.warning(
                    "invalid SecuXperAI filter detail index=%s error=%s",
                    index,
                    error,
                )

        return SecuxperInspectionResult(
            evidence=evidence,
            provider_action=action,
            provider_filtered_content=filtered_content,
            provider_metadata={
                "provider": "secuxperai",
                "external_filter_id": external_id,
                "external_chat_id": chat_id,
            },
        )

    def _detail_evidence(
        self,
        detail: object,
        *,
        prompt: str,
    ) -> list[dict[str, object]]:
        if not isinstance(detail, Mapping):
            raise TypeError("상세 결과는 JSON object여야 합니다.")

        function = self._required_text(detail, "function")
        item = self._required_text(detail, "item")
        policy = self._integer(detail.get("policy"), "policy")
        detected_count = self._integer(
            detail.get("detected_count"),
            "detected_count",
        )

        if policy not in {0, 1, 2}:
            raise ValueError("policy는 0, 1, 2 중 하나여야 합니다.")
        if detected_count < 0:
            raise ValueError("detected_count는 음수일 수 없습니다.")

        # policy=0은 분류 결과일 뿐 차단·마스킹 근거가 아니다.
        if policy == 0:
            return []

        action = self._DETAIL_ACTIONS[policy]
        base_evidence: dict[str, object] = {
            "source": "secuxperai",
            "kind": self._kind(function),
            "function": function,
            "type": item,
            "action": action,
            "detected_count": detected_count,
        }

        detected_items = detail.get("detected_items")
        if detected_items is None:
            return [
                {
                    **base_evidence,
                    "value": "",
                }
            ]
        if not isinstance(detected_items, Mapping):
            raise TypeError(
                "detected_items는 JSON object여야 합니다.",
            )

        evidence: list[dict[str, object]] = []
        for raw_value, raw_positions in detected_items.items():
            if not isinstance(raw_value, str) or not raw_value:
                self._logger.warning(
                    "invalid SecuXperAI detected item value function=%s type=%s",
                    function,
                    item,
                )
                continue
            if not isinstance(raw_positions, list):
                self._logger.warning(
                    "invalid SecuXperAI detected item positions function=%s type=%s",
                    function,
                    item,
                )
                continue

            value_has_valid_position = False
            for raw_position in raw_positions:
                position = self._position(
                    raw_position,
                    prompt_length=len(prompt),
                )
                if position is None:
                    self._logger.warning(
                        "invalid SecuXperAI detected item position function=%s type=%s",
                        function,
                        item,
                    )
                    continue

                start, end = position
                value_has_valid_position = True
                evidence.append(
                    {
                        **base_evidence,
                        "value": raw_value,
                        "start": start,
                        "end": end,
                    }
                )

            # 탐지값은 유효하지만 위치만 잘못된 경우에도 근거는 보존한다.
            # masking에서는 외부 filtered content를 fallback으로 사용한다.
            if not value_has_valid_position:
                evidence.append(
                    {
                        **base_evidence,
                        "value": raw_value,
                    }
                )

        if evidence:
            return evidence

        return [
            {
                **base_evidence,
                "value": "",
            }
        ]

    @classmethod
    def _action(cls, value: object) -> str:
        # bool은 int의 하위 타입이므로 명시적으로 제외한다.
        if type(value) is not int or value not in cls._ACTIONS:
            raise ValueError(
                "SecuXperAI filter_result는 0, 1, 2 중 하나여야 합니다.",
            )
        return cls._ACTIONS[value]

    @staticmethod
    def _required_text(
        payload: Mapping[str, Any],
        field_name: str,
    ) -> str:
        value = payload.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"SecuXperAI {field_name}은 비어 있지 않은 문자열이어야 합니다.",
            )
        return value.strip()

    @staticmethod
    def _integer(
        value: object,
        field_name: str,
    ) -> int:
        if type(value) is not int:
            raise TypeError(
                f"SecuXperAI {field_name}은 정수여야 합니다.",
            )
        return value

    @staticmethod
    def _position(
        value: object,
        *,
        prompt_length: int,
    ) -> tuple[int, int] | None:
        if not isinstance(value, list | tuple) or len(value) != 2:
            return None

        start, end = value
        if type(start) is not int or type(end) is not int:
            return None
        if start < 0 or end <= start or end > prompt_length:
            return None

        return start, end

    @staticmethod
    def _kind(function: str) -> str:
        if function == "개인정보":
            return "pii"
        if function == "LLM 취약점":
            return "guardrail"
        if function in {"유해 콘텐츠", "유해컨텐츠"}:
            return "abuse"
        return "external"
