# docs/

**Date:** 2026-08-17

| 문서 | 내용 |
|------|------|
| [MODE_INLINE_MIRROR.md](MODE_INLINE_MIRROR.md) | **Inline↔Mirror 정본** — §0 서비스 목록 · `mode.json` · SKIP · 파일차단 |
| [MODULES.md](MODULES.md) | **코드 맵** — core/mirror/adapters/deploy/scripts/tests · env |
| [DEPLOY_INLINE_1G.md](DEPLOY_INLINE_1G.md) | ChatGate Inline 설치 요약 |
| [DEPLOY_MIRROR_SHM_1G.md](DEPLOY_MIRROR_SHM_1G.md) | Mirror SHM 설치 · `run_shm_consumer.sh` · systemd `%%` |
| pack | [`../scripts/pack.sh`](../scripts/pack.sh) → `/home1/aivax/ai_engine` 로 풀기 |
| 상암 재설치 | [`../../mirror_engine/docs/sangam-khfc-wipe-reinstall.md`](../../mirror_engine/docs/sangam-khfc-wipe-reinstall.md) — `/home1/aivax` wipe · `SKIP_ENV=1` |
| [INSTALLER_INLINE.md](INSTALLER_INLINE.md) | Inline 인스톨러 쉘 호출 상세 |
| [customer_install_ha_off.md](../../inline_engine/aivax/docs/customer_install_ha_off.md) | **고객 HA 끔** — Pipeline 다음 이 트리 `deploy/inline/install.sh` |
| [ai_policy_engine.md](ai_policy_engine.md) | 엔진 CLI·UDS 프로토콜 · payload 정규화 · 계정 캐시 |
| [BLOCK_COPY.md](BLOCK_COPY.md) | 차단·마스킹 문구. 파이프라인과 엔진이 `config/block_phrases.json`을 같이 씀 |
| [VERSIONING.md](VERSIONING.md) | 서비스별 커밋·롤백 |
| [FILE_UPLOAD_CAPACITY.md](FILE_UPLOAD_CAPACITY.md) | 업로드 동시성·개수·크기 한도 |
| [FILE_UPLOAD_DEEP_PATH.md](FILE_UPLOAD_DEEP_PATH.md) | Inline Deep · S3/`deep_tls_ports` · Grok promote |
| [METRICS_API.md](METRICS_API.md) | **웹용** Mirror metrics `:9108` — 엔드포인트·필드·폴링 · [`schemas/metrics-api.schema.json`](../schemas/metrics-api.schema.json) |

Outer 타겟 호스트 계약: [../../docs/DEPLOY_TARGET_HOST.md](../../docs/DEPLOY_TARGET_HOST.md)
