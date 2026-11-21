"""Genspark AI Chat prompt, SSE answer, and account extraction."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

AI_ENGINE = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "genspark"
sys.path.insert(0, str(AI_ENGINE / "core"))
sys.path.insert(0, str(AI_ENGINE / "adapters"))

import genspark_engine  # noqa: E402
from ai_policy_engine import (  # noqa: E402
    _eve_response_use_full_body,
    _host_to_service_id,
    _is_conversation_path,
)
from ai_service_codes import AI_SERVICE_GENSPARK, resolve_ai_service_code  # noqa: E402

PROJECT = "72ceffc2-4f3a-4153-9a58-d73aab6fb5da"
MESSAGE = "ef039d1c-110e-462e-807a-dd692c4b17c3"
FOLLOWUP = "2a5ec5cb-1e0c-40bb-b0a1-6bfecf687480"
PROMPT = "Hello Genspak AI!"
FOLLOWUP_PROMPT = "What is genspark?"


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _req(
    direction: str,
    path: str,
    *,
    request_id: str = "req-genspark-1",
    cookie: str = "",
) -> dict:
    content = "application/json" if direction == "request" else "text/event-stream"
    unit = "json_body" if direction == "request" else "sse_event"
    if direction == "response" and path.split("?", 1)[0].rstrip("/") == "/api/user":
        content = "application/json"
        unit = "json_body"
    return {
        "direction": direction,
        "request_id": request_id,
        "content_type": content,
        "unit_type": unit,
        "meta": {
            "host": "www.genspark.ai",
            "path": path,
            "method": "POST" if direction == "request" and "ask_proxy" in path else "GET",
            "client_ip": "10.0.50.60",
            "cookie": cookie,
        },
    }


class GensparkEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        genspark_engine._TURNS.clear()
        genspark_engine._SSE_BUFFERS.clear()

    def test_service_routing(self) -> None:
        self.assertEqual(
            _host_to_service_id("www.genspark.ai", "/api/agent/ask_proxy"),
            "genspark",
        )
        self.assertEqual(_host_to_service_id("notgenspark.ai", "/api/agent/ask_proxy"), "")
        self.assertEqual(resolve_ai_service_code("genspark"), AI_SERVICE_GENSPARK)
        self.assertEqual(AI_SERVICE_GENSPARK, 13)
        self.assertTrue(_is_conversation_path("genspark", "/api/agent/ask_proxy"))
        self.assertFalse(_is_conversation_path("genspark", "/api/user"))
        self.assertFalse(_is_conversation_path("genspark", "/static/app.js"))
        self.assertTrue(
            _eve_response_use_full_body(
                "www.genspark.ai",
                "/api/agent/ask_proxy",
                "text/event-stream",
            )
        )
        self.assertFalse(
            _eve_response_use_full_body(
                "www.genspark.ai",
                "/api/user",
                "application/json",
            )
        )

    def test_prompt_model_and_session(self) -> None:
        req = _req("request", "/api/agent/ask_proxy")
        payload = _load("ask_proxy_request.json")
        self.assertFalse(genspark_engine.genspark_should_skip_policy(req, payload))
        self.assertEqual(genspark_engine.extract(req, payload), PROMPT)
        self.assertEqual(genspark_engine.extract_model(req, payload), "gpt-4.1")
        self.assertEqual(genspark_engine.extract_session_id(req, payload), PROJECT)
        built = genspark_engine.build_policy_api_payload(req, payload, PROMPT)
        self.assertEqual(built["prompt"], PROMPT)
        self.assertEqual(built["session_id"], PROJECT)
        self.assertEqual(built["message_id"], MESSAGE)
        self.assertEqual(built["model"], "gpt-4.1")
        self.assertEqual(built["ai_service"], AI_SERVICE_GENSPARK)

    def test_followup_uses_last_user_text_not_history_or_label(self) -> None:
        req = _req("request", "/api/agent/ask_proxy", request_id="req-genspark-2")
        payload = _load("ask_proxy_followup.json")
        self.assertEqual(genspark_engine.extract(req, payload), FOLLOWUP_PROMPT)
        built = genspark_engine.build_policy_api_payload(req, payload, FOLLOWUP_PROMPT)
        self.assertEqual(built["session_id"], PROJECT)
        self.assertEqual(built["message_id"], FOLLOWUP)
        self.assertNotIn(PROMPT, built["prompt"])
        self.assertNotIn("I'm Genspark AI", built["prompt"])

    def test_slides_prompt_is_collected_and_other_agents_stay_out(self) -> None:
        slides = _req("request", "/api/agent/ask_proxy", request_id="req-slides")
        slides_body = json.dumps(
            {
                "type": "ai_slides",
                "models": ["genspark-gen-1"],
                "project_id": PROJECT,
                "user_s_input": "재생에너지 10장 슬라이드",
                "messages": [
                    {"role": "user", "id": MESSAGE, "content": "재생에너지 10장 슬라이드"}
                ],
            }
        ).encode()
        self.assertFalse(genspark_engine.genspark_should_skip_policy(slides, slides_body))
        self.assertEqual(genspark_engine.extract(slides, slides_body), "재생에너지 10장 슬라이드")
        built = genspark_engine.build_policy_api_payload(slides, slides_body, "재생에너지 10장 슬라이드")
        self.assertEqual(built["prompt"], "재생에너지 10장 슬라이드")
        self.assertEqual(built["model"], "genspark-gen-1")
        self.assertEqual(built["message_id"], MESSAGE)

        docs = _req("request", "/api/agent/ask_proxy", request_id="req-docs")
        docs_body = b'{"type":"ai_docs","user_s_input":"docs","messages":[{"role":"user","content":"docs"}]}'
        self.assertTrue(genspark_engine.genspark_should_skip_policy(docs, docs_body))
        self.assertEqual(genspark_engine.extract(docs, docs_body), "")

        static = _req("request", "/_next/static/app.js", request_id="req-static")
        self.assertTrue(genspark_engine.genspark_should_skip_policy(static, b""))
        self.assertEqual(genspark_engine.extract(static, b"console.log(1)"), "")

    def test_sse_answer_ignores_tools_and_empty_deltas(self) -> None:
        request = _req("request", "/api/agent/ask_proxy")
        genspark_engine.extract(request, _load("ask_proxy_request.json"))
        response = _req("response", "/api/agent/ask_proxy")
        payload = _load("ask_proxy_sse.txt")
        answer = json.loads(_load("ask_proxy_followup.json"))["messages"][1]["content"]
        self.assertEqual(genspark_engine.extract_full_body(response, payload), answer)
        self.assertTrue(response["_extract_final"])
        self.assertTrue(genspark_engine.response_stream_done(response, payload))
        built = genspark_engine.build_policy_api_payload(response, payload, answer)
        self.assertEqual(built["llm_output"], answer)
        self.assertEqual(built["session_id"], PROJECT)
        self.assertEqual(built["message_id"], MESSAGE)
        self.assertNotIn("내부 추론", built["llm_output"])
        self.assertNotIn("도구 호출", built["llm_output"])

    def test_incremental_sse_commits_on_done(self) -> None:
        req = _req("response", "/api/agent/ask_proxy", request_id="req-stream")
        first = 'data: {"type":"message_field_delta","field_name":"content","delta":"안"}\n\n'.encode()
        empty = b'data: {"type":"message_field_delta","field_name":"content","delta":""}\n\n'
        second = 'data: {"type":"message_field_delta","field_name":"content","delta":"녕"}\n\n'.encode()
        done = b'data: {"type":"message_done"}\n\n'
        self.assertEqual(genspark_engine.extract(req, first), "")
        self.assertFalse(req["_extract_final"])
        self.assertEqual(genspark_engine.extract(req, empty), "")
        self.assertEqual(genspark_engine.extract(req, second), "")
        self.assertEqual(genspark_engine.extract(req, done), "안녕")
        self.assertTrue(req["_extract_final"])

    def test_account_is_available_on_the_later_prompt(self) -> None:
        import account_lookup

        cache = tempfile.mkdtemp(prefix="genspark_accounts_")
        os.environ["AIVAX_ACCOUNT_CACHE_PATH"] = os.path.join(cache, "accounts.sqlite")
        account_lookup.configure()
        cookie = "session_token=session-example"
        account = _req("response", "/api/user", cookie=cookie)
        self.assertEqual(genspark_engine.extract_full_body(account, _load("user.json")), "")
        self.assertEqual(account["_genspark_user_id"], "63a16ff9-f108-4572-9bdd-73b0a0e624ed")
        self.assertEqual(account["_genspark_email"], "juyonglee@wins21.co.kr")
        self.assertEqual(account["_genspark_name"], "이주용")

        send = _req("request", "/api/agent/ask_proxy", request_id="req-account", cookie=cookie)
        payload = _load("ask_proxy_request.json")
        built = genspark_engine.build_policy_api_payload(send, payload, PROMPT)
        self.assertEqual(built["user_id"], "63a16ff9-f108-4572-9bdd-73b0a0e624ed")
        self.assertEqual(built["email"], "juyonglee@wins21.co.kr")
        self.assertEqual(built["user_name"], "이주용")
        self.assertIsNone(
            genspark_engine.build_policy_api_payload(account, _load("user.json"), "")
        )

    def test_nested_account_and_paired_answer(self) -> None:
        import account_lookup

        cache = tempfile.mkdtemp(prefix="genspark_accounts_")
        os.environ["AIVAX_ACCOUNT_CACHE_PATH"] = os.path.join(cache, "accounts.sqlite")
        account_lookup.configure()
        cookie = "genspark_session=session-example-value"
        account = _req("response", "/api/user", cookie=cookie)
        nested = json.dumps(
            {"data": {"cogen": {"id": 42, "email": "lee@example.com", "full_name": "Lee"}}}
        ).encode()
        self.assertEqual(genspark_engine.extract_full_body(account, nested), "")
        self.assertEqual(account["_genspark_user_id"], "42")
        self.assertEqual(account["_genspark_email"], "lee@example.com")
        self.assertEqual(account["_genspark_name"], "Lee")

        request = _req("request", "/api/agent/ask_proxy", request_id="req-pair", cookie=cookie)
        genspark_engine.extract(request, _load("ask_proxy_request.json"))
        response = _req("response", "/api/agent/ask_proxy", request_id="req-pair", cookie=cookie)
        payload = (
            b'data: {"type":"message_field_delta","field_name":"content","delta":"ok","message_id":"assistant-id"}\n\n'
            b'data: {"type":"message_done","message_id":"assistant-id","session_state":{"_llm_model":"glm-5p3-flash-baseten"}}\n\n'
        )
        self.assertEqual(genspark_engine.extract_full_body(response, payload), "ok")
        self.assertEqual(response["_genspark_ui_message_id"], MESSAGE)
        self.assertEqual(genspark_engine.extract_model(response, payload), "glm-5p3-flash-baseten")
        built = genspark_engine.build_policy_api_payload(response, payload, "ok")
        self.assertEqual(built["message_id"], MESSAGE)
        self.assertEqual(built["user_id"], "42")
        self.assertEqual(built["email"], "lee@example.com")
        self.assertEqual(built["model"], "glm-5p3-flash-baseten")

    def test_upload_is_attached_to_the_later_prompt(self) -> None:
        boundary = "gensparkboundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="note.txt"\r\n'
            "Content-Type: text/plain\r\n\r\n"
            "hello file\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        upload = _req("request", "/api/files/upload", request_id="req-upload")
        upload["content_type"] = f"multipart/form-data; boundary={boundary}"
        upload["meta"]["method"] = "POST"
        saved = genspark_engine.normalize_upload(upload, body)
        self.assertTrue(saved and saved.get("handled"))
        self.assertTrue(saved.get("saved_path"))

        response = _req("response", "/api/files/upload", request_id="req-upload")
        response["content_type"] = "application/json"
        response["meta"]["method"] = "POST"
        genspark_engine.normalize_upload(
            response,
            b'{"file_wrapper_url":"/api/files/s/abc","file_name":"note.txt"}',
        )

        chat = _req("request", "/api/agent/ask_proxy", request_id="req-with-file")
        payload = json.dumps(
            {
                "type": "super_agent",
                "models": ["gpt-4.1"],
                "project_id": PROJECT,
                "messages": [
                    {
                        "role": "user",
                        "id": MESSAGE,
                        "content": "\uc774 \ud30c\uc77c \ubcf4\uace0",
                        "file_wrapper_url": "/api/files/s/abc",
                        "file_name": "note.txt",
                    }
                ],
                "user_s_input": "\uc774 \ud30c\uc77c \ubcf4\uace0",
            }
        ).encode()
        built = genspark_engine.build_policy_api_payload(chat, payload, "\uc774 \ud30c\uc77c \ubcf4\uace0")
        self.assertEqual(built["prompt"], "\uc774 \ud30c\uc77c \ubcf4\uace0")
        self.assertEqual(built["attachments"][0]["name"], "note.txt")
        self.assertGreater(built["attachments"][0]["size"], 0)
        self.assertTrue(built["attachments"][0]["id"])

    def test_inline_image_in_the_prompt_is_attached(self) -> None:
        png = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        gif = "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
        payload = json.dumps(
            {
                "type": "super_agent",
                "models": ["gpt-4.1"],
                "project_id": PROJECT,
                "user_s_input": "해당 이미지 분석해주세요.",
                "messages": [
                    {
                        "role": "user",
                        "id": "older-image",
                        "content": "이전 그림",
                        "image": "data:image/gif;base64," + gif,
                    },
                    {
                        "role": "user",
                        "id": MESSAGE,
                        "content": [
                            {"type": "text", "text": "해당 이미지 분석해주세요."},
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64," + png},
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/gif;base64," + gif},
                            },
                        ],
                        "blob": "data:image/png;base64," + png,
                    },
                ],
            }
        ).encode()
        chat = _req("request", "/api/agent/ask_proxy", request_id="req-image")
        built = genspark_engine.build_policy_api_payload(chat, payload, "해당 이미지 분석해주세요.")
        self.assertEqual(built["prompt"], "해당 이미지 분석해주세요.")
        self.assertEqual(len(built["attachments"]), 2)
        self.assertEqual(built["attachments"][0]["name"], "attachment-1.png")
        self.assertEqual(built["attachments"][0]["mime_type"], "image/png")
        self.assertEqual(built["attachments"][1]["name"], "attachment-2.gif")
        self.assertEqual(built["attachments"][1]["mime_type"], "image/gif")
        self.assertGreater(built["attachments"][0]["size"], 0)
        self.assertTrue(built["attachments"][0]["id"])

    def test_inline_non_image_data_uri_is_attached_with_declared_mime(self) -> None:
        pdf = "JVBERi0xLjQKJcTl8uXrp/Og0MTGCjEgMCBvYmoKPDwvVHlwZS9DYXRhbG9nPj4KZW5kb2JqCg=="
        payload = json.dumps(
            {
                "type": "super_agent",
                "project_id": PROJECT,
                "user_s_input": "첨부 문서를 요약해줘.",
                "messages": [
                    {
                        "role": "user",
                        "id": MESSAGE,
                        "content": [
                            {"type": "text", "text": "첨부 문서를 요약해줘."},
                            {
                                "type": "file",
                                "file": {
                                    "name": "원본-보고서.pdf",
                                    "source": "data:application/pdf;base64," + pdf,
                                },
                            },
                        ],
                    }
                ],
            }
        ).encode()
        chat = _req("request", "/api/agent/ask_proxy", request_id="req-document")
        built = genspark_engine.build_policy_api_payload(chat, payload, "첨부 문서를 요약해줘.")
        self.assertEqual(len(built["attachments"]), 1)
        self.assertEqual(built["attachments"][0]["name"], "원본-보고서.pdf")
        self.assertEqual(built["attachments"][0]["mime_type"], "application/pdf")
        self.assertGreater(built["attachments"][0]["size"], 0)


if __name__ == "__main__":
    unittest.main()
