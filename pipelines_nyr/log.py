from typing import Optional, Dict, Any, List, Union
from pydantic import BaseModel, Field
import json
import logging

# ----------------------------------------------------
# 로깅 설정
# ----------------------------------------------------
logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


# ----------------------------------------------------
# 유틸
# ----------------------------------------------------
def _json_dumps_safe(obj: Any, pretty: bool = False) -> str:
    """dict, list 등 무엇이 들어와도 최대한 안전하게 JSON 문자열로 변환"""
    try:
        return json.dumps(
            obj,
            indent=2 if pretty else None,
            ensure_ascii=False,
            default=str,
        )
    except Exception as e:
        try:
            # 직렬화 실패 시 문자열화
            return json.dumps(str(obj), ensure_ascii=False)
        except Exception:
            return f"<unserializable: {e!r}>"


def _truncate_text(s: str, max_len: Optional[int]) -> str:
    """긴 JSON 로그가 부담될 경우 잘라서 찍기"""
    if not isinstance(s, str):
        return s
    if max_len is None or max_len <= 0 or len(s) <= max_len:
        return s
    return s[:max_len] + f"... (truncated, total={len(s)})"


# ----------------------------------------------------
# 파이프라인 (원본 inlet 로그 전용)
# ----------------------------------------------------
class Pipeline:
    def __init__(self):
        self.type = "filter"
        self.id = "inlet_raw_logger"
        self.name = "Inlet Raw Logger"

        class Valves(BaseModel):
            # 어떤 파이프라인에서 동작할지 선택(관성 유지용)
            pipelines: List[str] = Field(default_factory=lambda: ["*"])
            priority: int = 0
            enabled: bool = True

            # inlet 원본 로그 설정
            inlet_log_enabled: bool = True
            inlet_log_pretty: bool = True
            inlet_log_truncate: Optional[int] = None  # 예: 20000. None이면 무제한

            # 로그 레벨 선택 (INFO/DEBUG/WARNING 등)
            inlet_log_level: str = "INFO"

            # user 정보도 함께 찍을지 여부
            include_user_in_log: bool = True

        self.Valves = Valves
        self.valves = Valves()

    # 프레임워크 호환을 위한 생명주기 훅(비움)
    async def on_startup(self):
        pass

    async def on_shutdown(self):
        pass

    async def on_valves_updated(self):
        pass

    async def outlet(self, body: Dict[str, Any], user: Optional[dict] = None) -> Dict[str, Any]:
        """
        들어온 원본 body를 마스킹 없이 그대로 로그로 출력하고, body를 변형하지 않고 통과시킵니다.
        """
        if not self.valves.enabled:
            return body

        if self.valves.inlet_log_enabled:
            payload: Dict[str, Any] = {
                "filter": self.id,
                "name": self.name,
                "event": "inlet_received",
                "body": body,
            }
            if self.valves.include_user_in_log:
                payload["user"] = user

            txt = _json_dumps_safe(payload, pretty=self.valves.inlet_log_pretty)
            txt = _truncate_text(txt, self.valves.inlet_log_truncate)

            level = (self.valves.inlet_log_level or "INFO").upper()
            if level == "DEBUG":
                logger.debug(txt)
            elif level == "WARNING":
                logger.warning(txt)
            elif level == "ERROR":
                logger.error(txt)
            elif level == "CRITICAL":
                logger.critical(txt)
            else:
                logger.info(txt)

        # 아무런 변형 없이 그대로 통과
        return body
