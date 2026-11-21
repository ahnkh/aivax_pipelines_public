"""Process-wide Pipeline runtime configuration."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

from src.domain.inspection_input.models.masking import DEFAULT_MASK_TOKEN


class ConfigurationError(ValueError):
    """A Pipeline execution setting is missing or invalid."""


DEFAULT_FILE_EXTENSIONS = (
    "txt",
    "doc",
    "docx",
    "hwp",
    "hwpx",
    "pdf",
    "ppt",
    "pptx",
    "xls",
    "xlsx",
    "bmp",
    "jpeg",
    "jpg",
    "png",
    "gif",
    "webp",
    "avif",
)
DEFAULT_OCR_PATTERNS = (
    r"(미\s*공\s*개)",
    r"(특\s*허)",
    r"(외\s*부)",
    r"(반\s*출)",
    r"(외부|사외).{0,20}(반출|배포)",
)


@dataclass(frozen=True, slots=True)
class PipelineSettings:
    """
    Pipeline 전역에서 사용하는 변수를 정의한다.
    각 변수는 환경변수를 통해서 조절할 수 있다.

    분석 로그 경로 변경해서 fluent bit의 설정 파일도 수정해야함.
    """

    # Pipeline runtime / HTTP server
    runtime_dir: str = ".runtime/pipeline"
    host: str = "127.0.0.1"
    port: int = 9099
    workers: int = 1
    cors_origins: tuple[str, ...] = ("*",)
    ssl_key_file: str | None = None
    ssl_cert_file: str | None = None
    product_mode_file: str = "/etc/aivax/mode.json"
    mask_token: str = DEFAULT_MASK_TOKEN

    # Application process logs (stdout + trace files)
    log_level: str = "info"
    trace_log_path: str = "./trace-log"

    # Policy refresh
    policy_poll_interval_seconds: int = 15
    policy_refresh_stream_name: str = "pipeline:policy:refresh:v1"
    policy_refresh_stream_block_ms: int = 500

    # Analysis logs (OpenSearch-compatible bulk files)
    log_bulk_dir: str = "/home1/aivax/pipeline/bulk_logs"
    toolkit_enabled: bool = False
    log_bulk_backup_dir: str = "/data/backup/prompt_log_backup"

    # nNetCDS delivery
    cds_delivery_enabled: bool = False
    cds_log_dir: str = ".runtime/pipeline/cds-logs"
    cds_delivery_interval_seconds: int = 5
    cds_delivery_batch_size: int = 1_000

    cds_api_url: str | None = None
    cds_api_key: str = ""
    cds_request_timeout_seconds: int = 10

    cds_src_port: int = 0
    cds_dst_host: str = "example.com"
    cds_dst_port: int = 443

    # Management internal API
    management_api_url: str = "http://127.0.0.1:3000"
    management_request_timeout_seconds: int = 3

    # Analysis log correlation
    analysis_log_merge_backend: str = "valkey"
    analysis_log_merge_key_prefix: str = "pipeline:analysis-log-merge:v1:"
    log_merge_input_sweep_interval_seconds: int = 30
    analysis_log_merge_ttl_seconds: int = 2_400
    analysis_log_merge_input_flush_after_seconds: int = 1_800
    # 입력 검사가 병합 창(기본 30분)을 넘겨도 응답을 보관하는 시간.
    analysis_log_merge_late_output_ttl_seconds: int = 43_200
    mirror_ttl_seconds: int = 86_400
    mirror_input_flush_after_seconds: int = 82_800

    # Valkey connection shared by Management event streams
    valkey_host: str = "127.0.0.1"
    valkey_port: int = 6_379
    valkey_db: int = 0
    valkey_password: str = ""
    valkey_socket_connect_timeout_ms: int = 1_000
    valkey_socket_timeout_ms: int = 1_000

    # Management event streams
    event_flush_interval_seconds: int = 1
    event_dedupe_ttl_seconds: int = 86_400
    event_attachment_stream_name: str = "management:file-asset:v1"
    event_attachment_index_name: str = "aivax_log*"
    event_policy_notification_stream_name: str = "management:policy-notification:v1"
    event_user_sync_stream_name: str = "management:user-sync:v1"

    # Management policy snapshots in Valkey
    policy_regex_key: str = "pipeline:policy:regex:v1"
    policy_multimodal_key: str = "pipeline:policy:multimodal:v1"
    policy_secuxper_key: str = "pipeline:policy:secuxper:v1"
    policy_slm_key: str = "pipeline:policy:slm:v1"
    ai_service_catalog_key: str = "pipeline:ai-service:catalog:v1"

    # SLM inspection execution limit
    inspect_slm_max_inflight: int = 2
    # 0이면 대기열을 제한하지 않는다. 기본값은 느린 SLM이 병합 창을 넘기지 않게 한다.
    inspect_slm_queue_skip_after: int = 32

    # Multimodal inspection execution limits (per worker process)
    inspect_multimodal_file_max_inflight: int = 4
    inspect_multimodal_ocr_max_inflight: int = 2
    # 0이면 파일 슬롯을 제한 없이 기다린다.
    inspect_multimodal_acquire_timeout_seconds: int = 300

    # CSO grade lookup (independent of multimodal filters)
    cso_enabled: bool = False
    cso_provider: str = "fasoo"
    cso_skip_unsupported_extensions_enabled: bool = False
    cso_skip_untagged_enabled: bool = False
    cso_skip_error_enabled: bool = False
    cso_request_timeout_seconds: int = 5
    cso_startup_timeout_seconds: int = 15
    user_consent_global_bypass_enabled: bool = False
    user_consent_regex_enabled: bool = False
    user_consent_multimodal_enabled: bool = True
    user_consent_slm_enabled: bool = False
    user_consent_secuxper_enabled: bool = False

    # SLM inspection
    slm_url: str = "http://127.0.0.1:1200/v1/chat/completions"
    slm_request_timeout_seconds: int = 10
    slm_model_version: int = 4
    slm_http_max_connections: int = 200
    slm_http_max_keepalive: int = 50
    slm_http_connect_timeout_seconds: int = 5

    # SecuXperAI
    secuxperai_base_url: str = "https://agentpoc-dev.secuxperai.com"
    secuxperai_filter_path: str = "/chat/filter/"
    secuxperai_token_path: str = "/auth/token"
    secuxperai_refresh_path: str = "/auth/token/refresh"
    secuxperai_team_uuid: str = ""
    secuxperai_client_no: str = ""
    secuxperai_site_code: str = "test-wt"
    secuxperai_sync_mode: str = "true"
    secuxperai_token_key: str = "aivax:secuxperai:access-token:v1"
    secuxperai_token_lock_key: str = "aivax:secuxperai:access-token-lock:v1"
    secuxperai_token_lock_ttl_seconds: int = 10
    secuxperai_token_wait_seconds: int = 2
    secuxperai_request_timeout_seconds: int = 10
    secuxperai_connect_timeout_seconds: int = 5

    # Multi modal / watermark detection
    file_bypass_mode: bool = False
    file_read_timeout_seconds: int = 60
    file_allowed_extensions: tuple[str, ...] = DEFAULT_FILE_EXTENSIONS
    file_max_size_bytes: int = 26_214_400
    unsupported_file_handling: str = "skip"
    oversized_file_handling: str = "skip"
    backup_skipped_oversized_files: bool = True
    pdf_evidence_engine: str = "pdfplumber"
    file_content_chunk_size: int = 0
    detail_reason_temp_dir: str = ".runtime/pipeline/detail-reason"
    file_block_backup_dir: str = "/data/backup/attach_file_backup"
    watermark_use_regex_base_drm: bool = True
    watermark_use_ocr_base_watermark: bool = True
    watermark_drm_detect_pattern: str = (
        r"DRMONE\s+This\s+Document\s+is\s+encrypted"
        r"\s+and\s+protected\s+by\s+Fasoo\s+DRM"
    )
    watermark_drm_pattern_flag: int = 18
    watermark_drm_file_header_limit: int = 8_096
    watermark_ocr_patterns: tuple[str, ...] = DEFAULT_OCR_PATTERNS
    watermark_ocr_pattern_flag: int = 18
    watermark_ocr_max_hitcount: int = 2
    watermark_ocr_pages: tuple[int, ...] = (0,)

    # Filter runtime behavior
    ssl_proxy_bypass_bitmask: int = 0


class EnvironmentLoader:
    """Load one environment file and produce a validated settings snapshot."""

    ENV_FILE_VARIABLE = "AIVAX_PIPELINE_ENV_FILE"
    _LOG_LEVELS = frozenset({"critical", "error", "warning", "info", "debug", "trace"})
    _PRODUCT_MODES = frozenset({"inline", "mirror", "proxy"})
    _FILE_HANDLING = frozenset({"skip", "block"})
    _PDF_EVIDENCE_ENGINES = frozenset({"pdfplumber", "pymupdf"})
    _CSO_PROVIDERS = frozenset({"fasoo", "markany"})
    _ANALYSIS_LOG_MERGE_BACKENDS = frozenset({"memory", "valkey"})

    def __init__(
        self, root_dir: Path | None = None, environ: Mapping[str, str] | None = None
    ) -> None:
        # 설정 파일은 bootstrap 패키지 안이 아니라 repository root에 둔다.
        self._root_dir = root_dir or Path(__file__).resolve().parents[2]
        self._environ = dict(os.environ if environ is None else environ)
        self._env_file = self.resolve_env_file(
            self._environ.get(self.ENV_FILE_VARIABLE)
        )

    @property
    def env_file(self) -> Path:
        return self._env_file

    def resolve_env_file(self, value: str | None = None) -> Path:
        if not value:
            return self._root_dir / ".env"
        path = Path(value)
        return path if path.is_absolute() else self._root_dir / path

    def load_environment(self) -> tuple[bool, dict[str, str]]:
        if not self.env_file.exists():
            if self._environ.get(self.ENV_FILE_VARIABLE):
                raise ConfigurationError(
                    f"{self.ENV_FILE_VARIABLE}: file not found: {self.env_file}"
                )
            return False, {}
        values = dotenv_values(self.env_file)
        return True, {key: value for key, value in values.items() if value is not None}

    def build_settings(
        self, environ: Mapping[str, str] | None = None
    ) -> PipelineSettings:
        values = dict(self._environ if environ is None else environ)
        defaults = PipelineSettings()
        string = self._string
        integer = self._int
        boolean = self._bool
        runtime_dir = string(values, "AIVAX_PIPELINE_RUNTIME_DIR", defaults.runtime_dir)

        def runtime_path(key: str, suffix: str) -> str:
            return self._path(values, key, f"{runtime_dir}/{suffix}")

        settings = PipelineSettings(
            mask_token=string(values, "AIVAX_PIPELINE_MASK_TOKEN", defaults.mask_token),
            # Pipeline runtime / HTTP server
            runtime_dir=runtime_dir,
            host=string(values, "AIVAX_PIPELINE_HOST", defaults.host),
            port=integer(values, "AIVAX_PIPELINE_PORT", defaults.port, maximum=65535),
            workers=integer(values, "AIVAX_PIPELINE_WORKERS", defaults.workers),
            cors_origins=self._list(
                values, "AIVAX_PIPELINE_CORS_ORIGINS", defaults.cors_origins
            ),
            ssl_key_file=self._optional(values, "AIVAX_PIPELINE_SSL_KEY_FILE"),
            ssl_cert_file=self._optional(values, "AIVAX_PIPELINE_SSL_CERT_FILE"),
            product_mode_file=str(
                (
                    self._root_dir
                    / self._path(
                        values, "AIVAX_PIPELINE_MODE_FILE", defaults.product_mode_file
                    )
                ).resolve()
            ),
            # Application process logs
            log_level=self._enum(
                values, "AIVAX_PIPELINE_LOG_LEVEL", defaults.log_level, self._LOG_LEVELS
            ),
            trace_log_path=runtime_path("AIVAX_PIPELINE_TRACE_LOG_PATH", "trace-log"),
            # Policy refresh
            policy_poll_interval_seconds=integer(
                values,
                "AIVAX_PIPELINE_POLICY_POLL_INTERVAL_SECONDS",
                defaults.policy_poll_interval_seconds,
            ),
            policy_refresh_stream_name=string(
                values,
                "AIVAX_PIPELINE_POLICY_REFRESH_STREAM_NAME",
                defaults.policy_refresh_stream_name,
            ),
            policy_refresh_stream_block_ms=integer(
                values,
                "AIVAX_PIPELINE_POLICY_REFRESH_STREAM_BLOCK_MS",
                defaults.policy_refresh_stream_block_ms,
            ),
            toolkit_enabled=boolean(
                values, "AIVAX_TOOLKIT_ENABLED", defaults.toolkit_enabled
            ),
            # Analysis logs (OpenSearch-compatible bulk files)
            log_bulk_dir=runtime_path("AIVAX_PIPELINE_LOG_BULK_DIR", "bulk"),
            log_bulk_backup_dir=runtime_path(
                "AIVAX_PIPELINE_LOG_BULK_BACKUP_DIR", "bulk-backup"
            ),
            # nNetCDS delivery
            cds_delivery_enabled=boolean(
                values,
                "AIVAX_PIPELINE_CDS_DELIVERY_ENABLED",
                defaults.cds_delivery_enabled,
            ),
            cds_log_dir=runtime_path(
                "AIVAX_PIPELINE_CDS_LOG_DIR",
                "cds-logs",
            ),
            cds_delivery_interval_seconds=integer(
                values,
                "AIVAX_PIPELINE_CDS_DELIVERY_INTERVAL_SECONDS",
                defaults.cds_delivery_interval_seconds,
            ),
            cds_delivery_batch_size=integer(
                values,
                "AIVAX_PIPELINE_CDS_DELIVERY_BATCH_SIZE",
                defaults.cds_delivery_batch_size,
                maximum=1_000,
            ),
            cds_api_url=self._optional_url(
                values,
                "AIVAX_PIPELINE_CDS_API_URL",
                {"http", "https"},
            ),
            cds_api_key=string(
                values,
                "AIVAX_PIPELINE_CDS_API_KEY",
                defaults.cds_api_key,
            ),
            cds_request_timeout_seconds=integer(
                values,
                "AIVAX_PIPELINE_CDS_REQUEST_TIMEOUT_SECONDS",
                defaults.cds_request_timeout_seconds,
            ),
            cds_src_port=integer(
                values,
                "AIVAX_PIPELINE_CDS_SRC_PORT",
                defaults.cds_src_port,
                minimum=0,
                maximum=65_535,
            ),
            cds_dst_host=string(
                values,
                "AIVAX_PIPELINE_CDS_DST_HOST",
                defaults.cds_dst_host,
            ),
            cds_dst_port=integer(
                values,
                "AIVAX_PIPELINE_CDS_DST_PORT",
                defaults.cds_dst_port,
                minimum=0,
                maximum=65_535,
            ),
            management_api_url=self._url(
                values,
                "AIVAX_PIPELINE_MANAGEMENT_API_URL",
                defaults.management_api_url,
                {"http", "https"},
            ),
            management_request_timeout_seconds=integer(
                values,
                "AIVAX_PIPELINE_MANAGEMENT_REQUEST_TIMEOUT_SECONDS",
                defaults.management_request_timeout_seconds,
            ),
            # Analysis log correlation
            analysis_log_merge_backend=self._enum(
                values,
                "AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_BACKEND",
                defaults.analysis_log_merge_backend,
                self._ANALYSIS_LOG_MERGE_BACKENDS,
            ),
            analysis_log_merge_key_prefix=string(
                values,
                "AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_KEY_PREFIX",
                defaults.analysis_log_merge_key_prefix,
            ),
            log_merge_input_sweep_interval_seconds=integer(
                values,
                "AIVAX_PIPELINE_LOG_MERGE_INPUT_SWEEP_INTERVAL_SECONDS",
                defaults.log_merge_input_sweep_interval_seconds,
            ),
            analysis_log_merge_ttl_seconds=integer(
                values,
                "AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_TTL_SECONDS",
                defaults.analysis_log_merge_ttl_seconds,
            ),
            analysis_log_merge_input_flush_after_seconds=integer(
                values,
                "AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_INPUT_FLUSH_AFTER_SECONDS",
                defaults.analysis_log_merge_input_flush_after_seconds,
            ),
            analysis_log_merge_late_output_ttl_seconds=integer(
                values,
                "AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_LATE_OUTPUT_TTL_SECONDS",
                defaults.analysis_log_merge_late_output_ttl_seconds,
                minimum=60,
            ),
            mirror_ttl_seconds=integer(
                values, "AIVAX_PIPELINE_MIRROR_TTL_SECONDS", defaults.mirror_ttl_seconds
            ),
            mirror_input_flush_after_seconds=integer(
                values,
                "AIVAX_PIPELINE_MIRROR_INPUT_FLUSH_AFTER_SECONDS",
                defaults.mirror_input_flush_after_seconds,
            ),
            # Valkey connection shared by Management event streams
            valkey_host=string(
                values, "AIVAX_PIPELINE_VALKEY_HOST", defaults.valkey_host
            ),
            valkey_port=integer(
                values,
                "AIVAX_PIPELINE_VALKEY_PORT",
                defaults.valkey_port,
                maximum=65535,
            ),
            valkey_db=integer(
                values, "AIVAX_PIPELINE_VALKEY_DB", defaults.valkey_db, minimum=0
            ),
            valkey_password=string(
                values, "AIVAX_PIPELINE_VALKEY_PASSWORD", defaults.valkey_password
            ),
            valkey_socket_connect_timeout_ms=integer(
                values,
                "AIVAX_PIPELINE_VALKEY_SOCKET_CONNECT_TIMEOUT_MS",
                defaults.valkey_socket_connect_timeout_ms,
            ),
            valkey_socket_timeout_ms=integer(
                values,
                "AIVAX_PIPELINE_VALKEY_SOCKET_TIMEOUT_MS",
                defaults.valkey_socket_timeout_ms,
            ),
            # Management event streams
            event_flush_interval_seconds=integer(
                values,
                "AIVAX_PIPELINE_EVENT_FLUSH_INTERVAL_SECONDS",
                defaults.event_flush_interval_seconds,
                minimum=1,
            ),
            event_dedupe_ttl_seconds=integer(
                values,
                "AIVAX_PIPELINE_EVENT_DEDUPE_TTL_SECONDS",
                defaults.event_dedupe_ttl_seconds,
                minimum=1,
            ),
            event_attachment_stream_name=string(
                values,
                "AIVAX_PIPELINE_EVENT_ATTACHMENT_STREAM_NAME",
                defaults.event_attachment_stream_name,
            ),
            event_attachment_index_name=string(
                values,
                "AIVAX_PIPELINE_EVENT_ATTACHMENT_INDEX_NAME",
                defaults.event_attachment_index_name,
            ),
            event_policy_notification_stream_name=string(
                values,
                "AIVAX_PIPELINE_EVENT_POLICY_NOTIFICATION_STREAM_NAME",
                defaults.event_policy_notification_stream_name,
            ),
            event_user_sync_stream_name=string(
                values,
                "AIVAX_PIPELINE_EVENT_USER_SYNC_STREAM_NAME",
                defaults.event_user_sync_stream_name,
            ),
            # Management policy snapshots in Valkey
            policy_regex_key=string(
                values, "AIVAX_PIPELINE_POLICY_REGEX_KEY", defaults.policy_regex_key
            ),
            policy_multimodal_key=string(
                values,
                "AIVAX_PIPELINE_POLICY_MULTIMODAL_KEY",
                defaults.policy_multimodal_key,
            ),
            policy_secuxper_key=string(
                values,
                "AIVAX_PIPELINE_POLICY_SECUXPER_KEY",
                defaults.policy_secuxper_key,
            ),
            policy_slm_key=string(
                values, "AIVAX_PIPELINE_POLICY_SLM_KEY", defaults.policy_slm_key
            ),
            ai_service_catalog_key=string(
                values,
                "AIVAX_PIPELINE_AI_SERVICE_CATALOG_KEY",
                defaults.ai_service_catalog_key,
            ),
            # SLM inspection execution limit
            inspect_slm_max_inflight=integer(
                values,
                "AIVAX_PIPELINE_INSPECT_SLM_MAX_INFLIGHT",
                defaults.inspect_slm_max_inflight,
            ),
            inspect_slm_queue_skip_after=integer(
                values,
                "AIVAX_PIPELINE_INSPECT_SLM_QUEUE_SKIP_AFTER",
                defaults.inspect_slm_queue_skip_after,
                minimum=0,
            ),
            # Multimodal inspection execution limits
            inspect_multimodal_file_max_inflight=integer(
                values,
                "AIVAX_PIPELINE_INSPECT_MULTIMODAL_FILE_MAX_INFLIGHT",
                defaults.inspect_multimodal_file_max_inflight,
            ),
            inspect_multimodal_ocr_max_inflight=integer(
                values,
                "AIVAX_PIPELINE_INSPECT_MULTIMODAL_OCR_MAX_INFLIGHT",
                defaults.inspect_multimodal_ocr_max_inflight,
            ),
            inspect_multimodal_acquire_timeout_seconds=integer(
                values,
                "AIVAX_PIPELINE_INSPECT_MULTIMODAL_ACQUIRE_TIMEOUT_SECONDS",
                defaults.inspect_multimodal_acquire_timeout_seconds,
                minimum=0,
            ),
            # CSO grade lookup
            cso_enabled=boolean(
                values,
                "AIVAX_PIPELINE_CSO_ENABLED",
                defaults.cso_enabled,
            ),
            cso_provider=self._enum(
                values,
                "AIVAX_PIPELINE_CSO_PROVIDER",
                defaults.cso_provider,
                self._CSO_PROVIDERS,
            ),
            cso_skip_unsupported_extensions_enabled=boolean(
                values,
                "AIVAX_PIPELINE_CSO_SKIP_UNSUPPORTED_EXTENSIONS_ENABLED",
                defaults.cso_skip_unsupported_extensions_enabled,
            ),
            cso_skip_untagged_enabled=boolean(
                values,
                "AIVAX_PIPELINE_CSO_SKIP_UNTAGGED_ENABLED",
                defaults.cso_skip_untagged_enabled,
            ),
            cso_skip_error_enabled=boolean(
                values,
                "AIVAX_PIPELINE_CSO_SKIP_ERROR_ENABLED",
                defaults.cso_skip_error_enabled,
            ),
            cso_request_timeout_seconds=integer(
                values,
                "AIVAX_PIPELINE_CSO_REQUEST_TIMEOUT_SECONDS",
                defaults.cso_request_timeout_seconds,
            ),
            cso_startup_timeout_seconds=integer(
                values,
                "AIVAX_PIPELINE_CSO_STARTUP_TIMEOUT_SECONDS",
                defaults.cso_startup_timeout_seconds,
            ),
            user_consent_global_bypass_enabled=boolean(
                values,
                "AIVAX_PIPELINE_USER_CONSENT_GLOBAL_BYPASS_ENABLED",
                defaults.user_consent_global_bypass_enabled,
            ),
            user_consent_regex_enabled=boolean(
                values,
                "AIVAX_PIPELINE_USER_CONSENT_REGEX_ENABLED",
                defaults.user_consent_regex_enabled,
            ),
            user_consent_multimodal_enabled=boolean(
                values,
                "AIVAX_PIPELINE_USER_CONSENT_MULTIMODAL_ENABLED",
                defaults.user_consent_multimodal_enabled,
            ),
            user_consent_slm_enabled=boolean(
                values,
                "AIVAX_PIPELINE_USER_CONSENT_SLM_ENABLED",
                defaults.user_consent_slm_enabled,
            ),
            user_consent_secuxper_enabled=boolean(
                values,
                "AIVAX_PIPELINE_USER_CONSENT_SECUXPER_ENABLED",
                defaults.user_consent_secuxper_enabled,
            ),
            # SLM inspection
            slm_url=self._url(
                values, "AIVAX_PIPELINE_SLM_URL", defaults.slm_url, {"http", "https"}
            ),
            slm_request_timeout_seconds=integer(
                values,
                "AIVAX_PIPELINE_SLM_REQUEST_TIMEOUT_SECONDS",
                defaults.slm_request_timeout_seconds,
            ),
            slm_model_version=integer(
                values, "AIVAX_PIPELINE_SLM_MODEL_VERSION", defaults.slm_model_version
            ),
            slm_http_max_connections=integer(
                values,
                "AIVAX_PIPELINE_SLM_HTTP_MAX_CONNECTIONS",
                defaults.slm_http_max_connections,
            ),
            slm_http_max_keepalive=integer(
                values,
                "AIVAX_PIPELINE_SLM_HTTP_MAX_KEEPALIVE",
                defaults.slm_http_max_keepalive,
                minimum=0,
            ),
            slm_http_connect_timeout_seconds=integer(
                values,
                "AIVAX_PIPELINE_SLM_HTTP_CONNECT_TIMEOUT_SECONDS",
                defaults.slm_http_connect_timeout_seconds,
            ),
            # SecuXperAI independent filter connection settings
            secuxperai_base_url=self._url(
                values,
                "AIVAX_PIPELINE_SECUXPERAI_BASE_URL",
                defaults.secuxperai_base_url,
                {"http", "https"},
            ),
            secuxperai_filter_path=string(
                values,
                "AIVAX_PIPELINE_SECUXPERAI_FILTER_PATH",
                defaults.secuxperai_filter_path,
            ),
            secuxperai_token_path=string(
                values,
                "AIVAX_PIPELINE_SECUXPERAI_TOKEN_PATH",
                defaults.secuxperai_token_path,
            ),
            secuxperai_refresh_path=string(
                values,
                "AIVAX_PIPELINE_SECUXPERAI_REFRESH_PATH",
                defaults.secuxperai_refresh_path,
            ),
            secuxperai_team_uuid=string(
                values,
                "AIVAX_PIPELINE_SECUXPERAI_TEAM_UUID",
                defaults.secuxperai_team_uuid,
            ),
            secuxperai_client_no=string(
                values,
                "AIVAX_PIPELINE_SECUXPERAI_CLIENT_NO",
                defaults.secuxperai_client_no,
            ),
            secuxperai_site_code=string(
                values,
                "AIVAX_PIPELINE_SECUXPERAI_SITE_CODE",
                defaults.secuxperai_site_code,
            ),
            secuxperai_sync_mode=string(
                values,
                "AIVAX_PIPELINE_SECUXPERAI_SYNC_MODE",
                defaults.secuxperai_sync_mode,
            ),
            secuxperai_token_key=string(
                values,
                "AIVAX_PIPELINE_SECUXPERAI_TOKEN_KEY",
                defaults.secuxperai_token_key,
            ),
            secuxperai_token_lock_key=string(
                values,
                "AIVAX_PIPELINE_SECUXPERAI_TOKEN_LOCK_KEY",
                defaults.secuxperai_token_lock_key,
            ),
            secuxperai_token_lock_ttl_seconds=integer(
                values,
                "AIVAX_PIPELINE_SECUXPERAI_TOKEN_LOCK_TTL_SECONDS",
                defaults.secuxperai_token_lock_ttl_seconds,
            ),
            secuxperai_token_wait_seconds=integer(
                values,
                "AIVAX_PIPELINE_SECUXPERAI_TOKEN_WAIT_SECONDS",
                defaults.secuxperai_token_wait_seconds,
            ),
            secuxperai_request_timeout_seconds=integer(
                values,
                "AIVAX_PIPELINE_SECUXPERAI_REQUEST_TIMEOUT_SECONDS",
                defaults.secuxperai_request_timeout_seconds,
            ),
            secuxperai_connect_timeout_seconds=integer(
                values,
                "AIVAX_PIPELINE_SECUXPERAI_CONNECT_TIMEOUT_SECONDS",
                defaults.secuxperai_connect_timeout_seconds,
            ),
            # File inspection / watermark detection
            file_bypass_mode=boolean(
                values, "AIVAX_PIPELINE_FILE_BYPASS_MODE", defaults.file_bypass_mode
            ),
            file_read_timeout_seconds=integer(
                values,
                "AIVAX_PIPELINE_FILE_READ_TIMEOUT_SECONDS",
                defaults.file_read_timeout_seconds,
            ),
            file_allowed_extensions=self._list(
                values,
                "AIVAX_PIPELINE_FILE_ALLOWED_EXTENSIONS",
                defaults.file_allowed_extensions,
            ),
            file_max_size_bytes=integer(
                values,
                "AIVAX_PIPELINE_FILE_MAX_SIZE_BYTES",
                defaults.file_max_size_bytes,
            ),
            unsupported_file_handling=self._enum(
                values,
                "AIVAX_PIPELINE_UNSUPPORTED_FILE_HANDLING",
                defaults.unsupported_file_handling,
                self._FILE_HANDLING,
            ),
            oversized_file_handling=self._enum(
                values,
                "AIVAX_PIPELINE_OVERSIZED_FILE_HANDLING",
                defaults.oversized_file_handling,
                self._FILE_HANDLING,
            ),
            backup_skipped_oversized_files=boolean(
                values,
                "AIVAX_PIPELINE_BACKUP_SKIPPED_OVERSIZED_FILES",
                defaults.backup_skipped_oversized_files,
            ),
            pdf_evidence_engine=self._enum(
                values,
                "AIVAX_PIPELINE_PDF_EVIDENCE_ENGINE",
                defaults.pdf_evidence_engine,
                self._PDF_EVIDENCE_ENGINES,
            ),
            file_content_chunk_size=integer(
                values,
                "AIVAX_PIPELINE_FILE_CONTENT_CHUNK_SIZE",
                defaults.file_content_chunk_size,
                minimum=0,
            ),
            detail_reason_temp_dir=runtime_path(
                "AIVAX_PIPELINE_DETAIL_REASON_TEMP_DIR", "detail-reason"
            ),
            file_block_backup_dir=runtime_path(
                "AIVAX_PIPELINE_FILE_BLOCK_BACKUP_DIR", "file-backup"
            ),
            watermark_use_regex_base_drm=boolean(
                values,
                "AIVAX_PIPELINE_WATERMARK_USE_REGEX_BASE_DRM",
                defaults.watermark_use_regex_base_drm,
            ),
            watermark_use_ocr_base_watermark=boolean(
                values,
                "AIVAX_PIPELINE_WATERMARK_USE_OCR_BASE_WATERMARK",
                defaults.watermark_use_ocr_base_watermark,
            ),
            watermark_drm_detect_pattern=string(
                values,
                "AIVAX_PIPELINE_WATERMARK_DRM_DETECT_PATTERN",
                defaults.watermark_drm_detect_pattern,
            ),
            watermark_drm_pattern_flag=integer(
                values,
                "AIVAX_PIPELINE_WATERMARK_DRM_PATTERN_FLAG",
                defaults.watermark_drm_pattern_flag,
                minimum=0,
            ),
            watermark_drm_file_header_limit=integer(
                values,
                "AIVAX_PIPELINE_WATERMARK_DRM_FILE_HEADER_LIMIT",
                defaults.watermark_drm_file_header_limit,
            ),
            watermark_ocr_patterns=self._list(
                values,
                "AIVAX_PIPELINE_WATERMARK_OCR_PATTERNS",
                defaults.watermark_ocr_patterns,
            ),
            watermark_ocr_pattern_flag=integer(
                values,
                "AIVAX_PIPELINE_WATERMARK_OCR_PATTERN_FLAG",
                defaults.watermark_ocr_pattern_flag,
                minimum=0,
            ),
            watermark_ocr_max_hitcount=integer(
                values,
                "AIVAX_PIPELINE_WATERMARK_OCR_MAX_HITCOUNT",
                defaults.watermark_ocr_max_hitcount,
            ),
            watermark_ocr_pages=self._int_list(
                values,
                "AIVAX_PIPELINE_WATERMARK_OCR_PAGES",
                defaults.watermark_ocr_pages,
            ),
            # Filter runtime behavior
            ssl_proxy_bypass_bitmask=integer(
                values,
                "AIVAX_PIPELINE_SSL_PROXY_BYPASS_BITMASK",
                defaults.ssl_proxy_bypass_bitmask,
                minimum=0,
            ),
        )
        self._validate(settings)
        return settings

    def bootstrap(self) -> tuple[bool, PipelineSettings]:
        loaded, file_values = self.load_environment()
        return loaded, self.build_settings({**file_values, **self._environ})

    @staticmethod
    def _raw(values: Mapping[str, str], key: str) -> str | None:
        value = values.get(key)
        return value.strip() if value is not None else None

    def _string(self, values: Mapping[str, str], key: str, default: str) -> str:
        value = self._raw(values, key)
        return default if value is None else value

    def _optional(self, values: Mapping[str, str], key: str) -> str | None:
        return self._raw(values, key) or None

    def _int(
        self,
        values: Mapping[str, str],
        key: str,
        default: int,
        *,
        minimum: int = 1,
        maximum: int | None = None,
    ) -> int:
        value = self._raw(values, key)
        if value is None or value == "":
            return default
        try:
            parsed = int(value)
        except ValueError as error:
            raise ConfigurationError(f"{key}: integer is required") from error
        if parsed < minimum or maximum is not None and parsed > maximum:
            raise ConfigurationError(f"{key}: value is outside the allowed range")
        return parsed

    def _bool(self, values: Mapping[str, str], key: str, default: bool) -> bool:
        value = self._raw(values, key)
        if value is None or value == "":
            return default
        if value.lower() in {"1", "true", "yes", "on"}:
            return True
        if value.lower() in {"0", "false", "no", "off"}:
            return False
        raise ConfigurationError(f"{key}: boolean is required (true/false)")

    def _enum(
        self,
        values: Mapping[str, str],
        key: str,
        default: str,
        allowed: set[str] | frozenset[str],
        *,
        allow_empty: bool = False,
    ) -> str:
        value = self._raw(values, key)
        if value is None or value == "":
            return default
        if value.lower() not in allowed:
            raise ConfigurationError(f"{key}: invalid value")
        return value.lower()

    def _url(
        self, values: Mapping[str, str], key: str, default: str, schemes: set[str]
    ) -> str:
        value = self._string(values, key, default)
        parsed = urlparse(value)
        if parsed.scheme not in schemes or not parsed.hostname:
            raise ConfigurationError(f"{key}: valid URL is required")
        return value

    def _optional_url(
        self, values: Mapping[str, str], key: str, schemes: set[str]
    ) -> str | None:
        value = self._optional(values, key)
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in schemes or not parsed.hostname:
            raise ConfigurationError(f"{key}: valid URL is required")
        return value

    def _list(
        self, values: Mapping[str, str], key: str, default: tuple[str, ...]
    ) -> tuple[str, ...]:
        value = self._raw(values, key)
        if value is None or value == "":
            return default
        try:
            items = (
                json.loads(value)
                if value.startswith("[")
                else [item.strip() for item in value.split(",")]
            )
        except json.JSONDecodeError as error:
            raise ConfigurationError(f"{key}: list is required") from error
        if not isinstance(items, list) or not all(
            isinstance(item, str) and item for item in items
        ):
            raise ConfigurationError(f"{key}: list of strings is required")
        return tuple(items)

    def _int_list(
        self, values: Mapping[str, str], key: str, default: tuple[int, ...]
    ) -> tuple[int, ...]:
        value = self._raw(values, key)
        if value is None or value == "":
            return default
        try:
            items = json.loads(value) if value.startswith("[") else value.split(",")
            parsed = tuple(int(item) for item in items)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ConfigurationError(f"{key}: integer list is required") from error
        if not parsed or any(item < 0 for item in parsed):
            raise ConfigurationError(f"{key}: non-negative integers are required")
        return parsed

    def _path(self, values: Mapping[str, str], key: str, default: str) -> str:
        # Keep relative paths exactly as configured.  Several legacy modules
        # resolve them from the Pipeline working directory.
        return self._string(values, key, default)

    @staticmethod
    def _validate(settings: PipelineSettings) -> None:
        if not settings.mask_token.strip():
            raise ConfigurationError(
                "AIVAX_PIPELINE_MASK_TOKEN: non-empty value is required",
            )
        if bool(settings.ssl_key_file) != bool(settings.ssl_cert_file):
            raise ConfigurationError(
                "SSL key and certificate must be provided together",
            )
        if (
            settings.analysis_log_merge_backend == "valkey"
            and not settings.analysis_log_merge_key_prefix.strip()
        ):
            raise ConfigurationError(
                "AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_KEY_PREFIX: "
                "non-empty value is required",
            )
        if settings.workers > 1 and settings.analysis_log_merge_backend == "memory":
            raise ConfigurationError(
                "AIVAX_PIPELINE_WORKERS: memory analysis-log merge requires one worker",
            )
        if not settings.policy_refresh_stream_name.strip():
            raise ConfigurationError(
                "AIVAX_PIPELINE_POLICY_REFRESH_STREAM_NAME: "
                "non-empty value is required",
            )

        if settings.cds_delivery_enabled:
            if settings.cds_api_url is None:
                raise ConfigurationError(
                    "AIVAX_PIPELINE_CDS_API_URL: CDS delivery requires a valid URL",
                )
            if not settings.cds_api_key.strip():
                raise ConfigurationError(
                    "AIVAX_PIPELINE_CDS_API_KEY: "
                    "CDS delivery requires a non-empty API key",
                )
            if not settings.cds_dst_host.strip() or len(settings.cds_dst_host) > 255:
                raise ConfigurationError(
                    "AIVAX_PIPELINE_CDS_DST_HOST: "
                    "destination host must be 1~255 characters",
                )

        if settings.slm_model_version not in {1, 2, 3, 4, 5}:
            raise ConfigurationError(
                "AIVAX_PIPELINE_SLM_MODEL_VERSION: supported values are 1, 2, 3, 4, 5; "
                "use mode.json filters.secuxper.enabled for SecuXper",
            )

        if settings.secuxperai_team_uuid or settings.secuxperai_client_no:
            required_values = {
                "AIVAX_PIPELINE_SECUXPERAI_TEAM_UUID": (settings.secuxperai_team_uuid),
                "AIVAX_PIPELINE_SECUXPERAI_CLIENT_NO": (settings.secuxperai_client_no),
                "AIVAX_PIPELINE_SECUXPERAI_SITE_CODE": (settings.secuxperai_site_code),
            }
            for name, value in required_values.items():
                if not value.strip():
                    raise ConfigurationError(
                        f"{name}: non-empty value is required for SecuXper",
                    )

            paths = {
                "AIVAX_PIPELINE_SECUXPERAI_FILTER_PATH": (
                    settings.secuxperai_filter_path
                ),
                "AIVAX_PIPELINE_SECUXPERAI_TOKEN_PATH": (
                    settings.secuxperai_token_path
                ),
                "AIVAX_PIPELINE_SECUXPERAI_REFRESH_PATH": (
                    settings.secuxperai_refresh_path
                ),
            }
            for name, path in paths.items():
                if not path.startswith("/"):
                    raise ConfigurationError(
                        f'{name}: path must start with "/"',
                    )

            if settings.secuxperai_token_key == settings.secuxperai_token_lock_key:
                raise ConfigurationError(
                    "AIVAX_PIPELINE_SECUXPERAI_TOKEN_LOCK_KEY: "
                    "token key and lock key must be different",
                )

            if settings.secuxperai_sync_mode not in {"true", "false"}:
                raise ConfigurationError(
                    "AIVAX_PIPELINE_SECUXPERAI_SYNC_MODE: value must be true or false",
                )


ENV_LOADER = EnvironmentLoader()
ENV_FILE = ENV_LOADER.env_file
ENVIRONMENT_LOADED, SETTINGS = ENV_LOADER.bootstrap()
