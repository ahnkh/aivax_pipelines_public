# Toolkit 스케줄러 대체 계획

작성: 2026-10-06. 기본 범위는 필요성 검토·계획 작성이다. 추가로 확정된
동의 상태·증적 임시파일 정리와 기존 방식의 bulk 로그 백업을 공통 cleanup cron에
반영했다. 운영 적용은 수행하지 않았다.

## 선행 판단: 그대로 이관할 필요가 있는가

현재 방침은 **1·2번은 Pipeline에서 처리하고, 3·4번은 보류**다. 사용자 결정에 따라
bulk 로그는 기존과 동일하게 마지막 수정 후 87400초가 지나면 날짜별 백업 경로로
이동한다. 별도 scheduler 없이 같은 cleanup cron에서 매시 05분에 실행한다.

추가 확정 및 개발 반영: `.consent-state`는 최초 저장 후 **30분** 동안 유효하다.
조회·동의 갱신으로 만료를 연장하지 않으며, 만료 조회는 기존 HTTP 409 경로로 처리한다.
`CleanupCron` 하나가 시작 시와 매 60초마다 동의 상태와 30분 지난 증적 임시파일을
`CleanupPipelineFilesUseCase`로 정리한다. 한 대상의 실패는 다른 정리를 막지 않는다.
원본 bulk 및 동의 출력 파일도 같은 cron에서 매시 05분에 백업한다.

### 1a. 오래된 첨부파일을 백업 폴더로 이동

현재 Pipeline 정상 흐름에서는 별도 10분 scheduler가 필요하지 않다. `MultimodalInputInspector.inspect()`가 검사 결과 중 `inspection_status`가 없는 파일을 즉시 FileDeliveryPort로 전달하고, LocalFileDeliveryAdapter는 최종 `YYYYMMDD/HH` 경로에 저장한 뒤 원본을 제거한다. 최종 경로는 로그와 Management 첨부파일 이벤트에 전달한다. 이 조건은 단순히 “차단된 파일만”이라는 조건이 아니며, skipped 파일은 백업하지 않는다.

레거시의 30분 지난 파일 이동은 정상 전달의 잔존 파일을 처리하는 보완 기능이었다. 새로운 검사 입력의 소유자가 AI engine이라면 그 임시파일을 Pipeline의 일괄 백업 대상으로 옮기는 것은 별도 보관 정책 없이는 필요하지 않다.

현재 배포된 `/home1/aivax/ai_engine/core/file_upload_manager.py`에는 `/run/aivax/attachments` 기본 경로와 upload TTL(기본 600초)·registry 초과 시 원본 삭제가 이미 있다. 이는 개발 저장소 밖의 배포 코드에서 확인한 사항이며, 모든 입력 경로의 삭제 완료를 증명하지는 않는다. 특히 cleanup은 호출 시 수행되므로 무트래픽·재시작의 잔존 처리는 engine 쪽 검증 대상이다.

판정: **일반 첨부파일 이동 scheduler 추가는 보류**. 즉시 백업 실패 시 원본·부분 목적지·알림 상태와 skipped 입력 정리의 실제 소유권을 먼저 확인한다. 필요하면 실패 파일에 대한 명시적 재시도 또는 engine 정리를 보완한다.

### 1b. 증적 임시파일 삭제

필요한 정리 책임은 확인됐다. 기존 `LocalMultimodalInspectionAdapter._collect_evidence()`는 변환 가능한 문서에 대해 공용 detail_reason_directory로 PDF를 변환하고 증적을 추출했지만 변환 결과를 삭제하지 않았다. 응답 증적은 구조화된 텍스트이며 이 변환 PDF 경로를 반환하지 않는다.

반면 일반 텍스트 변환·구형 PPT/XLS 변환·정책 테스트 업로드는 TemporaryDirectory를 사용해 정상 종료 시 정리한다.

