# Pipeline 환경변수 참조

## 1. 실행 경로와 HTTP 서버 (8개)

| 환경변수 | 로더 기본값 | 권장 설정값 | 의미 |
| --- | --- | --- | --- |
| `AIVAX_PIPELINE_RUNTIME_DIR` | `.runtime/pipeline` | `./.runtime/production` | 실행 파일 기준 경로<br>정책 백업: `${AIVAX_PIPELINE_RUNTIME_DIR}/policy-cache/`<br>개별 경로 미지정 시 기본 경로 계산에 사용 |
| `AIVAX_PIPELINE_HOST` | `127.0.0.1` | `127.0.0.1` | Uvicorn 수신 주소 |
| `AIVAX_PIPELINE_PORT` | `9099` | `9099` | Uvicorn 수신 포트 (1~65535) |
| `AIVAX_PIPELINE_WORKERS` | `1` | `4` | Uvicorn worker 프로세스 수<br>2개 이상: worker별 앱 초기화, 로그 병합 backend `valkey` 필수 |
| `AIVAX_PIPELINE_CORS_ORIGINS` | `["*"]` | `*` | FastAPI CORS 허용 origin 목록 |
| `AIVAX_PIPELINE_SSL_KEY_FILE` | 미지정 (`None`) | 빈 문자열 | Uvicorn HTTPS 개인키 파일<br>인증서 파일과 쌍으로 지정 필수 |
| `AIVAX_PIPELINE_SSL_CERT_FILE` | 미지정 (`None`) | 빈 문자열 | Uvicorn HTTPS 인증서 파일<br>개인키 파일과 쌍으로 지정 필수 |
| `AIVAX_PIPELINE_MODE_FILE` | `/etc/aivax/mode.json` | `/etc/aivax/mode.json` | 제품 모드·필터 활성화 JSON 경로<br>Inline/Mirror 분기 및 `filters.*.enabled` 설정 |

## 2. 애플리케이션 로그 (2개)

| 환경변수 | 로더 기본값 | 권장 설정값 | 의미 |
| --- | --- | --- | --- |
| `AIVAX_PIPELINE_LOG_LEVEL` | `info` | `info` | 애플리케이션·Uvicorn 로그 레벨<br>허용값: `critical/error/warning/info/debug/trace` |
| `AIVAX_PIPELINE_TRACE_LOG_PATH` | `${AIVAX_PIPELINE_RUNTIME_DIR}/trace-log` | `./trace-log` | 애플리케이션 로그 디렉터리<br>`pipeline_log.txt`: 5 MiB 단위 회전, 백업 7개 유지<br>stdout 동시 출력 |

## 3. 정책 갱신 (1개)

| 환경변수 | 로더 기본값 | 권장 설정값 | 의미 |
| --- | --- | --- | --- |
| `AIVAX_PIPELINE_POLICY_POLL_INTERVAL_SECONDS` | `15`초 | `60` | Valkey 정책 snapshot 주기 갱신 간격<br>대상: 정규식·멀티모달·SLM·SecuXper<br>즉시 갱신 Stream과 병행 |

## 4. 분석 로그 파일 (2개)

| 환경변수 | 로더 기본값 | 권장 설정값 | 의미 |
| --- | --- | --- | --- |
| `AIVAX_PIPELINE_LOG_BULK_DIR` | `${AIVAX_PIPELINE_RUNTIME_DIR}/bulk` | `/home1/aivax/pipeline/bulk_logs` | OpenSearch bulk 분석 로그·동의 갱신 파일 저장 경로<br>Fluent Bit 입력 경로와 일치 필요 |
| `AIVAX_PIPELINE_LOG_BULK_BACKUP_DIR` | `${AIVAX_PIPELINE_RUNTIME_DIR}/bulk-backup` | `/data/backup/prompt_log_backup` | 원본 bulk 로그 백업 경로<br>`AIVAX_TOOLKIT_ENABLED=true` 시 백업 스케줄 생성 |

