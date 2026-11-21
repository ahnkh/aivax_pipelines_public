#!/usr/bin/env python3
"""Customer-regression coverage for prompt/output transport cleanup."""

from __future__ import annotations

import html
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, Optional
from unittest import mock
from urllib.parse import quote_plus


_ROOT = Path(__file__).resolve().parents[1]
for _path in (_ROOT, _ROOT / "core", _ROOT / "adapters"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from adapters import (  # noqa: E402
    chatgpt_engine,
    claude_engine,
    gemini_engine,
    grok_engine,
    perplexity_engine,
)
from core import ai_policy_engine  # noqa: E402
from core.http_payload_normalizer import normalize_http_payload  # noqa: E402
from core.ai_policy_engine import extract_text_for_policy  # noqa: E402


class TestHttpPayloadNormalizer(unittest.TestCase):
    def test_trims_next_keepalive_request_after_complete_json(self) -> None:
        payload = (
            b'{"prompt":"hello","conversation_id":"abc"}'
            b"POST /ces/v1/t HTTP/1.1\r\nHost: chatgpt.com\r\n\r\n{}"
        )
        body, info = normalize_http_payload(
            payload,
            unit_type="json_body",
            content_type="application/json",
        )
        self.assertEqual(
            body,
            b'{"prompt":"hello","conversation_id":"abc"}',
        )
        self.assertTrue(info["stitch_trimmed"])
        self.assertFalse(info["suspect"])

    def test_strips_http_response_headers_and_dechunks(self) -> None:
        payload = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            b"7\r\n{\"a\":1}\r\n0\r\n\r\n"
        )
        body, info = normalize_http_payload(
            payload,
            unit_type="json_body",
            content_type="application/json",
        )
        self.assertEqual(body, b'{"a":1}')
        self.assertTrue(info["header_stripped"])
        self.assertTrue(info["dechunked"])

    def test_incomplete_chunk_is_fail_open_and_suspect(self) -> None:
        payload = b"20\r\nshort"
        body, info = normalize_http_payload(payload, content_type="text/plain")
        self.assertEqual(body, payload)
        self.assertTrue(info["suspect"])
        self.assertEqual(info["reason"], "malformed_or_incomplete_chunked")

    def test_multipart_is_never_modified(self) -> None:
        payload = b"7\r\nbinary\r\nPOST /inside-file HTTP/1.1\r\n"
        body, info = normalize_http_payload(
            payload,
            content_type="multipart/form-data; boundary=x",
        )
        self.assertEqual(body, payload)
        self.assertFalse(info["dechunked"])
        self.assertFalse(info["stitch_trimmed"])

    def test_account_html_body_is_preserved_after_header_strip(self) -> None:
        html = b'<html><script>window.WIZ_global_data={"S06Grb":"user@example.com"}</script></html>'
        payload = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n" + html
        body, info = normalize_http_payload(
            payload,
            content_type="text/html",
        )
        self.assertEqual(body, html)
        self.assertTrue(info["header_stripped"])

    def test_masked_ws_frame_is_not_suspect_binary(self) -> None:
        payload = b"\x81\xfe\x02\x56" + b"\x00" * 602
        body, info = normalize_http_payload(payload, unit_type="ws_frame")
        self.assertEqual(body, payload)
        self.assertFalse(info["suspect"])


