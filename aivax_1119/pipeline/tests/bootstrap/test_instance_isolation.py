from __future__ import annotations

from src.bootstrap import env_loader
from pathlib import Path


def _settings(instance: str, port: int):
    return env_loader.EnvironmentLoader().build_settings(
        {
            "AIVAX_PIPELINE_RUNTIME_DIR": f"./.runtime/{instance}",
            "AIVAX_PIPELINE_PORT": str(port),
        }
    )


def test_instance_specific_runtime_resources_do_not_overlap() -> None:
    first = _settings("dev-a", 9101)
    second = _settings("dev-b", 9102)

    assert first.port != second.port
    assert first.detail_reason_temp_dir != second.detail_reason_temp_dir


def test_repository_development_and_production_files_use_distinct_runtime_areas() -> (
    None
):
    root = Path(__file__).resolve().parents[2]
    _, production = env_loader.EnvironmentLoader(
        root, {"AIVAX_PIPELINE_ENV_FILE": ".env"}
    ).bootstrap()
    _, development = env_loader.EnvironmentLoader(
        root, {"AIVAX_PIPELINE_ENV_FILE": ".env.development"}
    ).bootstrap()

    assert production.runtime_dir != development.runtime_dir
    assert production.trace_log_path != development.trace_log_path
