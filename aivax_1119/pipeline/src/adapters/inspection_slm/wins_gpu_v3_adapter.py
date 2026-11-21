"""WINS GPU v3 SLM adapter."""

from typing import Any

from src.adapters.inspection_slm.http_slm_client import HttpSlmClient
from src.adapters.inspection_slm.response import assistant_content, json_object
from src.domain.inspection_slm.models import SlmInspectionRequest, SlmInspectionResult
from src.domain.inspection_slm.ports import SlmInspectionPort


class WinsGpuV3SlmAdapter(SlmInspectionPort):
    _ABUSE_POLICY_NAME = "악성/욕설/유해 문장 탐지"

    def __init__(self, client: HttpSlmClient) -> None:
        self._client = client

    async def inspect(self, request: SlmInspectionRequest) -> SlmInspectionResult:
        response = await self._client.post(
            {
                "messages": [{"role": "user", "content": request.prompt}],
                "temperature": 0,
                "max_tokens": 3000,
                "repeat_penalty": 1.15,
            }
        )
        if response is None:
            return SlmInspectionResult(unavailable=True)
        content = assistant_content(response)
        parsed = json_object(content)
        if parsed is None:
            return SlmInspectionResult(raw_content=content)
        has_pii = parsed.get("has_pii") is True
        is_abuse = parsed.get("is_abuse") is True
        evidence = parsed.get("items")
        return SlmInspectionResult(
            raw_content=content,
            unsafe=has_pii or is_abuse,
            evidence=evidence if isinstance(evidence, list) else [],
            policy_name=self._ABUSE_POLICY_NAME if is_abuse else "",
            force_block_without_policy=True,
        )
