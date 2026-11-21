#!/usr/bin/env python3
"""동의 replay re-attaches the blocked turn's files (same chat + client only)."""

from __future__ import annotations

import base64
import json
import sys
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.parse import parse_qs, urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT))

import ai_policy_engine as ape  # noqa: E402
from adapters import chatgpt_engine, claude_engine, claude_upload, gemini_engine  # noqa: E402
from adapters import (  # noqa: E402
    github_copilot_engine,
    grok_engine,
    notion_engine,
    perplexity_engine,
    timely_engine,
    wrks_engine,
)


_CLAUDE_COMPLETION = (
    "/api/organizations/0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    "/chat_conversations/26ddc0af-7643-443f-b2cc-3d37ac2dc9bb/completion"
)


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _ld(field: int, blob: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(blob)) + blob


def _fields(buf: bytes):
    return [f for f, _, _ in claude_engine._pb_iter(buf)]


class ClaudeConsentAttachments(unittest.TestCase):
    def test_new_direct_upload_path_is_supported(self) -> None:
        self.assertTrue(
            claude_upload._is_claude_upload_path(
                "/api/edccb839-745a-40ed-8088-81d60c79435c/upload"
            )
        )
        self.assertTrue(
            claude_upload._is_claude_upload_path(
                "/api/organizations/0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0/"
                "conversations/26ddc0af-7643-443f-b2cc-3d37ac2dc9bb/wiggle/upload-file"
            )
        )

    def test_rpc_policy_payload_resolves_file_extras(self) -> None:
        req = {
            "direction": "request",
            "content_type": "application/proto",
            "meta": {
                "host": "claude.ai",
                "path": "/claudeai-rpc/anthropic.bard.api.v1alpha.ConversationService/PerformAction",
                "client_ip": "10.0.50.60",
            },
        }
        conversation = "26ddc0af-7643-443f-b2cc-3d37ac2dc9bb"
        file_uuid = "edccb839-745a-40ed-8088-81d60c79435c"
        context = _ld(2, conversation.encode())
        action = (
            _ld(1, b"m1")
            + _ld(2, b"m2")
            + _ld(3, "첨부 파일 요약".encode())
            + _ld(4, _ld(7, file_uuid.encode()))
        )
        payload = _ld(1, context) + _ld(2, action)
        expected = [{"id": "/tmp/file", "size": 10, "name": "a.txt", "mime_type": ""}]
        with patch.object(
            claude_engine,
            "build_rpc_policy_attachments",
            return_value=expected,
        ) as build:
            policy = claude_engine.build_policy_api_payload(req, payload, "")
        self.assertEqual(policy["prompt"], "첨부 파일 요약")
        self.assertEqual(policy["attachments"], expected)
        self.assertEqual(build.call_args.args[1], [file_uuid])
        self.assertTrue(build.call_args.args[2])

    def test_compressed_nested_timeline_extracts_answer(self) -> None:
        import gzip

        conversation = "26ddc0af-7643-443f-b2cc-3d37ac2dc9bb"
        claude_engine._CLAUDE_RPC_PROMPTS[conversation] = ("parent-rid", 1e20, "질문")
        entity = _ld(4, _ld(2, conversation.encode())) + _ld(5, _ld(9, "파일 답변입니다".encode()))
        message = _ld(1, _ld(1, entity))
        compressed = gzip.compress(message)
        frame = bytes([1]) + len(compressed).to_bytes(4, "big") + compressed
        req = {
            "direction": "response",
            "request_id": "timeline-rid",
            "meta": {
                "host": "claude.ai",
                "path": "/claudeai-rpc/anthropic.bard.api.v1alpha.ConversationService/StreamTimeline",
            },
        }
        self.assertEqual(claude_engine.extract(req, frame), "파일 답변입니다")
        self.assertEqual(req["_claude_ui_message_id"], "parent-rid")

    def test_rpc_restores_file_fields(self) -> None:
        req = {
            "direction": "request",
            "content_type": "application/proto",
            "meta": {
                "host": "claude.ai",
                "path": "/claudeai-rpc/anthropic.bard.api.v1alpha.ConversationService/PerformAction",
            },
        }
        context = _ld(2, b"26ddc0af-7643-443f-b2cc-3d37ac2dc9bb")
        file_field = _ld(4, _ld(1, b"file_uuid_1"))
        blocked = _ld(1, context) + _ld(
            2, _ld(1, b"m1") + _ld(2, b"m2") + _ld(3, "주민번호 요약".encode()) + file_field
        )
        state = claude_engine.consent_attachment_state(req, blocked)
        self.assertIsNotNone(state)

        consent = _ld(1, context) + _ld(
            2, _ld(1, b"m3") + _ld(2, b"m4") + _ld(3, "주민번호 요약".encode())
        )
        restored = claude_engine.restore_consent_attachments(req, consent, state)
        self.assertIsNotNone(restored)
        action = claude_engine._perform_action_message(restored)
        self.assertEqual(_fields(action), [1, 2, 3, 4])
        self.assertIn(b"file_uuid_1", action)
        self.assertIn(b"m3", action)
        self.assertNotIn(b"m1", action)

    def test_completion_restores_files_and_blocks(self) -> None:
        req = {
            "direction": "request",
            "content_type": "application/json",
            "meta": {
                "host": "claude.ai",
                "path": _CLAUDE_COMPLETION,
            },
        }
        blocked = json.dumps(
            {"prompt": "요약", "files": ["f-1"], "attachments": [{"file_name": "a.txt"}]}
        ).encode()
        state = claude_engine.consent_attachment_state(req, blocked)
        self.assertEqual(state["top"]["files"], ["f-1"])
        consent = json.dumps({"prompt": "요약", "files": [], "attachments": []}).encode()
        restored = json.loads(claude_engine.restore_consent_attachments(req, consent, state))
        self.assertEqual(restored["files"], ["f-1"])
        self.assertEqual(restored["attachments"], [{"file_name": "a.txt"}])
        self.assertEqual(restored["prompt"], "요약")

    def test_text_only_turn_has_no_state(self) -> None:
        req = {
            "direction": "request",
            "content_type": "application/json",
            "meta": {"host": "claude.ai", "path": _CLAUDE_COMPLETION},
        }
        self.assertIsNone(
            claude_engine.consent_attachment_state(req, json.dumps({"prompt": "x"}).encode())
        )


