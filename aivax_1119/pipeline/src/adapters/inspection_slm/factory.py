"""실행 설정에 맞는 SLM 버전 adapter를 조립한다."""

from typing import Any

from src.adapters.inspection_slm.http_slm_client import HttpSlmClient
from src.adapters.inspection_slm.mtm_cpu_v1_adapter import (
    MtmCpuV1SlmAdapter,
)
from src.adapters.inspection_slm.mtm_gpu_v2_adapter import (
    MtmGpuV2SlmAdapter,
)
from src.adapters.inspection_slm.settings import SlmHttpSettings
from src.adapters.inspection_slm.wins_gpu_v3_adapter import (
    WinsGpuV3SlmAdapter,
)
from src.adapters.inspection_slm.wins_gpu_v4_adapter import (
    WinsGpuV4SlmAdapter,
)
from src.adapters.inspection_slm.wins_safeguard_v1_0_adapter import (
    WinsSafeguardV1_0SlmAdapter,
)
from src.core.http import AsyncJsonHttpClient
from src.domain.inspection_slm.ports import SlmInspectionPort


def create_slm_inspection_adapter(
    settings: Any,
    http_client: AsyncJsonHttpClient,
) -> SlmInspectionPort:
    version = _model_version(settings)

    client = HttpSlmClient(
        SlmHttpSettings.from_runtime_settings(settings),
        http_client,
    )
    adapter_types = {
        1: MtmCpuV1SlmAdapter,
        2: MtmGpuV2SlmAdapter,
        3: WinsGpuV3SlmAdapter,
        4: WinsGpuV4SlmAdapter,
        5: WinsSafeguardV1_0SlmAdapter,
    }
    try:
        return adapter_types[version](client)
    except KeyError as error:
        raise ValueError(
            f"unsupported SLM model version: {version}",
        ) from error


def _model_version(settings: Any) -> int:
    if isinstance(settings, dict):
        values = settings.get("slm_pipeline_filter_module", settings)
        return int(values.get("model_version", 4))
    return int(settings.slm_model_version)
