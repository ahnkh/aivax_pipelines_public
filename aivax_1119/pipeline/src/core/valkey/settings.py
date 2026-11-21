"""연결에 필요한 설정 계약. 환경변수 로딩과 기본값 결정은 조립부의 책임이다."""

from typing import Protocol


class ValkeySettings(Protocol):
    """Valkey 연결에 필요한 읽기 전용 설정값."""

    @property
    def valkey_host(self) -> str: ...

    @property
    def valkey_port(self) -> int: ...

    @property
    def valkey_db(self) -> int: ...

    @property
    def valkey_password(self) -> str: ...

    @property
    def valkey_socket_connect_timeout_ms(self) -> int: ...

    @property
    def valkey_socket_timeout_ms(self) -> int: ...
