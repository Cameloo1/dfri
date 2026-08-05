from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

import polars as pl
import pytest

from dfri.lake.guard import VintageGuard
from dfri.nowcast.features import (
    BridgeFeatureError,
    build_bridge_feature,
    historical_bridge_features,
    latest_h8_data_vintage,
)
from dfri.nowcast.targets import FirstPrintTarget


@dataclass
class Reader:
    frames: dict[str, pl.DataFrame]

    def read_series(self, series_id: str) -> pl.DataFrame:
        return self.frames[series_id]


def raw_row(
    series_id: str,
    period: date,
    value: float,
    release_at: datetime,
    *,
    source: str = "FEDERAL_RESERVE_BOARD",
    unit: str = "Millions of U.S. Dollars",
    source_url: str | None = None,
    checksum: str = "1" * 64,
) -> dict[str, object]:
    is_h8 = series_id.startswith("B")
    return {
        "source": source,
        "series_id": series_id,
        "obs_period": period,
        "value": value,
        "unit": unit,
        "release_date": release_at,
        "vintage_date": release_at.date(),
        "ingested_at": release_at,
        "source_url": source_url
        or (
            f"https://www.federalreserve.gov/releases/h8/{release_at.strftime('%Y%m%d')}/"
            if is_h8
            else "https://www2.census.gov/retail/releases/historical/marts/adv2401.pdf"
        ),
        "checksum": checksum,
    }


def feature_guard() -> VintageGuard:
    h8_rows = [
        raw_row("B1247NCBA", date(2023, 12, 27), 100.0, datetime(2024, 1, 5, 21, 15, tzinfo=UTC)),
        raw_row("B1247NCBA", date(2024, 1, 3), 110.0, datetime(2024, 1, 12, 21, 15, tzinfo=UTC)),
        raw_row("B1247NCBA", date(2024, 1, 3), 112.0, datetime(2024, 1, 19, 21, 15, tzinfo=UTC)),
        raw_row("B1247NCBA", date(2024, 1, 10), 120.0, datetime(2024, 1, 19, 21, 15, tzinfo=UTC)),
        raw_row("B1247NCBA", date(2024, 1, 17), 125.0, datetime(2024, 1, 26, 21, 15, tzinfo=UTC)),
        raw_row("B1247NCBA", date(2024, 1, 24), 130.0, datetime(2024, 2, 2, 21, 15, tzinfo=UTC)),
        raw_row("B1247NCBA", date(2024, 1, 31), 140.0, datetime(2024, 2, 9, 21, 15, tzinfo=UTC)),
        raw_row("B1247NCBA", date(2024, 1, 10), 999.0, datetime(2024, 2, 20, tzinfo=UTC)),
    ]
    retail = raw_row(
        "DELTA_RETAIL_SALES.M",
        date(2024, 1, 31),
        9.0,
        datetime(2024, 2, 15, 13, 30, tzinfo=UTC),
        source="CENSUS_MARTS_ARCHIVE",
    )
    return VintageGuard(
        Reader(
            {
                "B1247NCBA": pl.DataFrame(h8_rows),
                "B3248NCBA": pl.DataFrame(
                    [replace_dict(row, series_id="B3248NCBA") for row in h8_rows]
                ),
                "DELTA_RETAIL_SALES.M": pl.DataFrame([retail]),
            }
        )
    )


def replace_dict(row: dict[str, object], **changes: object) -> dict[str, object]:
    return {**row, **changes}


def test_bridge_feature_uses_latest_available_h8_vintages_and_explicit_ragged_edge() -> None:
    feature = build_bridge_feature(
        feature_guard(),
        "DELTA_DTCTLR.M",
        date(2024, 1, 31),
        datetime(2024, 1, 26, 22, tzinfo=UTC),
    )

    assert feature.h8_series == "B1247NCBA"
    assert feature.h8_change_sum == 25.0
    assert feature.h8_weeks_observed == 3
    assert feature.h8_weeks_expected == 5
    assert feature.h8_coverage == 0.6
    assert feature.h8_paced_change == pytest.approx(25.0 / 0.6)
    assert [(item.obs_period, item.change) for item in feature.h8_weekly_changes] == [
        (date(2024, 1, 3), 12.0),
        (date(2024, 1, 10), 8.0),
        (date(2024, 1, 17), 5.0),
    ]
    assert feature.retail_change is None
    assert feature.latest_h8_release_at == datetime(2024, 1, 26, 21, 15, tzinfo=UTC)
    assert feature.latest_h8_observation_period == date(2024, 1, 17)
    assert len(feature.inputs_hash) == 64


