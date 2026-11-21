from __future__ import annotations

from pathlib import Path

import pytest

from src.bootstrap import env_loader


@pytest.mark.parametrize(
    "value, expected",
    [(None, True), ("true", True), ("false", False)],
)
def test_skipped_oversized_file_backup_is_configurable(value, expected) -> None:
    values = (
        {}
        if value is None
        else {"AIVAX_PIPELINE_BACKUP_SKIPPED_OVERSIZED_FILES": value}
    )

    settings = env_loader.EnvironmentLoader().build_settings(values)

    assert settings.backup_skipped_oversized_files is expected


def test_skipped_oversized_file_backup_rejects_invalid_boolean() -> None:
    with pytest.raises(
        env_loader.ConfigurationError,
        match="AIVAX_PIPELINE_BACKUP_SKIPPED_OVERSIZED_FILES",
    ):
        env_loader.EnvironmentLoader().build_settings(
            {"AIVAX_PIPELINE_BACKUP_SKIPPED_OVERSIZED_FILES": "invalid"}
        )


def test_selected_env_file_is_loaded_without_mutating_process_environment(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env.development").write_text(
        "AIVAX_PIPELINE_PORT=9101\nAIVAX_PIPELINE_LOG_LEVEL=debug\n",
        encoding="utf-8",
    )
    shell = {
        "AIVAX_PIPELINE_ENV_FILE": ".env.development",
        "AIVAX_PIPELINE_PORT": "9102",
    }

    loaded, settings = env_loader.EnvironmentLoader(tmp_path, shell).bootstrap()

    assert loaded is True
    assert settings.port == 9102
    assert settings.log_level == "debug"
    assert shell == {
        "AIVAX_PIPELINE_ENV_FILE": ".env.development",
        "AIVAX_PIPELINE_PORT": "9102",
    }


def test_policy_snapshot_settings_are_loaded_from_one_snapshot() -> None:
    settings = env_loader.EnvironmentLoader().build_settings(
        {
            "AIVAX_PIPELINE_RUNTIME_DIR": "./.runtime/dev-a",
            "AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_BACKEND": "valkey",
            "AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_KEY_PREFIX": "pipeline:test:merge:",
            "AIVAX_PIPELINE_POLICY_POLL_INTERVAL_SECONDS": "17",
            "AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_TTL_SECONDS": "301",
        }
    )

    assert settings.policy_poll_interval_seconds == 17
    assert settings.analysis_log_merge_backend == "valkey"
    assert settings.analysis_log_merge_key_prefix == "pipeline:test:merge:"
    assert settings.analysis_log_merge_ttl_seconds == 301
    assert settings.detail_reason_temp_dir.endswith(".runtime/dev-a/detail-reason")


def test_development_environment_uses_local_mode_file():
    from pathlib import Path

    loader = env_loader.EnvironmentLoader(
        root_dir=Path(__file__).resolve().parents[2],
        environ={"AIVAX_PIPELINE_ENV_FILE": ".env.development"},
    )
    _, settings = loader.bootstrap()
    assert Path(settings.product_mode_file).resolve() == (
        Path(__file__).resolve().parents[4] / "config/aivax/mode.json"
    )
