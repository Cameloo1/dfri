"""Live-grade calibration metrics kept separate from backtest results."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from math import fsum, isclose
from typing import cast

from dfri.nowcast.backtest import NAIVE_VERSIONS
from dfri.nowcast.baselines import (
    AR2_VERSION,
    RANDOM_WALK_VERSION,
    SEASONAL_NAIVE_VERSION,
    BaselineForecast,
    ar2,
    random_walk,
    seasonal_naive,
)
from dfri.nowcast.mts import (
    MTS_AR2_VERSION,
    MTS_RANDOM_WALK_VERSION,
    MTS_SEASONAL_VERSION,
)
from dfri.nowcast.mts import (
    point_forecast as mts_point_forecast,
)
from dfri.nowcast.targets import FirstPrintTarget
from dfri.publish.ledger import GradeRecord, PredictionRecord


class LiveCalibrationError(RuntimeError):
    """Live calibration inputs cannot prove a point-in-time comparison."""


@dataclass(frozen=True)
class LiveGradeComparison:
    prediction_id: str
    within80: bool
    within95: bool
    abs_error: float
    naive_model_version: str
    naive_point: float
    naive_abs_error: float


@dataclass(frozen=True)
class LiveCalibration:
    comparisons: tuple[LiveGradeComparison, ...]
    naive_model_versions: Mapping[str, str]

    @property
    def graded_count(self) -> int:
        return len(self.comparisons)

    @property
    def within80_count(self) -> int:
        return sum(item.within80 for item in self.comparisons)

    @property
    def within95_count(self) -> int:
        return sum(item.within95 for item in self.comparisons)

    @property
    def coverage80(self) -> float | None:
        return self.within80_count / self.graded_count if self.graded_count else None

    @property
    def coverage95(self) -> float | None:
        return self.within95_count / self.graded_count if self.graded_count else None

    @property
    def mae(self) -> float | None:
        if not self.comparisons:
            return None
        return fsum(item.abs_error for item in self.comparisons) / self.graded_count

    @property
    def naive_mae(self) -> float | None:
        if not self.comparisons:
            return None
        return fsum(item.naive_abs_error for item in self.comparisons) / self.graded_count

    def feed(self) -> dict[str, object]:
        mae = self.mae
        naive_mae = self.naive_mae
        return {
            "scope": "live_grades_only",
            "graded_count": self.graded_count,
            "within80_count": self.within80_count,
            "coverage80": self.coverage80,
            "nominal80": 0.80,
            "within95_count": self.within95_count,
            "coverage95": self.coverage95,
            "nominal95": 0.95,
            "mae": mae,
            "naive_mae": naive_mae,
            "mae_difference_vs_naive": (
                mae - naive_mae if mae is not None and naive_mae is not None else None
            ),
            "naive_model_versions": dict(sorted(self.naive_model_versions.items())),
        }


def calculate_live_calibration(
    predictions: Sequence[PredictionRecord],
    grades: Sequence[GradeRecord],
    target_histories: Mapping[str, Sequence[FirstPrintTarget]],
    backtest: Mapping[str, object],
) -> LiveCalibration:
    """Compare only live grades with their prediction-time best-naive forecasts."""

    prediction_by_id = {item.prediction_id: item for item in predictions}
    if len(prediction_by_id) != len(predictions):
        raise LiveCalibrationError("Live calibration predictions must have unique IDs")
    naive_versions = _best_naive_versions(backtest)
    comparisons: list[LiveGradeComparison] = []
    for grade in sorted(grades, key=lambda item: (item.graded_at, item.prediction_id)):
        prediction = prediction_by_id.get(grade.prediction_id)
        if prediction is None:
            raise LiveCalibrationError(f"Grade has no prediction: {grade.prediction_id}")
        history = tuple(target_histories.get(prediction.target_series, ()))
        actuals = [item for item in history if item.target_period == prediction.target_period]
        if len(actuals) != 1:
            raise LiveCalibrationError(
                f"Live grade target is not unique in first-print history: {grade.prediction_id}"
            )
        actual = actuals[0]
        if not isclose(actual.value, grade.actual_first_print, rel_tol=0.0, abs_tol=1e-9):
            raise LiveCalibrationError(
                f"Live grade differs from first-print history: {grade.prediction_id}"
            )
        if actual.source_url != grade.vintage_url:
            raise LiveCalibrationError(
                f"Live grade vintage differs from first-print history: {grade.prediction_id}"
            )
        if actual.release_at <= prediction.made_at:
            raise LiveCalibrationError(
                f"Live grade was already released when predicted: {grade.prediction_id}"
            )
        expected_abs_error = abs(prediction.point - grade.actual_first_print)
        if not isclose(expected_abs_error, grade.abs_error, rel_tol=0.0, abs_tol=1e-9):
            raise LiveCalibrationError(
                f"Live grade absolute error is inconsistent: {grade.prediction_id}"
            )
        prior = tuple(
            item
            for item in history
            if item.target_period < prediction.target_period
            and item.release_at <= prediction.made_at
        )
        version = naive_versions.get(prediction.target_series)
        if version is None:
            raise LiveCalibrationError(
                f"Backtest has no naive comparator for {prediction.target_series}"
            )
        baseline = _forecast(version, prior, prediction.target_period)
        comparisons.append(
            LiveGradeComparison(
                prediction_id=prediction.prediction_id,
                within80=prediction.low80 <= grade.actual_first_print <= prediction.high80,
                within95=prediction.low95 <= grade.actual_first_print <= prediction.high95,
                abs_error=grade.abs_error,
                naive_model_version=baseline.model_version,
                naive_point=baseline.point,
                naive_abs_error=abs(baseline.point - grade.actual_first_print),
            )
        )
    used_series = {prediction_by_id[item.prediction_id].target_series for item in comparisons}
    return LiveCalibration(
        comparisons=tuple(comparisons),
        naive_model_versions={item: naive_versions[item] for item in used_series},
    )


def calculate_live_calibration_by_series(
    predictions: Sequence[PredictionRecord],
    grades: Sequence[GradeRecord],
    target_histories: Mapping[str, Sequence[FirstPrintTarget]],
    backtest: Mapping[str, object],
) -> dict[str, LiveCalibration]:
    """Return isolated calibration ledgers; no statistic crosses a target-series boundary."""

    grade_by_prediction = {item.prediction_id: item for item in grades}
    output: dict[str, LiveCalibration] = {}
    for target_series in sorted({item.target_series for item in predictions}):
        series_predictions = tuple(
            item for item in predictions if item.target_series == target_series
        )
        series_grades = tuple(
            grade_by_prediction[item.prediction_id]
            for item in series_predictions
            if item.prediction_id in grade_by_prediction
        )
        output[target_series] = calculate_live_calibration(
            series_predictions,
            series_grades,
            {target_series: target_histories.get(target_series, ())},
            backtest,
        )
    return output


def _best_naive_versions(backtest: Mapping[str, object]) -> dict[str, str]:
    raw_targets = backtest.get("targets")
    if not isinstance(raw_targets, list):
        raise LiveCalibrationError("Backtest targets must be a list")
    versions: dict[str, str] = {}
    for raw_target in raw_targets:
        if not isinstance(raw_target, dict):
            raise LiveCalibrationError("Backtest target must be an object")
        target = cast(Mapping[str, object], raw_target)
        target_series = target.get("target_series")
        metrics = target.get("metrics")
        if not isinstance(target_series, str) or not isinstance(metrics, list):
            raise LiveCalibrationError("Backtest target comparator fields are invalid")
        candidates: list[tuple[float, str]] = []
        for raw_metric in metrics:
            if not isinstance(raw_metric, dict):
                continue
            version = raw_metric.get("model_version")
            mae = raw_metric.get("mae")
            if version in {
                *NAIVE_VERSIONS,
                MTS_RANDOM_WALK_VERSION,
                MTS_SEASONAL_VERSION,
                MTS_AR2_VERSION,
            } and isinstance(mae, (int, float)):
                candidates.append((float(mae), cast(str, version)))
        if not candidates:
            raise LiveCalibrationError(f"Backtest has no naive metrics for {target_series}")
        versions[target_series] = min(candidates)[1]
    return versions


def _forecast(
    model_version: str,
    history: tuple[FirstPrintTarget, ...],
    target_period: date,
) -> BaselineForecast:
    if model_version == RANDOM_WALK_VERSION:
        return random_walk(history, target_period)
    if model_version == SEASONAL_NAIVE_VERSION:
        forecast = seasonal_naive(history, target_period)
        if forecast is None:
            raise LiveCalibrationError("Seasonal naive comparator is unavailable")
        return forecast
    if model_version == AR2_VERSION:
        return ar2(history, target_period)
    if model_version in {MTS_RANDOM_WALK_VERSION, MTS_SEASONAL_VERSION, MTS_AR2_VERSION}:
        result = mts_point_forecast(model_version, history, target_period)
        if result is None:
            raise LiveCalibrationError("MTS naive comparator is unavailable")
        return BaselineForecast(
            model_version=result.model_version,
            target_series=result.target_series,
            target_period=result.target_period,
            point=result.point,
            training_observations=len(history),
            inputs_hash=result.inputs_hash,
        )
    raise LiveCalibrationError(f"Unsupported naive comparator: {model_version}")