class TestPolicyInputScope(unittest.TestCase):
    def test_generic_json_fallback_extracts_user_prompt_only(self) -> None:
        payload = json.dumps(
            {
                "prompt": "사용자 질문",
                "parent_message_uuid": "019fff09-00e8-777e-984b-364efd3bf981",
            },
            ensure_ascii=False,
        ).encode()
        req = {"unit_type": "json_body", "content_type": "application/json"}
        self.assertEqual(extract_text_for_policy(req, payload), "사용자 질문")

    def test_generic_json_without_user_text_does_not_scan_metadata(self) -> None:
        payload = json.dumps(
            {
                "parent_message_uuid": "019fff09-00e8-777e-984b-364efd3bf981",
                "conversation_id": "6a61cc59-8e20-83ee-b0a5-98d181b27e73",
            }
        ).encode()
        req = {"unit_type": "json_body", "content_type": "application/json"}
        self.assertEqual(extract_text_for_policy(req, payload), "")

    def test_claude_policy_payload_contains_prompt_not_parent_uuid(self) -> None:
        obj = {
            "prompt": "정상 질문",
            "parent_message_uuid": "019fff09-00e8-777e-984b-364efd3bf981",
        }
        payload = json.dumps(obj, ensure_ascii=False).encode()
        req = {
            "direction": "request",
            "content_type": "application/json",
            "meta": {
                "host": "claude.ai",
                "path": (
                    "/api/organizations/11111111-1111-1111-1111-111111111111/"
                    "chat_conversations/22222222-2222-2222-2222-222222222222/completion"
                ),
            },
        }
        built = claude_engine.build_policy_api_payload(req, payload, "정상 질문")
        self.assertIsNotNone(built)
        self.assertEqual(built["prompt"], "정상 질문")
        self.assertNotIn("parent_message_uuid", built["prompt"])

    def test_claude_perform_action_protobuf_is_the_prompt(self) -> None:
        def varint(value: int) -> bytes:
            out = bytearray()
            while True:
                byte = value & 0x7F
                value >>= 7
                if value:
                    out.append(byte | 0x80)
                else:
                    out.append(byte)
                    return bytes(out)

        def field_bytes(field: int, blob: bytes) -> bytes:
            return varint((field << 3) | 2) + varint(len(blob)) + blob

        def field_str(field: int, text: str) -> bytes:
            return field_bytes(field, text.encode())

        conversation = "26ddc0af-7643-443f-b2cc-3d37ac2dc9bb"
        context = field_str(2, conversation) + varint((3 << 3) | 0) + varint(3)
        action = (
            field_str(1, "a8b3dc2d-3d5e-4a4e-9583-9c5807741c93")
            + field_str(2, "b881ecbe-af37-41ec-9272-fd60da28e74a")
            + field_str(3, "CVE 목록을 docx로 뽑아줘")
            + field_bytes(4, field_str(1, "file_uuid_should_be_removed"))
        )
        top_context = field_bytes(4, field_str(1, "required_top_level_context"))
        payload = field_bytes(1, context) + field_bytes(2, action) + top_context
        req = {
            "direction": "request",
            "request_id": "req-claude-rpc",
            "content_type": "application/proto",
            "meta": {
                "host": "claude.ai",
                "path": "/claudeai-rpc/anthropic.bard.api.v1alpha.ConversationService/PerformAction",
                "client_ip": "10.0.50.60",
            },
        }
        self.assertFalse(claude_engine.claude_should_skip_policy(req, payload))
        built = claude_engine.build_policy_api_payload(req, payload, "")
        self.assertEqual(built["prompt"], "CVE 목록을 docx로 뽑아줘")
        self.assertEqual(built["session_id"], conversation)
        heartbeat = field_bytes(1, context)
        self.assertTrue(claude_engine.claude_should_skip_policy(req, heartbeat))
        self.assertIsNone(claude_engine.build_policy_api_payload(req, heartbeat, ""))
        # Classic /completion JSON is a different request and must still be inspected.
        classic = {
            "direction": "request",
            "content_type": "application/json",
            "meta": {
                "host": "claude.ai",
                "path": (
                    "/api/organizations/11111111-1111-1111-1111-111111111111/"
                    "chat_conversations/22222222-2222-2222-2222-222222222222/completion"
                ),
            },
        }
        self.assertFalse(
            claude_engine.claude_should_skip_policy(
                classic, '{"prompt":"기존 방식"}'.encode()
            )
        )
        claude_engine._CLAUDE_RPC_PROMPTS.clear()
        self.assertEqual(claude_engine.extract(req, payload), "CVE 목록을 docx로 뽑아줘")

        def timeline(text: str) -> bytes:
            # Live shape: 1 { 1 { 2: workspace, 4: conversation, 5: message { 9: body } } }
            workspace = field_bytes(2, field_str(8, "type.googleapis.com/anthropic.bard.api.v1alpha.WorkspaceOutputs") + field_str(3, "workspace file listing must not be the answer " * 5))
            conv = field_bytes(4, field_str(2, conversation) + field_str(5, "대화 제목"))
            message = field_bytes(5, field_str(1, "msg_01abcdefghijklmnopqrstu") + field_str(9, text))
            body = field_bytes(1, field_bytes(1, workspace + conv + message))
            return b"\x00" + len(body).to_bytes(4, "big") + body

        state_only = field_bytes(1, field_bytes(1, field_bytes(2, field_str(3, "sandbox state " * 20)) + field_bytes(4, field_str(2, conversation))))
        response_state = {
            "direction": "response",
            "request_id": "req-claude-stream",
            "meta": {
                "host": "claude.ai",
                "path": "/claudeai-rpc/anthropic.bard.api.v1alpha.ConversationService/StreamTimeline",
            },
        }
        self.assertEqual(
            claude_engine.extract(
                response_state, b"\x00" + len(state_only).to_bytes(4, "big") + state_only
            ),
            "",
        )
        frame = timeline("docx로 정리했습니다")
        response = {
            "direction": "response",
            "request_id": "req-claude-stream",
            "content_type": "application/connect+proto",
            "meta": {
                "host": "claude.ai",
                "path": "/claudeai-rpc/anthropic.bard.api.v1alpha.ConversationService/StreamTimeline",
            },
        }
        self.assertEqual(claude_engine.extract(response, frame), "docx로 정리했습니다")
        self.assertEqual(response["_claude_ui_message_id"], "req-claude-rpc")
        # INLINE marks the body _eve_full_body and calls extract_full_body.
        full = dict(response)
        self.assertEqual(claude_engine.extract_full_body(full, frame), "docx로 정리했습니다")
        self.assertTrue(full.get("_claude_response_growing"))
        # Desktop app streams the answer one level higher: 1 { 5 { 9: body } }.
        desktop_answer = (
            "Hello! Good to meet you. It's Thursday afternoon, October 22, 2026, "
            "here in Seoul. What can I help you with today?"
        )
        desktop_entity = field_bytes(
            5,
            field_str(1, "cblk_011CgGi7Ci3uXVtAGK6eeokX")
            + field_str(2, "dgrp_018gLwA6UTzrxMcAwYbfsStM")
            + field_str(9, desktop_answer),
        )
        desktop_body = field_bytes(1, field_str(2, conversation) + desktop_entity)
        desktop_frame = b"\x00" + len(desktop_body).to_bytes(4, "big") + desktop_body
        desktop_prompt = field_bytes(
            1,
            field_str(2, conversation)
            + field_bytes(5, field_str(1, "cblk_user") + field_str(9, "CVE 목록을 docx로 뽑아줘")),
        )
        desktop_prompt_frame = b"\x00" + len(desktop_prompt).to_bytes(4, "big") + desktop_prompt
        self.assertEqual(claude_engine.extract(response, desktop_prompt_frame), "")
        self.assertEqual(claude_engine.extract(response, desktop_frame), desktop_answer)
        # Desktop field 9 stops at a short prefix. Later frames append field 2
        # deltas and must stay on the prompt's output id.
        cblk = "cblk_011CgGjj4J9TnpRpacW1Er2r"
        prefix = field_bytes(
            1,
            field_str(2, conversation)
            + field_bytes(
                5,
                field_str(1, cblk) + field_str(2, "dgrp_01YTkGfj2iP5cdeaixMHBTot") + field_str(9, "조선시대는"),
            ),
        )
        prefix_frame = b"\x00" + len(prefix).to_bytes(4, "big") + prefix
        delta_piece = " 1392년 이성계가 고려를"
        delta = field_bytes(
            1,
            field_bytes(2, field_str(1, cblk) + field_str(2, delta_piece)),
        )
        delta_frame = b"\x00" + len(delta).to_bytes(4, "big") + delta
        prefix_req = dict(response)
        self.assertEqual(claude_engine.extract(prefix_req, prefix_frame), "조선시대는")
        self.assertEqual(prefix_req["_claude_ui_message_id"], "req-claude-rpc")
        delta_req = {
            "direction": "response",
            "request_id": "req-claude-stream-delta",
            "meta": {
                "host": "claude.ai",
                "path": "/claudeai-rpc/anthropic.bard.api.v1alpha.ConversationService/StreamTimeline",
            },
        }
        self.assertEqual(
            claude_engine.extract(delta_req, delta_frame),
            "조선시대는" + delta_piece,
        )
        self.assertEqual(delta_req["_claude_ui_message_id"], "req-claude-rpc")
        # A model switch can announce the conversation in a state frame, then
        # answer on a new cblk in later frames of the same StreamTimeline.
        stream_req = {
            **response,
            "request_id": "req-claude-model-stream",
        }
        state_body = field_bytes(1, field_str(2, conversation))
        state_frame = b"\x00" + len(state_body).to_bytes(4, "big") + state_body
        self.assertEqual(claude_engine.extract(stream_req, state_frame), "")
        switched_cblk = "cblk_011ModelChangedAnswer123"
        switched_delta_body = field_bytes(
            1,
            field_bytes(
                2,
                field_str(1, switched_cblk)
                + field_str(2, "# TLS 1.3 개요"),
            ),
        )
        switched_delta_frame = (
            b"\x00"
            + len(switched_delta_body).to_bytes(4, "big")
            + switched_delta_body
        )
        later_req = {
            **response,
            "request_id": "req-claude-model-stream",
        }
        self.assertEqual(
            claude_engine.extract(later_req, switched_delta_frame),
            "# TLS 1.3 개요",
        )
        self.assertEqual(later_req["_claude_ui_message_id"], "req-claude-rpc")
        # A file-read tool result is not the answer. The prose that follows on a
        # new StreamTimeline for the same client replaces it on the prompt row.
        tool_cblk = "cblk_01ToolResultSlideDump"
        slide = "--- Slide 1 ---\nGPT-6 Luna High\n[notes] 발표를 마칩니다."
        tool_body = field_bytes(
            1,
            field_str(2, conversation)
            + field_bytes(
                5,
                field_str(1, tool_cblk)
                + field_str(5, "명령 실행 중")
                + field_str(15, "Bash")
                + field_str(9, slide)
                + field_str(37, "toolu_01H1g8PkUydtDGBDETY16VvB"),
            ),
        )
        tool_frame = b"\x00" + len(tool_body).to_bytes(4, "big") + tool_body
        def stream_req(request_id: str, client_ip: str) -> dict:
            return {
                "direction": "response",
                "request_id": request_id,
                "content_type": "application/connect+proto",
                "meta": {
                    "host": "claude.ai",
                    "path": "/claudeai-rpc/anthropic.bard.api.v1alpha.ConversationService/StreamTimeline",
                    "client_ip": client_ip,
                },
            }

        tool_req = stream_req("req-claude-tool-stream", "10.0.50.60")
        self.assertEqual(claude_engine.extract(tool_req, tool_frame), "")
        tool_delta_body = field_bytes(
            1,
            field_bytes(2, field_str(1, tool_cblk) + field_str(2, slide)),
        )
        tool_delta_frame = (
            b"\x00" + len(tool_delta_body).to_bytes(4, "big") + tool_delta_body
        )
        tool_delta_req = stream_req("req-claude-tool-stream", "10.0.50.60")
        self.assertEqual(claude_engine.extract(tool_delta_req, tool_delta_frame), "")
        prose = "파일 내용을 먼저 읽어볼게요.\n\n## 1. 파일 요약"
        prose_body = field_bytes(
            1,
            field_bytes(
                2,
                field_str(1, "cblk_01VisibleAnswer") + field_str(2, prose),
            ),
        )
        prose_frame = b"\x00" + len(prose_body).to_bytes(4, "big") + prose_body
        prose_req = stream_req("req-claude-after-tool", "10.0.50.60")
        self.assertEqual(claude_engine.extract(prose_req, prose_frame), prose)
        self.assertEqual(prose_req["_claude_ui_message_id"], "req-claude-rpc")
        other_body = field_bytes(
            1,
            field_bytes(
                2,
                field_str(1, "cblk_01OtherClient")
                + field_str(2, "다른 사용자 답변입니다"),
            ),
        )
        other_frame = b"\x00" + len(other_body).to_bytes(4, "big") + other_body
        other_req = stream_req("req-claude-other-client", "10.0.50.61")
        self.assertEqual(
            claude_engine.extract(other_req, other_frame), "다른 사용자 답변입니다"
        )
        self.assertNotIn("_claude_ui_message_id", other_req)
        foreign_body = field_bytes(
            1,
            field_str(2, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
            + field_bytes(
                5,
                field_str(1, "cblk_01ForeignChat") + field_str(9, "다른 대화"),
            ),
        )
        foreign_frame = b"\x00" + len(foreign_body).to_bytes(4, "big") + foreign_body
        foreign_req = stream_req("req-claude-foreign-chat", "10.0.50.60")
        self.assertEqual(claude_engine.extract(foreign_req, foreign_frame), "다른 대화")
        self.assertNotIn("_claude_ui_message_id", foreign_req)
        frame2 = timeline("docx로 정리했습니다. 표도 넣었습니다")
        self.assertEqual(
            claude_engine.extract(response, frame2),
            "docx로 정리했습니다. 표도 넣었습니다",
        )
        rewritten, reason = claude_engine.rewrite_prompt_for_block(req, payload, "차단되었습니다", "")
        self.assertEqual(reason, "adapter:claude_policy_block_sysinstruct")
        self.assertEqual(claude_engine._extract_perform_action_prompt(rewritten), "차단되었습니다")
        self.assertEqual(claude_engine._extract_perform_action_conversation(rewritten), conversation)
        self.assertNotIn(b"file_uuid_should_be_removed", rewritten)
        self.assertIn(b"required_top_level_context", rewritten)
        rewritten_action = claude_engine._perform_action_message(rewritten)
        self.assertEqual(
            [field for field, _wire, _value in claude_engine._pb_iter(rewritten_action)],
            [1, 2, 3],
        )

    def test_claude_stream_keeps_a_new_turn_off_the_previous_prompt(self) -> None:
        """A long-lived StreamTimeline omits the conversation uuid on deltas.

        A newer PerformAction must own new cblks. The previous answer's cblk
        stays on the previous prompt, including a short follow-up that would
        otherwise be dropped as a shorter revision of the first answer.
        """
        def varint(value: int) -> bytes:
            out = bytearray()
            while True:
                byte = value & 0x7F
                value >>= 7
                if value:
                    out.append(byte | 0x80)
                else:
                    out.append(byte)
                    return bytes(out)

        def field_bytes(field: int, blob: bytes) -> bytes:
            return varint((field << 3) | 2) + varint(len(blob)) + blob

        def field_str(field: int, text: str) -> bytes:
            return field_bytes(field, text.encode())

        def frame(body: bytes) -> bytes:
            return b"\x00" + len(body).to_bytes(4, "big") + body

        for store in (
            claude_engine._CLAUDE_RPC_PROMPTS,
            claude_engine._CLAUDE_CBLK_TEXT,
            claude_engine._CLAUDE_CBLK_PARENT,
            claude_engine._CLAUDE_STREAM_PARENT,
            claude_engine._CLAUDE_STREAM_CONV,
            claude_engine._CLAUDE_CLIENT_PARENT,
            claude_engine._CLAUDE_TOOL_CBLK,
        ):
            store.clear()

        conversation = "26ddc0af-7643-443f-b2cc-3d37ac2dc9bb"
        client_ip = "10.0.80.12"

        def perform(request_id: str, prompt: str) -> None:
            context = field_str(2, conversation) + varint((3 << 3) | 0) + varint(3)
            action = (
                field_str(1, "a8b3dc2d-3d5e-4a4e-9583-9c5807741c93")
                + field_str(2, "b881ecbe-af37-41ec-9272-fd60da28e74a")
                + field_str(3, prompt)
            )
            payload = field_bytes(1, context) + field_bytes(2, action)
            req = {
                "direction": "request",
                "request_id": request_id,
                "content_type": "application/proto",
                "meta": {
                    "host": "claude.ai",
                    "path": "/claudeai-rpc/anthropic.bard.api.v1alpha.ConversationService/PerformAction",
                    "client_ip": client_ip,
                },
            }
            self.assertEqual(claude_engine.extract(req, payload), prompt)

        def timeline(request_id: str, body: bytes) -> dict:
            return {
                "direction": "response",
                "request_id": request_id,
                "content_type": "application/connect+proto",
                "meta": {
                    "host": "claude.ai",
                    "path": "/claudeai-rpc/anthropic.bard.api.v1alpha.ConversationService/StreamTimeline",
                    "client_ip": client_ip,
                },
            }

        perform("prompt-a", "첫번째 질문")
        first_body = field_bytes(
            1,
            field_str(2, conversation)
            + field_bytes(
                5,
                field_str(1, "cblk_first_answer") + field_str(9, "첫번째 답변입니다"),
            ),
        )
        first_req = timeline("stream-live", first_body)
        self.assertEqual(
            claude_engine.extract(first_req, frame(first_body)),
            "첫번째 답변입니다",
        )
        self.assertEqual(first_req["_claude_ui_message_id"], "prompt-a")

        perform("prompt-b", "두번째 질문")
        second_body = field_bytes(
            1,
            field_bytes(
                2,
                field_str(1, "cblk_second_answer") + field_str(2, "두번째 짧은 답"),
            ),
        )
        second_req = timeline("stream-live", second_body)
        self.assertEqual(
            claude_engine.extract(second_req, frame(second_body)),
            "두번째 짧은 답",
        )
        self.assertEqual(second_req["_claude_ui_message_id"], "prompt-b")

        late_body = field_bytes(
            1,
            field_bytes(
                2,
                field_str(1, "cblk_first_answer") + field_str(2, " 이어서"),
            ),
        )
        late_req = timeline("stream-live", late_body)
        self.assertEqual(
            claude_engine.extract(late_req, frame(late_body)),
            "첫번째 답변입니다 이어서",
        )
        self.assertEqual(late_req["_claude_ui_message_id"], "prompt-a")

    def test_claude_perform_action_inline_file_is_an_attachment(self) -> None:
        def varint(value: int) -> bytes:
            out = bytearray()
            while True:
                byte = value & 0x7F
                value >>= 7
                if value:
                    out.append(byte | 0x80)
                else:
                    out.append(byte)
                    return bytes(out)

        def field_bytes(field: int, blob: bytes) -> bytes:
            return varint((field << 3) | 2) + varint(len(blob)) + blob

        def field_str(field: int, text: str) -> bytes:
            return field_bytes(field, text.encode())

        content = b"<!DOCTYPE html>\n<h1>inline file</h1>\n"
        file_blob = (
            field_str(1, "attach (6).html")
            + varint((2 << 3) | 0)
            + varint(len(content))
            + field_str(3, "text/html")
            + field_bytes(4, content)
        )
        conversation = "26ddc0af-7643-443f-b2cc-3d37ac2dc9bb"
        context = field_str(2, conversation)
        action = (
            field_str(1, "a8b3dc2d-3d5e-4a4e-9583-9c5807741c93")
            + field_str(2, "b881ecbe-af37-41ec-9272-fd60da28e74a")
            + field_str(3, "파일")
            + field_str(12, "Asia/Seoul")
            + field_str(13, "ko")
            + field_bytes(15, file_blob)
            + b"/di"
        )
        payload = field_bytes(1, context) + field_bytes(2, action)
        req = {
            "direction": "request",
            "request_id": "req-claude-inline-file",
            "content_type": "application/proto",
            "meta": {
                "host": "claude.ai",
                "path": "/claudeai-rpc/anthropic.bard.api.v1alpha.ConversationService/PerformAction",
                "client_ip": "10.0.50.60",
            },
        }
        import tempfile
        import file_upload_manager as fum

        previous = fum._UPLOAD_SAVE_DIR
        fum._UPLOAD_SAVE_DIR = tempfile.mkdtemp(prefix="claude-inline-")
        try:
            built = claude_engine.build_policy_api_payload(req, payload, "")
        finally:
            fum._UPLOAD_SAVE_DIR = previous
        self.assertEqual(built["prompt"], "파일")
        named = [item for item in built["attachments"] if item.get("name")]
        self.assertEqual(len(named), 1)
        self.assertEqual(named[0]["name"], "attach (6).html")
        self.assertEqual(named[0]["mime_type"], "text/html")
        self.assertEqual(named[0]["size"], len(content))
        self.assertTrue(str(named[0]["id"]))

    def test_claude_completion_waits_for_profile_identity(self) -> None:
        org_id = "11111111-1111-1111-1111-111111111111"
        payload = json.dumps({"prompt": "프로필보다 먼저 온 질문"}).encode()
        req = {
            "direction": "request",
            "content_type": "application/json",
            "meta": {
                "host": "claude.ai",
                "path": (
                    f"/api/organizations/{org_id}/chat_conversations/"
                    "22222222-2222-2222-2222-222222222222/completion"
                ),
            },
        }
        with mock.patch.object(claude_engine, "_lookup_claude_account", return_value=None):
            built = claude_engine.build_policy_api_payload(req, payload, "프로필보다 먼저 온 질문")
        self.assertEqual(built["user_id"], "")
        self.assertEqual(built["email"], "")

        with mock.patch.object(
            claude_engine,
            "_lookup_claude_account",
            return_value=(org_id, "user@example.com"),
        ):
            built = claude_engine.build_policy_api_payload(req, payload, "프로필보다 먼저 온 질문")
        self.assertEqual(built["user_id"], org_id)
        self.assertEqual(built["email"], "user@example.com")

    def test_claude_response_promotes_late_profile_identity(self) -> None:
        org_id = "11111111-1111-1111-1111-111111111111"
        req = {
            "direction": "response",
            "content_type": "text/event-stream",
            "meta": {
                "host": "claude.ai",
                "path": (
                    f"/api/organizations/{org_id}/chat_conversations/"
                    "22222222-2222-2222-2222-222222222222/completion"
                ),
            },
        }
        with mock.patch.object(
            claude_engine,
            "_wait_for_claude_account",
            return_value=(org_id, "user@example.com"),
        ):
            built = claude_engine.build_policy_api_payload(req, b"", "응답")
        self.assertEqual(built["user_id"], org_id)
        self.assertEqual(built["email"], "user@example.com")

    def test_gemini_stream_uses_cached_profile_not_psid(self) -> None:
        req = {
            "direction": "request",
            "meta": {
                "host": "gemini.google.com",
                "path": "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate",
                "cookie": "__Secure-1PSID=psid-device-1",
            },
        }
        fake_lookup = mock.Mock()
        fake_lookup.get.return_value = None
        with mock.patch.object(gemini_engine, "_account_lookup", fake_lookup):
            built = gemini_engine.build_policy_api_payload(req, b"", "질문")
        self.assertEqual(built["user_id"], "")
        self.assertEqual(built["email"], "")

        fake_lookup.get.return_value = ("gaia-1", "user@example.com")
        with mock.patch.object(gemini_engine, "_account_lookup", fake_lookup):
            built = gemini_engine.build_policy_api_payload(req, b"", "질문")
        self.assertEqual(built["user_id"], "gaia-1")
        self.assertEqual(built["email"], "user@example.com")

    def test_gemini_list_accounts_registers_psid_identity(self) -> None:
        req = {
            "direction": "response",
            "content_type": "application/json",
            "meta": {
                "host": "accounts.google.com",
                "path": "/ListAccounts?gpsia=1&source=ChromiumBrowser",
                "cookie": "__Secure-1PSID=psid-device-1; SID=fallback",
                "client_ip": "10.0.0.7",
            },
        }
        body = (
            b")]}'\n"
            b'["gaia.l.a.r",[["gaia.l.a",1,"First Last",'
            b'"user@example.com","//photo",1,1,0,0,1,"123456789012345678901",'
            b'11,12,13,0]]]'
        )
        fake_lookup = mock.Mock()
        with mock.patch.object(gemini_engine, "_account_lookup", fake_lookup):
            self.assertEqual(gemini_engine.extract(req, body), "")
        fake_lookup.set_account.assert_called_once_with(
            "gemini",
            "psid-device-1",
            "123456789012345678901",
            "user@example.com",
            client_ip="10.0.0.7",
            display_name="First Last",
        )
        self.assertFalse(req["_extract_final"])

    def test_gemini_list_accounts_prefers_signed_in_record(self) -> None:
        body = json.dumps(
            [
                "gaia.l.a.r",
                [
                    ["gaia.l.a", 1, "Old", "old@example.com", "", 1, 1, 0, 0, 1, "111", 11, 12, 13, 1],
                    ["gaia.l.a", 1, "Active", "active@example.com", "", 1, 1, 0, 0, 1, "222", 11, 12, 13, 0],
                ],
            ]
        )
        self.assertEqual(
            gemini_engine._extract_account_from_list_accounts(body),
            {"email": "active@example.com", "gaia_id": "222", "name": "Active"},
        )

    def test_gemini_account_rpc_registers_psid_identity(self) -> None:
        gaia_id = "123456789012345678901"
        flags = [None, 7, True, None, None, None, None, None, gaia_id]
        profile = [gaia_id, None, None, None, None, None, None, None, None]
        profile.append([[flags, "user@example.com", None, None]])
        inner = [[["me", 1, profile]]]
        frame = json.dumps(
            [["wrb.fr", "o30O0e", json.dumps(inner), None, None, None, "generic"]],
            separators=(",", ":"),
        )
        body = f")]}}'\n\n{len(frame)}\n{frame}\n".encode()
        req = {
            "direction": "response",
            "content_type": "text/plain",
            "meta": {
                "host": "gemini.google.com",
                "path": "/_/BardChatUi/data/batchexecute?rpcids=o30O0e&rt=c",
                "cookie": "__Secure-1PSID=psid-device-1",
                "client_ip": "10.0.0.7",
            },
        }
        fake_lookup = mock.Mock()
        with mock.patch.object(gemini_engine, "_account_lookup", fake_lookup):
            self.assertEqual(gemini_engine.extract(req, body), "")
        fake_lookup.set_account.assert_called_once_with(
            "gemini",
            "psid-device-1",
            gaia_id,
            "user@example.com",
            client_ip="10.0.0.7",
            display_name="",
        )
        self.assertFalse(req["_extract_final"])

    def test_gemini_account_rpc_rejects_email_from_other_rpc(self) -> None:
        gaia_id = "123456789012345678901"
        flags = [None, 7, True, None, None, None, None, None, gaia_id]
        profile = [gaia_id, None, None, None, None, None, None, None, None]
        profile.append([[flags, "prompt@example.com"]])
        inner = [[["me", 1, profile]]]
        frame = json.dumps(
            [["wrb.fr", "notAccount", json.dumps(inner), None, None, None, "generic"]],
            separators=(",", ":"),
        )
        body = f")]}}'\n\n{len(frame)}\n{frame}\n".encode()
        req = {
            "direction": "response",
            "content_type": "text/plain",
            "meta": {
                "host": "gemini.google.com",
                "path": "/_/BardChatUi/data/batchexecute?rpcids=notAccount&rt=c",
                "cookie": "__Secure-1PSID=psid-device-1",
            },
        }
        fake_lookup = mock.Mock()
        with mock.patch.object(gemini_engine, "_account_lookup", fake_lookup):
            gemini_engine.extract(req, body)
        fake_lookup.set_account.assert_not_called()

    def test_perplexity_ask_uses_cached_session_identity(self) -> None:
        payload = json.dumps({"query_str": "날씨 어때"}).encode()
        req = {
            "direction": "request",
            "content_type": "application/json",
            "meta": {
                "host": "www.perplexity.ai",
                "path": "/rest/sse/perplexity_ask",
                "cookie": "pplx.visitor-id=visitor-device-1",
            },
        }
        with mock.patch.object(perplexity_engine, "_account_lookup", None):
            built = perplexity_engine.build_policy_api_payload(req, payload, "날씨 어때")
        self.assertEqual(built["user_id"], "")
        self.assertEqual(built["email"], "")

        with mock.patch.object(
            perplexity_engine,
            "_lookup_perplexity_account",
            return_value=("pplx-user-1", "user@example.com"),
        ):
            built = perplexity_engine.build_policy_api_payload(req, payload, "날씨 어때")
        self.assertEqual(built["user_id"], "pplx-user-1")
        self.assertEqual(built["email"], "user@example.com")

    def test_perplexity_graphql_sidebar_is_not_an_ask(self) -> None:
        self.assertTrue(
            perplexity_engine.is_perplexity_ask_path("/rest/sse/perplexity_ask")
        )
        self.assertFalse(
            perplexity_engine.is_perplexity_ask_path(
                "/rest/perplexity_ask/graphql"
            )
        )
        self.assertFalse(
            perplexity_engine.perplexity_should_skip_policy(
                {
                    "direction": "request",
                    "meta": {
                        "host": "www.perplexity.ai",
                        "path": "/rest/sse/perplexity_ask",
                    },
                }
            )
        )
        self.assertTrue(
            perplexity_engine.perplexity_should_skip_policy(
                {
                    "direction": "request",
                    "meta": {
                        "host": "www.perplexity.ai",
                        "path": "/rest/perplexity_ask/graphql",
                    },
                }
            )
        )

    def test_perplexity_block_rewrite_uses_gpt_gemini_instruction(self) -> None:
        req = {
            "direction": "request",
            "unit_type": "json_body",
            "content_type": "application/json",
            "meta": {
                "host": "www.perplexity.ai",
                "path": "/rest/sse/perplexity_ask",
            },
        }
        payload = json.dumps({"query_str": "127.0.0.1"}, ensure_ascii=False).encode("utf-8")
        out = perplexity_engine.rewrite_prompt_for_block(
            req,
            payload,
            "입력 프롬프트\n[AIVAX MASKING]\n\n### [AIVAX] 프롬프트 차단",
            ai_policy_engine.PERPLEXITY_BLOCK_MODEL_INSTRUCTION,
        )
        self.assertIsNotNone(out)
        encoded, reason = out or (b"", "")
        self.assertEqual(reason, "adapter:perplexity_policy_block_sysinstruct")
        body = json.loads(encoded.decode("utf-8"))
        query = str(body.get("query_str") or "")
        self.assertTrue(query.startswith("아래 ### [AIVAX]"))
        self.assertIn("### [AIVAX] 프롬프트 차단", query)
        self.assertNotIn("[AIVAX 정책 응답 처리]", query)
        self.assertIn("### [AIVAX] 프롬프트 차단부터 주의까지", query)
        self.assertLess(query.find("한 번만 출력"), query.find("입력 프롬프트"))
        self.assertLess(query.find("입력 프롬프트"), query.find("\n### [AIVAX] 프롬프트 차단"))
        self.assertNotIn("127.0.0.1", query)

    def test_perplexity_collect_consent_session_ids_includes_backend_uuid(self) -> None:
        req = {
            "direction": "request",
            "unit_type": "json_body",
            "content_type": "application/json",
            "meta": {
                "host": "www.perplexity.ai",
                "path": "/rest/sse/perplexity_ask",
            },
        }
        payload = json.dumps(
            {
                "query_str": "동의",
                "frontend_context_uuid": "front-2",
                "params": {
                    "frontend_context_uuid": "front-2",
                    "last_backend_uuid": "back-1",
                    "backend_uuid": "back-1",
                    "thread_url_slug": "back-1",
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        ids = perplexity_engine.collect_consent_session_ids(req, payload)
        self.assertEqual(ids[0], "front-2")
        self.assertIn("back-1", ids)

    def test_perplexity_visitor_remembers_prior_thread_id(self) -> None:
        perplexity_engine._PPLX_VISITOR_THREAD_IDS.clear()
        cookie = "pplx.visitor-id=vis-remember-1"
        req = {
            "direction": "request",
            "unit_type": "json_body",
            "content_type": "application/json",
            "meta": {
                "host": "www.perplexity.ai",
                "path": "/rest/sse/perplexity_ask",
                "cookie": cookie,
            },
        }
        first = json.dumps(
            {"query_str": "127.0.0.1", "frontend_context_uuid": "front-1"},
            ensure_ascii=False,
        ).encode("utf-8")
        ids1 = perplexity_engine.collect_consent_session_ids(req, first)
        self.assertIn("front-1", ids1)
        second = json.dumps(
            {"query_str": "동의", "frontend_context_uuid": "front-2"},
            ensure_ascii=False,
        ).encode("utf-8")
        ids2 = perplexity_engine.collect_consent_session_ids(req, second)
        self.assertIn("front-2", ids2)
        self.assertIn("front-1", ids2)
        perplexity_engine._PPLX_VISITOR_THREAD_IDS.clear()

    def test_perplexity_consent_rewrite_restores_original_query(self) -> None:
        req = {
            "direction": "request",
            "unit_type": "json_body",
            "content_type": "application/json",
            "meta": {
                "host": "www.perplexity.ai",
                "path": "/rest/sse/perplexity_ask",
            },
        }
        payload = json.dumps({"query_str": "동의"}, ensure_ascii=False).encode("utf-8")
        out = perplexity_engine.rewrite_prompt_for_consent(req, payload, "127.0.0.1")
        self.assertIsNotNone(out)
        encoded, reason = out or (b"", "")
        self.assertEqual(reason, "adapter:perplexity_policy_consent")
        body = json.loads(encoded.decode("utf-8"))
        self.assertEqual(body.get("query_str"), "127.0.0.1")

    def test_grok_chat_uses_cached_profile_not_xuser(self) -> None:
        payload = json.dumps({"message": "hello grok"}).encode()
        req = {
            "direction": "request",
            "content_type": "application/json",
            "unit_type": "json_body",
            "meta": {
                "host": "grok.com",
                "path": "/rest/app-chat/conversations/abc/responses",
                "cookie": "x-userid=grok-user-1",
            },
        }
        with mock.patch.object(grok_engine, "_lookup_grok_account", return_value=None):
            built = grok_engine.build_policy_api_payload(req, payload, "hello grok")
        self.assertEqual(built["user_id"], "")
        self.assertEqual(built["email"], "")

        with mock.patch.object(
            grok_engine,
            "_lookup_grok_account",
            return_value=("grok-user-1", "user@example.com"),
        ):
            built = grok_engine.build_policy_api_payload(req, payload, "hello grok")
        self.assertEqual(built["user_id"], "grok-user-1")
        self.assertEqual(built["email"], "user@example.com")

    def test_chatgpt_oai_did_looks_up_jwt_identity(self) -> None:
        payload = json.dumps(
            {
                "messages": [
                    {
                        "author": {"role": "user"},
                        "content": {"content_type": "text", "parts": ["hi"]},
                    }
                ]
            }
        ).encode()
        req = {
            "direction": "request",
            "content_type": "application/json",
            "unit_type": "json_body",
            "meta": {
                "host": "chatgpt.com",
                "path": "/backend-api/f/conversation",
                "cookie": "oai-did=did-device-1",
            },
        }
        fake_lookup = mock.Mock()
        fake_lookup.get.return_value = None
        with mock.patch.object(chatgpt_engine, "_account_lookup", fake_lookup):
            built = chatgpt_engine.build_policy_api_payload(req, payload, "hi")
        self.assertEqual(built["user_id"], "")
        self.assertEqual(built["email"], "")

        fake_lookup.get.return_value = ("user-1", "user@example.com")
        with mock.patch.object(chatgpt_engine, "_account_lookup", fake_lookup):
            built = chatgpt_engine.build_policy_api_payload(req, payload, "hi")
        self.assertEqual(built["user_id"], "user-1")
        self.assertEqual(built["email"], "user@example.com")


class TestAdapterGarbageGuards(unittest.TestCase):
    def test_gemini_response_never_returns_raw_http(self) -> None:
        req = {
            "direction": "response",
            "unit_type": "json_body",
            "content_type": "text/plain",
            "meta": {"host": "gemini.google.com", "path": "/not-a-model-response"},
        }
        payload = b"HTTP/1.1 200 OK\r\nSet-Cookie: SID=x\r\n\r\n"
        self.assertFalse(gemini_engine.extract(req, payload))

    def test_gemini_rpc_form_is_not_a_prompt(self) -> None:
        inner = quote_plus(json.dumps([[["VxUbXb", "[]", None, "generic"]]]))
        payload = f"f.req={inner}&at=ADR5zaSecretValue".encode()
        req = {
            "direction": "request",
            "unit_type": "form_body",
            "content_type": "application/x-www-form-urlencoded",
            "meta": {
                "host": "gemini.google.com",
                "path": "/_/BardChatUi/data/batchexecute?rpcids=VxUbXb",
            },
        }
        self.assertFalse(gemini_engine.extract(req, payload))

    def test_gemini_process_session_prompt_and_response(self) -> None:
        import base64

        def varint(number: int) -> bytes:
            out = bytearray()
            while True:
                byte = number & 0x7F
                number >>= 7
                if number:
                    out.append(byte | 0x80)
                else:
                    out.append(byte)
                    return bytes(out)

        def ld(field: int, data: bytes) -> bytes:
            return varint((field << 3) | 2) + varint(len(data)) + data

        prompt_pb = bytes.fromhex("121208014a0e0a0c48656c6c6f2047656d696e69")
        self.assertEqual(gemini_engine._parse_process_session_prompt(prompt_pb), "Hello Gemini")
        # File turns insert another field between the marker and the text box.
        inner = varint((1 << 3) | 0) + varint(1) + ld(3, b"file-token") + ld(9, ld(1, "엔디안".encode()))
        self.assertEqual(gemini_engine._parse_process_session_prompt(ld(2, inner)), "엔디안")
        # A file turn can batch context and prompt as req0/req1 in one POST.
        batched_req = {
            "direction": "request",
            "request_id": "batched-1",
            "meta": {
                "host": "geminiweb-pa.clients6.google.com",
                "path": "/v1/processSession?gsessionid=batched&RID=3",
            },
        }
        ctx_pb = ld(1, ld(8, ld(5, ld(1, ld(72, ld(1, b"c_40baf96683717ca1"))))))
        batched = (
            "count=2&ofs=0"
            + "&req0___data__=" + quote_plus(base64.b64encode(ctx_pb).decode())
            + "&req1___data__=" + quote_plus(base64.b64encode(ld(2, inner)).decode())
        ).encode()
        self.assertEqual(gemini_engine.extract(batched_req, batched), "엔디안")
        # Prompt box nested one level down inside a wrapper frame.
        self.assertEqual(
            gemini_engine._find_nested_process_session_prompt(ld(7, ld(2, inner))), "엔디안"
        )
        # Ack post and a context post are not user prompts.
        self.assertEqual(gemini_engine._parse_process_session_prompt(bytes.fromhex("22020801")), "")
        context = ld(1, ld(8, ld(5, ld(1, ld(72, ld(1, b"c_40baf96683717ca1"))))))
        self.assertEqual(gemini_engine._parse_process_session_prompt(context), "")
        self.assertEqual(
            gemini_engine._conversation_id_from_pb(context), "c_40baf96683717ca1"
        )

        gsession = "gsession-login-1"
        gemini_engine._GEMINI_GSESSION_CONV.clear()
        context_req = {
            "direction": "request",
            "request_id": "ctx-1",
            "unit_type": "form_body",
            "content_type": "application/x-www-form-urlencoded",
            "meta": {
                "host": "geminiweb-pa.clients6.google.com",
                "path": f"/v1/processSession?gsessionid={gsession}&RID=1",
            },
        }
        context_body = (
            "count=1&ofs=0&req0___data__=" + base64.b64encode(context).decode()
        ).encode()
        self.assertEqual(gemini_engine.extract(context_req, context_body), "")

        prompt_req = {
            "direction": "request",
            "request_id": "prompt-1",
            "unit_type": "form_body",
            "content_type": "application/x-www-form-urlencoded",
            "meta": {
                "host": "geminiweb-pa.clients6.google.com",
                "path": f"/v1/processSession?gsessionid={gsession}&RID=2",
            },
        }
        prompt_body = (
            "count=1&ofs=1&req0___data__=" + base64.b64encode(prompt_pb).decode()
        ).encode()
        self.assertEqual(gemini_engine.extract(prompt_req, prompt_body), "Hello Gemini")
        self.assertEqual(prompt_req.get("_gemini_session_id"), "c_40baf96683717ca1")
        self.assertEqual(
            ai_policy_engine.PolicyEngine._resolve_adapter_service_id(prompt_req, "gpt"),
            "gemini",
        )
        self.assertTrue(
            ai_policy_engine._is_conversation_path("gemini", "/v1/processSession")
        )
        self.assertTrue(
            ai_policy_engine._is_conversation_path(
                "gemini",
                "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate",
            )
        )
        self.assertFalse(
            ai_policy_engine._is_conversation_path(
                "gemini", "/_/BardChatUi/data/batchexecute"
            )
        )
        self.assertEqual(
            ai_policy_engine.PolicyEngine._resolve_adapter_service_id(
                {
                    "meta": {
                        "host": "ogads-pa.clients6.google.com",
                        "path": "/$rpc/google.internal.onegoogle.asyncdata.v1.AsyncDataService/GetAsyncData",
                    }
                },
                "gpt",
            ),
            "gpt",
        )

        answer = "안녕하세요! 반갑습니다."
        ids = ld(1, b"c_40baf96683717ca1") + ld(2, b"r_a796802ef84494f0")
        rc = ld(1, b"rc_59a95422819e3164") + ld(2, ld(1, answer.encode()))
        box = ld(2, ids) + ld(5, rc) + ld(6, ld(1, "대한민국".encode()))
        box += ld(45, ld(1, ld(1, ld(2, "다시 시도".encode()))))
        # Field 50 forces a '+' into the standard base64 alphabet.
        response_pb = ld(2, ld(7, ld(1, ld(4, ld(4, box))))) + ld(50, bytes([62]))
        encoded = base64.b64encode(response_pb).decode()
        # Live browserchannel blobs contain '+'. Treating '+' as a space drops the answer.
        self.assertIn("+", encoded)
        self.assertEqual(gemini_engine._b64_decode_loose(encoded), response_pb)
        record = json.dumps([[4, [encoded]]], separators=(",", ":"))
        response_body = f"{len(record)}\n{record}".encode()
        response_req = {
            "direction": "response",
            "request_id": "resp-1",
            "unit_type": "json_body",
            "content_type": "text/plain",
            "_eve_full_body": True,
            "meta": {
                "host": "geminiweb-pa.clients6.google.com",
                "path": f"/v1/processSession?gsessionid={gsession}&TYPE=xmlhttp",
            },
        }
        self.assertEqual(gemini_engine.extract_full_body(response_req, response_body), answer)
        self.assertEqual(response_req.get("_gemini_output_session_id"), "c_40baf96683717ca1")
        self.assertEqual(response_req.get("_gemini_output_message_id"), "r_a796802ef84494f0")
        self.assertTrue(response_req.get("_gemini_response_growing"))
        self.assertFalse(response_req.get("_extract_final"))
        self.assertTrue(gemini_engine.response_stream_done(response_req, response_body))

        rewritten, reason = gemini_engine.rewrite_prompt_for_block(
            prompt_req, prompt_body, "[AIVAX] 차단", "지시문", "Hello Gemini"
        )
        self.assertIn("sysinstruct", reason)
        again = gemini_engine.extract(prompt_req, rewritten)
        self.assertIn("[AIVAX] 차단", again)
        self.assertIn("지시문", again)
        self.assertNotIn("Hello Gemini", again)

    def test_google_ai_mode_folif_prompt_and_answer(self) -> None:
        path = (
            "/async/folif?srtst=REDACTED&ei=E-fUao3IKeS-rfcPvZvmiQI"
            "&udm=50&q=What+the+hell...&sclient=aim-smartcompose"
        )
        prompt_req = {
            "direction": "request",
            "request_id": "ai-mode-prompt",
            "meta": {"host": "www.google.com", "path": path},
        }
        self.assertEqual(gemini_engine.extract(prompt_req, b""), "What the hell...")
        self.assertEqual(
            gemini_engine.extract_session_id(prompt_req, b""),
            "E-fUao3IKeS-rfcPvZvmiQI",
        )
        self.assertNotIn("REDACTED", gemini_engine.extract(prompt_req, b""))

        html_body = (
            ")]}'\n\n"
            "85\n"
            "<div><script>token</script><p>What the hell...</p>"
            "<p>It is a logged answer.</p></div>\n"
        ).encode()
        response_req = {
            "direction": "response",
            "request_id": "ai-mode-answer",
            "meta": {"host": "www.google.com", "path": path},
        }
        answer = gemini_engine.extract_full_body(response_req, html_body)
        self.assertIn("It is a logged answer.", answer)
        self.assertNotIn("token", answer)
        self.assertNotIn("srtst", answer)
        self.assertEqual(response_req.get("_gemini_output_session_id"), "E-fUao3IKeS-rfcPvZvmiQI")
        self.assertTrue(response_req.get("_extract_final"))

        chrome_body = (
            "<style>.n6owBd{overflow-wrap:break-word}</style>"
            '<h3 data-ignore-copy>genspark?에 대한 AI 모드 대답</h3>'
            "<h1>공유됨</h1><div>파일 0개</div>"
            '<div class="n6owBd"><span data-subtree="aimfl">맞아요!</span> 기능이 많습니다.'
            '<span class="notranslate"><button data-amic="true">블로그 +1</button></span></div>'
            '<div role="heading">1. 통합 AI 검색</div>'
            "<ul><li>여러 모델을 동시에 씁니다.</li></ul>"
            '<div>AI 대답에는 오류가 있을 수 있습니다. '
            '<a href="https://support.google.com/websearch?p=aimode">자세히 알아보기</a></div>'
            "<div>공개 링크 공유</div><div>Facebook</div><div>만족스러운 대답</div>"
        ).encode()
        self.assertEqual(
            gemini_engine.google_ai_mode_answer(chrome_body),
            "맞아요! 기능이 많습니다.\n"
            "1. 통합 AI 검색\n"
            "여러 모델을 동시에 씁니다.\n"
            "AI 대답에는 오류가 있을 수 있습니다. 자세히 알아보기",
        )

        shell = {
            "direction": "response",
            "meta": {"host": "www.google.com", "path": "/search?q=cats&udm=50"},
        }
        self.assertEqual(gemini_engine.extract(shell, b"<html><body>Google</body></html>"), "")

        ordinary = {
            "direction": "request",
            "meta": {"host": "www.google.com", "path": "/search?q=cats"},
        }
        self.assertIsNone(gemini_engine.extract(ordinary, b""))

        shell_policy = gemini_engine.build_policy_api_payload(
            {
                "direction": "request",
                "meta": {"host": "www.google.com", "path": "/search?q=cats&udm=50&ei=abc"},
            },
            b"",
            "cats",
        )
        self.assertNotIn("input_filter", shell_policy["filter_list"])
        self.assertIn("secret_filter", shell_policy["filter_list"])
        answer_policy = gemini_engine.build_policy_api_payload(
            {
                "direction": "request",
                "meta": {
                    "host": "www.google.com",
                    "path": "/async/folif?q=cats&udm=50&ei=abc",
                },
            },
            b"",
            "cats",
        )
        self.assertIn("input_filter", answer_policy["filter_list"])

        notice, reason = gemini_engine.rewrite_response(
            response_req, html_body, "차단 안내"
        )
        self.assertEqual(reason, "adapter:google_ai_mode_notice")
        self.assertIn("차단 안내", notice.decode())
        self.assertNotIn("logged answer", notice.decode())

        self.assertEqual(
            ai_policy_engine._host_to_service_id("www.google.com", path),
            "gemini",
        )
        self.assertEqual(
            ai_policy_engine._host_to_service_id("www.google.com", "/search?q=cats"),
            "",
        )
        self.assertTrue(ai_policy_engine._is_conversation_path("gemini", path))
        self.assertFalse(
            ai_policy_engine._is_conversation_path("gemini", "/async/folsrch?q=cats")
        )

    def test_private_process_session_answer_joins_the_prompt_post(self) -> None:
        """Logged-out Edge sends the prompt on one POST and the answer on the long-poll."""
        import base64

        gsession = "private-gsession"
        gemini_engine._GEMINI_GSESSION_PROMPT.clear()
        gemini_engine._GEMINI_GSESSION_CONV.clear()
        gemini_engine._GEMINI_SESSION_PARENT.clear()
        prompt_req = {
            "direction": "request",
            "request_id": "prompt-private",
            "meta": {
                "host": "geminiweb-pa.clients6.google.com",
                "path": f"/v1/processSession?gsessionid={gsession}&RID=42733",
            },
        }
        prompt_body = (
            b"count=1&ofs=1&req0___data__="
            b"EiQIAUogCh5bRWRnZV0gUHJpdmF0ZSBGbGFzaCBMaXRlIFRlc3Q%3D"
        )
        self.assertEqual(
            gemini_engine.extract(prompt_req, prompt_body),
            "[Edge] Private Flash Lite Test",
        )
        self.assertFalse(prompt_req.get("_gemini_session_id"))

        answer = "Test received successfully!"

        def varint(number: int) -> bytes:
            out = bytearray()
            while True:
                byte = number & 0x7F
                number >>= 7
                if number:
                    out.append(byte | 0x80)
                else:
                    out.append(byte)
                    return bytes(out)

        def ld(field: int, data: bytes) -> bytes:
            return varint((field << 3) | 2) + varint(len(data)) + data

        ids = ld(1, b"c_2f7ab34d75fdf4ea") + ld(2, b"r_a796802ef84494f0")
        rc = ld(1, b"rc_59a95422819e3164") + ld(2, ld(1, answer.encode()))
        box = ld(2, ids) + ld(5, rc)
        response_pb = ld(2, ld(7, ld(1, ld(4, ld(4, box)))))
        record = json.dumps(
            [[4, [base64.b64encode(response_pb).decode()]]],
            separators=(",", ":"),
        )
        response_req = {
            "direction": "response",
            "request_id": "long-poll",
            "meta": {
                "host": "geminiweb-pa.clients6.google.com",
                "path": f"/v1/processSession?gsessionid={gsession}&RID=rpc&TYPE=xmlhttp",
            },
        }
        self.assertEqual(
            gemini_engine.extract(response_req, f"{len(record)}\n{record}".encode()),
            answer,
        )
        self.assertEqual(response_req.get("_gemini_ui_message_id"), "prompt-private")

    def test_gemini_streamgenerate_extracts_model(self) -> None:
        inner_data = [None] * 45
        inner_data[1] = ["c_22dc0c2e058fc1a1", "r_763dde1953ceca0a"]
        inner_data[4] = [["rc_59a95422819e3164", ["안녕하세요!"]]]
        inner_data[42] = "3.6 Flash"
        frame_obj = [["wrb.fr", None, json.dumps(inner_data)]]
        frame_str = json.dumps(frame_obj)
        payload = f")]}}'\n\n{len(frame_str)}\n{frame_str}\n".encode("utf-8")
        req = {
            "direction": "response",
            "request_id": "req-gemini-1",
            "meta": {
                "host": "gemini.google.com",
                "path": "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate",
            },
        }
        text = gemini_engine.extract(req, payload)
        self.assertEqual(text, "안녕하세요!")
        self.assertEqual(gemini_engine.extract_model(req, payload), "3.6 Flash")
        self.assertEqual(req.get("_gemini_model"), "3.6 Flash")

        pol_req = {}
        ai_policy_engine.stamp_policy_model(req, payload, pol_req, gemini_engine)
        self.assertEqual(pol_req.get("model"), "3.6 Flash")

    def test_gemini_model_raw_scan_and_session_fallback(self) -> None:
        gemini_engine._GEMINI_SESSION_MODEL.clear()
        raw = b'not-json null,null,\\"3.1 Pro\\",true tail'
        req = {"_gemini_output_session_id": "c_followup_model"}
        self.assertEqual(gemini_engine.extract_model(req, raw), "3.1 Pro")
        follow = {"_gemini_session_id": "c_followup_model"}
        self.assertEqual(gemini_engine.extract_model(follow, b"no model in this body"), "3.1 Pro")
        self.assertEqual(follow.get("_gemini_model"), "3.1 Pro")

    def test_gemini_thinking_model_display_name_is_kept(self) -> None:
        gemini_engine._GEMINI_SESSION_MODEL.clear()
        inner_data = [None] * 45
        inner_data[1] = ["c_thinking_model", "r_1"]
        inner_data[4] = [["rc_1", ["받아라"]]]
        inner_data[42] = "3.8 사고 모델"
        frame_obj = [["wrb.fr", None, json.dumps(inner_data, ensure_ascii=False)]]
        frame_str = json.dumps(frame_obj, ensure_ascii=False)
        payload = f")]}}'\n\n{len(frame_str)}\n{frame_str}\n".encode("utf-8")
        req = {
            "direction": "response",
            "request_id": "req-gemini-thinking",
            "meta": {
                "host": "gemini.google.com",
                "path": "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate",
            },
        }
        self.assertEqual(gemini_engine.extract(req, payload), "받아라")
        self.assertEqual(gemini_engine.extract_model(req, payload), "3.8 사고 모델")
        pol_req = {}
        ai_policy_engine.stamp_policy_model(req, payload, pol_req, gemini_engine)
        self.assertEqual(pol_req.get("model"), "3.8 사고 모델")
        self.assertEqual(ai_policy_engine._adapter_model_name("3.6 사고 모델"), "3.6 사고 모델")

    def test_gemini_model_slot_is_stored_as_sent(self) -> None:
        gemini_engine._GEMINI_SESSION_MODEL.clear()
        inner_data = [None] * 45
        inner_data[1] = ["c_new_model", "r_1"]
        inner_data[4] = [["rc_1", ["안녕"]]]
        inner_data[10] = "Professional - Other"
        inner_data[42] = "4.0 Nano"
        frame_obj = [["wrb.fr", None, json.dumps(inner_data, ensure_ascii=False)]]
        frame_str = json.dumps(frame_obj, ensure_ascii=False)
        payload = f")]}}'\n\n{len(frame_str)}\n{frame_str}\n".encode("utf-8")
        req = {
            "direction": "response",
            "meta": {
                "host": "gemini.google.com",
                "path": "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate",
            },
        }
        self.assertEqual(gemini_engine.extract_model(req, payload), "4.0 Nano")
        pol_req = {}
        ai_policy_engine.stamp_policy_model(req, payload, pol_req, gemini_engine)
        self.assertEqual(pol_req.get("model"), "4.0 Nano")

    def test_gemini_process_session_extracts_model(self) -> None:
        import base64
        import gzip

        gemini_engine._GEMINI_SESSION_MODEL.clear()

        def varint(number: int) -> bytes:
            return gemini_engine._encode_pb_varint(number)

        def ld(field: int, data: bytes) -> bytes:
            return gemini_engine._encode_pb_ld(field, data)

        def flag(field: int, value: int) -> bytes:
            return varint((field << 3) | 0) + varint(value)

        def browserchannel(blobs: list) -> bytes:
            records = []
            for blob in blobs:
                encoded = base64.b64encode(blob).decode("ascii")
                record = json.dumps([[4, [encoded]]], separators=(",", ":"))
                records.append(f"{len(record)}\n{record}")
            return "".join(records).encode()

        def candidate(
            answer: str,
            model: str,
            *,
            session: bytes = b"c_5b8c37f3ace3290c",
            marked: bool = True,
            mark: int = 1,
        ) -> bytes:
            ids = ld(1, session) + ld(2, b"r_a796802ef84494f0")
            rc = ld(1, b"rc_59a95422819e3164") + ld(2, ld(1, answer.encode()))
            box = ld(2, ids) + ld(5, rc) + ld(6, ld(1, "대한민국".encode()))
            box += ld(43, model.encode())
            if marked:
                box += flag(44, mark)
            box += ld(45, ld(1, ld(1, ld(2, "다시 시도".encode()))))
            # Field 50 forces '+' into standard base64, as live blobs do.
            return ld(2, ld(7, ld(1, ld(4, ld(4, box))))) + ld(50, bytes([62]))

        answer = "제공해주신 자료의 내용을 바탕으로 첫 페이지 제목을 확인해 드립니다."
        payload = browserchannel([candidate(answer, "3.6 Flash")])
        self.assertIn("+", base64.b64encode(candidate(answer, "3.6 Flash")).decode())
        req = {
            "direction": "response",
            "request_id": "long-poll-model",
            "meta": {
                "host": "geminiweb-pa.clients6.google.com",
                "path": "/v1/processSession?gsessionid=gs-model&TYPE=xmlhttp",
            },
        }
        self.assertEqual(gemini_engine.extract(req, payload), answer)
        self.assertEqual(req.get("_gemini_output_session_id"), "c_5b8c37f3ace3290c")
        self.assertEqual(req.get("_gemini_model"), "3.6 Flash")
        self.assertNotIn("다시 시도", gemini_engine.extract_model(req, payload))
        self.assertEqual(gemini_engine.extract_model(req, payload), "3.6 Flash")
        built = gemini_engine.build_policy_api_payload(req, payload, answer)
        self.assertEqual(built.get("model"), "3.6 Flash")
        pol_req = {}
        ai_policy_engine.stamp_policy_model(req, payload, pol_req, gemini_engine)
        self.assertEqual(pol_req.get("model"), "3.6 Flash")

        thinking = {
            "direction": "response",
            "meta": {
                "host": "geminiweb-pa.clients6.google.com",
                "path": "/v1/processSession?gsessionid=gs-think&TYPE=xmlhttp",
            },
        }
        thinking_body = browserchannel(
            [candidate("받아라", "3.8 사고 모델", session=b"c_aaaaaaaaaaaaaaaa")]
        )
        self.assertEqual(gemini_engine.extract(thinking, thinking_body), "받아라")
        self.assertEqual(thinking.get("_gemini_model"), "3.8 사고 모델")
        thinking_policy = {}
        ai_policy_engine.stamp_policy_model(thinking, thinking_body, thinking_policy, gemini_engine)
        self.assertEqual(thinking_policy.get("model"), "3.8 사고 모델")

        # The true marker is what makes field 43 the model slot.
        unmarked = browserchannel([candidate(answer, "3.6 Flash", marked=False)])
        bare = {"direction": "response", "_gemini_output_session_id": "c_unmarked"}
        self.assertEqual(gemini_engine.extract_model(bare, unmarked), "")
        zero = browserchannel([candidate(answer, "3.6 Flash", mark=0)])
        self.assertEqual(
            gemini_engine.extract_model(
                {"direction": "response", "_gemini_output_session_id": "c_zero"},
                zero,
            ),
            "",
        )

        # Same fields on a different message are not the answer candidate.
        decoy = ld(43, "Decoy Model".encode()) + flag(44, 1)
        nested = ld(1, decoy) + candidate(answer, "3.6 Flash")
        self.assertEqual(
            gemini_engine.extract_model(
                {"direction": "response"},
                browserchannel([nested]),
            ),
            "3.6 Flash",
        )
        inside = ld(43, "Location Name".encode()) + flag(44, 1)
        box_ids = ld(1, b"c_5b8c37f3ace3290c") + ld(2, b"r_a796802ef84494f0")
        box_rc = ld(1, b"rc_59a95422819e3164") + ld(2, ld(1, answer.encode()))
        sneaky = ld(2, box_ids) + ld(5, box_rc) + ld(6, inside)
        sneaky += ld(43, "3.6 Flash".encode()) + flag(44, 1)
        sneaky_pb = ld(2, ld(7, ld(1, ld(4, ld(4, sneaky)))))
        self.assertEqual(
            gemini_engine.extract_model({}, browserchannel([sneaky_pb])),
            "3.6 Flash",
        )

        # A later record replaces the name. A later record without a name keeps it.
        replaced = browserchannel(
            [
                candidate("짧은 답", "3.5 Flash-Lite"),
                candidate(answer, "3.6 Flash"),
            ]
        )
        self.assertEqual(gemini_engine.extract_model({}, replaced), "3.6 Flash")
        kept = browserchannel(
            [
                candidate("짧은 답", "3.5 Flash-Lite"),
                candidate(answer, "3.6 Flash", marked=False),
            ]
        )
        self.assertEqual(gemini_engine.extract_model({}, kept), "3.5 Flash-Lite")

        # The next long-poll can omit the name. The c_* session still has it.
        follow_ids = ld(1, b"c_5b8c37f3ace3290c") + ld(2, b"r_a796802ef84494f0")
        follow_rc = ld(1, b"rc_59a95422819e3164") + ld(2, ld(1, (answer + " 더 길어진 답변").encode()))
        follow_box = ld(2, follow_ids) + ld(5, follow_rc)
        follow_pb = ld(2, ld(7, ld(1, ld(4, ld(4, follow_box)))))
        follow_req = {
            "direction": "response",
            "meta": {
                "host": "geminiweb-pa.clients6.google.com",
                "path": "/v1/processSession?gsessionid=gs-model&TYPE=xmlhttp",
            },
        }
        follow_body = browserchannel([follow_pb])
        self.assertEqual(
            gemini_engine.extract(follow_req, follow_body),
            answer + " 더 길어진 답변",
        )
        self.assertEqual(follow_req.get("_gemini_model"), "3.6 Flash")
        follow_policy = {}
        ai_policy_engine.stamp_policy_model(follow_req, follow_body, follow_policy, gemini_engine)
        self.assertEqual(follow_policy.get("model"), "3.6 Flash")

        other = {"_gemini_output_session_id": "c_other_session"}
        self.assertEqual(gemini_engine.extract_model(other, follow_body), "")

        # The slot limit counts characters. Surrounding space is trimmed; an embedded control character is not a name.
        self.assertEqual(
            gemini_engine.extract_model({}, browserchannel([candidate("답", "가" * 40)])),
            "가" * 40,
        )
        self.assertEqual(
            gemini_engine.extract_model({}, browserchannel([candidate("답", "A" * 80)])),
            "A" * 80,
        )
        self.assertEqual(
            gemini_engine.extract_model({}, browserchannel([candidate("답", "가" * 81)])),
            "",
        )
        self.assertEqual(
            gemini_engine.extract_model({}, browserchannel([candidate("답", "A" * 81)])),
            "",
        )
        self.assertEqual(
            gemini_engine.extract_model({}, browserchannel([candidate("답", "  3.6 Flash  ")])),
            "3.6 Flash",
        )
        self.assertEqual(
            gemini_engine.extract_model({}, browserchannel([candidate("답", "3.6\x01Flash")])),
            "",
        )
        gemini_engine._GEMINI_SESSION_MODEL.clear()

        zipped = gzip.compress(payload)
        self.assertEqual(gemini_engine.extract_model({}, zipped), "3.6 Flash")
        self.assertEqual(gemini_engine.extract_model({}, b""), "")
        self.assertEqual(
            gemini_engine.extract_model(
                {},
                b"count=1&ofs=0&req0___data__=EiQIAUogCh5bRWRnZV0gUHJpdmF0ZSBGbGFzaCBMaXRlIFRlc3Q%3D",
            ),
            "",
        )

    def test_gemini_inline_full_body_keeps_model_after_name_drops(self) -> None:
        """Deep INLINE calls extract_full_body. A later snapshot omits the name."""
        import base64
        import json

        gemini_engine._GEMINI_SESSION_MODEL.clear()

        def varint(number: int) -> bytes:
            return gemini_engine._encode_pb_varint(number)

        def ld(field: int, data: bytes) -> bytes:
            return gemini_engine._encode_pb_ld(field, data)

        def flag(field: int, value: int) -> bytes:
            return varint((field << 3) | 0) + varint(value)

        def browserchannel(blobs: list) -> bytes:
            records = []
            for blob in blobs:
                encoded = base64.b64encode(blob).decode("ascii")
                record = json.dumps([[4, [encoded]]], separators=(",", ":"))
                records.append(f"{len(record)}\n{record}")
            return "".join(records).encode()

        def box(answer: str, model: str = "", session: bytes = b"c_1111111111111111") -> bytes:
            ids = ld(1, session) + ld(2, b"r_a796802ef84494f0")
            rc = ld(1, b"rc_59a95422819e3164") + ld(2, ld(1, answer.encode()))
            body = ld(2, ids) + ld(5, rc)
            if model:
                body += ld(43, model.encode()) + flag(44, 1)
            return ld(2, ld(7, ld(1, ld(4, ld(4, body)))))

        meta = {
            "host": "geminiweb-pa.clients6.google.com",
            "path": "/v1/processSession?gsessionid=gs-inline&TYPE=xmlhttp",
        }
        early = {
            "direction": "response",
            "request_id": "inline-early",
            "meta": meta,
        }
        early_body = browserchannel([box("짧은 답", "3.6 Flash")])
        self.assertEqual(gemini_engine.extract_full_body(early, early_body), "짧은 답")
        self.assertEqual(early.get("_gemini_model"), "3.6 Flash")

        later = {
            "direction": "response",
            "request_id": "inline-later",
            "meta": dict(meta),
        }
        later_answer = "짧은 답 그리고 길어진 본문"
        later_body = browserchannel([box(later_answer)])
        self.assertEqual(gemini_engine.extract_full_body(later, later_body), later_answer)
        self.assertEqual(later.get("_gemini_model"), "3.6 Flash")
        policy = {}
        ai_policy_engine.stamp_policy_model(later, later_body, policy, gemini_engine)
        self.assertEqual(policy.get("model"), "3.6 Flash")

        other = {
            "direction": "response",
            "request_id": "inline-other",
            "meta": dict(meta),
        }
        other_body = browserchannel([box("다른 대화", session=b"c_2222222222222222")])
        self.assertEqual(gemini_engine.extract_full_body(other, other_body), "다른 대화")
        self.assertEqual(other.get("_gemini_model", ""), "")

    def test_gemini_stream_form_without_f_req_is_not_a_prompt(self) -> None:
        req = {
            "direction": "request",
            "unit_type": "form_body",
            "content_type": "application/x-www-form-urlencoded",
            "meta": {
                "host": "gemini.google.com",
                "path": "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate",
            },
        }
        self.assertEqual(gemini_engine.extract(req, b"at=ADR5zaSecretValue"), "")

    def test_chatgpt_ces_path_is_skipped(self) -> None:
        req = {
            "direction": "request",
            "meta": {"path": "/ces/v1/t"},
        }
        self.assertTrue(chatgpt_engine.chatgpt_should_skip_policy(req, b"{}"))

    def test_chatgpt_partial_query_is_a_prompt(self) -> None:
        # Anonymous and prepare-dispatch turns ship the text without messages[].
        payload = json.dumps(
            {
                "action": "next",
                "conversation_id": "6a7e0000-0000-0000-0000-000000000000",
                "client_prepare_state": "primed",
                "partial_query": {
                    "id": "1b2c0000-0000-0000-0000-000000000000",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["연차 요건이 어떻게 돼?"]},
                },
            }
        ).encode("utf-8")
        req = {
            "direction": "request",
            "unit_type": "json_body",
            "content_type": "application/json",
            "meta": {"host": "chatgpt.com", "path": "/backend-anon/f/conversation"},
        }
        self.assertEqual(chatgpt_engine.extract(req, payload), "연차 요건이 어떻게 돼?")

    def test_chatgpt_image_gen_sse_extracts_asset_not_prompt_bytes(self) -> None:
        pointer = {
            "content_type": "image_asset_pointer",
            "asset_pointer": "sediment://file_00000000faa88209821580f838b6050e",
            "mime_type": "image/png",
            "width": 1536,
            "height": 1024,
        }
        body = "\n".join(
            [
                "event: delta_encoding",
                'data: "v1"',
                "",
                "data: "
                + json.dumps(
                    {
                        "v": {
                            "message": {
                                "id": "tool-1",
                                "author": {"role": "tool", "name": "image_gen"},
                                "content": {
                                    "content_type": "multimodal_text",
                                    "parts": [pointer],
                                },
                            }
                        }
                    },
                    ensure_ascii=False,
                ),
                "",
                'data: {"type":"message_stream_complete","conversation_id":"conv-1"}',
                "",
                "data: [DONE]",
                "",
            ]
        ).encode("utf-8")
        req = {
            "direction": "response",
            "unit_type": "sse_event",
            "content_type": "text/event-stream",
            "meta": {"host": "chatgpt.com", "path": "/backend-api/f/conversation"},
        }
        text = chatgpt_engine.extract_full_body(req, body) or ""
        self.assertIn("[이미지 생성] file_00000000faa88209821580f838b6050e image/png 1536x1024", text)
        self.assertNotIn("블랙핑크", text)

    def test_chatgpt_generated_image_is_saved_from_the_session_download(self) -> None:
        import tempfile

        chatgpt_engine.clear_generated_images_for_tests()
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        thumb = (
            "/backend-api/estuary/content?id=file_00000000faa88209821580f838b6050e"
            "%23thumbnail"
        )
        full = (
            "/backend-api/estuary/content?id=file_00000000faa88209821580f838b6050e"
            "&ts=1&p=fsns&sig=secret"
        )
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(
                chatgpt_engine.capture_chatgpt_generated_image(thumb, png, directory)
            )
            self.assertTrue(
                chatgpt_engine.capture_chatgpt_generated_image(full, png, directory)
            )
            text = (
                "[이미지 생성] file_00000000faa88209821580f838b6050e image/png 1536x1024"
            )
            files = chatgpt_engine.generated_image_attachments(text)
        self.assertEqual(len(files), 1)
        self.assertEqual(
            files[0]["file_name"],
            "file_00000000faa88209821580f838b6050e.png",
        )
        self.assertTrue(files[0]["file_backup_path"].endswith(
            "file_00000000faa88209821580f838b6050e.png"
        ))
        self.assertNotIn("sig=secret", files[0]["file_backup_path"])

    def test_chatgpt_generated_pptx_is_linked_to_the_original_turn(self) -> None:
        import tempfile

        chatgpt_engine.clear_generated_images_for_tests()
        sandbox_path = "/workspace/scratch/job-1/output/GPT-6-Luna-High-소개.pptx"
        text = (
            "[PPT 다운로드]"
            "(sandbox:/workspace/scratch/job-1/output/GPT-6-Luna-High-%EC%86%8C%EA%B0%9C.pptx)"
        )
        self.assertEqual(
            chatgpt_engine.register_generated_artifact_links(text, "prompt-message-1"),
            [],
        )
        download = (
            "/backend-api/conversation/conv-1/interpreter/download"
            "?message_id=assistant-1"
            "&sandbox_path=%2Fworkspace%2Fscratch%2Fjob-1%2Foutput%2F"
            "GPT-6-Luna-High-%EC%86%8C%EA%B0%9C.pptx"
        )
        pptx = b"PK\x03\x04" + b"\x00" * 64
        with tempfile.TemporaryDirectory() as directory:
            record = chatgpt_engine.capture_chatgpt_generated_artifact(
                download,
                pptx,
                directory,
            )
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record["message_id"], "prompt-message-1")
            self.assertEqual(record["file_name"], "GPT-6-Luna-High-소개.pptx")
            self.assertTrue(Path(record["file_backup_path"]).is_file())
            self.assertNotIn("sandbox_path=", record["file_backup_path"])
            chatgpt_engine.note_chatgpt_artifact_download(download)
            raw = "/files/00000000-8968-820b-8f11-3231c75bdbdf/raw?sig=secret"
            redirected = chatgpt_engine.capture_chatgpt_generated_artifact(
                raw,
                pptx,
                directory,
            )
            self.assertIsNotNone(redirected)
            assert redirected is not None
            self.assertEqual(redirected["message_id"], "prompt-message-1")
            self.assertEqual(redirected["file_name"], "GPT-6-Luna-High-소개.pptx")

    def test_chatgpt_block_rewrite_replaces_partial_query(self) -> None:
        payload = json.dumps(
            {
                "action": "next",
                "conversation_id": "6a7e0000-0000-0000-0000-000000000000",
                "partial_query": {
                    "id": "1b2c0000-0000-0000-0000-000000000000",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["127.0.0.1"]},
                },
            }
        ).encode("utf-8")
        req = {
            "direction": "request",
            "unit_type": "json_body",
            "content_type": "application/json",
            "meta": {"host": "chatgpt.com", "path": "/backend-api/f/conversation"},
        }
        out = chatgpt_engine.rewrite_prompt_for_block(
            req, payload, "차단되었습니다.", "do not answer the original"
        )
        self.assertIsNotNone(out)
        body, reason = out
        self.assertIn("adapter:chatgpt_policy_block_sysinstruct", reason)
        obj = json.loads(body.decode("utf-8"))
        parts = (((obj.get("partial_query") or {}).get("content") or {}).get("parts") or [])
        text = "\n".join(str(p) for p in parts)
        self.assertIn("차단되었습니다.", text)
        self.assertNotIn("127.0.0.1", text)
        self.assertNotIn("[AIVAX 정책 응답 처리]", text)
        self.assertLess(text.find("차단되었습니다."), text.find("do not answer"))

    def test_chatgpt_consent_rewrite_replaces_동의(self) -> None:
        payload = json.dumps(
            {
                "action": "next",
                "conversation_id": "6aa771e9-fc50-83ee-b465-3d40ac0c7975",
                "messages": [
                    {
                        "author": {"role": "user"},
                        "content": {"content_type": "text", "parts": ["동의"]},
                    }
                ],
            }
        ).encode("utf-8")
        req = {
            "direction": "request",
            "unit_type": "json_body",
            "content_type": "application/json",
            "meta": {"host": "chatgpt.com", "path": "/backend-api/f/conversation"},
        }
        out = chatgpt_engine.rewrite_prompt(
            req, payload, "127.0.0.1", "CONSENT"
        )
        self.assertIsNotNone(out)
        body, reason = out
        self.assertEqual(reason, "adapter:chatgpt_policy_consent")
        obj = json.loads(body.decode("utf-8"))
        parts = (((obj.get("messages") or [{}])[0].get("content") or {}).get("parts") or [])
        self.assertEqual(parts, ["127.0.0.1"])

    def test_chatgpt_session_id_from_sse_and_ws_conversation_created(self) -> None:
        sse = (
            b'data: {"type":"message","conversation_id":"6aa7ba43-c220-83ee-b272-1f0d0b99e393"}\n\n'
            b'data: {"type":"stream_handoff","conversation_id":"6aa7ba43-c220-83ee-b272-1f0d0b99e393"}\n\n'
        )
        sse_req = {
            "direction": "response",
            "unit_type": "sse_event",
            "content_type": "text/event-stream",
            "request_id": "da63889d-512a-4290-ba75-e7b1215ddb0e",
            "meta": {"host": "chatgpt.com", "path": "/backend-api/f/conversation"},
        }
        self.assertEqual(
            chatgpt_engine.extract_session_id(sse_req, sse),
            "6aa7ba43-c220-83ee-b272-1f0d0b99e393",
        )
        inner = json.dumps(
            [
                {
                    "type": "message",
                    "topic_id": "conversations",
                    "payload": {
                        "type": "conversation-created",
                        "payload": {
                            "conversation_id": "6aa7ba43-c220-83ee-b272-1f0d0b99e393"
                        },
                        "metadata": None,
                    },
                }
            ],
            separators=(",", ":"),
        ).encode("utf-8")
        ws = bytes([0x81, 126]) + len(inner).to_bytes(2, "big") + inner
        ws_req = {
            "direction": "response",
            "unit_type": "ws_frame",
            "content_type": "application/octet-stream",
            "meta": {
                "host": "ws.chatgpt.com",
                "path": "/p24/ws/user/user-test",
            },
        }
        self.assertEqual(
            chatgpt_engine.extract_session_id(ws_req, ws),
            "6aa7ba43-c220-83ee-b272-1f0d0b99e393",
        )
        # Missing resume_parent_store must not abort full-body parse.
        chatgpt_engine.extract_full_body(sse_req, sse)

    def test_unauth_mweb_updates_form_prompt_only(self) -> None:
        # Lightweight anon turn: form-urlencoded `prompt` only — never tokens.
        from urllib.parse import urlencode

        payload = urlencode(
            {
                "lightweight_authenticated": "0",
                "operationId": "8696e853-fa35-4a3d-8671-b3385936cc4d",
                "conversationState": '{"messages":[],"parentMessageId":"client-created-root"}',
                "prompt": "Sam Altman What's wrong?",
                "chatRequirementsToken": "gAAAAA_token_should_not_appear",
                "proofToken": "gAAAAA_proof",
                "turnstileToken": "SBAc_turnstile",
                "userMessageId": "bbdbad93-4144-4414-8bdf-c193be8e351a",
                "oai-session-id": "fa7fbd78-731a-41b3-bae4-9a67e53dbf2d",
            }
        ).encode("utf-8")
        path = (
            "/unauth-mweb/conversation/updates"
            "?lightweight_authenticated=0&operationId=8696e853-fa35-4a3d-8671-b3385936cc4d"
        )
        req = {
            "direction": "request",
            "unit_type": "json_body",  # Envoy often tags non-upload as json_body
            "content_type": "application/x-www-form-urlencoded;charset=UTF-8",
            "meta": {"host": "chatgpt.com", "path": path},
        }
        self.assertTrue(chatgpt_engine.is_chatgpt_conversation_turn_path(path))
        self.assertFalse(chatgpt_engine.chatgpt_should_skip_policy(req, payload))
        self.assertEqual(chatgpt_engine.extract(req, payload), "Sam Altman What's wrong?")
        # Existing anon paths stay allowed.
        self.assertTrue(
            chatgpt_engine.is_chatgpt_conversation_turn_path("/backend-anon/f/conversation")
        )
        # prepare / runtime must not become turns (UI garbage).
        self.assertFalse(
            chatgpt_engine.is_chatgpt_conversation_turn_path(
                "/unauth-mweb/conversation/prepare?lightweight_authenticated=0"
            )
        )
        self.assertFalse(
            chatgpt_engine.is_chatgpt_conversation_turn_path("/unauth-mweb/conversation/runtime")
        )
        # Token-only form → skip (no prompt in UI).
        no_prompt = urlencode(
            {"operationId": "x", "chatRequirementsToken": "tok"}
        ).encode("utf-8")
        self.assertTrue(chatgpt_engine.chatgpt_should_skip_policy(req, no_prompt))
        file_only = urlencode(
            {
                "operationId": "x",
                "imageAttachments": '[{"id":"file-abc1234567","name":"a.png"}]',
            }
        ).encode("utf-8")
        self.assertFalse(chatgpt_engine.chatgpt_should_skip_policy(req, file_only))
        # Response body must not become UI text.
        resp_req = dict(req)
        resp_req["direction"] = "response"
        self.assertEqual(chatgpt_engine.extract(resp_req, b"<html>partial</html>"), "")
        # MASK rewrites only prompt=.
        rewritten = chatgpt_engine.rewrite_prompt(req, payload, "[masked]", "MASKING")
        self.assertIsNotNone(rewritten)
        new_body, reason = rewritten
        self.assertIn("adapter:chatgpt_policy_masking", reason)
        self.assertEqual(chatgpt_engine.extract(req, new_body), "[masked]")
        self.assertIn(b"chatRequirementsToken=gAAAAA_token_should_not_appear", new_body)
        # Policy API payload must carry prompt (not decline as non-JSON).
        pol = chatgpt_engine.build_policy_api_payload(req, payload, "Sam Altman What's wrong?")
        self.assertIsInstance(pol, dict)
        self.assertEqual(pol.get("prompt"), "Sam Altman What's wrong?")
        self.assertNotIn("gAAAAA_token_should_not_appear", str(pol))

    @staticmethod
    def _unauth_answer_req() -> Dict[str, Any]:
        path = "/unauth-mweb/conversation/updates?lightweight_authenticated=0"
        return {
            "request_id": "rid-1",
            "direction": "response",
            "unit_type": "json_body",
            "content_type": "text/vnd.openai.web-mobile-partial+html; charset=utf-8",
            "meta": {"host": "chatgpt.com", "path": path},
        }

    def test_unauth_mweb_rendered_assistant_role(self) -> None:
        # Completed logged-out turns ship the answer as HTML, not a snapshot.
        body = (
            '<template data-web-mobile-dpu-frame="12">'
            '<div data-message-role="user"><p>010-9472-1232</p></div>'
            '<div data-message-role="assistant"><p>Hello, I can help.</p>'
            "<ul><li>one</li><li>two</li></ul></div>"
            "</template>"
        ).encode("utf-8")
        req = self._unauth_answer_req()
        out = chatgpt_engine.extract_full_body(req, body)
        self.assertEqual(out, "Hello, I can help.\n- one\n- two")
        self.assertNotIn("010-9472-1232", out or "")
        self.assertTrue(req.get("_extract_final"))

    def test_unauth_mweb_single_quoted_conversation_snapshot(self) -> None:
        answer = "single quote answer"
        conversation = json.dumps(
            {
                "messages": [
                    {"content": "prompt", "role": "user"},
                    {"content": answer, "role": "assistant"},
                ]
            },
            ensure_ascii=False,
        )
        body = ("<span data-conversation='" + conversation + "'></span>").encode("utf-8")
        out = chatgpt_engine.extract_full_body(self._unauth_answer_req(), body)
        self.assertEqual(out, answer)

    def test_unauth_mweb_updates_answer_uses_final_snapshot(self) -> None:
        # The terminal frame of a completed lightweight anon answer carries the
        # whole conversation as JSON, so the assistant markdown is available
        # verbatim instead of rebuilt from render patches.
        answer = (
            "네! 🖤💗\n\n# 멤버\n\n- **Jisoo** — 2011년 합류\n- Rosé — 2012년 합류\n\n"
            "데뷔 2016년. ([YG LIFE](https://example.com/yg?utm_source=chatgpt.com))"
        )
        conversation = json.dumps(
            {
                "backendConversationId": "6a983c03-e5e4-83ea-a420-87d694b722be",
                "messages": [
                    {"content": "블랙핑크 멤버 알려줘", "role": "user"},
                    {"content": answer, "role": "assistant"},
                ],
            },
            ensure_ascii=False,
        )
        body = (
            '<template data-web-mobile-dpu-frame="751">'
            '<span data-conversation-control="resume-token" '
            'data-resume-token="eyJhbGciOiJFUzI1NiJ9.token_should_not_appear"></span>'
            "</template>"
            '<template data-web-mobile-dpu-frame="94627">'
            '<template for="conversation-partial-control" '
            'data-web-mobile-dpu-apply="append">'
            '<span data-conversation-control="complete" hidden data-conversation="'
            + html.escape(conversation, quote=True)
            + '"></span></template></template>'
        ).encode("utf-8")
        req = self._unauth_answer_req()
        self.assertTrue(
            chatgpt_engine.is_chatgpt_response_collect_path(req["meta"]["path"])
        )
        out = chatgpt_engine.extract_full_body(req, body)
        self.assertEqual(out, answer)
        self.assertTrue(req.get("_extract_final"))
        self.assertNotIn("token_should_not_appear", out or "")
        self.assertNotIn("블랙핑크 멤버 알려줘", out or "")

    def test_unauth_mweb_updates_answer_patch_replay_fallback(self) -> None:
        # Without the terminal snapshot (aborted turn) the patch stream is
        # replayed: only `-committed-tail` appends build the document, replaces
        # rewrite a block in place, and `-pending-tail` previews are dropped.
        pending_id = "assistant-pending-dd062e14"
        body = (
            f'<template for="{pending_id}-pending" data-web-mobile-dpu-apply="replace">'
            f'<?start name="{pending_id}-pending">'
            '<p data-assistant-stream-block="" data-assistant-stream-block-index="0">BLACKP'
            f'<?marker name="{pending_id}-pending-tail"></p> <?end></template>'
            f'<template for="{pending_id}-committed-tail" data-web-mobile-dpu-apply="append">'
            f'<?start name="{pending_id}-committed-block-0">'
            '<p data-assistant-stream-block="" data-assistant-stream-block-index="0">'
            "BLACKPINK! &amp; friends</p> <?end></template>"
            f'<template for="{pending_id}-committed-tail" data-web-mobile-dpu-apply="append">'
            f'<?start name="{pending_id}-committed-block-1">'
            '<h2 data-assistant-stream-block="" data-assistant-stream-block-index="1">'
            "멤버</h2> <?end></template>"
            f'<template for="{pending_id}-committed-tail" data-web-mobile-dpu-apply="append">'
            f'<?start name="{pending_id}-committed-block-2">'
            '<ul data-assistant-stream-block="" data-assistant-stream-block-index="2">\n'
            "<li><strong>Jisoo</strong> • Jennie</li>\n<li>Rosé • Lisa</li>\n"
            "</ul> <?end></template>"
            # Preview of the next block: already-committed text would double up.
            f'<template for="{pending_id}-pending-tail" data-web-mobile-dpu-apply="append">'
            f'<?start name="{pending_id}-committed-block-3">'
            '<p data-assistant-stream-block="">중복 미리보기</p> <?end></template>'
            # Citation chips are UI affordances, not answer text.
            f'<template for="{pending_id}-committed-block-1" data-web-mobile-dpu-apply="replace">'
            f'<?start name="{pending_id}-committed-block-1">'
            '<h2 data-assistant-stream-block="" data-assistant-stream-block-index="1">멤버 소개'
            '<span data-assistant-content-reference=""><button '
            'data-assistant-sources-trigger="" data-assistant-sources-payload="[{&quot;url&quot;:'
            '&quot;https://example.com&quot;}]">YG</button></span></h2> <?end></template>'
            # A later frame restarts slot numbering; replaying appends keeps both.
            f'<template for="{pending_id}-committed-tail" data-web-mobile-dpu-apply="append">'
            f'<?start name="{pending_id}-committed-block-0">'
            '<p data-assistant-stream-block="" data-assistant-stream-block-index="0">'
            "마지막 문단.</p> <?end></template>"
        ).encode("utf-8")
        out = chatgpt_engine.extract_full_body(self._unauth_answer_req(), body)
        self.assertEqual(
            out,
            "BLACKPINK! & friends\n"
            "## 멤버 소개\n"
            "- **Jisoo** • Jennie\n"
            "- Rosé • Lisa\n"
            "마지막 문단.",
        )
        self.assertNotIn("중복 미리보기", out or "")
        self.assertNotIn("BLACKP\n", out or "")
        self.assertNotIn("example.com", out or "")
        # Control-only frames (no committed block) must not log an empty answer.
        control_only = (
            '<template data-web-mobile-dpu-frame="127">'
            '<template data-web-mobile-conversation-turn-marker="start"></template></template>'
        ).encode("utf-8")
        req2 = self._unauth_answer_req()
        self.assertIsNone(chatgpt_engine.extract_full_body(req2, control_only))
        self.assertFalse(req2.get("_extract_final"))

    def test_unauth_mweb_ndjson_snapshot_and_markdown(self) -> None:
        # Logged-out updates now return concatenated JSON objects. The terminal
        # snapshot's quotes are escaped inside the html string, and the same
        # answer is also streamed as content.markdown.
        answer = "Hello, World! 👋😊\n\nWelcome! What can I help you with today?"
        conversation = json.dumps(
            {
                "backendConversationId": "6acfcfb1-63f4-83ea-aa6d-50eae2791474",
                "messages": [
                    {"role": "user", "content": "Hello World!", "id": "user-1"},
                    {"role": "assistant", "content": answer, "id": "asst-1"},
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        html_attr = html.escape(conversation, quote=True)
        events = [
            {
                "version": 1,
                "type": "control",
                "detail": {"kind": "started"},
            },
            {
                "version": 1,
                "type": "content",
                "messageId": "asst-1",
                "revision": 1,
                "mode": "replace",
                "markdown": "Hello, ",
                "status": "streaming",
            },
            {
                "version": 1,
                "type": "content",
                "messageId": "asst-1",
                "revision": 2,
                "mode": "append",
                "baseRevision": 1,
                "markdown": "World! 👋😊\n\nWelcome! What can I help you with today?",
                "status": "streaming",
            },
            {
                "version": 1,
                "type": "content",
                "messageId": "asst-1",
                "revision": 3,
                "mode": "append",
                "baseRevision": 2,
                "markdown": "",
                "status": "complete",
            },
            {
                "version": 1,
                "type": "shared",
                "html": (
                    '<template for="conversation-partial-control">'
                    '<span data-conversation-control="complete" hidden data-conversation="'
                    + html_attr
                    + '"></span></template>'
                ),
            },
            {"version": 1, "type": "end"},
        ]
        body = "".join(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) for event in events
        ).encode("utf-8")
        self.assertNotIn(b'data-conversation="', body)
        req = self._unauth_answer_req()
        req["content_type"] = "application/vnd.openai.conversation-source+ndjson; charset=utf-8"
        out = chatgpt_engine.extract_full_body(req, body)
        self.assertEqual(out, answer)
        self.assertNotIn("Hello World!", out or "")
        # Without the snapshot, replace+append still rebuilds the answer.
        stream_only = "".join(
            json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            for event in events
            if event.get("type") == "content"
        ).encode("utf-8")
        self.assertEqual(chatgpt_engine.extract_full_body(req, stream_only), answer)

    def test_unauth_mweb_ndjson_dil_fallback_revisions(self) -> None:
        # The live anonymous mweb transport emits markdown="" while the
        # rendered answer is in dil.fallbackMarkdown. Each append contains a
        # cumulative revision, not merely a text delta.
        answer = "저는 **GPT-6** 모델이에요!\n\n질문에 답할 수 있어요."
        events = [
            {
                "type": "content",
                "messageId": "asst-dil",
                "revision": 1,
                "mode": "replace",
                "markdown": "",
                "dil": {"fallbackMarkdown": "저는 **GPT-6**"},
            },
            {
                "type": "content",
                "messageId": "asst-dil",
                "revision": 2,
                "mode": "append",
                "markdown": "",
                "dil": {"fallbackMarkdown": "저는 **GPT-6** 모델이에요!"},
            },
            {
                "type": "content",
                "messageId": "asst-dil",
                "revision": 3,
                "mode": "append",
                "markdown": "",
                "dil": {"fallbackMarkdown": answer},
            },
            {
                "type": "content",
                "messageId": "asst-dil",
                "revision": 4,
                "mode": "append",
                "markdown": "",
                "status": "complete",
            },
        ]
        body = "".join(
            json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            for event in events
        ).encode("utf-8")
        req = self._unauth_answer_req()
        out = chatgpt_engine.extract_full_body(req, body)
        self.assertEqual(out, answer)
        self.assertTrue(req.get("_extract_final"))

    def _unauth_content_body(self, events: list) -> bytes:
        return "".join(
            json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            for event in events
        ).encode("utf-8")

    def test_unauth_mweb_revision_escapes_do_not_duplicate(self) -> None:
        # Live appends re-send the whole answer. The next copy may close a bold
        # marker that the previous frame left open, and may unescape `\-`.
        final = (
            "**Aerospace(에어로스페이스)**는 한국어로 **항공우주**라는 뜻이야.\n\n"
            "항공과 우주를 모두 포함합니다.\n\n"
            "- **Aero(항공)**: 비행기"
        )
        revisions = [
            "**Aerospace**",
            r"\*\*Aerospace(에어로스페이스)\*\*는 한국어로 **항공우주**라는 뜻이야.",
            (
                r"\*\*Aerospace(에어로스페이스)\*\*는 한국어로 **항공우주**라는 뜻이야."
                "\n\n항공과 우주를 모두 포함합니다.\n\n\\- **Aero(항공)**"
            ),
            (
                r"\*\*Aerospace(에어로스페이스)\*\*는 한국어로 **항공우주**라는 뜻이야."
                "\n\n항공과 우주를 모두 포함합니다.\n\n"
                "- **Aero(항공)**: 비행기"
            ),
        ]
        events = [
            {
                "type": "content",
                "messageId": "asst-live",
                "revision": index + 1,
                "mode": "replace" if index == 0 else "append",
                "markdown": text,
            }
            for index, text in enumerate(revisions)
        ]
        out = chatgpt_engine.extract_full_body(
            self._unauth_answer_req(), self._unauth_content_body(events)
        )
        self.assertEqual(out, final)
        self.assertEqual(out.count("에어로스페이스"), 1)
        self.assertNotIn("\\*", out or "")

    def test_unauth_mweb_write_head_caret_does_not_duplicate(self) -> None:
        # The caret sits at the write head (`#`, or an empty table cell) and
        # the next snapshot removes it. The first frame can also park `# ` in front.
        final = (
            "TLS는 전송 계층에서 동작합니다.\n\n"
            "| 항목 | 설명 |\n| --- | --- |\n| 기밀성 | 내용을 숨깁니다 |"
        )
        revisions = [
            "# TLS는 전송 계층에서 동작합니다. |  |# ",
            "TLS는 전송 계층에서 동작합니다. | 기밀성 |# ",
            final,
        ]
        events = [
            {
                "type": "content",
                "messageId": "asst-caret",
                "revision": index + 1,
                "mode": "replace" if index == 0 else "append",
                "markdown": "",
                "dil": {"fallbackMarkdown": text},
            }
            for index, text in enumerate(revisions)
        ]
        out = chatgpt_engine.extract_full_body(
            self._unauth_answer_req(), self._unauth_content_body(events)
        )
        self.assertEqual(out, final)
        self.assertEqual(out.count("TLS는 전송 계층에서"), 1)

    def test_unauth_mweb_tail_window_still_extracts_answer(self) -> None:
        # The inline tee keeps only the tail once a long anonymous body passes
        # the size cap. That tail can start mid-object. The latest cumulative
        # snapshot is still the answer the user saw.
        answer = (
            "TLS는 전송 계층에서 동작하는 보안 프로토콜입니다.\n\n"
            "핸드셰이크가 끝나면 응용 데이터는 암호화되어 전달됩니다."
        )
        older = '{"type":"content","messageId":"asst-tls","mode":"append","markdown":"TLS는 전'
        event = {
            "type": "content",
            "messageId": "asst-tls",
            "revision": 40,
            "mode": "append",
            "markdown": "",
            "dil": {"fallbackMarkdown": answer},
        }
        body = (older + json.dumps(event, ensure_ascii=False, separators=(",", ":"))).encode(
            "utf-8"
        )
        req = self._unauth_answer_req()
        out = chatgpt_engine.extract_full_body(req, body)
        self.assertEqual(out, answer)
        self.assertTrue(req.get("_extract_final"))

    def test_unauth_mweb_fallback_beats_markdown_fragment(self) -> None:
        # The same object carries a short markdown token and the real document.
        answer = "**Aerospace(에어로스페이스)**는 항공우주입니다."
        events = [
            {
                "type": "content",
                "messageId": "asst-both",
                "revision": 1,
                "mode": "append",
                "markdown": "**Aerospace**",
                "dil": {
                    "fallbackMarkdown": r"\*\*Aerospace(에어로스페이스)\*\*는 항공우주입니다."
                },
            }
        ]
        out = chatgpt_engine.extract_full_body(
            self._unauth_answer_req(), self._unauth_content_body(events)
        )
        self.assertEqual(out, answer)

    def test_unauth_mweb_tail_delta_still_concatenates(self) -> None:
        # A conversation-source append is only the new tail. It must not be
        # dropped as a revision of the text already stored.
        opening = "알파벳을 길게 설명합니다. 가나다라마바사."
        tail = "\n\n둘째 문단은 이어서 붙습니다."
        events = [
            {
                "type": "content",
                "messageId": "asst-delta",
                "revision": 1,
                "mode": "replace",
                "markdown": opening,
            },
            {
                "type": "content",
                "messageId": "asst-delta",
                "revision": 2,
                "mode": "append",
                "markdown": tail,
            },
            {
                "type": "content",
                "messageId": "asst-delta",
                "revision": 3,
                "mode": "append",
                "markdown": tail,
            },
        ]
        out = chatgpt_engine.extract_full_body(
            self._unauth_answer_req(), self._unauth_content_body(events)
        )
        self.assertEqual(out, opening + tail)

    def test_unauth_mweb_stale_shorter_snapshot_is_not_appended(self) -> None:
        opening = "알파벳을 길게 설명합니다. 가나다라마바사."
        full = opening + "\n\n둘째 문단까지 포함한 완성본입니다."
        events = [
            {
                "type": "content",
                "messageId": "asst-stale",
                "revision": 1,
                "mode": "replace",
                "markdown": full,
            },
            {
                "type": "content",
                "messageId": "asst-stale",
                "revision": 2,
                "mode": "append",
                "markdown": opening,
            },
        ]
        out = chatgpt_engine.extract_full_body(
            self._unauth_answer_req(), self._unauth_content_body(events)
        )
        self.assertEqual(out, full)

    def test_unauth_mweb_answer_miss_keeps_evidence(self) -> None:
        import tempfile

        body = b"<html><body>no assistant turn here</body></html>"
        req = self._unauth_answer_req()
        with tempfile.TemporaryDirectory() as dump_dir:
            with mock.patch.dict(os.environ, {"AIVAX_UNAUTH_DUMP_DIR": dump_dir}):
                self.assertIsNone(chatgpt_engine.extract_full_body(req, body))
                dumped = os.listdir(dump_dir)
            self.assertEqual(len(dumped), 1)
            with open(os.path.join(dump_dir, dumped[0]), "rb") as fh:
                self.assertEqual(fh.read(), body)
        self.assertFalse(req.get("_extract_final"))

    def test_chatgpt_prepare_body_is_control_only(self) -> None:
        payload = json.dumps(
            {
                "action": "next",
                "conversation_id": "6a7e0000-0000-0000-0000-000000000000",
                "client_prepare_state": "warm",
                "thinking_effort": "standard",
                "model": "gpt-5",
            }
        ).encode("utf-8")
        self.assertTrue(ai_policy_engine.is_control_only_body(payload))

    def test_body_with_user_content_is_not_control_only(self) -> None:
        payload = json.dumps({"messages": [{"content": {"parts": ["hi"]}}]}).encode("utf-8")
        self.assertFalse(ai_policy_engine.is_control_only_body(payload))


class TestChatgptWsCollectPath(unittest.TestCase):
    def setUp(self) -> None:
        chatgpt_engine._WS_RESP_BUFFER.clear()
        chatgpt_engine._WS_RESP_FINISHED.clear()
        chatgpt_engine._WS_RESP_CONV_TOPICS.clear()
        chatgpt_engine._WS_SEEN_ITEMS.clear()

    def tearDown(self) -> None:
        chatgpt_engine._WS_RESP_BUFFER.clear()
        chatgpt_engine._WS_RESP_FINISHED.clear()
        chatgpt_engine._WS_RESP_CONV_TOPICS.clear()
        chatgpt_engine._WS_SEEN_ITEMS.clear()

    def test_ws_user_is_response_collect_not_request_turn(self) -> None:
        ws = "/p24/ws/user/user-abc__def"
        self.assertTrue(chatgpt_engine.is_chatgpt_ws_user_path(ws))
        self.assertTrue(chatgpt_engine.is_chatgpt_response_collect_path(ws))
        self.assertFalse(chatgpt_engine.is_chatgpt_conversation_turn_path(ws))
        self.assertFalse(ai_policy_engine._is_conversation_path("gpt", ws))

    def test_http_conversation_still_request_turn(self) -> None:
        path = "/backend-api/f/conversation"
        self.assertTrue(chatgpt_engine.is_chatgpt_conversation_turn_path(path))
        self.assertTrue(chatgpt_engine.is_chatgpt_response_collect_path(path))
        self.assertTrue(ai_policy_engine._is_conversation_path("gpt", path))
        self.assertFalse(ai_policy_engine._is_conversation_path("gpt", path + "/prepare"))

    @staticmethod
    def _ws_req() -> dict:
        return {
            "unit_type": "ws_frame",
            "direction": "response",
            "request_id": "ws-conn-1",
            "meta": {
                "path": "/p24/ws/user/user-abc",
                "host": "ws.chatgpt.com",
            },
        }

    @staticmethod
    def _celsius_frame(encoded_item: str, *, item_id: str, topic: str) -> bytes:
        turn = topic.replace("conversation-turn-", "", 1)
        return json.dumps(
            [
                {
                    "type": "message",
                    "topic_id": topic,
                    "payload": {
                        "type": "conversation-turn-stream",
                        "payload": {
                            "type": "stream-item",
                            "conversation_id": "conv-ha-1",
                            "turn_id": turn,
                            "stream_item_id": item_id,
                            "encoded_item": encoded_item,
                        },
                    },
                }
            ],
            ensure_ascii=False,
        ).encode("utf-8")

    @staticmethod
    def _encoded_assistant(text: str, *, msg_id: str) -> str:
        inner = {
            "v": {
                "message": {
                    "id": msg_id,
                    "author": {"role": "assistant"},
                    "status": "finished_successfully",
                    "content": {"content_type": "text", "parts": [text]},
                }
            }
        }
        return "event: delta\ndata: " + json.dumps(inner, ensure_ascii=False) + "\n\n"

    def test_ws_buffers_item_finished_until_stream_complete(self) -> None:
        """High-reasoning emits several finished assistant snapshots before the turn ends.

        The first ~200-char item must not be treated as extract_final, or
        output_filter message_id dedup drops the later full answer.
        """
        topic = "conversation-turn-3ac2e88d-8f07-4311-9fa0-4b45180341f4"
        short = "그러면 요구사항이 훨씬 명확합니다. " * 4
        full = short + " OVS-DPDK Active/Standby 본딩과 Bypass NIC 페일오버를 이렇게 구성하세요. " * 20
        req = self._ws_req()

        first = chatgpt_engine.extract_full_body(
            req,
            self._celsius_frame(
                self._encoded_assistant(short, msg_id="asst-short"),
                item_id="item-short",
                topic=topic,
            ),
        )
        self.assertIsNone(first)
        self.assertFalse(req.get("_extract_final"))

        req2 = self._ws_req()
        second = chatgpt_engine.extract_full_body(
            req2,
            self._celsius_frame(
                self._encoded_assistant(full, msg_id="asst-full"),
                item_id="item-full",
                topic=topic,
            ),
        )
        self.assertIsNone(second)
        self.assertFalse(req2.get("_extract_final"))

        req3 = self._ws_req()
        complete = chatgpt_engine.extract_full_body(
            req3,
            self._celsius_frame(
                'data: {"type":"message_stream_complete"}\n\n',
                item_id="item-done",
                topic=topic,
            ),
        )
        self.assertIsNotNone(complete)
        self.assertTrue(req3.get("_extract_final"))
        self.assertIn("OVS-DPDK", complete or "")
        self.assertGreater(len(complete or ""), len(short))

    def test_ws_answer_remaps_to_turn_when_handoff_has_unknown_types(self) -> None:
        """Pro turns hand off with control types we do not model yet.

        Without the parent binding the WS answer falls back to the long-lived
        connection request_id, and output_filter drops it as a duplicate of the
        previous answer on the same socket.
        """
        turn = "23fb2e4c-12cb-43f0-a476-4c8590e37be0"
        topic = f"conversation-turn-{turn}"
        handoff = (
            'event: delta_encoding\ndata: "v1"\n\n'
            'data: {"type":"resume_conversation_token","kind":"topic",'
            '"conversation_id":"conv-pro-1"}\n\n'
            'data: {"type":"server_ste_metadata","conversation_id":"conv-pro-1"}\n\n'
            'data: {"type":"title_generation","title":"x"}\n\n'
            'data: {"type":"stream_handoff","conversation_id":"conv-pro-1",'
            f'"turn_exchange_id":"{turn}","options":[{{"type":"subscribe_ws_topic",'
            f'"topic_id":"{topic}"}}]}}\n\n'
            "data: [DONE]\n\n"
        )
        turn_req = {
            "request_id": "parent-rid-hr",
            "unit_type": "sse_event",
            "direction": "response",
            "meta": {"path": "/backend-api/f/conversation", "host": "chatgpt.com"},
        }
        self.assertIsNone(
            chatgpt_engine.extract_full_body(turn_req, handoff.encode("utf-8"))
        )
        self.assertTrue(turn_req.get("_chatgpt_sse_control_only"))

        answer = "Pro 추론 결과입니다. " * 30
        buffered = chatgpt_engine.extract_full_body(
            self._ws_req(),
            self._celsius_frame(
                self._encoded_assistant(answer, msg_id="asst-pro"),
                item_id="item-pro",
                topic=topic,
            ),
        )
        self.assertIsNone(buffered)

        final_req = self._ws_req()
        final = chatgpt_engine.extract_full_body(
            final_req,
            self._celsius_frame(
                'data: {"type":"message_stream_complete"}\n\n',
                item_id="item-pro-done",
                topic=topic,
            ),
        )
        self.assertIsNotNone(final)
        self.assertIn("Pro 추론 결과입니다.", final or "")
        self.assertEqual(final_req.get("_chatgpt_ui_message_id"), "parent-rid-hr")

    @staticmethod
    def _delta_msg(msg_id: str, c: int, *, channel: str, text: str = "",
                   preamble: bool = False, first: bool = False) -> str:
        meta = {"is_thinking_preamble_message": True} if preamble else {}
        msg = {
            "id": msg_id,
            "author": {"role": "assistant", "name": None, "metadata": {}},
            "content": {"content_type": "text", "parts": [text]},
            "status": "in_progress",
            "metadata": meta,
            "recipient": "all",
            "channel": channel,
        }
        ev = {"v": {"message": msg, "conversation_id": "conv-bp"}, "c": c}
        if first:
            ev = {"p": "", "o": "add", **ev}
        return "event: delta\ndata: " + json.dumps(ev, ensure_ascii=False) + "\n\n"

    @staticmethod
    def _delta(obj: dict) -> str:
        return "event: delta\ndata: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

    def test_sse_commentary_preamble_is_not_the_answer(self) -> None:
        """High-reasoning search turn: planning note is a separate commentary message."""
        note = ("핑크의 결성 배경, 멤버별 특징, 대표곡, 주요 기록, 솔로 활동, 그리고 2026년 "
                "현재 흐름까지 한 번에 정리해드릴게요. 최신 활동은 공식·신뢰할 만한 자료로 "
                "확인해서 함께 설명하겠습니다.")
        answer = "물론입니다. **BLACKPINK(블랙핑크)**는 4인조 그룹입니다."
        sse = (
            'event: delta_encoding\ndata: "v1"\n\n'
            + self._delta_msg("m-note", 2, channel="commentary", preamble=True, first=True)
            + self._delta({"p": "/message/content/parts/0", "o": "append", "v": note[:20]})
            + self._delta({"v": note[20:]})
            + self._delta({"p": "", "o": "patch", "v": [
                {"p": "/message/status", "o": "replace", "v": "finished_successfully"}]})
            + self._delta_msg("m-final", 5, channel="final")
            + self._delta({"p": "/message/content/parts/0", "o": "append", "v": answer[:10]})
            + self._delta({"v": answer[10:]})
            + self._delta({"p": "", "o": "patch", "v": [
                {"p": "/message/status", "o": "replace", "v": "finished_successfully"}]})
            + 'data: {"type":"message_stream_complete","conversation_id":"conv-bp"}\n\n'
        )
        req = {"request_id": "rid-bp", "unit_type": "sse_event",
               "meta": {"path": "/backend-api/f/conversation"}}
        self.assertEqual(chatgpt_engine.extract_full_body(req, sse.encode("utf-8")), answer)

    def test_sse_commentary_only_yields_no_answer(self) -> None:
        note = "이스X를 회사 개요 → 로켓/우주선 순서로 정리하겠습니다. 공개 자료도 확인해 설명할게요."
        sse = (
            'event: delta_encoding\ndata: "v1"\n\n'
            + self._delta_msg("m-note", 2, channel="commentary", preamble=True, first=True)
            + self._delta({"p": "/message/content/parts/0", "o": "append", "v": note})
            + 'data: {"type":"message_stream_complete","conversation_id":"conv-bp"}\n\n'
        )
        req = {"request_id": "rid-sx", "unit_type": "sse_event",
               "meta": {"path": "/backend-api/f/conversation"}}
        self.assertIsNone(chatgpt_engine.extract_full_body(req, sse.encode("utf-8")))

    def test_sse_channel_patched_to_commentary_is_dropped(self) -> None:
        sse = (
            self._delta_msg("m-a", 1, channel="final", first=True)
            + self._delta({"p": "", "o": "patch", "v": [
                {"p": "/message/channel", "o": "replace", "v": "commentary"},
                {"p": "/message/content/parts/0", "o": "append", "v": "검색해 볼게요."}]})
            + self._delta_msg("m-b", 2, channel="final", text="진짜 답변")
        )
        req = {"request_id": "rid-ch", "unit_type": "sse_event",
               "meta": {"path": "/backend-api/f/conversation"}}
        self.assertEqual(chatgpt_engine.extract_full_body(req, sse.encode("utf-8")), "진짜 답변")

    def test_ws_commentary_preamble_across_frames_is_dropped(self) -> None:
        topic = "conversation-turn-6b1f0c2e-1111-4222-8333-944455556666"
        note = "공식 자료로 확인해서 함께 설명하겠습니다."
        answer = "SpaceX는 민간 우주 기업입니다. " * 5
        req = self._ws_req()
        self.assertIsNone(chatgpt_engine.extract_full_body(req, self._celsius_frame(
            self._delta_msg("w-note", 2, channel="commentary", preamble=True, first=True)
            + self._delta({"p": "/message/content/parts/0", "o": "append", "v": note}),
            item_id="w1", topic=topic)))
        self.assertIsNone(chatgpt_engine.extract_full_body(self._ws_req(), self._celsius_frame(
            self._delta({"v": " 추가 안내"}), item_id="w2", topic=topic)))
        chatgpt_engine.extract_full_body(self._ws_req(), self._celsius_frame(
            self._delta_msg("w-final", 5, channel="final")
            + self._delta({"p": "/message/content/parts/0", "o": "append", "v": answer}),
            item_id="w3", topic=topic))
        final = chatgpt_engine.extract_full_body(self._ws_req(), self._celsius_frame(
            'data: {"type":"message_stream_complete"}\n\n', item_id="w4", topic=topic))
        self.assertEqual(final, answer)

    def test_later_answer_stays_on_the_prompt_request(self) -> None:
        turn = "5a1b0c2d-1111-4222-8333-944455556666"
        first = (
            'event: delta_encoding\ndata: "v1"\n\n'
            'data: {"type":"resume_conversation_token","kind":"topic",'
            f'"conversation_id":"conv-keep-1","turn_exchange_id":"{turn}"}}\n\n'
            'data: {"type":"message_stream_complete","conversation_id":"conv-keep-1"}\n\n'
        )
        prompt = {
            "request_id": "prompt-keep-1",
            "unit_type": "sse_event",
            "direction": "response",
            "meta": {"path": "/backend-api/f/conversation", "host": "chatgpt.com"},
        }
        self.assertIsNone(chatgpt_engine.extract_full_body(prompt, first.encode("utf-8")))
        answer = "이 질문의 완성된 답변입니다."
        second = (
            'event: delta_encoding\ndata: "v1"\n\n'
            'data: {"type":"resume_conversation_token","kind":"topic",'
            f'"conversation_id":"conv-keep-1","turn_exchange_id":"{turn}"}}\n\n'
            + self._delta_msg("m-keep", 2, channel="final", text=answer, first=True)
            + 'data: {"type":"message_stream_complete","conversation_id":"conv-keep-1"}\n\n'
        )
        follow = {
            "request_id": "follow-keep-1",
            "unit_type": "sse_event",
            "direction": "response",
            "meta": {"path": "/backend-api/f/conversation/resume", "host": "chatgpt.com"},
        }
        text = chatgpt_engine.extract_full_body(follow, second.encode("utf-8"))
        self.assertEqual(text, answer)
        self.assertEqual(follow.get("_chatgpt_ui_message_id"), "prompt-keep-1")

    def test_websocket_answer_arriving_first_fills_the_prompt(self) -> None:
        conv = "conv-early-keep-1"
        answer = "웹소켓으로 먼저 도착한 답변"
        chatgpt_engine._stash_pending_answer(conv, answer)
        body = (
            'data: {"type":"resume_conversation_token","conversation_id":"' + conv + '"}\n\n'
            'data: {"type":"stream_handoff","conversation_id":"' + conv + '"}\n\n'
        )
        req = {
            "request_id": "prompt-early-1",
            "unit_type": "sse_event",
            "direction": "response",
            "meta": {"path": "/backend-api/f/conversation", "host": "chatgpt.com"},
        }
        self.assertEqual(
            chatgpt_engine.extract_full_body(req, body.encode("utf-8")),
            answer,
        )
        self.assertFalse(req.get("_chatgpt_ui_message_id"))

    def test_repeated_prompt_uses_the_original_message(self) -> None:
        payload = json.dumps(
            {
                "conversation_id": "conv-repeat-1",
                "messages": [
                    {
                        "id": "user-msg-keep-1",
                        "author": {"role": "user"},
                        "content": {"content_type": "text", "parts": ["즐거운 이미지"]},
                    }
                ],
            }
        ).encode("utf-8")
        first = {
            "request_id": "prompt-repeat-1",
            "unit_type": "json_body",
            "direction": "request",
            "meta": {"path": "/backend-api/f/conversation"},
        }
        second = {
            "request_id": "prompt-repeat-2",
            "unit_type": "json_body",
            "direction": "request",
            "meta": {"path": "/backend-api/f/conversation"},
        }
        self.assertIn("즐거운 이미지", chatgpt_engine.extract(first, payload) or "")
        self.assertIn("즐거운 이미지", chatgpt_engine.extract(second, payload) or "")
        self.assertEqual(second.get("_chatgpt_ui_message_id"), "prompt-repeat-1")

    def test_thinking_shell_follow_up_is_traced_without_tokens(self) -> None:
        import tempfile

        conv = "6ad5aaaa-5030-83e8-8e2c-21e5d0ba5400"
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJ0dXJuX3RvcGljX2lkIjoieCJ9.c2lnbmF0dXJlLXNpZw"
        shell = (
            'event: delta_encoding\ndata: "v1"\n\n'
            'data: {"type":"resume_conversation_token","kind":"topic","token":"' + jwt + '",'
            '"conversation_id":"' + conv + '"}\n\n'
            'data: {"type":"message_marker","marker":"cot_token","conversation_id":"' + conv + '"}\n\n'
            'data: {"type":"message_stream_complete","conversation_id":"' + conv + '"}\n\n'
            "data: [DONE]\n\n"
        )
        frame = json.dumps(
            {
                "type": "conversation-turn-stream",
                "conversation_id": conv,
                "token": jwt,
                "payload": {"type": "delta", "v": "생각 뒤에 온 답"},
            }
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"AIVAX_GPT_ANSWER_TRACE_DIR": tmp}
        ):
            chatgpt_engine._ANSWER_TRACE_WATCH.clear()
            prompt = {
                "request_id": "prompt-think-1",
                "unit_type": "sse_event",
                "direction": "response",
                "meta": {"path": "/backend-api/f/conversation", "host": "chatgpt.com"},
            }
            self.assertFalse(chatgpt_engine.extract_full_body(prompt, shell.encode("utf-8")))
            self.assertEqual(os.listdir(tmp), [])
            ws = {
                "request_id": "ws-conn-1",
                "unit_type": "ws_frame",
                "direction": "response",
                "meta": {
                    "path": "/p28/ws/user/user-x?verify=secret-sig",
                    "host": "ws.chatgpt.com",
                },
            }
            chatgpt_engine.extract_full_body(ws, frame.encode("utf-8"))
            names = os.listdir(tmp)
            self.assertEqual(len(names), 1)
            self.assertIn("prompt-think-1_ws_ws-conn-1", names[0])
            with open(os.path.join(tmp, names[0]), "rb") as fh:
                saved = fh.read()
            self.assertNotIn(jwt.encode(), saved)
            self.assertIn(b"<jwt>", saved)
            self.assertIn(conv.encode(), saved)
            chatgpt_engine._ANSWER_TRACE_WATCH.clear()

    def test_answer_trace_is_off_by_default(self) -> None:
        chatgpt_engine._ANSWER_TRACE_WATCH.clear()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AIVAX_GPT_ANSWER_TRACE_DIR", None)
            req = {
                "request_id": "prompt-think-off",
                "unit_type": "sse_event",
                "direction": "response",
                "meta": {"path": "/backend-api/f/conversation", "host": "chatgpt.com"},
            }
            body = 'data: {"type":"stream_handoff","conversation_id":"conv-off-1"}\n\n'
            chatgpt_engine.extract_full_body(req, body.encode("utf-8"))
        self.assertEqual(chatgpt_engine._ANSWER_TRACE_WATCH, {})


class TestChatgptTruncatedStreamAnswer(unittest.TestCase):
    """Live /f/conversation bodies: gzip missing its trailer, thoughts patch without ``c``."""

    _SSE = (
        'event: delta_encoding\ndata: "v1"\n\n'
        'data: {"type":"resume_conversation_token","kind":"topic","token":"x",'
        '"conversation_id":"conv-tr-1"}\n\n'
        'event: delta\ndata: {"p":"","o":"add","v":{"message":{"id":"t0",'
        '"author":{"role":"assistant"},"content":{"content_type":"thoughts","thoughts":[]},'
        '"status":"finished_successfully"}},"c":0}\n\n'
        'event: delta\ndata: {"v":{"message":{"id":"t1","author":{"role":"assistant"},'
        '"content":{"content_type":"thoughts","thoughts":[{"summary":"Thinking"}]},'
        '"status":"finished_successfully","metadata":{"is_visually_hidden_from_conversation":false}}},"c":1}\n\n'
        'event: delta\ndata: {"v":{"message":{"id":"ans","author":{"role":"assistant"},'
        '"content":{"content_type":"text","parts":[""]},"status":"in_progress",'
        '"channel":"final"}},"c":2}\n\n'
        'event: delta\ndata: {"o":"patch","v":[{"p":"/message/content/thoughts/0/summary",'
        '"o":"replace","v":"Worked"},{"p":"/message/metadata/is_visually_hidden_from_conversation",'
        '"o":"replace","v":true}]}\n\n'
        'event: delta\ndata: {"p":"/message/content/parts/0","o":"append","v":"네","c":2}\n\n'
        'event: delta\ndata: {"v":", 함수만 교체하면 됩니다."}\n\n'
        'event: delta\ndata: {"p":"","o":"patch","v":[{"p":"/message/status","o":"replace",'
        '"v":"finished_successfully"}]}\n\n'
        'data: {"type":"message_stream_complete","conversation_id":"conv-tr-1"}\n\n'
        "data: [DONE]\n\n"
    )

    def _req(self) -> dict:
        return {
            "unit_type": "sse_event",
            "request_id": "req-tr-1",
            "meta": {"path": "/backend-api/f/conversation", "host": "chatgpt.com"},
        }

    def test_gzip_without_trailer_still_decompresses(self) -> None:
        import gzip

        body = gzip.compress(self._SSE.encode("utf-8"))[:-8]
        out, ok = ai_policy_engine.maybe_content_decompress(body, "")
        self.assertTrue(ok)
        self.assertIn(b"data: [DONE]", out)

    def test_brotli_without_end_marker_still_decompresses(self) -> None:
        try:
            import brotli
        except ImportError:
            self.skipTest("brotli module is unavailable")

        encoded = brotli.compress(self._SSE.encode("utf-8"))
        truncated = encoded[:-1]
        with self.assertRaises(brotli.error):
            brotli.decompress(truncated)
        out, ok = ai_policy_engine.maybe_content_decompress(truncated, "br")
        self.assertTrue(ok)
        self.assertIn(b"message_stream_complete", out)
        text = chatgpt_engine.extract_full_body(self._req(), out)
        self.assertEqual(text, "네, 함수만 교체하면 됩니다.")

    def test_thoughts_patch_without_c_does_not_hide_answer(self) -> None:
        text = chatgpt_engine.extract_full_body(self._req(), self._SSE.encode("utf-8"))
        self.assertEqual(text, "네, 함수만 교체하면 됩니다.")

    def test_brotli_without_content_encoding_still_decompresses(self) -> None:
        try:
            import brotli
        except ImportError:
            self.skipTest("brotli module is unavailable")

        encoded = brotli.compress(self._SSE.encode("utf-8"))
        out, ok = ai_policy_engine.maybe_content_decompress(encoded[:-1], "")
        self.assertTrue(ok)
        self.assertIn(b"message_stream_complete", out)
        text = chatgpt_engine.extract_full_body(self._req(), out)
        self.assertEqual(text, "네, 함수만 교체하면 됩니다.")


class TestContentDecompressKeepsBinaryUploads(unittest.TestCase):
    """Brotli fallback must not rewrite a binary upload that only shares a prefix."""

    _HWP = Path(__file__).resolve().parent / "fixtures" / "hwp_not_brotli.hwp"
    _HWP_SHA256 = "cca09f1db9d2a55779fc043b8eb9106e2456de0bf45a818770c5160d2c968cec"

    def _hwp(self) -> bytes:
        import hashlib

        data = self._HWP.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        self.assertEqual(digest, self._HWP_SHA256, "HWP fixture bytes changed")
        return data

    def test_hwp_is_not_rewritten_without_content_encoding(self) -> None:
        data = self._hwp()
        for encoding in ("", "br", "gzip", "identity"):
            out, ok = ai_policy_engine.maybe_content_decompress(data, encoding)
            self.assertFalse(ok, encoding)
            self.assertEqual(out, data, encoding)

    def test_complete_brotli_and_short_junk_still_decompress(self) -> None:
        try:
            import brotli
        except ImportError:
            self.skipTest("brotli module is unavailable")

        plain = b'{"upload":"init","name":"notes.txt"}'
        encoded = brotli.compress(plain)
        for payload, encoding in (
            (encoded, ""),
            (encoded, "br"),
            (encoded + b"\xff" * 200, ""),
        ):
            out, ok = ai_policy_engine.maybe_content_decompress(payload, encoding)
            self.assertTrue(ok, encoding or "empty-ce")
            self.assertEqual(out, plain)

    def test_gzip_zstd_deflate_and_plaintext_stay_intact(self) -> None:
        import gzip
        import zlib

        plain = b'{"kind":"chat","text":"hello"}'
        gzip_out, gzip_ok = ai_policy_engine.maybe_content_decompress(gzip.compress(plain), "")
        self.assertTrue(gzip_ok)
        self.assertEqual(gzip_out, plain)

        deflate_out, deflate_ok = ai_policy_engine.maybe_content_decompress(
            zlib.compress(plain), "deflate"
        )
        self.assertTrue(deflate_ok)
        self.assertEqual(deflate_out, plain)

        try:
            import zstandard
        except ImportError:
            zstd_blob = None
        else:
            zstd_blob = zstandard.ZstdCompressor().compress(plain)
        if zstd_blob is not None:
            zstd_out, zstd_ok = ai_policy_engine.maybe_content_decompress(zstd_blob, "")
            self.assertTrue(zstd_ok)
            self.assertEqual(zstd_out, plain)

        raw_json, raw_ok = ai_policy_engine.maybe_content_decompress(plain, "br")
        self.assertFalse(raw_ok)
        self.assertEqual(raw_json, plain)

        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 2048
        png_out, png_ok = ai_policy_engine.maybe_content_decompress(png, "")
        self.assertFalse(png_ok)
        self.assertEqual(png_out, png)


class TestChatgptWsConversationUpdate(unittest.TestCase):
    def setUp(self) -> None:
        chatgpt_engine._WS_RESP_BUFFER.clear()
        chatgpt_engine._WS_RESP_FINISHED.clear()
        chatgpt_engine._WS_RESP_CONV_TOPICS.clear()
        chatgpt_engine._WS_SEEN_ITEMS.clear()

    @staticmethod
    def _frame(update_type: str, content_key: str, msg: dict) -> bytes:
        content = {"messages": [msg]} if content_key == "messages" else {"message": msg}
        body = json.dumps(
            {
                "type": "conversation-update",
                "payload": {
                    "conversation_id": "conv-async-1",
                    "update_type": update_type,
                    "update_content": content,
                },
                "metadata": None,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        return bytes([0x81, 126]) + len(body).to_bytes(2, "big") + body

    @staticmethod
    def _answer(text: str) -> dict:
        return {
            "id": "m-async",
            "author": {"role": "assistant"},
            "content": {"content_type": "text", "parts": [text]},
            "status": "finished_successfully",
            "channel": "final",
            "recipient": "all",
        }

    def _req(self) -> dict:
        return {
            "unit_type": "ws_frame",
            "request_id": "ws-conn-1-ws-00ab",
            "meta": {"path": "/p24/ws/user/user-abc", "host": "ws.chatgpt.com"},
        }

    def test_async_task_update_message_is_final_answer(self) -> None:
        req = self._req()
        frame = self._frame("async-task-update-message", "message", self._answer("최종 답변입니다."))
        with mock.patch.object(chatgpt_engine, "_resume_parent_get", return_value="req-parent-1"):
            text = chatgpt_engine.extract_full_body(req, frame)
        self.assertEqual(text, "최종 답변입니다.")
        self.assertEqual(req.get("_chatgpt_ui_message_id"), "req-parent-1")

    def test_add_messages_answer_is_extracted(self) -> None:
        req = self._req()
        frame = self._frame("add-messages", "messages", self._answer("추가 메시지 답변"))
        with mock.patch.object(chatgpt_engine, "_resume_parent_get", return_value="req-parent-2"):
            text = chatgpt_engine.extract_full_body(req, frame)
        self.assertEqual(text, "추가 메시지 답변")

    def test_conversation_update_without_parent_is_not_emitted(self) -> None:
        req = self._req()
        frame = self._frame("async-task-update-message", "message", self._answer("부모 없음"))
        with mock.patch.object(chatgpt_engine, "_resume_parent_get", return_value=""):
            self.assertIsNone(chatgpt_engine.extract_full_body(req, frame))


class TestClaudeSupportEmailNotIdentity(unittest.TestCase):
    def test_support_anthropic_is_not_a_user_email(self) -> None:
        self.assertFalse(claude_engine._is_real_user_email("support@anthropic.com"))
        self.assertFalse(claude_engine._is_real_user_email("support+help@anthropic.com"))
        self.assertFalse(claude_engine._is_real_user_email("noreply@claude.ai"))
        self.assertTrue(claude_engine._is_real_user_email("alice@corp.com"))
        self.assertTrue(claude_engine._is_real_user_email("jane@anthropic.com"))

    def test_web_html_skips_footer_support_email(self) -> None:
        html = (
            b"<html><body>Need help? Email support@anthropic.com"
            b" Signed in as alice@corp.com</body></html>"
        )
        uuid, email = claude_engine._extract_web_profile(html)
        self.assertEqual(email, "alice@corp.com")
        self.assertEqual(uuid, "")

    def test_web_html_with_only_support_email_has_no_identity(self) -> None:
        html = b"<html><footer>support@anthropic.com</footer></html>"
        uuid, email = claude_engine._extract_web_profile(html)
        self.assertEqual(email, "")
        self.assertEqual(uuid, "")

    def test_next_flight_html_skips_support_and_uses_account(self) -> None:
        inner = {
            "help": {
                "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "email_address": "support@anthropic.com",
            },
            "account": {
                "uuid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "email_address": "alice@corp.com",
            },
            "organization": {"uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
        }
        frame = "11:" + json.dumps(inner, separators=(",", ":"))
        html = (
            "<html>Need help? support@anthropic.com"
            f"<script>self.__next_f.push([1, {json.dumps(frame)}])</script>"
            "</html>"
        ).encode()
        uuid, email = claude_engine._extract_web_profile(html)
        self.assertEqual(uuid, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        self.assertEqual(email, "alice@corp.com")

    def test_keyed_support_email_does_not_win_over_user(self) -> None:
        payload = (
            b'{"email":"support@anthropic.com",'
            b'"email_address":"alice@corp.com"}'
        )
        _, email = claude_engine._extract_web_profile(payload)
        self.assertEqual(email, "alice@corp.com")

    def test_account_json_ignores_vendor_support_mailbox(self) -> None:
        obj = {
            "account": {
                "uuid": "11111111-1111-1111-1111-111111111111",
                "email_address": "support@anthropic.com",
            }
        }
        uuid, email = claude_engine._extract_claude_identity(obj)
        self.assertEqual(uuid, "11111111-1111-1111-1111-111111111111")
        self.assertEqual(email, "")

    def test_account_json_keeps_real_user_email(self) -> None:
        obj = {
            "oauth_account": {
                "uuid": "11111111-1111-1111-1111-111111111111",
                "email_address": "alice@corp.com",
            }
        }
        uuid, email = claude_engine._extract_claude_identity(obj)
        self.assertEqual(uuid, "11111111-1111-1111-1111-111111111111")
        self.assertEqual(email, "alice@corp.com")

    def test_org_profile_name_does_not_register_support_email(self) -> None:
        req = {
            "direction": "response",
            "meta": {
                "host": "claude.ai",
                "method": "GET",
                "path": "/api/organizations/11111111-1111-1111-1111-111111111111",
            },
        }
        claude_engine._log_org_profile(
            req,
            {
                "uuid": "11111111-1111-1111-1111-111111111111",
                "name": "support@anthropic.com",
            },
        )
        self.assertEqual(req.get("_claude_org_email"), "")

    def test_poisoned_support_email_is_replaced_by_real_account(self) -> None:
        import account_lookup
        import os

        prev_path = os.environ.get("AIVAX_ACCOUNT_CACHE_PATH")
        os.environ["AIVAX_ACCOUNT_CACHE_PATH"] = ""
        account_lookup.configure(maxsize=32, ttl_seconds=60, shards=4)
        req = {
            "meta": {
                "host": "claude.ai",
                "path": (
                    "/api/organizations/11111111-1111-1111-1111-111111111111/"
                    "chat_conversations/22222222-2222-2222-2222-222222222222/completion"
                ),
            }
        }
        try:
            account_lookup.set_account(
                "claude",
                "org:11111111-1111-1111-1111-111111111111",
                "11111111-1111-1111-1111-111111111111",
                "support@anthropic.com",
            )
            looked = claude_engine._lookup_claude_account(req)
            self.assertEqual(looked[1], "")
            claude_engine._register_claude_account(
                req,
                "11111111-1111-1111-1111-111111111111",
                "alice@corp.com",
            )
            looked = claude_engine._lookup_claude_account(req)
            self.assertEqual(looked[1], "alice@corp.com")
        finally:
            if account_lookup._shared is not None:
                account_lookup._shared.close()
                account_lookup._shared = None
            if prev_path is None:
                os.environ.pop("AIVAX_ACCOUNT_CACHE_PATH", None)
            else:
                os.environ["AIVAX_ACCOUNT_CACHE_PATH"] = prev_path


class TestChatgptCodexDesktop(unittest.TestCase):
    PNG_B64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )

    @staticmethod
    def _req(path: str = "/backend-api/codex/responses") -> Dict[str, Any]:
        return {
            "direction": "request",
            "unit_type": "json_body",
            "content_type": "application/json",
            "meta": {"host": "chatgpt.com", "path": path},
        }

    def _user_turn_payload(self, prompt: str, with_image: bool = False) -> bytes:
        content: list = [{"type": "input_text", "text": prompt}]
        if with_image:
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{self.PNG_B64}",
                }
            )
        body = {
            "model": "gpt-5",
            "stream": True,
            "previous_response_id": "resp_parent_1",
            "input": [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "internal instructions"}],
                },
                {"type": "message", "role": "user", "content": content},
            ],
        }
        return json.dumps(body).encode("utf-8")

    def test_codex_responses_is_a_conversation_turn(self) -> None:
        path = "/backend-api/codex/responses"
        self.assertTrue(chatgpt_engine.is_chatgpt_conversation_turn_path(path))
        self.assertTrue(chatgpt_engine.is_chatgpt_response_collect_path(path))
        self.assertTrue(ai_policy_engine._is_conversation_path("gpt", path))
        self.assertFalse(
            chatgpt_engine.is_chatgpt_conversation_turn_path(
                "/backend-api/codex/analytics-events/events"
            )
        )
        self.assertFalse(
            chatgpt_engine.is_chatgpt_conversation_turn_path("/backend-api/codex/alpha/search")
        )

    def test_codex_desktop_cloud_socket_is_a_user_turn(self) -> None:
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "turn/start",
                "params": {
                    "threadId": "thr_1",
                    "input": [
                        {
                            "type": "message",
                            "role": "developer",
                            "content": [{"type": "input_text", "text": "system rules"}],
                        },
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "AIVAX-CODEX-DESKTOP-1"}],
                        },
                    ],
                },
            }
        ).encode("utf-8")
        frame = bytes([0x81, 126]) + len(body).to_bytes(2, "big") + body
        req = {
            "direction": "request",
            "unit_type": "ws_frame",
            "content_type": "",
            "meta": {
                "host": "codex-cloud-backend.chatgpt.com",
                "path": "/",
                "client_ip": "10.0.50.62",
            },
        }
        self.assertTrue(chatgpt_engine._is_codex_desktop_ws(req))
        self.assertFalse(chatgpt_engine.chatgpt_should_skip_policy(req, frame))
        self.assertEqual(chatgpt_engine.extract(req, frame), "AIVAX-CODEX-DESKTOP-1")
        ping = {
            "direction": "request",
            "unit_type": "ws_frame",
            "meta": {"host": "codex-cloud-backend.chatgpt.com", "path": "/"},
        }
        other = json.dumps({"method": "ping", "params": {}}).encode("utf-8")
        other_frame = bytes([0x81, len(other)]) + other
        self.assertTrue(chatgpt_engine.chatgpt_should_skip_policy(ping, other_frame))
        self.assertEqual(chatgpt_engine.extract(ping, other_frame), "")

    def test_extracts_my_request_not_developer_instructions(self) -> None:
        wrapped = (
            "# Instructions\nDo not leak secrets.\n\n"
            "# My request\nAIVAX-APP-GPT-0921"
        )
        payload = self._user_turn_payload(wrapped, with_image=True)
        req = self._req()
        self.assertFalse(chatgpt_engine.chatgpt_should_skip_policy(req, payload))
        self.assertEqual(chatgpt_engine.extract(req, payload), "AIVAX-APP-GPT-0921")
        pol = chatgpt_engine.build_policy_api_payload(req, payload, "AIVAX-APP-GPT-0921")
        self.assertIsInstance(pol, dict)
        self.assertEqual(pol.get("prompt"), "AIVAX-APP-GPT-0921")
        atts = pol.get("attachments") or []
        self.assertTrue(any(str(a.get("id") or "").strip() for a in atts if isinstance(a, dict)))

    def _with_turn_metadata(self, payload: bytes, metadata: dict) -> bytes:
        body = json.loads(payload)
        body["client_metadata"] = {
            "x-codex-turn-metadata": json.dumps(metadata, separators=(",", ":"))
        }
        return json.dumps(body).encode("utf-8")

    def test_app_started_turn_is_not_a_user_prompt(self) -> None:
        # The wording is irrelevant. Codex marks ambient/system turns in metadata.
        payload = self._with_turn_metadata(
            self._user_turn_payload("오늘 회의록 정리해줘"),
            {"thread_source": "system", "request_kind": "turn"},
        )
        req = self._req()
        self.assertTrue(chatgpt_engine.chatgpt_should_skip_policy(req, payload))
        self.assertFalse(chatgpt_engine.extract(req, payload))

    def test_turn_metadata_header_classifies_the_response_too(self) -> None:
        req = self._req()
        req["direction"] = "response"
        req["unit_type"] = "sse_event"
        req["content_type"] = "text/event-stream"
        req["meta"]["x-codex-turn-metadata"] = json.dumps(
            {"thread_source": "ambient_suggestions", "request_kind": "turn"}
        )
        sse = (
            "event: response.output_text.delta\n"
            'data: {"type":"response.output_text.delta","delta":"suggestion"}\n\n'
            "event: response.completed\n"
            'data: {"type":"response.completed","response":{"id":"resp_1"}}\n\n'
        ).encode("utf-8")
        self.assertIsNone(chatgpt_engine.extract_full_body(req, sse))
        self.assertTrue(req.get("_chatgpt_sse_control_only"))

    def test_compaction_and_memory_turns_are_not_prompts(self) -> None:
        for kind, source in (("compaction", "user"), ("memory", "memory_consolidation")):
            payload = self._with_turn_metadata(
                self._user_turn_payload("이전 대화를 요약"),
                {"thread_source": source, "request_kind": kind},
            )
            req = self._req()
            self.assertTrue(
                chatgpt_engine.chatgpt_should_skip_policy(req, payload),
                f"{kind}/{source}",
            )

    # Codex builds this client-side (openai.chatgpt extension, thread title
    # generation) and sends it as the user message of an ephemeral thread.
    TITLE_PROMPT = (
        "You are a helpful assistant. You will be presented with a user prompt, "
        "and your job is to provide a short title for a task that will be "
        "created from that prompt.\n"
        "Generate a concise UI title (up to 36 characters) for this task.\n"
        "Fill the structured title field with plain text.\n\n"
        "User prompt:\nCodex Test"
    )

    def _generation_turn_payload(
        self,
        prompt: str,
        metadata: Optional[dict] = None,
        output_format: Optional[dict] = None,
    ) -> bytes:
        body: Dict[str, Any] = {
            "type": "response.create",
            "model": "gpt-6-luna",
            "stream": True,
            "store": False,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                }
            ],
        }
        if output_format is not None:
            body["text"] = {"format": output_format}
        if metadata is not None:
            body["client_metadata"] = {
                "x-codex-turn-metadata": json.dumps(metadata, separators=(",", ":"))
            }
        return json.dumps(body, ensure_ascii=False).encode("utf-8")

    @staticmethod
    def _title_schema_format() -> dict:
        return {
            "type": "json_schema",
            "name": "thread_title",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["title", "description"],
                "additionalProperties": False,
            },
        }

    def test_thread_title_generation_is_not_a_user_prompt(self) -> None:
        payload = self._generation_turn_payload(
            self.TITLE_PROMPT,
            metadata={
                "thread_source": "thread_title",
                "turn_trigger": "thread_title",
                "request_kind": "turn",
            },
            output_format=self._title_schema_format(),
        )
        req = self._req()
        self.assertTrue(chatgpt_engine.chatgpt_should_skip_policy(req, payload))
        self.assertFalse(chatgpt_engine.extract(req, payload))

    def test_structured_output_turn_is_skipped_without_turn_metadata(self) -> None:
        # The schema is the signal that survives a renamed thread_source: a
        # turn somebody typed is never answered against a fixed JSON schema.
        payload = self._generation_turn_payload(
            self.TITLE_PROMPT,
            output_format=self._title_schema_format(),
        )
        req = self._req()
        self.assertTrue(chatgpt_engine.chatgpt_should_skip_policy(req, payload))
        self.assertFalse(chatgpt_engine.extract(req, payload))

    def test_ephemeral_generation_sources_are_skipped(self) -> None:
        for source in (
            "thread_title",
            "thread_description",
            "commit_message",
            "pull_request_message",
            "commit_pull_request_message",
        ):
            payload = self._generation_turn_payload(
                "요약해줘",
                metadata={"thread_source": source, "request_kind": "turn"},
            )
            req = self._req()
            self.assertTrue(
                chatgpt_engine.chatgpt_should_skip_policy(req, payload), source
            )
            trigger_only = self._generation_turn_payload(
                "요약해줘",
                metadata={"turn_trigger": source, "request_kind": "turn"},
            )
            req = self._req()
            self.assertTrue(
                chatgpt_engine.chatgpt_should_skip_policy(req, trigger_only), source
            )

    def test_free_text_turn_stays_a_user_prompt(self) -> None:
        payload = self._generation_turn_payload(
            "Codex Test",
            metadata={
                "thread_source": "user",
                "turn_trigger": "user_message",
                "request_kind": "turn",
            },
            output_format={"type": "text"},
        )
        req = self._req()
        self.assertFalse(chatgpt_engine.chatgpt_should_skip_policy(req, payload))
        self.assertEqual(chatgpt_engine.extract(req, payload), "Codex Test")

    def test_title_generation_answer_is_not_logged_as_an_answer(self) -> None:
        conn = "c5102073-ca59-4391-9344-38222827c935"

        def _answer_frame(text: str, response_id: str) -> bytes:
            return self._ws_text_frame(
                json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": response_id,
                            "output": [
                                {
                                    "type": "message",
                                    "content": [{"type": "output_text", "text": text}],
                                }
                            ],
                        },
                    },
                    ensure_ascii=False,
                )
            )

        title = self._req()
        title["unit_type"] = "ws_frame"
        title["request_id"] = f"{conn}-ws-0000000000000011"
        title["_eve_full_body"] = True
        title_frame = self._ws_text_frame(
            self._generation_turn_payload(
                self.TITLE_PROMPT,
                metadata={"thread_source": "thread_title", "request_kind": "turn"},
                output_format=self._title_schema_format(),
            ).decode("utf-8")
        )
        self.assertFalse(chatgpt_engine.extract_full_body(title, title_frame))

        # The JSON title comes back on the same socket. Without the request
        # verdict it would be logged as the answer to the previous turn.
        title_answer = self._req()
        title_answer["direction"] = "response"
        title_answer["unit_type"] = "ws_frame"
        title_answer["request_id"] = conn
        title_answer["_eve_full_body"] = True
        self.assertIsNone(
            chatgpt_engine.extract_full_body(
                title_answer, _answer_frame('{"title":"Review Codex test"}', "resp_t")
            )
        )
        self.assertTrue(title_answer.get("_chatgpt_sse_control_only"))

        # A real turn on that same socket is logged again.
        user = self._req()
        user["unit_type"] = "ws_frame"
        user["request_id"] = f"{conn}-ws-0000000000000012"
        user["_eve_full_body"] = True
        user_frame = self._ws_text_frame(
            self._generation_turn_payload("Codex Test").decode("utf-8")
        )
        self.assertEqual(chatgpt_engine.extract_full_body(user, user_frame), "Codex Test")

        user_answer = self._req()
        user_answer["direction"] = "response"
        user_answer["unit_type"] = "ws_frame"
        user_answer["request_id"] = conn
        user_answer["_eve_full_body"] = True
        self.assertEqual(
            chatgpt_engine.extract_full_body(
                user_answer, _answer_frame("Hi! Codex is working.", "resp_u")
            ),
            "Hi! Codex is working.",
        )

    def test_user_turn_keeps_text_even_when_it_looks_like_an_instruction(self) -> None:
        payload = self._with_turn_metadata(
            self._user_turn_payload(
                "# Overview\nGenerate 0 to 3 hyperpersonalized suggestions"
            ),
            {"thread_source": "user", "request_kind": "turn"},
        )
        req = self._req()
        self.assertFalse(chatgpt_engine.chatgpt_should_skip_policy(req, payload))
        self.assertEqual(
            chatgpt_engine.extract(req, payload),
            "# Overview\nGenerate 0 to 3 hyperpersonalized suggestions",
        )

    def test_tool_call_response_is_not_the_visible_answer(self) -> None:
        req = self._req()
        req["direction"] = "response"
        req["unit_type"] = "sse_event"
        req["content_type"] = "text/event-stream"
        sse = (
            "event: response.output_item.added\n"
            'data: {"type":"response.output_item.added","item":{"type":"custom_tool_call","name":"exec"}}\n\n'
            "event: response.custom_tool_call_input.done\n"
            'data: {"type":"response.custom_tool_call_input.done","input":"{\\"cmd\\":\\"ls\\"}"}\n\n'
            "event: response.completed\n"
            'data: {"type":"response.completed","response":{"id":"resp_2"}}\n\n'
        ).encode("utf-8")
        self.assertIsNone(chatgpt_engine.extract_full_body(req, sse))
        self.assertTrue(req.get("_chatgpt_sse_control_only"))

    def test_codex_tool_preamble_is_not_stored_before_the_followup(self) -> None:
        preamble = "요청하신 PDF 파일들의 실제 파일 크기를 확인하겠습니다."
        answer = "파일 크기는 다음과 같습니다."
        tool_turn = b"".join(
            [
                self._ws_text_frame(
                    json.dumps(
                        {"type": "response.output_text.done", "text": preamble},
                        ensure_ascii=False,
                    )
                ),
                self._ws_text_frame(
                    json.dumps(
                        {
                            "type": "response.custom_tool_call_input.delta",
                            "delta": "{\"cmd\":\"stat\"}",
                        }
                    )
                ),
                self._ws_text_frame(
                    json.dumps(
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "resp_tool",
                                "output": [
                                    {
                                        "type": "message",
                                        "content": [{"type": "output_text", "text": preamble}],
                                    }
                                ],
                            },
                        },
                        ensure_ascii=False,
                    )
                ),
            ]
        )
        req = self._req("/backend-api/codex/responses")
        req["direction"] = "response"
        req["unit_type"] = "ws_frame"
        req["_eve_full_body"] = True
        self.assertIsNone(chatgpt_engine.extract_full_body(req, tool_turn))
        self.assertTrue(req.get("_chatgpt_sse_control_only"))
        self.assertFalse(req.get("_extract_final"))

        follow = self._ws_text_frame(
            json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_after_tool",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": answer}],
                            }
                        ],
                    },
                },
                ensure_ascii=False,
            )
        )
        req2 = self._req("/backend-api/codex/responses")
        req2["direction"] = "response"
        req2["unit_type"] = "ws_frame"
        req2["_eve_full_body"] = True
        self.assertEqual(chatgpt_engine.extract_full_body(req2, follow), answer)
        self.assertTrue(req2.get("_extract_final"))

    def _response_req(self) -> Dict[str, Any]:
        req = self._req("/backend-api/codex/responses")
        req["direction"] = "response"
        req["unit_type"] = "ws_frame"
        req["_eve_full_body"] = True
        return req

    def test_completed_response_with_a_client_tool_is_not_the_answer(self) -> None:
        # The live failure: response.completed arrives alone, carrying both the
        # status sentence and the tool call. The call is not a sibling event.
        status = "현재 제공되는 모델 정보와 명칭을 확인해 보겠습니다."
        answer = "GPT-6.1 Terra Light는 가벼운 추론 모델입니다."
        waiting = self._ws_text_frame(
            json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_wait",
                        "output": [
                            {
                                "type": "reasoning",
                                "summary": [],
                            },
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": status}],
                            },
                            {
                                "type": "custom_tool_call",
                                "call_id": "call_models",
                                "name": "shell",
                                "input": "{}",
                            },
                        ],
                    },
                },
                ensure_ascii=False,
            )
        )
        req = self._response_req()
        self.assertIsNone(chatgpt_engine.extract_full_body(req, waiting))
        self.assertTrue(req.get("_chatgpt_sse_control_only"))
        self.assertFalse(req.get("_extract_final"))

        # The text snapshot can also arrive one frame earlier. It is the same
        # step, so it must not be stored before response.completed.
        early = self._ws_text_frame(
            json.dumps(
                {
                    "type": "response.output_text.done",
                    "response_id": "resp_wait_2",
                    "text": status,
                },
                ensure_ascii=False,
            )
        )
        early_req = self._response_req()
        self.assertIsNone(chatgpt_engine.extract_full_body(early_req, early))
        self.assertFalse(early_req.get("_extract_final"))
        done = self._ws_text_frame(
            json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_wait_2",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": status}],
                            },
                            {
                                "type": "function_call",
                                "call_id": "call_2",
                                "name": "lookup",
                                "arguments": "{}",
                            },
                        ],
                    },
                },
                ensure_ascii=False,
            )
        )
        done_req = self._response_req()
        self.assertIsNone(chatgpt_engine.extract_full_body(done_req, done))
        self.assertTrue(done_req.get("_chatgpt_sse_control_only"))

        final = self._ws_text_frame(
            json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_answer",
                        "output": [
                            {"type": "reasoning", "summary": []},
                            {
                                "type": "web_search_call",
                                "id": "ws_1",
                                "status": "completed",
                            },
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": answer}],
                            },
                        ],
                    },
                },
                ensure_ascii=False,
            )
        )
        final_req = self._response_req()
        self.assertEqual(chatgpt_engine.extract_full_body(final_req, final), answer)
        self.assertTrue(final_req.get("_extract_final"))

    def test_environment_context_is_not_a_user_prompt(self) -> None:
        ctx = (
            "<environment_context>\n"
            "  <current_date>2026-11-06</current_date>\n"
            "  <timezone>Asia/Seoul</timezone>\n"
            "  <filesystem><workspace_roots><root>C:\\Users\\최혜원\\Desktop\\chatGPT\\10.0.37.24</root></workspace_roots></filesystem>\n"
            "  <subagents><agent name=\"/root/approval_postgres\" /></subagents>\n"
            "</environment_context>"
        )
        payload = self._user_turn_payload(ctx)
        req = self._req()
        self.assertTrue(chatgpt_engine.chatgpt_should_skip_policy(req, payload))
        self.assertFalse(chatgpt_engine.extract(req, payload))

    def test_environment_context_does_not_hide_the_typed_request(self) -> None:
        text = (
            "오케이너가 설계한대로 개발해줘\n"
            "<environment_context><current_date>2026-11-06</current_date></environment_context>"
        )
        payload = self._user_turn_payload(text)
        req = self._req()
        self.assertEqual(
            chatgpt_engine.extract(req, payload),
            "오케이너가 설계한대로 개발해줘",
        )

    def test_tool_step_message_is_held_until_the_turn_settles(self) -> None:
        conn = "5fb1c1d5-0a26-4e03-91c3-81a16d91b460"
        prompt_id = conn + "-ws-0000000000000001"
        prompt = "오케이너가 설계한대로 개발해주고 API 명세를 만들어줘"
        user = self._req()
        user["unit_type"] = "ws_frame"
        user["request_id"] = prompt_id
        user["_eve_full_body"] = True
        user_frame = self._ws_text_frame(
            self._generation_turn_payload(prompt).decode("utf-8")
        )
        self.assertEqual(chatgpt_engine.extract_full_body(user, user_frame), prompt)

        status = ("요청하신 API 명세를 작성했습니다.\n" + ("항목 " * 40)).strip()
        waiting = self._ws_text_frame(
            json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_spec",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": status}],
                            },
                            {
                                "type": "custom_tool_call",
                                "call_id": "call_write",
                                "name": "shell",
                                "input": "{}",
                            },
                        ],
                    },
                },
                ensure_ascii=False,
            )
        )
        held_req = self._response_req()
        held_req["request_id"] = conn
        self.assertIsNone(chatgpt_engine.extract_full_body(held_req, waiting))
        self.assertEqual(held_req.get("_codex_held_message_id"), prompt_id)
        self.assertEqual(chatgpt_engine.take_codex_held_answer(prompt_id, status), status)
        self.assertFalse(chatgpt_engine.take_codex_held_answer(prompt_id, status))

        # A later tool-free answer is what gets logged, and it drops the hold.
        chatgpt_engine._hold_codex_answer(prompt_id, status)
        answer = "명세는 아래와 같습니다."
        final = self._ws_text_frame(
            json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_spec_final",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": answer}],
                            }
                        ],
                    },
                },
                ensure_ascii=False,
            )
        )
        final_req = self._response_req()
        final_req["request_id"] = conn
        self.assertEqual(chatgpt_engine.extract_full_body(final_req, final), answer)
        self.assertEqual(final_req.get("_chatgpt_ui_message_id"), prompt_id)
        self.assertFalse(chatgpt_engine.take_codex_held_answer(prompt_id))

    def test_tool_call_frame_suppresses_the_later_completed_snapshot(self) -> None:
        # The call is streamed first. response.completed then repeats only the
        # status sentence, without the call item.
        call = self._ws_text_frame(
            json.dumps(
                {
                    "type": "response.output_item.done",
                    "response_id": "resp_split",
                    "item": {
                        "type": "custom_tool_call",
                        "call_id": "call_split",
                        "name": "shell",
                        "input": "{}",
                    },
                }
            )
        )
        call_req = self._response_req()
        self.assertIsNone(chatgpt_engine.extract_full_body(call_req, call))

        status = self._ws_text_frame(
            json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_split",
                        "output": [
                            {
                                "type": "message",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": "확인해 보겠습니다.",
                                    }
                                ],
                            }
                        ],
                    },
                },
                ensure_ascii=False,
            )
        )
        status_req = self._response_req()
        self.assertIsNone(chatgpt_engine.extract_full_body(status_req, status))
        self.assertTrue(status_req.get("_chatgpt_sse_control_only"))

    def test_codex_turns_on_one_socket_keep_separate_message_ids(self) -> None:
        conn = "44808ed1-c4da-4c03-943b-e992a4b29de8"
        first_id = f"{conn}-ws-0000000000000001"
        second_id = f"{conn}-ws-0000000000000002"

        def _prompt_frame(text: str) -> bytes:
            body = {
                "type": "response.create",
                "model": "gpt-5.4",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": text}],
                    }
                ],
            }
            return self._ws_text_frame(json.dumps(body, ensure_ascii=False))

        def _answer_frame(text: str, response_id: str) -> bytes:
            return self._ws_text_frame(
                json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": response_id,
                            "output": [
                                {
                                    "type": "message",
                                    "content": [{"type": "output_text", "text": text}],
                                }
                            ],
                        },
                    },
                    ensure_ascii=False,
                )
            )

        first = self._req("/backend-api/codex/responses")
        first["unit_type"] = "ws_frame"
        first["request_id"] = first_id
        first["_eve_full_body"] = True
        self.assertEqual(
            chatgpt_engine.extract_full_body(first, _prompt_frame("첫번째 질문")),
            "첫번째 질문",
        )
        self.assertEqual(first.get("_chatgpt_ui_message_id"), first_id)

        first_answer = self._req("/backend-api/codex/responses")
        first_answer["direction"] = "response"
        first_answer["unit_type"] = "ws_frame"
        first_answer["request_id"] = conn
        first_answer["_eve_full_body"] = True
        self.assertEqual(
            chatgpt_engine.extract_full_body(first_answer, _answer_frame("첫번째 답", "resp_a")),
            "첫번째 답",
        )
        self.assertEqual(first_answer.get("_chatgpt_ui_message_id"), first_id)

        second = self._req("/backend-api/codex/responses")
        second["unit_type"] = "ws_frame"
        second["request_id"] = second_id
        second["_eve_full_body"] = True
        self.assertEqual(
            chatgpt_engine.extract_full_body(second, _prompt_frame("두번째 질문")),
            "두번째 질문",
        )
        self.assertEqual(second.get("_chatgpt_ui_message_id"), second_id)

        second_answer = self._req("/backend-api/codex/responses")
        second_answer["direction"] = "response"
        second_answer["unit_type"] = "ws_frame"
        second_answer["request_id"] = conn
        second_answer["_eve_full_body"] = True
        self.assertEqual(
            chatgpt_engine.extract_full_body(
                second_answer, _answer_frame("두번째 답", "resp_b")
            ),
            "두번째 답",
        )
        self.assertEqual(second_answer.get("_chatgpt_ui_message_id"), second_id)

    def test_real_request_after_overview_is_kept(self) -> None:
        wrapped = (
            "# Overview\n\n"
            "Generate 0 to 3 hyperpersonalized suggestions\n\n"
            "# My request\n이 로그가 왜 남았는지 봐줘"
        )
        payload = self._user_turn_payload(wrapped)
        req = self._req()
        self.assertFalse(chatgpt_engine.chatgpt_should_skip_policy(req, payload))
        self.assertEqual(chatgpt_engine.extract(req, payload), "이 로그가 왜 남았는지 봐줘")

    def test_tool_followup_is_skipped(self) -> None:
        body = {
            "model": "gpt-5",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "# My request\nAIVAX-APP-GPT-0921"}],
                },
                {"type": "function_call", "name": "shell", "call_id": "call_1"},
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "ok",
                },
            ],
        }
        payload = json.dumps(body).encode("utf-8")
        req = self._req()
        self.assertTrue(chatgpt_engine.chatgpt_should_skip_policy(req, payload))

    def test_zstd_request_body_reaches_policy(self) -> None:
        import asyncio
        from unittest.mock import AsyncMock

        import zstandard

        prompt = "GPT 6.1 Luna High 설명해주세요."
        payload = zstandard.ZstdCompressor().compress(self._user_turn_payload(prompt))
        req = self._req()
        req.update(
            {
                "request_id": "rid-codex-zstd",
                "service_id": "gpt",
                "content_encoding": "zstd",
            }
        )
        req["meta"]["client_ip"] = "10.0.50.60"
        eng = ai_policy_engine.PolicyEngine(
            keywords=[],
            payload_preview_bytes=64,
            log_payload=False,
            rewrite_from="",
            rewrite_to="",
            adapters_dir=str(_ROOT / "adapters"),
            adapters_hot_reload=False,
            chatgpt_force_request_text="",
            chatgpt_block_sysinstruct=True,
            pipeline_server_url="http://127.0.0.1:9",
            pipeline_server_port=9,
            pipeline_input_api_path="/in",
            pipeline_output_api_path="/out",
            policy_api_timeout_ms=100,
            policy_api_max_inflight=1,
            policy_api_pool_limit=1,
            policy_api_acquire_timeout_ms=100,
            policy_api_failure_policy="open",
        )
        eng._call_policy_api = AsyncMock(  # type: ignore[method-assign]
            return_value=("PASS", "", "")
        )
        prev_gate = dict(ai_policy_engine._SERVICE_GATE)
        ai_policy_engine._SERVICE_GATE = {"gpt": "inspect_on"}
        try:
            out = asyncio.run(eng.evaluate(req, payload))
        finally:
            ai_policy_engine._SERVICE_GATE = prev_gate
        self.assertNotEqual(out.get("reason"), "policy_api:chatgpt_non_prompt")
        eng._call_policy_api.assert_awaited()
        self.assertIn(prompt, json.dumps(eng._call_policy_api.await_args, default=str, ensure_ascii=False))

    def test_block_rewrites_current_turn_only(self) -> None:
        wrapped = "# My request\nAIVAX-APP-GPT-0921"
        payload = self._user_turn_payload(wrapped, with_image=True)
        req = self._req()
        out = chatgpt_engine.rewrite_prompt_for_block(
            req, payload, "차단되었습니다.", "do not answer"
        )
        self.assertIsNotNone(out)
        body, reason = out
        self.assertIn("adapter:chatgpt_policy_block_sysinstruct", reason)
        obj = json.loads(body.decode("utf-8"))
        last = obj["input"][-1]
        texts = [
            p.get("text")
            for p in last.get("content") or []
            if isinstance(p, dict) and p.get("type") == "input_text"
        ]
        self.assertTrue(any("차단되었습니다." in str(t) for t in texts))
        self.assertFalse(any("AIVAX-APP-GPT-0921" in str(t) for t in texts))
        self.assertFalse(
            any(
                isinstance(p, dict) and p.get("type") == "input_image"
                for p in last.get("content") or []
            )
        )
        self.assertEqual(obj["input"][0]["role"], "developer")

    def test_responses_sse_extracts_output_text(self) -> None:
        sse = (
            "event: response.output_text.delta\n"
            'data: {"type":"response.output_text.delta","delta":"Hello "}\n\n'
            "event: response.output_text.delta\n"
            'data: {"type":"response.output_text.delta","delta":"World"}\n\n'
            "event: response.completed\n"
            "data: {\"type\":\"response.completed\",\"response\":{"
            "\"id\":\"resp_1\",\"output\":[{\"type\":\"message\",\"role\":\"assistant\","
            "\"content\":[{\"type\":\"output_text\",\"text\":\"Hello World\"}]}]}}\n\n"
        ).encode("utf-8")
        req = {
            "direction": "response",
            "unit_type": "sse_event",
            "content_type": "text/event-stream",
            "request_id": "rid-codex-1",
            "meta": {"host": "chatgpt.com", "path": "/backend-api/codex/responses"},
        }
        out = chatgpt_engine.extract_full_body(req, sse)
        self.assertEqual(out, "Hello World")

    @staticmethod
    def _ws_text_frame(text: str) -> bytes:
        data = text.encode("utf-8")
        length = len(data)
        if length < 126:
            header = bytes([0x81, length])
        else:
            header = bytes([0x81, 126]) + length.to_bytes(2, "big")
        return header + data

    def test_api_responses_path_is_a_codex_turn(self) -> None:
        self.assertTrue(chatgpt_engine.is_chatgpt_conversation_turn_path("/v1/responses"))
        self.assertTrue(
            chatgpt_engine.is_chatgpt_conversation_turn_path("/v1/responses?foo=1")
        )
        self.assertFalse(
            chatgpt_engine.is_chatgpt_conversation_turn_path("/v1/responses/resp_123")
        )
        self.assertTrue(
            ai_policy_engine._is_conversation_path("gpt", "/v1/responses")
        )

    def test_codex_plugin_websocket_prompt_and_answer(self) -> None:
        prompt = "나를 찾아봐!"
        answer = "어디에서 찾을까요?"
        body = {
            "type": "response.create",
            "model": "gpt-5.4",
            "stream": True,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                }
            ],
        }
        frame = self._ws_text_frame(json.dumps(body, ensure_ascii=False))
        req = self._req("/v1/responses")
        req["unit_type"] = "ws_frame"
        req["meta"]["host"] = "api.openai.com"
        req["_eve_full_body"] = True
        self.assertFalse(chatgpt_engine.chatgpt_should_skip_policy(req, frame))
        self.assertEqual(chatgpt_engine.extract_full_body(req, frame), prompt)
        self.assertEqual(req.get("_llm_model"), "gpt-5.4")
        pol = chatgpt_engine.build_policy_api_payload(req, frame, prompt)
        self.assertIsInstance(pol, dict)
        self.assertEqual(pol.get("prompt"), prompt)

        delta = self._ws_text_frame(
            json.dumps(
                {
                    "type": "response.output_text.delta",
                    "response_id": "resp_plugin_1",
                    "delta": answer,
                },
                ensure_ascii=False,
            )
        )
        done = self._ws_text_frame(
            json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_plugin_1",
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": answer}],
                            }
                        ],
                    },
                },
                ensure_ascii=False,
            )
        )
        resp = self._req("/v1/responses")
        resp["direction"] = "response"
        resp["unit_type"] = "ws_frame"
        resp["meta"]["host"] = "api.openai.com"
        self.assertIsNone(chatgpt_engine.extract_full_body(resp, delta))
        self.assertFalse(resp.get("_extract_final"))
        self.assertEqual(chatgpt_engine.extract_full_body(resp, done), answer)
        self.assertTrue(resp.get("_extract_final"))

    def test_codex_websocket_ping_and_prewarm_are_control(self) -> None:
        ping = bytes([0x89, 0x84]) + b"\x01\x02\x03\x04" + b"abcd"
        prewarm = self._ws_text_frame(
            json.dumps({"type": "response.create", "model": "gpt-5.4", "generate": False, "input": []})
        )
        for payload in (ping, prewarm):
            req = self._req("/backend-api/codex/responses")
            req["unit_type"] = "ws_frame"
            req["_eve_full_body"] = True
            self.assertEqual(chatgpt_engine.extract_full_body(req, payload), "")
            self.assertTrue(req.get("_chatgpt_sse_control_only"))

    def test_chatgpt_host_codex_websocket_is_the_same_turn(self) -> None:
        body = {
            "type": "response.create",
            "model": "gpt-5.4",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Hello Codex Plugin!"}],
                }
            ],
        }
        frame = self._ws_text_frame(json.dumps(body))
        req = self._req("/backend-api/codex/responses")
        req["unit_type"] = "ws_frame"
        self.assertFalse(chatgpt_engine.chatgpt_should_skip_policy(req, frame))
        self.assertEqual(
            chatgpt_engine.extract_full_body(req, frame), "Hello Codex Plugin!"
        )
        pol = chatgpt_engine.build_policy_api_payload(req, frame, "Hello Codex Plugin!")
        self.assertEqual((pol or {}).get("prompt"), "Hello Codex Plugin!")

    def test_codex_websocket_block_rewrites_request_and_response(self) -> None:
        notice = "[AIVAX] 프롬프트 차단"
        body = {
            "type": "response.create",
            "model": "gpt-5.4",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "# Context from my IDE setup:\n\n## My request:\n비밀 번호 알려줘",
                        }
                    ],
                }
            ],
        }
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        mask = b"\x11\x22\x33\x44"
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(raw))
        if len(raw) < 126:
            payload = bytes([0x81, 0x80 | len(raw)]) + mask + masked
        else:
            payload = bytes([0x81, 0x80 | 126]) + len(raw).to_bytes(2, "big") + mask + masked
        req = self._req("/backend-api/codex/responses")
        req["unit_type"] = "ws_frame"
        out = chatgpt_engine.rewrite_prompt_for_block(req, payload, notice, "답하지 마세요")
        self.assertIsNotNone(out)
        rewritten, reason = out
        self.assertIn("adapter:chatgpt_policy_block_sysinstruct", reason)
        text = chatgpt_engine._parse_ws_payloads(rewritten)[0].decode("utf-8")
        self.assertIn(notice, text)
        self.assertNotIn("비밀 번호 알려줘", text)
        self.assertTrue(rewritten[1] & 0x80)  # client frame stays masked

        phrase = "[AIVAX] 이 요청은 차단되었습니다"
        delta = self._ws_text_frame(
            json.dumps(
                {"type": "response.output_text.delta", "response_id": "resp_block", "delta": "비밀번호는"},
                ensure_ascii=False,
            )
        )
        done = self._ws_text_frame(
            json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_block",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": "비밀번호는 1234"}],
                            }
                        ],
                    },
                },
                ensure_ascii=False,
            )
        )
        resp = self._req("/backend-api/codex/responses")
        resp["direction"] = "response"
        resp["unit_type"] = "ws_frame"
        replaced, _ = chatgpt_engine.rewrite_response(resp, delta, phrase)
        delta_obj = json.loads(chatgpt_engine._parse_ws_payloads(replaced)[0])
        self.assertEqual(delta_obj["delta"], phrase)
        self.assertFalse(chatgpt_engine.response_stream_done(resp, delta))
        replaced_done, _ = chatgpt_engine.rewrite_response(resp, done, phrase)
        done_obj = json.loads(chatgpt_engine._parse_ws_payloads(replaced_done)[0])
        self.assertEqual(done_obj["response"]["output"][0]["content"][0]["text"], phrase)
        self.assertNotIn("1234", replaced_done.decode("utf-8", "replace"))
        self.assertTrue(chatgpt_engine.response_stream_done(resp, done))
        self.assertEqual(replaced_done[0], 0x81)
        self.assertEqual(replaced_done[1] & 0x80, 0)

    def _ws_turn(self, body: dict, request_id: str) -> dict:
        frame = self._ws_text_frame(json.dumps(body, ensure_ascii=False))
        req = self._req("/backend-api/codex/responses")
        req["unit_type"] = "ws_frame"
        req["request_id"] = request_id
        req["_eve_full_body"] = True
        req["_frame"] = frame
        return req

    def test_ide_selection_and_image_are_extracted_files(self) -> None:
        prompt = "이 파일 검토해줘"
        body = {
            "type": "response.create",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "# Context from my IDE setup:\n\n"
                                "## Active file: notes/비밀.txt\n\n"
                                "## Active selection of the file:\n"
                                "계좌 비밀번호 1234\n"
                                "## Open tabs:\n"
                                "- notes/비밀.txt: notes/비밀.txt\n\n"
                                f"## My request:\n{prompt}"
                            ),
                        },
                        {
                            "type": "input_text",
                            "text": '<image>[Image #1] path="shots/화면.png">',
                        },
                        {
                            "type": "input_image",
                            "image_url": f"data:image/png;base64,{self.PNG_B64}",
                        },
                    ],
                }
            ],
        }
        req = self._ws_turn(body, "44808ed1-c4da-4c03-943b-e992a4b29de8-ws-0000000000000001")
        frame = req.pop("_frame")
        self.assertFalse(chatgpt_engine.chatgpt_should_skip_policy(req, frame))
        self.assertEqual(chatgpt_engine.extract_full_body(req, frame), prompt)
        pol = chatgpt_engine.build_policy_api_payload(req, frame, prompt)
        self.assertEqual((pol or {}).get("prompt"), prompt)
        names = sorted(str(item.get("name") or "") for item in (pol or {}).get("attachments") or [])
        self.assertEqual(names, ["비밀.txt", "화면.png"])
        for item in pol["attachments"]:
            self.assertTrue(os.path.isfile(item["id"]))
            self.assertGreater(item["size"], 0)

    def test_tool_read_is_attached_to_the_prompt_turn(self) -> None:
        conn = "44808ed1-c4da-4c03-943b-e992a4b29de8"
        prompt_id = conn + "-ws-000000000000000a"
        prompt = "파일 내용 확인해줘"
        user = {
            "type": "response.create",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": f"## My request:\n{prompt}"}
                    ],
                }
            ],
        }
        req = self._ws_turn(user, prompt_id)
        frame = req.pop("_frame")
        self.assertEqual(chatgpt_engine.extract_full_body(req, frame), prompt)

        file_body = "인공지능 안전성 가이드라인 본문\n" + ("가나다라 " * 40)
        follow = {
            "type": "response.create",
            "input": [
                {
                    "type": "function_call",
                    "name": "shell",
                    "call_id": "call_pdf",
                    "arguments": json.dumps(
                        {"command": "pdftotext 'docs/안전성.pdf' -"},
                        ensure_ascii=False,
                    ),
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_pdf",
                    "output": file_body,
                },
            ],
        }
        follow_req = self._ws_turn(follow, conn + "-ws-000000000000000b")
        follow_frame = follow_req.pop("_frame")
        self.assertFalse(chatgpt_engine.chatgpt_should_skip_policy(follow_req, follow_frame))
        self.assertEqual(chatgpt_engine.extract_full_body(follow_req, follow_frame), "")
        pol = chatgpt_engine.build_policy_api_payload(follow_req, follow_frame, "")
        self.assertEqual((pol or {}).get("prompt"), prompt)
        self.assertEqual(follow_req.get("_chatgpt_ui_message_id"), prompt_id)
        names = [item.get("name") for item in (pol or {}).get("attachments") or []]
        self.assertEqual(names, ["안전성.pdf"])
        saved = (pol or {})["attachments"][0]["id"]
        self.assertIn("인공지능 안전성 가이드라인 본문", open(saved, encoding="utf-8").read())

    def test_tool_size_result_is_not_a_file(self) -> None:
        body = {
            "type": "response.create",
            "input": [
                {
                    "type": "function_call",
                    "name": "shell",
                    "call_id": "call_size",
                    "arguments": json.dumps(
                        {"command": "python -c \"import os; print(os.path.getsize('docs/안전성.pdf'))\""}
                    ),
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_size",
                    "output": "3857135\n",
                },
            ],
        }
        req = self._ws_turn(body, "44808ed1-c4da-4c03-943b-e992a4b29de8-ws-000000000000000c")
        frame = req.pop("_frame")
        self.assertTrue(chatgpt_engine.chatgpt_should_skip_policy(req, frame))


