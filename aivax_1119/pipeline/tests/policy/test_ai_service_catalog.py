import json
from unittest.mock import Mock

import pytest
from valkey import Valkey

from src.adapters.ai_service_catalog.valkey_ai_service_catalog import (
    ValkeyAiServiceCatalog,
)
from src.application.use_cases.refresh_ai_service_catalog.use_case import (
    RefreshAiServiceCatalogUseCase,
)


def payload(names):
    return json.dumps({"version": "test", "services": names}).encode()


def test_reads_deployed_names_and_replaces_catalog_on_refresh(tmp_path):
    client = Mock(spec=Valkey)
    client.get.return_value = payload({"9": "GitHub Copilot", "42": "Custom service"})
    catalog = ValkeyAiServiceCatalog(
        client, "catalog", backup_path=tmp_path / "catalog.json"
    )
    refresh = RefreshAiServiceCatalogUseCase(catalog)

    assert refresh.execute() == 2
    assert catalog.name_for(9) == "GitHub Copilot"
    assert catalog.name_for(42) == "Custom service"
    assert catalog.name_for(None) == ""
    assert catalog.name_for(1) == ""
    client.get.assert_called_with("catalog")

    client.get.return_value = payload({"42": "Renamed service"})
    assert refresh.execute() == 1
    assert catalog.name_for(42) == "Renamed service"
    assert catalog.name_for(9) == ""

    client.get.return_value = payload({})
    assert refresh.execute() == 0
    assert catalog.name_for(42) == ""


@pytest.mark.parametrize(
    "broken",
    [None, b"invalid json", b"[]", payload({"9": None}), payload({"9": ""})],
)
def test_failed_refresh_and_restart_keep_last_valkey_snapshot(tmp_path, broken):
    client = Mock(spec=Valkey)
    client.get.return_value = payload({"9": "Deployed Copilot name"})
    backup = tmp_path / "catalog.json"
    catalog = ValkeyAiServiceCatalog(client, "catalog", backup_path=backup)
    catalog.refresh()

    client.get.return_value = broken
    catalog.refresh()
    assert catalog.name_for(9) == "Deployed Copilot name"
    restarted = ValkeyAiServiceCatalog(client, "catalog", backup_path=backup)
    restarted.refresh()
    assert restarted.name_for(9) == "Deployed Copilot name"


@pytest.mark.parametrize(
    "broken", [None, b"invalid json", ConnectionError("unavailable")]
)
def test_failed_refresh_restores_shared_backup_before_memory_cache(tmp_path, broken):
    client = Mock(spec=Valkey)
    backup = tmp_path / "catalog.json"
    client.get.return_value = payload({"9": "Old name"})
    catalog = ValkeyAiServiceCatalog(client, "catalog", backup_path=backup)
    catalog.refresh()

    client.get.return_value = payload({"9": "Updated by another worker"})
    other_worker = ValkeyAiServiceCatalog(client, "catalog", backup_path=backup)
    other_worker.refresh()

    if isinstance(broken, Exception):
        client.get.side_effect = broken
    else:
        client.get.return_value = broken
    catalog.refresh()

    assert catalog.name_for(9) == "Updated by another worker"


@pytest.mark.parametrize("backup_exists", [True, False])
def test_failed_refresh_keeps_memory_when_backup_is_unusable(tmp_path, backup_exists):
    client = Mock(spec=Valkey)
    backup = tmp_path / "catalog.json"
    client.get.return_value = payload({"9": "Current name"})
    catalog = ValkeyAiServiceCatalog(client, "catalog", backup_path=backup)
    catalog.refresh()

    if backup_exists:
        backup.write_bytes(b"invalid json")
    else:
        backup.unlink()
    client.get.side_effect = ConnectionError("unavailable")
    catalog.refresh()

    assert catalog.name_for(9) == "Current name"


def test_unavailable_valkey_without_snapshot_has_no_builtin_service_names():
    client = Mock(spec=Valkey)
    client.get.side_effect = ConnectionError("unavailable")
    catalog = ValkeyAiServiceCatalog(client, "catalog")
    assert catalog.refresh() == 0
    assert catalog.name_for(1) == ""
    assert catalog.name_for(9) == ""


def test_null_service_id_always_has_an_empty_name():
    client = Mock(spec=Valkey)
    client.get.return_value = payload({"None": "A service with a string ID"})
    catalog = ValkeyAiServiceCatalog(client, "catalog")
    catalog.refresh()
    assert catalog.name_for(None) == ""
