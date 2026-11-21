# AIVAX Policy Engine (ChatGate / Mirror)

**Date:** 2026-08-16

SSLproxy는 **사용하지 않습니다.**  
Inline은 Envoy ChatGate UDS, Mirror는 Suricata SHM. 같은 `PolicyEngine`입니다.

서비스별 커밋·태그·원복: [`VERSIONING.md`](./VERSIONING.md)  
모드·서비스 목록: [`MODE_INLINE_MIRROR.md`](./MODE_INLINE_MIRROR.md)  
모듈 맵: [`MODULES.md`](./MODULES.md)

---

## 1) 실행 방법

제품 설치는 systemd (`aivax-engine-inline` / `aivax-engine-mirror`). 로컬 디버그는 `scripts/run_engine.sh`.

```bash
# Inline ChatGate UDS (기본 소켓 /run/aivax/policy.sock)
./scripts/run_engine.sh -- --log-payload --log-level INFO

# 또는 직접 — --socket 을 빼면 argparse 기본이 레거시 /tmp/sslproxy-ai-engine.sock 이라 제품 경로와 다름
python3 core/ai_policy_engine.py \
  --socket /run/aivax/policy.sock \
  --server-url http://127.0.0.1 \
  --port 9099 \
  --log-payload \
  --log-level INFO
```

ChatGPT 요청 프롬프트를 강제로 변조(예: "이주용"):

```bash
python3 core/ai_policy_engine.py \
  --socket /run/aivax/policy.sock \
  --chatgpt-force-request-text "이주용" \
  --log-payload \
  --log-level INFO
```

선택: 키워드 차단 / Rewrite

```bash
python3 core/ai_policy_engine.py \
  --socket /run/aivax/policy.sock \
  --block-keywords "secret,credit card,ssn" \
  --rewrite-from "블랙핑크" \
  --rewrite-to "걸그룹 블랙핑크" \
  --log-payload
```

정책 API 연동 — 제품 기본은 Pipeline `:9099`:

```bash
python3 core/ai_policy_engine.py \
  --socket /run/aivax/policy.sock \
  --server-url http://127.0.0.1 \
  --port 9099 \
  --PipelineInputAPIPath "/v1/filter/multiple_filter" \
  --PipelineOutputAPIPath "/v1/filter/output_filter" \
  --policy-api-timeout-ms 2000 \
  --policy-api-failure-policy open \
  --log-payload \
  --log-level INFO
```

Mirror SHM (로컬):

```bash
./scripts/run_engine.sh -m shm -- --workers 8
```

제품 Mirror unit은 위가 아니라 **`scripts/run_shm_consumer.sh`** 만 호출합니다.  
unit `ExecStart`에 `${hp%%:*}` 를 넣지 마세요 (systemd `%%` → `%`).

---

## 2) 앞단 (SSLproxy 아님)

| 모드 | 앞단 | 엔진 입구 | systemd |
|------|------|-----------|---------|
| Inline | Envoy `aivax-inline` (ChatGate) | `/run/aivax/policy.sock` | `aivax-engine-inline` |
| Mirror | Suricata (`aivax-mirror`) | POSIX SHM ring | `aivax-engine-mirror` → `run_shm_consumer.sh` |

레거시 `ai-engine.service` (`/tmp/sslproxy-ai-engine.sock`)와 **혼용 금지**. 남아 있으면 stop.

제품 Inline ExecStart 요지 (`deploy/inline/aivax-engine-inline.service`):

- `--socket /run/aivax/policy.sock --socket-mode 660`
- `--server-url http://127.0.0.1 --port 9099`
- `--policy-api-timeout-ms 60000`
- `--policy-api-max-inflight 256 --policy-api-pool-limit 256`
- `--policy-api-acquire-timeout-ms 200`
- `--policy-api-failure-policy open`

Mirror 래퍼 요지 (`scripts/run_shm_consumer.sh` ← `ai_engine.env`):

- `--adapters-no-reload --workers $AIVAX_SHM_WORKERS --cpu-list $AIVAX_SHM_CPU_LIST`
- `--policy-api-timeout-ms ${AIVAX_POLICY_API_TIMEOUT_MS:-15000}`
- `--policy-api-max-inflight 256 --policy-api-pool-limit 256`
- `AIVAX_POLICY_API=1` 이면 `--server-url` / `--port` 를 URL에서 분해 (제품 필수)

---

## 3) 무엇을 하는가

1. ChatGate(UDS 프레임) 또는 Mirror(SHM)에서 요청/응답 payload 수신
2. `core/http_payload_normalizer.py`로 **확실한 프레이밍만** 정리  
   HTTP 응답/요청 헤드 제거, HTTP/1 chunked 디코드, keep-alive 다음 요청 절단.  
   애매하면 바이트를 그대로 두고 `suspect`만 표시. multipart·바이너리 Content-Type은 스킵.
