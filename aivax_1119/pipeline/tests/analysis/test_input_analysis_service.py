"""multiple_filter 이전 전용 입력 분석 유스케이스 계약."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from src.application.use_cases.input_analysis import InputAnalysisUseCase
from src.domain.config_aivax.models import AivaxConfig, AivaxMode
from src.domain.inspection_input.errors import UnsupportedInputInspectorError
from src.domain.inspection_input.models.visible_block_log import (
    logged_block_output_for_result,
)
from src.domain.inspection_input.models import (
    InputAnalysisOptions,
    InputAnalysisRequest,
    InputInspectionAction,
    InputInspectionResult,
    InputInspectorType,
)


@dataclass
class StubInspector:
    result: InputInspectionResult
    calls: int = 0

    async def inspect(self, request: InputAnalysisRequest) -> InputInspectionResult:
        self.calls += 1
        return self.result


def _use_case(*, options=None, **kwargs) -> InputAnalysisUseCase:
    aivax_config = kwargs.pop("aivax_config", None)
    if aivax_config is None:
        aivax_config = MagicMock()
        aivax_config.get_config.return_value = AivaxConfig()
    input_log_builder = kwargs.pop("input_log_builder", None)
    if input_log_builder is None:
        input_log_builder = MagicMock()
        input_log_builder.build.return_value = {
            "message_id": "message",
            "prompt": "prompt",
            "user": {},
        }
    analysis_log_merge_service = kwargs.pop("analysis_log_merge_service", None)
    if analysis_log_merge_service is None:
        analysis_log_merge_service = MagicMock()
    kwargs.setdefault("regex_inspector", MagicMock())
    kwargs.setdefault("multimodal_inspector", MagicMock())
    kwargs.setdefault("slm_inspector", MagicMock())
    return InputAnalysisUseCase(
        options=options or InputAnalysisOptions(),
        input_log_builder=input_log_builder,
        analysis_log_merge_service=analysis_log_merge_service,
        aivax_config=aivax_config,
        **kwargs,
    )


def _request(*inspector_types: InputInspectorType) -> InputAnalysisRequest:
    return InputAnalysisRequest(
        requested_filter_names=tuple(item.value for item in inspector_types),
        prompt="prompt",
    )


def _mirror_config():
    config = MagicMock()
    config.get_config.return_value = AivaxConfig(mode=AivaxMode("mirror"))
    return config


def test_stops_after_block() -> None:
    regex = StubInspector(
        InputInspectionResult(
            inspector_type=InputInspectorType.REGEX,
            action=InputInspectionAction.BLOCK,
            block_message="blocked",
        )
    )
    slm = StubInspector(InputInspectionResult(inspector_type=InputInspectorType.SLM))
    use_case = _use_case(regex_inspector=regex, slm_inspector=slm)

    outcome = asyncio.run(
        use_case.execute(
            _request(InputInspectorType.REGEX, InputInspectorType.SLM),
        )
    )
    result = outcome.analysis_result

    assert result.final_decision.action is InputInspectionAction.BLOCK
    assert result.final_decision.block_message == "blocked"
    assert result.stopped_after_block
    assert regex.calls == 1
    assert slm.calls == 0
    assert tuple(execution.inspector_type for execution in result.executions) == (
        InputInspectorType.REGEX,
        InputInspectorType.SLM,
    )
    assert result.executions[1].action is InputInspectionAction.BLANK
    assert result.executions[1].details == {"slm_content": ""}
    assert result.executions[0].duration_ms is not None


def test_later_block_has_priority_over_prior_masking() -> None:
    masking = StubInspector(
        InputInspectionResult(
            inspector_type=InputInspectorType.REGEX,
            action=InputInspectionAction.MASKING,
            masked_prompt="[masked]",
        )
    )
    blocking = StubInspector(
        InputInspectionResult(
            inspector_type=InputInspectorType.SLM,
            action=InputInspectionAction.BLOCK,
            block_message="blocked by slm",
        )
    )
    use_case = _use_case(
        regex_inspector=masking,
        slm_inspector=blocking,
    )

    outcome = asyncio.run(
        use_case.execute(
            _request(InputInspectorType.REGEX, InputInspectorType.SLM),
        )
    )
    result = outcome.analysis_result

    assert result.final_decision.action is InputInspectionAction.BLOCK
    assert result.final_decision.block_message == "blocked by slm"
    assert result.stopped_after_block


def test_bypass_block_preserves_prior_masking_legacy_semantics() -> None:
    masking = StubInspector(
        InputInspectionResult(
            inspector_type=InputInspectorType.REGEX,
            action=InputInspectionAction.MASKING,
            masked_prompt="[masked]",
        )
    )
    blocking = StubInspector(
        InputInspectionResult(
            inspector_type=InputInspectorType.SLM,
            action=InputInspectionAction.BLOCK,
        )
    )
    use_case = _use_case(
        regex_inspector=masking,
        slm_inspector=blocking,
        options=InputAnalysisOptions(
            bypass_block=True,
        ),
    )

    outcome = asyncio.run(
        use_case.execute(
            _request(InputInspectorType.REGEX, InputInspectorType.SLM),
        )
    )
    result = outcome.analysis_result

    assert result.final_decision.action is InputInspectionAction.MASKING
    assert result.final_decision.masked_prompt == "[masked]"


def test_unknown_filter_is_explicitly_rejected() -> None:
    use_case = _use_case()

    with pytest.raises(UnsupportedInputInspectorError):
        asyncio.run(
            use_case.execute(
                InputAnalysisRequest(
                    requested_filter_names=("unknown_filter",),
                    prompt="prompt",
                )
            )
        )


def test_service_interprets_input_log_request_without_controller_decision() -> None:
    input_log_service = MagicMock()
    input_log_service.build.return_value = {"filter_detect": []}
    merge_service = MagicMock()
    use_case = _use_case(
        input_log_builder=input_log_service,
        analysis_log_merge_service=merge_service,
    )
    request = InputAnalysisRequest(
        requested_filter_names=("input_filter",),
        prompt="prompt",
        message_id="input-log",
    )

    outcome = asyncio.run(use_case.execute(request))
    result = outcome.analysis_result

    assert result.executions == ()
    assert result.write_input_log
    assert outcome.log_evidence == {"filter_detect": []}
    input_log_service.build.assert_called_once_with(request, result)
    merge_service.record_input.assert_called_once()


def test_service_delivers_block_without_output_api() -> None:
    blocking = StubInspector(
        InputInspectionResult(
            inspector_type=InputInspectorType.REGEX,
            action=InputInspectionAction.BLOCK,
            block_message="blocked",
        )
    )
    input_log_service = MagicMock()
    input_log_service.build.return_value = {
        "@timestamp": "2026-09-12T00:00:00Z",
        "message_id": "blocked-input",
        "prompt": "prompt",
        "client_host": "127.0.0.1",
        "ai_service_id": 1,
        "ai_service": "GPT",
        "mode": "block",
        "policy": {"policy_name": "IP policy"},
        "user": {"id": "user-1", "email": "user@example.com"},
    }
    merge_service = MagicMock()
    use_case = _use_case(
        regex_inspector=blocking,
        input_log_builder=input_log_service,
        analysis_log_merge_service=merge_service,
    )
    request = InputAnalysisRequest(
        requested_filter_names=(InputInspectorType.REGEX.value,),
        prompt="prompt",
        message_id="blocked-input",
    )

    outcome = asyncio.run(use_case.execute(request))

    merge_service.deliver.assert_called_once()
    delivered_document = merge_service.deliver.call_args.args[0]
    assert delivered_document["prompt"] == "prompt"
    assert delivered_document["output_text"] == logged_block_output_for_result(
        outcome.analysis_result, "prompt"
    )
    assert outcome.log_evidence == input_log_service.build.return_value


def test_service_does_not_create_output_for_allow_without_input_log() -> None:
    allowing = StubInspector(
        InputInspectionResult(
            inspector_type=InputInspectorType.REGEX,
            action=InputInspectionAction.ACCEPT,
        )
    )
    merge_service = MagicMock()
    use_case = _use_case(
        regex_inspector=allowing,
        analysis_log_merge_service=merge_service,
    )

    asyncio.run(use_case.execute(_request(InputInspectorType.REGEX)))

    merge_service.record_input.assert_not_called()
    merge_service.deliver.assert_not_called()


def test_service_delegates_late_input_merge_to_analysis_log_merge_service() -> None:
    input_log_service = MagicMock()
    input_log_service.build.return_value = {
        "message_id": "output-first",
        "prompt": "prompt",
        "user": {},
    }
    merge_service = MagicMock()
    use_case = _use_case(
        input_log_builder=input_log_service,
        analysis_log_merge_service=merge_service,
    )
    request = InputAnalysisRequest(
        requested_filter_names=("input_filter",),
        prompt="prompt",
        message_id="output-first",
    )

    asyncio.run(use_case.execute(request))

    merge_service.record_input.assert_called_once_with(
        "output-first",
        input_log_service.build.return_value,
    )


def test_service_delegates_block_delivery_to_analysis_log_merge_service() -> None:
    blocking = StubInspector(
        InputInspectionResult(
            inspector_type=InputInspectorType.REGEX,
            action=InputInspectionAction.BLOCK,
            block_message="blocked",
        )
    )
    input_log_service = MagicMock()
    input_log_service.build.return_value = {
        "@timestamp": "2026-09-12T00:00:00Z",
        "message_id": "delivery-failed",
        "prompt": "prompt",
        "mode": "block",
        "user": {},
    }
    merge_service = MagicMock()
    use_case = _use_case(
        regex_inspector=blocking,
        input_log_builder=input_log_service,
        analysis_log_merge_service=merge_service,
    )
    request = InputAnalysisRequest(
        requested_filter_names=(InputInspectorType.REGEX.value,),
        prompt="prompt",
        message_id="delivery-failed",
    )

    asyncio.run(use_case.execute(request))

    merge_service.deliver.assert_called_once()


def test_mirror_returns_allow_before_analysis_finishes() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class WaitingInspector:
            async def inspect(
                self,
                request: InputAnalysisRequest,
            ) -> InputInspectionResult:
                started.set()
                await release.wait()
                return InputInspectionResult(
                    inspector_type=InputInspectorType.REGEX,
                    action=InputInspectionAction.BLOCK,
                )

        use_case = _use_case(
            regex_inspector=WaitingInspector(),
            aivax_config=_mirror_config(),
        )
        outcome = await use_case.execute(_request(InputInspectorType.REGEX))

        assert (
            outcome.analysis_result.final_decision.action is InputInspectionAction.BLANK
        )
        assert not outcome.debug_details_enabled
        await asyncio.wait_for(started.wait(), timeout=0.2)

        release.set()
        await use_case.shutdown()

    asyncio.run(scenario())


def test_mirror_stops_after_block_and_waits_for_real_output() -> None:
    async def scenario() -> None:
        regex = StubInspector(
            InputInspectionResult(
                inspector_type=InputInspectorType.REGEX,
                action=InputInspectionAction.BLOCK,
                block_message="detected only",
            )
        )
        multimodal = StubInspector(
            InputInspectionResult(
                inspector_type=InputInspectorType.MULTIMODAL,
            )
        )
        slm = StubInspector(
            InputInspectionResult(
                inspector_type=InputInspectorType.SLM,
            )
        )
        input_log_builder = MagicMock()
        input_log_builder.build.return_value = {
            "message_id": "mirror-message",
            "prompt": "prompt",
            "user": {},
        }
        merge_service = MagicMock()
        use_case = _use_case(
            regex_inspector=regex,
            multimodal_inspector=multimodal,
            slm_inspector=slm,
            input_log_builder=input_log_builder,
            analysis_log_merge_service=merge_service,
            aivax_config=_mirror_config(),
        )
        request = InputAnalysisRequest(
            requested_filter_names=(
                "input_filter",
                InputInspectorType.REGEX.value,
                InputInspectorType.MULTIMODAL.value,
                InputInspectorType.SLM.value,
            ),
            prompt="prompt",
            message_id="mirror-message",
        )

        outcome = await use_case.execute(request)
        await use_case.shutdown()

        assert (
            outcome.analysis_result.final_decision.action is InputInspectionAction.BLANK
        )
        assert regex.calls == 1
        assert multimodal.calls == 0
        assert slm.calls == 0
        background_result = input_log_builder.build.call_args.args[1]
        assert background_result.final_decision.action is InputInspectionAction.BLOCK
        assert background_result.stopped_after_block
        assert tuple(
            execution.inspector_type for execution in background_result.executions
        ) == (
            InputInspectorType.REGEX,
            InputInspectorType.MULTIMODAL,
            InputInspectorType.SLM,
        )
        assert background_result.executions[1].details == {
            "file_summary": [],
        }
        assert background_result.executions[2].details == {
            "slm_content": "",
        }
        merge_service.record_input.assert_called_once()

    asyncio.run(scenario())


def test_mirror_merges_output_that_arrived_during_background_analysis() -> None:
    async def scenario() -> None:
        blocking = StubInspector(
            InputInspectionResult(
                inspector_type=InputInspectorType.REGEX,
                action=InputInspectionAction.BLOCK,
                block_message="detected only",
            )
        )
        input_log_builder = MagicMock()
        input_log_builder.build.return_value = {
            "message_id": "mirror-output-first",
            "prompt": "prompt",
            "user": {},
        }
        merge_service = MagicMock()
        use_case = _use_case(
            regex_inspector=blocking,
            input_log_builder=input_log_builder,
            analysis_log_merge_service=merge_service,
            aivax_config=_mirror_config(),
        )
        request = InputAnalysisRequest(
            requested_filter_names=(InputInspectorType.REGEX.value,),
            prompt="prompt",
            message_id="mirror-output-first",
        )

        await use_case.execute(request)
        await use_case.shutdown()

        merge_service.record_input.assert_called_once_with(
            "mirror-output-first",
            input_log_builder.build.return_value,
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("secuxper_enabled", [True, False])
def test_secuxper_runs_independently_after_slm(secuxper_enabled):
    from src.domain.config_aivax.models import AivaxFilters, FilterSettings

    slm = StubInspector(InputInspectionResult(inspector_type=InputInspectorType.SLM))
    secuxper = StubInspector(
        InputInspectionResult(inspector_type=InputInspectorType.SECUXPER)
    )
    config = MagicMock()
    config.get_config.return_value = AivaxConfig(
        filters=AivaxFilters(secuxper=FilterSettings(secuxper_enabled)),
    )
    use_case = _use_case(
        slm_inspector=slm, secuxper_inspector=secuxper, aivax_config=config
    )
    result = asyncio.run(
        use_case.execute(_request(InputInspectorType.SECUXPER, InputInspectorType.SLM))
    )
    assert slm.calls == 1
    assert secuxper.calls == int(secuxper_enabled)
    assert [item.inspector_type for item in result.analysis_result.executions] == (
        [InputInspectorType.SLM, InputInspectorType.SECUXPER]
        if secuxper_enabled
        else [InputInspectorType.SLM]
    )


def test_disabled_slm_does_not_disable_secuxper():
    from src.domain.config_aivax.models import AivaxFilters, FilterSettings

    slm = StubInspector(InputInspectionResult(inspector_type=InputInspectorType.SLM))
    secuxper = StubInspector(
        InputInspectionResult(inspector_type=InputInspectorType.SECUXPER)
    )
    config = MagicMock()
    config.get_config.return_value = AivaxConfig(
        filters=AivaxFilters(slm=FilterSettings(False), secuxper=FilterSettings(True)),
    )
    use_case = _use_case(
        slm_inspector=slm, secuxper_inspector=secuxper, aivax_config=config
    )
    asyncio.run(
        use_case.execute(_request(InputInspectorType.SLM, InputInspectorType.SECUXPER))
    )
    assert slm.calls == 0
    assert secuxper.calls == 1


def test_blocked_slm_prevents_secuxper_call():
    from src.domain.config_aivax.models import AivaxFilters, FilterSettings

    slm = StubInspector(
        InputInspectionResult(
            inspector_type=InputInspectorType.SLM,
            action=InputInspectionAction.BLOCK,
            block_message="blocked",
        )
    )
    secuxper = StubInspector(
        InputInspectionResult(
            inspector_type=InputInspectorType.SECUXPER,
        )
    )
    config = MagicMock()
    config.get_config.return_value = AivaxConfig(
        filters=AivaxFilters(secuxper=FilterSettings(True)),
    )
    use_case = _use_case(
        slm_inspector=slm,
        secuxper_inspector=secuxper,
        aivax_config=config,
    )
    result = asyncio.run(
        use_case.execute(
            _request(
                InputInspectorType.SLM,
                InputInspectorType.SECUXPER,
            )
        )
    )
    assert slm.calls == 1
    assert secuxper.calls == 0
    assert result.analysis_result.stopped_after_block is True


def test_failed_filter_keeps_reason_and_allows_later_block():
    class FailingInspector:
        async def inspect(self, request):
            raise RuntimeError("inspection failed")

    blocking = StubInspector(InputInspectionResult(
        inspector_type=InputInspectorType.SLM,
        action=InputInspectionAction.BLOCK,
        block_message="blocked by slm",
    ))
    use_case = _use_case(regex_inspector=FailingInspector(), slm_inspector=blocking)
    result = asyncio.run(use_case.execute(_request(InputInspectorType.REGEX, InputInspectorType.SLM))).analysis_result
    assert result.final_decision.action is InputInspectionAction.BLOCK
    assert result.executions[0].action is InputInspectionAction.FAILED
    assert result.executions[0].error.code == "INTERNAL_ERROR"
    assert result.executions[0].duration_ms is not None
    assert blocking.calls == 1