## 5. 입력·출력 분석 로그 병합 (7개)

| 환경변수 | 로더 기본값 | 권장 설정값 | 의미 |
| --- | --- | --- | --- |
| `AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_BACKEND` | `valkey` | `valkey` | INPUT/OUTPUT 병합 대기 저장소<br>`memory`: 프로세스 메모리<br>`valkey`: worker 간 공유 저장소 |
| `AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_KEY_PREFIX` | `pipeline:analysis-log-merge:v1:` | `pipeline:analysis-log-merge:v1:` | Valkey 병합 데이터·만료 인덱스 key 접두사<br>동일 인스턴스 worker 간 공유, 환경별 분리<br>`valkey` 사용 시 공백이 아닌 값 필수 |
| `AIVAX_PIPELINE_LOG_MERGE_INPUT_SWEEP_INTERVAL_SECONDS` | `30`초 | `30` | 짝 없는 INPUT/OUTPUT 만료 대상 회수 주기 |
| `AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_TTL_SECONDS` | `2400`초 | `2400` | Inline 병합 대기 데이터 TTL<br>선도착 OUTPUT 포함 |
| `AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_INPUT_FLUSH_AFTER_SECONDS` | `1800`초 | `1800` | Inline 병합 대기 로그 만료 회수 시간<br>짝 없는 INPUT: 단독 로그 전달<br>짝 없는 OUTPUT: 폐기 |
| `AIVAX_PIPELINE_MIRROR_TTL_SECONDS` | `86400`초 (24시간) | `86400` | Mirror 병합 대기 데이터 TTL<br>Inline용 TTL 대체 |
| `AIVAX_PIPELINE_MIRROR_INPUT_FLUSH_AFTER_SECONDS` | `82800`초 (23시간) | `82800` | Mirror 병합 대기 로그 만료 회수 시간<br>Inline용 flush 시간 대체<br>짝 없는 INPUT: 전달 / OUTPUT: 폐기 |

## 6. 공용 Valkey 접속 (6개)

| 환경변수 | 로더 기본값 | 권장 설정값 | 의미 |
| --- | --- | --- | --- |
| `AIVAX_PIPELINE_VALKEY_HOST` | `127.0.0.1` | `127.0.0.1` | 공용 Valkey 서버 주소<br>용도: 정책·갱신 Stream·알림·공유 토큰·로그 병합 |
| `AIVAX_PIPELINE_VALKEY_PORT` | `6379` | `6379` | Valkey 접속 포트 (1~65535) |
| `AIVAX_PIPELINE_VALKEY_DB` | `0` | `0` | Valkey 논리 DB 번호 (0 이상) |
| `AIVAX_PIPELINE_VALKEY_PASSWORD` | 빈 문자열 | 빈 문자열 | Valkey 인증 비밀번호<br>빈 값: 인증 비밀번호 미지정 (`None`) |
| `AIVAX_PIPELINE_VALKEY_SOCKET_CONNECT_TIMEOUT_MS` | `1000` ms | `1000` | Valkey 소켓 연결 제한 시간<br>클라이언트 생성 시 ms → 초 변환 |
| `AIVAX_PIPELINE_VALKEY_SOCKET_TIMEOUT_MS` | `1000` ms | `1000` | Valkey 소켓 응답 제한 시간<br>클라이언트 생성 시 ms → 초 변환<br>Stream listener: 별도 blocking 설정 병용 |

## 7. Management 이벤트와 정책 snapshot key (10개)

