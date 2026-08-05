from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from dfri.ingest.bea import (
    BeaClient,
    BeaContractError,
    bea_rows_checksum,
    parse_bea_rows,
    parse_bea_value,
)
from dfri.ingest.http import HttpReceipt
from dfri.ingest.registry import load_context_series


def fixture_bytes() -> bytes:
    return (Path(__file__).parents[1] / "fixtures" / "bea" / "context_sample.json").read_bytes()


class FakeTransport:
    def get(self, url: str, **_kwargs: object) -> HttpReceipt:
        return HttpReceipt(
            content=fixture_bytes(),
            source_url=url,
            checksum="b" * 64,
            retrieved_at=datetime(2026, 8, 4, tzinfo=UTC),
            status_code=200,
        )


def test_parse_real_bea_rows_and_values() -> None:
    rows = parse_bea_rows(fixture_bytes())
    assert rows[0]["SeriesCode"] == "DPCERC"
    assert bea_rows_checksum(rows) == bea_rows_checksum(tuple(reversed(rows)))
    assert parse_bea_value(rows[0]["DataValue"]) == Decimal("22184132")
    assert parse_bea_value("(NA)") is None
    with pytest.raises(BeaContractError, match="invalid"):
        parse_bea_value("not-a-number")


def test_bea_client_verifies_pinned_metadata() -> None:
    definitions = tuple(item for item in load_context_series() if item.source == "bea")
    client = BeaClient(FakeTransport(), "test-key")  # type: ignore[arg-type]
    receipts = client.verify_series(definitions, year="2026")
    assert len(receipts) == 14
    assert {receipt.checksum for receipt in receipts} == {
        bea_rows_checksum(parse_bea_rows(fixture_bytes()))
    }
    assert "b" * 64 not in {receipt.checksum for receipt in receipts}
    assert {receipt.series_id for receipt in receipts}.issuperset(
        {
            "BEA:NIUnderlyingDetail:U20405:DPCERC",
            "BEA:NIUnderlyingDetail:U20405:DMOTRC",
            "BEA:NIUnderlyingDetail:U20405:DIFSRC",
            "BEA:NIPA:T20600:A065RC",
        }
    )
    drifted = replace(definitions[0], expected_title="Wrong")
    with pytest.raises(BeaContractError, match="mismatch"):
        client.verify_series((drifted,), year="2026")


def test_bea_rejects_missing_key_and_malformed_responses() -> None:
    with pytest.raises(ValueError, match="required"):
        BeaClient(FakeTransport(), "")  # type: ignore[arg-type]
    with pytest.raises(BeaContractError, match="valid API"):
        parse_bea_rows(b"not json")
    with pytest.raises(BeaContractError, match="structured error"):
        parse_bea_rows(b'{"BEAAPI":{"Results":{"Error":{"APIErrorCode":"4"}}}}')
