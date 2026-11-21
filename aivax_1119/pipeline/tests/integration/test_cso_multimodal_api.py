"""O 등급 파일의 실제 정책 검사·동의 재전송·OpenSearch 갱신을 검증한다."""

import json
import uuid
from pathlib import Path

import pytest

from scripts.cso_fixtures import write_cso_docx
from src.domain.inspection_input.models.block_phrases import get_block_phrases
from src.domain.inspection_input.models.visible_block_log import logged_block_output

pytestmark = pytest.mark.pipeline_container_integration

POLICY_ID = "cso-integration-file-policy"
POLICY_NAME = "CSO 통합 테스트 민감문서"
SECRET = "CSO_POLICY_SECRET_ABCD1234"
CONSENT_NOTICE = (
    "차단된 요청의 전송에 동의하시면, ‘동의합니다’를 입력한 후 다시 전송해 주세요."
)


def open_file_request(tmp_path, *, matches_policy):
    """동일한 실제 정책에 걸리는 문서와 걸리지 않는 문서를 각각 준비한다."""
    text = (
        SECRET if matches_policy else "Public business document without sensitive data"
    )
    path = write_cso_docx(tmp_path / "open.docx", 3, text=text)
    body = {
        "message_id": "cso-multimodal-" + uuid.uuid4().hex,
        "prompt": "첨부 문서를 요약해 주세요.",
        "filter_list": ["input_filter", "file_block_filter"],
        "attachments": [{"id": str(path), "name": path.name}],
        "email": "multimodal@example.invalid",
        "user_id": "multimodal-test-user",
        "client_host": "127.0.0.1",
        "ai_service": 1,
        "debug": True,
    }
    return body, path, path.read_bytes()


def file_detection(document):
    """CSO 선행 차단용 요약이 아닌 실제 멀티모달 필터의 실행 결과를 찾는다."""
    detections = [
        row for row in document["filter_detect"] if row["filter"] == "file block"
    ]
    assert len(detections) == 1
    detection = detections[0]
    assert detection["started_at"] and detection["completed_at"]
    assert not detection.get("error")
    assert len(detection["file_summary"]) == 1
    summary = detection["file_summary"][0]
    assert summary["file_info"]["hash"]
    return detection, summary


def block_and_index(stack, body, path, original_contents):
    """정책 차단 및 동의 시나리오의 공통 첫 단계를 실제 HTTP로 검증한다."""
    response = stack.api.post("/v1/filter/multiple_filter", json=body)
    assert response.status_code == 200
    result = response.json()
    decision = result["final_decision"]
    assert decision["action"] == "block"
    assert decision["action_code"] == 1
    assert decision["message_id"] == body["message_id"]
    assert POLICY_NAME in decision["block_message"]
    assert decision["block_message"].endswith(CONSENT_NOTICE)
    assert result["filter_result"]["file_block_filter"]["action"] == "block"

    # CSO 자체는 O 등급으로 통과하고, 최종 차단 원인은 멀티모달 정책이어야 한다.
    hit = stack.indexed_document(body["message_id"])
    document = hit["_source"]
    assert document["cso"] == {
        "status": "open",
        "files": [
            {
                "file_name": path.name,
                "file_path": str(path),
                "grade": 3,
                "status": "tagged",
            }
        ],
    }
    assert document["mode"] == "block"
    assert document["policy"]["policy_id"] == POLICY_ID
    assert document["output_text"] == logged_block_output(
        decision["block_message"],
        prompt=body["prompt"],
        file_block=True,
        cso_status="open",
        cso_message=get_block_phrases().cso_guidance("open", blocked=True),
    )
    detection, summary = file_detection(document)
    assert detection["mode"] == "block"
    assert detection["policy_id"] == POLICY_ID
    assert summary["policy_id"] == POLICY_ID
    assert SECRET in summary["file_info"]["chunk"]
    assert not path.exists()
    backup = Path(summary["file_backup_path"])
    assert backup.read_bytes() == original_contents
    assert len(stack.spooled_documents(body["message_id"])) == 1
    return decision, hit, backup


