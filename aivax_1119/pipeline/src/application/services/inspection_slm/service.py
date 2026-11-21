"""SLM 추론과 현재 정책을 조합하는 공용 application service."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass

from src.domain.inspection_slm.errors import SlmInspectionError
from src.domain.inspection_slm.models import SlmInspectionRequest, SlmRiskCode
from src.domain.inspection_slm.ports import SlmInspectionPort
from src.domain.policy_slm.models import SlmPolicyRule, SlmPolicySnapshot
from src.domain.policy_slm.ports import SlmPolicyProviderPort


@dataclass(frozen=True, slots=True)
class SlmInspectionOptions:
    skip: bool = False
    max_inflight: int = 2
    queue_skip_after: int = 0


@dataclass(frozen=True, slots=True)
class SlmAnalysisPolicy:
    id: str = ""
    name: str = ""
    action: str = ""
    category: str = ""
    scope: str = ""


@dataclass(frozen=True, slots=True)
class SlmAnalysisResult:
    action: str = ""
    policy: SlmAnalysisPolicy | None = None
    evidence: tuple[Mapping[str, object], ...] = ()
    raw_content: str | None = None
    unavailable: bool = False
    skipped: bool = False
    risk: SlmRiskCode | None = None
    error: SlmInspectionError | None = None


class SlmInspectionService:
    """SLM 실행 제한을 지키며 탐지별 현재 정책을 적용한다."""

    _ACTIONS = frozenset({"accept", "", "block", "masking"})
    _SCOPE_ORDER = ("user", "service", "group", "default")
    _ACTION_PRIORITY = {"": 0, "accept": 1, "masking": 2, "block": 3}

    def __init__(
        self,
        inspection: SlmInspectionPort,
        policy_provider: SlmPolicyProviderPort,
        options: SlmInspectionOptions,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._inspection = inspection
        self._policy_provider = policy_provider
        self._options = options
        self._logger = logger or logging.getLogger("khan-python")
        self._semaphore = asyncio.Semaphore(max(1, options.max_inflight))
        self._waiting = 0
        self._waiting_lock = threading.Lock()

    async def inspect(
        self,
        prompt: str,
        *,
        email: str = "",
        ai_service_id: int | str = 0,
        policy_override: SlmPolicySnapshot | None = None,
    ) -> SlmAnalysisResult:
        if self._options.skip:
            return SlmAnalysisResult(skipped=True)
        if not await self._acquire():
            self._logger.warning("skip slm inspection because queue is full")
            return SlmAnalysisResult(skipped=True)

        try:
            try:
                inspection = await self._inspection.inspect(
                    SlmInspectionRequest(prompt=prompt),
                )
            except SlmInspectionError as error:
                self._logger.warning(
                    "slm inspection unavailable code=%s",
                    error.code,
                )
                return SlmAnalysisResult(unavailable=True, error=error)
            except Exception:
                # 입력 차단 경로는 외부 SLM 장애 때문에 실패시키지 않는다.
                self._logger.exception("slm inspection failed")
                return SlmAnalysisResult(
                    unavailable=True,
                    error=SlmInspectionError(
                        "INTERNAL_ERROR", "SLM 검사 처리 중 오류가 발생했습니다."
                    ),
                )
        finally:
            self._semaphore.release()

        detections = tuple(
            dict(item) for item in inspection.evidence if isinstance(item, Mapping)
        )
        if inspection.unavailable or inspection.error is not None:
            return SlmAnalysisResult(
                raw_content=inspection.raw_content,
                unavailable=True,
                error=inspection.error,
            )

        if not inspection.unsafe:
            return SlmAnalysisResult(
                raw_content=inspection.raw_content,
                evidence=detections,
                risk=inspection.risk,
            )

        policy_snapshot = (
            policy_override
            if policy_override is not None
            else self._policy_provider.get_policy()
        )

        if not detections:
            detections = ({"kind": "", "type": None, "value": ""},)

        applied: list[SlmPolicyRule] = []
        evidence: list[dict[str, object]] = []
        rules = self._applicable_rules(
            policy_snapshot, detections, email=email, ai_service_id=ai_service_id
        )
        for detection in detections:
            matched = False
            for rule in rules:
                if not self._matches_detection(rule, detection):
                    continue
                matched = True
                applied.append(rule)
                evidence.append(
                    dict(
                        detection,
                        policy_id=rule.id,
                        policy_name=rule.name,
                        action=rule.action,
                        category=rule.category,
                        scope=rule.scope,
                    )
                )
                if rule.action == "block":
                    break
            if applied and applied[-1].action == "block":
                break
            if not matched:
                evidence.append(dict(detection, action=""))

        selected_policy = max(
            applied,
            key=lambda item: self._ACTION_PRIORITY.get(item.action, 0),
            default=None,
        )

        action = self._action(
            selected_policy,
            inspection.force_block_without_policy,
        )

        policy = self._applied_policy(
            selected_policy,
            use_default_metadata=inspection.use_default_policy_metadata,
            result_name=inspection.policy_name,
            result_category=inspection.policy_category,
            action=action,
        )

        return SlmAnalysisResult(
            action=action,
            policy=policy,
            evidence=tuple(evidence),
            raw_content=inspection.raw_content,
            risk=inspection.risk,
        )

    @staticmethod
    def _applicable_rules(
        snapshot: SlmPolicySnapshot,
        detections: tuple[dict[str, object], ...],
        *,
        email: str,
        ai_service_id: int | str,
    ) -> tuple[SlmPolicyRule, ...]:
        group_members = {
            group_id: frozenset(emails) for group_id, emails in snapshot.group_members
        }

        for scope in SlmInspectionService._SCOPE_ORDER:
            rules: dict[str, SlmPolicyRule] = {}
            for rule in snapshot.rules:
                if rule.scope == scope and SlmInspectionService._matches_subject(
                    rule,
                    email=email,
                    ai_service_id=ai_service_id,
                    group_members=group_members,
                ):
                    rules.setdefault(rule.id, rule)
            if any(
                SlmInspectionService._matches_detection(rule, detection)
                for rule in rules.values()
                for detection in detections
            ):
                return tuple(rules.values())

        return ()

    @staticmethod
    def _matches_detection(
        rule: SlmPolicyRule,
        detection: Mapping[str, object],
    ) -> bool:
        kind = str(detection.get("kind") or "")
        if rule.detection_kind and rule.detection_kind != kind:
            return False
        if not rule.detection_kind or kind == "abuse":
            return True
        return rule.detection_type == detection.get("type")

    @staticmethod
    def _matches_subject(
        rule: SlmPolicyRule,
        *,
        email: str,
        ai_service_id: int | str,
        group_members: dict[str, frozenset[str]],
    ) -> bool:
        if rule.scope == "default":
            return True
        if rule.scope == "user":
            return bool(email.strip()) and (
                email.strip().lower() == rule.subject_id.strip().lower()
            )
        if rule.scope == "service":
            return str(ai_service_id).strip() == rule.subject_id.strip()
        if rule.scope == "group":
            return email.strip().lower() in group_members.get(
                rule.subject_id,
                frozenset(),
            )
        return False

    async def _acquire(self) -> bool:
        with self._waiting_lock:
            if (
                self._options.queue_skip_after > 0
                and self._waiting >= self._options.queue_skip_after
            ):
                return False
            self._waiting += 1
        try:
            await self._semaphore.acquire()
            return True
        finally:
            with self._waiting_lock:
                self._waiting = max(0, self._waiting - 1)

    def _action(
        self,
        policy: SlmPolicyRule | None,
        force_block_without_policy: bool,
    ) -> str:
        if policy is not None and policy.action in self._ACTIONS:
            return policy.action
        if policy is None and force_block_without_policy:
            return "block"
        return ""

    @staticmethod
    def _applied_policy(
        policy: SlmPolicyRule | None,
        *,
        use_default_metadata: bool,
        result_name: str,
        result_category: str,
        action: str,
    ) -> SlmAnalysisPolicy | None:
        if policy is not None and use_default_metadata:
            return SlmAnalysisPolicy(
                id=policy.id,
                name=policy.name,
                action=action,
                category=policy.category,
                scope=policy.scope,
            )
        if result_name or result_category:
            return SlmAnalysisPolicy(
                name=result_name,
                action=action,
                category=result_category,
            )
        return None