사용자 결정 및 개발 반영: **삭제는 공통 cleanup cron에서 처리**한다. 변환은 요청별
`evidence-*` 디렉터리를 사용하고, 처리 종료 후 30분이 지나면 디렉터리를 삭제한다.
강제 종료로 남은 디렉터리와 기존 root의 오래된 PDF도 같은 cron이 정리한다.
검사 중에는 공유 lock을 유지하며 cron은 해당 경로 정리를 건너뛴다.

### 2. bulk 로그를 하루 뒤 별도 폴더로 이동

개발 코드에 다음 동작을 구현했다.

- Asia/Seoul 기준 매시 05분. 긴 작업으로 실행이 늦어진 경우에도 한 번 수행한다.
- `mtime` 기준 87400초 이상 지난 `aivax_log#YYYYMMDD#PID.json`과 `.json.consent` 대상.
- 목적지: `AIVAX_PIPELINE_LOG_BULK_BACKUP_DIR/YYYYMMDD/<원래 파일명>`.
- 운영 env: `/data/backup/prompt_log_backup`, 개발 env: `./.runtime/development/bulk-backup`.
- 원본과 파일 크기·mtime을 대조한 뒤 백업을 공개하고 원본을 제거한다.
- 기존 백업이 있으면 덮어쓰지 않고 원본을 유지한다. 파일별 실패를 격리한다.
- 쓰기와 이동은 directory lock으로 worker 간 동시 실행을 방지한다.
- `.consent-state`, CDS queue, 잘못된 파일명·날짜, symlink는 백업하지 않는다.

기존과 같이 경과 시간 기준이다. mtime은 OpenSearch 적재 완료 확인이 아니며,
Fluent Bit offset도 읽기 위치다. 장애 후 백업을 통한 재처리는 별도 운영 절차다.
백업 파일의 365일 만료·디스크 압박 정리는 보류된 3번 범위로 남긴다.

## 책임 배분

| 기존 작업 | 새 담당 | 구현 방향 |
|---|---|---|
| 1. 첨부파일 백업·증적 임시파일 정리 | Pipeline | 기존 즉시 백업 유지. 증적 임시파일은 공통 cleanup cron에서 30분 만료 정리 |
| 2. 프롬프트 bulk 원본 로그 백업 | Pipeline | 매시 05분, 수정 후 87400초 지난 파일을 날짜별 백업 경로로 이동 |
| 3a. OpenSearch 인덱스 기간 만료 | OpenSearch ISM | 날짜별 인덱스에 보관·삭제 정책 적용 |
| 3b. Snapshot 기간 만료 | OpenSearch SM | 생성 정책과 함께 보관·삭제 정책 설정 |
| 3c. 일반 백업 파일 보관기간·디스크 압박 처리 | Management | 첨부파일·bulk 백업 파일 정리 구현 |
| 3d. OpenSearch 디스크 압박 시 조기 삭제 | Management, 해당 정책을 유지할 경우 | API 기반으로 별도 조정. 일반 watermark 설정으로 대체 불가 |
| 4. 일일 snapshot | OpenSearch SM 우선 | 미지원 배포 환경에서만 Management scheduler fallback |

Management는 지원되는 ISM/SM 정책을 설치·조회·변경하는 역할을 맡을 수 있다. 예약 작업의 실제 실행 주체는 OpenSearch로 유지한다. 동일 작업에 SM과 Management cron을 동시에 활성화하지 않는다.

## 현재 개발 코드에서 확인한 내용

- `src/adapters/delivery_file/local_file_delivery_adapter.py`는 파일을 `YYYYMMDD/HH`에 즉시 전달한다.
- 현재 `.env`와 `.env.development` 모두 최종 첨부파일 경로가 `/data/backup/attach_file_backup`이다.
- `src/application/use_cases/input_analysis/multimodal_input_inspector.py`는 전달한 최종 경로를 결과와 첨부파일 알림 이벤트에 넣는다. 이 경로를 다시 이동하면 Management의 파일 참조가 깨질 수 있다.
- 증적 경로는 `detail_reason_temp_dir` 설정으로 받는다. 레거시 `.tmp_detail_dir`를 새 경로로 간주하지 않는다.
- Fluent Bit adapter는 일자·PID별 `.json`에 append하고, 동의 갱신은 `.json.consent`, 조회 상태는 `.consent-state/`에 별도 저장한다.
- `infra/fluent-bit/fluent-bit.conf`는 본문과 동의 갱신을 별도로 수집하고 `aivax_log-YYYYMMDD`로 출력한다. 실제 운영 alias/template는 적용 전에 조회해야 한다.
- 운영 설정은 worker 4개이므로 scheduler를 worker마다 시작하면 같은 파일을 중복 처리할 수 있다.
- 이 호스트의 `/usr/share/opensearch/plugins`에는 Index Management와 Job Scheduler 3.3.2용 플러그인이 설치돼 있다. 실제 클러스터 로딩·API 권한·정책 상태는 추가 읽기 조회로 확인해야 한다.

