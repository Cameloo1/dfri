from __future__ import annotations

import json
from pathlib import Path

import pytest

from dfri.attribution.engine import run_attribution
from dfri.attribution.pipeline import main, write_attribution_report
from dfri.attribution.registry import load_attribution_bundle


def test_attribution_report_is_byte_stable_and_machine_readable(tmp_path: Path) -> None:
    output = tmp_path / "reports" / "dfri_companies.json"
    write_attribution_report(output)
    first = output.read_bytes()
    write_attribution_report(output)
    second = output.read_bytes()

    assert first == second
    payload = json.loads(first)
    assert payload["methodology_version"] == "1.2.1"
    assert payload["evidence_lift_headline"]
    assert payload["source_degradations"] == []
    assert payload["aggregate"]["weighting"] == "revenue-weighted"
    assert len(payload["companies"]) == 50


def test_attribution_cli_writes_requested_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "custom.json"

    assert main(["--output", str(output), "--draws", "10000", "--seed", "7"]) == 0
    assert output.exists()
    assert '"status": "PASS"' in capsys.readouterr().out


def test_tier1_restatement_does_not_perturb_unaffected_company_draws() -> None:
    previous = run_attribution(load_attribution_bundle("1.2.0"))
    current = run_attribution(load_attribution_bundle("1.2.1"))
    previous_by_ticker = {item.ticker: item for item in previous.companies}
    current_by_ticker = {item.ticker: item for item in current.companies}

    for ticker in sorted(set(previous_by_ticker) - {"TJX"}):
        assert previous_by_ticker[ticker] == current_by_ticker[ticker]
    assert previous_by_ticker["TJX"] != current_by_ticker["TJX"]
    assert previous_by_ticker["TJX"].evidence_lift_status == "baseline-only"
    assert current_by_ticker["TJX"].evidence_lift_status == "evidence-supported"
