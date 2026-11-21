from __future__ import annotations

from pathlib import Path
import sqlite3
import threading


class SqliteConnecter:
    """주입받은 SQLite 경로로 연결을 만들고 종료한다."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)
        self._client: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def connect(self) -> sqlite3.Connection:
        """SQLite 연결을 생성하고 공통 PRAGMA를 적용한 뒤 반환한다."""
        with self._lock:
            if self._client is None:
                path = self._database_path
                if path.parent != Path("."):
                    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)

                client = sqlite3.connect(
                    path,
                    timeout=30.0,
                    check_same_thread=False,
                )
                try:
                    client.execute("PRAGMA busy_timeout=30000")
                    client.execute("PRAGMA journal_mode=WAL")
                    client.execute("PRAGMA foreign_keys=ON")
                except Exception:
                    client.close()
                    raise
                self._client = client
            return self._client

    def disconnect(self) -> None:
        """현재 보유한 SQLite 연결을 종료한다. 여러 번 호출해도 안전하다."""
        with self._lock:
            if self._client is None:
                return
            client, self._client = self._client, None
            client.close()

    @property
    def is_connected(self) -> bool:
        """현재 이 객체가 SQLite 연결을 보유하는지 반환한다."""
        with self._lock:
            return self._client is not None
