"""문구 파일과 서버 제목이 화면·지시문에 같이 쓰이는지 본다."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "core"), str(ROOT / "mirror")]

import ai_policy_engine as ape  # noqa: E402
import block_copy  # noqa: E402


FILE_MESSAGE = (
    "[AIVAX] 첨부파일 차단\n"
    "🔒AIVAX 정책에 의해 민감정보가 첨부파일에 포함된 것으로 탐지되었습니다.\n"
    "❌탐지 유형은 '비밀문서' 입니다.\n\n"
    "전송하신 첨부파일은 AIVAX에 의해서 요청이 차단되었습니다.\n"
    "세부 지침 사항은 관리자에게 문의해주세요\n"
)


class BlockPhraseTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("AIVAX_BLOCK_NOTICE_STYLE", None)
        block_copy.NOTICE_STYLE = ""

    def tearDown(self) -> None:
        os.environ.pop("AIVAX_BLOCK_NOTICE_STYLE", None)
        block_copy.load_phrases(block_copy.phrase_file_path())
        block_copy.NOTICE_STYLE = ""

    def test_prompt_instruction_stays_the_default_sentence(self) -> None:
        shown = block_copy._instruction_anchor(block_copy.PROMPT_BLOCK_TITLE, "[AIVAX] 프롬프트 차단")
        end = block_copy._instruction_anchor(block_copy.CONSENT_WARN_LABEL, "주의")
        self.assertIn(
            f"### {shown}부터 {end}까지",
            block_copy.GPT_BLOCK_MODEL_INSTRUCTION,
        )
        self.assertIn("SNIPER AIVAX", block_copy.GPT_BLOCK_MODEL_INSTRUCTION)
        self.assertNotIn(
            "요청하신 프롬프트/관리자 문의",
            block_copy.GPT_BLOCK_MODEL_INSTRUCTION,
        )
        self.assertNotIn(
            "[AIVAX 정책 응답 처리]",
            block_copy.PERPLEXITY_BLOCK_MODEL_INSTRUCTION,
        )

    def test_file_message_keeps_server_title_and_footer(self) -> None:
        visible = ape._block_visible_response(FILE_MESSAGE, consent_hint=True)
        self.assertTrue(visible.startswith("### [AIVAX] 첨부파일 차단"))
        self.assertNotIn("### [AIVAX] 프롬프트 차단", visible)
        self.assertIn("* **유형:** 비밀문서", visible)
        model_input = ape._block_model_input_prompt(
            "[AIVAX MASKING]",
            FILE_MESSAGE,
            consent_hint=True,
        )
        self.assertIn("SNIPER AIVAX\n\n### [AIVAX] 첨부파일 차단", model_input)
        self.assertIn("전송하신 첨부파일은", model_input)
        instruction = block_copy.instruction_for(FILE_MESSAGE)
        self.assertIn("### [AIVAX] 첨부파일 차단부터 주의까지", instruction)
        gemini_instruction = block_copy.instruction_for(FILE_MESSAGE, channel="gemini")
        self.assertEqual(gemini_instruction, instruction)
        self.assertNotIn("전송하신 첨부파일", instruction)

    def test_gemini_instruction_matches_the_shared_phrase_instruction(self) -> None:
        old_label = block_copy.CONSENT_WARN_LABEL
        block_copy.CONSENT_WARN_LABEL = "주의2"
        message = (
            "[AIVAX] 프롬프트 차단\n"
            "🔒차단 안내\n"
            "요청하신 프롬프트는 AIVAX 정책에 의해 차단되었습니다.\n"
            "관리자에게 문의해 주세요.\n"
        )
        try:
            instruction = block_copy.instruction_for(message, channel="gemini")
            shared = block_copy.instruction_for(message)
        finally:
            block_copy.CONSENT_WARN_LABEL = old_label
        self.assertEqual(instruction, shared)
        self.assertIn("### [AIVAX] 프롬프트 차단부터 주의2까지", instruction)

    def test_multiline_phrases_stay_out_of_the_instruction(self) -> None:
        old_mask = block_copy.MASK_TOKEN
        old_label = block_copy.CONSENT_WARN_LABEL
        old_anchor = block_copy.CSO_END_ANCHOR
        block_copy.MASK_TOKEN = "[AIVAX MASKING2]\n\n😀"
        block_copy.CONSENT_WARN_LABEL = "주의2\n\n😀"
        block_copy.CSO_END_ANCHOR = "마지막 안내 문장\n\n뭔소리😀"
        try:
            instruction = block_copy.instruction_for(
                "[AIVAX] 프롬프트 차단\n\n😀\n🔒잠금",
                channel="gemini",
            )
            cso = block_copy.instruction_for(
                "[AIVAX] CSO 등급 차단\n\n본문",
                cso=True,
                channel="gemini",
            )
        finally:
            block_copy.MASK_TOKEN = old_mask
            block_copy.CONSENT_WARN_LABEL = old_label
            block_copy.CSO_END_ANCHOR = old_anchor
        self.assertIn("### [AIVAX] 프롬프트 차단부터 주의2까지", instruction)
        self.assertNotIn("😀", instruction)
        self.assertNotIn("[AIVAX MASKING2]", instruction)
        self.assertNotIn("\n\n", instruction)
        self.assertIn("### [AIVAX] CSO 등급 차단부터 마지막 안내 문장까지", cso)
        self.assertNotIn("뭔소리", cso)
        self.assertNotIn("😀", cso)

    def test_guideline_title_keeps_emoji_and_newline_on_the_card(self) -> None:
        import os

        os.environ["AIVAX_BLOCK_NOTICE_STYLE"] = "guideline"
        old_title = block_copy.GUIDELINE_TITLE
        old_closing = block_copy.GUIDELINE_CLOSING
        block_copy.GUIDELINE_TITLE = "🔒 [정보보호팀] 차단되었습니다.\n두번째 안내"
        block_copy.GUIDELINE_CLOSING = "다시 확인해주시기 바랍니다."
        try:
            visible = block_copy.format_guideline_notice("이메일")
            self.assertTrue(
                visible.startswith("### 🔒 [정보보호팀] 차단되었습니다.\n두번째 안내\n")
            )
            instruction = block_copy.instruction_for(
                "[AIVAX] 프롬프트 차단\n❌탐지 유형은 '이메일' 입니다.\n",
                channel="gemini",
            )
            self.assertIn(
                "### [정보보호팀] 차단되었습니다.부터 다시 확인해주시기 바랍니다.까지",
                instruction,
            )
            self.assertNotIn("🔒", instruction)
            self.assertNotIn("두번째", instruction)
            self.assertNotIn("\n", instruction.split("부터", 1)[0].split("### ", 1)[-1])
        finally:
            os.environ.pop("AIVAX_BLOCK_NOTICE_STYLE", None)
            block_copy.GUIDELINE_TITLE = old_title
            block_copy.GUIDELINE_CLOSING = old_closing

    def test_gemini_rewrites_the_last_user_turn_with_the_card(self) -> None:
        import json

        import adapters.gemini_engine as gemini_engine

        inner = [["예전 질문", 0, None], ["test1234", 0, None]]
        f_req = json.dumps([json.dumps(inner, ensure_ascii=False)], ensure_ascii=False)
        rewritten, count = gemini_engine._rewrite_form_f_req_value(f_req, "카드", "test1234")
        self.assertGreater(count, 0)
        self.assertIn("예전 질문", rewritten or "")
        self.assertIn("카드", rewritten or "")
        self.assertNotIn("test1234", rewritten or "")

    def test_cso_instruction_uses_server_title(self) -> None:
        message = "[AIVAX] CSO 등급 차단\n\nC 또는 S 등급의 첨부파일이 있어 요청을 차단했습니다."
        instruction = block_copy.instruction_for(message, cso=True)
        self.assertIn("### [AIVAX] CSO 등급 차단부터 마지막 안내 문장까지", instruction)
        self.assertNotIn("프롬프트 차단", instruction)

    def test_every_phrase_keeps_newlines_and_bold_on_the_card(self) -> None:
        message = (
            "[AIVAX] 프롬프트 차단\n"
            "\n"
            "**제목 아래**\n"
            "😀\n"
            "🔒AIVAX 정책에 의해 민감정보가 프롬프트에 포함된 것으로 탐지되었습니다.😀\n"
            "\n"
            "**Hello World!**\n"
            "❌탐지 유형은 '이메일' 입니다.\n"
            "❌탐지된 내용은 '**주민번호**' 입니다.\n"
            "\n"
            "**내용 보충**\n"
            "민감 정보를 전송할 경우, 기밀 정보 또는 개인 정보 유출등의 "
            "피해가 발생할 수 있으니 각별한 주의를 부탁드려요\n"
            "요청하신 프롬프트는 AIVAX에 의해서 요청이 차단되었습니다.\n"
            "\n"
            "**푸터**\n"
            "세부 지침 사항은 관리자에게 문의해주세요\n"
        )
        visible = ape._block_visible_response(message, consent_hint=False)
        self.assertIn("> 🔎 **제목 아래**", visible)
        self.assertIn("> 😀", visible)
        self.assertLess(visible.find("> 😀"), visible.find("> 🔎 AIVAX 정책에 의해"))
        self.assertIn("> 🔎 AIVAX 정책에 의해 민감정보가 프롬프트에 포함된 것으로 탐지되었습니다.😀", visible)
        self.assertIn("> **Hello World!**", visible)
        self.assertLess(visible.find("> **Hello World!**"), visible.find("**탐지 사유**"))
        self.assertIn("* **유형:** 이메일", visible)
        self.assertIn("**주민번호**", visible)
        self.assertIn("**내용 보충**", visible)
        self.assertIn("각별한 주의를 부탁드려요", visible)
        self.assertIn("**푸터**", visible)
        self.assertLess(visible.find("**탐지 사유**"), visible.find("**푸터**"))

    def test_phrase_file_changes_title_lead_and_instruction(self) -> None:
        path = Path("/tmp") / "aivax-block-phrases-test.json"
        path.write_text(
            json.dumps(
                {
                    "prompt_title": "[AIVAX] 정책 차단",
                    "consent_warn_label": "확인",
                    "file_footer_lead": "올리신 파일은",
                    "hide_file_mention": "올리신 파일",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        block_copy.load_phrases(path)
        message = "[AIVAX] 정책 차단\n올리신 파일은 정책으로 막혔습니다.\n"
        self.assertEqual(block_copy.resolve_block_title(message), "[AIVAX] 정책 차단")
        self.assertTrue(block_copy.find_footer(message).startswith("올리신 파일은"))
        instruction = block_copy.instruction_for(message)
        self.assertIn("### [AIVAX] 정책 차단부터 확인까지", instruction)
        self.assertNotIn("올리신 파일은", instruction)
        path.unlink(missing_ok=True)

    def test_guideline_style_replaces_the_visible_card(self) -> None:
        import os

        os.environ["AIVAX_BLOCK_NOTICE_STYLE"] = "guideline"
        old_title = block_copy.GUIDELINE_TITLE
        old_detect = block_copy.GUIDELINE_DETECT
        old_file = block_copy.GUIDELINE_FILE_DETECT
        old_closing = block_copy.GUIDELINE_CLOSING
        block_copy.GUIDELINE_TITLE = (
            "[정보보호팀] 해당 요청은 행정안전부 사이버보안지침에 따라서 차단되었습니다."
        )
        block_copy.GUIDELINE_DETECT = "입력하신 내용에 **'{detect_type}'** 이 포함되어있습니다."
        block_copy.GUIDELINE_FILE_DETECT = "첨부파일내용에 '{detect_type}'이 포함되어있습니다."
        block_copy.GUIDELINE_CLOSING = "다시 확인해주시기 바랍니다."
        try:
            visible = ape._block_visible_response(FILE_MESSAGE, consent_hint=True)
            self.assertTrue(visible.startswith("### [정보보호팀] "))
            self.assertIn("행정안전부 사이버보안지침", visible)
            self.assertIn("첨부파일내용에 '비밀문서'이 포함되어있습니다.", visible)
            self.assertNotIn("입력하신 내용에 **'비밀문서'**", visible)
            self.assertIn("다시 확인해주시기 바랍니다.", visible)
            self.assertNotIn("탐지 사유", visible)
            self.assertIn("**'동의합니다'**", visible)
            self.assertLess(visible.find("다시 확인해주시기 바랍니다."), visible.find("동의합니다"))
            self.assertNotIn("[AIVAX] 첨부파일 차단", visible)
            prompt = ape._block_visible_response(
                "[AIVAX] 프롬프트 차단\n"
                "🔒AIVAX 정책에 의해 민감정보가 프롬프트에 포함된 것으로 탐지되었습니다.\n"
                "❌탐지 유형은 '이메일' 입니다.\n",
                consent_hint=True,
            )
            self.assertIn("입력하신 내용에 **'이메일'** 이 포함되어있습니다.", prompt)
            self.assertNotIn("첨부파일내용에", prompt)
            instruction = block_copy.instruction_for(FILE_MESSAGE)
            self.assertIn("[정보보호팀]", instruction)
            self.assertIn("다시 확인해주시기 바랍니다", instruction)
            self.assertNotIn("부터 주의까지", instruction)
            cso = block_copy.instruction_for(
                "[AIVAX] CSO 등급 차단\n\n본문",
                cso=True,
            )
            self.assertIn("CSO 등급 차단부터 마지막 안내 문장까지", cso)
            cso_card = block_copy.format_guideline_cso_notice_for_message(
                "[AIVAX] CSO 등급 차단\n\n"
                "C 또는 S 등급의 첨부파일이 있어 요청을 차단했습니다.\n"
                "CSO 등급이 존재하지 않는 첨부파일이 있어 요청을 차단했습니다."
            )
            self.assertTrue(cso_card.startswith("### [정보보호팀] "))
            self.assertIn("S(민감) 또는 C(기밀)", cso_card)
            self.assertIn("등급이 분류되지 않은 자료", cso_card)
            self.assertNotIn("전송할 수 없는 확장자", cso_card)
            self.assertIn("> **등급 설정**", cso_card)
            block_copy.GUIDELINE_CSO_HELP_LABEL = "등급 설정"
            block_copy.GUIDELINE_CSO_HELP = (
                "등급은 [안내 페이지](https://example.invalid/grade)에서 바꿉니다."
            )
            helped = block_copy.format_guideline_cso_notice(error=True)
            self.assertIn("전송할 수 없는 확장자", helped)
            self.assertIn("---\n> **등급 설정**\n등급은 [안내 페이지](https://example.invalid/grade)에서 바꿉니다.", helped)
            self.assertLess(helped.find("전송할 수 없는 확장자"), helped.find("> **등급 설정**"))
            visible = ape._cso_blocked_visible_response(
                "[AIVAX] CSO 등급 차단\n\n"
                "CSO 등급 확인에 실패한 첨부파일이 있어 요청을 차단했습니다.",
                "첨부파일의 CSO 등급 조회에 실패했습니다.",
            )
            self.assertEqual(visible, helped)
            self.assertNotIn("CSO 등급 조회에 실패했습니다.", visible)
            instruction = block_copy.instruction_for(
                "[AIVAX] CSO 등급 차단\n\n"
                "CSO 등급 확인에 실패한 첨부파일이 있어 요청을 차단했습니다.",
                cso=True,
            )
            self.assertIn("### [정보보호팀] ", instruction)
            self.assertIn(
                "부터 등급은 [안내 페이지](https://example.invalid/grade)에서 바꿉니다.까지",
                instruction,
            )
        finally:
            os.environ.pop("AIVAX_BLOCK_NOTICE_STYLE", None)
            block_copy.GUIDELINE_TITLE = old_title
            block_copy.GUIDELINE_DETECT = old_detect
            block_copy.GUIDELINE_FILE_DETECT = old_file
            block_copy.GUIDELINE_CLOSING = old_closing


    def test_phrase_template_reads_custom_emoji_newline_and_bold(self) -> None:
        old_detect = block_copy.DETECT_TYPE_LINE
        old_lock = block_copy.PROMPT_LOCK
        old_found = block_copy.DETECTED_CONTENT_LINE
        block_copy.DETECT_TYPE_LINE = "**📌 유형**\n'{detect_type}'\n확인"
        block_copy.PROMPT_LOCK = "🧪 **잠금**\n둘째 줄"
        block_copy.DETECTED_CONTENT_LINE = "발견한 값 **{detected}**"
        message = (
            "[AIVAX] 프롬프트 차단\n"
            "🧪 **잠금**\n"
            "둘째 줄\n"
            "**📌 유형**\n"
            "'이메일'\n"
            "확인\n"
            "발견한 값 **주민번호**\n"
        )
        try:
            parts = ape._parse_block_message_parts(message)
            self.assertEqual(parts["detect_type"], "이메일")
            self.assertIn("**잠금**", parts["headline"])
            self.assertIn("둘째 줄", parts["headline"])
            self.assertIn("**주민번호**", parts["notes"])
            os.environ["AIVAX_BLOCK_NOTICE_STYLE"] = "guideline"
            visible = ape._block_visible_response(message, consent_hint=False)
            self.assertIn("**'이메일'**", visible)
            self.assertIn("🧪", message)
        finally:
            os.environ.pop("AIVAX_BLOCK_NOTICE_STYLE", None)
            block_copy.DETECT_TYPE_LINE = old_detect
            block_copy.PROMPT_LOCK = old_lock
            block_copy.DETECTED_CONTENT_LINE = old_found


if __name__ == "__main__":
    unittest.main()
