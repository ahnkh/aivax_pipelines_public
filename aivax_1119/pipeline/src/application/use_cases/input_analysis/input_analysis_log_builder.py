"""입력 분석 결과를 기존 분석 로그 문서로 변환한다."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from src.core.network import IpAddressConverter
from src.core.time import TimeNormalizer
from src.domain.config_ai_service.ports import AiServiceCatalogPort
from src.domain.inspection_input.models import (
    InputAnalysisRequest,
    InputAnalysisResult,
    InputInspectionAction,
    InputInspectionResult,
    InputInspectorType,
    InputPolicyReference,
)
from src.domain.inspection_input.models.cso_grade_result import summarize_cso_grades
from src.domain.inspection_input.models.input_analysis_result import (
    InputInspectionError,
)


class InputAnalysisLogBuilder:
    """입력 분석 결과를 병합 가능한 로그 문서로 변환한다."""

    _LOG_FILTER_NAMES = {
        InputInspectorType.REGEX: "regex",
        InputInspectorType.MULTIMODAL: "file block",
        InputInspectorType.SLM: "slm",
        InputInspectorType.SECUXPER: "secuxper",
    }

    def __init__(self, ai_service_catalog: AiServiceCatalogPort) -> None:
        self._ai_service_catalog = ai_service_catalog

    def build(
        self,
        request: InputAnalysisRequest,
        result: InputAnalysisResult,
        *,
        include_block_messages: bool = False,
        file_summaries: list[dict[str, object]] | None = None,
        bypassed_filters: tuple[InputInspectorType, ...] = (),
    ) -> dict[str, Any]:
        document = self._to_document(request, result)
        bypassed_names = {self._LOG_FILTER_NAMES[kind] for kind in bypassed_filters}
        for detection in document["filter_detect"]:
            if detection["mode"] == "block" and detection["filter"] in bypassed_names:
                detection["block_bypassed"] = True
        if file_summaries is not None:
            file_detection = next(
                (
                    item
                    for item in document["filter_detect"]
                    if item["filter"] == "file block"
                ),
                None,
            )
            if file_detection is None:
                # 파일 보관용 항목에는 필터 실행 시각이나 탐지 정책을 넣지 않는다.
                file_detection = self._to_filter_detect(
                    InputInspectionResult(inspector_type=InputInspectorType.MULTIMODAL)
                )
                document["filter_detect"].append(file_detection)
            file_detection["file_summary"] = file_summaries
        if include_block_messages:
            messages = {
                self._LOG_FILTER_NAMES[item.inspector_type]: item.block_message
                for item in result.executions
                if item.block_message
            }
            for detection in document["filter_detect"]:
                if detection["filter"] in messages:
                    detection["block_message"] = messages[detection["filter"]]
        return document

    def restore_executions(
        self, document: dict[str, Any]
    ) -> tuple[InputInspectionResult, ...]:
        """기존 로그의 필터 결과와 미실행 기본 항목을 복원한다."""
        kinds = {name: kind for kind, name in self._LOG_FILTER_NAMES.items()}
        executions = {}
        fields = {
            "filter",
            "mode",
            "masked_contents",
            "evidence",
            "error",
            "policy_id",
            "policy_name",
            "target",
            "category",
            "scope",
            "started_at",
            "completed_at",
            "duration_ms",
            "block_bypassed",
            "block_message",
        }
        for item in document["filter_detect"]:
            kind = kinds[item["filter"]]
            if kind in executions:
                continue
            error = item.get("error")
            action = (
                InputInspectionAction.FAILED
                if error
                else InputInspectionAction(item["mode"])
            )
            executions[kind] = InputInspectionResult(
                inspector_type=kind,
                action=action,
                masked_prompt=item.get("masked_contents") or "",
                block_message=item.get("block_message")
                or (
                    document.get("output_text") or ""
                    if action is InputInspectionAction.BLOCK
                    else ""
                ),
                policy=InputPolicyReference(
                    id=item.get("policy_id") or "",
                    name=item.get("policy_name") or "",
                    target=item.get("target") or "",
                    category=item.get("category") or "",
                    scope=item.get("scope") or "",
                )
                if any(
                    item.get(field)
                    for field in (
                        "policy_id",
                        "policy_name",
                        "target",
                        "category",
                        "scope",
                    )
                )
                else None,
                evidence=tuple(item.get("evidence") or ()),
                details={
                    key: value for key, value in item.items() if key not in fields
                },
                error=InputInspectionError(**error) if error else None,
                started_at=datetime.fromisoformat(item["started_at"])
                if item.get("started_at")
                else None,
                completed_at=datetime.fromisoformat(item["completed_at"])
                if item.get("completed_at")
                else None,
                duration_ms=item.get("duration_ms"),
            )
        return tuple(executions.values())

    def _to_document(
        self,
        request: InputAnalysisRequest,
        result: InputAnalysisResult,
    ) -> dict[str, Any]:
        now = TimeNormalizer.utc_now()
        timestamp = self._to_iso_z(now)
        event_time = request.event_time.strip() or timestamp
        request_date = self._normalize_request_date(request.event_time) or now.strftime(
            "%Y-%m-%dT%H:%M:%S",
        )
        policy = result.final_decision.policy
        document: dict[str, Any] = {
            "@timestamp": timestamp,
            "message_id": request.message_id,
            "user": {
                "id": request.user.id,
                "name": request.user.name,
                "role": None,
                "email": request.user.email,
                "uuid": request.user.uuid,
            },
            "client_host": request.user.client_host,
            "client_host_cidr": self._client_host_cidr(request.user.client_host),
            # 표시명과 Management 식별용 원본 ID를 섞지 않는다.
            "ai_service_id": request.user.ai_service,
            "ai_service": self._ai_service_catalog.name_for(request.user.ai_service),
            "prompt": request.prompt,
            "model": (request.model or "").strip() or None,
            "request_date": request_date,
            "event_time": event_time,
            "filter_detect": [
                document
                for execution in result.executions
                for document in self._filter_documents(execution)
            ],
            "mode": self._to_log_mode(result.final_decision.action.value),
            "masked_contents": result.final_decision.masked_prompt or None,
            "policy": {
                "policy_id": policy.id if policy else "",
                "policy_name": policy.name if policy else "",
                "category": policy.category if policy else "",
                "scope": policy.scope if policy else "",
            },
        }
        cso_status = summarize_cso_grades(result.cso_grades)
        if cso_status is not None:
            document["cso"] = {
                "status": cso_status.value,
                "files": [
                    {
                        "file_name": attachment.name,
                        "file_path": attachment.id,
                        "grade": grade.grade,
                        "status": grade.status.value,
                    }
                    for attachment, grade in zip(
                        request.attachments,
                        result.cso_grades,
                        strict=True,
                    )
                ],
            }
        return document

    def _filter_documents(
        self, execution: InputInspectionResult
    ) -> list[dict[str, Any]]:
        documents = [self._to_filter_detect(execution)]
        if execution.inspector_type is InputInspectorType.SECUXPER:
            for failure in execution.details.get("failed_policies", []):
                if (
                    execution.action.value == "failed"
                    and failure.get("policy_id") == documents[0]["policy_id"]
                ):
                    continue
                documents.append(
                    {
                        "filter": "secuxper",
                        **failure,
                        "mode": self._to_log_mode(failure.get("mode", "")),
                        "masked_contents": "",
                        "evidence": [],
                        **{
                            key: value
                            for key, value in documents[0].items()
                            if key in ("started_at", "completed_at", "duration_ms")
                        },
                    }
                )
        return documents

    def _to_filter_detect(self, execution: InputInspectionResult) -> dict[str, Any]:
        policy = execution.policy
        document: dict[str, Any] = {
            "filter": self._LOG_FILTER_NAMES[execution.inspector_type],
            "mode": execution.action.value,
            "policy_id": policy.id if policy else "",
            "policy_name": policy.name if policy else "",
            "target": policy.target if policy else "",
            "category": policy.category if policy else "",
            "scope": policy.scope if policy else "",
            "masked_contents": execution.masked_prompt,
            "evidence": self._to_evidence(execution),
        }
        if execution.started_at is not None:
            document["started_at"] = self._to_iso_z(execution.started_at)
        if execution.completed_at is not None:
            document["completed_at"] = self._to_iso_z(execution.completed_at)
        if execution.duration_ms is not None:
            document["duration_ms"] = execution.duration_ms
        document.update(execution.details)
        document["mode"] = self._to_log_mode(document["mode"])
        if execution.action.value == "failed":
            if execution.inspector_type is InputInspectorType.SLM:
                document["slm_content"] = ""
            elif execution.inspector_type is InputInspectorType.MULTIMODAL:
                document["file_summary"] = []
        if execution.error is not None:
            document["error"] = execution.error.to_document()
        return document

    @staticmethod
    def _to_log_mode(mode: str) -> str:
        # 실패 상태는 내부 판정과 error에 유지하고 로그 mode는 수집 값으로 쓴다.
        return "" if mode in {"fail", "failed"} else mode

    @staticmethod
    def _to_evidence(execution: InputInspectionResult) -> list[dict[str, object]]:
        """로그 evidence에도 해당 정책의 식별자를 유지한다.

        Regex/SLM inspector는 항목별 정책을 ``policy_id``/``policy_name``으로
        전달한다. 기존 로그 계약은 evidence를 우선 조회하면서 ``id``/``name``을
        사용하므로, 두 표현을 함께 기록한다. 항목별 값이 없을 때만 실행의 대표
        정책을 사용해 서로 다른 정책이 동시에 탐지된 경우도 보존한다.
        """
        policy = execution.policy
        evidence: list[dict[str, object]] = []
        for item in execution.evidence:
            entry = dict(item)
            policy_id = str(entry.get("policy_id") or (policy.id if policy else ""))
            policy_name = str(
                entry.get("policy_name") or (policy.name if policy else ""),
            )
            entry.setdefault("id", policy_id)
            entry.setdefault("name", policy_name)
            evidence.append(entry)
        return evidence

    @staticmethod
    def _client_host_cidr(client_host: str) -> int:
        return IpAddressConverter.to_integer(client_host) or 0

    @staticmethod
    def _normalize_request_date(value: str) -> str:
        normalized = value.strip()
        if len(normalized) > 10 and normalized[10] == " ":
            return f"{normalized[:10]}T{normalized[11:]}"
        return normalized

    @staticmethod
    def _to_iso_z(value: datetime) -> str:
        return TimeNormalizer.to_utc_iso(value)
