from src.core.sqlite.connecter import SqliteConnecter


def test_connect_uses_injected_path_and_disconnect_closes_the_client(tmp_path) -> None:
    database_path = tmp_path / "state" / "users.sqlite"
    connecter = SqliteConnecter(database_path)

    client = connecter.connect()

    assert connecter.is_connected is True
    assert database_path.exists()
    assert client.execute("PRAGMA foreign_keys").fetchone() == (1,)

    connecter.disconnect()
    assert connecter.is_connected is False
