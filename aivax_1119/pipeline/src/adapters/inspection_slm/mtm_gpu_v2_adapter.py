"""MTM GPU v2 SLM adapter."""

from collections.abc import Mapping

from src.adapters.inspection_slm.http_slm_client import HttpSlmClient
import json

from src.adapters.inspection_slm.response import assistant_content
from src.domain.inspection_slm.models import SlmInspectionRequest, SlmInspectionResult
from src.domain.inspection_slm.ports import SlmInspectionPort


class MtmGpuV2SlmAdapter(SlmInspectionPort):
    def __init__(self, client: HttpSlmClient) -> None:
        self._client = client

    async def inspect(self, request: SlmInspectionRequest) -> SlmInspectionResult:
        response = await self._client.post(
            {
                "model": "cipherguard01",
                "messages": [{"role": "user", "content": request.prompt}],
                "temperature": 0.0,
                "max_tokens": 2048,
            }
        )
        if response is None:
            return SlmInspectionResult(unavailable=True)
        content = assistant_content(response)
        try:
            parsed = (
                json.loads(
                    content.replace("```json", "").replace("```", "").strip(),
                )
                if content
                else None
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        parsed = parsed if isinstance(parsed, dict) else None
        items = parsed.get("detected_items") if parsed else None
        first = items[0] if isinstance(items, list) and items else None
        if not isinstance(first, Mapping):
            return SlmInspectionResult(
                raw_content=content, use_default_policy_metadata=True
            )
        category = first.get("category")
        confidence = first.get("confidence")
        unsafe = (
            isinstance(category, str)
            and "없음" not in category
            and isinstance(confidence, (int, float))
            and confidence > 0
        )
        return SlmInspectionResult(
            raw_content=content,
            unsafe=unsafe,
            use_default_policy_metadata=True,
        )
