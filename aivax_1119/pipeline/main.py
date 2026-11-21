import logging
import os
import traceback
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Literal, TypedDict

import uvicorn
import uvloop
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from src.bootstrap import (
    PipelineAnalysisModules,
    PipelineBootstrap,
    configure_logging,
)
from src.bootstrap.env_loader import SETTINGS
from src.controllers.documentation import setup_documentation
from src.core.constants import ERR_OK
from src.core.http import HttpClientRegistry
from src.core.http.request_logging_middleware import RequestLoggingMiddleware


class _UvicornOptions(TypedDict):
    """Pipeline이 Uvicorn에 전달하는 고정 실행 옵션."""

    host: str
    port: int
    workers: int
    ssl_keyfile: str | None
    ssl_certfile: str | None
    log_level: str
    loop: Literal["uvloop"]
    http: Literal["httptools"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap: PipelineBootstrap = app.state.pipeline_bootstrap
    analysis_modules = app.state.pipeline_analysis_modules
    guard_runtime = analysis_modules.input_guard_runtime
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(app.state.http_client_registry.aclose)
        cleanup.callback(
            bootstrap.shutdown_runtime_modules, app.state.pipeline_runtime_modules
        )
        if guard_runtime is not None:
            cleanup.push_async_callback(guard_runtime.aclose)
        cleanup.push_async_callback(analysis_modules.input_analysis_use_case.shutdown)
        if guard_runtime is not None:
            await guard_runtime.start()
        yield


uvloop.install()


def setup_fast_api(
    fastApi: FastAPI,
    analysis_modules: PipelineAnalysisModules,
) -> int:
    fastApi.add_middleware(
        CORSMiddleware,
        allow_origins=SETTINGS.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    fastApi.add_middleware(
        RequestLoggingMiddleware, logger=logging.getLogger("khan-python")
    )

    fastApi.include_router(analysis_modules.input_analysis_controller.router)
    fastApi.include_router(analysis_modules.output_analysis_controller.router)
    fastApi.include_router(analysis_modules.input_analysis_v2_controller.router)
    fastApi.include_router(analysis_modules.output_analysis_v2_controller.router)
    fastApi.include_router(analysis_modules.policy_refresh_controller.router)
    fastApi.include_router(analysis_modules.test_filter_rule_controller.router)
    fastApi.include_router(analysis_modules.health_controller.router)

    return ERR_OK


def create_app() -> FastAPI:
    """Build one fully initialized application for a Uvicorn worker."""
    logger = configure_logging(SETTINGS)

    fast_api = FastAPI(
        docs_url="/docs",
        redoc_url=None,
        root_path="",
        openapi_url="/openapi.json",
        lifespan=lifespan,
        default_response_class=ORJSONResponse,
    )
    setup_documentation(fast_api)
    bootstrap = PipelineBootstrap(
        SETTINGS,
        HttpClientRegistry(),
        logger,
    )
    runtime_modules = bootstrap.assemble_runtime_modules()
    analysis_modules = bootstrap.assemble_analysis_modules(runtime_modules)
    fast_api.state.http_client_registry = bootstrap.http_client_registry
    fast_api.state.pipeline_bootstrap = bootstrap
    fast_api.state.pipeline_runtime_modules = runtime_modules
    fast_api.state.pipeline_analysis_modules = analysis_modules
    setup_fast_api(fast_api, analysis_modules)
    return fast_api


def run_uvicorn() -> int:
    """Build locally for one worker; let multiple workers import the factory."""
    options: _UvicornOptions = {
        "host": SETTINGS.host,
        "port": SETTINGS.port,
        "workers": SETTINGS.workers,
        "ssl_keyfile": SETTINGS.ssl_key_file,
        "ssl_certfile": SETTINGS.ssl_cert_file,
        "log_level": SETTINGS.log_level,
        "loop": "uvloop",
        "http": "httptools",
    }

    if SETTINGS.workers > 1:
        uvicorn.run("main:create_app", factory=True, **options)
    else:
        uvicorn.run(create_app(), **options)

    return ERR_OK


def main():
    logger = configure_logging(SETTINGS)
    try:
        run_uvicorn()
    except Exception:
        logger.error(traceback.format_exc())
    finally:
        logger.info(f"end process pid = {os.getpid()}")


if __name__ == "__main__":
    main()
    pass
