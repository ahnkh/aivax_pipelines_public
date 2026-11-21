# develop 기준 Pipeline INPUT/OUTPUT API 명세

조회 전용 develop Swagger·OpenAPI 파일: [swagger/README.md](swagger/README.md).

확인일: 2026-10-18 · 기준 브랜치: `develop` · 고정 커밋: `b05a06becb5e6430b603d0d4396e2e3afa4bdf9a`.

이 문서는 **develop의 과거 구현**을 기록한다. 현재 작업 브랜치의 계약은
[API_REFERENCE.md](API_REFERENCE.md)를 따른다. 같은 필드라도 가능한 값과 처리
방식이 다르므로 두 명세를 섞어 사용하지 않는다.

대상은 `POST /v1/filter/multiple_filter`와 `POST /v1/filter/output_filter`다.
요청 본문은 `Content-Type: application/json`이다. 기본 입력 필터인 `input_filter`,
`secret_filter`, `file_block_filter`, `slm_filter`를 기준으로 설명한다.
동적으로 등록한 다른 pipeline의 임의 응답, 정책 테스트·갱신, IPC API는 별도 계약이다.

## 1. 응답 값을 읽는 기준

| 위치 | develop의 값·의미 |
| --- | --- |
| `api_message_code` | 성공은 `success`. `ok`, `pass`가 아님 |
| `final_decision.action` | 기본 필터 기준 `allow`, `block`, `masking` |
| `final_decision.action_code` | `allow=0`, `block=1`, `masking=2` |
| `filter_result.<필터>.action` | 개별 필터 응답. 파일 미탐지는 `""`도 가능 |
| `log_evidence.filter_detect[].mode` | 정책 탐지 값. `accept`, `block`, `masking`, `""` 등이 가능 |
| `log_evidence.mode` | 로그에서 별도 취합한 판정. SSL 우회 후 최종 응답과 다를 수 있음 |

**최종 허용값은 `allow`다.** accept 정책만 탐지하거나 미탐지면 최종 응답은 allow/0이다.
최종 `accept`, `failed`, 빈 action을 반환하는 현재 브랜치의 동작을 develop에 적용하지 않는다.
HTTP 200은 요청 허용이나 전체 검사 정상 완료를 보장하지 않는다. 차단·마스킹도
HTTP 200이고, SLM 통신 실패도 다른 탐지가 없으면 allow로 이어질 수 있다.

## 2. 입력 요청: multiple_filter

### 2.1 요청 필드

기본값은 필드 생략 시의 값이다. null 허용은 **DTO 타입 기준**이며 이후 실행 코드가
null을 안전하게 처리한다는 뜻은 아니다. 정의되지 않은 추가 키는 기본적으로 무시한다.

| 필드 | JSON 타입 | 기본값 | null 허용 | 의미와 develop 처리 |
| --- | --- | --- | --- | --- |
| `filter_list` | 문자열 배열 | 아래 네 이름 전체 | 예 | 요청 순서대로 실행. 빈 배열은 검사 없이 allow. null은 순회 중 예외 가능 |
| `prompt` | 문자열 | 아래 예제 문장 | 아니오 | 검사 원문. 생략 시 빈 문자열이 아니라 DTO에 하드코딩된 예제 문장 사용 |
| `encoding` | 불리언 | `false` | 예 | 필드는 있지만 활성 입력 body 생성 코드에서 사용하지 않음. Base64 해독 안 함 |
| `user_id` | 문자열 | `""` | 예 | 사용자 ID. null을 일괄적으로 빈 문자열로 바꾸지 않음 |
| `email` | 문자열 | `""` | 예 | 사용자 정보. email과 ai_service 조합으로 사용자 키 생성 |
| `ai_service` | 정수 | `0` | 예 | AI 서비스 ID. 실제 매핑은 2.4 참고. null을 입력 mapper에서 0으로 정리하는 현재 구조와 다름 |
| `client_host` | 문자열 | `""` | 예 | 공백 제거 후 IPv4/IPv6 표준화. 잘못된 주소·null·빈 값은 `""` |
| `session_id` | 문자열 | `""` | 예 | 내부 metadata에 전달. 입력·출력 연결 키는 message_id |
| `attachments` | 객체 배열 | `[]` | 예 | 첨부파일 목록. body 생성에서 순회하므로 null은 예외 가능 |
| `message_id` | 문자열 | `""` | 아니오 | 길이 0이면 32자리 hex ID 생성. **앞뒤 공백 제거 안 함** |
| `debug` | 불리언 | `false` | 예 | true이면 최상위 filter_result, log_evidence 추가. 그 외 생략 |

