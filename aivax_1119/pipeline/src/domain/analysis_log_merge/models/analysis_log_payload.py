"""분석 로그 병합 저장소가 다루는 원본 로그 본문 타입."""

from typing import Any, Mapping, TypeAlias

# 로그 스키마는 pipeline 종류에 따라 넓게 변한다. 이 계층에서는 필드를
# 해석하지 않고, 병합 가능한 key/value 문서라는 계약만 가진다.
AnalysisLogPayload: TypeAlias = Mapping[str, Any]
