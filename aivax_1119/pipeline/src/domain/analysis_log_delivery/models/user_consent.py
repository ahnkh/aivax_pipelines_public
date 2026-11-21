"""기존 엔진의 사용자 동의 문자열 계약."""

# 병합 대기 중 원본 인덱스 날짜를 보존하며 OpenSearch 본문에는 기록하지 않는다.
CONSENT_LOG_TIMESTAMP_FIELD = "_consent_log_timestamp"


def is_user_consent_on(value: object) -> bool:
    return str(value or "").strip().lower() in {
        "on",
        "1",
        "true",
        "yes",
        "enable",
        "enabled",
    }
