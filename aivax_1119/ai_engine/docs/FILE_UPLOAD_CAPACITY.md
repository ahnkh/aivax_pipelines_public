# 파일 업로드 용량·동시성·설정 가이드

AIVAX가 다수 사용자·다수 파일 업로드를 어떻게 처리하는지, **실제로 몇 명/몇 개까지 버틸 수 있는지**, **어디를 설정으로 바꿀 수 있는지**를 정리한다.

> 기준일: 2026-08-16 (코드 상수·Envoy 설정·Management UI 스키마 기준)

---

## 1. 한눈에 보는 답

| 질문 | 답 (요약) |
|------|-----------|
| 동시에 몇 명이 업로드 가능한가? | 업로드 자체는 ChatGate에 **붙잡지 않음**. 실질 상한은 NIC/tmpfs/레지스트리(약 **5,000 upload_id**)·스코프 큐(**2,000**). 채팅 정책 판정과는 별개. |
| 한 사용자가 몇 개까지 올릴 수 있는가? | 인라인: 스코프당 pending **최대 64개**. Chat 앱: 기본 **파일 10개**/턴 (`fileLimit`). Pipeline “동시 10개”는 **TODO(미구현)**. |
| 파일 크기 상한은? | 정책(파일 제어) 기본 **10MB**, Management UI **10~20MB**, 엔진 clamp 상한 **50MB**. Envoy tee 상한 **64MB**/요청. Chat 앱 기본 업로드 한도는 **512MB**(LibreChat 설정). |
| 설정으로 바꿀 수 있나? | **가능**: 크기·확장자(Management), Envoy `max_tee_bytes`, TTL/tmpfs 경로(env), Chat rate limit/fileConfig. **코드 상수(재배포 필요)**: `_MAP_LIMIT` 등 레지스트리 상한. |

---

## 2. 업로드 경로 두 갈래

```
┌─────────────────────────────────────────────────────────────┐
│ A. 인라인 AI 가로채기 (주 경로)                              │
│  Client → Envoy(즉시 Continue + tee) → AI 서비스            │
│              └─ UDS → ai_engine(tmpfs) → pipeline(정책)     │
│  ChatGPT / Claude / Gemini / Grok / Perplexity 등           │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ B. LibreChat 앱 (`aivax/apps/chat`)                         │
│  Client → POST /api/files (파일당 1회, 순차)                 │
│         → multer → S3/Local + IP/User rate limit            │
└─────────────────────────────────────────────────────────────┘
```

용량·동시성 숫자는 **경로마다 다르다**. 아래 3절은 인라인, 4절은 Chat 앱.

엔진은 `core/http_payload_normalizer.py`로 HTTP 프레이밍만 정리한다. **multipart/바이너리 업로드 바디는 정규화하지 않는다.**

---

## 3. 인라인 경로 — 동시성·개수·크기

### 3.1 처리 모델 (왜 “동시 N명”이 고정이 아닌가)

대용량 업로드를 ChatGate에 붙잡으면 UX가 깨지고 `policy_timeout`이 난다. 그래서 Envoy는 업로드 경로에서 **upstream을 즉시 Continue** 하고 body만 tee한다.

```
업로드 PUT/POST
  → Envoy: Continue (사용자·AI 서비스 간은 바로 흐름)
  → AsyncDiskTee / request buffer (max_tee_bytes까지)
  → end_stream 후 policy UDS → tmpfs 저장 → (채팅 턴에서) pipeline 검사
```

즉 **“동시 업로드 슬롯 = N”** 같은 하드 한도는 없다. 대신 아래 soft limit들이 실질 용량을 결정한다.

### 3.2 레지스트리·큐 상한 (`file_upload_manager.py`)

| 상수 | 기본값 | 의미 | 설정 가능? |
|------|--------|------|------------|
| `_MAP_LIMIT` | **5000** | 전역 `upload_id` 맵 / request pending 맵 상한. 초과 시 오래된 항목 eviction + tmpfs unlink | ❌ 코드 상수 |
| `_SCOPE_PENDING_LIMIT` | **2000** | 스코프(세션/사용자 단위 키)별 pending 큐 엔트리 수 상한 | ❌ 코드 상수 |
| `_MAX_IDS_PER_SCOPE` | **64** | **한 스코프에 동시에 기억할 수 있는 upload_id 최대 개수** | ❌ 코드 상수 |
| `_PENDING_TTL` | **600초** | pending/맵 TTL (`AIVAX_UPLOAD_TTL_SECONDS`) | ✅ env |
| `_UPLOAD_SAVE_DIR` | `/run/aivax/attachments` | ephemeral tmpfs 경로 (`AIVAX_ATTACH_TMPFS`) | ✅ env |

