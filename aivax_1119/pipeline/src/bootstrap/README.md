# Bootstrap 계층

프로세스 설정을 읽고 controller, use case, service, port 구현체와 scheduler를
연결하는 composition root다. 구체 adapter를 선택하는 코드는 이 계층에만 둔다.

## 파일 역할

| 파일 | 역할 |
|------|------|
| `env_loader.py` | 환경 파일을 읽어 불변 `PipelineSettings` 객체 생성 |
| `logging_bootstrap.py` | Pipeline 공용 logger와 handler 설정 |
| `pipeline_bootstrap.py` | 장기 수명 resource와 API use case/controller 조립 및 종료 |
| `__init__.py` | `main.py`에 필요한 bootstrap 공개 API 제공 |

## 수명주기

```text
환경 설정 로드
  → logger 구성
  → DB/HTTP/Valkey/파일 adapter 생성
  → policy cache 초기화
  → application service와 use case 조립
  → controller와 scheduler 시작
  → FastAPI lifespan에서 선택 가드 runtime 시작·준비 확인
  → 종료 시 선택 가드 runtime 종료·회수
  → 종료 시 scheduler, outbox, 연결 역순 종료
```

bootstrap에는 정책 판정이나 로그 병합 같은 업무 로직을 넣지 않는다. 설정값을
해석해 구현체를 선택하고 생성자에 주입하는 책임만 가진다.

CSO 활성 배포는 `src/extensions/cso`의 가드와 JVM runtime을 조건부로 조립한다.
`PipelineAnalysisModules.input_guard_runtime`은 lifespan이 시작·종료하며,
공통 입력 use case에는 `InputGuardPort`만 주입한다.
