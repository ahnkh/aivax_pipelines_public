from src.core.network import IpAddressConverter


def test_normalize_returns_canonical_ipv4_and_ipv6_values() -> None:
    assert IpAddressConverter.normalize(" 127.0.0.1 ") == "127.0.0.1"
    assert IpAddressConverter.normalize("2001:0db8::1") == "2001:db8::1"


def test_invalid_or_blank_address_returns_none() -> None:
    assert IpAddressConverter.normalize("") is None
    assert IpAddressConverter.normalize("not-an-ip") is None


def test_to_integer_returns_none_for_invalid_address() -> None:
    assert IpAddressConverter.to_integer("127.0.0.1") == 2_130_706_433
    assert IpAddressConverter.to_integer("not-an-ip") is None
