"""Timely AI account, prompt, and Solar Mini SSE parsing."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("AIVAX_TIMELY_SEND_DUMP_DIR", tempfile.mkdtemp(prefix="timely_send_"))


AI_ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AI_ENGINE / "core"))
sys.path.insert(0, str(AI_ENGINE / "adapters"))

import timely_engine  # noqa: E402
from ai_policy_engine import (  # noqa: E402
    _eve_response_use_full_body,
    _host_to_service_id,
    _is_conversation_path,
    llm_model_for_policy,
)
from ai_service_codes import AI_SERVICE_TIMELY, resolve_ai_service_code  # noqa: E402


SPACE = "7320e3cb-c057-462f-bd12-cfc547084e78"
WIN_MEMBER = "sm_cmuakukyc00d307y7545ncl3c"
MAC_MEMBER = "sm_cmuakuky500d207y71gd2gox8"
SUBSCRIBE = "01M5S35NK51X1DCSVQX3J91YDB"
SESSION = "01M5S33DNYMRWF4CS3RYVCYKWY"
ANSWER = "Hello! I'm Solar Mini, an AI assistant. How can I help you today?"

SOLAR_MINI_SSE = """id: 1792917300270-0
data: {"type":"edit_chat_title","state":"call","message":"Hello World"}

id: 1792917300297-0
data: {"type":"start"}

id: 1792917300306-0
data: {"type":"message_created","message_id":"01M5S35QYRDJKRDPXPXHE8AG3E","user_message_id":"01M5S35QYRNNT6352ADBRAXY8M","role":"assistant","created_at":"2026-10-25T08:34:59.160Z"}

id: 1792917305266-0
data: {"type":"token","content":"Hello! I'm Solar Mini, an AI assistant. How can"}

id: 1792917305266-1
data: {"type":"token","content":" I help you today?"}

id: 1792917305410-0
data: {"type":"final_response","session_id":"01M5S33DNYMRWF4CS3RYVCYKWY","message":"Hello! I'm Solar Mini, an AI assistant. How can I help you today?","thinking":"","tool_results":[],"sources":{},"parsed":null,"model":"solar-mini","credits":3.39384}

