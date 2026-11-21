"""환경·문구 파일의 마스킹 문자열을 실제 검사와 최종 병합까지 전달한다."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.adapters.inspection_regex import PythonRegexInspectionAdapter
from src.application.services.inspection_slm import SlmAnalysisResult
from src.application.use_cases.test_filter_rule import FilterRuleTestRequest
from src.bootstrap.env_loader import ConfigurationError, EnvironmentLoader
from src.bootstrap.pipeline_bootstrap import PipelineBootstrap
from src.domain.config_aivax.models import AivaxConfig
from src.domain.config_aivax.models.aivax_filters import AivaxFilters, FilterSettings
from src.domain.inspection_input.models import (
    InputAnalysisRequest,
    InputInspectionAction,
)
from src.domain.inspection_secuxper.models import SecuxperInspectionResult
from src.domain.policy_regex.models import RegexPolicyRule, RegexPolicySnapshot
from src.domain.policy_secuxper.models import SecuxperPolicyRule, SecuxperPolicySnapshot


@pytest.mark.parametrize(
    ("token", "phrase_token", "expected"),
    [
        (None, "", "[AIVAX MASKING]"),
        ("[비공개]", "", "[비공개]"),
        ("***", "", "***"),
        ("[ENV]", "[FILE]", "[FILE]"),
    ],
)
def test_mask_token_reaches_all_inspectors_final_merge_and_rule_test(
    tmp_path, monkeypatch, token, phrase_token, expected
):
    phrase_file = tmp_path / "block_phrases.json"
    phrase_file.write_text(json.dumps({"mask_token": phrase_token}), encoding="utf-8")
    monkeypatch.setenv("AIVAX_PIPELINE_BLOCK_PHRASES_FILE", str(phrase_file))
    env = (
        "AIVAX_PIPELINE_SECUXPERAI_TEAM_UUID=test-team\n"
        "AIVAX_PIPELINE_SECUXPERAI_CLIENT_NO=test-client\n"
    )
    if token is not None:
        env += f'AIVAX_PIPELINE_MASK_TOKEN="{token}"\n'
    (tmp_path / ".env").write_text(env, encoding="utf-8")
    loaded, settings = EnvironmentLoader(tmp_path, environ={}).bootstrap()
    assert loaded
    assert settings.mask_token == (token if token is not None else "[AIVAX MASKING]")

    regex_policy = RegexPolicySnapshot(
        rules=(RegexPolicyRule("secret", "Secret", "secret", "masking"),)
    )
    secuxper_policy = SecuxperPolicySnapshot(
        rules=(SecuxperPolicyRule("token", "Token", "policy-code", "default"),)
    )
    config = AivaxConfig(filters=AivaxFilters(secuxper=FilterSettings(True)))
    runtime = SimpleNamespace(
        ai_service_catalog=Mock(),
        aivax_config=SimpleNamespace(get_config=lambda: config),
        regex_inspection=PythonRegexInspectionAdapter(
            SimpleNamespace(get_policy=lambda: regex_policy)
        ),
        secuxper_policy=SimpleNamespace(get_policy=lambda: secuxper_policy),
        policy_valkey_connecter=Mock(),
        file_delivery=Mock(),
        attachment_notification_service=Mock(),
        analysis_log_merge_service=Mock(),
        analysis_log_delivery=Mock(),
    )
    services = SimpleNamespace(
        multimodal=Mock(),
        slm=SimpleNamespace(
            inspect=AsyncMock(
                return_value=SlmAnalysisResult(
                    action="masking",
                    evidence=(
                        {
                            "kind": "pii",
                            "value": "user@example.com",
                            "action": "masking",
                        },
                    ),
                )
            )
        ),
    )
    bootstrap = PipelineBootstrap(settings, None, Mock())
    monkeypatch.setattr(
        bootstrap,
        "_create_secuxper_inspection_adapter",
        lambda _: SimpleNamespace(
            inspect=AsyncMock(
                return_value=SecuxperInspectionResult(
                    provider_action="masking",
                    evidence=[{"kind": "pii", "value": "token", "action": "masking"}],
                )
            )
        ),
    )
    use_case = bootstrap._create_input_analysis_use_case(runtime, services)
    result = asyncio.run(
        use_case.execute(
            InputAnalysisRequest(
                requested_filter_names=(
                    "secret_filter",
                    "slm_filter",
                    "secuxper_filter",
                ),
                prompt="secret user@example.com token secret",
            )
        )
    ).analysis_result

    assert [execution.masked_prompt for execution in result.executions] == [
        f"{expected} user@example.com token {expected}",
        f"secret {expected} token secret",
        f"secret user@example.com {expected} secret",
    ]
    assert result.final_decision.action is InputInspectionAction.MASKING
    assert result.final_decision.masked_prompt == " ".join([expected] * 4)

    rule_test = bootstrap._create_test_filter_rule_use_case(
        runtime, services.multimodal, services.slm
    )
    tested = asyncio.run(
        rule_test.execute(
            FilterRuleTestRequest(prompt="secret and secret", regex_policy=regex_policy)
        )
    )
    assert tested.regex_result.content == f"{expected} and {expected}"
    assert tested.final_decision.content == f"{expected} and {expected}"


def test_shell_mask_token_overrides_env_file(tmp_path):
    (tmp_path / ".env").write_text('AIVAX_PIPELINE_MASK_TOKEN="[FILE]"\n')
    _, settings = EnvironmentLoader(
        tmp_path, environ={"AIVAX_PIPELINE_MASK_TOKEN": "[SHELL]"}
    ).bootstrap()
    assert settings.mask_token == "[SHELL]"


@pytest.mark.parametrize("token", ["", "   "])
def test_empty_mask_token_is_rejected(token):
    with pytest.raises(ConfigurationError, match="AIVAX_PIPELINE_MASK_TOKEN"):
        EnvironmentLoader(environ={}).build_settings(
            {"AIVAX_PIPELINE_MASK_TOKEN": token}
        )
