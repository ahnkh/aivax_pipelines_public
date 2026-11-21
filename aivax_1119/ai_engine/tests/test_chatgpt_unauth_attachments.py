#!/usr/bin/env python3
"""Anonymous ChatGPT turns must carry their uploaded images to policy.

The unauth-mweb UI posts a form body instead of the authenticated JSON turn, so
uploads are referenced by file id inside JSON-encoded form fields. Losing that
reference leaves the image saved on tmpfs but never inspected.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

_ROOT = Path(__file__).resolve().parents[1]
for _path in (_ROOT, _ROOT / "core", _ROOT / "adapters"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from adapters import chatgpt_engine  # noqa: E402
from adapters import chatgpt_upload  # noqa: E402

fum = chatgpt_upload._fum

UPLOAD_ID = "file-QRs4EaiAhUQJR6PJRDtyEL"
JPEG = b"\xff\xd8\xff\xe0" + b"anon-image-bytes" * 8


class TestUnauthMwebAttachments(unittest.TestCase):
    def setUp(self) -> None:
        self._old_dir = fum._UPLOAD_SAVE_DIR
        self._tmp = tempfile.TemporaryDirectory()
        fum._UPLOAD_SAVE_DIR = self._tmp.name
        self._clear_memory()

    def tearDown(self) -> None:
        self._clear_memory()
        fum._UPLOAD_SAVE_DIR = self._old_dir
        self._tmp.cleanup()

    @staticmethod
    def _clear_memory() -> None:
        fum._upload_id_map.clear()
        fum._saved_file_paths.clear()
        fum._saved_file_sizes.clear()
        fum._upload_expiry_heap.clear()

    @staticmethod
    def _form_payload(**overrides: str) -> bytes:
        form = {
            "lightweight_authenticated": "0",
            "conversationState": json.dumps(
                {"messages": [], "parentMessageId": "client-created-root"}
            ),
            "prompt": "이 이미지 설명해줘",
            "imageAttachments": "[]",
            "pendingImageUploads": "[]",
            "oai-session-id": "fa7fbd78-731a-41b3-bae4-9a67e53dbf2d",
            "userMessageId": "bbdbad93-4144-4414-8bdf-c193be8e351a",
            "chatRequirementsToken": "gAAAAAB-token-must-never-be-read-as-prompt",
        }
        form.update(overrides)
        return urlencode(form).encode("utf-8")

    @staticmethod
    def _req() -> dict:
        return {
            "direction": "request",
            "unit_type": "form_body",
            "content_type": "application/x-www-form-urlencoded",
            "request_id": "anon-rid-1",
            "meta": {
                "host": "chatgpt.com",
                "path": "/unauth-mweb/conversation/updates?lightweight_authenticated=0",
            },
        }

    def _upload_image(self) -> None:
        self.assertTrue(fum.register_upload_mapping(UPLOAD_ID, "jennie.jpg", {}))
        self.assertTrue(fum.append_upload_payload(UPLOAD_ID, JPEG, {}))

    def test_image_attachment_field_reaches_policy_payload(self) -> None:
        self._upload_image()
        payload = self._form_payload(
            imageAttachments=json.dumps(
                [
                    {
                        "id": UPLOAD_ID,
                        "name": "jennie.jpg",
                        "mimeType": "image/jpeg",
                        "size": len(JPEG),
                    }
                ]
            )
        )

        built = chatgpt_engine.build_policy_api_payload(
            self._req(), payload, "이 이미지 설명해줘"
        )

        self.assertIsNotNone(built)
        assert built is not None
        self.assertEqual("이 이미지 설명해줘", built["prompt"])
        self.assertIn("file_block_filter", built["filter_list"])
        self.assertEqual(1, len(built["attachments"]))
        att = built["attachments"][0]
        self.assertTrue(att["id"].startswith(self._tmp.name))
        self.assertEqual(JPEG, Path(att["id"]).read_bytes())
        self.assertEqual("jennie.jpg", att["name"])

    def test_pending_upload_shape_and_bare_ids_are_accepted(self) -> None:
        """Field names and nesting have already changed once upstream."""
        self._upload_image()
        payload = self._form_payload(
            imageAttachments="[]",
            pendingImageUploads=json.dumps([{"upload": {"fileId": UPLOAD_ID}}]),
        )

        built = chatgpt_engine.build_policy_api_payload(
            self._req(), payload, "이 이미지 설명해줘"
        )

        self.assertIsNotNone(built)
        assert built is not None
        self.assertEqual(1, len(built["attachments"]))
        self.assertEqual(JPEG, Path(built["attachments"][0]["id"]).read_bytes())

        bare = chatgpt_engine.build_policy_api_payload(
            self._req(),
            self._form_payload(imageAttachments=json.dumps([UPLOAD_ID])),
            "이 이미지 설명해줘",
        )
        assert bare is not None
        self.assertEqual(JPEG, Path(bare["attachments"][0]["id"]).read_bytes())

    def test_image_only_turn_is_still_evaluated(self) -> None:
        self._upload_image()
        payload = self._form_payload(
            prompt="",
            imageAttachments=json.dumps([{"id": UPLOAD_ID, "name": "jennie.jpg"}]),
        )

        built = chatgpt_engine.build_policy_api_payload(self._req(), payload, "")

        self.assertIsNotNone(built)
        assert built is not None
        self.assertEqual("", built["prompt"])
        self.assertEqual(1, len(built["attachments"]))

    def test_text_only_conversation_does_not_invent_an_attachment(self) -> None:
        payload = json.dumps(
            {
                "action": "next",
                "messages": [
                    {
                        "id": "msg-1",
                        "author": {"role": "user"},
                        "content": {
                            "content_type": "text",
                            "parts": ["gdb 잡아보자"],
                        },
                        "metadata": {"attachments": [{}]},
                    }
                ],
                "parent_message_id": "parent-1",
            }
        ).encode()
        req = {
            "direction": "request",
            "unit_type": "json_body",
            "content_type": "application/json",
            "meta": {"path": "/backend-api/f/conversation"},
            "request_id": "req-blank-attachment",
        }

        built = chatgpt_engine.build_policy_api_payload(req, payload, "gdb 잡아보자")

        self.assertIsNotNone(built)
        assert built is not None
        self.assertEqual("gdb 잡아보자", built["prompt"])
        self.assertEqual([], built["attachments"])
        self.assertEqual([], chatgpt_upload.build_policy_attachments({}))
        self.assertEqual(
            [],
            chatgpt_upload.build_policy_attachments({"attachments": [{}, {"id": "", "name": ""}]}),
        )

    def test_named_upload_is_still_an_attachment(self) -> None:
        rows = chatgpt_upload.build_policy_attachments(
            {"attachments": [{"id": UPLOAD_ID, "name": "note.pdf", "size": 12}]}
        )

        self.assertEqual(1, len(rows))
        self.assertEqual("note.pdf", rows[0]["name"])
        self.assertTrue(str(rows[0]["id"]).strip())

    def test_text_only_turn_keeps_empty_attachments_and_no_token_leak(self) -> None:
        built = chatgpt_engine.build_policy_api_payload(
            self._req(), self._form_payload(), "이 이미지 설명해줘"
        )

        self.assertIsNotNone(built)
        assert built is not None
        self.assertEqual([], built["attachments"])
        self.assertNotIn("token", json.dumps(built, ensure_ascii=False))

    def test_turn_without_prompt_or_upload_is_not_invented(self) -> None:
        built = chatgpt_engine.build_policy_api_payload(
            self._req(), self._form_payload(prompt=""), ""
        )
        self.assertIsNone(built)


if __name__ == "__main__":
    unittest.main()
