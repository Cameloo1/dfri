from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from dfri.ingest.census_archive import (
    CensusArchiveEntry,
    CensusArchiveError,
    CensusArchiveIngestor,
    CensusMartsArchiveClient,
    CensusMartsReleaseData,
    discover_marts_releases,
    marts_release_row,
    parse_marts_release,
    parse_marts_release_text,
    validate_census_archive,
)
from dfri.ingest.http import HttpReceipt
from dfri.ingest.registry import load_census_archive
from dfri.lake.store import AppendOnlyParquetStore

FIXTURES = Path(__file__).parents[1] / "fixtures" / "census"


class FakeTransport:
    def __init__(self, receipts: dict[str, HttpReceipt]) -> None:
        self.receipts = receipts

    def get(self, url: str, **_kwargs: object) -> HttpReceipt:
        return self.receipts[url]


class FakeArchiveClient:
    def __init__(self, releases: dict[str, CensusMartsReleaseData]) -> None:
        self.releases = releases
        self.calls: list[str] = []

    def fetch(self, entry: CensusArchiveEntry) -> CensusMartsReleaseData:
        self.calls.append(entry.source_url)
        return self.releases[entry.source_url]


def fixture_receipt(name: str, source_url: str) -> HttpReceipt:
    content = (FIXTURES / name).read_bytes()
    return HttpReceipt(
        content=content,
        source_url=source_url,
        checksum=hashlib.sha256(content).hexdigest(),
        retrieved_at=datetime(2026, 8, 4, 18, 58, tzinfo=UTC),
        status_code=200,
    )


def synthetic_release(month: int) -> CensusMartsReleaseData:
    target_period = date(2015, month + 1, 1) - date.resolution
    prior_period = target_period.replace(day=1) - date.resolution
    release_at = datetime(2015, month + 1, 12, 13, 30, tzinfo=UTC)
    current = 400_000.0 + month
    prior = 399_000.0 + month
    return CensusMartsReleaseData(
        target_period=target_period,
        prior_period=prior_period,
        release_at=release_at,
        current_level=current,
        prior_level=prior,
        flow=current - prior,
        source_url=(
            f"https://www2.census.gov/retail/releases/historical/marts/adv15{month:02d}.pdf"
        ),
        checksum=f"{month:064x}",
        retrieved_at=datetime(2026, 8, 4, tzinfo=UTC),
    )


def test_archive_registry_pins_first_print_derivation() -> None:
    definition = load_census_archive()

    assert definition.series_id == "DELTA_RETAIL_SALES.M"
    assert definition.lake_source == "CENSUS_MARTS_ARCHIVE"
    assert "same immutable dated MARTS Table 1" in definition.derivation
    assert definition.source_url_pattern.endswith("advYYMM.pdf")
    assert definition.release_time == "08:30"
    assert definition.time_zone == "America/New_York"


def test_discovery_deduplicates_archive_links_and_rejects_gaps() -> None:
    html = b"""
    <a href="https://www2.census.gov/retail/releases/historical/marts/adv1801.pdf">pdf</a>
    <a href="https://www2.census.gov/retail/releases/historical/marts/adv1801.pdf">January</a>
    <a href="https://www2.census.gov/retail/releases/historical/marts/adv1802.pdf">pdf</a>
    <a href="https://www2.census.gov/retail/releases/historical/marts/adv5310.pdf">1953</a>
    """
    entries = discover_marts_releases(html, start=date(2018, 1, 1))

    assert [item.target_period for item in entries] == [date(2018, 1, 31), date(2018, 2, 28)]
    with pytest.raises(CensusArchiveError, match="gap"):
        discover_marts_releases(
            html.replace(b"adv1802.pdf", b"adv1803.pdf"),
            start=date(2018, 1, 1),
        )


