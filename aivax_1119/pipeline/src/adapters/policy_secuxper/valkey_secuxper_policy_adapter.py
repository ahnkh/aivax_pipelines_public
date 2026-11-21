from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from valkey import Valkey

from src.domain.policy_secuxper.models import (
    SecuxperPolicyRefreshResult,
    SecuxperPolicyRule,
    SecuxperPolicySnapshot,
)
from src.domain.policy_secuxper.ports import SecuxperPolicyPort


class ValkeySecuxperPolicyAdapter(SecuxperPolicyPort):
    """secuXper rule과 group membership snapshot을 읽는다.

    Valkey에서 정상 load한 payload는 메모리 cache와 로컬 JSON에 저장한다.
    Valkey를 사용할 수 없으면 JSON, 기존 메모리 cache, 빈 정책 순서로 복구한다.
    """

    def __init__(
        self,
        client: Valkey,
        key: str,
        *,
        backup_path: str | Path | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if not key.strip():
            raise ValueError("secuXper policy key는 비어 있을 수 없습니다.")
        self._client = client
        self._key = key
        self._backup_path = Path(backup_path) if backup_path is not None else None
        self._logger = logger or logging.getLogger("khan-python")
        self._lock = threading.RLock()
        self._policy = SecuxperPolicySnapshot()

    def refresh(self) -> SecuxperPolicyRefreshResult:
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

    def get_policy(self) -> SecuxperPolicySnapshot:
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
    ) -> tuple[SecuxperPolicySnapshot, SecuxperPolicyRefreshResult]:
        rows = payload.get("rules")
        raw_group_members = payload.get("group_members")
        if not isinstance(rows, list) or not isinstance(
            raw_group_members,
            Mapping,
        ):
            raise ValueError(
                f"{self._key}: rules 배열과 group_members object가 필요합니다.",
            )
        if set(payload) != {"version", "rules", "group_members"}:
            raise ValueError("secuXper policy envelope fields are invalid")
        version = payload.get("version")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("secuXper policy version is required")
        rules = tuple(self._to_rule(row) for row in rows)
        snapshot = SecuxperPolicySnapshot(
            version=version,
            rules=rules,
            group_members=self._group_members(raw_group_members),
        )
        return snapshot, SecuxperPolicyRefreshResult(active_rule_count=len(rules))

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
    ) -> SecuxperPolicyRefreshResult:
        try:
            payload = self._backup_payload()
            if payload is not None:
                snapshot, result = self._snapshot(payload)
                self._logger.warning(
                    "secuXper policy Valkey load failed; "
                    "using local backup (%s, error=%s)",
                    self._backup_path,
                    valkey_error,
                )
                self._policy = snapshot
                return result
        except Exception:
            self._logger.exception(
                "secuXper policy local backup is invalid; using current cache",
            )
        self._logger.warning(
            "secuXper policy Valkey load failed and local backup is missing; "
            "using current cache (error=%s)",
            valkey_error,
        )
        return self._current_result()

    def _refresh_when_key_is_missing(self) -> SecuxperPolicyRefreshResult:
        try:
            payload = self._backup_payload()
            if payload is not None:
                snapshot, result = self._snapshot(payload)
                self._logger.warning(
                    "secuXper policy key is missing; using local backup (%s)",
                    self._backup_path,
                )
                self._policy = snapshot
                return result
        except Exception:
            self._logger.exception(
                "secuXper policy local backup is invalid; using current cache",
            )
        return self._current_result()

    def _current_result(self) -> SecuxperPolicyRefreshResult:
        return SecuxperPolicyRefreshResult(active_rule_count=len(self._policy.rules))

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
                "secuXper policy local backup save failed (%s)",
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

    @staticmethod
    def _to_rule(row: object) -> SecuxperPolicyRule:
        fields = {"id", "name", "targets", "policy_code", "scope", "subject_id"}
        if not isinstance(row, Mapping) or not fields.issubset(row) or set(row) - fields - {"category", "description"}:
            raise ValueError("secuXper rule fields are invalid")
        if not all(isinstance(row[field], str) for field in fields):
            raise ValueError("secuXper rule fields must be strings")
        values = {field: row[field].strip() for field in fields}
        description = row.get("description")
        if description is not None and not isinstance(description, str):
            raise ValueError("secuXper policy description must be a string")
        if isinstance(description, str) and description.strip():
            values["name"] = description.strip()
        if (
            not all(values[field] for field in ("id", "name", "policy_code"))
            or values["targets"] != "prompt"
            or values["scope"] not in {"user", "service", "group", "default"}
            or (values["scope"] != "default" and not values["subject_id"])
            or (values["scope"] == "default" and values["subject_id"])
        ):
            raise ValueError("secuXper rule values are invalid")
        if values["scope"] == "user":
            values["subject_id"] = values["subject_id"].lower()
        category = row.get("category")
        if category is not None and not isinstance(category, str):
            raise ValueError("secuXper policy category must be a string or null")
        return SecuxperPolicyRule(**values, category=(category or "").strip())
