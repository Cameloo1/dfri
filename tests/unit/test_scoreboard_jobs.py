from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

import dfri.scoreboard as scoreboard
from dfri.lake.store import AppendOnlyParquetStore
from dfri.nowcast.bridge import BRIDGE_VERSION, BridgeForecast
from dfri.nowcast.features import BridgeFeature
from dfri.nowcast.targets import FirstPrintTarget
from dfri.publish.ledger import PredictionLedger

ORIGIN = datetime(2026, 7, 31, 20, 15, tzinfo=UTC)


def target_history(series: str, *, include_june: bool = False) -> tuple[FirstPrintTarget, ...]:
    targets: list[FirstPrintTarget] = []
    year = 2023
    month = 6
    count = 37 if include_june else 36
    for index in range(count):
        period = month_end(year, month)
        release_at = datetime.combine(period + timedelta(days=36), datetime.min.time(), UTC)
        targets.append(
            FirstPrintTarget(
                target_series=series,
                level_series="DTCTLR.M" if series.endswith("R.M") else "DTCTLN.M",
                target_period=period,
                value=10_000.0 + index,
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
    return tuple(targets)


def month_end(year: int, month: int) -> date:
    next_year = year + int(month == 12)
    next_month = 1 if month == 12 else month + 1
    return date(next_year, next_month, 1) - date.resolution


def feature(series: str, period: date, as_of: datetime) -> BridgeFeature:
    return BridgeFeature(
        target_series=series,
        target_period=period,
        as_of=as_of,
        h8_series="B1247NCBA" if series.endswith("R.M") else "B3248NCBA",
        h8_change_sum=100.0,
        h8_paced_change=125.0,
        h8_weeks_observed=4,
        h8_weeks_expected=5,
        h8_coverage=0.8,
        latest_h8_release_at=ORIGIN,
        retail_change=None,
        retail_release_at=None,
        inputs_hash="a" * 64,
        latest_h8_observation_period=date(2026, 7, 22),
    )


def patch_prediction_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        scoreboard,
        "read_first_print_targets",
        lambda _guard, series, _as_of, start: target_history(series),
    )
    monkeypatch.setattr(
        scoreboard,
        "historical_bridge_features",
        lambda _guard, targets, start: tuple(object() for _ in targets),
    )
    monkeypatch.setattr(
        scoreboard,
        "build_bridge_feature",
        lambda _guard, series, period, as_of: feature(series, period, as_of),
    )

    def fake_fit(
        targets: tuple[FirstPrintTarget, ...],
        _features: tuple[object, ...],
        forecast_feature: BridgeFeature,
    ) -> BridgeForecast:
        return BridgeForecast(
            model_version=BRIDGE_VERSION,
            target_series=forecast_feature.target_series,
            target_period=forecast_feature.target_period,
            made_at=forecast_feature.as_of,
            point=10_100.0,
            low80=9_000.0,
            high80=11_000.0,
            low95=8_000.0,
            high95=12_000.0,
            training_observations=len(targets),
            inputs_hash=forecast_feature.inputs_hash,
        )

    monkeypatch.setattr(scoreboard, "fit_bridge", fake_fit)


def test_prediction_job_forecasts_all_unreleased_months_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_prediction_inputs(monkeypatch)
    raw = AppendOnlyParquetStore(tmp_path / "raw")
    curated = AppendOnlyParquetStore(tmp_path / "curated")

    first = scoreboard.run_prediction_job(raw, curated, as_of=datetime(2026, 8, 4, tzinfo=UTC))
    second = scoreboard.run_prediction_job(raw, curated, as_of=datetime(2026, 8, 4, 1, tzinfo=UTC))

    assert first.appended == 4
    assert first.already_present == 0
    assert second.appended == 0
    assert second.already_present == 4
    assert all(
        item.target_periods == (date(2026, 6, 30), date(2026, 7, 31)) for item in first.series
    )
    records = PredictionLedger(curated).read_all()
    assert len(records) == 4
    assert {item.made_at for item in records} == {datetime(2026, 8, 4, tzinfo=UTC)}


def test_grading_job_uses_new_first_prints_and_verifies_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_prediction_inputs(monkeypatch)
    raw = AppendOnlyParquetStore(tmp_path / "raw")
    curated = AppendOnlyParquetStore(tmp_path / "curated")
    scoreboard.run_prediction_job(raw, curated, as_of=datetime(2026, 8, 4, tzinfo=UTC))
    monkeypatch.setattr(
        scoreboard,
        "read_first_print_targets",
        lambda _guard, series, _as_of, start: target_history(series, include_june=True),
    )
    as_of = datetime(2026, 8, 6, tzinfo=UTC)

    result = scoreboard.run_grading_job(raw, curated, as_of=as_of)
    repeat = scoreboard.run_grading_job(raw, curated, as_of=as_of)

    assert result.appended == 2
    assert result.not_matured == 2
    assert result.integrity_verified is True
    assert (
        result.latest_graded_at
        == target_history("DELTA_DTCTLR.M", include_june=True)[-1].release_at
    )
    assert repeat.latest_graded_at is None
    assert repeat.appended == 0


def test_prediction_job_rejects_missing_or_invalid_h8_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_prediction_inputs(monkeypatch)
    raw = AppendOnlyParquetStore(tmp_path / "raw")
    curated = AppendOnlyParquetStore(tmp_path / "curated")
    monkeypatch.setattr(
        scoreboard,
        "build_bridge_feature",
        lambda _guard, series, period, as_of: replace(
            feature(series, period, as_of), latest_h8_release_at=None
        ),
    )
    with pytest.raises(scoreboard.ScoreboardJobError, match=r"No dated H\.8"):
        scoreboard.run_prediction_job(raw, curated, as_of=datetime(2026, 8, 4, tzinfo=UTC))


def test_job_receipts_are_content_addressed_and_preserve_attempts(tmp_path: Path) -> None:
    result = scoreboard.PredictionJobResult(as_of=ORIGIN, series=())
    first = scoreboard.write_job_receipt(
        tmp_path, "predict", result, executed_at=ORIGIN, duration_ms=25
    )
    repeat = scoreboard.write_job_receipt(
        tmp_path, "predict", result, executed_at=ORIGIN, duration_ms=25
    )
    later = scoreboard.write_job_receipt(
        tmp_path,
        "predict",
        result,
        executed_at=ORIGIN + timedelta(seconds=1),
        duration_ms=26,
    )

    assert first == repeat
    assert first != later
    assert len(list(tmp_path.glob("predict-*.json"))) == 2
    with pytest.raises(scoreboard.ScoreboardJobError, match="duration"):
        scoreboard.write_job_receipt(
            tmp_path, "predict", result, executed_at=ORIGIN, duration_ms=-1
        )
