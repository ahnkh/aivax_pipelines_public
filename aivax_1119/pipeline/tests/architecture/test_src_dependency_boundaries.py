"""src 계층으로 레거시 의존성이 재유입되지 않도록 한다."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"


def test_src_does_not_import_legacy_filter_modules() -> None:
    violations: list[str] = []

    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue

            if any(
                module == "block_filter_modules"
                or module.startswith("block_filter_modules.")
                for module in modules
            ):
                violations.append(str(path.relative_to(ROOT)))

    assert violations == []


def test_optional_extensions_are_only_imported_by_bootstrap_or_extensions() -> None:
    violations = []
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(SRC)
        if relative.parts[0] in {"bootstrap", "extensions"}:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            if any(module.startswith("src.extensions") for module in modules):
                violations.append(str(relative))
    assert violations == []
