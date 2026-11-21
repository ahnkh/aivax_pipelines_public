# Pipeline INPUT/OUTPUT API 필드 사전

Swagger 조회·OpenAPI 파일·재생성 방법: [swagger/README.md](swagger/README.md).

develop의 과거 계약은 [API_REFERENCE_DEVELOP.md](API_REFERENCE_DEVELOP.md)를 참고한다.

확인일: 2026-10-18 · 브랜치: `refact/khfc-mirror-inspect/env` · 해시: `019de127`.

Pipeline의 주요 API인 multiple_filter, output_filter 두 api의 명세를 작성.

## 1. 값과 필드를 읽는 기준

| 표기        | 의미                                                                   |
| ----------- | ---------------------------------------------------------------------- |
| 기본값      | JSON에서 해당 필드를 생략했을 때 DTO에 들어가는 값                     |
| `nullable`  | 명시적으로 JSON `null`을 보내도 요청 모델이 허용하는지 여부            |
| `""`        | 빈 문자열. 필드가 없는 것, `null`, 검사 실패와는 구분해야 함           |
| `optional`  | 특정 모드·디버그·탐지 상황에서만 나타나며 생략될 수 있음               |
| 정책 문자열 | 정책 데이터에서 가져오는 값. 문서의 예시를 고정 enum으로 해석하지 않음 |

두 요청 DTO 모두 모든 필드에 기본값이 있다.
다만 **요청의 `message_id`는 공백이 아닌 값이 필요**하다.
필드 타입이 틀리면 HTTP 422가 발생한다.

문서의 타입은 JSON에서 사용할 타입이며, Pydantic의 일부 타입 변환까지 API 사용법으로
보장하지 않는다. 정의되지 않은 추가 키는 기본적으로 무시한다.
예를 들어 `userConsent`는 공개 필드가 아니며 `user_consent`를 사용해야 한다.

**HTTP 200 / `api_message_code="success"`는 검사가 전부 성공했다는 뜻이 아니다.**
차단·마스킹·검사 실패도 body에 담긴다. `action_code=0`에는 `failed`도
포함되므로 반드시 `final_decision.action`을 함께 읽는다.

## 2. 입력 요청: `multiple_filter` API

### 2.1 요청 필드

| 필드           | JSON 타입          | 기본값                | null 허용 | 의미와 현재 처리                                                                                                                  |
| -------------- | ------------------ | --------------------- | --------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `message_id`   | 문자열             | `""`                  | 아니오    | INPUT/OUTPUT 및 동의 재전송의 연결 키. 앞뒤 공백 제거. 일반 입력에서 비면 32자리 hex 문자열을 생성(이 로직은 레거시. 제거해야함.) |
| `filter_list`  | 문자열 배열        | 아래 다섯 이름 전체   | 예        | 어떤 필터를 검사할 건지 정함. 기본값은 활성화된 전체 필터 검사(활성화 여부는 `/etc/aivax/mode.json`파일에 작성됨)                 |
| `prompt`       | 문자열             | 아래 명시한 예제 문장 | 아니오    | 검사해야하는 문자열 원문. 빈 문자열 허용하지만 생략하면 빈 문자열이 아니라 예제 문장이 사용되니 항상 문자열이 존재해야함          |
| `encoding`     | 불리언             | `false`               | 예        | [레거시] 존재하지만 현재 사용하지 않음. `true`여도 Base64 해독하지 않음                                                           |
| `user_id`      | 문자열             | `""`                  | 예        | [레거시] 사용자 식별자. null은 입력 mapper에서 `""`로 변환. 사용자 동기화·정책 알림 등에도 사용하지만 필요없어서 제거 예정.       |
| `user_name`    | 문자열             | `""`                  | 예        | 사용자 이름. null은 `""`로 변환하고 로그의 `user.name`에 저장                                                                   |
| `email`        | 문자열             | `""`                  | 예        | prompt를 보내는 사용자의 email. null은 `""`로 변환. ""은 익명 사용자로 취급.                                                      |
| `ai_service`   | 정수               | `0`                   | 예        | AI 서비스 식별자. null은 `0`으로 변환. 매핑은 2.4 참고                                                                            |
| `client_host`  | 문자열             | `""`                  | 예        | 클라이언트 IPv4/IPv6. 앞뒤 공백 제거 후 표준화. 잘못된 IP·빈 값·null은 `""`로 변환                                                |
| `session_id`   | 문자열             | `""`                  | 예        | [레거시] 세션 ID. INPUT/OUTPUT 로그 병합 키는 이 값이 아니라 `message_id`를 사용함.                                               |
| `attachments`  | 객체 배열          | `[]`                  | 예        | 검사할 첨부파일 목록. `filter_list`에 멀티모달 검사 넣어야함. null은 빈 목록으로 변환. 객체 필드는 2.3 참고                       |
| `event_time`   | 문자열             | `""`                  | 예        | 호출자가 제공하는 이벤트 시각. ISO 형식 사용 권장. DTO가 날짜 형식을 검증하지 않음. 빈 값·null은 로그 생성 시각으로 대체          |
| `user_consent` | 문자열 또는 불리언 | `""`                  | 예        | 사용자 동의 플래그. 행안부 요구사항. 허용 문자열과 동작은 2.5 참고                                                                |
| `debug`        | 불리언             | `false`               | 예        | Inline 응답에 `filter_result`, `log_evidence` 추가. null은 false로 취급. Mirror에서는 true여도 추가하지 않음                      |

`prompt`의 현재 생략 기본값은 다음 문자열이다.

```text
내 API key는 API_key=sk-1234567-0000-abdcdef 인데 이걸로 어떻게 OpenAI 로 KEY를 전달하는지 예제를 알려주세요
```

### 2.2 `filter_list` 값

