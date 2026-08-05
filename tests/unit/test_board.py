from __future__ import annotations

import io
import zipfile
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from dfri.ingest.board import (
    BoardContractError,
    BoardReleaseData,
    FederalReserveBoardClient,
    g19_first_print_checksum,
    parse_archive_dates,
    parse_dated_release,
    parse_g19_first_print_flows,
    parse_release_dates_manifest,
    parse_sdmx_archive,
    release_timestamp,
    verify_series,
)
from dfri.ingest.http import HttpReceipt
from dfri.ingest.registry import load_board_series


def fixture_zip() -> bytes:
    fixture = Path(__file__).parents[1] / "fixtures" / "board" / "g19_sample.xml"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("G19_data.xml", fixture.read_bytes())
    return output.getvalue()


def dated_fixture(name: str) -> bytes:
    return (Path(__file__).parents[1] / "fixtures" / "board" / name).read_bytes()


def test_parse_real_board_sdmx_fragment() -> None:
    parsed = parse_sdmx_archive(fixture_zip(), "g19")
    series = parsed["DTCTLR.M"]
    assert series.title == (
        "Revolving consumer credit owned and securitized, seasonally adjusted level"
    )
    assert series.observations[-1].value == Decimal("1344207.79")
    assert series.observations[-1].period.isoformat() == "2026-05-31"


def test_verify_series_fails_on_metadata_drift() -> None:
    parsed = parse_sdmx_archive(fixture_zip(), "g19")
    definition = next(
        definition for definition in load_board_series() if definition.series_id == "DTCTLR.M"
    )
    data = BoardReleaseData(
        release="g19",
        source_url="https://www.federalreserve.gov/releases/g19/data/FRB_g19_xml.zip",
        checksum="a" * 64,
        retrieved_at="2026-08-04T00:00:00+00:00",
        series=parsed,
    )
    receipt = verify_series(data, (definition,))
    assert receipt[0].observations == 2

    drifted = replace(definition, expected_title="Wrong")
    with pytest.raises(BoardContractError, match="title mismatch"):
        verify_series(data, (drifted,))


def test_archive_index_discovers_dated_releases() -> None:
    html = (
        b'<a href="20150108/">Jan</a>'
        b'<a href="/releases/g19/20150206/">Feb</a>'
        b'<a href="20150206/g19.pdf">duplicate PDF</a>'
    )
    dates = parse_archive_dates(html, "g19")
    assert [item.isoformat() for item in dates] == ["2015-01-08", "2015-02-06"]


def test_parse_real_g19_dated_release_fragment() -> None:
    observations = parse_dated_release(dated_fixture("g19_20150108.html"), "g19", date(2015, 1, 8))
    by_series = {observation.series_id: observation for observation in observations}

    assert len(observations) == 6
    assert by_series["DTCTL.M"].period == date(2014, 11, 30)
    assert by_series["DTCTLR.M"].value == Decimal("882100.0")
    assert by_series["DTCTLN.M"].value == Decimal("2415800.0")
    assert by_series["DTCTL_N.M"].value == Decimal("3277700.0")
    assert by_series["DTCTLR_N.M"].value == Decimal("858700.0")
    assert by_series["DTCTLN_N.M"].value == Decimal("2419000.0")
    assert release_timestamp("g19", date(2015, 1, 8)).isoformat() == ("2015-01-08T20:00:00+00:00")


def test_parse_real_modern_march_g19_dated_release_fragment() -> None:
    observations = parse_dated_release(dated_fixture("g19_20200306.html"), "g19", date(2020, 3, 6))
    by_series = {observation.series_id: observation for observation in observations}

    assert len(observations) == 6
    assert {observation.period for observation in observations} == {date(2020, 1, 31)}
    assert by_series["DTCTL.M"].value == Decimal("4202700.0")
    assert by_series["DTCTLR.M"].value == Decimal("1090100.0")
    assert by_series["DTCTLN.M"].value == Decimal("3112600.0")
    assert by_series["DTCTL_N.M"].value == Decimal("4197100.0")
    assert by_series["DTCTLR_N.M"].value == Decimal("1065500.0")
    assert by_series["DTCTLN_N.M"].value == Decimal("3131500.0")


def test_parse_real_g19_first_print_flow_excerpt() -> None:
    flows = parse_g19_first_print_flows(
        dated_fixture("g19_20260708_first_print_excerpt.html"), date(2026, 7, 8)
    )
    by_target = {flow.target_series_id: flow for flow in flows}

    revolving = by_target["DELTA_DTCTLR.M"]
    assert revolving.level_series_id == "DTCTLR.M"
    assert revolving.target_period == date(2026, 5, 31)
    assert revolving.previous_period == date(2026, 4, 30)
    assert revolving.target_level == Decimal("1344200.0")
    assert revolving.previous_level == Decimal("1349500.0")
    assert revolving.value == Decimal("-5300.0")
    assert by_target["DELTA_DTCTLN.M"].value == Decimal("5100.0")


