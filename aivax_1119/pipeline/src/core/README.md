# Core 계층

특정 업무 도메인에 속하지 않고 여러 계층에서 재사용하는 기술 공통 기능을 둔다.
업무 규칙이나 특정 API 시나리오는 `core`로 올리지 않는다.

## 하위 폴더

| 폴더 | 역할 |
|------|------|
| `constants` | 프로세스 공통 성공·실패 코드 |
| `ddd` | DomainEntity, ValueObject, Mapper 기반 계약 |
| `file` | 파일 metadata, 복사·이동·삭제, 문서 텍스트 추출 |
| `hash` | 업무 의미와 무관한 hash 생성 |
| `http` | 비동기 JSON HTTP client와 process-wide client registry |
| `network` | IP 주소처럼 외부 네트워크 값을 안전하게 정규화·변환 |
| `sqlite` | 경로를 주입받는 범용 SQLite 연결 관리 |
| `time` | 날짜와 시간 표현 정규화 |
| `valkey` | Valkey 연결 생성·종료와 설정 계약 |
| `type_converter.py` | 외부 값을 안전하게 기본 Python 타입으로 변환 |

`core`는 domain의 이름을 모르는 방향을 유지한다. 파일 검사 정책, 분석 로그 병합,
알림 이벤트처럼 업무 용어가 들어가면 해당 domain/application/adapter에 둔다.
