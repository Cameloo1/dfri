from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path

import pytest
from openpyxl import load_workbook

import dfri.ingest.nyfed as nyfed
from dfri.ingest.http import HttpReceipt
from dfri.ingest.nyfed import (
    NYFED_HHDC_PAGE,
    NyFedClient,
    NyFedContractError,
    NyFedHistoryIngestor,
    NyFedWorkbookData,
    discover_workbook_url,
    nyfed_history_rows,
    report_period_from_url,
    validate_nyfed_history,
)
from dfri.ingest.registry import load_nyfed_series
from dfri.lake.store import AppendOnlyParquetStore

WORKBOOK_URL = (
    "https://www.newyorkfed.org/medialibrary/interactives/householdcredit/"
    "data/xls/HHD_C_Report_2026Q1"
)
SNAPSHOT_AT = "2026-08-04T08:48:52+00:00"
FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "nyfed"


def fixture_bytes() -> bytes:
    return (FIXTURE_DIR / "hhdc_2026q1_excerpt.xlsx").read_bytes()


def workbook_data(**changes: object) -> NyFedWorkbookData:
    content = fixture_bytes()
    values: dict[str, object] = {
        "report_period": date(2026, 3, 31),
        "source_url": WORKBOOK_URL,
        "checksum": hashlib.sha256(content).hexdigest(),
        "retrieved_at": SNAPSHOT_AT,
        "content": content,
    }
    values.update(changes)
    return NyFedWorkbookData(**values)  # type: ignore[arg-type]


def changed_workbook(*changes: tuple[str, str, object]) -> bytes:
    workbook = load_workbook(BytesIO(fixture_bytes()))
    for sheet_name, cell, value in changes:
        workbook[sheet_name][cell] = value
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


class FakeTransport:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url: str, **_kwargs: object) -> HttpReceipt:
        self.urls.append(url)
        content = (
            b'<a href="/medialibrary/interactives/householdcredit/data/xls/'
            b'HHD_C_Report_2026Q1">Data</a>'
            if url == NYFED_HHDC_PAGE
            else fixture_bytes()
        )
        return HttpReceipt(
            content=content,
            source_url=url,
            checksum=hashlib.sha256(content).hexdigest(),
            retrieved_at=datetime(2026, 8, 4, 8, 48, 52, tzinfo=UTC),
            status_code=200,
        )


def test_discovery_and_client_pin_the_current_official_workbook() -> None:
    html = (
        b'<a href="/medialibrary/interactives/householdcredit/data/xls/'
        b'HHD_C_Report_2026Q1">Data</a>'
        b"<!-- /medialibrary/interactives/householdcredit/data/xls/HHD_C_Report_2026Q1 -->"
    )
    assert discover_workbook_url(html) == WORKBOOK_URL
    assert report_period_from_url(WORKBOOK_URL) == date(2026, 3, 31)

    transport = FakeTransport()
    data = NyFedClient(transport).fetch()  # type: ignore[arg-type]
    assert data.report_period == date(2026, 3, 31)
    assert data.source_url == WORKBOOK_URL
    assert data.content == fixture_bytes()
    assert transport.urls == [NYFED_HHDC_PAGE, WORKBOOK_URL]


def test_fixture_provenance_pins_source_and_derivative_checksums() -> None:
    provenance = json.loads(
        (FIXTURE_DIR / "hhdc_2026q1_excerpt.provenance.json").read_text(encoding="utf-8")
    )

    assert provenance["source_url"] == WORKBOOK_URL
    assert provenance["source_sha256"] == (
        "ff913fcabc4e50a154ab45d27663a97069acfe10625bd5d86f6d21f5cbcfade9"
    )
    assert provenance["fixture_sha256"] == hashlib.sha256(fixture_bytes()).hexdigest()
    assert provenance["attribution"] == "New York Fed Consumer Credit Panel / Equifax"
    assert provenance["terms_url"] == "https://www.newyorkfed.org/privacy/termsofuse.html"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"no workbook", "found 0"),
        (
            b"/medialibrary/interactives/householdcredit/data/xls/HHD_C_Report_2026Q1 "
            b"/medialibrary/interactives/householdcredit/data/xls/HHD_C_Report_2026Q2",
            "found 2",
        ),
        (b"\xff", "UTF-8"),
    ],
)
def test_discovery_fails_closed(content: bytes, message: str) -> None:
    with pytest.raises(NyFedContractError, match=message):
        discover_workbook_url(content)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/medialibrary/interactives/householdcredit/data/xls/HHD_C_Report_2026Q1",
        "https://www.newyorkfed.org/not-a-workbook",
    ],
)
def test_report_period_rejects_untrusted_urls(url: str) -> None:
    with pytest.raises(NyFedContractError, match="URL mismatch"):
        report_period_from_url(url)


