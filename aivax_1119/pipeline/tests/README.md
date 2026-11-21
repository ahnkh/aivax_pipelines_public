# Pipeline 테스트

기능 테스트는 도메인별 폴더에 둔다. 여러 도메인의 구조 규칙을 검사하는 테스트,
별도 HTTP 서버를 띄우는 E2E, Docker 통합 테스트는 역할별로 분리한다.

| 폴더 | 검증 대상 |
| --- | --- |
| `analysis/` | 입력·출력 분석 유스케이스, 컨트롤러, 입력 로그 조립 |
| `analysis_log/` | 로그 모델·병합·저장·전달, CDS 큐·재전송, 만료·백업 |
| `api/` | 공통 API 계약, 상태 API |
| `architecture/` | 추상 포트, 계층 의존성, 레거시 의존성 금지, 출력 분석 독립성 |
| `bootstrap/` | 환경·설정 로딩과 검증, 인스턴스 격리, 가드 수명, 개발 실행 절차 |
| `consent/` | 사용자 동의, 동의 상태 보존, Valkey·Fluent Bit 동의 흐름 |
| `core/` | 파일·해시·시간·타입 변환, HTTP·SQLite·Valkey 공통 도구 |
| `cso/` | CSO 등급 처리, JVM 프로세스 |
| `file_delivery/` | 첨부파일 전달, 임시파일 정리 |
| `inspection/` | Regex·멀티모달·SLM·Secuxper 검사기와 어댑터 |
| `notification/` | 첨부파일·정책·사용자 이벤트, 감사 알림, Stream 매핑 |
| `policy/` | 정책 조회·갱신·신호, 필터 규칙 테스트 API |
| `e2e/` | 실제 HTTP 서버·Valkey를 통한 분석 로그 API와 파일·이벤트 출력 |
| `integration/` | Docker Fluent Bit·OpenSearch·Valkey와 실제 Pipeline·CSO JVM |

프로젝트 루트의 `conftest.py`가 공통 실행 옵션과 격리된 API 환경을 제공한다.
Docker 전용 fixture와 Compose 설정은 `integration/`에서 관리한다.
테스트 파일명은 유지하므로 `-k`로 기존 테스트 이름을 선택할 수 있다.

## 실행

`apps/pipeline` 디렉터리에서 실행한다.

```bash
# E2E·Docker 폴더를 제외한 회귀 테스트
make test-unit

# 특정 도메인 또는 파일
.venv-dev/bin/python -m pytest tests/policy
.venv-dev/bin/python -m pytest tests/notification/test_attachment_events.py

# 별도 HTTP 서버·임시 Valkey를 사용하는 E2E
RUN_ANALYSIS_LOG_E2E=1 .venv-dev/bin/python -m pytest tests/e2e

# Docker 통합 테스트
make test-integration

# Fluent Bit·Docker·CSO까지 포함한 전체 테스트
make test
```

`consent/test_user_consent_valkey_flow.py`와 API fixture는 서버에 설치된
`valkey-server`를 PATH에서 찾아 임시 디렉터리와 별도 포트 또는 Unix socket으로 실행한다.
서버 바이너리는 AIVAX packaging이 관리하며 Pipeline에 동봉하지 않는다.
`consent/test_user_consent_fluent_bit.py`는 `FLUENT_BIT_BIN` 또는 `make test`로
실제 Fluent Bit를 실행하며, 기본 실행에서는 건너뛴다.
Docker 준비 사항은 [통합 테스트 문서](../docs/CONTAINER_INTEGRATION_TEST.md)를 따른다.

## 새 테스트 배치

- 검증 대상 도메인의 폴더에 추가하고 파일명은 `test_*.py`로 작성한다.
- 특정 도메인의 컨트롤러·서비스·어댑터 테스트는 같은 폴더에서 관리한다.
- 여러 도메인에 걸친 계층·의존성 규칙은 `architecture/`에 둔다.
- 별도 서버를 실행하는 분석 로그 시나리오는 `e2e/`, Docker 스택 검증은
  `integration/`에 둔다. `make test-unit`은 두 폴더를 통째로 제외한다.