| 값                  | 역할                                           | 디버그 `filter_result` 키 | 로그 `filter_detect[].filter` |
| ------------------- | ---------------------------------------------- | ------------------------- | ----------------------------- |
| `input_filter`      | [레거시] 입력 로그 저장 요청. 검사 필터가 아님 | 항목 없음                 | 항목 없음                     |
| `secret_filter`     | 정규식 정책 검사                               | `secret_filter`           | `regex`                       |
| `file_block_filter` | 첨부파일 분석 및 백업                          | `file_block_filter`       | `file block`                  |
| `slm_filter`        | SLM 정책 검사                                  | `slm_filter`              | `slm`                         |
| `secuxper_filter`   | SecuXper 정책 검사                             | `secuxper_filter`         | `secuxper`                    |

생략하면 위 다섯 이름이 기본값이다. 실행 순서는 요청 배열 순서와 무관하게
**정규식 → 첨부파일 → SLM → SecuXper**다. 중복 이름은 한 번만 실행한다.
요청 목록에 있고 `mode.json`의 해당 필터가 활성화된 경우에 실행한다.
알 수 없는 이름이 있으면 검사 전에 HTTP 404로 거부한다.
CSO 선행 검사는 `filter_list`와 독립적이므로 빈 목록도 CSO 검사를 끄지 않는다.

`input_filter`를 빼면 일반 허용·마스킹·실패의 입력 로그 저장을 생략한다. (레거시)
일반 차단 로그와 동의 입력 로그는 이 값이 없어도 처리한다.

### 2.3 `attachments[]` 필드

| 필드        | JSON 타입 | 기본값 | null 허용 | 의미                                                                                                          |
| ----------- | --------- | ------ | --------- | ------------------------------------------------------------------------------------------------------------- |
| `id`        | 문자열    | `""`   | 예        | Pipeline에서 접근할 수 있는 파일 경로. null은 `""`로 변환                                                     |
| `size`      | 정수      | `0`    | 예        | 호출자가 제공한 파일 크기(바이트). null은 `0`으로 변환. 실제 파일 처리에서는 읽은 메타데이터가 사용될 수 있음 |
| `name`      | 문자열    | `""`   | 예        | 파일 이름. null은 `""`로 변환                                                                                 |
| `mime_type` | 문자열    | `""`   | 예        | 호출자가 제공한 MIME 타입. null은 `""`로 변환. 실제 조회 결과와 다를 수 있음                                  |

필드 생략을 DTO가 허용하는 것과 실제 파일 분석·보관이 가능한 것은 별개다.
CSO·파일 분석·보관 결과에 따라 차단·실패·빈 상세 항목이 발생할 수 있다.

### 2.4 `ai_service` 값

요청 값은 Management의 서비스 ID다. Pipeline은 Valkey의
`pipeline:ai-service:catalog:v1`에 배포된 카탈로그에서 INPUT/OUTPUT 로그의
`ai_service` 표시명을 읽는다. Pipeline 코드에 고정된 ID와 이름 목록은 없다.
예를 들어 Management가 `9=GitHub Copilot`, `10=openapi.copilot`을 배포하면
두 서비스를 각각의 ID와 이름으로 기록한다.

숫자 원본은 로그의 `ai_service_id`에 보존한다. 카탈로그에 없는 ID의 표시명은
빈 문자열이며 DTO는 enum으로 ID를 제한하지 않는다. INPUT의 생략·null은 `0`으로
변환하고 OUTPUT의 null은 유지한다. OUTPUT null의 표시명은 빈 문자열이다.
카탈로그 갱신·장애 처리와 초기 배포 순서는
[실행 정책 snapshot](../../../docs/300-Data-model/310-valkey/312-string/312.1-policy-snapshots.md)을 따른다.

### 2.5 `user_consent` 값과 효과

| 요청 값                                                         | 보정 후                       |
| --------------------------------------------------------------- | ----------------------------- |
| JSON `true`                                                     | true                          |
| 문자열 `on`, `1`, `true`, `yes`, `enable`, `enabled`            | true. 대소문자·앞뒤 공백 무시 |
| 생략, null, JSON `false`, `""`, `off`, `false` 및 나머지 문자열 | false                         |

Inline에서 user_consent가 true라면 **이 전의 요청과 같은 `message_id`와 원문을 보내야 한다.**
ID가 비면 HTTP 422다. 프롬프트에 ‘동의합니다’라고 적는 것만으로 동의가 되지 않는다.
CSO 차단이 없으면 일반 필터를 전부 생략하고 `accept/0`을 반환하며 동의 입력 로그를 저장한다.
기존 차단 로그가 없다는 이유만으로 입력을 409로 거부하지는 않는다.

현재 구현에서 `AIVAX_PIPELINE_USER_CONSENT_GLOBAL_BYPASS_ENABLED`와 필터별
`USER_CONSENT_*_ENABLED`는 동의 요청 진입을 막는 스위치가 아니다.
전역 옵션과 차단한 필터 옵션이 모두 켜지면 일반 차단 메시지에 동의 안내를 추가한다.
전역 옵션은 최초 일반 차단의 필터별 차단 문구 저장과 로그 전달 실패 처리에도 관여한다.
CSO 차단은 동의·SSL 우회로 허용하지 않는다. Mirror에서는 동의 분기를 적용하지 않는다.
로그 갱신·보존 계약은 [USER_CONSENT.md](USER_CONSENT.md)를 참고한다.

## 3. 입력 성공 응답

### 3.1 최상위 필드

