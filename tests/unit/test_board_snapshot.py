from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

import dfri.ingest.board_snapshot as board_snapshot
from dfri.ingest.board import (
    RELEASE_URLS,
    BoardDatedReleaseData,
    BoardObservation,
    BoardRelease,
    BoardReleaseData,
    BoardSeries,
    parse_dated_release,
    release_timestamp,
)
from dfri.ingest.board_history import BoardHistoryIngestor, validate_board_history
from dfri.ingest.board_snapshot import (
    BoardSnapshotError,
    BoardSnapshotIngestor,
    current_snapshot_rows,
    validate_board_snapshots,
)
from dfri.ingest.registry import BoardSeriesDefinition, load_board_series
from dfri.lake.store import AppendOnlyParquetStore

SNAPSHOT_DATES: dict[BoardRelease, date] = {
    "g19": date(2026, 7, 8),
    "h8": date(2026, 7, 31),
}


def definitions_for(release: BoardRelease) -> tuple[BoardSeriesDefinition, ...]:
    return tuple(item for item in load_board_series() if item.release == release)


def release_data(
    release: BoardRelease,
    *,
    checksum: str = "a" * 64,
    retrieved_at: str = "2026-08-04T08:13:00+00:00",
) -> BoardReleaseData:
    periods = (
        (date(2026, 4, 30), date(2026, 5, 31))
        if release == "g19"
        else (date(2026, 7, 15), date(2026, 7, 22))
    )
    series = {
        definition.series_id: BoardSeries(
            series_id=definition.series_id,
            title=definition.expected_title,
            attributes=definition.expected_source_attributes,
            observations=tuple(
                BoardObservation(
                    period=period,
                    value=Decimal(1_000_000 + (index * 1_000)),
                    status="A",
                )
                for index, period in enumerate(periods)
            ),
        )
        for definition in definitions_for(release)
    }
    return BoardReleaseData(
        release=release,
        source_url=RELEASE_URLS[release],
        checksum=checksum,
        retrieved_at=retrieved_at,
        series=series,
    )


def test_snapshot_ingest_is_idempotent_and_explicitly_revised(tmp_path: Path) -> None:
    store = AppendOnlyParquetStore(tmp_path)
    ingestor = BoardSnapshotIngestor(store)
    data = release_data("g19")

    first = ingestor.ingest(data, snapshot_date=SNAPSHOT_DATES["g19"])
    second = BoardSnapshotIngestor(store).ingest(
        replace(data, retrieved_at="2026-08-05T08:13:00+00:00"),
        snapshot_date=SNAPSHOT_DATES["g19"],
    )

    assert first.row_count == 12
    assert first.already_present is False
    assert second.already_present is True
    assert second.batch_path == first.batch_path
    frame = store.read_table("raw_observations")
    assert frame.height == 12
    assert frame["vintage_date"].unique().to_list() == [SNAPSHOT_DATES["g19"]]
    assert frame["source_url"].unique().to_list() == [RELEASE_URLS["g19"]]


def test_snapshot_validator_separates_current_files_from_dated_first_prints(
    tmp_path: Path,
) -> None:
    store = AppendOnlyParquetStore(tmp_path)
    ingestor = BoardSnapshotIngestor(store)
    g19 = ingestor.ingest(release_data("g19"), snapshot_date=SNAPSHOT_DATES["g19"])
    h8 = ingestor.ingest(release_data("h8"), snapshot_date=SNAPSHOT_DATES["h8"])

    report = validate_board_snapshots(store, SNAPSHOT_DATES)

    assert g19.row_count == 12
    assert h8.row_count == 6
    assert report.total_rows == 18
    assert report.snapshot_batches == 2
    assert report.rows_by_release == {"g19": 12, "h8": 6}
    assert report.checksums_by_release == {"g19": 1, "h8": 1}
    assert report.latest_snapshot_by_release == SNAPSHOT_DATES
    scoped = validate_board_snapshots(store, {"g19": SNAPSHOT_DATES["g19"]})
    assert scoped.total_rows == 12
    assert scoped.rows_by_release == {"g19": 12}

    dated = (Path(__file__).parents[1] / "fixtures" / "board" / "g19_20150108.html").read_bytes()
    BoardHistoryIngestor(store).ingest(
        BoardDatedReleaseData(
            release="g19",
            archive_date=date(2015, 1, 8),
            release_date=date(2015, 1, 8),
            release_at=release_timestamp("g19", date(2015, 1, 8)),
            source_url="https://www.federalreserve.gov/releases/g19/20150108/",
            checksum=hashlib.sha256(dated).hexdigest(),
            retrieved_at=datetime(2026, 8, 4, tzinfo=UTC),
            observations=parse_dated_release(dated, "g19", date(2015, 1, 8)),
        )
    )
    history = validate_board_history(store, {"g19": (date(2015, 1, 8),)})
    assert history.total_rows == 6


