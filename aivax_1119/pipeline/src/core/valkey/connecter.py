from __future__ import annotations
import threading
from valkey import Valkey
from src.core.valkey.settings import ValkeySettings


class ValkeyConnecter:
    """
    valkey의 연결 맺고 끊음을 관리하는 객체.
    연결을 맺고 valkey 클라이언트를 반환해 다른 계층/모듈에서 valkey를 사용할 수 있도록한다.
    """

    def __init__(self, settings: ValkeySettings) -> None:
        self._settings = settings
        self._client: Valkey | None = None
        self._verified = False
        self._lock = threading.RLock()

    def connect(self, *, verify: bool = True) -> Valkey:
        """공용 Valkey 클라이언트를 생성·검증한 뒤 반환한다.

        ``Valkey``는 실제 연결을 지연할 수 있다. 여기서 ``PING``을 수행해
        호출자가 사용 가능한 client를 받거나 연결 오류를 즉시 받게 한다.
        ``verify=False``이면 client만 만들어 두고 실제 명령에서 연결한다.
        """
        with self._lock:
            if self._client is None:
                self._client = self._create_client()
                self._verified = False
            if verify and not self._verified:
                try:
                    self._client.ping()
                except Exception:
                    client, self._client = self._client, None
                    client.close()
                    raise
                self._verified = True
            return self._client

    def disconnect(self) -> None:
        """클라이언트 connection pool을 종료한다. 여러 번 호출해도 안전하다."""
        with self._lock:
            if self._client is None:
                return
            client, self._client = self._client, None
            self._verified = False
            client.close()

    @property
    def is_connected(self) -> bool:
        """현재 이 객체가 연결된 client 인스턴스를 보유하는지 반환한다."""
        with self._lock:
            return self._client is not None

    def _create_client(self) -> Valkey:
        settings = self._settings
        options = {
            "socket_connect_timeout": settings.valkey_socket_connect_timeout_ms / 1_000,
            "socket_timeout": settings.valkey_socket_timeout_ms / 1_000,
            "retry_on_timeout": False,
        }

        return Valkey(
            host=settings.valkey_host,
            port=settings.valkey_port,
            db=settings.valkey_db,
            password=settings.valkey_password or None,
            **options,
        )
