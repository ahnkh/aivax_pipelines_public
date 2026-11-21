"""VS Code GitHub Copilot chat: /chat/completions prompt and SSE answer."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT))

from adapters import github_copilot_engine as copilot  # noqa: E402


def _req(path: str, *, direction: str = "request", unit: str = "json_body", host: str = "api.individual.githubcopilot.com") -> dict:
    return {
        "direction": direction,
        "unit_type": unit,
        "content_type": "application/json" if unit == "json_body" else "text/event-stream",
        "request_id": "req-copilot-1",
        "meta": {
            "host": host,
            "path": path,
            "method": "POST",
            "client_ip": "10.0.50.60",
        },
    }


class GithubCopilotVscodeTests(unittest.TestCase):
    def setUp(self) -> None:
        # Turn state spans requests by design, so each test starts clean.
        copilot._RESPONSE_BUFFER.clear()
        copilot._TOOL_CALL_STREAMS.clear()
        copilot._TOOL_WAIT_BY_CLIENT.clear()
        copilot._TURN_PARENT_BY_KEY.clear()
        copilot._PARENT_BY_REQUEST.clear()
        copilot._SKIP_RESPONSE_IDS.clear()
        copilot._SKIP_RESPONSE_SET.clear()

    def test_chat_completions_user_request_tag(self) -> None:
        body = {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "you are copilot"},
                {
                    "role": "user",
                    "content": "<context>file.py</context>\n<userRequest>\n연차 규정 알려줘\n</userRequest>",
                },
            ],
            "stream": True,
        }
        raw = json.dumps(body).encode()
        req = _req("/chat/completions")
        self.assertFalse(copilot.github_copilot_should_skip_policy(req, raw))
        self.assertEqual(copilot.extract(req, raw), "연차 규정 알려줘")

    def test_content_array_and_v1_path(self) -> None:
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "<user_query>비밀번호는 secret</user_query>"},
                    ],
                }
            ]
        }
        raw = json.dumps(body).encode()
        req = _req("/v1/chat/completions")
        self.assertFalse(copilot.github_copilot_should_skip_policy(req, raw))
        self.assertEqual(copilot.extract(req, raw), "비밀번호는 secret")
        rewritten, reason = copilot.rewrite_prompt(req, raw, "[masked]")
        self.assertIn("github_copilot_policy_rewrite", reason)
        self.assertEqual(copilot.extract(req, rewritten), "[masked]")

    def test_datetime_wrapper_keeps_only_the_typed_prompt(self) -> None:
        body = {
            "model": "gpt-4o",
            "input": [
                {
                    "role": "user",
                    "content": (
                        "<current_datetime>2026-11-01T23:51:11.935+09:00</current_datetime>\n\n"
                        "Hello Copilot!!"
                    ),
                }
            ],
        }
        raw = json.dumps(body).encode()
        req = _req("/responses")
        self.assertEqual(copilot.extract(req, raw), "Hello Copilot!!")
        self.assertFalse(copilot.github_copilot_should_skip_policy(req, raw))

    def test_previous_current_scaffold_is_not_a_user_prompt(self) -> None:
        body = {
            "input": [
                {
                    "role": "user",
                    "content": (
                        "[PREVIOUS MESSAGE]\nHello Copilot!!\n\n"
                        "[CURRENT MESSAGE]\ntest1234"
                    ),
                }
            ]
        }
        raw = json.dumps(body).encode()
        req = _req("/responses")
        req["request_id"] = "scaffold-1"
        self.assertEqual(copilot.extract(req, raw), "")
        self.assertTrue(copilot.github_copilot_should_skip_policy(req, raw))
        answer = _req("/responses", direction="response", unit="sse_event")
        answer["request_id"] = "scaffold-1"
        sse = (
            'data: {"type":"response.output_text.delta","delta":"{\\"frustration\\":false}"}\n\n'
            'data: {"type":"response.completed"}\n\n'
        ).encode()
        self.assertEqual(copilot.extract(answer, sse), "")

    def test_title_request_and_system_reminder_are_not_recorded(self) -> None:
        title = {
            "messages": [
                {"role": "system", "content": "You title chats."},
                {
                    "role": "user",
                    "content": (
                        "Please write a brief title for the following conversation:\n\n"
                        "User request:\nHello Copilot!!\n"
                    ),
                },
            ]
        }
        raw = json.dumps(title).encode()
        req = _req("/chat/completions")
        req["request_id"] = "title-1"
        self.assertEqual(copilot.extract(req, raw), "")
        self.assertTrue(copilot.github_copilot_should_skip_policy(req, raw))
        hidden = {
            "messages": [
                {"role": "system", "content": "Please write a brief title for the following conversation."},
                {"role": "user", "content": "Hello Copilot!!"},
            ]
        }
        hidden_req = _req("/chat/completions")
        self.assertEqual(copilot.extract(hidden_req, json.dumps(hidden).encode()), "")
        reminder = {
            "input": [
                {"role": "user", "content": "<system_reminder>\nIMPORTANT: tools are deferred.\n</system_reminder>"}
            ]
        }
        self.assertEqual(copilot.extract(_req("/responses"), json.dumps(reminder).encode()), "")

    def test_followup_keeps_the_new_question_when_history_has_scaffolds(self) -> None:
        question = "왜 이렇게 응답이 느린거지?\n코파일럿 응답이 느리건가?"
        body = {
            "instructions": (
                "You are Copilot. When a chat has no name, please write a brief "
                "title for the following conversation.\n" + ("tool rule\n" * 40)
            ),
            "input": [
                {
                    "role": "user",
                    "content": (
                        "Please write a brief title for the following conversation:\n\n"
                        "User request:\nHello World!\n"
                    ),
                },
                {"role": "assistant", "content": "greeting"},
                {
                    "role": "user",
                    "content": (
                        "<system_reminder>\nTools listed below are deferred.\n</system_reminder>\n\n"
                        "<current_datetime>2026-11-02T00:26:00+09:00</current_datetime>\n\n"
                        "<user_query>" + question + "</user_query>"
                    ),
                },
            ],
        }
        raw = json.dumps(body).encode()
        req = _req("/responses")
        req["request_id"] = "followup-1"
        self.assertFalse(copilot.github_copilot_should_skip_policy(req, raw))
        self.assertEqual(copilot.extract(req, raw), question)
        answer = _req("/responses", direction="response", unit="sse_event")
        answer["request_id"] = "followup-1"
        sse = (
            'data: {"type":"response.output_text.delta","delta":"서버 대기열"}\n\n'
            'data: {"type":"response.completed"}\n\n'
        ).encode()
        self.assertEqual(copilot.extract(answer, sse), "서버 대기열")

    def test_inline_completion_is_not_a_chat_prompt(self) -> None:
        raw = json.dumps({"prompt": "def add(a, b):\n", "suffix": "\n"}).encode()
        req = _req("/v1/engines/gpt-41-copilot/completions", host="proxy.individual.githubcopilot.com")
        self.assertTrue(copilot.github_copilot_should_skip_policy(req, raw))

    def test_chat_sse_full_body_merges_every_event(self) -> None:
        req = _req("/chat/completions", direction="response", unit="sse_event")
        body = (
            'data: {"choices":[{"index":0,"delta":{"content":"AIVAX"}}]}\n\n'
            'data: {"choices":[{"index":0,"delta":{"content":" OK"}}]}\n\n'
            "data: [DONE]\n\n"
        ).encode()
        self.assertEqual(copilot.extract(req, body), "AIVAX OK")
        self.assertTrue(req.get("_extract_final"))

    def test_chat_sse_deltas_merge(self) -> None:
        req = _req("/chat/completions", direction="response", unit="sse_event")
        first = json.dumps({"choices": [{"index": 0, "delta": {"content": "연차"}, "finish_reason": None}]}).encode()
        second = json.dumps({"choices": [{"index": 0, "delta": {"content": " 규정"}, "finish_reason": None}]}).encode()
        done = json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}).encode()
        self.assertEqual(copilot.extract(req, first), "연차")
        self.assertEqual(copilot.extract(req, second), "연차 규정")
        self.assertEqual(copilot.extract(req, done), "연차 규정")
        self.assertTrue(req.get("_extract_final"))

    def test_github_profile_is_attached_to_the_chat_payload(self) -> None:
        import os

        os.environ["AIVAX_ACCOUNT_CACHE_PATH"] = ""
        import account_lookup

        account_lookup.configure(maxsize=32, ttl_seconds=60, shards=2)
        copilot._SESSION_TO_GITHUB.clear()
        copilot._GITHUB_SESSIONS.clear()
        copilot._MODEL_BY_CLIENT.clear()
        try:
            profile = _req("/user", direction="response", host="api.github.com")
            profile["meta"]["method"] = "GET"
            profile["meta"]["authorization"] = "Bearer gho_test_user"
            copilot.extract(
                profile,
                json.dumps(
                    {
                        "login": "octocat",
                        "id": 7,
                        "name": "Mona Lisa",
                        "email": "mona@example.com",
                    }
                ).encode(),
            )
            minted = _req("/copilot_internal/v2/token", direction="response", host="api.github.com")
            minted["meta"]["authorization"] = "token gho_test_user"
            copilot.extract(minted, json.dumps({"token": "tid=abc;sku=free", "sku": "free"}).encode())
            emails = _req("/user/emails", direction="response", host="api.github.com")
            emails["meta"]["authorization"] = "Bearer gho_test_user"
            copilot.extract(
                emails,
                json.dumps(
                    [
                        {"email": "other@example.com", "primary": False, "verified": True},
                        {"email": "mona@example.com", "primary": True, "verified": True},
                    ]
                ).encode(),
            )
            chat = _req("/responses")
            chat["meta"]["authorization"] = "Bearer tid=abc;sku=free"
            raw = json.dumps(
                {"model": "gpt-4o", "input": [{"role": "user", "content": "장점과 단점을 비교"}]}
            ).encode()
            payload = copilot.build_policy_api_payload(chat, raw, "장점과 단점을 비교")
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(payload["user_id"], "octocat")
            self.assertEqual(payload["email"], "mona@example.com")
            self.assertEqual(payload["user_name"], "Mona Lisa")
            self.assertEqual(payload["model"], "gpt-4o")
            self.assertEqual(payload["ai_service"], 9)
        finally:
            if account_lookup._shared is not None:
                account_lookup._shared.close()
                account_lookup._shared = None

    def test_proxy_host_maps_to_copilot(self) -> None:
        import ai_policy_engine

        req = _req("/v1/engines/gpt-41-copilot/completions", host="copilot-proxy.githubusercontent.com")
        self.assertEqual(
            ai_policy_engine.PolicyEngine._resolve_adapter_service_id(req, "gpt"),
            "github_copilot",
        )
        self.assertEqual(
            ai_policy_engine._host_to_service_id("api.githubcopilot.com", "/chat/completions"),
            "github_copilot",
        )
        self.assertEqual(
            ai_policy_engine._host_to_service_id("api.github.com", "/user"),
            "github_copilot",
        )
        self.assertEqual(
            ai_policy_engine._host_to_service_id("api.github.com", "/repos/octocat/hello"),
            "",
        )

    def test_context_blocks_around_the_typed_question_are_not_the_prompt(self) -> None:
        question = "runner 등록이 이 로그에서 왜 실패하나요?"
        body = {
            "model": "gpt-5",
            "input": [
                {
                    "role": "user",
                    "content": (
                        "<ide_selection>\nThe user has the following text selected.\n"
                        "File: frontend.yaml (line 1)\n```\nimage: web\n```\n</ide_selection>\n\n"
                        + question
                        + "\n\n<system_reminder>\nThe following attached files are read-only.\n"
                        "- c:\\Users\\kim\\Pasted Image.png\n</system_reminder>\n\n"
                        "<tagged_files>\n* c:\\Users\\kim\\MKS_YAML\\frontend.yaml (22 lines)\n</tagged_files>\n"
                        "Image file at path c:\\Users\\kim\\Pasted Image.png\n"
                    ),
                }
            ],
        }
        raw = json.dumps(body).encode()
        req = _req("/responses")
        self.assertEqual(copilot.extract(req, raw), question)
        self.assertFalse(copilot.github_copilot_should_skip_policy(req, raw))

    def test_user_query_keeps_only_the_typed_text_when_context_is_nested(self) -> None:
        question = "이 설정으로 배포하면 되나요?"
        body = {
            "input": [
                {
                    "role": "user",
                    "content": (
                        "<user_query>\n"
                        + question
                        + "\n\n<tagged_files>\n* frontend.yaml (22 lines)\n</tagged_files>\n"
                        "</user_query>"
                    ),
                }
            ]
        }
        self.assertEqual(copilot.extract(_req("/responses"), json.dumps(body).encode()), question)

    def test_new_activity_call_is_not_a_user_turn(self) -> None:
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "<new-activity>\nUse these rounds to decide whether the todo list needs updating:\n"
                        "<round index=\"15\">\n<response>\n각 TC를 판정하겠습니다.\n</response>\n"
                        "</round>\n</new-activity>"
                    ),
                }
            ],
            "max_tokens": 256,
            "tools": [],
            "stream": True,
        }
        raw = json.dumps(body).encode()
        req = _req("/chat/completions")
        req["request_id"] = "activity-1"
        self.assertEqual(copilot.extract(req, raw), "")
        self.assertTrue(copilot.github_copilot_should_skip_policy(req, raw))

    def test_oneshot_generators_are_not_user_turns_but_tagged_chat_is(self) -> None:
        summary = {
            "messages": [
                {"role": "user", "content": "Summarize the following content in a SINGLE sentence.\n\nContent: Exploring Argo CD"}
            ],
            "max_tokens": 64,
            "n": 1,
            "stream": True,
        }
        raw = json.dumps(summary).encode()
        req = _req("/chat/completions")
        self.assertEqual(copilot.extract(req, raw), "")
        self.assertTrue(copilot.github_copilot_should_skip_policy(req, raw))
        progress = {
            "messages": [
                {"role": "user", "content": "Please generate exactly 10 unique progress messages for the edit code scenario."}
            ],
            "max_tokens": 200,
            "temperature": 0,
        }
        self.assertEqual(copilot.extract(_req("/chat/completions"), json.dumps(progress).encode()), "")
        nes = {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "<|recently_viewed_code_snippets|>\n\n<|/recently_viewed_code_snippets|>\n"
                        "<|current_file_content|>\ncurrent_file_path: a.tsx\n<|/current_file_content|>\n"
                        "please continue the developer's work."
                    ),
                }
            ],
            "prediction": {"type": "content", "content": "const x = 1;"},
            "stream": True,
        }
        self.assertEqual(copilot.extract(_req("/chat/completions"), json.dumps(nes).encode()), "")
        tagged = {
            "messages": [{"role": "user", "content": "<user_query>연차 규정</user_query>"}],
            "max_tokens": 2048,
            "stream": True,
        }
        self.assertEqual(copilot.extract(_req("/chat/completions"), json.dumps(tagged).encode()), "연차 규정")
        continued = {
            "messages": [
                {"role": "user", "content": "처음 질문"},
                {"role": "assistant", "content": "첫 답"},
                {"role": "user", "content": "이어서 물어본 문장"},
            ],
            "max_tokens": 2048,
        }
        self.assertEqual(
            copilot.extract(_req("/chat/completions"), json.dumps(continued).encode()),
            "이어서 물어본 문장",
        )

    def test_trailing_sse_event_does_not_drop_a_finished_answer(self) -> None:
        req = _req("/chat/completions", direction="response", unit="sse_event")
        req["request_id"] = "sse-final-1"
        body = (
            'data: {"choices":[{"index":0,"delta":{"content":"AIVAX"}}]}\n\n'
            'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        ).encode()
        self.assertEqual(copilot.extract(req, body), "AIVAX")
        self.assertTrue(req.get("_extract_final"))

        resp = _req("/responses", direction="response", unit="sse_event")
        resp["request_id"] = "resp-final-1"
        sse = (
            'data: {"type":"response.output_text.delta","delta":"최종"}\n\n'
            'data: {"type":"response.output_text.done","text":"최종 답"}\n\n'
            'data: {"type":"response.completed","response":{"id":"resp_1","output":['
            '{"type":"message","content":[{"type":"output_text","text":"최종 답"}]}]}}\n\n'
            "data: [DONE]\n\n"
        ).encode()
        self.assertEqual(copilot.extract(resp, sse), "최종 답")
        self.assertTrue(resp.get("_extract_final"))

    def test_narration_beside_a_client_tool_call_is_not_the_answer(self) -> None:
        resp = _req("/responses", direction="response", unit="sse_event")
        resp["request_id"] = "resp-tool-1"
        sse = (
            'data: {"type":"response.output_text.delta","delta":"파일을 "}\n\n'
            'data: {"type":"response.output_text.done","text":"파일을 확인하겠습니다."}\n\n'
            'data: {"type":"response.completed","response":{"id":"resp_t","output":['
            '{"type":"message","content":[{"type":"output_text","text":"파일을 확인하겠습니다."}]},'
            '{"type":"function_call","call_id":"call_1","name":"read_file","arguments":"{}"}]}}\n\n'
            "data: [DONE]\n\n"
        ).encode()
        self.assertEqual(copilot.extract(resp, sse), "")
        self.assertFalse(resp.get("_extract_final"))

    def test_server_side_tool_keeps_the_answer(self) -> None:
        resp = _req("/responses", direction="response", unit="sse_event")
        resp["request_id"] = "resp-websearch-1"
        sse = (
            'data: {"type":"response.output_text.done","text":"검색 결과입니다."}\n\n'
            'data: {"type":"response.completed","response":{"id":"resp_w","output":['
            '{"type":"web_search_call","status":"completed"},'
            '{"type":"message","content":[{"type":"output_text","text":"검색 결과입니다."}]}]}}\n\n'
        ).encode()
        self.assertEqual(copilot.extract(resp, sse), "검색 결과입니다.")
        self.assertTrue(resp.get("_extract_final"))

    def test_chat_completions_tool_round_is_not_the_answer(self) -> None:
        resp = _req("/chat/completions", direction="response", unit="sse_event")
        resp["request_id"] = "chat-tool-1"
        sse = (
            'data: {"choices":[{"index":0,"delta":{"content":"확인하겠습니다."}}]}\n\n'
            'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
            '"id":"call_1","function":{"name":"read_file","arguments":"{}"}}]}}]}\n\n'
            'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n'
            "data: [DONE]\n\n"
        ).encode()
        self.assertEqual(copilot.extract(resp, sse), "")
        self.assertFalse(resp.get("_extract_final"))

    def test_agent_tool_round_replays_the_question_without_logging_it(self) -> None:
        question = {"role": "user", "content": [{"type": "input_text", "text": "배포 상태 확인해줘"}]}
        first = _req("/responses")
        first["request_id"] = "turn-round-1"
        body = json.dumps({"model": "gpt-5", "input": [question], "stream": True}).encode()
        self.assertFalse(copilot.github_copilot_should_skip_policy(first, body))
        self.assertEqual(copilot.extract(first, body), "배포 상태 확인해줘")

        later = _req("/responses")
        later["request_id"] = "turn-round-2"
        replay = json.dumps(
            {
                "model": "gpt-5",
                "input": [
                    question,
                    {"type": "reasoning", "summary": []},
                    {"type": "function_call", "call_id": "call_1", "name": "read_file"},
                    {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
                ],
                "stream": True,
            }
        ).encode()
        self.assertTrue(copilot.github_copilot_should_skip_policy(later, replay))
        self.assertEqual(copilot.extract(later, replay), "")

        # The finished answer arrives on a continuation round, and has to land
        # on the row that holds the question.
        answer = _req("/responses", direction="response", unit="sse_event")
        answer["request_id"] = later["request_id"]
        sse = (
            'data: {"type":"response.output_text.done","text":"배포는 정상입니다."}\n\n'
            'data: {"type":"response.completed","response":{"id":"resp_done","output":['
            '{"type":"message","content":[{"type":"output_text","text":"배포는 정상입니다."}]}]}}\n\n'
        ).encode()
        self.assertEqual(copilot.extract(answer, sse), "배포는 정상입니다.")
        self.assertTrue(answer.get("_extract_final"))
        self.assertEqual(answer.get("_github_copilot_ui_message_id"), first["request_id"])

    def test_follow_up_question_after_an_answer_is_a_new_turn(self) -> None:
        req = _req("/responses")
        body = json.dumps(
            {
                "model": "gpt-5",
                "input": [
                    {"role": "user", "content": [{"type": "input_text", "text": "첫 질문"}]},
                    {"role": "assistant", "content": [{"type": "output_text", "text": "답"}]},
                    {"role": "user", "content": [{"type": "input_text", "text": "두 번째 질문"}]},
                ],
                "stream": True,
            }
        ).encode()
        self.assertFalse(copilot.github_copilot_should_skip_policy(req, body))
        self.assertEqual(copilot.extract(req, body), "두 번째 질문")

    def test_compaction_request_injected_mid_tool_loop_is_not_a_prompt(self) -> None:
        req = _req("/responses")
        req["request_id"] = "compact-1"
        body = json.dumps(
            {
                "model": "gpt-5",
                "input": [
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "로그 확인해줘"}]},
                    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "확인하겠습니다."}]},
                    {"type": "function_call", "call_id": "call_1", "name": "read_file"},
                    {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "The conversation has grown too large for the context "
                                "window and must be compacted now.",
                            }
                        ],
                    },
                ],
                "stream": True,
            }
        ).encode()
        self.assertTrue(copilot.github_copilot_should_skip_policy(req, body))
        self.assertEqual(copilot.extract(req, body), "")

        # The summary it produces is not an answer to anyone.
        resp = _req("/responses", direction="response", unit="sse_event")
        resp["request_id"] = req["request_id"]
        sse = (
            'data: {"type":"response.output_text.done","text":"<analysis>...</analysis>"}\n\n'
            'data: {"type":"response.completed","response":{"id":"r","output":['
            '{"type":"message","content":[{"type":"output_text","text":"<analysis>...</analysis>"}]}]}}\n\n'
        ).encode()
        self.assertEqual(copilot.extract(resp, sse), "")
        self.assertFalse(resp.get("_extract_final"))

    def test_pinned_tool_choice_marks_a_generated_request(self) -> None:
        req = _req("/responses")
        req["request_id"] = "pinned-1"
        body = json.dumps(
            {
                "model": "gpt-5",
                "input": [
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "요약해"}]}
                ],
                "tool_choice": "none",
                "stream": True,
            }
        ).encode()
        self.assertTrue(copilot.github_copilot_should_skip_policy(req, body))

    def test_replayed_history_after_compaction_keeps_only_the_answer(self) -> None:
        req = _req("/responses")
        req["request_id"] = "replay-1"
        replay = (
            "Some of the conversation history has been summarized to free up context.\n"
            "<user_message>처음 질문</user_message>\n"
            "<user_message>이어서 한 질문</user_message>\n"
            "<summary>지금까지 한 일</summary>"
        )
        body = json.dumps(
            {
                "model": "gpt-5",
                "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": replay}]}],
                "stream": True,
            }
        ).encode()
        self.assertTrue(copilot.github_copilot_should_skip_policy(req, body))
        self.assertEqual(copilot.extract(req, body), "")

        resp = _req("/responses", direction="response", unit="sse_event")
        resp["request_id"] = req["request_id"]
        sse = (
            'data: {"type":"response.output_text.done","text":"이어서 작업했습니다."}\n\n'
            'data: {"type":"response.completed","response":{"id":"r2","output":['
            '{"type":"message","content":[{"type":"output_text","text":"이어서 작업했습니다."}]}]}}\n\n'
        ).encode()
        self.assertEqual(copilot.extract(resp, sse), "이어서 작업했습니다.")
        self.assertTrue(resp.get("_extract_final"))

    def test_subagent_thread_opened_by_a_pending_tool_call_is_not_a_prompt(self) -> None:
        parent = _req("/responses")
        parent["request_id"] = "parent-1"
        question = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "프로파일러 분석해줘"}]}
        parent_body = json.dumps({"model": "gpt-5", "input": [question], "stream": True}).encode()
        self.assertFalse(copilot.github_copilot_should_skip_policy(parent, parent_body))

        # The round ends by handing a tool back to the editor.
        parent_resp = _req("/responses", direction="response", unit="sse_event")
        parent_resp["request_id"] = parent["request_id"]
        copilot.extract(
            parent_resp,
            (
                'data: {"type":"response.output_text.done","text":"분석하겠습니다."}\n\n'
                'data: {"type":"response.completed","response":{"id":"r3","output":['
                '{"type":"message","content":[{"type":"output_text","text":"분석하겠습니다."}]},'
                '{"type":"function_call","call_id":"call_9","name":"runSubagent"}]}}\n\n'
            ).encode(),
        )

        spawn = _req("/responses")
        spawn["request_id"] = "spawn-1"
        spawn_body = json.dumps(
            {
                "model": "gpt-5",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Read-only exploration only. In workspace /tmp, analyze …"}
                        ],
                    }
                ],
                "stream": True,
            }
        ).encode()
        self.assertTrue(copilot.github_copilot_should_skip_policy(spawn, spawn_body))
        self.assertEqual(copilot.extract(spawn, spawn_body), "")

    def test_new_chat_while_an_ordinary_tool_runs_is_still_a_prompt(self) -> None:
        running = _req("/responses", direction="response", unit="sse_event")
        running["request_id"] = "reader-1"
        copilot.extract(
            running,
            (
                'data: {"type":"response.completed","response":{"id":"r4","output":['
                '{"type":"function_call","call_id":"call_3","name":"read_file"}]}}\n\n'
            ).encode(),
        )

        typed = _req("/responses")
        typed["request_id"] = "typed-1"
        body = json.dumps(
            {
                "model": "gpt-5",
                "input": [
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "다른 질문"}]}
                ],
                "stream": True,
            }
        ).encode()
        self.assertFalse(copilot.github_copilot_should_skip_policy(typed, body))
        self.assertEqual(copilot.extract(typed, body), "다른 질문")

    def test_new_chat_without_a_pending_tool_call_is_a_prompt(self) -> None:
        req = _req("/responses")
        req["request_id"] = "fresh-1"
        body = json.dumps(
            {
                "model": "gpt-5",
                "input": [
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "새 질문입니다"}]}
                ],
                "stream": True,
            }
        ).encode()
        self.assertFalse(copilot.github_copilot_should_skip_policy(req, body))
        self.assertEqual(copilot.extract(req, body), "새 질문입니다")


if __name__ == "__main__":
    unittest.main()