class FakeClient:
    def __init__(self, data: BoardReleaseData, manifests: list[tuple[date, ...]]) -> None:
        self.data = data
        self.manifests = manifests

    def discover_release_dates(self, _release: BoardRelease) -> tuple[date, ...]:
        return self.manifests.pop(0)

    def fetch_release(self, _release: BoardRelease) -> BoardReleaseData:
        return self.data


def test_fetch_checks_manifest_stability_and_requires_client(tmp_path: Path) -> None:
    stable = (SNAPSHOT_DATES["g19"],)
    client = FakeClient(release_data("g19"), [stable, stable])
    receipt = BoardSnapshotIngestor(  # type: ignore[arg-type]
        AppendOnlyParquetStore(tmp_path), client
    ).fetch_and_ingest("g19")
    assert receipt.snapshot_date == SNAPSHOT_DATES["g19"]

    with pytest.raises(BoardSnapshotError, match="requires a Board client"):
        BoardSnapshotIngestor(AppendOnlyParquetStore(tmp_path / "other")).fetch_and_ingest("g19")
    changing = FakeClient(
        release_data("g19"),
        [(date(2026, 6, 5),), (date(2026, 6, 5), SNAPSHOT_DATES["g19"])],
    )
    with pytest.raises(BoardSnapshotError, match="changed during"):
        BoardSnapshotIngestor(  # type: ignore[arg-type]
            AppendOnlyParquetStore(tmp_path / "changing"), changing
        ).fetch_and_ingest("g19")


def test_snapshot_rejects_missing_nonfinal_and_stale_values() -> None:
    data = release_data("g19")
    definition = definitions_for("g19")[0]
    series = data.series[definition.series_id]

    with pytest.raises(BoardSnapshotError, match="start must not follow"):
        current_snapshot_rows(
            data,
            snapshot_date=SNAPSHOT_DATES["g19"],
            start=date(2026, 8, 1),
            definitions=definitions_for("g19"),
        )
    with pytest.raises(BoardSnapshotError, match="non-final"):
        current_snapshot_rows(
            replace(
                data,
                series={
                    **data.series,
                    definition.series_id: replace(
                        series,
                        observations=(replace(series.observations[0], status="P"),),
                    ),
                },
            ),
            snapshot_date=SNAPSHOT_DATES["g19"],
            start=date(2026, 4, 1),
            definitions=definitions_for("g19"),
        )
    with pytest.raises(BoardSnapshotError, match="missing value"):
        current_snapshot_rows(
            replace(
                data,
                series={
                    **data.series,
                    definition.series_id: replace(
                        series,
                        observations=(replace(series.observations[0], value=None),),
                    ),
                },
            ),
            snapshot_date=SNAPSHOT_DATES["g19"],
            start=date(2026, 4, 1),
            definitions=definitions_for("g19"),
        )
    with pytest.raises(BoardSnapshotError, match="stale"):
        current_snapshot_rows(
            replace(data, retrieved_at="2027-01-01T00:00:00+00:00"),
            snapshot_date=date(2026, 12, 31),
            start=date(2026, 4, 1),
            definitions=definitions_for("g19"),
        )


