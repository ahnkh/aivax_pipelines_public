"""INPUT과 OUTPUT 원본 분석 로그를 병합하는 도메인 규칙."""

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from src.domain.analysis_log_merge.models.analysis_log_payload import AnalysisLogPayload


class AnalysisLogMerger:
    """INPUT 분석 로그를 기준으로 OUTPUT 분석 로그를 하나의 문서로 병합한다."""

    def merge(
        self,
        input_log: AnalysisLogPayload,
        output_log: AnalysisLogPayload,
    ) -> dict[str, Any]:
        """원본을 변경하지 않고, OUTPUT 정보를 반영한 새 문서를 반환한다."""
        merged_log = deepcopy(dict(input_log))
        output_document = deepcopy(dict(output_log))
        generated_files = output_document.pop("generated_files", None)
        generated_only = bool(output_document.pop("generated_only", False))

        # A session-only generated file may be downloaded after the model
        # response was already stored. In that case only append the attachment;
        # never blank or replace the existing response and identity metadata.
        if generated_only:
            self.__append_generated_files(merged_log, generated_files)
            return merged_log

        # OUTPUT의 user가 비어 있거나 일부 식별값만 가진 경우에도
        # INPUT에서 이미 확보한 사용자 식별값을 잃지 않는다.
        input_user = self.__copy_user(merged_log.get("user"))
        input_ai_service_id = merged_log.get("ai_service_id")
        input_model = str(merged_log.get("model") or "").strip()
        merged_log.update(output_document)
        if not str(merged_log.get("model") or "").strip() and input_model:
            merged_log["model"] = input_model

        # 사용자 동기화용 AI 서비스 ID는 입력 요청이 소유한다. OUTPUT API의
        # 기본값이나 표시용 서비스명이 원본 ID를 덮어쓰면 안 된다.
        if input_ai_service_id is not None:
            merged_log["ai_service_id"] = input_ai_service_id

        merged_user = merged_log.get("user")
        if not isinstance(merged_user, Mapping):
            merged_user = input_user
            merged_log["user"] = merged_user
        else:
            merged_user = dict(merged_user)
            merged_log["user"] = merged_user
            self.__fill_missing_identity(merged_user, input_user)

        # OUTPUT 처리 시점에 보강된 flat 사용자 값이 있으면 이를 최종 값으로 쓴다.
        self.__apply_output_identity(merged_user, output_document)
        self.__append_generated_files(merged_log, generated_files)
        return merged_log

    @staticmethod
    def __append_generated_files(merged_log: dict[str, Any], generated: object) -> None:
        """생성 이미지를 업로드와 같은 file block 첨부로 붙인다."""
        if not isinstance(generated, list) or not generated:
            return
        summaries: list[dict[str, Any]] = []
        for item in generated:
            if not isinstance(item, Mapping):
                continue
            path = str(item.get("file_backup_path") or "").strip()
            name = str(item.get("file_name") or "").strip()
            if not path or not name:
                continue
            mime = str(item.get("mime_type") or "image/png").strip() or "image/png"
            extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if not extension:
                extension = mime.rsplit("/", 1)[-1].lower()
            if extension == "jpeg":
                extension = "jpg"
            try:
                size = int(item.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            summaries.append(
                {
                    # 생성 파일 보관은 정책 탐지가 아니므로 수집 값으로 기록한다.
                    "action": "",
                    "policy_id": "",
                    "policy_name": "",
                    "file_name": name,
                    "file_backup_path": path,
                    "name": "",
                    "targets": "",
                    "category": "",
                    "scope": "",
                    "regex_pattern": "",
                    "file_info": {
                        "mime_type": mime,
                        "file_ext": extension,
                        "size": size,
                    },
                }
            )
        if not summaries:
            return
        detections = merged_log.get("filter_detect")
        if not isinstance(detections, list):
            detections = []
            merged_log["filter_detect"] = detections
        file_detection = next(
            (
                item
                for item in detections
                if isinstance(item, Mapping) and item.get("filter") == "file block"
            ),
            None,
        )
        if not isinstance(file_detection, dict):
            file_detection = {"filter": "file block", "mode": "", "file_summary": []}
            detections.append(file_detection)
        existing = file_detection.get("file_summary")
        if not isinstance(existing, list):
            existing = []
            file_detection["file_summary"] = existing
        existing_paths = {
            str(item.get("file_backup_path") or "")
            for item in existing
            if isinstance(item, Mapping)
        }
        existing.extend(
            item
            for item in summaries
            if str(item.get("file_backup_path") or "") not in existing_paths
        )

    @staticmethod
    def __copy_user(value: object) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {}
        return deepcopy(dict(value))

    @staticmethod
    def __fill_missing_identity(
        merged_user: dict[str, Any],
        input_user: Mapping[str, Any],
    ) -> None:
        for key in ("id", "name", "email", "uuid"):
            if not str(merged_user.get(key) or "").strip() and input_user.get(key):
                merged_user[key] = input_user[key]

    @staticmethod
    def __apply_output_identity(
        merged_user: dict[str, Any],
        output_log: Mapping[str, Any],
    ) -> None:
        output_user = output_log.get("user")
        output_user = output_user if isinstance(output_user, Mapping) else {}

        identities = {
            "id": output_log.get("user_id") or output_user.get("id"),
            "email": output_log.get("user_email") or output_user.get("email"),
            "uuid": output_log.get("user_uuid") or output_user.get("uuid"),
        }
        for key, value in identities.items():
            normalized_value = str(value or "").strip()
            if normalized_value:
                merged_user[key] = normalized_value
