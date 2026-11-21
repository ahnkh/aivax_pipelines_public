from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.adapters.health.runtime_health_adapter import RuntimeHealthAdapter
from src.application.use_cases.check_health import CheckHealthUseCase, HealthProbeError
from src.bootstrap.env_loader import PipelineSettings
from src.controllers.health.health_controller import HealthController


def client_for(probes, *, timeout_seconds=2.0):
    app = FastAPI()
    app.include_router(
        HealthController(
            CheckHealthUseCase(probes, timeout_seconds=timeout_seconds)
        ).router
    )
    return TestClient(app)


@pytest.mark.parametrize("path", ["/", "/v1"])
def test_simple_status_matches_management_and_pipeline_smoke_contract(path):
    probe = AsyncMock(side_effect=RuntimeError("offline"))
    with client_for({"valkey": probe}) as client:
        response = client.get(path)
    assert response.status_code == 200
    assert response.json() == {"status": True}
    probe.assert_not_awaited()


@pytest.mark.parametrize("prefix", ["/v1/health", "/api/v1/health"])
@pytest.mark.parametrize("suffix", ["", "/live"])
def test_liveness_matches_management_and_never_calls_dependencies(prefix, suffix):
    probe = AsyncMock(side_effect=RuntimeError("offline"))
    with client_for({"valkey": probe}) as client:
        response = client.get(prefix + suffix)
    assert response.status_code == 200
    assert set(response.json()) == {"status", "timestamp"}
    assert response.json()["status"] == "ok"
    timestamp = response.json()["timestamp"]
    assert timestamp.endswith("Z")
    assert datetime.fromisoformat(timestamp).tzinfo is not None
    probe.assert_not_awaited()


@pytest.mark.parametrize("prefix", ["/v1/health", "/api/v1/health"])
def test_readiness_success_uses_management_field_names(prefix):
    with client_for(
        {"valkey": AsyncMock(return_value=None), "disk": AsyncMock(return_value=42.5)}
    ) as client:
        response = client.get(prefix + "/ready")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"status", "timestamp", "checks"}
    assert body["status"] == "ok"
    assert set(body["checks"]["valkey"]) == {"status", "latencyMs"}
    assert body["checks"]["disk"]["usedPercent"] == 42.5
    for item in body["checks"].values():
        assert item["status"] == "ok"
        assert isinstance(item["latencyMs"], int)
        assert item["latencyMs"] >= 0


def test_readiness_failure_returns_503_and_retains_other_checks():
    with client_for(
        {
            "valkey": AsyncMock(side_effect=RuntimeError("unreachable")),
            "disk": AsyncMock(side_effect=HealthProbeError("full", used_percent=95)),
            "logDirectory": AsyncMock(return_value=None),
        }
    ) as client:
        response = client.get("/v1/health/ready")
        live = client.get("/v1/health/live")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    assert body["checks"]["valkey"]["message"] == "unreachable"
    assert body["checks"]["disk"]["usedPercent"] == 95
    assert body["checks"]["logDirectory"]["status"] == "ok"
    assert live.status_code == 200


def test_slow_probe_is_cancelled_and_other_checks_still_complete():
    cancelled = []

    async def slow():
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.append(True)

    fast = AsyncMock(return_value=None)
    with client_for(
        {"valkey": slow, "logDirectory": fast}, timeout_seconds=0.02
    ) as client:
        response = client.get("/v1/health/ready")
    assert response.status_code == 503
    assert "타임아웃" in response.json()["checks"]["valkey"]["message"]
    assert response.json()["checks"]["logDirectory"]["status"] == "ok"
    assert cancelled == [True]
    fast.assert_awaited_once()


def make_adapter(tmp_path: Path, *, cds: bool = False):
    return RuntimeHealthAdapter(
        PipelineSettings(
            valkey_host="127.0.0.1",
            valkey_port=6399,
            valkey_db=2,
            valkey_password="test-secret",
        ),
        log_directory=str(tmp_path / "logs"),
        bulk_directory=str(tmp_path / "bulk"),
        cds_directory=str(tmp_path / "cds") if cds else None,
    )


def test_readiness_checks_real_directories_without_leaving_probe_files(tmp_path):
    adapter = make_adapter(tmp_path, cds=True)
    ping = AsyncMock(return_value=True)
    close = AsyncMock()
    with (
        patch(
            "src.adapters.health.runtime_health_adapter.Valkey",
            return_value=SimpleNamespace(ping=ping, aclose=close),
        ) as client_class,
        client_for(adapter.probes()) as client,
    ):
        response = client.get("/v1/health/ready")
    assert response.status_code == 200
    assert set(response.json()["checks"]) == {
        "valkey",
        "disk",
        "logDirectory",
        "bulkDirectory",
        "cdsDirectory",
    }
    for name in ("logs", "bulk", "cds"):
        assert (tmp_path / name).is_dir()
        assert list((tmp_path / name).iterdir()) == []
    ping.assert_awaited_once()
    close.assert_awaited_once()
    assert client_class.call_args.kwargs["db"] == 2
    assert client_class.call_args.kwargs["password"] == "test-secret"


def test_failed_directory_and_valkey_are_reported_without_exposing_credentials(
    tmp_path,
):
    (tmp_path / "bulk").write_text("not a directory")
    adapter = make_adapter(tmp_path)
    close = AsyncMock()
    with (
        patch(
            "src.adapters.health.runtime_health_adapter.Valkey",
            return_value=SimpleNamespace(
                ping=AsyncMock(side_effect=RuntimeError("test-secret")), aclose=close
            ),
        ),
        client_for(adapter.probes()) as client,
    ):
        response = client.get("/v1/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["bulkDirectory"]["status"] == "error"
    assert response.json()["checks"]["valkey"]["status"] == "error"
    assert "test-secret" not in response.text
    assert "cdsDirectory" not in response.json()["checks"]
    close.assert_awaited_once()


def test_disk_threshold_includes_used_percent_when_unhealthy(tmp_path):
    adapter = make_adapter(tmp_path)
    with patch(
        "src.adapters.health.runtime_health_adapter.shutil.disk_usage",
        return_value=SimpleNamespace(total=100, used=90, free=10),
    ):
        with pytest.raises(HealthProbeError) as error:
            adapter._check_disk()
    assert error.value.used_percent == 90


def test_main_exposes_health_routes_from_bootstrap_without_external_io():
    import main

    bootstrap = main.PipelineBootstrap(main.SETTINGS, None, SimpleNamespace())
    # Controller construction does not ping, write files or start background jobs.
    health = bootstrap._create_health_controller()
    app = FastAPI()
    assembled = SimpleNamespace(
        **{
            name: SimpleNamespace(router=FastAPI().router)
            for name in (
                "input_analysis_controller",
                "output_analysis_controller",
                "input_analysis_v2_controller",
                "output_analysis_v2_controller",
                "policy_refresh_controller",
                "test_filter_rule_controller",
            )
        },
        health_controller=health,
    )
    main.setup_fast_api(app, assembled)
    with TestClient(app) as client:
        assert client.get("/").json() == {"status": True}
        assert client.get("/v1").json() == {"status": True}
        assert client.get("/v1/health/live").status_code == 200
        assert client.get("/api/v1/health/live").status_code == 200
    paths = app.openapi()["paths"]
    assert "/v1/health/ready" in paths
    assert "/api/v1/health/ready" not in paths
