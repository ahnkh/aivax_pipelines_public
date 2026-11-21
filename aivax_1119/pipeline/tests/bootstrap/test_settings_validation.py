from __future__ import annotations

import pytest

from src.bootstrap import env_loader


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AIVAX_PIPELINE_PORT", "not-a-port"),
        ("AIVAX_PIPELINE_PORT", "70000"),
        ("AIVAX_PIPELINE_LOG_LEVEL", "verbose"),
    ],
)
def test_invalid_runtime_values_fail_configuration_validation(
    name: str, value: str
) -> None:
    with pytest.raises(env_loader.ConfigurationError, match=name):
        env_loader.EnvironmentLoader().build_settings({name: value})


def test_tls_key_and_certificate_must_be_provided_together() -> None:
    with pytest.raises(env_loader.ConfigurationError, match="SSL"):
        env_loader.EnvironmentLoader().build_settings(
            {"AIVAX_PIPELINE_SSL_KEY_FILE": "./certs/server.key"}
        )


def test_multi_worker_memory_merge_is_rejected() -> None:
    with pytest.raises(env_loader.ConfigurationError, match="WORKERS"):
        env_loader.EnvironmentLoader().build_settings(
            {
                "AIVAX_PIPELINE_WORKERS": "2",
                "AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_BACKEND": "memory",
            }
        )


def test_multi_worker_valkey_merge_is_allowed() -> None:
    settings = env_loader.EnvironmentLoader().build_settings(
        {
            "AIVAX_PIPELINE_WORKERS": "4",
            "AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_BACKEND": "valkey",
        }
    )

    assert settings.workers == 4
    assert settings.analysis_log_merge_backend == "valkey"


def test_policy_refresh_stream_name_must_not_be_empty() -> None:
    with pytest.raises(
        env_loader.ConfigurationError,
        match="AIVAX_PIPELINE_POLICY_REFRESH_STREAM_NAME",
    ):
        env_loader.EnvironmentLoader().build_settings(
            {"AIVAX_PIPELINE_POLICY_REFRESH_STREAM_NAME": " "},
        )


def test_multimodal_execution_limits_are_configurable() -> None:
    settings = env_loader.EnvironmentLoader().build_settings(
        {
            "AIVAX_PIPELINE_INSPECT_MULTIMODAL_FILE_MAX_INFLIGHT": "7",
            "AIVAX_PIPELINE_INSPECT_MULTIMODAL_OCR_MAX_INFLIGHT": "3",
        }
    )

    assert settings.inspect_multimodal_file_max_inflight == 7
    assert settings.inspect_multimodal_ocr_max_inflight == 3


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, "pdfplumber"),
        ("", "pdfplumber"),
        ("pdfplumber", "pdfplumber"),
        ("pymupdf", "pymupdf"),
        ("PyMuPDF", "pymupdf"),
    ],
)
def test_pdf_evidence_engine_is_configurable(value, expected):
    values = {} if value is None else {"AIVAX_PIPELINE_PDF_EVIDENCE_ENGINE": value}
    settings = env_loader.EnvironmentLoader().build_settings(values)
    assert settings.pdf_evidence_engine == expected


def test_invalid_pdf_evidence_engine_is_rejected():
    with pytest.raises(
        env_loader.ConfigurationError, match="AIVAX_PIPELINE_PDF_EVIDENCE_ENGINE"
    ):
        env_loader.EnvironmentLoader().build_settings(
            {"AIVAX_PIPELINE_PDF_EVIDENCE_ENGINE": "other"}
        )


@pytest.mark.parametrize(
    "name",
    [
        "AIVAX_PIPELINE_INSPECT_MULTIMODAL_FILE_MAX_INFLIGHT",
        "AIVAX_PIPELINE_INSPECT_MULTIMODAL_OCR_MAX_INFLIGHT",
    ],
)
def test_multimodal_execution_limits_must_be_positive(name: str) -> None:
    with pytest.raises(env_loader.ConfigurationError, match=name):
        env_loader.EnvironmentLoader().build_settings({name: "0"})


def test_invalid_analysis_log_merge_backend_is_rejected() -> None:
    with pytest.raises(
        env_loader.ConfigurationError,
        match="AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_BACKEND",
    ):
        env_loader.EnvironmentLoader().build_settings(
            {"AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_BACKEND": "sqlite"},
        )


def test_valkey_merge_requires_non_empty_key_prefix() -> None:
    with pytest.raises(
        env_loader.ConfigurationError,
        match="AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_KEY_PREFIX",
    ):
        env_loader.EnvironmentLoader().build_settings(
            {
                "AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_BACKEND": "valkey",
                "AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_KEY_PREFIX": " ",
            }
        )


@pytest.mark.parametrize("value, expected", [(None, False), ("false", False), ("true", True)])
def test_toolkit_is_disabled_by_default(value, expected):
    values = {} if value is None else {"AIVAX_TOOLKIT_ENABLED": value}
    assert env_loader.EnvironmentLoader().build_settings(values).toolkit_enabled is expected


def test_toolkit_rejects_invalid_boolean():
    with pytest.raises(env_loader.ConfigurationError, match="AIVAX_TOOLKIT_ENABLED"):
        env_loader.EnvironmentLoader().build_settings({"AIVAX_TOOLKIT_ENABLED": "invalid"})