3. `payload.raw_b64` 디코딩 (UDS)
4. 메타데이터(`service_id`, `direction`, `meta.host`, `meta.path`)와 payload preview 로그(옵션)
5. `account_lookup.py`로 user/email 보정 (SQLite 미러 기본 on)
6. 기본 동작: `ALLOW`
7. `--server-url` / `--port` / `--PipelineInputAPIPath` 설정 시 요청 정책 API 호출  
   Input: `filter_list`, `prompt`, `encoding`, `user_id`, `email`, `ai_service`, `client_host`, `session_id`, `attachments`, `message_id`
8. 응답은 실시간 전송을 유지한 채 `PipelineOutputAPIPath`로 로그 저장  
   Output: `llm_output`, `user_id`, `email`, `ai_service`, `client_host`, `session_id`, `message_id`
9. 정책 API `final_decision.action`  
   - `allow` → `ALLOW`  
   - `masking` → `masked_contents`로 입력 치환 (`MASK`), 응답 스트림 PASS  
   - `block` → 입력 치환 후 응답 스트림을 `block_message`로 치환
10. 키워드 매칭 → `BLOCK` · Rewrite 조건 → `REWRITE` + `payload_b64`
11. `--policy-api-failure-policy`: `open`(통과, 제품 기본) / `close`(차단)

---

## 4) 어댑터 구조 (동적 로딩)

경로: `adapters/`. 파일명 `{service}_engine.py`.  
`service_id=gpt` → `chatgpt_engine.py`.

선택 구현:

- `preprocess(req, payload) -> bytes`
- `extract(req, payload) -> str | None`
- `extract_session_id(req, payload) -> str`
- `response_stream_done(req, payload) -> bool`
- `rewrite_always` / `rewrite_prompt` / `rewrite_response` / `rewrite`

실패 시 기본 파서로 **안전하게 fallback**.  
핫 리로드 기본 ON. 끄려면 `--adapters-no-reload` (Mirror 제품 경로).

현재 엔진: chatgpt, gemini, claude, grok, perplexity, notion, wrks, github_copilot, chat_version.

---

## 5) Inline UDS 프로토콜 요약

Envoy `aivax_core` ↔ 엔진 계약. 경로 `/run/aivax/policy.sock`은 유지.

- 요청 프레임: `uint32_be length` + JSON bytes
- 응답 프레임: `uint32_be length` + JSON bytes

요청 JSON: `request_id`, `service_id`, `direction` (`request`/`response`), `unit_type`, `payload.raw_b64`  
응답 JSON: `request_id`, `action` (`ALLOW|MASK|REWRITE|BLOCK`), `reason`  
`BLOCK` 시: `block_message`, `block_response` (`status`, `content_type`, `body`)

---

## 6) CLI 플래그 (자주 쓰는 것)

| 플래그 | 의미 |
|--------|------|
| `--socket` | UDS 경로. **제품은 항상 `/run/aivax/policy.sock`** |
| `--extra-socket` | 추가 UDS (반복 가능) |
| `--socket-mode` | 8진 권한 (기본 660) |
| `--server-url` / `--port` | Pipeline |
| `--PipelineInputAPIPath` / `--PipelineOutputAPIPath` | 입력/출력 API |
| `--policy-api-timeout-ms` | HTTP 타임아웃 |
| `--policy-api-max-inflight` / `--policy-api-pool-limit` | 동시성·커넥션 풀 |
| `--policy-api-acquire-timeout-ms` | 슬롯 대기 |
| `--policy-api-failure-policy` | `open` \| `close` |
| `--adapters-dir` / `--adapters-no-reload` | 어댑터 |
| `--account-cache-maxsize` / `--account-cache-ttl` | 계정 LRU |
| `--log-level` / `--log-payload` / `--payload-preview-bytes` | 로그 |
| `--block-keywords` / `--rewrite-from` / `--rewrite-to` | 로컬 테스트 |

계정 SQLite: env `AIVAX_ACCOUNT_CACHE_PATH` (기본 `/run/aivax/account_cache.sqlite`, 빈 값=끔).

---

## 7) 테스트

```bash
cd /home1/aivax/ai_engine   # 또는 이 작업 트리
PYTHONPATH=.:core:mirror python3 tests/test_extraction_regression.py
PYTHONPATH=.:core:mirror python3 tests/test_account_lookup_concurrency.py
PYTHONPATH=.:core:mirror python3 tests/test_ops_classify.py
```
