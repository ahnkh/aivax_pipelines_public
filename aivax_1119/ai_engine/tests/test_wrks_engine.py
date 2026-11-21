from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


AI_ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AI_ENGINE / "core"))
sys.path.insert(0, str(AI_ENGINE / "adapters"))

import account_lookup  # noqa: E402
import wrks_engine  # noqa: E402


def chat_req(direction: str = "request", path: str = "/conversation/c1/message/stream") -> dict:
    return {
        "direction": direction,
        "request_id": "req-1",
        "content_type": "application/json" if direction == "request" else "text/event-stream",
        "unit_type": "json_body" if direction == "request" else "sse_event",
        "meta": {
            "host": "gateway-api.wrks.ai",
            "path": path,
            "method": "POST" if direction == "request" else "POST",
            "authorization": "Bearer tok-1",
        },
    }


def sse_event(event: str, data: dict) -> bytes:
    payload = {"event": event, "data": data}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


class WrksEnginePromptResponseTest(unittest.TestCase):
    def setUp(self) -> None:
        wrks_engine._WRKS_RESPONSE_BUFFER.clear()
        wrks_engine._WRKS_RESPONSE_MESSAGE_ID.clear()
        wrks_engine._WRKS_RESPONSE_OVERRIDE_EMITTED.clear()
        wrks_engine._WRKS_MODEL_CATALOG.clear()

    def test_models_response_name_is_stored_on_the_next_prompt(self) -> None:
        catalog = chat_req(direction="response", path="/ai/chat/models")
        catalog["content_type"] = "application/json"
        catalog["unit_type"] = "json_body"
        catalog["meta"]["method"] = "GET"
        catalog["_eve_full_body"] = True
        body = json.dumps(
            [{"id": 99, "name": "GPT-5.4"}, {"id": 3, "name": "user"}],
            ensure_ascii=False,
        ).encode()
        self.assertEqual("", wrks_engine.extract_full_body(catalog, body))
        self.assertEqual("GPT-5.4", wrks_engine._WRKS_MODEL_CATALOG.get("99"))
        self.assertNotIn("3", wrks_engine._WRKS_MODEL_CATALOG)

        req = chat_req(path="/ai/chat/stream/48b110f7-b8a5-44c4-9c31-8c0af89b8743")
        prompt_body = json.dumps(
            {
                "id": "48b110f7-b8a5-44c4-9c31-8c0af89b8743",
                "modelId": 99,
                "message": {"role": "user", "parts": [{"type": "text", "text": "안녕"}]},
            }
        ).encode()
        self.assertEqual("안녕", wrks_engine.extract(req, prompt_body))
        self.assertEqual("GPT-5.4", req.get("_wrks_model"))
        import ai_policy_engine

        pol: dict = {}
        ai_policy_engine.stamp_policy_model(req, prompt_body, pol, wrks_engine)
        self.assertEqual("GPT-5.4", pol.get("model"))

    def test_should_skip_policy_only_chat_turn(self) -> None:
        self.assertFalse(wrks_engine.wrks_should_skip_policy(chat_req()))
        other = chat_req(path="/my/info")
        other["meta"]["method"] = "GET"
        self.assertTrue(wrks_engine.wrks_should_skip_policy(other))
        upload = chat_req(path="/file/toOpenai")
        self.assertTrue(wrks_engine.wrks_should_skip_policy(upload))

    def test_extract_prompt_and_mask_block_rewrite(self) -> None:
        body = json.dumps(
            {"message": "secret prompt", "gptFileIds": ["file-a"]},
            ensure_ascii=False,
        ).encode()
        prompt = wrks_engine.extract(chat_req(), body)
        self.assertEqual("secret prompt", prompt)

        masked = wrks_engine.rewrite_prompt_masking(
            chat_req(), body, "***", source_prompt="secret prompt"
        )
        self.assertIsNotNone(masked)
        masked_obj = json.loads(masked[0])
        self.assertEqual("***", masked_obj["message"])
        self.assertEqual([], masked_obj["gptFileIds"])

        blocked = wrks_engine.rewrite_prompt_for_block(
            chat_req(), body, "blocked user text", "sys"
        )
        self.assertIsNotNone(blocked)
        blocked_obj = json.loads(blocked[0])
        self.assertIn("blocked user text", blocked_obj["message"])
        self.assertEqual([], blocked_obj["gptFileIds"])

    def test_extract_sse_response_deltas_and_done(self) -> None:
        req = chat_req(direction="response")
        delta = {
            "id": "msg-1",
            "delta": {"content": [{"type": "text", "text": {"value": "Hel"}}]},
        }
        self.assertEqual("", wrks_engine.extract(req, sse_event("thread.message.delta", delta)))
        self.assertFalse(req.get("_extract_final"))

        done = wrks_engine.extract(req, b"data: [DONE]\n\n")
        self.assertEqual("Hel", done)
        self.assertTrue(req.get("_extract_final"))

    def test_extract_full_body_sse_and_account_fallback(self) -> None:
        chat = chat_req(direction="response")
        body = (
            b"data: {\"event\":\"thread.message.delta\",\"data\":{\"delta\":{\"content\":["
            b"{\"type\":\"text\",\"text\":{\"value\":\"A\"}}]}}}\n\n"
            b"data: {\"event\":\"thread.message.delta\",\"data\":{\"delta\":{\"content\":["
            b"{\"type\":\"text\",\"text\":{\"value\":\"B\"}}]}}}\n\n"
        )
        self.assertEqual("AB", wrks_engine.extract_full_body(chat, body))

        account = {
            "direction": "response",
            "content_type": "application/json",
            "meta": {
                "host": "gateway-api.wrks.ai",
                "path": "/my/info",
                "method": "GET",
                "authorization": "Bearer tok-1",
            },
        }
        info = json.dumps(
            {"data": {"info": {"id": "u1", "email": "a@example.com"}}},
            ensure_ascii=False,
        ).encode()
        self.assertEqual("", wrks_engine.extract_full_body(account, info))
        self.assertFalse(account.get("_extract_final"))

    def test_rewrite_response_emits_block_sse(self) -> None:
        req = chat_req(direction="response")
        payload = sse_event(
            "thread.message.delta",
            {
                "id": "msg-1",
                "delta": {"content": [{"type": "text", "text": {"value": "hi"}}]},
            },
        )
        out = wrks_engine.rewrite_response(req, payload, "BLOCKED")
        self.assertIsNotNone(out)
        encoded, reason = out
        self.assertIn(b"BLOCKED", encoded)
        self.assertTrue(reason.startswith("adapter:wrks"))

    def test_rewrite_response_handles_full_body_stream(self) -> None:
        req = chat_req(direction="response")
        body = b"".join(
            [
                sse_event(
                    "thread.message.delta",
                    {"id": "msg-1", "delta": {"content": [{"type": "text", "text": {"value": "레"}}]}},
                ),
                sse_event(
                    "thread.message.delta",
                    {"id": "msg-1", "delta": {"content": [{"type": "text", "text": {"value": "저"}}]}},
                ),
                sse_event(
                    "thread.message.completed",
                    {"id": "msg-1", "content": [{"type": "text", "text": {"value": "레저 답변"}}]},
                ),
                b"data: [DONE]\n\n",
            ]
        )

        out = wrks_engine.rewrite_response(req, body, "BLOCKED")
        self.assertIsNotNone(out)
        encoded, _ = out
        text = encoded.decode()
        self.assertNotIn("레저", text)
        self.assertEqual(2, text.count("BLOCKED"))
        self.assertIn("data: [DONE]", text)
        events = [
            json.loads(line[len("data: "):])
            for line in text.split("\n\n")
            if line.startswith("data: ") and not line.endswith("[DONE]")
        ]
        self.assertEqual(
            "BLOCKED",
            events[0]["data"]["delta"]["content"][0]["text"]["value"],
        )
        self.assertEqual(
            "",
            events[1]["data"]["delta"]["content"][0]["text"]["value"],
        )
        self.assertEqual(
            "BLOCKED",
            events[2]["data"]["content"][0]["text"]["value"],
        )


