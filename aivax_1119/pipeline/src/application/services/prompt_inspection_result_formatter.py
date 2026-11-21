"""입력 검사 결과의 차단 안내와 마스킹 구간을 조립한다."""

import logging
from collections.abc import Mapping

from src.domain.inspection_input.models.block_phrases import get_block_phrases
from src.domain.inspection_input.models.masking import DEFAULT_MASK_TOKEN


class PromptInspectionResultFormatter:
    _LOGGER = logging.getLogger("khan-python")

    @staticmethod
    def _evidence_text(
        evidence: tuple[Mapping[str, object], ...],
    ) -> tuple[str, str, str]:
        kinds = {str(item.get("kind") or "") for item in evidence}
        types = ",".join(str(item.get("type") or "") for item in evidence)
        values = ",".join(str(item.get("value") or "") for item in evidence)
        if "pii" in kinds:
            return (
                "민감정보가 프롬프트 문맥에 포함된 것으로",
                types or "개인정보",
                values or "(탐지값 미제공)",
            )
        if "guardrail" in kinds:
            return (
                "가드레일 위반 가능성이 있는 요청으로",
                types or "가드레일",
                values or "(모델이 탐지값을 제공하지 않음)",
            )
        return (
            "악성·욕설·유해 콘텐츠가 포함된 것으로",
            types or "악성/욕설/유해",
            values or "(모델이 탐지값을 제공하지 않음)",
        )

    @staticmethod
    def _with_mask_spans(
        prompt: str,
        evidence: tuple[dict[str, object], ...],
    ) -> tuple[dict[str, object], ...]:
        expanded: list[dict[str, object]] = []
        for item in evidence:
            value = item.get("value")
            if (
                item.get("kind") != "pii"
                or item.get("action") != "masking"
                or not isinstance(value, str)
                or not value
            ):
                expanded.append(item)
                continue
            start = 0
            found = False
            while (index := prompt.find(value, start)) >= 0:
                found = True
                expanded.append(
                    {
                        **item,
                        "start": index,
                        "end": index + len(value),
                    }
                )
                start = index + len(value)

            if not found:
                PromptInspectionResultFormatter._LOGGER.warning(
                    "slm masking value was not found in prompt "
                    "(type=%s, value_length=%d, prompt_length=%d)",
                    item.get("type"),
                    len(value),
                    len(prompt),
                )
                expanded.append(item)
        return tuple(expanded)

    @staticmethod
    def _mask_prompt(
        prompt: str,
        evidence: tuple[Mapping[str, object], ...],
        *,
        fallback: str = "",
        mask_token: str = DEFAULT_MASK_TOKEN,
    ) -> str:
        spans: list[tuple[int, int]] = []
        for item in evidence:
            if item.get("action") not in (None, "masking"):
                continue
            start = item.get("start")
            end = item.get("end")
            if isinstance(start, int) and isinstance(end, int):
                spans.append((start, end))
        spans.sort()
        merged: list[tuple[int, int]] = []
        for start, end in spans:
            start = max(0, start)
            end = min(len(prompt), end)
            if start >= end:
                continue
            if not merged or start > merged[-1][1]:
                merged.append((start, end))
            else:
                merged[-1] = (
                    merged[-1][0],
                    max(merged[-1][1], end),
                )

        if not merged:
            return fallback or prompt

        parts: list[str] = []
        previous_end = 0
        for start, end in merged:
            parts.extend(
                (
                    prompt[previous_end:start],
                    mask_token,
                )
            )
            previous_end = end
        parts.append(prompt[previous_end:])
        return "".join(parts)

    @classmethod
    def _provider_block_message(
        cls,
        evidence: tuple[Mapping[str, object], ...],
        *,
        provider_name: str | None = None,
    ) -> str:
        grouped_types: dict[str, list[str]] = {}

        for item in evidence:
            if item.get("action") != "block":
                continue

            function = str(item.get("function") or "").strip()
            item_type = str(item.get("type") or "").strip()
            if not function:
                continue

            types = grouped_types.setdefault(function, [])
            if item_type and item_type not in types:
                types.append(item_type)

        reasons: list[str] = []
        for function, item_types in grouped_types.items():
            if item_types and not (len(item_types) == 1 and item_types[0] == function):
                reasons.append(
                    f"{function}({', '.join(item_types)})",
                )
            else:
                reasons.append(function)

        phrases = get_block_phrases()
        if provider_name == "secuXper":
            reason_text = ", ".join(reasons) if reasons else ""
            return phrases.secuxper_block(reason_text)

        if not reasons:
            return phrases.external_block()

        return phrases.external_block(", ".join(reasons))

    @classmethod
    def _block_message(cls, evidence: tuple[Mapping[str, object], ...]) -> str:
        detection_text, evidence_type, evidence_value = cls._evidence_text(evidence)
        return get_block_phrases().slm_block(
            detection_text, evidence_type, evidence_value
        )