def test_history_ingest_is_complete_conservative_and_idempotent(tmp_path: Path) -> None:
    store = AppendOnlyParquetStore(tmp_path)
    data = workbook_data()
    first = NyFedHistoryIngestor(store).ingest(data)
    repeated = NyFedHistoryIngestor(store).ingest(
        replace(data, retrieved_at="2026-08-05T08:48:52+00:00")
    )

    assert first.row_count == 945
    assert first.series_count == 21
    assert repeated.already_present is True
    assert repeated.batch_path == first.batch_path
    frame = store.read_table("raw_observations")
    assert frame.height == 945
    assert frame.select((frame["release_date"] == frame["ingested_at"]).all()).item()

    report = validate_nyfed_history(store)
    assert report.total_rows == 945
    assert report.snapshot_batches == 1
    assert set(report.rows_by_series.values()) == {45}
    latest = report.latest_period_by_series
    assert latest["NYFED:HHDC:BALANCE:AUTO"] == date(2026, 3, 31)
    assert latest["NYFED:HHDC:ORIGINATION:AUTO:TOTAL"] == date(2026, 3, 31)
    assert latest["NYFED:HHDC:DELINQUENCY:90PLUS:AUTO"] == date(2026, 3, 31)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (workbook_data(checksum="bad"), "checksum"),
        (workbook_data(retrieved_at="bad"), "timestamp"),
        (workbook_data(retrieved_at="2026-08-04T08:48:52"), "timezone-aware"),
        (workbook_data(retrieved_at="2027-08-04T08:48:52+00:00"), "stale"),
        (workbook_data(report_period=date(2026, 6, 30)), "disagree"),
        (workbook_data(content=b"not xlsx"), "valid XLSX"),
    ],
)
def test_workbook_source_contracts_fail_closed(data: NyFedWorkbookData, message: str) -> None:
    with pytest.raises(NyFedContractError, match=message):
        nyfed_history_rows(data, definitions=load_nyfed_series(), start=date(2015, 1, 1))


def test_registry_contracts_fail_closed() -> None:
    data = workbook_data()
    definitions = load_nyfed_series()
    with pytest.raises(NyFedContractError, match="empty"):
        nyfed_history_rows(data, definitions=(), start=date(2015, 1, 1))

    attrs = dict(definitions[0].expected_source_attributes)
    attrs.pop("header")
    with pytest.raises(NyFedContractError, match="incomplete"):
        nyfed_history_rows(
            data,
            definitions=(replace(definitions[0], expected_source_attributes=attrs),),
            start=date(2015, 1, 1),
        )

    with pytest.raises(NyFedContractError, match="frequency"):
        nyfed_history_rows(
            data,
            definitions=(replace(definitions[0], frequency="Monthly"),),
            start=date(2015, 1, 1),
        )

    attrs = {**definitions[0].expected_source_attributes, "source_page": "https://example.com"}
    with pytest.raises(NyFedContractError, match="endpoint"):
        nyfed_history_rows(
            data,
            definitions=(replace(definitions[0], expected_source_attributes=attrs),),
            start=date(2015, 1, 1),
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (("Page 3 Data", "A1", "Wrong"), "title changed"),
        (("Page 3 Data", "A2", "Wrong"), "unit label"),
        (("Page 3 Data", "B4", "Wrong"), "header changed"),
        (("Page 3 Data", "A5", "bad"), "quarterly period"),
        (("Page 3 Data", "B5", None), "missing or nonnumeric"),
        (("Page 13 Data", "B6", 101), "value is invalid"),
        (("Page 3 Data", "A6", None), "gap"),
        (("Page 13 Data", "A50", None), "declared lag"),
    ],
)
def test_workbook_shape_and_values_fail_closed(
    changes: tuple[str, str, object], message: str
) -> None:
    content = changed_workbook(changes)
    with pytest.raises(NyFedContractError, match=message):
        nyfed_history_rows(
            workbook_data(content=content, checksum=hashlib.sha256(content).hexdigest()),
            definitions=load_nyfed_series(),
            start=date(2015, 1, 1),
        )


