# ai_engine Inline 인스톨러 — 쉘 호출 상세 (`deploy/inline/install.sh`)

**Date:** 2026-08-17  
**한 줄:** 트리 배치 후 `sudo bash deploy/inline/install.sh` → `aivax-engine-inline.service`  
**대상:** Envoy ChatGate → `/run/aivax/policy.sock` → Pipeline `:9099`  
**형제:** Mirror는 `deploy/mirror/install.sh` — 전환 정본 [`MODE_INLINE_MIRROR.md`](MODE_INLINE_MIRROR.md) · 코드 [`MODULES.md`](MODULES.md)

**고객 HA 끔 따라 설치:** Pipeline 다음 이 스크립트.  
[`customer_install_ha_off.md`](../../inline_engine/aivax/docs/customer_install_ha_off.md) **§5**.

타겟 호스트 / Outer 호출 계약: [`../../docs/DEPLOY_TARGET_HOST.md`](../../docs/DEPLOY_TARGET_HOST.md)  
(**AIVAX ROOT = `/home1/aivax` 고정**, Outer가 이 `install.sh`를 직접 호출)  
폐쇄망이 **기본** (`SKIP_PIP=1`): [`../../aivax/pipeline/docs/DEPLOY_OFFLINE_AIRGAP.md`](../../aivax/pipeline/docs/DEPLOY_OFFLINE_AIRGAP.md)  
온라인 pip: `SKIP_PIP=0`

---

## 0. 인스톨러가 하는 일 / 안 하는 일

| 함 | 안 함 |
|----|--------|
| **반대 모드(Mirror) stop** + 첨부 reconcile/wipe | Mirror unit을 새로 설치 |
| `@…@` 치환해서 systemd unit 설치 | 코드를 다른 경로로 rsync (트리가 곧 `ENGINE_ROOT`) |
| `/etc/aivax/ai_engine.env` | Pipeline `pipeline.env` / Pipeline unit |
| 공용 Python에 deps 확인 (기본 SKIP_PIP) | 전용 venv 생성 |
| `enable --now aivax-engine-inline` | Envoy / OVS / flow_agent 설치 |
| 있으면 Valkey·Pipeline도 `--now` | 없으면 만들어 주지는 않음 |
| `/etc/aivax/mode.json` (`mode=inline`) | management가 파일을 파싱하는 UI (별도) |

```text
[당신이 먼저]  ai_engine 트리를 타겟 경로에 둔다
       │
       ▼
 sudo bash …/ai_engine/deploy/inline/install.sh
       │
       ├─ _aivax_deploy_init   → ENGINE_ROOT, PYTHON, LOG
       ├─ _aivax_install_engine_env → /etc/aivax/ai_engine.env only
       ├─ pip / deps 확인
       ├─ _aivax_prepare_mode_switch inline
       │     → Mirror unit stop → reconcile → attachments wipe
       ├─ render aivax-engine-inline.service → /etc/systemd/system/
       ├─ daemon-reload
       ├─ enable --now aivax-engine-inline (+ valkey/pipeline if present)
       └─ _aivax_write_mode_json inline → /etc/aivax/mode.json
```

**반대 모드** = Mirror (`aivax-engine-mirror` / metrics / lifecycle).  
같은 호스트에서 Inline과 Mirror lifecycle을 **동시에** 돌리지 마세요.  
`SKIP_MODE_SWITCH=1` / `SKIP_ATTACH_CLEANUP=1` — [MODE_INLINE_MIRROR.md](MODE_INLINE_MIRROR.md) §3.

---

## 1. 호출 한 줄 (상황별)

### 1.1 같은 개발 머신

```bash
sudo bash /home/AIVAX/ai_engine/deploy/inline/install.sh
# 제품 타겟은 아래 1.2 (/home1/aivax)
```

### 1.2 타겟 호스트 (`/home1/aivax` 고정)

```bash
export AIVAX_ENGINE_LOG=/home1/aivax/ai_engine/ai_engine.log
# DRY_RUN 으로 치환 결과만 먼저 보기
sudo -E DRY_RUN=1 bash /home1/aivax/ai_engine/deploy/inline/install.sh

# 실제 설치
sudo -E bash /home1/aivax/ai_engine/deploy/inline/install.sh
```

`ENGINE_ROOT`는 **환경변수가 아니라** `install.sh`가 있는 트리로 고정됩니다.  
`/home1/aivax/ai_engine/deploy/inline/install.sh` → `ENGINE_ROOT=/home1/aivax/ai_engine`.

