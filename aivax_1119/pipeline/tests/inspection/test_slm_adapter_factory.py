"""SLM 숫자 선택 설정과 실제 HTTP 응답 변환의 연결을 검증한다."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from src.adapters.inspection_slm.factory import create_slm_inspection_adapter
from src.adapters.inspection_slm.wins_gpu_v4_adapter import WinsGpuV4SlmAdapter
from src.adapters.inspection_slm.wins_safeguard_v1_0_adapter import (
    WinsSafeguardV1_0SlmAdapter,
)
from src.bootstrap.env_loader import ConfigurationError, EnvironmentLoader
from src.core.http import AsyncJsonHttpClient
from src.domain.inspection_slm.models import SlmInspectionRequest


@pytest.mark.parametrize("settings_format", ["runtime", "flat", "nested"])
def test_selection_5_builds_safeguard_v1_0_and_uses_configured_endpoint(
    settings_format: str,
) -> None:
    endpoint = "http://slm.example/v1/chat/completions"
    if settings_format == "runtime":
        settings = EnvironmentLoader(environ={}).build_settings(
            {
                "AIVAX_PIPELINE_SLM_MODEL_VERSION": "5",
                "AIVAX_PIPELINE_SLM_URL": endpoint,
            }
        )
    else:
        settings = {"model_version": 5, "slm_url": endpoint}
        if settings_format == "nested":
            settings = {"slm_pipeline_filter_module": settings}

    http_client = AsyncMock(spec=AsyncJsonHttpClient)
    http_client.request_json.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "risk": "H",
                            "pii": False,
                            "secret": True,
                            "abuse": False,
                            "gr": "NONE",
                            "items": [{"type": "PASSWORD", "value": "example"}],
                        }
                    )
                }
            }
        ]
    }
    adapter = create_slm_inspection_adapter(settings, http_client)
    result = asyncio.run(adapter.inspect(SlmInspectionRequest("password: example")))

    assert isinstance(adapter, WinsSafeguardV1_0SlmAdapter)
    assert result.unsafe
    assert not result.unavailable
    assert result.evidence == [{"kind": "pii", "type": "PASSWORD", "value": "example"}]
    http_client.request_json.assert_awaited_once_with(
        "POST",
        endpoint,
        json_body={
            "messages": [{"role": "user", "content": "password: example"}],
            "max_tokens": 3000,
        },
        headers={"Content-Type": "application/json"},
        timeout_seconds=10.0,
        connect_timeout_seconds=5.0,
    )


@pytest.mark.parametrize("settings_format", ["runtime", "flat", "nested"])
def test_default_selection_remains_wins_gpu_v4(settings_format: str) -> None:
    if settings_format == "runtime":
        settings = EnvironmentLoader(environ={}).build_settings({})
    elif settings_format == "nested":
        settings = {"slm_pipeline_filter_module": {}}
    else:
        settings = {}

    adapter = create_slm_inspection_adapter(
        settings, AsyncMock(spec=AsyncJsonHttpClient)
    )

    assert isinstance(adapter, WinsGpuV4SlmAdapter)


@pytest.mark.parametrize("version", ["0", "6"])
def test_unsupported_selection_is_rejected_during_settings_validation(
    version: str,
) -> None:
    with pytest.raises(ConfigurationError, match="AIVAX_PIPELINE_SLM_MODEL_VERSION"):
        EnvironmentLoader(environ={}).build_settings(
            {"AIVAX_PIPELINE_SLM_MODEL_VERSION": version}
        )
