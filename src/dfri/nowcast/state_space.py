"""Mixed-frequency state-space nowcast using statsmodels' Kalman filter."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from math import isclose, isfinite
from typing import Final

import numpy as np
import statsmodels
from statsmodels.tsa.statespace.kalman_filter import FilterResults, KalmanFilter

from dfri.nowcast.features import BridgeFeature
from dfri.nowcast.targets import FirstPrintTarget

STATE_SPACE_VERSION: Final = f"mixed-frequency-kalman-v1-sm{statsmodels.__version__}"
DEFAULT_MIN_HISTORY: Final = 36
TRANSITION_RIDGE: Final = 1e-6
MAX_ABS_PHI: Final = 0.98
Z80: Final = 1.2815515655446004
Z95: Final = 1.959963984540054
_WEEKLY_CHANNELS: Final = 5
_RETAIL_CHANNEL: Final = 6
_K_ENDOG: Final = 7


class StateSpaceModelError(RuntimeError):
    """Mixed-frequency inputs or filtered outputs violate the model contract."""


@dataclass(frozen=True)
class StateSpaceForecast:
    model_version: str
    target_series: str
    target_period: date
    made_at: datetime
    point: float
    low80: float
    high80: float
    low95: float
    high95: float
    training_observations: int
    inputs_hash: str


@dataclass(frozen=True)
class _Scale:
    mean: float
    std: float


@dataclass(frozen=True)
class _Parameters:
    target_scale: _Scale
    h8_scale: _Scale
    retail_scale: _Scale
    phi: float
    month_intercepts: tuple[float, ...]
    state_variance: float
    h8_intercept: float
    h8_loading: float
    h8_variance: float
    retail_intercept: float
    retail_loading: float
    retail_variance: float
    target_variance: float


def fit_state_space(
    targets: tuple[FirstPrintTarget, ...],
    features: tuple[BridgeFeature, ...],
    forecast_feature: BridgeFeature,
    *,
    min_history: int = DEFAULT_MIN_HISTORY,
) -> StateSpaceForecast:
    """Estimate on prior first prints and Kalman-filter one mixed-frequency month."""

    _validate_training(targets, features, forecast_feature, min_history=min_history)
    parameters = _estimate_parameters(targets, features)
    endog = _endog(targets, features, forecast_feature, parameters)
    result = _filter(endog, (*features, forecast_feature), parameters)
    state = float(result.filtered_state[0, -1])
    state_variance = float(result.filtered_state_cov[0, 0, -1])
    point = parameters.target_scale.mean + parameters.target_scale.std * state
    prediction_variance = parameters.target_scale.std**2 * (
        max(state_variance, 0.0) + parameters.target_variance
    )
    prediction_std = float(np.sqrt(max(prediction_variance, 0.0)))
    if not all(isfinite(item) for item in (state, state_variance, point, prediction_std)):
        raise StateSpaceModelError("State-space filter produced a non-finite result")
    return StateSpaceForecast(
        model_version=STATE_SPACE_VERSION,
        target_series=forecast_feature.target_series,
        target_period=forecast_feature.target_period,
        made_at=forecast_feature.as_of,
        point=point,
        low80=point - Z80 * prediction_std,
        high80=point + Z80 * prediction_std,
        low95=point - Z95 * prediction_std,
        high95=point + Z95 * prediction_std,
        training_observations=len(targets),
        inputs_hash=_inputs_hash(targets, features, forecast_feature),
    )


def expanding_window_state_space(
    targets: tuple[FirstPrintTarget, ...],
    features: tuple[BridgeFeature, ...],
    *,
    start: date,
    min_history: int = DEFAULT_MIN_HISTORY,
) -> tuple[StateSpaceForecast, ...]:
    """Forecast every gradeable month from a strictly prior expanding window."""

    _validate_alignment(targets, features)
    if min_history < 24:
        raise StateSpaceModelError("State-space minimum history must be at least 24 months")
    forecasts: list[StateSpaceForecast] = []
    for index, target in enumerate(targets):
        if target.target_period < start or index < min_history:
            continue
        forecasts.append(
            fit_state_space(
                targets[:index],
                features[:index],
                features[index],
                min_history=min_history,
            )
        )
    return tuple(forecasts)


def _estimate_parameters(
    targets: tuple[FirstPrintTarget, ...], features: tuple[BridgeFeature, ...]
) -> _Parameters:
    target_values = np.asarray([item.value for item in targets], dtype=np.float64)
    target_scale = _scale(target_values)
    target_standard = (target_values - target_scale.mean) / target_scale.std

    transition_design = np.asarray(
        [
            [
                1.0,
                target_standard[index - 1],
                *[float(target.target_period.month == month) for month in range(2, 13)],
            ]
            for index, target in enumerate(targets[1:], start=1)
        ],
        dtype=np.float64,
    )
    transition_response = target_standard[1:]
    transition = _ridge(transition_design, transition_response)
    phi = float(np.clip(transition[1], -MAX_ABS_PHI, MAX_ABS_PHI))
    month_intercepts = tuple(
        [float(transition[0])]
        + [float(transition[0] + transition[index]) for index in range(2, 13)]
    )
    transition_fitted = (
        np.asarray(
            [month_intercepts[target.target_period.month - 1] for target in targets[1:]],
            dtype=np.float64,
        )
        + phi * target_standard[:-1]
    )
    state_variance = _variance_floor(transition_response - transition_fitted)

    h8_values = np.asarray(
        [weekly.change for feature in features for weekly in feature.h8_weekly_changes],
        dtype=np.float64,
    )
    h8_targets = np.asarray(
        [
            target_standard[index]
            for index, feature in enumerate(features)
            for _weekly in feature.h8_weekly_changes
        ],
        dtype=np.float64,
    )
    if len(h8_values) < len(targets):
        raise StateSpaceModelError("State-space history has too few weekly H.8 observations")
    h8_scale = _scale(h8_values)
    h8_standard = (h8_values - h8_scale.mean) / h8_scale.std
    h8_measurement = _ridge(
        np.column_stack((np.ones(len(h8_targets), dtype=np.float64), h8_targets)),
        h8_standard,
    )
    h8_residuals = h8_standard - (h8_measurement[0] + h8_measurement[1] * h8_targets)

    retail_pairs = [
        (target_standard[index], feature.retail_change)
        for index, feature in enumerate(features)
        if feature.retail_change is not None
    ]
    if len(retail_pairs) < 24:
        raise StateSpaceModelError("State-space history has fewer than 24 retail observations")
    retail_values = np.asarray([float(value) for _target, value in retail_pairs], dtype=np.float64)
    retail_targets = np.asarray([target for target, _value in retail_pairs], dtype=np.float64)
    retail_scale = _scale(retail_values)
    retail_standard = (retail_values - retail_scale.mean) / retail_scale.std
    retail_measurement = _ridge(
        np.column_stack((np.ones(len(retail_targets), dtype=np.float64), retail_targets)),
        retail_standard,
    )
    retail_residuals = retail_standard - (
        retail_measurement[0] + retail_measurement[1] * retail_targets
    )
    return _Parameters(
        target_scale=target_scale,
        h8_scale=h8_scale,
        retail_scale=retail_scale,
        phi=phi,
        month_intercepts=month_intercepts,
        state_variance=state_variance,
        h8_intercept=float(h8_measurement[0]),
        h8_loading=float(h8_measurement[1]),
        h8_variance=_variance_floor(h8_residuals),
        retail_intercept=float(retail_measurement[0]),
        retail_loading=float(retail_measurement[1]),
        retail_variance=_variance_floor(retail_residuals),
        target_variance=max(state_variance * 1e-6, 1e-8),
    )


def _endog(
    targets: tuple[FirstPrintTarget, ...],
    features: tuple[BridgeFeature, ...],
    forecast: BridgeFeature,
    parameters: _Parameters,
) -> np.ndarray:
    rows = np.full((len(targets) + 1, _K_ENDOG), np.nan, dtype=np.float64)
    for index, (target, feature) in enumerate(zip(targets, features, strict=True)):
        rows[index, 0] = (target.value - parameters.target_scale.mean) / parameters.target_scale.std
        _put_feature_observations(rows[index], feature, parameters)
    _put_feature_observations(rows[-1], forecast, parameters)
    return rows


def _put_feature_observations(
    row: np.ndarray, feature: BridgeFeature, parameters: _Parameters
) -> None:
    for index, weekly in enumerate(feature.h8_weekly_changes):
        row[index + 1] = (weekly.change - parameters.h8_scale.mean) / parameters.h8_scale.std
    if feature.retail_change is not None:
        row[_RETAIL_CHANNEL] = (
            feature.retail_change - parameters.retail_scale.mean
        ) / parameters.retail_scale.std


def _filter(
    endog: np.ndarray, features: tuple[BridgeFeature, ...], parameters: _Parameters
) -> FilterResults:
    model = KalmanFilter(k_endog=_K_ENDOG, k_states=1, k_posdef=1)
    model.bind(np.ascontiguousarray(endog))
    design = np.zeros((_K_ENDOG, 1), dtype=np.float64)
    design[0, 0] = 1.0
    design[1 : _WEEKLY_CHANNELS + 1, 0] = parameters.h8_loading
    design[_RETAIL_CHANNEL, 0] = parameters.retail_loading
    model.design = design
    model.obs_intercept = np.asarray(
        [0.0, *([parameters.h8_intercept] * _WEEKLY_CHANNELS), parameters.retail_intercept],
        dtype=np.float64,
    )[:, None]
    model.obs_cov = np.diag(
        [
            parameters.target_variance,
            *([parameters.h8_variance] * _WEEKLY_CHANNELS),
            parameters.retail_variance,
        ]
    )
    model.transition = np.asarray([[parameters.phi]], dtype=np.float64)
    model.state_intercept = np.asarray(
        [[parameters.month_intercepts[feature.target_period.month - 1] for feature in features]],
        dtype=np.float64,
    )
    model.selection = np.asarray([[1.0]], dtype=np.float64)
    model.state_cov = np.asarray([[parameters.state_variance]], dtype=np.float64)
    model.initialize_known(np.asarray([0.0]), np.asarray([[1.0]]))
    return model.filter()


def _scale(values: np.ndarray) -> _Scale:
    if len(values) < 2 or not np.all(np.isfinite(values)):
        raise StateSpaceModelError("State-space scale input is insufficient or non-finite")
    mean = float(values.mean())
    std = float(values.std())
    if std <= 1e-12:
        raise StateSpaceModelError("State-space scale input has zero variance")
    return _Scale(mean, std)


def _ridge(design: np.ndarray, response: np.ndarray) -> np.ndarray:
    penalty = np.eye(design.shape[1], dtype=np.float64) * TRANSITION_RIDGE
    penalty[0, 0] = 0.0
    try:
        coefficients = np.linalg.solve(design.T @ design + penalty, design.T @ response)
    except np.linalg.LinAlgError as exc:
        raise StateSpaceModelError("State-space parameter system is singular") from exc
    if not np.all(np.isfinite(coefficients)):
        raise StateSpaceModelError("State-space parameter estimate is non-finite")
    return coefficients


def _variance_floor(residuals: np.ndarray) -> float:
    if not np.all(np.isfinite(residuals)):
        raise StateSpaceModelError("State-space residuals are non-finite")
    return max(float(np.mean(np.square(residuals))), 1e-6)


def _validate_training(
    targets: tuple[FirstPrintTarget, ...],
    features: tuple[BridgeFeature, ...],
    forecast: BridgeFeature,
    *,
    min_history: int,
) -> None:
    _validate_alignment(targets, features)
    if len(targets) < min_history:
        raise StateSpaceModelError(f"State-space model requires at least {min_history} months")
    if targets[-1].target_period >= forecast.target_period:
        raise StateSpaceModelError("State-space forecast target must follow its training history")
    if targets[-1].target_series != forecast.target_series:
        raise StateSpaceModelError("State-space forecast mixes target series")
    _validate_feature(forecast)


def _validate_alignment(
    targets: tuple[FirstPrintTarget, ...], features: tuple[BridgeFeature, ...]
) -> None:
    if not targets or len(targets) != len(features):
        raise StateSpaceModelError("State-space targets and features must be non-empty and aligned")
    series = {item.target_series for item in targets} | {item.target_series for item in features}
    if len(series) != 1:
        raise StateSpaceModelError("State-space training data mixes target series")
    for target, feature in zip(targets, features, strict=True):
        if target.target_period != feature.target_period:
            raise StateSpaceModelError("State-space target and feature periods are misaligned")
        if feature.as_of >= target.release_at:
            raise StateSpaceModelError("State-space feature was made after its target release")
        if not isfinite(target.value):
            raise StateSpaceModelError("State-space target value is non-finite")
        _validate_feature(feature)


def _validate_feature(feature: BridgeFeature) -> None:
    if feature.as_of.tzinfo is None or feature.as_of.utcoffset() is None:
        raise StateSpaceModelError("State-space feature timestamp must be timezone-aware")
    if feature.h8_weeks_expected not in (4, 5):
        raise StateSpaceModelError("State-space expected H.8 week count must be four or five")
    if feature.h8_weeks_observed != len(feature.h8_weekly_changes):
        raise StateSpaceModelError("State-space H.8 weekly count is inconsistent")
    if len(feature.h8_weekly_changes) > _WEEKLY_CHANNELS:
        raise StateSpaceModelError("State-space feature has too many weekly H.8 observations")
    periods = [item.obs_period for item in feature.h8_weekly_changes]
    if periods != sorted(set(periods)):
        raise StateSpaceModelError("State-space H.8 weekly periods are duplicated or unordered")
    if any(
        item.obs_period.year != feature.target_period.year
        or item.obs_period.month != feature.target_period.month
        or item.obs_period.weekday() != 2
        or not isfinite(item.change)
        for item in feature.h8_weekly_changes
    ):
        raise StateSpaceModelError("State-space H.8 weekly observation is invalid")
    if not isclose(
        sum(item.change for item in feature.h8_weekly_changes),
        feature.h8_change_sum,
        rel_tol=1e-12,
        abs_tol=1e-9,
    ):
        raise StateSpaceModelError("State-space H.8 weekly changes do not match their sum")
    if not _is_sha256(feature.inputs_hash):
        raise StateSpaceModelError("State-space feature hash is invalid")


def _inputs_hash(
    targets: tuple[FirstPrintTarget, ...],
    features: tuple[BridgeFeature, ...],
    forecast: BridgeFeature,
) -> str:
    payload = {
        "model_version": STATE_SPACE_VERSION,
        "transition_ridge": TRANSITION_RIDGE,
        "max_abs_phi": MAX_ABS_PHI,
        "training": [
            {
                "target_period": target.target_period.isoformat(),
                "target_value": target.value,
                "target_release": target.release_at.isoformat(),
                "target_checksum": target.checksum,
                "feature_hash": feature.inputs_hash,
            }
            for target, feature in zip(targets, features, strict=True)
        ],
        "forecast_feature_hash": forecast.inputs_hash,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