def test_discovery_rejects_malformed_or_empty_indexes() -> None:
    with pytest.raises(CensusArchiveError, match="not UTF-8"):
        discover_marts_releases(b"\xff", start=date(2018, 1, 1))
    with pytest.raises(CensusArchiveError, match="no releases"):
        discover_marts_releases(b"<html></html>", start=date(2018, 1, 1))
    with pytest.raises(CensusArchiveError, match="invalid month"):
        discover_marts_releases(
            b"https://www2.census.gov/retail/releases/historical/marts/adv1813.pdf",
            start=date(2018, 1, 1),
        )


def test_archive_client_discovers_and_fetches_official_artifacts() -> None:
    definition = load_census_archive()
    source_url = "https://www2.census.gov/retail/releases/historical/marts/adv1501.pdf"
    pdf = fixture_receipt("marts_adv1501.pdf", source_url)
    index_body = source_url.encode()
    index = HttpReceipt(
        content=index_body,
        source_url=definition.archive_index_url,
        checksum=hashlib.sha256(index_body).hexdigest(),
        retrieved_at=datetime(2026, 8, 4, tzinfo=UTC),
        status_code=200,
    )
    client = CensusMartsArchiveClient(
        FakeTransport({definition.archive_index_url: index, source_url: pdf})  # type: ignore[arg-type]
    )

    entries = client.discover(start=date(2015, 1, 1))
    release = client.fetch(entries[0])

    assert entries == (CensusArchiveEntry(date(2015, 1, 31), source_url),)
    assert release.flow == -3_504.0


@pytest.mark.parametrize(
    ("name", "period", "release_at", "current", "prior", "flow"),
    [
        (
            "marts_adv1501.pdf",
            date(2015, 1, 31),
            datetime(2015, 2, 12, 13, 30, tzinfo=UTC),
            439_771.0,
            443_275.0,
            -3_504.0,
        ),
        (
            "marts_adv2605.pdf",
            date(2026, 5, 31),
            datetime(2026, 6, 17, 12, 30, tzinfo=UTC),
            763_705.0,
            757_036.0,
            6_669.0,
        ),
    ],
)
def test_real_release_pdfs_parse_exact_first_print_values(
    name: str,
    period: date,
    release_at: datetime,
    current: float,
    prior: float,
    flow: float,
) -> None:
    url = (
        f"https://www2.census.gov/retail/releases/historical/marts/adv{period.strftime('%y%m')}.pdf"
    )
    parsed = parse_marts_release(fixture_receipt(name, url))

    assert parsed.target_period == period
    assert parsed.prior_period == period.replace(day=1) - date.resolution
    assert parsed.release_at == release_at
    assert parsed.current_level == current
    assert parsed.prior_level == prior
    assert parsed.flow == flow


