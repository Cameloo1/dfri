from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from dfri.ingest.registry import load_source_contracts
from dfri.ingest.verify import VerificationError, validate_source_contracts, write_receipt


def test_source_contract_gate_and_atomic_receipt(tmp_path: Path) -> None:
    contracts = load_source_contracts()
    validate_source_contracts(contracts)

    output = tmp_path / "nested" / "receipt.json"
    write_receipt(output, {"status": "PASS", "schema_version": 1})
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "status": "PASS",
    }


def test_source_contract_gate_fails_closed() -> None:
    contracts = load_source_contracts()
    with pytest.raises(VerificationError, match="incomplete"):
        validate_source_contracts({"bea": contracts["bea"]})
    broken = dict(contracts)
    broken["bea"] = replace(contracts["bea"], storage=False)
    with pytest.raises(VerificationError, match="not publication-safe"):
        validate_source_contracts(broken)
