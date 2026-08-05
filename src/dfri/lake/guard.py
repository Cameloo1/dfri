"""The sole point-in-time read boundary for model-facing code."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Protocol

import polars as pl


class VintageBoundaryError(RuntimeError):
    """Raised when model input cannot prove its release-time boundary."""


class SeriesReader(Protocol):
    def read_series(self, series_id: str) -> pl.DataFrame:
        """Return observations including a timezone-aware release_date column."""


class VintageGuard:
    """Expose only observations that existed at an explicit as-of time."""

    def __init__(self, reader: SeriesReader) -> None:
        self._reader = reader

    def read(self, series_id: str, as_of: date | datetime) -> pl.DataFrame:
        frame = self._reader.read_series(series_id)
        if "release_date" not in frame.columns:
            raise VintageBoundaryError(f"{series_id} has no release_date column")
        boundary = _utc_boundary(as_of)
        released = frame.filter(pl.col("release_date") <= boundary)
        max_release = released.select(pl.col("release_date").max()).item()
        if isinstance(max_release, datetime) and max_release > boundary:
            raise VintageBoundaryError(f"Future release leaked for {series_id}")
        return released.sort(["obs_period", "release_date", "vintage_date"])


def _utc_boundary(as_of: date | datetime) -> datetime:
    if isinstance(as_of, datetime):
        if as_of.tzinfo is None:
            raise VintageBoundaryError("as_of datetime must be timezone-aware")
        return as_of.astimezone(UTC)
    return datetime.combine(as_of, time.max, tzinfo=UTC)
