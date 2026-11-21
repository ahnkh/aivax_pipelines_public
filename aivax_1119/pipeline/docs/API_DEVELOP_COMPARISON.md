# develop 대비 multiple/output API 비교

비교일: 2026-10-19. 대상은 아래 두 API다.

- `POST /v1/filter/multiple_filter`
- `POST /v1/filter/output_filter`

기준은 **로컬 develop `b05a06becb5e6430b603d0d4396e2e3afa4bdf9a`**다.
비교 대상은 현재 HEAD `019de12741aa174aaac64eb1bb260eef342b2f4f`와 작업 트리다.
`origin/develop`은 다른 커밋이므로 이 결과를 원격 develop 최신 상태와의 비교로 해석하지 않는다.

## 결론

**현재 두 API는 develop과 완전히 동일하지 않다.**

multiple의 기본 성공 응답 필드와 final_decision의 필드 구성은 유지된다.
하지만 허용 action, 마스킹 내용, debug 내부 구조, 요청 기본값, 일부 오류 조건이 다르다.
output의 일반 성공 응답 구조는 유지되지만, 동의 요청 검증과 ID 처리가 달라졌다.
아래 차이는 확인된 변경이며, 의도된 변경으로 승인됐다는 뜻은 아니다.

## 확인 방법과 한계

- develop 원본 DTO 세 클래스와 `RouterCustomHelper` 클래스를 AST로 추출해 실행했다.
  클래스 본문은 수정하지 않았고, 기존 bootstrap/import 부작용을 제외했다.
- 현재 DTO, mapper, 판정 resolver는 작업 트리 코드를 그대로 import했다.
- 양쪽 DTO에 같은 요청을 보내는 임시 FastAPI route로 **21개 요청 검증 조건**을 비교했다.
  이 route의 200은 **DTO 검증 통과**이며, 실제 multiple/output 처리 성공을 의미하지 않는다.
- 같은 필터 결과를 양쪽 취합기에 전달해 **11개 판정 조건**을 비교했다.
  필터 자체의 탐지 정확도나 실제 외부 SLM 응답까지 검증한 수치는 아니다.
- 실행 환경은 Python 3.13.9, FastAPI 0.123.0, Pydantic 2.12.5다.
  과거 배포의 FastAPI/Pydantic 버전 차이는 이 비교에 포함하지 않았다.
- develop 전체 서버 및 실제 DB·Valkey·SLM·첨부파일 서비스는 기동하지 않았다.
  실행 순서, debug, 오류 전달 등 나머지 항목은 원본 코드 대조 결과다.
- develop의 공용 응답 라이브러리 본체는 해당 트리에 없다. 공통 envelope는
  명령의 attach 호출과 `4b99e172`의 편입된 공용 라이브러리로 보완 확인했다.
  해당 부분을 develop 배포 실응답 수집 결과로 취급하면 안 된다.
- 상세 실행 결과: [API_DEVELOP_COMPARISON_COMPONENTS.json](API_DEVELOP_COMPARISON_COMPONENTS.json).
  이 JSON은 부분 실행의 관측값이며 회귀테스트용 전체 응답 baseline이 아니다.

## 1. 요청 구성

| 항목 | develop | 현재 | 확인 |
| --- | --- | --- | --- |
| multiple 기존 필드 | 기존 필드 유지 | 삭제 없음. 공통 필드 타입·null 허용 동일 | DTO schema 실행 |
| multiple 추가 필드 | 없음 | `event_time`, `user_consent` | DTO schema 실행 |
| multiple `filter_list` 기본값 | input, secret, file_block, slm의 4개 | `secuxper_filter`가 추가된 5개 | DTO schema 실행 |
| output 기존 필드 | 기존 필드 유지 | 삭제·공통 필드 타입 변경 없음 | DTO schema 실행 |
| output 추가 필드 | 없음 | `user_consent` | DTO schema 실행 |
| 첨부파일 항목 | id, size, name, mime_type | 필드·타입·기본값 동일 | DTO schema 실행 |
| 필드별 필수 지정 | 두 DTO 모두 없음 | 동일. 단, 동의 ID의 조건부 검증 추가 | DTO 실행 |
| `user_consent=on`이고 ID 생략 또는 공백 | 추가 필드를 무시하여 DTO 통과 | HTTP 422 | 임시 route 실행 |
| 알 수 없는 일반 추가 필드 | 무시 | 동일 | 임시 route 실행 |

