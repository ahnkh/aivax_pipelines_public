# Mirror Metrics API — 웹 개발자용 스펙

Mirror 모드 UI용 **읽기 전용** JSON API입니다.  
프로세스: `aivax-engine-metrics` → `core/metrics_api.py`  
기본 바인드: `http://127.0.0.1:9108` (외부 노출 시 reverse proxy + 인증 필수)

JSON Schema: [`schemas/metrics-api.schema.json`](../schemas/metrics-api.schema.json)

---

## 엔드포인트

| Method | Path | 용도 | 권장 폴링 |
|--------|------|------|-----------|
| GET | `/v1/metrics/health` | 기동·소스 가용성 | 30–60s |
| GET | `/v1/metrics/current` | **대시보드 메인** (NIC + drops + pipeline) | **1–2s** (차트), 5s (카드) |
| GET | `/v1/metrics/pipeline` | AI 이벤트 파이프라인만 | 2–5s |
| GET | `/v1/metrics/history?window=1h&step=5` | 서버측 히스토리 포인트 | 차트 초기 로드 1회 + 필요 시 |
| GET | `/v1/metrics/service-gate` | 서비스 게이트 카운터만 | 5–10s |

CORS: `Access-Control-Allow-Origin: *` (로컬 UI 개발 편의). 프로덕션은 프록시에서 제한하세요.

---

## 폴링 가이드

| UI 요소 | 소스 | 주기 | 비고 |
|---------|------|------|------|
| pps / Mbps 실시간 선 | `current.port.pps` / `mbps` | 1s | `valid=false`면 표시 숨김 |
| drop rate 게이지 | `current.drops.headline.drop_rate` | 1–2s | `headline.metric_valid` 확인 |
| 서비스별 AI 이벤트 | `current.pipeline.ai_service` | 2–5s | 누적 카운터 |
| EPS | `pipeline.eps_instant` / `eps_avg` | 2s | instant는 ~0.5s 윈도우 |
| 히스토리 차트 | `history` 또는 클라이언트가 `current` 누적 | 초기 `history`, 이후 `current` append | 서버 history는 pipeline 미포함(경량) |

**누적 vs 레이트:** `packets_total` / `pmd_imissed_total` / `ai_service.*` 는 **누적**.  
그래프용 속도는 `pps`, `mbps`, `window_*`, `headline.drop_rate` 를 쓰세요.

---

## `GET /v1/metrics/current`

### 요약 필드

| 필드 | 타입 | 설명 |
|------|------|------|
| `sampled_at` | string (UTC ISO) | 샘플 시각 |
| `sampled_at_unix` | number | epoch seconds |
| `sensor_boot_id` | string | 센서 부트 식별 (재시작 시 변경) |
| `port` | object | NIC/DPDK 처리량 |
| `drops` | object | 계층별 드롭 + headline |
| `pipeline` | object | SHM consumer + export (아래 pipeline과 동일 스키마) |
| `ai_classified_flow` | object | (옵션) Suricata flow 기반 AI 비율 — 피드 연결 시 의미 있음 |
| `counter_support` | object | 어떤 카운터가 유효한지 |
| `flow_cardinality` | number | 내부 flow 스냅샷 수 |

### `port`

| 필드 | 타입 | 설명 |
|------|------|------|
| `valid` | bool | false면 NIC 카드 숨김/경고 |
| `source` | string | `dpdk_telemetry` \| `nic_sysfs_accepted` |
| `packets_total` | number | 누적 RX packets (`ipackets` 또는 sysfs) |
| `bytes_total` | number | 누적 RX bytes |
| `pps` | number | 직전 샘플 구간 패킷/초 |
| `bps` | number | 직전 샘플 구간 bit/초 |
| `mbps` | number | `bps / 1e6` |
| `window_seconds` | number | 레이트 산출에 쓴 구간 길이 |
| `window_packets` / `window_bytes` / `window_dropped` | number | 구간 delta |
| `interfaces[]` | array | 포트/IF별 상세 |

DPDK(미러 기본)일 때 `source=dpdk_telemetry` 이고 sysfs IF는 커널에 없을 수 있습니다.

### `drops`

| 필드 | 타입 | 설명 |
|------|------|------|
| `pmd_imissed_total` | number\|null | DPDK HW/PMD miss 누적 |
| `pmd_rx_nombuf_total` | number\|null | mbuf 부족 누적 |
| `pmd_imissed_rate` | number\|null | 구간 miss% |
| `pmd_imissed_rate_valid` | bool | |
| `window_imissed` | number\|null | 구간 Δimissed |
| `nic_ingress_drop_*` | … | sysfs 모드일 때만 채움 |
| `export_queue_drop_total` | number | 앱 계층 export drop (내부 주입 시) |
| `headline` | object | **UI 단일 게이지용** |

#### `drops.headline`

| 필드 | 설명 |
|------|------|
| `metric_valid` | false면 게이지 비활성 |
| `drop_rate` | % (0–100) |
| `capture_drop_packets_total` | 구간 drop 패킷 수 |
| `drop_scope` | `pmd_rx` (DPDK) \| `nic_ingress` (sysfs) |
| `formula_id` | DPDK: `delta_imissed/(delta_ipackets+delta_imissed)` |
| `accepted_source` / `drop_source` | 출처 라벨 |

---

## `GET /v1/metrics/pipeline`

