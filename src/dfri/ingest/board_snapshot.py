"""Ingest live Federal Reserve Board DDP files as explicitly revised snapshots."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from math import isfinite
from pathlib import Path
from typing import cast

import polars as pl

from dfri.ingest.board import (
    RELEASE_URLS,
    BoardRelease,
    BoardReleaseData,
    FederalReserveBoardClient,
    release_timestamp,
    verify_series,
)
from dfri.ingest.board_history import BOARD_SOURCE, BOARD_UNIT
from dfri.ingest.http import HttpTransport
from dfri.ingest.registry import (
    BoardSeriesDefinition,
    load_board_archive_exceptions,
    load_board_series,
)
from dfri.lake.store import AppendOnlyParquetStore

DEFAULT_START = date(2015, 1, 1)


class BoardSnapshotError(RuntimeError):
    """A live Board snapshot failed its metadata, freshness, or storage contract."""


@dataclass(frozen=True)
class BoardSnapshotReceipt:
    release: BoardRelease
    snapshot_date: date
    source_url: str
    checksum: str
    row_count: int
    already_present: bool
    batch_path: Path | None
    content_hash: str | None


@dataclass(frozen=True)
class BoardSnapshotValidation:
    total_rows: int
    snapshot_batches: int
    rows_by_release: dict[str, int]
    checksums_by_release: dict[str, int]
    latest_snapshot_by_release: dict[str, date]


class BoardSnapshotIngestor:
    """Fetch and append current DDP files without treating revisions as first prints."""

    def __init__(
        self,
        store: AppendOnlyParquetStore,
        client: FederalReserveBoardClient | None = None,
        definitions: Sequence[BoardSeriesDefinition] | None = None,
    ) -> None:
        self._store = store
        self._client = client
        self._definitions = tuple(definitions or load_board_series())
        self._known_rows: dict[tuple[str, str], pl.DataFrame] = {}
        self._known_batches: dict[tuple[str, str], Path | None] = {}
        self._load_existing_index()

    def fetch_and_ingest(
        self, release: BoardRelease, *, start: date = DEFAULT_START
    ) -> BoardSnapshotReceipt:
        if self._client is None:
            raise BoardSnapshotError("fetch_and_ingest requires a Board client")
        manifest_before = self._client.discover_release_dates(release)
        data = self._client.fetch_release(release)
        manifest_after = self._client.discover_release_dates(release)
        if manifest_before != manifest_after:
            raise BoardSnapshotError("Board release manifest changed during snapshot fetch")
        if not manifest_after:
            raise BoardSnapshotError(f"Board {release.upper()} manifest is empty")
        snapshot_date = _declared_release_date(release, manifest_after[-1])
        return self.ingest(data, snapshot_date=snapshot_date, start=start)

    def ingest(
        self,
        data: BoardReleaseData,
        *,
        snapshot_date: date,
        start: date = DEFAULT_START,
    ) -> BoardSnapshotReceipt:
        definitions = tuple(
            definition for definition in self._definitions if definition.release == data.release
        )
        rows = current_snapshot_rows(
            data,
            snapshot_date=snapshot_date,
            start=start,
            definitions=definitions,
        )
        identity = (data.source_url, data.checksum)
        matching = self._known_rows.get(identity)
        if matching is not None:
            _verify_existing_rows(matching, rows)
            batch_path = self._known_batches.get(identity)
            return BoardSnapshotReceipt(
                release=data.release,
                snapshot_date=snapshot_date,
                source_url=data.source_url,
                checksum=data.checksum,
                row_count=len(rows),
                already_present=True,
                batch_path=batch_path,
                content_hash=(
                    batch_path.stem.removeprefix("batch-") if batch_path is not None else None
                ),
            )

        write = self._store.append("raw_observations", rows)
        self._known_rows[identity] = pl.read_parquet(write.path)
        self._known_batches[identity] = write.path
        return BoardSnapshotReceipt(
            release=data.release,
            snapshot_date=snapshot_date,
            source_url=data.source_url,
            checksum=data.checksum,
            row_count=write.row_count,
            already_present=write.already_present,
            batch_path=write.path,
            content_hash=write.content_hash,
        )

    def _load_existing_index(self) -> None:
        current_urls = set(RELEASE_URLS.values())
        paths = sorted((self._store.root / "raw_observations").glob("batch-*.parquet"))
        for path in paths:
            frame = pl.read_parquet(path).filter(pl.col("source_url").is_in(current_urls))
            if frame.is_empty():
                continue
            identities = frame.select(["source_url", "checksum"]).unique()
            for source_url, checksum in identities.iter_rows():
                identity = (str(source_url), str(checksum))
                matching = frame.filter(
                    (pl.col("source_url") == source_url) & (pl.col("checksum") == checksum)
                )
                prior = self._known_rows.get(identity)
                if prior is None:
                    self._known_rows[identity] = matching
                    self._known_batches[identity] = path
                else:
                    self._known_rows[identity] = pl.concat([prior, matching], how="vertical")
                    self._known_batches[identity] = None


def current_snapshot_rows(
    data: BoardReleaseData,
    *,
    snapshot_date: date,
    start: date,
    definitions: Sequence[BoardSeriesDefinition],
) -> list[dict[str, object]]:
    """Validate a current DDP archive and label every row as one revised snapshot."""

    if start > snapshot_date:
        raise BoardSnapshotError("Board snapshot start must not follow the snapshot date")
    if data.source_url != RELEASE_URLS[data.release]:
        raise BoardSnapshotError(f"Board current-snapshot URL mismatch: {data.source_url!r}")
    if re.fullmatch(r"[0-9a-f]{64}", data.checksum) is None:
        raise BoardSnapshotError("Board current-snapshot checksum is not lowercase SHA-256")
    try:
        retrieved_at = datetime.fromisoformat(data.retrieved_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BoardSnapshotError("Board snapshot retrieval timestamp is invalid") from exc
    if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
        raise BoardSnapshotError("Board snapshot retrieval timestamp must be timezone-aware")
    release_at = release_timestamp(data.release, snapshot_date)
    if retrieved_at.astimezone(UTC) < release_at:
        raise BoardSnapshotError("Board snapshot retrieval predates its release")

    applicable = tuple(definitions)
    if not applicable or any(item.release != data.release for item in applicable):
        raise BoardSnapshotError(f"Board {data.release.upper()} snapshot definitions are invalid")
    verify_series(data, applicable)

    rows: list[dict[str, object]] = []
    latest_periods: set[date] = set()
    for definition in applicable:
        series = data.series[definition.series_id]
        selected = tuple(item for item in series.observations if item.period >= start)
        if not selected:
            raise BoardSnapshotError(
                f"Board snapshot has no {definition.series_id} observations on or after {start}"
            )
        latest_periods.add(selected[-1].period)
        seen_periods: set[date] = set()
        for observation in selected:
            if observation.period in seen_periods:
                raise BoardSnapshotError(
                    f"Board snapshot contains duplicate period: {definition.series_id} "
                    f"{observation.period}"
                )
            seen_periods.add(observation.period)
            if observation.period > snapshot_date:
                raise BoardSnapshotError("Board snapshot contains a future observation")
            if data.release == "h8" and observation.period.weekday() != 2:
                raise BoardSnapshotError("Board H.8 snapshot contains a non-Wednesday period")
            if observation.status != "A":
                raise BoardSnapshotError(
                    f"Board snapshot contains non-final status {observation.status!r}"
                )
            if observation.value is None:
                raise BoardSnapshotError("Board snapshot contains an unlabeled missing value")
            value = float(observation.value)
            if not isfinite(value) or value < 0:
                raise BoardSnapshotError("Board snapshot contains an invalid value")
            rows.append(
                {
                    "source": BOARD_SOURCE,
                    "series_id": definition.series_id,
                    "obs_period": observation.period,
                    "value": value,
                    "unit": BOARD_UNIT,
                    "release_date": release_at,
                    "vintage_date": snapshot_date,
                    "ingested_at": retrieved_at.astimezone(UTC),
                    "source_url": data.source_url,
                    "checksum": data.checksum,
                }
            )
    if len(latest_periods) != 1:
        raise BoardSnapshotError("Board snapshot registered series have different latest periods")
    _validate_latest_period(data.release, snapshot_date, next(iter(latest_periods)))
    return rows


def validate_board_snapshots(
    store: AppendOnlyParquetStore,
    expected_latest: Mapping[BoardRelease, date],
    *,
    start: date = DEFAULT_START,
) -> BoardSnapshotValidation:
    """Validate all stored current DDP snapshots separately from dated first prints."""

    if not expected_latest:
        raise BoardSnapshotError("Board snapshot validation requires expected releases")
    url_to_release = {url: release for release, url in RELEASE_URLS.items()}
    expected_urls = {RELEASE_URLS[release] for release in expected_latest}
    frame = store.read_table("raw_observations").filter(pl.col("source_url").is_in(expected_urls))
    if frame.is_empty():
        raise BoardSnapshotError("Board current-snapshot history is empty")
    if set(frame["source_url"].unique().to_list()) != expected_urls:
        raise BoardSnapshotError("Board current-snapshot release coverage mismatch")
    if frame.select((pl.col("source") == BOARD_SOURCE).all()).item() is not True:
        raise BoardSnapshotError("Board snapshot contains an invalid source")
    if frame.select((pl.col("unit") == BOARD_UNIT).all()).item() is not True:
        raise BoardSnapshotError("Board snapshot contains an invalid unit")
    if frame.select(pl.col("checksum").str.contains(r"^[0-9a-f]{64}$").all()).item() is not True:
        raise BoardSnapshotError("Board snapshot contains an invalid checksum")
    if frame.select((pl.col("obs_period") >= start).all()).item() is not True:
        raise BoardSnapshotError("Board snapshot contains an observation before its start")
    if frame.select((pl.col("ingested_at") >= pl.col("release_date")).all()).item() is not True:
        raise BoardSnapshotError("Board snapshot was ingested before release")
    if any(not isfinite(value) or value < 0 for value in frame["value"].to_list()):
        raise BoardSnapshotError("Board snapshot contains an invalid value")
    identities = frame.select(["source_url", "checksum", "series_id", "obs_period", "release_date"])
    if identities.is_duplicated().any():
        raise BoardSnapshotError("Board snapshots contain duplicate source observations")

    definitions = load_board_series()
    expected_series = {
        release: {item.series_id for item in definitions if item.release == release}
        for release in expected_latest
    }
    rows_by_release: Counter[str] = Counter()
    checksums_by_release: Counter[str] = Counter()
    latest_by_release: dict[str, date] = {}
    batch_count = 0
    for (source_url, _checksum), snapshot in frame.group_by(
        ["source_url", "checksum"], maintain_order=True
    ):
        release = url_to_release[str(source_url)]
        if release not in expected_latest:
            raise BoardSnapshotError(f"Unexpected Board snapshot release: {release}")
        vintage_dates = snapshot["vintage_date"].unique().to_list()
        if len(vintage_dates) != 1 or not isinstance(vintage_dates[0], date):
            raise BoardSnapshotError("Board snapshot batch has invalid vintage dates")
        snapshot_date = vintage_dates[0]
        expected_timestamp = release_timestamp(release, snapshot_date)
        if snapshot["release_date"].unique().to_list() != [expected_timestamp]:
            raise BoardSnapshotError("Board snapshot batch has an invalid release timestamp")
        if set(snapshot["series_id"].unique().to_list()) != expected_series[release]:
            raise BoardSnapshotError("Board snapshot batch has incomplete series coverage")
        for series_id in expected_series[release]:
            series = snapshot.filter(pl.col("series_id") == series_id)
            if series["obs_period"].n_unique() != series.height:
                raise BoardSnapshotError("Board snapshot series contains duplicate periods")
        latest_periods = set(
            snapshot.group_by("series_id").agg(pl.col("obs_period").max())["obs_period"].to_list()
        )
        if len(latest_periods) != 1:
            raise BoardSnapshotError("Board snapshot series have different latest periods")
        _validate_latest_period(release, snapshot_date, next(iter(latest_periods)))
        rows_by_release[release] += snapshot.height
        checksums_by_release[release] += 1
        latest_by_release[release] = max(
            latest_by_release.get(release, snapshot_date), snapshot_date
        )
        batch_count += 1

    actual_latest = {cast(BoardRelease, key): value for key, value in latest_by_release.items()}
    if actual_latest != dict(expected_latest):
        raise BoardSnapshotError(
            f"Board latest-snapshot dates disagree: {actual_latest!r} != {dict(expected_latest)!r}"
        )
    return BoardSnapshotValidation(
        total_rows=frame.height,
        snapshot_batches=batch_count,
        rows_by_release=dict(sorted(rows_by_release.items())),
        checksums_by_release=dict(sorted(checksums_by_release.items())),
        latest_snapshot_by_release=dict(sorted(latest_by_release.items())),
    )


def _declared_release_date(release: BoardRelease, manifest_date: date) -> date:
    for exception in load_board_archive_exceptions():
        if exception.release == release and exception.manifest_date == manifest_date:
            return exception.declared_release_date
    return manifest_date


def _validate_latest_period(
    release: BoardRelease, snapshot_date: date, latest_period: date
) -> None:
    age_days = (snapshot_date - latest_period).days
    if release == "g19" and not 28 <= age_days <= 95:
        raise BoardSnapshotError("Board G.19 current snapshot is stale or implausibly current")
    if release == "h8" and not 2 <= age_days <= 16:
        raise BoardSnapshotError("Board H.8 current snapshot is stale or implausibly current")


def _verify_existing_rows(
    existing: pl.DataFrame, expected_rows: Sequence[Mapping[str, object]]
) -> None:
    columns = [
        "source",
        "series_id",
        "obs_period",
        "value",
        "unit",
        "release_date",
        "vintage_date",
        "source_url",
        "checksum",
    ]
    expected = {tuple(row[column] for column in columns) for row in expected_rows}
    actual = {tuple(row) for row in existing.select(columns).iter_rows()}
    if len(existing) != len(expected_rows) or actual != expected:
        raise BoardSnapshotError(
            "Stored Board snapshot with the same source checksum does not match the live parse"
        )


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", choices=("g19", "h8", "all"), default="all")
    parser.add_argument("--start", type=_parse_date, default=DEFAULT_START)
    parser.add_argument("--lake-root", type=Path, default=Path(".local/lake/raw"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    releases: tuple[BoardRelease, ...] = (
        ("g19", "h8") if args.release == "all" else (cast(BoardRelease, args.release),)
    )
    store = AppendOnlyParquetStore(args.lake_root)
    with HttpTransport(min_interval_seconds=0.5) as transport:
        client = FederalReserveBoardClient(transport)
        ingestor = BoardSnapshotIngestor(store, client)
        receipts = [ingestor.fetch_and_ingest(release, start=args.start) for release in releases]
        expected_latest = {receipt.release: receipt.snapshot_date for receipt in receipts}
        validation = validate_board_snapshots(store, expected_latest, start=args.start)
    print(
        json.dumps(
            {
                "snapshots": [asdict(receipt) for receipt in receipts],
                "validation": asdict(validation),
            },
            sort_keys=True,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