**해석 예시**

- 전역적으로 대략 **최대 ~5,000개**의 활성 upload 매핑을 메모리에 유지한다 (TTL 600초 또는 limit eviction).
- 한 사용자가 짧은 시간에 파일을 폭풍처럼 올리면, 스코프당 **64개**를 넘는 pending ID는 잘린다 (`merged[-64:]`).
- 병렬 업로드 시 `claim_pending_uploads_for_scope(count)`로 **앞에서 N개만** 꺼내 응답 1건이 큐 전체를 비우지 않게 한다.

### 3.3 서비스별 보조 맵 상한 (어댑터)

| 서비스 | 상수 | 값 | 용도 |
|--------|------|-----|------|
| ChatGPT | `_UPLOAD_INIT_MAP_LIMIT` | 2048 | init filename ↔ request 매핑 |
| ChatGPT | `_UPLOAD_INIT_PENDING_LIMIT` | 64 | init 응답 유실 시 FIFO filename |
| Grok | `_V2_INIT_MAP_LIMIT` | 2048 | v2 init filename/size 매핑 |
| Grok | `_GROK_FILE_METADATA_LIMIT` | 512 | fileMetadataId 메타 LRU |
| Claude | `_CLAUDE_FILE_UUID_LIMIT` | 512 | file_uuid 메타 LRU |
| Perplexity | `_PENDING_BATCH_LIMIT` | 256 | batch_create client_id 맵 |
| Perplexity | `_PENDING_BATCH_FIFO_LIMIT` | 128 | FIFO fallback |
| Perplexity | `_PPLX_FILE_META_LIMIT` | 512 | file_uuid 메타 LRU |

모두 **코드 상수**(재배포 필요).

### 3.4 Envoy tee / 타임아웃

| 항목 | 기본값 | 위치 | 설정 가능? |
|------|--------|------|------------|
| `max_tee_bytes` | **64 MiB** (`67108864`) | `envoy_ingress_*.yaml` / proto | ✅ YAML |
| `kDefaultMaxTeeBytes` | 64 MiB | `aivax_filter.cc` (proto 0일 때) | 코드 기본값 |
| `spool_dir` | `/var/lib/aivax/spool` | Envoy YAML | ✅ YAML |
| `policy_timeout_ms` | **20000** | Envoy YAML (채팅 ChatGate용; 업로드는 hold 안 함) | ✅ YAML |
| Registry op queue | **단일 inflight**/필터 인스턴스 | `enqueueRegistryOp` / `pumpRegistryQueue` | ❌ 구조 고정 |

`max_tee_bytes`를 넘기면 **tee만 skip**하고 upstream 업로드는 계속된다 (정책 쪽은 해당 파일을 못 볼 수 있음).

### 3.5 파일 크기·확장자 정책 (차단 기준)

| 계층 | 기본 | 설정 |
|------|------|------|
| Management UI 「파일 제어」 | 최소 **10MB**, 최대 **20MB** | `FILE_CONTROL_MIN/MAX_SIZE_MB` (프론트 스키마) |
| ai_engine clamp | 기본 **10MB**, 절대 상한 **50MB** | `_DEFAULT_FILE_CONTROL_MAXSIZE` / `_MAX_FILE_CONTROL_MAXSIZE` (코드). 정책값이 50MB 초과면 clamp |
| Pipeline `config.json` | `file_max_size`: **10485760** (10MB), `file_allow_ext`: 문서·이미지 목록 | `aivax/pipeline/local_resource/config.json` + DB file-control |
| ai_engine 저장 게이트 | 확장자/크기 차단은 **의도적으로 비활성** → pipeline에 위임 | `_file_policy_decision` 주석 참고 |

**Mirror SHM:** Inline과 같이 Pipeline `:9099` 판정 **필수**.  
`/etc/aivax/ai_engine.env` 의 `AIVAX_POLICY_API=1` + `AIVAX_POLICY_SERVER_URL=http://127.0.0.1:9099` (deploy 기본) 유지 후 `aivax-engine-mirror` restart.  
`AIVAX_POLICY_API=0`(로컬 extract만)은 랩/디버그 전용 — 제품 경로 금지.Inline↔Mirror 전환·동시 기동 금지: [MODE_INLINE_MIRROR.md](MODE_INLINE_MIRROR.md).

