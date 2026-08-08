from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from dfri.nowcast.baselines import AR2_VERSION, ar2
from dfri.nowcast.targets import FirstPrintTarget
from dfri.publish.ledger import GradeRecord, PredictionRecord
from dfri.publish.live_calibration import LiveCalibrationError, calculate_live_calibration


def _history(series: str, values: list[float]) -> tuple[FirstPrintTarget, ...]:
    periods = [
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 31),
        date(2026, 4, 30),
        date(2026, 5, 31),
        date(2026, 6, 30),
    ]
    releases = [
        datetime(2026, 2, 7, 19, tzinfo=UTC),
        datetime(2026, 3, 7, 19, tzinfo=UTC),
        datetime(2026, 4, 7, 19, tzinfo=UTC),
        datetime(2026, 5, 7, 19, tzinfo=UTC),
        datetime(2026, 6, 7, 19, tzinfo=UTC),
        datetime(2026, 8, 7, 19, tzinfo=UTC),
    ]
    return tuple(
        FirstPrintTarget(
            target_series=series,
            level_series="DTCTLR.M" if series.endswith("R.M") else "DTCTLN.M",
            target_period=period,
            value=value,
            unit="Millions of U.S. Dollars",
            release_at=release,
            vintage_date=release.date(),
            source_url=f"https://www.federalreserve.gov/releases/g19/{release:%Y%m%d}/",
            checksum=f"{index:064x}",
        )
        for index, (period, release, value) in enumerate(
            zip(periods, releases, values, strict=True)
        )
    )


def _prediction(identifier: str, series: str, point: float) -> PredictionRecord:
    return PredictionRecord(
        prediction_id=identifier,
        made_at=datetime(2026, 8, 5, 2, 24, tzinfo=UTC),
        model_version="bridge-ridge-v2-alpha10",
        inputs_hash=identifier[-1] * 64,
        target_series=series,
        target_period=date(2026, 6, 30),
        point=point,
        low80=-1_000.0 if point < 0 else 6_000.0,
        high80=1_000.0 if point < 0 else 12_000.0,
        low95=-2_000.0 if point < 0 else 4_000.0,
        high95=2_000.0 if point < 0 else 14_000.0,
    )


def _backtest() -> dict[str, object]:
    return {
        "targets": [
            {
                "target_series": series,
                "metrics": [
                    {"model_version": "naive-random-walk-v1", "mae": 9_000.0},
                    {"model_version": "naive-seasonal-v1", "mae": 8_000.0},
                    {"model_version": AR2_VERSION, "mae": 5_000.0},
                ],
            }
            for series in ("DELTA_DTCTLR.M", "DELTA_DTCTLN.M")
        ]
    }


def test_live_calibration_is_live_only_point_in_time_and_feed_ready() -> None:
    revolving = _history("DELTA_DTCTLR.M", [1_000, 3_000, -2_000, 4_500, 500, 6_800])
    nonrevolving = _history("DELTA_DTCTLN.M", [8_000, 7_000, 9_000, 6_500, 8_500, 7_400])
    predictions = (
        _prediction("prd_" + "a" * 64, "DELTA_DTCTLR.M", -5_529.0),
        _prediction("prd_" + "b" * 64, "DELTA_DTCTLN.M", 9_968.0),
    )
    grades = (
        GradeRecord(
            prediction_id=predictions[0].prediction_id,
            actual_first_print=6_800.0,
            vintage_url=revolving[-1].source_url,
            abs_error=12_329.0,
            graded_at=revolving[-1].release_at,
        ),
        GradeRecord(
            prediction_id=predictions[1].prediction_id,
            actual_first_print=7_400.0,
            vintage_url=nonrevolving[-1].source_url,
            abs_error=2_568.0,
            graded_at=nonrevolving[-1].release_at,
        ),
    )

    result = calculate_live_calibration(
        predictions,
        grades,
        {
            "DELTA_DTCTLR.M": revolving,
            "DELTA_DTCTLN.M": nonrevolving,
        },
        _backtest(),
    )

    expected_naive_errors = [
        abs(ar2(revolving[:-1], date(2026, 6, 30)).point - 6_800.0),
        abs(ar2(nonrevolving[:-1], date(2026, 6, 30)).point - 7_400.0),
    ]
    assert result.graded_count == 2
    assert result.within80_count == 1
    assert result.within95_count == 1
    assert result.coverage80 == 0.5
    assert result.coverage95 == 0.5
    assert result.mae == pytest.approx((12_329.0 + 2_568.0) / 2)
    assert result.naive_mae == pytest.approx(sum(expected_naive_errors) / 2)
    assert result.feed()["scope"] == "live_grades_only"
    assert result.feed()["nominal80"] == 0.8
    assert result.feed()["nominal95"] == 0.95
    assert result.feed()["naive_model_versions"] == {
        "DELTA_DTCTLN.M": AR2_VERSION,
        "DELTA_DTCTLR.M": AR2_VERSION,
    }


