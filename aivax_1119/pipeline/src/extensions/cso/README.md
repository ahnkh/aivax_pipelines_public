# CSO 선택 기능

Fasoo와 MarkAny CSO를 사용하는 배포의 선행 가드와 조회 어댑터를 모은다.
`AIVAX_PIPELINE_CSO_ENABLED=true`일 때 bootstrap에서만 구현을 import하고 연결한다.
비활성 배포는 이 디렉터리와 `third_party/fasoo/cso`, `third_party/markany` 없이 실행할 수 있다.
공개 API·로그 호환성을 위한 `CsoGradeResult`와 `InputAnalysisResult`의 CSO 필드는
공통 domain에 유지한다.

## 조회 라이브러리 선택

`AIVAX_PIPELINE_CSO_PROVIDER`는 `fasoo` 또는 `markany`이며 기본값은 `fasoo`다.
기존 `CsoGradePort`에 선택한 어댑터를 연결하고 비활성 배포에서는 생성하지 않는다.
설정을 바꾸면 Pipeline을 재시작한다.

Fasoo는 기존 Java handler와 상주 JVM을 사용한다. 아래 JVM 수명과 타임아웃 설명은
Fasoo 선택에 해당한다. MarkAny 선택 시에는 Java 구현을 import하거나 JVM을 생성하지 않는다.

MarkAny는 [`third_party/markany/n2sf_read.py`](../../../third_party/markany/n2sf_read.py)의
`read_tags()`를 작업 스레드에서 파일별로 호출한다.
제공사 원본과 출처 안내는 `third_party/markany`에서 관리하고, 호출·결과 변환은
이 디렉터리의 `markany_grade_adapter.py`가 담당한다.
`OK`의 C/S/O를 기존 숫자 등급 1/2/3으로 변환하고, 등급이 없으면 `untagged`,
잘못된 등급·`ERROR`·호출 예외는 `error`로 처리한다. 한 파일이 실패해도 나머지
파일은 계속 조회한다. `decide()`는 호출하지 않는다.

`UNSUPPORTED`는 `grade=null`, `status=error`로 변환한다. 내부 `unsupported`
정보로 미지원 여부를 구분하며 외부 필드는 추가하지 않는다. 미지원 스킵 또는 오류
스킵이 켜져 있으면 `skipped`로 처리한다. 미태깅 스킵은 미지원에 적용하지 않는다.
미지원만으로 차단할 때는 요청 전체 상태도 `error`이며 기존 조회 불가 안내를 사용한다.

MarkAny의 의존성은 `olefile`과 암호화 PDF용 `pikepdf`다. 파일 내용을 판독하므로
Fasoo용 MIME 사전 판독으로 조회 전에 생략하지 않는다. 기존 JVM 타임아웃은 적용하지 않는다.

환경변수 `AIVAX_PIPELINE_CSO_ENABLED`는 `on`/`true`이면 활성화,
`off`/`false`이면 비활성화한다. 기본값은 `false`이며 OFF에서는 가드와 JVM을
생성하지 않는다. 실행 환경 파일을 변경한 뒤 Pipeline을 재시작해야 반영된다.
Mirror에서는 설정과 관계없이 가드 검사를 생략한다.

로그는 생략 정책을 적용한 뒤 `cso.status`로 `open`(O와 생략 파일만 있고 O가 있음),
`blocked`(C/S), `untagged`(생략하지 않은 등급 없음), `error`(생략하지 않은 조회 오류·미지원),
`skipped`(모든 파일이 생략 정책 적용 대상)를 구분한다.
상위 상태는 미태깅 → C/S → 조회 오류·미지원 → O → 모두 생략 순으로 집계한다.
Inline의 `untagged`·`blocked`·`error`는 모두 요청을 차단한다. 파일별 상태는 `cso.files[].status`에
유지한다. 이전 boolean 필드 `cso.blocked`는 새 로그에서 사용하지 않는다.