class ChatGPTConsentAttachments(unittest.TestCase):
    def test_restores_metadata_and_parts(self) -> None:
        req = {"direction": "request", "content_type": "application/json", "meta": {}}
        image = {"content_type": "image_asset_pointer", "asset_pointer": "file-service://file-1"}
        blocked = json.dumps(
            {
                "messages": [
                    {
                        "author": {"role": "user"},
                        "content": {"content_type": "multimodal_text", "parts": [image, "요약해줘"]},
                        "metadata": {"attachments": [{"id": "file-1", "name": "a.png"}]},
                    }
                ]
            }
        ).encode()
        state = chatgpt_engine.consent_attachment_state(req, blocked)
        self.assertIsNotNone(state)
        consent = json.dumps(
            {
                "messages": [
                    {
                        "author": {"role": "user"},
                        "content": {"content_type": "text", "parts": ["요약해줘"]},
                        "metadata": {},
                    }
                ]
            }
        ).encode()
        restored = json.loads(chatgpt_engine.restore_consent_attachments(req, consent, state))
        msg = restored["messages"][0]
        self.assertEqual(msg["content"]["parts"], [image, "요약해줘"])
        self.assertEqual(msg["content"]["content_type"], "multimodal_text")
        self.assertEqual(msg["metadata"]["attachments"][0]["id"], "file-1")


