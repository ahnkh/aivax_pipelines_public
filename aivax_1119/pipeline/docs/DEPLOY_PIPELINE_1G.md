# AIVAX Pipeline 배포 — 1G (Inline / Mirror 공용)

**Date:** 2026-08-17  
**Systemd 관리:** AIVAX packaging 설치기
**코드 맵:** [MODULES.md](MODULES.md)

**고객 Inline · HA 끔 따라 설치 (정본):**  
[`inline_engine/aivax/docs/customer_install_ha_off.md`](../../../inline_engine/aivax/docs/customer_install_ha_off.md)  
이 문서의 빠른 설치는 그 안의 **§4** 와 같다. HA와 무관 — Pipeline은 하나.

Pipeline은 **Inline ChatGate와 Mirror가 공유**하는 판정 API입니다.  
모드별로 Pipeline을 두 개 띄우지 않습니다. 엔진만 갈라집니다.

```text
Inline:  Envoy → aivax-engine-inline (UDS) → Pipeline :9099
Mirror:  Suricata SHM → aivax-engine-mirror → Pipeline :9099   # 동일·필수 (AIVAX_POLICY_API=1)
```

## 운영 설치

2026-11-12부터 Pipeline systemd unit과 서비스 설치 로직은 별도 AIVAX packaging 저장소에서 관리합니다.
unit 원본은 `etc/systemd/aivax-pipeline.service`이며 설치기 내부의 `config/systemd/`에 포함됩니다.
Pipeline 소스에는 unit 설치·제거 스크립트를 두지 않습니다.

packaging 설치기는 다음 순서로 작업합니다.

1. Valkey 등 공통 구성요소를 준비합니다.
2. Pipeline archive를 배치하고 `make setup-offline`으로 의존성을 준비합니다.
3. unit의 `@PIPELINE_ROOT@`를 배치 경로로 바꾸고 이전 cpu·deps·workers drop-in을 정리합니다.
4. `daemon-reload` 후 `enable --now aivax-pipeline.service`를 실행합니다.
5. 엔진 설치를 진행하고 마지막 서비스 확인 단계에서 Pipeline 상태를 확인합니다.

기본 배치 경로는 `/home1/aivax/pipeline`, 공용 Python은 `/home1/aivax/aivax-venv/bin/python3`입니다.
`.env`는 Pipeline이 관리하며 기존 포장·로딩 방식을 유지합니다.
workers 설정을 바꾼 뒤에는 `sudo systemctl restart aivax-pipeline`로 반영합니다.
서비스 기동 실패는 기존과 같이 기록하고 나머지 설치를 계속합니다.

설치기에서 Pipeline archive는 `main.py`, `pyproject.toml`, `src/`로 식별합니다.
앱 실행·설정 계약은 [Pipeline 구조](../../../docs/200-Architecture/220-pipeline/220-readme.md)를 참고합니다.

## systemd 구성

### Unit

`/etc/systemd/system/aivax-pipeline.service` (원본: packaging의 `etc/systemd/aivax-pipeline.service`)

- `ExecStart=/home1/aivax/aivax-venv/bin/python3 <배치 경로>/main.py`
- `AIVAX_PIPELINE_WORKERS` → `main.py`가 uvicorn multi-worker로 기동
- 각 worker의 OpenBLAS/OpenMP/MKL/NumExpr thread 수는 `1`로 제한
- 콘솔 로그: systemd journal. 앱 파일 로그는 Pipeline 설정을 따릅니다.
- 설정: LIVE `.env` 하나를 `EnvironmentFile`로 주입

### 단일 Pipeline unit

Pipeline의 CPU affinity, native thread 제한, Valkey 의존성은
`aivax-pipeline.service` 본문에 포함한다. Valkey도 `aivax-valkey.service`와
`valkey.conf` 정적 파일을 사용한다. 설치 시 구버전 drop-in은 제거한다.

### CPU 맵 (이 호스트 1G)

```text
NUMA0  0-11,24-35   DPDK / OVS / Envoy
NUMA1  12-15        Pipeline workers ×4
       16           Inline ai_engine (별도)
       17           Valkey
       18-23,36-37  Mirror SHM (별도)
```

기준 env: LIVE `/home1/aivax/pipeline/.env` (ai_engine의 설정 파일과 분리됨)

CPU 범위나 native thread 제한을 변경할 때는
packaging의 `etc/systemd/aivax-pipeline.service`에서 운영 CPU 설정을 관리한다.

변경한 unit을 배포한 뒤 `systemctl daemon-reload`와 Pipeline 재시작이 필요하다.

현장 CPU 배치가 다른 경우 packaging의 unit에 해당 배치를 반영합니다.
등록 시 알려진 이전 drop-in만 제거하며, 다른 drop-in이 남아 있으면 등록을 중단합니다.

