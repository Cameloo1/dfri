from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

import dfri.ingest.context_history as context_history
from dfri.ingest.bea import BeaTableData, parse_bea_rows
from dfri.ingest.census import CensusDatasetData, parse_census_rows
from dfri.ingest.context_history import (
    ContextHistoryError,
    ContextHistoryIngestor,
    bea_history_rows,
    census_history_rows,
    validate_context_history,
)
from dfri.ingest.registry import ContextSeriesDefinition, load_context_series
from dfri.lake.store import AppendOnlyParquetStore

SNAPSHOT_AT = "2026-08-04T12:00:00+00:00"


def fixture(path: str) -> bytes:
    return (Path(__file__).parents[1] / "fixtures" / path).read_bytes()


def definitions(source: str, table: str | None = None) -> tuple[ContextSeriesDefinition, ...]:
    selected = tuple(item for item in load_context_series() if item.source == source)
    if table is None:
        return selected
    return tuple(
        item for item in selected if item.expected_source_attributes["table_name"] == table
    )


def bea_data(table: str, *, checksum: str | None = None) -> BeaTableData:
    dataset = "NIPA" if table == "T20600" else "NIUnderlyingDetail"
    return BeaTableData(
        dataset=dataset,
        table_name=table,
        source_url=(
            "https://apps.bea.gov/api/data/?method=GetData&"
            f"DataSetName={dataset}&TableName={table}&Frequency=M&Year=X&ResultFormat=JSON"
        ),
        checksum=checksum or (("a" if table == "U20405" else "b") * 64),
        retrieved_at=SNAPSHOT_AT,
        rows=parse_bea_rows(fixture("bea/context_sample.json")),
    )


def census_data(*, checksum: str = "c" * 64) -> CensusDatasetData:
    return CensusDatasetData(
        dataset="marts",
        source_url=(
            "https://api.census.gov/data/timeseries/eits/marts?"
            "get=cell_value%2Ccategory_code&time=from+2015-01"
        ),
        checksum=checksum,
        retrieved_at=SNAPSHOT_AT,
        rows=parse_census_rows(fixture("census/marts_sample.json")),
    )


def test_context_histories_are_idempotent_retrieval_time_snapshots(tmp_path: Path) -> None:
    store = AppendOnlyParquetStore(tmp_path)
    ingestor = ContextHistoryIngestor(store)

    u_receipt = ingestor.ingest_bea(
        bea_data("U20405"), definitions=definitions("bea", "U20405"), start=date(2026, 1, 1)
    )
    n_receipt = ingestor.ingest_bea(
        bea_data("T20600"), definitions=definitions("bea", "T20600"), start=date(2026, 1, 1)
    )
    c_receipt = ingestor.ingest_census(
        census_data(), definitions=definitions("census"), start=date(2026, 1, 1)
    )
    repeated = ContextHistoryIngestor(store).ingest_census(
        replace(census_data(), retrieved_at="2026-08-05T12:00:00+00:00"),
        definitions=definitions("census"),
        start=date(2026, 1, 1),
    )

    assert (u_receipt.row_count, n_receipt.row_count, c_receipt.row_count) == (13, 1, 6)
    assert repeated.already_present is True
    assert repeated.batch_path == c_receipt.batch_path
    frame = store.read_table("raw_observations")
    assert frame.height == 20
    assert frame.select((frame["release_date"] == frame["ingested_at"]).all()).item()
    assert frame["source_url"].str.contains("key=").any() is False

    report = validate_context_history(store, ("bea", "census"))
    assert report.total_rows == 20
    assert report.snapshot_batches == 3
    assert report.rows_by_source == {"BEA": 14, "CENSUS": 6}
    assert set(report.latest_period_by_series.values()) == {date(2026, 6, 30)}
    assert validate_context_history(store, ("census",)).total_rows == 6


class FakeBeaClient:
    def fetch_table(
        self, *, dataset: str, table_name: str, year: str, frequency: str
    ) -> BeaTableData:
        assert dataset in {"NIPA", "NIUnderlyingDetail"}
        assert year == "X"
        assert frequency == "M"
        return bea_data(table_name)


