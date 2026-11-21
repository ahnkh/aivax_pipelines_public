# 폐쇄망(Air-gap) 설치 — Pipeline (+ Valkey) · ai_engine

**Date:** 2026-08-17  
**AIVAX ROOT (고정):** `/home1/aivax`  
**대상:** 인터넷·PyPI·GitHub에 닿지 않는 타겟  
**코드 맵:** [`MODULES.md`](MODULES.md)  
**고객 HA 끔 (따라 설치):** [`customer_install_ha_off.md`](../../../inline_engine/aivax/docs/customer_install_ha_off.md)  
**전제:**

- Python 패키지는 **`/home1/aivax/aivax-venv`에 미리 설치**  
  → packaging 설치기가 오프라인 의존성을 준비합니다.
- Valkey 서버·설정·unit은 AIVAX packaging이 먼저 설치하고 PING으로 준비 상태를 확인합니다.
  Pipeline 설치 중에는 서버 다운로드·컴파일을 수행하지 않습니다.
- packaging이 Pipeline unit을 등록·기동하고 엔진별 설치 절차를 호출
- 타겟 OS/arch ≈ 빌드 호스트 (el9 x86_64 등)

관련: [`DEPLOY_PIPELINE_1G.md`](DEPLOY_PIPELINE_1G.md) · [`../../../docs/DEPLOY_TARGET_HOST.md`](../../../docs/DEPLOY_TARGET_HOST.md) · [Valkey 인프라 안내](../../../docs/200-Architecture/230-infra/235-valkey/235-readme.md)

---

## 0. 사이트 레이아웃 (고정)

```text
/home1/aivax/
  ai_engine/
  aivax-venv/
  data_resource/
  extension/
  fluent-bit/
  management/
  pipeline/          ← Pipeline LIVE 코드
  slm/
  sslproxy/
  toolkit/
  version.json
```

| 변수 / 역할 | 경로 |
|-------------|------|
| AIVAX root | `/home1/aivax` |
| Pipeline LIVE root | `/home1/aivax/pipeline` (고정) |
| ai_engine `ENGINE_ROOT` | `/home1/aivax/ai_engine` |
| Python | `/home1/aivax/aivax-venv/bin/python3` |
| data | `/home1/aivax/data_resource` |

다른 루트(`/opt/aivax` 등)로 바꾸지 않는다.

---

## 1. 한 장 요약

```text
[반출망]
    AIVAX packaging CI로 Valkey 자산과 Pipeline 패키지를 함께 준비
    cd ../../ai_engine && ./scripts/pack.sh
    │  USB / 내부  (pipeline-x86_64.tar.gz · ai-engine-x86_64.tar.gz)
    ▼
[폐쇄망]
    cd /home1/aivax && tar -xzf pipeline-x86_64.tar.gz && tar -xzf ai-engine-x86_64.tar.gz
    aivax-venv 이미 OK
    packaging이 Pipeline unit을 등록·기동한 뒤 엔진 설치
    Valkey = packaging의 extension/valkey 페이로드로 선행 설치
```

| 구성 | 폐쇄망 | 비고 |
|------|--------|------|
| Pipeline 코드 | `/home1/aivax/pipeline` | RPM/단일 binary 없음 |
| Valkey 서버 | packaging의 `extension/valkey/` | → `/usr/local/bin` |
| Python 의존성 | **오프라인 준비** | packaging 설치기가 수행 |
| GitHub 빌드 | **없음** | 필수 자산 누락 시 설치 전 실패 |

---

## 2. 설치된 Valkey 확인

| 항목 | 값 |
|------|-----|
| 버전 | **9.1.1** |
| Arch | **x86_64** |
| 링크 | `libc` / `libm` |
| 동일 계열 호스트 (예: el9↔el9) | **문제 없음** |

```bash
ls /usr/local/bin/valkey-server
/usr/local/bin/valkey-server --version
ldd /usr/local/bin/valkey-server
```

---

## 3. 반출 · 타겟 배치

### 반출

```bash
cd aivax/apps/pipeline && ./scripts/pack.sh     # Valkey는 packaging이 별도로 포함
cd ../../ai_engine && ./scripts/pack.sh
```

- [ ] `pipeline-x86_64.tar.gz`와 별도 Valkey 자산을 포함한 전체 packaging 패키지
- [ ] `ai-engine-x86_64.tar.gz`
- [ ] `aivax-venv`는 보통 **타겟에 이미** 있음 (없으면 동일 OS용 venv를 통째 반입)
- [ ] `/home1/aivax/pipeline/.env`에 사이트별 DB·SLM·파일 경로·Valkey 설정 입력

### 타겟

```bash
cd /home1/aivax
sudo tar -xzf /tmp/pipeline-x86_64.tar.gz
sudo tar -xzf /tmp/ai-engine-x86_64.tar.gz
# 또는 rsync:
# sudo rsync -a /tmp/unpack/pipeline/  /home1/aivax/pipeline/
# sudo rsync -a /tmp/unpack/ai_engine/ /home1/aivax/ai_engine/

ls /home1/aivax/aivax-venv/bin/python3
ls /usr/local/bin/valkey-server
```

### Pipeline 환경 변수