class GeminiConsentAttachments(unittest.TestCase):
    def _form(self, turn: list) -> bytes:
        inner = json.dumps([turn, ["ko"], ["c_0123456789abcdef", "r_0123456789abcdef"]], ensure_ascii=False)
        return urlencode({"f.req": json.dumps([None, inner], ensure_ascii=False), "at": "x"}).encode()

    def test_stream_generate_restores_slot(self) -> None:
        req = {
            "direction": "request",
            "content_type": "application/x-www-form-urlencoded",
            "meta": {
                "host": "gemini.google.com",
                "path": "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate",
            },
        }
        slot = [[["/contrib_service/ttl_1d/abc", 1, None, "image/png"], "a.png"]]
        blocked = self._form(["요약해줘", 0, None, slot, None, None, 0])
        state = gemini_engine.consent_attachment_state(req, blocked)
        self.assertEqual(state, {"kind": "f_req", "slot": slot})

        consent = self._form(["요약해줘", 0, None, None, None, None, 0])
        restored = gemini_engine.restore_consent_attachments(req, consent, state, prompt="요약해줘")
        self.assertIsNotNone(restored)
        form = parse_qs(restored.decode())
        self.assertEqual(form["at"], ["x"])
        inner = json.loads(json.loads(form["f.req"][0])[1])
        self.assertEqual(inner[0][0], "요약해줘")
        self.assertEqual(inner[0][3], slot)

    def test_process_session_restores_file_fields(self) -> None:
        req = {
            "direction": "request",
            "meta": {
                "host": "geminiweb-pa.clients6.google.com",
                "path": "/v1/processSession?gsessionid=g1",
            },
        }
        box = _ld(9, _ld(1, "요약해줘".encode()))
        file_part = _ld(5, b"file-ref-1")
        blocked_buf = _ld(2, _varint(8) + _varint(1) + file_part + box)
        blocked = urlencode(
            {"count": "1", "req0___data__": base64.b64encode(blocked_buf).decode()}
        ).encode()
        state = gemini_engine.consent_attachment_state(req, blocked)
        self.assertEqual(state, {"kind": "process_session", "extras": [file_part]})

        consent_buf = gemini_engine._encode_process_session_prompt("요약해줘")
        consent = urlencode(
            {"count": "1", "req0___data__": base64.b64encode(consent_buf).decode()}
        ).encode()
        restored = gemini_engine.restore_consent_attachments(req, consent, state)
        form = parse_qs(restored.decode())
        buf = base64.b64decode(form["req0___data__"][0])
        self.assertEqual(buf, blocked_buf)
        self.assertEqual(gemini_engine._parse_process_session_prompt(buf), "요약해줘")

    def test_consent_phrase_with_period(self) -> None:
        self.assertTrue(gemini_engine._is_consent_phrase("동의합니다."))
        self.assertFalse(gemini_engine._is_consent_phrase("동의합니다 그리고"))


def _json_request(host: str, path: str, unit_type: str = "json_body") -> dict:
    return {
        "direction": "request",
        "content_type": "application/json",
        "unit_type": unit_type,
        "meta": {"host": host, "path": path},
    }


class GrokConsentAttachments(unittest.TestCase):
    _PATH = "/rest/app-chat/conversations/new"
    _UUID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"

    def test_chat_json_restores_file_refs(self) -> None:
        req = _json_request("grok.com", self._PATH)
        blocked = json.dumps(
            {
                "message": "이 파일 요약해줘",
                "fileAttachments": [self._UUID],
                "fileAttachmentsMetadata": [{"fileMetadataId": self._UUID}],
            },
            ensure_ascii=False,
        ).encode()
        state = grok_engine.consent_attachment_state(req, blocked)
        self.assertIsNotNone(state)

        consent = json.dumps(
            {"message": "이 파일 요약해줘", "fileAttachments": [], "fileAttachmentsMetadata": []},
            ensure_ascii=False,
        ).encode()
        restored = json.loads(grok_engine.restore_consent_attachments(req, consent, state))
        self.assertEqual(restored["fileAttachments"], [self._UUID])
        self.assertEqual(restored["fileAttachmentsMetadata"][0]["fileMetadataId"], self._UUID)
        self.assertEqual(restored["message"], "이 파일 요약해줘")

    def test_ws_frame_restores_item_refs_and_stays_a_frame(self) -> None:
        req = _json_request("grok.com", "/ws/mgw", unit_type="ws_frame")
        blocked_obj = {
            "event": {
                "item": {
                    "file_attachment_ids": [self._UUID],
                    "x_grok": {
                        "input_chunks": [
                            {"mention": {"file_mention": {"file_id": self._UUID}}},
                            {"text": {"text": "이 파일 요약해줘"}},
                        ]
                    },
                }
            }
        }
        blocked = grok_engine._encode_ws_text_frame(
            json.dumps(blocked_obj, ensure_ascii=False).encode(), masked=True
        )
        state = grok_engine.consent_attachment_state(req, blocked)
        self.assertIsNotNone(state)

        consent_obj = {
            "event": {
                "item": {
                    "file_attachment_ids": [],
                    "x_grok": {"input_chunks": [{"text": {"text": "이 파일 요약해줘"}}]},
                }
            }
        }
        consent = grok_engine._encode_ws_text_frame(
            json.dumps(consent_obj, ensure_ascii=False).encode(), masked=True
        )
        restored = grok_engine.restore_consent_attachments(req, consent, state)
        self.assertIsNotNone(restored)
        self.assertNotEqual(restored[:1], b"{")
        doc = grok_engine._ws_json_objects(restored)[0]
        item = doc["event"]["item"]
        self.assertEqual(item["file_attachment_ids"], [self._UUID])
        chunks = item["x_grok"]["input_chunks"]
        self.assertEqual(chunks[0]["mention"]["file_mention"]["file_id"], self._UUID)
        self.assertEqual(chunks[-1]["text"]["text"], "이 파일 요약해줘")

    def test_text_only_turn_has_no_state(self) -> None:
        req = _json_request("grok.com", self._PATH)
        self.assertIsNone(
            grok_engine.consent_attachment_state(
                req, json.dumps({"message": "안녕"}).encode()
            )
        )