class FakeCensusClient:
    def __init__(self) -> None:
        self.variables_checked: list[str] = []

    def fetch_variables(self, dataset: str) -> frozenset[str]:
        self.variables_checked.append(dataset)
        return frozenset({"cell_value"})

    def fetch_periods(self, dataset: str, time_range: str) -> CensusDatasetData:
        assert dataset == "marts"
        assert time_range == "from 2026-01"
        return census_data()


def test_fetches_group_registered_tables_and_categories(tmp_path: Path) -> None:
    ingestor = ContextHistoryIngestor(AppendOnlyParquetStore(tmp_path))
    bea_receipts = ingestor.fetch_bea(FakeBeaClient(), start=date(2026, 1, 1))  # type: ignore[arg-type]
    census = FakeCensusClient()
    census_receipts = ingestor.fetch_census(census, start=date(2026, 1, 1))  # type: ignore[arg-type]

    assert len(bea_receipts) == 2
    assert len(census_receipts) == 1
    assert census.variables_checked == ["marts"]


def test_fetches_require_registered_definitions_for_the_selected_source(tmp_path: Path) -> None:
    store = AppendOnlyParquetStore(tmp_path)
    with pytest.raises(ContextHistoryError, match="No BEA"):
        ContextHistoryIngestor(store, definitions=definitions("census")).fetch_bea(
            FakeBeaClient()  # type: ignore[arg-type]
        )
    with pytest.raises(ContextHistoryError, match="No Census"):
        ContextHistoryIngestor(store, definitions=definitions("bea")).fetch_census(
            FakeCensusClient()  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("data", "source", "message"),
    [
        (replace(bea_data("U20405"), source_url="https://example.com"), "bea", "URL"),
        (replace(bea_data("U20405"), checksum="bad"), "bea", "checksum"),
        (replace(bea_data("U20405"), retrieved_at="not-a-date"), "bea", "timestamp"),
        (
            replace(bea_data("U20405"), retrieved_at="2026-08-04T12:00:00"),
            "bea",
            "timezone-aware",
        ),
        (
            replace(
                bea_data("U20405"),
                source_url=f"{bea_data('U20405').source_url}&UserID=secret",
            ),
            "bea",
            "credential",
        ),
        (replace(census_data(), source_url="https://example.com"), "census", "URL"),
    ],
)
def test_context_source_contracts_fail_closed(
    data: BeaTableData | CensusDatasetData, source: str, message: str
) -> None:
    with pytest.raises(ContextHistoryError, match=message):
        if source == "bea":
            assert isinstance(data, BeaTableData)
            bea_history_rows(data, definitions=definitions("bea", "U20405"), start=date(2026, 1, 1))
        else:
            assert isinstance(data, CensusDatasetData)
            census_history_rows(data, definitions=definitions("census"), start=date(2026, 1, 1))


def test_context_rows_reject_missing_values_and_bad_time_slots() -> None:
    bea = bea_data("U20405")
    target = definitions("bea", "U20405")[0]
    attrs = target.expected_source_attributes
    changed_bea_rows = tuple(
        {**row, "DataValue": "(NA)"}
        if row.get("SeriesCode") == attrs["series_code"]
        and row.get("LineNumber") == attrs["line_number"]
        else row
        for row in bea.rows
    )
    with pytest.raises(ContextHistoryError, match="missing value"):
        bea_history_rows(
            replace(bea, rows=changed_bea_rows),
            definitions=definitions("bea", "U20405"),
            start=date(2026, 1, 1),
        )

    census = census_data()
    changed_census_rows = tuple(
        {**row, "time_slot_date": "bad"} if index == 0 else row
        for index, row in enumerate(census.rows)
    )
    with pytest.raises(ContextHistoryError, match="time_slot_date"):
        census_history_rows(
            replace(census, rows=changed_census_rows),
            definitions=definitions("census"),
            start=date(2026, 1, 1),
        )

    disagreeing_rows = tuple(
        {**row, "time_slot_date": "2026-05-01 00:00:00.0"} if index == 0 else row
        for index, row in enumerate(census.rows)
    )
    with pytest.raises(ContextHistoryError, match="disagree"):
        census_history_rows(
            replace(census, rows=disagreeing_rows),
            definitions=definitions("census"),
            start=date(2026, 1, 1),
        )


