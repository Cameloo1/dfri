"""Ingest BEA and Census histories as conservative retrieval-time snapshots."""

from __future__ import annotations

import argparse
import json
import os
import re
from calendar import monthrange
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from math import isfinite
from pathlib import Path
from typing import Literal, cast

import polars as pl

from dfri.ingest.bea import (
    BEA_ENDPOINT,
    BeaClient,
    BeaTableData,
    parse_bea_value,
    verify_bea_row,
)
from dfri.ingest.census import (
    CENSUS_BASE,
    CensusClient,
    CensusDatasetData,
    parse_census_value,
)
from dfri.ingest.http import HttpTransport
from dfri.ingest.registry import ContextSeriesDefinition, load_context_series
from dfri.lake.store import AppendOnlyParquetStore

DEFAULT_START = date(2015, 1, 1)
ContextSource = Literal["bea", "census"]
LAKE_SOURCE = {"bea": "BEA", "census": "CENSUS"}


class ContextHistoryError(RuntimeError):
    """A BEA or Census history failed source, value, or snapshot contracts."""


@dataclass(frozen=True)
class ContextHistoryReceipt:
    source: ContextSource
    source_url: str
    checksum: str
    snapshot_at: datetime
    series_count: int
    row_count: int
    already_present: bool
    batch_path: Path | None
    content_hash: str | None


@dataclass(frozen=True)
class ContextHistoryValidation:
    total_rows: int
    snapshot_batches: int
    rows_by_source: dict[str, int]
    rows_by_series: dict[str, int]
    latest_period_by_series: dict[str, date]


class ContextHistoryIngestor:
    """Append source-response snapshots while preserving conservative availability times."""

    def __init__(
        self,
        store: AppendOnlyParquetStore,
        definitions: Sequence[ContextSeriesDefinition] | None = None,
    ) -> None:
        self._store = store
        self._definitions = tuple(definitions or load_context_series())
        self._known_rows: dict[tuple[str, str], pl.DataFrame] = {}
        self._known_batches: dict[tuple[str, str], Path | None] = {}
        self._load_existing_index()

    def fetch_bea(
        self, client: BeaClient, *, start: date = DEFAULT_START
    ) -> tuple[ContextHistoryReceipt, ...]:
        grouped: dict[tuple[str, str], list[ContextSeriesDefinition]] = {}
        for definition in self._definitions:
            if definition.source != "bea":
                continue
            attrs = definition.expected_source_attributes
            grouped.setdefault((attrs["dataset"], attrs["table_name"]), []).append(definition)
        if not grouped:
            raise ContextHistoryError("No BEA history definitions are registered")
        receipts: list[ContextHistoryReceipt] = []
        for (dataset, table_name), definitions in sorted(grouped.items()):
            data = client.fetch_table(
                dataset=dataset,
                table_name=table_name,
                year="X",
                frequency="M",
            )
            receipts.append(self.ingest_bea(data, definitions=definitions, start=start))
        return tuple(receipts)

    def fetch_census(
        self, client: CensusClient, *, start: date = DEFAULT_START
    ) -> tuple[ContextHistoryReceipt, ...]:
        definitions = tuple(item for item in self._definitions if item.source == "census")
        if not definitions:
            raise ContextHistoryError("No Census history definitions are registered")
        grouped: dict[str, list[ContextSeriesDefinition]] = {}
        for definition in definitions:
            grouped.setdefault(definition.expected_source_attributes["dataset"], []).append(
                definition
            )
        receipts: list[ContextHistoryReceipt] = []
        for dataset, dataset_definitions in sorted(grouped.items()):
            client.fetch_variables(dataset)
            time_range = f"from {start.year:04d}-{start.month:02d}"
            data = client.fetch_periods(dataset, time_range)
            receipts.append(self.ingest_census(data, definitions=dataset_definitions, start=start))
        return tuple(receipts)

    def ingest_bea(
        self,
        data: BeaTableData,
        *,
        definitions: Sequence[ContextSeriesDefinition],
        start: date = DEFAULT_START,
    ) -> ContextHistoryReceipt:
        rows = bea_history_rows(data, definitions=definitions, start=start)
        return self._ingest("bea", data.source_url, data.checksum, rows)

    def ingest_census(
        self,
        data: CensusDatasetData,
        *,
        definitions: Sequence[ContextSeriesDefinition],
        start: date = DEFAULT_START,
    ) -> ContextHistoryReceipt:
        rows = census_history_rows(data, definitions=definitions, start=start)
        return self._ingest("census", data.source_url, data.checksum, rows)

    def _ingest(
        self,
        source: ContextSource,
        source_url: str,
        checksum: str,
        rows: list[dict[str, object]],
    ) -> ContextHistoryReceipt:
        identity = (source_url, checksum)
        matching = self._known_rows.get(identity)
        if matching is not None:
            _verify_existing_rows(matching, rows)
            batch_path = self._known_batches.get(identity)
            snapshot_at = matching["ingested_at"].min()
            if not isinstance(snapshot_at, datetime):
                raise ContextHistoryError("Stored context snapshot timestamp is invalid")
            return ContextHistoryReceipt(
                source=source,
                source_url=source_url,
                checksum=checksum,
                snapshot_at=snapshot_at,
                series_count=matching["series_id"].n_unique(),
                row_count=len(rows),
                already_present=True,
                batch_path=batch_path,
                content_hash=(
                    batch_path.stem.removeprefix("batch-") if batch_path is not None else None
                ),
            )

        write = self._store.append("raw_observations", rows)
        stored = pl.read_parquet(write.path)
        self._known_rows[identity] = stored
        self._known_batches[identity] = write.path
        snapshot_at = stored["ingested_at"].min()
        if not isinstance(snapshot_at, datetime):
            raise ContextHistoryError("Stored context snapshot timestamp is invalid")
        return ContextHistoryReceipt(
            source=source,
            source_url=source_url,
            checksum=checksum,
            snapshot_at=snapshot_at,
            series_count=stored["series_id"].n_unique(),
            row_count=write.row_count,
            already_present=write.already_present,
            batch_path=write.path,
            content_hash=write.content_hash,
        )

    def _load_existing_index(self) -> None:
        paths = sorted((self._store.root / "raw_observations").glob("batch-*.parquet"))
        for path in paths:
            frame = pl.read_parquet(path).filter(pl.col("source").is_in(set(LAKE_SOURCE.values())))
            if frame.is_empty():
                continue
            for source_url, checksum in (
                frame.select(["source_url", "checksum"]).unique().iter_rows()
            ):
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