## 단계 1. 파일 수명주기 계약 확정

구현 전 실제 생성·사용 경로를 기준으로 다음을 확인한다.

1. 검사 입력 파일은 누가 만들고 언제 사용 완료되는가? 외부 요청의 attachment.id 경로 전체를 cleanup 대상으로 사용하지 않는다. Pipeline이 소유한 staging 경로만 지정한다.
2. 즉시 백업에 실패한 파일과 미검사·허용 파일의 잔존 처리 규칙을 구분한다. 정상 백업 파일을 다시 옮기거나 같은 이벤트를 중복 발행하지 않는다.
3. 증적 임시파일 생성·응답 참조·다운로드 수명과 완료 시 정리 여부를 확인한다. 정상 완료 시 정리와 비정상 종료 후 잔존 정리를 구분한다.
4. `.json.consent`도 원본과 같은 기간 기준으로 백업한다. `.consent-state`는 별도 30분 만료 정책으로 처리한다.
5. Fluent Bit의 읽기 위치와 OpenSearch 전달 완료는 다른 상태다. 기존과 같은 mtime 기반 백업을 사용하며, 아직 쓰는 파일과의 경합은 공유 lock으로 방지한다.

동의 상태·증적은 30분 만료, bulk는 87400초·매시 05분 기준으로 확정했다.
보관기간·디스크 정리 정책은 3·4번 재검토 때 다룬다.

## 단계 2. Pipeline 구현

기존 계층 구조를 따른다. scheduler는 실행 시점·thread lifecycle만 맡고 파일 순회와 이동은 adapter에 둔다.

예상 변경 위치:

```text
src/domain/analysis_log_delivery/ports/analysis_log_consent_writer_port.py
src/domain/inspection_multimodal/ports/multimodal_inspection_port.py
src/application/use_cases/cleanup_pipeline_files/
src/adapters/analysis_log_delivery/fluent_bit_analysis_log_delivery_adapter.py
src/adapters/inspection_multimodal/local_multimodal_inspection_adapter.py
src/schedulers/
  cleanup_cron.py
src/bootstrap/env_loader.py
src/bootstrap/pipeline_bootstrap.py
```

1. **첨부파일·임시파일**: 기존 즉시 백업 경로 유지. 추가 작업은 관리 대상 staging의 오래된 잔존 파일과 증적 임시파일 처리. 최종 백업 파일은 이 작업에서 이동하지 않는다. 레거시 staging을 비울 필요가 있으면 최종 경로·알림 계약에 맞춘 별도 전환 작업으로 수행한다.
2. **bulk 백업**: 허용한 `.json` 및 `.json.consent` 파일명·날짜·mtime을 검증하고 날짜별 목적지로 이동한다. 숨김 상태 디렉터리와 CDS 전송 큐는 제외한다.
3. **파일 동시성**: 모든 worker가 같은 관리 경로를 다룰 때 실행별 비차단 filesystem lock으로 한 작업만 실행한다. 쓰기 adapter와 cleanup이 공유할 파일 lock, lock 취득 후 stat 재확인, 새 파일 생성/append와 삭제 간 경합을 함께 해결한다. 공유 파일시스템 또는 여러 호스트 환경은 lock 지원과 처리 소유권을 따로 검증한다.
4. **이동 완료**: 목적지 충돌 시 덮어쓰지 않는다. 다른 파티션으로 이동할 때 임시 목적지로 복사·검증·완료 표시 후 원본을 제거한다. 중간 실패와 프로세스 재시작에서 같은 파일을 안전하게 재처리한다. 기존 LocalFileOperations.move는 최종 경로에 직접 복사하므로 그대로 재사용하기 전에 실패·충돌 동작을 보완한다.
5. **설정과 관측**: enable, 소스·목적지 경로, 실행 간격, 만료 시간, batch 한도를 검증해 주입한다. bootstrap에서 시작·종료하며 파일별 실패를 격리한다. 이동/삭제/skip/실패 건수·바이트·실행 시간을 기록한다.

