#!/usr/bin/env python3
"""Grok display name follows the same account cache as the email."""
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
from adapters import grok_engine  # noqa: E402


class TestGrokAccountName(unittest.TestCase):
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

    def test_session_name_is_kept_for_the_later_prompt(self) -> None:
        cookie = "x-userid=device-1"
        session = {
            "direction": "response",
            "meta": {
                "host": "grok.com",
                "path": "/api/auth/session",
                "cookie": cookie,
                "client_ip": "10.0.50.60",
            },
        }
        body = json.dumps(
            {
                "status": "authenticated",
                "session": {
                    "userId": "26bbaeff-271e-4c1e-b189-80b8e7e39f79",
                    "email": "juyonglee@wins21.co.kr",
                    "givenName": "이주용",
                    "familyName": "",
                    "profileImage": "https://example.com/avatar.png",
                },
            }
        ).encode()
        self.assertEqual(grok_engine.extract_full_body(session, body), "")
        prompt = {
            "direction": "request",
            "content_type": "application/json",
            "meta": {
                "host": "grok.com",
                "path": "/rest/app-chat/conversations/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/responses",
                "cookie": cookie,
                "client_ip": "10.0.50.60",
            },
        }
        built = grok_engine.build_policy_api_payload(
            prompt, json.dumps({"message": "안녕"}).encode(), "안녕"
        )
        self.assertEqual(built["email"], "juyonglee@wins21.co.kr")
        self.assertEqual(built["user_id"], "26bbaeff-271e-4c1e-b189-80b8e7e39f79")
        self.assertEqual(built["user_name"], "이주용")
        self.assertEqual(built["prompt"], "안녕")

    def test_page_name_stays_next_to_the_account_email(self) -> None:
        html = (
            '{"userId":"26bbaeff-271e-4c1e-b189-80b8e7e39f79",'
            '"email":"juyonglee@wins21.co.kr","givenName":"이주용","familyName":"Lee"}'
            + ("x" * 5000)
            + '{"givenName":"Other","email":"other@example.com"}'
        )
        user_id, email, name = grok_engine._extract_user_profile_from_html(html)
        self.assertEqual(user_id, "26bbaeff-271e-4c1e-b189-80b8e7e39f79")
        self.assertEqual(email, "juyonglee@wins21.co.kr")
        self.assertEqual(name, "이주용 Lee")


if __name__ == "__main__":
    unittest.main()