@pytest.mark.parametrize(
    ("data_change", "definitions", "start", "message"),
    [
        ({"source_url": "https://example.com/current.zip"}, None, date(2026, 4, 1), "URL"),
        ({"checksum": "not-sha256"}, None, date(2026, 4, 1), "checksum"),
        ({"retrieved_at": "not-a-date"}, None, date(2026, 4, 1), "timestamp is invalid"),
        (
            {"retrieved_at": "2026-08-04T08:13:00"},
            None,
            date(2026, 4, 1),
            "timezone-aware",
        ),
        (
            {"retrieved_at": "2026-01-01T00:00:00+00:00"},
            None,
            date(2026, 4, 1),
            "predates",
        ),
        ({}, (), date(2026, 4, 1), "definitions are invalid"),
        ({}, None, date(2026, 6, 1), "has no"),
    ],
)
def test_snapshot_metadata_contracts_fail_closed(
    data_change: dict[str, object],
    definitions: tuple[BoardSeriesDefinition, ...] | None,
    start: date,
    message: str,
) -> None:
    data = replace(release_data("g19"), **data_change)
    with pytest.raises(BoardSnapshotError, match=message):
        current_snapshot_rows(
            data,
            snapshot_date=SNAPSHOT_DATES["g19"],
            start=start,
            definitions=definitions if definitions is not None else definitions_for("g19"),
        )


def test_same_checksum_with_conflicting_snapshot_fails_closed(tmp_path: Path) -> None:
    store = AppendOnlyParquetStore(tmp_path)
    data = release_data("g19")
    rows = current_snapshot_rows(
        data,
        snapshot_date=SNAPSHOT_DATES["g19"],
        start=board_snapshot.DEFAULT_START,
        definitions=definitions_for("g19"),
    )
    rows[0]["value"] = 999.0
    store.append("raw_observations", rows)

    with pytest.raises(BoardSnapshotError, match="does not match"):
        BoardSnapshotIngestor(store).ingest(data, snapshot_date=SNAPSHOT_DATES["g19"])


def test_cli_runs_both_snapshots_and_prints_validation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    class FakeTransport:
        def __enter__(self) -> FakeTransport:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    store = AppendOnlyParquetStore(tmp_path / "lake")
    fake_client = object()

    class CliIngestor:
        def __init__(self, _store: object, _client: object) -> None:
            pass

        def fetch_and_ingest(
            self, release: BoardRelease, *, start: date
        ) -> board_snapshot.BoardSnapshotReceipt:
            return BoardSnapshotIngestor(store).ingest(
                release_data(release), snapshot_date=SNAPSHOT_DATES[release], start=start
            )

    monkeypatch.setattr(board_snapshot, "HttpTransport", lambda **_kwargs: FakeTransport())
    monkeypatch.setattr(board_snapshot, "FederalReserveBoardClient", lambda _transport: fake_client)
    monkeypatch.setattr(board_snapshot, "AppendOnlyParquetStore", lambda _path: store)
    monkeypatch.setattr(board_snapshot, "BoardSnapshotIngestor", CliIngestor)

    result = board_snapshot.main(["--lake-root", str(tmp_path / "ignored")])

    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert len(output["snapshots"]) == 2
    assert output["validation"]["snapshot_batches"] == 2

    with pytest.raises(SystemExit):
        board_snapshot.build_parser().parse_args(["--start", "not-a-date"])


def test_snapshot_validator_requires_expected_and_stored_releases(tmp_path: Path) -> None:
    empty = AppendOnlyParquetStore(tmp_path)
    with pytest.raises(BoardSnapshotError, match="requires expected"):
        validate_board_snapshots(empty, {})
    with pytest.raises(BoardSnapshotError, match="history is empty"):
        validate_board_snapshots(empty, {"g19": SNAPSHOT_DATES["g19"]})

    BoardSnapshotIngestor(empty).ingest(release_data("g19"), snapshot_date=SNAPSHOT_DATES["g19"])
    with pytest.raises(BoardSnapshotError, match="coverage mismatch"):
        validate_board_snapshots(empty, SNAPSHOT_DATES)
