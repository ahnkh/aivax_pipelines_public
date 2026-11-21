# ai_engine 모듈 맵

**Date:** 2026-08-16  
**트리:** `/home1/aivax/ai_engine` (이 저장소)  
**런타임 `PYTHONPATH`:** `<repo>:<repo>/core:<repo>/mirror` (flat import)

Inline(ChatGate UDS)과 Mirror(SHM)가 **같은 PolicyEngine**을 씁니다.  
SSLproxy는 제품에서 사용하지 않습니다.

관련: [MODE_INLINE_MIRROR.md](MODE_INLINE_MIRROR.md) · [ai_policy_engine.md](ai_policy_engine.md) · [DEPLOY_MIRROR_SHM_1G.md](DEPLOY_MIRROR_SHM_1G.md)

---

## 1. 데이터 경로

```text
Inline
  Envoy aivax_core  --UDS 4B+JSON-->  core/ai_policy_engine.py
                                    --HTTP-->  Pipeline :9099

Mirror
  Suricata plugin  --POSIX SHM-->  mirror/shm_ring_consumer.py
                   (drain 루프 ≠ policy HTTP)
                                    -- 동일 PolicyEngine -->  Pipeline :9099
```

같은 호스트에서 **한 모드만**. 공유 `/run/aivax/attachments`.

---

## 2. 폴더

```text
ai_engine/
  config/         block_phrases.json  차단 문구
  core/           PolicyEngine + 공통
  mirror/         SHM consumer + ops 분류
  adapters/       서비스별 추출/rewrite (롤백 단위)
  deploy/         systemd install (inline | mirror | common)
  scripts/        로컬 실행 · SHM 래퍼 · ops
  tests/          회귀
  docs/           이 문서들
  requirements.txt
```

---

## 3. `core/`

| 파일 | 역할 |
|------|------|
| `ai_policy_engine.py` | 엔트리. UDS 프레임 수신, 어댑터 extract, Pipeline 판정, ALLOW/MASK/BLOCK/REWRITE |
| `block_copy.py` | 차단 카드, 모델 지시문, Gemini/Grok 복원 표식. 문구는 `config/block_phrases.json`. [BLOCK_COPY.md](BLOCK_COPY.md) |
| `http_payload_normalizer.py` | Inline·Mirror 공통. HTTP 헤드 제거 / chunked 디코드 / keep-alive 다음 요청 절단. **애매하면 손대지 않음** (suspect만 표시). multipart·바이너리 Content-Type은 스킵 |
| `account_lookup.py` | 계정 캐시 (shard LRU+TTL, confidence grade). 기본 SQLite `/run/aivax/account_cache.sqlite` — 재시작·SHM 워커 간 공유. `AIVAX_ACCOUNT_CACHE_PATH=` 빈 값이면 디스크 미러 끔 |
| `file_upload_manager.py` | 업로드 tmpfs 저장 · chat 시점 `attachments[].id` = 로컬 절대경로 |
| `attachment_lifecycle.py` | 첨부 tmpfs reconcile/wipe (mode-switch가 호출) |
| `adapter_registry.py` | `{service}_engine.py` 동적 로딩 |
| `ai_service_codes.py` | 서비스 코드 매핑 |
| `url_normalize.py` | URL 정규화 |
| `metrics_api.py` | Mirror UI metrics `:9108` (`aivax-engine-metrics`) — DPDK port + pipeline JSON. 스펙: [METRICS_API.md](METRICS_API.md) · 스키마: [`../schemas/metrics-api.schema.json`](../schemas/metrics-api.schema.json) |
| `meta_wal.py` | 메타 WAL |

계정 grade: `explicit` | `session_correlated` | `ip_predicted` | `anonymous` | `ambiguous`.  
동시 기록 시 빈 값으로 기존 이메일을 지우지 않음 (`prefer_identity`).

---

## 4. `mirror/`

| 파일 | 역할 |
|------|------|
| `shm_ring.py` | POSIX SHM ring 바인딩 (Suricata 플러그인이 생성) |
| `shm_ring_consumer.py` | 워커 프로세스 풀. **drain 루프는 policy HTTP를 await하지 않음** |
| `ops_classify.py` | ops 모니터용 host/HTTP 버전 분류 (정책 라우팅 아님) |

제품 ExecStart는 **Python을 unit에 직접 넣지 않습니다.**

```text
aivax-engine-mirror
  ExecStart = scripts/run_shm_consumer.sh
            → shm_ring_consumer.py
```

이유: systemd가 unit 안의 `${hp%%:*}` 의 `%%`를 `%`로 바꿔 argv가 깨짐 (`unrecognized arguments:`).  
URL host/port 분할은 래퍼 스크립트에서만 수행. [DEPLOY_MIRROR_SHM_1G.md](DEPLOY_MIRROR_SHM_1G.md) 트러블슈팅.

워커 기본(1G): `--workers 8` · `--cpu-list 18,19,20,21,22,23,36,37` · supervisor CPU 38.  
`n_rings`를 넘으면 clamp.

---

## 5. `adapters/`

파일 규칙: `{service}_engine.py` · 업로드는 `{service}_upload.py`.

