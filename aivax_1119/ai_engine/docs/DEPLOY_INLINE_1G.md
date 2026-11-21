# Inline 배포 — 1G ChatGate (`aivax-engine-inline`)

**Date:** 2026-08-17  
**Install:** `sudo bash deploy/inline/install.sh`  
**Uninstall:** `sudo bash deploy/inline/uninstall.sh`  
**서비스 전체·모드 전환 정본:** [MODE_INLINE_MIRROR.md](MODE_INLINE_MIRROR.md) **§0**  
**코드 맵:** [MODULES.md](MODULES.md)

**고객 HA 끔:** Pipeline이 `:9099` 200 인지 확인한 뒤 이 install.  
순서 정본: [`customer_install_ha_off.md`](../../inline_engine/aivax/docs/customer_install_ha_off.md).  
`deploy/mirror/install.sh` 는 Inline 현장에 쓰지 않는다.

`aivax-inline` → `/run/aivax/policy.sock` (`aivax-engine-inline`) → Pipeline `:9099`  
코드는 Mirror와 **동일 트리** (`/home1/aivax/ai_engine`).

Inline 풀스택 서비스: pipeline/valkey · datapath · bypass-inline · **aivax-inline** · **aivax-engine-inline** · registry · leaf-issuer · flow-agent · health-agent · metrics(`:9100`).

## 한 호스트 · Mirror와 동시 금지

같은 `/run/aivax/attachments` tmpfs. **순차 전환만.**

`deploy/inline/install.sh` 기본 동작 (`_aivax_prepare_mode_switch inline`):

1. **반대 모드** stop — `aivax-engine-mirror` / metrics / lifecycle (+ 구형 `aivax-shm-consumer` 등)
2. `attachment_lifecycle --reconcile-only`
3. `/run/aivax/attachments` **내용 wipe**

| 변수 | 의미 |
|------|------|
| *(기본)* | stop + reconcile + wipe |
| `SKIP_MODE_SWITCH=1` | mode-switch 전부 스킵 |
| `SKIP_ATTACH_CLEANUP=1` | 반대 모드 stop만 |
| `SKIP_MODE_JSON=1` | `/etc/aivax/mode.json` 미기록 (비권장) |

상세: [MODE_INLINE_MIRROR.md](MODE_INLINE_MIRROR.md).

## 설치

```bash
sudo bash /home1/aivax/ai_engine/deploy/inline/install.sh
```

하는 일: mode-switch 정리 → `ai_engine.env` → `aivax-engine-inline.service` 렌더 → `enable --now` → **`/etc/aivax/mode.json` (`mode=inline`)**.

인스톨러 상세: [INSTALLER_INLINE.md](INSTALLER_INLINE.md).  
제품 모드 계약: [MODE_INLINE_MIRROR.md](MODE_INLINE_MIRROR.md) **§1b**.

## 검증

```bash
systemctl is-active aivax-engine-inline aivax-pipeline aivax-valkey
systemctl is-active aivax-flow-agent || true
ls -l /run/aivax/policy.sock
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9099/docs
taskset -cp "$(systemctl show aivax-engine-inline -p MainPID --value)"   # expect: 16
systemctl is-active aivax-engine-mirror || echo "shm stopped (ok)"
cat /etc/aivax/mode.json   # "mode":"inline"
```

## CPU (이 호스트 1G)

| 컴포넌트 | CPU |
|----------|-----|
| Pipeline ×4 | 12-15 |
| Inline engine ×1 | 16 |
| Valkey | 17 |

기준값: `/etc/aivax/ai_engine.env` ← `deploy/common/ai_engine.env`  
(Pipeline은 `/etc/aivax/pipeline.env` — 별도)

## ExecStart 요지

- `PYTHONPATH=<repo>:<repo>/core:<repo>/mirror`
- ExecStart: 설치 시 고른 **호스트 공용/기본 Python** (예: `/home1/aivax/aivax-venv`) — ai_engine 전용 venv 없음
- Pipeline URL unit에 고정 (`--server-url` / `--port 9099`)
- `--policy-api-timeout-ms 60000` · `--policy-api-failure-policy open`
- 레거시 SSLproxy `ai-engine.service` (`/tmp/sslproxy-ai-engine.sock`)와 **혼용 금지** (SSLproxy는 제품에서 사용하지 않음)

계정 캐시 기본: `/run/aivax/account_cache.sqlite` (`AIVAX_ACCOUNT_CACHE_PATH`).

## 메트릭 구분

Inline 관측은 **inline_engine** `aivax-metrics-api` `:9100`.  
Mirror UI용 `aivax-engine-metrics` `:9108` 과 다릅니다.

## 삭제

```bash
sudo bash deploy/inline/uninstall.sh
# 공통 env까지: REMOVE_ENV=1 (또는 REMOVE_COMMON=1) sudo -E bash deploy/inline/uninstall.sh
# mode.json 삭제: REMOVE_MODE=1 sudo -E bash deploy/inline/uninstall.sh
```

**다른 호스트로 옮길 때:** 레포 루트 [DEPLOY_TARGET_HOST.md](../../docs/DEPLOY_TARGET_HOST.md)  
(Outer가 `deploy/inline/install.sh`를 직접 호출).

관련: [MODE_INLINE_MIRROR.md](MODE_INLINE_MIRROR.md) · [DEPLOY_MIRROR_SHM_1G.md](DEPLOY_MIRROR_SHM_1G.md) · [MODULES.md](MODULES.md) · [VERSIONING.md](VERSIONING.md)