**운영에서 바꾸는 곳 (권장)**  
Management → 환경 설정 → **파일 제어 설정** (`file-control`): `maxSize`, `allowedExtensions`.  
이 값이 policy snapshot으로 엔진/파이프라인에 전달된다.

> UI는 20MB까지지만 엔진 코드는 50MB까지 clamp한다. UI를 올리지 않으면 운영자가 20MB 초과를 UI에서 넣을 수 없다.

### 3.6 Pipeline 「동시 파일 10개」

`file_block_filter_pattern.py` 주석에 *「동시 파일 개수: 최대 10개」* 가 있으나, `DetectPattern`은 전달된 attachment를 **전부 순회**한다.  
**하드 cap은 현재 미구현(TODO)** 이다. 실질 개수 제한은 상위(스코프 64, Chat `fileLimit` 10)에 가깝다.

### 3.7 채팅 정책 동시성과의 관계 (참고)

업로드는 hold하지 않지만, **파일이 붙은 채팅 턴**은 여전히 ChatGate → policy UDS → Pipeline을 탄다.

Lab Phase B (합성 HTTPS ChatGate, M corpus):

| 지표 | 값 |
|------|-----|
| MaxStableInflight (p99≤500ms) | **16** |
| Peak RPS | ~197 |
| Est. concurrent users (think 30s) | **~6.3k** |

이 숫자는 **채팅 판정 경로** 추정이지, “동시 업로드 사용자 수”의 하드 한도가 아니다.  
업로드 폭주 시 병목은 보통 **tmpfs 용량**, **tee 메모리**, **레지스트리 5k**, **pipeline 파일 내용 검사 CPU** 쪽이다.

### 3.8 Race 완화

채팅이 업로드 PUT 직후 도착하면 `wait_for_saved_attachment(timeout_ms=200, interval_ms=25)`로 tmpfs flush를 짧게 기다린다. (호출부 인자; 전역 env는 아님)

---

## 4. LibreChat 앱 경로 — 개수·크기·레이트리밋

### 4.1 파일 개수·크기

| 항목 | 기본값 | 설정 |
|------|--------|------|
| `fileLimit` | **10** (엔드포인트별) | `librechat.yaml` / `fileConfig` |
| `fileSizeLimit` / `serverFileSizeLimit` | **512 MB** | 동일 (`file-config.ts` `defaultSizeLimit`) |
| `totalSizeLimit` | **512 MB** | 동일 |
| 업로드 방식 | **파일당 1 POST**, 클라이언트 **순차 await** | 서버 batch API 없음 |

### 4.2 Rate limit (동시·폭풍 업로드 억제)

`uploadLimiters.js` 환경변수:

| env | 기본 | 의미 |
|-----|------|------|
| `FILE_UPLOAD_IP_MAX` | **100** | IP당 윈도우 내 최대 POST |
| `FILE_UPLOAD_IP_WINDOW` | **15** (분) | IP 윈도우 |
| `FILE_UPLOAD_USER_MAX` | **50** | 사용자당 윈도우 내 최대 POST |
| `FILE_UPLOAD_USER_WINDOW` | **15** (분) | 사용자 윈도우 |

초과 시 **HTTP 429**. speech 경로는 limiter 제외.

**해석**: 사용자 1명이 15분 동안 파일 업로드 API를 **최대 50회** 호출 가능(기본). 파일 10개면 대략 5턴 분량에 해당(순차 업로드 가정).

### 4.3 S3

multipart part size **5 MB** (`MULTIPART_UPLOAD_PART_SIZE`). 스트리밍 업로드.

---

## 5. 설정 가능한 항목 체크리스트

### 5.1 운영 UI / 정책 (재시작 없이 또는 policy reload)

| 설정 | 위치 | 범위 |
|------|------|------|
| 파일 최대 크기 | Management → 파일 제어 | **10~20 MB** (UI) |
| 허용 확장자 | Management → 파일 제어 | 콤마 목록 |
| Pipeline 로컬 기본값 | `pipeline/local_resource/config.json` | `file_max_size`, `file_allow_ext` |

### 5.2 환경변수

