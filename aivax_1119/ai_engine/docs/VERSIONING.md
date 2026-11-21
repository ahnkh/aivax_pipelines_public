# ai_engine 버전 관리 가이드

초보 개발자도 **서비스별로 안전하게 원복**할 수 있도록 정리한 문서입니다.

- 저장소 위치: `/home1/aivax/ai_engine` (이 디렉터리만 독립 git). 작업 트리는 `/home/AIVAX/ai_engine` 일 수 있음.
- 브랜치: `main`
- 기준 태그:
  - `prompt-phase-v1` — 프롬프트 마스킹/차단/요청·응답 추출 완료
  - `file-phase-v1` — 파일 추출·검사·차단 보강 완료

---

## 1. 왜 서비스별로 커밋하나?

한 번에 전체를 커밋하면, ChatGPT만 깨졌을 때도 Gemini·Claude까지 같이 되돌려야 합니다.

그래서 아래처럼 **레이어를 나눕니다**.

```
공통 코어 (ai_policy_engine, http_payload_normalizer, account_lookup, file_upload_manager, …)
   ↑
서비스별 어댑터 (chatgpt / gemini / claude / …)
```

| 문제 범위 | 되돌릴 것 |
|-----------|-----------|
| ChatGPT만 이상 | `adapters/chatgpt_*.py` 만 |
| 파일 검사 공통 헬퍼 | `core/file_upload_manager.py` |
| 프롬프트 단계로 전체 회귀 | `prompt-phase-v1` |
| 파일 보강 단계로 복귀 | `file-phase-v1` |

---

## 2. 단계(Phase) 한눈에 보기

### Phase A — 프롬프트 (`prompt-phase-v1`)

**하는 일**

1. 요청에서 사용자 프롬프트 추출
2. Policy API 판정 (`input_filter` 등)
3. `MASK` / `BLOCK` / `ALLOW`
4. 응답 스트림에서 모델 출력 추출·수집

**포함 서비스**

- chatgpt, gemini, claude, perplexity, grok
- notion, wrks
- (보조) github_copilot, chat_version

### Phase B — 파일 (`file-phase-v1`)

**하는 일**

```
업로드 HTTP
  → normalize_upload() 로 바이트를 tmpfs에 저장   ← 추출
  → (업로드 자체는 보통 ALLOW passthrough)

채팅(completion) HTTP
  → build_policy_attachments()
  → attachments[].id = 로컬 절대경로
  → Policy API file_block_filter                 ← 검사
  → BLOCK/MASK 시 strip_*_attachments()          ← 차단(업스트림에서 첨부 제거)
```

**중요 계약**

| 항목 | 내용 |
|------|------|
| 저장 위치 | `AIVAX_ATTACH_TMPFS` (기본 `/run/aivax/attachments`) |
| `attachments[].id` | **로컬 파일 절대경로** (원격 URL 아님) |
| 검사 시점 | **채팅 시점** (업로드 직후 차단이 아님) |
| 업로드 `handled=True` | 조기 ALLOW — chat policy 와 분리 |
| Inline datapath | ChatGPT/Gemini/Claude 업로드는 Envoy `file_upload_policy_` async tee → 동일 `normalize_upload` (Registry Phase 3 spool과 분리) |

---

## 3. 커밋 히스토리 (오래된 → 최신)

### 3-1. 저장소 / 프롬프트 단계

| SHA (요약) | 커밋 | 의미 |
|------------|------|------|
| `ca67476` | `chore: ai_engine 저장소 초기화` | `.gitignore` |
| `872e572` | `chore(core): … 코어 baseline` | 공통 엔진 |
| `a2ee127` | `feat(chatgpt):` 프롬프트 … | ChatGPT |
| `ce6ffcd` | `feat(gemini):` 프롬프트 … | Gemini |
| `e86941e` | `feat(claude):` 프롬프트 … | Claude |
| `5c74ff3` | `feat(perplexity):` 프롬프트 … | Perplexity |
| `06afe09` | `feat(grok):` 프롬프트 … | Grok |
| `97bf1e5` | `feat(notion):` 프롬프트 … | Notion |
| `75fee9b` | `feat(wrks):` 프롬프트 … | WRKS |
| `37528a4` | `chore(adapters):` Copilot·chat_version | 보조 + **tag `prompt-phase-v1`** |

### 3-2. 파일 단계

