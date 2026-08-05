from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from dfri.nowcast.bridge import (
    BridgeModelError,
    expanding_window_bridge,
    fit_bridge,
)
from dfri.nowcast.features import BridgeFeature
from dfri.nowcast.targets import FirstPrintTarget


def month_end(year: int, month: int) -> date:
    next_year = year + int(month == 12)
    next_month = 1 if month == 12 else month + 1
    return date(next_year, next_month, 1) - date.resolution


def dataset(count: int = 60) -> tuple[tuple[FirstPrintTarget, ...], tuple[BridgeFeature, ...]]:
    targets: list[FirstPrintTarget] = []
    features: list[BridgeFeature] = []
    year = 2015
    month = 1
    for index in range(count):
        period = month_end(year, month)
        made_at = datetime.combine(period + timedelta(days=30), datetime.min.time(), UTC)
        h8 = float((index % 9) * 100 - 300)
        retail = float((index % 7) * 80 - 200)
        value = 50.0 + 0.8 * h8 + 0.35 * retail + float(month * 4)
        feature = BridgeFeature(
            target_series="DELTA_DTCTLR.M",
            target_period=period,
            as_of=made_at,
            h8_series="B1247NCBA",
            h8_change_sum=h8,
            h8_paced_change=h8,
            h8_weeks_observed=4,
            h8_weeks_expected=4,
            h8_coverage=1.0,
            latest_h8_release_at=made_at,
            retail_change=retail,
            retail_release_at=made_at,
            inputs_hash=f"{index + 1:064x}",
        )
        features.append(feature)
        targets.append(
            FirstPrintTarget(
                target_series="DELTA_DTCTLR.M",
                level_series="DTCTLR.M",
                target_period=period,
                value=value,
                unit="Millions of U.S. Dollars",
                release_at=made_at + timedelta(days=5),
                vintage_date=(made_at + timedelta(days=5)).date(),
                source_url=(
                    "https://www.federalreserve.gov/releases/g19/"
                    f"{(made_at + timedelta(days=5)).strftime('%Y%m%d')}/"
                ),
                checksum=f"{index + 101:064x}",
            )
        )
        year += int(month == 12)
        month = 1 if month == 12 else month + 1
    return tuple(targets), tuple(features)


def test_bridge_recovers_deterministic_linear_signal_and_intervals() -> None:
    targets, features = dataset()
    forecast_period = month_end(2020, 1)
    forecast_feature = replace(
        features[0],
        target_period=forecast_period,
        as_of=datetime(2020, 2, 28, tzinfo=UTC),
        h8_change_sum=250.0,
        h8_paced_change=250.0,
        retail_change=120.0,
        inputs_hash="f" * 64,
    )

    forecast = fit_bridge(targets, features, forecast_feature, alpha=1e-6)
    expected = 50.0 + 0.8 * 250.0 + 0.35 * 120.0 + 4.0

    assert forecast.model_version == "bridge-ridge-v2-alpha1e-06"
    assert forecast.point == pytest.approx(expected, abs=1e-3)
    assert forecast.low95 <= forecast.low80 <= forecast.point
    assert forecast.point <= forecast.high80 <= forecast.high95
    assert forecast.training_observations == 60
    assert len(forecast.inputs_hash) == 64


def test_expanding_bridge_uses_strictly_prior_months() -> None:
    targets, features = dataset()

    forecasts = expanding_window_bridge(targets, features, start=date(2018, 1, 1))

    assert len(forecasts) == 24
    assert forecasts[0].target_period == date(2018, 1, 31)
    assert forecasts[0].training_observations == 36
    assert forecasts[-1].training_observations == 59


def test_bridge_imputes_unavailable_ragged_inputs_from_training_scale() -> None:
    targets, features = dataset()
    missing = replace(
        features[0],
        target_period=month_end(2020, 1),
        as_of=datetime(2020, 1, 3, tzinfo=UTC),
        h8_change_sum=0.0,
        h8_paced_change=None,
        h8_weeks_observed=0,
        h8_coverage=0.0,
        retail_change=None,
        retail_release_at=None,
        inputs_hash="e" * 64,
    )

    forecast = fit_bridge(targets, features, missing)

    assert forecast.point == pytest.approx(forecast.point)
    assert forecast.low95 <= forecast.point <= forecast.high95


def test_bridge_hash_changes_with_evidence() -> None:
    targets, features = dataset()
    forecast_feature = replace(
        features[0],
        target_period=month_end(2020, 1),
        as_of=datetime(2020, 2, 28, tzinfo=UTC),
        inputs_hash="d" * 64,
    )
    original = fit_bridge(targets, features, forecast_feature)
    changed = fit_bridge(
        targets,
        (*features[:-1], replace(features[-1], inputs_hash="c" * 64)),
        forecast_feature,
    )

    assert original.inputs_hash != changed.inputs_hash


def test_bridge_rejects_alignment_boundary_and_parameter_errors() -> None:
    targets, features = dataset()
    forecast = replace(
        features[0],
        target_period=month_end(2020, 1),
        as_of=datetime(2020, 2, 28, tzinfo=UTC),
        inputs_hash="b" * 64,
    )
    with pytest.raises(BridgeModelError, match="positive"):
        fit_bridge(targets, features, forecast, alpha=0)
    with pytest.raises(BridgeModelError, match="aligned"):
        fit_bridge(targets, features[:-1], forecast)
    with pytest.raises(BridgeModelError, match="mixes"):
        fit_bridge(
            targets,
            (*features[:-1], replace(features[-1], target_series="DELTA_DTCTLN.M")),
            forecast,
        )
    with pytest.raises(BridgeModelError, match="misaligned"):
        fit_bridge(
            targets,
            (*features[:-1], replace(features[-1], target_period=date(1999, 1, 31))),
            forecast,
        )
    with pytest.raises(BridgeModelError, match="after its target"):
        fit_bridge(
            targets,
            (*features[:-1], replace(features[-1], as_of=targets[-1].release_at)),
            forecast,
        )
    with pytest.raises(BridgeModelError, match="at least 24"):
        expanding_window_bridge(targets, features, start=date(2018, 1, 1), min_history=12)
