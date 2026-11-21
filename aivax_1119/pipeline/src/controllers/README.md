# 컨트롤러 폴더

api endpoint를 받는 곳.

- 입력 값 검사를 수행한다.
- use case를 호출하고 외부 계약에 맞게 데이터를 변환한다.

## 하위 폴더

| 폴더 | HTTP 계약 |
|------|-----------|
| `input_analysis/v1`, `input_analysis/v2` | `POST /v1/filter/multiple_filter`, `/v2/filter/multiple_filter` |
| `output_analysis/v1`, `output_analysis/v2` | `POST /v1/filter/output_filter`, `/v2/filter/output_filter` |
| `test_filter_rule` | `POST /v1/filter/testrule` |
| `policy_refresh` | `POST /v1/filter/policy-refresh` |
| `health` | `GET /`, `/v1`, `/v1/health`, `/v1/health/live`, `/v1/health/ready` |
| `http_controller_mapper.py` | controller mapper의 공통 응답 생성 기반 |

입력·출력은 기존 기능 폴더 아래 `v1/`, `v2/`로 나눈다. 각 버전 폴더는
`dto.py`, `mapper.py`, controller 파일로 구성한다. DTO는 HTTP
입력 검증, mapper는 HTTP 계약과 application/domain 모델 변환, controller는 use
case 호출만 담당한다.

`health/`는 Management의 `status`, `timestamp`, `checks` 응답 계약을 그대로
사용한다. 준비 확인 실패는 HTTP 503이며 기본·live 조회는 외부 의존성을 검사하지
않는다. 실제 Valkey·파일 시스템 진단은 `adapters/health`에 두고
`CheckHealthUseCase`가 제한 시간과 결과 집계를 담당한다.
간단 상태 조회 `/v1`과 smoke 도구용 `/`는 `{"status":true}`를 반환한다.

## 입력 분석 V1

`input_analysis/v1/`는 외부 `filter_list` 이름을 보존해 application 요청으로 변환한다.
어떤 inspector를 어떤 순서로 실행할지는 controller가 아니라
`InputAnalysisUseCase`가 결정한다.

## 출력 분석 V1

`output_analysis/v1/`는 `/v1/filter/output_filter`의 controller, 요청 DTO,
입력 mapper, 응답·HTTP 예외 변환을 소유한다. MainApp에서 생성자로 use case를
주입하며 Depends나 앱 state에서 use case를 조회하지 않는다.

출력 경로는 `src` 외부의 레거시 모듈을 import하지 않는다. 독립성 테스트는
패키지 초기화를 포함한 import 그래프와 레거시 import를 금지한 별도 프로세스에서
HTTP → 메모리 병합 → JSON 기록까지 검증한다.

## 입력·출력 V2

`input_analysis/v2/`와 `output_analysis/v2/`가 각각 요청·응답 DTO, mapper,
controller를 소유한다. 두 버전은 같은 분석 유스케이스를 공유한다.
공통 오류 DTO와 HTTP 예외 변환은 `http_error_v2.py`에 둔다.
CSO 상태와 안내는 `input_analysis/cso_response.py`에서 공통으로 만들며,
두 버전 모두 입력 응답의 `cso`에 반환한다. CSO 미수행은 `null`이다.

## 필터 규칙 테스트 V1

`test_filter_rule/` 은 `/v1/filter/testrule`의 DTO, mapper, controller를 소유한다.
controller는 `FilterRuleTestUseCase.execute()` 하나만 호출하며, management 호환을
위해 `regex_result`와 `slm_result`를 top-level에 두는 기존 응답 계약을 유지한다.
요청의 `policies`에는 regex·multimodal·SLM snapshot을 함께 전달할 수 있으며,
`subject`와 `attachments`를 기준으로 실제 할당 선택 로직과 파일 검사를 실행한다.
응답은 `multimodal_result`와 검사 전체의 `final_decision`을 추가로 제공한다.

테스트 경로는 전달받은 snapshot을 호출 범위에서만 사용하므로 Valkey 정책을
교체하지 않는다. 또한 운영 입력 use case를 거치지 않아 분석 로그와 OpenSearch
적재, 첨부 백업·전송을 수행하지 않는다.

파일 테스트는 `/v1/filter/testrule/upload`가 multipart의 `payload`와 `files`를
받는다. controller는 요청 전용 임시 디렉터리에 파일을 스트리밍하고 같은 use case를
호출하며, 응답 또는 예외 처리 직후 임시 파일을 제거한다. 영구 파일 경로나 파일
본문은 테스트 이력에 저장하지 않는다.

## 정책 Refresh V1

`policy_refresh/`는 `/v1/filter/policy-refresh` 요청 DTO와 응답 mapper를 소유한다.
controller는 갱신 Stream에 이벤트만 발행하고, 정책 cache 갱신은 각 worker의
Stream listener가 수행한다. AI 엔진용
`aivax_policy.json` 생성은 Management가 담당한다.
