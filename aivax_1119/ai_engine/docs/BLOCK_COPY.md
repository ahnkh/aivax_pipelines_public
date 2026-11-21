# 차단·마스킹 문구

**Date:** 2026-10-28  
**대상:** Inline 엔진 + Pipeline

차단·마스킹에 보이는 문장은 **`config/block_phrases.json` 한 파일**에서 고칩니다.  
파이프라인이 이 파일로 `block_message`를 만들고, 엔진이 같은 파일로 화면 카드·모델 지시문·동의 문구를 만듭니다.

| 역할 | 경로 |
|------|------|
| 문구 원문 | `ai_engine/config/block_phrases.json` |
| 엔진 조립 | `ai_engine/core/block_copy.py` |
| 파이프라인 조립 | `pipeline/src/domain/inspection_input/models/block_phrases.py` |

관련: [ai_policy_engine.md](ai_policy_engine.md) · [MODULES.md](MODULES.md)

---

## 1. 문구 변경 방법

### 1.1 기본

1. `/home1/aivax/ai_engine/config/block_phrases.json`을 편집합니다.
2. 파이프라인과 엔진이 **같은 파일**을 보게 환경변수를 맞춥니다.
3. **둘 다 재시작**합니다. (프로세스가 뜰 때 파일을 읽습니다. 핫 리로드만으로는 안 바뀝니다.)

| 프로세스 | 환경변수 | 변수를 비우면 |
|----------|----------|----------------|
| 엔진 | `AIVAX_BLOCK_PHRASES_FILE` | `ai_engine/config/block_phrases.json` |
| 파이프라인 | `AIVAX_PIPELINE_BLOCK_PHRASES_FILE` 또는 `AIVAX_BLOCK_PHRASES_FILE` | 코드 기본값(빈 칸). 라이브는 `.env`에 경로를 둡니다. |

라이브 파이프라인 예 (`/home1/aivax/pipeline/.env`):

```bash
AIVAX_BLOCK_PHRASES_FILE=/home1/aivax/ai_engine/config/block_phrases.json
```

엔진은 변수가 없으면 위 기본 JSON을 읽습니다. 별도 경로를 쓰려면 `/etc/aivax/ai_engine.env`에 `AIVAX_BLOCK_PHRASES_FILE=...`을 넣습니다.

### 1.2 바꿀 때 같이 맞출 것

| 바꿀 키 | 같이 맞출 키 / 주의 |
|---------|---------------------|
| `prompt_footer` | `prompt_footer_lead` = 그 문장의 **앞부분**. 엔진이 이 리드로 푸터 줄을 찾습니다. |
| `file_footer` | `file_footer_lead` = 그 문장의 앞부분 |
| `admin_footer` | `hide_admin_mention`이 그 문장 안에 들어 있어야 모델이 숨깁니다. |
| `mask_token` | 파이프라인·엔진이 같은 토큰을 써야 합니다. 문구 파일의 값이 우선입니다. |
| `consent_warn_label` | 동의 블록 제목이자 지시문의 `…부터 {라벨}까지` 끝 앵커입니다. |
| `prompt_title` / `file_title` / `cso_title` | 카드 제목·지시문 시작. 첫 줄이 `[AIVAX]…` 형태를 유지하세요. |

지시문에는 제목·끝 앵커의 **첫 줄만** 들어갑니다. 카드 본문의 이모지·굵게·여러 줄은 지시문에 넣지 않습니다. 넣으면 Gemini가 보이는 답을 비울 수 있습니다.

`{detect_type}`, `{detected}`, `{detection}`, `{reason}` 같은 자리는 요청마다 채워집니다. JSON에서는 중괄호 문법을 유지하세요.

### 1.2.1 정보보호팀 안내로 바꾸기

기본 카드(제목, 탐지 사유, 푸터)는 그대로입니다. 아래 중 하나만 켜면 사용자에게 보이는 안내가 세 줄로 바뀝니다. 코드 분기는 없습니다.

| 방법 | 값 |
|------|-----|
| 문구 파일 `notice_style` | `guideline` |
| 환경변수 `AIVAX_BLOCK_NOTICE_STYLE` | `guideline` (문구 파일보다 우선) |

문장은 같은 파일에서 고칩니다. `{detect_type}` 자리는 그대로 두세요.