## 입력 API 응답

V1·V2 `multiple_filter` 정상 응답은 최상위 `cso`를 반환한다. V1의 `debug` 값과 무관하다.
CSO 결과가 없으면 `null`, 결과가 있으면 `status`와 `message`만 담는다.
HTTP mapper에서 공통 domain의 등급 결과와 최종 판정을 사용해 안내를 만들며,
CSO 확장 구현이나 Java에 대한 의존성을 추가하지 않는다.

- 미태깅: `untagged`와 보안등급 태깅 절차 안내.
- C/S: `blocked`와 전송 가능한 등급으로 변경하는 절차 안내.
- O + 일반 필터 차단: `open`과 결재 상신·동의 후 재전송 안내.
- O + 허용: `open`과 결재 상신 안내. 마스킹·빈 판정도 같은 안내를 사용한다.
- O + 필터 처리 실패: `open`과 결재 상신 안내.
- 조회 오류·미지원: `error`와 조회 실패 안내.
- 모두 생략: `skipped`와 빈 안내 문자열 (`""`).

생략 옵션 적용 후의 상태를 사용하며, CSO 미수행은 `skipped`와 구분해 `null`이다.
여러 파일이 섞이면 `untagged` → `blocked`(C/S) → `error` → `open` → `skipped`
순으로 우선하며, 가장 높은 상태와 해당 안내만 반환한다.
API·OpenSearch 로그·`log_evidence.cso.status`는 같은 domain 집계 함수를 사용한다.
미태깅 차단은 로그의 요청 전체 상태에도 `untagged`로 기록한다.
파일별 결과는 모두 유지하며 기존 저장 문서는 변경하지 않는다.
기존 최종 판정·차단 문구·OpenSearch 로그 구조와 결재 처리 흐름은 유지한다.
전체 응답 계약은 [`API_JSON_STRUCTURE.md`](../../../docs/API_JSON_STRUCTURE.md)를 따른다.

## 빈 첨부 경로 조회 생략

첨부파일의 `id`가 누락·null·빈 문자열·공백뿐인 경우 HTTP mapper와 가드에서
파일이 아닌 빈 첨부 칸으로 제거한다. 이 항목을 Java에 전달하거나 파일별 로그에
기록하지 않는다. 남은 파일이 없으면 응답의 `cso`는 `null`이다.

실제 파일의 순서와 C/S·등급 없음·오류 판정은 유지한다.
일반 요청은 CSO 통과 후 요청한 필터를 계속 수행한다.
동의 재요청은 기존 보관 경로를 복원한 뒤 빈 경로인지 판단한다.

## 미지원 형식 조회 생략

Fasoo는 스킵 옵션과 관계없이 `LocalFileMetadataReader`로 파일 내용의 MIME을 판독한다.
멀티모달과 같은 MIME → 형식 매핑을 사용하며, 매핑된 형식이
`doc`, `docx`, `xls`, `xlsx`, `ppt`, `pptx`, `hwp`, `hwpx`, `pdf` 밖이면
라이브러리를 호출하지 않고 `grade=null`, `status=error`, 내부 `unsupported=true`로 처리한다.
MarkAny는 `UNSUPPORTED` 응답을 같은 결과로 변환한다.
`AIVAX_PIPELINE_CSO_SKIP_UNSUPPORTED_EXTENSIONS_ENABLED`와 오류 스킵은 기본값이 `false`다.
둘 중 하나라도 `true`/`on`이면 미지원 결과를 `skipped`로 바꾼다.
파일명과 요청의 `mime_type` 값은 판별에 사용하지 않는다. 확장자가 없는 파일도
같은 기준을 적용한다. 설정 변경 후 Pipeline을 재시작한다.