`prompt`의 develop 생략 기본값:

```text
내 API key는 API_key=sk-1234567-0000-abdcdef 인데 이걸로 어떻게 OpenAI 로 KEY를 전달하는지 예제를 알려주세요
```

`event_time`, `user_consent`, `userConsent`, `prompt_base64`는 이 요청 DTO의 필드가 아니다.
**user_consent=true를 보내도 동의 전용 우회는 생기지 않는다.** 현재의 CSO 선행 검사도 없다.

### 2.2 filter_list 기본값과 실행 순서

| 기본 이름 | 역할 | 디버그·로그 특징 |
| --- | --- | --- |
| `input_filter` | 입력 사용자·원문 로그 생성 | filter_result.input_filter는 보통 `{"action":"allow"}` |
| `secret_filter` | 정규식 정책 검사 | filter_result.secret_filter, 로그 필터명 regex |
| `file_block_filter` | 첨부파일 분석·보관 | filter_result.file_block_filter, 로그 필터명 file block |
| `slm_filter` | SLM 검사 | filter_result.slm_filter, 로그 필터명 slm |

생략하면 위 네 이름이 위 순서로 실행된다. 직접 보내면 **요청 배열 순서가 실행 순서**다.
중복 제거를 하지 않아 같은 이름을 반복 실행할 수 있지만, 결과 딕셔너리의 같은 키에는
마지막 저장 결과가 남는다. 현재 브랜치의 고정 검사 순서와 다르다.

필터 이름은 실행 시 pipeline map에서 찾는다. 등록되지 않은 이름에 도달하면 404다.
기본 설정에서 앞 필터의 block으로 순회가 끝났다면 뒤의 잘못된 이름은 확인하지 않을 수 있다.
`secuxper_filter`는 이 develop 기본 계약에 없다.

input_filter를 빼면 공통 사용자·원문 로그 필드가 빠질 수 있다. 명령은 실행 후 조립한
입력 로그 버퍼를 큐에 전달한다. 현재의 입력 로그 저장 요청 플래그와 동일한 구현이 아니다.

### 2.3 attachments[]

| 필드 | JSON 타입 | 기본값 | null 허용 | 의미 |
| --- | --- | --- | --- | --- |
| `id` | 문자열 | `""` | 예 | Pipeline에서 읽을 실제 파일 경로 |
| `size` | 정수 | `0` | 예 | 호출자가 제공한 바이트 크기. 실제 분석에서는 파일 메타데이터를 읽음 |
| `name` | 문자열 | `""` | 예 | 파일 이름 |
| `mime_type` | 문자열 | `""` | 예 | 호출자가 제공한 MIME 타입 |

기본 파일 검사는 경로/이름의 길이가 0이거나 파일이 없으면 해당 파일을 건너뛴다.
경로·이름이 null이면 길이 확인에서 예외가 발생할 수 있다.
`attachments=null`과 `attachments=[]`를 같은 값으로 처리하지 않는다.

### 2.4 ai_service 실제 매핑

DTO description의 `GPT=0, CLAUDE=1, GEMINI=2` 설명과 실제 상수가 다르다.
아래는 develop의 `pipeline/lib_include.py`에 있는 표시명 매핑이다.

| 요청 값 | 로그 표시명 |
| --- | --- |
| `0` | `unknown` |
| `1` | `GPT` |
| `2` | `Gemini` |
| `3` | `Claude` |
| `4` | `Grok` |
| `5` | `Perplexity` |
| `6` | `Notion AI` |
| `7` | `Works AI` |
| `8` | `claude.code` |
| `10` | `openapi.copilot` |
| `11` | `cursor.ai` |
| 그 외 또는 null | `""` |