| 키 | 용도 |
|----|------|
| `guideline_title` | `###` 제목 |
| `guideline_detect` | 프롬프트 탐지 한 줄. `{detect_type}` |
| `guideline_file_detect` | 첨부·멀티모달 탐지 한 줄. `{detect_type}` |
| `guideline_closing` | 마지막 확인 문장 |
| `guideline_cso_title` | C·S, 미태깅, 등급 조회 실패 카드의 `###` 제목 |
| `guideline_cso_cs` | C 또는 S 등급 첨부 본문 |
| `guideline_cso_untagged` | 등급이 없는 첨부 본문 |
| `guideline_cso_error` | 등급 조회 실패 본문 |
| `guideline_cso_help_label` | 본문 아래 상자 제목. 이모지도 이 문장 앞에 적는다. 비우면 `안내` |
| `guideline_cso_help` | 등급 설정 방법·접속 링크. 비어 있으면 상자를 그리지 않음 |

엔진을 재시작합니다. 파이프라인이 저장하는 `block_message` 원문은 이 플래그의 대상이 아닙니다. C·S, 미태깅, 조회 실패로 막힌 첨부만 위 `guideline_cso_*` 카드로 바뀝니다. 동의 기능이 켜진 일반 차단에는 기존 동의 상자가 안내 아래에 붙습니다. CSO 하드 차단에는 동의 상자를 붙이지 않고, `guideline_cso_help`에 적은 문장만 동의 상자와 같은 모양으로 붙입니다.

```text
### [정보보호팀] …

첨부파일에 S(민감) 또는 C(기밀) 등급으로 분류된 자료가 포함되어 있습니다.
O(공개) 등급으로 …

---
> **등급 설정**
등급 설정 방법과 접속 링크
```

### 1.3 키 목록

#### 제목·마스킹

| 키 | 기본 | 용도 |
|----|------|------|
| `mask_token` | `[AIVAX MASKING]` | 민감값 치환 |
| `prompt_title` | `[AIVAX] 프롬프트 차단` | 정규식·SLM·외부필터 제목 |
| `file_title` | `[AIVAX] 첨부파일 차단` | 첨부 차단 제목 |
| `cso_title` | `[AIVAX] CSO 등급 차단` | CSO 하드 차단 제목 |
| `masking_title` | `[AIVAX] 프롬프트 마스킹` | 정책 테스트 마스킹 안내(채팅 경로 아님) |
| `secuxper_title` | `[AIVAX] 요청이 차단되었습니다.` | secuXper 제목 |

#### 본문·유형·푸터

| 키 | 용도 |
|----|------|
| `prompt_lock` / `file_lock` | 🔒 헤드라인 |
| `external_lock` / `slm_lock` / `masking_lock` | 🚫·⚠️ 헤드라인 (`slm_lock`은 `{detection}` 자리) |
| `sensitivity_warning` | 주의 문장. `⚠️`가 없어 **화면 카드에는 안 들어갑니다.** |
| `detect_type_line` | `❌탐지 유형은 '{detect_type}' 입니다.` |
| `detected_content_line` | `❌탐지된 내용은 '{detected}' 입니다.` (카드에는 안 넣음) |
| `unknown_detect_type_line` | 외부 필터가 유형을 못 줄 때 |
| `detect_type_label` | 엔진이 유형 줄을 찾을 때 쓰는 말. 기본 `탐지 유형` |
| `prompt_footer` / `file_footer` / `admin_footer` | 푸터. **화면에는 없고** 모델 입력에만 붙습니다. |
| `prompt_footer_lead` / `file_footer_lead` | 푸터 앞부분. 파서·숨김용 |
| `hide_prompt_mention` / `hide_file_mention` / `hide_admin_mention` | 지시문이 “출력하지 마”라고 하는 조각 |
| `default_block_footer` | 서버 문장에 푸터 리드가 없을 때 엔진이 넣는 기본 푸터 |

#### CSO

| 키 | 용도 |
|----|------|
| `cso_cs` / `cso_untagged` / `cso_error` | 하드 차단 `block_message` 본문. 엔진이 이 문장으로 행안부 카드 종류를 가른다 |
| `cso_guidance_untagged` / `cso_guidance_blocked` / `cso_guidance_error` / `cso_guidance_open` | API `cso.message`. O등급은 `cso_guidance_open`을 그대로 보여 준다. 앞에 붙는 이모지도 이 문장에 적는다. |
| `guideline_cso_open` | O등급 사후 결재 안내. **`notice_style`이 `guideline`일 때만** `cso_guidance_open` 대신 쓴다. 앞에 붙는 이모지도 이 문장에 적는다 |
| `cso_guidance_open_consent` | 개방 등급 + 다른 필터 차단일 때 동의 안내 덧붙임 |
| `cso_end_anchor` | CSO 지시문 끝. 기본 `마지막 안내 문장` |
| `headline_quote_mark` | 기본 카드 인용 첫 줄 앞 표시. 기본 `🔎`. 비우면 붙이지 않음 |

