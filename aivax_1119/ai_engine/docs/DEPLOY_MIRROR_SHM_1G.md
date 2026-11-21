# Mirror 배포 — 1G SHM (`aivax-engine-mirror`)

**Date:** 2026-08-16  
**Install:** `sudo bash deploy/mirror/install.sh`  
**Uninstall:** `sudo bash deploy/mirror/uninstall.sh`  
**서비스 전체·모드 전환 정본:** [MODE_INLINE_MIRROR.md](MODE_INLINE_MIRROR.md) **§0**  
**코드 맵:** [MODULES.md](MODULES.md)

Mirror **기본** 경로는 POSIX SHM ring (`mirror/shm_ring_consumer.py`). Inline ChatGate UDS(`/run/aivax/policy.sock`)와 혼용하지 않습니다. SSLproxy 사용 안 함.  
코드는 Inline과 **동일 트리** (`/home1/aivax/ai_engine`). 옛 flat 경로 unit에 트리만 덮어쓰지 말고 이 install을 사용하세요.

**ExecStart 계약:** unit은 `scripts/run_shm_consumer.sh` **만** 호출합니다. Python argv / URL 분해를 unit 안에 넣지 마세요.

Mirror 풀스택: **aivax-pipeline** · **aivax-valkey** · **aivax-mirror** · **aivax-engine-mirror** · metrics(`:9108`) · lifecycle.  
판정은 Inline과 같이 Pipeline `:9099` **필수** (`AIVAX_POLICY_API=1`).

## 한 호스트 · Inline과 동시 금지

같은 `/run/aivax/attachments` tmpfs를 씁니다. **순차 전환만** (한 모드 stop → 다른 모드).

`deploy/mirror/install.sh` 기본 동작 (`_aivax_prepare_mode_switch mirror`):

1. **반대 모드** stop — Inline `aivax-engine-inline` (+ legacy `aivax-ai-engine`)
2. `attachment_lifecycle --reconcile-only`
3. `/run/aivax/attachments` **내용 wipe** (마운트 유지)

| 변수 | 의미 |
|------|------|
| *(기본)* | stop + reconcile + wipe |
| `SKIP_MODE_SWITCH=1` | mode-switch 전부 스킵 |
| `SKIP_ATTACH_CLEANUP=1` | 반대 모드 stop만 (reconcile/wipe 안 함) |
| `SKIP_MODE_JSON=1` | `/etc/aivax/mode.json` 미기록 (비권장) |

상세·용어(반대 모드): [MODE_INLINE_MIRROR.md](MODE_INLINE_MIRROR.md).

## 설치

```bash
sudo bash /home1/aivax/ai_engine/deploy/mirror/install.sh
# → /etc/aivax/mode.json 에 mode=mirror 기록 (management UI)
```

설치되는 unit:
| unit | 역할 |
|------|------|
| `aivax-engine-mirror` | SHM consumer (필수). `BindsTo=aivax-mirror` + meta `ExecStartPre` ~45s. **`ExecStart=scripts/run_shm_consumer.sh`**. `Restart=on-failure` |
| `aivax-engine-metrics` | Mirror UI metrics `:9108` (`core/metrics_api.py`) — Inline `:9100` 과 **별개**. 웹 스펙: [METRICS_API.md](METRICS_API.md) |
| `aivax-engine-lifecycle` | 첨부 tmpfs 수명주기 (`core/attachment_lifecycle.py`) |

옵션:

```bash
INSTALL_METRICS=0 INSTALL_LIFECYCLE=0 sudo -E bash deploy/mirror/install.sh
SKIP_ENV=1 sudo -E bash deploy/mirror/install.sh   # ai_engine.env 유지
```

`install.sh` 기본은 `deploy/common/ai_engine.env` 로 **덮어쓴다** (`SHM_WORKERS=8`, CPU `18–23`).  
상암 16워커 맵을 쓰려면 **`SKIP_ENV=1` 후** [`sangam-khfc-wipe-reinstall.md`](../../mirror_engine/docs/sangam-khfc-wipe-reinstall.md) §6.4 값을 넣는다. 기본 CPU **38** 도 상암 SHM HT와 겹친다 → `90-sangam-cpu.conf`.

