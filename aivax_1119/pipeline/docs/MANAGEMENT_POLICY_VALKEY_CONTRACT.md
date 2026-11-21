# Management → Pipeline 정책 Valkey 계약

키 namespace와 배포 전환 절차는 [Valkey 키 명명 규칙](../../../docs/300-Data-model/310-valkey/310.1-key-conventions.md)을 따른다.

Pipeline은 MariaDB를 직접 조회하지 않고 Management가 Valkey에 배포한 정책
projection을 읽는다. 정책 key에는 TTL을 설정하지 않으며, Management가 전체
snapshot을 교체한다.

## Key와 자료형

| Key | Type | 내용 |
| --- | --- | --- |
| `pipeline:policy:regex:v1` | String(JSON) | Regex rule과 group membership snapshot |
| `pipeline:policy:multimodal:v1` | String(JSON) | Multimodal rule, group membership, file-control snapshot |
| `pipeline:policy:slm:v1` | String(JSON) | SLM rule과 group membership snapshot |
| `pipeline:policy:secuxper:v1` | String(JSON) | secuXper 실행 코드와 대상 매핑 snapshot |

key suffix의 `v1`은 자료 구조의 schema version이다. 세 JSON의 `version`은 한
번의 전체 배포를 식별하는 동일한 UTC timestamp다. 빈 목록인지와 관계없이 해당
배포에서 생성한 값을 사용한다.

## Group membership

Regex, Multimodal, SLM, secuXper는 각각 자신의 JSON에 `group_members`를 포함한다. key는
Management group ID이고 value는 해당 그룹의 사용자 email 배열이다. 각 정책에는
해당 정책의 group scope rule이 참조하는 그룹만 포함하며, email은
`trim + lowercase`로 정규화한다. 한 사용자가 여러 그룹에 들어가면 여러 배열에
같은 email이 있어도 된다.

## 공통 scope

| `scope` | `subject_id` |
| --- | --- |
| `user` | 정규화된 사용자 email |
| `service` | Management AI service ID 문자열(예: `"1"`) |
| `group` | Management group ID |
| `default` | 빈 문자열 |

우선순위는 `user → service → group → default`다.

## Regex

Regex는 그룹 판별에 필요한 email 목록을 동일한 JSON의 `group_members`에 넣어
한 번의 `GET`으로 완결된 snapshot을 받는다.

```json
{
  "version": "20260914T153000Z",
  "group_members": {
    "group-1": [
      "user1@example.com",
      "user2@example.com"
    ]
  },
  "rules": [
    {
      "id": "policy-rule-id",
      "name": "API Key 차단",
      "rule": "c2stW0EtWmEtejAtOV0r",
      "action": "block",
      "category": "security",
      "scope": "group",
      "subject_id": "group-1"
    }
  ]
}
```

Regex rule의 필드는 `id`, `name`, `rule`, `action`, `scope`, `subject_id`,
`category`만 사용한다. `rule`은 Base64 UTF-8 정규식이다. `targets`는 항상
`prompt`, 정규식 flag는 기본값(`0`), 탐지 범위는 전체 match로 고정하므로
`targets`, `regex_flag`, `regex_group`, `regex_group_val`은 보내지 않는다.

## Multimodal

```json
{
  "version": "20260914T153000Z",
  "group_members": {
    "group-1": [
      "user1@example.com",
      "user2@example.com"
    ]
  },
  "rules": [
    {
      "id": "policy-rule-id",
      "name": "대외비 차단",
      "targets": "file",
      "rule": "64yA7J207JeE",
      "regex_flag": 0,
      "action": "block",
      "category": "security",
      "scope": "group",
      "subject_id": "group-1"
    }
  ],
  "settings": [
    {
      "skey": "fileControlAllowedExtensions",
      "svalue": "pdf,docx,xlsx"
    },
    {
      "skey": "fileControlMaxSize",
      "svalue": "26214400"
    }
  ]
}
```

Multimodal은 부분 masking을 지원하지 않아 `masking` action을 `block`으로 실행한다.

## SLM

