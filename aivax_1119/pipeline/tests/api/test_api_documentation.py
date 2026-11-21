"""docs/swagger가 문서 원본이며 실제 요청 계약과 일치하는지 검증한다."""

import re
from copy import deepcopy
from unittest.mock import AsyncMock
from urllib.parse import urljoin

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts.check_openapi import check_request_contracts, validate_document
from src.controllers.documentation import load_openapi, setup_documentation
from src.controllers.input_analysis.v1.dto import InputAnalysisItem
from src.controllers.input_analysis.v1.input_analysis_controller import (
    InputAnalysisController,
)
from src.controllers.output_analysis.v1.dto import OutputFilterItem
from src.controllers.output_analysis.v1.output_analysis_controller import (
    OutputAnalysisController,
)


def documentation_app(root_path=""):
    app = FastAPI(root_path=root_path)
    app.include_router(InputAnalysisController(AsyncMock()).router)
    app.include_router(OutputAnalysisController(AsyncMock()).router)

    @app.get("/other", summary="기존 API")
    def other():
        return {"ok": True}

    setup_documentation(app)
    return app


def test_live_openapi_uses_file_definitions_and_preserves_other_routes():
    document = load_openapi()
    with TestClient(documentation_app()) as client:
        schema = client.get("/openapi.json").json()
        assert client.get("/docs").status_code == 200
    for path, definition in document["paths"].items():
        live = deepcopy(schema["paths"][path])
        live["post"]["description"] = live["post"]["description"].replace(
            "./docs/swagger/", "./"
        )
        assert live == definition
    for name, definition in document["components"]["schemas"].items():
        assert schema["components"]["schemas"][name] == definition
    assert schema["paths"]["/other"]["get"]["summary"] == "기존 API"


def test_field_description_is_read_from_docs_file(monkeypatch, tmp_path):
    import json

    from src.controllers import documentation

    document = load_openapi()
    field = document["components"]["schemas"]["VariantFilterForm"]["properties"][
        "prompt"
    ]
    field["description"] = "이 설명은 docs/swagger 파일에서 직접 수정한 값입니다."
    (tmp_path / "openapi.json").write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(documentation, "SWAGGER_DIRECTORY", tmp_path)
    schema = documentation_app().openapi()
    assert (
        schema["components"]["schemas"]["VariantFilterForm"]["properties"]["prompt"]
        == field
    )


@pytest.mark.parametrize("filename", ["openapi.json", "openapi-develop.json"])
def test_all_references_resolve_and_all_declared_fields_have_descriptions(filename):
    assert validate_document(load_openapi(filename)) == []


def test_checker_detects_missing_description_and_broken_reference():
    document = deepcopy(load_openapi())
    field = document["components"]["schemas"]["VariantFilterForm"]["properties"][
        "prompt"
    ]
    del field["description"]
    field["$ref"] = "#/components/schemas/Missing"
    errors = validate_document(document)
    assert any("missing description" in error for error in errors)
    assert any("unresolved reference" in error for error in errors)


def test_current_request_definitions_match_dtos_and_checker_detects_drift():
    document = load_openapi()
    assert check_request_contracts(document) == []
    document["components"]["schemas"]["VariantFilterForm"]["properties"]["prompt"][
        "default"
    ] = "changed"
    assert check_request_contracts(document)


@pytest.mark.parametrize(
    "path, model",
    [
        ("/v1/filter/multiple_filter", InputAnalysisItem),
        ("/v1/filter/output_filter", OutputFilterItem),
    ],
)
def test_documented_request_examples_are_accepted_by_real_dtos(path, model):
    media = load_openapi()["paths"][path]["post"]["requestBody"]["content"][
        "application/json"
    ]
    for example in media["examples"].values():
        model.model_validate(example["value"])


def test_current_and_historical_contracts_remain_distinct():
    current = load_openapi()["components"]["schemas"]
    old = load_openapi("openapi-develop.json")["components"]["schemas"]
    assert {"user_consent", "event_time"} <= set(
        current["VariantFilterForm"]["properties"]
    )
    assert not {"user_consent", "event_time"} & set(
        old["DevelopInputRequest"]["properties"]
    )
    assert "user_consent" not in old["DevelopOutputRequest"]["properties"]
    assert current["FinalDecision"]["properties"]["action"]["enum"] == [
        "accept",
        "block",
        "masking",
        "failed",
        "",
    ]
    assert old["DevelopFinalDecision"]["properties"]["action"]["enum"] == [
        "allow",
        "block",
        "masking",
    ]
    assert "filter_result" not in current["InputAnalysisResponse"]["required"]


def test_historical_swagger_is_read_only_and_supports_proxy_root_path():
    app = FastAPI(root_path="/pipeline")
    setup_documentation(app)
    with TestClient(app) as client:
        page = client.get("/docs/develop")
        schema = client.get("/openapi-develop.json")
    assert page.status_code == 200
    assert '"supportedSubmitMethods": []' in page.text
    assert "/pipeline/openapi-develop.json" in page.text
    assert schema.json() == load_openapi("openapi-develop.json")
    assert "/docs/develop" not in app.openapi()["paths"]


@pytest.mark.parametrize("root_path", ["", "/pipeline"])
def test_api_description_links_serve_detail_pages_and_assets(root_path):
    with TestClient(documentation_app(root_path)) as client:
        schema = client.get("/openapi.json").json()
        for path in ("/v1/filter/multiple_filter", "/v1/filter/output_filter"):
            description = schema["paths"][path]["post"]["description"]
            link = re.search(r"\]\(([^)]+\.html)\)", description).group(1)
            url = urljoin(f"http://testserver{root_path}/docs", link)
            response = client.get(url)
            assert response.status_code == 200
            assert f'data-api-path="{path}"' in response.text
            for asset in (
                "reference.js",
                "reference.css",
                "../openapi.json",
                "../index.html",
            ):
                assert client.get(urljoin(url, asset)).status_code == 200
