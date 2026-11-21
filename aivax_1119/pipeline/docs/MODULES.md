# Pipeline 모듈 맵

**Date:** 2026-08-16  
**LIVE:** `/home1/aivax/pipeline`  
**이 작업 트리:** `/home/AIVAX/aivax/apps/pipeline`
**Listen:** `127.0.0.1:9099` (`main.py`)

Inline ChatGate와 Mirror SHM이 **같은 판정 API**를 호출합니다.  
모드별로 Pipeline을 두 개 띄우지 않습니다. 엔진만 갈라집니다.

관련: [DEPLOY_PIPELINE_1G.md](DEPLOY_PIPELINE_1G.md) · [../../../ai_engine/docs/MODULES.md](../../../ai_engine/docs/MODULES.md)

---

## 1. 호출 경로

```text
Inline:  Envoy → aivax-engine-inline (UDS) → Pipeline :9099
Mirror:  Suricata SHM → aivax-engine-mirror → Pipeline :9099   # AIVAX_POLICY_API=1 필수

엔진 Input  → POST /v1/filter/multiple_filter
엔진 Output → POST /v1/filter/output_filter
```

엔진은 요청에서 프롬프트/첨부를 추출하고, **판정(ALLOW/BLOCK/MASK)과 파일차단은 Pipeline**이 합니다.

---

## 2. 제품 API (엔진이 쓰는 것)

구현:

- 입력·출력·규칙 테스트·정책 signal: `src/controllers`

| 메서드 | 경로 | 역할 |
|--------|------|------|
| `POST` | `/v1/filter/multiple_filter` | 입력 필터 체인 (프롬프트 + 파일) |
| `POST` | `/v1/filter/output_filter` | 출력 로그 merge (판정 없이 저장) |
| `POST` | `/v1/filter/testrule` | 규칙 테스트 |
| `POST` | `/v1/filter/policy-refresh` | Valkey 정책 cache 갱신 |
| `GET` | `/docs` | OpenAPI |

입력 본문(엔진 쪽): `filter_list`, `prompt`, `encoding`, `user_id`, `email`, `ai_service`, `client_host`, `session_id`, `attachments`, `message_id`.  
`attachments[].id` = **로컬 절대경로** (tmpfs). URL이 아님.

출력 본문: `llm_output`, `user_id`, `email`, `ai_service`, `client_host`, `session_id`, `message_id`.

입력 분석 실행은 `src/application/use_cases/input_analysis`, 출력 병합은
`src/application/use_cases/output_analysis`, 정책 signal은
`src/application/use_cases/refresh_filter_policy`가 담당한다. 의존성은
`src/bootstrap/pipeline_bootstrap.py`에서 조립한다.

---

## 3. 폴더

```text
pipeline/
  main.py                 uvicorn 엔트리 (AIVAX_PIPELINE_WORKERS)
  src/bootstrap/env_loader.py  실행 설정 계약
  src/controllers/        FastAPI controller
  src/application/        분석 유스케이스
  src/adapters/           파일·정책·SLM 등 외부 구현
  src/adapters/analysis_log_merge/  Memory/Valkey merge store 구현
  src/adapters/analysis_log_delivery/  bulk 로그 전달
  scripts/                pack · Valkey · smoke/soak
  docs/
```

---

## 4. 분석 실행 (`src/application/`)

`InputAnalysisUseCase`가 요청된 regex·multimodal·SLM inspector를 실행하고 최종 판정을 만든다.
`OutputAnalysisUseCase`는 output 로그를 병합 저장소로 전달한다. 파일 확장자·크기·워터마크와
문서 본문 검사는 `src/adapters/inspection_multimodal`이 담당하며, 정책은 각 `policy_*` adapter가 공급한다.

레거시 pipeline/valves 로더는 제거됐다. 출력 로그 저장 정책은 bootstrap의 `OutputLogOptions` 기본값으로
고정된다.

---

## 5. INPUT→OUTPUT 로그 merge

`PipelineBootstrap`은 환경변수에 따라 Memory 또는 Valkey store를 조립한다.
Memory는 프로세스 로컬 상태이므로 단일 worker만 허용한다. Valkey를 선택하면 여러
worker가 같은 병합 상태를 공유하며, 기동 시 Valkey 연결을 확인한다.

구현: `src/application/services/analysis_log_merge` +
`src/adapters/analysis_log_merge/{memory,valkey}_analysis_log_merge_store.py`.

```text
INPUT  → AnalysisLogMergeStore.set(message_id)
OUTPUT → AnalysisLogMergeStore.pop(message_id) → merge → bulk_logs 전달
```

| 환경 변수 | 개발 기본 | 의미 |
|-----------|-----------|------|
| `AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_BACKEND` | `memory` | 병합 저장소. `memory` 또는 `valkey` |
| `AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_KEY_PREFIX` | `pipeline:development:analysis-log-merge:v1:` | Valkey 병합 key prefix |
| `AIVAX_PIPELINE_LOG_MERGE_INPUT_SWEEP_INTERVAL_SECONDS` | `30` | 미완성 input 로그 flush 주기 |
| `AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_TTL_SECONDS` | `2400` | 병합 상태 보관 시간 |
| `AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_INPUT_FLUSH_AFTER_SECONDS` | `1800` | OUTPUT이 없을 때 prompt-only 전달까지의 시간. `0`이면 sweep 비활성화 |

**왜 sweep가 있나:** OUTPUT이 없더라도 INPUT 로그가 조용히 사라지지 않도록
일정 시간이 지난 prompt-only 문서를 bulk log로 전달한다.

**merge 신원:** OUTPUT이 나중에 알게 된 email/id로 **빈 INPUT 필드를 채운다**. 빈 OUTPUT으로 기존 email을 지우지 않음.  
테스트: `tests/analysis_log/test_log_merge_domain_models.py`.

유저·첨부파일·정책 알림 이벤트는 항상 발행되므로 Valkey는 필수 인프라다.
다중 worker에서는 로그 병합도 다음처럼 Valkey backend를 선택해야 한다.
`memory`인데 worker가 2개 이상이면 설정 검증 단계에서 기동을 거부한다.

```env
AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_BACKEND=valkey
AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_KEY_PREFIX=pipeline:production:analysis-log-merge:v1:
AIVAX_PIPELINE_WORKERS=4
```

---

## 6. 환경 변수 운영 체크

LIVE `/home1/aivax/pipeline/.env`:

- [ ] 로그 병합은 위 환경 변수 표
- [ ] `AIVAX_PIPELINE_LOG_BULK_DIR` = `/home1/aivax/pipeline/bulk_logs/`
- [ ] `AIVAX_PIPELINE_REGEX_FULL_SCAN=true`
- [ ] `AIVAX_PIPELINE_FILE_BLOCK_BACKUP_DIR`이 사이트 백업 경로와 일치
- [ ] `mode.json`의 `filters.slm.enabled`가 사이트 요구와 일치

---

## 8. 테스트

```bash
cd /home1/aivax/pipeline   # 또는 /home/AIVAX/aivax/apps/pipeline
export PYTHONPATH=.
python3 -m pytest tests/analysis_log/test_log_merge_domain_models.py
python3 scripts/smoke_valkey_merge.py          # Valkey + :9099 기동 후
```

---

## 9. env (`/home1/aivax/pipeline/.env`)

Pipeline은 이 파일 하나를 systemd `EnvironmentFile`과 앱 설정 로더에서 사용한다.

| 변수 | 기본 |
|------|------|
| `AIVAX_PIPELINE_WORKERS` | `1` |
