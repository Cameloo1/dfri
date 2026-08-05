"""Deterministic ridge bridge regression for monthly first-print credit flows."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite
from typing import Final

import numpy as np

from dfri.nowcast.features import BridgeFeature
from dfri.nowcast.targets import FirstPrintTarget

BRIDGE_VERSION: Final = "bridge-ridge-v2-alpha10"
DEFAULT_ALPHA: Final = 10.0
DEFAULT_MIN_HISTORY: Final = 36
Z80: Final = 1.2815515655446004
Z95: Final = 1.959963984540054


class BridgeModelError(RuntimeError):
    """Bridge training data or fitted output violates its deterministic contract."""


@dataclass(frozen=True)
class BridgeForecast:
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
class _FeatureScale:
    h8_mean: float
    h8_std: float
    retail_mean: float
    retail_std: float


def fit_bridge(
    targets: tuple[FirstPrintTarget, ...],
    features: tuple[BridgeFeature, ...],
    forecast_feature: BridgeFeature,
    *,
    alpha: float = DEFAULT_ALPHA,
    min_history: int = DEFAULT_MIN_HISTORY,
) -> BridgeForecast:
    """Fit on prior first prints and forecast one point-in-time feature row."""

    _validate_training(targets, features, forecast_feature, min_history=min_history)
    if not isfinite(alpha) or alpha <= 0:
        raise BridgeModelError("Bridge ridge alpha must be positive and finite")
    scale = _feature_scale(features)
    design = np.asarray([_design_row(item, scale) for item in features], dtype=np.float64)
    response = np.asarray([item.value for item in targets], dtype=np.float64)
    forecast_row = np.asarray(_design_row(forecast_feature, scale), dtype=np.float64)
    penalty = np.eye(design.shape[1], dtype=np.float64) * alpha
    penalty[0, 0] = 0.0
    normal = design.T @ design + penalty
    try:
        inverse = np.linalg.inv(normal)
        coefficients = inverse @ design.T @ response
    except np.linalg.LinAlgError as exc:
        raise BridgeModelError("Bridge normal matrix is singular") from exc
    point = float(forecast_row @ coefficients)
    residuals = response - design @ coefficients
    rank = int(np.linalg.matrix_rank(design))
    degrees = max(len(response) - rank, 1)
    residual_variance = float(residuals @ residuals / degrees)
    leverage = float(forecast_row @ inverse @ forecast_row)
    prediction_std = float(np.sqrt(max(residual_variance * (1.0 + leverage), 0.0)))
    if not all(isfinite(item) for item in (point, residual_variance, leverage, prediction_std)):
        raise BridgeModelError("Bridge regression produced a non-finite result")
    model_version = _model_version(alpha)
    inputs_hash = _inputs_hash(targets, features, forecast_feature, alpha, model_version)
    return BridgeForecast(
        model_version=model_version,
        target_series=forecast_feature.target_series,
        target_period=forecast_feature.target_period,
        made_at=forecast_feature.as_of,
        point=point,
        low80=point - Z80 * prediction_std,
        high80=point + Z80 * prediction_std,
        low95=point - Z95 * prediction_std,
        high95=point + Z95 * prediction_std,
        training_observations=len(targets),
        inputs_hash=inputs_hash,
    )


def expanding_window_bridge(
    targets: tuple[FirstPrintTarget, ...],
    features: tuple[BridgeFeature, ...],
    *,
    start: date,
    alpha: float = DEFAULT_ALPHA,
    min_history: int = DEFAULT_MIN_HISTORY,
) -> tuple[BridgeForecast, ...]:
    """Forecast every gradeable target from ``start`` with a strictly prior expanding window."""

    _validate_alignment(targets, features)
    if min_history < 24:
        raise BridgeModelError("Bridge minimum history must be at least 24 months")
    forecasts: list[BridgeForecast] = []
    for index, target in enumerate(targets):
        if target.target_period < start or index < min_history:
            continue
        forecasts.append(
            fit_bridge(
                targets[:index],
                features[:index],
                features[index],
                alpha=alpha,
                min_history=min_history,
            )
        )
    return tuple(forecasts)


def _validate_training(
    targets: tuple[FirstPrintTarget, ...],
    features: tuple[BridgeFeature, ...],
    forecast: BridgeFeature,
    *,
    min_history: int,
) -> None:
    _validate_alignment(targets, features)
    if len(targets) < min_history:
        raise BridgeModelError(f"Bridge requires at least {min_history} training months")
    if targets[-1].target_period >= forecast.target_period:
        raise BridgeModelError("Bridge forecast target must follow every training target")
    if targets[-1].target_series != forecast.target_series:
        raise BridgeModelError("Bridge forecast mixes target series")
    if forecast.as_of.tzinfo is None or forecast.as_of.utcoffset() is None:
        raise BridgeModelError("Bridge forecast timestamp must be timezone-aware")


def _validate_alignment(
    targets: tuple[FirstPrintTarget, ...],
    features: tuple[BridgeFeature, ...],
) -> None:
    if not targets or len(targets) != len(features):
        raise BridgeModelError("Bridge targets and features must be non-empty and aligned")
    series = {target.target_series for target in targets} | {
        feature.target_series for feature in features
    }
    if len(series) != 1:
        raise BridgeModelError("Bridge training data mixes target series")
    for target, feature in zip(targets, features, strict=True):
        if target.target_period != feature.target_period:
            raise BridgeModelError("Bridge target and feature periods are misaligned")
        if feature.as_of >= target.release_at:
            raise BridgeModelError("Bridge training feature was made after its target release")
        if not isfinite(target.value):
            raise BridgeModelError("Bridge target value must be finite")
        if not 0 <= feature.h8_coverage <= 1:
            raise BridgeModelError("Bridge H.8 coverage is outside [0, 1]")
        if not _is_sha256(feature.inputs_hash):
            raise BridgeModelError("Bridge feature hash is invalid")


def _feature_scale(features: tuple[BridgeFeature, ...]) -> _FeatureScale:
    h8 = [item.h8_paced_change for item in features if item.h8_paced_change is not None]
    retail = [item.retail_change for item in features if item.retail_change is not None]
    h8_mean, h8_std = _mean_std(h8)
    retail_mean, retail_std = _mean_std(retail)
    return _FeatureScale(h8_mean, h8_std, retail_mean, retail_std)


def _mean_std(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 1.0
    array = np.asarray(values, dtype=np.float64)
    mean = float(array.mean())
    std = float(array.std())
    if not isfinite(mean) or not isfinite(std):
        raise BridgeModelError("Bridge feature scale is non-finite")
    return mean, std if std > 1e-12 else 1.0


def _design_row(feature: BridgeFeature, scale: _FeatureScale) -> list[float]:
    h8_value = feature.h8_paced_change
    retail_value = feature.retail_change
    h8_standard = (
        (h8_value if h8_value is not None else scale.h8_mean) - scale.h8_mean
    ) / scale.h8_std
    retail_standard = (
        (retail_value if retail_value is not None else scale.retail_mean) - scale.retail_mean
    ) / scale.retail_std
    return [
        1.0,
        h8_standard,
        feature.h8_coverage,
        retail_standard,
        float(retail_value is not None),
        *[float(feature.target_period.month == month) for month in range(2, 13)],
    ]


def _inputs_hash(
    targets: tuple[FirstPrintTarget, ...],
    features: tuple[BridgeFeature, ...],
    forecast: BridgeFeature,
    alpha: float,
    model_version: str,
) -> str:
    payload = {
        "model_version": model_version,
        "alpha": alpha,
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


def _model_version(alpha: float) -> str:
    return f"bridge-ridge-v2-alpha{alpha:g}"
