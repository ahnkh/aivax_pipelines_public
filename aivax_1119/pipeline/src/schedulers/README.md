# Scheduler 계층

HTTP 요청과 독립적으로 프로세스 내에서 주기적으로 실행할 진입점을 둔다.

- scheduler는 실행 주기와 thread lifecycle만 관리한다.
- 실제 업무 로직은 application use case 또는 domain port에 위임한다.
- 생성·시작·종료는 `src/bootstrap` composition root가 관리한다.
- HTTP DTO나 router를 두지 않는다.

## 파일 역할

| 파일 | 역할 |
|------|------|
| `analysis_log_merge_expiry_cron.py` | 미매칭 input/output 로그 만료 use case를 주기 실행 |
| `cds_log_delivery_cron.py` | CDS 전송 대기 분석 로그의 batch 전송 use case를 주기 실행 |
| `cleanup_cron.py` | 시작 시·매 60초마다 동의 상태·증적 파일 정리, 매시 05분에 오래된 bulk 로그 백업 |
| `policy_refresh_cron.py` | 정책 종류별 refresh use case를 같은 방식으로 주기 실행 |

scheduler는 DB 조회, 정책 변환, 로그 병합을 직접 수행하지 않는다. 한 주기마다
주입받은 use case의 `execute()`만 호출하고, 중복 thread 시작 방지와 정상 종료를
책임진다.

cleanup은 `.consent-state`와 설정된 증적 임시 경로를 30분 기준으로 정리한다.
같은 cron이 매시 05분(Asia/Seoul)에 마지막 수정 후 87400초가 지난 `.json`과
`.json.consent`를 파일명 날짜별 백업 경로로 이동한다. 쓰기와 백업 이동은
worker 간 공유하는 lock으로 격리한다. 백업 파일의 기간 만료 삭제는 담당하지 않는다.
증적 변환·추출 중에는 worker가 공유하는 lock으로 삭제를 방지한다.
한 대상의 정리가 실패해도 나머지는 실행하며, 종료 대기 시간 초과는 warning으로
기록하고 프로세스의 나머지 종료를 계속한다.
