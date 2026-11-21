import asyncio
import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from src.adapters.inspection_multimodal import LocalMultimodalInspectionAdapter
from src.adapters.policy_multimodal import ValkeyMultimodalPolicyAdapter
from src.adapters.policy_regex import ValkeyRegexPolicyAdapter
from src.adapters.policy_secuxper.valkey_secuxper_policy_adapter import (
    ValkeySecuxperPolicyAdapter,
)
from src.adapters.policy_slm import ValkeySlmPolicyAdapter
from src.application.services.inspection_slm import (
    SlmInspectionOptions,
    SlmInspectionService,
)
from src.bootstrap import PipelineBootstrap
from src.bootstrap.env_loader import PipelineSettings
from src.domain.inspection_multimodal.models import MultimodalInspectionRequest
from src.domain.inspection_slm.models import SlmInspectionResult
from src.domain.policy_multimodal.models import (
    MultimodalPolicyRule,
    MultimodalPolicySnapshot,
)
from src.domain.policy_regex.models import RegexPolicySnapshot
from src.domain.policy_slm.models import SlmPolicyRule, SlmPolicySnapshot


def _encoded(expression: str) -> str:
    return base64.b64encode(expression.encode()).decode()


def test_missing_policy_keys_are_applied_as_empty_snapshots() -> None:
    client = Mock()
    client.get.return_value = None
    regex = ValkeyRegexPolicyAdapter(client, "missing:regex")
    multimodal = ValkeyMultimodalPolicyAdapter(
        client,
        "missing:multimodal",
        default_allowed_extensions=("txt",),
        default_max_size_bytes=1024,
    )
    slm = ValkeySlmPolicyAdapter(client, "missing:slm")

    regex_result = regex.refresh()
    multimodal_result = multimodal.refresh()
    slm_result = slm.refresh()

    assert regex_result.active_rule_count == 0
    assert regex.get_policy() is not None
    assert regex.get_policy().rules == ()  # type: ignore[union-attr]
    assert multimodal_result.active_rule_count == 0
    assert multimodal.get_policy() == MultimodalPolicySnapshot(
        allowed_extensions=("txt",),
        max_size_bytes=1024,
    )
    assert slm_result.active_rule_count == 0
    assert slm.get_policy() == SlmPolicySnapshot()


def test_regex_adapter_loads_self_contained_projection() -> None:
    client = Mock()
    client.get.return_value = json.dumps(
        {
            "version": "regex-1",
            "group_members": {
                "group-1": [" USER@example.com ", "second@example.com"],
            },
            "rules": [
                {
                    "id": "regex-1",
                    "name": "secret",
                    "rule": _encoded("secret"),
                    "action": "block",
                    "category": "security",
                    "scope": "group",
                    "subject_id": "group-1",
                }
            ],
        }
    ).encode()
    adapter = ValkeyRegexPolicyAdapter(client, "pipeline:policy:regex:v1")

    result = adapter.refresh()

    assert result.active_rule_count == 1
    assert adapter.get_policy() is not None
    assert adapter.get_policy().rules[0].expression == "secret"  # type: ignore[union-attr]
    assert adapter.get_policy().group_members == (  # type: ignore[union-attr]
        ("group-1", ("user@example.com", "second@example.com")),
    )
    client.get.assert_called_once_with("pipeline:policy:regex:v1")


def test_regex_adapter_loads_policy_without_category() -> None:
    client = Mock()
    client.get.return_value = json.dumps(
        {
            "group_members": {},
            "rules": [
                {
                    "id": "regex-uncategorized",
                    "name": "uncategorized regex",
                    "rule": _encoded("secret"),
                    "action": "block",
                    "category": None,
                    "scope": "default",
                    "subject_id": "",
                }
            ],
        }
    )
    adapter = ValkeyRegexPolicyAdapter(client, "pipeline:policy:regex:v1")

    result = adapter.refresh()

    assert result.active_rule_count == 1
    assert adapter.get_policy().rules[0].category == ""  # type: ignore[union-attr]


def test_regex_adapter_keeps_cache_when_group_members_is_invalid() -> None:
    client = Mock()
    client.get.side_effect = (
        json.dumps({"group_members": {}, "rules": []}),
        json.dumps({"group_members": [], "rules": []}),
    )
    adapter = ValkeyRegexPolicyAdapter(client, "pipeline:policy:regex:v1")
    adapter.refresh()
    previous = adapter.get_policy()

    result = adapter.refresh()

    assert adapter.get_policy() is previous
    assert result.active_rule_count == 0