def bea_history_rows(
    data: BeaTableData,
    *,
    definitions: Sequence[ContextSeriesDefinition],
    start: date,
) -> list[dict[str, object]]:
    snapshot_at = _snapshot_at(data.retrieved_at)
    _validate_common_source(data.source_url, data.checksum, "bea")
    applicable = tuple(definitions)
    if not applicable or any(item.source != "bea" for item in applicable):
        raise ContextHistoryError("BEA history definitions are invalid")
    if any(
        item.expected_source_attributes["dataset"] != data.dataset
        or item.expected_source_attributes["table_name"] != data.table_name
        for item in applicable
    ):
        raise ContextHistoryError("BEA response does not match its registered table")

    rows: list[dict[str, object]] = []
    period_sets: list[set[date]] = []
    for definition in applicable:
        attrs = definition.expected_source_attributes
        matches = [
            row
            for row in data.rows
            if row.get("SeriesCode") == attrs["series_code"]
            and row.get("LineNumber") == attrs["line_number"]
        ]
        if not matches:
            raise ContextHistoryError(f"Registered BEA history is missing: {definition.series_id}")
        selected: dict[date, float] = {}
        for raw in matches:
            verify_bea_row(definition, raw)
            period = _bea_period(raw.get("TimePeriod", ""))
            if period < start:
                continue
            value = parse_bea_value(raw.get("DataValue", ""))
            if value is None:
                raise ContextHistoryError("BEA history contains an unlabeled missing value")
            numeric = float(value)
            if not isfinite(numeric) or numeric < 0:
                raise ContextHistoryError("BEA history contains an invalid value")
            if period in selected:
                raise ContextHistoryError("BEA history contains a duplicate period")
            selected[period] = numeric
        if not selected:
            raise ContextHistoryError(
                f"BEA history has no rows since {start}: {definition.series_id}"
            )
        period_sets.append(set(selected))
        rows.extend(
            _snapshot_row(
                source="bea",
                definition=definition,
                period=period,
                value=value,
                snapshot_at=snapshot_at,
                source_url=data.source_url,
                checksum=data.checksum,
            )
            for period, value in sorted(selected.items())
        )
    _validate_period_alignment(period_sets, snapshot_at)
    return rows


