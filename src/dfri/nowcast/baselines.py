"""Deterministic expanding-window baseline models prescribed by section 6.2."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from math import isfinite
from typing import Final

import numpy as np

from dfri.nowcast.targets import FirstPrintTarget

RANDOM_WALK_VERSION: Final = "naive-random-walk-v1"
SEASONAL_NAIVE_VERSION: Final = "naive-seasonal-v1"
AR2_VERSION: Final = "ar2-ols-v1"
DEFAULT_AR2_MIN_HISTORY: Final = 24


class BaselineModelError(RuntimeError):
    """Baseline input history or fitted output violates its deterministic contract."""


@dataclass(frozen=True)
class BaselineForecast:
    model_version: str
    target_series: str
    target_period: date
    point: float
    training_observations: int
    inputs_hash: str


def expanding_window_baselines(
    targets: tuple[FirstPrintTarget, ...],
    *,
    start: date,
    ar2_min_history: int = DEFAULT_AR2_MIN_HISTORY,
) -> tuple[BaselineForecast, ...]:
    """Forecast each gradeable target using only strictly earlier first-print targets."""

    _validate_history(targets)
    if ar2_min_history < 4:
        raise BaselineModelError("AR(2) minimum history must be at least four months")
    forecasts: list[BaselineForecast] = []
    for index, actual in enumerate(targets):
        if actual.target_period < start:
            continue
        history = targets[:index]
        if not history:
            continue
        forecasts.append(random_walk(history, actual.target_period))
        seasonal = seasonal_naive(history, actual.target_period)
        if seasonal is not None:
            forecasts.append(seasonal)
        if len(history) >= ar2_min_history:
            forecasts.append(ar2(history, actual.target_period))
    return tuple(forecasts)


def random_walk(history: tuple[FirstPrintTarget, ...], target_period: date) -> BaselineForecast:
    """Forecast the last observed monthly flow."""

    _validate_forecast_boundary(history, target_period)
    return _forecast(RANDOM_WALK_VERSION, history, target_period, history[-1].value)


def seasonal_naive(
    history: tuple[FirstPrintTarget, ...], target_period: date
) -> BaselineForecast | None:
    """Forecast the flow from the same calendar month one year earlier."""

    _validate_forecast_boundary(history, target_period)
    seasonal_period = date(target_period.year - 1, target_period.month, 1)
    seasonal_period = _month_end(seasonal_period)
    lookup = {row.target_period: row for row in history}
    seasonal = lookup.get(seasonal_period)
    if seasonal is None:
        return None
    return _forecast(SEASONAL_NAIVE_VERSION, history, target_period, seasonal.value)


def ar2(history: tuple[FirstPrintTarget, ...], target_period: date) -> BaselineForecast:
    """Fit an intercept plus two lags by deterministic ordinary least squares."""

    _validate_forecast_boundary(history, target_period)
    if len(history) < 4:
        raise BaselineModelError("AR(2) requires at least four monthly observations")
    values = np.asarray([row.value for row in history], dtype=np.float64)
    design = np.column_stack(
        (
            np.ones(len(values) - 2, dtype=np.float64),
            values[1:-1],
            values[:-2],
        )
    )
    response = values[2:]
    coefficients, _residuals, rank, _singular_values = np.linalg.lstsq(design, response, rcond=None)
    if rank < 3:
        raise BaselineModelError("AR(2) design matrix is rank deficient")
    point = float(coefficients @ np.asarray([1.0, values[-1], values[-2]]))
    if not isfinite(point):
        raise BaselineModelError("AR(2) produced a non-finite forecast")
    return _forecast(AR2_VERSION, history, target_period, point)


def _forecast(
    model_version: str,
    history: tuple[FirstPrintTarget, ...],
    target_period: date,
    point: float,
) -> BaselineForecast:
    if not isfinite(point):
        raise BaselineModelError(f"{model_version} produced a non-finite forecast")
    return BaselineForecast(
        model_version=model_version,
        target_series=history[-1].target_series,
        target_period=target_period,
        point=point,
        training_observations=len(history),
        inputs_hash=_inputs_hash(model_version, history, target_period),
    )


def _validate_forecast_boundary(history: tuple[FirstPrintTarget, ...], target_period: date) -> None:
    _validate_history(history)
    if not history:
        raise BaselineModelError("Baseline forecast requires non-empty history")
    if _next_month_end(history[-1].target_period) != target_period:
        raise BaselineModelError("Baseline target must immediately follow the training history")


def _validate_history(history: tuple[FirstPrintTarget, ...]) -> None:
    if not history:
        return
    series = {row.target_series for row in history}
    if len(series) != 1:
        raise BaselineModelError("Baseline history mixes target series")
    for row in history:
        if not isfinite(row.value):
            raise BaselineModelError("Baseline history contains a non-finite value")
    for previous, current in pairwise(history):
        if _next_month_end(previous.target_period) != current.target_period:
            raise BaselineModelError("Baseline history is not continuous monthly data")
        if previous.release_at >= current.release_at:
            raise BaselineModelError("Baseline history releases are not strictly increasing")


def _inputs_hash(
    model_version: str,
    history: tuple[FirstPrintTarget, ...],
    target_period: date,
) -> str:
    payload = {
        "model_version": model_version,
        "target_period": target_period.isoformat(),
        "history": [
            {
                "target_period": row.target_period.isoformat(),
                "value": row.value,
                "release_at": row.release_at.isoformat(),
                "checksum": row.checksum,
            }
            for row in history
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _next_month_end(period: date) -> date:
    year = period.year + int(period.month == 12)
    month = 1 if period.month == 12 else period.month + 1
    return _month_end(date(year, month, 1))


def _month_end(period: date) -> date:
    year = period.year + int(period.month == 12)
    month = 1 if period.month == 12 else period.month + 1
    return date(year, month, 1) - date.resolution