def test_regex_adapter_restores_local_backup_when_valkey_is_unavailable(
    tmp_path: Path,
) -> None:
    backup_path = tmp_path / "policy-cache" / "regex-v1.json"
    payload = {
        "version": "regex-1",
        "group_members": {"group-1": ["user@example.com"]},
        "rules": [
            {
                "id": "regex-1",
                "name": "secret",
                "rule": _encoded("secret"),
                "action": "block",
                "category": "security",
                "scope": "group",
                "subject_id": "group-1",
            }
        ],
    }
    available_client = Mock()
    available_client.get.return_value = json.dumps(payload)
    first_adapter = ValkeyRegexPolicyAdapter(
        available_client,
        "pipeline:policy:regex:v1",
        backup_path=backup_path,
    )

    first_adapter.refresh()

    assert json.loads(backup_path.read_text(encoding="utf-8")) == payload
    assert backup_path.stat().st_mode & 0o777 == 0o600

    unavailable_client = Mock()
    unavailable_client.get.side_effect = ConnectionError("valkey unavailable")
    restarted_adapter = ValkeyRegexPolicyAdapter(
        unavailable_client,
        "pipeline:policy:regex:v1",
        backup_path=backup_path,
    )

    result = restarted_adapter.refresh()

    assert result.active_rule_count == 1
    assert restarted_adapter.get_policy() is not None
    assert restarted_adapter.get_policy().rules[0].id == "regex-1"  # type: ignore[union-attr]


def test_regex_adapter_uses_empty_snapshot_when_key_and_backup_are_missing(
    tmp_path: Path,
) -> None:
    client = Mock()
    client.get.return_value = None
    adapter = ValkeyRegexPolicyAdapter(
        client,
        "pipeline:policy:regex:v1",
        backup_path=tmp_path / "missing.json",
    )

    result = adapter.refresh()

    assert result.active_rule_count == 0
    assert adapter.get_policy() == RegexPolicySnapshot(rules=())


def test_regex_adapter_uses_empty_snapshot_when_valkey_and_backup_are_unavailable(
    tmp_path: Path,
) -> None:
    client = Mock()
    client.get.side_effect = ConnectionError("valkey unavailable")
    adapter = ValkeyRegexPolicyAdapter(
        client,
        "pipeline:policy:regex:v1",
        backup_path=tmp_path / "missing.json",
    )

    result = adapter.refresh()

    assert result.active_rule_count == 0
    assert adapter.get_policy() == RegexPolicySnapshot(rules=())


def test_multimodal_adapter_loads_rules_and_file_settings() -> None:
    client = Mock()
    client.get.return_value = json.dumps(
        {
            "version": "multimodal-1",
            "group_members": {
                "group-1": [" USER@example.com ", "second@example.com"],
            },
            "rules": [
                {
                    "id": "file-1",
                    "name": "secret file",
                    "targets": "file",
                    "rule": _encoded("secret"),
                    "regex_flag": 0,
                    "action": "masking",
                    "category": "security",
                    "scope": "group",
                    "subject_id": "group-1",
                }
            ],
            "settings": [
                {"skey": "fileControlAllowedExtensions", "svalue": "PDF,docx"},
                {"skey": "fileControlMaxSize", "svalue": "2048"},
            ],
        }
    )
    adapter = ValkeyMultimodalPolicyAdapter(
        client,
        "pipeline:policy:multimodal:v1",
    )

    result = adapter.refresh()
    snapshot = adapter.get_policy()

    assert result.active_rule_count == 1
    assert snapshot.rules[0].action == "block"
    assert snapshot.rules[0].subject_id == "group-1"
    assert snapshot.group_members == (
        ("group-1", ("user@example.com", "second@example.com")),
    )
    assert snapshot.allowed_extensions == ("pdf", "docx")
    assert snapshot.max_size_bytes == 2048


def test_slm_adapter_loads_group_policy() -> None:
    client = Mock()
    client.get.return_value = json.dumps(
        {
            "version": "slm-1",
            "group_members": {
                "group-1": [" USER@example.com "],
            },
            "rules": [
                {
                    "id": "slm-1",
                    "name": "prompt injection",
                    "detection_kind": "guardrail",
                    "detection_type": "PROMPT_INJECTION",
                    "action": "block",
                    "category": "safety",
                    "scope": "group",
                    "subject_id": "group-1",
                }
            ],
        }
    )
    adapter = ValkeySlmPolicyAdapter(
        client,
        "pipeline:policy:slm:v1",
    )

    result = adapter.refresh()
    snapshot = adapter.get_policy()

    assert result.active_rule_count == 1
    assert snapshot.rules[0] == SlmPolicyRule(
        id="slm-1",
        name="prompt injection",
        action="block",
        detection_kind="guardrail",
        detection_type="PROMPT_INJECTION",
        scope="group",
        category="safety",
        subject_id="group-1",
    )
    assert snapshot.group_members == (("group-1", ("user@example.com",)),)