class PerplexityConsentAttachments(unittest.TestCase):
    _PATH = "/rest/sse/perplexity_ask"
    _URL = "https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0/a.pdf"

    def test_params_attachments_are_restored(self) -> None:
        req = _json_request("www.perplexity.ai", self._PATH)
        blocked = json.dumps(
            {"query_str": "이 파일 요약해줘", "params": {"attachments": [self._URL]}},
            ensure_ascii=False,
        ).encode()
        state = perplexity_engine.consent_attachment_state(req, blocked)
        self.assertEqual(state["params_attachments"], [self._URL])

        consent = json.dumps(
            {"query_str": "이 파일 요약해줘", "params": {"attachments": []}},
            ensure_ascii=False,
        ).encode()
        restored = json.loads(
            perplexity_engine.restore_consent_attachments(req, consent, state)
        )
        self.assertEqual(restored["params"]["attachments"], [self._URL])
        self.assertEqual(restored["query_str"], "이 파일 요약해줘")

    def test_non_ask_path_is_ignored(self) -> None:
        req = _json_request("www.perplexity.ai", "/rest/event/analytics")
        blocked = json.dumps({"params": {"attachments": [self._URL]}}).encode()
        self.assertIsNone(perplexity_engine.consent_attachment_state(req, blocked))


class WrksConsentAttachments(unittest.TestCase):
    _PATH = "/api/v1/chat/stream"

    def test_gpt_file_ids_and_message_parts_are_restored(self) -> None:
        req = _json_request("gateway-api.wrks.ai", self._PATH)
        blocked = json.dumps(
            {
                "message": {
                    "parts": [
                        {"type": "file", "fileId": 1999068, "filename": "a.pdf"},
                        {"type": "text", "text": "이 파일 요약해줘"},
                    ]
                },
                "gptFileIds": ["1999068"],
            },
            ensure_ascii=False,
        ).encode()
        state = wrks_engine.consent_attachment_state(req, blocked)
        self.assertIsNotNone(state)

        consent = json.dumps(
            {
                "message": {"parts": [{"type": "text", "text": "이 파일 요약해줘"}]},
                "gptFileIds": [],
            },
            ensure_ascii=False,
        ).encode()
        restored = json.loads(wrks_engine.restore_consent_attachments(req, consent, state))
        self.assertEqual(restored["gptFileIds"], ["1999068"])
        parts = restored["message"]["parts"]
        self.assertEqual(parts[0]["fileId"], 1999068)
        self.assertEqual(parts[-1]["text"], "이 파일 요약해줘")

    def test_analyze_image_url_token_is_put_back_on_the_prompt(self) -> None:
        req = _json_request("gateway-api.wrks.ai", self._PATH)
        token = " [analyzeImageUrl] https://images.wrks.ai/dalle/abc_1.png"
        blocked = json.dumps(
            {"message": f"이 이미지 설명해줘{token}", "visionImageGptFileId": "1999069"},
            ensure_ascii=False,
        ).encode()
        state = wrks_engine.consent_attachment_state(req, blocked)
        self.assertTrue(state["message_tokens"])

        consent = json.dumps({"message": "이 이미지 설명해줘"}, ensure_ascii=False).encode()
        restored = json.loads(wrks_engine.restore_consent_attachments(req, consent, state))
        self.assertIn("[analyzeImageUrl]", restored["message"])
        self.assertTrue(restored["message"].startswith("이 이미지 설명해줘"))
        self.assertEqual(restored["visionImageGptFileId"], "1999069")