id: 1792917305410-1
data: {"type":"end"}
"""


def _req(direction: str, path: str, *, client_ip: str = "10.0.50.60", method: str = "GET") -> dict:
    content = "application/json" if direction == "request" else "text/event-stream"
    unit = "json_body" if direction == "request" else "sse_event"
    if direction == "response" and "/member/" in path:
        content = "application/json"
        unit = "json_body"
    return {
        "direction": direction,
        "request_id": "req-timely-1",
        "content_type": content,
        "unit_type": unit,
        "meta": {
            "host": "timelyai.io",
            "path": path,
            "method": method,
            "client_ip": client_ip,
        },
    }


class TimelyEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        timely_engine._TURNS.clear()
        timely_engine._EMITTED.clear()
        timely_engine._REQUEST_PROMPTS.clear()
        upload_mod = __import__(
            timely_engine.remember_turn_request.__module__, fromlist=["_PENDING"]
        )
        upload_mod._PENDING.clear()
        upload_mod._TURN_REQUEST.clear()

    def test_service_routing(self) -> None:
        self.assertEqual(_host_to_service_id("timelyai.io", "/timely/api/stream"), "timely")
        self.assertEqual(resolve_ai_service_code("timely"), AI_SERVICE_TIMELY)
        self.assertTrue(_is_conversation_path("timely", "/timely/api/stream"))
        self.assertFalse(
            _is_conversation_path(
                "timely",
                f"/proxy/api/backend/v2/space/{SPACE}/member/{WIN_MEMBER}",
            )
        )
        self.assertTrue(
            _eve_response_use_full_body(
                "timelyai.io",
                "/timely/api/stream",
                "text/event-stream",
            )
        )
        self.assertFalse(
            _eve_response_use_full_body(
                "timelyai.io",
                f"/proxy/api/backend/v2/space/{SPACE}/member/{WIN_MEMBER}",
                "application/json",
            )
        )

    def test_member_account_windows_and_macos(self) -> None:
        windows = _req(
            "response",
            f"/proxy/api/backend/v2/space/{SPACE}/member/{WIN_MEMBER}",
        )
        body = json.dumps(
            {
                "data": {
                    "email": "win@example.com",
                    "name": "이주용",
                    "chatNickname": "별명",
                    "files": [{"name": "pic.webp"}],
                }
            }
        ).encode()
        self.assertEqual(timely_engine.extract(windows, body), "")
        self.assertEqual(windows["_timely_user_id"], WIN_MEMBER)
        self.assertEqual(windows["_timely_space_id"], SPACE)
        self.assertEqual(windows["_timely_email"], "win@example.com")
        self.assertEqual(windows["_timely_name"], "이주용")

        macos = _req(
            "response",
            f"/proxy/api/backend/v2/space/{SPACE}/member/{MAC_MEMBER}",
            client_ip="10.0.50.61",
        )
        nested = json.dumps({"data": {"member": {"email": "mac@example.com"}}}).encode()
        self.assertEqual(timely_engine.extract(macos, nested), "")
        self.assertEqual(macos["_timely_member_id"], MAC_MEMBER)
        self.assertEqual(macos["_timely_email"], "mac@example.com")
        self.assertNotIn("_timely_name", macos)

    def test_member_name_is_stored_on_the_later_prompt(self) -> None:
        import account_lookup

        cache = tempfile.mkdtemp(prefix="timely_accounts_")
        os.environ["AIVAX_ACCOUNT_CACHE_PATH"] = os.path.join(cache, "accounts.sqlite")
        account_lookup.configure()
        member = _req(
            "response",
            f"/proxy/api/backend/v2/space/{SPACE}/member/{WIN_MEMBER}",
        )
        body = json.dumps(
            {"email": "win@example.com", "chatNickname": "이주용"}
        ).encode()
        timely_engine.extract(member, body)
        send = _req("request", "/timely/api/stream", method="POST")
        payload = json.dumps({"message": "안녕"}).encode()
        built = timely_engine.build_policy_api_payload(send, payload, "안녕")
        self.assertEqual(built["email"], "win@example.com")
        self.assertEqual(built["user_id"], WIN_MEMBER)
        self.assertEqual(built["user_name"], "이주용")

    def test_subscribe_is_not_a_prompt(self) -> None:
        req = _req("request", "/timely/api/stream", method="POST")
        payload = json.dumps(
            {
                "url": f"/api-ai/v3/llm-completion/stream/subscribe/{SUBSCRIBE}",
                "body": {"lastEventId": "0"},
            }
        ).encode()
        self.assertEqual(timely_engine.extract(req, payload), "")
        self.assertTrue(timely_engine.timely_should_skip_policy(req, payload))
        self.assertEqual(timely_engine.extract_session_id(req, payload), SUBSCRIBE)
        self.assertEqual(timely_engine.collect_consent_session_ids(req, payload), [])
        self.assertIsNone(timely_engine.build_policy_api_payload(req, payload, ""))

    def test_consent_session_is_chat_id_not_stream_session(self) -> None:
        req = _req("request", "/timely/api/stream", method="POST")
        payload = json.dumps(
            {
                "url": "/api-ai/v3/llm-completion/stream/start",
                "body": {
                    "chat_id": "01M5S8514ZMP3P7WD28ZHKEHCX",
                    "messages": [{"role": "user", "content": "이 파일 요약해줘"}],
                },
            }
        ).encode()
        self.assertEqual(
            timely_engine.collect_consent_session_ids(req, payload),
            ["01M5S8514ZMP3P7WD28ZHKEHCX"],
        )
        self.assertEqual(timely_engine.extract_session_id(req, payload), "")

    def test_request_prompt_body(self) -> None:
        req = _req("request", "/timely/api/stream", method="POST")
        payload = json.dumps(
            {"url": "/api-ai/v3/llm-completion", "body": {"message": "Hello World"}}
        ).encode()
        self.assertEqual(timely_engine.extract(req, payload), "Hello World")
        self.assertFalse(timely_engine.timely_should_skip_policy(req, payload))
        built = timely_engine.build_policy_api_payload(req, payload, "Hello World")
        self.assertEqual(built["prompt"], "Hello World")
        self.assertEqual(built["ai_service"], AI_SERVICE_TIMELY)
        masked = timely_engine.rewrite_prompt_masking(req, payload, "***")
        self.assertIsNotNone(masked)
        self.assertEqual(json.loads(masked[0])["body"]["message"], "***")

    def test_solar_mini_sse_full_body(self) -> None:
        req = _req("response", "/timely/api/stream", method="POST")
        payload = SOLAR_MINI_SSE.encode()
        parsed = timely_engine.parse_timely_sse(payload)
        self.assertEqual(parsed["user_prompt"], "Hello World")
        self.assertEqual(parsed["assistant"], ANSWER)
        self.assertEqual(parsed["model"], "solar-mini")
        self.assertEqual(parsed["session_id"], SESSION)
        self.assertEqual(parsed["message_id"], "01M5S35QYRDJKRDPXPXHE8AG3E")
        self.assertEqual(parsed["user_message_id"], "01M5S35QYRNNT6352ADBRAXY8M")
        self.assertTrue(parsed["done"])

        text = timely_engine.extract_full_body(req, payload)
        self.assertEqual(text, ANSWER)
        self.assertTrue(req["_extract_final"])
        self.assertEqual(req["_timely_deferred_human_prompt"], "Hello World")
        self.assertEqual(req["_timely_model"], "solar-mini")
        self.assertEqual(timely_engine.extract_session_id(req, payload), SESSION)
        built = timely_engine.build_policy_api_payload(req, payload, text)
        self.assertEqual(built["llm_output"], ANSWER)
        self.assertEqual(built["session_id"], SESSION)
        self.assertEqual(built["ai_service"], AI_SERVICE_TIMELY)

    def test_incremental_events_emit_once(self) -> None:
        req = _req("response", "/timely/api/stream", method="POST")
        title = b'data: {"type":"edit_chat_title","state":"call","message":"Hello World"}\n\n'
        token_a = b'data: {"type":"token","content":"Hello! I\'m Solar Mini, an AI assistant. How can"}\n\n'
        token_b = b'data: {"type":"token","content":" I help you today?"}\n\n'
        final = (
            b'data: {"type":"final_response","session_id":"01M5S33DNYMRWF4CS3RYVCYKWY",'
            b'"message":"Hello! I\'m Solar Mini, an AI assistant. How can I help you today?",'
            b'"model":"solar-mini"}\n\n'
        )
        end = b'data: {"type":"end"}\n\n'
        self.assertEqual(timely_engine.extract(req, title), "")
        self.assertEqual(req["_timely_deferred_human_prompt"], "Hello World")
        self.assertEqual(timely_engine.extract(req, token_a), "")
        self.assertNotIn("_timely_deferred_human_prompt", req)
        self.assertEqual(timely_engine.extract(req, token_b), "")
        self.assertEqual(timely_engine.extract(req, final), ANSWER)
        self.assertTrue(req["_extract_final"])
        self.assertEqual(timely_engine.extract(req, end), "")
        self.assertFalse(req["_extract_final"])

    def test_response_rewrite_replaces_answer_once(self) -> None:
        req = _req("response", "/timely/api/stream", method="POST")
        first = timely_engine.rewrite_response(
            req,
            b'data: {"type":"token","content":"Hello! I\'m Solar Mini, an AI assistant. How can"}\n\n',
            "blocked",
        )
        self.assertIsNotNone(first)
        self.assertIn('"content":"blocked"', first[0].decode())
        second = timely_engine.rewrite_response(
            req,
            b'data: {"type":"token","content":" I help you today?"}\n\n',
            "blocked",
        )
        self.assertIsNotNone(second)
        self.assertIn('"content":""', second[0].decode())
        final = timely_engine.rewrite_response(
            req,
            b'data: {"type":"final_response","session_id":"s","message":"secret","model":"solar-mini"}\n\n',
            "blocked",
        )
        self.assertIsNotNone(final)
        obj = json.loads(final[0].decode().split("data:", 1)[1])
        self.assertEqual(obj["message"], "blocked")
        self.assertEqual(obj["model"], "solar-mini")

    def test_request_prompt_suppresses_duplicate_deferred_title(self) -> None:
        request = _req("request", "/timely/api/stream", method="POST")
        request_body = json.dumps({"message": "Hello World"}).encode()
        self.assertEqual(timely_engine.extract(request, request_body), "Hello World")
        response = _req("response", "/timely/api/stream", method="POST")
        response["request_id"] = "req-timely-2"
        title = b'data: {"type":"edit_chat_title","state":"call","message":"Hello World"}\n\n'
        self.assertEqual(timely_engine.extract(response, title), "")
        self.assertNotIn("_timely_deferred_human_prompt", response)
        self.assertEqual(response["_timely_user_prompt"], "Hello World")

    def test_send_prompt_in_message_parts_and_rewrite(self) -> None:
        body = {
            "url": "/api-ai/v3/llm-completion",
            "body": {
                "chat_id": "01M5S8514ZMP3P7WD28ZHKEHCX",
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "old question"}]},
                    {"role": "assistant", "content": "old answer"},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "이 이미지 설명해줘"},
                            {"type": "image_url", "image_url": {"url": "https://x"}},
                        ],
                    },
                ],
            },
        }
        send = _req("request", "/timely/api/stream", method="POST")
        payload = json.dumps(body, ensure_ascii=False).encode()
        self.assertEqual(timely_engine.extract(send, payload), "이 이미지 설명해줘")
        self.assertFalse(timely_engine.timely_should_skip_policy(send, payload))
        out = timely_engine.rewrite_prompt_masking(send, payload, "이 이미지 [MASK]")
        self.assertIsNotNone(out)
        rewritten = json.loads(out[0])
        last = rewritten["body"]["messages"][-1]["content"]
        self.assertEqual(last[0]["text"], "이 이미지 [MASK]")
        self.assertEqual(last[1]["type"], "image_url")
        self.assertEqual(rewritten["body"]["messages"][0]["content"][0]["text"], "old question")

    def test_send_prompt_in_string_body_and_nested_wrapper(self) -> None:
        inner = {"payload": {"chat": {"question": "주민번호 알려줘"}}, "system": {"text": "sys"}}
        body = {"url": "/api-ai/v3/llm-completion", "body": json.dumps(inner, ensure_ascii=False)}
        send = _req("request", "/timely/api/stream", method="POST")
        payload = json.dumps(body, ensure_ascii=False).encode()
        self.assertEqual(timely_engine.extract(send, payload), "주민번호 알려줘")
        out = timely_engine.rewrite_prompt(send, payload, "[BLOCKED]", "block")
        self.assertIsNotNone(out)
        nested = json.loads(json.loads(out[0])["body"])
        self.assertEqual(nested["payload"]["chat"]["question"], "[BLOCKED]")
        self.assertEqual(nested["system"]["text"], "sys")

    def test_consent_replay_restores_blocked_turn_file_references(self) -> None:
        send = _req("request", "/timely/api/stream", method="POST")
        blocked = json.dumps(
            {
                "url": "/api-ai/v3/llm-completion",
                "body": {
                    "messages": [{"role": "user", "content": "이 파일 요약해줘"}],
                    "files": [STORED],
                    "attachments": [{"fileId": "asset-1", "name": ORIGINAL}],
                },
            },
            ensure_ascii=False,
        ).encode()
        state = timely_engine.consent_attachment_state(send, blocked)
        self.assertIsNotNone(state)

        consent = json.dumps(
            {
                "url": "/api-ai/v3/llm-completion",
                "body": {"messages": [{"role": "user", "content": "이 파일 요약해줘"}]},
            },
            ensure_ascii=False,
        ).encode()
        restored = timely_engine.restore_consent_attachments(send, consent, state)
        self.assertIsNotNone(restored)
        replay = json.loads(restored)
        self.assertEqual(replay["body"]["files"], [STORED])
        self.assertEqual(replay["body"]["attachments"][0]["fileId"], "asset-1")

    def test_consent_replay_restores_multipart_body_file_references(self) -> None:
        boundary = "----WebKitFormBoundaryConsent"
        send = _req("request", "/timely/api/stream", method="POST")
        send["content_type"] = f"multipart/form-data; boundary={boundary}"
        def multipart(body: dict) -> bytes:
            return (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="url"\r\n\r\n'
                "/api-ai/v3/llm-completion/stream/start\r\n"
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="body"\r\n\r\n'
                f"{json.dumps(body, ensure_ascii=False)}\r\n"
                f"--{boundary}--\r\n"
            ).encode()

        blocked = multipart(
            {"messages": [{"role": "user", "content": "이 파일 요약해줘"}], "files": [STORED]}
        )
        state = timely_engine.consent_attachment_state(send, blocked)
        self.assertIsNotNone(state)

        consent = multipart({"messages": [{"role": "user", "content": "이 파일 요약해줘"}]})
        restored = timely_engine.restore_consent_attachments(send, consent, state)
        self.assertIsNotNone(restored)
        replay_body = restored.split(b'name="body"\r\n\r\n', 1)[1].split(b"\r\n--", 1)[0]
        self.assertEqual(json.loads(replay_body)["files"], [STORED])

    def test_unparsed_send_logs_shape_and_dumps(self) -> None:
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("AIVAX_TIMELY_SEND_DUMP_DIR")
            os.environ["AIVAX_TIMELY_SEND_DUMP_DIR"] = tmp
            try:
                send = _req("request", "/timely/api/stream", method="POST")
                send["request_id"] = "send-unknown"
                payload = json.dumps(
                    {"url": "/api-ai/v3/llm-completion", "body": {"chat_id": "x", "files": []}}
                ).encode()
                with self.assertLogs(timely_engine.logger, level="INFO") as logs:
                    self.assertEqual(timely_engine.extract(send, payload), "")
                self.assertIn("timely send no_prompt", "\n".join(logs.output))
                self.assertIn("body.chat_id:str1", "\n".join(logs.output))
                dumped = os.listdir(tmp)
                self.assertEqual(len(dumped), 1)
                self.assertEqual(os.stat(os.path.join(tmp, dumped[0])).st_mode & 0o777, 0o600)

                subscribe = _req("request", "/timely/api/stream", method="POST")
                subscribe["request_id"] = "sub-unknown"
                sub_payload = json.dumps(
                    {
                        "url": f"/api-ai/v3/llm-completion/stream/subscribe/{SUBSCRIBE}",
                        "body": {"lastEventId": "0"},
                    }
                ).encode()
                timely_engine.extract(subscribe, sub_payload)
                self.assertEqual(len(os.listdir(tmp)), 1)
            finally:
                if old is None:
                    os.environ.pop("AIVAX_TIMELY_SEND_DUMP_DIR", None)
                else:
                    os.environ["AIVAX_TIMELY_SEND_DUMP_DIR"] = old


STORED = "p_CHAT_01M5S8514ZMP3P7WD28ZHKEHCX.webp"
ORIGINAL = "주민번호.webp"
WRITE_URL = f"https://tg7prod7storage.blob.core.windows.net/timely-file-khdgj/{STORED}?sp=cw&sig=w"
READ_URL = f"https://tg7prod7storage.blob.core.windows.net/timely-file-khdgj/{STORED}?sp=r&sig=r"


class TimelyUploadTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        import file_upload_manager as fum

        self.upload = sys.modules[timely_engine.remember_turn_request.__module__]
        self.upload._PENDING.clear()
        self.upload._TURN_REQUEST.clear()
        timely_engine._TURNS.clear()
        timely_engine._REQUEST_PROMPTS.clear()
        self.fum = fum
        self.old_dir = fum._UPLOAD_SAVE_DIR
        self.tmp = tempfile.TemporaryDirectory()
        fum._UPLOAD_SAVE_DIR = self.tmp.name
        self.fetched = []
        self.old_fetch = self.upload._fetch_blob

        def fake_fetch(url: str) -> bytes:
            self.fetched.append(url)
            if "sp=cw" in url:
                raise PermissionError("403")
            return b"RIFF" + b"w" * 60

        self.upload._fetch_blob = fake_fetch

    def tearDown(self) -> None:
        self.upload._fetch_blob = self.old_fetch
        self.fum._UPLOAD_SAVE_DIR = self.old_dir
        self.upload._PENDING.clear()
        self.upload._TURN_REQUEST.clear()
        self.tmp.cleanup()

    def _meta_response(self, path: str, body: dict) -> dict:
        req = _req("response", path, method="POST")
        req["content_type"] = "application/json"
        req["unit_type"] = "json_body"
        self.assertIsNone(
            timely_engine.normalize_upload(req, json.dumps(body, ensure_ascii=False).encode())
        )
        return req

    def _wait_saved(self) -> None:
        import time

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            rows = self.upload._PENDING.get("10.0.50.60") or []
            if rows and all(row["state"] in ("saved", "failed") for row in rows):
                return
            time.sleep(0.01)

    def _upload(self) -> None:
        self._meta_response(
            "/api/generate-file-path-info",
            {
                "data": {
                    "fileName": STORED,
                    "originalName": ORIGINAL,
                    "path": f"timely-file-khdgj/{STORED}",
                    "uploadUrl": WRITE_URL,
                }
            },
        )
        self._meta_response("/api/update-file-status", {"success": True})
        self._meta_response(
            f"/proxy/api/file/timely-file-khdgj/files/by/name/{STORED}/get-url",
            {"url": READ_URL},
        )
        self._wait_saved()

    def test_blob_file_attached_to_send_and_output_merged(self) -> None:
        self._upload()
        self.assertIn(READ_URL, self.fetched)

        send = _req("request", "/timely/api/stream", method="POST")
        send["request_id"] = "send-1"
        payload = json.dumps(
            {"url": "/api-ai/v3/llm-completion", "body": {"message": "이 사진 뭐야", "files": [STORED]}},
            ensure_ascii=False,
        ).encode()
        self.assertFalse(timely_engine.timely_should_skip_policy(send, payload))
        self.assertEqual(timely_engine.extract(send, payload), "이 사진 뭐야")
        built = timely_engine.build_policy_api_payload(send, payload, "이 사진 뭐야")
        self.assertEqual(built["prompt"], "이 사진 뭐야")
        attachment = built["attachments"][0]
        self.assertEqual(attachment["name"], ORIGINAL)
        self.assertEqual(attachment["mime_type"], "image/webp")
        self.assertEqual(attachment["size"], 64)
        self.assertTrue(Path(attachment["id"]).is_file())

        subscribe = _req("request", "/timely/api/stream", method="POST")
        subscribe["request_id"] = "sub-1"
        sub_payload = json.dumps(
            {
                "url": f"/api-ai/v3/llm-completion/stream/subscribe/{SUBSCRIBE}",
                "body": {"lastEventId": "0"},
            }
        ).encode()
        self.assertTrue(timely_engine.timely_should_skip_policy(subscribe, sub_payload))

        ack = _req("response", "/timely/api/stream", method="POST")
        ack["request_id"] = "send-1"
        self.assertEqual(timely_engine.extract(ack, b'data: {"type":"start"}\n\n'), "")

        response = _req("response", "/timely/api/stream", method="POST")
        response["request_id"] = "sub-1"
        sse = (
            b'data: {"type":"final_response","session_id":"s","message":"image",'
            b'"model":"solar-mini"}\n\n'
        )
        self.assertEqual(timely_engine.extract(response, sse), "image")
        self.assertEqual(response["_timely_ui_message_id"], "send-1")
        self.assertNotIn("_timely_deferred_human_prompt", response)

    def test_file_only_send_is_inspected(self) -> None:
        self._upload()
        send = _req("request", "/timely/api/stream", method="POST")
        send["request_id"] = "send-2"
        payload = json.dumps({"url": "/api-ai/v3/llm-completion", "body": {"chat_id": "c"}}).encode()
        self.assertFalse(timely_engine.timely_should_skip_policy(send, payload))
        built = timely_engine.build_policy_api_payload(send, payload, "")
        self.assertEqual(built["prompt"], "")
        self.assertEqual(built["attachments"][0]["name"], ORIGINAL)

    def test_subscribe_does_not_take_pending_file(self) -> None:
        self._upload()
        subscribe = _req("request", "/timely/api/stream", method="POST")
        subscribe["request_id"] = "sub-early"
        sub_payload = json.dumps(
            {
                "url": f"/api-ai/v3/llm-completion/stream/subscribe/{SUBSCRIBE}",
                "body": {"lastEventId": "0"},
            }
        ).encode()
        self.assertTrue(timely_engine.timely_should_skip_policy(subscribe, sub_payload))
        timely_engine.extract(subscribe, sub_payload)
        self.assertNotIn("10.0.50.60", self.upload._TURN_REQUEST)
        self.assertTrue(self.upload._PENDING.get("10.0.50.60"))

    def test_history_preview_download_is_not_attached(self) -> None:
        download = _req("response", "/api/download-file?filename=p_CHAT_01M5S76S28TJED65XCNDNGN5DA.webp")
        download["content_type"] = "image/webp"
        download["unit_type"] = "raw_body"
        result = timely_engine.normalize_upload(download, b"RIFF" + b"x" * 40)
        self.assertTrue(result["handled"])
        self.assertEqual(result["skip_reason"], "timely_history_preview")
        send = _req("request", "/timely/api/stream", method="POST")
        payload = json.dumps({"url": "/api-ai/v3/llm-completion", "body": {"chat_id": "c"}}).encode()
        self.assertTrue(timely_engine.timely_should_skip_policy(send, payload))

    def test_download_bytes_used_when_announced(self) -> None:
        self._meta_response(
            "/api/generate-file-path-info",
            {"items": [{"fileName": STORED, "originalName": ORIGINAL}]},
        )
        download = _req("response", f"/api/download-file?filename={STORED}")
        download["content_type"] = "image/webp"
        download["unit_type"] = "raw_body"
        result = timely_engine.normalize_upload(download, b"RIFF" + b"d" * 20)
        self.assertFalse(result["skipped"])
        self.assertEqual(result["upload_filename"], ORIGINAL)
        send = _req("request", "/timely/api/stream", method="POST")
        built = timely_engine.build_policy_api_payload(send, b"{}", "설명")
        self.assertEqual(built["attachments"][0]["size"], 24)
        self.assertEqual(self.fetched, [])

    def test_write_only_url_falls_back_to_get_url(self) -> None:
        self._upload()
        rows = self.upload._PENDING["10.0.50.60"]
        self.assertEqual(rows[0]["state"], "saved")
        self.assertEqual(self.fetched[-1], READ_URL)

    def test_failed_write_url_retries_when_get_url_arrives(self) -> None:
        self._meta_response(
            "/api/generate-file-path-info",
            {"data": {"fileName": STORED, "originalName": ORIGINAL, "uploadUrl": WRITE_URL}},
        )
        self._meta_response("/api/update-file-status", {"success": True})
        self._wait_saved()
        rows = self.upload._PENDING["10.0.50.60"]
        self.assertEqual(rows[0]["state"], "failed")
        self._meta_response(
            f"/proxy/api/file/timely-file-khdgj/files/by/name/{STORED}/get-url",
            {"url": READ_URL},
        )
        self._wait_saved()
        self.assertEqual(rows[0]["state"], "saved")
        self.assertEqual(self.fetched, [WRITE_URL, READ_URL])

    def test_claim_waits_for_inflight_fetch(self) -> None:
        import threading

        gate = threading.Event()

        def slow_fetch(url: str) -> bytes:
            gate.wait(2)
            return b"RIFF" + b"s" * 10

        self.upload._fetch_blob = slow_fetch
        self._meta_response(
            "/api/generate-file-path-info",
            {"data": {"fileName": STORED, "originalName": ORIGINAL, "uploadUrl": READ_URL}},
        )
        self._meta_response("/api/update-file-status", {"success": True})
        threading.Timer(0.2, gate.set).start()
        send = _req("request", "/timely/api/stream", method="POST")
        built = timely_engine.build_policy_api_payload(send, b"{}", "설명")
        self.assertEqual(built["attachments"][0]["size"], 14)

    def test_side_request_does_not_steal_response_id(self) -> None:
        self._upload()
        side = _req("request", "/api/update-file-status", method="POST")
        side["request_id"] = "side-72"
        side["content_type"] = "application/json"
        timely_engine.extract(side, b'{"ok":true}')
        send = _req("request", "/timely/api/stream", method="POST")
        send["request_id"] = "send-real"
        body = b"\x00not-json" + b"x" * 300
        self.assertEqual(timely_engine.extract(send, body), "")
        self.assertFalse(timely_engine.timely_should_skip_policy(send, body))
        built = timely_engine.build_policy_api_payload(send, body, "")
        self.assertEqual(built["attachments"][0]["name"], ORIGINAL)
        response = _req("response", "/timely/api/stream", method="POST")
        response["request_id"] = "sub-real"
        sse = b'data: {"type":"final_response","session_id":"s","message":"endian","model":"solar-mini"}\n\n'
        self.assertEqual(timely_engine.extract(response, sse), "endian")
        self.assertEqual(response["_timely_ui_message_id"], "send-real")

    def test_sse_title_is_reused_by_the_following_file_send(self) -> None:
        title = _req("response", "/timely/api/stream", method="POST")
        title["request_id"] = "sse-title"
        event = (
            'data: {"type":"edit_chat_title","state":"call","message":"이미지 설명해줘"}\n\n'
        ).encode()
        self.assertEqual(timely_engine.extract(title, event), "")
        self.assertEqual(title["_timely_deferred_human_prompt"], "이미지 설명해줘")
        syn = dict(title)
        syn["direction"] = "request"
        titled = timely_engine.build_policy_api_payload(syn, event, "이미지 설명해줘")
        self.assertEqual(titled["prompt"], "이미지 설명해줘")

        self._upload()
        send = _req("request", "/timely/api/stream", method="POST")
        send["request_id"] = "send-after-title"
        payload = json.dumps({"url": "/api-ai/v3/llm-completion", "body": {"chat_id": "c"}}).encode()
        self.assertEqual(timely_engine.extract(send, payload), "이미지 설명해줘")
        self.assertEqual(send["_timely_ui_message_id"], "sse-title")
        built = timely_engine.build_policy_api_payload(send, payload, "이미지 설명해줘")
        self.assertEqual(built["prompt"], "이미지 설명해줘")
        self.assertEqual(built["attachments"][0]["name"], ORIGINAL)

    def test_title_after_file_send_keeps_the_file_on_the_same_id(self) -> None:
        self._upload()
        send = _req("request", "/timely/api/stream", method="POST")
        send["request_id"] = "send-first"
        payload = json.dumps({"url": "/api-ai/v3/llm-completion", "body": {"chat_id": "c"}}).encode()
        self.assertEqual(timely_engine.extract(send, payload), "")
        filed = timely_engine.build_policy_api_payload(send, payload, "")
        self.assertEqual(filed["attachments"][0]["name"], ORIGINAL)

        title = _req("response", "/timely/api/stream", method="POST")
        title["request_id"] = "sse-later"
        event = 'data: {"type":"edit_chat_title","state":"call","message":"이 파일 봐줘"}\n\n'.encode()
        self.assertEqual(timely_engine.extract(title, event), "")
        self.assertEqual(title["_timely_ui_message_id"], "send-first")
        syn = dict(title)
        syn["direction"] = "request"
        replaced = timely_engine.build_policy_api_payload(syn, event, "이 파일 봐줘")
        self.assertEqual(replaced["prompt"], "이 파일 봐줘")
        self.assertEqual(replaced["attachments"][0]["name"], ORIGINAL)
        self.assertEqual(replaced["attachments"][0]["size"], filed["attachments"][0]["size"])

    def test_multipart_start_keeps_file_prompt_and_model_on_one_id(self) -> None:
        self._upload()
        boundary = "----WebKitFormBoundaryTest"
        content = (
            "[attachment:IMAGE:Big-Endian.webp:abc]"
            "(https://timelyai.io/api/download-file?filename=p.webp)"
            "Solar Mini 4입니다."
        )
        start_body = {
            "messages": [{"role": "user", "content": content}],
            "model": "solar-mini4",
            "files": ["https://example.blob.core.windows.net/a.webp"],
            "stream": True,
        }
        start = self._multipart(
            boundary,
            "/api-ai/v3/llm-completion/stream/start",
            start_body,
        )
        send = _req("request", "/timely/api/stream", method="POST")
        send["request_id"] = "send-file"
        send["content_type"] = f"multipart/form-data; boundary={boundary}"
        self.assertEqual(timely_engine.extract(send, start), "Solar Mini 4입니다.")
        self.assertEqual(send["_llm_model"], "solar-mini4")
        self.assertFalse(timely_engine.timely_should_skip_policy(send, start))
        built = timely_engine.build_policy_api_payload(send, start, "Solar Mini 4입니다.")
        self.assertEqual(built["prompt"], "Solar Mini 4입니다.")
        self.assertEqual(built["model"], "solar-mini4")
        self.assertEqual(built["attachments"][0]["name"], ORIGINAL)

        subscribe = self._multipart(
            boundary,
            "/api-ai/v3/llm-completion/stream/subscribe/01M5SAX1APC1HGE1HA3X02W4TF",
            {"lastEventId": "0"},
        )
        sub = _req("request", "/timely/api/stream", method="POST")
        sub["request_id"] = "subscribe-id"
        sub["content_type"] = send["content_type"]
        self.assertEqual(timely_engine.extract(sub, subscribe), "")
        self.assertTrue(timely_engine.timely_should_skip_policy(sub, subscribe))
        self.assertNotIn("_timely_ui_message_id", sub)

        title = _req("response", "/timely/api/stream", method="POST")
        title["request_id"] = "subscribe-id"
        event = (
            'data: {"type":"edit_chat_title","state":"call","message":"생성된 제목"}\n\n'
            'data: {"type":"final_response","session_id":"s","message":"확인했습니다",'
            '"model":"solar-mini"}\n\n'
        ).encode()
        self.assertEqual(timely_engine.extract(title, event), "확인했습니다")
        self.assertEqual(title["_timely_ui_message_id"], "send-file")
        self.assertNotIn("_timely_deferred_human_prompt", title)
        self.assertEqual(title["_llm_model"], "solar-mini4")
        answer = timely_engine.build_policy_api_payload(title, event, "확인했습니다")
        self.assertEqual(answer["model"], "solar-mini4")
        self.assertEqual(answer["llm_output"], "확인했습니다")

        masked = timely_engine.rewrite_prompt_masking(send, start, "***")
        self.assertIsNotNone(masked)
        rewritten = masked[0].split(b'name="body"\r\n\r\n', 1)[1].split(b"\r\n--", 1)[0]
        self.assertEqual(json.loads(rewritten)["messages"][0]["content"], "***")
        self.assertEqual(json.loads(rewritten)["model"], "solar-mini4")

    def test_model_field_is_read_for_any_service_body(self) -> None:
        payload = b'{"messages":[{"role":"user","content":"hi"}],"model":"gpt-4o"}'
        self.assertEqual(llm_model_for_policy({}, payload, None), "gpt-4o")
        self.assertEqual(llm_model_for_policy({"_llm_model": "claude-sonnet"}, payload, None), "claude-sonnet")
        self.assertEqual(llm_model_for_policy({}, b'{"model":"user"}', None), "")
        self.assertEqual(
            llm_model_for_policy({}, b'{"model":"gpt-6 (instant)"}', None),
            "gpt-6 (instant)",
        )

    def _multipart(self, boundary: str, url: str, body: dict) -> bytes:
        encoded = json.dumps(body, ensure_ascii=False)
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="url"\r\n\r\n'
            f"{url}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="body"\r\n\r\n'
            f"{encoded}\r\n"
            f"--{boundary}--\r\n"
        ).encode()