`current.pipeline` 과 동일 스키마.

| 필드 | 타입 | 설명 |
|------|------|------|
| `valid` | bool | shm_consumer_stats 읽기 성공 |
| `path` | string | 읽은 파일 경로 |
| `age_s` | number\|null | 파일 `ts` 기준 초 |
| `processed` | number | 누적 처리 이벤트 |
| `eps_instant` | number | 순간 EPS |
| `eps_avg` | number | 기동 이후 평균 EPS |
| `workers` / `n_rings` | number | |
| `ring_processed` / `ring_errors` / `worker_processed` | number[] | 링/워커 분배 |
| `http_version` | object | `{ "1.1", "2", "other" }` 누적 |
| `ai_service` | object | `{ chatgpt, gemini, claude, … }` 누적 |
| `service_gate` | object | inspect_on/off/block/… ([SERVICE_GATE.md](SERVICE_GATE.md)) |
| `export` / `export_valid` / `export_path` | … | Suricata plugin export stats (있으면) |
| `consumer` | object | 원본 shm_consumer_stats JSON |

---

## `GET /v1/metrics/history`

Query:

| 파라미터 | 기본 | 설명 |
|----------|------|------|
| `window` | `1h` | `30s` / `5m` / `1h` / 초 단위 숫자 |
| `step` | `5` | 포인트 간격(초, 1Hz 샘플러 기준 다운샘플) |

응답: `{ "points": [ … current에서 pipeline 제외한 샘플 … ], "window", "step", "count" }`  
최대 포인트: 10000. 초과 요청은 400.

클라이언트가 1s로 `current`를 폴링해 자체 버퍼링하는 방식을 권장합니다. `history`는 새로고침 직후 백필용입니다.

---

## `GET /v1/metrics/health`

| 필드 | 설명 |
|------|------|
| `ok` | 프로세스 생존 |
| `collector_fresh` | 샘플이 한 번이라도 쌓였는지 |
| `last_sample` | 마지막 샘플 시각 |
| `dpdk_telemetry` | telemetry 소켓 경로 또는 null |
| `pipeline_stats_valid` / `pipeline_stats_path` | SHM consumer stats |
| `counter_support` | `{ nic_sysfs, dpdk_imissed, dpdk_rx_nombuf, shm_consumer_stats, … }` |

---

## 예제 (`current` 발췌)

```json
{
  "sampled_at": "2026-09-11T06:22:00Z",
  "port": {
    "valid": true,
    "source": "dpdk_telemetry",
    "pps": 5973.0,
    "mbps": 25.7,
    "packets_total": 147777843,
    "bytes_total": 113249843611
  },
  "drops": {
    "pmd_imissed_total": 2466098,
    "window_imissed": 0,
    "pmd_imissed_rate": 0.0,
    "pmd_imissed_rate_valid": true,
    "headline": {
      "metric_valid": true,
      "drop_rate": 0.0,
      "drop_scope": "pmd_rx",
      "drop_source": "dpdk_imissed",
      "formula_id": "delta_imissed/(delta_ipackets+delta_imissed)"
    }
  },
  "pipeline": {
    "valid": true,
    "eps_avg": 0.2,
    "processed": 2441,
    "ai_service": { "gemini": 684, "chatgpt": 891 },
    "http_version": { "1.1": 1761, "2": 0 },
    "service_gate": {
      "total": { "inspect_on": 1148, "inspect_off": 0, "block": 0 }
    }
  },
  "counter_support": {
    "dpdk_imissed": true,
    "dpdk_rx_nombuf": true,
    "shm_consumer_stats": true
  }
}
```

---

## 환경 변수

| 변수 | 기본 | 의미 |
|------|------|------|
| `AIVAX_METRICS_BIND` | `127.0.0.1` | bind address |
| `AIVAX_METRICS_PORT` | `9108` | listen port |
| `AIVAX_METRICS_PREFER_DPDK` | `1` | DPDK 우선 (실패 시 sysfs) |
| `AIVAX_METRICS_DPDK_PORT` | `0` | ethdev port id |
| `DPDK_TELEMETRY` / `AIVAX_DPDK_TELEMETRY` | auto | telemetry 소켓 |
| `AIVAX_SHM_CONSUMER_STATS` | `/run/aivax/shm_consumer_stats.json` | pipeline 파일 |
| `AIVAX_EXPORT_STATS` | (후보 목록) | export stats 파일 |
| `AIVAX_METRICS_IFS` | `MIRROR_IF` | sysfs fallback IF 목록(쉼표) |

---

## 연동 체크리스트 (프론트)

1. `health` → `ok` / `dpdk_telemetry` / `pipeline_stats_valid`
2. `current.port.valid` 가 true일 때만 NIC 차트 표시
3. drop 게이지는 `drops.headline.metric_valid` + `drop_rate`
4. 서비스 파이차트는 `pipeline.ai_service` (누적 → 비율은 클라이언트에서 sum 기준)
5. 프록시는 management backend에서 `127.0.0.1:9108` 로만 전달

로컬 확인:

```bash
curl -s http://127.0.0.1:9108/v1/metrics/health | python3 -m json.tool
curl -s http://127.0.0.1:9108/v1/metrics/current | python3 -m json.tool | head -80
curl -s http://127.0.0.1:9108/v1/metrics/pipeline | python3 -m json.tool | head -60
```
