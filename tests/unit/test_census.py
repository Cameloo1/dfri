from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from dfri.ingest.census import (
    CensusClient,
    CensusContractError,
    parse_census_rows,
    parse_census_value,
)
from dfri.ingest.http import HttpReceipt
from dfri.ingest.registry import load_context_series


def fixture_bytes() -> bytes:
    return (Path(__file__).parents[1] / "fixtures" / "census" / "marts_sample.json").read_bytes()


class FakeTransport:
    def get(self, url: str, **_kwargs: object) -> HttpReceipt:
        if url.endswith("variables.json"):
            content = json.dumps(
                {
                    "variables": {
                        name: {}
                        for name in (
                            "cell_value",
                            "data_type_code",
                            "category_code",
                            "seasonally_adj",
                            "time",
                        )
                    }
                }
            ).encode()
        else:
            content = fixture_bytes()
        return HttpReceipt(
            content=content,
            source_url=url,
            checksum="c" * 64,
            retrieved_at=datetime(2026, 8, 4, tzinfo=UTC),
            status_code=200,
        )


def test_parse_real_census_rows_and_values() -> None:
    rows = parse_census_rows(fixture_bytes())
    assert rows[0]["category_code"] == "44X72"
    assert parse_census_value(rows[0]["cell_value"]) == Decimal("768553")
    assert parse_census_value("NA") is None
    with pytest.raises(CensusContractError, match="invalid"):
        parse_census_value("bad")


def test_census_client_verifies_dataset_and_series() -> None:
    definition = next(item for item in load_context_series() if item.source == "census")
    client = CensusClient(FakeTransport(), "test-key")  # type: ignore[arg-type]
    result = client.verify_series((definition,), month="2026-06")
    assert result[0].series_id == "CENSUS:MARTS:44X72:SM:SA"
    assert "cell_value" in client.fetch_variables("marts")


def test_census_rejects_bad_contracts() -> None:
    with pytest.raises(ValueError, match="required"):
        CensusClient(FakeTransport(), "")  # type: ignore[arg-type]
    with pytest.raises(CensusContractError, match="not JSON"):
        parse_census_rows(b"bad")
    with pytest.raises(CensusContractError, match="no tabular"):
        parse_census_rows(b"[]")
    with pytest.raises(CensusContractError, match="row shape"):
        parse_census_rows(b'[["a","b"],["one"]]')