def census_history_rows(
    data: CensusDatasetData,
    *,
    definitions: Sequence[ContextSeriesDefinition],
    start: date,
) -> list[dict[str, object]]:
    snapshot_at = _snapshot_at(data.retrieved_at)
    _validate_common_source(data.source_url, data.checksum, "census")
    applicable = tuple(definitions)
    if not applicable or any(item.source != "census" for item in applicable):
        raise ContextHistoryError("Census history definitions are invalid")
    if any(item.expected_source_attributes["dataset"] != data.dataset for item in applicable):
        raise ContextHistoryError("Census response does not match its registered dataset")

    rows: list[dict[str, object]] = []
    period_sets: list[set[date]] = []
    for definition in applicable:
        attrs = definition.expected_source_attributes
        matches = [
            row
            for row in data.rows
            if row.get("category_code") == attrs["category_code"]
            and row.get("data_type_code") == attrs["data_type_code"]
            and row.get("seasonally_adj") == attrs["seasonally_adj"]
        ]
        if not matches:
            raise ContextHistoryError(
                f"Registered Census history is missing: {definition.series_id}"
            )
        selected: dict[date, float] = {}
        for raw in matches:
            if raw.get("program_code") != attrs["program_code"]:
                raise ContextHistoryError("Census history program_code changed")
            period = _census_period(raw.get("time", ""))
            _validate_time_slot(raw.get("time_slot_date", ""), period)
            if period < start:
                continue
            value = parse_census_value(raw.get("cell_value", ""))
            if value is None:
                raise ContextHistoryError("Census history contains an unlabeled missing value")
            numeric = float(value)
            if not isfinite(numeric) or numeric < 0:
                raise ContextHistoryError("Census history contains an invalid value")
            if period in selected:
                raise ContextHistoryError("Census history contains a duplicate period")
            selected[period] = numeric
        if not selected:
            raise ContextHistoryError(
                f"Census history has no rows since {start}: {definition.series_id}"
            )
        period_sets.append(set(selected))
        rows.extend(
            _snapshot_row(
                source="census",
                definition=definition,
                period=period,
                value=value,
                snapshot_at=snapshot_at,
                source_url=data.source_url,
                checksum=data.checksum,
            )
            for period, value in sorted(selected.items())
        )
    _validate_period_alignment(period_sets, snapshot_at)
    return rows


def validate_context_history(
    store: AppendOnlyParquetStore, sources: Sequence[ContextSource]
) -> ContextHistoryValidation:
    selected_sources = tuple(dict.fromkeys(sources))
    if not selected_sources:
        raise ContextHistoryError("Context validation requires at least one source")
    if any(source not in LAKE_SOURCE for source in selected_sources):
        raise ContextHistoryError("Context validation source is invalid")
    expected_lake_sources = {LAKE_SOURCE[source] for source in selected_sources}
    frame = store.read_table("raw_observations").filter(
        pl.col("source").is_in(expected_lake_sources)
    )
    if frame.is_empty():
        raise ContextHistoryError("Context history is empty")
    if set(frame["source"].unique().to_list()) != expected_lake_sources:
        raise ContextHistoryError("Context source coverage is incomplete")
    if frame.select(pl.col("checksum").str.contains(r"^[0-9a-f]{64}$").all()).item() is not True:
        raise ContextHistoryError("Context history contains an invalid checksum")
    if frame.select((pl.col("release_date") == pl.col("ingested_at")).all()).item() is not True:
        raise ContextHistoryError("Context history has a false historical release timestamp")
    if (
        frame.select((pl.col("vintage_date") == pl.col("ingested_at").dt.date()).all()).item()
        is not True
    ):
        raise ContextHistoryError("Context history has an invalid snapshot vintage")
    if any(not isfinite(value) or value < 0 for value in frame["value"].to_list()):
        raise ContextHistoryError("Context history contains an invalid value")
    identities = frame.select(["source_url", "checksum", "series_id", "obs_period"])
    if identities.is_duplicated().any():
        raise ContextHistoryError("Context history contains duplicate source observations")

    definitions = {item.series_id: item for item in load_context_series()}
    expected_series = {
        item.series_id for item in definitions.values() if item.source in selected_sources
    }
    if set(frame["series_id"].unique().to_list()) != expected_series:
        raise ContextHistoryError("Context series coverage is incomplete")
    for series_key, series in frame.group_by("series_id"):
        series_id = series_key[0] if isinstance(series_key, tuple) else series_key
        definition = definitions[str(series_id)]
        if series.select((pl.col("unit") == definition.units).all()).item() is not True:
            raise ContextHistoryError(f"Context unit changed: {series_id}")

    batch_count = 0
    for (_source_url, _checksum), snapshot in frame.group_by(["source_url", "checksum"]):
        snapshot_times = snapshot["ingested_at"].unique().to_list()
        if len(snapshot_times) != 1 or not isinstance(snapshot_times[0], datetime):
            raise ContextHistoryError("Context source response has multiple snapshot times")
        period_sets = [
            set(series["obs_period"].to_list())
            for _series_id, series in snapshot.group_by("series_id")
        ]
        _validate_period_alignment(period_sets, snapshot_times[0])
        batch_count += 1

    rows_by_source = {
        str(source): int(count) for source, count in frame.group_by("source").len().iter_rows()
    }
    rows_by_series = {
        str(series_id): int(count)
        for series_id, count in frame.group_by("series_id").len().iter_rows()
    }
    latest_by_series = {
        str(series_id): period
        for series_id, period in frame.group_by("series_id")
        .agg(pl.col("obs_period").max())
        .iter_rows()
        if isinstance(period, date)
    }
    if len(latest_by_series) != len(expected_series):
        raise ContextHistoryError("Context latest-period calculation is incomplete")
    return ContextHistoryValidation(
        total_rows=frame.height,
        snapshot_batches=batch_count,
        rows_by_source=dict(sorted(rows_by_source.items())),
        rows_by_series=dict(sorted(rows_by_series.items())),
        latest_period_by_series=dict(sorted(latest_by_series.items())),
    )


