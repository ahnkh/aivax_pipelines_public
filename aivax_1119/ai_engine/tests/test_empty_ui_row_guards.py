#!/usr/bin/env python3
"""Guards: empty Claude/Grok turns must not become OpenSearch/UI rows."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "core") not in sys.path:
    sys.path.insert(0, str(_ROOT / "core"))

from adapters import claude_engine, grok_engine  # noqa: E402
from adapters import grok_upload  # noqa: E402
import ai_policy_engine  # noqa: E402


FILE_UUID = "24c23fd9-a2ca-420c-b502-fca5f693e726"


def _ws_req(rid: str = "rid-ws-0001") -> dict:
    return {
        "direction": "request",
        "unit_type": "ws_frame",
        "request_id": rid,
        "meta": {"host": "grok.com", "path": "/ws/mgw/?uid=user-1", "x_userid": "user-1"},
    }


def _frame(obj: dict) -> bytes:
    return grok_engine._encode_ws_text_frame(
        json.dumps(obj, ensure_ascii=False).encode("utf-8"),
        masked=True,
    )


class TestClaudeCompletionStatusNotPolicy(unittest.TestCase):
    def test_completion_status_is_not_completion_endpoint(self) -> None:
        req = {
            "direction": "request",
            "meta": {
                "path": (
                    "/api/organizations/edccb839-745a-40ed-8088-81d60c79435c/"
                    "chat_conversations/f3f4fc21-f815-49b0-ade1-cc0dae968b03/"
                    "completion_status?poll=false"
                )
            },
        }
        self.assertFalse(claude_engine._is_completion_endpoint(req))
        self.assertTrue(claude_engine.claude_should_skip_policy(req, b""))

    def test_real_completion_still_runs_policy(self) -> None:
        req = {
            "direction": "request",
            "meta": {
                "path": (
                    "/api/organizations/edccb839-745a-40ed-8088-81d60c79435c/"
                    "chat_conversations/f3f4fc21-f815-49b0-ade1-cc0dae968b03/completion"
                )
            },
        }
        self.assertTrue(claude_engine._is_completion_endpoint(req))
        self.assertFalse(claude_engine.claude_should_skip_policy(req, b""))

    def test_conversation_path_excludes_completion_status(self) -> None:
        status = (
            "/api/organizations/x/chat_conversations/y/completion_status?poll=false"
        )
        completion = "/api/organizations/x/chat_conversations/y/completion"
        self.assertFalse(ai_policy_engine._is_conversation_path("claude", status))
        self.assertTrue(ai_policy_engine._is_conversation_path("claude", completion))


class TestGrokPeekRecentDoesNotCreateEmptyRow(unittest.TestCase):
    def setUp(self) -> None:
        grok_upload._RECENT_CHAT_FILES_BY_SCOPE.clear()

    def test_stale_recent_file_without_in_frame_refs_skips(self) -> None:
        req = _ws_req("parent-ws-0000aabb")
        grok_upload.remember_recent_chat_file(req, FILE_UUID)
        # Empty response.create with no prompt / file_mention — only castle token.
        obj = {
            "session_id": "sess-1",
            "event": {"type": "response.create", "castle_request_token": "x" * 80},
        }
        text = grok_engine.extract(req, _frame(obj))
        self.assertEqual("", text)
        self.assertTrue(req.get("_grok_ws_skip"))
        self.assertTrue(grok_engine.grok_should_skip_policy(req, _frame(obj)))
        policy = grok_engine.build_policy_api_payload(req, _frame(obj), text)
        self.assertIsNone(policy)

    def test_true_file_only_in_frame_still_builds_policy(self) -> None:
        req = _ws_req("parent-ws-fileonly")
        obj = {
            "session_id": "sess-1",
            "event": {
                "type": "response.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "x_grok": {
                        "input_chunks": [
                            {
                                "mention": {
                                    "file_mention": {"file_id": FILE_UUID},
                                }
                            },
                        ],
                    },
                },
            },
        }
        text = grok_engine.extract(req, _frame(obj))
        self.assertEqual("", text)
        self.assertFalse(req.get("_grok_ws_skip"))
        with mock.patch.object(grok_engine, "build_policy_attachments") as build_att:
            build_att.return_value = [
                {
                    "id": f"/run/aivax/attachments/{FILE_UUID}",
                    "size": 10,
                    "name": "img.jpg",
                    "mime_type": "image/jpeg",
                }
            ]
            policy = grok_engine.build_policy_api_payload(req, _frame(obj), text)
        self.assertIsNotNone(policy)
        self.assertEqual(
            (policy or {}).get("prompt"),
            "사용자가 파일을 업로드하였습니다.",
        )


if __name__ == "__main__":
    unittest.main()
