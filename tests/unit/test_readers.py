from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from dfri.lake.readers import CachingSeriesReader, LakeSeriesReader
from dfri.lake.store import AppendOnlyParquetStore


def test_lake_series_reader_filters_exact_series(tmp_path: Path) -> None:
    store = AppendOnlyParquetStore(tmp_path)
    common: dict[str, object] = {
        "source": "FEDERAL_RESERVE_BOARD",
        "obs_period": date(2024, 1, 1),
        "value": 1.0,
        "unit": "Billions of Dollars",
        "release_date": datetime(2024, 3, 7, 20, 0, tzinfo=UTC),
        "vintage_date": date(2024, 3, 7),
        "ingested_at": datetime(2024, 3, 7, 20, 5, tzinfo=UTC),
        "source_url": "https://www.federalreserve.gov/releases/g19/data/FRB_g19_xml.zip",
        "checksum": "a" * 64,
    }
    store.append(
        "raw_observations",
        [
            {**common, "series_id": "DTCTLR.M"},
            {**common, "series_id": "DTCTLN.M"},
        ],
    )

    frame = LakeSeriesReader(store).read_series("DTCTLR.M")
    assert frame["series_id"].to_list() == ["DTCTLR.M"]


def test_caching_series_reader_reads_underlying_series_once() -> None:
    class CountingReader:
        def __init__(self) -> None:
            self.calls = 0

        def read_series(self, series_id: str) -> pl.DataFrame:
            self.calls += 1
            return pl.DataFrame({"series_id": [series_id]})

    underlying = CountingReader()
    reader = CachingSeriesReader(underlying)

    first = reader.read_series("A")
    second = reader.read_series("A")

    assert underlying.calls == 1
    assert first.equals(second)
    assert first is not second
