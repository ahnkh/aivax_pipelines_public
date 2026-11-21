# AIVAX ai_engine

독립 git 저장소. Inline(ChatGate)과 Mirror(SHM)를 **한 트리**에서 각각 한 줄로 설치합니다.  
같은 호스트에서는 **한 모드만** (공유 `/run/aivax/attachments`). 전환·SKIP: [docs/MODE_INLINE_MIRROR.md](docs/MODE_INLINE_MIRROR.md).  
코드 맵: [docs/MODULES.md](docs/MODULES.md).

SSLproxy는 사용하지 않습니다.

## pack (폐쇄망 이송)

```bash
./scripts/pack.sh            # dist/ai-engine-x86_64.tar.gz
# 현장: cd /home1/aivax && tar -xzf … && cd ai_engine && sudo SKIP_ENV=1 bash deploy/mirror/install.sh
```

Mirror Suricata 바이너리 pack은 `mirror_engine/build/build.sh pack` (이 스크립트와 별개). Pipeline은 `aivax/pipeline/scripts/pack.sh`.

## 빠른 설치

```bash
# Inline 고객 · HA 끔 — Pipeline 먼저, 그다음 이 한 줄
# 상세: inline_engine/aivax/docs/customer_install_ha_off.md
sudo bash /home1/aivax/aivax/pipeline/deploy/install.sh   # 또는 LIVE
sudo bash deploy/inline/install.sh
# 상세: docs/INSTALLER_INLINE.md
# 미리보기: DRY_RUN=1 sudo -E bash deploy/inline/install.sh

# Mirror — Suricata SHM (+ metrics :9108, lifecycle)
# ExecStart = scripts/run_shm_consumer.sh (unit에 bash ${hp%%:*} 넣지 말 것)
# → /etc/aivax/mode.json 에 mode=mirror
sudo bash deploy/mirror/install.sh
# 상세: docs/DEPLOY_MIRROR_SHM_1G.md · docs/MODE_INLINE_MIRROR.md
```

삭제:

```bash
sudo bash deploy/inline/uninstall.sh
sudo bash deploy/mirror/uninstall.sh
# Pipeline/Valkey CPU drop-in은 pipeline uninstall 사용.
# ai_engine.env 까지: REMOVE_ENV=1 sudo -E bash deploy/inline/uninstall.sh
# mode.json 삭제: REMOVE_MODE=1 sudo -E bash deploy/inline/uninstall.sh
```

## 폴더 맵

| 경로 | 역할 |
|------|------|
| `core/` | PolicyEngine + 공통 (`ai_policy_engine.py`, `http_payload_normalizer.py`, `account_lookup.py`, …) |
| `mirror/` | SHM consumer (`shm_ring_consumer.py`) · `ops_classify.py` |
| `adapters/` | 서비스별 어댑터 (롤백 단위) |
| `deploy/inline/` | `aivax-engine-inline` systemd + `install.sh` |
| `deploy/mirror/` | Mirror SHM + metrics/lifecycle systemd + `install.sh` |
| `deploy/common/` | `ai_engine.env` · `_lib.sh` (mode-switch · `mode.json`) |
| `scripts/` | `run_engine.sh` · **`run_shm_consumer.sh` (제품 Mirror)** · `aivax-ops` · `mount_attach_tmpfs.sh` |
| `tests/` | 회귀 (`test_extraction_regression.py` 등) |
| `docs/` | 배포·모듈 문서 |

런타임 `PYTHONPATH` = `repo:core:mirror` (flat import 유지).

**Python:** ai_engine 전용 venv를 **만들지 않습니다**.  
사이트 고정 루트 **`/home1/aivax`** 의 공용 env를 씁니다  
(`/home1/aivax/aivax-venv`, 없으면 `PATH`의 `python3`).  
트리는 `/home1/aivax/ai_engine` 에 둔다.

## 문서

- [docs/MODE_INLINE_MIRROR.md](docs/MODE_INLINE_MIRROR.md) — **Inline↔Mirror 전환 정본** (서비스 목록 · `mode.json` · SKIP)
- [docs/MODULES.md](docs/MODULES.md) — **코드 맵** (모듈·env·테스트)
- [docs/INSTALLER_INLINE.md](docs/INSTALLER_INLINE.md) — Inline 인스톨러 쉘 호출 상세
- [docs/DEPLOY_INLINE_1G.md](docs/DEPLOY_INLINE_1G.md) — Inline 빠른 설치
- [docs/DEPLOY_MIRROR_SHM_1G.md](docs/DEPLOY_MIRROR_SHM_1G.md) — Mirror SHM 설치
- [../docs/DEPLOY_TARGET_HOST.md](../docs/DEPLOY_TARGET_HOST.md) — Outer가 호출할 컴포넌트별 install 계약
- [docs/VERSIONING.md](docs/VERSIONING.md)
- [docs/FILE_UPLOAD_CAPACITY.md](docs/FILE_UPLOAD_CAPACITY.md) — 용량·한도
- [docs/FILE_UPLOAD_DEEP_PATH.md](docs/FILE_UPLOAD_DEEP_PATH.md) — Inline Deep 첨부 (S3 TLS · Grok promote)
- [docs/ai_policy_engine.md](docs/ai_policy_engine.md) — 엔진 CLI·UDS 프로토콜
