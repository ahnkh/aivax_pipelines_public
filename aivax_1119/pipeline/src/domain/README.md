# 도메인 폴더

도메인 규칙, 모델, 도메인 서비스와 port를 둔다.
도메인은 controller, application, adapter와 외부 인프라 구현을 import하지 않는다.

## 하위 도메인

| 폴더 | 책임 |
|------|------|
| `analysis_log_delivery` | 완성 분석 로그, 즉시·배치 전달과 전달 대기열 port |
| `analysis_log_merge` | input/output 식별자, payload, 병합 규칙과 store port |
| `config_aivax` | AIVAX mode와 설정 entity, 설정 조회 port |
| `delivery_file` | 첨부파일 전달 요청·결과와 delivery port |
| `inspection_input` | 입력 inspector 공통 결과, 최종 판정과 inspector port |
| `inspection_multimodal` | 파일 분석 요청·결과, 워터마크 기준과 분석 port |
| `inspection_regex` | regex 검사 요청·탐지 근거·결과와 분석 port |
| `inspection_slm` | SLM 검사 요청·결과와 분석 port |
| `notification_attachment` | 보관된 첨부파일 이벤트와 outbox port |
| `notification_audit` | 외부 감사 시스템에 전달할 이벤트와 알림 port |
| `notification_policy` | 정책 탐지 이벤트와 outbox port |
| `notification_user` | 사용자 활동 이벤트와 outbox port |
| `policy_multimodal` | 멀티모달 정책·group membership snapshot과 갱신 port |
| `policy_regex` | regex 정책 snapshot과 갱신 port |
| `policy_slm` | SLM 정책·group membership snapshot과 갱신 port |

## 폴더 네이밍

최상위 도메인 폴더는 `<행동 또는 책임>_<대상>` 순서로 이름 붙인다.
같은 행동의 도메인이 정렬될 때 서로 모여 보이게 하는 것이 목적이다.

```text
domain/
  delivery_file/
  inspection_input/
  inspection_multimodal/
  inspection_regex/
  inspection_slm/
  notification_attachment/
  notification_policy/
  notification_user/
  policy_multimodal/
  policy_regex/
  policy_slm/
  config_aivax/
```

- `regex_inspection`, `user_notification`, `aivax_config`처럼 대상부터 시작하지 않는다.
- 이미 외부 계약에서 사용하는 JSON 필터 이름은 폴더 이름과 별개다.
  `input_filter`, `secret_filter`, `file_block_filter`, `slm_filter`는 변경하지 않는다.
- `analysis_log`는 하나의 업무 용어로 취급하는 예외다. 따라서
  `analysis_log_delivery`, `analysis_log_merge`처럼 `analysis_log`를 앞에 유지한다.
- 새 adapter가 같은 도메인 capability를 구현한다면 adapter 최상위 폴더도 같은
  이름과 순서를 사용한다.

## 내부 구조

```text
<domain_name>/
  models/       # Entity, Value Object, 도메인 입력·결과 모델
  ports/        # 외부 구현에 필요한 추상 계약
  services/     # 순수 도메인 규칙
  errors/       # 도메인 오류
```

필요한 책임만 만들며 빈 폴더나 범용 `shared` 폴더를 미리 만들지 않는다.
