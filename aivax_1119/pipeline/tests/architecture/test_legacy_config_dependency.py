from __future__ import annotations

from pathlib import Path


PIPELINE_ROOT = Path(__file__).resolve().parents[2]


def test_runtime_bootstrap_does_not_load_environment_json_files() -> None:
    sources = [PIPELINE_ROOT / "src" / "bootstrap" / "pipeline_bootstrap.py"]
    forbidden = ("config.json", "db_custom_wins.json", "JsonFileToDictionary")

    violations = [
        f"{source.relative_to(PIPELINE_ROOT)}: {token}"
        for source in sources
        for token in forbidden
        if token in source.read_text(encoding="utf-8")
    ]

    assert violations == []


def test_runtime_environment_and_install_scripts_do_not_reference_execution_json() -> (
    None
):
    sources = [
        PIPELINE_ROOT / ".env",
        PIPELINE_ROOT / ".env.development",
        PIPELINE_ROOT / "deploy" / "install.sh",
    ]
    forbidden = ("AIVAX_PIPELINE_CONFIG", "local_resource/config.json")

    violations = [
        f"{source.relative_to(PIPELINE_ROOT)}: {token}"
        for source in sources
        for token in forbidden
        if token in source.read_text(encoding="utf-8")
    ]

    assert violations == []
