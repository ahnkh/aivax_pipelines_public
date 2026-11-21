"""로그 테스트에서 명시적으로 배포하는 서비스 카탈로그."""

import json
from unittest.mock import Mock

from valkey import Valkey

from src.adapters.ai_service_catalog.valkey_ai_service_catalog import (
    ValkeyAiServiceCatalog,
)


def make_ai_service_catalog(
    names: dict[str, str] | None = None,
) -> ValkeyAiServiceCatalog:
    client = Mock(spec=Valkey)
    client.get.return_value = json.dumps(
        {
            "version": "test",
            "services": names
            if names is not None
            else {
                "0": "unknown",
                "1": "GPT",
                "2": "Gemini",
                "3": "Claude",
                "9": "GitHub Copilot",
                "10": "openapi.copilot",
                "13": "Genspark",
            },
        }
    )
    catalog = ValkeyAiServiceCatalog(client, "test:ai-service:catalog:v1")
    catalog.refresh()
    return catalog