#### 동의(엔진 화면)

| 키 | 기본 |
|----|------|
| `consent_warn_label` | `⚠️ 주의`. 이모지도 이 문장에 적는다 |
| `consent_short` | `동의` |
| `consent_long` | `동의합니다` |
| `consent_closing` | `전송에 대한 책임은 사용자에게 있습니다.` |

#### secuXper

| 키 | 용도 |
|----|------|
| `secuxper_body` / `secuxper_detect` / `secuxper_unknown_reason` / `secuxper_retry` / `secuxper_admin` | secuXper 전용 문장 |

### 1.4 상황별 조립 결과

파이프라인이 만드는 서버 문장입니다. `{…}`만 요청마다 바뀝니다.

**정규식 프롬프트**

```text
[AIVAX] 프롬프트 차단
🔒AIVAX 정책에 의해 민감정보가 프롬프트에 포함된 것으로 탐지되었습니다.
❌탐지 유형은 '{정책이름}' 입니다.

민감 정보를 전송할 경우, 기밀 정보 또는 개인 정보 유출등의 피해가 발생할 수 있으니 각별한 주의를 부탁드려요
요청하신 프롬프트는 AIVAX에 의해서 요청이 차단되었습니다.
세부 지침 사항은 관리자에게 문의해주세요
```

**첨부파일** — 제목·잠금·푸터가 `file_*` 키입니다. 푸터는 `전송하신 첨부파일은 …`입니다.

**SLM** — `🚫SLM…{detection}…`, 유형 줄 + `탐지된 내용은` 줄. 탐지값 줄은 카드에 없습니다.

**외부 필터** — 유형 없으면 `unknown_detect_type_line`.

**secuXper** — `secuxper_*` 전용 형식. `🔒`/`❌탐지 유형은`이 없어 엔진은 본문을 헤드라인으로 붙입니다.

**CSO 하드 차단** — 해당 줄만 이어 붙입니다. 동의·탐지 사유 없음.

```text
[AIVAX] CSO 등급 차단

CSO 등급이 존재하지 않는 첨부파일이 있어 요청을 차단했습니다.
```

**채팅 마스킹** — 카드 없이 값만 `mask_token`으로 바꿉니다.  
**정책 테스트 마스킹** — `masking_*` 안내(채팅 경로 아님).

엔진 카드(정규식 예):

```text
### [AIVAX] 프롬프트 차단

> 🔎 AIVAX 정책에 의해 민감정보가 프롬프트에 포함된 것으로 탐지되었습니다.

**탐지 사유**
* **유형:** …
* **설명:** …
* **필터:** 프롬프트

---
> **⚠️ 주의**
**'동의'** 또는 **'동의합니다'** 입력 시 요청이 전송됩니다.
전송에 대한 책임은 사용자에게 있습니다.
```

제목은 서버의 첫 `[AIVAX]` 줄입니다. 첨부면 `### [AIVAX] 첨부파일 차단`, CSO면 `### [AIVAX] CSO 등급 차단`입니다. 프롬프트 제목으로 덮어쓰지 않습니다.

---

## 2. 패치 방법

