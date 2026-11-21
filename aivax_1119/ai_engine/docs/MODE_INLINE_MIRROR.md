# Inline ↔ Mirror — 한 트리·모드 전환

**Date:** 2026-08-16  
**코드:** `/home1/aivax/ai_engine` 하나 (Inline ChatGate + Mirror SHM)  
**Install:** `deploy/inline/install.sh` · `deploy/mirror/install.sh`  
**사이트 루트:** `/home1/aivax` (고정)  
**코드 맵:** [MODULES.md](MODULES.md)

같은 호스트에서 **한 번에 한 모드만** 켠다. 공유 첨부 경로 `/run/aivax/attachments` 때문이다.

---

## 0. 서비스 전체 목록 (정본)

### 공유 (Inline · Mirror **둘 다 필수**)

| 서비스 | 역할 | install |
|--------|------|---------|
| `aivax-pipeline` | 판정 API `:9099` **필수** (ALLOW/BLOCK/MASK, 파일차단) | `pipeline/deploy/install.sh` |
| `aivax-valkey` | Pipeline 백엔드 | 위와 동일 |

### Inline 모드

| 서비스 | 역할 | install |
|--------|------|---------|
| `aivax-datapath` | OVS/DPDK/vhost · TPROXY 준비 (oneshot) | datapath / oneclick |
| `aivax-bypass-inline` | HW bypass → inline 릴레이 (마지막 bring-up) | bypass / oneclick |
| `aivax-inline` | Deep Path datapath (구현: Envoy+aivax_core) | `install_envoy_from_bundle.sh` |
| `aivax-engine-inline` | ChatGate PolicyEngine → `/run/aivax/policy.sock` | `ai_engine/deploy/inline/install.sh` |
| `aivax-registry` | Track B 공유 상태 (`registry.sock`) | `install_registry.sh` |
| `aivax-leaf-issuer-watch` | bump CA leaf/SDS (`post-cmd` → `aivax-inline`) | `install_leaf_issuer_watch.sh` |
| `aivax-flow-agent` | host_registry → OVS Deep divert | `install_flow_agent.sh` |
| `aivax-health-agent` | 헬스 FSM · bypass fail-open | `install_health_agent.sh` |
| `aivax-metrics-collector` | 관측 scrape → JSONL | `install_metrics.sh` |
| `aivax-metrics-api` | 관측 HTTP `:9100` | 위와 동일 |

기동 권장:  
`aivax-valkey` · `aivax-pipeline` → `aivax-engine-inline` (sock) → datapath/Envoy → flow/health → `bypass-inline`

고객 HA 끔 따라 설치: [`customer_install_ha_off.md`](../../inline_engine/aivax/docs/customer_install_ha_off.md).  
`INSTALL_FULL_STACK=1`로 판정을 숨기지 말 것.

검증:

```bash
systemctl is-active \
  aivax-valkey aivax-pipeline \
  aivax-datapath aivax-inline aivax-engine-inline \
  aivax-registry aivax-leaf-issuer-watch \
  aivax-flow-agent aivax-health-agent \
  aivax-metrics-collector aivax-metrics-api \
  aivax-bypass-inline
ls -l /run/aivax/policy.sock /run/aivax/registry.sock
```

### Mirror 모드

| 서비스 | 역할 | install |
|--------|------|---------|
| `aivax-mirror` | Suricata · POSIX SHM ring 생성 | mirror_engine / 사이트 Suricata unit |
| `aivax-engine-mirror` | SHM consumer · 동일 PolicyEngine (`BindsTo=aivax-mirror`) | `ai_engine/deploy/mirror/install.sh` |
| `aivax-engine-metrics` | Mirror UI metrics `:9108` (기본 on) | 위 install (`INSTALL_METRICS=0`으로 off) |
| `aivax-engine-lifecycle` | 첨부 tmpfs 수명주기 (기본 on) | 위 install (`INSTALL_LIFECYCLE=0`으로 off) |
| `aivax-pipeline` (+ `aivax-valkey`) | 정책/파일 판정 `:9099` **필수** (Inline과 동일) | pipeline install |

기동 권장:  
`aivax-valkey` · `aivax-pipeline` → `aivax-mirror` (SHM meta+segment) → `aivax-engine-mirror` → (선택) metrics/lifecycle  

`AIVAX_POLICY_API=1` + `AIVAX_POLICY_SERVER_URL=http://127.0.0.1:9099` 이 제품 기본·필수 (`deploy/common/ai_engine.env`).

검증:

```bash
systemctl is-active aivax-valkey aivax-pipeline \
  aivax-mirror aivax-engine-mirror \
  aivax-engine-metrics aivax-engine-lifecycle
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9099/docs
# Inline 엔진은 꺼져 있어야 함
systemctl is-active aivax-engine-inline || echo 'engine-inline stopped (ok)'
cat /etc/aivax/mode.json   # "mode":"mirror"
```

