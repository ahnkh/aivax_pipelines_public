# `multiple_filter` / `output_filter` JSON 구조

Swagger 조회·OpenAPI 파일: [swagger/README.md](swagger/README.md).

필드별 타입·기본값·허용값·생략 조건과 오류 응답은
[API_REFERENCE.md](API_REFERENCE.md)를 기준으로 확인한다.
이 문서는 API JSON과 분석 로그·최종 저장 문서의 모양을 예제로 설명한다.

이 문서는 다음 두 API의 현재 JSON 계약을 정리한다.

- `POST /v1/filter/multiple_filter`
- `POST /v1/filter/output_filter`

두 API의 요청에는 같은 `message_id`를 사용해야 한다. Pipeline은 이 값을 기준으로 INPUT 로그와 OUTPUT 로그를 병합한다.

사용자 동의 재전송 계약과 Fluent Bit 파일 설정은
[USER_CONSENT.md](USER_CONSENT.md)를 참고한다.
Inline의 검사 순서와 최종 판정·로그 처리 분기는
[INLINE_MULTIPLE_FILTER.md](INLINE_MULTIPLE_FILTER.md)를 참고한다.

## 1. `multiple_filter` 요청

`POST /v1/filter/multiple_filter`

```json
{
  "filter_list": [
    "input_filter",
    "secret_filter",
    "file_block_filter",
    "slm_filter"
  ],
  "prompt": "사용자 프롬프트",
  "encoding": false,
  "user_id": "user-100",
  "email": "user@example.com",
  "ai_service": 1,
  "client_host": "127.0.0.1",
  "session_id": "session-001",
  "attachments": [
    {
      "id": "/backup/file.pdf",
      "size": 12345,
      "name": "file.pdf",
      "mime_type": "application/pdf"
    }
  ],
  "message_id": "msg-20260918-7",
  "event_time": "2026-09-18T12:00:00",
  "debug": true
}
```

### `filter_list` 값

| 값 | 역할 |
| --- | --- |
| `input_filter` | 입력 분석 로그 저장 요청 |
| `secret_filter` | 정규식 기반 정책 검사 |
| `file_block_filter` | 첨부파일 분석 |
| `slm_filter` | SLM 기반 정책 검사 |
| `secuxper_filter` | SecuXper 정책 검사 (전역 활성화 설정 필요) |

검사는 요청 목록 순서와 무관하게 정규식 → 첨부파일 → SLM → SecuXper 순서로
실행하며, 전역 활성화된 요청 대상만 검사한다. `input_filter`는 로그 저장 요청이다.

`message_id`가 빈 문자열이면 서버가 임의의 ID를 생성한다. INPUT과 OUTPUT을 연결하려면 클라이언트가 직접 같은 값을 전달해야 한다.

## 2. `multiple_filter` 응답

기본 응답은 develop과 동일하게 `final_decision`을 최상위에 반환한다.
`multiple_filter` 성공 응답에는 `out_result` 래퍼가 없다.

```json
{
  "api_code": 200,
  "api_message_code": "success",
  "api_command": "pipeline multiple filter",
  "cso": null,
  "final_decision": {
    "action": "masking",
    "action_code": 2,
    "masked_contents": "[AIVAX MASKING]",
    "block_message": "",
    "message_id": "msg-20260918-7"
  },
  "completed_date": "2026-09-18T12:00:01"
}
```

### `cso`

최상위 `cso`는 `debug=false`를 포함한 모든 정상 응답에 들어간다.
CSO 비활성·첨부파일 없음·Mirror에서는 `null`이다. 경로 없는 첨부 칸만 전달한
경우도 요청 정규화에서 제거되므로 `null`이다.

CSO 결과가 있으면 아래 두 필드만 반환한다.

```json
{
  "cso": {
    "status": "open",
    "message": "첨부파일에 대한 결재가 상신됩니다."
  }
}
```

| 상황 | `cso.status` | `cso.message` |
| --- | --- | --- |
| 미태깅 차단 | `untagged` | 사내 태깅 절차에 따라 등급 지정 후 재요청 안내 |
| C/S 차단 | `blocked` | 사내 등급 변경 절차에 따라 전송 가능한 등급으로 변경 안내 |
| 미지원 형식 차단 | `error` | CSO 등급 조회 불가 안내 |
| O + 일반 필터 차단 | `open` | 결재 상신 + ‘동의합니다’ 입력 후 재전송 안내 |
| O + 허용 | `open` | 결재 상신 안내 |
| 조회 오류 | `error` | CSO 등급 조회 실패 안내 |
| 모두 생략 정책 적용 | `skipped` | 빈 문자열 (`""`) |

