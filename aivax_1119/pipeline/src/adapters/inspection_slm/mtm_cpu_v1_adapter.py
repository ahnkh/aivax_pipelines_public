"""MTM CPU v1 SLM adapter."""

from src.adapters.inspection_slm.http_slm_client import HttpSlmClient
from src.adapters.inspection_slm.response import assistant_content
from src.domain.inspection_slm.models import SlmInspectionRequest, SlmInspectionResult
from src.domain.inspection_slm.ports import SlmInspectionPort


class MtmCpuV1SlmAdapter(SlmInspectionPort):
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
        return SlmInspectionResult(
            raw_content=content,
            unsafe=bool(content and "unsafe" in content),
            use_default_policy_metadata=True,
        )