class NotionConsentAttachments(unittest.TestCase):
    _PATH = "/api/v3/runInferenceTranscript"

    def test_attachment_block_returns_ahead_of_the_user_block(self) -> None:
        req = _json_request("www.notion.so", self._PATH)
        attachment = {
            "type": "attachment",
            "fileUrl": "attachment:7ed69fea-4b5a-6978-8796-a5b4c3d2e1f0:a.pdf",
            "fileName": "a.pdf",
        }
        blocked = json.dumps(
            {
                "transcript": [
                    {"type": "context", "value": {"userEmail": "u@example.com"}},
                    attachment,
                    {"type": "user", "value": [["이 파일 요약해줘"]]},
                ]
            },
            ensure_ascii=False,
        ).encode()
        state = notion_engine.consent_attachment_state(req, blocked)
        self.assertEqual(state["blocks"], [attachment])

        consent = json.dumps(
            {
                "transcript": [
                    {"type": "context", "value": {"userEmail": "u@example.com"}},
                    {"type": "user", "value": [["이 파일 요약해줘"]]},
                ]
            },
            ensure_ascii=False,
        ).encode()
        restored = json.loads(
            notion_engine.restore_consent_attachments(req, consent, state)
        )
        types = [block["type"] for block in restored["transcript"]]
        self.assertEqual(types, ["context", "attachment", "user"])
        self.assertEqual(restored["transcript"][1]["fileName"], "a.pdf")

    def test_gzip_body_stays_gzip(self) -> None:
        import zlib

        req = _json_request("www.notion.so", self._PATH)
        attachment = {"type": "attachment", "fileUrl": "attachment:f-1:a.pdf"}

        def _gz(obj: dict) -> bytes:
            raw = json.dumps(obj, ensure_ascii=False).encode()
            return zlib.compress(raw, 9, 16 + zlib.MAX_WBITS)

        state = notion_engine.consent_attachment_state(
            req, _gz({"transcript": [attachment, {"type": "user", "value": [["요약"]]}]})
        )
        self.assertIsNotNone(state)
        restored = notion_engine.restore_consent_attachments(
            req, _gz({"transcript": [{"type": "user", "value": [["요약"]]}]}), state
        )
        self.assertEqual(restored[:2], b"\x1f\x8b")
        body = json.loads(zlib.decompress(restored, 16 + zlib.MAX_WBITS))
        self.assertEqual(body["transcript"][0]["fileUrl"], "attachment:f-1:a.pdf")


class CopilotConsentAttachments(unittest.TestCase):
    _PATH = "/responses"

    def test_input_file_part_is_restored(self) -> None:
        req = _json_request("api.githubcopilot.com", self._PATH)
        file_part = {"type": "input_file", "file_id": "file-1", "filename": "a.pdf"}
        blocked = json.dumps(
            {
                "input": [
                    {
                        "role": "user",
                        "content": [file_part, {"type": "input_text", "text": "요약해줘"}],
                    }
                ],
                "attachments": [{"id": "file-1"}],
            },
            ensure_ascii=False,
        ).encode()
        state = github_copilot_engine.consent_attachment_state(req, blocked)
        self.assertIsNotNone(state)

        consent = json.dumps(
            {
                "input": [
                    {"role": "user", "content": [{"type": "input_text", "text": "요약해줘"}]}
                ],
                "attachments": [],
            },
            ensure_ascii=False,
        ).encode()
        restored = json.loads(
            github_copilot_engine.restore_consent_attachments(req, consent, state)
        )
        content = restored["input"][0]["content"]
        self.assertEqual(content[0], file_part)
        self.assertEqual(content[-1]["text"], "요약해줘")
        self.assertEqual(restored["attachments"], [{"id": "file-1"}])