문구 기능이 **아직 없는** 장비에 코드까지 올릴 때입니다.  
이미 반영된 장비에서 **문장만** 바꾸면 [1절](#1-문구-변경-방법)과 [3.2](#32-문구만-바꾼-뒤)만 하면 됩니다.

### 2.1 하지 말 것

- `ai_engine/deploy/inline/install.sh` 다시 실행하지 않습니다. Mirror를 끄고 첨부를 지웁니다.
- `pipeline/deploy/install.sh`도 이 패치에 쓰지 않습니다.
- 엔진만 넣고 파이프라인을 빼거나, 그 반대로 넣지 않습니다. 문장과 카드가 어긋납니다.

### 2.2 묶음

| 묶음 | 최상단 | 푸는 위치 |
|------|--------|-----------|
| `dist/ai-engine-<arch>.tar.gz` | `ai_engine/` | `/home1/aivax` |
| `dist/pipeline-<arch>.tar.gz` | `pipeline/` | `/home1/aivax` |

엔진:

| 묶음 안 경로 | 이유 |
|--------------|------|
| `ai_engine/config/block_phrases.json` | 고객 문구 |
| `ai_engine/core/block_copy.py` | 카드·지시문·표식 |
| `ai_engine/core/ai_policy_engine.py` | 제목·푸터·지시문 연결 |
| `ai_engine/adapters/gemini_engine.py` | 복원 표식·마스킹 |
| `ai_engine/adapters/grok_engine.py` | 복원 표식·마스킹 |
| `ai_engine/adapters/github_copilot_engine.py` | 마스킹 토큰 |
| `ai_engine/adapters/chatgpt_engine.py` | 지시문 헤더 제거 |

파이프라인:

| 묶음 안 경로 | 이유 |
|--------------|------|
| `pipeline/src/domain/inspection_input/models/block_phrases.py` | 안내 조립 |
| `pipeline/src/application/use_cases/input_analysis/regex_input_inspector.py` | 정규식 |
| `pipeline/src/application/use_cases/input_analysis/multimodal_input_inspector.py` | 첨부 |
| `pipeline/src/application/services/prompt_inspection_result_formatter.py` | SLM·외부·secuXper |
| `pipeline/src/extensions/cso/guard.py` | CSO 차단 문장 |
| `pipeline/src/controllers/input_analysis/v1/mapper.py` | CSO API 안내 |
| `pipeline/src/application/use_cases/test_filter_rule/use_case.py` | 정책 테스트 |
| `pipeline/src/bootstrap/pipeline_bootstrap.py` | `mask_token`을 문구 파일에서 읽음 |

`.env`의 `AIVAX_BLOCK_PHRASES_FILE`은 묶음에 없을 수 있습니다. 현장에 한 줄 추가합니다.

### 2.3 묶음에서 꺼내기

```bash
ENG=/home1/aivax/ai-engine-x86_64.tar.gz
PIP=/home1/aivax/pipeline-x86_64.tar.gz

tar -tzf "$ENG" | grep -E 'block_phrases.json|block_copy.py|ai_policy_engine.py|gemini_engine.py|grok_engine.py|github_copilot_engine.py|chatgpt_engine.py'
tar -tzf "$PIP" | grep -E 'block_phrases.py|regex_input_inspector.py|multimodal_input_inspector.py|prompt_inspection_result_formatter.py|cso/guard.py|mapper.py|test_filter_rule/use_case.py|pipeline_bootstrap.py'

sudo tar -xzf "$ENG" -C /home1/aivax \
  ai_engine/config/block_phrases.json \
  ai_engine/core/block_copy.py \
  ai_engine/core/ai_policy_engine.py \
  ai_engine/adapters/gemini_engine.py \
  ai_engine/adapters/grok_engine.py \
  ai_engine/adapters/github_copilot_engine.py \
  ai_engine/adapters/chatgpt_engine.py

sudo tar -xzf "$PIP" -C /home1/aivax \
  pipeline/src/domain/inspection_input/models/block_phrases.py \
  pipeline/src/application/use_cases/input_analysis/regex_input_inspector.py \
  pipeline/src/application/use_cases/input_analysis/multimodal_input_inspector.py \
  pipeline/src/application/services/prompt_inspection_result_formatter.py \
  pipeline/src/extensions/cso/guard.py \
  pipeline/src/controllers/input_analysis/v1/mapper.py \
  pipeline/src/application/use_cases/test_filter_rule/use_case.py \
  pipeline/src/bootstrap/pipeline_bootstrap.py
```

해시 확인(합계 파일 경로는 `ai_engine/...`, `pipeline/...`):

```bash
tar -xOf "$ENG" ai_engine/SHA256SUMS | grep 'ai_engine/core/block_copy.py'
sha256sum /home1/aivax/ai_engine/core/block_copy.py

tar -xOf "$PIP" pipeline/SHA256SUMS | grep 'block_phrases.py'
sha256sum /home1/aivax/pipeline/src/domain/inspection_input/models/block_phrases.py
```

### 2.4 재시작 전 import만 확인

여기서 실패하면 프로세스를 내리지 않습니다.

```bash
PYTHONPATH=/home1/aivax/ai_engine:/home1/aivax/ai_engine/core:/home1/aivax/ai_engine/mirror \
  /home1/aivax/aivax-venv/bin/python3 -c "import block_copy; print(block_copy.BLOCK_TITLE)"

cd /home1/aivax/pipeline
/home1/aivax/aivax-venv/bin/python3 -c \
  "from src.domain.inspection_input.models.block_phrases import get_block_phrases; print(get_block_phrases().prompt_title)"
```

둘 다 `[AIVAX] 프롬프트 차단`(또는 바꾼 제목)이면 문구가 읽힌 것입니다.

파이프라인 `.env`에 경로가 없으면 추가합니다.

```bash
grep -q AIVAX_BLOCK_PHRASES_FILE /home1/aivax/pipeline/.env || \
  echo 'AIVAX_BLOCK_PHRASES_FILE=/home1/aivax/ai_engine/config/block_phrases.json' \
  | sudo tee -a /home1/aivax/pipeline/.env
```

---

## 3. 적용 방법

### 3.1 코드 패치 직후

파이프라인을 먼저, 엔진을 다음에 올립니다. 재시작 동안 정책 API와 `policy.sock`이 잠시 끊깁니다.

```bash
# systemd로 띄운 경우
sudo systemctl restart aivax-pipeline
systemctl is-active aivax-pipeline

sudo systemctl restart aivax-engine-inline
systemctl is-active aivax-engine-inline
ss -xlp | grep policy.sock
```

`policy.sock`을 여는 프로세스는 **하나**여야 합니다.

라이브가 systemd가 아니라 수동 `python3 …/pipeline/main.py` / 엔진 Popen이면, 같은 환경변수·작업 디렉터리로 프로세스를 다시 띄웁니다. `install.sh`는 쓰지 않습니다.

엔진 로그 `/home1/aivax/ai_engine/ai_engine.log`에 아래가 있으면 기동이 끝난 것입니다.

```text
ai policy engine listening on /run/aivax/policy.sock
chatgpt block sysinstruct (AIVAX_BLOCK_MODEL_INSTRUCTION)=on
```

파이프라인: `journalctl -u aivax-pipeline -n 50` (또는 해당 stdio 로그).

### 3.2 문구만 바꾼 뒤

1. `block_phrases.json` 저장  
2. 파이프라인·엔진 **둘 다** 재시작 (3.1과 동일)  
3. 차단 한 건으로 화면 제목·본문이 JSON과 같은지 확인  

한쪽만 재시작하면 안내 문장과 카드/지시문이 어긋납니다.

### 3.3 확인

```bash
# 엔진이 읽은 제목
PYTHONPATH=/home1/aivax/ai_engine:/home1/aivax/ai_engine/core:/home1/aivax/ai_engine/mirror \
  /home1/aivax/aivax-venv/bin/python3 -c "import block_copy; print(block_copy.BLOCK_TITLE); print(block_copy.MASK_TOKEN)"

# 파이프라인이 같은 파일을 쓰는지
cd /home1/aivax/pipeline
/home1/aivax/aivax-venv/bin/python3 -c \
  "from src.domain.inspection_input.models.block_phrases import get_block_phrases as g; p=g(); print(p.prompt_title); print(p.mask_token)"
```

개발 트리 회귀:

```bash
cd /home/AIVAX/ai_engine
PYTHONPATH=.:core:mirror python3 tests/test_block_phrases.py
PYTHONPATH=.:core:mirror python3 tests/test_user_consent.py

cd /home/AIVAX/aivax/apps/pipeline
python3 -m pytest tests/inspection/test_block_phrases.py -q
```

---

## 4. 빠른 체크리스트

**문장만 변경**

- [ ] `config/block_phrases.json` 수정  
- [ ] 푸터를 바꿨으면 `*_footer_lead` / `hide_*`도 맞춤  
- [ ] 파이프라인·엔진 둘 다 재시작  
- [ ] 차단 한 건으로 제목·본문 확인  

**최초 코드 패치**

- [ ] 엔진·파이프라인 묶음에서 2.2 파일만 덮음 (`install.sh` 금지)  
- [ ] SHA256 확인  
- [ ] import 스모크  
- [ ] `.env`에 `AIVAX_BLOCK_PHRASES_FILE`  
- [ ] 파이프라인 → 엔진 재시작  
- [ ] `policy.sock` 리스너 1개  
