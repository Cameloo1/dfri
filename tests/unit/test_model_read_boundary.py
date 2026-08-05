from __future__ import annotations

import ast
from pathlib import Path


def test_models_cannot_import_direct_lake_readers() -> None:
    repo = Path(__file__).parents[2]
    forbidden = {"dfri.lake.store", "dfri.lake.readers"}
    violations: list[str] = []
    for package in (repo / "src" / "dfri" / "nowcast", repo / "src" / "dfri" / "attribution"):
        for path in package.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = {alias.name for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = {node.module}
                else:
                    continue
                if names & forbidden:
                    violations.append(f"{path}:{node.lineno}")
    assert violations == [], f"Direct model lake imports bypass VintageGuard: {violations}"