| 환경변수 | 로더 기본값 | 권장 설정값 | 의미 |
| --- | --- | --- | --- |
| `AIVAX_PIPELINE_EVENT_FLUSH_INTERVAL_SECONDS` | `1`초 | `1` | 첨부파일·정책 알림·사용자 동기화 outbox flush 주기 |
| `AIVAX_PIPELINE_EVENT_DEDUPE_TTL_SECONDS` | `86400`초 | `86400` | 첨부파일·정책 알림 중복 발행 방지 key TTL<br>사용자 동기화 outbox 적용 제외 |
| `AIVAX_PIPELINE_EVENT_ATTACHMENT_STREAM_NAME` | `management:file-asset:v1` | `management:file-asset:v1` | Management 첨부파일 보관 알림용 Valkey Stream 이름 |
| `AIVAX_PIPELINE_EVENT_ATTACHMENT_INDEX_NAME` | `aivax_log*` | `aivax_log*` | 첨부파일 알림의 분석 로그 인덱스 식별자<br>Stream 이름과 별도 설정 |
| `AIVAX_PIPELINE_EVENT_POLICY_NOTIFICATION_STREAM_NAME` | `management:policy-notification:v1` | `management:policy-notification:v1` | Management 정책 판정 알림용 Valkey Stream 이름 |
| `AIVAX_PIPELINE_EVENT_USER_SYNC_STREAM_NAME` | `management:user-sync:v1` | `management:user-sync:v1` | 분석 로그 사용자 식별 정보 동기화용 Valkey Stream 이름 |
| `AIVAX_PIPELINE_POLICY_REGEX_KEY` | `pipeline:policy:regex:v1` | `pipeline:policy:regex:v1` | 정규식 정책 snapshot 조회용 Valkey key |
| `AIVAX_PIPELINE_POLICY_MULTIMODAL_KEY` | `pipeline:policy:multimodal:v1` | `pipeline:policy:multimodal:v1` | 멀티모달 정책·파일 제한 snapshot 조회용 Valkey key |
| `AIVAX_PIPELINE_POLICY_SECUXPER_KEY` | `pipeline:policy:secuxper:v1` | `pipeline:policy:secuxper:v1` | SecuXper 정책 snapshot 조회용 Valkey key<br>검사 요청 `policy_code` 결정에 사용 |
| `AIVAX_PIPELINE_POLICY_SLM_KEY` | `pipeline:policy:slm:v1` | `pipeline:policy:slm:v1` | SLM 탐지 결과 적용 정책 snapshot 조회용 Valkey key |

## 8. CSO 등급 가드 (6개)

| 환경변수 | 로더 기본값 | 권장 설정값 | 의미 |
| --- | --- | --- | --- |
| `AIVAX_PIPELINE_CSO_ENABLED` | `false` | `false` | CSO 선행 가드·worker별 상주 JVM 활성화 여부<br>앱 시작 시 JVM 준비 확인<br>가드 적용: Inline 입력·동의 재전송<br>가드 호출 제외: Mirror 입력 |
| `AIVAX_PIPELINE_CSO_SKIP_UNSUPPORTED_EXTENSIONS_ENABLED` | `false` | `false` | `true`: CSO 미지원 확장자 조회 생략 |
| `AIVAX_PIPELINE_CSO_SKIP_UNTAGGED_ENABLED` | `false` | `false` | `true`: 조회 후 등급 없는 파일의 CSO 차단 생략 |
| `AIVAX_PIPELINE_CSO_SKIP_ERROR_ENABLED` | `false` | `false` | `true`: CSO 조회 오류 파일 차단 생략, 후속 처리 진행 |
| `AIVAX_PIPELINE_CSO_REQUEST_TIMEOUT_SECONDS` | `5`초 | `5` | CSO 요청 전체 제한 시간<br>포함: 직렬화 대기·장애 후 JVM 재기동·전체 파일 조회<br>파일별 개별 제한 적용 제외 |
| `AIVAX_PIPELINE_CSO_STARTUP_TIMEOUT_SECONDS` | `15`초 | `15` | 앱 시작 시 JVM 준비 대기 제한 시간 |

## 9. 사용자 동의 안내 (5개)

