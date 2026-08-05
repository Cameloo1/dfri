from __future__ import annotations

import json
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
    matrix_b = json.loads((attribution / "matrix_b_v1_1.json").read_text())

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