def save_evidence(stack, message_id, **evidence):
    """실제 요청·응답·색인·Management 이벤트를 수동 확인할 수 있도록 남긴다."""
    (stack.artifacts / f"{message_id}.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2)
    )


def test_open_file_policy_match_returns_user_consent_notice(pipeline_stack, tmp_path):
    """O 등급도 파일 정책에 걸리면 차단하고 '동의합니다' 입력을 안내한다."""
    stack = pipeline_stack
    # 준비: 실제 Valkey 정책의 탐지 문자열을 본문에 넣고 Fasoo로 O 태깅한다.
    body, path, contents = open_file_request(tmp_path, matches_policy=True)

    # 실행·검증: 실제 CSO 조회와 본문 정책 검사를 거쳐 동의 안내까지 확인한다.
    # OUTPUT을 보내지 않아도 최초 차단 로그가 OpenSearch에 있어야 한다.
    decision, hit, backup = block_and_index(stack, body, path, contents)
    events = stack.attachment_events(body["message_id"])
    assert len(events) == 1
    assert events[0]["fileBackupPath"] == str(backup)
    assert "user_consent" not in hit["_source"]
    save_evidence(
        stack,
        body["message_id"],
        request=body,
        response=decision,
        opensearch=hit,
        attachment_events=events,
    )


def test_open_file_policy_miss_allows_and_emits_management_event(
    pipeline_stack, tmp_path
):
    """같은 정책에 걸리지 않은 O 문서는 허용하고 보관 파일을 Management에 알린다."""
    stack = pipeline_stack
    # 준비: 정책을 제거하거나 mock하지 않고 탐지 문자열이 없는 O 문서를 만든다.
    body, path, contents = open_file_request(tmp_path, matches_policy=False)

    # 실행·검증 1: 정책 미탐지는 기존 계약상 action="", action_code=0으로 허용한다.
    response = stack.api.post("/v1/filter/multiple_filter", json=body)
    assert response.status_code == 200
    result = response.json()
    decision = result["final_decision"]
    assert decision["action"] == ""
    assert decision["action_code"] == 0
    assert decision["block_message"] == ""
    assert result["filter_result"]["file_block_filter"]["action"] == ""

    # 검증 2: Pipeline 책임인 파일 보관과 실제 Management Stream 발행을 확인한다.
    # 결재권자 지정·결재 처리·사용자 안내 메시지는 이 테스트의 검사 대상이 아니다.
    events = stack.attachment_events(body["message_id"])
    assert len(events) == 1
    assert events[0]["fileName"] == path.name
    assert events[0]["sourceIp"] == body["client_host"]
    backup = Path(events[0]["fileBackupPath"])
    assert not path.exists()
    assert backup.read_bytes() == contents

    # 검증 3: 허용 요청은 OUTPUT까지 병합한 후 Fluent Bit가 OpenSearch에 전달한다.
    output_text = "Policy-free O document summary"
    output = stack.api.post(
        "/v1/filter/output_filter",
        json={
            "message_id": body["message_id"],
            "llm_output": output_text,
        },
    )
    output.raise_for_status()
    hit = stack.indexed_document(body["message_id"])
    document = hit["_source"]
    assert document["cso"]["status"] == "open"
    assert document["cso"]["files"][0]["grade"] == 3
    assert document["mode"] == ""
    assert document["output_text"] == output_text
    assert document["policy"]["policy_id"] == ""
    detection, summary = file_detection(document)
    assert detection["mode"] == ""
    assert summary["policy_id"] == ""
    assert Path(summary["file_backup_path"]) == backup
    assert len(stack.spooled_documents(body["message_id"])) == 1
    save_evidence(
        stack,
        body["message_id"],
        request=body,
        response=decision,
        opensearch=hit,
        attachment_events=events,
    )