### 배타 (동시 기동 금지)

| Inline일 때 끔 | Mirror일 때 끔 |
|----------------|----------------|
| `aivax-engine-mirror` | `aivax-engine-inline` |
| `aivax-engine-metrics` | |
| `aivax-engine-lifecycle` | |

`aivax-inline`(datapath)과 `aivax-mirror`(Suricata)는 보통 **장비 역할이 갈라지므로** 한 호스트에 둘 다 상시 돌리지 않는다.  
엔진·lifecycle이 같은 `/run/aivax/attachments`를 쓰므로 **엔진 쪽은 반드시 한 모드만**.

### 이름 대응 (예전 → 현재)

| 예전 | 현재 |
|------|------|
| `aivax-envoy` | `aivax-inline` |
| `aivax-policy` | `aivax-engine-inline` |
| `aivax-engine-shm` | `aivax-engine-mirror` |

---

## 1. 모드란 (요약)

| 역할 | systemd (현재) | 예전 이름 | 용도 |
|------|----------------|-----------|------|
| Inline datapath | `aivax-inline` | ~~aivax-envoy~~ | Deep Path |
| Mirror datapath | `aivax-mirror` | — | Suricata SHM |
| Inline 엔진 | `aivax-engine-inline` | ~~aivax-policy~~ | ChatGate → `policy.sock` |
| Mirror 엔진 | `aivax-engine-mirror` | ~~aivax-engine-shm~~ | SHM consumer |
| Mirror 부가 | `aivax-engine-metrics` · `aivax-engine-lifecycle` | — | UI `:9108` · 첨부 수명 |

### “마이그레이션용 문자열”이란?

코드/install 스크립트 안에 `aivax-envoy`, `aivax-policy` 같은 **예전 이름**이 조금 남아 있을 수 있다.  
그건 평소에 그 이름으로 서비스를 돌리라는 뜻이 **아니다**.

- **목적:** 예전에 깔아 둔 `/etc/systemd/system/aivax-envoy.service` 등이 아직 있으면, install이 그걸 **찾아서 끄고 지운 다음** 새 이름(`aivax-inline` 등)으로 교체하기 위함.
- **신규 설치 / 일상 운영:** `aivax-inline`, `aivax-engine-inline`, `aivax-engine-mirror` **만** 사용.
- **사이트 경로:** 항상 `/home1/aivax/...` 아래 install.sh를 실행.

UDS `/run/aivax/policy.sock` 경로 문자열은 Envoy↔엔진 계약이라 **유지**한다 (서비스 이름과 무관).  
**SSLproxy는 사용하지 않는다.** 옛 `ai-engine.service` / `/tmp/sslproxy-ai-engine.sock` 이 남아 있으면 stop.

**반대 모드** = 지금 install 하려는 쪽의 다른 쪽.

| 지금 install | 반대 모드 (자동 stop 대상) |
|--------------|---------------------------|
| Mirror (`deploy/mirror/install.sh`) | Inline 엔진 → `aivax-engine-inline` (예전 unit `aivax-policy` 등이 있으면 제거) |
| Inline (`deploy/inline/install.sh`) | Mirror 엔진 → `aivax-engine-mirror` / metrics / lifecycle (예전 `aivax-engine-shm` 등이 있으면 제거) |

Datapath(`aivax-inline` / `aivax-mirror`)는 mode-switch가 건드리지 않는다.

---

## 1b. `/etc/aivax/mode.json` — 제품 모드 (management / Outer)

웹·관리 UI가 Inline vs Mirror를 알기 위한 **공용 계약 파일**.  
**writer = ai_engine install.sh** (정본).

**`/etc/aivax/aivax.env`와 혼동하지 말 것**

| | `mode.json` | `aivax.env` |
|--|-------------|-------------|
| 용도 | 제품 모드 표시 (`inline`/`mirror`) | Mirror Suricata/DPDK **사이트** 설정 |
| Inline | ✅ install이 씀 | ❌ **파일 없음이 정상** |
| Mirror | ✅ install이 씀 | ✅ 프로파일 예제 → 복사·PCI 편집 |
| 웹 | 이 파일(또는 API)만 | 모드 판단에 쓰지 않음 |

`aivax.env`에 임의로 `mode=…` 키를 넣지 않는다. Inline은 `datapath.*.env` + `/etc/aivax/aivax-*.env` 로 설정한다.

### 스키마 (`schema_version: 1`)

```json
{
  "schema_version": 1,
  "mode": "inline",
  "updated_at": "2026-08-09T10:00:00Z",
  "source": "ai_engine/deploy/inline/install.sh"
}
```