현재 SecuXper는 전역 기본 설정에서 비활성화되어 있다.
따라서 기본 요청 목록이 5개라는 사실이 항상 실제 5개 필터 실행을 뜻하지는 않는다.

21개 요청 검증 조건 중 HTTP 상태가 달라진 조건은 4개다.
multiple/output 각각의 동의 ID 생략·공백 조건이 이에 해당한다.
나머지 조건에서 상태가 같아도 DTO 통과 이후 동작과 응답까지 같다는 뜻은 아니다.

## 2. ID·null 처리

| 조건 | develop | 현재 |
| --- | --- | --- |
| multiple `message_id=""` | 32자리 hex ID 생성 | 동일 |
| multiple `message_id="  "` | 공백 유지 | 공백 제거 후 새 ID 생성 |
| 두 API의 `message_id=" msg-1 "` | 공백 유지 | `msg-1`로 변경 |
| output `message_id="  "` | 공백 유지 | 빈 문자열. 자동 생성하지 않음 |
| multiple `attachments=null` | DTO 통과 뒤 첨부파일 변환에서 TypeError | 빈 첨부파일 목록으로 정리 |
| multiple `filter_list=null` | DTO 통과 뒤 순회 시 TypeError 가능 | 빈 요청 목록으로 정리 |
| multiple 사용자·첨부파일 속성의 null | 원본 null이 후속 코드로 전달될 수 있음 | 문자열은 빈 문자열, 숫자는 0 등으로 정리 |
| output 사용자 속성의 null | 후속 코드로 전달 | mapper도 null 유지 |

ID의 develop 열은 원본 route의 조건문 대조, 현재 열은 mapper 실행 결과다.
`attachments=null`의 develop TypeError는 원본 helper를 직접 실행해 확인했다.
null 목록의 API 최종 HTTP 응답은 서버 예외 처리에 의존하므로 422라고 단정하지 않는다.
현재 CSO가 활성화되어 있으면 빈 요청 목록도 선행 검사를 끄지 않는다.

## 3. multiple 성공 응답

기본 최상위 필드는 양쪽 모두 다음과 같다.

```text
api_code, api_message_code, api_command, final_decision, completed_date
```

`final_decision`의 필드는 양쪽 모두 다음과 같다.

```text
action, action_code, masked_contents, block_message, message_id
```

성공 envelope의 값은 `200`, `success`, `pipeline multiple filter`다.
INPUT에는 `out_result` wrapper가 없다.

| 같은 조건 | develop | 현재 | 근거 |
| --- | --- | --- | --- |
| 필터 없음·모두 미탐지 | `allow/0` | `""/0` | 취합기 실행 |
| accept 정책만 탐지 | `allow/0` | `accept/0` | 취합기 실행 |
| 같은 block 결과를 취합 | `block/1` | 같은 문자열을 주면 동일 | 취합기 실행 |
| 한 masking 결과를 취합 | `masking/2` | 같은 문자열·span을 주면 동일 | 취합기 실행 |
| 두 masking에 위치 근거가 있음 | 첫 masking 문자열 | 여러 위치를 병합한 문자열 | 취합기 실행 |
| 정규식 block 탐지 | 마스킹한 원문을 `masked_contents`에 포함 | `masked_contents=""` | 필터 코드 대조 |
| SLM masking 탐지 | SLM 마스킹 안내문 | 근거 span·provider 내용으로 원문 마스킹 | 필터 코드 대조 |
| SLM 통신 실패, 다른 탐지 없음 | 기본 경로에서 `allow/0` 유지 | unavailable이면 `failed/0` | 필터 코드 대조 |
| 필터에서 처리하지 않은 일반 예외 | 성공 응답으로 변환하지 않고 전파 가능 | inspector 실행 예외를 failed 결과로 변환 | 실행 코드 대조 |
| 차단·마스킹 우회 후 다른 결과 없음 | `allow/0` | `""/0` | 취합기 실행 |