def test_open_file_policy_block_then_user_consent_updates_same_opensearch_document(
    pipeline_stack,
    tmp_path,
):
    """정책 차단 후 동의하면 보관본을 CSO 재조회하고 기존 색인 문서를 갱신한다."""
    stack = pipeline_stack
    # 준비: 첫 번째와 같은 정책 차단을 실제로 만들고 원본 문서·색인 위치를 보관한다.
    body, path, contents = open_file_request(tmp_path, matches_policy=True)
    decision, original_hit, backup = block_and_index(stack, body, path, contents)
    original = original_hit["_source"]
    assert not path.exists()
    assert len(stack.attachment_events(body["message_id"])) == 1

    # 실행: userConsent의 Pipeline 공개 필드명은 user_consent다.
    # 같은 ID·원문·첨부 경로를 보내면 이미 이동한 파일의 보관 경로로 CSO를 재조회한다.
    consent_body = {**body, "user_consent": True}
    resumed = stack.api.post("/v1/filter/multiple_filter", json=consent_body)
    assert resumed.status_code == 200
    resumed_result = resumed.json()
    assert resumed_result["final_decision"]["action"] == "accept"
    assert resumed_result["final_decision"]["action_code"] == 0
    assert resumed_result["final_decision"]["block_message"] == ""
    assert resumed_result["filter_result"] == {}

    # 동의 입력은 Valkey에서 출력과 병합을 기다린다. 실제 AI 응답을 OUTPUT에 보내야
    # .json.consent 갱신 파일을 거쳐 기존 OpenSearch 문서가 완성된다.
    output_text = "Actual AI answer after consenting to the blocked O document"
    output = stack.api.post(
        "/v1/filter/output_filter",
        json={
            "message_id": body["message_id"],
            "llm_output": output_text,
        },
    )
    output.raise_for_status()

    # 검증 1: 최초 차단 문서를 다시 읽은 것만으로 통과하지 않도록 갱신값을 기다린다.
    # 새 문서를 만드는 대신 원래 날짜 인덱스의 같은 _id를 부분 갱신해야 한다.
    updated_hit = stack.indexed_document(
        body["message_id"],
        expected_fields={
            "user_consent": "on",
            "output_text": output_text,
        },
    )
    updated = updated_hit["_source"]
    assert updated_hit["_id"] == original_hit["_id"] == body["message_id"]
    assert updated_hit["_index"] == original_hit["_index"]
    assert updated["@timestamp"] == original["@timestamp"]

    # 검증 2: 동의로 최초 탐지 이력·정책·사용자·차단 판정을 지우지 않는다.
    # mode=block은 최초 기록이고 user_consent=on이 후속 동의를 나타낸다.
    for field in ("mode", "filter_detect", "policy", "prompt", "user"):
        assert updated[field] == original[field]
    assert updated["mode"] == "block"
    assert updated["cso"] == {
        "status": "open",
        "files": [
            {
                "file_name": path.name,
                "file_path": str(backup),
                "grade": 3,
                "status": "tagged",
            }
        ],
    }
    assert backup.read_bytes() == contents
    assert "_consent_log_timestamp" not in updated

    # 검증 3: 최초 create는 한 건을 유지하고 동의 update 파일이 별도로 생성된다.
    # 재동의 때문에 보관 이벤트를 중복 발행하지 않아야 한다.
    assert len(stack.spooled_documents(body["message_id"])) == 1
    patches = stack.spooled_documents(body["message_id"], consent=True)
    assert len(patches) == 1
    assert patches[0]["user_consent"] == "on"
    assert patches[0]["output_text"] == output_text
    assert patches[0]["cso"] == updated["cso"]
    events = stack.attachment_events(body["message_id"])
    assert len(events) == 1
    save_evidence(
        stack,
        body["message_id"],
        request=body,
        response=decision,
        original_opensearch=original_hit,
        consent_request=consent_body,
        consent_response=resumed_result,
        updated_opensearch=updated_hit,
        consent_spool=patches,
        attachment_events=events,
    )