```json
{
  "version": "20260914T153000Z",
  "group_members": {
    "group-1": [
      "user1@example.com",
      "user2@example.com"
    ]
  },
  "rules": [
    {
      "id": "policy-rule-id",
      "name": "이메일 마스킹",
      "detection_kind": "pii",
      "detection_type": "EMAIL",
      "action": "masking",
      "category": "연락·위치:통신/온라인 식별자",
      "scope": "user",
      "subject_id": "user@example.com"
    }
  ]
}
```

SLM rule은 `id`, `name`, `detection_kind`, `detection_type`, `action`,
`category`, `scope`, `subject_id` 필드를 사용한다. `detection_kind`는 `pii`,
`guardrail`, `abuse` 중 하나다. PII와 가드레일은 `detection_type`이 필요하고,
악성·욕설·유해 정책은 `detection_type`을 `null`로 보낸다.

PII는 `accept`, `masking`, `block` action을 사용할 수 있다. 가드레일과
악성·욕설·유해는 마스킹할 문자열을 제공하지 않으므로 `accept`, `block`만
사용한다. 유효하지 않은 개별 rule은 제외하고 나머지 정상 rule을 적용한다.
`targets`는 보내지 않는다.

## 발행 규칙

정책 String은 직렬화가 끝난 전체 JSON을 `SET` 한 번으로 교체하고 부분 수정하지
않는다. 세 snapshot은 하나의 `MULTI/EXEC` 안에서 함께 교체한다.

```text
MULTI
SET pipeline:policy:regex:v1 '<전체 JSON>'
SET pipeline:policy:multimodal:v1 '<전체 JSON>'
SET pipeline:policy:slm:v1 '<전체 JSON>'
EXEC
```

세 key의 갱신을 모두 완료한 다음 Pipeline의 기존 시그널 API를 한 번 호출한다.

```http
POST /v1/filter/policy-refresh
Content-Type: application/json

{}
```

Pipeline은 시그널을 받으면 Regex, Multimodal, SLM snapshot을 Valkey에서 다시
읽어 각 로컬 cache를 교체한다. 이 조회 과정에서는 정책 key에 `SET`, `DEL`,
`RENAME`, `XTRIM` 등의 쓰기 명령을 실행하지 않는다.

빈 Regex와 SLM 정책은
`{"version":"...","group_members":{},"rules":[]}`로 발행한다. 빈 Multimodal
정책은 `{"version":"...","group_members":{},"rules":[],"settings":[]}`로
발행한다. key는 삭제하지 않는다.
Pipeline 시작 시 key가 아직 없으면 해당 정책을 빈 snapshot으로 적용한다. 이는
Management와 Pipeline의 기동 순서 차이를 허용하기 위한 동작이며, Management의
정상 발행에서는 key를 유지하는 원칙을 그대로 따른다.
Pipeline은 JSON 문서나 `rules` 배열 자체가 잘못되면 기존 정상 snapshot을
유지한다. 개별 Regex rule의 Base64·정규식 또는 SLM rule의 scope·action·탐지
필드가 잘못되면 해당 rule만 제외하고 나머지 정상 rule을 적용한다.

Pipeline은 정상 검증된 세 정책 snapshot을 `<runtime_dir>/policy-cache` 아래의
`regex-v1.json`, `multimodal-v1.json`, `slm-v1.json`에 각각 원자 저장한다.
프로세스 재시작 시 Valkey 조회 또는 payload 검증에 실패하면 해당 마지막 정상
snapshot으로 cache를 복원한다. Valkey key와 로컬 snapshot이 모두 없는 최초
기동에서는 빈 snapshot을 적용한다. 로컬 파일은 Valkey 원본을 대체하지 않으며
장애 복구에만 사용한다.

## AI 엔진 정책 파일

