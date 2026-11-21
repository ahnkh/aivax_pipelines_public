# AIVAX Pipeline

판정 API (`:9099`). **Inline / Mirror 공용** — 모드별 이중 배포 없음.

코드 맵: [docs/MODULES.md](docs/MODULES.md)

Swagger / OpenAPI: [docs/swagger/README.md](docs/swagger/README.md) — 현재 `/docs`, 과거 develop `/docs/develop`

INPUT/OUTPUT 요청·응답 필드, 가능한 값과 오류 계약:
[docs/API_REFERENCE.md](docs/API_REFERENCE.md)

develop 과거 계약(`b05a06be`): [docs/API_REFERENCE_DEVELOP.md](docs/API_REFERENCE_DEVELOP.md)

사용자 동의 재전송 및 Fluent Bit 로그 갱신: [docs/USER_CONSENT.md](docs/USER_CONSENT.md)

Management 정책 Valkey 발행 계약:
[docs/MANAGEMENT_POLICY_VALKEY_CONTRACT.md](docs/MANAGEMENT_POLICY_VALKEY_CONTRACT.md)

CSO 등급 조회 및 최종 차단 처리:
[CSO 선택 기능](src/extensions/cso/README.md)

CSO 가드는 실행 환경의 `.env` 또는 `.env.development`에서
`AIVAX_PIPELINE_CSO_ENABLED=on`으로 켜고 `off`로 끈다.
`true`/`false`도 지원하며 기본값은 `false`다. OFF이면 가드와 JVM을 생성하지 않는다.
설정은 프로세스 시작 시 읽으므로 변경 후 Pipeline을 재시작한다.
Mirror에서는 이 값과 관계없이 가드 검사를 생략한다.

`AIVAX_PIPELINE_CSO_PROVIDER=fasoo|markany`로 조회 라이브러리를 선택한다.
기본값은 `fasoo`이며, `markany`를 선택하면 Java와 JVM을 사용하지 않는다.
MarkAny는 `olefile`, 암호화 PDF에는 `pikepdf`를 사용한다.
기존 CSO 기동·요청 타임아웃은 Fasoo JVM에만 적용한다.

첨부파일의 `id`가 누락·null·빈 문자열·공백뿐인 경우 빈 첨부 칸으로 제거한다.
일반 요청의 후속 필터는 계속 실행한다.

