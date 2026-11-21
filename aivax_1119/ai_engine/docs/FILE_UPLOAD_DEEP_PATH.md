# Inline Deep Path — 파일 업로드 · 첨부 바인딩

**Date:** 2026-08-16  
**대상:** `prod_i350_bypass_*` · Policy UDS · Envoy `aivax_core` · `AIVAX_ATTACH_TMPFS`  
**관련:** [`FILE_UPLOAD_CAPACITY.md`](FILE_UPLOAD_CAPACITY.md) · [`VERSIONING.md`](VERSIONING.md) ·  
Perplexity 증적 [`PERPLEXITY_DEEP_PATH.md`](../../inline_engine/aivax/evidence/PROVE/I350_BYPASS_1G/PERPLEXITY_DEEP_PATH.md) ·  
Grok 증적 [`GROK_FILE_UPLOAD.md`](../../inline_engine/aivax/evidence/PROVE/I350_BYPASS_1G/GROK_FILE_UPLOAD.md)

---

## 1. 한 줄 계약

| 단계 | 동작 |
|------|------|
| 업로드 HTTP | `normalize_upload` → tmpfs 저장 · 대개 **ALLOW passthrough** |
| 채팅 HTTP/WS | `build_policy_attachments` → `attachments[].id` = **로컬 절대경로** → `file_block_filter` |

업로드 직후 차단이 아니다. 검사·차단은 **채팅 턴**이다.

---

## 2. `plain_http` + 복호화기에서 자주 깨지는 패턴

복호화기가 AI 본문은 `:80` 평문으로 넘기고, **S3 등 일부 오리진은 MITM하지 않으면** 업로드 바디는 계속 **TLS/443**이다.

```text
ask / chat  ──► decryptor ──HTTP:80──► Deep (deep_ports)
S3 PUT      ──► (MITM 없음) ──TLS:443──► Fast hairpin  ← 예전: tee 0건
```

조치 (`configs/ingress.yaml`):

```yaml
deep_ports: [80]           # 복호화기 평문 출구
deep_tls_ports: [443]      # MITM 안 되는 TLS 오리진 → Envoy terminate
```

| 계층 | 동작 |
|------|------|
| flow_agent | S3 host/CIDR 행은 `ports = deep_ports ∪ deep_tls_ports` divert |
| TPROXY | `tproxy_ports` = 위 합집합 (`select_ingress_profile.sh`) |
| Envoy `plain_http` | `tls_inspector` + SDS leaf 체인(`aivax_plain_http_tls`) + `raw_buffer` 평문 체인 |
| UDP | **항상** UDP/443 drop (QUIC). `deep_ports`를 재사용하지 않음 (`[80]`이면 QUIC drop이 무력화되던 버그 수정) |

클라이언트(또는 MDM)는 **bump/leaf CA**를 신뢰해야 Envoy가 끊은 S3 TLS가 통과한다.  
노트북이 복호화기 CA만 신뢰하고 S3를 복호화기가 안 푸는 구성이면, S3 구간은 AIVAX leaf를 신뢰해야 한다.

상세 포트·회신: [`deep_tproxy_return_path.md`](../../inline_engine/aivax/docs/deep_tproxy_return_path.md) ·  
랩: [`plain_http_lab_test.md`](../../inline_engine/aivax/docs/plain_http_lab_test.md)

---

## 3. Perplexity

```text
POST …/batch_create_upload_urls  → file_uuids
PUT  ppl-ai-file-upload.s3.amazonaws.com/…/<file_uuid>/…  → 바디 tee → tmpfs
POST …/perplexity_ask  params.attachments[] URL → UUID → 로컬 경로
```

| 기대 로그 | 의미 |
|-----------|------|
| `file_upload_policy_tee` / host `ppl-ai` | S3가 Deep·Envoy에 들어옴 |
| `normalize_upload: saved` | tmpfs에 본문 존재 |
| ask `attachments named=N on_disk=N` | 채팅 바인딩 성공 |

실패 시 OF에서 S3 CIDR `tp_dst=443` divert `n_packets`·Envoy S3 tee를 본다.  
코드: `adapters/perplexity_upload.py` · host resolve `ppl-ai-file-upload.s3.amazonaws.com`.

---

## 4. Grok

```text
POST /http/upload-file-v2/direct (multipart, filename 있는 파트만)
  → pending tmpfs (예: grok-<temp>)
응답이 status:1 only 인 경우 fileMetadataId 없음
채팅 REST/WS fileAttachments|file_mention UUID
  → build_policy_attachments 직전 _promote_one_pending_to_asset
  → attachments[].id = 로컬 경로
```

| 규칙 | 이유 |
|------|------|
| multipart는 **`filename` 있는 파트만** 저장 | `source_conversation_id` 등 폼 필드가 파일로 저장되던 오탐 제거 |
| promote는 **채팅 `build_policy_attachments`** | 업로드 응답에 UUID가 없어도 FIFO pending → 서버 UUID 바인딩 |
| WS `normalize_upload` promote 분기는 no-op | 이중 promote / 레이스 방지 |

테스트: `ai_engine/tests/test_grok_upload.py`  
코드: `adapters/grok_upload.py`

---

## 5. HTTP/2 · HPACK

Deep는 Envoy HTTP 필터 경로다. HPACK은 코덱이 풀어 `HeaderMap`으로 넘긴다.  
`aivax_core` / policy는 **압축 헤더 바이트를 직접 파싱하지 않는다** → 헤더 압축만으로 첨부 추출이 깨지지 않는다.  
바디 `content-encoding`·WS frame 압축은 별 이슈다.

---

## 6. 운영 주의 (`/run/aivax`)

`RuntimeDirectory=aivax`를 쓰는 unit을 재시작하면 `/run/aivax`가 비워질 수 있다.

- `policy.sock` / `registry.sock` / `attachments/` / account_cache 재생성 확인
- policy·registry를 **동시에** stop→start 하면 dangling sock inode를 피할 수 있다
- 첨부 TTL(기본 600s) 지나면 pending 파일이 삭제된다 — 업로드 후 오래 두고 채팅하면 `on_disk=0`

---

## 7. 라이브 스모크

```bash
curl -sf http://127.0.0.1:9901/ready; echo
ls -la /run/aivax/policy.sock /run/aivax/attachments
# Perplexity: PDF 업로드 → ask → on_disk≥1
# Grok: PDF 업로드 → 채팅 → grok asset promote · attachment not on disk 없음
tail -F /home1/aivax/ai_engine/ai_engine.log
```
