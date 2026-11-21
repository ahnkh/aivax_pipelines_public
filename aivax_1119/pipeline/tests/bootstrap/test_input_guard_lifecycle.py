"""선택 가드의 조건부 로딩과 앱 수명주기를 검증한다."""

import builtins
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from src.bootstrap.env_loader import ConfigurationError, EnvironmentLoader
from src.bootstrap.pipeline_bootstrap import PipelineBootstrap


@pytest.mark.parametrize("failure", (None, "start", "shutdown"))
def test_lifespan_owns_guard_runtime_and_cleans_up_on_failure(failure):
    guard_runtime = SimpleNamespace(start=AsyncMock(), aclose=AsyncMock())
    inputs = SimpleNamespace(shutdown=AsyncMock())
    if failure == "start":
        guard_runtime.start.side_effect = RuntimeError("start failed")
    if failure == "shutdown":
        inputs.shutdown.side_effect = RuntimeError("shutdown failed")
    app = FastAPI(lifespan=main.lifespan)
    bootstrap = Mock()
    runtime = Mock()
    http = SimpleNamespace(aclose=AsyncMock())
    app.state.pipeline_bootstrap = bootstrap
    app.state.pipeline_analysis_modules = SimpleNamespace(
        input_guard_runtime=guard_runtime,
        input_analysis_use_case=inputs,
    )
    app.state.pipeline_runtime_modules = runtime
    app.state.http_client_registry = http

    def run():
        with TestClient(app):
            guard_runtime.start.assert_awaited_once()
            guard_runtime.aclose.assert_not_awaited()

    if failure:
        with pytest.raises(RuntimeError, match=failure):
            run()
    else:
        run()
    inputs.shutdown.assert_awaited_once()
    guard_runtime.aclose.assert_awaited_once()
    bootstrap.shutdown_runtime_modules.assert_called_once_with(runtime)
    http.aclose.assert_awaited_once()


@pytest.mark.parametrize("skip_unsupported", ("false", "true"))
@pytest.mark.parametrize("provider", ("fasoo", "markany"))
def test_disabled_cso_assembles_without_importing_optional_extension(
    monkeypatch, skip_unsupported, provider
):
    real_import = builtins.__import__

    def without_cso(name, *args, **kwargs):
        if name.startswith("src.extensions.cso"):
            raise AssertionError("disabled CSO extension must not be loaded")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_cso)
    settings = EnvironmentLoader(environ={}).build_settings(
        {
            "AIVAX_PIPELINE_CSO_PROVIDER": provider,
            "AIVAX_PIPELINE_CSO_SKIP_UNSUPPORTED_EXTENSIONS_ENABLED": skip_unsupported,
            "AIVAX_PIPELINE_CSO_SKIP_UNTAGGED_ENABLED": "true",
            "AIVAX_PIPELINE_CSO_SKIP_ERROR_ENABLED": "true",
        }
    )
    bootstrap = PipelineBootstrap(settings, None, Mock())
    assert bootstrap._create_input_guard(Mock()) is None
    assert bootstrap._input_guard_runtime is None


@pytest.mark.parametrize("value", ("0", "-1", "bad"))
def test_cso_startup_timeout_must_be_positive(value):
    with pytest.raises(ConfigurationError):
        EnvironmentLoader(environ={}).build_settings(
            {
                "AIVAX_PIPELINE_CSO_STARTUP_TIMEOUT_SECONDS": value,
            }
        )


def test_cso_startup_timeout_default_and_override():
    loader = EnvironmentLoader(environ={})
    assert loader.build_settings({}).cso_startup_timeout_seconds == 15
    assert (
        loader.build_settings(
            {
                "AIVAX_PIPELINE_CSO_STARTUP_TIMEOUT_SECONDS": "20",
            }
        ).cso_startup_timeout_seconds
        == 20
    )
