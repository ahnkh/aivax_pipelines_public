# load_regex.py
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime as _dt
import json
import requests

from commons.regex_detector import (
    detect,
    apply_plan,
)

class Pipeline:

    def __init__(self):
        self.type = "filter"
        self.id = "Load_regex_filter"
        self.name = "Load Regex Filter"

        class Valves(BaseModel):
            pipelines: List[str] = Field(default_factory=lambda: ["*"])
            priority: int = 2
            enabled: bool = True

            on_detect_policy: str = Field(
                "masking",
                description='처리 정책: "masking" | "block" | "allow"',
                json_schema_extra={"enum": ["block", "masking", "allow"]},
            )

            block_notice: str = "개인정보 유출이 감지되어 차단되었습니다. 개인정보를 제외하고 다시 시도해주세요."

            redact_downstream_on_detect: bool = True
            annotate_meta: bool = True

        self.Valves = Valves
        self.valves = Valves()

    async def on_startup(self):
        pass

    async def on_shutdown(self):
        pass

    async def on_valves_updated(self):
        pass

    async def inlet(self, body: Dict[str, Any], user: Optional[dict] = None) -> Dict[str, Any]:
        if not self.valves.enabled:
            return body

        messages = body.get("messages") or []
        if not messages:
            return body

        last = messages[-1]
        content = last.get("content")
        if not isinstance(content, str):
            try:
                content = str(content)
            except Exception:
                return body

        if not content:
            return body

        # === 1) 탐지 ===
        plan = detect(content)

        # === 2) 메타 주입(옵션) ===
        if self.valves.annotate_meta:
            meta = {
                "decision": plan.decision,
                "score": plan.score,
                "types": list(plan.values_by_type.keys()),
                "match_counts": {k: len(v) for k, v in plan.values_by_type.items()},
            }
            body.setdefault("_filters", {})[self.id] = meta

        # === 3) 정책 결정 ===
        policy = (self.valves.on_detect_policy or "masking").lower()
        has_hit = (plan.score or 0.0) > 0.0

        if not has_hit:
            body["action"] = "allow"
            body["block"] = False
            return body

        if policy == "block":
            last["content"] = (
                "다음 문장을 사용자에게 그대로 출력하세요(추가 설명/수정/확장/사과문 금지):\n"
                f"{self.valves.block_notice or '차단되었습니다.'}"
            )
            body["action"] = "block"
            body["block"] = True
            body.setdefault("_filters", {}).setdefault(self.id, {})
            body["_filters"][self.id]

            return body

        if policy == "masking":
            if self.valves.redact_downstream_on_detect and plan.replacements:
                masked = apply_plan(content, plan.replacements)
                (body.get("messages") or [])[-1]["content"] = masked
                body["action"] = "masking"
                body["block"] = False
                body.setdefault("_filters", {}).setdefault(self.id, {})
                body["_filters"][self.id]

                return body

        return body