이 목록은 fdoc-handler 매뉴얼의 지원 형식에 따른 Pipeline 정책이다.
Fasoo 공개 API에는 지원 여부를 확인하는 별도 메서드가 없으며,
라이브러리 내부는 파일 시그니처로 형식을 판별한다. DOCX의 이름을 PNG로 바꿔도
문서 내용으로 판별해 조회한다. PNG에 DOCX 이름을 붙여도 미지원으로 분류한다.
MIME 판독은 작업 스레드에서 실행한다. 판독에 실패하면 warning과 traceback을
기록하고 실제 등급 조회를 계속한다. 이후 조회 오류에는 기존 오류 스킵 옵션을 적용한다.
공통 매핑에서 `application/zip`은 `hwpx`다. `application/octet-stream`은 `docx`로
보지 않는다. 본문이 `HWP Document File`로 시작하거나 OLE `FileHeader`가 그 서명이면
`hwp`로 조회하고, `word/document.xml`이 있는 ZIP만 `docx`로 조회한다. 어느 쪽도
아니면 미지원이다. 멀티모달과 판독기는 공유하지만 허용 목록과 스킵 정책은 독립적이다.

생략 파일은 `grade=null`, `status=skipped`로 원래 첨부파일 순서에 기록한다.
CSO 파일별 로그는 `file_name`, `file_path`, `grade`, `status`만 기록한다.
O 등급으로 바꾸지 않으며, C/S 차단은 유지한다.
미태깅과 오류는 각각 아래 등급 없음·조회 오류 생략 옵션이 꺼져 있으면 차단한다.
조회 예외가 발생해도 생략 파일의 상태는 유지한다. 생략과 O만 있으면 CSO는
차단하지 않지만 일반 요청의 후속 필터는 계속 실행한다. 동의 요청에도 같은
생략 기준을 적용하고 이후에는 기존 동의 흐름을 따른다.
Fasoo에서 모든 파일이 미지원이면 스킵 여부와 관계없이 JVM 조회를 호출하지 않으며, CSO 활성 시 앱 시작의 JVM
기동·준비 확인은 기존대로 수행한다.

Management에서 조회하려면 신규 `skipped` 상태를 수용하는 버전이 필요하다.

## 조회 후 등급 없음 차단 생략

`AIVAX_PIPELINE_CSO_SKIP_UNTAGGED_ENABLED`는 기본값 `false`이며,
`true`/`on`이면 실제 CSO 조회 결과가 `untagged`인 파일을 차단 대상에서 제외한다.
`false`/`off`이면 기존처럼 차단한다. 설정 변경 후 Pipeline을 재시작한다.
미지원 형식 생략 옵션과 독립적이며, 이 옵션만 켜도 조회 후 등급 없음은 생략한다.

해당 파일은 `grade=null`, `status=skipped`로 기록한다.
Fasoo에서 MIME으로 미지원을 구분하지 못하면 라이브러리의 `untagged`에
미지원 형식·일부 암호 문서가 포함될 수 있다. 미지원으로 판별한 `error`에는
이 옵션을 적용하지 않는다. C/S 등급은 생략하지 않는다.
`error`와 통신·응답 파싱·결과 개수 오류는 아래 조회 오류 생략 옵션을 따른다.

일반 요청의 후속 필터와 동의 요청의 기존 흐름은 유지한다. 다른 파일이 C/S나
생략하지 않은 오류이면 요청을 차단하고, 안내에는 생략한 파일을 차단 원인으로 넣지 않는다.
모두 생략되면 상위 상태는 `skipped`, 생략과 O만 있으면 `open`으로 기록한다.
CSO 비활성과 Mirror 모드에서는 이 옵션에 관계없이 CSO 조회를 수행하지 않는다.

## 조회 오류 차단 생략

`AIVAX_PIPELINE_CSO_SKIP_ERROR_ENABLED`는 기본값 `false`이며,
`true`/`on`이면 CSO 조회 결과가 `error`인 파일을 차단 대상에서 제외한다.
`false`/`off`이면 기존처럼 차단한다. 다른 생략 옵션과 독립적이며,
설정 변경 후 Pipeline을 재시작한다.