def test_live_calibration_rejects_grade_that_differs_from_first_print_history() -> None:
    history = _history("DELTA_DTCTLR.M", [1_000, 3_000, -2_000, 4_500, 500, 6_800])
    prediction = _prediction("prd_" + "a" * 64, "DELTA_DTCTLR.M", -5_529.0)
    grade = GradeRecord(
        prediction_id=prediction.prediction_id,
        actual_first_print=6_801.0,
        vintage_url=history[-1].source_url,
        abs_error=12_330.0,
        graded_at=history[-1].release_at,
    )

    with pytest.raises(LiveCalibrationError, match="differs from first-print history"):
        calculate_live_calibration(
            (prediction,),
            (grade,),
            {"DELTA_DTCTLR.M": history},
            _backtest(),
        )


def test_live_calibration_empty_state_is_explicit_not_fabricated() -> None:
    result = calculate_live_calibration((), (), {}, _backtest())

    assert result.graded_count == 0
    assert result.coverage80 is None
    assert result.coverage95 is None
    assert result.mae is None
    assert result.naive_mae is None
    assert result.feed() == {
        "scope": "live_grades_only",
        "graded_count": 0,
        "within80_count": 0,
        "coverage80": None,
        "nominal80": 0.8,
        "within95_count": 0,
        "coverage95": None,
        "nominal95": 0.95,
        "mae": None,
        "naive_mae": None,
        "mae_difference_vs_naive": None,
        "naive_model_versions": {},
    }


def test_live_calibration_rejects_duplicate_predictions_or_orphan_grades() -> None:
    prediction = _prediction("prd_" + "a" * 64, "DELTA_DTCTLR.M", -5_529.0)
    with pytest.raises(LiveCalibrationError, match="unique IDs"):
        calculate_live_calibration((prediction, prediction), (), {}, _backtest())

    orphan = GradeRecord(
        prediction_id="prd_" + "f" * 64,
        actual_first_print=6_800.0,
        vintage_url="https://www.federalreserve.gov/releases/g19/20260807/",
        abs_error=1.0,
        graded_at=datetime(2026, 8, 7, 19, tzinfo=UTC),
    )
    with pytest.raises(LiveCalibrationError, match="Grade has no prediction"):
        calculate_live_calibration((prediction,), (orphan,), {}, _backtest())


def test_live_calibration_rejects_missing_target_and_invalid_backtest_shapes() -> None:
    history = _history("DELTA_DTCTLR.M", [1_000, 3_000, -2_000, 4_500, 500, 6_800])
    prediction = _prediction("prd_" + "a" * 64, "DELTA_DTCTLR.M", -5_529.0)
    grade = GradeRecord(
        prediction_id=prediction.prediction_id,
        actual_first_print=6_800.0,
        vintage_url=history[-1].source_url,
        abs_error=12_329.0,
        graded_at=history[-1].release_at,
    )
    with pytest.raises(LiveCalibrationError, match="not unique"):
        calculate_live_calibration((prediction,), (grade,), {}, _backtest())

    invalid_reports = [
        ({"targets": "not-a-list"}, "targets must be a list"),
        ({"targets": [None]}, "target must be an object"),
        ({"targets": [{"target_series": 1, "metrics": []}]}, "fields are invalid"),
        (
            {"targets": [{"target_series": "DELTA_DTCTLR.M", "metrics": [None]}]},
            "no naive metrics",
        ),
    ]
    for report, message in invalid_reports:
        with pytest.raises(LiveCalibrationError, match=message):
            calculate_live_calibration((), (), {}, report)