class TestAttachmentResolution(unittest.TestCase):
    def test_policy_attachment_waits_for_late_tmpfs_file(self) -> None:
        attachment = {
            "id": "/run/aivax/attachments/file-1",
            "size": 12,
            "name": "doc.pdf",
            "mime_type": "application/pdf",
        }
        with mock.patch.object(ai_policy_engine, "get_saved_attachment", return_value=None):
            with mock.patch.object(
                ai_policy_engine,
                "wait_for_saved_attachment",
                return_value=attachment,
            ) as wait:
                result = ai_policy_engine.resolve_saved_policy_attachments(
                    [{"id": "file-1", "name": "doc.pdf", "mime_type": "application/pdf"}]
                )
        self.assertEqual(result[0]["id"], attachment["id"])
        wait.assert_called_once()


class TestRepeatedAnswerDedup(unittest.TestCase):
    def test_same_answer_on_another_turn_is_not_the_same_key(self) -> None:
        key = ai_policy_engine.PolicyEngine._policy_cross_source_dedup_key
        first = key(
            "gpt",
            "response",
            "chatgpt.com",
            "/backend-api/f/conversation",
            "ㄹㅇㅋㅋ",
            "mid-1",
        )
        second = key(
            "gpt",
            "response",
            "chatgpt.com",
            "/backend-api/f/conversation",
            "ㄹㅇㅋㅋ",
            "mid-2",
        )
        same_turn = key(
            "gpt",
            "response",
            "chatgpt.com",
            "/backend-api/f/conversation",
            "ㄹㅇㅋㅋ",
            "mid-1",
        )
        self.assertNotEqual(first, second)
        self.assertEqual(first, same_turn)


