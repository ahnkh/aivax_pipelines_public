
'''
pipeline, 기존 filter에 선언된 함수들, 공통화

중복된 코드가 많아서 별도 함수로 분리
'''

from datetime import datetime, timezone

from lib_include import *

# ----------------------------------------------------
# 유틸
# ----------------------------------------------------
def ts_isoz() -> str:
    # UTC ISO 8601 + Z
    return datetime.datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def safe_get(d: Dict[str, Any], *keys, default=None):
    cur = d
    try:
        for k in keys:
            if not isinstance(cur, dict):
                return default
            cur = cur.get(k)
        return cur if cur is not None else default
    except Exception:
        return default