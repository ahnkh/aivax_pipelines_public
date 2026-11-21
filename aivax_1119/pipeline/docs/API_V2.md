# Pipeline multiple/output V2

요청·응답 DTO 원본:

- [INPUT V2 DTO](../src/controllers/input_analysis/v2/dto.py)
- [OUTPUT V2 DTO](../src/controllers/output_analysis/v2/dto.py)
- [공통 V2 오류 DTO](../src/controllers/http_error_v2.py)

실행 앱의 `/docs`, `/openapi.json`에서 V2 스키마를 확인할 수 있다.
기존 정적 `swagger/openapi.json`은 V1 명세이며, V2는 DTO와 route에서 생성한다.

V2는 기존 분석·CSO·동의·로그 병합 유스케이스를 공유한다.
필터 실행 순서와 action 값은 현재 구현을 따른다.

## 요청 DTO

### POST /v2/filter/multiple_filter

`InputAnalysisRequestV2`:

| 필드 | 필수 | 기본값·의미 |
| --- | --- | --- |
| `prompt` | 예 | 검사 원문. 첨부파일만 보내면 `""`. 샘플 프롬프트 기본값 없음 |
| `message_id` | 예 | INPUT/OUTPUT 연결 ID. 앞뒤 공백 제거 후 빈 값은 422. 자동 생성하지 않음 |
| `filter_list` | 아니오 | secret_filter, file_block_filter, slm_filter, secuxper_filter |
| `user_name` | 아니오 | `""`. 사용자 이름. null은 빈 문자열로 변환하고 로그의 `user.name`에 저장 |
| `email` | 아니오 | `""`. 정책 적용·로그 이메일 |
| `ai_service` | 아니오 | `0`. 서비스 식별값. 1=GPT, 2=Gemini, 3=Claude |
| `client_host` | 아니오 | `""`. 사용자 IP. 유효한 IP로 정리하고 잘못된 값은 빈 문자열 |
| `attachments` | 아니오 | `[]`. AttachmentV2 목록 |
| `event_time` | 아니오 | 요청 시점의 UTC 현재 시각. `YYYY-MM-DDTHH:mm:ss.ssssssZ` |
| `user_consent` | 아니오 | `false`. 아래 명시한 값만 불리언으로 정리 |

`encoding`, `session_id`, `user_id`, `debug`는 제거했다.
공개 `filter_list`에서는 `input_filter`를 허용하지 않는다. 보내면 422다.
빈 목록·null로 검사를 생략해도 로그는 처리한다. 기존 use case는 수정하지 않고,
mapper가 내부 로그 요청 신호를 추가해 INPUT/OUTPUT 병합을 유지한다.
`event_time` 생략·null·빈 문자열은 요청 DTO를 검증할 때마다 현재 시각으로 채운다.
클라이언트가 제공한 시각 문자열은 기존처럼 보존한다.

INPUT의 `user_consent` 허용값:

| 정리 결과 | JSON 값 | 문자열 값 |
| --- | --- | --- |
| `true` | `true`, `1` | `true`, `1`, `on`, `yes`, `enable`, `enabled` |
| `false` | `false`, `0`, `null` | 빈 문자열, `false`, `0`, `off`, `no`, `disable`, `disabled` |

문자열은 앞뒤 공백과 대소문자를 무시한다. 그 외 문자열·숫자·배열·객체는 422다.
실수 `1.0` 등도 허용하지 않는다. use case에는 정리한 bool만 전달한다.

AttachmentV2의 id, name, size, mime_type은 유지한다.
파일 크기와 MIME 타입은 CSO 차단 시 메타데이터 대체값으로도 사용한다.

### POST /v2/filter/output_filter

`OutputAnalysisRequestV2`:

| 필드 | 필수 | 기본값·의미 |
| --- | --- | --- |
| `message_id` | 예 | INPUT과 같은 ID. 앞뒤 공백 제거 후 빈 값은 422 |
| `llm_output` | 예 | 실제 AI 응답. 빈 문자열 허용 |
| `user_id` | 아니오 | `""`. 출력 로그 사용자 ID |
| `email` | 아니오 | `""`. 출력 로그 이메일 |
| `ai_service` | 아니오 | `0`. 서비스 식별값 |
| `client_host` | 아니오 | `""`. 출력 로그 IP. 기존 출력과 같이 원본 값 전달 |
| `user_consent` | 아니오 | `""`. 기존 bool/문자열 동의 값 유지 |

