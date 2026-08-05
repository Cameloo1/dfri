from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

import dfri.backtest as backtest_cli
from dfri.nowcast.backtest import (
    PRIMARY_TARGET,
    BacktestError,
    ForecastValue,
    build_report,
    evaluate_target,
    render_markdown,
    write_report,
)
from dfri.nowcast.baselines import AR2_VERSION, RANDOM_WALK_VERSION, SEASONAL_NAIVE_VERSION
from dfri.nowcast.bridge import BRIDGE_VERSION
from dfri.nowcast.state_space import STATE_SPACE_VERSION
from dfri.nowcast.targets import FirstPrintTarget


@dataclass(frozen=True)
class Scenario:
    targets: tuple[FirstPrintTarget, ...]
    forecasts: tuple[ForecastValue, ...]


def month_end(year: int, month: int) -> date:
    next_year = year + int(month == 12)
    next_month = 1 if month == 12 else month + 1
    return date(next_year, next_month, 1) - date.resolution


def scenario(series: str = PRIMARY_TARGET) -> Scenario:
    targets: list[FirstPrintTarget] = []
    year = 2017
    month = 11
    values = tuple(100.0 + index * 10.0 for index in range(12))
    for index, value in enumerate(values):
        period = month_end(year, month)
        release_at = datetime.combine(period + timedelta(days=10), datetime.min.time(), UTC)
        targets.append(
            FirstPrintTarget(
                target_series=series,
                level_series="DTCTLR.M" if series == PRIMARY_TARGET else "DTCTLN.M",
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
    forecasts: list[ForecastValue] = []
    forecast_targets = tuple(item for item in targets if item.target_period >= date(2018, 1, 1))
    errors = {
        RANDOM_WALK_VERSION: 10.0,
        SEASONAL_NAIVE_VERSION: 20.0,
        AR2_VERSION: 2.0,
        BRIDGE_VERSION: 1.0,
        STATE_SPACE_VERSION: 3.0,
    }
    for model, error in errors.items():
        has_bands = model in (BRIDGE_VERSION, STATE_SPACE_VERSION)
        for index, target in enumerate(forecast_targets):
            point = target.value - error
            width80 = 10.0 if index < 8 else 0.5
            width95 = 20.0 if index < 9 else 0.75
            forecasts.append(
                ForecastValue(
                    model_version=model,
                    target_series=series,
                    target_period=target.target_period,
                    point=point,
                    low80=point - width80 if has_bands else None,
                    high80=point + width80 if has_bands else None,
                    low95=point - width95 if has_bands else None,
                    high95=point + width95 if has_bands else None,
                )
            )
    return Scenario(tuple(targets), tuple(forecasts))


def test_evaluate_target_uses_acceleration_and_comparable_periods() -> None:
    sample = scenario()

    result = evaluate_target(sample.targets, sample.forecasts)
    metrics = {item.model_version: item for item in result.metrics}

    assert metrics[BRIDGE_VERSION].mae == 1.0
    assert metrics[BRIDGE_VERSION].rmse == 1.0
    assert metrics[BRIDGE_VERSION].coverage80 == 0.8
    assert metrics[BRIDGE_VERSION].coverage95 == 0.9
    assert metrics[BRIDGE_VERSION].acceleration_sign_accuracy == 1.0
    assert metrics[AR2_VERSION].coverage80 is None


def test_report_selects_bridge_and_applies_all_primary_bars() -> None:
    revolving = scenario()
    nonrevolving = scenario("DELTA_DTCTLN.M")
    results = (
        evaluate_target(revolving.targets, revolving.forecasts),
        evaluate_target(nonrevolving.targets, nonrevolving.forecasts),
    )

    report = build_report(results, as_of=datetime(2026, 8, 4, 23, 59, tzinfo=UTC))
    headline = report["primary_headline"]
    assert isinstance(headline, dict)
    assert headline["model_version"] == BRIDGE_VERSION
    assert headline["state_space_eligible_under_section_6_2"] is False
    assert headline["mae_improvement_vs_best_naive"] == pytest.approx(0.5)
    assert headline["all_bars_pass"] is True
    assert len(str(report["report_hash"])) == 64


def test_report_render_and_atomic_writes_are_deterministic(tmp_path: Path) -> None:
    revolving = scenario()
    nonrevolving = scenario("DELTA_DTCTLN.M")
    report = build_report(
        (
            evaluate_target(revolving.targets, revolving.forecasts),
            evaluate_target(nonrevolving.targets, nonrevolving.forecasts),
        ),
        as_of=datetime(2026, 8, 4, 23, 59, tzinfo=UTC),
    )
    output = tmp_path / "report.json"
    markdown = tmp_path / "report.md"

    write_report(report, output, markdown)
    first = (output.read_bytes(), markdown.read_bytes())
    write_report(report, output, markdown)

    assert (output.read_bytes(), markdown.read_bytes()) == first
    assert json.loads(output.read_text(encoding="utf-8"))["report_hash"] == report["report_hash"]
    assert "Overall primary bar decision: `PASS`" in render_markdown(report)


def test_evaluate_target_rejects_duplicate_incomplete_and_partial_intervals() -> None:
    sample = scenario()
    duplicate = (*sample.forecasts, sample.forecasts[0])
    with pytest.raises(BacktestError, match="duplicate"):
        evaluate_target(sample.targets, duplicate)
    with pytest.raises(BacktestError, match="incomplete periods"):
        evaluate_target(sample.targets, sample.forecasts[:-1])
    partial = list(sample.forecasts)
    bridge_index = next(
        index for index, item in enumerate(partial) if item.model_version == BRIDGE_VERSION
    )
    partial[bridge_index] = replace(partial[bridge_index], low80=None)
    with pytest.raises(BacktestError):
        evaluate_target(sample.targets, partial)


def test_backtest_rejects_mixed_empty_nonfinite_and_invalid_band_inputs() -> None:
    sample = scenario()
    with pytest.raises(BacktestError, match="empty"):
        evaluate_target((), ())
    mixed_targets = (
        sample.targets[0],
        replace(sample.targets[1], target_series="DELTA_DTCTLN.M"),
        *sample.targets[2:],
    )
    with pytest.raises(BacktestError, match="mixes series"):
        evaluate_target(mixed_targets, sample.forecasts)
    with pytest.raises(BacktestError, match="duplicate periods"):
        evaluate_target((*sample.targets, sample.targets[-1]), sample.forecasts)
    with pytest.raises(BacktestError, match="no target"):
        evaluate_target(sample.targets, sample.forecasts, start=date(2030, 1, 1))

    mixed_forecasts = list(sample.forecasts)
    mixed_forecasts[0] = replace(mixed_forecasts[0], target_series="DELTA_DTCTLN.M")
    with pytest.raises(BacktestError, match="mixes target"):
        evaluate_target(sample.targets, mixed_forecasts)
    nonfinite = list(sample.forecasts)
    nonfinite[0] = replace(nonfinite[0], point=float("nan"))
    with pytest.raises(BacktestError, match="non-finite"):
        evaluate_target(sample.targets, nonfinite)
    invalid_bands = list(sample.forecasts)
    bridge_index = next(
        index for index, item in enumerate(invalid_bands) if item.model_version == BRIDGE_VERSION
    )
    invalid_bands[bridge_index] = replace(
        invalid_bands[bridge_index],
        low80=invalid_bands[bridge_index].point + 1,
    )
    with pytest.raises(BacktestError, match="ordering"):
        evaluate_target(sample.targets, invalid_bands)


def test_report_rejects_missing_target_and_naive_as_of() -> None:
    sample = scenario()
    result = evaluate_target(sample.targets, sample.forecasts)
    with pytest.raises(BacktestError, match="both prescribed"):
        build_report((result,), as_of=datetime(2026, 8, 4, tzinfo=UTC))
    with pytest.raises(BacktestError, match="timezone-aware"):
        build_report(
            (result, replace(result, target_series="DELTA_DTCTLN.M")),
            as_of=datetime(2026, 8, 4, tzinfo=UTC).replace(tzinfo=None),
        )


def test_cli_boundary_orchestrates_guarded_models_and_parses_as_of(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revolving = scenario()
    nonrevolving = scenario("DELTA_DTCTLN.M")
    by_target = {
        PRIMARY_TARGET: revolving,
        "DELTA_DTCTLN.M": nonrevolving,
    }
    monkeypatch.setattr(backtest_cli, "AppendOnlyParquetStore", lambda _path: object())
    monkeypatch.setattr(backtest_cli, "LakeSeriesReader", lambda _store: object())
    monkeypatch.setattr(backtest_cli, "CachingSeriesReader", lambda reader: reader)
    monkeypatch.setattr(backtest_cli, "VintageGuard", lambda reader: reader)
    monkeypatch.setattr(
        backtest_cli,
        "read_first_print_targets",
        lambda _guard, series, _as_of, start: by_target[series].targets,
    )
    monkeypatch.setattr(
        backtest_cli,
        "historical_bridge_features",
        lambda _guard, targets, start: targets,
    )
    monkeypatch.setattr(
        backtest_cli,
        "expanding_window_baselines",
        lambda targets, start: tuple(
            item
            for item in by_target[targets[0].target_series].forecasts
            if item.model_version in (RANDOM_WALK_VERSION, SEASONAL_NAIVE_VERSION, AR2_VERSION)
        ),
    )
    monkeypatch.setattr(
        backtest_cli,
        "expanding_window_bridge",
        lambda targets, _features, start: tuple(
            item
            for item in by_target[targets[0].target_series].forecasts
            if item.model_version == BRIDGE_VERSION
        ),
    )
    monkeypatch.setattr(
        backtest_cli,
        "expanding_window_state_space",
        lambda targets, _features, start: tuple(
            item
            for item in by_target[targets[0].target_series].forecasts
            if item.model_version == STATE_SPACE_VERSION
        ),
    )

    report = backtest_cli.run_backtest(
        Path("unused"), as_of=datetime(2026, 8, 4, 23, 59, tzinfo=UTC)
    )

    assert report["report_hash"]
    assert backtest_cli._parse_as_of("2026-08-04T23:59:00Z").tzinfo is not None
    with pytest.raises(argparse.ArgumentTypeError, match="Invalid ISO"):
        backtest_cli._parse_as_of("not-a-date")
    with pytest.raises(argparse.ArgumentTypeError, match="timezone"):
        backtest_cli._parse_as_of("2026-08-04T23:59:00")
