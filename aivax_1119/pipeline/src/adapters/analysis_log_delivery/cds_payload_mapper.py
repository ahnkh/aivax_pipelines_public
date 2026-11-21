"""완성 분석 로그와 nNetCDS JSON 계약의 변환기."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from src.core.time import TimeNormalizer
from src.domain.analysis_log_delivery.models import MergedAnalysisLogDocument


class CdsAnalysisLogPayloadMapper:
    """목적지 독립 분석 로그를 nNetCDS payload로 변환한다."""

    _ACTION_LABELS = {
        "block": "차단",
        "masking": "마스킹",
        "allow": "허용",
        "accept": "허용",
        "": "허용",
    }

    def __init__(self, *, src_port: int, dst_host: str, dst_port: int) -> None:
        self._src_port = src_port
        self._dst_host = dst_host
        self._dst_port = dst_port

    def toPersistence(
        self,
        entity: MergedAnalysisLogDocument,
    ) -> dict[str, Any]:
        document = entity.to_dict()
        # 배포 전에 쌓인 spool 파일은 이미 CDS payload 형식이다.
        if document.get("protocol") == "HTTPS" and document.get("detect") == 4:
            self._validate_prepared_payload(document)
            return document
        user = document.get("user")
        user = user if isinstance(user, Mapping) else {}
        action = str(document.get("mode") or "").strip().lower()
        if action not in self._ACTION_LABELS:
            raise ValueError(f"지원하지 않는 CDS 처리 결과: {action}")
        src_ip = str(document.get("client_host") or "").strip()
        src_account = str(user.get("email") or "").strip()
        if not src_ip and not src_account:
            raise ValueError("CDS 로그에는 출발지 IP 또는 계정이 필요합니다.")
        if len(src_ip) > 64 or len(src_account) > 100:
            raise ValueError("CDS 출발지 정보의 최대 길이를 초과했습니다.")
        return {
            "occurredAt": self._occurred_at(document),
            "protocol": "HTTPS",
            "msgType": 0,
            "detect": 4,
            "inspectResult": self._inspect_result(document, action),
            "srcIp": src_ip,
            "srcAccount": src_account,
            "srcPort": self._src_port,
            "dst": self._dst_host,
            "dstPort": self._dst_port,
            "result": "B" if action == "block" else "S",
        }

    @staticmethod
    def _occurred_at(document: Mapping[str, Any]) -> str:
        raw_value = str(
            document.get("@timestamp") or document.get("event_time") or "",
        ).strip()
        if not raw_value:
            value = TimeNormalizer.utc_now()
        else:
            value = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
            if value.tzinfo is None:
                raise ValueError("CDS 탐지 시각에는 timezone이 필요합니다.")
        return value.astimezone().strftime("%Y-%m-%dT%H:%M:%S")

    def _inspect_result(
        self,
        document: Mapping[str, Any],
        action: str,
    ) -> str:
        policy = document.get("policy")
        policy = policy if isinstance(policy, Mapping) else {}
        name = self._one_line(policy.get("policy_name"))
        if not name:
            name = self._one_line(policy.get("category"))
        summary = name or "탐지 정보 없음"
        prefix = "탐지: "
        suffix = f" 처리: {self._ACTION_LABELS[action]}"
        available = 100 - len(prefix) - len(suffix)
        if len(summary) > available:
            summary = summary[: available - 1] + "…"
        return prefix + summary + suffix

    @staticmethod
    def _one_line(value: object) -> str:
        return " ".join(str(value or "").split())

    @staticmethod
    def _validate_prepared_payload(payload: Mapping[str, Any]) -> None:
        if payload.get("msgType") != 0 or payload.get("detect") != 4:
            raise ValueError("기존 CDS spool payload가 올바르지 않습니다.")
        if payload.get("result") not in {"B", "S"}:
            raise ValueError("기존 CDS spool 처리 결과가 올바르지 않습니다.")
