# Pipeline `src` 아키텍처

`src`는 Pipeline의 제품 코드를 계층과 업무 책임에 따라 분리한 루트다.
레거시 모듈을 직접 참조하지 않으며, 구체 구현의 선택과 연결은 `bootstrap`에서만
수행한다.

## 전체 구조

| 폴더 | 역할 |
|------|------|
| `domain/` | 업무 모델, 불변식, 도메인 서비스와 port 계약 |
| `application/` | API·scheduler 요구사항을 완성하는 use case와 재사용 서비스 |
| `adapters/` | DB, Valkey, HTTP, 파일 시스템 등 port의 외부 구현 |
| `extensions/` | 배포별 선택 기능의 가드·업무 처리·외부 구현 |
| `controllers/` | HTTP 요청/응답 계약과 use case 호출 |
| `schedulers/` | 주기 실행과 background thread 수명 관리 |
| `bootstrap/` | 설정 로딩과 프로세스 전체 의존성 조립 |
| `core/` | 특정 업무 도메인에 속하지 않는 기술 공통 기능 |

## 의존 방향

```text
main.py
  └─ bootstrap
       ├─ controller ──> application/use_cases
       │                    ├─ application/services
       │                    └─ domain/ports + domain/models
       ├─ scheduler ───> application/use_cases
       └─ adapter ─────> domain/ports

domain ──> core의 최소 공통 추상화만 허용
```

- controller는 요청을 변환하고 use case 하나만 호출한다.
- application은 구체 adapter를 알지 않는다.
- domain은 application, controller, adapter를 import하지 않는다.
- adapter는 도메인 port를 구현하며 다른 adapter를 업무 흐름처럼 조립하지 않는다.
- bootstrap만 구체 클래스를 생성하고 서로 연결한다.
- 선택 기능은 `extensions`에 두고 bootstrap에서 활성화된 구현만 import한다.
  공통 application은 domain port를 통해 호출한다. CSO는
  [`extensions/cso`](extensions/cso/README.md)의 선행 가드로 연결한다.

## 주요 실행 흐름

```text
HTTP 요청
  → controller DTO/mapper
  → use case
  → application service 또는 domain service
  → domain port
  → adapter
```

외부 JSON 계약의 `input_filter`, `secret_filter`, `file_block_filter`,
`slm_filter` 이름은 내부 폴더 네이밍과 별도로 유지한다.
