from __future__ import annotations

from src.bootstrap import env_loader


def _settings() -> env_loader.PipelineSettings:
    return env_loader.EnvironmentLoader().build_settings(
        {
            "AIVAX_PIPELINE_EVENT_FLUSH_INTERVAL_SECONDS": "1",
            "AIVAX_PIPELINE_EVENT_DEDUPE_TTL_SECONDS": "86400",
            "AIVAX_PIPELINE_EVENT_USER_SYNC_STREAM_NAME": "management:test-user-sync:v1",
            "AIVAX_PIPELINE_EVENT_POLICY_NOTIFICATION_STREAM_NAME": "management:test-policy-notification:v1",
            "AIVAX_PIPELINE_POLICY_REGEX_KEY": "test:regex",
            "AIVAX_PIPELINE_POLICY_MULTIMODAL_KEY": "test:multimodal",
            "AIVAX_PIPELINE_POLICY_SLM_KEY": "test:slm",
            "AIVAX_PIPELINE_AI_SERVICE_CATALOG_KEY": "test:ai-services",
        }
    )


def test_user_sync_stream_uses_typed_settings() -> None:
    assert _settings().event_user_sync_stream_name == "management:test-user-sync:v1"


def test_policy_notification_stream_uses_typed_settings() -> None:
    assert (
        _settings().event_policy_notification_stream_name
        == "management:test-policy-notification:v1"
    )


def test_event_flush_interval_uses_typed_settings() -> None:
    assert _settings().event_flush_interval_seconds == 1
    assert _settings().event_dedupe_ttl_seconds == 86_400


def test_policy_valkey_keys_use_typed_settings() -> None:
    settings = _settings()
    assert settings.policy_regex_key == "test:regex"
    assert settings.policy_multimodal_key == "test:multimodal"
    assert settings.policy_slm_key == "test:slm"
    assert settings.ai_service_catalog_key == "test:ai-services"
