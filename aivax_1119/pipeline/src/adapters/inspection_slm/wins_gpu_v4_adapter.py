"""WINS GPU v4 SLM adapter."""

import json
import logging

from src.adapters.inspection_slm.http_slm_client import HttpSlmClient
from src.adapters.inspection_slm.response import assistant_content, json_object
from src.domain.inspection_slm.models import SlmInspectionRequest, SlmInspectionResult
from src.domain.inspection_slm.ports import SlmInspectionPort

_LOGGER = logging.getLogger("khan-python")


class WinsGpuV4SlmAdapter(SlmInspectionPort):
    _GUARD_RAIL_DEFAULT = "NONE"

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
            log_error(
                "invalid slm response content (content_length=%d)",
                len(content or ""),
            )
            return SlmInspectionResult(raw_content=content)

        raw_has_pii = parsed.get("has_pii")
        raw_is_abuse = parsed.get("is_abuse")
        guardrail = parsed.get("guardrail")
        items = parsed.get("items")

        if not isinstance(raw_has_pii, bool):
            log_error(
                "invalid slm response field type (field=has_pii, actual_type=%s)",
                type(raw_has_pii).__name__,
            )

        if not isinstance(raw_is_abuse, bool):
            log_error(
                "invalid slm response field type (field=is_abuse, actual_type=%s)",
                type(raw_is_abuse).__name__,
            )

        if not isinstance(guardrail, str):
            log_error(
                "invalid slm response field type (field=guardrail, actual_type=%s)",
                type(guardrail).__name__,
            )

        if not isinstance(items, list):
            log_error(
                "invalid slm response field type (field=items, actual_type=%s)",
                type(items).__name__,
            )
            items = []

        has_pii = raw_has_pii is True
        is_abuse = raw_is_abuse is True

        evidence: list[dict[str, object]] = []
        pii_count = 0

        for index, item in enumerate(items):
            if not isinstance(item, dict):
                log_error(
                    "invalid slm pii item (index=%d, actual_type=%s)",
                    index,
                    type(item).__name__,
                )
                continue

            item_type = item.get("type")
            if not isinstance(item_type, str) or not item_type.strip():
                log_error(
                    "invalid slm pii item type (index=%d, actual_type=%s)",
                    index,
                    type(item_type).__name__,
                )
                continue

            item_value = item.get("value")
            if not isinstance(item_value, str):
                log_error(
                    "invalid slm pii item value (index=%d, type=%s, actual_type=%s)",
                    index,
                    item_type.strip(),
                    type(item_value).__name__,
                )
                item_value = ""

            evidence.append(
                {
                    "kind": "pii",
                    "type": item_type.strip(),
                    "value": item_value,
                }
            )
            pii_count += 1

        if has_pii and pii_count == 0:
            log_error("slm response has_pii is true but valid pii items are missing")

        if not has_pii and pii_count > 0:
            log_error(
                "slm response has_pii is false but pii items exist (pii_count=%d)",
                pii_count,
            )

        if (
            isinstance(guardrail, str)
            and guardrail
            and guardrail != self._GUARD_RAIL_DEFAULT
        ):
            evidence.append(
                {
                    "kind": "guardrail",
                    "type": guardrail,
                    "value": "",
                }
            )

        if is_abuse:
            evidence.append(
                {
                    "kind": "abuse",
                    "type": None,
                    "value": "",
                }
            )

        return SlmInspectionResult(
            raw_content=content,
            unsafe=has_pii or bool(evidence),
            evidence=evidence,
            use_default_policy_metadata=True,
        )
