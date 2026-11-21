#!/usr/bin/env python3
"""Gemini and Claude display names follow the same account cache as the email."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _path in (_ROOT, _ROOT / "core", _ROOT / "adapters"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import account_lookup  # noqa: E402
from adapters import claude_engine  # noqa: E402
from adapters import gemini_engine  # noqa: E402


class TestAccountDisplayName(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old_path = os.environ.get("AIVAX_ACCOUNT_CACHE_PATH")
        os.environ["AIVAX_ACCOUNT_CACHE_PATH"] = str(Path(self._tmp.name) / "accounts.sqlite")
        account_lookup.configure()

    def tearDown(self) -> None:
        if self._old_path is None:
            os.environ.pop("AIVAX_ACCOUNT_CACHE_PATH", None)
        else:
            os.environ["AIVAX_ACCOUNT_CACHE_PATH"] = self._old_path
        account_lookup.configure()
        self._tmp.cleanup()

    def test_gemini_page_label_is_kept_for_the_later_prompt(self) -> None:
        html = (
            "<html><script>window.WIZ_global_data = {"
            '"oPEP7c":"user@example.com","S06Grb":"123456789012345678901"'
            "}</script>"
            '<a aria-label="Google 계정: 이주용 &#10;(user@example.com)"></a>'
            + ("x" * 60)
            + "</html>"
        ).encode()
        page = {
            "direction": "response",
            "meta": {
                "host": "gemini.google.com",
                "path": "/",
                "cookie": "__Secure-1PSID=psid-device-1",
                "client_ip": "10.0.50.60",
            },
        }
        self.assertEqual(gemini_engine.extract_full_body(page, html), "")
        prompt = {
            "direction": "request",
            "meta": {
                "host": "gemini.google.com",
                "path": "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate",
                "cookie": "__Secure-1PSID=psid-device-1",
            },
        }
        built = gemini_engine.build_policy_api_payload(prompt, b"", "안녕")
        self.assertEqual(built["email"], "user@example.com")
        self.assertEqual(built["user_id"], "123456789012345678901")
        self.assertEqual(built["user_name"], "이주용")

    def test_gemini_label_for_another_account_is_ignored(self) -> None:
        html = (
            "<html><script>"
            '"oPEP7c":"user@example.com","S06Grb":"123456789012345678901"'
            "</script>"
            '<a aria-label="Google Account: Other Person (other@example.com)"></a>'
            + ("x" * 60)
            + "</html>"
        )
        info = gemini_engine._extract_account_from_html(html)
        self.assertEqual(info["email"], "user@example.com")
        self.assertNotIn("name", info)

    def test_claude_account_name_is_kept_and_org_title_is_not(self) -> None:
        org_id = "11111111-1111-1111-1111-111111111111"
        cookie = "__ssid=session-device-1"
        profile = {
            "direction": "response",
            "meta": {
                "host": "claude.ai",
                "path": f"/edge-api/bootstrap/{org_id}/app_start",
                "cookie": cookie,
                "client_ip": "10.0.50.60",
            },
        }
        body = json.dumps(
            {
                "account": {
                    "uuid": "22222222-2222-2222-2222-222222222222",
                    "email_address": "alice@corp.com",
                    "full_name": "uzins",
                }
            }
        ).encode()
        self.assertEqual(claude_engine.extract(profile, body), "")
        org = {
            "direction": "response",
            "meta": {
                "host": "claude.ai",
                "method": "GET",
                "path": f"/api/organizations/{org_id}",
                "cookie": cookie,
                "client_ip": "10.0.50.60",
            },
        }
        claude_engine.extract(
            org,
            json.dumps(
                {
                    "uuid": org_id,
                    "name": "alice@corp.com's Organization",
                    "email_address": "alice@corp.com",
                }
            ).encode(),
        )
        prompt = {
            "direction": "request",
            "content_type": "application/json",
            "meta": {
                "host": "claude.ai",
                "path": (
                    f"/api/organizations/{org_id}/chat_conversations/"
                    "33333333-3333-3333-3333-333333333333/completion"
                ),
                "cookie": cookie,
                "client_ip": "10.0.50.60",
            },
        }
        built = claude_engine.build_policy_api_payload(
            prompt, json.dumps({"prompt": "안녕"}).encode(), "안녕"
        )
        self.assertEqual(built["email"], "alice@corp.com")
        self.assertEqual(built["user_name"], "uzins")


if __name__ == "__main__":
    unittest.main()
