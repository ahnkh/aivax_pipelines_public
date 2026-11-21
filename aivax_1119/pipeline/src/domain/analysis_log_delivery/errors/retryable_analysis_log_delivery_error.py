"""재시도 가능한 분석 로그 전달 실패."""


class RetryableAnalysisLogDeliveryError(RuntimeError):
    """외부 시스템의 일시적 실패로 같은 요청을 재시도할 수 있다."""