| SHA (요약) | 커밋 | 의미 |
|------------|------|------|
| `77a4937` | `feat(core):` wait/expected_path | 공통 레이스 헬퍼 |
| `ea33989` | `feat(chatgpt):` 파일 … | ChatGPT |
| `8a2293b` | `feat(gemini):` 파일 … | Gemini |
| `0fb80e2` | `feat(claude):` 파일 … | Claude |
| `700aaa7` | `feat(perplexity):` 파일 … | Perplexity |
| `f172003` | `feat(grok):` 파일 … | Grok |
| `1369826` | `feat(notion):` 파일 … | Notion |
| `629e8fd` | `feat(wrks):` 파일 … | WRKS + **tag `file-phase-v1`** |

최신 목록은 항상:

```bash
cd /home1/aivax/ai_engine
git log --oneline --decorate
git tag -l '*-phase-v1'
```

---

## 4. 원복 치트시트 (초보용)

작업 전 **항상** 현재 상태를 확인하세요.

```bash
cd /home1/aivax/ai_engine
git status
git log --oneline --decorate -15
```

### 4-1. 특정 서비스만 프롬프트 단계로 되돌리기

예: ChatGPT만

```bash
git checkout prompt-phase-v1 -- \
  adapters/chatgpt_engine.py \
  adapters/chatgpt_upload.py
```

예: Grok만

```bash
git checkout prompt-phase-v1 -- \
  adapters/grok_engine.py \
  adapters/grok_upload.py
```

### 4-2. 특정 서비스만 파일 단계(최신 보강)로 맞추기

```bash
git checkout file-phase-v1 -- \
  adapters/chatgpt_engine.py \
  adapters/chatgpt_upload.py
```

### 4-3. 공통 파일 헬퍼만 되돌리기

```bash
# 프롬프트 단계의 file_upload_manager 로
git checkout prompt-phase-v1 -- core/file_upload_manager.py

# 파일 단계 헬퍼로 복귀
git checkout file-phase-v1 -- core/file_upload_manager.py
```

### 4-4. 작업 트리 전체를 태그 시점으로 (주의)

이후 커밋되지 않은 변경은 **사라집니다**.

```bash
git reset --hard prompt-phase-v1   # 프롬프트 완료 시점
# 또는
git reset --hard file-phase-v1     # 파일 보강 완료 시점
```

### 4-5. “방금 그 커밋”만 취소 (히스토리에 revert 커밋 남김)

```bash
git log --oneline -10          # SHA 확인
git revert <커밋-SHA>          # 예: git revert ea33989
```

서비스 단위로 나뉘어 있으므로, ChatGPT 파일 커밋만 revert 해도 다른 서비스는 유지됩니다.

### 4-6. 원복 후 엔진 반영

어댑터는 핫리로드를 지원하지만, 확실히 하려면 정책 엔진 프로세스를 재시작하세요.

```bash
# 환경에 맞는 실행 스크립트 사용
./run_engine.sh
# 또는 운영 배포 절차에 따라 systemctl / 기존 restart 스크립트
```

---

## 5. 서비스별 파일 맵

| 서비스 | 엔진 | 업로드 | 비고 |
|--------|------|--------|------|
| ChatGPT | `adapters/chatgpt_engine.py` | `adapters/chatgpt_upload.py` | allowlist: conversation turn |
| Gemini | `adapters/gemini_engine.py` | `adapters/gemini_upload.py` | StreamGenerate + TTL |
| Claude | `adapters/claude_engine.py` | `adapters/claude_upload.py` | completion `files[]` |
| Perplexity | `adapters/perplexity_engine.py` | `adapters/perplexity_upload.py` | S3 PUT tee + ask URL→UUID; Inline는 `deep_tls_ports` 필요 |
| Grok | `adapters/grok_engine.py` | `adapters/grok_upload.py` | 업로드 skip(검사), 채팅 시 promote+검사 |
| Notion | `adapters/notion_engine.py` | `adapters/notion_upload.py` | transcript attachment |
| WRKS | `adapters/wrks_engine.py` | `adapters/wrks_upload.py` | `gptFileIds` |
| 공통 | `core/ai_policy_engine.py` | `core/file_upload_manager.py` | UDS 엔진 / tmpfs 저장 |

관련 실행 문서: [`ai_policy_engine.md`](./ai_policy_engine.md) · 배포: [`DEPLOY_INLINE_1G.md`](./DEPLOY_INLINE_1G.md)