| 환경변수 | 로더 기본값 | 권장 설정값 | 의미 |
| --- | --- | --- | --- |
| `AIVAX_PIPELINE_USER_CONSENT_GLOBAL_BYPASS_ENABLED` | `false` | `true` | 필터별 동의 안내 공통 스위치<br>적용 조건: 공통 스위치·해당 필터 옵션 모두 `true` |
| `AIVAX_PIPELINE_USER_CONSENT_REGEX_ENABLED` | `false` | `false` | 정규식 차단 결과의 동의 안내 여부<br>공통 스위치 활성화 필요 |
| `AIVAX_PIPELINE_USER_CONSENT_MULTIMODAL_ENABLED` | `true` | `false` | 멀티모달 차단 결과의 동의 안내 여부<br>공통 스위치 활성화 필요 |
| `AIVAX_PIPELINE_USER_CONSENT_SLM_ENABLED` | `false` | `false` | SLM 차단 결과의 동의 안내 여부<br>공통 스위치 활성화 필요 |
| `AIVAX_PIPELINE_USER_CONSENT_SECUXPER_ENABLED` | `false` | `false` | SecuXper 차단 결과의 동의 안내 여부<br>공통 스위치 활성화 필요 |

## 10. 검사 동시 실행·대기 제한 (4개)

| 환경변수 | 로더 기본값 | 권장 설정값 | 의미 |
| --- | --- | --- | --- |
| `AIVAX_PIPELINE_INSPECT_SLM_MAX_INFLIGHT` | `2` | `2` | worker당 SLM 동시 검사 수<br>semaphore 제한 적용<br>SecuXper 독립 필터 적용 제외 |
| `AIVAX_PIPELINE_INSPECT_SLM_QUEUE_SKIP_AFTER` | `0` | `0` | SLM 검사 생략 기준 대기 건수<br>현재 대기 건수 ≥ 설정값: 신규 검사 생략<br>`0`: 대기 건수 기준 생략 비활성화 |
| `AIVAX_PIPELINE_INSPECT_MULTIMODAL_FILE_MAX_INFLIGHT` | `4` | `4` | worker당 멀티모달 동시 검사 호출 수<br>호출당 복수 첨부파일 포함 가능 |
| `AIVAX_PIPELINE_INSPECT_MULTIMODAL_OCR_MAX_INFLIGHT` | `2` | `2` | worker당 OCR 동시 작업 수<br>로컬 어댑터 내부 semaphore 제한 적용 |

## 11. SLM 접속과 모델 (6개)

| 환경변수 | 로더 기본값 | 권장 설정값 | 의미 |
| --- | --- | --- | --- |
| `AIVAX_PIPELINE_SLM_URL` | `http://127.0.0.1:1200/v1/chat/completions` | `http://127.0.0.1:1200/v1/chat/completions` | SLM 검사 POST URL<br>허용 scheme: `http/https` |
| `AIVAX_PIPELINE_SLM_REQUEST_TIMEOUT_SECONDS` | `10`초 | `10` | SLM HTTP 요청 제한 시간 |
| `AIVAX_PIPELINE_SLM_MODEL_VERSION` | `4` | `4` | SLM 요청·응답 규격 어댑터 선택 번호<br>`1`: MTM CPU / `2`: MTM GPU<br>`3`: WINS GPU v3 / `4`: WINS GPU v4<br>`5`: WINS SafeGuard v1.0<br>허용값: `1~5` |
| `AIVAX_PIPELINE_SLM_HTTP_MAX_CONNECTIONS` | `200` | `200` | HTTP 풀 최대 연결 수<br>SLM·SecuXper 개별 풀에 동일 제한 적용 |
| `AIVAX_PIPELINE_SLM_HTTP_MAX_KEEPALIVE` | `50` | `50` | HTTP 풀 keep-alive 연결 유지 수<br>SLM·SecuXper 개별 풀에 적용<br>`0` 허용 |
| `AIVAX_PIPELINE_SLM_HTTP_CONNECT_TIMEOUT_SECONDS` | `5`초 | `5` | SLM HTTP 연결 제한 시간<br>SecuXper 연결 제한: 별도 변수 |