| 필드               | JSON 타입 | 존재 조건             | 값과 의미                                                                  |
| ------------------ | --------- | --------------------- | -------------------------------------------------------------------------- |
| `api_code`         | 정수      | 항상                  | `200`. API 성공 envelope 코드. 보안 허용 판정이 아님                       |
| `api_message_code` | 문자열    | 항상                  | `success`. `ok`, `pass`가 아님                                             |
| `api_command`      | 문자열    | 항상                  | `pipeline multiple filter`                                                 |
| `final_decision`   | 객체      | 항상                  | 호출자가 사용할 최종 판정. 세부 필드는 3.2 참고                            |
| `completed_date`   | 문자열    | 항상                  | 응답 조립 시각. 서버 로컬 시간 `YYYY-MM-DDTHH:mm:ss`. UTC 표시·오프셋 없음 |
| `filter_result`    | 객체      | `debug=true`인 Inline | 검사별 공통 응답. 없을 때와 `{}`일 때를 구분                               |
| `log_evidence`     | 객체      | `debug=true`인 Inline | 입력 분석 로그 근거. 생성된 로그가 없으면 `{}`                             |

`debug=false` 또는 Mirror이면 디버그 두 필드는 **생략**된다. null로 반환하지 않는다.
`out_result`, `effective_decision`, `user_consent_log_update`는 이 성공 응답에 없다.
`user_consent`도 독립된 최상위 응답 필드가 아니며 동의 로그 안에 기록된다.

### 3.2 `final_decision` 필드와 값

다음 다섯 필드는 항상 존재하고 null이 아니다.

| 필드              | JSON 타입 | 가능한 값·의미                                                                                             |
| ----------------- | --------- | ---------------------------------------------------------------------------------------------------------- |
| `action`          | 문자열    | `accept`, `block`, `masking`, `failed`, `""`. 현재 브랜치는 `allow`, `ok`, `pass`를 반환하지 않음          |
| `action_code`     | 정수      | `block=1`, `masking=2`, `accept`·`failed`·`""`는 모두 `0`                                                  |
| `masked_contents` | 문자열    | 마스킹된 프롬프트 또는 공급자의 대체 콘텐츠. 없으면 `""`. CSO 차단이면 항상 `""`                           |
| `block_message`   | 문자열    | 최종 선택한 차단/안내 문구. 없으면 `""`. 정규식 마스킹에도 문구가 있을 수 있음. CSO 차단이면 CSO 안내 우선 |
| `message_id`      | 문자열    | 공백 제거한 요청 ID 또는 일반 입력에서 서버가 생성한 ID. OUTPUT·동의 재전송에 같은 값을 사용               |

| `action`  | 의미                                           | 대표 발생 조건                                        |
| --------- | ---------------------------------------------- | ----------------------------------------------------- |
| `accept`  | 명시적 허용 판정                               | 허용 정책 탐지, 또는 CSO를 통과한 Inline 동의 요청    |
| `block`   | 실제 요청 차단                                 | 일반 필터 차단 또는 CSO 선행 차단                     |
| `masking` | 마스킹 판정                                    | 차단이 없고 마스킹 결과가 선택됨                      |
| `failed`  | 검사 실패가 최종 판정으로 선택됨               | 필터 사용 불가·검사 예외 등. HTTP 200으로 반환 가능   |
| `""`      | 명시적 accept/block/masking/failed가 없는 판정 | 전체 미탐지, 실행 검사 없음, Mirror 즉시 접수 응답 등 |

`""`는 `failed`의 별칭이 아니다. Mirror의 빈 action은 백그라운드 검사가 끝났다는 뜻도 아니다.
`failed/0`을 정상 검사 성공으로 처리할지는 호출자가 정해야 하며, 현재 API가 `block`으로 바꾸지 않는다.
`final_decision`에는 `policy_id`, `policy_name`, 오류 상세 필드가 없다.

### 3.3 조건별 최종 결과

다음 표의 일반 판정은 SSL 우회가 꺼진 경우다.

| 조건                                      | `action / action_code` | 응답 상세                                                             |
| ----------------------------------------- | ---------------------- | --------------------------------------------------------------------- |
| 모든 검사 미탐지, 다른 판정 없음          | `"" / 0`               | 일반 미탐지를 `accept`로 강제하지 않음                                |
| `filter_list=[]` 또는 null, CSO 차단 없음 | `"" / 0`               | `input_filter`만 요청한 경우도 검사 결과가 없으므로 동일              |
| 허용 정책 탐지, 다른 우선 판정 없음       | `accept / 0`           | 명시적 허용                                                           |
| 정규식 차단                               | `block / 1`            | 현재 정규식 inspector는 차단에서 `masked_contents=""`. 차단 문구 반환 |
| 정규식 마스킹                             | `masking / 2`          | 원문 구간 마스킹과 안내 문구 반환                                     |
| SLM·SecuXper 마스킹                       | `masking / 2`          | 위치/탐지값 근거로 원문 마스킹. 공급자 대체 콘텐츠가 사용될 수 있음   |
| 필터 실패만 남고 차단·마스킹 없음         | `failed / 0`           | 실패가 accept보다 우선. HTTP 오류와 구분                              |
| 검사 실패 뒤 다른 필터가 차단             | `block / 1`            | 실패 때문에 나머지 검사를 중단하지 않음                               |
| CSO C/S·미태깅·조회 실패                  | `block / 1`            | 일반 필터 생략, 마스킹 빈 문자열, CSO 차단 안내                       |
| Inline 동의 요청, CSO 차단 없음           | `accept / 0`           | 일반 필터 미실행. 디버그 `filter_result={}`                           |
| Mirror 입력                               | `"" / 0`               | 즉시 접수, CSO·동의 분기 없음. 디버그 필드 생략                       |

일반 판정의 우선순위는 `block > masking > failed > accept > ""`다.
검사는 block에서 중단하며, 미실행 후속 검사에는 빈 결과를 남길 수 있다.
마스킹은 여러 필터의 위치 근거를 병합하고, 근거가 없으면 첫 마스킹 결과를 쓴다.
각 필터는 앞 필터의 마스킹 결과가 아닌 원래 요청을 검사한다.

