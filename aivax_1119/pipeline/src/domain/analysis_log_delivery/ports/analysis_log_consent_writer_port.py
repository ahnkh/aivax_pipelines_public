"""사용자 동의 로그 갱신과 갱신에 필요한 상태의 만료 계약."""

from abc import ABC, abstractmethod
from collections.abc import Mapping

CONSENT_STATE_TTL_SECONDS = 30 * 60


class AnalysisLogConsentWriterPort(ABC):
    @abstractmethod
    def read_block_log(self, message_id: str) -> dict[str, object]:
        """기존 차단 로그와 누적 분석 갱신을 읽는다."""
        raise NotImplementedError

    @abstractmethod
    def write_analysis_update(
        self, message_id: str, fields: Mapping[str, object]
    ) -> None:
        """누적 분석 결과를 기존 차단 문서에 갱신한다."""
        raise NotImplementedError

    @abstractmethod
    def require_block_log(self, message_id: str) -> None:
        """동의 재전송 대상인 기존 BLOCK 로그가 없으면 예외를 발생시킨다."""
        raise NotImplementedError

    @abstractmethod
    def write_consent_update(
        self, message_id: str, output_text: str | None, text_truncated_bytes: int
    ) -> None:
        """기존 차단 로그 갱신을 전달한다. 신규 분석 로그를 생성하지 않는다."""
        raise NotImplementedError

    @abstractmethod
    def expire_consent_states(self) -> int:
        """최초 저장 후 30분이 지난 동의 상태를 삭제하고 건수를 반환한다."""
        raise NotImplementedError