DTO가 enum으로 제한하지는 않는다. 기본 입력 로그에는 현재의 ai_service_id 원본 필드가
없고 ai_service 표시명을 기록한다.

### 2.5 입력 요청 예제

```json
{
  "filter_list": ["input_filter", "secret_filter", "file_block_filter", "slm_filter"],
  "prompt": "회의 내용을 요약해 주세요.",
  "user_id": "user-100",
  "email": "user@example.com",
  "ai_service": 1,
  "client_host": "127.0.0.1",
  "session_id": "session-001",
  "message_id": "msg-001",
  "attachments": [],
  "debug": true
}
```

## 3. 입력 성공 응답

### 3.1 최상위 필드

| 필드 | JSON 타입 | 존재 조건 | 값과 의미 |
| --- | --- | --- | --- |
| `api_code` | 정수 | 항상 | 200 |
| `api_message_code` | 문자열 | 항상 | success |
| `api_command` | 문자열 | 항상 | pipeline multiple filter |
| `final_decision` | 객체 | 항상 | 엔진에 전달할 최종 판정 |
| `completed_date` | 문자열 | 항상 | 서버 로컬 응답 조립 시각 YYYY-MM-DDTHH:mm:ss |
| `filter_result` | 객체 | debug=true | 실행한 pipeline별 원본 응답 |
| `log_evidence` | 객체 | debug=true | 입력 로그 버퍼. 최종 저장 완료 증명이 아님 |

입력 성공 응답에는 out_result 래퍼가 없다. 디버그가 꺼져 있으면 디버그 두 필드를 생략한다.
이 develop 명령에는 현재의 Mirror 전용 즉시 접수·디버그 숨김 분기가 없다.

```json
{
  "api_code": 200,
  "api_message_code": "success",
  "api_command": "pipeline multiple filter",
  "final_decision": {
    "action": "allow",
    "action_code": 0,
    "masked_contents": "",
    "block_message": "",
    "message_id": "msg-001"
  },
  "completed_date": "2026-10-18T12:00:00"
}
```

### 3.2 final_decision 필드

| 필드 | JSON 타입 | 값과 의미 |
| --- | --- | --- |
| `action` | 문자열 | 기본 필터 기준 allow, block, masking |
| `action_code` | 정수 | 허용 0, 차단 1, 마스킹 2 |
| `masked_contents` | 문자열 | 선택한 필터의 마스킹/대체 콘텐츠. 없으면 빈 문자열 |
| `block_message` | 문자열 | 선택한 필터의 차단/안내문. 없으면 빈 문자열 |
| `message_id` | 문자열 | 요청 값 또는 서버 생성 ID. 요청의 앞뒤 공백도 보존 |

기본 필터의 정상 응답에서는 다섯 필드가 항상 존재한다. 정책 ID·이름, error,
user_consent, effective_decision, CSO 정보를 이 객체에 추가하지 않는다.

취합기는 개별 결과의 **숫자 action_code가 1 또는 2인지** 확인한다.
최초 block을 선택하고, block이 없으면 최초 masking을 선택한다.
둘 다 없으면 초기값 allow/0/빈 문자열을 유지한다.
동적으로 등록한 다른 pipeline이 action과 code를 불일치하게 만들면 정상 조합을
보장할 수 없다. 취합기가 action 문자열을 enum으로 다시 검증하지 않기 때문이다.

### 3.3 우회가 없는 일반 케이스