| 키 | 사이트 값 예 |
|----|----------------|
| `AIVAX_PIPELINE_LOG_BULK_DIR` | `/home1/aivax/pipeline/bulk_logs/` |
| `AIVAX_PIPELINE_LOG_BULK_BACKUP_DIR` | `/data/backup/prompt_log_backup` (날짜별 원본 로그 백업) |
| `AIVAX_PIPELINE_FILE_BLOCK_BACKUP_DIR` | `/home1/aivax/data_resource/attach_file` |
| `AIVAX_PIPELINE_WORKERS` | `1` |
| `AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_BACKEND` | `memory` (다중 worker는 `valkey`) |
| `AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_KEY_PREFIX` | `pipeline:production:analysis-log-merge:v1:` |
| `AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_TTL_SECONDS` | `2400` |
| `AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_INPUT_FLUSH_AFTER_SECONDS` | `1800` |
| `AIVAX_PIPELINE_SLM_URL` | 사이트 SLM (예: `http://127.0.0.1:1200/...`) |

```bash
sudo mkdir -p /home1/aivax/pipeline/bulk_logs \
  /home1/aivax/data_resource/attach_file
```

### Python 확인 (오프라인)

```bash
PY=/home1/aivax/aivax-venv/bin/python3
$PY -c "import fastapi,uvicorn,uvloop,pydantic,aiohttp,requests,valkey,httpx,orjson; print('pipeline-ok')"
$PY -c "import aiohttp,brotli,zstandard; print('ai_engine-ok')"
```

---

## 4. Pipeline 설치

AIVAX packaging 설치기는 Pipeline archive를 배치하고 `make setup-offline`을 수행합니다.
내장된 `aivax-pipeline.service`를 배치 경로에 맞게 렌더링해 등록하고 엔진보다 먼저 기동합니다.
Pipeline의 `.env`와 포장 방식은 유지합니다. 소스 트리의 별도 설치 스크립트는 사용하지 않습니다.

설치 후 확인합니다.

```bash
systemctl is-active aivax-valkey aivax-pipeline
valkey-cli -h 127.0.0.1 -p 6379 PING
```

---

## 5. ai_engine 설치

Inline 고객(HA 끔)은 **`deploy/inline/install.sh`**. Mirror 사이트만 `deploy/mirror/install.sh`.

```bash
cd /home1/aivax/ai_engine
sudo -E AIVAX_ENGINE_LOG=/home1/aivax/ai_engine/ai_engine.log \
  bash deploy/inline/install.sh
# SKIP_PIP=1 이 기본값. ai_engine는 자체 환경 파일만 사용
```

- Valkey는 ai_engine이 설치하지 않음  
- Inline install은 `/etc/aivax/mode.json` (`mode=inline`) 기록. Mirror는 `deploy/mirror/install.sh` (`mode=mirror`). **한 호스트에서 엔진은 한 모드만** (첨부 tmpfs 공유). Pipeline은 둘 다 같은 `:9099`.  

---

## 6. 검증

```bash
systemctl is-active aivax-valkey aivax-pipeline
systemctl is-active aivax-engine-inline || systemctl is-active aivax-engine-mirror
valkey-cli -h 127.0.0.1 -p 6379 PING
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9099/docs
# Inline일 때:
ls -l /run/aivax/policy.sock
# Mirror일 때:
pgrep -af shm_ring_consumer
cat /etc/aivax/mode.json
systemctl cat aivax-pipeline | grep WorkingDirectory
# → WorkingDirectory=/home1/aivax/pipeline
```

---

## 7. 설치기 실행 순서

packaging 설치기는 Valkey 준비, Pipeline 배치·의존성 준비·unit 기동, 엔진 배포 순서로 진행합니다.
Pipeline unit은 packaging에서 관리하고 엔진 모드별 설치 스크립트는 해당 엔진의 계약을 따릅니다.

---

## 8. 트러블슈팅

| 증상 | 조치 |
|------|------|
| Valkey 자산 누락 | 전체 packaging 패키지의 `extension/valkey/`를 확인 |
| GLIBC not found | 동일 OS 계열 번들 사용 |
| 의존성 준비 실패 | packaging의 offline-wheel과 공용 venv를 확인 |
| ModuleNotFoundError | `aivax-venv`에 패키지 보강 |
| 경로 FileNotFound | config를 `/home1/aivax/...` 로 |

---

## 9. 비주장

폐쇄망 반입·설치 절차. RPM/단일 binary·10G 주장 아님. Valkey는 x86_64 glibc 동적 링크.

---

## 10. inline_engine (별도)

제어면·datapath 의존성·폐쇄망 판정·서비스명:  
[`inline_engine/aivax/docs/MODULES_AND_STRUCTURE.md`](../../../inline_engine/aivax/docs/MODULES_AND_STRUCTURE.md) **§0.5 · §6 · §7**.

요약:
- Python → `aivax-venv` (PyYAML+cryptography, `SKIP_PIP=1`)
- OVS-DPDK / Envoy → `third_party` tarball (타겟 빌드·pip 금지)
- OS 툴·MariaDB/OpenSearch·bypass.ko → 출고 이미지
- 제어면 systemd 일괄 → `install_prod_control_services.sh` (`AIVAX_INLINE_ROOT=/home1/aivax/inline_engine`)
