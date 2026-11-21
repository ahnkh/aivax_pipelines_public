# 사용자 동의 입력·출력 로그 병합

Inline에서 `user_consent=true`인 요청은 CSO 등급만 확인하고 일반 필터를 생략한다.
허용된 동의 입력도 기존 입력·출력 로그 병합 저장소에 저장하고, 실제 AI 출력과
병합한 뒤 Fluent Bit 갱신 파일로 전달한다. 운영·개발 env는 이 저장소로 Valkey를 사용한다.
Resume 전용 키나 완료 필터 목록은 사용하지 않는다.

## 요청 흐름

1. 엔진이 일반 입력 API를 호출한다.
2. 차단되면 프롬프트·탐지 결과·차단 문구를 포함한 로그를 `.json`에 기록한다.
   Fluent Bit가 `create`로 OpenSearch에 적재한다.
3. 사용자가 동의하면 엔진은 원래 요청 값을 유지하고 `user_consent=true`로 다시 입력을 호출한다.
4. CSO 차단이면 그대로 차단한다. 그 외에는 일반 필터를 전부 생략하고 입력 로그를 생성한다.
   입력 로그에 `user_consent=on`과 원본 문서의 날짜 정보를 넣어 Valkey에 저장한 뒤 허용한다.
5. 엔진이 AI를 호출하고 같은 `message_id`로 출력 API를 호출한다.
6. 출력 API는 일반 `record_output()` 경로에서 Valkey 입력 로그와 AI 출력을 병합한다.
7. 동의 병합 로그는 `.json.consent`로 기록하고 Fluent Bit가 원래 날짜 인덱스의
   같은 문서를 `update`한다.

출력이 먼저 도착하면 기존 병합 저장소가 출력을 대기시켰다가 동의 입력 도착 시 병합한다.
동의 여부는 입력 로그에서 가져오므로 출력 요청에 `user_consent`를 다시 보낼 필요가 없다.
보내는 경우에도 동일한 병합 경로를 사용한다.
동의 입력은 `filter_list`에 `input_filter`가 없어도 병합 저장소에 기록한다.

Mirror 입력은 CSO와 동의 분기를 적용하지 않고 즉시 허용한 뒤 일반 필터를
백그라운드에서 분석·기록한다.

## API 계약

입력은 `POST /v1/filter/multiple_filter`, 출력은 `POST /v1/filter/output_filter`다.
동의 입력에는 원래 요청 값과 원래 `message_id`를 사용한다.

```json
{
  "message_id": "original-message-id",
  "prompt": "원래 사용자 입력",
  "user_consent": true
}
```

```json
{
  "message_id": "original-message-id",
  "llm_output": "동의 후 실제 모델 응답"
}
```

JSON boolean `true`와 문자열 `on`, `1`, `true`, `yes`, `enable`, `enabled`를
동의로 해석한다. 문자열의 대소문자와 앞뒤 공백을 무시한다.
미전달·빈 문자열·`off`·`false` 입력은 일반 분석이다.
원래 ID가 없는 동의 요청은 HTTP 422다. 프롬프트의 ‘동의합니다’를 직접 인식하지 않는다.
전역·필터별 동의 옵션은 일반 차단 안내 문구를 결정하며 동의 입력의 필터 생략을 제한하지 않는다.

출력 성공의 `out_result`는 일반 출력과 같은 `{}`다.
기존 `user_consent_log_update=queued/skipped` 응답은 사용하지 않는다.
HTTP 성공은 최종 OpenSearch 반영 완료를 뜻하지 않는다.
동의 입력의 병합 저장 실패는 HTTP 503이다.

## CSO와 기존 로그 보존

CSO가 활성화되고 첨부파일이 있으면 동의 입력에서도 등급을 확인한다.
모두 O(3)일 때만 허용하고, C/S·미태깅·조회 오류는 차단한다.
이미 이동한 파일은 기존 차단 로그의 보관 경로로 조회한다.
CSO 차단에서는 일반 필터와 동의 입력의 Valkey 적재를 생략하고,
기존 파일 보관·차단 로그 처리 경로를 사용한다.

