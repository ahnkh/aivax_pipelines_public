"""분석 로그 병합의 기록·반환 계약. 레거시 호출부와 값은 동일하다."""

from src.core.constants import ERR_FAIL, ERR_OK


class AnalysisLogContract:
    TYPE_LOG_INPUT = 0
    TYPE_LOG_OUTPUT = 1
    FIELD_SCHEMA_VERSION = "schema_version"
    SCHEMA_VERSION = 1
    FIELD_MERGE_STATE = "merge_state"
    MERGE_STATE_INPUT_ONLY = "input_only"
    MERGE_STATE_OUTPUT_ONLY = "output_only"