def test_bridge_feature_adds_retail_only_after_release_and_filters_future_poison() -> None:
    guard = feature_guard()
    before = build_bridge_feature(
        guard,
        "DELTA_DTCTLR.M",
        date(2024, 1, 31),
        datetime(2024, 2, 14, tzinfo=UTC),
    )
    after = build_bridge_feature(
        guard,
        "DELTA_DTCTLR.M",
        date(2024, 1, 31),
        datetime(2024, 2, 16, tzinfo=UTC),
    )

    assert before.h8_change_sum == 40.0
    assert before.retail_change is None
    assert after.h8_change_sum == 40.0
    assert after.retail_change == 9.0
    assert after.retail_release_at == datetime(2024, 2, 15, 13, 30, tzinfo=UTC)
    assert before.inputs_hash != after.inputs_hash


def test_nonrevolving_target_maps_to_other_consumer_h8_series() -> None:
    feature = build_bridge_feature(
        feature_guard(),
        "DELTA_DTCTLN.M",
        date(2024, 1, 31),
        datetime(2024, 2, 16, tzinfo=UTC),
    )

    assert feature.h8_series == "B3248NCBA"


def test_latest_h8_data_vintage_requires_one_shared_registered_release() -> None:
    guard = feature_guard()
    boundary = datetime(2024, 2, 16, tzinfo=UTC)

    assert latest_h8_data_vintage(guard, boundary) == datetime(2024, 2, 9, 21, 15, tzinfo=UTC)

    reader = guard._reader
    assert isinstance(reader, Reader)
    reader.frames["B3248NCBA"] = reader.frames["B3248NCBA"].filter(
        pl.col("release_date") < datetime(2024, 2, 9, tzinfo=UTC)
    )
    with pytest.raises(BridgeFeatureError, match="different latest vintages"):
        latest_h8_data_vintage(guard, boundary)


def test_historical_feature_origin_is_final_h8_release_before_grade() -> None:
    target = FirstPrintTarget(
        target_series="DELTA_DTCTLR.M",
        level_series="DTCTLR.M",
        target_period=date(2024, 1, 31),
        value=10.0,
        unit="Millions of U.S. Dollars",
        release_at=datetime(2024, 2, 18, tzinfo=UTC),
        vintage_date=date(2024, 2, 18),
        source_url="https://www.federalreserve.gov/releases/g19/20240218/",
        checksum="a" * 64,
    )

    features = historical_bridge_features(feature_guard(), (target,))

    assert features[0].as_of == datetime(2024, 2, 9, 21, 15, tzinfo=UTC)
    assert features[0].h8_coverage == 1.0


def test_bridge_feature_supports_no_target_weeks_yet() -> None:
    feature = build_bridge_feature(
        feature_guard(),
        "DELTA_DTCTLR.M",
        date(2024, 2, 29),
        datetime(2024, 1, 26, tzinfo=UTC),
    )

    assert feature.h8_weeks_observed == 0
    assert feature.h8_change_sum == 0.0
    assert feature.h8_paced_change is None
    assert feature.h8_coverage == 0.0


def test_bridge_feature_rejects_invalid_target_and_as_of() -> None:
    guard = feature_guard()
    with pytest.raises(BridgeFeatureError, match="Unsupported"):
        build_bridge_feature(guard, "BAD", date(2024, 1, 31), date(2024, 2, 1))
    with pytest.raises(BridgeFeatureError, match="month end"):
        build_bridge_feature(guard, "DELTA_DTCTLR.M", date(2024, 1, 1), date(2024, 2, 1))
    with pytest.raises(BridgeFeatureError, match="timezone-aware"):
        build_bridge_feature(
            guard,
            "DELTA_DTCTLR.M",
            date(2024, 1, 31),
            datetime(2024, 2, 1, tzinfo=UTC).replace(tzinfo=None),
        )


def test_bridge_feature_rejects_conflicting_h8_vintage() -> None:
    guard = feature_guard()
    reader = guard._reader
    assert isinstance(reader, Reader)
    frame = reader.frames["B1247NCBA"]
    conflict = replace_dict(frame.row(1, named=True), value=777.0, checksum="f" * 64)
    reader.frames["B1247NCBA"] = pl.concat([frame, pl.DataFrame([conflict])])

    with pytest.raises(BridgeFeatureError, match="conflicting"):
        build_bridge_feature(
            guard,
            "DELTA_DTCTLR.M",
            date(2024, 1, 31),
            datetime(2024, 2, 16, tzinfo=UTC),
        )
