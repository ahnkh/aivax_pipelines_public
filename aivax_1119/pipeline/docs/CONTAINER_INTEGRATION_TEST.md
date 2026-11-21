# Pipeline / CSO 컨테이너 통합 테스트

실제 HTTP 요청으로 CSO 가드를 실행하고, `bulk_logs`에 기록한 로그를
Fluent Bit가 읽어 OpenSearch에 저장하는 경로를 검증한다.

```text
pytest → 호스트 Pipeline → 상주 CSO JVM (실제 Fasoo 라이브러리)
                 ↓
          지정한 bulk_logs 디렉터리
                 ↓ bind mount (read-only, /bulk_logs)
          Fluent Bit 컨테이너 → OpenSearch 컨테이너
                 Pipeline ↔ Valkey 컨테이너 (INPUT/OUTPUT 병합·이벤트)
```

## 실행

Pipeline 개발 의존성이 설치된 Python 3.13, JDK 17 이상(`java`·`javac`), Docker Engine과
Docker Compose v2 이상이 필요하다. 호스트의 `vm.max_map_count`는
262144 이상이어야 한다. Docker 이미지가 없으면 최초 실행 시 다운로드한다.

`apps/pipeline`에서 실행한다.

```bash
make setup-dev
make test                 # 유닛·API·E2E·Fluent Bit·CSO 컨테이너 전체 테스트
# 컨테이너 통합 테스트만 실행할 경우:
# bash src/extensions/cso/java/build.sh
# make test-integration
```

직접 실행하거나 특정 케이스만 선택할 수도 있다.

```bash
.venv-dev/bin/python -m pytest tests/integration --run-container-integration -v -s
.venv-dev/bin/python -m pytest tests/integration --run-container-integration -k O-allow
```

일반 `pytest` 실행에서는 이 테스트들을 skip한다. 명시적으로 실행했는데
Docker·JDK·JVM wrapper가 없거나 서비스가 시작하지 못하면 실패한다.
`make test`는 `--run-all`로 이 테스트와 HTTP/Valkey E2E, Fluent Bit 전송 테스트를
모두 활성화한다. Docker·Compose·JDK·커널 설정을 확인하고 JVM wrapper를 자동
빌드한다. 옵션은 Pipeline 루트 `conftest.py`에서 등록하므로 전체 수집에서도
`no option named 'run_container_integration'` 오류 없이 처리한다.

## bulk_logs 경로 지정

기본 경로는 `.runtime/container-integration/<실행 ID>/bulk_logs`다.
CLI 또는 환경변수로 **비어 있는 테스트 전용 디렉터리**를 지정한다.
상대 경로는 pytest를 실행하는 디렉터리를 기준으로 해석한다.

```bash
make test-integration INTEGRATION_TEST_ARGS='--bulk-logs-dir=/tmp/pipeline-test/bulk_logs -v -s'

AIVAX_INTEGRATION_BULK_DIR=/tmp/pipeline-test/bulk_logs \
  .venv-dev/bin/python -m pytest tests/integration --run-container-integration -v -s
```

fixture는 Pipeline의 `AIVAX_PIPELINE_LOG_BULK_DIR`와 Compose의 bind mount
source에 같은 절대 경로를 전달한다. Fluent Bit는 컨테이너 안에서
`/bulk_logs/aivax_log#*.json`을 읽는다. `--bulk-logs-dir`가 환경변수보다 우선한다.
기존 로그가 있는 디렉터리는 실행을 거부하며, 테스트 후 로그 디렉터리를 삭제하지 않는다.

## 현재 검증 범위

모든 요청은 `/v1/filter/multiple_filter`를 실제 HTTP로 호출한다.
Inline 모드와 `AIVAX_PIPELINE_CSO_ENABLED=on`을 사용한다.
가드 자체 테스트는 `filter_list=["input_filter"]`로 호출하여 파일 분석 필터 요청 없이도
CSO 가드가 실행되는지 확인한다. O 등급 후속 처리 테스트는
`["input_filter", "file_block_filter"]`로 실제 멀티모달 검사를 요청한다.
fixture는 멀티모달 필터·동의 안내를 켜고 테스트 전용 Valkey에 정책을 발행한다.
정규식·SLM·SecuXper 필터는 비활성화한다.

