import pytest

from src.bootstrap.env_loader import (
    ConfigurationError,
    EnvironmentLoader,
)


def test_cds_can_remain_disabled_without_api_credentials() -> None:
    settings = EnvironmentLoader(environ={}).build_settings(
        {
            "AIVAX_PIPELINE_CDS_DELIVERY_ENABLED": "false",
            "AIVAX_PIPELINE_CDS_API_URL": "",
            "AIVAX_PIPELINE_CDS_API_KEY": "",
        }
    )

    assert settings.cds_delivery_enabled is False
    assert settings.cds_api_url is None


def test_cds_enabled_requires_api_url() -> None:
    with pytest.raises(ConfigurationError):
        EnvironmentLoader(environ={}).build_settings(
            {
                "AIVAX_PIPELINE_CDS_DELIVERY_ENABLED": "true",
                "AIVAX_PIPELINE_CDS_API_KEY": "test-key",
            }
        )


def test_cds_enabled_requires_api_key() -> None:
    with pytest.raises(ConfigurationError):
        EnvironmentLoader(environ={}).build_settings(
            {
                "AIVAX_PIPELINE_CDS_DELIVERY_ENABLED": "true",
                "AIVAX_PIPELINE_CDS_API_URL": (
                    "http://192.168.0.10/api/ext/v1/traffic-logs"
                ),
            }
        )


def test_builds_enabled_cds_settings() -> None:
    settings = EnvironmentLoader(environ={}).build_settings(
        {
            "AIVAX_PIPELINE_CDS_DELIVERY_ENABLED": "true",
            "AIVAX_PIPELINE_CDS_API_URL": (
                "http://192.168.0.10/api/ext/v1/traffic-logs"
            ),
            "AIVAX_PIPELINE_CDS_API_KEY": "test-key",
            "AIVAX_PIPELINE_CDS_DELIVERY_BATCH_SIZE": "500",
            "AIVAX_PIPELINE_CDS_LOG_DIR": ".runtime/pipeline/cds-logs",
            "AIVAX_PIPELINE_MANAGEMENT_API_URL": ("http://127.0.0.1:3000"),
        }
    )

    assert settings.cds_delivery_enabled is True
    assert settings.cds_delivery_batch_size == 500
    assert settings.cds_log_dir == ".runtime/pipeline/cds-logs"
    assert settings.management_api_url == "http://127.0.0.1:3000"
