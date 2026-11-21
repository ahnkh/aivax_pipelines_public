#!/usr/bin/env python3
"""Logged-in ChatGPT display name follows the same account cache as the email."""
from __future__ import annotations

import base64
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
from adapters import chatgpt_engine as engine  # noqa: E402


def _jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).decode("ascii").rstrip("=")
    return f"hdr.{payload}.sig"


def _chat_body() -> bytes:
    return json.dumps(
        {
            "action": "next",
            "conversation_id": "conv-1",
            "messages": [
                {
                    "id": "msg-1",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["안녕"]},
                }
            ],
        }
    ).encode("utf-8")


class TestChatgptAccountName(unittest.TestCase):
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
        session = {
            "user": {
                "id": "user-OAXUvzqodQq5QVEc5cWiqCby",
                "email": "luvjw0405@gmail.com",
                "name": "김상표",
            },
            "accessToken": "secret-token",
            "expires": "2099-01-01T00:00:00.000Z",
        }
        req = {
            "direction": "response",
            "unit_type": "json_body",
            "content_type": "application/json",
            "meta": {
                "path": "/api/auth/session",
                "host": "chatgpt.com",
                "cookie": "oai-did=device-1",
                "client_ip": "10.0.50.60",
            },
        }
        raw = json.dumps(session).encode("utf-8")
        text = engine.extract_full_body(req, raw)
        self.assertEqual(text, "")
        self.assertEqual(engine.extract(req, raw), "")
        self.assertNotIn("secret-token", text or "")

        prompt_req = {
            "direction": "request",
            "unit_type": "json_body",
            "content_type": "application/json",
            "meta": {
                "path": "/backend-api/f/conversation",
                "host": "chatgpt.com",
                "cookie": "oai-did=device-1",
                "client_ip": "10.0.50.60",
                "authorization": "Bearer " + _jwt(
                    {
                        "https://api.openai.com/auth": {
                            "chatgpt_account_user_id": "user-OAXUvzqodQq5QVEc5cWiqCby"
                        },
                        "https://api.openai.com/profile": {"email": "luvjw0405@gmail.com"},
                    }
                ),
            },
        }
        payload = engine.build_policy_api_payload(prompt_req, _chat_body(), "안녕")
        self.assertIsNotNone(payload)
        self.assertEqual(payload["email"], "luvjw0405@gmail.com")
        self.assertEqual(payload["user_id"], "user-OAXUvzqodQq5QVEc5cWiqCby")
        self.assertEqual(payload["user_name"], "김상표")
        self.assertEqual(payload["prompt"], "안녕")

    def test_me_response_name_and_email_are_not_a_prompt(self) -> None:
        body = json.dumps(
            {"id": "user-1", "email": "a@example.com", "name": "Sangpyo Kim", "picture": "https://x"}
        ).encode("utf-8")
        req = {
            "direction": "response",
            "unit_type": "json_body",
            "content_type": "application/json",
            "meta": {"path": "/backend-api/me", "client_ip": "10.0.50.61"},
        }
        self.assertEqual(engine.extract_full_body(req, body), "")
        stored = account_lookup.get_result("chatgpt", "user-1")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.display_name, "Sangpyo Kim")
        self.assertEqual(stored.email, "a@example.com")

    def test_profile_name_in_the_token_is_recorded(self) -> None:
        req = {
            "direction": "request",
            "unit_type": "json_body",
            "content_type": "application/json",
            "meta": {
                "path": "/backend-api/f/conversation",
                "authorization": "Bearer " + _jwt(
                    {
                        "https://api.openai.com/auth": {"chatgpt_user_id": "user-2"},
                        "https://api.openai.com/profile": {
                            "email": "b@example.com",
                            "name": "Kim Sangpyo",
                        },
                    }
                ),
                "cookie": "oai-did=device-2",
                "client_ip": "10.0.50.62",
            },
        }
        payload = engine.build_policy_api_payload(req, _chat_body(), "안녕")
        self.assertEqual(payload["user_name"], "Kim Sangpyo")
        self.assertEqual(payload["email"], "b@example.com")


if __name__ == "__main__":
    unittest.main()