| 케이스 | 최종 action/code | masked_contents | block_message |
| --- | --- | --- | --- |
| 모두 미탐지 | allow/0 | 빈 문자열 | 빈 문자열 |
| 정규식 accept 정책만 탐지 | allow/0 | 빈 문자열 | 빈 문자열 |
| input_filter만 요청 | allow/0 | 빈 문자열 | 빈 문자열 |
| filter_list=[] | allow/0 | 빈 문자열 | 빈 문자열 |
| 정규식 차단 | block/1 | **마스킹한 원문** | 정규식 차단 안내 |
| 정규식 마스킹 | masking/2 | 마스킹한 원문 | 정규식 차단 안내도 포함 |
| 파일 내용·워터마크·확장자·크기 등 차단 | block/1 | 빈 문자열 | 파일 차단 안내 |
| 파일 없음·미탐지·대상 건너뜀 | 다른 탐지 없으면 allow/0 | 빈 문자열 | 빈 문자열 |
| SLM 안전 판정·검사 생략 | 다른 탐지 없으면 allow/0 | 빈 문자열 | 빈 문자열 |
| SLM 탐지 + 정책 block | block/1 | 빈 문자열 | SLM 차단 안내 |
| SLM 탐지 + 정책 masking | masking/2 | **SLM 마스킹 안내문** | 빈 문자열 |
| SLM 탐지 + 정책 accept | 다른 탐지 없으면 allow/0 | 빈 문자열 | 빈 문자열 |
| SLM 통신 실패·타임아웃·상대 HTTP 오류 | 다른 탐지 없으면 **allow/0** | 빈 문자열 | 빈 문자열 |

파일 정규식 정책은 매칭 시 원래 정책 action과 관계없이 block으로 강제하는 코드가 있다.
파일 응답 조립기에 masking 분기가 있어도 정책의 masking이 그대로 전달된다고 가정하지 않는다.
SLM masking은 원문 치환 결과가 아닌 안내문이다.
정규식 block에도 마스킹 문자열이 있으므로 **문자열 존재 여부로 판정하면 안 된다.**

### 3.4 여러 필터와 실행 중단

| 실행/탐지 순서 | 결과 |
| --- | --- |
| masking A → block B | block B의 문자열·메시지 사용 |
| masking A → masking B | 최초 masking A 사용. 두 마스킹 병합 안 함 |
| block A → block B | 최초 block A 사용 |
| accept·미탐지만 있음 | 최종 allow |

`next_detect_after_block=false`가 기본값이다. 개별 필터가 action=block 또는
action_code=1이면 뒤 pipeline을 실행하지 않는다. true면 뒤 검사도 수행하지만
최종 선택은 최초 block이다. 후속 검사 예외가 전파되면 앞 판정이 있어도 요청은 실패할 수 있다.

### 3.5 SSL 우회 옵션

설정 위치는 develop의 `pipe_filter_custom_config.filter_output_config.ssl_proxy_bypass_bitmask`다.
값 1(0b01)은 block 우회, 2(0b10)는 masking 우회, 3은 둘 다 우회다. 저장소 config는 0이다.
현재 브랜치의 환경변수 설정 경로와 구분한다.

아래는 수집된 결과를 순서대로 취합하는 기준이다.

| 우회 값 | 결과 순서 | 최종 판정 |
| --- | --- | --- |
| 1 | block | allow/0 |
| 1 | masking A → block B | masking A/2 유지 |
| 1 | block A → masking B | allow/0. 뒤 masking 취합 안 함 |
| 2 | masking | allow/0 |
| 2 | masking A → block B | **allow/0. 뒤 block도 취합 안 함** |
| 2 | block A → masking B | block A/1 |
| 3 | block 또는 masking | allow/0 |

우회 대상 결과를 만났을 때 continue가 아니라 **취합 반복문을 break**한다.
검사 실행 중단은 별개여서 기본 설정이면 block 우회여도 block 이후 검사를 실행하지 않는다.
로그에는 SSL 우회를 적용하지 않아 최종 allow인데 로그 mode는 block일 수 있다.

### 3.6 SLM 응답 해석 조건