| 첨부파일 | API 판정 | OpenSearch `cso.status` |
| --- | --- | --- |
| O 등급 (3) | 허용 (`action=""`, code 0) | open |
| C 등급 (1) | block | blocked |
| S 등급 (2) | block | blocked |
| 등급 없는 DOCX | block | untagged |
| O + 등급 없는 DOCX | block | untagged |
| 존재하지 않는 파일 | block | error |
| O + C | block | blocked |
| O + S | block | blocked |
| C + S | block | blocked |
| C + 읽기 오류 | block | blocked |
| 미태깅 + C + 읽기 오류 | block | untagged |
| MIME 사전 판독에서 미지원인 TXT·PNG (각각 별도 케이스) | block | error |
| 일반 ZIP (HWPX로 매핑하여 실제 등급 조회) | block | untagged |
| 열기 암호가 있는 DOCX(Standard·Agile) | block | error |
| 열기 암호가 있는 PDF | block | untagged |
| 실제 CSO JVM 응답 중단 → 다음 정상 요청 | block → 허용 | error → open |
| O + 멀티모달 정책 탐지 | block + 동의 안내 | open |
| O + 멀티모달 정책 미탐지 | 허용 + Management 첨부 이벤트 | open |
| O + 정책 차단 후 동의 재전송·OUTPUT | 허용 + 기존 색인 문서 갱신 | open |

일반 DOCX를 생성한 뒤 제공된 Fasoo 라이브러리의
`DocProperties.setCTProperty(path, "FDRClass", grade)`로 태깅한다.
`getCTProperty()` 재조회까지 성공해야 API 테스트를 진행한다.
미태깅 DOCX는 태깅 API를 호출하지 않은 일반 문서를 사용한다.
CSO 판정은 mock하지 않는다.
일반 필터 미요청·정책 미탐지 O 등급 요청의 허용 응답은 기존 계약상
`action=""`, `action_code=0`이다. 동의 재전송의 허용 응답은 `action=accept`다.
O 등급은 `/v1/filter/output_filter`까지 호출하여 INPUT/OUTPUT 병합 로그를 전달한다.
차단·오류는 OUTPUT 호출 없이 최종 로그가 저장되어야 한다.

테스트에서는 다음을 검증한다.

- API의 action, action_code, message_id와 차단 문구
- 호스트 spool에 같은 message_id의 로그가 정확히 한 건 기록됨
- Fluent Bit가 전달한 OpenSearch `_source.cso`와 spool의 값이 같음
- 전체 status 및 파일별 grade/status/path가 예상값과 일치함
- OpenSearch `_id=message_id`, `@timestamp` 날짜에 해당하는 일별 인덱스
- 허용 응답의 output_text 병합 및 차단 응답의 mode/output_text
- 일반 조회·파일 오류에서는 JVM을 재사용하고, JVM 장애 후에는 새 프로세스로 복구함

### 미태깅 첨부파일 차단 계약

`test_untagged_attachment_is_blocked_and_returns_block_prompt`는 미태깅 파일
단독과 O 등급 파일에 미태깅 파일이 섞인 경우를 별도 검증한다.
테스트의 준비·실행·검증 단계와 각 기대 동작은 한국어 주석으로 설명했다.

```bash
make test-integration INTEGRATION_TEST_ARGS='-k untagged -v -s'
```

미태깅 파일이 하나라도 있으면 API가 `action=block`, `action_code=1`과 함께
태깅 여부 및 지원 형식을 확인하라는 안내를 `block_message`로 반환해야 한다.
요청의 모든 첨부파일은 원래 입력 경로에서 보관 경로로 옮겨지며 내용이 보존되어야 한다.
OUTPUT API를 호출하지 않아도 차단 로그가 한 건 생성되고,
OpenSearch의 `output_text`가 응답 안내와 같아야 한다.