def test_slm_adapter_loads_policy_without_category() -> None:
    client = Mock()
    client.get.return_value = json.dumps(
        {
            "group_members": {},
            "rules": [
                {
                    "id": "slm-uncategorized",
                    "name": "uncategorized slm",
                    "detection_kind": "abuse",
                    "detection_type": None,
                    "action": "block",
                    "category": None,
                    "scope": "default",
                    "subject_id": "",
                }
            ],
        }
    )
    adapter = ValkeySlmPolicyAdapter(client, "pipeline:policy:slm:v1")

    result = adapter.refresh()

    assert result.active_rule_count == 1
    assert adapter.get_policy().rules[0].category == ""


def test_multimodal_adapter_restores_local_backup_when_valkey_is_unavailable(
    tmp_path: Path,
) -> None:
    backup_path = tmp_path / "multimodal-v1.json"
    payload = {
        "version": "multimodal-1",
        "group_members": {"group-1": ["user@example.com"]},
        "rules": [
            {
                "id": "file-1",
                "name": "secret file",
                "targets": "file",
                "rule": _encoded("secret"),
                "regex_flag": 0,
                "action": "block",
                "category": "security",
                "scope": "group",
                "subject_id": "group-1",
            }
        ],
        "settings": [
            {"skey": "fileControlAllowedExtensions", "svalue": "pdf"},
            {"skey": "fileControlMaxSize", "svalue": "2048"},
        ],
    }
    available = Mock()
    available.get.return_value = json.dumps(payload)
    ValkeyMultimodalPolicyAdapter(
        available,
        "pipeline:policy:multimodal:v1",
        backup_path=backup_path,
    ).refresh()

    unavailable = Mock()
    unavailable.get.side_effect = ConnectionError("valkey unavailable")
    restarted = ValkeyMultimodalPolicyAdapter(
        unavailable,
        "pipeline:policy:multimodal:v1",
        backup_path=backup_path,
    )

    result = restarted.refresh()

    assert json.loads(backup_path.read_text(encoding="utf-8")) == payload
    assert result.active_rule_count == 1
    assert restarted.get_policy().group_members == (("group-1", ("user@example.com",)),)
    assert restarted.get_policy().allowed_extensions == ("pdf",)
    assert restarted.get_policy().max_size_bytes == 2048


def test_slm_adapter_restores_local_backup_when_valkey_is_unavailable(
    tmp_path: Path,
) -> None:
    backup_path = tmp_path / "slm-v1.json"
    payload = {
        "version": "slm-1",
        "group_members": {
            "group-1": ["user@example.com"],
        },
        "rules": [
            {
                "id": "slm-1",
                "name": "prompt injection",
                "detection_kind": "guardrail",
                "detection_type": "PROMPT_INJECTION",
                "action": "block",
                "category": "safety",
                "scope": "group",
                "subject_id": "group-1",
            }
        ],
    }
    available = Mock()
    available.get.return_value = json.dumps(payload)

    ValkeySlmPolicyAdapter(
        available,
        "pipeline:policy:slm:v1",
        backup_path=backup_path,
    ).refresh()

    unavailable = Mock()
    unavailable.get.side_effect = ConnectionError(
        "valkey unavailable",
    )
    restarted = ValkeySlmPolicyAdapter(
        unavailable,
        "pipeline:policy:slm:v1",
        backup_path=backup_path,
    )

    result = restarted.refresh()
    snapshot = restarted.get_policy()

    assert (
        json.loads(
            backup_path.read_text(encoding="utf-8"),
        )
        == payload
    )
    assert result.active_rule_count == 1
    assert snapshot.rules[0].detection_kind == "guardrail"
    assert snapshot.rules[0].detection_type == ("PROMPT_INJECTION")
    assert snapshot.group_members == (("group-1", ("user@example.com",)),)