def test_context_rows_reject_malformed_periods_and_snapshot_drift() -> None:
    bea = bea_data("U20405")
    changed_bea_rows = tuple(
        {**row, "TimePeriod": "2026M13"} if index == 0 else row
        for index, row in enumerate(bea.rows)
    )
    with pytest.raises(ContextHistoryError, match="BEA monthly period"):
        bea_history_rows(
            replace(bea, rows=changed_bea_rows),
            definitions=definitions("bea", "U20405"),
            start=date(2026, 1, 1),
        )

    census = census_data()
    changed_census_rows = tuple(
        {**row, "time": "bad"} if index == 0 else row for index, row in enumerate(census.rows)
    )
    with pytest.raises(ContextHistoryError, match="Census monthly period"):
        census_history_rows(
            replace(census, rows=changed_census_rows),
            definitions=definitions("census"),
            start=date(2026, 1, 1),
        )

    mismatched_periods = tuple(
        {
            **row,
            "time": "2026-05",
            "time_slot_date": "2026-05-01 00:00:00.0",
        }
        if index == 0
        else row
        for index, row in enumerate(census.rows)
    )
    with pytest.raises(ContextHistoryError, match="different period coverage"):
        census_history_rows(
            replace(census, rows=mismatched_periods),
            definitions=definitions("census"),
            start=date(2026, 1, 1),
        )

    with pytest.raises(ContextHistoryError, match="stale"):
        census_history_rows(
            replace(census, retrieved_at="2027-08-04T12:00:00+00:00"),
            definitions=definitions("census"),
            start=date(2026, 1, 1),
        )


def test_context_validator_and_existing_conflicts_fail_closed(tmp_path: Path) -> None:
    store = AppendOnlyParquetStore(tmp_path)
    with pytest.raises(ContextHistoryError, match="at least one"):
        validate_context_history(store, ())
    with pytest.raises(ContextHistoryError, match="source is invalid"):
        validate_context_history(store, ("bogus",))  # type: ignore[arg-type]
    with pytest.raises(ContextHistoryError, match="empty"):
        validate_context_history(store, ("bea",))

    data = census_data()
    rows = census_history_rows(data, definitions=definitions("census"), start=date(2026, 1, 1))
    rows[0]["value"] = 999.0
    store.append("raw_observations", rows)
    with pytest.raises(ContextHistoryError, match="does not match"):
        ContextHistoryIngestor(store).ingest_census(
            data, definitions=definitions("census"), start=date(2026, 1, 1)
        )


def test_cli_runs_both_sources_without_exposing_keys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    class FakeTransport:
        def __enter__(self) -> FakeTransport:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setenv("BEA_API_KEY", "secret-bea")
    monkeypatch.setenv("CENSUS_API_KEY", "secret-census")
    monkeypatch.setattr(context_history, "HttpTransport", lambda **_kwargs: FakeTransport())
    monkeypatch.setattr(context_history, "BeaClient", lambda *_args: FakeBeaClient())
    monkeypatch.setattr(context_history, "CensusClient", lambda *_args: FakeCensusClient())

    result = context_history.main(["--start", "2026-01-01", "--lake-root", str(tmp_path / "lake")])

    output_text = capsys.readouterr().out
    output = json.loads(output_text)
    assert result == 0
    assert len(output["snapshots"]) == 3
    assert "secret-bea" not in output_text
    assert "secret-census" not in output_text
    with pytest.raises(SystemExit):
        context_history.build_parser().parse_args(["--start", "bad"])
