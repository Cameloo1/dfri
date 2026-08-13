"""Vintage-safe benchmark selection and forecasting for Treasury MTS targets."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from itertools import pairwise
from math import isfinite, sqrt
from typing import Final, cast

import numpy as np
import polars as pl

from dfri.ingest.registry import TreasuryMtsDefinition, load_treasury_mts
from dfri.lake.guard import VintageGuard
from dfri.nowcast.targets import FirstPrintTarget

MTS_RANDOM_WALK_VERSION: Final = "mts-naive-random-walk-v1"
MTS_SEASONAL_VERSION: Final = "mts-naive-seasonal-v1"
MTS_AR2_VERSION: Final = "mts-ar2-ols-v1"
MTS_FORECAST_VERSION: Final = "mts-benchmark-empirical-v1"
MTS_BACKTEST_VERSION: Final = "mts-point-in-time-backtest-v1"
MTS_BACKTEST_START: Final = date(2018, 1, 31)


class MtsModelError(RuntimeError):
    """MTS history or benchmark output violates a point-in-time contract."""


@dataclass(frozen=True)
class MtsPointForecast:
    model_version: str
    target_series: str
    target_period: date
    point: float
    inputs_hash: str


@dataclass(frozen=True)
class MtsForecast:
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


def read_mts_first_print_targets(
    guard: VintageGuard,
    target_series: str,
    as_of: date | datetime,
    *,
    start: date | None = None,
    definition: TreasuryMtsDefinition | None = None,
) -> tuple[FirstPrintTarget, ...]:
    """Read verified MTS issues; known schedule gaps remain explicit and are not filled."""

    definition = definition or load_treasury_mts()
    target_by_id = {item.target_series_id: item for item in definition.targets}
    target = target_by_id.get(target_series)
    if target is None:
        raise MtsModelError(f"Unsupported MTS target series: {target_series}")
    frame = guard.read(target_series, as_of).filter(
        (pl.col("source") == definition.source) & (pl.col("series_id") == target_series)
    )
    if frame.is_empty():
        return ()
    frame = frame.sort(["obs_period", "release_date", "checksum"]).unique(
        subset=["obs_period", "value", "release_date", "source_url", "checksum"],
        keep="first",
        maintain_order=True,
    )
    if frame["obs_period"].n_unique() != frame.height:
        raise MtsModelError("An MTS target month has multiple first-print rows")
    archive_pattern = re.compile(
        r"^https://fiscaldata\.treasury\.gov/static-data/published-reports/mts/"
        r"MonthlyTreasuryStatement_\d{6}\.pdf$"
    )
    output: list[FirstPrintTarget] = []
    for row in frame.iter_rows(named=True):
        period = row["obs_period"]
        release_at = row["release_date"]
        value = float(row["value"])
        if not isinstance(period, date) or isinstance(period, datetime):
            raise MtsModelError("MTS period must be a date")
        if not isinstance(release_at, datetime) or release_at.tzinfo is None:
            raise MtsModelError("MTS release must be timezone-aware")
        if release_at.date() != definition.release_schedule.get(period):
            raise MtsModelError(f"MTS release timestamp is not pinned for {period}")
        if period >= release_at.date() or not isfinite(value):
            raise MtsModelError("MTS first-print value or release boundary is invalid")
        if row["unit"] != target.unit:
            raise MtsModelError(f"MTS target unit changed: {row['unit']!r}")
        source_url = str(row["source_url"])
        checksum = str(row["checksum"])
        if archive_pattern.fullmatch(source_url) is None:
            raise MtsModelError("MTS vintage is not a dated Treasury issue PDF")
        if re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
            raise MtsModelError("MTS vintage checksum is not lowercase SHA-256")
        output.append(
            FirstPrintTarget(
                target_series=target_series,
                level_series=target_series,
                target_period=period,
                value=value,
                unit=target.unit,
                release_at=release_at,
                vintage_date=row["vintage_date"],
                source_url=source_url,
                checksum=checksum,
            )
        )
    if any(a.release_at >= b.release_at for a, b in pairwise(output)):
        raise MtsModelError("MTS releases are not strictly increasing")
    if start is not None:
        output = [item for item in output if item.target_period >= start]
    return tuple(output)


def point_forecast(
    model_version: str,
    history: tuple[FirstPrintTarget, ...],
    target_period: date,
) -> MtsPointForecast | None:
    """Fit one benchmark using only target values already released."""

    _validate_history(history, target_period)
    lookup = {item.target_period: item.value for item in history}
    if model_version == MTS_RANDOM_WALK_VERSION:
        point = history[-1].value
    elif model_version == MTS_SEASONAL_VERSION:
        prior = _month_end(date(target_period.year - 1, target_period.month, 1))
        if prior not in lookup:
            return None
        point = lookup[prior]
    elif model_version == MTS_AR2_VERSION:
        prior1 = _previous_month_end(target_period)
        prior2 = _previous_month_end(prior1)
        if prior1 not in lookup or prior2 not in lookup:
            return None
        samples: list[tuple[float, float, float]] = []
        for current in history:
            lag1 = lookup.get(_previous_month_end(current.target_period))
            lag2 = lookup.get(_previous_month_end(_previous_month_end(current.target_period)))
            if lag1 is not None and lag2 is not None:
                samples.append((current.value, lag1, lag2))
        if len(samples) < 24:
            return None
        design = np.asarray([[1.0, lag1, lag2] for _, lag1, lag2 in samples])
        response = np.asarray([value for value, _, _ in samples])
        coefficients, _residuals, rank, _singular = np.linalg.lstsq(design, response, rcond=None)
        if rank < 3:
            return None
        point = float(coefficients @ np.asarray([1.0, lookup[prior1], lookup[prior2]]))
    else:
        raise MtsModelError(f"Unsupported MTS benchmark: {model_version}")
    if not isfinite(point):
        raise MtsModelError("MTS benchmark returned a non-finite point")
    return MtsPointForecast(
        model_version=model_version,
        target_series=history[-1].target_series,
        target_period=target_period,
        point=point,
        inputs_hash=_inputs_hash(model_version, history, target_period),
    )


def run_mts_backtest(
    histories: dict[str, tuple[FirstPrintTarget, ...]],
    *,
    as_of: datetime,
    start: date = MTS_BACKTEST_START,
) -> dict[str, object]:
    """Compare all prescribed benchmarks over their vintage-safe eligible months."""

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise MtsModelError("MTS backtest as-of must be timezone-aware")
    reports: list[dict[str, object]] = []
    for series_id in sorted(histories):
        history = histories[series_id]
        if not history:
            raise MtsModelError(f"No MTS history exists for {series_id}")
        metrics: list[dict[str, object]] = []
        forecasts_by_model: dict[str, list[tuple[MtsPointForecast, FirstPrintTarget]]] = {
            version: []
            for version in (MTS_RANDOM_WALK_VERSION, MTS_SEASONAL_VERSION, MTS_AR2_VERSION)
        }
        for index, actual in enumerate(history):
            if actual.target_period < start or index == 0:
                continue
            prior = history[:index]
            for version in forecasts_by_model:
                forecast = point_forecast(version, prior, actual.target_period)
                if forecast is not None:
                    forecasts_by_model[version].append((forecast, actual))
        for version, rows in forecasts_by_model.items():
            if not rows:
                raise MtsModelError(f"MTS benchmark has no eligible rows: {version}")
            errors = [forecast.point - actual.value for forecast, actual in rows]
            signs = [
                _sign(forecast.point - prior.value) == _sign(actual.value - prior.value)
                for forecast, actual in rows
                for prior in [history[history.index(actual) - 1]]
            ]
            prior_absolute_errors: list[float] = []
            covered80 = 0
            covered95 = 0
            interval_observations = 0
            for forecast, actual in rows:
                absolute_error = abs(forecast.point - actual.value)
                if len(prior_absolute_errors) >= 24:
                    prior_array = np.asarray(prior_absolute_errors)
                    width80 = float(np.quantile(prior_array, 0.80, method="higher"))
                    width95 = float(np.quantile(prior_array, 0.95, method="higher"))
                    interval_observations += 1
                    covered80 += int(absolute_error <= width80)
                    covered95 += int(absolute_error <= width95)
                prior_absolute_errors.append(absolute_error)
            metrics.append(
                {
                    "model_version": version,
                    "observations": len(rows),
                    "period_start": rows[0][1].target_period.isoformat(),
                    "period_end": rows[-1][1].target_period.isoformat(),
                    "mae": sum(abs(item) for item in errors) / len(errors),
                    "rmse": sqrt(sum(item * item for item in errors) / len(errors)),
                    "acceleration_sign_accuracy": sum(signs) / len(signs),
                    "interval_observations": interval_observations,
                    "coverage80": covered80 / interval_observations,
                    "coverage95": covered95 / interval_observations,
                }
            )
        selected = min(metrics, key=lambda item: cast(float, item["mae"]))
        reports.append(
            {
                "target_series": series_id,
                "target_observations": len(history),
                "first_vintage_url": history[0].source_url,
                "last_vintage_url": history[-1].source_url,
                "metrics": metrics,
                "selected_model_version": selected["model_version"],
                "selection_rule": "lowest point-in-time MAE among naive, seasonal, and AR(2)",
                "accuracy_gap_logged": True,
                "accuracy_note": (
                    "The benchmark is published regardless of its absolute accuracy; no "
                    "unproved bridge or state-space candidate displaced it."
                ),
            }
        )
    payload: dict[str, object] = {
        "backtest_version": MTS_BACKTEST_VERSION,
        "as_of": as_of.isoformat(),
        "window_start": start.isoformat(),
        "vintage_policy": (
            "Only release timestamps pinned to an official Treasury schedule are used; "
            "historical September issues without exact dates are omitted, not imputed."
        ),
        "targets": reports,
    }
    payload["report_hash"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return payload


def fit_mts_forecast(
    history: tuple[FirstPrintTarget, ...],
    *,
    target_period: date,
    made_at: datetime,
    backtest: dict[str, object],
) -> MtsForecast:
    """Use the selected benchmark with empirical prior out-of-sample error bands."""

    target_reports = cast(list[dict[str, object]], backtest["targets"])
    report = next(
        (item for item in target_reports if item["target_series"] == history[-1].target_series),
        None,
    )
    if report is None or not isinstance(report.get("selected_model_version"), str):
        raise MtsModelError("MTS backtest has no selected model for the target")
    selected = cast(str, report["selected_model_version"])
    point = point_forecast(selected, history, target_period)
    if point is None:
        raise MtsModelError("Selected MTS benchmark is unavailable for the live target")
    residuals: list[float] = []
    for index, actual in enumerate(history):
        if actual.target_period < MTS_BACKTEST_START or index == 0:
            continue
        prior = history[:index]
        prior_forecast = point_forecast(selected, prior, actual.target_period)
        if prior_forecast is not None:
            residuals.append(abs(prior_forecast.point - actual.value))
    if len(residuals) < 24:
        raise MtsModelError("MTS empirical intervals require at least 24 prior errors")
    width80 = float(np.quantile(np.asarray(residuals), 0.80, method="higher"))
    width95 = float(np.quantile(np.asarray(residuals), 0.95, method="higher"))
    model_version = f"{MTS_FORECAST_VERSION}:{selected}"
    forecast = MtsForecast(
        model_version=model_version,
        target_series=point.target_series,
        target_period=target_period,
        made_at=made_at,
        point=point.point,
        low80=point.point - width80,
        high80=point.point + width80,
        low95=point.point - width95,
        high95=point.point + width95,
        training_observations=len(history),
        inputs_hash=_inputs_hash(model_version, history, target_period),
    )
    return replace(
        forecast,
        low95=min(forecast.low95, forecast.low80),
        high95=max(forecast.high95, forecast.high80),
    )


def _validate_history(history: tuple[FirstPrintTarget, ...], target_period: date) -> None:
    if not history:
        raise MtsModelError("MTS forecast requires history")
    if len({item.target_series for item in history}) != 1:
        raise MtsModelError("MTS history mixes target series")
    if target_period <= history[-1].target_period:
        raise MtsModelError("MTS forecast target must follow the training history")
    if any(a.target_period >= b.target_period for a, b in pairwise(history)):
        raise MtsModelError("MTS target periods are not strictly increasing")
    if any(a.release_at >= b.release_at for a, b in pairwise(history)):
        raise MtsModelError("MTS releases are not strictly increasing")


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
                "period": item.target_period.isoformat(),
                "value": item.value,
                "release_at": item.release_at.isoformat(),
                "checksum": item.checksum,
            }
            for item in history
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _previous_month_end(period: date) -> date:
    first = period.replace(day=1)
    return first - date.resolution


def _month_end(period: date) -> date:
    year = period.year + int(period.month == 12)
    month = 1 if period.month == 12 else period.month + 1
    return date(year, month, 1) - date.resolution


def _sign(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0