11개 취합 조건 중 final_decision 전체가 같은 조건은 4개, 다른 조건은 7개였다.
이 수치는 동일한 필터 결과를 전달한 취합기 비교다.
예를 들어 block 취합 자체가 같아도 정규식 검사기가 만드는 masked_contents가 달라
실제 정규식 block 응답 전체는 달라질 수 있다.

복수 masking의 직접 실행 예:

```text
원문:    alpha secret omega token
develop: alpha [AIVAX MASKING] omega token
현재:    alpha [AIVAX MASKING] omega [AIVAX MASKING]
```

## 4. 실행 조건이 응답에 미치는 차이

| 조건 | develop | 현재 |
| --- | --- | --- |
| filter_list 순서 변경 | 요청 순서대로 실행 | regex → multimodal → slm → secuxper로 고정 |
| 같은 필터 이름 중복 | 차단 등 중단이 없다면 중복 실행 가능 | 한 번 실행 |
| 차단 뒤 후속 필터 | 기본 설정에서 미실행·결과 없음 | 미실행이지만 빈 기본 결과를 포함할 수 있음 |
| `next_detect_after_block=true` | 차단 뒤 검사도 실행 가능 | 일반 실행은 block 뒤 중단 |
| 차단 필터 뒤 unknown 필터 지정 | unknown에 도달하기 전에 중단하면 정상 block 응답 가능 | 시작 전에 전체 이름을 검증하므로 404 |
| `user_consent` 활성 + 유효한 ID | 추가 필드 무시, 일반 검사 | CSO 통과 후 일반 검사 생략·accept 가능 |
| CSO 선행 차단 조건 | 해당 기본 명령에 없음 | 일반 필터 실행 전에 block 가능 |
| Mirror | 해당 명령에 즉시 접수 분기 없음 | 즉시 빈 action/0 응답, debug 필드 생략 |

필터가 둘 이상 탐지하면 실행 순서 변화만으로 선택되는 차단 정책과 메시지도 달라질 수 있다.
CSO·동의·Mirror 조건을 끈 일반 Inline에서도 허용 action과 debug 차이는 남는다.

## 5. debug=true 응답 구성

일반 Inline에서 `filter_result`, `log_evidence`라는 최상위 키는 유지된다.
그러나 그 내부 구성은 동일하지 않다.

| 위치 | develop | 현재 |
| --- | --- | --- |
| `filter_result` | 실행한 pipeline별 원본 응답 | inspector별 공통 9개 필드 |
| `filter_result.input_filter` | 요청·실행한 경우 `{}` 포함 가능 | 이 이름은 로그 요청이므로 inspector 결과에 없음 |
| 차단 뒤 필터 키 | 기본 설정에서 없음 | 요청·활성화된 후속 필터의 기본 항목 포함 가능 |
| 필터별 부가정보 | 파일 summary 등 원본 응답에 존재 가능 | 공통 응답에서 제외. 필요한 상세는 log_evidence에 기록 |
| 입력 로그 없이 일반 허용 | filter_detect, mode, policy 등 취합 버퍼 존재 | 입력 로그가 없으면 `log_evidence={}` |
| `log_evidence` 사용자·서비스 | develop 입력 로그 구성 | ai_service_id, event_time 추가 등 변경 |
| SSL 우회 시 `log_evidence.mode` | 우회 전 탐지에서 취합 | 현재 최종 판정에서 구성하므로 달라질 수 있음 |
| 실패 상세 | 기존 필터별 동작에 의존 | error·evidence 등 현재 공통 로그 구조 |

현재 필터별 공통 9개 필드는 다음과 같다.

```text
action, action_code, masked_contents, block_message,
policy_id, policy_name, target, category, scope
```