| 조건 | develop 동작 |
| --- | --- |
| use_skip 활성화 | 모델을 호출하지 않고 해당 필터 allow/0 |
| 타임아웃·연결 실패·상대 HTTP 오류 | 요청 함수가 None 반환. inlet이 검사 실패 반환값을 최종 실패로 바꾸지 않아 allow로 남음 |
| 모델 원문의 JSON 파싱·필드 접근 예외 | HTTP 요청 함수 바깥에서 전파될 수 있음. 성공 응답의 failed로 바꾸지 않음 |
| 모델 1·2 탐지 + 기본 정책 있음 | 첫 기본 정책의 action 반영 |
| 모델 1·2 탐지 + 기본 정책 없음 | 정책 action이 비어 allow로 정리될 수 있음 |
| 모델 3·4 탐지 + 기본 정책 있음 | 첫 기본 정책 action 사용 |
| 모델 3·4 탐지 + 기본 정책 없음 | block을 기본값으로 사용 |

model_version=1은 MTM CPU, 2는 MTM GPU, 3은 WINS GPU, 4는 WINS GPU V4다.
모델별 원문 해석이 다르며 malformed 응답을 모두 안전한 allow로 처리하는 것은 아니다.

## 4. 디버그 응답

### 4.1 filter_result: 필터별 원본 딕셔너리

| 키 | 대표 필드와 값 |
| --- | --- |
| input_filter | action=allow만 있는 객체. code·마스킹·정책 필드 보장 안 함 |
| secret_filter | action, action_code, masked_contents, block_message |
| file_block_filter | action, action_code, file_summary, policy_id, policy_name, id, name, targets, category, scope. block/masking이면 block_message 추가 |
| slm_filter | action, action_code, 조건부 slm_content. block이면 block_message, masking이면 masked_contents |

실행한 필터만 들어간다. 기본 설정에서 block 뒤 생략한 후속 필터는 항목이 없다.
SLM skip/통신 실패에서는 slm_content 키가 없을 수 있다.
파일 미탐지는 action이 빈 문자열, code=0일 수 있지만 최종 판정은 allow다.
파일 bypass의 이른 반환에서는 일부 정책 관련 필드가 생략될 수 있다.

다음은 **filter_result만 발췌한 예시**다. 파일 검사는 미탐지, SLM은 skip인 경우다.

```json
{
  "filter_result": {
    "input_filter": {"action": "allow"},
    "secret_filter": {
      "action": "allow", "action_code": 0, "masked_contents": "", "block_message": ""
    },
    "file_block_filter": {
      "action": "", "action_code": 0, "file_summary": [],
      "policy_id": "", "policy_name": "", "id": "", "name": "",
      "targets": "", "category": "", "scope": ""
    },
    "slm_filter": {"action": "allow", "action_code": 0}
  }
}
```

### 4.2 log_evidence 필드

| 필드 | JSON 타입 | 생성 조건·의미 |
| --- | --- | --- |
| filter_detect | 객체 배열 | 빈 배열로 시작, 각 검사에서 로그 항목 추가 |
| mode | 문자열 | 로그 대표 판정. block → masking → accept → undetected → 빈 문자열 순으로 대표 항목 선택 |
| masked_contents | 문자열 또는 null | 대표 로그의 마스킹. 해당 키가 없으면 null |
| policy.policy_id, policy.policy_name, policy.category, policy.scope | 정책 문자열 등 | 대표 로그에서 가져옴. 없으면 빈 문자열 |
| @timestamp | 문자열 | input_filter가 생성하는 UTC 로그 시각 |
| message_id | 문자열 | input_filter가 생성하는 연결 키 |
| user.id, user.email, user.uuid, user.role | 문자열 또는 null | input_filter가 생성하는 사용자 정보. UUID는 사용자 키에서 생성 |
| client_host | 문자열 | input_filter가 생성하는 정규화 IP 또는 빈 문자열 |
| client_host_cidr | 정수 | input_filter가 생성하는 IP 정수값. CIDR 접두 길이가 아님 |
| ai_service | 문자열 | input_filter가 생성하는 2.4 표시명 |
| prompt | 문자열 | input_filter가 생성하는 원문 |
| request_date | 문자열 | input_filter 실행 시 서버 로컬 시각. 호출자 event_time 없음 |

input_filter를 빼면 해당 공통 로그 필드도 보장하지 않는다.
log_evidence.mode=accept여도 final_decision.action=allow일 수 있다.
로그 대표 선택과 최종 응답 선택은 별도 함수다. 로그 mode로 최종 판정을 대체하지 않는다.