## Valkey (INPUT→OUTPUT 로그 merge)

workers≥2이면 **필수**. 상세 동작: [MODULES.md](MODULES.md) **§5**.

| 항목 | 값 |
|------|-----|
| Unit | `aivax-valkey.service` |
| Listen | `127.0.0.1:6379` |
| 설정 | LIVE `.env`의 `AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_*`, `AIVAX_PIPELINE_VALKEY_*` |
| backend | `AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_BACKEND=valkey` |
| 연결 실패 | Pipeline 기동 거부(자동 memory fallback 없음) |
| key_prefix | `pipeline:production:analysis-log-merge:v1:` |
| TTL | **2400s** |
| input_flush_after_seconds | **1800s** (OUTPUT 없는 INPUT을 prompt-only로 flush — Mirror TAP) |
| input_sweep_interval_sec | **30** |

Valkey 바이너리·설정·unit 설치는 AIVAX packaging 설치기가 수행합니다.
Pipeline 설치·제거 스크립트는 Valkey 자산을 관리하지 않습니다.

장애 시:

```bash
systemctl start aivax-valkey
systemctl restart aivax-pipeline
```

관련 설계: AIVAX 루트 기준  
`inline_engine/aivax/evidence/PROVE/PERF_ENGINE_PIPELINE/MULTI_WORKER_MERGE.md`

## 환경 변수 체크리스트 (LIVE)

배포 전 LIVE `/home1/aivax/pipeline/.env` 확인:

- [ ] 단일 worker면 `AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_BACKEND=memory`
- [ ] 다중 worker면 `AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_BACKEND=valkey`와 고유한 `AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_KEY_PREFIX` 설정
- [ ] `AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_TTL_SECONDS=2400`
- [ ] `AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_INPUT_FLUSH_AFTER_SECONDS=1800` (TTL보다 작을 것)
- [ ] `AIVAX_PIPELINE_LOG_MERGE_INPUT_SWEEP_INTERVAL_SECONDS=30`
- [ ] `AIVAX_PIPELINE_LOG_BULK_DIR`가 LIVE 경로와 일치
- [ ] `AIVAX_PIPELINE_REGEX_FULL_SCAN=true` (운영 요구)
- [ ] `AIVAX_PIPELINE_SLM_URL`, `AIVAX_PIPELINE_SLM_REQUEST_TIMEOUT_SECONDS`는 사이트에 맞게 설정. `mode.json`의 `filters.slm.enabled=false`이면 SLM 비활성화

## 검증

```bash
systemctl is-active aivax-pipeline aivax-valkey
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9099/docs   # 200
ss -ltnp | grep 9099
taskset -cp "$(systemctl show aivax-pipeline -p MainPID --value)"       # 12-15
pgrep -af '/home1/aivax/pipeline/main.py'                              # master + 4 workers

# Valkey
valkey-cli -h 127.0.0.1 -p 6379 PING                                   # PONG

# Merge smoke (REPO 또는 LIVE에서)
cd /home1/aivax/pipeline
/home1/aivax/aivax-venv/bin/python scripts/smoke_valkey_merge.py

# merge identity (Valkey 불필요)
PYTHONPATH=/home1/aivax/pipeline /home1/aivax/aivax-venv/bin/python -m pytest \
  tests/analysis_log/test_log_merge_domain_models.py
```

로그:

```bash
tail -f /home1/aivax/pipeline/pipeline-stderr.log
# "log_merge_queue backend=valkey" / "valkey merge store ready prefix=pipeline:analysis-log-merge:v1:"
```

## Inline / Mirror와 함께

packaging 설치기는 공용 Pipeline unit 등록·기동을 엔진 배포보다 먼저 수행합니다.
Pipeline 자체에는 설치 모드 선택이 없으며 Inline과 Mirror가 같은 서비스를 사용합니다.
엔진의 모드별 배포 절차는 해당 엔진 저장소에서 관리합니다.

## 개발용 수동 실행 (명령어)

systemd 대신 터미널에서 띄울 때. **`:9099` / `:6379`가 운영 unit과 겹치면** 먼저 중지하세요.

```bash
sudo systemctl stop aivax-pipeline
# Valkey도 수동으로 띄울 경우
sudo systemctl stop aivax-valkey
```

### 1) Valkey 준비 확인

AIVAX packaging이 설치한 `valkey-server`와 `valkey-cli`가 PATH에 있어야 합니다.
서버 설치·운영 데이터 경로는 [Valkey 인프라 안내](../../../docs/200-Architecture/230-infra/235-valkey/235-readme.md)에서 확인합니다.