bulk는 별도 소비 완료 조회를 추가하지 않고 기존 경과 시간 기준의 백업을 사용한다.

## 단계 3. OpenSearch 정책으로 대체

### 인덱스 보관: ISM

상세 설계와 기본 정책 초안은 [ISM 설계안](../../../../infra/opensearch/ISM_DESIGN.md)에 정리했다.
Management의 기존 보관기간 설정을 유지하고, 일별 인덱스의 하루 여유를 더해
기본 365일 설정을 `min_index_age: 366d`로 연결하는 제안이다. 운영 적용은 아직 하지 않았다.
Management의 검색·논리 백업은 공통 조회 alias로 통일하고, 신규 일별 인덱스의 alias는
index template에서 자동 연결한다. Fluent Bit 본문·동의 갱신은 원래 날짜의 인덱스에 직접 쓴다.

- 실제 `aivax_log-*` 및 `aivax_log` alias 구성을 조회한다.
- 날짜별 인덱스라면 해당 패턴에 보관기간 후 delete 정책을 연결하고, 새 인덱스에는 ISM template로 자동 적용한다.
- 기존 인덱스는 대상 목록과 삭제 후보 날짜를 먼저 검토한 뒤 명시적으로 연결한다. 이미 오래된 인덱스에 정책을 연결하면 바로 삭제 조건을 만족할 수 있다.
- ISM의 age는 문서 timestamp나 인덱스 이름 날짜와 동일하지 않다. 생성 시각 기반 정책으로 충분한지, 과거 데이터 재적재에서 별도 처리가 필요한지 검증한다.
- 실제로 단일 concrete index를 사용한다면 그 인덱스 전체에 365일 delete를 적용하지 않는다. 시간 단위 인덱스/alias 전환을 별도 작업으로 정하고 기존 검색·동의 update 경로의 호환성을 검증한다.

