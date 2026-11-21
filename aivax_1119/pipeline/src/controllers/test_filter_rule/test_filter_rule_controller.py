"""부수효과 없는 정책 테스트 HTTP controller."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from src.application.use_cases.test_filter_rule import FilterRuleTestUseCase
from src.controllers.test_filter_rule.dto import FilterRuleTestItem
from src.controllers.test_filter_rule.mapper import TestFilterRuleMapper


class FilterRuleTestController:
    def __init__(self, use_case: FilterRuleTestUseCase) -> None:
        self._use_case = use_case
        self.router = APIRouter()
        self.router.add_api_route(
            "/v1/filter/testrule",
            self.test_rule,
            methods=["POST"],
            name="testFilterRule",
        )
        self.router.add_api_route(
            "/v1/filter/testrule/upload",
            self.test_rule_with_files,
            methods=["POST"],
            name="testFilterRuleWithFiles",
        )

    async def test_rule(self, model_item: FilterRuleTestItem) -> dict:
        result = await self._use_case.execute(
            TestFilterRuleMapper.to_request(model_item),
        )
        return TestFilterRuleMapper.to_success_response(result)

    async def test_rule_with_files(
        self,
        payload: str = Form(...),
        files: list[UploadFile] = File(default_factory=list),
    ) -> dict:
        try:
            model_item = FilterRuleTestItem.model_validate(json.loads(payload))
        except (json.JSONDecodeError, ValueError) as error:
            raise HTTPException(
                status_code=422,
                detail="payload must match the policy test contract",
            ) from error
        with TemporaryDirectory(prefix="aivax-policy-test-") as directory:
            attachments = []
            for index, upload in enumerate(files):
                safe_name = Path(upload.filename or f"attachment-{index}").name
                target = Path(directory) / f"{index:03d}-{safe_name}"
                size = await self._write_upload(upload, target)
                attachments.append(
                    {
                        "id": str(target),
                        "name": safe_name,
                        "mime_type": upload.content_type or "",
                        "size": size,
                    }
                )
            request_item = FilterRuleTestItem.model_validate(
                {**model_item.model_dump(), "attachments": attachments},
            )
            result = await self._use_case.execute(
                TestFilterRuleMapper.to_request(request_item),
            )
            return TestFilterRuleMapper.to_success_response(result)

    @staticmethod
    async def _write_upload(upload: UploadFile, target: Path) -> int:
        size = 0
        with target.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                output.write(chunk)
                size += len(chunk)
        await upload.close()
        return size
