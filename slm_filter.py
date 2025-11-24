"""
title: PII Masking Inlet Filter
author: wins-tech
version: 1.0.1
license: MIT
description: Masks user input via a local FastAPI service before sending to the model.
requirements: requests
"""

import os
import requests
from typing import Optional, Dict, Any, List

# --- pydantic v1/v2 호환 레이어 ---
from pydantic import BaseModel

def _has_model_dump() -> bool:
    return hasattr(BaseModel, "model_dump")

class Valves(BaseModel):
    PII_API_URL: str = os.getenv("PII_API_URL", "http://host.docker.internal:9292/mask")
    TIMEOUT_SECONDS: int = int(os.getenv("PII_TIMEOUT", "10"))
    ENABLE_LOG: bool = False
    FALLBACK_ON_ERROR: bool = True

    # pydantic v1 호환: BaseModel.dict()를 model_dump 이름으로 노출
    if not _has_model_dump():
        def model_dump(self, *args, **kwargs):  # type: ignore
            return self.dict(*args, **kwargs)

class Pipeline:
    """
    Filter-style pipeline:
      - inlet():  user → (mask) → model
      - outlet(): model → (pass-through) → user
    """

    def __init__(self):
        self.id = "pii_mask_inlet_filter"
        self.name = "SLM Filter"
        # ★ dict가 아니라 Pydantic 모델이어야 합니다
        self.valves = Valves()

    async def inlet(self, body: Dict[str, Any], user: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        api_url = self.valves.PII_API_URL
        timeout = self.valves.TIMEOUT_SECONDS
        log_on = self.valves.ENABLE_LOG
        fallback = self.valves.FALLBACK_ON_ERROR

        try:
            messages: List[Dict[str, Any]] = body.get("messages", [])
            if not messages:
                return body

            # 마지막 user 메시지 찾아 마스킹
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    original = messages[i].get("content", "")
                    resp = requests.post(api_url, json={"text": original}, timeout=timeout)
                    resp.raise_for_status()
                    data = resp.json()
                    masked = data.get("masked_text") or data.get("text") or data.get("result") or original
                    messages[i]["content"] = masked
                    if log_on:
                        print(f"[PII-MASK] original={repr(original)[:200]} -> masked={repr(masked)[:200]}")
                    break

            body["messages"] = messages
            return body

        except Exception as e:
            if fallback:
                print(f"[PII-MASK][WARN] masking failed: {e}")
                return body
            raise

    async def outlet(self, body: Dict[str, Any], user: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return body