class WrksAccountNameTest(unittest.TestCase):
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
        self._tmp.cleanup()

    def test_my_info_name_is_kept_for_the_later_prompt(self) -> None:
        account = {
            "direction": "response",
            "content_type": "application/json",
            "meta": {
                "host": "gateway-api.wrks.ai",
                "path": "/my/info",
                "method": "GET",
                "authorization": "Bearer tok-name",
            },
        }
        info = json.dumps(
            {
                "data": {
                    "info": {
                        "id": 382004,
                        "email": "chong10@wins21.co.kr",
                        "name": "이지현",
                        "workspace": {"id": 7059, "name": "웍스AI 개인"},
                    }
                }
            }
        ).encode()
        self.assertEqual("", wrks_engine.extract_full_body(account, info))
        named = chat_req()
        named["meta"]["authorization"] = "Bearer tok-name"
        built = wrks_engine.build_policy_api_payload(
            named,
            json.dumps({"message": "안녕"}).encode(),
            "안녕",
        )
        self.assertEqual(built["email"], "chong10@wins21.co.kr")
        self.assertEqual(built["user_id"], "382004")
        self.assertEqual(built["user_name"], "이지현")

    def test_missing_person_name_does_not_use_the_workspace_title(self) -> None:
        account = {
            "direction": "response",
            "content_type": "application/json",
            "meta": {
                "host": "gateway-api.wrks.ai",
                "path": "/my/info",
                "method": "GET",
                "authorization": "Bearer tok-noname",
            },
        }
        info = json.dumps(
            {
                "data": {
                    "info": {
                        "id": 268762,
                        "email": "juyonglee@wins21.co.kr",
                        "name": None,
                        "workspace": {"name": "웍스AI 개인"},
                    }
                }
            }
        ).encode()
        self.assertEqual("", wrks_engine.extract_full_body(account, info))
        stored = account_lookup.get_result("wrks", "tok-noname")
        self.assertEqual(stored.email, "juyonglee@wins21.co.kr")
        self.assertEqual(stored.display_name, "")


if __name__ == "__main__":
    unittest.main()
