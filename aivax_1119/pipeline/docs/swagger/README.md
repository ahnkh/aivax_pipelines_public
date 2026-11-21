# Pipeline Swagger 정의

Swagger의 API 설명은 이 폴더에서 직접 작성한다.

| 파일 | 역할 |
| --- | --- |
| [openapi.json](openapi.json) | 현재 INPUT/OUTPUT API의 요청·응답·오류·필드 설명·예제 |
| [openapi-develop.json](openapi-develop.json) | develop `b05a06be`에 고정한 과거 계약 |
| [index.html](index.html) | 두 명세를 선택해서 보는 조회 전용 Swagger UI |
| [multiple-filter.html](details/multiple-filter.html) | multiple_filter 처리 흐름·필드·중첩 구조·예제·오류 상세 페이지 |
| [output-filter.html](details/output-filter.html) | output_filter 처리 흐름·필드·예제·오류 상세 페이지 |
| [mode-config.html](details/mode-config.html) | /etc/aivax/mode.json 설정·전역 필터 활성화·요청 목록과의 관계·변경 적용 |
| [reference.js](details/reference.js), [reference.css](details/reference.css) | 상세 페이지 공통 표시. 필드 표와 예제는 openapi.json에서 읽음 |

세부 계약의 근거는 [API_REFERENCE.md](../API_REFERENCE.md),
[API_REFERENCE_DEVELOP.md](../API_REFERENCE_DEVELOP.md),
[API_JSON_STRUCTURE.md](../API_JSON_STRUCTURE.md)다.

## 필드 설명 작성 위치

`openapi.json`의 `components.schemas` 아래에서 해당 모델의
`properties.<필드>.description`을 수정한다. 중첩 객체도 해당 스키마에 설명한다.

- `VariantFilterForm`: INPUT 요청. 기본값·null 처리·필터 이름·서비스 ID·동의·디버그.
- `FileAttachItem`: 첨부파일의 경로·크기·이름·MIME 타입.
- `OutputFilterItem`: OUTPUT 요청. INPUT과 다른 null·IP·ID 처리.
- `FinalDecision`: 최종 action·action_code·마스킹·차단 안내·연결 ID.
- `InspectionResult`, `DebugFilterResults`: 디버그 필터별 응답과 생략 조건.
- `LogEvidence`, `FilterDetection`: 입력 로그 근거, 필터별 오류·시각·탐지 상세.
- `FileSummary`, `FileInfo`, `FileDetailReason`: 파일 분석·보관·상세 근거.
- `CsoResult`, `CsoFile`: CSO 전체/파일별 상태와 등급.
- `ErrorEnvelope`, `ValidationIssue`: HTTP 오류와 검증 오류.

엔드포인트 전체 설명과 요청·응답 예제는
`paths.<경로>.post.description`, `requestBody`, `responses`에서 수정한다.
`default`, `anyOf`의 null, `required`로 기본값·null 허용·생략 여부를 표현한다.
추가 예시는 필드의 `examples` 또는 요청/응답 미디어 타입의 `examples`에 작성한다.
Swagger는 기본으로 Schema를 표시하므로 속성을 펼쳐 필드 설명을 읽을 수 있다.

각 API의 설명 첫 줄에서 상세 명세 페이지로 이동할 수 있다. 처리 흐름 설명은
해당 HTML에서 작성하고, 필드 표·중첩 객체·JSON 예제·오류는 `openapi.json`에서
읽어 표시하므로 API 정의를 수정하면 상세 페이지에도 반영된다.
모든 상세 페이지의 헤더에는 전체 상세 페이지 링크를 같은 순서로 표시하고,
현재 페이지를 강조한다. 상세 페이지를 추가할 때는 각 페이지의 헤더 목록에도 추가한다.

## 문서만 열기

저장소 루트에서:

```bash
cd apps/pipeline
make api-docs
```

<http://127.0.0.1:8080/>에서 상단 목록으로 현재/develop을 선택한다.
Valkey·SLM·CSO·환경 파일 없이 열 수 있다. 정적 뷰어는 조회 전용이며
Swagger UI의 JS/CSS는 CDN에서 가져온다.

## Pipeline에서 열기

기본 개발 주소가 `http://127.0.0.1:9099`일 때:

| 용도 | 주소 |
| --- | --- |
| 현재 Swagger, 요청 실행 가능 | <http://127.0.0.1:9099/docs> |
| 현재 앱 전체 OpenAPI | <http://127.0.0.1:9099/openapi.json> |
| multiple_filter 상세 명세 | <http://127.0.0.1:9099/docs/swagger/details/multiple-filter.html> |
| output_filter 상세 명세 | <http://127.0.0.1:9099/docs/swagger/details/output-filter.html> |
| mode.json 설정 명세 | <http://127.0.0.1:9099/docs/swagger/details/mode-config.html> |
| develop 과거 Swagger, 조회 전용 | <http://127.0.0.1:9099/docs/develop> |
| develop OpenAPI | <http://127.0.0.1:9099/openapi-develop.json> |

`src/controllers/documentation.py`는 이 폴더의 JSON을 읽어 FastAPI 문서에 연결한다.
현재 앱의 관리·Health API는 FastAPI 자동 명세를 유지하고, INPUT/OUTPUT 명세와
관련 스키마를 이 폴더의 정의로 반영한다. 실제 요청 검증과 응답 동작은 바꾸지 않는다.
실행 중인 앱은 OpenAPI를 캐시하므로 파일 수정 후 서버를 재시작한다.

현재 INPUT의 최종 action은 `accept`, `block`, `masking`, `failed`, 빈 문자열이다.
develop의 최종 action은 `allow`, `block`, `masking`이다. 두 계약을 구분해서 수정한다.
HTTP 200과 `action_code=0`만으로 허용이나 검사 성공을 판단하지 않는다.

## 정의 검증

```bash
make api-docs-check
```

내부 참조, 모든 선언 필드의 설명 유무, 현재 요청 DTO와 타입·기본값·필수 여부의
일치를 검사한다. JSON은 직접 관리하는 원본이며 코드로 다시 생성하지 않는다.
