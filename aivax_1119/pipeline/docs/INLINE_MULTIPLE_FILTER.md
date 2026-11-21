# Inline `multiple_filter` 처리

확인 기준: 2026-10-16 작업 트리. API는 `POST /v1/filter/multiple_filter`이며,
Inline은 검사와 로그 처리가 끝난 뒤 최종 판정을 반환한다.
Mirror는 CSO와 동의 경로를 적용하지 않고 즉시 허용한 뒤 필터를 백그라운드에서 실행한다.

## 설정

- `AIVAX_PIPELINE_CSO_ENABLED`로 Inline CSO 선행 등급 검사를 활성화한다.
- `AIVAX_PIPELINE_USER_CONSENT_GLOBAL_BYPASS_ENABLED`와 필터별 동의 옵션은
  일반 차단 응답의 동의 안내 문구를 결정한다.
- `user_consent=true`인 Inline 요청은 위 옵션과 관계없이 CSO만 확인하고 허용한다.
- Resume 전용 Valkey 상태와 키 prefix 설정은 사용하지 않는다.
- 개별 검사 활성화는 `mode.json`의 `filters.<종류>.enabled`로 정한다.
- `AIVAX_PIPELINE_SSL_PROXY_BYPASS_BITMASK`는 기존 차단·마스킹 우회 설정이다.

## 요청과 실행 순서

Controller → `InputAnalysisMapper` → `InputAnalysisUseCase.execute()` 순서로 진입한다.

| `filter_list` 값 | 역할 | 검사 순서 |
| --- | --- | --- |
| `input_filter` | 입력 로그 저장 요청 | 검사 필터가 아님 |
| `secret_filter` | 정규식 정책 검사 | 1 |
| `file_block_filter` | 첨부파일 멀티모달 검사·파일 보관 | 2 |
| `slm_filter` | SLM 정책 검사 | 3 |
| `secuxper_filter` | SecuXper 정책 검사 | 4 |

- 요청 목록에 있고, 전역 검사 설정이 켜져 있고, 구현이 주입된 필터를 실행한다.
- 요청 배열 순서는 실행 순서를 바꾸지 않으며, 중복 이름도 한 번만 실행한다.
- `filter_list`를 생략하면 위 다섯 이름이 기본값이다. `[]` 또는 `null`이면
  일반 검사와 입력 로그 저장 요청이 없다. 활성화된 CSO 검사는 별도다.
- 알 수 없는 이름은 HTTP 404로 거부한다.
- 각 검사에는 같은 원래 요청을 전달한다. 앞선 필터가 만든 마스킹 문자열을
  다음 필터의 입력으로 사용하지 않는다.
- 일반 요청의 빈 `message_id`는 서버에서 생성한다. 출력 병합과 동의 재개에는
  응답 ID 또는 클라이언트가 지정한 동일 ID를 사용한다.
- `encoding`은 공개 DTO에 남아 있지만 현재 mapper와 유스케이스에서 사용하지 않는다.

## 일반 입력의 검사와 판정

```text
필터 이름 검증 → 모드 확인 → CSO 검사(활성화·첨부 시)
  ├─ CSO 차단: 파일 보관 → 최종 차단 로그 전달 → block 응답
  └─ CSO 통과/비활성화:
       정규식 → 멀티모달 → SLM → SecuXper
         → 최종 판정 조합 → 입력/최종 로그 처리 → 응답
```

검사는 순차 실행한다. `block`이면 남은 필터 호출을 중단한다. `masking`,
`accept`, 빈 판정, `failed`는 다음 필터 검사를 계속한다. 검사 예외는 해당 필터의
`failed` 결과와 오류 근거로 바꾸며, 이후 필터의 차단 가능성을 확인한다.

우회 옵션이 없는 일반 판정 우선순위는 `block > masking > failed > accept > ""`다.
따라서 정규식이 마스킹해도 뒤의 SLM이 차단하면 최종 응답은 `block`이다.
마스킹은 각 필터의 원문 위치 근거를 합쳐 겹친 범위를 병합하며,
`[AIVAX MASKING]`으로 치환한다. 유효한 위치 근거가 없으면 첫 마스킹 결과를 쓴다.

차단 때문에 실행하지 않은 요청 대상 필터도 빈 결과 항목을 남긴다.
`debug=true`의 `filter_result`와 로그의 `filter_detect`에 포함될 수 있지만
실행 시각·소요 시간이 없고, 재개 시 수행 완료로 취급하지 않는다.
전역 비활성화 또는 요청에서 제외된 필터는 실행 계획에 포함하지 않는다.

`final_decision.action_code`는 `block=1`, `masking=2`, 나머지 `0`이다.
`failed`는 HTTP 오류와 별개이며 성공 envelope에 `action="failed"`,
`action_code=0`으로 반환될 수 있다. 실패의 세부 근거는 분석 로그에 저장한다.

