"""Vintage-Guarded first-print target reads for the nowcast engine."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from itertools import pairwise
from math import isfinite
from typing import Final

import polars as pl

from dfri.ingest.registry import BoardTargetDefinition, load_board_targets
from dfri.lake.guard import VintageBoundaryError, VintageGuard

DERIVED_SOURCE: Final = "DFRI_DERIVED_BOARD_FIRST_PRINT_V1"
ARCHIVE_URL_PATTERN: Final = re.compile(r"^https://www\.federalreserve\.gov/releases/g19/\d{8}/$")


class TargetDatasetError(RuntimeError):
    """Stored first-print target rows violate their point-in-time contract."""


@dataclass(frozen=True)
class FirstPrintTarget:
    target_series: str
    level_series: str
    target_period: date
    value: float
    unit: str
    release_at: datetime
    vintage_date: date
    source_url: str
    checksum: str


def read_first_print_targets(
    guard: VintageGuard,
    target_series: str,
    as_of: date | datetime,
    *,
    start: date | None = None,
    definitions: tuple[BoardTargetDefinition, ...] | None = None,
) -> tuple[FirstPrintTarget, ...]:
    """Return a continuous target history containing only rows available by ``as_of``."""

    definitions = definitions or load_board_targets()
    definition_by_id = {definition.target_series_id: definition for definition in definitions}
    try:
        definition = definition_by_id[target_series]
    except KeyError as exc:
        raise TargetDatasetError(f"Unsupported first-print target series: {target_series}") from exc
    try:
        frame = guard.read(target_series, as_of)
    except VintageBoundaryError:
        raise
    required = {
        "source",
        "series_id",
        "obs_period",
        "value",
        "unit",
        "release_date",
        "vintage_date",
        "source_url",
        "checksum",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise TargetDatasetError(f"First-print target input is missing columns: {missing}")
    frame = frame.filter(
        (pl.col("source") == definition.derived_source)
        & (pl.col("series_id") == definition.target_series_id)
    )
    if frame.is_empty():
        return ()
    conflicts = (
        frame.group_by(["obs_period", "release_date"])
        .agg(pl.col("value").n_unique().alias("value_count"))
        .filter(pl.col("value_count") > 1)
    )
    if not conflicts.is_empty():
        raise TargetDatasetError("A first-print target release has conflicting stored values")
    frame = (
        frame.sort(["obs_period", "release_date", "checksum"])
        .unique(
            subset=["obs_period", "value", "release_date", "source_url", "checksum"],
            keep="first",
            maintain_order=True,
        )
        .sort(["obs_period", "release_date"])
    )
    if frame["obs_period"].n_unique() != frame.height:
        raise TargetDatasetError("A first-print target month has multiple archive releases")

    output: list[FirstPrintTarget] = []
    for row in frame.iter_rows(named=True):
        period = row["obs_period"]
        release_at = row["release_date"]
        value = float(row["value"])
        source_url = str(row["source_url"])
        checksum = str(row["checksum"])
        if not isinstance(period, date) or isinstance(period, datetime):
            raise TargetDatasetError("First-print target period must be a date")
        if not isinstance(release_at, datetime) or release_at.tzinfo is None:
            raise TargetDatasetError("First-print target release must be timezone-aware")
        if period >= release_at.date():
            raise TargetDatasetError("First-print target period is not before its release")
        if not isfinite(value):
            raise TargetDatasetError("First-print target value must be finite")
        if row["unit"] != definition.units:
            raise TargetDatasetError(f"First-print target unit changed: {row['unit']!r}")
        if ARCHIVE_URL_PATTERN.fullmatch(source_url) is None:
            raise TargetDatasetError(
                f"First-print target URL is not a dated G.19 page: {source_url}"
            )
        if re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
            raise TargetDatasetError("First-print target checksum is not lowercase SHA-256")
        output.append(
            FirstPrintTarget(
                target_series=definition.target_series_id,
                level_series=definition.level_series_id,
                target_period=period,
                value=value,
                unit=definition.units,
                release_at=release_at,
                vintage_date=row["vintage_date"],
                source_url=source_url,
                checksum=checksum,
            )
        )

    for previous, current in pairwise(output):
        if _next_month_end(previous.target_period) != current.target_period:
            raise TargetDatasetError(
                f"First-print target history has a monthly gap: "
                f"{previous.target_period} -> {current.target_period}"
            )
        if previous.release_at >= current.release_at:
            raise TargetDatasetError("First-print target releases are not strictly increasing")
    if start is not None:
        output = [row for row in output if row.target_period >= start]
    return tuple(output)


def _next_month_end(period: date) -> date:
    year = period.year + int(period.month == 12)
    month = 1 if period.month == 12 else period.month + 1
    following_year = year + int(month == 12)
    following_month = 1 if month == 12 else month + 1
    return date(following_year, following_month, 1) - date.resolution
