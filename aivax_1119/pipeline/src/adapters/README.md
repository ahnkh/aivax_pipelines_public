# Adapter 폴더

도메인 port를 구현하고 DB, Valkey, HTTP, 파일 시스템, 외부 라이브러리 같은
인프라 세부사항을 격리한다. adapter는 domain을 import할 수 있지만 domain은
adapter를 import하지 않는다.

## 하위 폴더

| 폴더 | 외부 구현 책임 |
|------|----------------|
| `analysis_log_delivery` | 완성 분석 로그를 Fluent Bit 또는 CDS 파일 queue·HTTP API로 전달 |
| `analysis_log_merge` | 입력·출력 대기 상태를 memory 또는 Valkey에 저장 |
| `config_aivax` | `mode.json`을 읽고 AIVAX 설정 snapshot 제공 |
| `delivery_file` | 차단 첨부파일을 로컬 백업 경로로 전달 |
| `inspection_multimodal` | 로컬 문서·이미지·워터마크 분석 실행 |
| `inspection_regex` | 주입받은 regex 정책으로 문자열 검사 실행 |
| `inspection_slm` | SLM 버전별 HTTP 요청과 응답 해석 |
| `notification_attachment` | 첨부파일 이벤트를 buffering·dedupe·재시도 후 Valkey Stream에 발행 |
| `notification_audit` | 감사 이벤트를 Management 내부 HTTP API로 전달 |
| `notification_policy` | 정책 탐지 이벤트를 buffering·dedupe·재시도 후 Valkey Stream에 발행 |
| `notification_user` | 사용자 활동 이벤트를 buffering·dedupe·재시도 후 Valkey Stream에 발행 |
| `policy_multimodal` | Valkey 멀티모달 정책·group membership 조회와 local cache |
| `policy_regex` | Valkey regex 정책 조회와 local cache |
| `policy_slm` | Valkey SLM 정책·group membership 조회와 local cache |

`inspection_multimodal`의 로컬 구현은 `pytesseract` OCR 동시 실행 제한을 소유한다.
전체 파일 분석 동시 실행 제한은 구현 방식과 무관하게 적용되어야 하므로
`application/services/inspection_multimodal`이 담당한다. 두 제한값의 생성과 주입은
`bootstrap/pipeline_bootstrap.py`에서만 수행한다.

adapter의 존재와 제품 활성화는 별개다. 실제로 사용할 구현체는
`bootstrap/pipeline_bootstrap.py`에서 선택한다.

## 폴더 네이밍

최상위 adapter 폴더는 domain과 동일하게 `<행동 또는 책임>_<대상>` 순서로
이름 붙인다. 대응하는 domain이 있으면 이름을 그대로 맞춘다.

```text
domain/delivery_file          <-> adapters/delivery_file
domain/inspection_multimodal  <-> adapters/inspection_multimodal
domain/inspection_regex       <-> adapters/inspection_regex
domain/inspection_slm         <-> adapters/inspection_slm
domain/policy_regex           <-> adapters/policy_regex
domain/policy_slm             <-> adapters/policy_slm
domain/notification_policy    <-> adapters/notification_policy
domain/notification_user      <-> adapters/notification_user
domain/config_aivax           <-> adapters/config_aivax
```

- `multimodal_inspection`, `regex_policy`, `policy_notification`처럼 대상부터
  시작하거나 domain과 순서가 다른 이름을 사용하지 않는다.
- `analysis_log`는 하나의 업무 용어로 취급하므로 `analysis_log_delivery`,
  `analysis_log_merge` 형태를 유지한다.
- 외부 기술은 최상위 capability 폴더명이 아니라 구현 파일과 클래스명으로
  표현한다. 예: `policy_regex/valkey_regex_policy_adapter.py`.
- JSON 응답과 요청의 필터 이름은 내부 폴더명 변경 대상이 아니다.
  `input_filter`, `secret_filter`, `file_block_filter`, `slm_filter`는 유지한다.
- 여러 도메인 구현을 `common`, `shared`, `misc` 같은 폴더에 모으지 않는다.