## 6. output 성공 응답과 오류

일반 output 성공 응답의 필드 구성과 고정값은 유지된다.

```json
{
  "api_code": 200,
  "api_message_code": "success",
  "api_command": "output api router",
  "out_result": {},
  "completed_date": "YYYY-MM-DDTHH:mm:ss"
}
```

양쪽 기본 구현에서 `out_result`는 빈 객체다. debug=true여도 입력처럼 확장되지 않는다.
completed_date의 실제 시각이 같다는 뜻은 아니다.
OUTPUT 응답 구조가 유지돼도 공백 ID 제거 때문에 INPUT과의 병합 키는 달라질 수 있다.

| 조건 | develop | 현재 |
| --- | --- | --- |
| 기존 일반 필드 검증 오류 | 422, detail 배열 | 같은 형태. 세부 메시지는 라이브러리 버전 영향 |
| unknown 입력 필터 | 404 오류 envelope | 404 envelope 유지, 판정 시점·문구 변경 |
| unknown 오류 문자열 | `invalid pipeline, not exist pipeline, id = unknown_filter` | `unsupported input inspector: unknown_filter` |
| 출력 처리기 사용 불가 | 404 오류 envelope | 같은 계열. 사용 불가 판정 대상과 오류 문구는 다를 수 있음 |
| 동의 요청의 빈 ID | 해당 필드 무시 | 두 API 모두 422 |
| 동의 관련 예외 | 기본 계약에 없음 | controller까지 ConsentLogNotFoundError가 전파되면 409 |
| 재시도 가능한 저장·전달 실패 | 해당 동의 오류 계약 없음 | 503, detail 문자열 |

일반 출력의 병합 실패 반환값은 현재도 곧바로 HTTP 실패로 바꾸지 않는다.
동의 출력은 저장 결과가 성공이 아니면 503을 발생시킨다.
따라서 응답만으로 최종 저장 성공까지 확인할 수는 없다.

## 구현 근거

현재:

- [INPUT DTO](../src/controllers/input_analysis/v1/dto.py)
- [INPUT mapper](../src/controllers/input_analysis/v1/mapper.py)
- [INPUT use case](../src/application/use_cases/input_analysis/use_case.py)
- [판정 취합기](../src/domain/inspection_input/services/final_decision_resolver.py)
- [Regex inspector](../src/application/use_cases/input_analysis/regex_input_inspector.py)
- [SLM inspector](../src/application/use_cases/input_analysis/slm_input_inspector.py)
- [입력 로그 builder](../src/application/use_cases/input_analysis/input_analysis_log_builder.py)
- [OUTPUT DTO](../src/controllers/output_analysis/v1/dto.py)
- [OUTPUT mapper](../src/controllers/output_analysis/v1/mapper.py)
- [OUTPUT use case](../src/application/use_cases/output_analysis/use_case.py)
- [공통 HTTP mapper](../src/controllers/http_controller_mapper.py)

develop 원본은 현재 작업 트리 경로와 다르다. 기준 커밋에서 다음 파일을 대조했다.

```text
pipeline/api_modules/models/schemas.py
pipeline/api_modules/router/router_daemon_api.py
pipeline/api_modules/router/sub_modules/filter_pipeline_command.py
pipeline/api_modules/router/sub_modules/output_pipeline_command.py
pipeline/api_modules/router/sub_modules/api_router_impl_command.py
pipeline/api_modules/helper/router_custom_helper.py
pipeline/pipelines/detect_secrets.py
pipeline/pipelines/file_block_filter.py
pipeline/pipelines/slm_filter_v2.py
pipeline/pipelines/input_pass.py
pipeline/pipelines/output_pass.py
```

확인 예:

```bash
git show b05a06becb5e6430b603d0d4396e2e3afa4bdf9a:pipeline/api_modules/models/schemas.py
git show b05a06becb5e6430b603d0d4396e2e3afa4bdf9a:pipeline/api_modules/helper/router_custom_helper.py
```
