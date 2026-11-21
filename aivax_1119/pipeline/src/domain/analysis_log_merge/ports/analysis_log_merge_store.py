"""입력·출력 분석 로그의 원자적 병합 상태 저장소 계약."""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.domain.analysis_log_merge.models.analysis_log_message_key import (
    AnalysisLogMessageKey,
)
from src.domain.analysis_log_merge.models.analysis_log_payload import AnalysisLogPayload
from src.domain.analysis_log_merge.models.expired_analysis_log import ExpiredAnalysisLog
from src.domain.analysis_log_merge.models.unmatched_output_resolution import (
    UnmatchedOutputResolution,
)


class AnalysisLogMergeStorePort(ABC):
    """병합 대기 로그를 저장하고 상대 로그를 원자적으로 확보한다.

    구현체는 아래 메서드 하나가 실행되는 동안 동일 ``message_key``의
    INPUT/OUTPUT 상태 전이가 끼어들지 않도록 보장해야 한다. Valkey는 Lua
    script로, memory 구현체는 단일 lock으로 이 계약을 만족시킨다.

    저장소 연결·재시도·fallback은 adapter 또는 조립부 책임이며, port의
    set/pop 복합 연산은 구현체 내부에서 원자적으로 처리한다.
    """

    @abstractmethod
    def set_input_and_pop_output(
        self,
        message_key: AnalysisLogMessageKey,
        input_log: AnalysisLogPayload,
    ) -> AnalysisLogPayload | None:
        """INPUT을 저장하고 이미 대기 중인 OUTPUT이 있으면 제거해 반환한다."""
        raise NotImplementedError

    @abstractmethod
    def pop_input_or_set_output(
        self,
        message_key: AnalysisLogMessageKey,
        output_log: AnalysisLogPayload,
    ) -> AnalysisLogPayload | None:
        """INPUT을 제거해 반환하거나, 없으면 OUTPUT을 대기 상태로 저장한다."""
        raise NotImplementedError

    @abstractmethod
    def pop_input(
        self,
        message_key: AnalysisLogMessageKey,
    ) -> AnalysisLogPayload | None:
        """대기 중인 INPUT만 제거해 반환한다. OUTPUT은 건드리지 않는다."""
        raise NotImplementedError

    @abstractmethod
    def pop_expired_inputs(self, limit: int = 200) -> list[ExpiredAnalysisLog]:
        """상대 OUTPUT 없이 기한이 지난 INPUT을 다른 worker와 중복 없이 회수한다."""
        raise NotImplementedError

    @abstractmethod
    def pop_expired_outputs(self, limit: int = 200) -> list[ExpiredAnalysisLog]:
        """상대 INPUT 없이 기한이 지난 OUTPUT을 다른 worker와 중복 없이 회수한다."""
        raise NotImplementedError

    @abstractmethod
    def park_late_output(
        self,
        message_key: AnalysisLogMessageKey,
        output_log: AnalysisLogPayload,
    ) -> None:
        """만료된 OUTPUT을 늦은 입력 TTL 동안 보관한다. 만료 색인에는 넣지 않는다."""
        raise NotImplementedError

    @abstractmethod
    def resolve_unmatched_output(
        self,
        message_key: AnalysisLogMessageKey,
        output_log: AnalysisLogPayload,
    ) -> UnmatchedOutputResolution:
        """만료된 OUTPUT을 대기 INPUT·적재 표시·보관 중 하나로 원자적으로 넘긴다.

        대기 INPUT이 있으면 그것을 꺼내 ``merge``를 반환한다. INPUT이 이미
        프롬프트 단독 문서로 빠져나갔으면 ``revise``를 반환하고 보관하지 않는다.
        둘 다 아니면 늦은 입력 TTL 동안 보관하고 ``park``를 반환한다.
        """
        raise NotImplementedError
