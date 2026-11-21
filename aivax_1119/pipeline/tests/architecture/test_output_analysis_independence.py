"""출력 API의 레거시 의존성 재유입과 독립 실행을 검증한다."""

import ast
from pathlib import Path
import subprocess
import sys
import textwrap


ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINTS = (
    "src.schedulers.analysis_log_merge_expiry_cron",
    "src.controllers.output_analysis.v1.output_analysis_controller",
    "src.application.use_cases.expire_analysis_log_merge.use_case",
    "src.application.use_cases.output_analysis.use_case",
    "src.application.services.analysis_log_merge.service",
    "src.adapters.analysis_log_merge.memory_analysis_log_merge_store",
    "src.adapters.analysis_log_delivery.fluent_bit_analysis_log_delivery_adapter",
    "src.adapters.notification_user.valkey_user_notification_outbox_adapter",
    "src.adapters.notification_policy.valkey_policy_notification_outbox_adapter",
    "src.core.valkey.connecter",
)


def test_output_import_graph_has_no_project_dependencies_outside_src():
    pending = list(ENTRYPOINTS)
    visited = set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        relative = Path(*name.split("."))
        path = ROOT / relative.with_suffix(".py")
        if not path.is_file():
            path = ROOT / relative / "__init__.py"
        if not path.is_file():
            continue
        # 패키지 초기화를 통해 유입되는 간접 의존성도 검사한다.
        for parent in relative.parents:
            if (ROOT / parent / "__init__.py").is_file():
                pending.append(".".join(parent.parts))
        for node in ast.walk(ast.parse(path.read_text())):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, (
                    f"검사 가능한 절대 import를 사용해야 합니다: {path}"
                )
                modules = [node.module or ""]
                modules += [f"{node.module}.{alias.name}" for alias in node.names]
            for module in modules:
                top = module.split(".")[0]
                is_project = (ROOT / top).is_dir() or (ROOT / f"{top}.py").is_file()
                assert not is_project or top == "src", (
                    f"{path.relative_to(ROOT)} -> {module}"
                )
                if top == "src":
                    pending.append(module)


def test_output_http_to_json_runs_without_legacy_imports(tmp_path):
    # 기존 pytest 세션에서 이미 import된 main/lib_include의 도움을 받지 않도록 새 프로세스 사용.
    script = textwrap.dedent("""
        import importlib.abc
        import json
        from pathlib import Path
        import sys
        from unittest.mock import Mock

        class NoLegacy(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split('.')[0] in {
                    'main', 'mainapp', 'env_loader', 'lib_include', 'type_hint',
                    'pipelines', 'utils', 'shared', 'common_modules',
                    'block_filter_modules', 'local_common',
                }:
                    raise AssertionError('레거시 import: ' + fullname)
        sys.meta_path.insert(0, NoLegacy())

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from src.controllers.output_analysis.v1.output_analysis_controller import OutputAnalysisController
        from src.adapters.analysis_log_merge.memory_analysis_log_merge_store import MemoryAnalysisLogMergeStore
        from src.adapters.analysis_log_delivery.fluent_bit_analysis_log_delivery_adapter import FluentBitAnalysisLogDeliveryAdapter
        from src.adapters.notification_user.valkey_user_notification_outbox_adapter import ValkeyUserNotificationOutboxAdapter
        from src.adapters.notification_policy.valkey_policy_notification_outbox_adapter import ValkeyPolicyNotificationOutboxAdapter
        from src.application.use_cases.expire_analysis_log_merge import ExpireAnalysisLogMergeUseCase
        from src.core.valkey.connecter import ValkeyConnecter
        from src.application.services.analysis_log_merge import AnalysisLogMergeService
        from src.application.use_cases.output_analysis import OutputAnalysisUseCase
        from tests.ai_service_catalog import make_ai_service_catalog

        client = Mock()
        client.pipeline.return_value.execute.return_value = ['1-0']
        outbox = ValkeyUserNotificationOutboxAdapter(client, 'isolated', flush_interval_seconds=60)
        try:
            merge = AnalysisLogMergeService(
                MemoryAnalysisLogMergeStore(),
                FluentBitAnalysisLogDeliveryAdapter(sys.argv[1]),
                user_notification_outbox=outbox,
            )
            merge.record(0, 'isolated', {'message_id': 'isolated', 'prompt': '질문',
                'user': {'email': 'test@example.com'}, 'client_host': '127.0.0.1', 'ai_service': 'GPT'})
            app = FastAPI()
            app.include_router(OutputAnalysisController(OutputAnalysisUseCase(merge, ai_service_catalog=make_ai_service_catalog())).router)
            with TestClient(app) as http:
                response = http.post('/v1/filter/output_filter', json={
                    'message_id': 'isolated', 'llm_output': '응답', 'email': 'test@example.com',
                    'client_host': '127.0.0.1', 'ai_service': 1})
            assert response.status_code == 200, response.text
            document = json.loads(next(Path(sys.argv[1]).glob('*.json')).read_text())
            assert document['prompt'] == '질문' and document['output_text'] == '응답'
            assert document['schema_version'] == 1
            assert outbox.flush() == 1
            client.pipeline.return_value.xadd.assert_called_once()
        finally:
            outbox.close()
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
