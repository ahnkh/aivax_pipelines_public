from __future__ import annotations

import base64
import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from valkey import Valkey

from src.domain.policy_regex.models import (
    RegexPolicyRefreshResult,
    RegexPolicyRule,
    RegexPolicySnapshot,
)
from src.domain.policy_regex.ports import RegexPolicyPort


class ValkeyRegexPolicyAdapter(RegexPolicyPort):
    """
    Regex rule과 group membership을 담은 Valkey snapshot을 읽는다.

    - valkey에서 load한 정책을 로컬 캐시에 두어 재사용한다.
    - valkey에서 값을 가져오지 못할땐 백업 json을 사용해 정책을 load한다.
    - json도 없다면 빈 정책을 가진다.
    """

    _SUPPORTED_SCOPES = {"user", "service", "group", "default"}

    def __init__(
        self,
        client: Valkey,
        key: str,
        *,
        backup_path: str | Path | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if not key.strip():
            raise ValueError("regex policy key는 비어 있을 수 없습니다.")
        self._client = client
        self._key = key
        self._backup_path = Path(backup_path) if backup_path is not None else None
        self._logger = logger or logging.getLogger("khan-python")
        self._cached_snapshot: RegexPolicySnapshot | None = None
        self._lock = threading.RLock()

    def refresh(self) -> RegexPolicyRefreshResult:
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
            self._cached_snapshot = snapshot
            return result

    def get_policy(self) -> RegexPolicySnapshot | None:
        with self._lock:
            return self._cached_snapshot

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
    ) -> tuple[RegexPolicySnapshot, RegexPolicyRefreshResult]:
        rows = payload.get("rules")
        raw_group_members = payload.get("group_members")
        if not isinstance(rows, list) or not isinstance(
            raw_group_members,
            Mapping,
        ):
            raise ValueError(
                f"{self._key}: rules 배열과 group_members object가 필요합니다.",
            )
        source_rows = tuple(row for row in rows if isinstance(row, Mapping))
        mapped = tuple(self._to_rule(row) for row in source_rows)
        rules = tuple(rule for rule in mapped if rule is not None)
        snapshot = RegexPolicySnapshot(
            rules=rules,
            group_members=self._group_members(raw_group_members),
        )
        return snapshot, RegexPolicyRefreshResult(
            active_rule_count=len(rules),
            rejected_rule_count=len(rows) - len(rules),
        )

    @staticmethod
    def _empty_payload() -> Mapping[str, Any]:
        return {"rules": [], "group_members": {}}

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
    ) -> RegexPolicyRefreshResult:
        try:
            payload = self._backup_payload()
            if payload is not None:
                snapshot, result = self._snapshot(payload)
                self._logger.warning(
                    "regex policy Valkey load failed; using local backup "
                    "(%s, error=%s)",
                    self._backup_path,
                    valkey_error,
                )
                self._cached_snapshot = snapshot
                return result
        except Exception:
            self._logger.exception(
                "regex policy local backup is invalid; using current cache",
            )

        self._logger.warning(
            "regex policy Valkey load failed and local backup is missing; "
            "using current or empty cache (error=%s)",
            valkey_error,
        )
        return self._current_or_empty_result()

    def _refresh_when_key_is_missing(self) -> RegexPolicyRefreshResult:
        try:
            payload = self._backup_payload()
            if payload is not None:
                snapshot, result = self._snapshot(payload)
                self._logger.warning(
                    "regex policy key is missing; using local backup (%s)",
                    self._backup_path,
                )
                self._cached_snapshot = snapshot
                return result
        except Exception:
            self._logger.exception(
                "regex policy local backup is invalid; using empty snapshot",
            )

        return self._current_or_empty_result()

    def _current_or_empty_result(self) -> RegexPolicyRefreshResult:
        if self._cached_snapshot is not None:
            return RegexPolicyRefreshResult(
                active_rule_count=len(self._cached_snapshot.rules),
            )
        snapshot, result = self._snapshot(self._empty_payload())
        self._cached_snapshot = snapshot
        return result

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
                "regex policy local backup save failed (%s)",
                path,
            )
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _to_rule(self, row: Mapping[str, Any]) -> RegexPolicyRule | None:
        rule_id = str(row.get("id") or "").strip()
        name = str(row.get("name") or "").strip()
        scope = str(row.get("scope") or "").strip()
        action = str(row.get("action") or "").strip()
        category = self._text(row.get("category")).strip()
        subject_id = str(row.get("subject_id") or "").strip()
        expression = self._decode_expression(row.get("rule"))
        if (
            not rule_id
            or not name
            or not action
            or scope not in self._SUPPORTED_SCOPES
            or (scope != "default" and not subject_id)
            or expression is None
        ):
            return None
        try:
            re.compile(expression)
        except (re.error, TypeError, ValueError):
            return None
        return RegexPolicyRule(
            id=rule_id,
            name=name,
            expression=expression,
            action=action,
            scope=scope,
            category=category,
            subject_id=subject_id,
        )

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

    @staticmethod
    def _decode_expression(value: object) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            decoded = base64.b64decode(value, validate=True).decode("utf-8")
            return decoded.strip() or None
        except (ValueError, UnicodeDecodeError):
            return None

    @staticmethod
    def _text(value: object) -> str:
        if isinstance(value, (list, tuple, set)):
            return ",".join(str(item) for item in value)
        return str(value or "")
