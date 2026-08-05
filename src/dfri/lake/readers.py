"""Lake readers are infrastructure and must not be imported directly by model packages."""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from dfri.lake.guard import SeriesReader
from dfri.lake.store import AppendOnlyParquetStore


@dataclass(frozen=True)
class LakeSeriesReader:
    store: AppendOnlyParquetStore

    def read_series(self, series_id: str) -> pl.DataFrame:
        return self.store.read_table("raw_observations").filter(pl.col("series_id") == series_id)


@dataclass
class CachingSeriesReader:
    """Cache immutable series snapshots for repeated point-in-time model reads."""

    reader: SeriesReader
    _cache: dict[str, pl.DataFrame] = field(default_factory=dict, init=False)

    def read_series(self, series_id: str) -> pl.DataFrame:
        frame = self._cache.get(series_id)
        if frame is None:
            frame = self.reader.read_series(series_id)
            self._cache[series_id] = frame
        return frame.clone()
