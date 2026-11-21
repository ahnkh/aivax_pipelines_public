from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest.mock import ANY, AsyncMock, Mock, patch

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from src.bootstrap import env_loader
import main
from src.bootstrap import PipelineAnalysisModules, PipelineBootstrap
from src.application.use_cases.check_health import CheckHealthUseCase
from src.controllers.output_analysis.v2.output_analysis_controller import (
    OutputAnalysisControllerV2,
)
from src.controllers.health.health_controller import HealthController
from src.controllers.output_analysis.v1.output_analysis_controller import (
    OutputAnalysisController,
)


class MainEnvironmentTest(unittest.TestCase):
    def test_bootstrap_maps_environment_settings_directly_to_input_options(
        self,
    ) -> None:
        bootstrap = PipelineBootstrap(
            env_loader.PipelineSettings(
                ssl_proxy_bypass_bitmask=0b0011,
            ),
            http_client_registry=None,
            logger=Mock(),
        )

        options = bootstrap._create_input_analysis_options()

        self.assertTrue(options.bypass_block)
        self.assertTrue(options.bypass_masking)

    def test_bootstrap_injects_the_src_regex_adapter_directly(self) -> None:
        from types import SimpleNamespace

        bootstrap = PipelineBootstrap(
            env_loader.PipelineSettings(),
            http_client_registry=None,
            logger=Mock(),
        )
        regex_inspection = Mock()
        regex_policy = Mock()
        multimodal_inspection = Mock()
        multimodal_policy = Mock()
        slm_inspection = Mock()
        slm_policy = Mock()
        runtime = SimpleNamespace(
            ai_service_catalog=Mock(),
            analysis_log_merge_service=Mock(),
            regex_inspection=regex_inspection,
            regex_policy=regex_policy,
            multimodal_inspection=multimodal_inspection,
            multimodal_policy=multimodal_policy,
            slm_inspection=slm_inspection,
            slm_policy=slm_policy,
            file_delivery=Mock(),
            analysis_log_merge_store=Mock(),
            analysis_log_delivery=Mock(),
            aivax_config=Mock(),
            policy_refresh_signal_publisher=Mock(),
            attachment_notification_service=None,
            policy_notification_outbox=None,
            user_notification_outbox=None,
        )

        from src.domain.config_aivax.models import AivaxConfig

        runtime.aivax_config.get_config.return_value = AivaxConfig()
        modules = bootstrap.assemble_analysis_modules(runtime)

        self.assertIs(
            modules.input_analysis_v2_controller._use_case, modules.input_analysis_use_case
        )
        self.assertIs(
            modules.output_analysis_v2_controller._use_case, modules.output_analysis_use_case
        )

        regex_service = modules.input_analysis_use_case._regex_inspector
        self.assertIs(regex_service._inspection_port, regex_inspection)
        multimodal_service = modules.input_analysis_use_case._multimodal_inspector
        self.assertIs(
            multimodal_service._inspection_service._inspection,
            multimodal_inspection,
        )
        slm_input_inspector = modules.input_analysis_use_case._slm_inspector
        self.assertIs(
            slm_input_inspector._inspection_service._inspection,
            slm_inspection,
        )
        self.assertIs(
            slm_input_inspector._inspection_service._policy_provider,
            slm_policy,
        )
        signal_policy = modules.publish_policy_refresh_signal_use_case
        self.assertIs(
            signal_policy._publisher,
            runtime.policy_refresh_signal_publisher,
        )

    def test_env_loader_targets_the_repository_env_file(self) -> None:
        loader = env_loader.EnvironmentLoader(Path(__file__).resolve().parents[2])
        self.assertEqual(Path(__file__).resolve().parents[2] / ".env", loader.env_file)

    def test_env_file_selector_resolves_development_file_from_repository_root(
        self,
    ) -> None:
        loader = env_loader.EnvironmentLoader(Path(__file__).resolve().parents[2])
        self.assertEqual(
            Path(__file__).resolve().parents[2] / ".env.development",
            loader.resolve_env_file(".env.development"),
        )

    def test_runtime_settings_are_typed_and_derived_once(self) -> None:
        settings = env_loader.EnvironmentLoader().build_settings(
            {
                "AIVAX_PIPELINE_HOST": "0.0.0.0",
                "AIVAX_PIPELINE_PORT": "9100",
                "AIVAX_PIPELINE_WORKERS": "1",
                "AIVAX_PIPELINE_CORS_ORIGINS": "https://admin.example.test, https://ops.example.test",
                "AIVAX_PIPELINE_LOG_LEVEL": "debug",
                "AIVAX_PIPELINE_SSL_KEY_FILE": "./certs/server.key",
                "AIVAX_PIPELINE_SSL_CERT_FILE": "./certs/server.crt",
                "AIVAX_PIPELINE_MODE_FILE": "/tmp/aivax-mode.json",
                "AIVAX_PIPELINE_MODE_OVERRIDE": "mirror",
            }
        )

        self.assertEqual("0.0.0.0", settings.host)
        self.assertEqual(9100, settings.port)
        self.assertEqual(1, settings.workers)
        self.assertEqual(
            ("https://admin.example.test", "https://ops.example.test"),
            settings.cors_origins,
        )
        self.assertEqual("debug", settings.log_level)
        self.assertEqual("./certs/server.key", settings.ssl_key_file)
        self.assertEqual("./certs/server.crt", settings.ssl_cert_file)
        self.assertEqual("/tmp/aivax-mode.json", settings.product_mode_file)
        self.assertEqual("127.0.0.1", settings.valkey_host)
        self.assertEqual(6379, settings.valkey_port)

    def test_runtime_settings_reject_invalid_numbers(self) -> None:
        with self.assertRaises(env_loader.ConfigurationError):
            env_loader.EnvironmentLoader().build_settings(
                {"AIVAX_PIPELINE_PORT": "not-a-port", "AIVAX_PIPELINE_WORKERS": "0"}
            )

    def test_settings_has_complete_defaults_without_environment_values(self) -> None:
        loader = env_loader.EnvironmentLoader(
            Path("/tmp/aivax-pipeline-missing-env"), environ={}
        )
        loaded, settings = loader.bootstrap()

        self.assertFalse(loaded)
        self.assertEqual("127.0.0.1", settings.host)
        self.assertEqual(9099, settings.port)
        self.assertEqual(1, settings.workers)
        self.assertEqual(("*",), settings.cors_origins)
        self.assertEqual("info", settings.log_level)

    def test_main_uses_the_shared_settings_instance(self) -> None:
        self.assertIs(env_loader.SETTINGS, main.SETTINGS)

    def test_factory_assembles_one_bootstrap_for_one_fastapi_instance(self) -> None:
        runtime_modules = Mock()
        analysis_modules = Mock()
        with (
            patch.object(main, "configure_logging") as configure_logging,
            patch.object(main, "PipelineBootstrap") as bootstrap_class,
            patch.object(main, "setup_fast_api") as setup,
        ):
            bootstrap = bootstrap_class.return_value
            logger = configure_logging.return_value
            bootstrap.http_client_registry = main.HttpClientRegistry()
            bootstrap.assemble_runtime_modules.return_value = runtime_modules
            bootstrap.assemble_analysis_modules.return_value = analysis_modules
            application = main.create_app()

        configure_logging.assert_called_once_with(main.SETTINGS)
        bootstrap_class.assert_called_once_with(
            main.SETTINGS,
            ANY,
            logger,
        )
        bootstrap.assemble_runtime_modules.assert_called_once_with()
        bootstrap.assemble_analysis_modules.assert_called_once_with(runtime_modules)
        setup.assert_called_once_with(
            application,
            analysis_modules,
        )
        self.assertIsInstance(
            application.state.http_client_registry, main.HttpClientRegistry
        )
        self.assertIs(application.state.pipeline_bootstrap, bootstrap)
        self.assertIs(application.state.pipeline_runtime_modules, runtime_modules)
        self.assertIs(application.state.pipeline_analysis_modules, analysis_modules)

    def test_main_registers_assembled_output_controller(self) -> None:
        output_service = AsyncMock()
        output_service.execute.return_value = {"wired": True}
        assembled = PipelineAnalysisModules(
            input_analysis_use_case=Mock(),
            input_analysis_controller=Mock(router=APIRouter()),
            output_analysis_use_case=output_service,
            output_analysis_controller=OutputAnalysisController(output_service),
            publish_policy_refresh_signal_use_case=Mock(),
            policy_refresh_controller=Mock(router=APIRouter()),
            test_filter_rule_use_case=Mock(),
            test_filter_rule_controller=Mock(router=APIRouter()),
            health_controller=HealthController(CheckHealthUseCase({})),
            input_analysis_v2_controller=Mock(router=APIRouter()),
            output_analysis_v2_controller=OutputAnalysisControllerV2(output_service),
        )
        application = FastAPI()
        main.setup_fast_api(application, assembled)
        self.assertFalse(hasattr(application.state, "output_analysis_service"))
        routes = [
            route
            for route in application.routes
            if route.path == "/v1/filter/output_filter"
        ]
        self.assertEqual(len(routes), 1)
        self.assertIs(routes[0].endpoint.__self__, assembled.output_analysis_controller)
        with (
            self.assertLogs("khan-python", level="DEBUG") as request_logs,
            TestClient(application) as client,
        ):
            response = client.post(
                "/v1/filter/output_filter", json={"llm_output": "응답"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["out_result"], {"wired": True})
        self.assertTrue(
            any(
                "http request " in entry and "응답" in entry
                for entry in request_logs.output
            )
        )
        output_service.execute.assert_awaited_once()
        with TestClient(application) as client:
            response = client.post(
                "/v2/filter/output_filter",
                json={"message_id": "v2-wired", "llm_output": "응답"},
            )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertEqual(output_service.execute.await_count, 2)

    def test_uvicorn_uses_runtime_settings_without_cli_arguments(self) -> None:
        settings = env_loader.EnvironmentLoader().build_settings(
            {
                "AIVAX_PIPELINE_HOST": "0.0.0.0",
                "AIVAX_PIPELINE_PORT": "9100",
                "AIVAX_PIPELINE_WORKERS": "1",
                "AIVAX_PIPELINE_LOG_LEVEL": "warning",
                "AIVAX_PIPELINE_SSL_KEY_FILE": "./certs/server.key",
                "AIVAX_PIPELINE_SSL_CERT_FILE": "./certs/server.crt",
            }
        )

        with (
            patch.object(main, "SETTINGS", settings),
            patch.object(main, "create_app") as create_app,
            patch.object(main.uvicorn, "run") as run,
        ):
            main.run_uvicorn()

        create_app.assert_called_once_with()
        run.assert_called_once_with(
            create_app.return_value,
            host="0.0.0.0",
            port=9100,
            workers=1,
            ssl_keyfile="./certs/server.key",
            ssl_certfile="./certs/server.crt",
            log_level="warning",
            loop="uvloop",
            http="httptools",
        )

    def test_multiple_workers_are_rejected_for_memory_log_merge(self) -> None:
        with self.assertRaises(env_loader.ConfigurationError):
            env_loader.EnvironmentLoader().build_settings(
                {
                    "AIVAX_PIPELINE_WORKERS": "4",
                    "AIVAX_PIPELINE_ANALYSIS_LOG_MERGE_BACKEND": "memory",
                },
            )

    def test_bootstrap_selects_memory_analysis_log_merge_store(self) -> None:
        from types import SimpleNamespace

        from src.adapters.analysis_log_merge import MemoryAnalysisLogMergeStore

        settings = env_loader.PipelineSettings(
            analysis_log_merge_backend="memory",
        )
        bootstrap = PipelineBootstrap(settings, None, Mock())
        bootstrap._aivax_config = Mock()
        bootstrap._aivax_config.get_config.return_value = SimpleNamespace(
            is_mirror=False,
        )

        store, owner = bootstrap._create_analysis_log_merge_store()

        self.assertIsInstance(store, MemoryAnalysisLogMergeStore)
        self.assertIsNone(owner)

    def test_bootstrap_selects_valkey_analysis_log_merge_store(self) -> None:
        from types import SimpleNamespace

        from src.adapters.analysis_log_merge import ValkeyAnalysisLogMergeStore

        settings = env_loader.PipelineSettings(
            analysis_log_merge_backend="valkey",
            analysis_log_merge_key_prefix="pipeline:test:merge:",
        )
        bootstrap = PipelineBootstrap(settings, None, Mock())
        bootstrap._aivax_config = Mock()
        bootstrap._aivax_config.get_config.return_value = SimpleNamespace(
            is_mirror=False,
        )
        client = Mock()

        with patch(
            "src.bootstrap.pipeline_bootstrap.ValkeyConnecter",
        ) as connecter_class:
            connecter = connecter_class.return_value
            connecter.connect.return_value = client
            store, owner = bootstrap._create_analysis_log_merge_store()

        self.assertIsInstance(store, ValkeyAnalysisLogMergeStore)
        self.assertIs(owner, connecter)
        self.assertEqual(store._key_prefix, "pipeline:test:merge:")
        connecter.connect.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()


def test_bootstrap_builds_separate_slm_and_secuxper_adapters():
    from unittest.mock import patch

    settings = env_loader.PipelineSettings(
        secuxperai_team_uuid="test-team",
        secuxperai_client_no="test-client",
    )
    registry = Mock()
    connector = Mock()
    bootstrap = PipelineBootstrap(
        settings, http_client_registry=registry, logger=Mock()
    )
    with (
        patch("src.bootstrap.pipeline_bootstrap.create_slm_inspection_adapter") as slm,
        patch(
            "src.adapters.inspection_secuxper.factory.create_secuxper_inspection_adapter"
        ) as secuxper,
    ):
        assert bootstrap._create_slm_inspection_adapter() is slm.return_value
        assert (
            bootstrap._create_secuxper_inspection_adapter(connector)
            is secuxper.return_value
        )
    assert [call.args[0] for call in registry.get_or_create.call_args_list] == [
        "slm_inspection",
        "secuxper_inspection",
    ]
    slm.assert_called_once()
    secuxper.assert_called_once()


def test_toolkit_disabled_does_not_start_cleanup_thread():
    from unittest.mock import patch

    for enabled in (False, True):
        settings = env_loader.PipelineSettings(
            toolkit_enabled=enabled, pdf_evidence_engine="pymupdf"
        )
        bootstrap = PipelineBootstrap(settings, http_client_registry=Mock(), logger=Mock())
        with (
            patch.object(bootstrap, "_create_cds_runtime", return_value=None),
            patch.object(bootstrap, "_assemble_analysis_log_runtime", return_value=Mock()),
            patch.object(bootstrap, "_assemble_policy_runtime", return_value=Mock()),
            patch.object(bootstrap, "_create_slm_inspection_adapter", return_value=Mock()),
            patch.object(bootstrap, "_start_policy_refresh_crons"),
            patch("src.bootstrap.pipeline_bootstrap.CleanupCron") as cleanup,
            patch(
                "src.bootstrap.pipeline_bootstrap.LocalMultimodalInspectionAdapter"
            ) as multimodal,
            patch("src.bootstrap.pipeline_bootstrap.LocalFileDeliveryAdapter"),
        ):
            runtime = bootstrap.assemble_runtime_modules()
        multimodal.assert_called_once_with(
            ocr_max_inflight=settings.inspect_multimodal_ocr_max_inflight,
            ocr_timeout_seconds=max(1, settings.file_read_timeout_seconds),
            pdf_evidence_engine="pymupdf",
        )
        if enabled:
            cleanup.return_value.start.assert_called_once_with()
            assert runtime.cleanup_cron is cleanup.return_value
        else:
            cleanup.assert_not_called()
            assert runtime.cleanup_cron is None