`AIVAX_PIPELINE_SSL_PROXY_BYPASS_BITMASK`의 값 `1`(`0b01`)은 block 우회,
값 `2`(`0b10`)는 masking 우회이며 `3`은 둘 다 우회다.
우회 대상 결과를 만나면 판정 취합 순회를 끝내므로 일반 우선순위만으로 예측하면 안 된다.
block 우회여도 필터 실행은 block에서 중단한다. 예를 들어 마스킹 후 우회 대상 block을
만나면 앞 마스킹이 최종 결과로 남고, 우회 대상 마스킹을 먼저 만나면 뒤 block을
최종 판정에 반영하지 않을 수 있다. 상세 실행 규칙은 [INLINE_MULTIPLE_FILTER.md](INLINE_MULTIPLE_FILTER.md) 참고.

### 3.4 입력 응답 예제

일반 미탐지 예제다. 차단·마스킹·accept·failed도 같은 최상위 구조를 사용한다.

```json
{
  "api_code": 200,
  "api_message_code": "success",
  "api_command": "pipeline multiple filter",
  "final_decision": {
    "action": "",
    "action_code": 0,
    "masked_contents": "",
    "block_message": "",
    "message_id": "msg-001"
  },
  "completed_date": "2026-10-18T12:00:00"
}
```

## 4. 디버그 응답: 필터 결과와 로그 근거

### 4.1 `filter_result.<필터명>`

정규식·파일·SLM·SecuXper 결과에 다음 아홉 필드가 공통으로 존재한다.
요청 제외·전역 비활성화 필터는 항목 자체가 없다. block 때문에 생략된 후속 필터는
빈 action/문자열과 `action_code=0`인 항목이 생길 수 있다.

| 필드              | JSON 타입 | 값과 의미                                                          |
| ----------------- | --------- | ------------------------------------------------------------------ |
| `action`          | 문자열    | 해당 검사만의 `accept`, `block`, `masking`, `failed`, `""`         |
| `action_code`     | 정수      | 해당 검사의 `block=1`, `masking=2`, 나머지 `0`                     |
| `masked_contents` | 문자열    | 해당 검사에서 생성한 마스킹. 없으면 `""`                           |
| `block_message`   | 문자열    | 해당 검사의 원래 안내문. 최종 응답에 붙는 동의 안내와 다를 수 있음 |
| `policy_id`       | 문자열    | 대표 정책 ID. 없으면 `""`                                          |
| `policy_name`     | 문자열    | 대표 정책 이름. 없으면 `""`                                        |
| `target`          | 문자열    | 정책 대상. 예: `prompt`, `file`. 고정 enum으로 검증하지 않음       |
| `category`        | 문자열    | 정책 분류. 없으면 `""`. 정책 데이터의 값                           |
| `scope`           | 문자열    | 정책 적용 범위. 없으면 `""`. 정책 데이터의 값                      |

이 객체에는 `error`, `evidence`, `file_summary`, `slm_content`, 실행 시각을 넣지 않는다.
그 상세는 `log_evidence.filter_detect[]`에서 확인한다. 로그 저장을 요청하지 않은
실패 등에서는 `log_evidence={}`여서 HTTP 응답만으로 실패 상세를 얻지 못할 수 있다.
최종 차단이 CSO에서 발생하면 `filter_result={}`인 동시에 로그에 파일 보관 항목이 있을 수 있다.

### 4.2 `log_evidence` 공통 필드

이 객체는 응답 시점에 생성한 **입력 로그 근거**다. 최종 INPUT/OUTPUT 병합 문서나
OpenSearch 저장 완료 증명이 아니다. 로그가 생성되지 않았으면 아래 필드 없이 `{}`다.

| 필드                     | JSON 타입        | 값과 의미                                                                                 |
| ------------------------ | ---------------- | ----------------------------------------------------------------------------------------- |
| `@timestamp`             | 문자열           | 로그 시각. UTC ISO 문자열 `...Z`. 동의에서는 확보한 원본 문서 시각을 유지할 수 있음       |
| `message_id`             | 문자열           | 요청 연결 키                                                                              |
| `user.id`                | 문자열           | 입력 요청의 사용자 ID                                                                     |
| `user.name`              | 문자열           | 입력 요청의 사용자 이름. 생략·빈 문자열·null이면 `""`                                      |
| `user.role`              | null             | 현재 입력 로그 빌더는 null로 생성                                                         |
| `user.email`             | 문자열           | 입력 요청 이메일                                                                          |
| `user.uuid`              | 문자열           | 내부 사용자 UUID. 현재 입력 mapper의 기본값은 `""`                                        |
| `client_host`            | 문자열           | 정규화한 IP 또는 `""`                                                                     |
| `client_host_cidr`       | 정수             | IP를 정수로 변환한 값. CIDR 접두 길이가 아님. 변환 불가면 `0`                             |
| `ai_service_id`          | 정수             | 입력 요청의 서비스 ID                                                                     |
| `ai_service`             | 문자열           | 2.4의 표시명 또는 `""`                                                                    |
| `prompt`                 | 문자열           | 원래 입력 프롬프트                                                                        |
| `request_date`           | 문자열           | 제공한 event_time을 사용하거나 현재 UTC 시각. 날짜와 시간 사이의 공백은 `T`로 바꿈        |
| `event_time`             | 문자열           | 앞뒤 공백 제거한 요청 이벤트 시각. 없으면 로그 생성 UTC 시각                              |
| `filter_detect`          | 객체 배열        | 검사별 상세. 실행 없음이면 빈 배열. 이름 매핑은 2.2 참고                                  |
| `mode`                   | 문자열           | 분석 로그의 `accept`, `block`, `masking`, `""`. `failed`는 빈 문자열로 변환               |
| `masked_contents`        | 문자열 또는 null | 최종 분석 마스킹. 없으면 **null**. HTTP `final_decision`의 빈 문자열과 다름               |
| `policy.policy_id`       | 문자열           | 최종 분석 대표 정책 ID 또는 `""`                                                          |
| `policy.policy_name`     | 문자열           | 최종 분석 대표 정책 이름 또는 `""`                                                        |
| `policy.category`        | 문자열           | 대표 정책 분류 또는 `""`                                                                  |
| `policy.scope`           | 문자열           | 대표 정책 범위 또는 `""`                                                                  |
| `cso`                    | 객체, 조건부     | CSO 조회 결과가 있을 때 추가. 4.5 참고                                                    |
| `user_consent`           | 문자열, 조건부   | 허용된 동의 입력 로그에서 `on`                                                            |
| `_consent_log_timestamp` | 문자열, 조건부   | 동의 입력에서 원본 시각을 확보했을 때의 내부 상관 필드. OpenSearch 본문에는 기록하지 않음 |

