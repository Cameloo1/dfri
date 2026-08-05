from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from dfri.nowcast.features import BridgeFeature, H8WeeklyChange
from dfri.nowcast.state_space import (
    STATE_SPACE_VERSION,
    StateSpaceModelError,
    expanding_window_state_space,
    fit_state_space,
)
from dfri.nowcast.targets import FirstPrintTarget


def month_end(year: int, month: int) -> date:
    next_year = year + int(month == 12)
    next_month = 1 if month == 12 else month + 1
    return date(next_year, next_month, 1) - date.resolution


def wednesdays(period: date) -> tuple[date, ...]:
    first = date(period.year, period.month, 1)
    first_wednesday = first + timedelta(days=(2 - first.weekday()) % 7)
    output: list[date] = []
    current = first_wednesday
    while current.month == period.month:
        output.append(current)
        current += timedelta(days=7)
    return tuple(output)


def dataset(count: int = 60) -> tuple[tuple[FirstPrintTarget, ...], tuple[BridgeFeature, ...]]:
    targets: list[FirstPrintTarget] = []
    features: list[BridgeFeature] = []
    year = 2015
    month = 1
    previous = 500.0
    for index in range(count):
        period = month_end(year, month)
        made_at = datetime.combine(period + timedelta(days=30), datetime.min.time(), UTC)
        value = 180.0 + 0.55 * previous + month * 6.0 + float((index % 5) - 2) * 3.0
        weeks = wednesdays(period)
        weekly = tuple(
            H8WeeklyChange(
                obs_period=week,
                change=0.18 * value + float((week_index % 3) - 1) * 2.0,
            )
            for week_index, week in enumerate(weeks)
        )
        h8_sum = sum(item.change for item in weekly)
        retail = 0.72 * value + float((index % 4) - 1) * 4.0
        feature = BridgeFeature(
            target_series="DELTA_DTCTLR.M",
            target_period=period,
            as_of=made_at,
            h8_series="B1247NCBA",
            h8_change_sum=h8_sum,
            h8_paced_change=h8_sum,
            h8_weeks_observed=len(weekly),
            h8_weeks_expected=len(weekly),
            h8_coverage=1.0,
            latest_h8_release_at=made_at,
            retail_change=retail,
            retail_release_at=made_at,
            inputs_hash=f"{index + 1:064x}",
            h8_weekly_changes=weekly,
        )
        features.append(feature)
        release_at = made_at + timedelta(days=5)
        targets.append(
            FirstPrintTarget(
                target_series="DELTA_DTCTLR.M",
                level_series="DTCTLR.M",
                target_period=period,
                value=value,
                unit="Millions of U.S. Dollars",
                release_at=release_at,
                vintage_date=release_at.date(),
                source_url=(
                    f"https://www.federalreserve.gov/releases/g19/{release_at.strftime('%Y%m%d')}/"
                ),
                checksum=f"{index + 101:064x}",
            )
        )
        previous = value
        year += int(month == 12)
        month = 1 if month == 12 else month + 1
    return tuple(targets), tuple(features)


def forecast_feature(features: tuple[BridgeFeature, ...]) -> BridgeFeature:
    period = month_end(2020, 1)
    weeks = wednesdays(period)[:3]
    weekly = tuple(
        H8WeeklyChange(obs_period=week, change=98.0 + index) for index, week in enumerate(weeks)
    )
    return replace(
        features[0],
        target_period=period,
        as_of=datetime(2020, 1, 24, 22, tzinfo=UTC),
        h8_change_sum=sum(item.change for item in weekly),
        h8_paced_change=sum(item.change for item in weekly) / 0.6,
        h8_weeks_observed=3,
        h8_weeks_expected=5,
        h8_coverage=0.6,
        retail_change=None,
        retail_release_at=None,
        inputs_hash="f" * 64,
        h8_weekly_changes=weekly,
    )


def test_state_space_filters_weekly_ragged_edge_with_ordered_intervals() -> None:
    targets, features = dataset()

    forecast = fit_state_space(targets, features, forecast_feature(features))

    assert forecast.model_version == STATE_SPACE_VERSION
    assert forecast.target_period == date(2020, 1, 31)
    assert forecast.low95 <= forecast.low80 <= forecast.point
    assert forecast.point <= forecast.high80 <= forecast.high95
    assert forecast.training_observations == 60
    assert len(forecast.inputs_hash) == 64


def test_expanding_state_space_uses_strictly_prior_months() -> None:
    targets, features = dataset()

    forecasts = expanding_window_state_space(targets, features, start=date(2018, 1, 1))

    assert len(forecasts) == 24
    assert forecasts[0].target_period == date(2018, 1, 31)
    assert forecasts[0].training_observations == 36
    assert forecasts[-1].training_observations == 59


def test_state_space_supports_no_weekly_or_retail_observation() -> None:
    targets, features = dataset()
    empty = replace(
        forecast_feature(features),
        h8_change_sum=0.0,
        h8_paced_change=None,
        h8_weeks_observed=0,
        h8_coverage=0.0,
        h8_weekly_changes=(),
        inputs_hash="e" * 64,
    )

    forecast = fit_state_space(targets, features, empty)

    assert forecast.low95 <= forecast.point <= forecast.high95


def test_state_space_hash_changes_with_training_evidence() -> None:
    targets, features = dataset()
    next_feature = forecast_feature(features)

    original = fit_state_space(targets, features, next_feature)
    changed = fit_state_space(
        targets,
        (*features[:-1], replace(features[-1], inputs_hash="d" * 64)),
        next_feature,
    )

    assert original.inputs_hash != changed.inputs_hash


def test_state_space_rejects_alignment_and_weekly_contract_errors() -> None:
    targets, features = dataset()
    next_feature = forecast_feature(features)
    with pytest.raises(StateSpaceModelError, match="aligned"):
        fit_state_space(targets, features[:-1], next_feature)
    with pytest.raises(StateSpaceModelError, match="mixes"):
        fit_state_space(
            targets,
            (*features[:-1], replace(features[-1], target_series="DELTA_DTCTLN.M")),
            next_feature,
        )
    with pytest.raises(StateSpaceModelError, match="weekly count"):
        fit_state_space(
            targets,
            (*features[:-1], replace(features[-1], h8_weeks_observed=3)),
            next_feature,
        )
    with pytest.raises(StateSpaceModelError, match="changes do not match"):
        fit_state_space(
            targets,
            (*features[:-1], replace(features[-1], h8_change_sum=-1.0)),
            next_feature,
        )
    with pytest.raises(StateSpaceModelError, match="at least 24"):
        expanding_window_state_space(targets, features, start=date(2018, 1, 1), min_history=12)