```bash
systemctl is-active aivax-valkey
valkey-cli -h 127.0.0.1 -p 6379 PING
```

테스트·부하 도구는 설치된 실행 파일로 별도 Valkey를 띄우고 자동 종료합니다.
Python `valkey` 패키지는 클라이언트이며 서버 실행 파일은 별도로 설치돼 있어야 합니다.

### 2) Pipeline 수동 기동

cwd를 **pipeline 루트**로 두세요. 실행 설정은 선택한 환경 파일과 셸 환경변수에서 읽습니다.

```bash
cd /home/AIVAX/aivax/apps/pipeline
# LIVE에서 돌릴 때: cd /home1/aivax/pipeline

# 사이트 공용 Python (전용 venv 만들지 않음)
PY=/home1/aivax/aivax-venv/bin/python3

# 기본: 127.0.0.1:9099 · worker 1 (디버그에 적합)
$PY main.py --printlog
```

| 옵션 / env | 의미 |
|------------|------|
| `--printlog` | 콘솔 로그 |
| `--host` / `--port` | listen (기본 `127.0.0.1` / `9099`) |
| `--workers N` | uvicorn 워커 수 (CLI가 env보다 우선) |
| `--debug` | 로그 레벨 DEBUG |
| `AIVAX_PIPELINE_WORKERS` | `--workers` 없을 때 기본 워커 수 |
| `AIVAX_PIPELINE_HOST` / `PORT` | multi-worker factory 경로에서 사용 |

예시:

```bash
# 다른 포트 (운영 9099와 병행 가능)
$PY main.py --host 127.0.0.1 --port 9100 --printlog

# multi-worker (Valkey 필수 — config fallback_to_memory=false)
$PY main.py --workers 4 --printlog
# 또는
AIVAX_PIPELINE_WORKERS=4 $PY main.py --printlog
```

**config와 Valkey**

- `log_merge_queue.backend=valkey` + `fallback_to_memory=false` → Valkey가 안 떠 있으면 **기동 실패**
- 개발만 빠르게: worker **1** 유지 + Valkey를 위 절대로 먼저 기동 (권장). memory로 임시 바꾸는 것은 multi-worker 테스트와 혼동하지 말 것

확인:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9099/docs
# merge smoke
$PY scripts/smoke_valkey_merge.py
```

### 3) 운영 unit으로 복귀

```bash
# 수동 Valkey/Pipeline 프로세스가 남아 있으면 먼저 종료
sudo systemctl start aivax-valkey
sudo systemctl start aivax-pipeline
systemctl is-active aivax-valkey aivax-pipeline
```

## 삭제 / 롤백

서비스 제거와 복원은 packaging의 Reset·Rollback 절차를 사용합니다.
서비스만 일시 중지할 때는 `sudo systemctl stop aivax-pipeline`을 실행합니다.

## 트러블슈팅

| 증상 | 원인 / 조치 |
|------|-------------|
| Pipeline start 실패, Valkey 관련 RuntimeError | Valkey down 또는 `fallback_to_memory=false` — Valkey 기동 후 Pipeline restart |
| docs 200인데 merge miss | workers>1 + memory backend — config를 valkey로 |
| INPUT만 있고 OUTPUT이 없음 (Mirror) | 정상일 수 있음. `input_flush_after_seconds` 후 prompt-only flush. TTL만 짧으면 로그 유실 |
| UI에 빈 프롬프트 문서 | 구버전 merge_fail가 빈 dict를 저장. 현재는 INPUT miss 시 문서 미생성 |
| affinity가 0-47 | packaging unit의 `AllowedCPUs`와 실제 배포 파일을 확인 |
| 코드 수정이 안 보임 | 실행 중인 unit의 `WorkingDirectory`와 현재 checkout이 다른지 `systemctl cat`으로 확인 |
| pip/venv 혼동 | 사이트 공용 `/home1/aivax/aivax-venv` 사용 — Pipeline 전용 venv 만들지 않음 |

## 레포 파일

```text
docs/
  README.md
  MODULES.md                  # 코드 맵 · API · merge
  DEPLOY_PIPELINE_1G.md       # 이 문서
  DEPLOY_OFFLINE_AIRGAP.md
scripts/
  pack.sh                     # dist/pipeline-x86_64.tar.gz (최상단 pipeline/)
  smoke_valkey_merge.py
  soak_valkey_merge_http.py
src/adapters/analysis_log_merge/
  valkey_analysis_log_merge_store.py
src/application/services/analysis_log_merge/
  service.py
```

## 비주장

- 이 배포는 **1G 판정 스택 운영 절차**이며 Fast soft line-rate / 10G 주장이 아님
- 실 GPU SLM·OCR 코퍼스 재측정은 별도 환경에서 수행