| 엔진 | 업로드 헬퍼 |
|------|-------------|
| `chatgpt_engine.py` (`service_id=gpt` → chatgpt) | `chatgpt_upload.py` |
| `gemini_engine.py` | `gemini_upload.py` |
| `claude_engine.py` | `claude_upload.py` |
| `grok_engine.py` | `grok_upload.py` |
| `perplexity_engine.py` | `perplexity_upload.py` |
| `notion_engine.py` | `notion_upload.py` |
| `wrks_engine.py` | `wrks_upload.py` |
| `github_copilot_engine.py` | `github_copilot_upload.py` |
| `genspark_engine.py` | — |
| `chat_version_engine.py` | — |
| `adapter_template.py` | 템플릿 |

핫 리로드 기본 ON. Mirror 제품 경로는 `--adapters-no-reload` (래퍼가 넣음).

---

## 6. `deploy/`

| 경로 | 역할 |
|------|------|
| `common/_lib.sh` | Python resolve, unit render, mode-switch, **`_aivax_write_mode_json`** |
| `common/ai_engine.env` | → `/etc/aivax/ai_engine.env` |
| `common/mode.json.example` | `/etc/aivax/mode.json` 스키마 예 |
| `inline/install.sh` | `aivax-engine-inline` + `mode=inline` |
| `mirror/install.sh` | `aivax-engine-mirror` (+ metrics/lifecycle) + `mode=mirror` · `chmod +x scripts/run_shm_consumer.sh` |
| `*/uninstall.sh` | unit 제거. 기본 `mode=unknown`. `REMOVE_MODE=1` 이면 파일 삭제. `REMOVE_ENV=1` 이면 env 삭제 |

---

## 7. `scripts/`

| 파일 | 역할 |
|------|------|
| `run_engine.sh` | 로컬 디버그. `-m default` ChatGate UDS · `-m suricata` eve · `-m shm` consumer |
| `run_shm_consumer.sh` | **제품 Mirror ExecStart** |
| `mount_attach_tmpfs.sh` | 첨부 tmpfs |
| `aivax-ops` | ops 모니터 래퍼 (`status` / `live` / `json`) |
| `aivax_ops_monitor.py` | SHM/export stats 읽기 전용 |
| `vm_shm_stub.py` | 랩 SHM stub |
| `run_ens4f0_shm.sh` | 랩 NIC 헬퍼 |
| `pack.sh` | 폐쇄망 이송 tar (`dist/ai-engine-x86_64.tar.gz`) |

`run_engine.sh` 기본 소켓: `/run/aivax/policy.sock`.  
`ai_policy_engine.py --socket` **argparse 기본값은 레거시** `/tmp/sslproxy-ai-engine.sock` — 제품 unit과 래퍼는 항상 `/run/aivax/policy.sock`을 명시합니다. 직접 python 호출 시 `--socket`을 빼지 마세요.

---

## 8. `tests/`

```bash
cd /home1/aivax/ai_engine
PYTHONPATH=.:core:mirror python3 tests/test_ops_classify.py
PYTHONPATH=.:core:mirror python3 tests/test_account_lookup_concurrency.py
PYTHONPATH=.:core:mirror python3 tests/test_extraction_regression.py
```

| 테스트 | 대상 |
|--------|------|
| `test_extraction_regression.py` | payload normalizer · 추출 회귀 |
| `test_account_lookup_concurrency.py` | 계정 캐시 동시성 · 이메일 충돌 |
| `test_ops_classify.py` | host/HTTP 분류 |
| `test_grok_upload.py` / `test_wrks_*.py` | 업로드 어댑터 |

---

## 9. 환경 변수 (엔진이 읽는 것)

제품 파일: `/etc/aivax/ai_engine.env` ← `deploy/common/ai_engine.env`  
Pipeline 서버 설정은 `pipeline/deploy` 소유 (`pipeline.env`). 여기서 덮어쓰지 않음.

| 변수 | 기본 | 의미 |
|------|------|------|
| `AIVAX_ENGINE_ROOT` | `/home1/aivax/ai_engine` | 트리 |
| `AIVAX_POLICY_SOCKET` | `/run/aivax/policy.sock` | Inline UDS |
| `AIVAX_PIPELINE_URL` | `http://127.0.0.1:9099` | 문서/설치용 |
| `AIVAX_CPU_AI_ENGINE` | `16` | Inline pin |
| `AIVAX_SHM_WORKERS` | `8` | Mirror 워커 |
| `AIVAX_SHM_CPU_LIST` | `18,19,20,21,22,23,36,37` | 워커 CPU |
| `AIVAX_SHM_NAME` | `aivax_eve_ring` | POSIX shm 이름 |
| `AIVAX_SHM_META` | `/tmp/aivax_eve_ring.meta.json` | meta JSON |
| `AIVAX_POLICY_API` | `1` | Mirror가 Pipeline 호출. **제품은 1 유지** |
| `AIVAX_POLICY_SERVER_URL` | `http://127.0.0.1:9099` | Mirror 래퍼가 host/port로 분해 |
| `AIVAX_POLICY_API_TIMEOUT_MS` | `15000` | Mirror 래퍼 타임아웃 (Inline unit은 60000) |
| `AIVAX_ACCOUNT_CACHE_PATH` | `/run/aivax/account_cache.sqlite` | 빈 문자열이면 SQLite 끔 |
| `AIVAX_ATTACH_TMPFS` | `/run/aivax/attachments` | 첨부 |

Inline unit은 Pipeline URL을 ExecStart에 고정 (`--server-url http://127.0.0.1 --port 9099`).  
Mirror는 `AIVAX_POLICY_API=1`일 때 래퍼가 같은 `:9099`를 붙입니다.
