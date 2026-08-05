"""Point-in-time ragged-edge features for monthly consumer-credit nowcasts."""

from __future__ import annotations

import hashlib
import json
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from math import isfinite
from typing import Final, cast

import polars as pl

from dfri.ingest.board_history import BOARD_SOURCE, BOARD_UNIT
from dfri.ingest.registry import load_census_archive
from dfri.lake.guard import VintageGuard
from dfri.nowcast.targets import FirstPrintTarget

H8_BY_TARGET: Final[dict[str, str]] = {
    "DELTA_DTCTLR.M": "B1247NCBA",
    "DELTA_DTCTLN.M": "B3248NCBA",
}
H8_ARCHIVE_PATTERN: Final = re.compile(r"^https://www\.federalreserve\.gov/releases/h8/\d{8}/$")


class BridgeFeatureError(RuntimeError):
    """Point-in-time bridge inputs violate their source or vintage contract."""


@dataclass(frozen=True)
class H8WeeklyChange:
    obs_period: date
    change: float


@dataclass(frozen=True)
class BridgeFeature:
    target_series: str
    target_period: date
    as_of: datetime
    h8_series: str
    h8_change_sum: float
    h8_paced_change: float | None
    h8_weeks_observed: int
    h8_weeks_expected: int
    h8_coverage: float
    latest_h8_release_at: datetime | None
    retail_change: float | None
    retail_release_at: datetime | None
    inputs_hash: str
    h8_weekly_changes: tuple[H8WeeklyChange, ...] = ()
    latest_h8_observation_period: date | None = None


def latest_h8_data_vintage(guard: VintageGuard, as_of: date | datetime) -> datetime:
    """Return the one release timestamp shared by the registered headline H.8 inputs."""

    boundary = _as_of_utc(as_of)
    releases: set[datetime] = set()
    for series_id in sorted(set(H8_BY_TARGET.values())):
        frame = _latest_h8_vintages(guard.read(series_id, boundary), series_id)
        if frame.is_empty():
            raise BridgeFeatureError(f"No dated H.8 release is available for {series_id}")
        release = frame["release_date"].max()
        if not isinstance(release, datetime):
            raise BridgeFeatureError(f"H.8 latest release timestamp is invalid for {series_id}")
        releases.add(release)
    if len(releases) != 1:
        raise BridgeFeatureError("Registered headline H.8 inputs have different latest vintages")
    return releases.pop()