V1·V2 입력 API는 최상위 `cso`에 `status`와 `message`를 반환한다. V1의 `debug=false`에서도 제공한다.
미태깅·C/S는 태깅·등급 변경 절차를, O는 최종 판정에 따라 결재 상신 또는
동의·결재 상신을 안내한다. CSO 미수행은 `null`이다. 응답 안내와 저장 로그의
CSO 필드는 [`API 응답 계약`](docs/API_JSON_STRUCTURE.md#cso)을 따른다.
API와 OpenSearch의 요청 전체 상태는 같은 집계 함수를 사용하며,
`untagged` → `blocked`(C/S) → `error`(미지원 포함) → `open` → `skipped` 순으로 우선한다.

Fasoo는 스킵 옵션과 관계없이 파일 내용의 MIME을 공통 판독기로 확인하고 문서 형식으로 매핑한다.
그 형식이 `doc/docx/xls/xlsx/ppt/pptx/hwp/hwpx/pdf` 밖이면 라이브러리를 호출하지 않고
`grade=null`, `status=error`로 처리한다.
파일명과 요청의 MIME 값은 판별에 사용하지 않는다. 확장자가 없는 파일도 같은 기준을 적용한다.
MIME 판독 실패 시 임의로 스킵하지 않고 Fasoo 등급 조회를 진행한다.
생략 파일의 상태와 후속 필터 처리 기준은
[`CSO 선택 기능`](src/extensions/cso/README.md#미지원-형식-조회-생략)을 따른다.

MarkAny는 확장자 목록으로 먼저 생략하지 않고 파일 내용 판독 결과 `UNSUPPORTED`에
같은 `error` 분류를 적용한다. 두 프로바이더 모두 미지원 스킵 또는 오류 스킵이
켜져 있으면 미지원 파일을 `skipped`로 처리한다. 기본값은 모두 `false`다.
미태깅 스킵은 미지원에 적용하지 않으며, 미지원 여부는 Pipeline 내부에서만 구분한다.

`AIVAX_PIPELINE_CSO_SKIP_UNTAGGED_ENABLED=true`이면 조회 후 등급이 없는
(`untagged`) 파일의 CSO 차단을 생략한다. 기본값은 `false`다. 확장자 생략과
독립적이며 C/S 차단은 유지한다. 조회 오류는 아래 오류 생략 옵션을 따른다.
Fasoo에서 MIME으로 미지원 여부를 구분하지 못하면 미지원·일부 암호 문서도
`untagged`로 반환될 수 있다. 생략 파일은 `grade=null`, `status=skipped`로 기록한다.

`AIVAX_PIPELINE_CSO_SKIP_ERROR_ENABLED=true`이면 CSO 조회 결과가 `error`인
파일의 차단을 생략한다. 미지원 파일도 포함하며 기본값은 `false`다. JVM 요청 타임아웃·통신·응답 파싱
예외도 포함하며 예외 로그는 유지한다. C/S 등급은 계속 차단하고, 통과한 일반
요청의 후속 필터는 실행한다. 생략 파일은 `grade=null`, `status=skipped`로 기록한다.
앱 시작 시 JVM 기동·준비 실패에는 적용하지 않는다.

성능 측정 결과 및 재현 방법:
[Pipeline](docs/PIPELINE_LOAD_TEST.md) · [CSO](docs/CSO_LOAD_TEST.md)

컨테이너 통합 테스트: [실행 방법과 bulk_logs 경로 지정](docs/CONTAINER_INTEGRATION_TEST.md)

테스트 폴더와 도메인별 실행: [tests/README.md](tests/README.md)

## pack (폐쇄망 이송)

```bash
./scripts/pack.sh            # dist/pipeline-x86_64.tar.gz
# Valkey는 AIVAX packaging에서 먼저 설치한다.
# 현장 설치·서비스 등록은 AIVAX packaging 설치기가 수행한다.
```

ai_engine: `ai_engine/scripts/pack.sh`. Mirror 엔진 바이너리: `mirror_engine/build/build.sh pack`.

```text
Inline:  Envoy → aivax-engine-inline → Pipeline :9099
Mirror:  Suricata SHM → aivax-engine-mirror → Pipeline :9099   # AIVAX_POLICY_API=1 필수
```

## 빠른 설치

운영 서비스는 AIVAX packaging 설치기로 설치합니다.
packaging의 `etc/systemd/aivax-pipeline.service`가 unit 원본이며 설치기에 포함됩니다.
설치기는 Pipeline 코드 배치·오프라인 의존성 준비 후 unit을 등록하고 엔진보다 먼저 기동합니다.
서비스 제거·복원은 packaging의 Reset·Rollback 절차로 관리합니다.
`.env`는 Pipeline에서 관리하며 기존 포장·로딩 방식을 유지합니다.

상세: [docs/DEPLOY_PIPELINE_1G.md](docs/DEPLOY_PIPELINE_1G.md)  
폐쇄망(기본): [docs/DEPLOY_OFFLINE_AIRGAP.md](docs/DEPLOY_OFFLINE_AIRGAP.md)  
문서 인덱스: [docs/README.md](docs/README.md)

## 개발용 수동 실행

운영 unit과 **포트가 겹치면** 먼저 중지:

```bash
sudo systemctl stop aivax-pipeline
# Valkey도 직접 띄울 거면: sudo systemctl stop aivax-valkey
```

### Valkey (로그 병합·이벤트 Stream)

Valkey 서버·CLI·운영 설정·systemd unit은 별도 AIVAX packaging 저장소에서 관리합니다.
Pipeline 패키지에는 Valkey 서버 바이너리를 포함하지 않습니다.
설치와 관리 범위는 [Valkey 인프라 안내](../../docs/200-Architecture/230-infra/235-valkey/235-readme.md)를 참고합니다.

테스트와 부하 도구는 서버에 설치된 `valkey-server`를 PATH에서 찾아
임시 데이터 디렉터리와 별도 포트 또는 Unix socket으로 실행합니다.
운영 중인 Valkey의 데이터를 테스트에 사용하지 않습니다.

Pipeline의 사용자 동기화·정책 알림·첨부파일 이벤트 어댑터는 각 Management
Stream에 `XADD`만 수행합니다. Pipeline은 이 Stream들에 `DEL`, `FLUSHDB`,
`XTRIM`, `XGROUP`을 실행하지 않습니다. `management-user-sync`를 비롯한 consumer
group과 Stream 보존 정책은 Management consumer가 관리합니다.

**시스템 설치 conf로 실행**

```bash
sudo -u valkey /usr/local/bin/valkey-server /etc/valkey/valkey.conf
valkey-cli -h 127.0.0.1 -p 6379 PING
```

종료: `Ctrl+C`, 또는 `valkey-cli -h 127.0.0.1 -p 6379 SHUTDOWN`.

### Pipeline (`main.py`)

```bash
cd /path/to/aivax/apps/pipeline       # 개발 checkout
# LIVE: cd /home1/aivax/pipeline

# 최초 1회: Python 3.13 + uv 필요
make setup                  # /home1/aivax/aivax-venv, runtime + 개발 의존성
make setup-dev              # 개발 환경: .venv-dev, runtime + 개발 의존성

make dev    # .env.development
make prod   # .env
```

- 기본 listen: `127.0.0.1:9099`
- 실행 설정은 `.env` 또는 `.env.development`의 `AIVAX_PIPELINE_*` 변수에서만 읽습니다.
  셸 환경변수가 환경 파일보다 우선합니다.
- 서버·DB·SLM·파일 처리·로그 병합·정책 신호의 설정 계약은
  [env_loader.py](src/bootstrap/env_loader.py)의 `PipelineSettings`에 있습니다.
- 마스킹 치환 문자열은 `AIVAX_PIPELINE_MASK_TOKEN="[AIVAX MASKING]"`으로
  설정합니다. 미설정 시 기본값은 `[AIVAX MASKING]`이며, 빈 값은 허용하지 않습니다.
  정규식·SLM·secuXper 검사, 최종 결과 병합과 정규식 정책 테스트에 적용됩니다.
  환경 파일을 변경한 뒤 Pipeline을 재시작해야 적용됩니다.
- 로그 merge store는 `AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_BACKEND`로 선택합니다.
  기본값 `memory`는 단일 worker만 허용하며, `valkey`는 여러 worker가 같은 병합
  상태를 공유합니다.
- 정책 알림·사용자 동기화 Stream을 활성화한 환경에서는 Pipeline보다 Valkey가 먼저 떠
  있어야 합니다.

확인: `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9099/docs`

### 상태 API

Management의 간단 상태 API `GET /v1`과 같은 응답 `{"status":true}`를
`GET /v1`에서 제공한다. 기존 Pipeline smoke 도구가 사용하는 `GET /`도 같은
응답을 반환한다. 두 경로는 외부 의존성을 확인하지 않는 프로세스 생존 확인이다.

Management의 `/api/v1/health`와 동일한 JSON 형식을 사용한다. Pipeline에 직접
접속할 때는 `/v1/health`를 사용하며, 동일한 경로를 쓰는 클라이언트를 위해
`/api/v1/health`와 그 하위 경로도 제공한다.

| GET 경로           | 의미                                       | HTTP 상태          |
| ------------------ | ------------------------------------------ | ------------------ |
| `/v1/health`       | `/live`와 동일한 프로세스 생존 확인        | 200                |
| `/v1/health/live`  | HTTP 응답 가능 여부, 외부 의존성 조회 없음 | 200                |
| `/v1/health/ready` | Valkey PING·로그 경로 쓰기·디스크 상태     | 정상 200, 실패 503 |

```bash
curl -s http://127.0.0.1:9099/v1/health
curl -s http://127.0.0.1:9099/v1/health/ready
```

생존 확인 응답은 `{"status":"ok","timestamp":"2026-10-10T00:00:00.000Z"}`다.
준비 확인은 `checks`를 추가하며 각 항목은 `status` (`ok`/`error`), `latencyMs`,
실패 시 `message`, 디스크 항목의 `usedPercent`를 사용한다. 모든 시각은 UTC다.

준비 확인 항목은 `valkey`, `logDirectory`, `bulkDirectory`, `disk`다. CDS 전달을
활성화한 경우 `cdsDirectory`도 확인한다. 기존 `PipelineSettings`의 연결·경로 설정을
사용하며 별도 환경변수는 필요 없다. 경로는 필요하면 생성하고 임시 파일을 쓰고
닫아서 쓰기 가능 여부를 확인한다. 테스트 파일은 남기지 않는다. 지정 경로들에서
가장 높은 디스크 사용률이 90% 이상이면 오류로 처리한다. 각 항목을 병렬로 검사하며
항목당 제한 시간은 2초다.

상태 API는 인증 없이 조회할 수 있으며 분석·Stream 이벤트·정책 갱신을 실행하지
않는다. Pipeline이 직접 연결하지 않는 MariaDB/OpenSearch나 외부 SLM·CDS의 수신
완료는 이 API의 검사 범위에 포함하지 않는다. nginx의 `/api`가 Management로
전달되는 환경에서는 위 예시처럼 Pipeline 포트로 직접 조회해야 한다.

운영 복귀:

```bash
sudo systemctl start aivax-valkey aivax-pipeline
```

더 자세한 설명: [docs/DEPLOY_PIPELINE_1G.md](docs/DEPLOY_PIPELINE_1G.md) 「개발용 수동 실행」.

## 폴더 맵

| 경로                          | 역할                                       |
| ----------------------------- | ------------------------------------------ |
| `main.py`                     | uvicorn 엔트리 (`AIVAX_PIPELINE_WORKERS`)  |
| `src/controllers/`            | `/v1/filter` 제품 API controller           |
| `src/application/`            | 입력·출력 분석 유스케이스                  |
| `src/adapters/analysis_log_*` | bulk 로그 전달 + Memory/Valkey merge 구현  |
| `src/bootstrap/env_loader.py` | 검증된 실행 설정 계약과 환경 파일 로더     |
| `scripts/`                    | pack · 개발용 실행 · smoke/soak |
| `docs/`                       | 배포·모듈 문서                             |

`CleanupCron`이 시작 시와 매 60초마다 30분 지난 동의 상태·증적 임시파일을 정리하고, 매시
05분(Asia/Seoul)에 마지막 수정 후 87400초가 지난 `.json`·`.json.consent` 로그를
파일명 날짜별 백업 경로로 이동한다. 백업 경로는
`AIVAX_PIPELINE_LOG_BULK_BACKUP_DIR`로 지정하며 운영 env는
`/data/backup/prompt_log_backup`을 사용한다.

## Python

운영·`make prod`는 `/home1/aivax/aivax-venv`, 개발·테스트는 repo-local `.venv-dev`를 사용한다.
실행 의존성은 `requirements.txt`, 개발 의존성은 `requirements-dev.txt`에서 관리한다.

```bash
cd /path/to/aivax/apps/pipeline
make setup                  # 운영: /home1/aivax/aivax-venv
make setup-dev              # 개발/테스트 환경: .venv-dev
make check
make test-unit             # CI와 같은 유닛 테스트
make test                  # 유닛·API·E2E·Docker/CSO 전체 테스트
make dev
```

Python 3.13과 `uv`가 필요하며, 패키지는 설정된 package index에서 내려받습니다.
`make test`는 Docker Engine·Compose와 JDK 17 이상이 필요하다.
CSO JVM wrapper를 자동 빌드하고 모든 테스트를 한 번에 실행한다.
API/E2E는 임시 Valkey와 전용 설정을 사용하며, Fluent Bit는 로컬 바이너리가
없으면 Docker로 실행한다. 개발·운영 DB 접속 설정은 사용하지 않는다.
필수 도구가 없거나 Docker에 접속할 수 없으면 준비 단계에서 실패 사유를 표시한다.
OpenSearch용 `vm.max_map_count`는 262144 이상이어야 한다.
특정 테스트 선택은 `make test TEST_ARGS='-k open_file_policy -v -s'`로 한다.
`make check`는 `file`, `soffice`, `tesseract`와 핵심 Python import를 확인합니다.
개발 서버를 별도 터미널에서 시작한 뒤 `make smoke`를 실행하면 상태와 필터 API 계약을 확인합니다.
폐쇄망에서는 Python 3.13과 `uv`를 미리 준비하고, `make wheel`로 생성한
`offline-wheel/`을 복사한 뒤 `make setup-offline`을 실행합니다.
로컬 wheel만 사용해 `/home1/aivax/aivax-venv`에 `requirements.txt`와 `requirements-dev.txt`의 실행·개발 의존성을 모두 설치합니다.
다른 경로의 wheel을 사용하려면 `make setup-offline WHEEL_DIR=/path/to/offline-wheel`을 실행합니다.
개발 환경은 `make setup-dev-offline`으로 같은 wheel을 사용해 `.venv-dev`에 설치합니다.
오프라인 설치는 네트워크와 기존 uv 캐시를 사용하지 않습니다.
개발 실행값은 `.env` 또는 `.env.development`에 두며, 기본값은 `src/bootstrap/env_loader.py`에 있습니다.

`make dev`는 `.env.development`, `make prod`는 `.env`를 읽는다.
폐쇄망용 wheel은 `make wheels`로 실행·개발 의존성과 전이 의존성을 먼저 모으고 `make package`로 tarball에 포함한다.
`make package`는 `scripts/pack.sh`를 실행하며 wheel을 자동 생성하지 않는다.
`offline-wheel/`은 생성물이라 Git에 포함하지 않는다.

### 폐쇄망에서 개발

CI의 `pipeline.tar.gz`에는 소스, 테스트, 설정, 개발 스크립트와
실행·개발 의존성을 모은 `offline-wheel/`이 함께 포함됩니다.
압축을 푼 `pipeline/` 디렉터리에서 개발 환경을 구성합니다.
Python 3.13과 `uv`, `make`는 반입 서버에 준비하고, 네이티브 wheel을 사용할 수 있도록
CI 러너와 호환되는 OS·아키텍처를 사용합니다.

```bash
cd /path/to/pipeline
make setup-dev-offline PYTHON=/path/to/python3.13 UV=/path/to/uv
# 현장 서비스 접속 정보에 맞게 .env.development 수정
make dev
```

소스를 수정한 뒤 같은 디렉터리의 별도 터미널에서 검증합니다.

```bash
make test
make lint
```

운영 가상환경은 그대로 두고 `.venv-dev`에서 개발합니다.
설치된 의존성을 유지한 채 소스를 수정·실행·테스트할 수 있습니다.
폐쇄망에서는 온라인 설치용 `make setup`, `make setup-dev`, wheel 생성용 `make wheel`을 실행하지 않습니다.
새 Python 패키지나 버전이 필요하면 온라인 환경에서 wheel을 추가로 모아 반입합니다.
Valkey 등 외부 서비스와 `file`, `soffice`, `tesseract` 같은 OS 도구는 필요한 기능에 맞게 현장에 준비합니다.

## 관련

- ai_engine Inline: `ai_engine/deploy/inline/install.sh`
- ai_engine Mirror: `ai_engine/deploy/mirror/install.sh` (`AIVAX_POLICY_API=1`)
- 모드 정본: `ai_engine/docs/MODE_INLINE_MIRROR.md`
- 성능 증거: `inline_engine/aivax/evidence/PROVE/PERF_ENGINE_PIPELINE/`

## 상황별 HTTP 예제

먼저 접속 주소를 설정한다. 개발 포트는 `19099`, 운영 기본값은 `9099`다.

```bash
BASE_URL="http://127.0.0.1:29099"
```

각 예제는 별도 요청이다. 입력·출력·동의 재전송에는 같은 `message_id`를 사용한다.
아래 예제는 Inline 기준이며 판정은 설정된 정책에 따라 달라진다.
입력 응답의 `final_decision.action`을 확인하고 다음 단계로 진행한다.
LLM 호출은 호출자가 수행하며, `llm_output`은 실제 응답으로 바꿔 보낸다.
정책 이벤트 발행에는 `user_id`가 필요하다. 아래 `user-100`은 실제 사용자 ID로 바꾼다.
`email`, `session_id`는 필요에 따라 추가한다.
`model`에는 어댑터에서 확인한 실제 모델 표시명을 전달한다. 아래 `gpt-4o`는 실제 값으로 바꾸고, 알 수 없으면 빈 문자열로 보낸다.
일반 입력·출력 로그 병합에서는 출력의 `model`이 우선하며, 출력 값이 비어 있으면 입력 값을 유지한다.

### 1. REGEX 걸리게

원래 프롬프트로 LLM을 호출한 뒤 출력 로그를 기록한다.

```bash
MESSAGE_ID="msg-accept-$(date +%s%N)"
EMAIL="user@example.com"

curl -sS "$BASE_URL/v1/filter/multiple_filter" \
  -H 'Content-Type: application/json' \
  --data-binary @- <<JSON
{
  "message_id": "$MESSAGE_ID",
  "model": "gpt-4o",
  "user_id": "user-100",
  "prompt": "010-1234-5678로 전화해서 말해.",
  "client_host": "10.0.55.150",
  "email": "$EMAIL",
  "ai_service": 1
}
JSON

# accept 확인 → LLM 호출 → 실제 응답 기록
curl -sS "$BASE_URL/v1/filter/output_filter" \
  -H 'Content-Type: application/json' \
  --data-binary @- <<JSON
{
  "message_id": "$MESSAGE_ID",
  "model": "gpt-4o",
  "user_id": "user-100",
  "llm_output": "실제 LLM 응답",
  "client_host": "10.0.55.150",
  "email": "$EMAIL",
  "ai_service": 3
}
JSON
```

### 2. SLM 걸리게

`filter_list`에 `slm_filter`만 지정하고 욕설이 포함된 입력을 보낸다.
전역 SLM 필터와 욕설 차단 정책이 활성화되어 있어야 한다.
`final_decision.action`이 `block`이면 LLM과 출력 API를 호출하지 않는다.

```bash
MESSAGE_ID="msg-accept-$(date +%s%N)"
EMAIL="user@example.com"

curl -sS "$BASE_URL/v1/filter/multiple_filter" \
  -H 'Content-Type: application/json' \
  --data-binary @- <<JSON
{
  "message_id": "$MESSAGE_ID",
  "model": "gpt-4o",
  "user_id": "user-100",
  "filter_list": ["slm_filter"],
  "prompt": "씨발, 너는 병신이야.",
  "client_host": "10.0.55.150",
  "email": "$EMAIL",
  "ai_service": 1
}
JSON
```

### 3. 멀티모달 걸리게

`attachments[].id`를 Pipeline 서버에서 접근 가능한 실제 파일 경로로 바꾼다.
파일은 미리 서버에 준비한다. 이 API는 파일 업로드 API가 아니다.

```bash
MESSAGE_ID="msg-accept-$(date +%s%N)"
EMAIL="user@example.com"
FILE_PATH="/home/mark/aivax/apps/management/backend/test/fixtures/attachment-preview/files copy/source.py"

curl -sS "$BASE_URL/v1/filter/multiple_filter" \
  -H 'Content-Type: application/json' \
  --data-binary @- <<JSON
{
  "message_id": "$MESSAGE_ID",
  "model": "gpt-4o",
  "user_id": "user-100",
  "prompt": "첨부 문서를 요약해 주세요.",
  "client_host": "10.0.55.150",
  "ai_service": 1,
  "email": "$EMAIL",
  "attachments": [
    {
      "id": "$FILE_PATH",
      "name": "document.txt",
      "mime_type": "text/plain"
    }
  ]
}
JSON

# accept 확인 → LLM 호출 → 실제 응답 기록
curl -sS "$BASE_URL/v1/filter/output_filter" \
  -H 'Content-Type: application/json' \
  --data-binary @- <<JSON
{
  "message_id": "$MESSAGE_ID",
  "model": "gpt-4o",
  "user_id": "user-100",
  "llm_output": "실제 LLM 응답",
  "client_host": "10.0.55.150",
  "email": "$EMAIL",
  "ai_service": 1
}
JSON
```

첨부 입력도 `masking`이면 `masked_contents`를 사용하고, `block`이면 위 차단·동의 흐름을 따른다.

### 4. 차단 후 동의 없음

아래 입력을 차단하는 정책을 준비한다. `block`이면 LLM과 출력 API를 호출하지 않는다.
최초 차단 로그를 유지한다.

```bash
MESSAGE_ID="msg-accept-$(date +%s%N)"
EMAIL="user@example.com"

curl -sS "$BASE_URL/v1/filter/multiple_filter" \
  -H 'Content-Type: application/json' \
  --data-binary @- <<JSON
{
  "message_id": "$MESSAGE_ID",
  "model": "gpt-4o",
  "user_id": "user-100",
  "prompt": "주민등록번호 970901-1231234를 요약해 주세요.",
  "client_host": "10.0.55.150",
  "email": "$EMAIL",
  "ai_service": 1
}
JSON
```

### 5. 차단 후 사용자 동의

최초 요청이 `block`이고 사용자가 동의한 경우에만 다음 요청을 순서대로 보낸다.
동의 입력에는 원래 요청 값과 `user_consent: true`를 전달한다. 출력에는 같은 원래 ID를 사용하며 동의 플래그는 생략할 수 있다.
`user_consent`는 JSON boolean `true` 또는 문자열 `"true"`, `"on"`을 받는다.
Inline에서 동의가 참이면 전역·필터별 동의 옵션과 Valkey 상태에 관계없이 CSO만 확인하고 허용한다.

```bash
MESSAGE_ID="msg-accept-$(date +%s%N)"
EMAIL="user@example.com"

# 최초 입력: block 확인
curl -sS "$BASE_URL/v1/filter/multiple_filter" \
  -H 'Content-Type: application/json' \
  --data-binary @- <<JSON
{
  "message_id": "$MESSAGE_ID",
  "model": "gpt-4o",
  "user_id": "user-100",
  "prompt": "주민등록번호 970901-1231234를 요약해 주세요.",
  "client_host": "10.0.55.150",
  "email": "$EMAIL",
  "ai_service": 1
}
JSON

# 사용자 동의 후 재전송: accept 확인. CSO block이면 LLM과 출력 API 호출 중단
curl -sS "$BASE_URL/v1/filter/multiple_filter" \
  -H 'Content-Type: application/json' \
  --data-binary @- <<JSON
{
  "message_id": "$MESSAGE_ID",
  "model": "gpt-4o",
  "user_id": "user-100",
  "prompt": "주민등록번호 970901-1231234를 요약해 주세요.",
  "client_host": "10.0.55.150",
  "ai_service": 1,
  "email": "$EMAIL",
  "user_consent": "on"
}
JSON

# 원래 프롬프트로 LLM 호출 후 실제 응답 기록
curl -sS "$BASE_URL/v1/filter/output_filter" \
  -H 'Content-Type: application/json' \
  --data-binary @- <<JSON
{
  "message_id": "$MESSAGE_ID",
  "model": "gpt-4o",
  "user_id": "user-100",
  "llm_output": "실제 LLM 응답",
  "client_host": "10.0.55.150",
  "ai_service": 1,
  "email": "$EMAIL",
  "user_consent": "on"
}
JSON
```

Inline 동의 입력은 CSO 확인 후 일반 필터를 전부 생략하고, 입력 로그를 기존 Valkey 병합 저장소에 적재한다.
출력 API가 같은 ID의 입력과 AI 응답을 병합하면 `.json.consent`로 전달하고,
Fluent Bit가 원래 날짜 인덱스의 기존 문서를 `update`한다.
출력 요청은 동의 플래그 없이도 처리하며 성공의 `out_result`는 일반 출력과 같은 `{}`다.
`queued/skipped` 응답은 사용하지 않는다. HTTP 성공은 최종 OpenSearch 반영 완료를 뜻하지 않는다.
Resume 전용 Valkey 저장소·키 설정은 사용하지 않으며, 동의 없는 일반 요청은 매번 검사한다.
CSO는 계속 검사하며, C/S·미태깅·조회 오류이면 동의 입력도 차단한다.
기존 로그의 `output_text`를 AI 응답으로 바꾸고 `user_consent: on`을 기록한다.
원래 탐지 결과·정책·마스킹·`mode: block`·사용자 정보는 보존한다.
원본 날짜를 확인할 수 없으면 중복 문서를 만들지 않고 갱신을 생략한다.
Mirror 입력은 CSO와 동의 처리 없이 즉시 허용하고 기존 필터를 백그라운드에서 분석·기록한다.
Mirror 로그에는 `cso`를 기록하지 않으며, 동의 값이 들어와도 일반 Mirror 입력처럼 처리한다.
세부 계약은 [USER_CONSENT.md](docs/USER_CONSENT.md)를 참고한다.

## 전역 필터 설정

개발 실행: `AIVAX_PIPELINE_ENV_FILE=.env.development .venv-dev/bin/python main.py`.
개발 환경은 저장소 공통 `config/aivax/mode.json`, 운영 환경은 `/etc/aivax/mode.json`을 읽는다.
`AIVAX_PIPELINE_MODE_FILE`의 상대 경로는 `apps/pipeline`을 기준으로 해석한다.
`AIVAX_PIPELINE_MODE_FILE`로 경로를 지정할 수 있다.

`filters`에는 `multimodal`, `regex`, `slm`, `secuxper` 각각의 `enabled` boolean을 둔다.
`filters`가 추가된 현재 파일의 `schema_version`은 `2`다. Management와 Pipeline은 버전 `1`도 계속 지원한다.
기본값은 기본 필터 3개가 `true`, SecuXper가 `false`다. 기존 파일에 `filters`가 없어도 같은 기본값을 적용한다.
알 수 없는 필터 키나 boolean이 아닌 값은 유효하지 않은 설정으로 기록하고 기본 설정을 적용한다.
설정은 시작 시 캐시하므로 변경 후 Pipeline을 재시작해야 한다. Management도 같은 JSON 계약을 사용하면 된다.

SecuXper는 `secuxper_filter`라는 별도 요청 필터이며 `slm_filter` 뒤에 실행한다.
기본 요청 목록에도 포함되지만 전역 설정에서 활성화한 경우에만 실행한다.
명시적인 `filter_list`를 보내는 클라이언트는 `secuxper_filter`를 추가해야 한다.
SLM 모델 버전은 1–4만 지원한다. 기존 버전 5 설정은 SLM 버전을 1–4로 바꾸고
`filters.secuxper.enabled`를 `true`로 옮겨야 한다.
활성화 플래그는 JSON에서 관리하고, 접속 정보는 `AIVAX_PIPELINE_SECUXPERAI_*` 환경변수로 관리한다.

기존 `AIVAX_PIPELINE_SLM_USE_SKIP`는 제거했다. SLM 활성화 여부는 `filters.slm.enabled`로 관리한다.