`completed_date`는 서버 로컬 시각, `@timestamp`는 UTC라 서로 포맷·시간대가 다를 수 있다.
요청 `event_time`에는 날짜 파서 검증이 없으므로 형식이 잘못된 문자열도 로그에 남을 수 있다.

### 4.3 `log_evidence.filter_detect[]`

| 필드                                                      | JSON 타입             | 값과 의미                                                                                                                               |
| --------------------------------------------------------- | --------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `filter`                                                  | 문자열                | `regex`, `file block`, `slm`, `secuxper`                                                                                                |
| `mode`                                                    | 문자열                | 해당 검사의 `accept`, `block`, `masking`, `""`. 실패도 로그에서는 `""`로 기록                                                           |
| `policy_id`, `policy_name`, `target`, `category`, `scope` | 문자열                | 해당 대표 정책의 필드. 없으면 빈 문자열                                                                                                 |
| `masked_contents`                                         | 문자열                | 해당 검사의 마스킹 또는 `""`                                                                                                            |
| `evidence`                                                | 객체 배열             | 정책·탐지 근거. 없으면 `[]`. 객체의 세부 키는 검사 종류에 따라 다름                                                                     |
| `started_at`, `completed_at`                              | 문자열, 조건부        | 실제 실행 시각. UTC ISO `...Z`. 미실행 기본 항목에는 없음                                                                               |
| `duration_ms`                                             | 숫자, 조건부          | 실제 검사 소요 시간(밀리초). 미실행 기본 항목에는 없음                                                                                  |
| `error`                                                   | 객체, 조건부          | 오류가 있을 때 추가. `code`, `message`, 선택적 `http_status`                                                                            |
| `error.code`                                              | 문자열                | 오류 식별자. 예: `INTERNAL_ERROR`, `UPSTREAM_UNAVAILABLE`. 공급자 오류도 있어 전체 고정 enum은 아님                                     |
| `error.message`                                           | 문자열                | 오류 설명                                                                                                                               |
| `error.http_status`                                       | 정수, 조건부          | 외부 검사 서비스 HTTP 상태. multiple API 자체의 HTTP 상태와 다름                                                                        |
| `block_message`                                           | 문자열, 조건부        | 전역 동의 옵션을 켠 일반 차단 등에서 필터별 안내문을 포함할 수 있음                                                                     |
| `file_summary`                                            | 객체 배열, 파일 검사  | 파일별 분석·보관 결과. 실패/미실행 기본 결과에서는 `[]`일 수 있음                                                                       |
| `slm_content`                                             | 문자열 또는 null, SLM | 모델 원본 콘텐츠. 실패 결과는 `""`로 정리. 실행되지 않은 기본 항목도 `""`                                                               |
| `provider`                                                | 문자열, SecuXper      | 현재 정책 실행 서비스는 `secuxperai`로 기록                                                                                             |
| `policy_version`                                          | 문자열, SecuXper      | 검사에 사용한 정책 스냅샷 버전                                                                                                          |
| `skipped`                                                 | 불리언, SecuXper      | 적용할 정책을 선택하지 못했으면 true                                                                                                    |
| `policy_codes`                                            | 문자열 배열, SecuXper | 선택한 정책 코드 목록. block 뒤 미실행 정책도 선택 목록에는 남을 수 있음                                                                |
| `policy_results`                                          | 객체 배열, SecuXper   | 실제 정책별 결과 메타데이터. `policy_code`와 공급자 메타데이터를 포함                                                                   |
| `failed_policies`                                         | 객체 배열, SecuXper   | 실패 정책의 `policy_id`, `policy_name`, `category`, `scope`, `target`, `mode`, 선택적 `error`. 이 중첩 목록의 mode는 `failed`일 수 있음 |

정규식 `evidence[]`에는 `policy_id`, `policy_name`, `action`, `target`, `category`,
`scope`, `match`, `start`, `end`가 들어간다. `start`는 원문 시작 인덱스(포함),
`end`는 끝 인덱스(제외)다. 로그 빌더는 evidence 항목의 정책 별칭 `id`, `name`도 보완한다.
SLM은 공급자별 탐지값(`type`, `value` 등)을 담으며 모든 공급자가 같은 키를 보장하지 않는다.

SecuXper는 한 검사에 여러 정책 실패 항목을 로그로 펼칠 수 있다.
따라서 `filter_detect`를 필터명당 정확히 한 건이라고 가정하지 않는다.
실패를 빈 문자열로 바꾸는 규칙은 `filter_detect[].mode`에 적용되며,
중첩된 `failed_policies[].mode`까지 같은 값으로 바꾸지는 않는다.

### 4.4 `file_summary[]`의 필드

