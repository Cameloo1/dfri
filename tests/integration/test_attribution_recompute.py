from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from dfri.attribution.pipeline import write_attribution_report


def test_standalone_recompute_matches_four_published_companies(tmp_path: Path) -> None:
    project = Path(__file__).parents[2]
    report = tmp_path / "dfri_companies.json"
    write_attribution_report(report)

    completed = subprocess.run(
        [
            sys.executable,
            str(project / "tools" / "recompute_check.py"),
            "--published",
            str(report),
        ],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    receipt = json.loads(completed.stdout)
    assert receipt["status"] == "PASS"
    assert {item["ticker"] for item in receipt["checks"]} == {"AMZN", "GM", "TJX", "WMT"}
    assert all(item["absolute_difference_pp"] <= 0.5 for item in receipt["checks"])
