# docs/

**Date:** 2026-10-18

| 문서 | 내용 |
|------|------|
| [API_V2.md](API_V2.md) | **V2 INPUT/OUTPUT DTO** — 제거 필드, 필수값, 간소화 응답, OUTPUT 204 |
| [swagger/README.md](swagger/README.md) | **Swagger / OpenAPI** — 명세 직접 작성, 상세 필드 설명, 현재·develop 문서 조회·검증 |
| [API_REFERENCE.md](API_REFERENCE.md) | **INPUT/OUTPUT 필드 사전** — 타입·기본값·가능한 값·action/code·디버그·CSO·오류·Health 상태 구분 |
| [API_REFERENCE_DEVELOP.md](API_REFERENCE_DEVELOP.md) | **develop b05a06be 명세** — 과거 필드·allow/block/masking·디버그·실패·우회·현재 브랜치 차이 |
| [API_JSON_STRUCTURE.md](API_JSON_STRUCTURE.md) | multiple/output JSON 예제와 최종 bulk/OpenSearch 문서 구조 |
| [ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md) | `.env` 전체 103개 변수와 추가 지원 변수의 로더 기본값·현재 `.env` 기준 권장 설정값·의미 |
| [CONTAINER_INTEGRATION_TEST.md](CONTAINER_INTEGRATION_TEST.md) | 실제 multiple API·CSO JVM·bulk_logs·Fluent Bit·OpenSearch 컨테이너 통합 테스트 |
| [INLINE_MULTIPLE_FILTER.md](INLINE_MULTIPLE_FILTER.md) | Inline 입력의 검사 순서·중단·최종 판정·동의 재개·현재 env 설정 |
| [USER_CONSENT.md](USER_CONSENT.md) | 동의 API 계약·Fluent Bit 갱신·상태 보관기간·오류 응답 |
| [MODULES.md](MODULES.md) | **코드 맵** — API · 필터 체인 · 로그 merge · UUID |
| [PIPELINE_LOAD_TEST.md](PIPELINE_LOAD_TEST.md) | worker 4개 성능 측정: 필터·INPUT/OUTPUT 병합·파일 보관·Mirror·로그/이벤트 검증 |
| [CSO_LOAD_TEST.md](CSO_LOAD_TEST.md) | 상주 JVM 4개의 CSO 조회 성능 측정 |
| [레거시 Pipeline DDD 전환 개발 결과](<refactor/개선 5. 레거시 Pipeline DDD 전환 개발 결과.md>) | `src` 전환 배경, 계층별 설계, API·검사 단위 구현 결과와 남은 작업 |
| [DEPLOY_PIPELINE_1G.md](DEPLOY_PIPELINE_1G.md) | 설치 · systemd · CPU · config 체크리스트 · 수동 실행 |
| 상암 재설치 | [`../../../mirror_engine/docs/sangam-khfc-wipe-reinstall.md`](../../../mirror_engine/docs/sangam-khfc-wipe-reinstall.md) — packaging unit의 현장 CPU 배치 확인 |
| [UI_EVENT_TIME.md](UI_EVENT_TIME.md) | **UI 전달** — 로그 목록은 `event_time` (`@timestamp`는 판정 완료). management 코드는 여기 없음 |
| [customer_install_ha_off.md](../../../inline_engine/aivax/docs/customer_install_ha_off.md) | **고객 HA 끔** — Pipeline → ai_engine 따라 설치 |

Outer 타겟 계약: [../../../docs/DEPLOY_TARGET_HOST.md](../../../docs/DEPLOY_TARGET_HOST.md)  
엔진 모드: [../../../ai_engine/docs/MODE_INLINE_MIRROR.md](../../../ai_engine/docs/MODE_INLINE_MIRROR.md)