def test_release_parser_fails_closed_on_provenance_and_layout_changes() -> None:
    receipt = fixture_receipt(
        "marts_adv1501.pdf",
        "https://www2.census.gov/retail/releases/historical/marts/adv1501.pdf",
    )
    with pytest.raises(CensusArchiveError, match="URL mismatch"):
        parse_marts_release(replace(receipt, source_url="https://example.test/adv1501.pdf"))
    with pytest.raises(CensusArchiveError, match="checksum"):
        parse_marts_release(replace(receipt, checksum="0" * 64))
    with pytest.raises(CensusArchiveError, match="not a PDF"):
        parse_marts_release(replace(receipt, content=b"not a pdf"))
    with pytest.raises(CensusArchiveError, match="timezone-aware"):
        parse_marts_release(
            replace(receipt, retrieved_at=datetime(2026, 8, 4, tzinfo=UTC).replace(tzinfo=None))
        )
    with pytest.raises(CensusArchiveError, match="target period"):
        parse_marts_release(
            replace(
                receipt,
                source_url=("https://www2.census.gov/retail/releases/historical/marts/adv1502.pdf"),
            )
        )

    base = """
    FOR RELEASE AT 8:30 AM EST, THURSDAY, FEBRUARY 15, 2024
    JANUARY 2024
    Table\u00a01.\u00a0Estimated Monthly Sales for Retail and Food Services, by Kind of Business
    Retail & food services,
      total 1 2 3
    Total (excl. motor vehicle & parts)
    """
    with pytest.raises(CensusArchiveError, match="shape changed"):
        parse_marts_release_text(
            base,
            target_period=date(2024, 1, 31),
            source_url="https://www2.census.gov/retail/releases/historical/marts/adv2401.pdf",
            checksum="1" * 64,
            retrieved_at=datetime(2024, 2, 16, tzinfo=UTC),
        )

    with pytest.raises(CensusArchiveError, match="timestamp is missing"):
        parse_marts_release_text(
            "JANUARY 2024 Table 1.",
            target_period=date(2024, 1, 31),
            source_url="https://www2.census.gov/retail/releases/historical/marts/adv2401.pdf",
            checksum="1" * 64,
            retrieved_at=datetime(2024, 2, 16, tzinfo=UTC),
        )
    with pytest.raises(CensusArchiveError, match="multiple release timestamp"):
        parse_marts_release_text(
            base + " THURSDAY, FEBRUARY 15, 2024, AT 8:30 A.M. EST",
            target_period=date(2024, 1, 31),
            source_url="https://www2.census.gov/retail/releases/historical/marts/adv2401.pdf",
            checksum="1" * 64,
            retrieved_at=datetime(2024, 2, 16, tzinfo=UTC),
        )
    with pytest.raises(CensusArchiveError, match="timezone label"):
        parse_marts_release_text(
            base.replace("EST", "EDT"),
            target_period=date(2024, 1, 31),
            source_url="https://www2.census.gov/retail/releases/historical/marts/adv2401.pdf",
            checksum="1" * 64,
            retrieved_at=datetime(2024, 2, 16, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (
            "FOR RELEASE AT 8:30 AM EDT, THURSDAY, MAY 15, 2025 JANUARY 2025",
            "lag exceeds",
        ),
        (
            "FOR RELEASE AT 8:30 AM EST, THURSDAY, FEBRUARY 15, 2024 JANUARY 2024",
            "registered Table 1 title",
        ),
    ],
)
def test_release_text_rejects_lag_and_missing_table(content: str, message: str) -> None:
    with pytest.raises(CensusArchiveError, match=message):
        parse_marts_release_text(
            content,
            target_period=date(2024 if "2024" in content else 2025, 1, 31),
            source_url="https://www2.census.gov/retail/releases/historical/marts/adv2401.pdf",
            checksum="1" * 64,
            retrieved_at=datetime(2025, 6, 1, tzinfo=UTC),
        )


def test_legacy_release_header_allows_verified_missing_weekday_comma() -> None:
    text = """
    FOR IMMEDIATE RELEASE
    WEDNESDAY APRIL 13, 2016, AT 8:30 A.M. EDT
    MARCH 2016
    Table 1. Estimated Monthly Sales for Retail and Food Services, by Kind of Business
    Retail & food services,
      total 1,000,000 1.0 500,000 490,000 480,000 470,000 460,000
      446,900 448,000 430,000 420,000 410,000
    Total (excl. motor vehicle & parts)
    """

    parsed = parse_marts_release_text(
        text,
        target_period=date(2016, 3, 31),
        source_url="https://www2.census.gov/retail/releases/historical/marts/adv1603.pdf",
        checksum="1" * 64,
        retrieved_at=datetime(2016, 4, 14, tzinfo=UTC),
    )

    assert parsed.release_at == datetime(2016, 4, 13, 12, 30, tzinfo=UTC)
    assert parsed.flow == -1_100.0


