#!/usr/bin/env python3
"""Perplexity display name follows the same account cache as the email."""
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
from adapters import perplexity_engine  # noqa: E402


class TestPerplexityAccountName(unittest.TestCase):
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
        cookie = "pplx.visitor-id=visitor-1"
        session = {
            "direction": "response",
            "meta": {
                "host": "www.perplexity.ai",
                "method": "GET",
                "path": "/api/auth/session?version=2.18&source=default",
                "cookie": cookie,
            },
        }
        body = json.dumps(
            {
                "user": {
                    "id": "6569d805-20f2-4fa6-b906-b2ab4e831304",
                    "email": "juyonglee@wins21.co.kr",
                    "name": "이주용",
                    "username": "other-handle",
                    "image": "https://example.com/avatar.png",
                }
            }
        ).encode()
        self.assertEqual(perplexity_engine.extract_full_body(session, body), "")
        prompt = {
            "direction": "request",
            "content_type": "application/json",
            "meta": {
                "host": "www.perplexity.ai",
                "path": "/rest/sse/perplexity_ask",
                "cookie": cookie,
            },
        }
        built = perplexity_engine.build_policy_api_payload(
            prompt, json.dumps({"query_str": "안녕"}).encode(), "안녕"
        )
        self.assertEqual(built["email"], "juyonglee@wins21.co.kr")
        self.assertEqual(built["user_id"], "6569d805-20f2-4fa6-b906-b2ab4e831304")
        self.assertEqual(built["user_name"], "이주용")
        self.assertEqual(built["prompt"], "안녕")

    def test_username_is_used_when_name_is_empty(self) -> None:
        name = perplexity_engine._pplx_display_name(
            {"name": "", "username": "juyong", "image": "https://example.com/a.png"}
        )
        self.assertEqual(name, "juyong")


if __name__ == "__main__":
    unittest.main()
