# C·S·O 등급 조회 실행 파일

`CsoGradeHandler.class`와 `lib/*.jar`는 Pipeline 배포 패키지에 함께 포함한다.
기존 `fdoc-handler API/3.Sample`의 조회 코드를 기반으로 한다.
운영 환경에는 Java 17이 필요하다. 실행 시 컴파일하거나 `javac`를 호출하지 않는다.

Pipeline 연동은 기존 등급 조회 로직과 Fasoo JAR를 유지한다.
`CsoGradeHandler`는 로그 문구만 영어로 변경했으며 조회·오류 판정은 유지한다.
로그 문구를 변경하면 Java 17로 소스와 `.class`를 함께 갱신한다.
상주 통신은 별도 `src/extensions/cso/java/CsoJvmWorker`에서 처리하고,
기존 `CsoGradeHandler.main(paths)`을 그대로 호출한다.

Pipeline 루트에서 handler 로그 문구 변경 후 class를 갱신하는 명령:

```bash
javac --release 17 -encoding UTF-8 -implicit:none -sourcepath '' \
  -cp 'third_party/fasoo/cso/lib/*' -d third_party/fasoo/cso \
  third_party/fasoo/cso/CsoGradeHandler.java
```

입력 순서대로 파일별 등급과 상태를 JSON 배열로 출력한다. `--json` 옵션은 필요 없다.

```bash
java -Dfile.encoding=UTF-8 -cp '.:lib/*' CsoGradeHandler '/docs/a.docx' '/docs/b.pdf'
```

```json
[{"grade":3,"status":"tagged"},{"grade":null,"status":"untagged"}]
```

| status | 의미 | grade |
| --- | --- | --- |
| `tagged` | 유효한 FDRClass 조회 | `1`(C), `2`(S), `3`(O) |
| `untagged` | FDRClass 단일 조회 결과가 null 또는 공백 | `null` |
| `error` | 파일 없음, 잘못된 등급 등 조회 중 예외 발생 | `null` |

`getCTProperty(path, "FDRClass")`로 필요한 값만 조회한다.
`untagged`는 등급 값을 얻지 못했다는 뜻이며, 실제 미태깅과 미지원 형식 등을 구분하지 않는다.
열기 암호가 설정된 DOCX(Standard·Agile)와 PDF도 실제 조회에서 `untagged`로 반환된다.
Java handler의 응답만으로 암호 문서·미지원 형식·미태깅을 구분할 수 없다.
Pipeline에서는 아래 MIME 사전 판독으로 구분한 미지원을 별도로 `error`로 처리한다.
라이브러리 내부에서 삼킨 예외는 `error`로 구분할 수 없다.

파일별 예외는 모두 `error`로 반환하고 설명은 stderr에 출력한다.
한 파일에서 예외가 발생해도 나머지 파일 조회는 계속한다.

## Pipeline 연동

`AIVAX_PIPELINE_CSO_ENABLED=true`로 활성화한다. 기본값은 `false`이며,
멀티모달 필터 활성화 및 요청의 `filter_list`와 독립적이다.
구현은 [`src/extensions/cso`](../../../src/extensions/cso/README.md)에 격리한다.
비활성 배포는 확장 모듈을 import하거나 Java를 실행하지 않는다.

첨부파일의 `id`가 빈 문자열이면 Pipeline에서 조회 전에 생략한다.
id 누락·null도 같은 기준으로 `grade=null`, `status=skipped`로 기록한다.
이 처리는 Pipeline 가드에 적용하며 일회성 Java CLI의 경로 판정은 유지한다.

