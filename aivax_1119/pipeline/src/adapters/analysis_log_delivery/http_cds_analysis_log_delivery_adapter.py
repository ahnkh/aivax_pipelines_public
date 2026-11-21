"""nNetCDS traffic-log HTTP 전달 adapter."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import requests

from src.adapters.analysis_log_delivery.cds_payload_mapper import (
    CdsAnalysisLogPayloadMapper,
)
from src.domain.analysis_log_delivery.errors import (
    RetryableAnalysisLogDeliveryError,
)
from src.domain.analysis_log_delivery.models import (
    AnalysisLogBatchDeliveryResult,
    AnalysisLogDeliveryRow,
    MergedAnalysisLogDocument,
)
from src.domain.analysis_log_delivery.ports import AnalysisLogBatchDeliveryPort


class HttpCdsDeliveryAdapter(AnalysisLogBatchDeliveryPort):
    """CDS 로그 배치를 nNetCDS HTTP API로 전달한다."""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        *,
        timeout_seconds: float = 10,
        src_port: int = 0,
        dst_host: str = "example.com",
        dst_port: int = 443,
        session: requests.Session | None = None,
    ) -> None:
        if not api_url.strip():
            raise ValueError("CDS API URL이 필요합니다.")
        if not api_key.strip():
            raise ValueError("CDS API key가 필요합니다.")
        if timeout_seconds <= 0:
            raise ValueError("CDS 요청 제한 시간은 양수여야 합니다.")

        self._api_url = api_url
        self._timeout_seconds = timeout_seconds
        self._session = session or requests.Session()
        self._owns_session = session is None
        self._mapper = CdsAnalysisLogPayloadMapper(
            src_port=src_port,
            dst_host=dst_host,
            dst_port=dst_port,
        )

        self._session.headers.update(
            {
                "X-API-Key": api_key,
                "Content-Type": "application/json",
            }
        )

    def deliver(
        self,
        logs: Sequence[MergedAnalysisLogDocument],
    ) -> AnalysisLogBatchDeliveryResult:
        if not logs:
            return AnalysisLogBatchDeliveryResult(rows=())

        if len(logs) > 1_000:
            raise ValueError(
                "CDS API는 한 번에 최대 1000건만 전송할 수 있습니다.",
            )

        response = self._session.post(
            self._api_url,
            json=[self._mapper.toPersistence(log) for log in logs],
            timeout=self._timeout_seconds,
            verify=False,
        )

        if response.status_code == 500:
            raise RetryableAnalysisLogDeliveryError(
                "CDS 요청 실패 status=500",
            )

        if response.status_code != 200:
            response_body = response.text[:1_000].replace("\n", " ")
            raise RuntimeError(
                f"CDS 요청 실패 status={response.status_code} body={response_body}",
            )

        try:
            body = response.json()
        except ValueError as error:
            raise ValueError(
                "CDS 응답이 유효한 JSON이 아닙니다.",
            ) from error

        return self._parse_response(body, len(logs))

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    @classmethod
    def _parse_response(
        cls,
        body: Any,
        expected_count: int,
    ) -> AnalysisLogBatchDeliveryResult:
        """전체 응답을 검증한 뒤 파일 삭제에 사용할 결과를 만든다."""
        if not isinstance(body, Mapping):
            raise ValueError("CDS 응답이 JSON 객체가 아닙니다.")

        raw_rows = body.get("rows")
        if not isinstance(raw_rows, list) or len(raw_rows) != expected_count:
            raise ValueError(
                "CDS 응답 행 개수가 요청과 다릅니다.",
            )

        parsed_rows: list[AnalysisLogDeliveryRow] = []
        indexes: set[int] = set()
        inserted_count = 0

        for raw_row in raw_rows:
            if not isinstance(raw_row, Mapping):
                raise ValueError(
                    "CDS 응답 행이 JSON 객체가 아닙니다.",
                )

            index = raw_row.get("index")
            if (
                type(index) is not int
                or not 0 <= index < expected_count
                or index in indexes
            ):
                raise ValueError(
                    "CDS 응답 index가 올바르지 않습니다.",
                )
            indexes.add(index)

            status = raw_row.get("status")
            if status not in {"OK", "ERROR"}:
                raise ValueError(
                    "CDS 응답 status가 올바르지 않습니다.",
                )

            if status == "OK":
                raw_external_id = raw_row.get("id")
                if raw_external_id is None:
                    raise ValueError(
                        "CDS 성공 응답 행에 id가 없습니다.",
                    )
                inserted_count += 1

            parsed_rows.append(
                AnalysisLogDeliveryRow(
                    index=index,
                    succeeded=status == "OK",
                )
            )

        cls._validate_counts(
            body,
            expected_count=expected_count,
            inserted_count=inserted_count,
        )

        return AnalysisLogBatchDeliveryResult(rows=tuple(parsed_rows))

    @staticmethod
    def _validate_counts(
        body: Mapping[str, Any],
        *,
        expected_count: int,
        inserted_count: int,
    ) -> None:
        expected_values = {
            "received": expected_count,
            "inserted": inserted_count,
            "failed": expected_count - inserted_count,
        }

        for field_name, expected_value in expected_values.items():
            actual_value = body.get(field_name)
            if type(actual_value) is not int or actual_value != expected_value:
                raise ValueError(
                    f"CDS 응답 {field_name} 값이 일치하지 않습니다.",
                )