미지원 형식, 파일별 라이브러리 오류와 조회 중 발생한 JVM 요청 타임아웃·통신·응답 파싱·결과
개수 예외에 적용한다. 요청 전체 조회가 실패하면 조회 대상 파일 모두 `error`로
처리한 뒤 생략한다. 이 경우 개별 등급을 확인할 수 없으므로 C/S 여부도 알 수 없다.
정상 응답에서 확인한 C/S 등급은 계속 차단하며, 등급 없음은 별도 옵션을 따른다.
조회 예외의 서버 로그와 Java stderr 로그는 기존대로 유지한다.

생략 파일은 `grade=null`, `status=skipped`로 기록한다. 파일별 필드는
`file_name`, `file_path`, `grade`, `status`만 유지하며 별도 사유 필드를 추가하지 않는다.
모두 생략되면 상위 상태는 `skipped`, 생략과 O만 있으면 `open`이다.
일반 요청의 후속 필터는 계속 실행하며 해당 필터가 차단할 수 있다.
동의 요청은 CSO 통과 후 기존 동의 흐름을 따른다.
앱 시작 시 JVM 기동·준비 실패와 요청 취소에는 적용하지 않는다.
CSO 비활성과 Mirror 모드에서는 이 옵션에 관계없이 CSO 조회를 수행하지 않는다.

| 파일 | 책임 |
| --- | --- |
| `guard.py` | CSO 허용/차단, 동의 재전송의 보관 경로 복원, 차단 파일 보관·이벤트·로그 |
| `fasoo_grade_adapter.py` | `CsoGradePort` 구현, 응답 JSON과 등급 검증 |
| `jvm_process.py` | worker별 JVM 수명, 요청 직렬화, 타임아웃·취소·장애 회수 |
| `protocol.py` | stdio 요청 프레임 |
| `java/CsoJvmWorker.java` | 기존 제공 handler를 호출하는 Pipeline 소유의 상주 통신 진입점 |

`third_party/fasoo/cso`의 등급 조회 로직과 Fasoo JAR는 유지한다.
`CsoGradeHandler`의 로그 문구를 변경하면 Java 17로 빌드하여 소스와 `.class`를
함께 갱신한다. 이번 변경은 로그 문구의 영어화이며 조회·오류 판정은 유지한다.
worker는 같은 JVM에서 기존 `CsoGradeHandler.main(paths)`을 호출하고 마지막 stdout
줄의 JSON 배열을 프레임으로 전달한다. 기존 등급 조회·오류 처리 코드를 그대로 사용한다.
JVM과 로드한 클래스는 재사용하고, `DocProperties` 생성 수명은 기존 handler에 맡긴다.

worker를 수정했을 때만 Pipeline 루트에서 다음 명령으로 별도 class를 갱신한다.
제공 파일을 다시 컴파일하지 않도록 sourcepath를 worker 디렉터리로 제한한다.
배포에는 `java/CsoJvmWorker.class`를 함께 포함하며 운영 시 컴파일하지 않는다.

```bash
bash src/extensions/cso/java/build.sh
```

공통 입력 use case는 `InputGuardPort`만 의존한다. controller가 DTO를 검증·변환한 뒤,
use case가 Mirror를 제외하고 일반 필터 또는 동의 허용 전에 가드를 호출한다.
가드의 `blocked_result`가 있으면 후속 분석을 중단한다. 통과 결과의 `apply()`는
조회 결과를 분석 결과에 반영하여 기존 로그 빌더로 전달한다.

## 파일별 디버그 로그

`AIVAX_PIPELINE_LOG_LEVEL=debug`로 설정하고 Pipeline을 재시작하면 실제 CSO
조회 대상 파일마다 조회 호출 직전에 DEBUG 로그를 한 번 기록한다.
요청의 `debug=true`와는 별개이며, 빈 경로나 형식 정책으로 조회를 생략한 파일은
기록하지 않는다.