## 12. 파일 검사·보관 (11개)

| 환경변수 | 로더 기본값 | 권장 설정값 | 의미 |
| --- | --- | --- | --- |
| `AIVAX_PIPELINE_FILE_BYPASS_MODE` | `false` | `false` | `true`: 로컬 멀티모달 검사 생략, 빈 결과 반환<br>CSO 선행 가드: 별도 처리 |
| `AIVAX_PIPELINE_FILE_READ_TIMEOUT_SECONDS` | `60`초 | `60` | 문서 텍스트 추출·증거 PDF 변환 제한 시간<br>전체 입력 요청·전체 OCR 작업 합산 제한 적용 제외 |
| `AIVAX_PIPELINE_FILE_ALLOWED_EXTENSIONS` | `txt, doc, docx, hwp, hwpx, pdf, ppt, pptx, xls, xlsx, bmp, jpeg, jpg, png, gif, webp, avif` | `txt,doc,docx,hwp,hwpx,pdf,ppt,pptx,xls,xlsx,bmp,jpeg,jpg,png,gif,webp,avif` | 파일 검사 기본 허용 확장자 목록<br>Management 정책 snapshot의 파일 설정 우선 적용 |
| `AIVAX_PIPELINE_FILE_MAX_SIZE_BYTES` | `26214400` bytes (25 MiB) | `26214400` | 파일 크기 기본 상한 (bytes)<br>Management 정책의 파일 설정으로 변경 가능 |
| `AIVAX_PIPELINE_UNSUPPORTED_FILE_HANDLING` | `skip` | `skip` | 미지원 확장자 파일 처리 방식<br>`skip`: 검사 생략 / `block`: 차단 |
| `AIVAX_PIPELINE_OVERSIZED_FILE_HANDLING` | `skip` | `skip` | 크기 상한 초과 파일 처리 방식<br>`skip`: 검사 생략 / `block`: 차단 |
| `AIVAX_PIPELINE_BACKUP_SKIPPED_OVERSIZED_FILES` | `true` | `true` | 용량 초과로 검사 생략한 파일의 백업·Management 등록 이벤트 생성 여부<br>`false`: 해당 파일 보관 생략<br>`block` 파일의 기존 보관 및 미지원 확장자의 `skip` 처리에는 영향 없음<br>백업 경로는 Management 공유 루트여야 하며 변경 후 Pipeline 재시작 필요 |
| `AIVAX_PIPELINE_PDF_EVIDENCE_ENGINE` | `pdfplumber` | `pdfplumber` | PDF 근거 수집 엔진<br>`pdfplumber`: 탐지 후 PDF 재분석<br>`pymupdf`: 텍스트·위치 동시 추출 결과 재사용 |
| `AIVAX_PIPELINE_FILE_CONTENT_CHUNK_SIZE` | `0` | `0` | 추출 텍스트 앞부분의 결과 저장 최대 문자 수<br>결과 필드: `content_chunk`<br>`0`: 저장 생략 |
| `AIVAX_PIPELINE_DETAIL_REASON_TEMP_DIR` | `${AIVAX_PIPELINE_RUNTIME_DIR}/detail-reason` | `./.runtime/production/detail-reason` | 탐지 근거 수집용 문서→PDF 변환 임시 경로<br>요청별 작업 디렉터리·cleanup lock에 사용 |
| `AIVAX_PIPELINE_FILE_BLOCK_BACKUP_DIR` | `${AIVAX_PIPELINE_RUNTIME_DIR}/file-backup` | `/data/backup/attach_file_backup` | CSO·멀티모달 첨부파일 보관 경로<br>보관 경로: 로그·첨부파일 알림에 포함 |

## 13. DRM·OCR 워터마크 탐지 (9개)