상태는 생략 옵션을 적용한 파일별 결과를 집계한다. 여러 파일이 섞이면
`untagged` → `blocked`(C/S) → `error`(미지원 포함) → `open` → `skipped` 순으로 우선한다.
여러 사유가 있어도 가장 우선순위가 높은 상태와 해당 안내만 반환한다.
O와 생략 파일만 있고 O가 있으면 `open`, 모두 생략되면 `skipped`다.
마스킹·빈 판정은 전송 가능한 흐름으로 결재 안내를 반환한다.
최종 판정이 `failed`인 O 파일도 결재 상신 안내를 반환한다.

`cso.status`는 CSO 판정이고 최종 처리 여부는 `final_decision.action`으로 판단한다.
기존 `final_decision.block_message`는 유지한다. 엔진은 새 `cso.message`를 사용해
안내를 표시할 수 있다. 이 응답 필드는 결재 처리 결과를 확인하는 필드가 아니다.
OpenSearch와 `log_evidence.cso`는 기존 `status`/`files` 구조를 유지하며,
API 안내용 `message`를 저장 로그에 추가하지 않는다. API·OpenSearch·
`log_evidence.cso.status`는 같은 집계 함수를 사용하며 위 우선순위를 함께 적용한다.
미태깅 차단은 저장 로그의 요청 전체 상태에도 `untagged`로 기록한다.
파일별 `cso.files[]`에는 모든 조회 결과를 유지한다. 기존 저장 문서는 변경하지 않는다.

Fasoo는 MIME 판독으로, MarkAny는 `UNSUPPORTED` 응답으로 미지원을 구분한다.
미지원 파일의 로그는 `grade=null`, `status=error`이며 외부에 별도 사유 필드는 추가하지 않는다.
미지원 스킵 또는 오류 스킵이 켜져 있으면 해당 파일은 `skipped`가 된다.
미태깅 스킵은 미지원 결과에 적용하지 않는다.

### `final_decision`

| 필드 | 설명 |
| --- | --- |
| `action` | `accept`, `masking`, `block`, `failed`, 또는 미탐지 시 빈 문자열 |
| `action_code` | `accept`/`failed`/빈 문자열=`0`, `block`=`1`, `masking`=`2` |
| `masked_contents` | 마스킹된 프롬프트. 없으면 빈 문자열 |
| `block_message` | 차단 메시지. 없으면 빈 문자열 |
| `message_id` | 요청의 `message_id` |

`debug: true`이고 mirror 모드가 아니면 최상위에 아래 필드가 추가된다.

```json
{
  "filter_result": {
    "secret_filter": {
      "action": "masking",
      "action_code": 2,
      "masked_contents": "[AIVAX MASKING]",
      "block_message": "...",
      "policy_id": "policy-001",
      "policy_name": "Credential policy",
      "target": "prompt",
      "category": "credential",
      "scope": "default"
    }
  },
  "log_evidence": {
    "filter_detect": []
  }
}
```

`filter_result`의 key는 필터의 외부 이름이다. 차단으로 생략된 후속 필터도 빈
기본 결과로 포함될 수 있으며, 요청 제외·전역 비활성화 필터는 포함하지 않는다.
`log_evidence`는 입력 로그가 생성된 경우에만 의미 있는 값을 가지며, 없으면 빈 객체일 수 있다.

## 2-1. `filter_detect` 필터별 구조

`filter_detect`는 공통 필드와 필터별 추가 필드가 함께 저장된다.

### 공통 필드

모든 필터 항목은 아래 필드를 기본으로 가진다.

```json
{
  "filter": "regex",
  "mode": "accept",
  "policy_id": "policy-001",
  "policy_name": "Credential policy",
  "target": "prompt",
  "category": "credential",
  "scope": "default",
  "masked_contents": "",
  "evidence": [],
  "started_at": "2026-09-18T03:00:00Z",
  "completed_at": "2026-09-18T03:00:01Z",
  "duration_ms": 12.4
}
```

