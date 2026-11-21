#!/usr/bin/env python3
"""docs/swagger 명세의 참조·필드 설명·실제 요청 DTO와의 일치 여부를 검사한다."""

import json
import re
import sys
from pathlib import Path
from typing import Any

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE_ROOT))

from src.controllers.input_analysis.v1.dto import (  # noqa: E402
    FileAttachItem,
    InputAnalysisItem,
)
from src.controllers.output_analysis.v1.dto import OutputFilterItem  # noqa: E402

SWAGGER_DIRECTORY = PIPELINE_ROOT / "docs" / "swagger"


def contract_shape(value: Any) -> Any:
    """문서의 설명·예제를 제외하고 DTO가 정하는 타입·기본값·필수 여부를 비교한다."""
    if isinstance(value, dict):
        if "$ref" in value:
            return {"$ref": value["$ref"].rsplit("/", 1)[-1]}
        return {
            key: contract_shape(item)
            for key, item in value.items()
            if key not in {"description", "title", "example", "examples", "$defs"}
        }
    if isinstance(value, list):
        return [contract_shape(item) for item in value]
    return value


def validate_document(document: dict[str, Any]) -> list[str]:
    errors = []

    def visit(value: Any, location: str = "#") -> None:
        if isinstance(value, dict):
            for page in re.findall(
                r"\]\(\./((?:[a-z0-9-]+/)*[a-z0-9-]+\.html)\)",
                value.get("description", ""),
            ):
                if not (SWAGGER_DIRECTORY / page).is_file():
                    errors.append(f"{location}: missing documentation page: {page}")
            if "$ref" in value:
                reference = value["$ref"]
                if not reference.startswith("#/"):
                    errors.append(f"{location}: external reference: {reference}")
                else:
                    target = document
                    try:
                        for part in reference[2:].split("/"):
                            target = target[part.replace("~1", "/").replace("~0", "~")]
                    except (KeyError, TypeError):
                        errors.append(f"{location}: unresolved reference: {reference}")
            for field, schema in value.get("properties", {}).items():
                if not schema.get("description", "").strip():
                    errors.append(f"{location}/properties/{field}: missing description")
            for key, item in value.items():
                # 예제 데이터 안의 properties/$ref는 스키마가 아니다.
                if key not in {"example", "examples", "default"}:
                    visit(item, f"{location}/{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{location}/{index}")

    visit(document)
    return errors


def check_request_contracts(document: dict[str, Any]) -> list[str]:
    schemas = document["components"]["schemas"]
    errors = []
    for model in (InputAnalysisItem, OutputFilterItem, FileAttachItem):
        if contract_shape(schemas[model.__name__]) != contract_shape(
            model.model_json_schema()
        ):
            errors.append(
                f"{model.__name__}: docs/swagger와 실제 요청 DTO의 계약 불일치"
            )
    return errors


def main() -> int:
    errors = []
    for filename in ("openapi.json", "openapi-develop.json"):
        document = json.loads(
            (SWAGGER_DIRECTORY / filename).read_text(encoding="utf-8")
        )
        found = validate_document(document)
        if filename == "openapi.json":
            found.extend(check_request_contracts(document))
        errors.extend(f"{filename}: {error}" for error in found)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("docs/swagger: references, field descriptions and request contracts OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