_HELLO_WORLD_THOUGHT = (
    "Interpreting the Input\n\n"
    "I've begun dissecting the user's input. "
    '"Hello World!\\n010-947이-일이삼이" presents an interesting mix. '
    "I immediately translated the Korean characters into the corresponding numerical form. "
    "So, the key takeaway is the potential phone number: 010-9472-1232. "
    "I need to figure out how to correctly interpret the non-numeric part."
)


def _gemini_wrb_body(inner: Any) -> bytes:
    frame = json.dumps(
        [["wrb.fr", None, json.dumps(inner, ensure_ascii=False)]],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f")]}}'\n\n{len(frame)}\n{frame}\n".encode("utf-8")


_KOREAN_ANSWER = (
    "안녕하세요! 반가워요.\n\n"
    "(남겨주신 번호는 010-9472-1232네요!)\n\n"
    "기안서 작성은 잘 마무리되셨나요? 또 다른 문서 작성이나 필요한 작업이 있다면 편하게 말씀해 주세요."
)


def _gemini_stream_req() -> Dict[str, Any]:
    return {
        "direction": "response",
        "request_id": "46974649-f2b4-467f-ba12-4aa6dbe240a5",
        "content_type": "application/x-www-form-urlencoded",
        "meta": {
            "host": "gemini.google.com",
            "path": "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate",
        },
    }


def _rc_with_thoughts(answer_chunks: list, thoughts: str, rendered: list | None = None) -> list:
    node: list = ["rc_live", answer_chunks]
    node.extend([None] * (gemini_engine._GEMINI_RC_THOUGHTS_INDEX - 2))
    node.append([[thoughts]] if thoughts else None)
    if rendered:
        node[4] = rendered
    return node


class TestGeminiThoughtSummary(unittest.TestCase):
    def test_answer_sentence_and_file_name_are_both_kept(self) -> None:
        answer = "문서 내용을 요약하면 결재가 필요합니다."
        filename = "fdoc_sample_3_O_block_test123.docx"
        node = ["rc_live", [answer], None, None, [[None, 0, filename]]]
        text = gemini_engine._visible_text_from_rc_node(node)
        self.assertIn(answer, text or "")
        self.assertIn(filename, text or "")

    def test_rendered_reply_is_kept_and_plain_thought_is_not(self) -> None:
        rendered = [
            [None, 0, "안녕하세요! 반가워요."],
            [None, 0, "(남겨주신 번호는 010-9472-1232네요!)"],
            [None, 0, "기안서 작성은 잘 마무리되셨나요? 또 다른 문서 작성이나 필요한 작업이 있다면 편하게 말씀해 주세요."],
        ]
        node = _rc_with_thoughts([_HELLO_WORLD_THOUGHT], "", rendered)
        inner = [None, ["c_a833a2a0a067b40e", "r_1"], None, None, [node]]
        extracted = gemini_engine.extract_full_body(
            _gemini_stream_req(), _gemini_wrb_body(inner)
        )
        self.assertEqual(extracted, _KOREAN_ANSWER)

    def test_thought_slot_is_not_the_reply(self) -> None:
        answer = "The number written with Korean digits is 010-9472-1232."
        node = _rc_with_thoughts([answer], _HELLO_WORLD_THOUGHT)
        inner = [None, ["c_a833a2a0a067b40e", "r_1"], None, None, [node]]
        extracted = gemini_engine.extract_full_body(
            _gemini_stream_req(), _gemini_wrb_body(inner)
        )
        self.assertEqual(extracted, answer)
        self.assertNotIn("Interpreting the Input", extracted or "")

    def test_english_reply_with_thought_wording_is_kept(self) -> None:
        answer = (
            "I've begun dissecting the request. "
            "Interpreting the Input as a phone number gives 010-9472-1232."
        )
        node = ["rc_live", [answer]]
        inner = [None, ["c_a833a2a0a067b40e", "r_1"], None, None, [node]]
        extracted = gemini_engine.extract_full_body(
            _gemini_stream_req(), _gemini_wrb_body(inner)
        )
        self.assertEqual(extracted, answer)

    def test_handshake_without_a_candidate_gets_the_card(self) -> None:
        handshake = [None, ["c_8be1932b40ab3866", "r_3385d07f7b99d3b5"], {"44": True}]
        thought = [{"37": [0]}]
        frames = [
            json.dumps(
                [["wrb.fr", None, json.dumps(handshake, ensure_ascii=False)]],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            json.dumps(
                [["wrb.fr", None, json.dumps(thought, ensure_ascii=False)]],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        ]
        payload = gemini_engine._reassemble_gemini_transport_body(
            frames, xssi_prefix=")]}'\n\n"
        )
        req = _gemini_stream_req()
        rewritten, reason = gemini_engine.rewrite_response(
            req, payload, "### [AIVAX] 프롬프트 차단\n\n잠금 😀"
        )
        self.assertEqual(reason, "adapter:gemini_policy_block_message_synth")
        text = gemini_engine.extract_full_body(req, rewritten)
        self.assertIn("프롬프트 차단", text)
        self.assertIn("잠금 😀", text)

    def test_empty_candidate_text_is_filled_in_place(self) -> None:
        node = ["rc_live", [""]]
        node.extend([None] * 36)
        inner = [None] * 64
        inner[1] = ["c_8be1932b40ab3866", "r_1"]
        inner[4] = [node]
        inner[42] = "3.6 Flash"
        frame = json.dumps(
            [["wrb.fr", None, json.dumps(inner, ensure_ascii=False)]],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload = gemini_engine._reassemble_gemini_transport_body(
            [frame], xssi_prefix=")]}'\n\n"
        )
        req = _gemini_stream_req()
        rewritten, reason = gemini_engine.rewrite_response(req, payload, "보이는 카드")
        self.assertEqual(reason, "adapter:gemini_policy_block_message")
        self.assertEqual(gemini_engine.extract_full_body(req, rewritten), "보이는 카드")
        self.assertIn(b"3.6 Flash", rewritten)

    def test_thought_slot_alone_is_not_logged(self) -> None:
        node = _rc_with_thoughts([], _HELLO_WORLD_THOUGHT)
        inner = [None, ["c_a833a2a0a067b40e", "r_1"], None, None, [node]]
        req = _gemini_stream_req()
        extracted = gemini_engine.extract_full_body(req, _gemini_wrb_body(inner))
        self.assertFalse(extracted)
        self.assertTrue(req.get("_gemini_thinking_only"))

    def test_thinking_stub_session_joins_the_later_answer(self) -> None:
        gemini_engine._GEMINI_SESSION_PARENT.pop("c_newchat00000001", None)
        stub = [None, ["c_newchat00000001", "r_stub"], {"44": True}]
        stub_req = _gemini_stream_req()
        stub_req["request_id"] = "prompt-turn"
        self.assertFalse(
            gemini_engine.extract_full_body(stub_req, _gemini_wrb_body(stub))
        )
        self.assertEqual(
            gemini_engine.resolve_gemini_parent_request_id("c_newchat00000001"),
            "prompt-turn",
        )

        answer = [None, ["c_newchat00000001", "r_answer"], None, None, [["rc_live", ["나중 답변"]]]]
        answer_req = _gemini_stream_req()
        answer_req["request_id"] = "later-turn"
        self.assertEqual(
            gemini_engine.extract_full_body(answer_req, _gemini_wrb_body(answer)),
            "나중 답변",
        )
        self.assertEqual(answer_req.get("_gemini_ui_message_id"), "prompt-turn")


if __name__ == "__main__":
    unittest.main()