기동 조건:

- `aivax-mirror` active + meta + `/dev/shm/<name>` → SHM **start**
- 아니면 **enable만**. 이후 `systemctl start aivax-engine-mirror` (unit이 meta 대기)

Suricata 재시작 시 `BindsTo`로 consumer도 같이 내려갔다 다시 뜹니다.

## 사전 조건

```bash
systemctl is-active aivax-mirror
ls -l /tmp/aivax_eve_ring.meta.json    # 또는 $AIVAX_SHM_META
ls -l /dev/shm/aivax_eve_ring
```

## Pipeline 판정 (필수 · Inline과 동일)

엔진 선차단 없음 → Pipeline `:9099` (`file_block_filter` 등).  
출고/현장에서 `AIVAX_POLICY_API`를 끄지 않는다.

`/etc/aivax/ai_engine.env` (deploy 기본):

```bash
AIVAX_POLICY_API=1
AIVAX_POLICY_SERVER_URL=http://127.0.0.1:9099
AIVAX_POLICY_API_TIMEOUT_MS=15000
```

```bash
# Pipeline 먼저
systemctl is-active aivax-valkey aivax-pipeline
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9099/docs
sudo systemctl restart aivax-engine-mirror
```

규칙: Pipeline `file_max_size` / `file_allow_ext` (또는 DB 파일제어).  
`AIVAX_POLICY_API=0`(로컬 extract만)은 랩/디버그 전용 — 제품 경로 금지.

## CPU (이 호스트 1G · **Inline 랩 맵**)

| 컴포넌트 | CPU |
|----------|-----|
| SHM workers ×8 | 18-23,36-37 (`AIVAX_SHM_CPU_LIST`) |
| SHM supervisor | 38 |

**상암 1G (`PoC_HF_01_Sangam-1G`):** 워커 **16** · `12–23,36–39` · 부모 **27**.  
이 표의 8/38을 그대로 쓰지 말 것.

Worker 수는 Suricata `n_rings`를 넘지 않음 (자동 clamp).

## 검증

```bash
systemctl is-active aivax-valkey aivax-pipeline \
  aivax-engine-mirror aivax-engine-metrics aivax-engine-lifecycle
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9099/docs
pgrep -af shm_ring_consumer
systemctl is-active aivax-engine-inline || echo "engine-inline stopped (ok)"
cat /etc/aivax/mode.json   # "mode":"mirror"
# 선택
/home1/aivax/ai_engine/scripts/aivax-ops status
```

제품 모드: [MODE_INLINE_MIRROR.md](MODE_INLINE_MIRROR.md) **§1b**.

## 트러블슈팅: `unrecognized arguments:` (빈 인자)

`ai_engine_shm.log`에 `shm_ring_consumer.py: error: unrecognized arguments:` 가 반복되면,  
옛 unit이 `ExecStart` 안에 `${hp%%:*}` 를 직접 넣은 경우입니다. systemd가 `%%`→`%`로 바꿔 argv가 깨집니다.

**수정:** unit `ExecStart`는 `scripts/run_shm_consumer.sh` 만 호출해야 합니다 (이 트리 `deploy/mirror/install.sh`가 그렇게 설치).

```bash
# 코드 동기화 후
SKIP_ENV=1 sudo -E bash /home1/aivax/ai_engine/deploy/mirror/install.sh
sudo systemctl restart aivax-engine-mirror
# 확인: ExecStart에 bash -c / %% 가 없어야 함
systemctl cat aivax-engine-mirror | grep ExecStart
```

## 삭제

```bash
sudo bash deploy/mirror/uninstall.sh
# ai_engine.env 까지: REMOVE_ENV=1 sudo -E bash deploy/mirror/uninstall.sh
# mode.json 삭제: REMOVE_MODE=1 sudo -E bash deploy/mirror/uninstall.sh
```

관련: [MODE_INLINE_MIRROR.md](MODE_INLINE_MIRROR.md) · [DEPLOY_INLINE_1G.md](DEPLOY_INLINE_1G.md) · [MODULES.md](MODULES.md)
