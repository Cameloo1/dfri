from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from dfri.lake.schemas import SchemaViolationError, schema_for, table_from_rows


def raw_row() -> dict[str, object]:
    return {
        "source": "FEDERAL_RESERVE_BOARD",
        "series_id": "DTCTLR.M",
        "obs_period": date(2024, 1, 1),
        "value": 1336.7,
        "unit": "Billions of Dollars",
        "release_date": datetime(2024, 3, 7, 20, 0, tzinfo=UTC),
        "vintage_date": date(2024, 3, 7),
        "ingested_at": datetime(2024, 3, 7, 20, 5, tzinfo=UTC),
        "source_url": "https://www.federalreserve.gov/releases/g19/data/FRB_g19_xml.zip",
        "checksum": "a" * 64,
    }


def test_all_spec_tables_are_registered() -> None:
    expected = {
        "series_registry",
        "raw_observations",
        "releases_calendar",
        "predictions",
        "grades",
        "publication_records",
        "assumptions",
        "matrix_a",
        "matrix_b",
        "company_facts",
        "sec_xbrl_facts",
        "sec_filing_evidence",
        "auto_abs_aggregates",
        "card_trust_aggregates",
        "dfri_output",
    }
    assert expected == {name for name in expected if schema_for(name)}


def test_strict_rows_accept_exact_contract() -> None:
    table = table_from_rows("raw_observations", [raw_row()])
    assert table.num_rows == 1
    assert table.schema == schema_for("raw_observations")


@pytest.mark.parametrize("mutation", ["missing", "extra", "null"])
def test_strict_rows_reject_contract_drift(mutation: str) -> None:
    row = raw_row()
    if mutation == "missing":
        row.pop("checksum")
    elif mutation == "extra":
        row["unexpected"] = "bad"
    else:
        row["series_id"] = None
    with pytest.raises(SchemaViolationError):
        table_from_rows("raw_observations", [row])


def test_unknown_table_fails_closed() -> None:
    with pytest.raises(SchemaViolationError, match="Unknown curated table"):
        schema_for("invented")