### 4.3 filter_detect[] 검사별 필드

| 검사 | 기본 필드 |
| --- | --- |
| 정규식 | filter=regex, mode, policy_id, policy_name, target, category, scope, masked_contents, evidence |
| 파일 | filter=file block, mode, policy_id, policy_name, **targets**, category, scope, file_summary |
| SLM | filter=slm, mode, slm_content |

정규식은 target, 파일 로그는 targets라는 복수형 키를 쓴다.
SLM 로그에 공통 정책 식별자·evidence 필드를 강제로 추가하지 않는다.
slm_content는 모델 원본 문자열이며 얻지 못하면 로그에서는 null일 수 있다.
실패를 error 객체나 mode=failed로 통일하는 현재 브랜치 로직은 없다.

각 inlet 실행 중 새로 추가된 로그 항목에는 다음 실행 시간이 붙는다.

| 필드 | JSON 타입 | 의미 |
| --- | --- | --- |
| started_at | 문자열 | 시작 시각. UTC ISO ...Z |
| completed_at | 문자열 | 완료 시각. UTC ISO ...Z |
| duration_ms | 숫자 | 두 시각 차이의 밀리초. 소수점 셋째 자리까지 반올림 |

정규식 evidence는 id, name, action, targets, category, scope, match를 포함한다.
match는 매칭 문자열과 위치를 문자열로 표현한다. 현재 브랜치처럼 start/end 정수 필드를
이 로그 계약에서 보장하지 않는다.

### 4.4 file_summary[]

필터 원본 응답과 파일 검사 로그가 같은 파일 요약을 참조한다.

| 필드 | JSON 타입 | 값과 의미 |
| --- | --- | --- |
| action | 문자열 | 파일별 판정. 미탐지 빈 문자열, 기본 내용 정책 매칭은 block 강제 |
| policy_id, policy_name | 문자열 | 정책 ID·이름 또는 빈 문자열 |
| file_name | 문자열 | 파일 이름 |
| file_backup_path | 문자열 | 보관 경로 또는 빈 문자열 |
| name, targets, category, scope | 문자열 | 정책 정보 |
| regex_pattern | 문자열 | 정책에 매칭한 문자열 또는 빈 문자열 |
| id | 문자열, 조건부 | 내용 정책 탐지에서 추가하는 ID |
| file_info.mime_type | 문자열, 조건부 | MIME 타입 |
| file_info.file_ext | 문자열, 조건부 | 확장자 |
| file_info.size | 정수, 조건부 | 바이트 크기 |
| file_info.hash | 문자열, 조건부 | 파일 SHA-256 |
| file_info.chunk | 문자열, 조건부 | 설정된 길이의 추출 콘텐츠 |
| detail_reason | 객체, 조건부 | 파일 상세 분석 결과. policy_detail, evidence 등 포함 |

워터마크 분기 등에서 일반 내용 검사를 건너뛰면 file_info가 없을 수 있다.
현재의 inspection_status, skip_reason=cso_blocked, cso 객체를 develop에서 기대하지 않는다.

## 5. 출력 요청·응답: output_filter

### 5.1 요청 필드

| 필드 | JSON 타입 | 기본값 | null 허용 | 의미 |
| --- | --- | --- | --- | --- |
| llm_output | 문자열 | 빈 문자열 | 아니오 | 실제 AI 응답 또는 내부 차단 안내 |
| user_id | 문자열 | 빈 문자열 | 예 | 사용자 ID |
| email | 문자열 | 빈 문자열 | 예 | 사용자 키·정보 |
| ai_service | 정수 | 0 | 예 | 2.4 서비스 ID |
| client_host | 문자열 | 빈 문자열 | 예 | 사용자 IP. 이 명령에서는 입력처럼 IP 표준화 안 함 |
| session_id | 문자열 | 빈 문자열 | 예 | 내부 metadata에 전달 |
| message_id | 문자열 | 빈 문자열 | 아니오 | INPUT과 같은 키. OUTPUT은 자동 생성·공백 제거 안 함 |
| debug | 불리언 | false | 예 | DTO에는 있지만 출력 응답을 확장하지 않음 |

