"""프로세스 공용 logger의 초기 설정."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from uvicorn.logging import DefaultFormatter

from src.bootstrap.env_loader import PipelineSettings


_LOGGER_NAME = "khan-python"

_FILE_FORMAT = (
    "%(levelname)s [%(asctime)s]"
    "[%(process)d]"
    "[%(filename)s:%(funcName)s:%(lineno)d] "
    "%(message)s"
)

_STREAM_FORMAT = (
    "%(levelprefix)s "
    "\033[2m[%(asctime)s]"
    "[%(process)d]"
    "[%(filename)s:%(funcName)s:%(lineno)d]\033[0m "
    "\033[36m│\033[0m "
    "\033[1m%(message)s\033[0m"
)

_LOG_FILE_NAME = "pipeline_log.txt"


def configure_logging(settings: PipelineSettings) -> logging.Logger:
    """설정마다 하나의 파일 handler와 선택적 stream handler만 유지한다."""
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(settings.log_level.upper())
    logger.propagate = False

    for handler in tuple(logger.handlers):
        if getattr(handler, "_aivax_pipeline_handler", False):
            logger.removeHandler(handler)
            handler.close()

    # 파일에는 ANSI color를 넣지 않음
    file_formatter = logging.Formatter(_FILE_FORMAT)

    log_directory = Path(settings.trace_log_path)
    log_directory.mkdir(mode=0o750, parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        log_directory / _LOG_FILE_NAME,
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setFormatter(file_formatter)
    file_handler._aivax_pipeline_handler = True  # type: ignore[attr-defined]
    logger.addHandler(file_handler)

    # console log
    stream_formatter = DefaultFormatter(
        _STREAM_FORMAT,
        use_colors=True,
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(stream_formatter)
    stream_handler._aivax_pipeline_handler = True  # type: ignore[attr-defined]
    logger.addHandler(stream_handler)

    return logger