`started_at`, `completed_at`, `duration_ms`는 실행 결과에 값이 있을 때만 포함된다. `policy_*` 필드는 정책이 없으면 빈 문자열이다.

### Regex (`filter: "regex"`)

`evidence`에 정규식 매칭 결과가 들어간다.

```json
{
  "filter": "regex",
  "mode": "masking",
  "policy_id": "policy-001",
  "policy_name": "Credential policy",
  "target": "prompt",
  "category": "credential",
  "scope": "default",
  "masked_contents": "[AIVAX MASKING]",
  "evidence": [
    {
      "policy_id": "policy-001",
      "policy_name": "Credential policy",
      "action": "masking",
      "target": "prompt",
      "category": "credential",
      "scope": "default",
      "match": "API_key=...",
      "start": 10,
      "end": 22
    }
  ]
}
```

### Multimodal (`filter: "file block"`)

공통 `evidence`는 기본적으로 빈 배열이고, 파일별 분석 결과가 `file_summary`에 들어간다.

```json
{
  "filter": "file block",
  "mode": "block",
  "policy_id": "file-policy-001",
  "policy_name": "Malware document",
  "target": "file",
  "category": "file",
  "scope": "default",
  "masked_contents": "",
  "evidence": [],
  "file_summary": [
    {
      "action": "block",
      "policy_id": "file-policy-001",
      "policy_name": "Malware document",
      "file_name": "sample.pdf",
      "file_backup_path": "/backup/sample.pdf",
      "name": "Malware document",
      "targets": "file",
      "category": "file",
      "scope": "default",
      "regex_pattern": "suspicious-pattern",
      "file_info": {
        "mime_type": "application/pdf",
        "file_ext": "pdf",
        "size": 12345,
        "hash": "sha256...",
        "chunk": "matched text"
      },
      "id": "file-policy-001",
      "detail_reason": {
        "policy_detail": {
          "name": "Malware document",
          "rule": "suspicious-pattern",
          "regex_flag": 0
        },
        "evidence": [
          {
            "no": 1,
            "page_no": 1,
            "line_no": 3,
            "context": "...",
            "regex_match": "suspicious-pattern"
          }
        ]
      }
    }
  ]
}
```

Multimodal 파일 상태에 따라 `file_info`에 `inspection_status`, `skip_reason`, `hash`, `chunk`가 조건부로 추가된다. 정책 expression이 없으면 `detail_reason`는 생략된다.

### SLM (`filter: "slm"`)

SLM 원본 응답은 `slm_content`에 저장되고, 탐지 항목은 공통 `evidence`에 들어간다.
로그 빌더는 evidence 항목에 정책 ID·이름 별칭인 `id`, `name`도 보완한다.

```json
{
  "filter": "slm",
  "mode": "block",
  "policy_id": "slm-policy-001",
  "policy_name": "Unsafe content",
  "target": "prompt",
  "category": "SLM analysis",
  "scope": "default",
  "masked_contents": "",
  "evidence": [
    {
      "type": "unsafe",
      "value": "탐지된 내용"
    }
  ],
  "slm_content": "{\"has_pii\":true,\"items\":[...] }"
}
```

`slm_content`는 SLM 응답의 원문이며, 공급자 결과에 따라 `null`일 수 있다.
실패한 검사 결과와 차단 때문에 실행하지 않은 기본 SLM 항목에서는 빈 문자열이다.
`evidence`의 세부 key는 SLM 응답 형식에 따라 달라질 수 있지만, 현재 차단/마스킹 메시지 생성에서는 주로 `type`, `value`를 사용한다.

## 3. `output_filter` 요청

`POST /v1/filter/output_filter`

```json
{
  "llm_output": "LLM 응답 결과",
  "user_id": "user-100",
  "email": "user@example.com",
  "ai_service": 1,
  "client_host": "127.0.0.1",
  "session_id": "session-001",
  "message_id": "msg-20260918-7",
  "debug": false
}
```

### `output_filter` 응답

```json
{
  "api_code": 200,
  "api_message_code": "success",
  "api_command": "output api router",
  "out_result": {},
  "completed_date": "2026-09-18T12:00:02"
}
```

OUTPUT API는 분석 결과를 응답하지 않는다. 전달받은 OUTPUT 로그를 `message_id`로 저장하거나 INPUT 로그와 병합한다.