### 1.3 자주 쓰는 knobs

```bash
# 기본 설치 (Mirror stop + 첨부 정리 + ai_engine.env + aivax-engine-inline)
sudo -E bash deploy/inline/install.sh

# unit만 깔고 기동은 나중에
SKIP_START=1 sudo -E bash deploy/inline/install.sh
sudo systemctl start aivax-engine-inline

# env 파일 유지, unit만 재렌더
SKIP_ENV=1 sudo -E bash deploy/inline/install.sh

# mode-switch 끄기 (반대 모드/첨부 손대지 않음)
SKIP_MODE_SWITCH=1 sudo -E bash deploy/inline/install.sh

# mode.json 쓰지 않기 (비권장)
SKIP_MODE_JSON=1 sudo -E bash deploy/inline/install.sh

# 반대 모드만 stop, reconcile/wipe 생략
SKIP_ATTACH_CLEANUP=1 sudo -E bash deploy/inline/install.sh

# 온라인에서만 pip
# SKIP_PIP=0 sudo -E bash deploy/inline/install.sh

# 로그 위치
AIVAX_ENGINE_LOG=/home1/aivax/ai_engine/ai_engine.log \
  sudo -E bash deploy/inline/install.sh
```

---

## 2. 단계별 — 쉘이 실제로 하는 일

아래는 `install.sh`를 사람이 손으로 풀어 쓴 것과 같습니다.

### Step 0 — 경로·Python 결정 (`_aivax_deploy_init`)

```bash
HERE=…/ai_engine/deploy/inline
ENGINE_ROOT=…/ai_engine          # HERE/../..
AIVAX_PYTHON= 다음 중 첫 실행 가능 파일:
  1) /home1/aivax/aivax-venv/bin/python3
  2) $VIRTUAL_ENV/bin/python3
  3) $(command -v python3)
  4) /usr/bin/python3
ENGINE_LOG=${AIVAX_ENGINE_LOG:-/home1/aivax/ai_engine/ai_engine.log}
```

타겟에서 공용 venv는 **이미** `/home1/aivax/aivax-venv` 이다 (심볼릭 링크 불필요).

```bash
ls -l /home1/aivax/aivax-venv/bin/python3
# 임시로 다른 env를 쓸 때만:
# export VIRTUAL_ENV=/home1/aivax/aivax-venv
# export PATH="$VIRTUAL_ENV/bin:$PATH"
```

### Step 1 — ai_engine.env (`_aivax_install_engine_env`)

| 소스 | 설치 위치 |
|------|-----------|
| `deploy/common/ai_engine.env` | `/etc/aivax/ai_engine.env` (`AIVAX_ENGINE_ROOT=` 패치) |

**Pipeline은 건드리지 않음.**  
`/etc/aivax/pipeline.env` 및 `aivax-pipeline.service.d/*` 는 `pipeline/deploy/install.sh` 소유.

`SKIP_ENV=1` (또는 legacy `SKIP_COMMON=1`) 이면 이 단계를 건너뛴다.

### Step 2 — pip

```bash
# 개념상
/home1/aivax/aivax-venv/bin/pip install -r $ENGINE_ROOT/requirements.txt
# → aiohttp, brotli, zstandard
```

### Step 2b — mode-switch (`_aivax_prepare_mode_switch inline`)

Mirror(반대 모드)를 끄고 공유 첨부를 정리한다. 구현: `deploy/common/_lib.sh`.

1. stop: `aivax-engine-mirror` / `aivax-engine-metrics` / `aivax-engine-lifecycle` (+ 구형 `aivax-shm-consumer` 등)
2. `core/attachment_lifecycle.py --reconcile-only`
3. `/run/aivax/attachments` 내용 wipe (마운트 유지)

| 변수 | 효과 |
|------|------|
| `SKIP_MODE_SWITCH=1` | 이 단계 전체 스킵 |
| `SKIP_ATTACH_CLEANUP=1` | stop만, reconcile/wipe 안 함 |

정본: [MODE_INLINE_MIRROR.md](MODE_INLINE_MIRROR.md).

### Step 3 — unit 렌더

템플릿 `deploy/inline/aivax-engine-inline.service`의 치환:

| 토큰 | 값 |
|------|-----|
| `@AIVAX_ENGINE_ROOT@` | 이 트리 절대경로 |
| `@AIVAX_PYTHON@` | Step 0 Python |
| `@AIVAX_ENGINE_LOG@` | 로그 파일 |