| 변수 | 기본 | 영향 |
|------|------|------|
| `AIVAX_UPLOAD_TTL_SECONDS` | `600` | upload/pending TTL |
| `AIVAX_ATTACH_TMPFS` | `/run/aivax/attachments` | 인라인 첨부 저장 경로 (tmpfs 권장) |
| `AIVAX_PIPELINE_WORKERS` | (배포값, 예: 4) | 채팅/파일 **내용 검사** 처리량 (Valkey merge 전제) |
| `FILE_UPLOAD_IP_MAX` / `_WINDOW` | 100 / 15 | Chat 앱 IP rate limit |
| `FILE_UPLOAD_USER_MAX` / `_WINDOW` | 50 / 15 | Chat 앱 user rate limit |

### 5.3 Envoy YAML

| 키 | 권장/기본 | 파일 |
|----|-----------|------|
| `max_tee_bytes` | `67108864` (64MB) | `inline_engine/aivax/configs/envoy_ingress_*.yaml` |
| `spool_dir` | `/var/lib/aivax/spool` | 동일 |
| `policy_timeout_ms` | `20000` | 채팅 판정용 |
| `upload_path_prefixes` | 서비스별 경로 | 업로드 감지 |

### 5.4 코드 수정이 필요한 상한

다음을 바꾸려면 **상수 수정 후 재배포**가 필요하다.

- `_MAP_LIMIT` (5000), `_SCOPE_PENDING_LIMIT` (2000), `_MAX_IDS_PER_SCOPE` (64)
- `_MAX_FILE_CONTROL_MAXSIZE` (50MB) — UI `FILE_CONTROL_MAX_SIZE_MB`(20)와 함께 맞출지 결정
- 어댑터별 LRU/맵 limit (2048 / 512 / 256 …)
- Pipeline 동시 파일 개수 hard cap (현재 미구현)

---

## 6. 용량 산정 가이드 (실무)

### 6.1 “동시에 몇 명이 파일을 올려도 되나?”

대략적인 안전 구간:

1. **활성 upload 매핑** ≤ `_MAP_LIMIT` (5000)  
   - 예: 평균 사용자당 동시 업로드 2개 → 이론상 ~2,500명 동시 활성 매핑.  
   - TTL 10분이므로 “순간 동시”와 “10분 창 누적”을 구분할 것.
2. **사용자당 pending** ≤ 64 (`_MAX_IDS_PER_SCOPE`)  
   - 한 세션에서 64개를 넘는 초단기 폭풍은 ID가 잘림.
3. **디스크**  
   - tmpfs(`AIVAX_ATTACH_TMPFS`) 크기 ≥ (동시 활성 파일 × 평균 크기).  
   - Envoy spool도 `max_tee_bytes × 동시 tee 중 요청` 여유 필요.
4. **채팅에 첨부된 직후**  
   - pipeline 파일 검사 CPU / `AIVAX_PIPELINE_WORKERS`가 체감 지연을 좌우.

### 6.2 “한 번에 몇 개까지?”

| 경로 | 실효 상한 |
|------|-----------|
| 인라인 스코프 pending | **64**/scope |
| Chat 앱 UI/설정 | **`fileLimit` 기본 10** |
| Perplexity batch_create | 맵/FIFO 한도(256/128) 내 배치 메타 |
| Pipeline 동시 분석 | 주석상 10, **코드 미강제** |

### 6.3 tee skip이 나는 경우

요청 body가 `max_tee_bytes`(64MB)를 넘으면 정책 쪽 사본이 비거나 skip된다.  
정책상 파일 크기 한도(10~20MB)보다 tee 한도가 훨씬 크므로, **정상 정책 크기 안에서는 tee skip이 거의 없어야** 한다.  
정책 max를 크게 올릴 계획이면 `max_tee_bytes`와 tmpfs도 함께 올려야 한다.

---

## 7. 아키텍처 상세 (효율화 메커니즘)

| 메커니즘 | 위치 | 효과 |
|----------|------|------|
| Upstream 즉시 Continue | `aivax_filter.cc` `file_upload_policy_` | 대용량 업로드 UX / timeout 방지 |
| AsyncDiskTee worker | `async_disk_tee.cc` | Envoy worker 스레드 disk sync 금지 |
| max_tee_bytes | Envoy config | 메모리·spool 상한, 초과 시 tee skip |
| Registry 단일 inflight | `enqueueRegistryOp` | UDS registry 호출 직렬화 |
| tmpfs + append | `file_upload_manager.py` | 청크/resumable 자연 지원, I/O↓ |
| Heap TTL eviction | 동일 | O(log n) lazy cleanup |
| Scope claim(N) | `claim_pending_uploads_for_scope` | 병렬 업로드 ID 매핑 |
| Batch multipart / batch_create | Grok·Claude·Perplexity adapters | 1요청→N파일 또는 N URL 일괄 |
| Short poll 200ms | `wait_for_saved_attachment` | upload↔chat race |
| Chat rate limit 429 | `uploadLimiters.js` | 앱 API 과부하 방지 |