기존 구현에 대한 회귀 테스트로 작성했으며, 기대 문구와 판정을 테스트에 명시했다.
CSO 판정·응답·파일 보관은 실제 구현으로 실행한다.

### C·S 등급 첨부파일 차단 계약

`test_tagged_cs_attachment_is_blocked_and_returns_block_prompt`는
C 단독·S 단독·C+S·O+C·O+S의 5개 경우를 검증한다.
한국어 주석으로 준비·실행·검증 의도를 설명했다.

```bash
make test-integration INTEGRATION_TEST_ARGS='-k tagged_cs -v -s'
```

Fasoo 라이브러리로 C=1, S=2, O=3을 태깅한 DOCX를 사용한다.
C 또는 S 파일이 하나라도 있으면 요청 전체가 차단되어야 하며,
`block_message`에 C·S 등급 때문에 차단되었고 태깅 변경 후 다시 시도하라는
문구가 반환되어야 한다.

로그의 `cso.status`는 `blocked`, 각 파일의 상태는 `tagged`이고
원래 등급을 유지해야 한다. O 파일이 함께 있어도 첨부파일 전체를 보관하며
내용을 보존해야 한다. OUTPUT 호출 없이 최종 차단 로그가 한 건 저장되고
차단 문구와 CSO 값이 spool·OpenSearch에서 같아야 한다.
모든 C·S 케이스에서 상주 JVM PID가 유지되는지도 확인한다.

### 미지원 확장자와 CSO 모듈 장애

`test_unsupported_file_types_are_blocked_with_expected_cso_status`는 정상 TXT·일반 ZIP·PNG를
각각 생성해 MIME 사전 판독과 실제 등급 조회를 검증한다. 확장자만 바꾼 DOCX는 사용하지 않는다.
TXT·PNG는 MIME 사전 판독에서 미지원으로 분류하고, 일반 ZIP은 공통 매핑에 따라
HWPX로 조회한 뒤 등급 없음으로 처리한다. 다음 계약을 검증한다.

- TXT·PNG는 전체·파일별 `status=error`, 일반 ZIP은 전체·파일별 `status=untagged`
- 모두 `grade=null`이며 각 상태에 맞는 차단 안내와 `cso.message` 반환
- 첨부파일 보관·내용 보존, OUTPUT 없이 spool·OpenSearch에 최종 로그 한 건 저장
- 미지원 조회 후에도 같은 JVM을 유지

`test_unresponsive_cso_module_blocks_request_and_next_request_recovers`는 테스트
Pipeline의 실제 CSO JVM만 정지시켜 조회 타임아웃을 재현한다.
정상 O 등급 파일 두 개를 전달해도 해당 요청은 전체·파일별 `error`로 차단해야 한다.
파일 보관과 차단 안내의 spool·OpenSearch 저장도 검증한다.
다음 O 등급 요청은 새 JVM으로 복구하여 허용되고 INPUT/OUTPUT 로그도 정상 저장되어야 한다.
테스트 실패 시에도 정지된 프로세스를 남기지 않도록 해제한다.

```bash
make test-integration INTEGRATION_TEST_ARGS='-k "unsupported_file_types or unresponsive_cso" -v -s'
```

기동 시 vendor 모듈이 없는 경우는 단위 테스트
`test_real_java_missing_vendor_module_fails_startup_and_reaps_process`에서 검증한다.
원본 class/JAR를 삭제하지 않고 빈 vendor 경로로 실제 JVM을 실행하여 로드 실패를 재현한다.
준비 완료로 처리하지 않고 기동 예외를 전파하며 실패한 JVM을 정리해야 한다.
앱 기동 실패는 HTTP 요청을 처리한 결과가 아니므로 CSO 분석 로그가 생성되는 시나리오와 구분한다.

