# 애플리케이션 계층

외부 요청 하나를 완성하는 유스케이스와 여러 유스케이스가 공유하는 실행 로직을
분리한다.

## 하위 폴더

| 폴더 | 역할 |
|------|------|
| `use_cases/input_analysis` | `multiple_filter`의 inline/mirror 실행, 검사 순서, 최종 판정, 입력 로그 처리 |
| `use_cases/output_analysis` | `output_filter` 출력 로그 생성과 병합 요청 |
| `use_cases/test_filter_rule` | 운영 cache를 바꾸지 않는 regex·멀티모달·SLM 정책 테스트 |
| `use_cases/refresh_filter_policy` | 정책 전체 즉시 갱신 |
| `use_cases/refresh_policy_*` | 정책 종류별 cache 갱신 한 번 수행 |
| `use_cases/expire_analysis_log_merge` | 만료된 미매칭 분석 로그 회수 한 번 수행 |
| `use_cases/send_pending_cds_logs` | CDS 대기열의 분석 로그를 배치 전송하고 성공 항목 제거·실패 감사 기록 |
| `services/analysis_log_merge` | 여러 진입점이 공유하는 입력·출력 로그 병합과 주·보조 전달 port 호출 |
| `services/inspection_multimodal` | 멀티모달 정책과 분석 port 조합 |
| `services/inspection_slm` | SLM 실행 제한, 정책 적용과 결과 조합 |
| `services/notification_attachment` | 완성 로그에서 백업 파일을 찾아 첨부파일 알림 outbox에 적재 |

첨부파일 알림 service는 input 직접 전달과 input/output 병합 전달에서 함께 사용한다.
실제 Stream 발행과 재시도는 adapter의 outbox가 담당한다.

## 폴더 구조

```text
application/
  use_cases/
    <use_case>/
      use_case.py
      <use-case 전용 collaborator>.py
  services/
    <행동>_<대상>/
      service.py
```

## `use_cases`

- HTTP·cron 등 하나의 외부 요구사항을 완료한다.
- 실행 순서, 중단 조건, 로그 전송과 같은 유스케이스별 부수 효과를 조정한다.
- 진입점은 `use_case.py`, 클래스는 `<업무 이름>UseCase`, 메서드는 `execute()`로 한다.
- controller는 하나의 use case만 호출한다.
- 특정 유스케이스에서만 쓰는 mapper, builder, inspector는 해당 폴더에 둔다.

## `services`

- 두 개 이상의 유스케이스에서 재사용할 수 있는 application 로직을 둔다.
- domain 요청을 조립하고 port를 호출하지만 HTTP DTO와 adapter 구현에는 의존하지 않는다.
- 단순히 port 호출만 전달하는 wrapper는 만들지 않는다.

## 의존 방향

```text
controller -> application/use_cases -> application/services -> domain/ports
adapter -> domain/ports
bootstrap -> 모든 구현 조립
```

domain은 application을 import하지 않고, application은 구체 adapter를 import하지 않는다.

## 멀티모달 분석

`MultimodalInspectionService`는 현재 정책 또는 테스트용 임시 정책을 선택하고
프로세스별 파일 분석 동시 실행 제한을 적용한 뒤
`MultimodalInspectionPort.inspect()`를 호출한다. 로컬·외부 HTTP 분석 방식은 port
구현 교체로 결정한다. OCR처럼 특정 구현 라이브러리 안에서만 발생하는 세부 실행
제한은 해당 adapter가 소유한다. input-analysis 전용 JSON 조립과
`FileDeliveryPort` 호출은 `use_cases/input_analysis`에 두며 공용 분석 서비스에
넣지 않는다.

## SLM 분석

`SlmInspectionService`는 SLM 동시 실행 제한을 적용하고, 모델별 추론 adapter의
결과와 현재 `policy_slm` snapshot을 조합한다. `SlmInputInspector`는 이 공용 결과를
`multiple_filter`의 `InputInspectionResult`로 변환한다. SLM HTTP 응답 해석은
`adapters/inspection_slm`, 정책 조회와 cache는 `adapters/policy_slm`이 담당한다.

## 필터 규칙 테스트

`use_cases/test_filter_rule` 은 `/v1/filter/testrule` 요청 한 건을 완료한다.
regex와 멀티모달·SLM 공용 분석 로직을 재사용하되, 요청으로 받은 테스트 정책은
해당 실행에만 적용하여 프로세스의 운영 정책 cache를 교체하지 않는다.
테스트 요청의 `policies`에 포함된 필터만 regex → multimodal → SLM 순서로 실행한다.
앞선 필터가 차단하면 이후 필터는 실행하지 않는다.
각 필터 결과에는 실행 정책과 소요 시간, 가능한 경우 탐지 문자열·위치·페이지 등의
근거를 포함한다. 첨부파일의 임시 저장과 정리는 controller가 담당하며 use case는
전달받은 파일 경로를 운영 멀티모달 분석 서비스에 그대로 넘긴다.

## 필터 정책 즉시 갱신

Management는 정책 snapshot을 Valkey에 저장한 뒤 `pipeline:policy:refresh:v1`
Stream에 갱신 신호를 발행한다. 각 Pipeline worker는 독립 cursor로 모든 이벤트를
읽어 regex·multimodal·SLM 로컬 cache를 갱신한다. `POST /v1/filter/policy-refresh`는
기존 호출자 호환을 위해 같은 Stream에 신호를 발행하며, 즉시 갱신 결과를 반환하지
않는다. 주기 refresh는 Stream 유실·일시 장애에 대한 fallback이다.

## 분석 로그

- `services/analysis_log_merge`는 input/output/expiry가 공유하는 병합·전달 로직이다.
- `use_cases/output_analysis`는 `/v1/filter/output_filter` 요청 하나를 처리한다.
- `use_cases/expire_analysis_log_merge`는 cron이 호출하는 만료 회수 한 번을 처리한다.
- `use_cases/send_pending_cds_logs`는 실행 시작 시점의 CDS 대기 로그 전체를 최대
  1,000건씩 나눠 전송한다. HTTP 500만 최초 요청 이후 세 번까지 재시도하고,
  그 밖의 오류와 재시도 소진 항목은 파일 queue의 `failed`로 격리한다.