def test_multimodal_and_slm_use_the_same_scope_precedence() -> None:
    multimodal_rules = tuple(
        MultimodalPolicyRule(
            id=scope,
            name=scope,
            expression="secret",
            action="block",
            scope=scope,
            subject_id=subject_id,
        )
        for scope, subject_id in (
            ("default", ""),
            ("group", "group-1"),
            ("service", "1"),
            ("user", "user@example.com"),
        )
    )
    request = MultimodalInspectionRequest(
        targets=(),
        policy=MultimodalPolicySnapshot(
            rules=multimodal_rules,
            group_members=(("group-1", ("user@example.com",)),),
        ),
        options=Mock(),
        email="USER@example.com",
        ai_service_id=1,
    )

    applicable = LocalMultimodalInspectionAdapter()._current_rules(
        request,
    )

    assert tuple(item.rule.id for item in applicable) == (
        "user",
        "service",
        "group",
        "default",
    )

    slm_snapshot = SlmPolicySnapshot(
        rules=tuple(
            SlmPolicyRule(
                id=rule.id,
                name=rule.name,
                action=rule.action,
                detection_kind="pii",
                detection_type="EMAIL",
                scope=rule.scope,
                subject_id=rule.subject_id,
            )
            for rule in multimodal_rules
        ),
        group_members=(("group-1", ("user@example.com",)),),
    )

    service = SlmInspectionService(
        Mock(
            inspect=AsyncMock(
                return_value=SlmInspectionResult(
                    unsafe=True,
                    use_default_policy_metadata=True,
                    evidence=[
                        {
                            "kind": "pii",
                            "type": "EMAIL",
                            "value": "user@example.com",
                        }
                    ],
                )
            )
        ),
        Mock(get_policy=Mock(return_value=slm_snapshot)),
        SlmInspectionOptions(),
    )
    result = asyncio.run(
        service.inspect("user@example.com", email="USER@example.com", ai_service_id=1)
    )

    assert result.policy is not None
    assert result.policy.id == "user"


def test_bootstrap_wires_all_policy_sources_to_one_valkey_client(
    tmp_path: Path,
) -> None:
    client = Mock()
    client.get.side_effect = lambda key: json.dumps(
        {
            "version": "empty-1",
            "rules": [],
            "group_members": {},
            **(
                {"services": {"9": "GitHub Copilot"}}
                if key.endswith("catalog:v1")
                else {}
            ),
            **({"settings": []} if key.endswith("multimodal:v1") else {}),
        }
    )
    connecter = Mock()
    connecter.connect.return_value = client

    from unittest.mock import patch

    with patch(
        "src.bootstrap.pipeline_bootstrap.ValkeyConnecter",
        return_value=connecter,
    ):
        runtime = PipelineBootstrap(
            PipelineSettings(runtime_dir=str(tmp_path)),
            http_client_registry=None,
            logger=Mock(),
        )._assemble_policy_runtime()

    assert isinstance(runtime.regex_policy, ValkeyRegexPolicyAdapter)
    assert isinstance(runtime.multimodal_policy, ValkeyMultimodalPolicyAdapter)
    assert isinstance(runtime.slm_policy, ValkeySlmPolicyAdapter)
    assert isinstance(runtime.secuxper_policy, ValkeySecuxperPolicyAdapter)
    assert runtime.valkey_connecter is connecter
    assert runtime.ai_service_catalog.name_for(9) == "GitHub Copilot"
    assert {call.args[0] for call in client.get.call_args_list} == {
        "pipeline:policy:regex:v1",
        "pipeline:policy:multimodal:v1",
        "pipeline:policy:slm:v1",
        "pipeline:policy:secuxper:v1",
        "pipeline:ai-service:catalog:v1",
    }


def test_bootstrap_initializes_missing_policy_keys_as_empty_without_error(
    tmp_path: Path,
) -> None:
    client = Mock()
    client.get.return_value = None
    connecter = Mock()
    connecter.connect.return_value = client
    logger = Mock()

    from unittest.mock import patch

    with patch(
        "src.bootstrap.pipeline_bootstrap.ValkeyConnecter",
        return_value=connecter,
    ):
        runtime = PipelineBootstrap(
            PipelineSettings(runtime_dir=str(tmp_path)),
            http_client_registry=None,
            logger=logger,
        )._assemble_policy_runtime()

    assert runtime.regex_policy.get_policy().rules == ()  # type: ignore[union-attr]
    assert runtime.multimodal_policy.get_policy().rules == ()
    assert runtime.slm_policy.get_policy().rules == ()
    logger.exception.assert_not_called()
