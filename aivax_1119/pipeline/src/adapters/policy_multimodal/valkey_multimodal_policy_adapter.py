from __future__ import annotations

import base64
import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from valkey import Valkey

from src.core.type_converter import TypeConverter
from src.domain.policy_multimodal.models import (
    MultimodalPolicyRefreshResult,
    MultimodalPolicyRule,
    MultimodalPolicySnapshot,
)
from src.domain.policy_multimodal.ports import MultimodalPolicyPort


class ValkeyMultimodalPolicyAdapter(MultimodalPolicyPort):
    """Multimodal rule, group membership, file 설정 snapshot을 읽는다.

    Valkey에서 정상 load한 payload는 메모리 cache와 로컬 JSON에 저장한다.
    Valkey를 사용할 수 없으면 JSON, 기존 메모리 cache, 빈 정책 순서로 복구한다.
    """

    _SUPPORTED_SCOPES = {"user", "service", "group", "default"}

    def __init__(
        self,
        client: Valkey,
        key: str,
        *,
        default_allowed_extensions: Iterable[str] = (),
        default_max_size_bytes: int = 0,
        backup_path: str | Path | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if not key.strip():
            raise ValueError("multimodal policy key는 비어 있을 수 없습니다.")
        self._client = client
        self._key = key
        self._backup_path = Path(backup_path) if backup_path is not None else None
        self._logger = logger or logging.getLogger("khan-python")
        self._lock = threading.RLock()
        self._default_allowed_extensions = self._extensions(default_allowed_extensions)
        self._default_max_size_bytes = max(0, int(default_max_size_bytes))
        self._policy = MultimodalPolicySnapshot(
            allowed_extensions=self._default_allowed_extensions,
            max_size_bytes=self._default_max_size_bytes,
        )

    def refresh(self) -> MultimodalPolicyRefreshResult:
        with self._lock:
            try:
                payload = self._valkey_payload()
            except Exception as valkey_error:
                return self._refresh_from_backup(valkey_error)

            if payload is None:
                return self._refresh_when_key_is_missing()

            try:
                snapshot, result = self._snapshot(payload)
            except Exception as valkey_error:
                return self._refresh_from_backup(valkey_error)

            self._save_backup(payload)
            self._policy = snapshot
            return result

    def get_policy(self) -> MultimodalPolicySnapshot:
        with self._lock:
            return self._policy

    def _valkey_payload(self) -> Mapping[str, Any] | None:
        raw = self._client.get(self._key)
        if raw is None:
            return None
        return self._decode_payload(raw, source=self._key)

    @staticmethod
    def _decode_payload(raw: object, *, source: str) -> Mapping[str, Any]:
        value = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        payload = json.loads(value)
        if not isinstance(payload, Mapping):
            raise ValueError(f"{source}: JSON object가 필요합니다.")
        return payload

    def _snapshot(
        self,
        payload: Mapping[str, Any],
    ) -> tuple[MultimodalPolicySnapshot, MultimodalPolicyRefreshResult]:
        rows = payload.get("rules")
        settings = payload.get("settings")
        raw_group_members = payload.get("group_members")
        if (
            not isinstance(rows, list)
            or not isinstance(settings, list)
            or not isinstance(raw_group_members, Mapping)
        ):
            raise ValueError(
                f"{self._key}: rules/settings 배열과 "
                "group_members object가 필요합니다.",
            )
        rules = tuple(
            rule
            for row in rows
            if isinstance(row, Mapping)
            if (rule := self._to_rule(row)) is not None
        )
        allowed_extensions, max_size_bytes = self._file_settings(settings)
        snapshot = MultimodalPolicySnapshot(
            rules=rules,
            group_members=self._group_members(raw_group_members),
            allowed_extensions=allowed_extensions,
            max_size_bytes=max_size_bytes,
        )
        return snapshot, MultimodalPolicyRefreshResult(
            active_rule_count=len(rules),
        )

    @staticmethod
    def _empty_payload() -> Mapping[str, Any]:
        return {"rules": [], "group_members": {}, "settings": []}

    def _backup_payload(self) -> Mapping[str, Any] | None:
        if self._backup_path is None or not self._backup_path.exists():
            return None
        return self._decode_payload(
            self._backup_path.read_bytes(),
            source=str(self._backup_path),
        )

    def _refresh_from_backup(
        self,
        valkey_error: Exception,
    ) -> MultimodalPolicyRefreshResult:
        try:
            payload = self._backup_payload()
            if payload is not None:
                snapshot, result = self._snapshot(payload)
                self._logger.warning(
                    "multimodal policy Valkey load failed; using local backup "
                    "(%s, error=%s)",
                    self._backup_path,
                    valkey_error,
                )
                self._policy = snapshot
                return result
        except Exception:
            self._logger.exception(
                "multimodal policy local backup is invalid; using current cache",
            )
        self._logger.warning(
            "multimodal policy Valkey load failed and local backup is missing; "
            "using current cache (error=%s)",
            valkey_error,
        )
        return self._current_result()

    def _refresh_when_key_is_missing(self) -> MultimodalPolicyRefreshResult:
        try:
            payload = self._backup_payload()
            if payload is not None:
                snapshot, result = self._snapshot(payload)
                self._logger.warning(
                    "multimodal policy key is missing; using local backup (%s)",
                    self._backup_path,
                )
                self._policy = snapshot
                return result
        except Exception:
            self._logger.exception(
                "multimodal policy local backup is invalid; using current cache",
            )
        return self._current_result()

    def _current_result(self) -> MultimodalPolicyRefreshResult:
        return MultimodalPolicyRefreshResult(
            active_rule_count=len(self._policy.rules),
        )

    def _save_backup(self, payload: Mapping[str, Any]) -> None:
        if self._backup_path is None:
            return
        path = self._backup_path
        temporary_path: Path | None = None
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as file_object:
                temporary_path = Path(file_object.name)
                json.dump(payload, file_object, ensure_ascii=False)
                file_object.flush()
                os.fsync(file_object.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, path)
        except OSError:
            self._logger.exception(
                "multimodal policy local backup save failed (%s)",
                path,
            )
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _group_members(
        self,
        raw_groups: Mapping[object, object],
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        groups: list[tuple[str, tuple[str, ...]]] = []
        for raw_group_id, raw_emails in raw_groups.items():
            group_id = str(raw_group_id).strip()
            if (
                not group_id
                or not isinstance(raw_emails, list)
                or not all(isinstance(email, str) for email in raw_emails)
            ):
                raise ValueError(
                    f"{self._key}: group_members email 배열이 필요합니다.",
                )
            emails = tuple(
                dict.fromkeys(
                    email.strip().lower() for email in raw_emails if email.strip()
                )
            )
            groups.append((group_id, emails))
        return tuple(groups)

    def _file_settings(self, rows: list[object]) -> tuple[tuple[str, ...], int]:
        allowed = self._default_allowed_extensions
        max_size = self._default_max_size_bytes
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            key = row.get("skey")
            value = row.get("svalue")
            if key == "fileControlAllowedExtensions" and isinstance(value, str):
                allowed = self._extensions(value.split(",")) or allowed
            elif key == "fileControlMaxSize":
                parsed = TypeConverter.to_int(value)
                if parsed > 0:
                    max_size = parsed
        return allowed, max_size

    def _to_rule(self, row: Mapping[str, Any]) -> MultimodalPolicyRule | None:
        scope = str(row.get("scope") or "").strip()
        expression = self._decode_expression(row.get("rule"))
        flags = TypeConverter.to_int(row.get("regex_flag"))
        if scope not in self._SUPPORTED_SCOPES or expression is None:
            return None
        try:
            re.compile(expression, flags)
        except (re.error, TypeError, ValueError):
            return None
        action = str(row.get("action") or "").strip().lower()
        return MultimodalPolicyRule(
            id=str(row.get("id") or row.get("policyId") or ""),
            name=str(row.get("name") or ""),
            expression=expression,
            action="block" if action == "masking" else action,
            scope=scope,
            target=self._text(row.get("targets")),
            category=self._text(row.get("category")),
            flags=flags,
            subject_id=str(row.get("subject_id") or "").strip(),
        )

    @staticmethod
    def _decode_expression(value: object) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            return (
                base64.b64decode(value, validate=True).decode("utf-8").strip() or None
            )
        except (ValueError, UnicodeDecodeError):
            return None

    @staticmethod
    def _extensions(values: Iterable[str]) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                value.strip().lower().removeprefix(".")
                for value in values
                if value.strip().removeprefix(".")
            )
        )

    @staticmethod
    def _text(value: object) -> str:
        if isinstance(value, (list, tuple, set)):
            return ",".join(str(item) for item in value)
        return str(value or "")