| 환경변수 | 로더 기본값 | 권장 설정값 | 의미 |
| --- | --- | --- | --- |
| `AIVAX_PIPELINE_WATERMARK_USE_REGEX_BASE_DRM` | `true` | `true` | 파일 헤더의 DRM 정규식 탐지 여부 |
| `AIVAX_PIPELINE_WATERMARK_USE_OCR_BASE_WATERMARK` | `true` | `true` | PDF 텍스트·OCR 결과 기반 워터마크 탐지 여부<br>대상: PDF 헤더 보유 파일 |
| `AIVAX_PIPELINE_WATERMARK_DRM_DETECT_PATTERN` | `DRMONE\s+This\s+Document\s+is\s+encrypted\s+and\s+protected\s+by\s+Fasoo\s+DRM` | `DRMONE\s+This\s+Document\s+is\s+encrypted\s+and\s+protected\s+by\s+Fasoo\s+DRM` | 파일 헤더 검색용 DRM 식별 정규식<br>UTF-8 bytes 정규식으로 컴파일 |
| `AIVAX_PIPELINE_WATERMARK_DRM_PATTERN_FLAG` | `18` | `18` | DRM 정규식 flag 비트 합 (0 이상)<br>기본: `IGNORECASE(2) + DOTALL(16)` |
| `AIVAX_PIPELINE_WATERMARK_DRM_FILE_HEADER_LIMIT` | `8096` bytes | `8096` | DRM 탐지용 파일 선두 최대 읽기 크기 (bytes) |
| `AIVAX_PIPELINE_WATERMARK_OCR_PATTERNS` | `(미\s*공\s*개)`<br>`(특\s*허)`<br>`(외\s*부)`<br>`(반\s*출)`<br>`(외부\|사외).{0,20}(반출\|배포)` | `["(미\\s*공\\s*개)", "(특\\s*허)", "(외\\s*부)", "(반\\s*출)", "(외부\|사외).{0,20}(반출\|배포)"]` | PDF 페이지 텍스트·OCR 결과 검색용 정규식 목록 |
| `AIVAX_PIPELINE_WATERMARK_OCR_PATTERN_FLAG` | `18` | `18` | OCR 정규식 flag 비트 합 (0 이상)<br>기본: `IGNORECASE + DOTALL` |
| `AIVAX_PIPELINE_WATERMARK_OCR_MAX_HITCOUNT` | `2` | `2` | 페이지당 일치 패턴 수 탐지 임계값<br>일치한 서로 다른 패턴 수 ≥ 설정값: 탐지<br>동일 패턴 반복 출현: 중복 집계 제외 |
| `AIVAX_PIPELINE_WATERMARK_OCR_PAGES` | `[0]` | `0` | OCR 검사 페이지 번호 목록 (0부터 시작)<br>0 이상 정수, 빈 목록 불가<br>문서 페이지 수 ≤ 지정 목록 길이: 첫 페이지로 대체 |

## 14. 최종 판정 우회·마스킹 (2개)

| 환경변수 | 로더 기본값 | 권장 설정값 | 의미 |
| --- | --- | --- | --- |
| `AIVAX_PIPELINE_SSL_PROXY_BYPASS_BITMASK` | `0` | `0` | 일반 필터 최종 판정 우회 비트 (0 이상)<br>`1`: block / `2`: masking / `3`: 둘 다<br>하위 2개 비트 사용 |
| `AIVAX_PIPELINE_MASK_TOKEN` | `[AIVAX MASKING]` | `[AIVAX MASKING]` | 정규식·SLM·secuXper 검사 및 최종 결과 병합의 마스킹 문구<br>정규식 정책 테스트에도 적용<br>미지정 시 기본값 사용, 빈 값·공백만 지정 시 설정 오류 |

## 15. nNetCDS 로그 전달 (10개)