| 필드                                     | JSON 타입         | 값과 의미                                                                                   |
| ---------------------------------------- | ----------------- | ------------------------------------------------------------------------------------------- |
| `action`                                 | 문자열            | 파일별 분석 판정. CSO 선행 차단의 파일 보관 요약은 `""`일 수 있음                           |
| `policy_id`, `policy_name`               | 문자열            | 탐지 정책 ID·이름. 없으면 `""`. CSO 전용 요약에서는 생략                                    |
| `file_name`                              | 문자열            | 파일 이름                                                                                   |
| `file_backup_path`                       | 문자열            | 보관 목적지. 보관되지 않았으면 `""`                                                         |
| `name`, `targets`, `category`, `scope`   | 문자열            | 파일 탐지 정책 필드. 일반 분석 요약에서 생성. CSO 전용 요약에서는 생략                      |
| `regex_pattern`                          | 문자열            | 정책에 매칭한 문자열. 정책의 전체 정규식 표현식과 다름. 없으면 `""`                         |
| `id`                                     | 문자열, 조건부    | 정책이 있을 때의 ID 별칭                                                                    |
| `file_info.mime_type`                    | 문자열            | MIME 타입                                                                                   |
| `file_info.file_ext`                     | 문자열            | 파일 확장자                                                                                 |
| `file_info.size`                         | 정수              | 파일 크기(바이트)                                                                           |
| `file_info.hash`                         | 문자열, 조건부    | 콘텐츠를 검사한 파일의 해시                                                                 |
| `file_info.chunk`                        | 문자열, 조건부    | 파일에서 추출한 콘텐츠 조각                                                                 |
| `file_info.inspection_status`            | 문자열, 조건부    | 예: `skipped`. 모든 파일 요약에 존재하는 필드는 아님                                        |
| `file_info.skip_reason`                  | 문자열, 조건부    | 검사 생략 이유. CSO 선행 차단은 `cso_blocked`. 다른 사유는 정책/파일 처리 결과의 문자열     |
| `detail_reason.policy_detail.name`       | 문자열, 조건부    | 상세 분석 정책 이름                                                                         |
| `detail_reason.policy_detail.rule`       | 문자열, 조건부    | 정책의 정규식 표현식                                                                        |
| `detail_reason.policy_detail.regex_flag` | 정수, 조건부      | 정규식 플래그 값                                                                            |
| `detail_reason.evidence[]`               | 객체 배열, 조건부 | `no`, `page_no`, `line_no`, `context`, `regex_match`로 순번·페이지·줄·문맥·매칭 문자열 표현 |

`detail_reason`는 탐지 정책에 표현식이 있는 경우 생성한다.
실제 JSON 모양과 최종 저장 문서는 [API_JSON_STRUCTURE.md](API_JSON_STRUCTURE.md)를 참고한다.

### 4.5 `log_evidence.cso`

| 필드                | JSON 타입      | 값과 의미                                                                              |
| ------------------- | -------------- | -------------------------------------------------------------------------------------- |
| `status`            | 문자열         | `open`: 모두 tagged O(3), `blocked`: 그 외 비오류 차단, `error`: 조회 오류가 하나 이상 |
| `files`             | 객체 배열      | 첨부파일별 조회 결과                                                                   |
| `files[].file_name` | 문자열         | 파일 이름                                                                              |
| `files[].file_path` | 문자열         | 조회에 사용한 파일 경로. 보관 경로로 치환될 수 있음                                    |
| `files[].grade`     | 정수 또는 null | 태깅 등급 `1`, `2`, `3`. `3`만 O로 허용. 미태깅·오류는 null                            |
| `files[].status`    | 문자열         | `tagged`, `untagged`, `error`                                                          |

CSO 미실행이면 `cso`를 생략한다. `cso.status`와 파일별 `status`는 서로 다른 enum이다.
CSO를 통과해도 일반 필터가 최종 요청을 차단할 수 있다.

## 5. 출력 요청·응답: `output_filter`

### 5.1 요청 필드

| 필드           | JSON 타입          | 기본값  | null 허용 | 의미와 현재 처리                                                                                                   |
| -------------- | ------------------ | ------- | --------- | ------------------------------------------------------------------------------------------------------------------ |
| `llm_output`   | 문자열             | `""`    | 아니오    | 실제 AI 출력. 빈 문자열 허용. 출력 저장 옵션에 따라 저장 생략·길이 제한 가능                                       |
| `message_id`   | 문자열             | `""`    | 아니오    | 앞뒤 공백 제거 후 사용. INPUT 응답의 ID와 동일하게 보냄. OUTPUT은 빈 ID를 자동 생성하지 않음                       |
| `user_id`      | 문자열             | `""`    | 예        | 출력 로그 사용자 ID. 입력과 달리 mapper가 null을 빈 문자열로 바꾸지 않음                                           |
| `email`        | 문자열             | `""`    | 예        | 출력 로그 이메일. null 유지                                                                                        |
| `ai_service`   | 정수               | `0`     | 예        | 2.4 매핑. null 유지, 로그 표시명은 `""`                                                                            |
| `client_host`  | 문자열             | `""`    | 예        | 출력 로그 IP. 입력과 달리 이 mapper는 IP 정규화를 수행하지 않음                                                    |
| `session_id`   | 문자열             | `""`    | 예        | 내부 출력 입력 모델에 전달. 기본 출력 로그 빌더는 세션 필드를 저장하지 않음                                        |
| `user_consent` | 문자열 또는 불리언 | `""`    | 예        | 2.5와 같은 해석. true이면 저장 실패 반환값을 HTTP 503으로 처리. 동의 INPUT이 있으면 OUTPUT에 다시 보낼 필요는 없음 |
| `debug`        | 불리언             | `false` | 예        | DTO에는 있지만 현재 OUTPUT 처리·응답에서 사용하지 않음                                                             |