def build_bridge_feature(
    guard: VintageGuard,
    target_series: str,
    target_period: date,
    as_of: date | datetime,
) -> BridgeFeature:
    """Build one feature vector from evidence available at ``as_of`` and nothing later."""

    try:
        h8_series = H8_BY_TARGET[target_series]
    except KeyError as exc:
        raise BridgeFeatureError(f"Unsupported bridge target series: {target_series}") from exc
    if target_period != _month_end(target_period):
        raise BridgeFeatureError("Bridge target period must be a month end")
    boundary = _as_of_utc(as_of)
    h8 = _latest_h8_vintages(guard.read(h8_series, boundary), h8_series)
    expected_wednesdays = _wednesdays(target_period)
    by_period = {row["obs_period"]: row for row in h8.iter_rows(named=True)}
    observed = [period for period in expected_wednesdays if period in by_period]

    weekly_changes: list[H8WeeklyChange] = []
    evidence: list[dict[str, object]] = []
    for period in observed:
        current = by_period[period]
        previous_period = period - timedelta(days=7)
        previous = by_period.get(previous_period)
        if previous is None:
            raise BridgeFeatureError(f"H.8 weekly predecessor is missing: {previous_period}")
        weekly_changes.append(
            H8WeeklyChange(
                obs_period=period,
                change=float(current["value"]) - float(previous["value"]),
            )
        )
        for row in (previous, current):
            evidence.append(
                {
                    "obs_period": row["obs_period"].isoformat(),
                    "value": float(row["value"]),
                    "release_date": row["release_date"].isoformat(),
                    "checksum": row["checksum"],
                }
            )
    h8_change_sum = float(sum(item.change for item in weekly_changes))
    weeks_observed = len(observed)
    weeks_expected = len(expected_wednesdays)
    coverage = weeks_observed / weeks_expected
    paced = h8_change_sum / coverage if coverage > 0 else None
    latest_h8_release = h8["release_date"].max() if not h8.is_empty() else None
    latest_h8_observation = h8["obs_period"].max() if not h8.is_empty() else None
    if latest_h8_release is not None and not isinstance(latest_h8_release, datetime):
        raise BridgeFeatureError("H.8 latest release timestamp is invalid")
    if latest_h8_observation is not None and (
        not isinstance(latest_h8_observation, date) or isinstance(latest_h8_observation, datetime)
    ):
        raise BridgeFeatureError("H.8 latest observation period is invalid")

    retail_row = _retail_for_period(guard, target_period, boundary)
    retail_change = float(cast(float, retail_row["value"])) if retail_row is not None else None
    retail_release = retail_row["release_date"] if retail_row is not None else None
    if retail_release is not None and not isinstance(retail_release, datetime):
        raise BridgeFeatureError("Retail release timestamp is invalid")
    payload = {
        "target_series": target_series,
        "target_period": target_period.isoformat(),
        "as_of": boundary.isoformat(),
        "h8_series": h8_series,
        "expected_wednesdays": [item.isoformat() for item in expected_wednesdays],
        "h8_evidence": sorted(
            {json.dumps(item, sort_keys=True, separators=(",", ":")) for item in evidence}
        ),
        "retail": (
            None
            if retail_row is None
            else {
                "value": retail_change,
                "release_date": cast(datetime, retail_release).isoformat(),
                "checksum": retail_row["checksum"],
            }
        ),
    }
    return BridgeFeature(
        target_series=target_series,
        target_period=target_period,
        as_of=boundary,
        h8_series=h8_series,
        h8_change_sum=h8_change_sum,
        h8_paced_change=paced,
        h8_weeks_observed=weeks_observed,
        h8_weeks_expected=weeks_expected,
        h8_coverage=coverage,
        latest_h8_release_at=latest_h8_release,
        retail_change=retail_change,
        retail_release_at=retail_release,
        inputs_hash=hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        h8_weekly_changes=tuple(weekly_changes),
        latest_h8_observation_period=latest_h8_observation,
    )


def historical_bridge_features(
    guard: VintageGuard,
    targets: tuple[FirstPrintTarget, ...],
    *,
    start: date | None = None,
) -> tuple[BridgeFeature, ...]:
    """Build comparable end-of-nowcast features at the final H.8 release before each grade."""

    features: list[BridgeFeature] = []
    for target in targets:
        if start is not None and target.target_period < start:
            continue
        h8_series = H8_BY_TARGET.get(target.target_series)
        if h8_series is None:
            raise BridgeFeatureError(f"Unsupported bridge target series: {target.target_series}")
        before_grade = target.release_at - timedelta(microseconds=1)
        guarded = _latest_h8_vintages(guard.read(h8_series, before_grade), h8_series)
        if guarded.is_empty():
            raise BridgeFeatureError(
                f"No H.8 release predates target grade: {target.target_period}"
            )
        origin = guarded["release_date"].max()
        if not isinstance(origin, datetime):
            raise BridgeFeatureError("Historical H.8 forecast origin is invalid")
        features.append(
            build_bridge_feature(guard, target.target_series, target.target_period, origin)
        )
    return tuple(features)


