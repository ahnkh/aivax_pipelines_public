#!/usr/bin/env python3
"""Client IP / X-Forwarded-For resolution for prompt logs.

Run from the ai_engine tree:

  cd /home1/aivax/ai_engine
  PYTHONPATH=.:core python3 tests/test_client_ip.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
for _path in (_ROOT, _ROOT / "core"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from core import ai_policy_engine  # noqa: E402
from core.ai_policy_engine import PolicyEngine  # noqa: E402


def first_forwarded_hop(raw: str) -> str:
    """Mirror of C++ AivaxCore::firstForwardedHop (envoy aivax_filter.cc)."""
    hop = raw.split(",", 1)[0].strip()
    if not hop:
        return ""
    if hop.startswith("["):
        end = hop.find("]")
        if end > 1:
            return hop[1:end]
        return hop
    if hop.count(":") == 1:
        host, _, port = hop.partition(":")
        if port.isdigit():
            return host
    return hop


class TestFirstForwardedHop(unittest.TestCase):
    def test_parses_first_hop_and_strips_port(self) -> None:
        self.assertEqual(first_forwarded_hop(""), "")
        self.assertEqual(first_forwarded_hop("   "), "")
        self.assertEqual(first_forwarded_hop("203.0.113.10"), "203.0.113.10")
        self.assertEqual(first_forwarded_hop("  203.0.113.10  , 10.0.0.1"), "203.0.113.10")
        self.assertEqual(first_forwarded_hop("203.0.113.10:51234, 10.0.0.1"), "203.0.113.10")
        self.assertEqual(first_forwarded_hop("[2001:db8::1]:443, 10.0.0.1"), "2001:db8::1")
        self.assertEqual(first_forwarded_hop("2001:db8::1"), "2001:db8::1")


class TestNormalizeClientIp(unittest.TestCase):
    def test_bare_ipv4(self) -> None:
        self.assertEqual(PolicyEngine._normalize_client_ip("203.0.113.10"), "203.0.113.10")

    def test_ipv4_with_port(self) -> None:
        self.assertEqual(PolicyEngine._normalize_client_ip("203.0.113.10:51234"), "203.0.113.10")

    def test_ipv6_bracket_port(self) -> None:
        self.assertEqual(PolicyEngine._normalize_client_ip("[2001:db8::1]:443"), "2001:db8::1")

    def test_ipv6_bare(self) -> None:
        self.assertEqual(PolicyEngine._normalize_client_ip("2001:db8::1"), "2001:db8::1")

    def test_hostname_rejected(self) -> None:
        self.assertEqual(PolicyEngine._normalize_client_ip("chatgpt.com"), "")

    def test_empty(self) -> None:
        self.assertEqual(PolicyEngine._normalize_client_ip(""), "")
        self.assertEqual(PolicyEngine._normalize_client_ip("   "), "")


class TestResolveClientHost(unittest.TestCase):
    def test_xff_meta_wins_over_client_ip(self) -> None:
        req = {
            "meta": {
                "host": "chatgpt.com",
                "client_ip": "10.0.3.115",
                "x_forwarded_for": "203.0.113.10, 10.0.0.1",
            }
        }
        self.assertEqual(PolicyEngine._resolve_client_host(req), "203.0.113.10")

    def test_xff_request_headers(self) -> None:
        req = {
            "meta": {
                "host": "chatgpt.com",
                "client_ip": "10.0.3.115",
                "request_headers": {"x-forwarded-for": "198.51.100.7"},
            }
        }
        self.assertEqual(PolicyEngine._resolve_client_host(req), "198.51.100.7")

    def test_client_ip_when_no_xff(self) -> None:
        req = {"meta": {"host": "chatgpt.com", "client_ip": "10.0.3.115"}}
        self.assertEqual(PolicyEngine._resolve_client_host(req), "10.0.3.115")

    def test_client_ip_port_stripped(self) -> None:
        req = {"meta": {"client_ip": "10.0.3.115:58651"}}
        self.assertEqual(PolicyEngine._resolve_client_host(req), "10.0.3.115")

    def test_true_client_ip_fallback(self) -> None:
        req = {"meta": {"host": "chatgpt.com", "true_client_ip": "192.0.2.9"}}
        self.assertEqual(PolicyEngine._resolve_client_host(req), "192.0.2.9")

    def test_host_not_used_as_ip(self) -> None:
        req = {"meta": {"host": "chatgpt.com"}}
        self.assertEqual(PolicyEngine._resolve_client_host(req), "")

    def test_invalid_xff_falls_through_to_client_ip(self) -> None:
        req = {
            "meta": {
                "x_forwarded_for": "not-an-ip",
                "client_ip": "10.0.3.115",
            }
        }
        self.assertEqual(PolicyEngine._resolve_client_host(req), "10.0.3.115")

    def test_policy_api_request_uses_xff(self) -> None:
        engine = PolicyEngine.__new__(PolicyEngine)
        req = {
            "direction": "request",
            "meta": {
                "host": "chatgpt.com",
                "path": "/backend-api/conversation",
                "client_ip": "10.0.3.115",
                "x_forwarded_for": "203.0.113.10",
            },
        }
        obj = engine._build_policy_api_request_obj(req, b"{}", "hello", "gpt", None)
        self.assertEqual(obj["client_host"], "203.0.113.10")
        self.assertEqual(obj["prompt"], "hello")


class TestResolveEveClientIp(unittest.TestCase):
    def test_wire_ip_wins_on_mirror(self) -> None:
        eve = {"client_ip": "10.0.3.115"}
        headers = [{"name": "x-forwarded-for", "value": "203.0.113.10"}]
        self.assertEqual(ai_policy_engine._resolve_eve_client_ip(eve, headers), "10.0.3.115")

    def test_xff_when_wire_ip_missing(self) -> None:
        eve = {}
        headers = [{"name": "x-forwarded-for", "value": "203.0.113.10, 10.0.0.1"}]
        self.assertEqual(ai_policy_engine._resolve_eve_client_ip(eve, headers), "203.0.113.10")


if __name__ == "__main__":
    unittest.main()