def test_workbook_requires_attribution_and_all_registered_sheets() -> None:
    page_three_without_attribution = changed_workbook(("Page 3 Data", "E3", None))
    assert (
        len(
            nyfed_history_rows(
                workbook_data(
                    content=page_three_without_attribution,
                    checksum=hashlib.sha256(page_three_without_attribution).hexdigest(),
                ),
                definitions=load_nyfed_series(),
                start=date(2015, 1, 1),
            )
        )
        == 945
    )

    content = changed_workbook(
        ("Page 3 Data", "E3", None),
        ("Page 6 Data", "F2", None),
        ("Page 8 Data", "F2", None),
        ("Page 13 Data", "E3", None),
        ("Page 14 Data", "E3", None),
    )
    with pytest.raises(NyFedContractError, match="attribution"):
        nyfed_history_rows(
            workbook_data(content=content, checksum=hashlib.sha256(content).hexdigest()),
            definitions=load_nyfed_series(),
            start=date(2015, 1, 1),
        )

    definition = load_nyfed_series()[0]
    attrs = {**definition.expected_source_attributes, "sheet": "Missing"}
    with pytest.raises(NyFedContractError, match="missing sheets"):
        nyfed_history_rows(
            workbook_data(),
            definitions=(replace(definition, expected_source_attributes=attrs),),
            start=date(2015, 1, 1),
        )


def test_existing_snapshot_conflict_fails_closed(tmp_path: Path) -> None:
    store = AppendOnlyParquetStore(tmp_path)
    data = workbook_data()
    NyFedHistoryIngestor(store).ingest(data)
    definitions = load_nyfed_series()
    changed = (replace(definitions[0], units="Wrong"), *definitions[1:])
    with pytest.raises(NyFedContractError, match="does not match"):
        NyFedHistoryIngestor(store, definitions=changed).ingest(data)


def test_validator_rejects_empty_incomplete_and_corrupt_histories(tmp_path: Path) -> None:
    with pytest.raises(NyFedContractError, match="empty"):
        validate_nyfed_history(AppendOnlyParquetStore(tmp_path / "empty"))

    rows = nyfed_history_rows(
        workbook_data(), definitions=load_nyfed_series(), start=date(2015, 1, 1)
    )
    incomplete = [row for row in rows if row["series_id"] != "NYFED:HHDC:BALANCE:MORTGAGE"]
    incomplete_store = AppendOnlyParquetStore(tmp_path / "incomplete")
    incomplete_store.append("raw_observations", incomplete)
    with pytest.raises(NyFedContractError, match="coverage"):
        validate_nyfed_history(incomplete_store)

    bad_checksum = [dict(row) for row in rows]
    bad_checksum[0]["checksum"] = "bad"
    checksum_store = AppendOnlyParquetStore(tmp_path / "checksum")
    checksum_store.append("raw_observations", bad_checksum)
    with pytest.raises(NyFedContractError, match="checksum"):
        validate_nyfed_history(checksum_store)

    duplicate_store = AppendOnlyParquetStore(tmp_path / "duplicate")
    duplicate_store.append("raw_observations", [*rows, dict(rows[0])])
    with pytest.raises(NyFedContractError, match="duplicate"):
        validate_nyfed_history(duplicate_store)


def test_cli_writes_a_secret_free_receipt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    class FakeContextTransport:
        def __enter__(self) -> FakeContextTransport:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class FakeClient:
        def fetch(self) -> NyFedWorkbookData:
            return workbook_data()

    monkeypatch.setattr(nyfed, "HttpTransport", lambda **_kwargs: FakeContextTransport())
    monkeypatch.setattr(nyfed, "NyFedClient", lambda _transport: FakeClient())

    result = nyfed.main(["--lake-root", str(tmp_path / "lake")])
    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["snapshot"]["row_count"] == 945
    assert output["validation"]["snapshot_batches"] == 1
    with pytest.raises(SystemExit):
        nyfed.build_parser().parse_args(["--start", "bad"])