class AdapterConsentCoverage(unittest.TestCase):
    def test_every_file_capable_adapter_can_replay_its_attachments(self) -> None:
        for module in (
            chatgpt_engine,
            claude_engine,
            gemini_engine,
            grok_engine,
            perplexity_engine,
            wrks_engine,
            notion_engine,
            github_copilot_engine,
            timely_engine,
        ):
            with self.subTest(adapter=module.__name__):
                self.assertTrue(hasattr(module, "consent_attachment_state"))
                self.assertTrue(hasattr(module, "restore_consent_attachments"))


class _Adapter:
    def __init__(self) -> None:
        self.calls = 0

    def restore_consent_attachments(self, req, payload, state, prompt=""):
        self.calls += 1
        return payload + b"+files"


class ChatgptResponsePrefix(unittest.TestCase):
    def test_first_delta_gets_the_notice_and_the_next_does_not(self) -> None:
        notice = "⚠️ 주의\n첨부파일에 대한 결재가 상신됩니다."
        req = {
            "direction": "response",
            "unit_type": "sse_event",
            "content_type": "text/event-stream",
            "_cso_response_prefix_key": "conv-prefix",
        }
        first = 'data: {"p":"/message/content/parts/0","o":"append","v":"안녕"}\n\n'.encode()
        out = chatgpt_engine.prefix_response(req, first, notice)
        self.assertIsNotNone(out)
        raw = (out or (b"", ""))[0].decode("utf-8")
        blob = raw.split("data:", 1)[1].strip()
        shown = str(json.loads(blob).get("v") or "")
        self.assertTrue(shown.startswith(notice))
        self.assertTrue(shown.endswith("안녕"))
        second = 'data: {"p":"/message/content/parts/0","o":"append","v":"하세요"}\n\n'.encode()
        self.assertIsNone(chatgpt_engine.prefix_response(req, second, notice))
        chatgpt_engine.clear_response_prefix_state("conv-prefix")


class EngineRestoreGuard(unittest.TestCase):
    def _run(self, hit, sessions, ip):
        adapter = _Adapter()
        out = ape.PolicyEngine._restore_consent_attachments(
            adapter, {}, b"body", hit, sessions, ip, "p", "rid", "svc"
        )
        return out, adapter.calls

    def test_same_chat_restores(self) -> None:
        hit = {"attachments": {"k": 1}, "session_id": "c1", "client_ip": "10.0.0.1"}
        self.assertEqual(self._run(hit, ["c1"], "10.0.0.1"), (b"body+files", 1))

    def test_other_chat_or_client_is_prompt_only(self) -> None:
        hit = {"attachments": {"k": 1}, "session_id": "c1", "client_ip": "10.0.0.1"}
        self.assertEqual(self._run(hit, ["c2"], "10.0.0.1"), (b"body", 0))
        self.assertEqual(self._run(hit, ["c1"], "10.0.0.2"), (b"body", 0))

    def test_no_state_is_prompt_only(self) -> None:
        self.assertEqual(self._run({"session_id": "c1"}, ["c1"], ""), (b"body", 0))

    def test_cso_open_notice_restores_file_refs_after_rewrite(self) -> None:
        class _Adapter:
            def consent_attachment_state(self, req, payload):
                return {"files": ["file-1"]}

            def restore_consent_attachments(self, req, payload, state, prompt=""):
                body = json.loads(payload)
                body["files"] = state["files"]
                return json.dumps(body, ensure_ascii=False).encode()

        original = json.dumps(
            {"prompt": "파일 요약", "files": ["file-1"]},
            ensure_ascii=False,
        ).encode()
        rewritten = json.dumps(
            {"prompt": "O등급 결제가 상신됩니다.\n\n파일 요약", "files": []},
            ensure_ascii=False,
        ).encode()
        restored = ape.PolicyEngine._restore_cso_open_attachments(
            _Adapter(),
            {},
            original,
            rewritten,
            "O등급 결제가 상신됩니다.\n\n파일 요약",
            "cso-open-attachment",
            "gemini",
        )
        self.assertEqual(json.loads(restored)["files"], ["file-1"])


if __name__ == "__main__":
    unittest.main()