SSL 우회는 검사 실행과 최종 판정을 구분해야 한다. `bypass_block`이 켜져도
필터의 `block`에서 뒤 검사를 중단한다. 판정 resolver는 우회 대상 `block` 또는
`masking`을 만나면 순회를 끝내는 기존 동작을 유지하므로, 우회 설정에서는 위의
일반 우선순위와 결과가 달라질 수 있다. 예를 들어 `bypass_block=true`이면
차단 전에 발견한 마스킹은 최종 판정에 남는다.

## CSO 선행 차단

CSO가 활성화되고 첨부파일이 있으면 `filter_list`와 독립적으로 등급을 조회한다.
모든 파일이 `tagged`이며 O 등급(`3`)일 때만 분석 필터로 진행한다.
C/S 등급, 미태깅, 조회 실패가 하나라도 있으면 Inline 분석 필터를 호출하지 않는다.
파일을 보관하고 `inspection_status="skipped"`, `skip_reason="cso_blocked"`와
등급 정보를 로그에 남긴 뒤 차단한다. SSL 우회와 사용자 동의로 승인하지 않으며,
동의 요청에서도 같은 CSO 차단 규칙을 적용한다.
CSO 차단 로그의 `filter_detect`에는 `regex`, `file block`, `slm` 항목을 남긴다.
미실행 정규식·SLM 항목은 빈 판정·정책·근거를 사용하며 SLM의 `slm_content`는
빈 문자열이다. 파일 보관 정보는 `file block.file_summary`에 기록한다.
기존 검사 결과가 있으면 해당 필터의 결과를 유지한다.

## 사용자 동의

같은 `message_id`와 `user_consent=true` 또는 `on`으로 재전송하면 CSO를 먼저
확인하고 일반 필터를 전부 생략한다. CSO 차단이 없으면 `accept`를 반환한다.
이미 실행한 필터를 복원해 이어서 실행하지 않으며, Valkey 재개 키를 조회하거나 저장하지 않는다.
이미 보관한 첨부파일의 CSO 조회에는 원본 로그의 보관 경로를 사용한다.
동의가 아닌 요청은 같은 ID로 재전송해도 처음부터 일반 필터를 실행한다.

동의 입력 로그는 일반 입력·출력 병합 저장소(Valkey)에 적재하고,
같은 ID의 실제 AI 출력과 병합한 뒤 `.json.consent`에 기록한다.
출력 요청에 동의 플래그를 다시 보내지 않아도 입력 로그에서 동의를 식별한다.
Fluent Bit가 원래 날짜 인덱스의 같은 문서에 `user_consent=on`과 AI 응답을 갱신한다.
기존 탐지 결과·대표 정책·마스킹·`mode=block`·시각은 보존한다.
원본 날짜를 확인할 수 없으면 중복 문서를 만들지 않고 최종 갱신을 생략한다.
동의 입력의 병합 저장 실패는 HTTP 503이고, 동의 요청의 ID가 없으면 HTTP 422다.
출력 응답은 일반 출력과 같은 빈 `out_result`이며 `queued/skipped`를 반환하지 않는다.
원래 프롬프트를 보내야 하며 Pipeline은 ‘동의합니다’ 문구를 직접 판별하지 않는다.

## 로그 저장

| 상황 | 로그 처리 |
| --- | --- |
| 일반 차단 | `input_filter`와 무관하게 차단 안내를 `output_text`에 넣어 최종 로그 직접 전달 |
| 허용·마스킹·실패 + `input_filter` | 입력 로그를 저장하고 같은 ID의 실제 `output_filter` 결과와 병합 |
| 허용·마스킹·실패 + `input_filter` 없음 | 입력 로그 저장 생략 |
| 사용자 동의 | 입력을 Valkey에 저장하고 출력과 병합한 뒤 기존 문서를 부분 갱신 |

동의 로그는 원래 `mode=block`을 유지하고 `user_consent=on`으로 동의를 기록한다.
새 필터 결과나 `block_bypassed`는 생성하지 않는다.
동의 출력은 같은 ID와 `user_consent=on`으로 기존 차단 문구를 AI 응답으로 갱신한다.
최초 판정이 마스킹인 요청은 출력 API의 일반 병합 경로를 쓴다.
상세 계약은 [USER_CONSENT.md](USER_CONSENT.md), JSON 구조는
[API_JSON_STRUCTURE.md](API_JSON_STRUCTURE.md)를 참고한다.

## 구현과 검증 위치

- HTTP 계약: `src/controllers/input_analysis/v1/{dto,mapper,input_analysis_controller}.py`
- 순서·중단·동의·로그 분기: `src/application/use_cases/input_analysis/use_case.py`
- 판정·마스킹 병합: `src/domain/inspection_input/services/final_decision_resolver.py`
- 로그 조립·결과 복원: `src/application/use_cases/input_analysis/input_analysis_log_builder.py`
- 회귀 검증: `tests/analysis/test_input_analysis_service.py`, `tests/analysis/test_input_analysis_controller.py`,
  `tests/consent/test_input_analysis_consent.py`, `tests/cso/test_cso_grade_processing.py`,
  `tests/consent/test_user_consent_flow.py`
