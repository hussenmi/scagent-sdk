from __future__ import annotations

import ast
from pathlib import Path


def _legacy_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(
                alias.name
                for alias in node.names
                if alias.name == "scagent" or alias.name.startswith("scagent.")
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "scagent" or module.startswith("scagent."):
                found.append(module)
    return found


def test_runtime_never_imports_legacy_scagent_package() -> None:
    source_root = Path(__file__).parents[2] / "src"
    violations = {
        str(path.relative_to(source_root)): imports
        for path in source_root.rglob("*.py")
        if (imports := _legacy_imports(path))
    }

    assert violations == {}
