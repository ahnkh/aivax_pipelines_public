"""선택한 CSO 판독기와 필요한 런타임만 생성한다."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from src.domain.inspection_input.ports.cso_grade_port import CsoGradePort

if TYPE_CHECKING:
    from src.extensions.cso.jvm_process import CsoJvmProcess


def create_cso_grade_reader(
    provider: str,
    *,
    directory: Path,
    timeout_seconds: float,
    startup_timeout_seconds: float,
    logger: logging.Logger,
) -> tuple[CsoGradePort, CsoJvmProcess | None]:
    if provider == "markany":
        from src.extensions.cso.markany_grade_adapter import MarkAnyCsoGradeAdapter

        return MarkAnyCsoGradeAdapter(logger=logger), None
    if provider == "fasoo":
        from src.extensions.cso.fasoo_grade_adapter import FasooCsoGradeAdapter
        from src.extensions.cso.jvm_process import CsoJvmProcess

        runtime = CsoJvmProcess(
            directory,
            timeout_seconds=timeout_seconds,
            startup_timeout_seconds=startup_timeout_seconds,
            logger=logger,
        )
        return FasooCsoGradeAdapter(runtime), runtime
    raise ValueError(f"unsupported CSO provider: {provider}")
