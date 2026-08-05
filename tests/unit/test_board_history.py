from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

from dfri.ingest.board import BoardDatedReleaseData, parse_dated_release, release_timestamp
from dfri.ingest.board_history import (
    BOARD_SOURCE,
    BOARD_UNIT,
    BoardHistoryError,
    BoardHistoryIngestor,
    dated_release_rows,
    select_first_print,
    validate_board_history,
)
from dfri.lake.guard import VintageGuard
from dfri.lake.readers import LakeSeriesReader
from dfri.lake.store import AppendOnlyParquetStore


def g19_release() -> BoardDatedReleaseData:
    release_date = date(2015, 1, 8)
    content = (Path(__file__).parents[1] / "fixtures" / "board" / "g19_20150108.html").read_bytes()
    return BoardDatedReleaseData(
        release="g19",
        archive_date=release_date,
        release_date=release_date,
        release_at=release_timestamp("g19", release_date),
        source_url="https://www.federalreserve.gov/releases/g19/20150108/",
        checksum=hashlib.sha256(content).hexdigest(),
        retrieved_at=datetime(2026, 8, 4, tzinfo=UTC),
        observations=parse_dated_release(content, "g19", release_date),
    )


def test_dated_release_rows_use_strict_provenance_contract() -> None:
    rows = dated_release_rows(g19_release())

    assert len(rows) == 6
    assert {row["source"] for row in rows} == {BOARD_SOURCE}
    assert {row["unit"] for row in rows} == {BOARD_UNIT}
    assert {row["vintage_date"] for row in rows} == {date(2015, 1, 8)}
    assert {row["release_date"] for row in rows} == {datetime(2015, 1, 8, 20, 0, tzinfo=UTC)}


def test_ingest_is_idempotent_by_source_url_and_checksum(tmp_path: Path) -> None:
    store = AppendOnlyParquetStore(tmp_path)
    ingestor = BoardHistoryIngestor(store)
    release = g19_release()

    first = ingestor.ingest(release)
    second = BoardHistoryIngestor(store).ingest(
        replace(release, retrieved_at=datetime(2026, 8, 5, tzinfo=UTC))
    )

    assert first.already_present is False
    assert first.batch_path is not None
    assert first.content_hash is not None
    assert second.already_present is True
    assert second.batch_path == first.batch_path
    assert second.content_hash == first.content_hash
    assert store.read_table("raw_observations").height == 6


def test_changed_source_checksum_appends_an_audited_batch(tmp_path: Path) -> None:
    store = AppendOnlyParquetStore(tmp_path)
    ingestor = BoardHistoryIngestor(store)
    release = g19_release()

    first = ingestor.ingest(release)
    changed = ingestor.ingest(replace(release, checksum="b" * 64))

    assert first.content_hash != changed.content_hash
    assert store.read_table("raw_observations").height == 12


def test_history_validator_proves_manifest_and_first_print_coverage(tmp_path: Path) -> None:
    store = AppendOnlyParquetStore(tmp_path)
    BoardHistoryIngestor(store).ingest(g19_release())

    report = validate_board_history(store, {"g19": (date(2015, 1, 8),)})

    assert report.total_rows == 6
    assert report.release_pages == 1
    assert report.pages_by_release == {"g19": 1}
    assert set(report.raw_rows_by_series.values()) == {1}
    assert set(report.first_print_rows_by_series.values()) == {1}
    assert report.earliest_release == date(2015, 1, 8)
    assert report.latest_release == date(2015, 1, 8)

    with pytest.raises(BoardHistoryError, match="coverage mismatch"):
        validate_board_history(store, {"g19": (date(2015, 1, 8), date(2015, 2, 6))})


def test_same_checksum_with_conflicting_stored_rows_fails_closed(tmp_path: Path) -> None:
    store = AppendOnlyParquetStore(tmp_path)
    release = g19_release()
    rows = dated_release_rows(release)
    rows[0]["value"] = 999.0
    store.append("raw_observations", rows)

    with pytest.raises(BoardHistoryError, match="does not match"):
        BoardHistoryIngestor(store).ingest(release)


