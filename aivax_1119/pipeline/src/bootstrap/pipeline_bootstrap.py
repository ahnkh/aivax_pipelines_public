"""
Pipeline 프로세스의 단일 composition root.

역할: controller, service, adapter 등의 계층을 로드하고, 필요한 의존성을 주입해준다.
사용처: main.py
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from src.adapters.ai_service_catalog.valkey_ai_service_catalog import (
    ValkeyAiServiceCatalog,
)
from src.adapters.analysis_log_delivery import (
    FluentBitAnalysisLogDeliveryAdapter,
    HttpCdsDeliveryAdapter,
    LocalFileCdsAnalysisLogQueueAdapter,
)
from src.adapters.analysis_log_merge import (
    MemoryAnalysisLogMergeStore,
    ValkeyAnalysisLogMergeStore,
)
from src.adapters.config_aivax import JsonFileAivaxConfigAdapter
from src.adapters.delivery_file import LocalFileDeliveryAdapter
from src.adapters.health.runtime_health_adapter import RuntimeHealthAdapter
from src.adapters.inspection_multimodal import LocalMultimodalInspectionAdapter
from src.adapters.inspection_regex import PythonRegexInspectionAdapter
from src.adapters.inspection_secuxper.secuxperai.settings import (
    SecuxperaiSettings,
)
from src.adapters.inspection_secuxper.secuxperai.valkey_token_store_adapter import (
    ValkeySecuxperaiTokenStoreAdapter,
)
from src.adapters.inspection_slm import create_slm_inspection_adapter
from src.adapters.inspection_slm.settings import SlmHttpSettings
from src.adapters.notification_attachment import (
    ValkeyAttachmentNotificationOutboxAdapter,
)
from src.adapters.notification_audit import (
    HttpManagementAuditNotificationAdapter,
)
from src.adapters.notification_policy.valkey_policy_notification_outbox_adapter import (
    ValkeyPolicyNotificationOutboxAdapter,
)
from src.adapters.notification_user.valkey_user_notification_outbox_adapter import (
    ValkeyUserNotificationOutboxAdapter,
)
from src.adapters.policy_multimodal import ValkeyMultimodalPolicyAdapter
from src.adapters.policy_refresh_signal import ValkeyPolicyRefreshSignalPublisher
from src.adapters.policy_regex import ValkeyRegexPolicyAdapter
from src.adapters.policy_secuxper.valkey_secuxper_policy_adapter import (
    ValkeySecuxperPolicyAdapter,
)
from src.adapters.policy_slm import ValkeySlmPolicyAdapter
from src.application.services.analysis_log_merge import AnalysisLogMergeService
from src.application.services.inspection_multimodal import (
    MultimodalInspectionService,
)
from src.application.services.inspection_secuxper.service import (
    SecuxperInspectionService,
)
from src.application.services.inspection_slm import (
    SlmInspectionOptions,
    SlmInspectionService,
)
from src.application.services.notification_attachment import (
    AttachmentNotificationService,
)
from src.application.use_cases.check_health import CheckHealthUseCase
from src.application.use_cases.cleanup_pipeline_files import CleanupPipelineFilesUseCase
from src.application.use_cases.expire_analysis_log_merge import (
    ExpireAnalysisLogMergeUseCase,
)
from src.application.use_cases.input_analysis import (
    InputAnalysisLogBuilder,
    InputAnalysisUseCase,
    MultimodalInputInspector,
    RegexInputInspector,
    SlmInputInspector,
)
from src.application.use_cases.input_analysis.secuxper_input_inspector import (
    SecuxperInputInspector,
)
from src.application.use_cases.output_analysis import OutputAnalysisUseCase
from src.application.use_cases.publish_policy_refresh_signal import (
    PublishPolicyRefreshSignalUseCase,
)
from src.application.use_cases.refresh_ai_service_catalog.use_case import (
    RefreshAiServiceCatalogUseCase,
)
from src.application.use_cases.refresh_policy_multimodal import (
    RefreshMultimodalPolicyUseCase,
)
from src.application.use_cases.refresh_policy_regex import RefreshRegexPolicyUseCase
from src.application.use_cases.refresh_policy_secuxper.use_case import (
    RefreshSecuxperPolicyUseCase,
)
from src.application.use_cases.refresh_policy_slm import RefreshSlmPolicyUseCase
from src.application.use_cases.send_pending_cds_logs import (
    SendPendingCdsLogsUseCase,
)
from src.application.use_cases.test_filter_rule import FilterRuleTestUseCase
from src.bootstrap.env_loader import PipelineSettings
from src.controllers.health.health_controller import HealthController
from src.controllers.input_analysis.v1.input_analysis_controller import (
    InputAnalysisController,
)
from src.controllers.input_analysis.v2.input_analysis_controller import (
    InputAnalysisControllerV2,
)
from src.controllers.output_analysis.v1.output_analysis_controller import (
    OutputAnalysisController,
)
from src.controllers.output_analysis.v2.output_analysis_controller import (
    OutputAnalysisControllerV2,
)
from src.controllers.policy_refresh import PolicyRefreshController
from src.controllers.test_filter_rule import FilterRuleTestController
from src.core.constants import ERR_FAIL, ERR_OK
from src.core.http import HttpClientOptions, HttpClientRegistry
from src.core.valkey.connecter import ValkeyConnecter
from src.domain.analysis_log_delivery.ports import (
    AnalysisLogBatchDeliveryPort,
)
from src.domain.analysis_log_delivery.ports.analysis_log_consent_writer_port import (
    AnalysisLogConsentWriterPort,
)
from src.domain.analysis_log_delivery.ports.analysis_log_delivery_port import (
    AnalysisLogDeliveryPort,
)
from src.domain.analysis_log_merge.models.output_log_options import OutputLogOptions
from src.domain.analysis_log_merge.ports.analysis_log_merge_store import (
    AnalysisLogMergeStorePort,
)
from src.domain.config_aivax.ports import AivaxConfigPort
from src.domain.delivery_file.ports import FileDeliveryPort
from src.domain.inspection_input.models import InputAnalysisOptions
from src.domain.inspection_input.models.block_phrases import get_block_phrases
from src.domain.inspection_input.ports.input_guard_port import InputGuardPort
from src.domain.inspection_multimodal.models import (
    MultimodalInspectionOptions,
    WatermarkCriteria,
)
from src.domain.inspection_multimodal.ports import MultimodalInspectionPort
from src.domain.inspection_regex.ports import RegexInspectionPort
from src.domain.inspection_secuxper.ports import SecuxperInspectionPort
from src.domain.inspection_slm.ports import SlmInspectionPort
from src.domain.notification_attachment.ports import (
    AttachmentNotificationOutboxPort,
)
from src.domain.notification_audit.ports import AuditNotificationPort
from src.domain.notification_policy.ports.policy_notification_outbox import (
    PolicyNotificationOutboxPort,
)
from src.domain.notification_user.ports.user_notification_outbox import (
    UserNotificationOutboxPort,
)
from src.domain.policy_multimodal.ports import MultimodalPolicyPort
from src.domain.policy_regex.ports import RegexPolicyPort
from src.domain.policy_secuxper.ports import SecuxperPolicyPort
from src.domain.policy_slm.ports import SlmPolicyPort
from src.schedulers import (
    AnalysisLogMergeExpiryCron,
    CdsLogDeliveryCron,
    PolicyRefreshCron,
    PolicyRefreshStreamListener,
)
from src.schedulers.cleanup_cron import CleanupCron

_SSL_PROXY_BYPASS_BLOCK = 0b0001
_SSL_PROXY_BYPASS_MASKING = 0b0010


class _RefreshPolicyUseCase(Protocol):
    def execute(self) -> object: ...


class _AsyncRuntime(Protocol):
    async def start(self) -> None: ...

    async def aclose(self) -> None: ...


@dataclass(slots=True)
class _CdsRuntime:
    queue: LocalFileCdsAnalysisLogQueueAdapter
    delivery: AnalysisLogBatchDeliveryPort
    audit: AuditNotificationPort
    delivery_cron: CdsLogDeliveryCron


@dataclass(slots=True)
class _AnalysisLogRuntime:
    store: AnalysisLogMergeStorePort
    merge_valkey_connecter: ValkeyConnecter | None
    delivery: AnalysisLogDeliveryPort
    merge_service: AnalysisLogMergeService
    expiry_cron: AnalysisLogMergeExpiryCron
    attachment_notification_service: AttachmentNotificationService | None
    attachment_notification_outbox: AttachmentNotificationOutboxPort | None
    attachment_valkey_connecter: ValkeyConnecter | None
    policy_notification_outbox: PolicyNotificationOutboxPort | None
    user_notification_outbox: UserNotificationOutboxPort | None
    policy_notification_valkey_connecter: ValkeyConnecter | None
    user_notification_valkey_connecter: ValkeyConnecter | None


@dataclass(slots=True)
class _PolicyRuntime:
    valkey_connecter: ValkeyConnecter
    ai_service_catalog: ValkeyAiServiceCatalog
    ai_service_catalog_refresh_cron: PolicyRefreshCron
    regex_policy: RegexPolicyPort
    regex_inspection: RegexInspectionPort
    refresh_regex_policy_use_case: RefreshRegexPolicyUseCase
    regex_policy_refresh_cron: PolicyRefreshCron
    multimodal_policy: MultimodalPolicyPort
    refresh_multimodal_policy_use_case: RefreshMultimodalPolicyUseCase
    multimodal_policy_refresh_cron: PolicyRefreshCron
    slm_policy: SlmPolicyPort
    refresh_slm_policy_use_case: RefreshSlmPolicyUseCase
    slm_policy_refresh_cron: PolicyRefreshCron
    secuxper_policy: SecuxperPolicyPort
    refresh_secuxper_policy_use_case: RefreshSecuxperPolicyUseCase
    secuxper_policy_refresh_cron: PolicyRefreshCron
    refresh_stream_listener: PolicyRefreshStreamListener
    refresh_signal_publisher: ValkeyPolicyRefreshSignalPublisher


@dataclass(frozen=True, slots=True)
class _InputInspectionServices:
    multimodal: MultimodalInspectionService
    slm: SlmInspectionService


@dataclass(slots=True)
class PipelineRuntimeModules:
    """프로세스 시작 시 한 번 조립되는 장기 수명 모듈."""

    # analysis log lifecycle
    analysis_log_merge_store: AnalysisLogMergeStorePort
    analysis_log_merge_valkey_connecter: ValkeyConnecter | None
    analysis_log_delivery: AnalysisLogDeliveryPort
    analysis_log_merge_service: AnalysisLogMergeService
    analysis_log_merge_expiry_cron: AnalysisLogMergeExpiryCron

    # policy cache and inspection implementations
    policy_valkey_connecter: ValkeyConnecter
    ai_service_catalog: ValkeyAiServiceCatalog
    ai_service_catalog_refresh_cron: PolicyRefreshCron
    regex_inspection: RegexInspectionPort
    regex_policy: RegexPolicyPort
    refresh_regex_policy_use_case: RefreshRegexPolicyUseCase
    regex_policy_refresh_cron: PolicyRefreshCron
    multimodal_inspection: MultimodalInspectionPort
    multimodal_policy: MultimodalPolicyPort
    refresh_multimodal_policy_use_case: RefreshMultimodalPolicyUseCase
    multimodal_policy_refresh_cron: PolicyRefreshCron
    slm_inspection: SlmInspectionPort
    slm_policy: SlmPolicyPort
    refresh_slm_policy_use_case: RefreshSlmPolicyUseCase
    slm_policy_refresh_cron: PolicyRefreshCron
    policy_refresh_stream_listener: PolicyRefreshStreamListener
    policy_refresh_signal_publisher: ValkeyPolicyRefreshSignalPublisher

    # external resources and delivery adapters
    file_delivery: FileDeliveryPort
    aivax_config: AivaxConfigPort
    secuxper_policy: SecuxperPolicyPort | None = None
    secuxper_policy_refresh_cron: PolicyRefreshCron | None = None
    attachment_notification_service: AttachmentNotificationService | None = None
    attachment_notification_outbox: AttachmentNotificationOutboxPort | None = None
    attachment_valkey_connecter: ValkeyConnecter | None = None
    policy_notification_outbox: PolicyNotificationOutboxPort | None = None
    user_notification_outbox: UserNotificationOutboxPort | None = None
    policy_notification_valkey_connecter: ValkeyConnecter | None = None
    user_notification_valkey_connecter: ValkeyConnecter | None = None

    # nNetCDS delivery
    cds_delivery: AnalysisLogBatchDeliveryPort | None = None
    cds_failure_audit: AuditNotificationPort | None = None
    cds_delivery_cron: CdsLogDeliveryCron | None = None
    cleanup_cron: CleanupCron | None = None


@dataclass(frozen=True, slots=True)
class PipelineAnalysisModules:
    """HTTP route 등록에 필요한 입력·출력 분석 모듈."""

    input_analysis_use_case: InputAnalysisUseCase
    input_analysis_controller: InputAnalysisController
    output_analysis_use_case: OutputAnalysisUseCase
    output_analysis_controller: OutputAnalysisController
    publish_policy_refresh_signal_use_case: PublishPolicyRefreshSignalUseCase
    policy_refresh_controller: PolicyRefreshController
    test_filter_rule_use_case: FilterRuleTestUseCase
    test_filter_rule_controller: FilterRuleTestController
    health_controller: HealthController
    input_analysis_v2_controller: InputAnalysisControllerV2
    output_analysis_v2_controller: OutputAnalysisControllerV2
    input_guard_runtime: _AsyncRuntime | None = None


class PipelineBootstrap:
    """프로세스 의존성 조립을 담당하는 유일한 진입점."""

    def __init__(
        self,
        settings: PipelineSettings,
        http_client_registry: HttpClientRegistry | None,
        logger: logging.Logger,
    ) -> None:
        self._settings = settings
        self._http_client_registry = http_client_registry or HttpClientRegistry()
        self._logger = logger
        self._input_guard_runtime: _AsyncRuntime | None = None
        self._aivax_config = JsonFileAivaxConfigAdapter(
            settings.product_mode_file,
            logger,
        )

    @property
    def http_client_registry(self) -> HttpClientRegistry:
        """프로세스 공용 HTTP client registry를 제공한다."""
        return self._http_client_registry

    def assemble_runtime_modules(self) -> PipelineRuntimeModules:
        """프로세스 공용 모듈을 수명주기 순서대로 조립한다."""
        cds = self._create_cds_runtime()
        analysis_log = self._assemble_analysis_log_runtime(
            cds.queue if cds is not None else None,
        )
        policy = self._assemble_policy_runtime()

        slm_inspection = self._create_slm_inspection_adapter()
        multimodal_inspection = LocalMultimodalInspectionAdapter(
            ocr_max_inflight=self._settings.inspect_multimodal_ocr_max_inflight,
            ocr_timeout_seconds=max(1, self._settings.file_read_timeout_seconds),
            pdf_evidence_engine=self._settings.pdf_evidence_engine,
        )
        file_delivery = LocalFileDeliveryAdapter(
            self._settings.file_block_backup_dir,
            logger=self._logger,
        )
        cleanup_cron = None
        if self._settings.toolkit_enabled:
            cleanup_cron = CleanupCron(
                CleanupPipelineFilesUseCase(
                    cast(AnalysisLogConsentWriterPort, analysis_log.delivery),
                    multimodal_inspection,
                    self._settings.detail_reason_temp_dir,
                    log_delivery=analysis_log.delivery,
                    log_backup_directory=self._settings.log_bulk_backup_dir,
                    logger=self._logger,
                ),
                logger=self._logger,
            )
            cleanup_cron.start()
        self._start_policy_refresh_crons(policy)
        policy.refresh_stream_listener.start()
        if cds is not None:
            self._start_cds_delivery_cron(cds.delivery_cron)
        return PipelineRuntimeModules(
            analysis_log_merge_store=analysis_log.store,
            analysis_log_merge_valkey_connecter=(analysis_log.merge_valkey_connecter),
            analysis_log_delivery=analysis_log.delivery,
            analysis_log_merge_service=analysis_log.merge_service,
            analysis_log_merge_expiry_cron=analysis_log.expiry_cron,
            cleanup_cron=cleanup_cron,
            policy_valkey_connecter=policy.valkey_connecter,
            ai_service_catalog=policy.ai_service_catalog,
            ai_service_catalog_refresh_cron=policy.ai_service_catalog_refresh_cron,
            regex_inspection=policy.regex_inspection,
            regex_policy=policy.regex_policy,
            refresh_regex_policy_use_case=(policy.refresh_regex_policy_use_case),
            regex_policy_refresh_cron=policy.regex_policy_refresh_cron,
            multimodal_inspection=multimodal_inspection,
            multimodal_policy=policy.multimodal_policy,
            refresh_multimodal_policy_use_case=(
                policy.refresh_multimodal_policy_use_case
            ),
            multimodal_policy_refresh_cron=(policy.multimodal_policy_refresh_cron),
            slm_inspection=slm_inspection,
            slm_policy=policy.slm_policy,
            refresh_slm_policy_use_case=policy.refresh_slm_policy_use_case,
            slm_policy_refresh_cron=policy.slm_policy_refresh_cron,
            secuxper_policy=policy.secuxper_policy,
            secuxper_policy_refresh_cron=policy.secuxper_policy_refresh_cron,
            policy_refresh_stream_listener=policy.refresh_stream_listener,
            policy_refresh_signal_publisher=policy.refresh_signal_publisher,
            file_delivery=file_delivery,
            aivax_config=self._aivax_config,
            attachment_notification_service=(
                analysis_log.attachment_notification_service
            ),
            attachment_notification_outbox=(
                analysis_log.attachment_notification_outbox
            ),
            attachment_valkey_connecter=(analysis_log.attachment_valkey_connecter),
            policy_notification_outbox=analysis_log.policy_notification_outbox,
            user_notification_outbox=analysis_log.user_notification_outbox,
            policy_notification_valkey_connecter=(
                analysis_log.policy_notification_valkey_connecter
            ),
            user_notification_valkey_connecter=(
                analysis_log.user_notification_valkey_connecter
            ),
            cds_delivery=cds.delivery if cds is not None else None,
            cds_failure_audit=cds.audit if cds is not None else None,
            cds_delivery_cron=(cds.delivery_cron if cds is not None else None),
        )

    def _create_cds_runtime(self) -> _CdsRuntime | None:
        """설정이 활성화된 경우 CDS 전달 모듈을 조립한다."""
        if not self._settings.cds_delivery_enabled:
            return None

        api_url = self._settings.cds_api_url

        if api_url is None:
            raise RuntimeError(
                "CDS delivery configuration is incomplete",
            )

        spool = LocalFileCdsAnalysisLogQueueAdapter(
            self._settings.cds_log_dir,
            src_port=self._settings.cds_src_port,
            dst_host=self._settings.cds_dst_host,
            dst_port=self._settings.cds_dst_port,
            logger=self._logger,
        )
        delivery = HttpCdsDeliveryAdapter(
            api_url,
            self._settings.cds_api_key,
            timeout_seconds=(self._settings.cds_request_timeout_seconds),
            src_port=self._settings.cds_src_port,
            dst_host=self._settings.cds_dst_host,
            dst_port=self._settings.cds_dst_port,
        )
        audit = HttpManagementAuditNotificationAdapter(
            self._settings.management_api_url,
            timeout_seconds=(self._settings.management_request_timeout_seconds),
        )

        send_use_case = SendPendingCdsLogsUseCase(
            spool,
            delivery,
            audit=audit,
            batch_size=self._settings.cds_delivery_batch_size,
            logger=self._logger,
        )
        delivery_cron = CdsLogDeliveryCron(
            send_use_case,
            self._settings.cds_delivery_interval_seconds,
            shutdown_timeout_seconds=(
                self._settings.cds_request_timeout_seconds
                + self._settings.management_request_timeout_seconds
                + 1
            ),
            logger=self._logger,
        )

        return _CdsRuntime(
            queue=spool,
            delivery=delivery,
            audit=audit,
            delivery_cron=delivery_cron,
        )

    def _start_cds_delivery_cron(
        self,
        delivery_cron: CdsLogDeliveryCron,
    ) -> None:
        if delivery_cron.start() == ERR_OK:
            self._logger.info(
                "CDS log delivery scheduler started (interval=%ss batch_size=%s)",
                self._settings.cds_delivery_interval_seconds,
                self._settings.cds_delivery_batch_size,
            )

    def _assemble_analysis_log_runtime(
        self,
        cds_delivery: AnalysisLogDeliveryPort | None,
    ) -> _AnalysisLogRuntime:
        store, merge_connecter = self._create_analysis_log_merge_store()
        delivery = FluentBitAnalysisLogDeliveryAdapter(
            self._settings.log_bulk_dir,
            logger=self._logger,
        )
        attachment_outbox, attachment_connecter = (
            self._create_attachment_notification_outbox()
        )
        attachment_service = self._create_attachment_notification_service(
            attachment_outbox,
        )
        (
            policy_outbox,
            policy_connecter,
        ) = self._create_policy_notification_outbox()
        user_outbox, user_connecter = self._create_user_notification_outbox()
        merge_service = AnalysisLogMergeService(
            store,
            delivery,
            additional_deliveries=((cds_delivery,) if cds_delivery is not None else ()),
            policy_notification_outbox=policy_outbox,
            user_notification_outbox=user_outbox,
            logger=self._logger,
        )
        expiry_use_case = ExpireAnalysisLogMergeUseCase(
            store=store,
            merge_service=merge_service,
            logger=self._logger,
        )
        expiry_cron = AnalysisLogMergeExpiryCron(
            expiry_use_case,
            self._settings.log_merge_input_sweep_interval_seconds,
            logger=self._logger,
        )
        if expiry_cron.start() == ERR_OK:
            self._logger.info(
                "analysis-log expiry scheduler started (interval=%ss)",
                self._settings.log_merge_input_sweep_interval_seconds,
            )
        return _AnalysisLogRuntime(
            store=store,
            merge_valkey_connecter=merge_connecter,
            delivery=delivery,
            merge_service=merge_service,
            expiry_cron=expiry_cron,
            attachment_notification_service=attachment_service,
            attachment_notification_outbox=attachment_outbox,
            attachment_valkey_connecter=attachment_connecter,
            policy_notification_outbox=policy_outbox,
            user_notification_outbox=user_outbox,
            policy_notification_valkey_connecter=policy_connecter,
            user_notification_valkey_connecter=user_connecter,
        )

    def _create_analysis_log_merge_store(
        self,
    ) -> tuple[AnalysisLogMergeStorePort, ValkeyConnecter | None]:
        mirror = self._aivax_config.get_config().is_mirror
        ttl_seconds = (
            self._settings.mirror_ttl_seconds
            if mirror
            else self._settings.analysis_log_merge_ttl_seconds
        )
        input_flush_after_seconds = (
            self._settings.mirror_input_flush_after_seconds
            if mirror
            else self._settings.analysis_log_merge_input_flush_after_seconds
        )
        late_output_ttl_seconds = (
            self._settings.analysis_log_merge_late_output_ttl_seconds
        )
        if self._settings.analysis_log_merge_backend == "memory":
            return (
                MemoryAnalysisLogMergeStore(
                    ttl_seconds=ttl_seconds,
                    input_flush_after_seconds=input_flush_after_seconds,
                    late_output_ttl_seconds=late_output_ttl_seconds,
                ),
                None,
            )

        connecter = ValkeyConnecter(self._settings)
        store = ValkeyAnalysisLogMergeStore(
            connecter.connect(),
            key_prefix=self._settings.analysis_log_merge_key_prefix,
            ttl_seconds=ttl_seconds,
            input_flush_after_seconds=input_flush_after_seconds,
            late_output_ttl_seconds=late_output_ttl_seconds,
            logger=self._logger,
        )
        return store, connecter

    def _create_attachment_notification_outbox(
        self,
    ) -> tuple[
        AttachmentNotificationOutboxPort,
        ValkeyConnecter,
    ]:
        connecter = ValkeyConnecter(self._settings)
        outbox = ValkeyAttachmentNotificationOutboxAdapter(
            connecter.connect(verify=False),
            self._settings.event_attachment_stream_name,
            flush_interval_seconds=self._settings.event_flush_interval_seconds,
            event_dedupe_ttl_seconds=self._settings.event_dedupe_ttl_seconds,
            logger=self._logger,
        )
        return outbox, connecter

    def _create_attachment_notification_service(
        self,
        outbox: AttachmentNotificationOutboxPort | None,
    ) -> AttachmentNotificationService | None:
        if outbox is None:
            return None
        return AttachmentNotificationService(
            outbox,
            self._settings.event_attachment_index_name,
            logger=self._logger,
        )

    def _create_policy_notification_outbox(
        self,
    ) -> tuple[
        PolicyNotificationOutboxPort,
        ValkeyConnecter,
    ]:
        connecter = ValkeyConnecter(self._settings)
        return (
            ValkeyPolicyNotificationOutboxAdapter(
                connecter.connect(verify=False),
                self._settings.event_policy_notification_stream_name,
                flush_interval_seconds=self._settings.event_flush_interval_seconds,
                event_dedupe_ttl_seconds=self._settings.event_dedupe_ttl_seconds,
                logger=self._logger,
            ),
            connecter,
        )

    def _create_user_notification_outbox(
        self,
    ) -> tuple[
        UserNotificationOutboxPort,
        ValkeyConnecter,
    ]:
        connecter = ValkeyConnecter(self._settings)
        return (
            ValkeyUserNotificationOutboxAdapter(
                connecter.connect(verify=False),
                self._settings.event_user_sync_stream_name,
                flush_interval_seconds=self._settings.event_flush_interval_seconds,
                logger=self._logger,
            ),
            connecter,
        )

    def _assemble_policy_runtime(self) -> _PolicyRuntime:
        valkey_connecter = ValkeyConnecter(self._settings)
        client = valkey_connecter.connect(verify=False)

        ai_service_catalog = ValkeyAiServiceCatalog(
            client,
            self._settings.ai_service_catalog_key,
            backup_path=(
                Path(self._settings.runtime_dir)
                / "policy-cache"
                / "ai-services-v1.json"
            ),
            logger=self._logger,
        )
        refresh_ai_service_catalog = RefreshAiServiceCatalogUseCase(ai_service_catalog)
        ai_service_catalog_refresh_cron = self._create_policy_refresh_cron(
            refresh_ai_service_catalog, policy_name="ai-services"
        )

        regex_policy = ValkeyRegexPolicyAdapter(
            client,
            self._settings.policy_regex_key,
            backup_path=(
                Path(self._settings.runtime_dir) / "policy-cache" / "regex-v1.json"
            ),
            logger=self._logger,
        )
        refresh_regex_policy_use_case = RefreshRegexPolicyUseCase(regex_policy)
        regex_policy_refresh_cron = self._create_policy_refresh_cron(
            refresh_regex_policy_use_case,
            policy_name="regex",
        )

        multimodal_policy = ValkeyMultimodalPolicyAdapter(
            client,
            self._settings.policy_multimodal_key,
            default_allowed_extensions=self._settings.file_allowed_extensions,
            default_max_size_bytes=self._settings.file_max_size_bytes,
            backup_path=(
                Path(self._settings.runtime_dir) / "policy-cache" / "multimodal-v1.json"
            ),
            logger=self._logger,
        )
        refresh_multimodal_policy_use_case = RefreshMultimodalPolicyUseCase(
            multimodal_policy,
        )
        multimodal_policy_refresh_cron = self._create_policy_refresh_cron(
            refresh_multimodal_policy_use_case,
            policy_name="multimodal",
        )

        slm_policy = ValkeySlmPolicyAdapter(
            client,
            self._settings.policy_slm_key,
            backup_path=(
                Path(self._settings.runtime_dir) / "policy-cache" / "slm-v1.json"
            ),
            logger=self._logger,
        )
        refresh_slm_policy_use_case = RefreshSlmPolicyUseCase(slm_policy)
        slm_policy_refresh_cron = self._create_policy_refresh_cron(
            refresh_slm_policy_use_case,
            policy_name="slm",
        )

        secuxper_policy = ValkeySecuxperPolicyAdapter(
            client,
            self._settings.policy_secuxper_key,
            backup_path=Path(self._settings.runtime_dir)
            / "policy-cache"
            / "secuxper-v1.json",
            logger=self._logger,
        )
        refresh_secuxper_policy_use_case = RefreshSecuxperPolicyUseCase(secuxper_policy)
        secuxper_policy_refresh_cron = self._create_policy_refresh_cron(
            refresh_secuxper_policy_use_case,
            policy_name="secuxper",
        )

        refresh_stream_listener = PolicyRefreshStreamListener(
            client,
            self._settings.policy_refresh_stream_name,
            lambda: self._refresh_all_policies(
                refresh_ai_service_catalog,
                refresh_regex_policy_use_case,
                refresh_multimodal_policy_use_case,
                refresh_slm_policy_use_case,
                refresh_secuxper_policy_use_case,
            ),
            block_ms=self._settings.policy_refresh_stream_block_ms,
            logger=self._logger,
        )
        # cursor를 먼저 기록해야 시작 snapshot과 이후 Stream 이벤트 사이에
        # 갱신 신호가 유실되지 않는다.
        refresh_stream_listener.prepare()

        self._refresh_policy_at_start("ai-services", refresh_ai_service_catalog)

        regex_result = self._refresh_policy_at_start(
            "regex",
            refresh_regex_policy_use_case,
        )
        multimodal_result = self._refresh_policy_at_start(
            "multimodal",
            refresh_multimodal_policy_use_case,
        )
        slm_result = self._refresh_policy_at_start(
            "slm",
            refresh_slm_policy_use_case,
        )
        secuxper_result = self._refresh_policy_at_start(
            "secuxper", refresh_secuxper_policy_use_case
        )
        self._logger.info(
            "policy cache initialized (regex=%s, multimodal=%s, slm=%s, secuxper=%s)",
            self._policy_result_summary(regex_result),
            self._policy_result_summary(multimodal_result),
            self._policy_result_summary(slm_result),
            self._policy_result_summary(secuxper_result),
        )
        return _PolicyRuntime(
            valkey_connecter=valkey_connecter,
            ai_service_catalog=ai_service_catalog,
            ai_service_catalog_refresh_cron=ai_service_catalog_refresh_cron,
            regex_policy=regex_policy,
            regex_inspection=PythonRegexInspectionAdapter(regex_policy),
            refresh_regex_policy_use_case=refresh_regex_policy_use_case,
            regex_policy_refresh_cron=regex_policy_refresh_cron,
            multimodal_policy=multimodal_policy,
            refresh_multimodal_policy_use_case=(refresh_multimodal_policy_use_case),
            multimodal_policy_refresh_cron=multimodal_policy_refresh_cron,
            slm_policy=slm_policy,
            refresh_slm_policy_use_case=refresh_slm_policy_use_case,
            slm_policy_refresh_cron=slm_policy_refresh_cron,
            secuxper_policy=secuxper_policy,
            refresh_secuxper_policy_use_case=refresh_secuxper_policy_use_case,
            secuxper_policy_refresh_cron=secuxper_policy_refresh_cron,
            refresh_stream_listener=refresh_stream_listener,
            refresh_signal_publisher=ValkeyPolicyRefreshSignalPublisher(
                client,
                self._settings.policy_refresh_stream_name,
            ),
        )

    def _refresh_all_policies(
        self,
        ai_services: RefreshAiServiceCatalogUseCase,
        regex: RefreshRegexPolicyUseCase,
        multimodal: RefreshMultimodalPolicyUseCase,
        slm: RefreshSlmPolicyUseCase,
        secuxper: RefreshSecuxperPolicyUseCase,
    ) -> None:
        for policy_name, use_case in (
            ("ai-services", ai_services),
            ("regex", regex),
            ("multimodal", multimodal),
            ("slm", slm),
            ("secuxper", secuxper),
        ):
            try:
                use_case.execute()
            except Exception:
                # 한 policy의 일시 오류가 다른 local cache의 즉시 갱신을
                # 막지 않으며, polling cron은 실패한 policy를 재시도한다.
                self._logger.exception(
                    "policy refresh stream event failed (policy=%s)",
                    policy_name,
                )

    def _create_policy_refresh_cron(
        self,
        use_case: _RefreshPolicyUseCase,
        *,
        policy_name: str,
    ) -> PolicyRefreshCron:
        return PolicyRefreshCron(
            use_case,
            self._settings.policy_poll_interval_seconds,
            policy_name=policy_name,
            logger=self._logger,
        )

    def _refresh_policy_at_start(
        self,
        policy_name: str,
        use_case: _RefreshPolicyUseCase,
    ) -> object | None:
        try:
            return use_case.execute()
        except Exception:
            # 시작은 보존하고 scheduler가 마지막 정상 snapshot을 다시 갱신한다.
            self._logger.exception(
                "policy cache initialization failed (policy=%s); "
                "the scheduler will retry",
                policy_name,
            )
            return None

    @staticmethod
    def _policy_result_summary(refresh_result: object | None) -> str:
        if refresh_result is None:
            return "unavailable"
        active_rule_count = getattr(refresh_result, "active_rule_count", None)
        if isinstance(active_rule_count, int):
            return f"{active_rule_count} rules"
        return "ready"

    def _create_slm_inspection_adapter(self) -> SlmInspectionPort:
        settings = SlmHttpSettings.from_runtime_settings(self._settings)
        client = self._http_client_registry.get_or_create(
            "slm_inspection",
            HttpClientOptions(
                max_connections=settings.max_connections,
                max_keepalive_connections=settings.max_keepalive_connections,
                default_timeout_seconds=float(settings.request_timeout_seconds),
            ),
        )
        return create_slm_inspection_adapter(self._settings, client)

    def _create_secuxper_inspection_adapter(
        self, valkey_connecter: ValkeyConnecter
    ) -> SecuxperInspectionPort:
        from src.adapters.inspection_secuxper.factory import (
            create_secuxper_inspection_adapter,
        )

        settings = SecuxperaiSettings.from_runtime_settings(self._settings)
        client = self._http_client_registry.get_or_create(
            "secuxper_inspection",
            HttpClientOptions(
                max_connections=settings.max_connections,
                max_keepalive_connections=settings.max_keepalive_connections,
                default_timeout_seconds=float(settings.request_timeout_seconds),
            ),
        )
        token_store = ValkeySecuxperaiTokenStoreAdapter(
            valkey_connecter.connect(verify=False),
            token_key=settings.token_key,
            lock_key=settings.token_lock_key,
        )
        return create_secuxper_inspection_adapter(
            self._settings, client, secuxperai_token_store=token_store
        )

    def _start_policy_refresh_crons(self, policy: _PolicyRuntime) -> None:
        policy.ai_service_catalog_refresh_cron.start()
        policy.regex_policy_refresh_cron.start()
        policy.multimodal_policy_refresh_cron.start()
        policy.slm_policy_refresh_cron.start()
        policy.secuxper_policy_refresh_cron.start()
        self._logger.info(
            "policy refresh schedulers started "
            "(policies=regex,multimodal,slm,secuxper interval=%ss)",
            self._settings.policy_poll_interval_seconds,
        )

    def assemble_analysis_modules(
        self,
        runtime_modules: PipelineRuntimeModules,
    ) -> PipelineAnalysisModules:
        """HTTP controller가 호출할 유스케이스와 controller를 조립한다."""
        output_analysis_use_case = self._create_output_analysis_use_case(
            runtime_modules,
        )
        output_analysis_controller = OutputAnalysisController(output_analysis_use_case)

        publish_policy_refresh_signal_use_case = (
            self._create_publish_policy_refresh_signal_use_case(runtime_modules)
        )
        policy_refresh_controller = PolicyRefreshController(
            publish_policy_refresh_signal_use_case,
        )

        inspection_services = self._create_input_inspection_services(
            runtime_modules,
        )
        input_analysis_use_case = self._create_input_analysis_use_case(
            runtime_modules,
            inspection_services,
        )
        input_analysis_controller = InputAnalysisController(input_analysis_use_case)

        test_filter_rule_use_case = self._create_test_filter_rule_use_case(
            runtime_modules,
            inspection_services.multimodal,
            inspection_services.slm,
        )
        test_filter_rule_controller = FilterRuleTestController(
            test_filter_rule_use_case,
        )

        return PipelineAnalysisModules(
            input_analysis_use_case=input_analysis_use_case,
            input_analysis_controller=input_analysis_controller,
            output_analysis_use_case=output_analysis_use_case,
            output_analysis_controller=output_analysis_controller,
            publish_policy_refresh_signal_use_case=(
                publish_policy_refresh_signal_use_case
            ),
            policy_refresh_controller=policy_refresh_controller,
            test_filter_rule_use_case=test_filter_rule_use_case,
            test_filter_rule_controller=test_filter_rule_controller,
            health_controller=self._create_health_controller(),
            input_analysis_v2_controller=InputAnalysisControllerV2(
                input_analysis_use_case
            ),
            output_analysis_v2_controller=OutputAnalysisControllerV2(
                output_analysis_use_case
            ),
            input_guard_runtime=self._input_guard_runtime,
        )

    def _create_health_controller(self) -> HealthController:
        adapter = RuntimeHealthAdapter(
            self._settings,
            log_directory=self._settings.trace_log_path,
            bulk_directory=self._settings.log_bulk_dir,
            cds_directory=(
                self._settings.cds_log_dir
                if self._settings.cds_delivery_enabled
                else None
            ),
        )
        return HealthController(CheckHealthUseCase(adapter.probes()))

    @staticmethod
    def _create_output_analysis_use_case(
        runtime_modules: PipelineRuntimeModules,
    ) -> OutputAnalysisUseCase:
        return OutputAnalysisUseCase(
            runtime_modules.analysis_log_merge_service,
            OutputLogOptions(),
            runtime_modules.attachment_notification_service,
            ai_service_catalog=runtime_modules.ai_service_catalog,
        )

    def _create_publish_policy_refresh_signal_use_case(
        self,
        runtime_modules: PipelineRuntimeModules,
    ) -> PublishPolicyRefreshSignalUseCase:
        return PublishPolicyRefreshSignalUseCase(
            runtime_modules.policy_refresh_signal_publisher,
        )

    def _phrase_mask_token(self) -> str:
        """문구 파일의 mask_token. 비어 있으면 환경변수 값을 쓴다."""
        token = get_block_phrases().mask_token.strip()
        return token or self._settings.mask_token

    def _create_input_analysis_use_case(
        self,
        runtime_modules: PipelineRuntimeModules,
        inspection_services: _InputInspectionServices,
    ) -> InputAnalysisUseCase:
        secuxper_inspector = None
        if runtime_modules.aivax_config.get_config().filters.secuxper.enabled:
            if (
                not self._settings.secuxperai_team_uuid
                or not self._settings.secuxperai_client_no
            ):
                raise ValueError(
                    "SecuXper 활성화에는 TEAM_UUID와 CLIENT_NO가 필요합니다."
                )
            if runtime_modules.secuxper_policy is None:
                raise ValueError("secuXper policy runtime is required")
            secuxper_inspector = SecuxperInputInspector(
                SecuxperInspectionService(
                    self._create_secuxper_inspection_adapter(
                        runtime_modules.policy_valkey_connecter,
                    ),
                    runtime_modules.secuxper_policy,
                    mask_token=self._phrase_mask_token(),
                    logger=self._logger,
                ),
            )
        return InputAnalysisUseCase(
            input_guard=self._create_input_guard(runtime_modules),
            secuxper_inspector=secuxper_inspector,
            regex_inspector=RegexInputInspector(
                runtime_modules.regex_inspection,
                mask_token=self._phrase_mask_token(),
            ),
            multimodal_inspector=MultimodalInputInspector(
                inspection_services.multimodal,
                runtime_modules.file_delivery,
                runtime_modules.attachment_notification_service,
                backup_skipped_oversized_files=(
                    self._settings.backup_skipped_oversized_files
                ),
            ),
            slm_inspector=SlmInputInspector(
                inspection_services.slm,
                mask_token=self._phrase_mask_token(),
            ),
            mask_token=self._phrase_mask_token(),
            options=self._create_input_analysis_options(),
            input_log_builder=InputAnalysisLogBuilder(runtime_modules.ai_service_catalog),
            analysis_log_merge_service=runtime_modules.analysis_log_merge_service,
            aivax_config=runtime_modules.aivax_config,
            consent_log_writer=cast(
                AnalysisLogConsentWriterPort, runtime_modules.analysis_log_delivery
            ),
            logger=self._logger,
        )

    def _create_input_guard(
        self,
        runtime_modules: PipelineRuntimeModules,
    ) -> InputGuardPort | None:
        if not self._settings.cso_enabled:
            return None
        # 선택 기능의 구체 구현과 vendor 의존성은 활성 배포에서만 로드한다.
        from src.extensions.cso.factory import create_cso_grade_reader
        from src.extensions.cso.guard import CsoInputGuard

        reader, runtime = create_cso_grade_reader(
            self._settings.cso_provider,
            directory=Path(__file__).resolve().parents[2] / "third_party/fasoo/cso",
            timeout_seconds=self._settings.cso_request_timeout_seconds,
            startup_timeout_seconds=self._settings.cso_startup_timeout_seconds,
            logger=self._logger,
        )
        self._input_guard_runtime = runtime
        return CsoInputGuard(
            reader,
            skip_unsupported_extensions_enabled=(
                self._settings.cso_skip_unsupported_extensions_enabled
            ),
            skip_untagged_enabled=self._settings.cso_skip_untagged_enabled,
            skip_error_enabled=self._settings.cso_skip_error_enabled,
            file_delivery=runtime_modules.file_delivery,
            input_log_builder=InputAnalysisLogBuilder(runtime_modules.ai_service_catalog),
            analysis_log_merge_service=runtime_modules.analysis_log_merge_service,
            consent_log_writer=cast(
                AnalysisLogConsentWriterPort, runtime_modules.analysis_log_delivery
            ),
            attachment_notification_service=runtime_modules.attachment_notification_service,
            logger=self._logger,
        )

    def _create_input_inspection_services(
        self,
        runtime_modules: PipelineRuntimeModules,
    ) -> _InputInspectionServices:
        return _InputInspectionServices(
            multimodal=MultimodalInspectionService(
                runtime_modules.multimodal_inspection,
                runtime_modules.multimodal_policy,
                self._create_multimodal_inspection_options(),
                max_inflight=(self._settings.inspect_multimodal_file_max_inflight),
                acquire_timeout_seconds=(
                    self._settings.inspect_multimodal_acquire_timeout_seconds
                ),
            ),
            slm=SlmInspectionService(
                runtime_modules.slm_inspection,
                runtime_modules.slm_policy,
                SlmInspectionOptions(
                    max_inflight=self._settings.inspect_slm_max_inflight,
                    queue_skip_after=self._settings.inspect_slm_queue_skip_after,
                ),
                logger=self._logger,
            ),
        )

    def _create_test_filter_rule_use_case(
        self,
        runtime_modules: PipelineRuntimeModules,
        multimodal_inspection_service: MultimodalInspectionService,
        slm_inspection_service: SlmInspectionService,
    ) -> FilterRuleTestUseCase:
        return FilterRuleTestUseCase(
            runtime_modules.regex_inspection,
            multimodal_inspection_service,
            slm_inspection_service,
            mask_token=self._phrase_mask_token(),
        )

    def _create_input_analysis_options(self) -> InputAnalysisOptions:
        bypass_bitmask = self._settings.ssl_proxy_bypass_bitmask
        return InputAnalysisOptions(
            bypass_block=bool(bypass_bitmask & _SSL_PROXY_BYPASS_BLOCK),
            bypass_masking=bool(bypass_bitmask & _SSL_PROXY_BYPASS_MASKING),
            user_consent_global_bypass_enabled=(
                self._settings.user_consent_global_bypass_enabled
            ),
            user_consent_regex_enabled=self._settings.user_consent_regex_enabled,
            user_consent_multimodal_enabled=self._settings.user_consent_multimodal_enabled,
            user_consent_slm_enabled=self._settings.user_consent_slm_enabled,
            user_consent_secuxper_enabled=self._settings.user_consent_secuxper_enabled,
        )

    def _create_multimodal_inspection_options(
        self,
    ) -> MultimodalInspectionOptions:
        return MultimodalInspectionOptions(
            bypass=self._settings.file_bypass_mode,
            read_timeout_seconds=self._settings.file_read_timeout_seconds,
            unsupported_file_handling=self._settings.unsupported_file_handling,
            oversized_file_handling=self._settings.oversized_file_handling,
            content_chunk_size=self._settings.file_content_chunk_size,
            detail_reason_directory=self._settings.detail_reason_temp_dir,
            watermark_criteria=WatermarkCriteria(
                drm_enabled=self._settings.watermark_use_regex_base_drm,
                drm_pattern=self._settings.watermark_drm_detect_pattern,
                drm_pattern_flags=self._settings.watermark_drm_pattern_flag,
                drm_header_limit=self._settings.watermark_drm_file_header_limit,
                ocr_enabled=self._settings.watermark_use_ocr_base_watermark,
                ocr_patterns=tuple(self._settings.watermark_ocr_patterns),
                ocr_pattern_flags=self._settings.watermark_ocr_pattern_flag,
                ocr_hit_threshold=self._settings.watermark_ocr_max_hitcount,
                ocr_pages=tuple(self._settings.watermark_ocr_pages),
            ),
        )

    def shutdown_runtime_modules(self, runtime_modules: PipelineRuntimeModules) -> int:
        """bootstrap이 생성한 background resource를 역순으로 종료한다."""
        policy_stopped = all(
            (
                runtime_modules.policy_refresh_stream_listener.shutdown(),
                runtime_modules.ai_service_catalog_refresh_cron.shutdown(),
                (
                    runtime_modules.secuxper_policy_refresh_cron.shutdown()
                    if runtime_modules.secuxper_policy_refresh_cron
                    else True
                ),
                runtime_modules.slm_policy_refresh_cron.shutdown(),
                runtime_modules.multimodal_policy_refresh_cron.shutdown(),
                runtime_modules.regex_policy_refresh_cron.shutdown(),
            )
        )
        expiry_stopped = (
            runtime_modules.analysis_log_merge_expiry_cron.shutdown() == ERR_OK
        )
        if runtime_modules.cleanup_cron is not None:
            runtime_modules.cleanup_cron.shutdown()
        cds_stopped = (
            not (runtime_modules.cds_delivery_cron is not None)
            or runtime_modules.cds_delivery_cron.shutdown() == ERR_OK
        )

        if (
            expiry_stopped
            and runtime_modules.analysis_log_merge_valkey_connecter is not None
        ):
            runtime_modules.analysis_log_merge_valkey_connecter.disconnect()

        if (
            expiry_stopped
            and runtime_modules.attachment_notification_outbox is not None
        ):
            runtime_modules.attachment_notification_outbox.close()
        if expiry_stopped and runtime_modules.attachment_valkey_connecter is not None:
            runtime_modules.attachment_valkey_connecter.disconnect()

        if expiry_stopped and runtime_modules.policy_notification_outbox is not None:
            runtime_modules.policy_notification_outbox.close()
        if (
            expiry_stopped
            and runtime_modules.policy_notification_valkey_connecter is not None
        ):
            runtime_modules.policy_notification_valkey_connecter.disconnect()

        if expiry_stopped and runtime_modules.user_notification_outbox is not None:
            runtime_modules.user_notification_outbox.close()
        if (
            expiry_stopped
            and runtime_modules.user_notification_valkey_connecter is not None
        ):
            runtime_modules.user_notification_valkey_connecter.disconnect()

        # 종료되지 않은 CDS worker가 session을 사용 중일 수 있으므로 그 경우
        # CDS 자원만 유지하고 나머지 runtime 자원은 계속 정리한다.
        if cds_stopped and runtime_modules.cds_delivery is not None:
            runtime_modules.cds_delivery.close()
        if cds_stopped and runtime_modules.cds_failure_audit is not None:
            runtime_modules.cds_failure_audit.close()

        if policy_stopped:
            runtime_modules.policy_valkey_connecter.disconnect()

        return (
            ERR_OK if all((policy_stopped, expiry_stopped, cds_stopped)) else ERR_FAIL
        )