MIME에서 미지원으로 판독한 파일은 `error`, 실제 라이브러리에서 등급 없음으로 반환한
파일은 `untagged`다. `unsupported`나 `unavailable` 상태값을 추가하지 않는다.
현재 Management 조회기는 상위 `untagged`와 `skipped`를 허용하지 않으므로,
이 상태의 CSO 정보를 화면에 표시하려면 Management의 조회 스키마와 변환도 맞춰야 한다.

### 암호가 설정된 문서

`test_password_protected_document_is_blocked_with_expected_cso_status`는 실제 열기 암호가
설정된 DOCX Standard·DOCX Agile·PDF AES-256의 3개 케이스를 검증한다.
편집 제한 표시만 넣은 문서나 파일 확장자만 바꾼 데이터는 사용하지 않는다.

준비 단계에서 일반 문서에 Fasoo 라이브러리로 O 등급을 태깅하고 재조회한 뒤 암호화한다.
Office 문서는 제공된 POI 라이브러리, PDF는 기존 PyMuPDF 의존성을 사용한다.
helper는 잘못된 암호로 열리지 않는지, 올바른 암호로 원래 내용을 복원할 수 있는지 확인한다.
평문 임시 파일은 준비 단계 종료 시 삭제한다. Pipeline API에는 암호를 전달하지 않는다.

암호 DOCX는 MIME이 `application/encrypted`로 판독되고 지원 형식에 매핑되지 않아
실제 등급 조회 전에 전체·파일별 `status=error`, `grade=null`로 차단한다.
PDF는 지원 MIME이므로 실제 라이브러리로 조회하며, 암호 예외는 vendor 내부에서
처리되어 전체·파일별 `status=untagged`, `grade=null`로 반환된다.
각 상태에 맞는 조회 불가 안내 또는 태깅 절차 안내를 검증한다.
Management에서는 현재 CSO 필드만으로 암호 문서·미지원 형식·미태깅을 구분할 수 없다.

테스트는 암호화된 파일의 보관본이 원본 암호문과 바이트 단위로 같은지 확인한다.
OUTPUT 없이 차단 로그가 한 건 기록되고 Fluent Bit·OpenSearch에 같은 결과가 전달되며,
문서 조회 후 기존 상주 JVM을 유지하는지도 검증한다.

```bash
make test-integration INTEGRATION_TEST_ARGS='-k password_protected -v -s'
```

Mirror와 CSO OFF, 4 worker 부하 테스트는 이 통합 테스트에
포함하지 않는다. 성능 테스트는 [PIPELINE_LOAD_TEST.md](PIPELINE_LOAD_TEST.md)와
[CSO_LOAD_TEST.md](CSO_LOAD_TEST.md)를 따른다.

### O 등급 이후 정책 검사와 동의

[`test_cso_multimodal_api.py`](../tests/integration/test_cso_multimodal_api.py)는
실제 Valkey에 발행한 멀티모달 정책을 기준으로 3개 흐름을 검증한다.
CSO 태깅·본문 추출·정책 판정·파일 이동·Stream 발행·Fluent Bit 전달 모두 실제 구현을 사용한다.

1. O 등급 파일 본문에서 정책 문자열이 탐지되면 CSO는 `open`을 유지하지만
   최종 판정은 `block`이어야 한다. 응답에는 정책 이름과 ‘동의합니다’ 입력 안내를 반환하고,
   OUTPUT 호출 없이 정책 탐지·파일 보관·차단 안내를 OpenSearch에 기록한다.
2. 같은 정책에 걸리지 않는 O 등급 문서는 허용한다. 파일을 보관하고
   `management:file-asset:v1`에 해당 파일의 이벤트를 한 건 발행해야 한다.
   실제 직위자 배정·결재 처리·사용자 결재 안내는 Pipeline 책임 밖이므로 검증하지 않는다.
   OUTPUT 호출 후 정상 INPUT/OUTPUT 병합 로그가 저장되는지도 확인한다.