def test_first_print_selection_runs_after_vintage_guard(tmp_path: Path) -> None:
    store = AppendOnlyParquetStore(tmp_path)
    common: dict[str, object] = {
        "source": BOARD_SOURCE,
        "series_id": "B1247NCBA",
        "obs_period": date(2014, 12, 24),
        "unit": BOARD_UNIT,
        "ingested_at": datetime(2026, 8, 4, tzinfo=UTC),
    }
    store.append(
        "raw_observations",
        [
            {
                **common,
                "value": 620_100.0,
                "release_date": datetime(2015, 1, 2, 21, 15, tzinfo=UTC),
                "vintage_date": date(2015, 1, 2),
                "source_url": "https://www.federalreserve.gov/releases/h8/20150102/",
                "checksum": "a" * 64,
            },
            {
                **common,
                "value": 621_000.0,
                "release_date": datetime(2015, 1, 9, 21, 15, tzinfo=UTC),
                "vintage_date": date(2015, 1, 9),
                "source_url": "https://www.federalreserve.gov/releases/h8/20150109/",
                "checksum": "b" * 64,
            },
        ],
    )

    guarded = VintageGuard(LakeSeriesReader(store)).read(
        "B1247NCBA", datetime(2015, 1, 10, tzinfo=UTC)
    )
    selected = select_first_print(guarded)

    assert selected.height == 1
    assert selected["value"].item() == 620_100.0
    assert selected["vintage_date"].item() == date(2015, 1, 2)


def test_first_print_selection_rejects_conflicting_same_release_values() -> None:
    frame = pl.DataFrame(
        {
            "source": [BOARD_SOURCE, BOARD_SOURCE],
            "series_id": ["B1247NCBA", "B1247NCBA"],
            "obs_period": [date(2014, 12, 24), date(2014, 12, 24)],
            "value": [620_100.0, 999_999.0],
            "release_date": [
                datetime(2015, 1, 2, 21, 15, tzinfo=UTC),
                datetime(2015, 1, 2, 21, 15, tzinfo=UTC),
            ],
            "vintage_date": [date(2015, 1, 2), date(2015, 1, 2)],
            "source_url": [
                "https://www.federalreserve.gov/releases/h8/20150102/",
                "https://www.federalreserve.gov/releases/h8/20150102/",
            ],
            "ingested_at": [
                datetime(2026, 8, 4, tzinfo=UTC),
                datetime(2026, 8, 5, tzinfo=UTC),
            ],
            "checksum": ["a" * 64, "b" * 64],
        }
    )

    with pytest.raises(BoardHistoryError, match="conflicting"):
        select_first_print(frame)


def test_first_print_selection_excludes_revised_sdmx_snapshot_at_same_release() -> None:
    common = {
        "source": BOARD_SOURCE,
        "series_id": "DTCTLR.M",
        "obs_period": date(2026, 5, 31),
        "release_date": datetime(2026, 7, 8, 19, 0, tzinfo=UTC),
        "vintage_date": date(2026, 7, 8),
        "ingested_at": datetime(2026, 8, 4, tzinfo=UTC),
    }
    frame = pl.DataFrame(
        [
            {
                **common,
                "value": 1_344_200.0,
                "source_url": "https://www.federalreserve.gov/releases/g19/20260708/",
                "checksum": "a" * 64,
            },
            {
                **common,
                "value": 1_344_207.79,
                "source_url": ("https://www.federalreserve.gov/releases/g19/data/FRB_g19_xml.zip"),
                "checksum": "b" * 64,
            },
        ]
    )

    selected = select_first_print(frame)

    assert selected.height == 1
    assert selected["value"].item() == 1_344_200.0


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"source_url": "https://example.com/"}, "URL mismatch"),
        ({"checksum": "not-sha256"}, "checksum"),
        ({"release_at": datetime.fromisoformat("2015-01-08T20:00:00")}, "timezone-aware"),
    ],
)
def test_invalid_release_metadata_fails_closed(change: dict[str, object], message: str) -> None:
    with pytest.raises(BoardHistoryError, match=message):
        dated_release_rows(replace(g19_release(), **change))


def test_fetch_requires_an_explicit_client(tmp_path: Path) -> None:
    with pytest.raises(BoardHistoryError, match="requires a Board client"):
        BoardHistoryIngestor(AppendOnlyParquetStore(tmp_path)).fetch_and_ingest(
            "g19", date(2015, 1, 8)
        )