def test_g19_first_print_flow_accepts_adjacent_prior_header_with_bad_year_reference() -> None:
    content = dated_fixture("g19_20260708_first_print_excerpt.html").replace(
        b'<tr><th id="K3" colspan="4">2026</th></tr>',
        b'<tr><th id="J3">2025</th><th id="K3" colspan="4">2026</th></tr>',
    )
    content = content.replace(b'id="M4" headers="K3"', b'id="M4" headers="J3"')

    flows = parse_g19_first_print_flows(content, date(2026, 7, 8))

    assert {flow.value for flow in flows} == {Decimal("-5300.0"), Decimal("5100.0")}


def test_g19_first_print_checksum_excludes_page_wrapper_and_is_order_stable() -> None:
    flows = parse_g19_first_print_flows(
        dated_fixture("g19_20260708_first_print_excerpt.html"), date(2026, 7, 8)
    )

    checksum = g19_first_print_checksum(date(2026, 7, 8), flows)

    assert checksum == g19_first_print_checksum(date(2026, 7, 8), tuple(reversed(flows)))
    assert len(checksum) == 64
    changed = (replace(flows[0], target_level=flows[0].target_level + 1), flows[1])
    assert checksum != g19_first_print_checksum(date(2026, 7, 8), changed)


def test_parse_real_h8_dated_release_fragment() -> None:
    observations = parse_dated_release(dated_fixture("h8_20150102.html"), "h8", date(2015, 1, 2))
    lookup = {
        (observation.series_id, observation.period): observation.value
        for observation in observations
    }

    assert len(observations) == 12
    assert lookup[("B1029NCBA", date(2014, 12, 24))] == Decimal("1198600.0")
    assert lookup[("B1247NCBA", date(2014, 12, 3))] == Decimal("616400.0")
    assert lookup[("B3248NCBA", date(2014, 12, 17))] == Decimal("580100.0")
    assert release_timestamp("h8", date(2015, 1, 2)).isoformat() == ("2015-01-02T21:15:00+00:00")


def test_parse_real_modern_h8_dated_release_fragment() -> None:
    observations = parse_dated_release(dated_fixture("h8_20241220.html"), "h8", date(2024, 12, 20))
    lookup = {
        (observation.series_id, observation.period): observation.value
        for observation in observations
    }

    assert len(observations) == 12
    assert lookup[("B1029NCBA", date(2024, 12, 11))] == Decimal("1929800.0")
    assert lookup[("B1247NCBA", date(2024, 11, 20))] == Decimal("1077600.0")
    assert lookup[("B3248NCBA", date(2024, 12, 4))] == Decimal("846400.0")


def test_invalid_archives_fail_closed() -> None:
    with pytest.raises(BoardContractError, match="valid ZIP"):
        parse_sdmx_archive(b"not-a-zip", "g19")
    with pytest.raises(BoardContractError, match="No dated"):
        parse_archive_dates(b"<html></html>", "h8")


class FakeTransport:
    def __init__(self, release_content: bytes, archive_content: bytes = b"") -> None:
        self.release_content = release_content
        self.archive_content = archive_content

    def get(self, url: str, **_kwargs: object) -> HttpReceipt:
        is_release_index = url.endswith("releaseDates.json")
        content = (
            self.archive_content
            if url.endswith("2015.htm") or is_release_index
            else self.release_content
        )
        return HttpReceipt(
            content=content,
            source_url=url,
            checksum="b" * 64,
            retrieved_at=datetime(2026, 8, 4, tzinfo=UTC),
            status_code=200,
        )


def test_client_fetch_verify_and_archive_discovery() -> None:
    html = b'<a href="/releases/g19/20150908/">Sep</a>'
    manifest = dated_fixture("g19_release_dates_2026.json")
    client = FederalReserveBoardClient(FakeTransport(fixture_zip(), html))  # type: ignore[arg-type]
    definition = next(
        definition for definition in load_board_series() if definition.series_id == "DTCTLR.M"
    )

    verified = client.verify_release("g19", (definition,))
    assert verified[0].series_id == "DTCTLR.M"
    assert client.discover_archive_dates("g19", 2015)[0].year == 2015
    manifest_client = FederalReserveBoardClient(  # type: ignore[arg-type]
        FakeTransport(fixture_zip(), manifest)
    )
    assert manifest_client.discover_release_dates("g19")[0] == date(2026, 1, 8)

    with pytest.raises(BoardContractError, match="No registered"):
        client.verify_release("g19", ())
    with pytest.raises(ValueError, match="outside supported"):
        client.fetch_archive_index("g19", 1800)


def test_client_fetches_and_parses_exact_dated_release() -> None:
    client = FederalReserveBoardClient(FakeTransport(dated_fixture("g19_20150108.html")))  # type: ignore[arg-type]
    release = client.fetch_dated_release("g19", date(2015, 1, 8))

    assert release.release_at == release_timestamp("g19", date(2015, 1, 8))
    assert release.source_url.endswith("/releases/g19/20150108/")
    assert len(release.observations) == 6

    with pytest.raises(ValueError, match="outside supported"):
        client.fetch_dated_release("g19", date(1800, 1, 1))