관련 롤백·서비스별 맵: [VERSIONING.md](VERSIONING.md) §파일 단계.  
Inline datapath(S3 TLS / Grok promote): [FILE_UPLOAD_DEEP_PATH.md](FILE_UPLOAD_DEEP_PATH.md).

---

## 8. 주요 파일 인덱스

| 파일 | 역할 |
|------|------|
| `ai_engine/core/file_upload_manager.py` | tmpfs, 레지스트리, 배치, TTL |
| `ai_engine/adapters/*_upload.py` | 서비스별 업로드 정규화 |
| `inline_engine/.../aivax_filter.cc` | Continue + tee + policy UDS |
| `inline_engine/.../async_disk_tee.cc` | 비동기 디스크 tee |
| `inline_engine/aivax/configs/envoy_ingress_*.yaml` | `max_tee_bytes` 등 |
| `aivax/pipeline/.../file_block_filter_pattern.py` | 확장자·크기·내용 검사 |
| `aivax/pipeline/local_resource/config.json` | `file_max_size` / `file_allow_ext` |
| `aivax/apps/management/.../FileControlSetting.tsx` | 운영 UI 10~20MB |
| `aivax/apps/chat/.../uploadLimiters.js` | Chat rate limit |
| `aivax/apps/chat/.../file-config.ts` | `fileLimit` 10, size 512MB |

---

## 9. FAQ

**Q. “동시 사용자 6.3k”면 파일도 그만큼 되나?**  
A. 아니오. 6.3k는 **채팅 판정** Lab 추정(think 30s). 업로드는 hold하지 않아 별도 모델이고, soft한도는 레지스트리·tmpfs·스코프 64·Chat rate limit이다.

**Q. 파일 크기를 30MB로 올리고 싶다.**  
A. Management UI는 현재 **20MB 캡**. 그 이상이면 UI 스키마(`FILE_CONTROL_MAX_SIZE_MB`)와 엔진 `_MAX_FILE_CONTROL_MAXSIZE`(50MB), Pipeline `file_max_size`, Envoy `max_tee_bytes`, tmpfs 크기를 **함께** 조정해야 한다.

**Q. 스코프당 64개 / 전역 5000을 env로 바꾸고 싶다.**  
A. 현재는 코드 상수. env화하려면 `file_upload_manager.py` 수정이 필요하다.

**Q. Chat에서 한 번에 20개 올리고 싶다.**  
A. `fileConfig.endpoints.*.fileLimit`을 올리고, rate limit(`FILE_UPLOAD_USER_MAX`)도 같이 검토. 서버는 여전히 파일당 1 POST·순차 업로드다.

**Q. 업로드가 정책에 안 잡힌다.**  
A. `max_tee_bytes` skip, TTL expiry, `_MAP_LIMIT` eviction, 스코프 64 truncate, race(200ms poll 실패)를 의 확인.
Inline `plain_http`에서 Perplexity S3는 **`deep_tls_ports`** 없으면 바디가 Deep에 안 온다. Grok은 채팅 전 **pending→UUID promote**가 필요하다.
정본: [FILE_UPLOAD_DEEP_PATH.md](FILE_UPLOAD_DEEP_PATH.md).

---

## 10. 변경 시 검증 체크리스트

- [ ] Management에서 크기/확장자 저장 → policy snapshot 반영 확인  
- [ ] 10MB 경계(허용/차단) E2E  
- [ ] 동일 세션에서 파일 10개 / 64개 근처 병렬 업로드 → pending 매핑·채팅 첨부  
- [ ] `max_tee_bytes` 미만·초과 tee 동작  
- [ ] tmpfs `df -h /run/aivax/attachments` (또는 `AIVAX_ATTACH_TMPFS`) 여유  
- [ ] Chat 앱: `fileLimit`·429 rate limit  
- [ ] Pipeline workers + Valkey merge (파일 내용 검사 부하 시)
