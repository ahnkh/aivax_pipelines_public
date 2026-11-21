"""실제 엔진이 만드는 채팅 카드와 로그 output_text를 비교한다."""

from __future__ import annotations

import json

import pytest

from src.domain.inspection_input.models import visible_block_log
from src.domain.inspection_input.models.block_phrases import get_block_phrases
from src.domain.inspection_input.models.visible_block_log import logged_block_output


@pytest.fixture(autouse=True)
def engine_renderers(request, monkeypatch, block_phrase_file):
    if not request.config.getoption("--run-engine-integration"):
        pytest.skip("외부 엔진 카드 비교는 --run-engine-integration으로 실행합니다.")
    monkeypatch.setenv("AIVAX_BLOCK_PHRASES_FILE", str(block_phrase_file))
    if visible_block_log._block_copy() is None:
        pytest.fail("채팅 카드 연동 검사에 ai_engine/core/block_copy.py가 필요합니다.")
    visible_block_log._engine()


def test_guideline_prompt_log_uses_the_visible_card_without_consent(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AIVAX_BLOCK_NOTICE_STYLE", "guideline")
    monkeypatch.setenv("AIVAX_DYN_POLICY_FILE", "/does/not/exist-aivax-policy.json")
    phrases = get_block_phrases()
    internal = phrases.prompt_block("이메일")
    logged = logged_block_output(internal, prompt="메일 보내줘")
    assert "이메일" in logged
    assert logged.startswith("### ")
    assert "정보보호팀" in logged
    assert "동의합니다" not in logged
    assert internal.startswith("[AIVAX] 프롬프트 차단")
    assert logged != internal


def test_hard_cso_log_has_no_consent_box(monkeypatch) -> None:
    monkeypatch.setenv("AIVAX_BLOCK_NOTICE_STYLE", "guideline")
    monkeypatch.setenv("AIVAX_DYN_POLICY_FILE", "/does/not/exist-aivax-policy.json")
    phrases = get_block_phrases()
    internal = phrases.cso_block(cs=True, untagged=False, error=False)
    logged = logged_block_output(internal, prompt="전송", cso_status="blocked")
    assert "S(민감)" in logged or "C(기밀)" in logged
    assert "동의" not in logged
    assert "등급 설정" in logged
    assert internal.startswith("[AIVAX] CSO 등급 차단")


def test_open_grade_with_filter_block_appends_guidance_and_consent_box(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("AIVAX_BLOCK_NOTICE_STYLE", "guideline")
    policy = tmp_path / "aivax_policy.json"
    policy.write_text(json.dumps({"user_consent": "on"}), encoding="utf-8")
    monkeypatch.setenv("AIVAX_DYN_POLICY_FILE", str(policy))
    phrases = get_block_phrases()
    internal = phrases.file_block("비밀")
    guidance = phrases.cso_guidance("open", blocked=True)
    logged = logged_block_output(
        internal,
        prompt="요약해줘",
        file_block=True,
        cso_status="open",
        cso_message=guidance,
    )
    assert "첨부파일내용" in logged
    assert "비밀" in logged
    assert guidance in logged
    assert "**'동의'**" in logged
    assert logged.index(guidance) < logged.index("**'동의'**")


def test_consent_box_stays_off_when_the_engine_switch_is_off(monkeypatch) -> None:
    monkeypatch.setenv("AIVAX_BLOCK_NOTICE_STYLE", "guideline")
    monkeypatch.setenv("AIVAX_DYN_POLICY_FILE", "/does/not/exist-aivax-policy.json")
    phrases = get_block_phrases()
    logged = logged_block_output(phrases.prompt_block("이메일"), prompt="질문")
    assert "**'동의'**" not in logged


def test_default_mode_log_matches_the_chat_card(monkeypatch) -> None:
    monkeypatch.setenv("AIVAX_BLOCK_NOTICE_STYLE", "default")
    monkeypatch.setenv("AIVAX_DYN_POLICY_FILE", "/does/not/exist-aivax-policy.json")
    phrases = get_block_phrases()
    internal = phrases.prompt_block("이메일")
    logged = logged_block_output(internal, prompt="질문", ai_service_id=1)
    import ai_policy_engine as engine

    expected = engine._gpt_gemini_visible_response(
        internal,
        consent_hint=False,
        analysis_ms=None,
        detect_filter="",
    )
    assert logged == expected
    assert logged.startswith("### [AIVAX] 프롬프트 차단")
    assert "이메일" in logged
    assert "분석시간" not in logged
    assert internal.startswith("[AIVAX] 프롬프트 차단\n")


def test_default_mode_cso_card_has_no_consent_box(monkeypatch) -> None:
    monkeypatch.setenv("AIVAX_BLOCK_NOTICE_STYLE", "default")
    monkeypatch.setenv("AIVAX_DYN_POLICY_FILE", "/does/not/exist-aivax-policy.json")
    phrases = get_block_phrases()
    internal = phrases.cso_block(cs=True, untagged=False, error=False)
    guidance = phrases.cso_guidance("blocked", blocked=True)
    logged = logged_block_output(
        internal,
        prompt="전송",
        cso_status="blocked",
        cso_message=guidance,
    )
    import ai_policy_engine as engine

    assert logged == engine._cso_blocked_visible_response(internal, guidance)
    assert "동의" not in logged


def test_consent_phrase_does_not_repeat_the_box(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIVAX_BLOCK_NOTICE_STYLE", "guideline")
    policy = tmp_path / "aivax_policy.json"
    policy.write_text(json.dumps({"user_consent": "on"}), encoding="utf-8")
    monkeypatch.setenv("AIVAX_DYN_POLICY_FILE", str(policy))
    phrases = get_block_phrases()
    logged = logged_block_output(phrases.prompt_block("이메일"), prompt="동의합니다")
    assert "**'동의'**" not in logged