`session_id`, `debug`는 제거했다.
V2 요청과 첨부파일 DTO는 추가 필드를 허용하지 않는다.
제거된 필드나 알 수 없는 필드를 보내면 HTTP 422를 반환한다.
OUTPUT의 기존 nullable 필드와 bool/문자열 동의 해석은 유지한다.

## 성공 응답

multiple은 HTTP 200과 `InputAnalysisResponseV2`를 반환한다.

```json
{
  "cso": null,
  "final_decision": {
    "action": "masking",
    "masked_contents": "내 비밀번호는 [AIVAX MASKING]",
    "block_message": "",
    "message_id": "msg-001"
  }
}
```

action은 accept, block, masking, failed 또는 빈 문자열이다.
미탐지와 Mirror 접수의 빈 action도 현재 동작대로 유지한다.
CSO 선행 차단은 block으로 반환하고, 안내·masked_contents도 기존 계약을 따른다.

INPUT V2는 debug를 받지 않으며 filter_result, log_evidence도 응답하지 않는다.
Inline/Mirror 모두 `cso`와 `final_decision`을 반환한다. use case의 내부 로그 근거는 유지한다.

`cso`는 V1과 같은 상태와 안내를 반환한다. CSO 비활성, Mirror 또는 실제 첨부 없음으로
검사를 수행하지 않으면 `null`이다. 파일별 결과는 응답에 추가하지 않고 분석 로그에 유지한다.

```json
{
  "cso": {
    "status": "blocked",
    "message": "첨부파일의 CSO 등급이 C 또는 S입니다. 사내 CSO 등급 변경 절차에 따라 전송 가능한 등급으로 변경한 후 다시 시도해 주세요."
  },
  "final_decision": {
    "action": "block",
    "masked_contents": "",
    "block_message": "CSO 차단 안내",
    "message_id": "msg-001"
  }
}
```

`cso.status`는 `open`, `blocked`, `untagged`, `error`, `skipped` 중 하나다.
`cso.message`는 해당 안내 문구이며 모든 파일을 스킵하면 빈 문자열이다.
Fasoo의 MIME 기준 미지원과 MarkAny의 `UNSUPPORTED`는 모두 `error`로 분류한다.
미지원 스킵 또는 오류 스킵이 켜져 있으면 해당 파일은 `skipped`가 된다.
등급 통과 후 일반 필터가 차단하면 `cso.status=open`을 유지하고 최종 action만 `block`이다.
집계와 안내 규칙은 [CSO 응답 계약](API_JSON_STRUCTURE.md#cso)을 따른다.

공통 envelope의 api_code, api_message_code, api_command, completed_date와
final_decision의 action_code를 제거했다.

output은 **HTTP 204, 빈 본문**을 반환한다. 응답 DTO와 out_result는 없다.
204는 최종 OpenSearch 반영 완료를 보장하지 않는다.
일반 출력은 기존 병합 실패 반환값 처리를 유지하고, 동의 출력 저장 실패는 503이다.

## 오류 응답

HTTP 상태와 별도로 `FilterErrorResponseV2`의 기계 판독용 코드·설명을 제공한다.

```json
{
  "detail": {
    "code": "unsupported_filter",
    "message": "unsupported input inspector: unknown_filter"
  }
}
```

| HTTP 상태 | detail.code | 조건 |
| --- | --- | --- |
| 404 | unsupported_filter | 지원하지 않는 필터 이름 |
| 404 | processor_unavailable | 필요한 처리기를 사용할 수 없음 |
| 409 | consent_log_not_found | 동의 원본 로그 예외가 controller에 전파됨 |
| 503 | log_delivery_failed | 재시도 가능한 로그 저장·전달 실패 |

HTTP 422는 FastAPI 기본 검증 오류 배열(detail)을 사용한다.
처리하지 않은 서버 예외의 HTTP 500은 위 DTO를 보장하지 않는다.
