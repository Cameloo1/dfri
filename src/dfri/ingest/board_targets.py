"""Release-coherent G.19 first-print target ingestion and validation."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path
from typing import Final, cast

import polars as pl

from dfri.ingest.board import (
    BoardG19FirstPrintData,
    BoardRelease,
    FederalReserveBoardClient,
    release_timestamp,
)
from dfri.ingest.board_backfill import BoardBackfillRunner, ErrorMode
from dfri.ingest.http import HttpTransport
from dfri.ingest.registry import (
    BoardTargetDefinition,
    load_board_archive_exceptions,
    load_board_targets,
)
from dfri.lake.store import AppendOnlyParquetStore

DERIVED_SOURCE: Final = "DFRI_DERIVED_BOARD_FIRST_PRINT_V1"
BOARD_UNIT: Final = "Millions of U.S. Dollars"


class BoardTargetError(RuntimeError):
    """A first-print target page, stored batch, or validation contract failed."""


@dataclass(frozen=True)
class BoardTargetReceipt:
    release: BoardRelease
    release_date: date
    source_url: str
    checksum: str
    row_count: int
    already_present: bool
    batch_path: Path | None
    content_hash: str | None


@dataclass(frozen=True)
class BoardTargetValidation:
    archive_pages: int
    row_count: int
    target_series: int
    first_target_period: date
    last_target_period: date


class BoardTargetIngestor:
    """Persist two derived target rows per immutable dated G.19 release."""

    def __init__(
        self,
        store: AppendOnlyParquetStore,
        client: FederalReserveBoardClient | None = None,
        definitions: tuple[BoardTargetDefinition, ...] | None = None,
    ) -> None:
        self._store = store
        self._client = client
        self._definitions = definitions or load_board_targets()
        self._known_rows: dict[tuple[str, str], pl.DataFrame] = {}
        self._known_batches: dict[tuple[str, str], Path | None] = {}
        self._load_existing_index()

    def fetch_and_ingest(self, release: BoardRelease, release_date: date) -> BoardTargetReceipt:
        if release != "g19":
            raise BoardTargetError("Board first-print targets support G.19 only")
        if self._client is None:
            raise BoardTargetError("fetch_and_ingest requires a Board client")
        return self.ingest(self._client.fetch_g19_first_print(release_date))

    def ingest(self, data: BoardG19FirstPrintData) -> BoardTargetReceipt:
        rows = first_print_target_rows(data, self._definitions)
        identity = (data.source_url, data.checksum)
        existing = self._known_rows.get(identity)
        if existing is not None:
            _verify_existing_rows(existing, rows)
            batch_path = self._known_batches.get(identity)
            return BoardTargetReceipt(
                release="g19",
                release_date=data.release_date,
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
        return BoardTargetReceipt(
            release="g19",
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
            frame = pl.read_parquet(path).filter(pl.col("source") == DERIVED_SOURCE)
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


def first_print_target_rows(
    data: BoardG19FirstPrintData,
    definitions: tuple[BoardTargetDefinition, ...] | None = None,
) -> list[dict[str, object]]:
    """Validate and convert one release to strict Vintage-Guard-readable rows."""

    definitions = definitions or load_board_targets()
    _validate_release(data, definitions)
    flows_by_target = {flow.target_series_id: flow for flow in data.flows}
    return [
        {
            "source": definition.derived_source,
            "series_id": definition.target_series_id,
            "obs_period": flows_by_target[definition.target_series_id].target_period,
            "value": float(flows_by_target[definition.target_series_id].value),
            "unit": definition.units,
            "release_date": data.release_at.astimezone(UTC),
            "vintage_date": data.release_date,
            "ingested_at": data.retrieved_at.astimezone(UTC),
            "source_url": data.source_url,
            "checksum": data.checksum,
        }
        for definition in definitions
    ]


def validate_board_targets(
    store: AppendOnlyParquetStore,
    expected_manifest_dates: Sequence[date],
    definitions: tuple[BoardTargetDefinition, ...] | None = None,
) -> BoardTargetValidation:
    """Prove exact page coverage, identities, continuity, and release-time metadata."""

    definitions = definitions or load_board_targets()
    if not expected_manifest_dates:
        raise BoardTargetError("Board target validation requires expected G.19 dates")
    expected_pages = _expected_pages(expected_manifest_dates)
    frame = store.read_table("raw_observations").filter(pl.col("source") == DERIVED_SOURCE)
    if frame.is_empty():
        raise BoardTargetError("No Board first-print target rows are stored")
    actual_urls = set(frame["source_url"].unique().to_list())
    if actual_urls != set(expected_pages):
        missing = sorted(set(expected_pages) - actual_urls)
        extra = sorted(actual_urls - set(expected_pages))
        raise BoardTargetError(
            f"Board target page coverage mismatch; missing={missing}, extra={extra}"
        )

    expected_ids = {definition.target_series_id for definition in definitions}
    for source_url, (declared_date, _manifest_date) in expected_pages.items():
        page = frame.filter(pl.col("source_url") == source_url)
        if page.height != len(definitions):
            raise BoardTargetError(
                f"Board target page {source_url} has {page.height} rows; "
                f"expected {len(definitions)}"
            )
        if page["checksum"].n_unique() != 1:
            raise BoardTargetError(f"Board target page has multiple checksums: {source_url}")
        if set(page["series_id"].to_list()) != expected_ids:
            raise BoardTargetError(f"Board target series coverage changed: {source_url}")
        if page["obs_period"].n_unique() != 1:
            raise BoardTargetError(f"Board target page has multiple target periods: {source_url}")
        if page["vintage_date"].unique().to_list() != [declared_date]:
            raise BoardTargetError(f"Board target page declared date changed: {source_url}")
        expected_release_at = release_timestamp("g19", declared_date)
        if page["release_date"].unique().to_list() != [expected_release_at]:
            raise BoardTargetError(f"Board target page timestamp changed: {source_url}")

    for definition in definitions:
        series = frame.filter(pl.col("series_id") == definition.target_series_id).sort("obs_period")
        periods = series["obs_period"].to_list()
        if len(periods) != len(set(periods)):
            raise BoardTargetError(
                f"Board target {definition.target_series_id} has duplicate target months"
            )
        for previous, current in pairwise(periods):
            if _next_month_end(previous) != current:
                raise BoardTargetError(
                    f"Board target {definition.target_series_id} has a monthly gap: "
                    f"{previous} -> {current}"
                )

    target_periods = frame["obs_period"].unique().sort().to_list()
    return BoardTargetValidation(
        archive_pages=len(expected_pages),
        row_count=frame.height,
        target_series=len(expected_ids),
        first_target_period=target_periods[0],
        last_target_period=target_periods[-1],
    )


def _validate_release(
    data: BoardG19FirstPrintData, definitions: tuple[BoardTargetDefinition, ...]
) -> None:
    if not definitions:
        raise BoardTargetError("No Board first-print target definitions are registered")
    if any(definition.release != "g19" for definition in definitions):
        raise BoardTargetError("Board target registry contains a non-G.19 definition")
    expected_url = (
        f"https://www.federalreserve.gov/releases/g19/{data.archive_date.strftime('%Y%m%d')}/"
    )
    if data.source_url != expected_url:
        raise BoardTargetError(f"Board target URL mismatch: {data.source_url!r}")
    if re.fullmatch(r"[0-9a-f]{64}", data.checksum) is None:
        raise BoardTargetError("Board target checksum is not lowercase SHA-256")
    if data.release_at != release_timestamp("g19", data.release_date):
        raise BoardTargetError("Board target release timestamp changed")
    if data.retrieved_at.tzinfo is None or data.retrieved_at.utcoffset() is None:
        raise BoardTargetError("Board target retrieval timestamp must be timezone-aware")
    flows = {flow.target_series_id: flow for flow in data.flows}
    expected_ids = {definition.target_series_id for definition in definitions}
    if set(flows) != expected_ids or len(flows) != len(data.flows):
        raise BoardTargetError("Board target flow identities do not match the registry")
    for definition in definitions:
        flow = flows[definition.target_series_id]
        if flow.level_series_id != definition.level_series_id:
            raise BoardTargetError(
                f"Board target level-series mismatch: {definition.target_series_id}"
            )
        if _next_month_end(flow.previous_period) != flow.target_period:
            raise BoardTargetError("Board target periods are not consecutive month ends")
        if flow.target_period >= data.release_date:
            raise BoardTargetError("Board target period is not before its release date")
        if not all(
            value.is_finite() for value in (flow.target_level, flow.previous_level, flow.value)
        ):
            raise BoardTargetError("Board target values must be finite")
        if flow.value != flow.target_level - flow.previous_level:
            raise BoardTargetError("Board target flow does not equal the release-coherent change")


def _expected_pages(expected_manifest_dates: Sequence[date]) -> dict[str, tuple[date, date]]:
    exceptions = {
        exception.manifest_date: exception
        for exception in load_board_archive_exceptions()
        if exception.release == "g19"
    }
    pages: dict[str, tuple[date, date]] = {}
    for manifest_date in expected_manifest_dates:
        exception = exceptions.get(manifest_date)
        archive_date = exception.archive_date if exception else manifest_date
        declared_date = exception.declared_release_date if exception else manifest_date
        url = f"https://www.federalreserve.gov/releases/g19/{archive_date.strftime('%Y%m%d')}/"
        if url in pages:
            raise BoardTargetError(f"Board target expected URL is duplicated: {url}")
        pages[url] = (declared_date, manifest_date)
    return pages


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
        raise BoardTargetError(
            "Stored Board target batch with the same source checksum does not match the release"
        )


def _row_identity(row: Mapping[str, object], columns: list[str]) -> tuple[object, ...]:
    return tuple(row[column] for column in columns)


def _next_month_end(period: date) -> date:
    year = period.year + int(period.month == 12)
    month = 1 if period.month == 12 else period.month + 1
    following_year = year + int(month == 12)
    following_month = 1 if month == 12 else month + 1
    return date(following_year, following_month, 1) - (date.resolution)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=_parse_date, default=date(2015, 1, 1))
    parser.add_argument("--end", type=_parse_date, default=datetime.now(UTC).date())
    parser.add_argument("--lake-root", type=Path, default=Path(".local/lake/raw"))
    parser.add_argument("--state", type=Path, default=Path(".local/state/board-targets-v1.json"))
    parser.add_argument(
        "--event-log", type=Path, default=Path(".local/evidence/board-targets-v1.jsonl")
    )
    parser.add_argument("--max-items", type=int)
    parser.add_argument("--on-error", choices=("stop", "continue"), default="stop")
    parser.add_argument("--recheck-complete", action="store_true")
    parser.add_argument("--skip-validation", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with HttpTransport(min_interval_seconds=0.5) as transport:
        client = FederalReserveBoardClient(transport)
        store = AppendOnlyParquetStore(args.lake_root)
        summary = BoardBackfillRunner(
            client=client,
            ingestor=BoardTargetIngestor(store, client),
            state_path=args.state,
            event_log_path=args.event_log,
        ).run(
            releases=("g19",),
            start=args.start,
            end=args.end,
            max_items=args.max_items,
            error_mode=cast(ErrorMode, args.on_error),
            recheck_complete=args.recheck_complete,
        )
        output: dict[str, object] = {"backfill": asdict(summary)}
        if args.max_items is None and summary.failed == 0 and not args.skip_validation:
            expected_dates = tuple(
                item
                for item in client.discover_release_dates("g19")
                if args.start <= item <= args.end
            )
            output["validation"] = asdict(validate_board_targets(store, expected_dates))
    print(json.dumps(output, sort_keys=True, default=str))
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
