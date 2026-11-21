"""엔진 렌더러에 전달하는 값과 렌더러가 없을 때의 로그를 검증한다."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.domain.inspection_input.models import visible_block_log
from src.domain.inspection_input.models.block_phrases import get_block_phrases


@pytest.fixture
def renderers(monkeypatch, tmp_path):
    copy = SimpleNamespace(
        CONSENT_SHORT="동의",
        CONSENT_LONG="동의합니다",
        guideline_notice_enabled=Mock(return_value=True),
        format_user_visible_block=Mock(return_value="guideline card"),
    )
    engine = SimpleNamespace(
        _gpt_gemini_visible_response=Mock(return_value="default card"),
        _cso_blocked_visible_response=Mock(return_value="cso card"),
        _block_display_text=Mock(return_value="block text"),
    )
    monkeypatch.setattr(visible_block_log, "_BLOCK_COPY", copy)
    monkeypatch.setattr(visible_block_log, "_ENGINE", engine)
    monkeypatch.setattr(visible_block_log, "_POLICY_CACHE", None)
    monkeypatch.setenv("AIVAX_DYN_POLICY_FILE", str(tmp_path / "missing-policy.json"))
    return copy, engine


def test_missing_engine_preserves_the_internal_block_message(monkeypatch):
    monkeypatch.setattr(visible_block_log, "_block_copy", lambda: None)
    internal = get_block_phrases().prompt_block("이메일")

    assert visible_block_log.logged_block_output(internal) == internal


def test_guideline_renderer_receives_the_internal_message(renderers):
    copy, _ = renderers
    internal = get_block_phrases().prompt_block("이메일")

    assert visible_block_log.logged_block_output(internal) == "guideline card"
    copy.format_user_visible_block.assert_called_once_with(
        internal, file_block=False, cso_status="", cso_message="", consent=False
    )


@pytest.mark.parametrize(
    ("status", "prompt", "consent"),
    [
        ("open", "요약해줘", True),
        ("open", "동의합니다!", False),
        ("open", "동의", False),
        ("blocked", "요약해줘", False),
        ("untagged", "요약해줘", False),
        ("error", "요약해줘", False),
    ],
)
def test_guideline_consent_and_cso_guidance(
    renderers, tmp_path, monkeypatch, status, prompt, consent
):
    copy, _ = renderers
    policy = tmp_path / "policy.json"
    policy.write_text('{"user_consent": "on"}', encoding="utf-8")
    monkeypatch.setenv("AIVAX_DYN_POLICY_FILE", str(policy))
    internal = get_block_phrases().file_block("비밀")

    assert (
        visible_block_log.logged_block_output(
            internal,
            prompt=prompt,
            file_block=True,
            cso_status=status,
            cso_message="CSO guidance",
        )
        == "guideline card"
    )
    copy.format_user_visible_block.assert_called_once_with(
        internal,
        file_block=True,
        cso_status=status,
        cso_message="CSO guidance" if status == "open" else "",
        consent=consent,
    )


@pytest.mark.parametrize("service_id", [0, 1, 2, 3, 4, 5, 99])
def test_default_renderer_selection_and_open_grade_guidance(renderers, service_id):
    copy, engine = renderers
    copy.guideline_notice_enabled.return_value = False
    internal = get_block_phrases().prompt_block("이메일")

    logged = visible_block_log.logged_block_output(
        internal,
        ai_service_id=service_id,
        cso_status="open",
        cso_message="CSO guidance",
        masked_contents="masked prompt",
    )

    if service_id == 99:
        assert logged == "block text"
        engine._block_display_text.assert_called_once_with(
            "masked prompt", internal, consent_hint=False, cso_message="CSO guidance"
        )
        engine._gpt_gemini_visible_response.assert_not_called()
    else:
        assert logged == "default card"
        engine._gpt_gemini_visible_response.assert_called_once_with(
            internal,
            consent_hint=False,
            analysis_ms=None,
            detect_filter="",
            cso_message="CSO guidance",
        )
        engine._block_display_text.assert_not_called()


@pytest.mark.parametrize("status", ["blocked", "untagged", "error"])
def test_default_cso_block_uses_the_cso_renderer(renderers, status):
    copy, engine = renderers
    copy.guideline_notice_enabled.return_value = False
    internal = get_block_phrases().cso_block(cs=True, untagged=False, error=False)

    assert (
        visible_block_log.logged_block_output(
            internal, cso_status=status, cso_message="CSO guidance"
        )
        == "cso card"
    )
    engine._cso_blocked_visible_response.assert_called_once_with(
        internal, "CSO guidance"
    )
    engine._gpt_gemini_visible_response.assert_not_called()
    engine._block_display_text.assert_not_called()


@pytest.mark.parametrize("guideline", [True, False])
def test_empty_rendered_card_preserves_the_internal_message(renderers, guideline):
    copy, engine = renderers
    copy.guideline_notice_enabled.return_value = guideline
    copy.format_user_visible_block.return_value = ""
    engine._gpt_gemini_visible_response.return_value = ""
    internal = get_block_phrases().prompt_block("이메일")

    assert visible_block_log.logged_block_output(internal) == internal
