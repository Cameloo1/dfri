"""Validated, idempotent persistence for dated Federal Reserve Board releases."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date
from math import isfinite
from pathlib import Path
from typing import Final

import polars as pl

from dfri.ingest.board import (
    BoardDatedReleaseData,
    BoardRelease,
    FederalReserveBoardClient,
    release_timestamp,
)
from dfri.ingest.registry import load_board_archive_exceptions
from dfri.lake.store import AppendOnlyParquetStore

BOARD_SOURCE: Final = "FEDERAL_RESERVE_BOARD"
BOARD_UNIT: Final = "Millions of U.S. Dollars"
EXPECTED_SERIES: Final[dict[BoardRelease, frozenset[str]]] = {
    "g19": frozenset(
        {
            "DTCTL.M",
            "DTCTLR.M",
            "DTCTLN.M",
            "DTCTL_N.M",
            "DTCTLR_N.M",
            "DTCTLN_N.M",
        }
    ),
    "h8": frozenset({"B1029NCBA", "B1247NCBA", "B3248NCBA"}),
}
EXPECTED_OBSERVATIONS: Final[dict[BoardRelease, int]] = {"g19": 6, "h8": 12}


class BoardHistoryError(RuntimeError):
    """A Board history batch failed persistence or point-in-time contracts."""


@dataclass(frozen=True)
class BoardHistoryReceipt:
    release: BoardRelease
    release_date: date
    source_url: str
    checksum: str
    row_count: int
    already_present: bool
    batch_path: Path | None
    content_hash: str | None


@dataclass(frozen=True)
class BoardHistoryValidation:
    total_rows: int
    release_pages: int
    pages_by_release: dict[str, int]
    raw_rows_by_series: dict[str, int]
    first_print_rows_by_series: dict[str, int]
    earliest_release: date
    latest_release: date


class BoardHistoryIngestor:
    """Fetch and append one immutable dated release at a time."""

    def __init__(
        self, store: AppendOnlyParquetStore, client: FederalReserveBoardClient | None = None
    ) -> None:
        self._store = store
        self._client = client
        self._known_rows: dict[tuple[str, str], pl.DataFrame] = {}
        self._known_batches: dict[tuple[str, str], Path | None] = {}
        self._load_existing_index()

    def fetch_and_ingest(self, release: BoardRelease, release_date: date) -> BoardHistoryReceipt:
        if self._client is None:
            raise BoardHistoryError("fetch_and_ingest requires a Board client")
        return self.ingest(self._client.fetch_dated_release(release, release_date))

    def ingest(self, data: BoardDatedReleaseData) -> BoardHistoryReceipt:
        rows = dated_release_rows(data)
        identity = (data.source_url, data.checksum)
        matching = self._known_rows.get(identity)
        if matching is not None:
            _verify_existing_rows(matching, rows)
            batch_path = self._known_batches.get(identity)
            content_hash = (
                batch_path.stem.removeprefix("batch-") if batch_path is not None else None
            )
            return BoardHistoryReceipt(
                release=data.release,
                release_date=data.release_date,
                source_url=data.source_url,
                checksum=data.checksum,
                row_count=len(rows),
                already_present=True,
                batch_path=batch_path,
                content_hash=content_hash,
            )

        write = self._store.append("raw_observations", rows)
        self._known_rows[identity] = pl.read_parquet(write.path)
        self._known_batches[identity] = write.path
        return BoardHistoryReceipt(
            release=data.release,
            release_date=data.release_date,
            source_url=data.source_url,
            checksum=data.checksum,
            row_count=write.row_count,
            already_present=write.already_present,
            batch_path=write.path,
            content_hash=write.content_hash,
        )

    def _load_existing_index(self) -> None:
        paths = sorted((self._store.root / "raw_observations").glob("batch-*.parquet"))
        for path in paths:
            frame = pl.read_parquet(path).filter(pl.col("source") == BOARD_SOURCE)
            if frame.is_empty():
                continue
            identities = frame.select(["source_url", "checksum"]).unique()
            for source_url, checksum in identities.iter_rows(named=False):
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


def dated_release_rows(data: BoardDatedReleaseData) -> list[dict[str, object]]:
    """Validate one dated release and convert it to strict raw-observation rows."""

    _validate_release(data)
    release_at = data.release_at.astimezone(UTC)
    ingested_at = data.retrieved_at.astimezone(UTC)
    return [
        {
            "source": BOARD_SOURCE,
            "series_id": observation.series_id,
            "obs_period": observation.period,
            "value": float(observation.value),
            "unit": BOARD_UNIT,
            "release_date": release_at,
            "vintage_date": data.release_date,
            "ingested_at": ingested_at,
            "source_url": data.source_url,
            "checksum": data.checksum,
        }
        for observation in data.observations
    ]


def select_first_print(frame: pl.DataFrame) -> pl.DataFrame:
    """Select the earliest Board release per observation from VintageGuard output."""

    required = {
        "source",
        "series_id",
        "obs_period",
        "value",
        "release_date",
        "vintage_date",
        "ingested_at",
        "source_url",
        "checksum",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise BoardHistoryError(f"first-print input is missing columns: {missing}")

    board = frame.filter(
        (pl.col("source") == BOARD_SOURCE)
        & pl.col("source_url").str.contains(
            r"^https://www\.federalreserve\.gov/releases/(?:g19|h8)/\d{8}/$"
        )
    )
    if board.is_empty():
        return board
    ambiguous = (
        board.group_by(["series_id", "obs_period", "release_date"])
        .agg(pl.col("value").n_unique().alias("value_count"))
        .filter(pl.col("value_count") > 1)
    )
    if not ambiguous.is_empty():
        raise BoardHistoryError("A Board first-print release has conflicting stored values")

    return (
        board.sort(["series_id", "obs_period", "release_date", "ingested_at", "checksum"])
        .unique(subset=["series_id", "obs_period"], keep="first", maintain_order=True)
        .sort(["series_id", "obs_period"])
    )


def validate_board_history(
    store: AppendOnlyParquetStore,
    expected_dates: Mapping[BoardRelease, Sequence[date]],
) -> BoardHistoryValidation:
    """Prove full manifest coverage and row-level Board history invariants."""

    frame = store.read_table("raw_observations").filter(
        (pl.col("source") == BOARD_SOURCE)
        & pl.col("source_url").str.contains(
            r"^https://www\.federalreserve\.gov/releases/(?:g19|h8)/\d{8}/$"
        )
    )
    if frame.is_empty():
        raise BoardHistoryError("Board history is empty")
    if frame.select(pl.col("checksum").str.contains(r"^[0-9a-f]{64}$").all()).item() is not True:
        raise BoardHistoryError("Board history contains an invalid checksum")
    if frame.select((pl.col("unit") == BOARD_UNIT).all()).item() is not True:
        raise BoardHistoryError("Board history contains an invalid unit")
    if (
        frame.select((pl.col("vintage_date") == pl.col("release_date").dt.date()).all()).item()
        is not True
    ):
        raise BoardHistoryError("Board release and vintage dates disagree")
    if any(not isfinite(value) or value < 0 for value in frame["value"].to_list()):
        raise BoardHistoryError("Board history contains an invalid value")

    identities = frame.select(["source_url", "checksum", "series_id", "obs_period", "release_date"])
    if identities.is_duplicated().any():
        raise BoardHistoryError("Board history contains duplicate source observations")

    expected_pages = _expected_archive_pages(expected_dates)
    actual_urls = set(frame["source_url"].unique().to_list())
    if actual_urls != set(expected_pages):
        missing = sorted(set(expected_pages) - actual_urls)
        extra = sorted(actual_urls - set(expected_pages))
        raise BoardHistoryError(
            f"Board history page coverage mismatch; missing={missing}, extra={extra}"
        )

    pages_by_release: Counter[str] = Counter()
    for source_url, (
        release,
        _manifest_date,
        _archive_date,
        expected_declared,
    ) in expected_pages.items():
        page = frame.filter(pl.col("source_url") == source_url)
        if page["checksum"].n_unique() != 1:
            raise BoardHistoryError(f"Board page has multiple checksums: {source_url}")
        expected_count = EXPECTED_OBSERVATIONS[release]
        if page.height != expected_count:
            raise BoardHistoryError(
                f"Board page {source_url} has {page.height} rows; expected {expected_count}"
            )
        expected_series_counts = dict.fromkeys(
            EXPECTED_SERIES[release], 1 if release == "g19" else 4
        )
        actual_series_counts = Counter(page["series_id"].to_list())
        if actual_series_counts != expected_series_counts:
            raise BoardHistoryError(f"Board page series coverage changed: {source_url}")
        release_dates = page["vintage_date"].unique().to_list()
        if len(release_dates) != 1:
            raise BoardHistoryError(f"Board page has multiple release dates: {source_url}")
        declared_date = release_dates[0]
        if not isinstance(declared_date, date):
            raise BoardHistoryError(f"Board page has invalid release date: {source_url}")
        if declared_date != expected_declared:
            raise BoardHistoryError(f"Board page declared date changed: {source_url}")
        release_at_values = page["release_date"].unique().to_list()
        if release_at_values != [release_timestamp(release, declared_date)]:
            raise BoardHistoryError(f"Board page timestamp changed: {source_url}")
        if release == "h8" and any(item.weekday() != 2 for item in page["obs_period"]):
            raise BoardHistoryError(f"H.8 page contains a non-Wednesday period: {source_url}")
        pages_by_release[release] += 1

    raw_rows_by_series = {
        str(series_id): int(count)
        for series_id, count in frame.group_by("series_id").len().iter_rows(named=False)
    }
    first_print_rows_by_series = {
        series_id: select_first_print(frame.filter(pl.col("series_id") == series_id)).height
        for series_id in sorted(raw_rows_by_series)
    }
    releases = frame["vintage_date"]
    earliest = releases.min()
    latest = releases.max()
    if not isinstance(earliest, date) or not isinstance(latest, date):
        raise BoardHistoryError("Board history release range is invalid")
    return BoardHistoryValidation(
        total_rows=frame.height,
        release_pages=len(actual_urls),
        pages_by_release=dict(sorted(pages_by_release.items())),
        raw_rows_by_series=dict(sorted(raw_rows_by_series.items())),
        first_print_rows_by_series=first_print_rows_by_series,
        earliest_release=earliest,
        latest_release=latest,
    )


def _expected_archive_pages(
    expected_dates: Mapping[BoardRelease, Sequence[date]],
) -> dict[str, tuple[BoardRelease, date, date, date]]:
    exceptions = {
        (item.release, item.manifest_date): item for item in load_board_archive_exceptions()
    }
    pages: dict[str, tuple[BoardRelease, date, date, date]] = {}
    for release, dates in expected_dates.items():
        for manifest_date in dates:
            exception = exceptions.get((release, manifest_date))
            archive_date = exception.archive_date if exception else manifest_date
            declared_date = exception.declared_release_date if exception else manifest_date
            url = (
                "https://www.federalreserve.gov/releases/"
                f"{release}/{archive_date.strftime('%Y%m%d')}/"
            )
            if url in pages:
                raise BoardHistoryError(f"Board manifest resolves to a duplicate page: {url}")
            pages[url] = (release, manifest_date, archive_date, declared_date)
    if not pages:
        raise BoardHistoryError("Board validation requires expected release dates")
    return pages


def _validate_release(data: BoardDatedReleaseData) -> None:
    expected_url = (
        "https://www.federalreserve.gov/releases/"
        f"{data.release}/{data.archive_date.strftime('%Y%m%d')}/"
    )
    if data.source_url != expected_url:
        raise BoardHistoryError(f"Board dated-release URL mismatch: {data.source_url!r}")
    if re.fullmatch(r"[0-9a-f]{64}", data.checksum) is None:
        raise BoardHistoryError("Board dated-release checksum is not lowercase SHA-256")
    if data.release_at.tzinfo is None or data.release_at.utcoffset() is None:
        raise BoardHistoryError("Board release timestamp must be timezone-aware")
    if data.retrieved_at.tzinfo is None or data.retrieved_at.utcoffset() is None:
        raise BoardHistoryError("Board retrieval timestamp must be timezone-aware")
    if data.release_at.astimezone(UTC) != release_timestamp(data.release, data.release_date):
        raise BoardHistoryError("Board release timestamp does not match its pinned schedule")
    if data.retrieved_at.astimezone(UTC) < data.release_at.astimezone(UTC):
        raise BoardHistoryError("Board retrieval timestamp predates the release")
    if len(data.observations) != EXPECTED_OBSERVATIONS[data.release]:
        raise BoardHistoryError(
            f"{data.release.upper()} release has {len(data.observations)} observations; "
            f"expected {EXPECTED_OBSERVATIONS[data.release]}"
        )

    seen: set[tuple[str, date]] = set()
    actual_series: set[str] = set()
    expected_status = "p" if data.release == "g19" else "first_print"
    for observation in data.observations:
        actual_series.add(observation.series_id)
        identity = (observation.series_id, observation.period)
        if identity in seen:
            raise BoardHistoryError(f"Duplicate Board observation: {identity}")
        seen.add(identity)
        if observation.series_id not in EXPECTED_SERIES[data.release]:
            raise BoardHistoryError(
                f"Unexpected {data.release.upper()} series: {observation.series_id}"
            )
        if observation.period > data.release_date:
            raise BoardHistoryError(f"Future Board observation period: {identity}")
        if data.release == "h8" and observation.period.weekday() != 2:
            raise BoardHistoryError(f"H.8 observation is not a Wednesday: {identity}")
        if not observation.value.is_finite() or observation.value < 0:
            raise BoardHistoryError(f"Invalid Board observation value: {identity}")
        if observation.status != expected_status:
            raise BoardHistoryError(
                f"Unexpected {data.release.upper()} observation status: {observation.status!r}"
            )
    if actual_series != EXPECTED_SERIES[data.release]:
        missing = sorted(EXPECTED_SERIES[data.release] - actual_series)
        raise BoardHistoryError(f"{data.release.upper()} release is missing series: {missing}")


def _verify_existing_rows(existing: pl.DataFrame, expected_rows: list[dict[str, object]]) -> None:
    identity_columns = [
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
    expected = {_row_identity(row, identity_columns) for row in expected_rows}
    actual = {tuple(row) for row in existing.select(identity_columns).iter_rows(named=False)}
    if len(existing) != len(expected_rows) or actual != expected:
        raise BoardHistoryError(
            "Stored Board batch with the same source checksum does not match the parsed release"
        )


def _row_identity(row: Mapping[str, object], columns: list[str]) -> tuple[object, ...]:
    return tuple(row[column] for column in columns)
