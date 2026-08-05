from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from dfri.nowcast.baselines import (
    AR2_VERSION,
    RANDOM_WALK_VERSION,
    SEASONAL_NAIVE_VERSION,
    BaselineModelError,
    ar2,
    expanding_window_baselines,
    random_walk,
    seasonal_naive,
)
from dfri.nowcast.targets import FirstPrintTarget


def month_end(year: int, month: int) -> date:
    next_year = year + int(month == 12)
    next_month = 1 if month == 12 else month + 1
    return date(next_year, next_month, 1) - date.resolution


def targets(values: list[float], *, start_year: int = 2015) -> tuple[FirstPrintTarget, ...]:
    output: list[FirstPrintTarget] = []
    year = start_year
    month = 1
    for index, value in enumerate(values):
        period = month_end(year, month)
        release_at = datetime.combine(period + timedelta(days=40), datetime.min.time(), UTC)
        output.append(
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
                checksum=f"{index + 1:064x}",
            )
        )
        year += int(month == 12)
        month = 1 if month == 12 else month + 1
    return tuple(output)


def test_random_walk_and_seasonal_naive_use_only_prior_targets() -> None:
    history = targets([float(value) for value in range(1, 14)])
    target_period = month_end(2016, 2)

    random = random_walk(history, target_period)
    seasonal = seasonal_naive(history, target_period)

    assert random.model_version == RANDOM_WALK_VERSION
    assert random.point == 13.0
    assert seasonal is not None
    assert seasonal.model_version == SEASONAL_NAIVE_VERSION
    assert seasonal.point == 2.0
    assert len(random.inputs_hash) == 64


def test_ar2_recovers_exact_autoregressive_process() -> None:
    values = [5.0, 8.0]
    for _ in range(30):
        values.append(2.0 + 0.6 * values[-1] - 0.2 * values[-2])
    history = targets(values)

    forecast = ar2(history, month_end(2017, 9))
    expected = 2.0 + 0.6 * values[-1] - 0.2 * values[-2]

    assert forecast.model_version == AR2_VERSION
    assert forecast.point == pytest.approx(expected, abs=1e-10)
    assert forecast.training_observations == len(history)


def test_expanding_window_emits_all_available_baselines_from_2018() -> None:
    history = targets([float((index % 7) * 10 + index) for index in range(48)])
    forecasts = expanding_window_baselines(history, start=date(2018, 1, 1))

    assert forecasts
    assert {item.model_version for item in forecasts} == {
        RANDOM_WALK_VERSION,
        SEASONAL_NAIVE_VERSION,
        AR2_VERSION,
    }
    assert min(item.target_period for item in forecasts) >= date(2018, 1, 1)
    assert all(item.training_observations < len(history) for item in forecasts)


def test_inputs_hash_changes_when_prior_evidence_changes() -> None:
    history = targets([float(value) for value in range(1, 14)])
    target_period = month_end(2016, 2)
    original = random_walk(history, target_period)
    changed = random_walk(
        (*history[:-1], replace(history[-1], value=999.0)),
        target_period,
    )
    assert original.inputs_hash != changed.inputs_hash


def test_baselines_fail_closed_on_gap_mixed_series_and_rank_deficiency() -> None:
    history = targets([1.0, 2.0, 3.0, 4.0])
    with pytest.raises(BaselineModelError, match="immediately follow"):
        random_walk(history, month_end(2015, 6))
    with pytest.raises(BaselineModelError, match="mixes"):
        random_walk(
            (*history[:-1], replace(history[-1], target_series="DELTA_DTCTLN.M")),
            month_end(2015, 5),
        )
    with pytest.raises(BaselineModelError, match="rank deficient"):
        ar2(targets([1.0] * 8), month_end(2015, 9))


def test_baseline_boundary_errors_and_unavailable_seasonal_forecast() -> None:
    short = targets([1.0, 2.0, 3.0])
    assert seasonal_naive(short, month_end(2015, 4)) is None
    with pytest.raises(BaselineModelError, match="non-empty"):
        random_walk((), month_end(2015, 1))
    with pytest.raises(BaselineModelError, match=r"AR\(2\) requires"):
        ar2(short, month_end(2015, 4))
    with pytest.raises(BaselineModelError, match="at least four"):
        expanding_window_baselines(short, start=date(2015, 1, 1), ar2_min_history=3)


def test_baseline_history_rejects_nonfinite_and_nonmonotone_release_time() -> None:
    history = targets([1.0, 2.0, 3.0, 4.0])
    with pytest.raises(BaselineModelError, match="non-finite"):
        random_walk(
            (*history[:-1], replace(history[-1], value=float("nan"))),
            month_end(2015, 5),
        )
    with pytest.raises(BaselineModelError, match="strictly increasing"):
        random_walk(
            (*history[:-1], replace(history[-1], release_at=history[-2].release_at)),
            month_end(2015, 5),
        )