def test_modern_release_header_allows_verified_split_timezone_text() -> None:
    text = """
    FOR RELEASE AT 8:30 AM ED T, THURSDAY, SEPTEMBER 15, 2022
    AUGUST 2022
    Table 1. Estimated Monthly Sales for Retail and Food Services, by Kind of Business
    Retail & food services,
      total 1,000,000 1.0 500,000 490,000 480,000 470,000 460,000
      500,500 499,000 430,000 420,000 410,000
    Total (excl. motor vehicle & parts)
    """

    parsed = parse_marts_release_text(
        text,
        target_period=date(2022, 8, 31),
        source_url="https://www2.census.gov/retail/releases/historical/marts/adv2208.pdf",
        checksum="2" * 64,
        retrieved_at=datetime(2022, 9, 16, tzinfo=UTC),
    )

    assert parsed.release_at == datetime(2022, 9, 15, 12, 30, tzinfo=UTC)
    assert parsed.flow == 1_500.0


def test_archive_ingest_is_idempotent_and_validation_requires_exact_coverage(
    tmp_path: Path,
) -> None:
    store = AppendOnlyParquetStore(tmp_path / "lake")
    ingestor = CensusArchiveIngestor(store)
    january = synthetic_release(1)
    february = synthetic_release(2)

    first = ingestor.ingest(january)
    repeated = ingestor.ingest(january)
    ingestor.ingest(february)
    entries = (
        CensusArchiveEntry(january.target_period, january.source_url),
        CensusArchiveEntry(february.target_period, february.source_url),
    )
    validation = validate_census_archive(store, entries)

    assert not first.already_present
    assert repeated.already_present
    assert validation.total_rows == 2
    assert validation.earliest_period == date(2015, 1, 31)
    assert validation.latest_period == date(2015, 2, 28)
    with pytest.raises(CensusArchiveError, match="period coverage"):
        validate_census_archive(store, entries[:1])
    with pytest.raises(CensusArchiveError, match="changed checksum"):
        ingestor.ingest(replace(january, checksum="f" * 64))


def test_backfill_resumes_rechecks_and_reloads_existing_index(tmp_path: Path) -> None:
    store = AppendOnlyParquetStore(tmp_path / "lake")
    release = synthetic_release(1)
    entry = CensusArchiveEntry(release.target_period, release.source_url)
    client = FakeArchiveClient({release.source_url: release})
    ingestor = CensusArchiveIngestor(store, client=client)  # type: ignore[arg-type]

    assert len(ingestor.backfill((entry,))) == 1
    assert ingestor.backfill((entry,)) == ()
    reloaded = CensusArchiveIngestor(store, client=client)  # type: ignore[arg-type]
    checked = reloaded.backfill((entry,), recheck_complete=True)

    assert checked[0].already_present
    assert client.calls == [release.source_url, release.source_url]
    with pytest.raises(CensusArchiveError, match="requires a Census archive client"):
        CensusArchiveIngestor(store).backfill((entry,))


def test_release_row_rejects_incoherent_flow_and_predated_retrieval() -> None:
    release = synthetic_release(1)
    with pytest.raises(CensusArchiveError, match="release-coherent"):
        marts_release_row(replace(release, flow=999.0))
    with pytest.raises(CensusArchiveError, match="predates"):
        marts_release_row(replace(release, retrieved_at=release.release_at - timedelta(days=1)))

    with pytest.raises(CensusArchiveError, match="source URL"):
        marts_release_row(replace(release, source_url="https://example.test/release.pdf"))
    with pytest.raises(CensusArchiveError, match="prior period"):
        marts_release_row(replace(release, prior_period=date(2014, 1, 31)))
    with pytest.raises(CensusArchiveError, match="lowercase SHA-256"):
        marts_release_row(replace(release, checksum="bad"))
    with pytest.raises(CensusArchiveError, match="target-month boundary"):
        marts_release_row(replace(release, release_at=datetime(2015, 1, 1, tzinfo=UTC)))
    with pytest.raises(CensusArchiveError, match="retrieval timestamp"):
        marts_release_row(
            replace(
                release,
                retrieved_at=datetime(2026, 8, 4, tzinfo=UTC).replace(tzinfo=None),
            )
        )