def _snapshot_row(
    *,
    source: ContextSource,
    definition: ContextSeriesDefinition,
    period: date,
    value: float,
    snapshot_at: datetime,
    source_url: str,
    checksum: str,
) -> dict[str, object]:
    return {
        "source": LAKE_SOURCE[source],
        "series_id": definition.series_id,
        "obs_period": period,
        "value": value,
        "unit": definition.units,
        "release_date": snapshot_at,
        "vintage_date": snapshot_at.date(),
        "ingested_at": snapshot_at,
        "source_url": source_url,
        "checksum": checksum,
    }


def _snapshot_at(raw: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContextHistoryError("Context retrieval timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContextHistoryError("Context retrieval timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _validate_common_source(source_url: str, checksum: str, source: ContextSource) -> None:
    expected_prefix = BEA_ENDPOINT if source == "bea" else f"{CENSUS_BASE}/"
    if not source_url.startswith(expected_prefix):
        raise ContextHistoryError(f"{source.upper()} history URL mismatch: {source_url!r}")
    if re.search(r"(?:UserID|key)=", source_url, flags=re.IGNORECASE):
        raise ContextHistoryError("Context provenance URL contains a credential parameter")
    if re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
        raise ContextHistoryError("Context checksum is not lowercase SHA-256")


def _bea_period(raw: str) -> date:
    match = re.fullmatch(r"(\d{4})M(0[1-9]|1[0-2])", raw)
    if match is None:
        raise ContextHistoryError(f"Invalid BEA monthly period: {raw!r}")
    year, month = map(int, match.groups())
    return date(year, month, monthrange(year, month)[1])


def _census_period(raw: str) -> date:
    match = re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])", raw)
    if match is None:
        raise ContextHistoryError(f"Invalid Census monthly period: {raw!r}")
    year, month = map(int, match.groups())
    return date(year, month, monthrange(year, month)[1])


def _validate_time_slot(raw: str, period: date) -> None:
    match = re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])-01 00:00:00\.0", raw)
    if match is None:
        raise ContextHistoryError(f"Invalid Census time_slot_date: {raw!r}")
    slot = date(int(match.group(1)), int(match.group(2)), 1)
    if slot != period.replace(day=1):
        raise ContextHistoryError("Census time slot and observation period disagree")


def _validate_period_alignment(period_sets: Sequence[set[date]], snapshot_at: datetime) -> None:
    if not period_sets or any(periods != period_sets[0] for periods in period_sets[1:]):
        raise ContextHistoryError("Context snapshot series have different period coverage")
    latest_period = max(period_sets[0])
    age_days = (snapshot_at.date() - latest_period).days
    if not 20 <= age_days <= 100:
        raise ContextHistoryError("Context snapshot is stale or implausibly current")


def _verify_existing_rows(
    existing: pl.DataFrame, expected_rows: Sequence[Mapping[str, object]]
) -> None:
    columns = ["source", "series_id", "obs_period", "value", "unit", "source_url", "checksum"]
    expected = {tuple(row[column] for column in columns) for row in expected_rows}
    actual = {tuple(row) for row in existing.select(columns).iter_rows()}
    if len(existing) != len(expected_rows) or actual != expected:
        raise ContextHistoryError(
            "Stored context snapshot with the same source checksum does not match the live parse"
        )


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("bea", "census", "all"), default="all")
    parser.add_argument("--start", type=_parse_date, default=DEFAULT_START)
    parser.add_argument("--lake-root", type=Path, default=Path(".local/lake/raw"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sources: tuple[ContextSource, ...] = (
        ("bea", "census") if args.source == "all" else (cast(ContextSource, args.source),)
    )
    store = AppendOnlyParquetStore(args.lake_root)
    receipts: list[ContextHistoryReceipt] = []
    with HttpTransport(min_interval_seconds=0.5) as transport:
        ingestor = ContextHistoryIngestor(store)
        if "bea" in sources:
            receipts.extend(
                ingestor.fetch_bea(
                    BeaClient(transport, os.environ.get("BEA_API_KEY", "")), start=args.start
                )
            )
        if "census" in sources:
            receipts.extend(
                ingestor.fetch_census(
                    CensusClient(transport, os.environ.get("CENSUS_API_KEY", "")),
                    start=args.start,
                )
            )
        validation = validate_context_history(store, sources)
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
