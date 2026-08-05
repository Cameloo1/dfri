from __future__ import annotations

from datetime import date
from pathlib import Path

from dfri.ingest.calendar import (
    WINDOW_END,
    WINDOW_START,
    release_calendar_evidence,
    release_calendar_rows,
)
from dfri.lake.store import AppendOnlyParquetStore, file_sha256


def test_calendar_spans_12_months_and_preserves_unknowns() -> None:
    rows = release_calendar_rows()
    g19 = [row for row in rows if str(row["release_name"]).startswith("G.19")]
    assert len(g19) == 12
    assert WINDOW_START == date(2026, 8, 1)
    assert WINDOW_END == date(2027, 7, 31)
    assert all(row["expected_at"] is None for row in g19)
    assert all(str(row["status"]).startswith("BLOCKED") for row in g19)

    census = [row for row in rows if str(row["release_name"]).startswith("Census")]
    assert len(census) == 12
    assert sum(row["status"] == "EXPECTED_OFFICIAL" for row in census) == 5
    assert sum(str(row["status"]).startswith("BLOCKED") for row in census) == 7


def test_h8_rule_handles_time_zones_and_friday_holidays() -> None:
    rows = release_calendar_rows()
    by_name = {str(row["release_name"]): row for row in rows}
    assert by_name["H.8 week 2026-08-07"]["expected_at"].isoformat() == (
        "2026-08-07T20:15:00+00:00"
    )
    assert by_name["H.8 week 2026-11-06"]["expected_at"].isoformat() == (
        "2026-11-06T21:15:00+00:00"
    )
    assert by_name["H.8 week 2026-12-25"]["expected_at"].isoformat() == (
        "2026-12-24T21:15:00+00:00"
    )
    assert by_name["H.8 week 2027-06-18"]["expected_at"].isoformat() == (
        "2027-06-17T20:15:00+00:00"
    )


def test_calendar_is_valid_schema_and_deterministic_parquet(tmp_path: Path) -> None:
    rows = release_calendar_rows()
    first = AppendOnlyParquetStore(tmp_path / "first").append("releases_calendar", rows)
    second = AppendOnlyParquetStore(tmp_path / "second").append("releases_calendar", rows)
    assert first.row_count == second.row_count == 92
    assert file_sha256(first.path) == file_sha256(second.path)
    evidence = release_calendar_evidence()
    assert evidence["window_start"] == "2026-08-01"