3. 정책 차단 요청을 같은 `message_id`와 `user_consent=true`로 재전송한다.
   이미 이동한 파일의 보관 경로에서 CSO를 재조회하고 일반 필터는 재실행하지 않는다.
   동의 입력 후 OUTPUT까지 보내면 `.json.consent`를 거쳐 원래 인덱스·같은 `_id`를 갱신한다.
   `user_consent=on`, 실제 AI 응답, 갱신한 CSO 정보를 확인하며 최초 `mode=block`·탐지 이력·정책은 보존한다.
   최초 create 로그와 첨부 이벤트는 한 건을 유지해야 한다.

Pipeline 공개 JSON 필드는 `user_consent`다. 동의 입력의 HTTP 성공만으로 최종
OpenSearch 반영을 간주하지 않고 `user_consent=on`과 실제 출력이 조회될 때까지 기다린다.

```bash
make test-integration INTEGRATION_TEST_ARGS='-k open_file_policy -v -s'
# pytest를 직접 실행할 때는 다음 경로를 사용한다.
.venv-dev/bin/python -m pytest tests/integration/test_cso_multimodal_api.py --run-container-integration -v -s
```

## 격리와 결과 확인

[Compose](../tests/integration/compose.yml)는 이미지 digest를 고정하고 실행마다
별도 프로젝트·네트워크·OpenSearch volume을 생성한다. OpenSearch와 Valkey는
loopback의 임의 포트에 게시한다. 기존 개발·운영 컨테이너에 연결하지 않는다.
테스트 전용 OpenSearch는 demo TLS와 고정 테스트 계정을 사용한다.

Fluent Bit는 저장소의 `infra/fluent-bit/fluent-bit.conf`와 `parsers.conf`를
읽기 전용으로 마운트하여 사용한다. flush/파일 감지 주기와 색인 지연을 고려해
문서 도착을 최대 45초 동안 polling한다.

Pipeline은 호스트에서 별도 프로세스로 실행한다. `.env`와 기존 `AIVAX_*` 환경은
읽지 않고 fixture가 만든 설정을 사용한다. 첨부파일 보관·이벤트 기록도 실제로
실행하되, 테스트 app factory에서 첨부파일 알림 서비스의 허용 보관 경로를
해당 테스트의 runtime 디렉터리로 한정한다. 원본 vendor Java/JAR는 수정하지 않는다.

테스트 데이터 생성은 [`scripts/cso_fixtures.py`](../scripts/cso_fixtures.py)와
[`scripts/java/CsoFixtureTagger.java`](../scripts/java/CsoFixtureTagger.java)가 담당한다.
helper는 `setCTProperty()`로 태깅하고 `getCTProperty()`로 값이 같은지 확인한다.
실패하면 테스트 준비 단계에서 오류를 내며, XML을 직접 수정하는 대체 경로는 없다.
이 helper만 임시 디렉터리에 프로세스당 한 번 컴파일하며 종료 시 삭제한다.
암호화 문서 준비용 `scripts/java/CsoFixtureEncryptor.java`도 함께 컴파일한다.
이 helper는 기존 POI API로 Office 문서를 암호화하고 복호화 검증을 수행한다.
태깅·암호화 helper의 JVM에는 프로세스별 임시 디렉터리를 `java.io.tmpdir`로 전달한다.
POI 임시 파일은 해당 디렉터리에 생성하여 다른 계정의 `/tmp/poifiles`와 분리한다.
운영 Pipeline의 상주 JVM 코드와 vendor 소스·class·JAR는 이 과정에서 빌드하지 않는다.
단위 테스트의 실제 JVM 시나리오와 부하 테스트의 자동 생성 문서도 같은 helper를 사용한다.

결과는 `.runtime/container-integration/<실행 ID>/`에 남는다.

- `pipeline.log`, `compose-up.log`, `compose.log`, `compose-down.log`
- `fluent-bit/fluent-bit.log`, tail DB
- `endpoints.json`: 실행 중 사용한 API·OpenSearch·Valkey 주소와 bulk_logs 경로
- `cso-integration-*.json`, `cso-untagged-*.json`, `cso-cs-*.json`:
  성공한 케이스의 요청·응답·OpenSearch 조회 결과
