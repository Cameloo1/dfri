from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

import polars as pl
import pytest

from dfri.lake.guard import VintageBoundaryError, VintageGuard


@dataclass
class Reader:
    frame: pl.DataFrame

    def read_series(self, series_id: str) -> pl.DataFrame:
        assert series_id == "CANARY"
        return self.frame


def canary_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "series_id": ["CANARY", "CANARY"],
            "obs_period": [date(2024, 1, 1), date(2024, 2, 1)],
            "value": [1.0, 999_999.0],
            "release_date": [
                datetime(2024, 2, 1, tzinfo=UTC),
                datetime(2024, 3, 1, tzinfo=UTC),
            ],
            "vintage_date": [date(2024, 2, 1), date(2024, 3, 1)],
        }
    )


def test_poisoned_future_is_visible_without_guard() -> None:
    assert canary_frame()["value"].max() == 999_999.0


def test_poisoned_future_is_removed_by_guard() -> None:
    guarded = VintageGuard(Reader(canary_frame())).read("CANARY", date(2024, 2, 15))
    assert guarded["value"].to_list() == [1.0]


def test_guard_requires_release_date() -> None:
    frame = canary_frame().drop("release_date")
    with pytest.raises(VintageBoundaryError, match="no release_date"):
        VintageGuard(Reader(frame)).read("CANARY", date(2024, 2, 15))


def test_guard_rejects_naive_datetime() -> None:
    with pytest.raises(VintageBoundaryError, match="timezone-aware"):
        VintageGuard(Reader(canary_frame())).read(
            "CANARY", datetime.fromisoformat("2024-02-15T00:00:00")
        )