def _latest_h8_vintages(frame: pl.DataFrame, series_id: str) -> pl.DataFrame:
    required = {
        "source",
        "series_id",
        "obs_period",
        "value",
        "unit",
        "release_date",
        "source_url",
        "checksum",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise BridgeFeatureError(f"H.8 input is missing columns: {missing}")
    h8 = frame.filter(
        (pl.col("source") == BOARD_SOURCE)
        & (pl.col("series_id") == series_id)
        & pl.col("source_url").str.contains(H8_ARCHIVE_PATTERN.pattern)
    )
    if h8.is_empty():
        return h8
    conflicts = (
        h8.group_by(["obs_period", "release_date"])
        .agg(pl.col("value").n_unique().alias("value_count"))
        .filter(pl.col("value_count") > 1)
    )
    if not conflicts.is_empty():
        raise BridgeFeatureError("An H.8 vintage contains conflicting values")
    h8 = (
        h8.sort(["obs_period", "release_date", "checksum"])
        .unique(
            subset=["obs_period", "value", "release_date", "source_url", "checksum"],
            keep="first",
            maintain_order=True,
        )
        .unique(subset=["obs_period"], keep="last", maintain_order=True)
        .sort("obs_period")
    )
    for row in h8.iter_rows(named=True):
        period = row["obs_period"]
        release_at = row["release_date"]
        if not isinstance(period, date) or isinstance(period, datetime) or period.weekday() != 2:
            raise BridgeFeatureError("H.8 observation period must be a Wednesday date")
        if not isinstance(release_at, datetime) or release_at.tzinfo is None:
            raise BridgeFeatureError("H.8 release timestamp must be timezone-aware")
        if row["unit"] != BOARD_UNIT:
            raise BridgeFeatureError("H.8 unit changed")
        if not isfinite(float(row["value"])):
            raise BridgeFeatureError("H.8 value must be finite")
        if H8_ARCHIVE_PATTERN.fullmatch(str(row["source_url"])) is None:
            raise BridgeFeatureError("H.8 source URL is not a dated release")
        if re.fullmatch(r"[0-9a-f]{64}", str(row["checksum"])) is None:
            raise BridgeFeatureError("H.8 checksum is invalid")
    return h8


def _retail_for_period(
    guard: VintageGuard,
    target_period: date,
    boundary: datetime,
) -> dict[str, object] | None:
    definition = load_census_archive()
    frame = guard.read(definition.series_id, boundary)
    required = {
        "source",
        "series_id",
        "obs_period",
        "value",
        "unit",
        "release_date",
        "source_url",
        "checksum",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise BridgeFeatureError(f"Retail input is missing columns: {missing}")
    retail = frame.filter(
        (pl.col("source") == definition.lake_source)
        & (pl.col("series_id") == definition.series_id)
        & (pl.col("obs_period") == target_period)
    )
    if retail.is_empty():
        return None
    conflicts = retail.select("value").n_unique()
    if conflicts != 1:
        raise BridgeFeatureError("A retail first print contains conflicting values")
    retail = retail.sort(["release_date", "checksum"]).unique(
        subset=["obs_period", "value", "release_date", "source_url", "checksum"],
        keep="first",
        maintain_order=True,
    )
    if retail.height != 1:
        raise BridgeFeatureError("A retail target month has multiple first-print releases")
    row = retail.row(0, named=True)
    expected_url = definition.source_url_pattern.replace("YYMM", target_period.strftime("%y%m"))
    release_at = row["release_date"]
    if row["unit"] != definition.units:
        raise BridgeFeatureError("Retail unit changed")
    if row["source_url"] != expected_url:
        raise BridgeFeatureError("Retail source URL does not match the target month")
    if not isinstance(release_at, datetime) or release_at.tzinfo is None:
        raise BridgeFeatureError("Retail release timestamp must be timezone-aware")
    if release_at > boundary:
        raise BridgeFeatureError("Future retail first print leaked through the Vintage Guard")
    if not isfinite(float(row["value"])):
        raise BridgeFeatureError("Retail flow must be finite")
    if re.fullmatch(r"[0-9a-f]{64}", str(row["checksum"])) is None:
        raise BridgeFeatureError("Retail checksum is invalid")
    return row


def _wednesdays(period: date) -> tuple[date, ...]:
    return tuple(
        date(period.year, period.month, day)
        for day in range(1, monthrange(period.year, period.month)[1] + 1)
        if date(period.year, period.month, day).weekday() == 2
    )


def _month_end(period: date) -> date:
    return date(period.year, period.month, monthrange(period.year, period.month)[1])


def _as_of_utc(as_of: date | datetime) -> datetime:
    if isinstance(as_of, datetime):
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise BridgeFeatureError("Bridge as_of timestamp must be timezone-aware")
        return as_of.astimezone(UTC)
    return datetime.combine(as_of, time.max, tzinfo=UTC)
