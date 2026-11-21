from unittest.mock import Mock, patch

import pytest

from src.bootstrap.env_loader import PipelineSettings
from src.core.valkey.connecter import ValkeyConnecter


def test_connect_returns_one_verified_client_and_disconnect_closes_it() -> None:
    client = Mock()
    settings = PipelineSettings(
        valkey_host="valkey.internal",
        valkey_port=6380,
        valkey_db=2,
        valkey_password="secret",
        valkey_socket_connect_timeout_ms=100,
        valkey_socket_timeout_ms=300,
    )
    with (
        patch("src.core.valkey.connecter.Valkey", return_value=client) as factory,
    ):
        connection = ValkeyConnecter(settings)

        assert connection.connect() is client
        assert connection.connect() is client
        client.ping.assert_called_once_with()
        factory.assert_called_once_with(
            host="valkey.internal",
            port=6380,
            db=2,
            password="secret",
            socket_connect_timeout=0.1,
            socket_timeout=0.3,
            retry_on_timeout=False,
        )

        connection.disconnect()
        assert connection.is_connected is False
        client.close.assert_called_once_with()


def test_failed_verification_closes_client_and_allows_retry() -> None:
    failed_client = Mock()
    failed_client.ping.side_effect = ConnectionError("unavailable")
    recovered_client = Mock()
    with patch(
        "src.core.valkey.connecter.Valkey",
        side_effect=[failed_client, recovered_client],
    ):
        connection = ValkeyConnecter(PipelineSettings())

        with pytest.raises(ConnectionError, match="unavailable"):
            connection.connect()
        assert connection.is_connected is False
        failed_client.close.assert_called_once_with()

        assert connection.connect() is recovered_client
        recovered_client.ping.assert_called_once_with()


def test_connect_can_create_lazy_client_without_ping() -> None:
    client = Mock()
    with patch("src.core.valkey.connecter.Valkey", return_value=client):
        connection = ValkeyConnecter(PipelineSettings())

        assert connection.connect(verify=False) is client

    client.ping.assert_not_called()


def test_verified_connect_checks_an_existing_lazy_client() -> None:
    client = Mock()
    with patch("src.core.valkey.connecter.Valkey", return_value=client):
        connection = ValkeyConnecter(PipelineSettings())
        connection.connect(verify=False)

        assert connection.connect() is client

    client.ping.assert_called_once_with()
