#!/usr/bin/env python3
"""user_consent on/off — one-shot pending replay, no cross-chat IP fallback."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(ROOT))

import ai_policy_engine as ape  # noqa: E402


@contextmanager
def _pinned_notice_style(style: str):
    """안내 모드를 이 구간만 고정하고, 끝나면 이전 값으로 되돌린다."""
    key = "AIVAX_BLOCK_NOTICE_STYLE"
    previous = os.environ.get(key)
    os.environ[key] = style
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


class _FakeAdapter:
    def extract(self, req: Dict[str, Any], payload: bytes) -> str:
        try:
            obj = json.loads(payload.decode("utf-8"))
            if isinstance(obj, dict) and obj.get("prompt") is not None:
                return str(obj.get("prompt") or "")
        except Exception:
            pass
        return str(req.get("_fake_extract") or "")

    def extract_session_id(self, req: Dict[str, Any], payload: bytes) -> str:
        return str(req.get("_fake_session") or "")

    def rewrite_prompt(
        self,
        req: Dict[str, Any],
        payload: bytes,
        replacement_text: str,
        policy_code: str,
    ) -> Optional[Tuple[bytes, str]]:
        body = json.dumps({"prompt": replacement_text}, ensure_ascii=False).encode("utf-8")
        return body, f"adapter:fake_{str(policy_code or '').lower()}"

    def rewrite_prompt_masking(
        self,
        req: Dict[str, Any],
        payload: bytes,
        replacement_text: str,
        source_prompt: str = "",
    ) -> Optional[Tuple[bytes, str]]:
        _ = source_prompt
        body = json.dumps({"prompt": replacement_text}, ensure_ascii=False).encode("utf-8")
        return body, "adapter:fake_masking"

    def rewrite_prompt_for_block(
        self,
        req: Dict[str, Any],
        payload: bytes,
        user_prompt: str,
        system_instruction: str,
        *args: Any,
        **kwargs: Any,
    ) -> Optional[Tuple[bytes, str]]:
        body = json.dumps(
            {"prompt": user_prompt, "sys": system_instruction},
            ensure_ascii=False,
        ).encode("utf-8")
        return body, "adapter:fake_block"

    def chatgpt_should_skip_policy(self, req: Dict[str, Any], payload: bytes) -> bool:
        return False

    def collect_consent_session_ids(self, req: Dict[str, Any], payload: bytes) -> list:
        extra = req.get("_fake_consent_sessions")
        if isinstance(extra, list):
            return [str(x) for x in extra]
        return []


def _engine() -> ape.PolicyEngine:
    eng = ape.PolicyEngine(
        keywords=[],
        payload_preview_bytes=64,
        log_payload=False,
        rewrite_from="",
        rewrite_to="",
        adapters_dir=str(ROOT / "adapters"),
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

    async def _noop_register(*_a, **_k):
        return None

    eng._register_user_consent_input_log = _noop_register  # type: ignore[method-assign]
    return eng


def _req(
    extract: str,
    session: str = "conv-1",
    request_id: str = "r1",
    service_id: str = "chatgpt",
    host: str = "chatgpt.com",
    path: str = "/backend-api/f/conversation",
) -> Dict[str, Any]:
    return {
        "request_id": request_id,
        "service_id": service_id,
        "direction": "request",
        "unit_type": "json_body",
        "content_type": "application/json",
        "meta": {
            "host": host,
            "path": path,
            "client_ip": "10.0.0.8",
        },
        "_fake_extract": extract,
        "_fake_session": session,
    }


def _payload_has_consent_hint(payload: bytes) -> bool:
    text = payload.decode("utf-8", errors="replace")
    return (
        ("[주의]" in text or "⚠️ 주의" in text)
        and "'동의'" in text
        and "'동의합니다'" in text
        and "전송에 대한 책임은 사용자에게 있습니다." in text
    )


class TestUserConsentHelpers(unittest.TestCase):
    def test_phrase_exact_only(self) -> None:
        self.assertTrue(ape.is_user_consent_phrase("동의"))
        self.assertTrue(ape.is_user_consent_phrase("  동의합니다  "))
        self.assertTrue(ape.is_user_consent_phrase("동의합니다."))
        self.assertTrue(ape.is_user_consent_phrase("동의합니다!"))
        self.assertFalse(ape.is_user_consent_phrase("동의하고 계속"))
        self.assertFalse(ape.is_user_consent_phrase("please 동의"))
        self.assertFalse(ape.is_user_consent_phrase(""))

    def test_normalize_and_hint(self) -> None:
        with _pinned_notice_style("default"):
            self._assert_default_notice_cards()
        with _pinned_notice_style("guideline"):
            self._assert_guideline_notice_cards()

    def _assert_default_notice_cards(self) -> None:
        self.assertEqual(ape.normalize_user_consent_value("ON"), "on")
        self.assertEqual(ape.normalize_user_consent_value("true"), "on")
        self.assertEqual(ape.normalize_user_consent_value(""), "off")
        self.assertEqual(ape.normalize_user_consent_ttl_sec(""), 600)
        self.assertEqual(ape.normalize_user_consent_ttl_sec("300"), 300)
        self.assertEqual(ape.normalize_user_consent_ttl_sec(1), 10)
        self.assertEqual(ape.normalize_user_consent_ttl_sec("nope"), 600)
        notice = ape.append_user_consent_hint("차단되었습니다.")
        self.assertIn("동의", notice)
        self.assertEqual(ape.append_user_consent_hint(notice), notice)
        ordered = ape.append_user_consent_hint(
            "[AIVAX MASKING]\n\n"
            "[AIVAX] 프롬프트 차단\n\n"
            "🔒 민감정보가 프롬프트에서 탐지되어 요청이 차단되었습니다.\n\n"
            "📛 탐지 유형: IP - v4\n\n"
            "⚠️ 민감 정보 전송 시 기밀·개인정보 유출 위험이 있습니다. 각별히 주의해 주세요.\n\n"
            "요청하신 프롬프트는 AIVAX 정책에 의해 차단되었습니다.\n\n"
            "세부 사항은 관리자에게 문의해 주세요."
        )
        lock_at = ordered.find("요청이 차단되었습니다.")
        hint_at = ordered.find("[⚠️ 주의]")
        detect_at = ordered.find("📛 탐지 유형")
        self.assertTrue(0 <= lock_at < hint_at < detect_at)
        self.assertLess(ordered.find("전송에 대한 책임은 사용자에게 있습니다."), detect_at)
        notice_body = (
            "[AIVAX] 프롬프트 차단\n\n"
            "🔒 민감정보가 프롬프트에서 탐지되어 요청이 차단되었습니다.\n\n"
            "📛 탐지 유형: IP - v4\n\n"
            "⚠️ 민감 정보 전송 시 기밀·개인정보 유출 위험이 있습니다. 각별히 주의해 주세요.\n\n"
            "요청하신 프롬프트는 AIVAX 정책에 의해 차단되었습니다.\n\n"
            "세부 사항은 관리자에게 문의해 주세요."
        )
        formatted = ape._block_display_text(
            "[AIVAX MASKING]",
            notice_body,
            consent_hint=True,
        )
        self.assertTrue(formatted.startswith("[AIVAX] 프롬프트 차단"))
        self.assertLess(formatted.find("요청이 차단되었습니다."), formatted.find("[⚠️ 주의]"))
        self.assertLess(formatted.find("[⚠️ 주의]"), formatted.find("[입력 프롬프트]"))
        self.assertLess(formatted.find("[입력 프롬프트]"), formatted.find("[AIVAX MASKING]"))
        self.assertLess(formatted.find("[AIVAX MASKING]"), formatted.find("📛 탐지 유형"))
        self.assertFalse(formatted.startswith("[AIVAX MASKING]"))
        self.assertIn("탐지 유형 줄부터 관리자 문의 문장까지", ape.AIVAX_BLOCK_MODEL_INSTRUCTION)
        visible = ape._block_visible_response(notice_body, consent_hint=True)
        self.assertTrue(visible.startswith("### [AIVAX] 프롬프트 차단"))
        self.assertIn("> 🔎 민감정보가 프롬프트에서 탐지되어", visible)
        self.assertIn("**탐지 사유**", visible)
        self.assertIn("* **유형:** IP - v4", visible)
        self.assertIn("* **설명:** IPv4 주소가 포함되어 있습니다.", visible)
        self.assertIn("* **필터:** 프롬프트", visible)
        self.assertNotIn("* **위치:**", visible)
        self.assertNotIn("* **조치:**", visible)
        self.assertIn("각별히 주의해 주세요.", visible)
        self.assertIn("> **⚠️ 주의**", visible)
        self.assertLess(visible.find("> 🔎"), visible.find("**탐지 사유**"))
        self.assertLess(visible.find("**유형:**"), visible.find("> **⚠️ 주의**"))
        self.assertNotIn("SNIPER AIVAX", visible)
        self.assertNotIn("[AIVAX MASKING]", visible)
        model_input = ape._block_model_input_prompt(
            "[AIVAX MASKING]",
            notice_body,
            consent_hint=True,
        )
        self.assertTrue(model_input.startswith("입력 프롬프트"))
        self.assertNotIn("**SNIPER AIVAX**", model_input)
        self.assertNotIn("**입력 프롬프트**", model_input)
        self.assertIn("입력 프롬프트", model_input)
        self.assertLess(model_input.find("입력 프롬프트"), model_input.find("[AIVAX MASKING]"))
        self.assertLess(model_input.find("[AIVAX MASKING]"), model_input.find("SNIPER AIVAX"))
        self.assertIn("SNIPER AIVAX\n\n### [AIVAX] 프롬프트 차단", model_input)
        self.assertLess(model_input.find("SNIPER AIVAX"), model_input.find("### [AIVAX] 프롬프트 차단"))
        self.assertLess(model_input.find("### [AIVAX] 프롬프트 차단"), model_input.find("요청하신 프롬프트는"))
        self.assertLess(model_input.find("> 🔎"), model_input.find("**유형:**"))
        self.assertLess(model_input.find("**유형:**"), model_input.find("> **⚠️ 주의**"))
        timed = ape._block_visible_response(notice_body, consent_hint=True, analysis_ms=12)
        self.assertIn("* **분석시간:** 12ms", timed)
        cso_visible = ape._cso_blocked_visible_response(
            "[AIVAX] 첨부파일 보안등급 차단\n\n"
            "보안등급이 존재하지 않는 첨부파일이 있어 요청을 차단했습니다.",
            "사내 보안등급 태깅 절차에 따라 등급을 지정한 후 다시 시도해 주세요.",
        )
        self.assertTrue(cso_visible.startswith("### [AIVAX] 첨부파일 보안등급 차단"))
        self.assertIn("> 🔎 보안등급이 존재하지 않는 첨부파일", cso_visible)
        self.assertNotIn("### [AIVAX] 프롬프트 차단", cso_visible)
        self.assertIn("보안등급이 존재하지 않는 첨부파일", cso_visible)
        self.assertIn("사내 보안등급 태깅 절차", cso_visible)
        self.assertNotIn("탐지 사유", cso_visible)
        self.assertNotIn("동의합니다", cso_visible)
        self.assertEqual(
            ape.extract_policy_cso(
                {
                    "cso": {
                        "status": "blocked",
                        "message": "사내 보안등급 태깅 절차에 따라 등급을 지정한 후 다시 시도해 주세요.",
                    },
                    "final_decision": {"action": "block", "action_code": 1},
                }
            ),
            (
                "blocked",
                "사내 보안등급 태깅 절차에 따라 등급을 지정한 후 다시 시도해 주세요.",
            ),
        )
        untagged_msg = (
            "첨부파일에 CSO 등급이 지정되지 않았습니다. 사내 CSO 등급 태깅 "
            "절차에 따라 등급을 지정한 후 다시 시도해 주세요."
        )
        self.assertEqual(
            ape.extract_policy_cso({"cso": {"status": "untagged", "message": untagged_msg}}),
            ("untagged", untagged_msg),
        )
        self.assertTrue(ape._cso_refuses_consent("untagged"))
        self.assertTrue(ape._cso_refuses_consent("blocked"))
        self.assertTrue(ape._cso_refuses_consent("error"))
        self.assertFalse(ape._cso_refuses_consent("open"))
        self.assertEqual(ape.extract_policy_cso({"cso": None}), ("", ""))
        self.assertEqual(ape.extract_policy_cso({"final_decision": {"action": "block"}}), ("", ""))
        guidance = (
            "차단된 요청의 전송에 동의하시면 ‘동의합니다’를 입력한 후 다시 시도해 주세요. "
            "또한 첨부파일에 대한 결재가 상신됩니다."
        )
        open_visible = ape._block_visible_response(
            notice_body,
            consent_hint=True,
            analysis_ms=631,
            detect_filter="file_block_filter",
            cso_message=guidance,
        )
        self.assertIn("**탐지 사유**", open_visible)
        self.assertIn("* **유형:** IP - v4", open_visible)
        self.assertIn("* **필터:** 멀티모달", open_visible)
        self.assertIn("* **분석시간:** 631ms", open_visible)
        self.assertIn(guidance, open_visible)
        self.assertIn("> **⚠️ 주의**", open_visible)
        self.assertLess(open_visible.find("**탐지 사유**"), open_visible.find(guidance))
        self.assertLess(open_visible.find("* **분석시간:** 631ms"), open_visible.find(guidance))
        self.assertLess(open_visible.find(guidance), open_visible.find("> **⚠️ 주의**"))
        self.assertEqual(
            ape._block_visible_response(notice_body, consent_hint=True).count(guidance),
            0,
        )
        prefixed = ape._cso_open_prompt_text(
            "이 파일 요약해줘",
            "📋[정보보호팀] 사후 결재가 상신됩니다.",
        )
        self.assertTrue(prefixed.startswith("📋[정보보호팀] 사후 결재가 상신됩니다."))
        self.assertNotIn("⚠️ 주의", prefixed)
        self.assertTrue(prefixed.endswith("이 파일 요약해줘"))
        self.assertLess(prefixed.find("📋"), prefixed.find("이 파일 요약해줘"))
        self.assertEqual(
            ape._cso_open_prompt_text(prefixed, "📋[정보보호팀] 사후 결재가 상신됩니다."),
            prefixed,
        )
        self.assertEqual(
            ape._format_detect_filter("secret_filter"),
            "프롬프트",
        )
        self.assertEqual(ape._format_detect_filter("file_block_filter"), "멀티모달")
        self.assertEqual(ape._format_detect_filter("slm_filter"), "SLM")
        slm_visible = ape._block_visible_response(
            "🚫 SLM 필터 정책에 의해 민감정보가 프롬프트 문맥에 포함된 것으로 탐지되었습니다.\n"
            "❌ 탐지 유형은 'PHONE' 입니다.",
            consent_hint=False,
        )
        self.assertIn("* **필터:** SLM", slm_visible)
        multimodal_visible = ape._block_visible_response(
            notice_body,
            consent_hint=False,
            detect_filter="file_block_filter",
        )
        self.assertIn("* **필터:** 멀티모달", multimodal_visible)
        self.assertIn("### [AIVAX] 프롬프트 차단부터 주의까지", ape.GPT_BLOCK_MODEL_INSTRUCTION)
        gemini_notice = ape._gemini_block_visible_notice(
            "[AIVAX MASKING]",
            notice_body,
            consent_hint=True,
        )
        self.assertEqual(gemini_notice, ape._gpt_gemini_block_notice("[AIVAX MASKING]", notice_body, consent_hint=True))
        self.assertTrue(gemini_notice.startswith("입력 프롬프트"))
        self.assertIn("### [AIVAX] 프롬프트 차단부터 주의까지", ape.GEMINI_BLOCK_MODEL_INSTRUCTION)
        self.assertIn("SNIPER AIVAX", ape.GEMINI_BLOCK_MODEL_INSTRUCTION)
        self.assertNotIn("[AIVAX MASKING]", ape.GEMINI_BLOCK_MODEL_INSTRUCTION)
        self.assertNotIn("[AIVAX 정책 응답 처리]", ape.PERPLEXITY_BLOCK_MODEL_INSTRUCTION)
        self.assertIn("### [AIVAX] 프롬프트 차단부터 주의까지", ape.PERPLEXITY_BLOCK_MODEL_INSTRUCTION)
        self.assertIn("이 지시문", ape.PERPLEXITY_BLOCK_MODEL_INSTRUCTION)
        self.assertEqual(ape.consent_replay_prompt_text("gpt", "127.0.0.1"), "127.0.0.1")
        self.assertEqual(
            ape.consent_replay_prompt_text("gemini", "127.0.0.1"),
            "127.0.0.1",
        )
        self.assertNotIn(
            "이전 차단 안내문은 무시",
            ape.consent_replay_prompt_text("gemini", "127.0.0.1"),
        )

    def _assert_guideline_notice_cards(self) -> None:
        notice_body = (
            "[AIVAX] 프롬프트 차단\n\n"
            "🔒 민감정보가 프롬프트에서 탐지되어 요청이 차단되었습니다.\n\n"
            "📛 탐지 유형: IP - v4\n\n"
            "⚠️ 민감 정보 전송 시 기밀·개인정보 유출 위험이 있습니다. 각별히 주의해 주세요.\n\n"
            "요청하신 프롬프트는 AIVAX 정책에 의해 차단되었습니다.\n\n"
            "세부 사항은 관리자에게 문의해 주세요."
        )
        formatted = ape._block_display_text(
            "[AIVAX MASKING]",
            notice_body,
            consent_hint=True,
        )
        self.assertTrue(formatted.startswith("### 🚫[정보보호팀] "))
        self.assertIn("입력하신 내용에 **'IP - v4'** 이 포함되어있습니다.", formatted)
        self.assertIn("다시 확인해주시기 바랍니다.", formatted)
        self.assertIn("> **⚠️ 주의**", formatted)
        self.assertLess(formatted.find("**'IP - v4'**"), formatted.find("> **⚠️ 주의**"))
        self.assertNotIn("**탐지 사유**", formatted)
        self.assertNotIn("[입력 프롬프트]", formatted)
        self.assertNotIn("[AIVAX MASKING]", formatted)
        self.assertNotIn("* **분석시간:**", formatted)
        visible = ape._block_visible_response(notice_body, consent_hint=True)
        self.assertEqual(visible, formatted)
        timed = ape._block_visible_response(notice_body, consent_hint=True, analysis_ms=12)
        self.assertNotIn("분석시간", timed)
        self.assertNotIn("**탐지 사유**", timed)
        model_input = ape._block_model_input_prompt(
            "[AIVAX MASKING]",
            notice_body,
            consent_hint=True,
        )
        self.assertTrue(model_input.startswith("입력 프롬프트"))
        self.assertIn("[AIVAX MASKING]", model_input)
        self.assertIn("SNIPER AIVAX", model_input)
        self.assertLess(model_input.find("SNIPER AIVAX"), model_input.find("### 🚫[정보보호팀]"))
        self.assertNotIn("### [AIVAX] 프롬프트 차단", model_input)
        guidance = "첨부파일에 대한 결재가 상신됩니다."
        open_visible = ape._block_visible_response(
            notice_body,
            consent_hint=True,
            analysis_ms=631,
            detect_filter="file_block_filter",
            cso_message=guidance,
        )
        self.assertIn("첨부파일내용에 **'IP - v4'** 이 포함되어있습니다.", open_visible)
        self.assertIn(guidance, open_visible)
        self.assertIn("> **⚠️ 주의**", open_visible)
        self.assertLess(open_visible.find("**'IP - v4'**"), open_visible.find(guidance))
        self.assertLess(open_visible.find(guidance), open_visible.find("> **⚠️ 주의**"))
        self.assertNotIn("**탐지 사유**", open_visible)
        self.assertNotIn("분석시간", open_visible)
        unmatched = ape._cso_blocked_visible_response(
            "[AIVAX] 첨부파일 보안등급 차단\n\n"
            "보안등급이 존재하지 않는 첨부파일이 있어 요청을 차단했습니다.",
            "사내 보안등급 태깅 절차에 따라 등급을 지정한 후 다시 시도해 주세요.",
        )
        self.assertTrue(unmatched.startswith("### [AIVAX] 첨부파일 보안등급 차단"))
        self.assertIn("사내 보안등급 태깅 절차", unmatched)
        self.assertNotIn("정보보호팀", unmatched)
        self.assertNotIn("동의합니다", unmatched)

    def test_policy_json_reserved_and_toggle(self) -> None:
        prev = ape._USER_CONSENT
        prev_ttl = ape._USER_CONSENT_TTL_SEC
        prev_gate = dict(ape._SERVICE_GATE)
        try:
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "aivax_policy.json"
                path.write_text(
                    json.dumps(
                        {
                            "gpt": "inspect_on",
                            "user_consent": "on",
                            "user_consent_ttl_sec": 45,
                            "block_ui_mode": "response_override",
                            "file_allow_extension": "pdf",
                        }
                    ),
                    encoding="utf-8",
                )
                ape.load_file_collection_policy(str(path))
                self.assertTrue(ape.user_consent_enabled())
                self.assertEqual(int(ape.user_consent_ttl_sec()), 45)
                self.assertNotIn("user_consent", ape._SERVICE_GATE)
                self.assertNotIn("user_consent_ttl_sec", ape._SERVICE_GATE)
                self.assertNotIn("block_ui_mode", ape._SERVICE_GATE)
        finally:
            ape._USER_CONSENT = prev
            ape._USER_CONSENT_TTL_SEC = prev_ttl
            ape._SERVICE_GATE = prev_gate


class TestUserConsentStore(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = ape._USER_CONSENT
        ape._USER_CONSENT = "on"

    def tearDown(self) -> None:
        ape._USER_CONSENT = self._prev

    def test_store_lookup_consume_session_only(self) -> None:
        eng = _engine()
        self.assertTrue(
            eng._user_consent_store("chatgpt", "s1", "1.2.3.4", "비밀 프롬프트", "mid-1")
        )
        hit = eng._user_consent_lookup("chatgpt", "s1", "9.9.9.9")
        self.assertIsNotNone(hit)
        self.assertEqual((hit or {}).get("prompt"), "비밀 프롬프트")
        self.assertEqual((hit or {}).get("message_id"), "mid-1")
        self.assertIsNone(eng._user_consent_lookup("chatgpt", "", "1.2.3.4"))
        eng._user_consent_consume("chatgpt", "s1", "1.2.3.4")
        self.assertIsNone(eng._user_consent_lookup("chatgpt", "s1", "1.2.3.4"))

    def test_ip_pending_does_not_attach_to_other_session(self) -> None:
        eng = _engine()
        self.assertTrue(eng._user_consent_store("gpt", "", "1.2.3.4", "127.0.0.1", "m1"))
        self.assertIsNone(eng._user_consent_lookup("gpt", "conv-later", "1.2.3.4"))
        self.assertTrue(eng._user_consent_rebind_ip_to_session("gpt", "1.2.3.4", "conv-1"))
        self.assertEqual(
            (eng._user_consent_lookup("gpt", "conv-1", "1.2.3.4") or {}).get("prompt"),
            "127.0.0.1",
        )
        self.assertIsNone(eng._user_consent_lookup("gpt", "conv-other", "1.2.3.4"))

    def test_rebind_request_id_attaches_empty_session_pending(self) -> None:
        eng = _engine()
        self.assertTrue(
            eng._user_consent_store("gemini", "", "10.0.0.8", "127.0.0.1", "gem-rid")
        )
        self.assertIsNone(eng._user_consent_lookup("gemini", "c_new", "10.0.0.8"))
        self.assertTrue(
            eng._user_consent_rebind_request_to_session("gemini", "gem-rid", "c_new")
        )
        self.assertEqual(
            (eng._user_consent_lookup("gemini", "c_new", "10.0.0.8") or {}).get("prompt"),
            "127.0.0.1",
        )

    def test_rebind_ip_skips_pending_placeholder(self) -> None:
        eng = _engine()
        self.assertTrue(eng._user_consent_store("gpt", "", "1.2.3.4", "127.0.0.1", "m1"))
        self.assertFalse(
            eng._user_consent_rebind_ip_to_session("gpt", "1.2.3.4", "pending:gpt")
        )
        self.assertIsNone(eng._user_consent_lookup("gpt", "pending:gpt", "1.2.3.4"))
        self.assertTrue(eng._user_consent_lookup_ip_only("gpt", "1.2.3.4"))
        self.assertTrue(eng._user_consent_rebind_ip_to_session("gpt", "1.2.3.4", "conv-1"))
        self.assertEqual(
            (eng._user_consent_lookup("gpt", "conv-1", "1.2.3.4") or {}).get("prompt"),
            "127.0.0.1",
        )

    def test_alias_sessions_let_followup_uuid_consume(self) -> None:
        eng = _engine()
        self.assertTrue(
            eng._user_consent_store("perplexity", "front-1", "10.0.0.8", "127.0.0.1", "m-pplx")
        )
        self.assertTrue(
            eng._user_consent_alias_sessions("perplexity", "front-1", ["back-1", "front-1"])
        )
        self.assertEqual(
            (eng._user_consent_lookup("perplexity", "back-1", "10.0.0.8") or {}).get("prompt"),
            "127.0.0.1",
        )
        hit = eng._user_consent_consume_any(
            "perplexity", ["front-2", "back-1"], "10.0.0.8"
        )
        self.assertIsNotNone(hit)
        self.assertEqual((hit or {}).get("prompt"), "127.0.0.1")
        self.assertIsNone(eng._user_consent_lookup("perplexity", "front-1", "10.0.0.8"))
        self.assertIsNone(eng._user_consent_lookup("perplexity", "back-1", "10.0.0.8"))


class TestUserConsentEvaluate(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_consent = ape._USER_CONSENT
        self._prev_gate = dict(ape._SERVICE_GATE)
        self._prev_path = ape._DYN_POLICY_PATH
        ape._SERVICE_GATE = {"gpt": "inspect_on"}
        ape._DYN_POLICY_PATH = "/nonexistent/aivax_user_consent_test.json"

    def tearDown(self) -> None:
        ape._USER_CONSENT = self._prev_consent
        ape._SERVICE_GATE = self._prev_gate
        ape._DYN_POLICY_PATH = self._prev_path

    def _run(
        self,
        eng: ape.PolicyEngine,
        req: Dict[str, Any],
        decision: Tuple[str, str, str],
    ) -> Dict[str, Any]:
        fake = _FakeAdapter()
        eng.adapter_registry.get = lambda service_id: fake  # type: ignore[method-assign]
        eng._call_policy_api = AsyncMock(return_value=decision)  # type: ignore[method-assign]
        eng._flush_chatgpt_inline_output_log = lambda *args, **kwargs: None  # type: ignore[method-assign]
        eng._submit_output_log_async = lambda *args, **kwargs: None  # type: ignore[method-assign]
        payload = json.dumps(
            {
                "prompt": req["_fake_extract"],
                "conversation_id": req.get("_fake_session") or "conv-1",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        return asyncio.run(eng.evaluate(req, payload))

    def test_cso_open_consent_replays_real_adapter_file_refs(self) -> None:
        """O grade + 동의 must send the blocked turn's files, not prompt only."""
        ape._USER_CONSENT = "on"
        ape._SERVICE_GATE = {"gpt": "inspect_on", "timely": "inspect_on"}
        guidance = "첨부파일에 대한 결재가 상신됩니다."
        stored = "p_CHAT_01M5S8514ZMP3P7WD28ZHKEHCX.webp"

        sys.path.insert(0, str(ROOT / "adapters"))
        from adapters import timely_engine

        async def _policy(req, payload, extracted, adapter_service_id, adapter):
            req["_policy_cso_status"] = "open"
            req["_policy_cso_message"] = guidance
            if ape.is_user_consent_phrase(extracted or ""):
                return ("PASS", "", "")
            return ("BLOCK", "", "[AIVAX] 프롬프트 차단\n\n📛 탐지 유형: 멀티모달")

        eng = _engine()
        eng.adapter_registry.get = lambda service_id: timely_engine  # type: ignore[method-assign]
        eng._call_policy_api = _policy  # type: ignore[method-assign]
        eng._flush_chatgpt_inline_output_log = lambda *a, **k: None  # type: ignore[method-assign]
        eng._submit_output_log_async = lambda *a, **k: None  # type: ignore[method-assign]

        def _timely_req(request_id: str) -> Dict[str, Any]:
            return {
                "request_id": request_id,
                "service_id": "timely",
                "direction": "request",
                "unit_type": "json_body",
                "content_type": "application/json",
                "meta": {
                    "host": "timelyai.io",
                    "path": "/timely/api/stream",
                    "client_ip": "10.0.50.60",
                },
            }

        blocked_body = json.dumps(
            {
                "url": "/api-ai/v3/llm-completion/stream/start",
                "body": {
                    "messages": [{"role": "user", "content": "이 파일 요약해줘"}],
                    "files": [stored],
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        blocked = asyncio.run(eng.evaluate(_timely_req("timely-block"), blocked_body))
        self.assertIn("policy_api:block", blocked.get("reason") or "")

        # A new chat has no chat_id. The response session must not hide that slot.
        sse_req = _timely_req("timely-block")
        sse_req["direction"] = "response"
        sse_req["unit_type"] = "sse_event"
        sse_req["content_type"] = "text/event-stream"
        asyncio.run(
            eng.evaluate(
                sse_req,
                b'data: {"type":"final_response","session_id":"01SSESESSION","message":"x","model":"solar-mini"}\n\n',
            )
        )

        consent_body = json.dumps(
            {
                "url": "/api-ai/v3/llm-completion/stream/start",
                "body": {"messages": [{"role": "user", "content": "동의합니다"}]},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        replay = asyncio.run(eng.evaluate(_timely_req("timely-consent"), consent_body))
        self.assertIn("user_consent:pass", replay.get("reason") or "")
        body = json.loads(
            base64.b64decode(str((replay.get("payload") or {}).get("raw_b64") or "")).decode()
        )
        self.assertEqual(body["body"]["files"], [stored])
        self.assertEqual(body["body"]["messages"][-1]["content"], "이 파일 요약해줘")

    def test_timely_consent_stays_on_the_same_chat_id(self) -> None:
        """Another Timely chat on the same IP must not receive the blocked files."""
        ape._USER_CONSENT = "on"
        ape._SERVICE_GATE = {"gpt": "inspect_on", "timely": "inspect_on"}
        stored = "p_CHAT_01M5S8514ZMP3P7WD28ZHKEHCX.webp"
        chat_a = "01M5S8514ZMP3P7WD28ZHKEHCX"
        chat_b = "01M5S33DNYMRWF4CS3RYVCYKWY"

        sys.path.insert(0, str(ROOT / "adapters"))
        from adapters import timely_engine

        async def _policy(req, payload, extracted, adapter_service_id, adapter):
            req["_policy_cso_status"] = "open"
            req["_policy_cso_message"] = "첨부파일에 대한 결재가 상신됩니다."
            if ape.is_user_consent_phrase(extracted or ""):
                return ("PASS", "", "")
            return ("BLOCK", "", "[AIVAX] 프롬프트 차단\n\n📛 탐지 유형: 멀티모달")

        eng = _engine()
        eng.adapter_registry.get = lambda service_id: timely_engine  # type: ignore[method-assign]
        eng._call_policy_api = _policy  # type: ignore[method-assign]
        eng._flush_chatgpt_inline_output_log = lambda *a, **k: None  # type: ignore[method-assign]
        eng._submit_output_log_async = lambda *a, **k: None  # type: ignore[method-assign]

        def _send(request_id: str, body: Dict[str, Any], *, response: bool = False) -> Dict[str, Any]:
            return {
                "request_id": request_id,
                "service_id": "timely",
                "direction": "response" if response else "request",
                "unit_type": "sse_event" if response else "json_body",
                "content_type": "text/event-stream" if response else "application/json",
                "meta": {
                    "host": "timelyai.io",
                    "path": "/timely/api/stream",
                    "client_ip": "10.0.50.60",
                },
            }

        def _start(chat_id: str, content: str, files: Optional[list] = None) -> bytes:
            body: Dict[str, Any] = {
                "messages": [{"role": "user", "content": content}],
            }
            if chat_id:
                body["chat_id"] = chat_id
            if files:
                body["files"] = files
            return json.dumps(
                {"url": "/api-ai/v3/llm-completion/stream/start", "body": body},
                ensure_ascii=False,
            ).encode("utf-8")

        blocked = asyncio.run(
            eng.evaluate(_send("timely-block", {}), _start(chat_a, "이 파일 요약해줘", [stored]))
        )
        self.assertIn("policy_api:block", blocked.get("reason") or "")

        # The blocked response carries a different session id. It must not
        # move the pending slot off chat A.
        sse = (
            b'data: {"type":"final_response","session_id":"01SSESESSION",'
            b'"message":"blocked","model":"solar-mini"}\n\n'
        )
        asyncio.run(eng.evaluate(_send("timely-block", {}, response=True), sse))

        other = asyncio.run(
            eng.evaluate(_send("timely-other", {}), _start(chat_b, "동의합니다"))
        )
        self.assertNotIn("user_consent:pass", other.get("reason") or "")

        replay = asyncio.run(
            eng.evaluate(_send("timely-consent", {}), _start(chat_a, "동의합니다"))
        )
        self.assertIn("user_consent:pass", replay.get("reason") or "")
        body = json.loads(
            base64.b64decode(str((replay.get("payload") or {}).get("raw_b64") or "")).decode()
        )
        self.assertEqual(body["body"]["files"], [stored])
        self.assertEqual(body["body"]["chat_id"], chat_a)
        self.assertEqual(body["body"]["messages"][-1]["content"], "이 파일 요약해줘")

    def test_cso_untagged_shows_guidance_and_refuses_consent(self) -> None:
        ape._USER_CONSENT = "on"
        block_message = (
            "[AIVAX] CSO 등급 차단\n\n"
            "CSO 등급이 존재하지 않는 첨부파일이 있어 요청을 차단했습니다."
        )
        guidance = (
            "첨부파일에 CSO 등급이 지정되지 않았습니다. 사내 CSO 등급 태깅 "
            "절차에 따라 등급을 지정한 후 다시 시도해 주세요."
        )
        expectations = {
            "default": {
                "present": (
                    "### [AIVAX] CSO 등급 차단",
                    "> 🔎 CSO 등급이 존재하지 않는 첨부파일이 있어 요청을 차단했습니다.",
                    guidance,
                ),
                "absent": (
                    "등급이 분류되지 않은 자료가 포함되어 있습니다.",
                    "탐지 사유",
                    "동의",
                ),
            },
            "guideline": {
                "present": (
                    "### [정보보호팀] ",
                    "등급이 분류되지 않은 자료가 포함되어 있습니다.",
                    "O(공개) 등급으로 설정 후 전송해 주시기 바랍니다.",
                ),
                "absent": (
                    guidance,
                    "탐지 사유",
                    "동의",
                    "> **⚠️ 주의**",
                ),
            },
        }
        for style, expect in expectations.items():
            with self.subTest(notice_style=style):
                with _pinned_notice_style(style):
                    session = f"conv-{style}"
                    eng = _engine()

                    async def _policy(req, payload, extracted, adapter_service_id, adapter):
                        req["_policy_cso_status"] = "untagged"
                        req["_policy_cso_message"] = guidance
                        return ("BLOCK", "", block_message)

                    fake = _FakeAdapter()
                    eng.adapter_registry.get = lambda service_id: fake  # type: ignore[method-assign]
                    eng._call_policy_api = _policy  # type: ignore[method-assign]
                    eng._flush_chatgpt_inline_output_log = lambda *args, **kwargs: None  # type: ignore[method-assign]
                    eng._submit_output_log_async = lambda *args, **kwargs: None  # type: ignore[method-assign]
                    blocked = asyncio.run(
                        eng.evaluate(
                            _req("미태깅.docx 요약", session=session, request_id=f"cso-u-{style}"),
                            json.dumps(
                                {"prompt": "미태깅.docx 요약", "conversation_id": session},
                                ensure_ascii=False,
                            ).encode("utf-8"),
                        )
                    )
                    body = json.loads(
                        base64.b64decode(
                            str((blocked.get("payload") or {}).get("raw_b64") or "")
                        ).decode("utf-8")
                    )
                    text = str(body.get("prompt") or "")
                    self.assertIn("policy_api:block", blocked.get("reason") or "")
                    for snippet in expect["present"]:
                        self.assertIn(snippet, text)
                    for snippet in expect["absent"]:
                        self.assertNotIn(snippet, text)
                    self.assertFalse(_payload_has_consent_hint(text.encode("utf-8")))
                    pending = eng._user_consent_lookup("gpt", session, "10.0.0.8")
                    self.assertIsNotNone(pending)
                    self.assertTrue(pending.get("cso_blocked"))
                    self.assertEqual(pending.get("cso_status"), "untagged")

                    consent = asyncio.run(
                        eng.evaluate(
                            _req("동의합니다", session=session, request_id=f"cso-u-c-{style}"),
                            json.dumps(
                                {"prompt": "동의합니다", "conversation_id": session},
                                ensure_ascii=False,
                            ).encode("utf-8"),
                        )
                    )
                    self.assertNotIn("user_consent:pass", consent.get("reason") or "")
                    self.assertIn("policy_api:block", consent.get("reason") or "")
                    consent_body = json.loads(
                        base64.b64decode(
                            str((consent.get("payload") or {}).get("raw_b64") or "")
                        ).decode("utf-8")
                    )
                    consent_text = str(consent_body.get("prompt") or "")
                    for snippet in expect["present"]:
                        self.assertIn(snippet, consent_text)
                    for snippet in expect["absent"]:
                        self.assertNotIn(snippet, consent_text)
                    self.assertNotIn("동의합니다", consent_text)
                    self.assertFalse(_payload_has_consent_hint(consent_text.encode("utf-8")))
                    still = eng._user_consent_lookup("gpt", session, "10.0.0.8")
                    self.assertIsNotNone(still)
                    self.assertEqual(still.get("cso_status"), "untagged")

    def test_cso_blocked_hides_detect_reason_and_refuses_consent(self) -> None:
        ape._USER_CONSENT = "on"
        eng = _engine()
        block_message = (
            "[AIVAX] 첨부파일 보안등급 차단\n\n"
            "보안등급이 존재하지 않는 첨부파일이 있어 요청을 차단했습니다."
        )
        guidance = "사내 보안등급 태깅 절차에 따라 등급을 지정한 후 다시 시도해 주세요."

        async def _policy(req, payload, extracted, adapter_service_id, adapter):
            req["_policy_cso_status"] = "blocked"
            req["_policy_cso_message"] = guidance
            return ("BLOCK", "", block_message)

        fake = _FakeAdapter()
        eng.adapter_registry.get = lambda service_id: fake  # type: ignore[method-assign]
        eng._call_policy_api = _policy  # type: ignore[method-assign]
        eng._flush_chatgpt_inline_output_log = lambda *args, **kwargs: None  # type: ignore[method-assign]
        eng._submit_output_log_async = lambda *args, **kwargs: None  # type: ignore[method-assign]
        blocked_req = _req("기밀파일.docx 요약", request_id="cso-b")
        payload = json.dumps(
            {"prompt": "기밀파일.docx 요약", "conversation_id": "conv-1"},
            ensure_ascii=False,
        ).encode("utf-8")
        blocked = asyncio.run(eng.evaluate(blocked_req, payload))
        decoded = base64.b64decode(str((blocked.get("payload") or {}).get("raw_b64") or ""))
        body = json.loads(decoded.decode("utf-8"))
        text = str(body.get("prompt") or "")
        self.assertIn("policy_api:block", blocked.get("reason") or "")
        self.assertIn("### [AIVAX] 첨부파일 보안등급 차단", text)
        self.assertIn("> 🔎 보안등급이 존재하지 않는 첨부파일", text)
        self.assertNotIn("### [AIVAX] 프롬프트 차단", text)
        self.assertIn("보안등급이 존재하지 않는 첨부파일", text)
        self.assertIn(guidance, text)
        self.assertNotIn("탐지 사유", text)
        self.assertNotIn("동의", text)
        self.assertIn("탐지 사유", str(body.get("sys") or ""))
        self.assertFalse(_payload_has_consent_hint(text.encode("utf-8")))
        pending = eng._user_consent_lookup("gpt", "conv-1", "10.0.0.8")
        self.assertIsNotNone(pending)
        self.assertTrue(pending.get("cso_blocked"))

        consent = asyncio.run(
            eng.evaluate(
                _req("동의합니다", request_id="cso-c"),
                json.dumps(
                    {"prompt": "동의합니다", "conversation_id": "conv-1"},
                    ensure_ascii=False,
                ).encode("utf-8"),
            )
        )
        self.assertNotIn("user_consent:pass", consent.get("reason") or "")
        self.assertIn("policy_api:block", consent.get("reason") or "")
        consent_body = json.loads(
            base64.b64decode(
                str((consent.get("payload") or {}).get("raw_b64") or "")
            ).decode("utf-8")
        )
        consent_text = str(consent_body.get("prompt") or "")
        self.assertNotIn("기밀파일.docx 요약", consent_text)
        self.assertIn(guidance, consent_text)
        self.assertNotIn("탐지 사유", consent_text)
        self.assertFalse(_payload_has_consent_hint(consent_text.encode("utf-8")))
        still = eng._user_consent_lookup("gpt", "conv-1", "10.0.0.8")
        self.assertIsNotNone(still)
        self.assertTrue(still.get("cso_blocked"))

    def test_cso_open_block_puts_guidance_under_detect_reason_and_allows_consent(self) -> None:
        ape._USER_CONSENT = "on"
        eng = _engine()
        block_message = (
            "[AIVAX] 프롬프트 차단\n\n"
            "🔒 AIVAX 정책에 의해 민감정보가 첨부파일에 포함된 것으로 탐지되었습니다.\n\n"
            "📛 탐지 유형: 멀티모달 테스트 정책 - 클라우드개발2팀\n\n"
            "요청하신 프롬프트는 AIVAX 정책에 의해 차단되었습니다.\n\n"
            "세부 사항은 관리자에게 문의해 주세요."
        )
        guidance = (
            "차단된 요청의 전송에 동의하시면 ‘동의합니다’를 입력한 후 다시 시도해 주세요. "
            "또한 첨부파일에 대한 결재가 상신됩니다."
        )

        async def _policy(req, payload, extracted, adapter_service_id, adapter):
            req["_policy_cso_status"] = "open"
            req["_policy_cso_message"] = guidance
            req["_policy_detect_filter"] = "file_block_filter"
            req["_policy_analysis_ms"] = 631
            return ("BLOCK", "", block_message)

        fake = _FakeAdapter()
        eng.adapter_registry.get = lambda service_id: fake  # type: ignore[method-assign]
        eng._call_policy_api = _policy  # type: ignore[method-assign]
        eng._flush_chatgpt_inline_output_log = lambda *args, **kwargs: None  # type: ignore[method-assign]
        eng._submit_output_log_async = lambda *args, **kwargs: None  # type: ignore[method-assign]
        blocked = asyncio.run(
            eng.evaluate(
                _req("기밀파일.docx 요약", request_id="cso-open-b"),
                json.dumps(
                    {"prompt": "기밀파일.docx 요약", "conversation_id": "conv-1"},
                    ensure_ascii=False,
                ).encode("utf-8"),
            )
        )
        body = json.loads(
            base64.b64decode(str((blocked.get("payload") or {}).get("raw_b64") or "")).decode("utf-8")
        )
        text = str(body.get("prompt") or "")
        self.assertIn("policy_api:block", blocked.get("reason") or "")
        self.assertIn("**탐지 사유**", text)
        self.assertIn("* **유형:** 멀티모달 테스트 정책 - 클라우드개발2팀", text)
        self.assertIn("* **필터:** 멀티모달", text)
        self.assertIn("* **분석시간:** 631ms", text)
        self.assertIn(guidance, text)
        self.assertIn("> **⚠️ 주의**", text)
        self.assertLess(text.find("**탐지 사유**"), text.find(guidance))
        self.assertLess(text.find("* **분석시간:** 631ms"), text.find(guidance))
        self.assertLess(text.find(guidance), text.find("> **⚠️ 주의**"))
        pending = eng._user_consent_lookup("gpt", "conv-1", "10.0.0.8")
        self.assertIsNotNone(pending)
        self.assertFalse(pending.get("cso_blocked"))

        consent = asyncio.run(
            eng.evaluate(
                _req("동의합니다", request_id="cso-open-c"),
                json.dumps(
                    {"prompt": "동의합니다", "conversation_id": "conv-1"},
                    ensure_ascii=False,
                ).encode("utf-8"),
            )
        )
        self.assertIn("user_consent:pass", consent.get("reason") or "")
        replay = base64.b64decode(
            str((consent.get("payload") or {}).get("raw_b64") or "")
        ).decode("utf-8")
        self.assertIn("기밀파일.docx 요약", replay)
        self.assertIsNone(eng._user_consent_lookup("gpt", "conv-1", "10.0.0.8"))

    def test_cso_open_accept_prefixes_prompt_and_passes(self) -> None:
        ape._USER_CONSENT = "on"
        eng = _engine()
        guidance = "첨부파일에 대한 결재가 상신됩니다."

        async def _policy(req, payload, extracted, adapter_service_id, adapter):
            req["_policy_cso_status"] = "open"
            req["_policy_cso_message"] = guidance
            return ("PASS", "", "")

        fake = _FakeAdapter()
        eng.adapter_registry.get = lambda service_id: fake  # type: ignore[method-assign]
        eng._call_policy_api = _policy  # type: ignore[method-assign]
        eng._flush_chatgpt_inline_output_log = lambda *args, **kwargs: None  # type: ignore[method-assign]
        eng._submit_output_log_async = lambda *args, **kwargs: None  # type: ignore[method-assign]
        out = asyncio.run(
            eng.evaluate(
                _req("이 파일 요약해줘", request_id="cso-accept"),
                json.dumps(
                    {"prompt": "이 파일 요약해줘", "conversation_id": "conv-1"},
                    ensure_ascii=False,
                ).encode("utf-8"),
            )
        )
        self.assertIn("cso_open_notice", out.get("reason") or "")
        self.assertNotIn("policy_api:block", out.get("reason") or "")
        body = json.loads(
            base64.b64decode(str((out.get("payload") or {}).get("raw_b64") or "")).decode("utf-8")
        )
        text = str(body.get("prompt") or "")
        self.assertTrue(text.startswith(guidance))
        self.assertNotIn("⚠️ 주의", text)
        self.assertIn("이 파일 요약해줘", text)
        self.assertLess(text.find(guidance), text.find("이 파일 요약해줘"))
        self.assertLess(text.find("이 파일 요약해줘"), text.find("응답의 맨 앞"))
        self.assertIn("이 지시문은 출력하지 마세요.", text)
        self.assertEqual(text.count(guidance), 2)

    def test_cso_open_keeps_prompt_notice_when_adapter_can_prefix_response(self) -> None:
        ape._USER_CONSENT = "on"
        eng = _engine()
        guidance = "첨부파일에 대한 결재가 상신됩니다."

        class _PrefixAdapter(_FakeAdapter):
            def prefix_response(self, req, payload, prefix):
                return None

        async def _policy(req, payload, extracted, adapter_service_id, adapter):
            req["_policy_cso_status"] = "open"
            req["_policy_cso_message"] = guidance
            return ("PASS", "", "")

        eng.adapter_registry.get = lambda service_id: _PrefixAdapter()  # type: ignore[method-assign]
        eng._call_policy_api = _policy  # type: ignore[method-assign]
        eng._flush_chatgpt_inline_output_log = lambda *args, **kwargs: None  # type: ignore[method-assign]
        eng._submit_output_log_async = lambda *args, **kwargs: None  # type: ignore[method-assign]
        sent = asyncio.run(
            eng.evaluate(
                _req("이 파일 요약해줘", request_id="cso-prefix-req"),
                json.dumps(
                    {"prompt": "이 파일 요약해줘", "conversation_id": "conv-1"},
                    ensure_ascii=False,
                ).encode("utf-8"),
            )
        )
        self.assertIn("cso_open_notice", sent.get("reason") or "")
        self.assertNotIn("response_prefix", sent.get("reason") or "")
        body = json.loads(
            base64.b64decode(str((sent.get("payload") or {}).get("raw_b64") or "")).decode("utf-8")
        )
        text = str(body.get("prompt") or "")
        self.assertTrue(text.startswith(guidance))
        self.assertNotIn("⚠️ 주의", text)
        self.assertIn("이 파일 요약해줘", text)
        self.assertIn("응답의 맨 앞", text)

    def test_off_does_not_store_or_hint(self) -> None:
        ape._USER_CONSENT = "off"
        eng = _engine()
        out = self._run(eng, _req("기밀 문서"), ("BLOCK", "", "차단되었습니다."))
        self.assertNotIn("user_consent:pass", out.get("reason") or "")
        self.assertIsNone(eng._user_consent_lookup("gpt", "conv-1", "10.0.0.8"))
        override = eng._response_override_by_session.get("conv-1") or ""
        self.assertNotIn("동의", override)

    def test_claude_block_rewrite_includes_hint_only_when_consent_on(self) -> None:
        claude_req = dict(
            extract="127.0.0.1",
            service_id="claude",
            host="claude.ai",
            path="/api/organizations/x/chat_conversations/y/completion",
        )
        ape._USER_CONSENT = "on"
        eng = _engine()
        blocked = self._run(
            eng,
            _req(request_id="cl-on", **claude_req),
            ("BLOCK", "[AIVAX MASKING]", "차단되었습니다."),
        )
        decoded = base64.b64decode(str((blocked.get("payload") or {}).get("raw_b64") or ""))
        self.assertTrue(_payload_has_consent_hint(decoded))
        self.assertIn(b"### [AIVAX]", decoded)
        self.assertNotIn("[AIVAX 정책 응답 처리]".encode("utf-8"), decoded)

        ape._USER_CONSENT = "off"
        eng_off = _engine()
        blocked_off = self._run(
            eng_off,
            _req(request_id="cl-off", **claude_req),
            ("BLOCK", "[AIVAX MASKING]", "차단되었습니다."),
        )
        decoded_off = base64.b64decode(
            str((blocked_off.get("payload") or {}).get("raw_b64") or "")
        )
        self.assertFalse(_payload_has_consent_hint(decoded_off))

    def test_gemini_block_rewrites_notice_without_response_override(self) -> None:
        ape._USER_CONSENT = "on"
        eng = _engine()
        blocked = self._run(
            eng,
            _req(
                "127.0.0.1",
                request_id="gem-no-ov",
                service_id="gemini",
                host="gemini.google.com",
                path="/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate",
            ),
            ("BLOCK", "[AIVAX MASKING]", "차단되었습니다."),
        )
        decoded = base64.b64decode(str((blocked.get("payload") or {}).get("raw_b64") or ""))
        prompt_obj = json.loads(decoded.decode("utf-8"))
        rewrite_prompt = str(prompt_obj.get("prompt") or "")
        self.assertIn("### [AIVAX] 프롬프트 차단", rewrite_prompt)
        self.assertIn("SNIPER AIVAX", rewrite_prompt)
        self.assertNotIn("Reply only: BLOCKED", rewrite_prompt)
        self.assertNotIn("기밀", rewrite_prompt)
        self.assertIn("프롬프트 차단부터", str(prompt_obj.get("sys") or ""))
        override = eng._lookup_response_override("conv-1", "gem-no-ov", "gemini") or ""
        self.assertEqual("", override)

    def test_perplexity_block_rewrites_notice_without_response_override(self) -> None:
        ape._USER_CONSENT = "on"
        eng = _engine()
        blocked = self._run(
            eng,
            _req(
                "127.0.0.1",
                request_id="pplx-no-ov",
                service_id="perplexity",
                host="www.perplexity.ai",
                path="/rest/sse/perplexity_ask",
            ),
            ("BLOCK", "[AIVAX MASKING]", "차단되었습니다."),
        )
        decoded = base64.b64decode(str((blocked.get("payload") or {}).get("raw_b64") or ""))
        prompt_obj = json.loads(decoded.decode("utf-8"))
        rewrite_prompt = str(prompt_obj.get("prompt") or "")
        self.assertTrue(rewrite_prompt.startswith("입력 프롬프트"))
        self.assertIn("SNIPER AIVAX", rewrite_prompt)
        self.assertIn("### [AIVAX] 프롬프트 차단", rewrite_prompt)
        self.assertNotIn("[AIVAX 정책 응답 처리]", rewrite_prompt)
        self.assertIn("### [AIVAX] 프롬프트 차단부터 주의까지", str(prompt_obj.get("sys") or ""))
        override = eng._lookup_response_override("conv-1", "pplx-no-ov", "perplexity") or ""
        self.assertEqual("", override)

    def test_perplexity_consent_replays_original_across_thread_uuids(self) -> None:
        ape._USER_CONSENT = "on"
        eng = _engine()
        block_req = _req(
            "127.0.0.1",
            session="front-1",
            request_id="pplx-b",
            service_id="perplexity",
            host="www.perplexity.ai",
            path="/rest/sse/perplexity_ask",
        )
        block_req["_fake_consent_sessions"] = ["front-1", "back-1"]
        blocked = self._run(
            eng,
            block_req,
            ("BLOCK", "[AIVAX MASKING]", "차단되었습니다."),
        )
        self.assertIn("policy_api:block", blocked.get("reason") or "")
        consent_req = _req(
            "동의",
            session="front-2",
            request_id="pplx-c",
            service_id="perplexity",
            host="www.perplexity.ai",
            path="/rest/sse/perplexity_ask",
        )
        consent_req["_fake_consent_sessions"] = ["front-2", "back-1"]
        replay = self._run(
            eng,
            consent_req,
            ("BLOCK", "", "blocked"),
        )
        self.assertIn("policy_api:user_consent:pass", replay.get("reason") or "")
        decoded = base64.b64decode(str((replay.get("payload") or {}).get("raw_b64") or ""))
        self.assertIn(b"127.0.0.1", decoded)
        self.assertIsNone(eng._user_consent_lookup("perplexity", "front-1", "10.0.0.8"))
        self.assertIsNone(eng._user_consent_lookup("perplexity", "back-1", "10.0.0.8"))

    def test_request_was_blocked_accepts_extended_cache_tuple(self) -> None:
        eng = _engine()
        rid = "rid-ext-cache"
        eng._policy_decision_by_request[rid] = (
            time.monotonic() + 60,
            1,
            ("BLOCK", "m", "b"),
            12,
            "secret_filter",
        )
        self.assertTrue(eng._request_was_blocked(rid))
        eng._policy_decision_by_request[rid] = (
            time.monotonic() + 60,
            1,
            ("PASS", "", ""),
        )
        self.assertFalse(eng._request_was_blocked(rid))

    def test_gpt_block_rewrite_keeps_input_prompt_response_drops_it(self) -> None:
        ape._USER_CONSENT = "on"
        eng = _engine()
        notice_body = (
            "[AIVAX] 프롬프트 차단\n\n"
            "🔒 민감정보가 프롬프트에서 탐지되어 요청이 차단되었습니다.\n\n"
            "📛 탐지 유형: IP - v4\n\n"
            "⚠️ 민감 정보 전송 시 기밀·개인정보 유출 위험이 있습니다. 각별히 주의해 주세요.\n\n"
            "요청하신 프롬프트는 AIVAX 정책에 의해 차단되었습니다.\n\n"
            "세부 사항은 관리자에게 문의해 주세요."
        )
        blocked = self._run(
            eng,
            _req("127.0.0.1", request_id="gpt-notice"),
            ("BLOCK", "[AIVAX MASKING]", notice_body),
        )
        decoded = base64.b64decode(str((blocked.get("payload") or {}).get("raw_b64") or "")).decode(
            "utf-8", errors="replace"
        )
        prompt_obj = json.loads(decoded)
        rewrite_prompt = str(prompt_obj.get("prompt") or "")
        self.assertTrue(rewrite_prompt.startswith("입력 프롬프트"))
        self.assertNotIn("**SNIPER AIVAX**", rewrite_prompt)
        self.assertNotIn("**입력 프롬프트**", rewrite_prompt)
        self.assertIn("입력 프롬프트", rewrite_prompt)
        self.assertLess(rewrite_prompt.find("입력 프롬프트"), rewrite_prompt.find("[AIVAX MASKING]"))
        self.assertLess(rewrite_prompt.find("[AIVAX MASKING]"), rewrite_prompt.find("SNIPER AIVAX"))
        self.assertLess(rewrite_prompt.find("SNIPER AIVAX"), rewrite_prompt.find("### [AIVAX] 프롬프트 차단"))
        self.assertLess(rewrite_prompt.find("> 🔎"), rewrite_prompt.find("**유형:**"))
        self.assertLess(rewrite_prompt.find("**유형:**"), rewrite_prompt.find("> **⚠️ 주의**"))
        self.assertIn("### [AIVAX] 프롬프트 차단부터 주의까지", str(prompt_obj.get("sys") or ""))
        override = eng._response_override_by_session.get("conv-1") or ""
        self.assertTrue(override.startswith("### [AIVAX] 프롬프트 차단"))
        self.assertIn("> 🔎 민감정보가 프롬프트에서 탐지되어", override)
        self.assertIn("* **유형:** IP - v4", override)
        self.assertIn("* **설명:** IPv4 주소가 포함되어 있습니다.", override)
        self.assertIn("* **필터:** 프롬프트", override)
        self.assertNotIn("* **위치:**", override)
        self.assertNotIn("* **조치:**", override)
        self.assertIn("> **⚠️ 주의**", override)
        self.assertLess(override.find("> 🔎"), override.find("**유형:**"))
        self.assertLess(override.find("**유형:**"), override.find("> **⚠️ 주의**"))
        self.assertNotIn("SNIPER AIVAX", override)
        self.assertNotIn("[AIVAX MASKING]", override)
        self.assertNotIn("[AIVAX 정책 응답 처리]", override)
        self.assertTrue(override.rstrip().endswith("사용자에게 있습니다."))

    def test_on_block_stores_and_one_shot_consent_then_reblock(self) -> None:
        ape._USER_CONSENT = "on"
        eng = _engine()
        blocked = self._run(
            eng,
            _req("127.0.0.1", request_id="b1"),
            ("BLOCK", "", "차단되었습니다."),
        )
        self.assertIn("policy_api:block", blocked.get("reason") or "")
        override = eng._response_override_by_session.get("conv-1") or ""
        self.assertIn("동의", override)
        pending = eng._user_consent_lookup("gpt", "conv-1", "10.0.0.8")
        self.assertIsNotNone(pending)
        self.assertEqual((pending or {}).get("prompt"), "127.0.0.1")

        replay = self._run(
            eng,
            _req("동의", request_id="b2"),
            ("BLOCK", "", "차단되었습니다."),
        )
        self.assertIn("policy_api:user_consent:pass", replay.get("reason") or "")
        self.assertEqual(replay.get("action"), "MASK")
        raw = replay.get("payload") or {}
        decoded = base64.b64decode(str(raw.get("raw_b64") or ""))
        self.assertIn(b"127.0.0.1", decoded)
        self.assertIsNone(eng._user_consent_lookup("gpt", "conv-1", "10.0.0.8"))

        second_phrase = self._run(
            eng,
            _req("동의", request_id="b5"),
            ("PASS", "", ""),
        )
        self.assertEqual(second_phrase.get("action"), "ALLOW")
        self.assertNotIn("user_consent:pass", second_phrase.get("reason") or "")

        reused = self._run(
            eng,
            _req("127.0.0.1", request_id="b3"),
            ("BLOCK", "", "차단되었습니다."),
        )
        self.assertIn("policy_api:block", reused.get("reason") or "")
        self.assertNotIn("user_consent:pass", reused.get("reason") or "")

    def test_consent_does_not_apply_to_new_chat(self) -> None:
        ape._USER_CONSENT = "on"
        eng = _engine()
        self._run(eng, _req("127.0.0.1", request_id="g1"), ("BLOCK", "", "blocked"))
        other = self._run(
            eng,
            _req("동의", session="other-conv", request_id="g3"),
            ("PASS", "", ""),
        )
        self.assertNotIn("user_consent:pass", other.get("reason") or "")

    def test_pass_after_block_clears_pending(self) -> None:
        ape._USER_CONSENT = "on"
        eng = _engine()
        self._run(eng, _req("기밀", request_id="c1"), ("BLOCK", "", "blocked"))
        self.assertIsNotNone(eng._user_consent_lookup("gpt", "conv-1", "10.0.0.8"))
        self._run(eng, _req("안녕하세요", request_id="c2"), ("PASS", "", ""))
        self.assertIsNone(eng._user_consent_lookup("gpt", "conv-1", "10.0.0.8"))

    def test_reload_clears_pending(self) -> None:
        ape._USER_CONSENT = "on"
        eng = _engine()
        self._run(eng, _req("127.0.0.1", request_id="r1"), ("BLOCK", "", "blocked"))
        self.assertIsNotNone(eng._user_consent_lookup("gpt", "conv-1", "10.0.0.8"))
        self._run(
            eng,
            _req("", request_id="r2", path="/backend-api/conversation/conv-1"),
            ("PASS", "", ""),
        )
        self.assertIsNone(eng._user_consent_lookup("gpt", "conv-1", "10.0.0.8"))

    def test_consent_arms_output_patch_message_id(self) -> None:
        ape._USER_CONSENT = "on"
        eng = _engine()
        eng._user_consent_store(
            "chatgpt", "conv-1", "10.0.0.8", "127.0.0.1", "block-mid-9"
        )
        out = self._run(eng, _req("동의", request_id="arm1"), ("BLOCK", "", "blocked"))
        self.assertIn("policy_api:user_consent:pass", out.get("reason") or "")
        taken = eng._user_consent_take_output_patch("chatgpt", "conv-1", "10.0.0.8")
        self.assertEqual(taken, "block-mid-9")
        self.assertEqual(
            eng._user_consent_take_output_patch("chatgpt", "conv-1", "10.0.0.8"),
            "",
        )

    def test_empty_session_rebind_then_consent(self) -> None:
        ape._USER_CONSENT = "on"
        eng = _engine()
        self.assertTrue(
            eng._user_consent_store("gpt", "", "10.0.0.8", "127.0.0.1", "mid-empty")
        )
        self.assertTrue(
            eng._user_consent_rebind_ip_to_session("gpt", "10.0.0.8", "conv-1")
        )
        out = self._run(eng, _req("동의합니다", request_id="rb1"), ("PASS", "", ""))
        self.assertIn("policy_api:user_consent:pass", out.get("reason") or "")
        other = self._run(
            eng,
            _req("동의", session="conv-other", request_id="rb2"),
            ("PASS", "", ""),
        )
        self.assertNotIn("user_consent:pass", other.get("reason") or "")

    def test_file_only_block_keeps_consent_slot(self) -> None:
        ape._USER_CONSENT = "on"
        eng = _engine()
        files = {"parts": [{"id": "file-1"}]}
        self.assertFalse(eng._user_consent_store("gpt", "conv-1", "10.0.0.8", "", "mid-file"))
        self.assertTrue(
            eng._user_consent_store(
                "gpt", "conv-1", "10.0.0.8", "", "mid-file", files
            )
        )
        hit = eng._user_consent_lookup("gpt", "conv-1", "10.0.0.8")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["prompt"], "첨부파일")
        self.assertEqual(hit["attachments"], files)
        out = self._run(eng, _req("동의", request_id="file-consent"), ("PASS", "", ""))
        self.assertIn("policy_api:user_consent:pass", out.get("reason") or "")

    def test_consent_response_has_no_override(self) -> None:
        ape._USER_CONSENT = "on"
        eng = _engine()
        self._run(eng, _req("127.0.0.1", request_id="ov1"), ("BLOCK", "", "blocked"))
        self.assertIn("동의", eng._response_override_by_session.get("conv-1") or "")
        self._run(eng, _req("동의", request_id="ov2"), ("BLOCK", "", "blocked"))
        self.assertFalse(bool(eng._response_override_by_session.get("conv-1")))

    def test_gpt_block_without_rewrite_is_hard_block_not_allow(self) -> None:
        ape._USER_CONSENT = "off"
        eng = _engine()
        fake = _FakeAdapter()
        fake.rewrite_prompt = lambda *args, **kwargs: None  # type: ignore[method-assign]
        fake.rewrite_prompt_for_block = lambda *args, **kwargs: None  # type: ignore[method-assign]
        eng.adapter_registry.get = lambda service_id: fake  # type: ignore[method-assign]
        eng._call_policy_api = AsyncMock(return_value=("BLOCK", "", "차단되었습니다."))  # type: ignore[method-assign]
        eng._flush_chatgpt_inline_output_log = lambda *args, **kwargs: None  # type: ignore[method-assign]
        eng._submit_output_log_async = lambda *args, **kwargs: None  # type: ignore[method-assign]
        req = _req("127.0.0.1", request_id="hb1")
        payload = json.dumps(
            {"prompt": "127.0.0.1", "conversation_id": "conv-1"},
            ensure_ascii=False,
        ).encode("utf-8")
        out = asyncio.run(eng.evaluate(req, payload))
        self.assertEqual(out.get("action"), "BLOCK")
        self.assertIn("hard_envoy", out.get("reason") or "")
        self.assertNotEqual(out.get("action"), "ALLOW")

    def test_consent_clears_block_override_for_old_request_id(self) -> None:
        ape._USER_CONSENT = "on"
        eng = _engine()
        blocked = self._run(
            eng,
            _req("127.0.0.1", request_id="block-rid"),
            ("BLOCK", "", "차단되었습니다."),
        )
        self.assertIn("policy_api:block", blocked.get("reason") or "")
        self.assertTrue(bool(eng._lookup_response_override("conv-1", "block-rid", "chatgpt")))
        replay = self._run(eng, _req("동의", request_id="consent-rid"), ("BLOCK", "", "blocked"))
        self.assertIn("user_consent:pass", replay.get("reason") or "")
        self.assertFalse(bool(eng._lookup_response_override("conv-1", "block-rid", "chatgpt")))
        self.assertTrue(eng._user_consent_skip_override("chatgpt", "conv-1", "block-rid"))
        self.assertFalse(eng._user_consent_skip_override("chatgpt", "conv-1", "second-block-rid"))
        second = self._run(
            eng,
            _req("127.0.0.1", request_id="second-block-rid"),
            ("BLOCK", "", "차단되었습니다."),
        )
        self.assertIn("policy_api:block", second.get("reason") or "")
        self.assertTrue(bool(eng._lookup_response_override("conv-1", "second-block-rid", "chatgpt")))

    def test_inline_services_match_gpt_hint_and_consent_replay(self) -> None:
        cases = (
            ("chatgpt", "chatgpt.com", "/backend-api/f/conversation"),
            (
                "gemini",
                "gemini.google.com",
                "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate",
            ),
            (
                "claude",
                "claude.ai",
                "/api/organizations/x/chat_conversations/y/completion",
            ),
            ("grok", "grok.com", "/rest/app-chat/conversations/x/chat-messages"),
            ("perplexity", "www.perplexity.ai", "/rest/sse/perplexity_ask"),
        )
        for service, host, path in cases:
            ape._USER_CONSENT = "on"
            eng = _engine()
            blocked = self._run(
                eng,
                _req(
                    "127.0.0.1",
                    request_id=f"{service}-b",
                    service_id=service,
                    host=host,
                    path=path,
                ),
                ("BLOCK", "[AIVAX MASKING]", "차단되었습니다."),
            )
            decoded = base64.b64decode(
                str((blocked.get("payload") or {}).get("raw_b64") or "")
            )
            if service == "grok":
                self.assertIn("### [AIVAX] 프롬프트 차단".encode("utf-8"), decoded, service)
                self.assertNotIn("[AIVAX 정책 응답 처리]".encode("utf-8"), decoded, service)
                self.assertTrue(_payload_has_consent_hint(decoded), service)
                override = eng._response_override_by_session.get("conv-1") or ""
                self.assertIn("### [AIVAX] 프롬프트 차단", override, service)
                self.assertTrue(_payload_has_consent_hint(override.encode("utf-8")), service)
            elif service == "claude":
                self.assertTrue(_payload_has_consent_hint(decoded), service)
                self.assertIn("### [AIVAX] 프롬프트 차단".encode("utf-8"), decoded, service)
                self.assertIn("**탐지 사유**".encode("utf-8"), decoded, service)
                self.assertNotIn("[AIVAX 정책 응답 처리]".encode("utf-8"), decoded, service)
                self.assertNotIn("[입력 프롬프트]".encode("utf-8"), decoded, service)
            elif service == "gemini":
                prompt_obj = json.loads(decoded.decode("utf-8"))
                rewrite_prompt = str(prompt_obj.get("prompt") or "")
                self.assertTrue(_payload_has_consent_hint(rewrite_prompt.encode("utf-8")), service)
                self.assertIn("### [AIVAX] 프롬프트 차단", rewrite_prompt, service)
                self.assertIn("SNIPER AIVAX", rewrite_prompt, service)
                self.assertNotIn("Reply only: BLOCKED", rewrite_prompt, service)
                self.assertIn("프롬프트 차단부터", str(prompt_obj.get("sys") or ""), service)
                override = eng._lookup_response_override("conv-1", f"{service}-b", service) or ""
                self.assertEqual("", override, service)
            elif service == "perplexity":
                prompt_obj = json.loads(decoded.decode("utf-8"))
                rewrite_prompt = str(prompt_obj.get("prompt") or "")
                self.assertTrue(_payload_has_consent_hint(rewrite_prompt.encode("utf-8")), service)
                self.assertIn("### [AIVAX] 프롬프트 차단", rewrite_prompt, service)
                self.assertIn("SNIPER AIVAX", rewrite_prompt, service)
                self.assertNotIn("[AIVAX 정책 응답 처리]", rewrite_prompt, service)
                self.assertIn("### [AIVAX] 프롬프트 차단부터 주의까지", str(prompt_obj.get("sys") or ""), service)
                override = eng._lookup_response_override("conv-1", f"{service}-b", service) or ""
                self.assertEqual("", override, service)
            else:
                self.assertTrue(_payload_has_consent_hint(decoded), service)
                self.assertIn("[AIVAX 정책 응답 처리]".encode("utf-8"), decoded, service)
            replay = self._run(
                eng,
                _req(
                    "동의",
                    request_id=f"{service}-c",
                    service_id=service,
                    host=host,
                    path=path,
                ),
                ("BLOCK", "", "blocked"),
            )
            self.assertIn("user_consent:pass", replay.get("reason") or "", service)
            replay_decoded = base64.b64decode(
                str((replay.get("payload") or {}).get("raw_b64") or "")
            )
            self.assertIn(b"127.0.0.1", replay_decoded, service)
            self.assertNotIn(
                "이전 차단 안내문은 무시".encode("utf-8"),
                replay_decoded,
                service,
            )

    def test_gemini_empty_session_rebind_then_consent(self) -> None:
        ape._USER_CONSENT = "on"
        eng = _engine()
        host = "gemini.google.com"
        path = "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate"
        blocked = self._run(
            eng,
            _req(
                "127.0.0.1",
                session="",
                request_id="gem-empty",
                service_id="gemini",
                host=host,
                path=path,
            ),
            ("BLOCK", "[AIVAX MASKING]", "차단되었습니다."),
        )
        self.assertIn("policy_api:block", blocked.get("reason") or "")
        self.assertTrue(
            eng._user_consent_rebind_request_to_session(
                "gemini", "gem-empty", "c_live"
            )
        )
        replay = self._run(
            eng,
            _req(
                "동의",
                session="c_live",
                request_id="gem-consent",
                service_id="gemini",
                host=host,
                path=path,
            ),
            ("BLOCK", "", "blocked"),
        )
        self.assertIn("user_consent:pass", replay.get("reason") or "")
        replay_decoded = base64.b64decode(
            str((replay.get("payload") or {}).get("raw_b64") or "")
        )
        self.assertIn(b"127.0.0.1", replay_decoded)
        self.assertNotIn("이전 차단 안내문은 무시".encode("utf-8"), replay_decoded)


class TestGeminiConsentRestoresOriginalPrompt(unittest.TestCase):
    def test_consent_rewrites_block_notice_and_ack_to_original(self) -> None:
        from urllib.parse import parse_qs, quote, unquote

        from adapters import gemini_engine as ge

        block_notice = (
            "127.0.0.1\n\n[AIVAX MASKING]\n\n[AIVAX] 프롬프트 차단\n\n"
            "[주의] '동의' 또는 '동의합니다' 입력 시 요청이 전송됩니다.\n"
            "전송에 대한 책임은 사용자에게 있습니다.\n\n"
            "[AIVAX 정책 응답 처리]\n제시된 안내문을 응답으로 출력합니다."
        )
        inner = [[block_notice, 0, None], ["동의", 0, None]]
        outer = json.dumps(
            [json.dumps(inner, ensure_ascii=False, separators=(",", ":"))],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload = ("f.req=" + quote(outer, safe="")).encode("utf-8")
        req = {
            "direction": "request",
            "unit_type": "form_body",
            "content_type": "application/x-www-form-urlencoded",
            "meta": {
                "host": "gemini.google.com",
                "path": "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate",
            },
        }
        out = ge.rewrite_prompt_for_consent(req, payload, "127.0.0.1", "동의")
        self.assertIsNotNone(out)
        encoded, reason = out or (b"", "")
        self.assertEqual(reason, "adapter:gemini_policy_consent")
        form = parse_qs(encoded.decode("utf-8"), keep_blank_values=True)
        restored = unquote((form.get("f.req") or [""])[0])
        self.assertIn("127.0.0.1", restored)
        self.assertNotIn("\"동의\"", restored)
        self.assertNotIn("프롬프트 차단", restored)
        self.assertEqual(restored.count("127.0.0.1"), 2)

    def test_consent_does_not_rewrite_unrelated_notice_tuples(self) -> None:
        from urllib.parse import parse_qs, quote, unquote

        from adapters import gemini_engine as ge

        decoy = "### [AIVAX] 프롬프트 차단 decoy"
        notice = (
            "[AIVAX MASKING]\n\n[AIVAX] 프롬프트 차단\n\n"
            "[주의] '동의' 또는 '동의합니다' 입력 시 요청이 전송됩니다."
        )
        inner = [
            [decoy, 0, None],
            ["hello", 0, None],
            [notice, 0, None],
            ["동의", 0, None],
        ]
        outer = json.dumps(
            [json.dumps(inner, ensure_ascii=False, separators=(",", ":"))],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        payload = ("f.req=" + quote(outer, safe="")).encode("utf-8")
        req = {
            "direction": "request",
            "unit_type": "form_body",
            "content_type": "application/x-www-form-urlencoded",
            "meta": {
                "host": "gemini.google.com",
                "path": "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate",
            },
        }
        out = ge.rewrite_prompt_for_consent(req, payload, "127.0.0.1", "동의")
        self.assertIsNotNone(out)
        encoded, _reason = out or (b"", "")
        restored = unquote(
            (parse_qs(encoded.decode("utf-8"), keep_blank_values=True).get("f.req") or [""])[0]
        )
        self.assertIn(decoy, restored)
        self.assertIn("hello", restored)
        self.assertNotIn("\"동의\"", restored)
        self.assertIn("127.0.0.1", restored)


class TestGeminiConsentBlockNoticeRace(unittest.TestCase):
    def test_looks_like_aivax_policy_block_notice(self) -> None:
        notice = (
            "### [AIVAX] 프롬프트 차단 > 🔎 AIVAX 정책에 의해 "
            "민감정보가 첨부파일에 포함"
        )
        self.assertTrue(ape.looks_like_aivax_policy_block_notice(notice))
        self.assertFalse(
            ape.looks_like_aivax_policy_block_notice("파일 정보는 다음과 같습니다.")
        )
        self.assertTrue(
            ape.looks_like_aivax_policy_block_notice(
                "### 🚫[정보보호팀] 해당 요청은 국가정보원 국가 사이버보안 기본지침 "
                "및 행정안전부 사이버보안지침에 따라서 차단되었습니다.\n"
                "입력하신 내용에 **'이메일'** 이 포함되어있습니다."
            )
        )

    def test_defer_skips_block_notice_and_drop_clears_hold(self) -> None:
        eng = _engine()
        notice = "### [AIVAX] 프롬프트 차단 > AIVAX 정책에 의해 차단"
        eng._defer_gemini_growing_output(
            {"_gemini_ui_message_id": "mid-block"},
            b"{}",
            notice,
            "gemini",
            _FakeAdapter(),
            "child-rid",
            "gemini",
        )
        self.assertNotIn("mid-block", eng._gemini_growing_outputs)

        eng._gemini_growing_outputs["mid-block"] = {
            "text": notice,
            "req": {},
            "payload": b"{}",
            "adapter_service_id": "gemini",
            "adapter": None,
            "request_id": "child-rid",
            "service_id": "gemini",
        }
        eng._gemini_growing_posted["mid-block"] = len(notice)

        class _Done:
            def done(self) -> bool:
                return True

            def cancel(self) -> None:
                return None

        eng._gemini_growing_tasks["mid-block"] = _Done()  # type: ignore[assignment]
        eng._drop_gemini_growing_output("mid-block", reason="user_consent")
        self.assertNotIn("mid-block", eng._gemini_growing_outputs)
        self.assertNotIn("mid-block", eng._gemini_growing_posted)
        self.assertNotIn("mid-block", eng._gemini_growing_tasks)

    def test_post_output_skips_block_notice_without_consuming_consent_patch(self) -> None:
        eng = _engine()
        eng._user_consent_arm_output_patch(
            "gemini", "c_sess", "10.0.50.60", "mid-block"
        )
        notice = "### [AIVAX] 프롬프트 차단 > AIVAX 정책에 의해 민감정보"
        eng._post_output_log_sync(
            {"_fake_session": "c_sess", "_gemini_ui_message_id": "mid-block"},
            b"{}",
            notice,
            "gemini",
            _FakeAdapter(),
            "child-rid",
            "gemini",
        )
        taken = eng._user_consent_take_output_patch("gemini", "c_sess", "10.0.50.60")
        self.assertEqual(taken, "mid-block")


class TestBlockOverrideStaysOnBlockedTurn(unittest.TestCase):
    def _seed(self, eng: ape.PolicyEngine, rid: str, action: str) -> None:
        eng._policy_decision_by_request[rid] = (
            time.monotonic() + 60,
            1,
            (action, "", ""),
            None,
            None,
        )

    def test_later_block_does_not_cover_earlier_pass_or_mask(self) -> None:
        eng = _engine()
        self._seed(eng, "pass-rid", "PASS")
        self._seed(eng, "mask-rid", "MASKING")
        self._seed(eng, "block-rid", "BLOCK")
        eng._set_response_override("conv-1", "block-rid", "BLOCK NOTICE")

        self.assertEqual(
            eng._lookup_response_override("conv-1", "block-rid", "gpt"),
            "BLOCK NOTICE",
        )
        self.assertEqual(eng._lookup_response_override("conv-1", "pass-rid", "gpt"), "")
        self.assertEqual(eng._lookup_response_override("conv-1", "mask-rid", "gpt"), "")
        self.assertEqual(
            eng._lookup_response_override("conv-1", "sse-only", "gpt"),
            "BLOCK NOTICE",
        )
        self.assertEqual(
            eng._lookup_response_override("conv-1", "", "gpt"),
            "BLOCK NOTICE",
        )

    def test_pending_block_is_not_attached_to_pass_response(self) -> None:
        eng = _engine()
        self._seed(eng, "pass-rid", "PASS")
        eng._queue_pending_response_override("gpt", "BLOCK NOTICE")
        adapter = _FakeAdapter()
        session_id = eng._bind_pending_response_override(
            {"request_id": "pass-rid", "_fake_session": "conv-1"},
            b"{}",
            adapter,
            "gpt",
            "pass-rid",
            "gpt",
        )
        self.assertEqual(session_id, "conv-1")
        self.assertEqual(eng._lookup_response_override("conv-1", "pass-rid", "gpt"), "")
        self.assertEqual(eng._peek_pending_response_override("gpt"), "BLOCK NOTICE")

        blocked_session = eng._bind_pending_response_override(
            {"_fake_session": "conv-1"},
            b"{}",
            adapter,
            "gpt",
            "block-rid",
            "gpt",
        )
        self.assertEqual(blocked_session, "conv-1")
        self.assertEqual(
            eng._lookup_response_override("conv-1", "block-rid", "gpt"),
            "BLOCK NOTICE",
        )
        self.assertEqual(eng._peek_pending_response_override("gpt"), "")


if __name__ == "__main__":
    unittest.main()