동의 입력에서 원본 차단 로그의 `@timestamp`를 확보하고, 병합 대기 중에는
`_consent_log_timestamp`로 원본 날짜를 보존한다. AI 출력 시각이 달라져도 같은
날짜 인덱스를 갱신하며 이 내부 필드는 OpenSearch에 기록하지 않는다.
갱신 파일에는 원본 `@timestamp`, `message_id`와 다음 필드만 담는다.

- `output_text`: AI 응답으로 기존 차단 문구 대체
- `text_truncated_bytes`: 기존 출력 저장 정책 적용 결과
- `user_consent`: 문자열 `on`
- `cso`: 동의 입력에서 조회한 결과가 있을 때만 포함

최초 `mode=block`, 탐지 결과, 정책, 마스킹, 사용자 정보는 덮어쓰지 않는다.
`effective_decision`이나 새 `block_bypassed`는 기록하지 않는다.
CDS·사용자·정책 이벤트도 최초 차단 때 발행한 것을 재발행하지 않는다.

원본 날짜 확인에 쓰는 `.consent-state`는 최초 저장 후 30분간 유효하다.
동의 입력에서 원본 날짜를 확보했다면 AI 처리 중 로컬 상태가 만료돼도 갱신할 수 있다.
원본 날짜를 확보하지 못했고 전달 시에도 유효한 원본 상태가 없으면 갱신을 생략한다.
이 경우에도 중복 문서나 부분 문서를 `create`하지 않는다.
동의 입력만 남아 만료 처리되면 동의만 기록하고 원래 차단 문구는 보존한다.

## Fluent Bit 전달

설정은 `infra/fluent-bit/fluent-bit.conf`와 `infra/fluent-bit/parsers.conf`를 사용한다.

| 경로 | 태그 | OpenSearch 작업 |
| --- | --- | --- |
| `aivax_log#YYYYMMDD#PID.json` | `aivax_log` | `create`, `Id_Key message_id` |
| `aivax_log#YYYYMMDD#PID.json.consent` | `aivax_consent` | `update`, `Id_Key message_id` |

`FLUENT_BIT_INPUT_PATH`는 `.json`으로 끝나는 glob을 사용한다.
갱신 tail과 DB는 `.consent`를 붙여 분리한다.
최초 create보다 update가 먼저 도착하면 Fluent Bit가 재시도한다.
기존 만료 상태 정리와 bulk 로그 백업 cron은 유지한다.

## 검증

```bash
cd apps/pipeline
make test
# 특정 검증만 개별 실행할 때:
.venv-dev/bin/python -m pytest tests/consent/test_user_consent_flow.py tests/consent/test_user_consent_valkey_flow.py tests/consent/test_input_analysis_consent.py tests/cso/test_cso_grade_processing.py
FLUENT_BIT_BIN=/path/to/fluent-bit .venv-dev/bin/python -m pytest tests/consent/test_user_consent_fluent_bit.py
.venv-dev/bin/python -m pytest tests/integration/test_cso_multimodal_api.py --run-container-integration -v -s
```

`make test`는 아래 개별 검증을 모두 포함한다. E2E용 Valkey와 Fluent Bit도
테스트 전용으로 실행하므로 별도 활성화 환경변수는 필요하지 않다.

실제 Valkey와 독립된 입력·출력 worker로 입력 적재, 출력 병합, 도착 순서 변경,
원본 날짜 보존, 동의 출력 플래그 생략, 기존 탐지 이력 보존을 검증한다.

컨테이너 통합 테스트는 실제 O 등급 DOCX·멀티모달 정책으로 최초 차단을 만든 뒤
`user_consent=true`와 OUTPUT을 호출한다. 실제 Fluent Bit·OpenSearch에서 같은 문서가
`user_consent=on`과 AI 응답으로 갱신되는지 검증한다.
실행 환경과 경로 지정은 [컨테이너 통합 테스트 문서](CONTAINER_INTEGRATION_TEST.md)를 따른다.
