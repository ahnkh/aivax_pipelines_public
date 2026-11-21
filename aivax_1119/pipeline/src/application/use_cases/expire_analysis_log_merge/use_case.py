"""만료된 분석 로그 병합 대기 항목을 처리하는 유스케이스."""

import logging
from typing import Any

from src.core.constants import ERR_OK
from src.domain.analysis_log_merge.models.log_contract import (
    AnalysisLogContract as LOG_INDEX_DEFINE,
)
from src.domain.analysis_log_merge.services import AnalysisLogMerger

from src.domain.analysis_log_merge.ports.analysis_log_merge_store import (
    AnalysisLogMergeStorePort,
)
from src.application.services.analysis_log_merge import AnalysisLogMergeService


class ExpireAnalysisLogMergeUseCase:
    """상대 로그가 끝내 도착하지 않은 병합 대기 항목을 한 번 처리한다.

    만료 문서를 만든 뒤 전달 함수에 넘길 뿐, 파일 저장이나 이벤트 발행 구현은
    소유하지 않는다. 해당 구현은 조립부가 주입한 후속 처리 함수의 책임이다.
    """

    def __init__(
        self,
        store: AnalysisLogMergeStorePort,
        merge_service: AnalysisLogMergeService,
        *,
        logger: logging.Logger | None = None,
    ):
        self._logger = (
            logger if logger is not None else logging.getLogger("khan-python")
        )
        self._store = store
        self._merge_service = merge_service

    def execute(self) -> int:
        """만료된 INPUT/OUTPUT을 한 번씩 회수해 후속 출력으로 전달한다."""
        self._flush_expired_inputs()
        self._flush_expired_outputs()
        return ERR_OK

    def _flush_expired_inputs(self) -> int:
        listExpired = self._store.pop_expired_inputs()
        if 0 == len(listExpired):
            return ERR_OK

        for expiredLog in listExpired:
            strLogMessageKey = expiredLog.message_key.value
            dictPromptLog = expiredLog.payload

            if dictPromptLog is None:
                self._logger.error(
                    "input expired before flush (prompt lost), "
                    f"messageid={strLogMessageKey}, reason=key_ttl_elapsed"
                )
                continue
            prompt_document: dict[str, Any] = {
                str(key): value for key, value in dictPromptLog.items()
            }
            late_output = expiredLog.late_output
            if str((late_output or {}).get("output_text") or "").strip():
                # 응답은 이미 보관돼 있었다. 프롬프트 단독 문서로 확정하면
                # 그 응답은 갱신 파일에만 남고 본문 수집이 비게 된다.
                self._logger.error(
                    "late output recovered while flushing input, "
                    f"messageid={strLogMessageKey}, "
                    f"service={prompt_document.get('ai_service')}, "
                    f"client_host={prompt_document.get('client_host')}, "
                    "reason=output_recovered_on_input_flush"
                )
                self._merge_service.deliver(
                    AnalysisLogMerger().merge(prompt_document, dict(late_output or {}))
                )
                continue

            self._logger.error(
                "no output captured, persisting prompt-only log, "
                f"messageid={strLogMessageKey}, "
                f"service={prompt_document.get('ai_service')}, "
                f"client_host={prompt_document.get('client_host')}, "
                "reason=output_absent"
            )
            prompt_document[LOG_INDEX_DEFINE.FIELD_MERGE_STATE] = (
                LOG_INDEX_DEFINE.MERGE_STATE_INPUT_ONLY
            )
            prompt_document.setdefault("output_text", "")
            self._merge_service.deliver(prompt_document)

        return ERR_OK

    def _flush_expired_outputs(self) -> int:
        listExpired = self._store.pop_expired_outputs()
        if 0 == len(listExpired):
            return ERR_OK

        for expiredLog in listExpired:
            strLogMessageKey = expiredLog.message_key.value
            dictOutputLog = expiredLog.payload

            if dictOutputLog is None:
                continue
            output_document: dict[str, Any] = {
                str(key): value for key, value in dictOutputLog.items()
            }

            # 문서가 없는 갱신은 Fluent Bit 청크를 영구 재시도하게 만든다.
            # 이미 적재된 프롬프트에만 본문을 갱신하고, 검사가 끝나지 않은
            # 입력은 보관했다가 도착할 때 한 문서로 합친다.
            if str(output_document.get("output_text") or "").strip():
                resolution = self._store.resolve_unmatched_output(
                    expiredLog.message_key, output_document
                )
                if resolution.action == "merge" and resolution.input_log is not None:
                    self._logger.error(
                        "output flush merged with waiting input, "
                        f"messageid={strLogMessageKey}, "
                        f"service={output_document.get('ai_service')}, "
                        f"client_host={output_document.get('client_host')}, "
                        "reason=output_merged_with_waiting_input"
                    )
                    self._merge_service.deliver(
                        AnalysisLogMerger().merge(resolution.input_log, output_document)
                    )
                    continue
                if resolution.action == "revise":
                    self._logger.error(
                        "input already flushed, revising output on existing log, "
                        f"messageid={strLogMessageKey}, "
                        f"service={output_document.get('ai_service')}, "
                        f"client_host={output_document.get('client_host')}, "
                        "reason=input_absent_output_revised"
                    )
                    self._merge_service.revise_output(
                        strLogMessageKey,
                        output_document,
                    )
                    continue
                self._logger.error(
                    "output parked until input inspection finishes, "
                    f"messageid={strLogMessageKey}, "
                    f"service={output_document.get('ai_service')}, "
                    f"client_host={output_document.get('client_host')}, "
                    "reason=output_parked_for_late_input"
                )
                continue

            self._logger.error(
                "no input captured, discarding output-only log, "
                f"messageid={strLogMessageKey}, "
                f"service={output_document.get('ai_service')}, "
                f"client_host={output_document.get('client_host')}, "
                "reason=input_absent"
            )

        return ERR_OK