일반 OUTPUT은 빈 ID를 DTO가 허용하지만 INPUT/OUTPUT 연결을 위해 실제 ID를 보낸다.
동의 OUTPUT의 빈 ID는 HTTP 422다. 출력은 INPUT보다 먼저 도착해도 병합 저장소에서 대기할 수 있다.
기본 출력 저장 한도는 UTF-8 200,000바이트이며 실행 환경에서 변경할 수 있다.

### 5.2 성공 응답 필드

| 필드               | JSON 타입 | 값과 의미                                                     |
| ------------------ | --------- | ------------------------------------------------------------- |
| `api_code`         | 정수      | `200`                                                         |
| `api_message_code` | 문자열    | `success`                                                     |
| `api_command`      | 문자열    | `output api router`                                           |
| `out_result`       | 객체      | 현재 use case는 항상 `{}` 반환. `debug=true`·동의 출력도 동일 |
| `completed_date`   | 문자열    | 서버 로컬 시각 `YYYY-MM-DDTHH:mm:ss`                          |

```json
{
  "api_code": 200,
  "api_message_code": "success",
  "api_command": "output api router",
  "out_result": {},
  "completed_date": "2026-10-18T12:00:01"
}
```

OUTPUT은 `final_decision`, `filter_result`, `log_evidence`, `queued/skipped`를 응답하지 않는다.
성공 응답은 최종 OpenSearch 반영 완료를 뜻하지 않는다.
일반 출력의 `record_output()` 실패 반환값은 기존 계약대로 HTTP 실패로 바꾸지 않지만,
명시적 동의 OUTPUT에서는 비성공 반환값을 503으로 바꾼다. 별도 예외가 전파되는 경우는 구분한다.

## 6. 오류 응답

HTTP 상태와 JSON 본문을 함께 확인한다. 모든 오류가 같은 envelope를 사용하지 않는다.

| HTTP 상태 | 발생 조건                                                       | 본문 형태                                                                         |
| --------- | --------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| `404`     | 알 수 없는 입력 필터, 출력/파일 처리기 등 필요한 구현 사용 불가 | `detail` 객체 안에 오류 envelope                                                  |
| `422`     | 필드 타입 검증 실패, 동의 INPUT/OUTPUT의 빈 ID                  | `detail` 검증 오류 배열                                                           |
| `409`     | controller에 `ConsentLogNotFoundError`가 전파되는 경우          | `detail` 문자열. 일반 동의 입력에서 원본 로그가 없다는 이유만으로 발생하지는 않음 |
| `503`     | 재시도 가능한 로그 전달·동의 저장 실패                          | `detail` 문자열                                                                   |
| `500`     | 별도로 처리하지 않은 서버 예외                                  | 위 envelope를 보장하지 않음                                                       |

HTTP 200의 `action="failed"`는 이 표의 HTTP 오류와 다르다.
CSO 조회 실패도 로그 전달이 성공하면 HTTP 200의 `block/1`로 반환할 수 있다.
일반 차단 로그의 실패 반환값은 전역 동의 옵션을 켰을 때 503으로 바뀌며,
CSO 차단 로그 전달 경로도 성공 여부를 요구한다.

### 6.1 HTTP 404: 오류 envelope

```json
{
  "detail": {
    "api_command": "pipeline multiple filter",
    "api_code": 404,
    "api_message_code": "Resource Not Found Error",
    "out_result": "unsupported input inspector: unknown_filter",
    "completed_date": "2026-10-18T12:00:00"
  }
}
```

| 필드                      | JSON 타입 | 값과 의미                                                     |
| ------------------------- | --------- | ------------------------------------------------------------- |
| `detail.api_command`      | 문자열    | 입력은 `pipeline multiple filter`, 출력은 `output api router` |
| `detail.api_code`         | 정수      | `404`                                                         |
| `detail.api_message_code` | 문자열    | `Resource Not Found Error`                                    |
| `detail.out_result`       | 문자열    | 예외 설명. 성공 OUTPUT의 `{}`와 타입이 다름                   |
| `detail.completed_date`   | 문자열    | 서버 로컬 응답 시각                                           |

### 6.2 HTTP 422: 검증 오류 배열

```json
{
  "detail": [
    {
      "type": "string_type",
      "loc": ["body", "message_id"],
      "msg": "Input should be a valid string",
      "input": null
    }
  ]
}
```

`type`은 검증 오류 종류, `loc`는 본문 안의 오류 위치(문자열/배열 인덱스),
`msg`는 설명, `input`은 검증 대상 값이다. 오류에 따라 `ctx` 등 추가 필드가 있을 수 있다.
동의 ID 검증 같은 모델 전체 오류는 `loc`가 `["body"]`일 수 있다.
오류 문구나 배열의 모든 세부 키를 고정값으로 가정하지 않는다.

### 6.3 HTTP 409·503: 문자열 detail

```json
{
  "detail": "동의 입력 로그 병합 저장에 실패했습니다."
}
```

이 예제는 HTTP 503이다. 이 형태에는 `api_code`, `api_message_code`, `out_result`,
`completed_date`가 없다. 오류 문구는 실패 경로마다 다르다.

## 7. 최소 요청과 응답 읽는 순서

일반 입력:

```json
{
  "message_id": "msg-001",
  "prompt": "회의 내용을 요약해 주세요.",
  "filter_list": ["input_filter", "secret_filter", "slm_filter"],
  "ai_service": 1,
  "debug": false
}
```

실제 AI 출력:

```json
{
  "message_id": "msg-001",
  "llm_output": "회의 요약입니다.",
  "ai_service": 1
}
```

1. 먼저 HTTP 상태를 확인한다. 오류에서는 `final_decision`이 있다고 가정하지 않는다.
2. 입력 200이면 **최상위** `final_decision.action`과 `action_code`를 읽는다.
3. `block/1`은 차단, `masking/2`는 마스킹 결과 적용, `accept/0`·`""/0`은
   각 의미에 맞게 처리한다. `failed/0`은 별도 검사 실패로 처리한다.
