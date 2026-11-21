"""실제 HTTP → 상주 CSO JVM → bulk_logs → Fluent Bit → OpenSearch를 검증한다."""

import json
import uuid
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile

import psutil
import pytest
from PIL import Image

from scripts.cso_fixtures import write_cso_docx as write_docx
from scripts.cso_fixtures import (
    write_password_protected_docx,
    write_password_protected_pdf,
)
from src.domain.inspection_input.models.block_phrases import get_block_phrases
from src.domain.inspection_input.models.visible_block_log import logged_block_output

pytestmark = pytest.mark.pipeline_container_integration


@pytest.mark.parametrize(
    ("grades", "expected_status", "expected_action"),
    [
        # O 등급만 있으면 허용하고 CSO 상태는 open으로 기록한다.
        # 일반 필터가 꺼져 있으므로 허용 응답은 action="", action_code=0이다.
        pytest.param([3], "open", "", id="O-allow"),
        # 파일이 없으면 조회 오류로 차단하고 CSO 상태를 error로 기록한다.
        pytest.param(["missing"], "error", "block", id="read-error-block"),
        # 미태깅 > C/S 차단 > 조회 오류 순으로 전체 상태를 집계한다.
        pytest.param([1, "missing"], "blocked", "block", id="block-overrides-error"),
        pytest.param(
            [None, 1, "missing"], "untagged", "block", id="untagged-overrides-all"
        ),
    ],
)
def test_multiple_api_runs_cso_and_indexes_log(
    pipeline_stack,
    tmp_path,
    grades,
    expected_status,
    expected_action,
):
    """O 등급 허용과 조회 오류 차단을 실제 응답·최종 로그에서 확인한다."""
    stack = pipeline_stack
    message_id = "cso-integration-" + uuid.uuid4().hex

    # 준비: 정상 파일은 Fasoo 라이브러리로 태깅한다.
    # missing은 경로만 전달하고 파일을 만들지 않아 실제 읽기 오류를 유발한다.
    attachments = []
    for index, grade in enumerate(grades):
        path = tmp_path / f"{index}.docx"
        if grade != "missing":
            write_docx(path, grade)
        attachments.append({"id": str(path), "name": path.name})
    body = {
        "message_id": message_id,
        "prompt": "첨부 문서를 요약해 주세요.",
        # 파일 분석 필터를 요청하지 않아도 CSO 가드가 독립적으로 실행되어야 한다.
        "filter_list": ["input_filter"],
        "attachments": attachments,
        "email": "integration@example.invalid",
        "user_id": "integration-user",
        "client_host": "127.0.0.1",
        "ai_service": 1,
    }
    # 실행: 실제 multiple API의 최종 판정과 요청 식별자를 확인한다.
    response = stack.api.post("/v1/filter/multiple_filter", json=body)
    response.raise_for_status()
    decision = response.json()["final_decision"]
    assert response.json()["cso"]["status"] == expected_status
    assert decision["action"] == expected_action
    assert decision["action_code"] == (1 if expected_action == "block" else 0)
    assert decision["message_id"] == message_id
    if expected_action != "block":
        # 허용된 입력은 테스트용 LLM 응답을 OUTPUT API에 전달해 최종 로그를 완성한다.
        response = stack.api.post(
            "/v1/filter/output_filter",
            json={
                "message_id": message_id,
                "llm_output": "integration summary",
                "email": body["email"],
                "user_id": body["user_id"],
                "client_host": body["client_host"],
                "ai_service": 1,
            },
        )
        response.raise_for_status()
    else:
        # 차단된 입력은 OUTPUT 호출 없이 안내 문구와 최종 로그가 생성되어야 한다.
        assert decision["block_message"]

    # 검증 1: Fluent Bit가 전달한 문서를 OpenSearch에서 조회한다.
    # spool에는 같은 요청의 로그가 정확히 한 건 있어야 한다.
    # CSO 값과 요청 원문도 그대로 보존되어야 한다.
    hit = stack.indexed_document(message_id)
    assert hit["_id"] == message_id
    indexed = hit["_source"]
    spooled = stack.spooled_documents(message_id)
    assert len(spooled) == 1
    assert indexed["cso"] == spooled[0]["cso"]
    assert indexed["prompt"] == body["prompt"]

    # 검증 2: 요청 전체 상태와 파일별 등급·상태·경로가 기대값과 같아야 한다.
    # 읽기 오류는 grade=null, status=error로 남겨 정상 태깅·미태깅과 구분한다.
    expected_files = [
        {
            "file_name": item["name"],
            "file_path": item["id"],
            "grade": grade if isinstance(grade, int) else None,
            "status": "error"
            if grade == "missing"
            else ("untagged" if grade is None else "tagged"),
        }
        for item, grade in zip(attachments, grades, strict=True)
    ]
    assert indexed["cso"] == {"status": expected_status, "files": expected_files}

    # 검증 3: 로그 시각의 날짜에 해당하는 일별 인덱스에 저장되어야 한다.
    day = datetime.fromisoformat(indexed["@timestamp"].replace("Z", "+00:00"))
    assert hit["_index"] == f"aivax_log-{day:%Y%m%d}"

    # 검증 4: 차단 로그에는 반환한 안내를, 허용 로그에는 병합한 LLM 응답을 기록한다.
    if expected_action == "block":
        assert indexed["mode"] == "block"
        assert indexed["output_text"] == logged_block_output(
            decision["block_message"],
            prompt=body["prompt"],
            cso_status=expected_status,
        )
    else:
        assert indexed["mode"] != "block"
        assert indexed["output_text"] == "integration summary"

    # 검증 5: 조회 오류가 발생해도 최초 상주 JVM 하나를 계속 재사용해야 한다.
    children = psutil.Process(stack.server.pid).children(recursive=True)
    assert tuple(p.pid for p in children if p.name() == "java") == stack.jvm_pids

    # 수동 확인과 실패 원인 추적을 위해 실제 요청·응답·색인 문서를 보존한다.
    (stack.artifacts / f"{message_id}.json").write_text(
        json.dumps(
            {
                "request": body,
                "response": decision,
                "opensearch": hit,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@pytest.mark.parametrize(
    "grades",
    [
        # 미태깅 파일 하나를 첨부하면 차단하고 태깅 여부 확인을 안내한다.
        pytest.param([None], id="untagged-only"),
        # O 등급 파일이 있어도 미태깅 파일이 섞이면 요청과 첨부파일 전체를 차단한다.
        pytest.param([3, None], id="O-and-untagged"),
    ],
)
def test_untagged_attachment_is_blocked_and_returns_block_prompt(
    pipeline_stack,
    tmp_path,
    grades,
):
    """미태깅 파일이 하나라도 있으면 요청을 차단하고 안내 문구를 반환한다.

    이미 구현된 동작의 회귀 테스트다. 판정이나 안내 문구를 mock하지 않고
    실제 DOCX → CSO JVM → HTTP 응답 → Fluent Bit → OpenSearch를 검증한다.
    """
    stack = pipeline_stack
    message_id = "cso-untagged-" + uuid.uuid4().hex

    # 준비: 보안등급 속성(FDRClass)이 없는 정상 DOCX를 만든다.
    # 파일 읽기 오류와 구분하기 위해 모든 첨부파일이 실제로 존재해야 한다.
    # O 등급과 섞인 경우도 준비해 미태깅 파일 하나가 전체 요청을 막는지 확인한다.
    attachments = []
    original_contents = {}
    for index, grade in enumerate(grades):
        path = tmp_path / f"attachment-{index}.docx"
        write_docx(path, grade)
        assert path.is_file()
        if grade is None:
            with ZipFile(path) as document:
                assert "docProps/custom.xml" not in document.namelist()
        original_contents[path.name] = path.read_bytes()
        attachments.append({"id": str(path), "name": path.name})

    # 파일 분석 필터를 요청하지 않아도 CSO 가드는 선행 차단을 수행해야 한다.
    body = {
        "message_id": message_id,
        "prompt": "첨부파일 내용을 외부 AI로 보내 요약해 주세요.",
        "filter_list": ["input_filter"],
        "attachments": attachments,
        "email": "untagged@example.invalid",
        "user_id": "untagged-test-user",
        "client_host": "127.0.0.1",
        "ai_service": 1,
    }

    # 실행: 실제 multiple API를 한 번 호출한다. 차단된 요청이므로
    # output API를 호출하거나 가상의 LLM 응답을 만들어 전달하지 않는다.
    response = stack.api.post("/v1/filter/multiple_filter", json=body)

    # 검증 1: API 처리 자체는 성공하고 최종 판정은 차단이어야 한다.
    # 차단 안내는 block_message로 반환하며 masked_contents에는 담지 않는다.
    assert response.status_code == 200
    decision = response.json()["final_decision"]
    expected_prompt = (
        "[AIVAX] CSO 등급 차단\n\n"
        "CSO 등급이 존재하지 않는 첨부파일이 있어 요청을 차단했습니다."
    )
    assert decision == {
        "action": "block",
        "action_code": 1,
        "message_id": message_id,
        "masked_contents": "",
        "block_message": expected_prompt,
    }

    # 검증 2: Fluent Bit가 실제로 수집한 최종 로그를 OpenSearch에서 조회한다.
    # 미태깅은 조회 오류(error)가 아닌 untagged로 남긴다.
    hit = stack.indexed_document(message_id)
    indexed = hit["_source"]
    assert hit["_id"] == message_id
    assert indexed["cso"] == {
        "status": "untagged",
        "files": [
            {
                "file_name": item["name"],
                "file_path": item["id"],
                "grade": grade,
                "status": "untagged" if grade is None else "tagged",
            }
            for item, grade in zip(attachments, grades, strict=True)
        ],
    }
    assert indexed["mode"] == "block"
    assert indexed["prompt"] == body["prompt"]
    assert indexed["output_text"] == expected_prompt

    # 검증 3: 응답만 차단하는 것으로 끝나지 않고 첨부파일을 보관해야 한다.
    # 원래 입력 경로에서는 제거되고, 보관본의 바이트는 원본과 같아야 한다.
    # O 등급이 함께 있어도 차단된 요청의 첨부파일 전체를 보관한다.
    summaries = [
        summary
        for detection in indexed["filter_detect"]
        for summary in detection.get("file_summary", [])
    ]
    assert len(summaries) == len(attachments)
    assert {summary["file_name"] for summary in summaries} == set(original_contents)
    for item in attachments:
        assert not Path(item["id"]).exists()
    for summary in summaries:
        backup = Path(summary["file_backup_path"])
        assert backup.is_file()
        assert backup.read_bytes() == original_contents[summary["file_name"]]
        assert summary["file_info"]["inspection_status"] == "skipped"
        assert summary["file_info"]["skip_reason"] == "cso_blocked"

    # 검증 4: output API 호출 없이도 최종 로그가 spool에 정확히 한 건 남고,
    # 그 차단 안내와 CSO 판정이 OpenSearch에 동일하게 전달되어야 한다.
    spooled = stack.spooled_documents(message_id)
    assert len(spooled) == 1
    assert spooled[0]["cso"] == indexed["cso"]
    assert spooled[0]["mode"] == "block"
    assert spooled[0]["output_text"] == expected_prompt

    # 실패 원인 추적과 수동 확인을 위해 실제 요청·응답·색인 문서를 보존한다.
    (stack.artifacts / f"{message_id}.json").write_text(
        json.dumps(
            {"request": body, "response": decision, "opensearch": hit},
            ensure_ascii=False,
            indent=2,
        )
    )


@pytest.mark.parametrize(
    "grades",
    [
        # C 등급 단독: 차단하고 태깅 변경을 안내하며 grade=1을 유지한다.
        pytest.param([1], id="C-only"),
        # S 등급 단독: 차단하고 태깅 변경을 안내하며 grade=2를 유지한다.
        pytest.param([2], id="S-only"),
        # C+S: 요청 전체를 차단하고 두 파일 모두 보관하며 각 등급을 유지한다.
        pytest.param([1, 2], id="C-and-S"),
        # O+C: C 파일로 인해 요청을 차단하며 O 파일까지 함께 보관한다.
        pytest.param([3, 1], id="O-and-C"),
        # O+S: S 파일로 인해 요청을 차단하며 O 파일까지 함께 보관한다.
        pytest.param([3, 2], id="O-and-S"),
    ],
)
def test_tagged_cs_attachment_is_blocked_and_returns_block_prompt(
    pipeline_stack,
    tmp_path,
    grades,
):
    """C·S 등급 파일이 하나라도 있으면 요청 전체를 차단하고 재태깅을 안내한다."""
    stack = pipeline_stack
    message_id = "cso-cs-" + uuid.uuid4().hex

    # 준비: 일반 DOCX를 만들고 Fasoo setCTProperty로 태깅한다(C=1, S=2, O=3).
    # helper는 getCTProperty로 태깅 결과를 확인하고, API는 별도 상주 JVM에서 읽는다.
    # C·S 단독, C+S, O와의 혼합을 각각 독립된 요청으로 검증한다.
    attachments = []
    original_contents = {}
    for index, grade in enumerate(grades):
        grade_name = {1: "C", 2: "S", 3: "O"}[grade]
        path = tmp_path / f"attachment-{index}-{grade_name}.docx"
        write_docx(path, grade)
        original_contents[path.name] = path.read_bytes()
        attachments.append({"id": str(path), "name": path.name})

    # 파일 분석 필터를 요청하지 않아도 CSO 가드가 등급에 따라 차단해야 한다.
    body = {
        "message_id": message_id,
        "prompt": "첨부된 보안 문서를 요약해 주세요.",
        "filter_list": ["input_filter"],
        "attachments": attachments,
        "email": "cs-grade@example.invalid",
        "user_id": "cs-grade-test-user",
        "client_host": "127.0.0.1",
        "ai_service": 1,
    }

    # 실행: 실제 multiple API를 호출한다. 선행 차단되는 요청이므로
    # output API나 외부 AI를 호출하지 않고도 최종 차단 처리가 완료되어야 한다.
    response = stack.api.post("/v1/filter/multiple_filter", json=body)

    # 검증 1: C·S 등급 차단 안내를 정확히 반환해야 한다.
    # 정상적으로 태깅된 파일이므로 미태깅·조회 오류 안내와 구분한다.
    assert response.status_code == 200
    decision = response.json()["final_decision"]
    expected_prompt = (
        "[AIVAX] CSO 등급 차단\n\n"
        "C 또는 S 등급의 첨부파일이 있어 요청을 차단했습니다."
    )
    assert decision == {
        "action": "block",
        "action_code": 1,
        "message_id": message_id,
        "masked_contents": "",
        "block_message": expected_prompt,
    }

    # 검증 2: 실제 Fluent Bit가 전달한 OpenSearch 문서에 정책 차단과
    # 파일별 원래 등급이 남아야 한다. C·S도 파일 상태 자체는 tagged다.
    hit = stack.indexed_document(message_id)
    indexed = hit["_source"]
    assert hit["_id"] == message_id
    assert indexed["cso"] == {
        "status": "blocked",
        "files": [
            {
                "file_name": item["name"],
                "file_path": item["id"],
                "grade": grade,
                "status": "tagged",
            }
            for item, grade in zip(attachments, grades, strict=True)
        ],
    }
    assert indexed["mode"] == "block"
    assert indexed["prompt"] == body["prompt"]
    assert indexed["output_text"] == expected_prompt

    # 검증 3: 차단된 요청의 모든 첨부파일을 보관한다. O 파일이 섞여 있어도
    # 전체 요청이 차단되므로 함께 보관하고, 보관 중 파일 내용이 바뀌면 안 된다.
    summaries = [
        summary
        for detection in indexed["filter_detect"]
        for summary in detection.get("file_summary", [])
    ]
    assert len(summaries) == len(attachments)
    assert {summary["file_name"] for summary in summaries} == set(original_contents)
    for item in attachments:
        assert not Path(item["id"]).exists()
    for summary in summaries:
        backup = Path(summary["file_backup_path"])
        assert backup.is_file()
        assert backup.read_bytes() == original_contents[summary["file_name"]]
        assert summary["file_info"]["inspection_status"] == "skipped"
        assert summary["file_info"]["skip_reason"] == "cso_blocked"

    # 검증 4: OUTPUT 없이 최종 로그가 정확히 한 건 생성되고, 반환한 차단
    # 안내와 파일별 등급이 spool에서 OpenSearch까지 동일하게 전달되어야 한다.
    spooled = stack.spooled_documents(message_id)
    assert len(spooled) == 1
    assert spooled[0]["cso"] == indexed["cso"]
    assert spooled[0]["mode"] == "block"
    assert spooled[0]["output_text"] == expected_prompt

    # 검증 5: 여러 등급의 파일을 차단한 뒤에도 최초 상주 JVM을 재사용한다.
    children = psutil.Process(stack.server.pid).children(recursive=True)
    assert tuple(p.pid for p in children if p.name() == "java") == stack.jvm_pids

    # 수동 확인을 위해 각 케이스의 실제 요청·차단 응답·색인 문서를 보존한다.
    (stack.artifacts / f"{message_id}.json").write_text(
        json.dumps(
            {"request": body, "response": decision, "opensearch": hit},
            ensure_ascii=False,
            indent=2,
        )
    )


@pytest.mark.parametrize(
    ("extension", "expected_status"),
    [
        # TXT·PNG는 MIME 사전 판독에서 미지원으로 확인하여 error로 차단한다.
        pytest.param("txt", "error", id="unsupported-text"),
        # ZIP은 공통 MIME 매핑에서 HWPX로 조회한 뒤 등급 없음으로 차단한다.
        pytest.param("zip", "untagged", id="unsupported-archive"),
        pytest.param("png", "error", id="unsupported-image"),
    ],
)
def test_unsupported_file_types_are_blocked_with_expected_cso_status(
    pipeline_stack,
    tmp_path,
    extension,
    expected_status,
):
    """MIME 사전 판독과 실제 등급 조회 결과에 따라 미지원 파일을 차단한다."""
    stack = pipeline_stack
    message_id = "cso-unsupported-" + uuid.uuid4().hex

    # 준비: 확장자만 바꾼 DOCX 대신 각 형식에 맞는 정상 파일을 만든다.
    path = tmp_path / f"unsupported.{extension}"
    if extension == "txt":
        path.write_text("CSO 미지원 형식 테스트 문서", encoding="utf-8")
    elif extension == "zip":
        with ZipFile(path, "w") as archive:
            archive.writestr("readme.txt", "Ordinary ZIP, not an Office document")
    else:
        Image.new("RGB", (1, 1), color="white").save(path)
    original_contents = path.read_bytes()
    body = {
        "message_id": message_id,
        "prompt": "첨부파일 내용을 확인해 주세요.",
        "filter_list": ["input_filter"],
        "attachments": [{"id": str(path), "name": path.name}],
        "email": "unsupported@example.invalid",
        "user_id": "unsupported-test-user",
        "client_host": "127.0.0.1",
        "ai_service": 1,
    }

    # 실행: 실제 CSO 라이브러리로 조회하며 OUTPUT API는 호출하지 않는다.
    response = stack.api.post("/v1/filter/multiple_filter", json=body)

    # 검증 1: MIME 미지원은 조회 불가 안내, 등급 없음은 미태깅 안내를 반환한다.
    assert response.status_code == 200
    decision = response.json()["final_decision"]
    expected_prompt = get_block_phrases().cso_block(
        cs=False,
        untagged=expected_status == "untagged",
        error=expected_status == "error",
    )
    assert decision == {
        "action": "block",
        "action_code": 1,
        "message_id": message_id,
        "masked_contents": "",
        "block_message": expected_prompt,
    }

    # 검증 2: 전체와 파일별 상태에 실제 판독 경로의 결과가 남는다.
    hit = stack.indexed_document(message_id)
    indexed = hit["_source"]
    assert hit["_id"] == message_id
    assert indexed["cso"] == {
        "status": expected_status,
        "files": [
            {
                "file_name": path.name,
                "file_path": str(path),
                "grade": None,
                "status": expected_status,
            }
        ],
    }
    assert indexed["mode"] == "block"
    assert indexed["prompt"] == body["prompt"]
    assert indexed["output_text"] == expected_prompt

    # 검증 3: 미지원 파일도 보관하며 원래 내용이 손상되지 않아야 한다.
    summaries = [
        summary
        for detection in indexed["filter_detect"]
        for summary in detection.get("file_summary", [])
    ]
    assert len(summaries) == 1
    assert summaries[0]["file_name"] == path.name
    assert not path.exists()
    assert Path(summaries[0]["file_backup_path"]).read_bytes() == original_contents
    spooled = stack.spooled_documents(message_id)
    assert len(spooled) == 1
    assert spooled[0]["cso"] == indexed["cso"]
    assert spooled[0]["output_text"] == expected_prompt

    # 검증 4: 미지원 파일 조회는 JVM 장애가 아니므로 같은 프로세스를 유지한다.
    children = psutil.Process(stack.server.pid).children(recursive=True)
    assert tuple(p.pid for p in children if p.name() == "java") == stack.jvm_pids
    (stack.artifacts / f"{message_id}.json").write_text(
        json.dumps(
            {"request": body, "response": decision, "opensearch": hit},
            ensure_ascii=False,
            indent=2,
        )
    )


@pytest.mark.parametrize(
    ("document_type", "expected_status"),
    [
        # 암호 DOCX는 application/encrypted로 판독되어 MIME 미지원으로 차단한다.
        pytest.param("standard", "error", id="password-DOCX-standard"),
        pytest.param("agile", "error", id="password-DOCX-agile"),
        # PDF는 지원 MIME이므로 라이브러리의 등급 없음 응답에 따라 차단한다.
        pytest.param("pdf", "untagged", id="password-PDF"),
    ],
)
def test_password_protected_document_is_blocked_with_expected_cso_status(
    pipeline_stack,
    tmp_path,
    document_type,
    expected_status,
):
    """암호로 보호된 문서는 등급을 확인할 수 없으므로 차단하고 원본을 보관한다."""
    stack = pipeline_stack
    message_id = "cso-password-protected-" + uuid.uuid4().hex

    # 준비: Fasoo 라이브러리로 O 태깅을 확인한 뒤 실제 열기 암호를 설정한다.
    # helper가 잘못된 암호의 실패와 올바른 암호의 복호화·원본 일치를 확인한다.
    # 편집 제한 표시만 넣거나 암호 문서로 위장한 파일을 사용하지 않는다.
    if document_type == "pdf":
        path = write_password_protected_pdf(tmp_path / "protected.pdf")
    else:
        path = write_password_protected_docx(
            tmp_path / f"protected-{document_type}.docx",
            mode=document_type,
        )
    encrypted_contents = path.read_bytes()
    body = {
        "message_id": message_id,
        "prompt": "암호가 설정된 첨부 문서를 요약해 주세요.",
        "filter_list": ["input_filter"],
        "attachments": [{"id": str(path), "name": path.name}],
        "email": "encrypted@example.invalid",
        "user_id": "encrypted-test-user",
        "client_host": "127.0.0.1",
        "ai_service": 1,
    }

    # 실행: 암호를 전달하지 않은 실제 multiple API 요청으로 CSO 가드를 실행한다.
    response = stack.api.post("/v1/filter/multiple_filter", json=body)

    # 검증 1: 등급 확인 불가로 요청을 차단한다.
    assert response.status_code == 200
    decision = response.json()["final_decision"]
    expected_prompt = get_block_phrases().cso_block(
        cs=False,
        untagged=expected_status == "untagged",
        error=expected_status == "error",
    )
    assert decision == {
        "action": "block",
        "action_code": 1,
        "message_id": message_id,
        "masked_contents": "",
        "block_message": expected_prompt,
    }

    # 검증 2: 암호화 전 O 등급을 추정해 사용하지 않고 실제 조회 결과를 기록한다.
    # 암호 DOCX는 MIME 미지원, 암호 PDF는 실제 조회의 등급 없음으로 남는다.
    hit = stack.indexed_document(message_id)
    indexed = hit["_source"]
    assert hit["_id"] == message_id
    assert indexed["cso"] == {
        "status": expected_status,
        "files": [
            {
                "file_name": path.name,
                "file_path": str(path),
                "grade": None,
                "status": expected_status,
            }
        ],
    }
    assert indexed["mode"] == "block"
    assert indexed["prompt"] == body["prompt"]
    assert indexed["output_text"] == expected_prompt

    # 검증 3: 첨부파일은 원래 입력 경로에서 제거하고 암호화된 상태 그대로 보관한다.
    # 암호 해제나 재저장 없이 암호문 바이트 전체가 일치해야 한다.
    summaries = [
        summary
        for detection in indexed["filter_detect"]
        for summary in detection.get("file_summary", [])
    ]
    assert len(summaries) == 1
    assert summaries[0]["file_name"] == path.name
    assert not path.exists()
    backup = Path(summaries[0]["file_backup_path"])
    assert backup.is_file()
    assert backup.read_bytes() == encrypted_contents
    assert summaries[0]["file_info"]["inspection_status"] == "skipped"

    # 검증 4: OUTPUT 호출 없이도 최종 로그가 한 건 저장되고 JVM은 유지된다.
    spooled = stack.spooled_documents(message_id)
    assert len(spooled) == 1
    assert spooled[0]["cso"] == indexed["cso"]
    assert spooled[0]["output_text"] == expected_prompt
    children = psutil.Process(stack.server.pid).children(recursive=True)
    assert tuple(p.pid for p in children if p.name() == "java") == stack.jvm_pids
    (stack.artifacts / f"{message_id}.json").write_text(
        json.dumps(
            {"request": body, "response": decision, "opensearch": hit},
            ensure_ascii=False,
            indent=2,
        )
    )


def test_unresponsive_cso_module_blocks_request_and_next_request_recovers(
    pipeline_stack,
    tmp_path,
):
    """실제 JVM 응답 중단 시 오류로 차단하고 다음 요청에서 JVM을 재기동한다."""
    stack = pipeline_stack
    message_id = "cso-module-unavailable-" + uuid.uuid4().hex

    # 준비: 정상이라면 모두 허용되어야 하는 O 등급 파일 두 개를 만든다.
    # 파일 오류가 아닌 모듈 장애로 인해 전체 결과가 error가 되는지 확인한다.
    attachments = []
    original_contents = {}
    for index in range(2):
        path = write_docx(tmp_path / f"open-{index}.docx", 3)
        attachments.append({"id": str(path), "name": path.name})
        original_contents[path.name] = path.read_bytes()
    body = {
        "message_id": message_id,
        "prompt": "정상 O 등급 첨부파일을 요약해 주세요.",
        "filter_list": ["input_filter"],
        "attachments": attachments,
        "email": "module-fault@example.invalid",
        "user_id": "module-fault-user",
        "client_host": "127.0.0.1",
        "ai_service": 1,
    }

    # 장애 주입: 테스트 Pipeline이 소유한 CSO JVM만 일시 정지한다.
    # 응답이나 판정을 mock하지 않고 실제 프로세스가 응답하지 않는 상황을 재현한다.
    children = psutil.Process(stack.server.pid).children(recursive=True)
    jvms = [child for child in children if child.pid in stack.jvm_pids]
    assert len(jvms) == 1
    frozen = jvms[0]
    frozen.suspend()
    try:
        response = stack.api.post("/v1/filter/multiple_filter", json=body)
    finally:
        # 실패한 테스트에서도 정지된 자식 프로세스를 남기지 않는다.
        try:
            frozen.resume()
        except psutil.NoSuchProcess:
            pass

    # 검증 1: 요청 타임아웃을 API 오류나 허용으로 넘기지 않고 차단 안내로 반환한다.
    assert response.status_code == 200
    decision = response.json()["final_decision"]
    expected_prompt = (
        "[AIVAX] CSO 등급 차단\n\n"
        "CSO 등급 확인에 실패한 첨부파일이 있어 요청을 차단했습니다."
    )
    assert decision == {
        "action": "block",
        "action_code": 1,
        "message_id": message_id,
        "masked_contents": "",
        "block_message": expected_prompt,
    }
    assert not psutil.pid_exists(frozen.pid)

    # 검증 2: 원래 O 등급이어도 이번 조회는 실패했으므로 모든 파일이 error다.
    hit = stack.indexed_document(message_id)
    indexed = hit["_source"]
    assert hit["_id"] == message_id
    assert indexed["cso"] == {
        "status": "error",
        "files": [
            {
                "file_name": item["name"],
                "file_path": item["id"],
                "grade": None,
                "status": "error",
            }
            for item in attachments
        ],
    }
    assert indexed["mode"] == "block"
    assert indexed["output_text"] == expected_prompt
    assert indexed["prompt"] == body["prompt"]
    summaries = [
        summary
        for detection in indexed["filter_detect"]
        for summary in detection.get("file_summary", [])
    ]
    assert len(summaries) == len(attachments)
    assert {summary["file_name"] for summary in summaries} == set(original_contents)
    for item in attachments:
        assert not Path(item["id"]).exists()
    for summary in summaries:
        backup = Path(summary["file_backup_path"])
        assert backup.read_bytes() == original_contents[summary["file_name"]]
    spooled = stack.spooled_documents(message_id)
    assert len(spooled) == 1
    assert spooled[0]["cso"] == indexed["cso"]
    assert spooled[0]["output_text"] == expected_prompt

    # 검증 3: 다음 정상 요청에서는 새 JVM으로 복구해 O 등급을 다시 허용한다.
    recovered_id = "cso-module-recovered-" + uuid.uuid4().hex
    recovered_path = write_docx(tmp_path / "recovered-open.docx", 3)
    recovered_body = {
        **body,
        "message_id": recovered_id,
        "attachments": [{"id": str(recovered_path), "name": recovered_path.name}],
    }
    recovered = stack.api.post("/v1/filter/multiple_filter", json=recovered_body)
    recovered.raise_for_status()
    recovered_decision = recovered.json()["final_decision"]
    assert recovered_decision["action_code"] == 0
    assert recovered_decision["action"] == ""
    assert recovered_decision["block_message"] == ""
    children = psutil.Process(stack.server.pid).children(recursive=True)
    recovered_pids = tuple(child.pid for child in children if child.name() == "java")
    assert len(recovered_pids) == 1
    assert recovered_pids != stack.jvm_pids
    # 이후 테스트에서는 장애 복구 후 JVM을 재사용 기준으로 삼는다.
    stack.jvm_pids = recovered_pids

    # 복구 요청은 정상 INPUT/OUTPUT 흐름으로 최종 로그까지 저장되어야 한다.
    output = stack.api.post(
        "/v1/filter/output_filter",
        json={
            "message_id": recovered_id,
            "llm_output": "recovered integration summary",
            "email": body["email"],
            "user_id": body["user_id"],
            "client_host": body["client_host"],
            "ai_service": 1,
        },
    )
    output.raise_for_status()
    recovered_hit = stack.indexed_document(recovered_id)
    assert recovered_hit["_source"]["cso"] == {
        "status": "open",
        "files": [
            {
                "file_name": recovered_path.name,
                "file_path": str(recovered_path),
                "grade": 3,
                "status": "tagged",
            }
        ],
    }
    assert recovered_hit["_source"]["mode"] != "block"
    assert recovered_hit["_source"]["output_text"] == "recovered integration summary"
    recovered_spool = stack.spooled_documents(recovered_id)
    assert len(recovered_spool) == 1
    assert recovered_spool[0]["cso"] == recovered_hit["_source"]["cso"]
    (stack.artifacts / f"{message_id}.json").write_text(
        json.dumps(
            {
                "request": body,
                "response": decision,
                "opensearch": hit,
                "recovered_request": recovered_body,
                "recovered_response": recovered_decision,
                "recovered_opensearch": recovered_hit,
                "old_jvm_pid": frozen.pid,
                "recovered_jvm_pids": recovered_pids,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