| 환경변수 | 로더 기본값 | 권장 설정값 | 의미 |
| --- | --- | --- | --- |
| `AIVAX_PIPELINE_CDS_DELIVERY_ENABLED` | `false` | `false` | CDS queue·HTTP·감사로그·전송 스케줄러 활성화 여부<br>활성 시 bulk 전달과 CDS queue 적재 병행 |
| `AIVAX_PIPELINE_CDS_LOG_DIR` | `${AIVAX_PIPELINE_RUNTIME_DIR}/cds-logs` | `.runtime/production/cds-logs` | CDS 전송 대기 로그의 로컬 spool 경로 |
| `AIVAX_PIPELINE_CDS_DELIVERY_INTERVAL_SECONDS` | `5`초 | `5` | 대기 CDS 로그 전송 스케줄 간격 |
| `AIVAX_PIPELINE_CDS_DELIVERY_BATCH_SIZE` | `1000` | `1000` | CDS 요청당 최대 전송 로그 수 (1~1000) |
| `AIVAX_PIPELINE_CDS_API_URL` | 미지정 (`None`) | `https://20.0.0.10:8443/api/ext/v1/traffic-logs` | CDS 로그 수신 POST URL<br>CDS 활성 시 유효한 `http/https` URL 필수 |
| `AIVAX_PIPELINE_CDS_API_KEY` | 빈 문자열 | 인증값 별도 설정 | CDS 요청 `X-API-Key` 인증값<br>CDS 활성 시 공백이 아닌 값 필수 |
| `AIVAX_PIPELINE_CDS_REQUEST_TIMEOUT_SECONDS` | `10`초 | `10` | CDS HTTP POST 제한 시간<br>스케줄러 종료 대기 시간 계산에 사용 |
| `AIVAX_PIPELINE_CDS_SRC_PORT` | `0` | `0` | CDS 로그 payload 출발지 포트 (0~65535)<br>HTTP 소켓 bind 설정 적용 제외 |
| `AIVAX_PIPELINE_CDS_DST_HOST` | `example.com` | `example.com` | CDS 로그 payload 목적지 host<br>전송 서버: `CDS_API_URL`로 지정<br>CDS 활성 시 공백이 아닌 1~255자 값 필수 |
| `AIVAX_PIPELINE_CDS_DST_PORT` | `443` | `443` | CDS 로그 payload 목적지 포트 (0~65535) |

## 16. Management 내부 API (2개)

| 환경변수 | 로더 기본값 | 권장 설정값 | 의미 |
| --- | --- | --- | --- |
| `AIVAX_PIPELINE_MANAGEMENT_API_URL` | `http://127.0.0.1:3000` | `http://127.0.0.1:3001` | CDS 전송 실패 감사로그 수신용 Management 기본 URL<br>POST 경로: `/api/v1/internal/audits` |
| `AIVAX_PIPELINE_MANAGEMENT_REQUEST_TIMEOUT_SECONDS` | `3`초 | `3` | Management 감사로그 HTTP 요청 제한 시간<br>CDS 스케줄러 종료 대기 시간 계산에 사용 |

## 17. SecuXperAI 및 toolkit (15개)

### SecuXperAI (14개)