---

## 6. 파일 단계 보강 포인트 (`file-phase-v1`)

공통 (`core/file_upload_manager.py`)

- `expected_saved_path(upload_id)` — tmpfs 정규 경로
- `wait_for_saved_attachment(...)` — 업로드↔채팅 레이스용 짧은 대기(기본 200ms)

서비스별

- finalize 시 wait 적용
- in-flight 시 **하드코딩 경로(`/home1/...`) 제거** → `expected_saved_path` 사용  
  (Gemini / Perplexity에서 특히 중요)
- ChatGPT: bare `upload_id`를 `attachments[].id`로 넣던 문제 수정 + 첨부-only BLOCK strip 통합

### 6-1. 후속 보강 (2026-08-07, file-phase 이후)

| 서비스 | 내용 |
|--------|------|
| Grok | multipart는 `filename` 있는 파트만 저장; `build_policy_attachments`에서 `_promote_one_pending_to_asset` (응답 `status:1` only 대응); WS normalize promote는 no-op |
| Perplexity | 어댑터 계약 동일; **datapath**에서 S3를 `deep_tls_ports`+Envoy TLS 체인으로 Deep divert (평문-only면 body tee 0) |
| 공통 Inline | UDP/443 drop을 `deep_ports`와 분리; `/run/aivax/attachments` RuntimeDirectory wipe 주의 |

정본: [`FILE_UPLOAD_DEEP_PATH.md`](./FILE_UPLOAD_DEEP_PATH.md) · 증적 [`GROK_FILE_UPLOAD.md`](../../inline_engine/aivax/evidence/PROVE/I350_BYPASS_1G/GROK_FILE_UPLOAD.md)

---

## 7. 앞으로 커밋할 때 규칙 (추천)

1. **서비스 하나 = 커밋 하나** (`feat(chatgpt): …`)
2. 공통 코어 변경은 **먼저** `feat(core): …` 또는 `chore(core): …`
3. 커밋 메시지에 초보용으로 넣을 것:
   - `[단계]` 프롬프트 / 파일 / …
   - `[왜]` 한두 줄
   - `[포함 파일]`
   - `[원복]` 명령 예시
4. 큰 마일스톤이 끝나면 **annotated tag**  
   예: `git tag -a next-phase-v1 -m "…"`

메시지 예시:

```bash
git commit -m "$(cat <<'EOF'
feat(chatgpt): …

[단계] …
[왜 필요한가]
- …

[포함 파일]
- adapters/chatgpt_engine.py

[원복]
  git checkout file-phase-v1 -- adapters/chatgpt_engine.py
EOF
)"
```

---

## 8. 자주 하는 실수

| 실수 | 결과 | 대안 |
|------|------|------|
| `git reset --hard` 를 습관적으로 사용 | 다른 서비스 수정까지 날아감 | 파일 단위 `git checkout <tag> -- <path>` |
| 업로드 시점에 막히길 기대 | 설계상 업로드는 passthrough | 채팅 시점 `file_block_filter` 확인 |
| `attachments[].id`에 URL/UUID만 넣음 | filter가 파일을 못 염 | 로컬 tmpfs 절대경로 |
| Grok pending promote 없이 UUID만 조회 | `attachment not on disk` | 채팅 전 `_promote_one_pending_to_asset` |
| Perplexity S3를 `deep_ports`만으로 divert | ask만 Deep, 파일 body 없음 | `deep_tls_ports` + leaf SNI |
| `AIVAX` 루트에서 git 실행 | 이 저장소가 아님 | 반드시 `cd …/ai_engine` |

---

## 9. 빠른 명령 모음

```bash
cd /home1/aivax/ai_engine

# 상태
git status
git log --oneline --decorate -20
git show prompt-phase-v1 --stat --no-patch
git show file-phase-v1 --stat --no-patch

# 태그 목록
git tag -l -n9 '*-phase-v1'

# ChatGPT만 프롬프트 단계로
git checkout prompt-phase-v1 -- adapters/chatgpt_engine.py adapters/chatgpt_upload.py

# 변경 확인 후 엔진 재시작
git diff --stat
```

---

*이 문서는 `prompt-phase-v1` / `file-phase-v1` 태그 시점의 운영 가이드입니다.  
SHA는 rebase 등으로 바뀔 수 있으니, 실무에서는 태그 이름과 파일 경로를 우선하세요.*
