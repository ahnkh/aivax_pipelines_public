"""IP 주소 문자열을 안전하게 정규화·변환하는 공통 기능."""

from __future__ import annotations

import ipaddress


class IpAddressConverter:
    """유효하지 않은 IP 주소는 예외 대신 ``None``으로 나타낸다."""

    @staticmethod
    def normalize(value: str) -> str | None:
        """IPv4/IPv6 문자열을 표준 표현으로 바꾸거나 ``None``을 반환한다."""
        normalized = value.strip()
        if not normalized:
            return None
        try:
            return str(ipaddress.ip_address(normalized))
        except ValueError:
            return None

    @classmethod
    def to_integer(cls, value: str) -> int | None:
        """유효한 IP 주소의 정수 표현을 반환한다."""
        normalized = cls.normalize(value)
        return int(ipaddress.ip_address(normalized)) if normalized is not None else None
