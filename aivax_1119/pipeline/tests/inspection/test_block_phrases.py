"""차단 안내 문장이 문구 파일에서 조립되는지 본다."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from src.domain.inspection_input.models import block_phrases
from src.domain.inspection_input.models.block_phrases import (
    get_block_phrases,
    load_block_phrases,
    reload_block_phrases,
)


@pytest.mark.parametrize(
    "variable", ["AIVAX_BLOCK_PHRASES_FILE", "AIVAX_PIPELINE_BLOCK_PHRASES_FILE"]
)
def test_selected_env_file_supplies_the_phrase_path(tmp_path, monkeypatch, variable):
    phrase_file = tmp_path / "phrases.json"
    phrase_file.write_text(json.dumps({"prompt_title": "[DEV] 차단"}), encoding="utf-8")
    env_file = tmp_path / ".env.development"
    env_file.write_text(f"{variable}=./phrases.json\n", encoding="utf-8")
    monkeypatch.setattr(block_phrases, "_canonical_phrase_file", lambda: None)
    monkeypatch.delenv("AIVAX_PIPELINE_BLOCK_PHRASES_FILE", raising=False)
    monkeypatch.delenv("AIVAX_BLOCK_PHRASES_FILE", raising=False)
    monkeypatch.setenv("AIVAX_PIPELINE_ENV_FILE", str(env_file))

    assert get_block_phrases().prompt_block("이메일").startswith("[DEV] 차단\n")


@pytest.mark.parametrize(
    "variable", ["AIVAX_BLOCK_PHRASES_FILE", "AIVAX_PIPELINE_BLOCK_PHRASES_FILE"]
)
def test_shell_phrase_path_overrides_the_selected_env_file(
    tmp_path, monkeypatch, variable
):
    file_phrases = tmp_path / "file.json"
    file_phrases.write_text(json.dumps({"prompt_title": "[FILE]"}), encoding="utf-8")
    shell_phrases = tmp_path / "shell.json"
    shell_phrases.write_text(json.dumps({"prompt_title": "[SHELL]"}), encoding="utf-8")
    env_file = tmp_path / ".env.development"
    env_file.write_text(
        "AIVAX_PIPELINE_BLOCK_PHRASES_FILE=./file.json\n", encoding="utf-8"
    )
    monkeypatch.setattr(block_phrases, "_canonical_phrase_file", lambda: None)
    monkeypatch.delenv("AIVAX_PIPELINE_BLOCK_PHRASES_FILE", raising=False)
    monkeypatch.delenv("AIVAX_BLOCK_PHRASES_FILE", raising=False)
    monkeypatch.setenv("AIVAX_PIPELINE_ENV_FILE", str(env_file))
    monkeypatch.setenv(variable, str(shell_phrases))

    assert get_block_phrases().prompt_block("이메일").startswith("[SHELL]\n")


def test_default_messages_keep_the_current_sentences(monkeypatch) -> None:
    monkeypatch.delenv("AIVAX_PIPELINE_BLOCK_PHRASES_FILE", raising=False)
    monkeypatch.delenv("AIVAX_BLOCK_PHRASES_FILE", raising=False)
    phrases = replace(load_block_phrases(), notice_style="")
    prompt = phrases.prompt_block("이메일")
    assert prompt.startswith("[AIVAX] 프롬프트 차단\n")
    assert "❌탐지 유형은 '이메일' 입니다.\n" in prompt
    assert "요청하신 프롬프트는 AIVAX에 의해서 요청이 차단되었습니다.\n" in prompt
    assert prompt.endswith("세부 지침 사항은 관리자에게 문의해주세요\n")

    attached = phrases.file_block("비밀")
    assert attached.startswith("[AIVAX] 첨부파일 차단\n")
    assert "전송하신 첨부파일은 AIVAX에 의해서 요청이 차단되었습니다.\n" in attached

    cso = phrases.cso_block(cs=False, untagged=True, error=False)
    assert cso.startswith("[AIVAX] CSO 등급 차단\n\n")
    assert "CSO 등급이 존재하지 않는 첨부파일" in cso
    assert "CSO 등급 태깅 절차" in phrases.cso_guidance("untagged", blocked=True)
    open_block = phrases.cso_guidance("open", blocked=True)
    assert open_block.startswith("⚠️ 주의\n첨부파일에 대한 결재가 상신됩니다. ")
    assert "‘동의합니다’" in open_block
    assert "‘동의합니다’" not in phrases.cso_guidance("open", blocked=False)


def test_guideline_open_notice_only_when_guideline_style(monkeypatch) -> None:
    monkeypatch.delenv("AIVAX_BLOCK_NOTICE_STYLE", raising=False)
    base = load_block_phrases()
    default = replace(base, notice_style="")
    assert default.cso_guidance("open", blocked=False) == (
        "⚠️ 주의\n첨부파일에 대한 결재가 상신됩니다."
    )
    guideline = replace(base, notice_style="guideline")
    text = guideline.cso_guidance("open", blocked=False)
    assert text.startswith("## 📋[정보보호팀] ")
    assert "사후 결재가 상신됩니다." in text
    assert "1개월 내 사후 결재" in text
    monkeypatch.setenv("AIVAX_BLOCK_NOTICE_STYLE", "")
    assert (
        replace(base, notice_style="")
        .cso_guidance("open", blocked=False)
        .startswith("⚠️ 주의\n")
    )
    monkeypatch.delenv("AIVAX_BLOCK_NOTICE_STYLE", raising=False)


def test_phrase_file_overrides_the_title(tmp_path, monkeypatch) -> None:
    path = tmp_path / "block_phrases.json"
    path.write_text(
        json.dumps(
            {"prompt_title": "[AIVAX] 정책 차단", "file_title": "[AIVAX] 파일 정책"}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIVAX_PIPELINE_BLOCK_PHRASES_FILE", str(path))
    reload_block_phrases()
    try:
        phrases = get_block_phrases()
        assert phrases.prompt_block("IP").startswith("[AIVAX] 정책 차단\n")
        assert phrases.file_block("비밀").startswith("[AIVAX] 파일 정책\n")
        assert "요청하신 프롬프트는" in phrases.prompt_block("IP")
    finally:
        monkeypatch.delenv("AIVAX_PIPELINE_BLOCK_PHRASES_FILE", raising=False)
        reload_block_phrases()