결과 요지:

```text
WorkingDirectory=<ENGINE_ROOT>
Environment=PYTHONPATH=<root>:<root>/core:<root>/mirror
ExecStart=<python> -u <root>/core/ai_policy_engine.py \
  --socket /run/aivax/policy.sock \
  --server-url http://127.0.0.1 --port 9099 \
  --policy-api-timeout-ms 60000 \
  --policy-api-max-inflight 256 \
  --policy-api-failure-policy open \
  …
CPUAffinity / AllowedCPUs = 16   (drop-in cpu.conf 도 동일)
RuntimeDirectory=aivax           → /run/aivax 자동 생성
```

수동으로 같은 일을 하려면:

```bash
ENGINE_ROOT=/home1/aivax/ai_engine
PY=/home1/aivax/aivax-venv/bin/python3
LOG=/home1/aivax/ai_engine/ai_engine.log
sed -e "s|@AIVAX_ENGINE_ROOT@|$ENGINE_ROOT|g" \
    -e "s|@AIVAX_PYTHON@|$PY|g" \
    -e "s|@AIVAX_ENGINE_LOG@|$LOG|g" \
    "$ENGINE_ROOT/deploy/inline/aivax-engine-inline.service" \
  | sudo tee /etc/systemd/system/aivax-engine-inline.service
sudo mkdir -p /etc/systemd/system/aivax-engine-inline.service.d
sudo cp "$ENGINE_ROOT/deploy/inline/aivax-engine-inline.cpu.conf" \
  /etc/systemd/system/aivax-engine-inline.service.d/cpu.conf
```

### Step 4–5 — reload / start

```bash
sudo systemctl daemon-reload
# 있으면:
sudo systemctl enable --now aivax-valkey.service
sudo systemctl enable --now aivax-pipeline.service
# 항상:
sudo systemctl enable --now aivax-engine-inline.service
```

Unit 의존성: `After=` / `Wants=aivax-pipeline` (하드 `Requires` 아님).  
Pipeline이 없어도 policy는 기동되지만, 트래픽 시 판정 API 호출이 실패하고 `failure-policy open`이면 통과 쪽으로 기울 수 있습니다.

---

## 3. 설치 전 체크리스트 (타겟)

```bash
# 1) 트리
ls /home1/aivax/ai_engine/core/ai_policy_engine.py
ls /home1/aivax/ai_engine/deploy/inline/install.sh

# 2) Python + deps 자리
ls -l /home1/aivax/aivax-venv/bin/python3   # 또는 VIRTUAL_ENV
/home1/aivax/aivax-venv/bin/python3 -c "import sys; print(sys.version)"

# 3) (권장) Pipeline이 이미 떠 있으면 판정 E2E까지 바로 검증 가능
systemctl is-active aivax-pipeline aivax-valkey || echo "Pipeline 아직 — policy만 먼저 가능"

# 4) CPU 16 존재 여부 (없으면 cpu.conf 수정 필요)
nproc
# 타겟 vCPU < 17 이면 install 후:
#   sudo sed -i 's/16/0/' /etc/systemd/system/aivax-engine-inline.service
#   sudo sed -i 's/16/0/' /etc/systemd/system/aivax-engine-inline.service.d/cpu.conf
#   sudo systemctl daemon-reload && sudo systemctl restart aivax-engine-inline

# 5) 로그 디렉터리
sudo mkdir -p /home1/aivax/ai_engine /var/log/aivax
```

---

## 4. 설치 후 검증 (쉘)

```bash
systemctl is-active aivax-engine-inline
systemctl status aivax-engine-inline --no-pager -l | head -30
ls -l /run/aivax/policy.sock

# unit에 박힌 경로 확인
systemctl cat aivax-engine-inline | grep -E 'ExecStart|WorkingDirectory|PYTHONPATH|StandardOutput'

# CPU pin
taskset -cp "$(systemctl show aivax-engine-inline -p MainPID --value)"

# 로그
tail -n 50 "$(systemctl show aivax-engine-inline -p StandardOutput --value | sed 's/^append://')"
# 또는
journalctl -u aivax-engine-inline -n 50 --no-pager

# Pipeline 연동 (Pipeline 살아 있을 때)
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9099/docs
```

레거시와 혼용 금지:

```bash
# 있으면 끄세요 — 레거시 SSLproxy ai-engine (/tmp/sslproxy-ai-engine.sock). 제품은 사용 안 함.
systemctl is-active ai-engine.service 2>/dev/null && echo "CONFLICT: stop legacy ai-engine"
```

---

## 5. 재배포 / 코드만 갱신

인스톨러는 rsync를 하지 않습니다. **파일을 트리에 덮어쓴 뒤** 재시작만 하면 됩니다.

```bash
# 예: 번들에서 adapters/core 만 갱신
sudo rsync -a /tmp/unpack/ai_engine/ /home1/aivax/ai_engine/
sudo systemctl restart aivax-engine-inline

# unit 템플릿·플래그를 바꿨으면 install 다시
sudo -E bash /home1/aivax/ai_engine/deploy/inline/install.sh
```

---

## 6. 삭제

```bash
sudo bash deploy/inline/uninstall.sh
# common drop-in까지:
REMOVE_COMMON=1 sudo -E bash deploy/inline/uninstall.sh
# mode.json 삭제:
REMOVE_MODE=1 sudo -E bash deploy/inline/uninstall.sh
```

---

## 7. 실패 패턴

| 증상 | 원인 | 조치 |
|------|------|------|
| `ai_policy_engine.py missing` | 잘못된 트리 / 부분 복사 | `ENGINE_ROOT`에 `core/` 포함 확인 |
| unit start → ModuleNotFoundError | pip 안 됨 / 다른 python | `systemctl cat`의 ExecStart python으로 `pip install -r requirements.txt` |
| sock 없음 + status failed | 로그 디렉터리 없음 · import 에러 | `journalctl -u aivax-engine-inline -n 80` |
| `Failed to set CPU affinity` | CPU 16 없음 | cpu.conf / unit AllowedCPUs 수정 |
| Pipeline 연결 실패 로그 | `:9099` down | Pipeline 설치·기동 후 `restart aivax-engine-inline` |
| 예전 코드가 실행됨 | 다른 경로 unit | `systemctl cat` WorkingDirectory 확인 |
| `unrecognized arguments:` (Mirror) | unit에 `${hp%%:*}` 가 남아 systemd가 `%%`를 깨뜨림 | `ExecStart`가 `run_shm_consumer.sh` 인지 확인 후 mirror `install.sh` 재실행 |

---

## 8. 권장 호출 순서 (타겟, ai_engine만 먼저)

Pipeline을 **나중에** 올려도 policy unit은 설치 가능합니다. 다만 E2E 전에는 Pipeline이 필요합니다.

```bash
# --- 준비 ---
# AIVAX ROOT = /home1/aivax (고정)
ls /home1/aivax/aivax-venv/bin/python3
sudo rsync -a /tmp/aivax-unpack/ai_engine/ /home1/aivax/ai_engine/

# --- dry-run ---
cd /home1/aivax/ai_engine
sudo -E DRY_RUN=1 AIVAX_ENGINE_LOG=/home1/aivax/ai_engine/ai_engine.log \
  bash deploy/inline/install.sh

# --- install ---
sudo -E AIVAX_ENGINE_LOG=/home1/aivax/ai_engine/ai_engine.log \
  bash deploy/inline/install.sh

# --- verify ---
systemctl is-active aivax-engine-inline
ls -l /run/aivax/policy.sock
cat /etc/aivax/mode.json   # "mode":"inline"
```

다음 순서(별도 세션): Pipeline 인스톨러 → `curl :9099/docs` → policy 재확인.

---

## 9. 관련 파일 맵

```text
ai_engine/
  requirements.txt
  core/ai_policy_engine.py          # ExecStart 엔트리
  core/http_payload_normalizer.py
  core/account_lookup.py
  deploy/
    common/
      _lib.sh                       # python resolve, render, mode.json, mode-switch
      ai_engine.env                 # → /etc/aivax/ai_engine.env
      mode.json.example
      install_common.sh             # env만 단독 설치
    inline/
      install.sh                    # ← 이 문서의 인스톨러
      uninstall.sh
      aivax-engine-inline.service          # 템플릿 (@토큰)
      aivax-engine-inline.cpu.conf
    mirror/
      install.sh                    # SHM (참고) — ExecStart=run_shm_consumer.sh
  scripts/
    run_engine.sh                   # 로컬 디버그
    run_shm_consumer.sh             # 제품 Mirror
  docs/
    INSTALLER_INLINE.md             # 이 문서
    DEPLOY_INLINE_1G.md             # 요약 ops
    MODULES.md
```