AI 서비스 ID와 로그 표시명 목록은 `pipeline:ai-service:catalog:v1`에
`{"version":"<정책 발행 버전>","services":{"9":"GitHub Copilot","10":"openapi.copilot"}}`
형태로 함께 배포한다. Management DB의 모든 type을 포함하고 TTL을 설정하지 않는다.
Regex·Multimodal·SLM snapshot과 같은 `MULTI/EXEC`에서 교체한 후 기존 refresh
Stream을 발행한다. Pipeline은 시작·Stream 수신·정책 polling 때 카탈로그를 읽고
입력·출력·CSO 로그의 이름을 조회한다. 숫자 원본은 `ai_service_id`에 유지한다.
미등록 ID의 이름은 빈 문자열이다. 키 누락·통신·검증 실패에는 다른 정책과 같이
로컬 백업, 현재 메모리 목록, 빈 목록 순서로 복구한다. 정상 빈 목록은 이전 목록을
비운다. 고정 서비스 번호표는 사용하지 않는다.
초기 반영은 Management에서 `npm run policy:publish-to-pipeline`을 실행한 뒤
Pipeline을 시작한다. 상세 복구 계약은 [실행 정책 snapshot](../../../docs/300-Data-model/310-valkey/312-string/312.1-policy-snapshots.md)을 따른다.

`aivax_policy.json` 생성과 배포는 Pipeline이 수행하지 않는다. Management가 AI
서비스 상태와 Multimodal file-control 설정을 조합해 엔진이 읽는 공유 경로에 직접
원자 교체한다.

```json
{
  "redirect_url": "https://block.example.com",
  "block_html_file": "/home1/aivax/data_resource/block.html",
  "chatgpt": "inspect_on",
  "copilot": "block",
  "file_allow_extension": "pdf,docx,xlsx",
  "file_control_maxsize": "26214400"
}
```

AI service 중 `type = 1`인 항목만 넣고, service name은 `trim + lowercase`하여
property name으로 사용한다. status는 `0=block`, `1=inspect_on`, `2=expired`,
`3=inspect_off`, `4=redirect`로 변환하며 알려지지 않은 값은 `inspect_on`으로
처리한다. file-control 값은 Multimodal의 `fileControlAllowedExtensions`와
`fileControlMaxSize`에서 가져온다. AI service 또는 file-control 설정이 변경될 때와
Management가 기동할 때 파일을 생성한다. 같은 filesystem에 임시 파일을 완성한 뒤
atomic rename하여 엔진이 부분 JSON을 읽지 않도록 한다.

## secuXper 독립 정책

키는 `pipeline:policy:secuxper:v1`이며 필터 활성화는 공통 `mode.json`의
`filters.secuxper.enabled`로 결정한다. 기존 SLM 정책과 독립적으로 읽고 실행한다.

```json
{
  "version": "배포 버전",
  "group_members": {"department-id": ["user@example.com"]},
  "rules": [{
    "id": "policy-code", "name": "정책명", "targets": "prompt",
    "scope": "group", "subject_id": "department-id", "policy_code": "policy-code"
  }]
}
```

scope와 대상 식별자는 위 공통 계약을 따른다. `default`의 `subject_id`는 빈 문자열이다.
처음 매칭된 scope의 모든 코드를 중복 제거하고 순서대로 호출한다. 차단이면 중단하고,
여러 마스킹은 원문 구간을 합친다. 매칭이 없으면 검사하지 않는다. 실행 `action`과
`category`는 외부 응답에서 결정한다. 한 요청은 하나의 스냅샷을 사용한다.

Management가 Lua CAS로 저장과 `pipeline:policy:refresh:v1`의 `XADD`를
원자적으로 처리한다. secuXper의 `version`은 발행마다 생성하는 UUID로, 기존 세
필터의 전체 배포 버전과 독립적이다. 각 worker는 기존 Stream listener로 갱신하며
기동·주기 갱신과 수동 refresh 신호도 적용된다. 정상 정책은
`AIVAX_PIPELINE_RUNTIME_DIR/policy-cache/secuxper-v1.json`에 백업한다.
JSON 오류·Valkey 장애·키 누락 시 마지막 정상 정책을 유지하고, 정상 빈 스냅샷은
기존 정책을 제거한다.

`AIVAX_PIPELINE_POLICY_SECUXPER_KEY` 기본값은 위 새 키다.
고정 코드 환경변수 `AIVAX_PIPELINE_SECUXPERAI_POLICY_CODE`는 사용하지 않는다.
새 키가 없을 때 Management가 구 키 `management:secuxper:policy:v1`을 읽어
메타데이터를 채우고 새 키로 발행한다. 구 키는 보존한다.

상세 발행·이관 계약: [Management secuXper 계약](../../../docs/400-Interfaces/410-management/412.1-secuxper-policy-valkey.md).
