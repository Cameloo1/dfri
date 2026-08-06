from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


def test_generated_m5_registries_are_current_and_partition_consumer_members() -> None:
    root = Path(__file__).parents[2]
    completed = subprocess.run(
        [sys.executable, str(root / "tools" / "build_m5_registries.py"), "--check"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    attribution = root / "src" / "dfri" / "attribution"
    coverage = json.loads((attribution / "coverage_registry_v1_1.json").read_text())
    history = json.loads((attribution / "coverage_history_v1.json").read_text())
    matrix_b = json.loads((attribution / "matrix_b_v1_1_1.json").read_text())

    assert len(coverage["expansion"]) == 40
    assert len(coverage["excluded"]) == 31
    assert all(item["reason"] for item in coverage["excluded"])
    assert len(history["snapshots"][0]["included_tickers"]) == 10
    assert len(history["snapshots"][1]["included_tickers"]) == 50
    for category in (
        "general_retail",
        "fungible_consumer",
        "fungible_consumer_nonrevolving",
        "auto_market",
    ):
        rows = [item for item in matrix_b["items"] if item["spend_category"] == category]
        assert abs(sum(item["weight_mid"] for item in rows) - 1) < 1e-12
    auto_tickers = {
        item["ticker"] for item in matrix_b["items"] if item["spend_category"] == "auto_market"
    }
    assert auto_tickers == {"CVNA", "F", "GM", "TSLA"}
    assert any(
        item["ticker"] == "CVNA" and item["spend_category"] == "carvana_auto_finance"
        for item in matrix_b["items"]
    )


def test_registry_drift_check_is_independent_of_checkout_line_endings(tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    attribution = root / "src" / "dfri" / "attribution"
    copied = tmp_path / "attribution"
    shutil.copytree(attribution, copied)
    output = copied / "assumption_registry_v1_1.json"
    content = output.read_text(encoding="utf-8")
    with output.open("w", encoding="utf-8", newline="") as handle:
        handle.write(content.replace("\n", "\r\n"))

    completed = subprocess.run(
        [
            sys.executable,
            str(root / "tools" / "build_m5_registries.py"),
            "--check",
            "--root",
            str(copied),
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