ISM은 인덱스 수명·크기 등에 따라 작업을 자동 수행하는 기능이다. [OpenSearch 3.3 ISM 정책](https://docs.opensearch.org/3.3/im-plugin/ism/policies/)

### Snapshot 예약·보관: SM

- 실제 API 지원·권한·기존 정책·저장소를 조회한다.
- 기존 저장소 접근 거부 문제를 해결하고 repository 검증 및 별도 테스트 snapshot 복원을 통과시킨다.
- `aivax_log*`, `include_global_state=false`, 매일 23:59, `Asia/Seoul`을 초기 정책으로 제안한다. 생성과 만료 삭제 일정은 분리한다.
- SM의 snapshot 이름은 정책명·날짜·랜덤 접미사 형식이다. 기존 이름 파싱에 의존하지 않고 메타데이터/API로 조회한다.
- 기존 toolkit snapshot도 삭제 대상에 넣을지는 이름 패턴과 서버 버전 지원을 검토해 별도 전환한다.
- SM 미지원 환경만 Management에서 동등한 생성·상태 확인·실패 재시도·보관 삭제를 구현한다. 권한 오류나 저장소 오류를 SM 미지원으로 간주하지 않는다.

SM은 생성·삭제를 예약할 수 있다. [OpenSearch 3.3 Snapshot Management](https://docs.opensearch.org/3.3/tuning-your-cluster/availability-and-recovery/snapshots/snapshot-management/)

## 단계 4. Management에 남는 정리

OpenSearch는 첨부파일·bulk 원본 백업 디렉터리의 수명주기를 관리하지 않는다. 일반 백업 파일의 365일 만료 정리와 파티션 압박 처리는 Management 책임으로 남긴다.

또한 OpenSearch watermark는 shard 배치·이동·쓰기 제한 기능이다. 기존의 “95% 초과 시 오래된 것을 삭제해서 85%까지 낮춤”과 같은 자동 삭제 정책은 아니다. [OpenSearch 3.3 cluster settings](https://docs.opensearch.org/3.3/install-and-configure/configuring-opensearch/cluster-settings/)

- 기간 만료와 디스크 압박 정리를 별도 use case로 나눈다.
- filesystem 정리와 OpenSearch 정리를 분리해 snapshot API 실패가 다른 작업을 막지 않게 한다.
- 기간 미만 데이터의 조기 삭제는 운영 정책으로 활성화 여부·최소 보관량·1회 삭제량을 명시한다.
- 관리 대상 경로·인덱스·snapshot을 검증하고 audit 기록을 남긴다. 활성 인덱스나 일반 디렉터리를 이름 정렬의 첫 항목이라는 이유로 삭제하지 않는다.
- 첨부파일 삭제 시 Management의 library 파일 참조와 상태도 일치시키는 계약을 포함한다.
- snapshot은 파일을 직접 지우지 않고 OpenSearch API로 삭제한다.
- 기존 CronLeaderElectionService로 중복 실행을 방지한다. 동일 OpenSearch 자원은 ISM/SM 실행과 충돌하지 않도록 대상과 책임을 명시한다.

## 검증과 전환 순서

1. Pipeline: 30분/87400초 경계, 활성 파일 제외, 동의 파일·상태 분리, 경로·파일명 검증, 파일별 실패 격리, 충돌, 다른 파티션 복사 실패, 재시작, worker 중복 실행을 테스트한다.
2. Pipeline + Fluent Bit: 오래된 파일 백업 후 동의 update 처리와 재시작·전송 장애 시 처리 상태를 확인한다.
3. OpenSearch: 테스트 인덱스에서 ISM explain/만료 동작, 테스트 repository에서 SM 생성·보관·실패 상태 및 복원을 검증한다.
4. Management: 임계값 경계, 보호 대상 제외, 삭제 한도·중단 조건, 첨부파일 메타데이터 정합성, API 장애 격리를 검증한다.
5. 운영 전환은 작업별로 toolkit 소유권을 해제한 뒤 새 소유자를 활성화한다. 현재 서비스는 실패 중이지만 패키지가 복구되면 자동 재시작하므로 중복 실행 방지가 필요하다.
6. Pipeline의 1·2번 → OpenSearch의 기간 정책·4번 → Management의 잔여 3번 순서로 전환한다. 각 단계에서 처리 결과와 파일/인덱스 수를 관찰한다.
7. 네 작업의 대체가 검증되면 toolkit service를 비활성화하고 배포에서 제거한다. 롤백은 새 소유자부터 정지한 뒤 기존 소유자를 복구한다. 이미 삭제한 데이터의 복구는 사전에 검증한 백업 범위 안에서만 가능하다.

공통 cleanup cron의 동의 상태·증적 임시파일 정리와 기존 방식의 bulk 로그 백업은
개발 코드에 반영했다. 일반 첨부파일 이동 scheduler, OpenSearch 정책과
Management의 보관기간·디스크 정리는 보류한다.

## 실행 스위치

Management와 Pipeline의 이관 스케줄러는 공통 환경변수 `AIVAX_TOOLKIT_ENABLED`로 제어하며 기본값은 `false`다. Pipeline은 비활성화 시 cleanup cron을 생성하거나 시작하지 않는다. Management도 디스크 정리 명령과 leader lock 실행을 건너뛴다. 각 서비스에서 `true`를 설정하고 재시작하면 활성화된다. 동의 상태 조회의 30분 만료 검증은 유지되며, 이 스위치는 OpenSearch에 설치된 ISM/SM 정책을 변경하지 않는다.