user_consent는 OUTPUT DTO에도 없다. 동의 전용 구분·ID 검증을 현재 브랜치에서 가져오지 않는다.
기본 output_pass는 전문을 저장하고 UTF-8 200,000바이트 제한을 적용한다.
설정은 실행 모듈의 valves로 변경될 수 있다.

### 5.2 성공 응답

| 필드 | JSON 타입 | 값과 의미 |
| --- | --- | --- |
| api_code | 정수 | 200 |
| api_message_code | 문자열 | success |
| api_command | 문자열 | output api router |
| out_result | 객체 | 기본 output_pass가 결과를 채우지 않아 빈 객체 |
| completed_date | 문자열 | 서버 로컬 시각 YYYY-MM-DDTHH:mm:ss |

```json
{
  "api_code": 200,
  "api_message_code": "success",
  "api_command": "output api router",
  "out_result": {},
  "completed_date": "2026-10-18T12:00:01"
}
```

출력은 develop에서도 out_result를 사용한다. 입력과 같은 이유로 제거하면 안 된다.
debug=true여도 final_decision, filter_result, log_evidence를 추가하지 않는다.
로그 큐 전달이 최종 OpenSearch 반영 완료를 뜻하지 않는다.

multiple 최종 판정이 block이면 명령이 **내부적으로 OUTPUT 처리도 호출**한다.
llm_output에는 최종 차단 안내를 넣는다. 이 과정이 실패하면 multiple도 정상 block 응답을
반환하지 못할 수 있다.

### 5.3 출력 요청 예제

입력 응답의 message_id와 같은 값을 보낸다.

```json
{
  "llm_output": "회의 요약입니다.",
  "user_id": "user-100",
  "email": "user@example.com",
  "ai_service": 1,
  "client_host": "127.0.0.1",
  "session_id": "session-001",
  "message_id": "msg-001"
}
```

## 6. 오류 응답과 비정상 케이스

| HTTP 상태 | 조건 | 본문 형태 |
| --- | --- | --- |
| 404 | 순회 중 pipeline map에 없는 필터에 도달 | detail 안의 오류 envelope |
| 404 | output_filter 미등록. multiple block 뒤 내부 출력 처리에서도 가능 | 오류 envelope. command가 output api router일 수 있음 |
| 422 | 잘못된 타입 등 DTO 검증 실패 | detail 검증 오류 배열 |
| 500 가능 | null 목록/파일 이름·경로, 검사·모델 파싱·후속 처리의 전파된 예외 등 | 공통 envelope 보장 없음 |

기본 multiple 명령은 필터 예외를 성공 응답의 action=failed로 변환하지 않는다.
SLM 통신 실패를 내부 처리해 allow로 남기는 경로와 모델 파싱 예외가 전파되는 경로는 다르다.
현재 브랜치의 동의용 409·503 계약은 이 develop 기본 계약에 없다.

### 6.1 HTTP 404

```json
{
  "detail": {
    "api_code": 404,
    "api_message_code": "Resource Not Found Error",
    "api_command": "pipeline multiple filter",
    "out_result": "invalid pipeline, not exist pipeline, id = unknown_filter",
    "completed_date": "2026-10-18T12:00:00"
  }
}
```

detail.api_code는 404, api_message_code는 Resource Not Found Error,
api_command는 실패한 입력/출력 명령, out_result는 **문자열** 오류 설명이다.
completed_date는 서버 로컬 시각이다. 오류에는 final_decision이 없다.

### 6.2 HTTP 422

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

세부 검증 형식은 배포 Pydantic/FastAPI 버전에 따라 달라질 수 있다.
loc는 오류 위치, msg는 설명, type은 종류다. input 등 추가 키가 있을 수 있다.
404의 객체 detail과 422의 배열 detail을 같은 타입으로 파싱하지 않는다.

## 7. 현재 브랜치와 비교할 때의 기준

현재 브랜치 열은 [API_REFERENCE.md](API_REFERENCE.md)의 019de127 기준이다.

