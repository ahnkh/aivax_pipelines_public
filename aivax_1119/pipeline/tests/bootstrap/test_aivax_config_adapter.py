import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.adapters.config_aivax import JsonFileAivaxConfigAdapter
from src.core.ddd.domain_entity import DomainEntity
from src.core.ddd.value_object import ValueObject
from src.domain.config_aivax.errors import AivaxConfigDomainError
from src.domain.config_aivax.models import AivaxConfig, AivaxMode


@pytest.mark.parametrize("schema_version", [1, 2])
def test_reads_and_caches_config_from_json_file(tmp_path, schema_version):
    mode_file = tmp_path / "mode.json"
    mode_file.write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "mode": "inline",
                "updated_at": "2026-09-06T15:35:44Z",
                "source": "ai_engine/deploy/inline/install.sh",
            }
        ),
        encoding="utf-8",
    )
    adapter = JsonFileAivaxConfigAdapter(str(mode_file))

    assert adapter.get_config() == AivaxConfig(
        schema_version=schema_version,
        mode=AivaxMode("inline"),
        updated_at=datetime(2026, 9, 6, 15, 35, 44, tzinfo=timezone.utc),
        source="ai_engine/deploy/inline/install.sh",
    )
    assert isinstance(adapter.get_config(), DomainEntity)
    assert isinstance(adapter.get_config().mode, ValueObject)

    mode_file.write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "mode": "mirror",
                "updated_at": "2026-09-07T00:00:00Z",
                "source": "test",
            }
        ),
        encoding="utf-8",
    )
    assert adapter.get_config().mode == AivaxMode("inline")

    adapter.reset_cache()
    assert adapter.get_config().mode == AivaxMode("mirror")


def test_mode_is_controlled_by_json_file(tmp_path):
    mode_file = tmp_path / "mode.json"
    mode_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "mirror",
                "updated_at": "2026-09-06T15:35:44Z",
                "source": "installer",
            }
        ),
        encoding="utf-8",
    )

    adapter = JsonFileAivaxConfigAdapter(str(mode_file))

    assert adapter.get_config().mode == AivaxMode("mirror")


def test_proxy_mode_is_accepted_and_stays_synchronous(tmp_path):
    mode_file = tmp_path / "mode.json"
    mode_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "proxy",
                "updated_at": "2026-11-07T12:00:00Z",
                "source": "aivax/scripts/ops/switch_aivax_mode.sh",
            }
        ),
        encoding="utf-8",
    )
    logger = MagicMock()
    config = JsonFileAivaxConfigAdapter(str(mode_file), logger=logger).get_config()

    assert config.mode == AivaxMode("proxy")
    assert config.is_proxy is True
    assert config.is_mirror is False
    logger.warning.assert_not_called()


def test_missing_or_invalid_mode_defaults_to_inline(tmp_path):
    logger = MagicMock()
    missing_file = tmp_path / "missing.json"
    invalid_file = tmp_path / "invalid.json"
    invalid_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "unknown",
                "updated_at": "2026-09-06T15:35:44Z",
                "source": "test",
            }
        ),
        encoding="utf-8",
    )

    assert (
        JsonFileAivaxConfigAdapter(
            str(missing_file),
            logger=logger,
        ).get_config()
        == AivaxConfig()
    )
    invalid_config = JsonFileAivaxConfigAdapter(
        str(invalid_file),
        logger=logger,
    ).get_config()
    assert invalid_config == AivaxConfig()
    assert logger.warning.call_count == 2


@pytest.mark.parametrize(
    "values",
    [
        {"schema_version": 0},
        {"schema_version": 3},
        {"schema_version": True},
        {"mode": "unknown"},
        {"updated_at": "2026-09-06T15:35:44Z"},
        {"source": ""},
    ],
)
def test_domain_entity_validates_during_initialization(values):
    with pytest.raises(AivaxConfigDomainError):
        AivaxConfig(**values)


def test_mode_value_object_validates_during_initialization():
    with pytest.raises(AivaxConfigDomainError):
        AivaxMode("unknown")


@pytest.mark.parametrize("schema_version", [1, 2])
def test_filters_defaults_and_partial_configuration(tmp_path, schema_version):
    mode_file = tmp_path / "mode.json"
    mode_file.write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "mode": "mirror",
                "filters": {
                    "secuxper": {"enabled": True},
                    "slm": {"enabled": False},
                },
            }
        )
    )
    config = JsonFileAivaxConfigAdapter(str(mode_file)).get_config()
    assert config.schema_version == schema_version
    assert config.mode == AivaxMode("mirror")
    assert config.filters.secuxper.enabled is True
    assert config.filters.slm.enabled is False
    assert config.filters.regex.enabled is True
    assert config.filters.multimodal.enabled is True
    assert AivaxConfig().filters.secuxper.enabled is False


@pytest.mark.parametrize(
    "filters",
    [
        {"secuxpr": {"enabled": True}},
        {"slm": {"enabled": "false"}},
        {"slm": {"enabled": 0}},
        {"slm": {"enable": False}},
        [],
    ],
)
def test_invalid_filters_log_warning_and_use_defaults(tmp_path, filters):
    mode_file = tmp_path / "mode.json"
    mode_file.write_text(json.dumps({"filters": filters}))
    logger = MagicMock()
    assert (
        JsonFileAivaxConfigAdapter(str(mode_file), logger).get_config() == AivaxConfig()
    )
    logger.warning.assert_called_once()
