# Service gate (`inspect_on` / `inspect_off` / `block` / …)

**UI는 별도.** 본 문서는 Pipeline Signal → Policy Engine → metrics 경로만 다룬다.

## DB `ai_services.status` → `aivax_policy.json`

| status | enum | policy 값 | 동작 (Inline) |
|--------|------|-----------|----------------|
| 0 | OFF | `block` | 403 + block.html |
| 1 | ON | `inspect_on` | 정상 분석 |
| 2 | EXPIRED | `expired` | 현재 = block 페이지 (`service_expired:*`) · 라이선스 연동 예정 |
| 3 | INSPECT_OFF | `inspect_off` | ALLOW, Pipeline 미호출 |
| 4 | REDIRECT | `redirect` | `redirect_url` 있으면 meta-refresh HTML, 없으면 block |

`pass` / `block_redirect` 등 구 문자열은 **미지원** — 패치 시 Signal이 파일을 재생성한다.

## Signal

- Handler: `aivax/pipeline/.../aivax_policy_signal_handler.py` (`AivaxPolicySignalHandler`)
- Config key: `aivax_policy_signal_handler`
- Export only — Engine은 mtime reload.

## Metrics (서비스별)

파일: `/run/aivax/service_gate_stats.json` (`AIVAX_GATE_STATS_PATH`)

```json
{
  "ts": 1234567890.1,
  "modes": ["inspect_on","inspect_off","block","redirect","expired"],
  "total": {"inspect_on": 10, "inspect_off": 2, "block": 1, "redirect": 0, "expired": 0},
  "by_service": {
    "gpt": {"inspect_on": 8, "inspect_off": 0, "block": 1, "redirect": 0, "expired": 0},
    "gemini": {"inspect_on": 2, "inspect_off": 2, "block": 0, "redirect": 0, "expired": 0}
  }
}
```

| 모드 | 조회 |
|------|------|
| Inline | metrics scrape JSON의 `service_gate` · 파일 직접 |
| Mirror | `:9108/v1/metrics/service-gate` · `:9108/v1/metrics/current.pipeline.service_gate` · shm stats의 `service_gate` — 전체 API: [METRICS_API.md](METRICS_API.md) |
