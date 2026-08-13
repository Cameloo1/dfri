from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from dfri.ingest.http import HttpReceipt
from dfri.ingest.ncua import (
    NCUA_DATA_URL_TEMPLATE,
    NcuaClient,
    NcuaContractError,
    parse_ncua_call_report,
)


def _bulk(*, mismatched_panel: bool = False) -> bytes:
    cycle = "03/31/2026 12:00:00 AM"
    schedules = {
        "FS220A.txt": (
            "CU_NUMBER,CYCLE_DATE,ACCT_370,ACCT_385,ACCT_396,ACCT_002\n"
            f'1,"{cycle}",100,100,50,10\n'
            f'2,"{cycle}",40,60,20,5\n'
        ),
        "FS220L.txt": (
            "CU_NUMBER,CYCLE_DATE,ACCT_025B1,ACCT_400A1,ACCT_400B1\n"
            f'1,"{cycle}",500,50,20\n'
            f'2,"{cycle}",300,20,5\n'
        ),
        "FS220R.txt": (
            "CU_NUMBER,CYCLE_DATE,ACCT_RL0047\n"
            f'1,"{cycle}",100\n' + ("" if mismatched_panel else f'2,"{cycle}",80\n')
        ),
    }
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for filename, content in schedules.items():
            archive.writestr(filename, content)
    return output.getvalue()


def test_ncua_parser_reconciles_auto_and_residual_consumer_loans() -> None:
    content = _bulk()
    observation = parse_ncua_call_report(
        content,
        report_date=date(2026, 3, 31),
        source_url="https://ncua.gov/files/publications/analysis/call-report-data-2026-03.zip",
        checksum=hashlib.sha256(content).hexdigest(),
    )

    assert observation.record_count == 2
    assert observation.automobile == 300
    assert observation.nonrevolving_consumer_residual == 440
    assert observation.automobile_share_of_nonrevolving_consumer == pytest.approx(300 / 440)


def test_ncua_parser_rejects_checksum_mismatch() -> None:
    with pytest.raises(NcuaContractError, match="checksum"):
        parse_ncua_call_report(
            _bulk(),
            report_date=date(2026, 3, 31),
            source_url="https://ncua.gov/example.zip",
            checksum="0" * 64,
        )


def test_ncua_parser_fails_closed_when_schedule_panels_differ() -> None:
    content = _bulk(mismatched_panel=True)
    with pytest.raises(NcuaContractError, match="panels differ"):
        parse_ncua_call_report(
            content,
            report_date=date(2026, 3, 31),
            source_url="https://ncua.gov/example.zip",
            checksum=hashlib.sha256(content).hexdigest(),
        )


def test_ncua_client_requests_the_pinned_quarterly_bulk_file() -> None:
    content = _bulk()

    class FakeTransport:
        def get(self, url: str, **kwargs: object) -> HttpReceipt:
            assert url == NCUA_DATA_URL_TEMPLATE.format(year=2026, month=3)
            assert kwargs["headers"] == {"Accept": "application/zip"}
            return HttpReceipt(
                content=content,
                source_url=url,
                checksum=hashlib.sha256(content).hexdigest(),
                retrieved_at=datetime(2026, 8, 9, tzinfo=UTC),
                status_code=200,
            )

    observation = NcuaClient(FakeTransport()).fetch(date(2026, 3, 31))  # type: ignore[arg-type]
    assert observation.record_count == 2

    with pytest.raises(NcuaContractError, match="quarter end"):
        NcuaClient(FakeTransport()).fetch(date(2026, 2, 28))  # type: ignore[arg-type]


def test_ncua_parser_rejects_a_non_zip_response() -> None:
    content = b"not-a-zip"
    with pytest.raises(NcuaContractError, match="valid ZIP"):
        parse_ncua_call_report(
            content,
            report_date=date(2026, 3, 31),
            source_url="https://ncua.gov/example.zip",
            checksum=hashlib.sha256(content).hexdigest(),
        )


@pytest.mark.parametrize(
    ("filename", "old", "new", "message"),
    [
        ("FS220A.txt", "ACCT_370,", "MISSING_FIELD,", "fields changed"),
        ("FS220A.txt", '2,"03/31/2026 12:00:00 AM"', '1,"03/31/2026 12:00:00 AM"', "duplicated"),
        ("FS220A.txt", "03/31/2026", "03/30/2026", "cycle date changed"),
        ("FS220A.txt", "100,100,50,10", "bad,100,50,10", "nonnumeric"),
        ("FS220A.txt", "100,100,50,10", "-1,100,50,10", "nonnegative integer"),
    ],
)
def test_ncua_parser_rejects_schedule_contract_drift(
    filename: str,
    old: str,
    new: str,
    message: str,
) -> None:
    content = _rewrite_bulk(filename, old, new)
    with pytest.raises(NcuaContractError, match=message):
        parse_ncua_call_report(
            content,
            report_date=date(2026, 3, 31),
            source_url="https://ncua.gov/example.zip",
            checksum=hashlib.sha256(content).hexdigest(),
        )


def test_ncua_parser_rejects_a_missing_schedule() -> None:
    content = _without_schedule("FS220R.txt")
    with pytest.raises(NcuaContractError, match=r"missing FS220R\.txt"):
        parse_ncua_call_report(
            content,
            report_date=date(2026, 3, 31),
            source_url="https://ncua.gov/example.zip",
            checksum=hashlib.sha256(content).hexdigest(),
        )


def _rewrite_bulk(filename: str, old: str, new: str) -> bytes:
    source = BytesIO(_bulk())
    output = BytesIO()
    with (
        ZipFile(source) as input_archive,
        ZipFile(output, "w", compression=ZIP_DEFLATED) as archive,
    ):
        for name in input_archive.namelist():
            content = input_archive.read(name).decode()
            archive.writestr(name, content.replace(old, new, 1) if name == filename else content)
    return output.getvalue()


def _without_schedule(filename: str) -> bytes:
    source = BytesIO(_bulk())
    output = BytesIO()
    with (
        ZipFile(source) as input_archive,
        ZipFile(output, "w", compression=ZIP_DEFLATED) as archive,
    ):
        for name in input_archive.namelist():
            if name != filename:
                archive.writestr(name, input_archive.read(name))
    return output.getvalue()
