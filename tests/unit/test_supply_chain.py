from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from dfri.ops.supply_chain import SupplyChainError, main, verify_supply_chain


def test_repository_supply_chain_is_exact_lock_backed_and_action_pinned() -> None:
    root = Path(__file__).parents[2]

    receipt = verify_supply_chain(root)

    assert receipt["status"] == "PASS"
    assert receipt["python_direct_pins"] >= 20
    assert receipt["node_direct_pins"] == 2
    assert receipt["github_action_pins"] >= 10
    assert receipt["locks"] == ["uv.lock", "package-lock.json"]


def test_supply_chain_rejects_floating_python_node_and_action_pins(tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    for filename in ("pyproject.toml", "uv.lock", "package.json", "package-lock.json"):
        shutil.copy2(root / filename, tmp_path / filename)
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "steps:\n  - uses: actions/checkout@" + "a" * 40 + " # pinned\n",
        encoding="utf-8",
    )

    pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        pyproject.replace("duckdb==1.5.5", "duckdb>=1.5"), encoding="utf-8"
    )
    with pytest.raises(SupplyChainError, match="exact =="):
        verify_supply_chain(tmp_path)

    (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    package = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    package["devDependencies"]["playwright-core"] = "^1.62.1"
    (tmp_path / "package.json").write_text(json.dumps(package), encoding="utf-8")
    with pytest.raises(SupplyChainError, match="exact versions"):
        verify_supply_chain(tmp_path)

    shutil.copy2(root / "package.json", tmp_path / "package.json")
    (workflows / "ci.yml").write_text("steps:\n  - uses: actions/checkout@v7\n", encoding="utf-8")
    with pytest.raises(SupplyChainError, match="full commit SHA"):
        verify_supply_chain(tmp_path)


def test_supply_chain_cli_reports_receipt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = Path(__file__).parents[2]
    monkeypatch.setattr(sys, "argv", ["supply-chain", "--root", str(root)])

    assert main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PASS"


def test_supply_chain_rejects_missing_contract(tmp_path: Path) -> None:
    with pytest.raises(SupplyChainError, match="Missing required"):
        verify_supply_chain(tmp_path)