`Starting CSO file lookup` 로그는 `message_id`, 0부터 시작하는 `file_index`,
`file_name`, `file_path`, `absolute_path`를 포함한다. 동의 재요청은 보관 경로 복원
후의 조회 경로를 기록한다. 문자열은 repr 형식으로 출력하여 빈 값과 개행을 구분한다.
절대 경로는 Python 프로세스 기준으로 계산하며 JVM도 같은 작업 디렉터리를 상속한다.
조회 시작 로그를 기록하는 과정에서는 파일 존재·권한을 추가 검사하거나 조회 경로를 변경하지 않는다.

서버 journal과 `AIVAX_PIPELINE_TRACE_LOG_PATH/pipeline_log.txt`에서 확인한다.

```bash
journalctl -u aivax-pipeline.service -f | rg 'Starting CSO file lookup'
```

## JVM 수명

Fasoo 선택 시 FastAPI lifespan 시작에서 JVM을 기동하고 준비 응답을 확인한다. 설정이 활성화됐는데
Java·class·라이브러리가 없거나 준비에 실패하면 앱 기동을 실패시킨다.
worker마다 JVM 하나를 소유하므로 worker가 4개면 JVM도 4개다.
앱 종료 시 JVM을 종료·회수한다. 생성자는 프로세스를 실행하지 않는다.

한 JVM에는 한 요청씩 전송한다. 한 요청의 파일들은 입력 순서대로 조회한다.
요청 제한 시간은 직렬화 대기·장애 후 재기동·전체 파일 조회를 포함한다.
대기 중 취소·시간 초과는 실행 중인 다른 요청에 영향을 주지 않는다.
전송 중 취소·시간 초과·프로토콜 오류가 발생하면 JVM을 종료·회수하고,
다음 요청에서 새 JVM을 기동한다. 실패한 요청을 자동으로 재전송하지 않는다.
조회 실패는 가드에서 파일별 `error`로 변환한다. 기본적으로 차단하며,
조회 오류 생략 옵션이 켜져 있으면 `skipped`로 처리한다.

## stdio v1

`CsoJvmWorker`를 기동하며, 기존 handler를 빈 파일 목록으로 한 번 호출해
초기화한 후 `AIVAX_CSO_V1\n`을 보낸다. 기존 handler의 실행 인자는 변경하지 않는다.
별도 JSON 파서 JAR 없이 Java 17 표준 라이브러리로 요청을 읽는다.

- 정수는 big-endian이다.
- 요청: 요청 ID 8바이트 + 파일 수 4바이트 + 파일별 UTF-8 길이 4바이트와 경로 바이트.
- 응답: 같은 요청 ID 8바이트 + JSON 바이트 길이 4바이트 + UTF-8 JSON 배열.
- 요청은 최대 65,536개 파일, 프레임은 최대 8 MiB다. 응답도 최대 8 MiB다.
- 경로의 공백·한글·개행은 프레임 경계를 바꾸지 않는다.
- worker의 stdout은 프로토콜 전용이다. 기존 handler의 stdout을 별도로 캡처해 마지막
  JSON 줄을 응답으로 쓰고 앞선 로그는 stderr로 전달한다. Python은 stderr를 계속 읽는다.

제공 라이브러리와 실행 설정은
[`third_party/fasoo/cso/README.md`](../../../third_party/fasoo/cso/README.md)를 따른다.

## 성능 측정

`python scripts/load_cso.py --workers 4 --concurrency 4 16 64 --seconds 10`으로
실제 Inline API와 상주 JVM, 전용 Valkey를 사용하는 부하 테스트를 실행한다.
별도 포트·디렉터리를 사용하고 종료 시 테스트 프로세스를 회수한다.
조건과 측정 결과는 [`docs/CSO_LOAD_TEST.md`](../../../docs/CSO_LOAD_TEST.md)에 있다.