4. AI를 실제 호출했다면 입력 응답의 ID로 OUTPUT을 보낸다.
5. 디버그 필드·로그 mode·Health 상태를 최종 보안 판정 대신 사용하지 않는다.

## 8. `ok`, `pass`, `allow`와 Health 상태 구분

| 값                                               | 현재 브랜치의 위치                                    | 의미                                                     |
| ------------------------------------------------ | ----------------------------------------------------- | -------------------------------------------------------- |
| `success`                                        | INPUT/OUTPUT의 `api_message_code`                     | HTTP 처리 성공 envelope                                  |
| `accept` / `block` / `masking` / `failed` / `""` | INPUT의 `final_decision.action`                       | 현재 공개 최종 판정 enum                                 |
| `allow`                                          | 현재 INPUT 최종 판정에는 없음                         | develop의 과거 최종 허용값과 혼동하지 않음               |
| `ok`, `error`                                    | Health의 `status`, `checks.<이름>.status`             | 프로세스/의존성 상태                                     |
| `OK`, `ERROR`                                    | CDS가 Pipeline에 반환하는 외부 응답의 `rows[].status` | 외부 로그 전송 결과. INPUT/OUTPUT 응답이 아님            |
| `pass`                                           | 현재 INPUT/OUTPUT의 판정 필드에는 없음                | 구형 탐지 로그나 SSLProxy 서비스 정책 값과 혼동하지 않음 |

Health 경로와 필드:

| 경로                                                            | 상태/본문                                                                     |
| --------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| `GET /`, `GET /v1`                                              | HTTP 200, `{"status": true}`. status는 **불리언**                             |
| `GET /v1/health`, `/v1/health/live`                             | 생존 확인. HTTP 200, 문자열 `status="ok"`, UTC `timestamp`                    |
| `GET /v1/health/ready`                                          | 준비 상태. 전체 정상은 200/`ok`, 하나라도 실패하면 503/`error`. `checks` 포함 |
| `/api/v1/health`, `/api/v1/health/live`, `/api/v1/health/ready` | 해당 `/v1/health` 경로의 별칭                                                 |

| Health 필드                 | JSON 타입      | 값과 의미                                        |
| --------------------------- | -------------- | ------------------------------------------------ |
| `status`                    | 문자열         | `ok`, `error`. `/`, `/v1`의 불리언 status와 구분 |
| `timestamp`                 | 문자열         | UTC ISO 시각                                     |
| `checks`                    | 객체, 조건부   | ready의 의존성별 결과. live에서는 생략           |
| `checks.<이름>.status`      | 문자열         | `ok`, `error`                                    |
| `checks.<이름>.latencyMs`   | 정수           | 검사 소요 시간(밀리초), 0 이상                   |
| `checks.<이름>.message`     | 문자열, 조건부 | 오류 등 설명. 값이 없으면 생략                   |
| `checks.<이름>.usedPercent` | 숫자, 조건부   | 디스크 등 사용률(%). 값이 없으면 생략            |

현재 ready의 검사 이름은 `valkey`, `disk`, `logDirectory`, `bulkDirectory`이며,
CDS 전송이 활성화되면 `cdsDirectory`도 추가한다.

## 9. 구현 근거와 관련 문서

| 확인 대상                        | 구현                                                                                                                           |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| INPUT 요청 기본값·타입           | [input_analysis/dto.py](../src/controllers/input_analysis/v1/dto.py)                                                              |
| INPUT 응답 위치·필드·action code | [input_analysis/mapper.py](../src/controllers/input_analysis/v1/mapper.py)                                                        |
| 판정 enum·결과 모델              | [input_analysis_result.py](../src/domain/inspection_input/models/input_analysis_result.py)                                     |
| 모드·실행 순서·동의·로그 분기    | [input_analysis/use_case.py](../src/application/use_cases/input_analysis/use_case.py)                                          |
| 최종 판정·우회·마스킹 병합       | [final_decision_resolver.py](../src/domain/inspection_input/services/final_decision_resolver.py)                               |
| 로그 근거·빈 mode·서비스 매핑    | [input_analysis_log_builder.py](../src/application/use_cases/input_analysis/input_analysis_log_builder.py)                     |
| 동의 문자열 해석                 | [user_consent.py](../src/domain/analysis_log_delivery/models/user_consent.py)                                                  |
| OUTPUT 요청·응답                 | [output_analysis/dto.py](../src/controllers/output_analysis/v1/dto.py), [mapper.py](../src/controllers/output_analysis/v1/mapper.py) |
| OUTPUT 저장·서비스 매핑          | [output_analysis/use_case.py](../src/application/use_cases/output_analysis/use_case.py)                                        |
| 오류 envelope·응답 시각          | [http_controller_mapper.py](../src/controllers/http_controller_mapper.py)                                                      |
| CSO 집계 상태·파일 상태          | [cso_grade_result.py](../src/domain/inspection_input/models/cso_grade_result.py)                                               |
| Health 필드                      | [health/dto.py](../src/controllers/health/dto.py), [health_controller.py](../src/controllers/health/health_controller.py)      |

- [JSON·최종 저장 문서 예제](API_JSON_STRUCTURE.md)
- [Inline 검사와 판정 흐름](INLINE_MULTIPLE_FILTER.md)
- [사용자 동의와 기존 로그 갱신](USER_CONSENT.md)

HTTP 성공 응답은 dict로 조립하므로 요청 DTO의 OpenAPI만으로 모든 응답 값·분기를
알 수 없다. 응답 필드나 enum을 변경할 때는 이 문서·JSON 예제·controller 계약 테스트를
함께 갱신한다.
