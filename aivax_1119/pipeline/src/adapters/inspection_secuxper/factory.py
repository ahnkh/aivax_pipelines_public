from typing import Any

from src.adapters.inspection_secuxper.secuxperai.adapter import (
    SecuxperaiInspectionAdapter,
)
from src.adapters.inspection_secuxper.secuxperai.http_client import (
    SecuxperaiHttpClient,
)
from src.adapters.inspection_secuxper.secuxperai.response_mapper import (
    SecuxperaiResponseMapper,
)
from src.adapters.inspection_secuxper.secuxperai.settings import (
    SecuxperaiSettings,
)
from src.adapters.inspection_secuxper.secuxperai.token_manager import (
    SecuxperaiTokenManager,
)
from src.adapters.inspection_secuxper.secuxperai.token_store import (
    SecuxperaiTokenStore,
)
from src.core.http import AsyncJsonHttpClient
from src.domain.inspection_secuxper.ports import SecuxperInspectionPort


def create_secuxper_inspection_adapter(
    settings: Any,
    http_client: AsyncJsonHttpClient,
    *,
    secuxperai_token_store: SecuxperaiTokenStore,
) -> SecuxperInspectionPort:
    if secuxperai_token_store is None:
        raise ValueError(
            "SecuXperAI model에는 token store가 필요합니다.",
        )

    secuxperai_settings = SecuxperaiSettings.from_runtime_settings(
        settings,
    )
    secuxperai_http_client = SecuxperaiHttpClient(
        secuxperai_settings,
        http_client,
    )
    return SecuxperaiInspectionAdapter(
        secuxperai_http_client,
        SecuxperaiTokenManager(
            secuxperai_settings,
            secuxperai_token_store,
            secuxperai_http_client,
        ),
        SecuxperaiResponseMapper(),
    )
