#!/usr/bin/env python3
"""Grok /ws/mgw response.create carries user prompt + file mentions."""
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

from adapters import grok_engine  # noqa: E402
from adapters.grok_upload import _collect_attachment_refs  # noqa: E402


FILE_UUID = "24c23fd9-a2ca-420c-b502-fca5f693e726"


def _ws_req() -> dict:
    return {
        "direction": "request",
        "unit_type": "ws_frame",
        "request_id": "rid-ws-0001",
        "meta": {"host": "grok.com", "path": "/ws/mgw/?uid=user-1"},
    }


def _frame(obj: dict) -> bytes:
    return grok_engine._encode_ws_text_frame(
        json.dumps(obj, ensure_ascii=False).encode("utf-8"),
        masked=True,
    )


class TestGrokWsResponseCreate(unittest.TestCase):
    def test_extracts_long_plugin_assembled_final_json(self) -> None:
        text = "긴 Grok 응답입니다. " * 80
        payload = json.dumps(
            {
                "session_id": "sess-1",
                "response_id": "resp-1",
                "aivax_ws_final": True,
                "text": text,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertGreater(len(payload), 895)
        req = {
            "direction": "response",
            "unit_type": "json_body",
            "request_id": "rid-ws-final",
            "meta": {"host": "grok.com", "path": "/ws/mgw/?uid=user-1"},
        }
        self.assertEqual(grok_engine.extract(req, payload), text)
        self.assertTrue(req.get("_extract_final"))
        self.assertEqual(req.get("_grok_output_session_id"), "sess-1")

    def test_extracts_prompt_from_response_create(self) -> None:
        obj = {
            "session_id": "sess-1",
            "event": {
                "type": "response.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "x_grok": {
                        "input_chunks": [{"text": {"text": "신청서 작성하는거 도와줄수있어!?!?"}}],
                    },
                },
            },
        }
        req = _ws_req()
        text = grok_engine.extract(req, _frame(obj))
        self.assertEqual("신청서 작성하는거 도와줄수있어!?!?", text)
        self.assertFalse(req.get("_grok_ws_skip"))
        self.assertFalse(grok_engine.grok_should_skip_policy(req, _frame(obj)))

    def test_extracts_file_mention_on_response_create(self) -> None:
        obj = {
            "session_id": "sess-1",
            "event": {
                "type": "response.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "x_grok": {
                        "input_chunks": [
                            {"text": {"text": "이 이미지 봐줘"}},
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
        self.assertEqual(_collect_attachment_refs(obj), [FILE_UUID])
        req = _ws_req()
        text = grok_engine.extract(req, _frame(obj))
        self.assertEqual("이 이미지 봐줘", text)
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
        self.assertEqual((policy or {}).get("attachments")[0]["name"], "img.jpg")

    def test_skips_empty_response_create_and_session_create(self) -> None:
        empty_create = {
            "session_id": "sess-1",
            "event": {"type": "response.create", "castle_request_token": "x" * 80},
        }
        session = {"event": {"type": "session.create", "session": {"model": "fast"}}}
        req = _ws_req()
        self.assertEqual("", grok_engine.extract(req, _frame(empty_create)))
        self.assertTrue(req.get("_grok_ws_skip"))
        req2 = _ws_req()
        self.assertEqual("", grok_engine.extract(req2, _frame(session)))
        self.assertTrue(req2.get("_grok_ws_skip"))
        self.assertTrue(grok_engine.grok_should_skip_policy(req2, _frame(session)))

    def test_session_create_model_is_recorded_on_the_next_prompt(self) -> None:
        grok_engine._GROK_SESSION_MODEL.clear()
        conn = "8e5d367a-4cd4-48ac-b047-5b3511390c61"
        create_req = _ws_req()
        create_req["request_id"] = conn + "-ws-0001"
        create = {"event": {"type": "session.create", "session": {"model": "fast"}}}
        self.assertEqual("", grok_engine.extract(create_req, _frame(create)))
        self.assertTrue(create_req.get("_grok_ws_skip"))

        prompt_req = _ws_req()
        prompt_req["request_id"] = conn + "-ws-0002"
        prompt = {
            "session_id": "ad0f4682-ed8e-4ad7-bdb8-9bca1cc1105f",
            "event": {
                "type": "response.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "x_grok": {"input_chunks": [{"text": {"text": "가능하려나!?!?"}}]},
                },
            },
        }
        self.assertEqual("가능하려나!?!?", grok_engine.extract(prompt_req, _frame(prompt)))
        self.assertEqual("fast", prompt_req.get("_grok_model"))

        from core import ai_policy_engine

        pol: dict = {}
        ai_policy_engine.stamp_policy_model(prompt_req, b"", pol, grok_engine)
        self.assertEqual("fast", pol.get("model"))

    def test_sharing_and_list_rest_are_not_chat_turns(self) -> None:
        sharing = {
            "direction": "request",
            "meta": {
                "host": "grok.com",
                "path": "/rest/app-chat/conversations/67d50028-602e-441b-836c-e59dd20fd960/sharing?responseId=abc",
            },
        }
        self.assertFalse(
            grok_engine.is_grok_conversation_path(sharing["meta"]["path"])
        )
        self.assertTrue(grok_engine.grok_should_skip_policy(sharing, b""))
        self.assertTrue(
            grok_engine.is_grok_conversation_path("/ws/mgw/?uid=x")
        )
        self.assertTrue(
            grok_engine.is_grok_conversation_path(
                "/rest/app-chat/conversations/67d50028-602e-441b-836c-e59dd20fd960/responses"
            )
        )
        self.assertTrue(
            grok_engine.is_grok_conversation_path(
                "/rest/app-chat/conversations/67d50028-602e-441b-836c-e59dd20fd960/load-responses"
            )
        )
        # REQUEST still auxiliary (empty 95B body) — RESPONSE must extract.
        load_req = {
            "direction": "request",
            "meta": {
                "host": "grok.com",
                "path": "/rest/app-chat/conversations/67d50028-602e-441b-836c-e59dd20fd960/load-responses",
            },
        }
        self.assertTrue(grok_engine._is_grok_auxiliary_request(load_req, b'{"cursor":""}'))

    def test_recent_upload_binds_on_text_only_response_create(self) -> None:
        from adapters import grok_upload

        req = {
            "direction": "request",
            "unit_type": "ws_frame",
            "request_id": "rid-ws-recent",
            "meta": {
                "host": "grok.com",
                "path": "/ws/mgw/?uid=user-1",
                "x_userid": "user-1",
            },
        }
        grok_upload._RECENT_CHAT_FILES_BY_SCOPE.clear()
        grok_upload.remember_recent_chat_file(req, FILE_UUID)
        obj = {
            "session_id": "sess-1",
            "event": {
                "type": "response.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "x_grok": {
                        "input_chunks": [{"text": {"text": "이 파일 요약해줘"}}],
                    },
                },
            },
        }
        text = grok_engine.extract(req, _frame(obj))
        self.assertEqual("이 파일 요약해줘", text)
        with mock.patch.object(grok_engine, "build_policy_attachments") as build_att:
            build_att.return_value = [
                {
                    "id": f"/run/aivax/attachments/{FILE_UUID}",
                    "size": 10,
                    "name": "신청서.pdf",
                    "mime_type": "application/pdf",
                }
            ]
            policy = grok_engine.build_policy_api_payload(req, _frame(obj), text)
        self.assertEqual((policy or {}).get("attachments")[0]["name"], "신청서.pdf")

    def test_skips_non_user_event_types_even_with_text(self) -> None:
        """Only response.create / conversation.item.create are prompt sources."""
        for et, phrase in (
            ("response.chunk", "Thinking about your request"),
            ("response.chunk", "Checking the system response"),
            ("response.created", "in_progress"),
            ("response.persisted", "done"),
            ("session.created", "fast"),
        ):
            obj = {
                "session_id": "sess-1",
                "event": {
                    "type": et,
                    "event_id": "evt_1",
                    "response_id": "resp-1",
                    "chunk": {
                        "text": {
                            "channel": "CHANNEL_ASSISTANT_PROGRESS",
                            "text": phrase,
                        }
                    },
                    "text": phrase,
                },
            }
            self.assertTrue(grok_engine._is_grok_ws_control_event(obj), et)
            self.assertEqual("", grok_engine._extract_prompt(obj), et)
            req = _ws_req()
            self.assertEqual("", grok_engine.extract(req, _frame(obj)), et)
            self.assertTrue(req.get("_grok_ws_skip"), et)
            self.assertTrue(grok_engine.grok_should_skip_policy(req, _frame(obj)), et)

    def test_conversation_item_create_is_user_turn(self) -> None:
        obj = {
            "session_id": "sess-1",
            "event": {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "x_grok": {
                        "input_chunks": [{"text": {"text": "아이템 생성 프롬프트"}}],
                    },
                },
            },
        }
        req = _ws_req()
        self.assertEqual("아이템 생성 프롬프트", grok_engine.extract(req, _frame(obj)))
        self.assertFalse(req.get("_grok_ws_skip"))

    def test_skips_response_created_lifecycle_as_request(self) -> None:
        obj = {
            "session_id": "sess-1",
            "event": {
                "type": "response.created",
                "response": {"id": "resp-1", "status": "in_progress"},
            },
        }
        req = _ws_req()
        self.assertEqual("", grok_engine.extract(req, _frame(obj)))
        self.assertTrue(req.get("_grok_ws_skip"))

    def test_inflates_rsv1_permessage_deflate_frame(self) -> None:
        import zlib

        body = json.dumps(
            {
                "session_id": "sess-1",
                "event": {
                    "type": "response.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "x_grok": {
                            "input_chunks": [{"text": {"text": "압축된 프롬프트"}}],
                        },
                    },
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        compressed = compressor.compress(body) + compressor.flush()
        if compressed.endswith(b"\x00\x00\xff\xff"):
            compressed = compressed[:-4]
        header = bytearray([0xC1, 0x80 | 126])
        header.extend(len(compressed).to_bytes(2, "big"))
        mask = b"\x11\x22\x33\x44"
        header.extend(mask)
        masked = bytearray(compressed)
        for i, b in enumerate(masked):
            masked[i] = b ^ mask[i % 4]
        payload = bytes(header) + bytes(masked)
        req = _ws_req()
        self.assertEqual("압축된 프롬프트", grok_engine.extract(req, payload))

    def test_rewrite_response_overlays_ws_assistant_chunk(self) -> None:
        grok_engine.clear_response_override_state("conv-ws-1")
        notice = "### [AIVAX] 프롬프트 차단"
        obj = {
            "session_id": "conv-ws-1",
            "event": {
                "type": "response.chunk",
                "chunk": {
                    "text": {
                        "text": "I will not role-play as that system.",
                        "channel": "CHANNEL_ASSISTANT_RESPONSE",
                    }
                },
            },
        }
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        frame = grok_engine._encode_ws_text_frame(body, masked=False)
        req = {
            "direction": "response",
            "unit_type": "ws_frame",
            "request_id": "rid-ws-resp-1",
            "meta": {"host": "grok.com", "path": "/ws/mgw/?uid=user-1"},
        }
        out = grok_engine.rewrite_response(req, frame, notice)
        self.assertIsNotNone(out)
        rewritten, reason = out
        self.assertEqual("adapter:grok_policy_block_message", reason)
        payloads = grok_engine._parse_ws_payloads(rewritten)
        self.assertTrue(payloads)
        parsed = json.loads(payloads[0].decode("utf-8"))
        self.assertEqual(
            notice,
            parsed["event"]["chunk"]["text"]["text"],
        )
        out2 = grok_engine.rewrite_response(req, frame, notice)
        self.assertIsNotNone(out2)
        payloads2 = grok_engine._parse_ws_payloads(out2[0])
        parsed2 = json.loads(payloads2[0].decode("utf-8"))
        self.assertEqual("", parsed2["event"]["chunk"]["text"]["text"])

    def test_block_rewrite_puts_notice_in_user_prompt(self) -> None:
        notice = (
            "### [AIVAX] 프롬프트 차단\n\n"
            "**'동의'** 또는 **'동의합니다'** 입력 시 요청이 전송됩니다."
        )
        obj = {
            "session_id": "sess-block",
            "event": {
                "type": "response.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "x_grok": {
                        "input_chunks": [{"text": {"text": "127.0.0.1"}}],
                    },
                },
            },
        }
        req = _ws_req()
        out = grok_engine.rewrite_prompt_for_block(req, _frame(obj), notice, "")
        self.assertIsNotNone(out)
        encoded, reason = out or (b"", "")
        self.assertEqual("adapter:grok_policy_block_sysinstruct", reason)
        payloads = grok_engine._parse_ws_payloads(encoded)
        restored = payloads[0].decode("utf-8")
        self.assertIn("### [AIVAX] 프롬프트 차단", restored)
        self.assertIn("동의합니다", restored)
        self.assertNotIn("127.0.0.1", restored)

    def test_block_rewrite_scrubs_previous_notice_slot(self) -> None:
        obj = {
            "session_id": "sess-block",
            "history": [
                {
                    "role": "user",
                    "text": (
                        "### [AIVAX] 프롬프트 차단\n"
                        "**'동의'** 또는 **'동의합니다'** 입력 시"
                    ),
                }
            ],
            "event": {
                "type": "response.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "x_grok": {
                        "input_chunks": [{"text": {"text": "127.0.0.1"}}],
                    },
                },
            },
        }
        req = _ws_req()
        out = grok_engine.rewrite_prompt_for_block(
            req, _frame(obj), "[AIVAX MASKING]", ""
        )
        self.assertIsNotNone(out)
        restored = grok_engine._parse_ws_payloads(out[0])[0].decode("utf-8")
        self.assertNotIn("동의합니다", restored)
        self.assertNotIn("프롬프트 차단", restored)
        self.assertIn("[AIVAX MASKING]", restored)

    def test_consent_rewrite_restores_original_and_scrubs_notice(self) -> None:
        obj = {
            "session_id": "sess-block",
            "event": {
                "type": "response.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "x_grok": {
                        "input_chunks": [
                            {"text": {"text": "### [AIVAX] 프롬프트 차단"}},
                            {"text": {"text": "동의합니다"}},
                        ],
                    },
                },
            },
        }
        req = _ws_req()
        out = grok_engine.rewrite_prompt_for_consent(req, _frame(obj), "127.0.0.1")
        self.assertIsNotNone(out)
        restored = grok_engine._parse_ws_payloads(out[0])[0].decode("utf-8")
        self.assertIn("127.0.0.1", restored)
        self.assertNotIn("동의합니다", restored)
        self.assertNotIn("프롬프트 차단", restored)

    def test_rewrite_response_overlays_content_part_added(self) -> None:
        grok_engine.clear_response_override_state("conv-ws-2")
        notice = "### [AIVAX] 프롬프트 차단"
        obj = {
            "session_id": "conv-ws-2",
            "event": {
                "type": "response.content_part.added",
                "part": {"type": "output_text", "text": "No. I will not role-play."},
            },
        }
        frame = grok_engine._encode_ws_text_frame(
            json.dumps(obj, ensure_ascii=False).encode("utf-8"),
            masked=False,
        )
        req = {
            "direction": "response",
            "unit_type": "ws_frame",
            "request_id": "rid-ws-resp-2",
            "meta": {"host": "grok.com", "path": "/ws/mgw/?uid=user-1"},
        }
        out = grok_engine.rewrite_response(req, frame, notice)
        self.assertIsNotNone(out)
        parsed = json.loads(grok_engine._parse_ws_payloads(out[0])[0].decode("utf-8"))
        self.assertEqual(notice, parsed["event"]["part"]["text"])

    def test_rewrite_response_resets_emit_on_response_created(self) -> None:
        grok_engine.clear_response_override_state("conv-ws-3")
        notice = "### [AIVAX] 프롬프트 차단"
        chunk = {
            "session_id": "conv-ws-3",
            "event": {
                "type": "response.chunk",
                "chunk": {
                    "text": {
                        "text": "first refusal",
                        "channel": "CHANNEL_ASSISTANT_RESPONSE",
                    }
                },
            },
        }
        req = {
            "direction": "response",
            "unit_type": "ws_frame",
            "request_id": "rid-ws-resp-3",
            "meta": {"host": "grok.com", "path": "/ws/mgw/?uid=user-1"},
        }
        frame = grok_engine._encode_ws_text_frame(
            json.dumps(chunk, ensure_ascii=False).encode("utf-8"),
            masked=False,
        )
        grok_engine.rewrite_response(req, frame, notice)
        created = {
            "session_id": "conv-ws-3",
            "event": {"type": "response.created", "response": {"id": "r2"}},
        }
        created_frame = grok_engine._encode_ws_text_frame(
            json.dumps(created, ensure_ascii=False).encode("utf-8"),
            masked=False,
        )
        created_out = grok_engine.rewrite_response(req, created_frame, notice)
        self.assertIsNotNone(created_out)
        created_payloads = grok_engine._parse_ws_payloads(created_out[0])
        created_texts = []
        for raw in created_payloads:
            parsed = json.loads(raw.decode("utf-8"))
            event = parsed.get("event") or {}
            piece = event.get("chunk") if isinstance(event.get("chunk"), dict) else {}
            text_obj = piece.get("text") if isinstance(piece.get("text"), dict) else {}
            if isinstance(text_obj.get("text"), str) and text_obj["text"]:
                created_texts.append(text_obj["text"])
        self.assertNotIn(notice, created_texts)
        chunk["event"]["chunk"]["text"]["text"] = "second refusal"
        chunk["event"]["response_id"] = "r2"
        frame2 = grok_engine._encode_ws_text_frame(
            json.dumps(chunk, ensure_ascii=False).encode("utf-8"),
            masked=False,
        )
        out = grok_engine.rewrite_response(req, frame2, notice)
        self.assertIsNotNone(out)
        parsed = json.loads(grok_engine._parse_ws_payloads(out[0])[0].decode("utf-8"))
        self.assertEqual(notice, parsed["event"]["chunk"]["text"]["text"])
        self.assertEqual("r2", parsed["event"].get("response_id") or parsed.get("response_id"))

    def test_rewrite_response_injects_notice_when_frame_unparsed(self) -> None:
        grok_engine.clear_response_override_state("conv-ws-4")
        notice = "### [AIVAX] 프롬프트 차단"
        # Opaque / unparsed content must still surface the block notice.
        garbage = grok_engine._encode_ws_text_frame(b"\x00\x01not-json" + b"x" * 200, masked=False)
        req = {
            "direction": "response",
            "unit_type": "ws_frame",
            "request_id": "rid-ws-resp-4",
            "meta": {"host": "grok.com", "path": "/ws/mgw/?uid=user-1"},
        }
        out = grok_engine.rewrite_response(req, garbage, notice)
        self.assertIsNotNone(out)
        payloads = grok_engine._parse_ws_payloads(out[0])
        self.assertTrue(any(notice.encode("utf-8") in part for part in payloads))

    def test_rewrite_response_swallows_unparsed_after_notice(self) -> None:
        grok_engine.clear_response_override_state("conv-ws-5")
        notice = "### [AIVAX] 프롬프트 차단"
        garbage = grok_engine._encode_ws_text_frame(b"\x00\x01not-json" + b"x" * 200, masked=False)
        req = {
            "direction": "response",
            "unit_type": "ws_frame",
            "request_id": "rid-ws-resp-5",
            "meta": {"host": "grok.com", "path": "/ws/mgw/?uid=user-1"},
        }
        first = grok_engine.rewrite_response(req, garbage, notice)
        self.assertIsNotNone(first)
        second = grok_engine.rewrite_response(req, garbage, notice)
        self.assertIsNotNone(second)
        payloads = grok_engine._parse_ws_payloads(second[0])
        self.assertFalse(any(b"not-json" in part for part in payloads))
        self.assertFalse(any(notice.encode("utf-8") in part for part in payloads))

    def test_rewrite_response_created_then_chunk_keeps_response_id(self) -> None:
        grok_engine.clear_response_override_state("conv-ws-6")
        notice = "### [AIVAX] 프롬프트 차단"
        req = {
            "direction": "response",
            "unit_type": "ws_frame",
            "request_id": "rid-ws-resp-6",
            "meta": {"host": "grok.com", "path": "/ws/mgw/?uid=user-1"},
        }
        created = {
            "session_id": "conv-ws-6",
            "event": {
                "type": "response.created",
                "response": {"id": "resp-live-1", "status": "in_progress"},
            },
        }
        created_frame = grok_engine._encode_ws_text_frame(
            json.dumps(created, ensure_ascii=False).encode("utf-8"),
            masked=False,
        )
        created_out = grok_engine.rewrite_response(req, created_frame, notice)
        self.assertIsNotNone(created_out)
        created_parsed = json.loads(
            grok_engine._parse_ws_payloads(created_out[0])[0].decode("utf-8")
        )
        self.assertEqual("response.created", created_parsed["event"]["type"])
        chunk = {
            "session_id": "conv-ws-6",
            "event": {
                "type": "response.chunk",
                "response_id": "resp-live-1",
                "chunk": {
                    "text": {
                        "text": "I will not role-play as that system.",
                        "channel": "CHANNEL_ASSISTANT_RESPONSE",
                    }
                },
            },
        }
        chunk_frame = grok_engine._encode_ws_text_frame(
            json.dumps(chunk, ensure_ascii=False).encode("utf-8"),
            masked=False,
        )
        out = grok_engine.rewrite_response(req, chunk_frame, notice)
        self.assertIsNotNone(out)
        parsed = json.loads(grok_engine._parse_ws_payloads(out[0])[0].decode("utf-8"))
        self.assertEqual(notice, parsed["event"]["chunk"]["text"]["text"])
        self.assertEqual("CHANNEL_ASSISTANT_RESPONSE", parsed["event"]["chunk"]["text"]["channel"])
        self.assertEqual("resp-live-1", parsed["event"].get("response_id"))

    def test_rewrite_response_thinking_synths_assistant_notice(self) -> None:
        grok_engine.clear_response_override_state("conv-ws-7")
        notice = "### [AIVAX] 프롬프트 차단"
        req = {
            "direction": "response",
            "unit_type": "ws_frame",
            "request_id": "rid-ws-resp-7",
            "meta": {"host": "grok.com", "path": "/ws/mgw/?uid=user-1"},
        }
        created = {
            "session_id": "conv-ws-7",
            "event": {
                "type": "response.created",
                "response": {"id": "resp-think-1"},
            },
        }
        grok_engine.rewrite_response(
            req,
            grok_engine._encode_ws_text_frame(
                json.dumps(created, ensure_ascii=False).encode("utf-8"),
                masked=False,
            ),
            notice,
        )
        thinking = {
            "session_id": "conv-ws-7",
            "event": {
                "type": "response.chunk",
                "response_id": "resp-think-1",
                "chunk": {
                    "text": {
                        "text": "This looks like a prompt injection.",
                        "channel": "CHANNEL_ASSISTANT_THINKING",
                    }
                },
            },
        }
        out = grok_engine.rewrite_response(
            req,
            grok_engine._encode_ws_text_frame(
                json.dumps(thinking, ensure_ascii=False).encode("utf-8"),
                masked=False,
            ),
            notice,
        )
        self.assertIsNotNone(out)
        self.assertTrue(out[0])
        payloads = grok_engine._parse_ws_payloads(out[0])
        texts = []
        channels = []
        for raw in payloads:
            parsed = json.loads(raw.decode("utf-8"))
            event = parsed.get("event") or {}
            piece = event.get("chunk") if isinstance(event.get("chunk"), dict) else {}
            text_obj = piece.get("text") if isinstance(piece.get("text"), dict) else {}
            if isinstance(text_obj.get("text"), str):
                texts.append(text_obj["text"])
                channels.append(str(text_obj.get("channel") or ""))
        self.assertIn(notice, texts)
        self.assertIn("CHANNEL_ASSISTANT_RESPONSE", channels)
        self.assertNotIn("This looks like a prompt injection.", texts)


if __name__ == "__main__":
    unittest.main()