def test_client_fetches_release_coherent_g19_targets() -> None:
    client = FederalReserveBoardClient(  # type: ignore[arg-type]
        FakeTransport(dated_fixture("g19_20260708_first_print_excerpt.html"))
    )

    release = client.fetch_g19_first_print(date(2026, 7, 8))

    assert release.release_at == release_timestamp("g19", date(2026, 7, 8))
    assert release.source_url.endswith("/releases/g19/20260708/")
    assert release.checksum != "b" * 64
    assert {flow.target_series_id for flow in release.flows} == {
        "DELTA_DTCTLR.M",
        "DELTA_DTCTLN.M",
    }

    with pytest.raises(ValueError, match="outside supported"):
        client.fetch_g19_first_print(date(1800, 1, 1))


def test_client_applies_pinned_manifest_and_declared_date_exception() -> None:
    client = FederalReserveBoardClient(  # type: ignore[arg-type]
        FakeTransport(dated_fixture("g19_20160204.html"))
    )

    release = client.fetch_dated_release("g19", date(2016, 2, 4))

    assert release.archive_date == date(2016, 2, 4)
    assert release.release_date == date(2016, 2, 5)
    assert release.release_at == release_timestamp("g19", date(2016, 2, 5))
    assert release.source_url.endswith("/releases/g19/20160204/")
    assert len(release.observations) == 6


def test_client_applies_pinned_holiday_archive_path_exception() -> None:
    client = FederalReserveBoardClient(  # type: ignore[arg-type]
        FakeTransport(dated_fixture("h8_20221110.html"))
    )

    release = client.fetch_dated_release("h8", date(2022, 11, 11))

    assert release.archive_date == date(2022, 11, 10)
    assert release.release_date == date(2022, 11, 10)
    assert release.release_at == release_timestamp("h8", date(2022, 11, 10))
    assert release.source_url.endswith("/releases/h8/20221110/")
    assert len(release.observations) == 12


def test_more_archive_contract_failures() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("wrong.xml", b"<root />")
    with pytest.raises(BoardContractError, match=r"missing G19_data\.xml"):
        parse_sdmx_archive(output.getvalue(), "g19")

    malformed = io.BytesIO()
    with zipfile.ZipFile(malformed, "w") as archive:
        archive.writestr("G19_data.xml", b"<broken")
    with pytest.raises(BoardContractError, match="malformed"):
        parse_sdmx_archive(malformed.getvalue(), "g19")


def test_verify_rejects_missing_series_and_attribute_drift() -> None:
    parsed = parse_sdmx_archive(fixture_zip(), "g19")
    definition = next(
        definition for definition in load_board_series() if definition.series_id == "DTCTLR.M"
    )
    data = BoardReleaseData(
        release="g19",
        source_url="https://www.federalreserve.gov/releases/g19/data/FRB_g19_xml.zip",
        checksum="a" * 64,
        retrieved_at="2026-08-04T00:00:00+00:00",
        series=parsed,
    )
    with pytest.raises(BoardContractError, match="missing"):
        verify_series(data, (replace(definition, series_id="MISSING"),))
    with pytest.raises(BoardContractError, match="attribute SA mismatch"):
        verify_series(
            data,
            (replace(definition, expected_source_attributes={"SA": "NSA"}),),
        )
    with pytest.raises(BoardContractError, match="units mismatch"):
        verify_series(data, (replace(definition, units="Wrong"),))
    with pytest.raises(BoardContractError, match="frequency mismatch"):
        verify_series(data, (replace(definition, frequency="Wrong"),))


@pytest.mark.parametrize(
    ("content", "release", "expected_date", "message"),
    [
        (b"not utf8 \xff", "g19", date(2015, 1, 8), "not UTF-8"),
        (b"<html>Release Date: January 9, 2015</html>", "g19", date(2015, 1, 8), "mismatch"),
        (b"<html>no release date</html>", "h8", date(2015, 1, 2), "missing its release date"),
    ],
)
def test_dated_release_contract_failures(
    content: bytes, release: str, expected_date: date, message: str
) -> None:
    with pytest.raises(BoardContractError, match=message):
        parse_dated_release(content, release, expected_date)  # type: ignore[arg-type]


def test_parse_real_board_release_dates_manifest_fragment() -> None:
    dates = parse_release_dates_manifest(dated_fixture("g19_release_dates_2026.json"), "g19")

    assert dates[0] == date(2026, 1, 8)
    assert dates[-1] == date(2026, 7, 8)
    assert len(dates) == 7


@pytest.mark.parametrize(
    "content",
    [
        b"not-json",
        b"{}",
        b'[{"yearValue":"2026","Months":{}}]',
        b'[{"yearValue":"2026","Months":[{"MonthValue":"202601","Dates":["bad"]}]}]',
        (
            b'[{"yearValue":"2026","Months":['
            b'{"MonthValue":"202601","Dates":["20260108","20260108"]}]}]'
        ),
    ],
)
def test_invalid_release_dates_manifests_fail_closed(content: bytes) -> None:
    with pytest.raises(BoardContractError):
        parse_release_dates_manifest(content, "g19")