| 필드 | 값 |
|------|-----|
| `mode` | `"inline"` \| `"mirror"` \| `"unknown"` |
| `updated_at` | UTC ISO-8601 |
| `source` | 기록한 스크립트 경로 힌트 |
| 권한 | `0644` (`/etc/aivax/mode.json`) |

예제: `deploy/common/mode.json.example`

### 누가 쓰나

| 시점 | `mode` |
|------|--------|
| `deploy/inline/install.sh` 성공 | `inline` |
| `deploy/mirror/install.sh` 성공 | `mirror` |
| 해당 uninstall (기본) | `unknown` (반대 엔진이 아직 active면 유지) |
| `REMOVE_MODE=1` uninstall | 파일 **삭제** |
| `SKIP_MODE_JSON=1` | write 스킵 |

구현: `deploy/common/_lib.sh` → `_aivax_write_mode_json` (atomic: `mktemp` → `mv`).

### management / Outer

1. 백엔드가 `/etc/aivax/mode.json`을 읽어 `GET` API로 노출 (프론트는 호스트 파일 직접 접근 금지).  
2. 파일 없음 · JSON 깨짐 · 알 수 없는 `mode` → UI는 `unknown`.  
3. **교차검증(권장):**  
   - `mode=inline` ↔ `aivax-engine-inline` active  
   - `mode=mirror` ↔ `aivax-engine-mirror` active  
   불일치 시 “설정 불일치” 표시.

### 하지 말 것

- Inline에서 `aivax.env`가 없다고 설치 실패로 오인  
- `/etc/aivax/aivax.env`에만 모드 키 넣기 (Mirror 전용 · Inline 미커버)  
- 사람만 손으로 고치고 install이 안 갱신하는 구조  
- install 없이 Outer bake만으로 “완성” 주장 (파일이 없거나 stale일 수 있음)

---

## 2. 구조 (통합 트리)

```text
ai_engine/
  core/           # PolicyEngine + 공통 (ai_policy_engine, http_payload_normalizer,
                  # account_lookup, metrics_api, attachment_lifecycle, …)
  mirror/         # SHM consumer (shm_ring*.py) · ops_classify.py
  adapters/
  deploy/inline/  # aivax-engine-inline
  deploy/mirror/  # aivax-engine-mirror · metrics · lifecycle
  deploy/common/  # _lib.sh (mode-switch · mode.json), ai_engine.env, mode.json.example
  scripts/        # run_engine.sh, run_shm_consumer.sh (제품 Mirror ExecStart),
                  # aivax-ops, mount_attach_tmpfs.sh
  tests/
```

런타임 `PYTHONPATH` = `repo:core:mirror` (flat import 유지).

**주의:** 옛 Mirror 패키지의 flat 경로(`…/ai_engine/run_engine.sh`, `metrics_api.py` 루트)에 트리만 덮어쓰면 기동 실패한다.  
반드시 `deploy/mirror/install.sh`로 unit을 맞춘다. (구형 unit 이름 `aivax-shm-consumer` → 신규 `aivax-engine-mirror`)

---

## 3. install 시 자동 mode-switch

구현: `deploy/common/_lib.sh` → `_aivax_prepare_mode_switch`.

### 기본 동작 (플래그 없음)

1. **반대 모드 unit stop**
2. `attachment_lifecycle.py --reconcile-only`
3. `/run/aivax/attachments` **내용 wipe** (마운트·옵션은 유지)

순차 전환(Inline stop → Mirror, 또는 그 반대)이면 공유 tmpfs 충돌 없음.

### 환경 변수 (끌 때)

| 변수 | 의미 |
|------|------|
| *(없음)* | stop + reconcile + wipe (권장) |
| `SKIP_MODE_SWITCH=1` | mode-switch **전부 스킵** (반대 모드도 안 끔, 첨부도 안 지움). 같은 모드 재설치·이미 수동 정리한 경우 |
| `SKIP_ATTACH_CLEANUP=1` | 반대 모드 **stop만**. reconcile/wipe 안 함 |
| `SKIP_MODE_JSON=1` | `/etc/aivax/mode.json` 미기록 (§1b · 비권장) |

```bash
# 예: Mirror 재설치만, Inline 건드리지 않음
SKIP_MODE_SWITCH=1 sudo -E bash deploy/mirror/install.sh

# 예: Inline 끄고 Mirror 올리되 첨부 파일은 남김
SKIP_ATTACH_CLEANUP=1 sudo -E bash deploy/mirror/install.sh
```

---

## 4. Mirror unit 상세