| 항목 | 이 develop 기준 | 현재 브랜치 기준 |
| --- | --- | --- |
| INPUT 판정 위치 | 최상위 final_decision | 최상위 final_decision |
| OUTPUT 결과 위치 | out_result | out_result |
| 최종 허용 action | allow | accept 또는 빈 문자열 |
| 검사 실패 최종 action | failed 계약 없음 | failed/0 가능 |
| 정규식 block의 마스킹 | 포함 | 빈 문자열 |
| 검사 순서 | 요청 순서 | 고정 순서 |
| 마스킹 여러 개 | 최초 결과 선택 | 위치 근거 병합 |
| 디버그 결과 | 필터별 원본 딕셔너리 | 공통 아홉 필드 |
| block 뒤 미실행 항목 | 기본 설정에서는 없음 | 빈 기본 항목 포함 가능 |
| 입력 null 목록 | DTO 허용 후 실행 예외 가능 | 빈 목록으로 정리 |
| 동의 전용 입력 경로 | 없음 | CSO 확인 후 일반 필터 생략 |
| CSO 선행 차단 | 없음 | 설정으로 활성화 |
| Mirror 즉시 응답 | 이 명령에 없음 | 빈 action/0, 디버그 생략 |

ok, pass도 develop 기본 multiple의 최종 action 값이 아니다.
구형 llm_pass.py·regex_pass_v3.py의 로그에는 detection=hit/pass가 쓰이고,
SSLProxy 서비스 정책 전달에는 서비스명 키의 값으로 pass가 쓰인다.
IPC 서버의 status=ok 예제는 주석 코드다. 최종 보안 판정과 혼동하지 않는다.

## 8. 구현 근거와 확인 방법

아래 경로는 현재 작업 트리가 아니라 **고정 develop 커밋 내부 경로**다.

| 내용 | develop 내부 경로 |
| --- | --- |
| DTO | pipeline/api_modules/models/schemas.py |
| 입력 ID 생성·HTTP route | pipeline/api_modules/router/router_daemon_api.py |
| 검사 순서·최종 기본값·디버그 | pipeline/api_modules/router/sub_modules/filter_pipeline_command.py |
| 취합·우회·로그 대표 선택·404 | pipeline/api_modules/helper/router_custom_helper.py |
| block 뒤 내부 OUTPUT | pipeline/api_modules/router/sub_modules/api_router_impl_command.py |
| OUTPUT 응답 | pipeline/api_modules/router/sub_modules/output_pipeline_command.py |
| 입력 로그 | pipeline/pipelines/input_pass.py |
| 정규식 응답 | pipeline/pipelines/detect_secrets.py |
| 파일 응답·탐지 | pipeline/pipelines/file_block_filter.py, pipeline/block_filter_modules/filter_pattern/helper/file_block_filter_pattern.py |
| SLM 응답·파싱·실패 | pipeline/pipelines/slm_filter_v2.py, pipeline/block_filter_modules/filter_pattern/helper/slm_filter_pattern.py |
| OUTPUT 저장 | pipeline/pipelines/output_pass.py |
| 상수·서비스 매핑 | pipeline/lib_include.py, pipeline/local_common/pipeline_filter/local_define/local_define.py |

브랜치 전환 없이 확인하는 예:

```bash
git show b05a06becb5e6430b603d0d4396e2e3afa4bdf9a:pipeline/api_modules/models/schemas.py
git show b05a06becb5e6430b603d0d4396e2e3afa4bdf9a:pipeline/api_modules/helper/router_custom_helper.py
```

ApiResponseHandlerEX의 공용 libhttp 부모 구현은 develop 트리에 포함되어 있지 않다.
기본 envelope는 명령의 attach 호출과 공용 코드를 편입한 4b99e172의
`pipeline/shared/libhttp/restapi/{api_response_handler,api_response_define}.py`로 보완 확인했다.
판정 취합·SLM inlet 분기는 develop에서 추출해 실행해 확인할 수 있지만,
이 문서는 develop 배포 서버 전체를 기동해 얻은 실응답 기록은 아니다.
