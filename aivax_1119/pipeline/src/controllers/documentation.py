"""docs/swagger의 명세를 읽어 FastAPI 문서에 연결한다."""

import json
import re
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

SWAGGER_DIRECTORY = Path(__file__).resolve().parents[2] / "docs" / "swagger"
if not SWAGGER_DIRECTORY.is_dir():
    SWAGGER_DIRECTORY = Path(sys.prefix) / "share" / "aivax-pipeline" / "swagger"


def load_openapi(filename: str = "openapi.json") -> dict[str, Any]:
    return json.loads((SWAGGER_DIRECTORY / filename).read_text(encoding="utf-8"))


def setup_documentation(app: FastAPI) -> None:
    original_openapi = app.openapi
    app.openapi_schema = None

    def documented_openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            generated = original_openapi()
            documented = load_openapi()
            for path in documented["paths"].values():
                for operation in path.values():
                    if isinstance(operation, dict) and "description" in operation:
                        # 정적 뷰어의 상대 링크를 실행 앱의 문서 경로에 맞춘다.
                        operation["description"] = re.sub(
                            r"\]\(\./((?:[a-z0-9-]+/)*[a-z0-9-]+\.html)\)",
                            r"](./docs/swagger/\1)",
                            operation["description"],
                        )
            # 다른 관리·Health API는 FastAPI가 생성한 문서를 유지한다.
            generated["info"] = documented["info"]
            generated["tags"] = documented.get("tags", [])
            generated["paths"].update(documented["paths"])
            generated.setdefault("components", {}).setdefault("schemas", {}).update(
                documented["components"]["schemas"]
            )
            app.openapi_schema = generated
        return app.openapi_schema

    app.openapi = documented_openapi
    app.title = load_openapi()["info"]["title"]
    app.swagger_ui_parameters = {
        "displayRequestDuration": True,
        "defaultModelsExpandDepth": 1,
        "defaultModelExpandDepth": 2,
        "defaultModelRendering": "model",
    }
    app.mount(
        "/docs/swagger",
        StaticFiles(directory=SWAGGER_DIRECTORY, html=True),
        name="swagger-reference",
    )

    @app.get("/openapi-develop.json", include_in_schema=False)
    def historical_schema() -> dict:
        return load_openapi("openapi-develop.json")

    @app.get("/docs/develop", include_in_schema=False)
    def historical_swagger(request: Request) -> HTMLResponse:
        root_path = request.scope.get("root_path", "").rstrip("/")
        return get_swagger_ui_html(
            openapi_url=f"{root_path}/openapi-develop.json",
            title="AIVAX Pipeline API — develop b05a06be",
            swagger_ui_parameters={
                "supportedSubmitMethods": [],
                "defaultModelRendering": "model",
                "defaultModelExpandDepth": 2,
            },
        )