| unit | 역할 | 기본 |
|------|------|------|
| `aivax-engine-mirror` | SHM consumer. `Requires`/`BindsTo`/`PartOf`=`aivax-mirror`. meta `ExecStartPre` ~45s 대기. **`ExecStart=scripts/run_shm_consumer.sh`만** (unit에 `${hp%%:*}` 금지). `Restart=on-failure` | 필수 |
| `aivax-engine-metrics` | Mirror UI metrics `127.0.0.1:9108` (`core/metrics_api.py`) | on (`INSTALL_METRICS=0`으로 off) |
| `aivax-engine-lifecycle` | 첨부 tmpfs 수명주기 (`core/attachment_lifecycle.py` + `scripts/mount_attach_tmpfs.sh`) | on (`INSTALL_LIFECYCLE=0`으로 off) |

- Suricata(`aivax-mirror`) 재시작 → consumer도 같이 내려갔다 다시 뜸 (`BindsTo`).
- env: `/etc/aivax/ai_engine.env` (Inline과 파일 공유, unit ExecStart는 모드별로 다름).

### metrics 구분

| | Inline 관측 | Mirror UI metrics |
|--|-------------|-------------------|
| unit | `aivax-metrics-collector` / `aivax-metrics-api` (inline_engine) | `aivax-engine-metrics` (ai_engine) |
| 포트 | `:9100` | `:9108` |

lifecycle은 엔진 본체 import 의존이 아니다. **동시 기동만** 피하면 된다.

---

## 5. Pipeline 판정 (Mirror · Inline과 동일하게 필수)

Mirror도 Inline처럼 **Pipeline `:9099`로 판정한다.**  
엔진 선차단은 비활성 → Pipeline `file_block_filter` 등에 위임.

`/etc/aivax/ai_engine.env` (deploy 기본값 · **끄지 말 것**):

```bash
AIVAX_POLICY_API=1
AIVAX_POLICY_SERVER_URL=http://127.0.0.1:9099
AIVAX_POLICY_API_TIMEOUT_MS=15000
```

```bash
systemctl is-active aivax-valkey aivax-pipeline
sudo systemctl restart aivax-engine-mirror
```

규칙: Pipeline `file_max_size` / `file_allow_ext` (또는 DB 파일제어).  

`AIVAX_POLICY_API=0`은 로컬 extract만 하고 확장자/크기 차단이 없다 → **랩/디버그 전용**. 출고·현장 제품 경로에서는 사용하지 않는다.

Inline `aivax-engine-inline`는 unit에 Pipeline URL이 이미 고정되어 있다. Mirror는 위 env로 **같은 `:9099`** 를 반드시 켠다.

---

## 6. 수동 전환 예시

install.sh를 다시 돌리면 위 정리가 포함된다. 수동이면:

```bash
# Inline → Mirror
sudo systemctl stop aivax-engine-inline
# (선택) reconcile + wipe 는 mirror install 이 수행
sudo bash /home1/aivax/ai_engine/deploy/mirror/install.sh
sudo systemctl start aivax-mirror   # 아직 아니면
sudo systemctl start aivax-engine-mirror

# Mirror → Inline
sudo bash /home1/aivax/ai_engine/deploy/inline/install.sh
# install 이 Mirror unit stop + reconcile/wipe 후 aivax-engine-inline 기동
```

---

## 7. 검증

```bash
# Mirror
systemctl is-active aivax-valkey aivax-pipeline aivax-mirror aivax-engine-mirror
systemctl is-active aivax-engine-metrics aivax-engine-lifecycle || true
pgrep -af shm_ring_consumer
systemctl cat aivax-engine-mirror | grep ExecStart   # run_shm_consumer.sh 만
systemctl is-active aivax-engine-inline || echo "engine-inline stopped (ok for Mirror)"
cat /etc/aivax/mode.json
# 선택: scripts/aivax-ops status

# Inline
systemctl is-active aivax-engine-inline aivax-pipeline aivax-valkey
ls -l /run/aivax/policy.sock
systemctl is-active aivax-engine-mirror || echo "shm stopped (ok for Inline)"
cat /etc/aivax/mode.json
```

---

## 관련 문서

- [MODULES.md](MODULES.md) — 코드 맵
- [DEPLOY_MIRROR_SHM_1G.md](DEPLOY_MIRROR_SHM_1G.md) — Mirror 설치·CPU·Policy API · `run_shm_consumer.sh`
- [DEPLOY_INLINE_1G.md](DEPLOY_INLINE_1G.md) — Inline 설치
- [INSTALLER_INLINE.md](INSTALLER_INLINE.md) — Inline 인스톨러 상세
- [ai_policy_engine.md](ai_policy_engine.md) — CLI·UDS·payload 정규화
- [FILE_UPLOAD_CAPACITY.md](FILE_UPLOAD_CAPACITY.md) — 업로드 한도·pipeline 위임
- [../../docs/DEPLOY_TARGET_HOST.md](../../docs/DEPLOY_TARGET_HOST.md) — 타겟 호스트 install 계약
