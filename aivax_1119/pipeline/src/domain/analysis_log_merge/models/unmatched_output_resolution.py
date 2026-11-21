"""OUTPUT 만료 시 저장소가 원자적으로 고른 후속 처리."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.domain.analysis_log_merge.models.analysis_log_payload import AnalysisLogPayload

UnmatchedOutputAction = Literal["merge", "revise", "park"]


@dataclass(frozen=True, slots=True)
class UnmatchedOutputResolution:
    """같은 message_id의 INPUT 상태에 따라 OUTPUT을 어디에 둘지 정한다.

    - ``merge``: 아직 저장소에 있는 INPUT과 바로 합친다.
    - ``revise``: INPUT은 이미 프롬프트 단독 문서로 적재됐다. 본문만 갱신한다.
    - ``park``: INPUT 검사가 아직 끝나지 않았다. 검사가 끝나면 회수한다.
    """

    action: UnmatchedOutputAction
    input_log: AnalysisLogPayload | None = None