Pipeline은 스킵 옵션과 관계없이 파일 내용의 MIME을 멀티모달과 같은 공통 판독기로 확인한다.
매핑한 형식이 `doc/docx/xls/xlsx/ppt/pptx/hwp/hwpx/pdf` 밖이면 라이브러리를
호출하지 않고 `grade=null`, `status=error`로 분류한다.
미지원 스킵 또는 오류 스킵을 켜면 해당 결과는 `skipped`가 된다. 기본값은 모두 `false`다.
파일명과 요청의 MIME 값은 사용하지 않으며 확장자가 없어도
같은 기준을 적용한다. MIME 판독이 실패하면 스킵하지 않고 실제 등급 조회를 진행한다.
생략 상태와 로그 계약은
[`CSO 선택 기능`](../../../src/extensions/cso/README.md#미지원-형식-조회-생략)을 따른다.

`AIVAX_PIPELINE_CSO_SKIP_UNTAGGED_ENABLED=true`이면 조회 결과가 `untagged`인
파일의 차단을 Pipeline에서 생략한다. 기본값은 `false`이며 C/S는 계속 차단한다.
MIME에서 미지원을 구분하지 못한 경우 미지원·일부 암호 문서도 `untagged`로 반환될 수 있다.
미지원으로 판별해 `error`가 된 파일에는 미태깅 스킵을 적용하지 않는다.

`AIVAX_PIPELINE_CSO_SKIP_ERROR_ENABLED=true`이면 `error` 파일의 차단을
Pipeline에서 생략한다. 미지원 파일도 포함하며 기본값은 `false`다. JVM 요청 타임아웃·통신·응답 파싱
예외도 포함한다. 서버 예외 로그는 유지하고 `grade=null`, `status=skipped`로 기록한다.
정상 조회한 C/S는 계속 차단하며, 앱 시작 시 JVM 준비 실패에는 적용하지 않는다.

활성화하면 FastAPI lifespan에서 worker마다 상주 JVM 하나를 기동하고 준비를 확인한다.
요청은 별도 `CsoJvmWorker`가 같은 JVM에서 반복 처리한다. worker가 4개면 JVM도 4개다.
준비 실패는 앱 기동 실패로 처리하며, 앱 종료 시 JVM을 종료·회수한다.
위의 파일 경로 인자를 받는 일회성 CLI는 수동 점검용으로 유지한다.

```text
Inline: 첨부파일 CSO 조회
    ├─ O(3)와 생략 파일만 있음: 기존 필터 분석 및 사용자 동의 흐름 적용
    └─ 그 외: 필터 분석 생략
             → 첨부파일 백업 및 등록 이벤트 생성
             → CSO 차단 로그 기록
             → CSO 차단 응답
```

파일을 이동하기 전에 대상 파일의 CSO 조회를 완료한다. Inline에서 조회 결과가
하나라도 C·S 또는 생략 옵션을 적용하지 않은 등급 없음·예외이면
요청 전체를 차단하며, 요청된 필터가 없어도 같은 규칙을 적용한다.
분석 필터를 실행하지 않고 요청의 첨부파일을 기존 백업 위치로 이동한다.
저장에 성공한 파일은 기존 첨부파일 등록 이벤트를 발행한다.
파일이 없거나 이동에 실패하면 보관 경로를 빈 값으로 기록하며 차단은 유지한다.

Inline의 최초 CSO 차단 로그는 `mode=block`, 빈 정책과 마스킹 결과를 기록한다.
조회 화면에서 파일을 확인할 수 있도록 `filter_detect`의 `file block` 항목에
`file_summary`를 기록한다. 파일별 `file_backup_path`와 기본 파일 정보를 담되,
필터의 `mode`는 빈 값이고 실행 시각·탐지 정책은 기록하지 않는다.
`file_info.inspection_status=skipped`, `skip_reason=cso_blocked`로 분석 생략을 표시한다.
요청 원문인 `prompt`는 유지한다.
CSO 결과는 OpenSearch로 전달되는 로그의 전용 필드 `cso`에 기록한다.
차단 안내는 최종 로그의 `output_text`에 기록하므로 Output API를 기다리지 않는다.

```json
{
  "cso": {
    "status": "blocked",
    "files": [
      {
        "file_name": "a.docx",
        "file_path": "/docs/a.docx",
        "grade": 2,
        "status": "tagged"
      }
    ]
  }
}
```

`cso.status`는 요청 전체의 CSO 상태다. 최종 분석 판정 `mode`와 독립적으로 기록한다.

| `cso.status` | 의미 | Inline 동작 |
| --- | --- | --- |
| `open` | O(3)와 생략 파일만 있고 O가 있음 | 기존 분석·동의 흐름 진행 |
| `blocked` | 생략하지 않은 조회 오류 없이 C/S 또는 등급 없는 파일이 있음 | 차단 |
| `error` | 생략하지 않은 조회 오류가 있음 | 차단 |
| `skipped` | 모든 파일이 생략 정책 적용 대상 | 기존 분석·동의 흐름 진행 |

여러 파일에서 차단 등급과 생략하지 않은 오류가 함께 있으면 `error`를 우선 기록한다.
각 파일의 등급과 생략 정책 적용 후 `tagged`/`untagged`/`error`/`skipped` 상태는
`files`에 유지한다.
예를 들어 조회 오류이면 `cso.status="error"`, 해당 파일은 `grade=null`, `status="error"`다.
`cso.blocked`는 새 로그에서 제거한다. 기존 OpenSearch 문서의 과거 필드는 자동 변경하지 않는다.
Inline의 CSO `blocked`·`error` 로그는 모두 `mode=block`이다.
안내 메시지는 `output_text`에서 확인한다.
CSO가 꺼져 있거나 첨부파일이 없으면 `cso` 필드를 추가하지 않는다.

Mirror는 CSO를 조회하거나 차단하지 않으며 로그에도 `cso`를 기록하지 않는다.
동의 값과 관계없이 즉시 허용하고 기존 필터 분석을 백그라운드에서 수행한다.
Inline에서 현재 첨부파일이 CSO 차단 대상이면 사용자 동의와 SSL 차단 bypass로 허용하지 않는다.
동의 재개용 Valkey 상태는 사용하지 않는다.
Inline에서 `user_consent=true`인 요청은 일반 필터를 생략한다. CSO가 허용되면 입력 로그를 기존 Valkey 병합 저장소에 적재한다.
CSO는 계속 확인하며, 기존 O 요청의 동의 재전송에서 등급이 바뀌면 차단한다.
이미 보관된 첨부파일은 원본 차단 로그의 보관 경로로 다시 조회한다.
이미 보관한 파일과 기존 분석 결과는 유지하고 차단 정보·파일 보관 경로를
원본 로그가 있으면 `.json.consent`에 기록하여 같은 OpenSearch 문서를 갱신한다.
원본이 없으면 최초 CSO 차단 로그를 기록한다.

| 환경변수 | 기본값 | 의미 |
| --- | --- | --- |
| `AIVAX_PIPELINE_CSO_ENABLED` | `false` | CSO 등급 처리 활성화 |
| `AIVAX_PIPELINE_CSO_SKIP_UNSUPPORTED_EXTENSIONS_ENABLED` | `false` | MIME 기반 미지원 형식 조회 생략 |
| `AIVAX_PIPELINE_CSO_SKIP_UNTAGGED_ENABLED` | `false` | 조회 후 등급 없는 파일 차단 생략 |
| `AIVAX_PIPELINE_CSO_SKIP_ERROR_ENABLED` | `false` | 조회 오류 파일 차단 생략 |
| `AIVAX_PIPELINE_CSO_REQUEST_TIMEOUT_SECONDS` | `5` | 대기·장애 후 재기동·전체 파일 조회 제한(양의 정수, 초) |
| `AIVAX_PIPELINE_CSO_STARTUP_TIMEOUT_SECONDS` | `15` | 앱 시작 시 JVM 준비 제한(양의 정수, 초) |

클래스와 라이브러리는 Pipeline 루트의 `third_party/fasoo/cso`에 두고,
PATH의 `java` 명령으로 실행한다.

Python은 JVM 하나에 요청 하나씩 전송한다. JVM과 로드한 클래스는 재사용한다.
`DocProperties` 생성은 수정하지 않은 기존 handler가 요청마다 수행한다.
요청 제한은 `.env`와 `.env.development`의
`AIVAX_PIPELINE_CSO_REQUEST_TIMEOUT_SECONDS`로 조정하며 기본값은 5초다.
파일별 제한이 아니며, 직렬화 대기 시간도 포함한다. 대기 중 시간 초과는 실행 중인
다른 요청에 영향을 주지 않는다. 전송 중 시간 초과·취소·통신 오류는 해당 JVM을
종료·회수하며, 다음 요청에서 재기동한다. 요청 전체 조회 실패는 조회 대상 파일을
모두 `error`로 처리한 뒤 생략 옵션을 적용한다. 개별 등급은 확인할 수 없다.
실패한 요청은 자동으로 재전송하지 않는다.

stderr는 Python에서 캡처하여 서버 warning 로그에 기록한다.
Java 실행 실패·타임아웃·잘못된 JSON 등 조회 예외는 모두 `error` 상태로 처리하고,
Inline은 기본적으로 필터 분석 없이 파일 보관과 차단 로그 기록 후 공통 조회 실패
안내로 차단한다. 조회 오류 생략 옵션이 켜져 있으면 후속 필터·동의 흐름을 진행한다.
Mirror는 CSO 조회 자체를 생략한다.