| 환경변수 | 로더 기본값 | 권장 설정값 | 의미 |
| --- | --- | --- | --- |
| `AIVAX_PIPELINE_SECUXPERAI_BASE_URL` | `https://agentpoc-dev.secuxperai.com` | `https://agentpoc-dev.secuxperai.com` | SecuXper 인증·갱신·검사 API 기본 URL<br>개별 API path와 결합 |
| `AIVAX_PIPELINE_SECUXPERAI_FILTER_PATH` | `/chat/filter/` | `/chat/filter/` | SecuXper 프롬프트 검사 API path |
| `AIVAX_PIPELINE_SECUXPERAI_TOKEN_PATH` | `/auth/token` | `/auth/token` | SecuXper access token 신규 발급 API path<br>인증 정보: `TEAM_UUID`, `CLIENT_NO` |
| `AIVAX_PIPELINE_SECUXPERAI_REFRESH_PATH` | `/auth/token/refresh` | `/auth/token/refresh` | SecuXper access token 갱신 API path<br>인증 정보: 기존 Bearer token |
| `AIVAX_PIPELINE_SECUXPERAI_TEAM_UUID` | 빈 문자열 | 인증값 별도 설정 | 토큰 발급 요청 body의 `team_uuid`<br>SecuXper 필터 활성 시 필수 |
| `AIVAX_PIPELINE_SECUXPERAI_CLIENT_NO` | 빈 문자열 | 인증값 별도 설정 | 토큰 발급 요청 body의 `client_no`<br>SecuXper 필터 활성 시 필수 |
| `AIVAX_PIPELINE_SECUXPERAI_SITE_CODE` | `test-wt` | `test-wt` | 검사 요청 body의 `site_code`<br>사이트별 발급 정보 기준 지정 |
| `AIVAX_PIPELINE_SECUXPERAI_SYNC_MODE` | 문자열 `"true"` | `true` | 검사 요청 body의 `sync_mode` 문자열<br>인증 정보 지정 시 허용값: 소문자 `true/false` |
| `AIVAX_PIPELINE_SECUXPERAI_TOKEN_KEY` | `aivax:secuxperai:access-token:v1` | `aivax:secuxperai:access-token:v1` | worker 간 access token 공유용 Valkey key<br>토큰 문자열 직접 지정 용도 제외 |
| `AIVAX_PIPELINE_SECUXPERAI_TOKEN_LOCK_KEY` | `aivax:secuxperai:access-token-lock:v1` | `aivax:secuxperai:access-token-lock:v1` | 토큰 발급·갱신 중복 방지용 Valkey lock key<br>토큰 저장 key와 분리 필수 |
| `AIVAX_PIPELINE_SECUXPERAI_TOKEN_LOCK_TTL_SECONDS` | `10`초 | `10` | 토큰 교체 lock TTL |
| `AIVAX_PIPELINE_SECUXPERAI_TOKEN_WAIT_SECONDS` | `2`초 | `2` | lock 획득 실패 시 다른 worker의 토큰 교체 대기 시간<br>초과 시 현재 요청의 자체 복구 시도 |
| `AIVAX_PIPELINE_SECUXPERAI_REQUEST_TIMEOUT_SECONDS` | `10`초 | `10` | 토큰 발급·갱신·검사 HTTP 요청 제한 시간 |
| `AIVAX_PIPELINE_SECUXPERAI_CONNECT_TIMEOUT_SECONDS` | `5`초 | `5` | SecuXper HTTP 연결 제한 시간 |

### Toolkit 이관 스케줄러 (1개)

| 환경변수 | 로더 기본값 | 권장 설정값 | 의미 |
| --- | --- | --- | --- |
| `AIVAX_TOOLKIT_ENABLED` | `false` | `false` | 이관 파일 정리·bulk 백업 스케줄러 활성화 여부<br>Pipeline `CleanupCron` 제어<br>임시파일 정리: 시작 시·60초 간격<br>bulk 백업: 한국 시간 매시 5분 |

## 18. `.env`에 없는 추가 지원 변수

| 환경변수 | 로더 기본값 | 권장 설정값 | 의미 |
| --- | --- | --- | --- |
| `AIVAX_PIPELINE_ENV_FILE` | `.env` | 미지정 (`.env` 미선언) | 환경 파일 선택 경로<br>실행 프로세스 환경에서 지정<br>명시 파일 부재: `ConfigurationError`<br>기본 `.env` 부재: 프로세스 환경·기본값으로 구성 |
| `AIVAX_PIPELINE_POLICY_REFRESH_STREAM_NAME` | `pipeline:policy:refresh:v1` | 미지정 (`.env` 미선언) | 정책 즉시 갱신 신호 발행·수신용 Stream 이름<br>공백이 아닌 값 필수 |
| `AIVAX_PIPELINE_POLICY_REFRESH_STREAM_BLOCK_MS` | `500` ms | 미지정 (`.env` 미선언) | 정책 갱신 listener의 `XREAD` blocking 대기 시간<br>1 ms 이상 |