- `cso-unsupported-*.json`, `cso-module-unavailable-*.json`:
  미지원·모듈 장애 케이스의 요청·응답·로그, 복구 요청 결과와 JVM PID
- `cso-password-protected-*.json`: 암호 문서 케이스의 요청·차단 응답·OpenSearch 조회 결과
- 기본 bulk_logs, 테스트 runtime과 파일 보관 결과

성공·실패 모두 fixture 종료 시 Pipeline을 종료하고 해당 Compose 프로젝트와
volume을 정리한다. 호스트 로그와 결과 파일은 보존한다.

## 실행 확인

2026-10-17: 공백이 포함된 별도 `--bulk-logs-dir` 경로로 실행하여
**7 passed / 62.08초**를 확인했다. 컨테이너·네트워크·volume 정리도 완료했다.
관련 CSO·가드 수명주기·동의·계층 의존성 회귀 테스트는 **155 passed**,
기본 실행의 통합 테스트는 **7 skipped**, 추가 Python 코드의 Ruff 검사도 통과했다.

미태깅 전용 계약 테스트 추가 후에는 미태깅 단독·O 등급 혼합 2개를 포함해
**8 passed / 67.26초**를 확인했다. 실제 JVM 판정, 차단 안내 반환,
첨부파일 이동·내용 보존, Fluent Bit 수집과 OpenSearch 저장을 모두 검증했다.

C·S 전용 계약 테스트로 기존 단순 등급 검증을 확장한 후에는
C 단독·S 단독·C+S·O+C·O+S를 포함해 **10 passed / 76.90초**를 확인했다.
정확한 재태깅 안내, 첨부파일 전체 보관·내용 보존, 파일별 원래 등급 기록,
차단 로그 전달과 상주 JVM 재사용을 실제 컨테이너 환경에서 검증했다.

태깅을 Fasoo `setCTProperty()` 호출로 교체한 뒤 **10 passed / 77.44초**,
관련 회귀 테스트 **155 passed**를 확인했다. 라이브러리 태깅 fixture로 4 worker
CSO 요청 1,256건과 Pipeline DOCX+CSO 292세션의 짧은 실행도 통과했다.
각 실행의 오류는 0건이며 Pipeline 로그 누락·중복·잘못된 로그도 0건이었다.
이는 생성 방식 변경 후 동작 확인이며 기존 부하 측정 결과의 대체 수치가 아니다.

미지원 TXT·일반 ZIP·PNG와 실제 JVM 응답 중단·복구 검증을 추가한 뒤
**14 passed / 107.25초**, 관련 회귀 테스트 **156 passed**를 확인했다.
미지원은 `blocked/untagged`, 모듈 응답 중단은 `error/error`로 차단되었고,
다음 O 등급 요청은 새 JVM에서 `open`으로 복구됐다.
빈 vendor 경로를 사용한 실제 JVM 기동 실패·프로세스 정리 테스트도 통과했다.

실제 열기 암호가 설정된 DOCX Standard·DOCX Agile·PDF AES-256을 추가한 뒤
**17 passed / 121.69초**, 관련 회귀 테스트 **156 passed**를 확인했다.
3개 모두 전체 `blocked`, 파일별 `untagged`, `grade=null`로 차단됐으며,
차단 안내 반환·암호화 원본 보관·Fluent Bit 수집·OpenSearch 저장을 검증했다.

O 등급 이후 실제 멀티모달 정책 탐지·미탐지·동의 재전송 3개를 추가한 뒤
**20 passed / 146.98초**, 관련 회귀 테스트 **156 passed**를 확인했다.
정책 차단 시 동의 안내, 미탐지 시 허용·첨부 Stream 발행,
동의 후 원래 OpenSearch 문서의 `user_consent=on`·AI 응답 갱신을 검증했다.
원본 인덱스·문서 ID·시각·정책·탐지 이력을 보존하고 첨부 이벤트 중복도 없었다.