## 4. 최종 bulk/OpenSearch 문서

같은 `message_id`의 INPUT과 OUTPUT이 모두 도착하면 Pipeline은 하나의 NDJSON 문서를 생성한다. 이 문서는 Fluent Bit을 거쳐 OpenSearch의 `aivax_log` 계열 인덱스로 전달된다.

```json
{
  "@timestamp": "2026-09-18T03:00:02Z",
  "schema_version": 1,
  "message_id": "msg-20260918-7",
  "user": {
    "id": "user-100",
    "role": null,
    "email": "user@example.com",
    "uuid": "uuid-001"
  },
  "client_host": "127.0.0.1",
  "client_host_cidr": 2130706433,
  "ai_service_id": 1,
  "ai_service": "GPT",
  "prompt": "사용자 프롬프트",
  "request_date": "2026-09-18T12:00:00",
  "event_time": "2026-09-18T12:00:00",
  "filter_detect": [
    {
      "filter": "regex",
      "mode": "masking",
      "policy_id": "policy-001",
      "policy_name": "Credential policy",
      "target": "prompt",
      "category": "credential",
      "scope": "default",
      "masked_contents": "[AIVAX MASKING]",
      "evidence": [],
      "started_at": "2026-09-18T03:00:00Z",
      "completed_at": "2026-09-18T03:00:01Z",
      "duration_ms": 12.4
    }
  ],
  "mode": "masking",
  "masked_contents": "[AIVAX MASKING]",
  "policy": {
    "policy_id": "policy-001",
    "policy_name": "Credential policy",
    "category": "credential",
    "scope": "default"
  },
  "output_text": "LLM 응답 결과",
  "text_truncated_bytes": 9,
  "model": null,
  "latency_ms": null,
  "tokens": {
    "prompt": null,
    "completion": null,
    "total": null
  },
  "user_id": "user-100",
  "user_email": "user@example.com",
  "user_uuid": "",
  "respose_date": "2026-09-18T12:00:02"
}
```

### 저장 문서의 주요 규칙

- `message_id`는 최상위 필드이며 OpenSearch 문서의 `_id`는 아니다.
- `mode`와 `filter_detect[].mode`는 `accept`, `masking`, `block`, 빈 문자열 중 하나다.
- `filter_detect[].filter`는 `regex`, `file block`, `slm`이다.
- 정책 미탐지 시 mode는 빈 문자열(`""`)이다.
- `output_text`, `model`, `latency_ms`, `tokens` 등 OUTPUT 필드는 OUTPUT API가 도착한 뒤 추가된다.
- `tokens`는 현재 사용량을 계산하지 않으므로 기본적으로 세부 값이 모두 `null`이다.
- `text_truncated_bytes` 필드명은 기존 계약을 따르며, 현재 구현은 일반 응답에서 문자열 길이를 기록한다.
- `ai_service_id`는 INPUT 요청의 값을 우선한다.
- `user`의 비어 있는 식별값은 OUTPUT의 `user_id`, `email`, `user_uuid` 값으로 보강될 수 있다.
- 차단 요청은 OUTPUT API가 없어도 차단 메시지를 `output_text`에 넣어 최종 문서로 전달할 수 있다.
- INPUT 또는 OUTPUT이 먼저 도착하면 반대쪽 로그가 올 때까지 merge store에 보관된다.

## 5. 호출 순서 예시

```text
multiple_filter(message_id=msg-20260918-7)
        ↓
INPUT 로그를 merge store에 저장
        ↓
output_filter(message_id=msg-20260918-7)
        ↓
INPUT + OUTPUT 병합
        ↓
bulk/*.json에 NDJSON 기록
        ↓
Fluent Bit → OpenSearch aivax_log*
```

`output_filter`를 먼저 호출해도 동일하게 동작한다. 이후 같은 `message_id`로 `multiple_filter`가 들어오면 두 문서를 병합한다.

## 6. 오류 응답 형태

지원하지 않는 필터나 OUTPUT 처리 불가 상황에서는 다음 형태를 사용한다.

```json
{
  "detail": {
    "api_command": "pipeline multiple filter",
    "api_code": 404,
    "api_message_code": "Resource Not Found Error",
    "out_result": "오류 내용",
    "completed_date": "2026-09-18T12:00:01"
  }
}
```
