"""Reproducible point-in-time M2 model comparison and acceptance-bar report."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from math import isfinite, sqrt
from pathlib import Path
from typing import Final, Protocol

from dfri.nowcast.baselines import (
    AR2_VERSION,
    RANDOM_WALK_VERSION,
    SEASONAL_NAIVE_VERSION,
)
from dfri.nowcast.bridge import BRIDGE_VERSION
from dfri.nowcast.state_space import STATE_SPACE_VERSION
from dfri.nowcast.targets import FirstPrintTarget

BACKTEST_VERSION: Final = "m2-point-in-time-backtest-v1"
BACKTEST_START: Final = date(2018, 1, 1)
PRIMARY_TARGET: Final = "DELTA_DTCTLR.M"
TARGET_SERIES: Final = (PRIMARY_TARGET, "DELTA_DTCTLN.M")
NAIVE_VERSIONS: Final = (RANDOM_WALK_VERSION, SEASONAL_NAIVE_VERSION, AR2_VERSION)


class BacktestError(RuntimeError):
    """Backtest inputs or output violate the reproducibility contract."""


class ForecastLike(Protocol):
    @property
    def model_version(self) -> str: ...

    @property
    def target_series(self) -> str: ...

    @property
    def target_period(self) -> date: ...

    @property
    def point(self) -> float: ...


@dataclass(frozen=True)
class ForecastValue:
    model_version: str
    target_series: str
    target_period: date
    point: float
    low80: float | None
    high80: float | None
    low95: float | None
    high95: float | None


@dataclass(frozen=True)
class MetricRow:
    model_version: str
    observations: int
    period_start: date
    period_end: date
    mae: float
    rmse: float
    coverage80: float | None
    coverage95: float | None
    acceleration_sign_accuracy: float


@dataclass(frozen=True)
class TargetResult:
    target_series: str
    target_observations: int
    target_period_start: date
    target_period_end: date
    first_vintage_url: str
    first_vintage_checksum: str
    last_vintage_url: str
    last_vintage_checksum: str
    metrics: tuple[MetricRow, ...]


def normalize_forecast(forecast: ForecastLike) -> ForecastValue:
    """Normalize all candidate dataclasses into one metric input contract."""

    return ForecastValue(
        model_version=forecast.model_version,
        target_series=forecast.target_series,
        target_period=forecast.target_period,
        point=forecast.point,
        low80=_optional_float(forecast, "low80"),
        high80=_optional_float(forecast, "high80"),
        low95=_optional_float(forecast, "low95"),
        high95=_optional_float(forecast, "high95"),
    )


def evaluate_target(
    targets: tuple[FirstPrintTarget, ...],
    forecasts: Sequence[ForecastValue],
    *,
    start: date = BACKTEST_START,
) -> TargetResult:
    """Calculate comparable metrics with acceleration measured against prior actual flow."""

    if not targets:
        raise BacktestError("Backtest target history is empty")
    target_series = targets[0].target_series
    if any(item.target_series != target_series for item in targets):
        raise BacktestError("Backtest target history mixes series")
    actual_by_period = {item.target_period: item for item in targets}
    if len(actual_by_period) != len(targets):
        raise BacktestError("Backtest target history has duplicate periods")
    expected_periods = tuple(item.target_period for item in targets if item.target_period >= start)
    if not expected_periods:
        raise BacktestError("Backtest window has no target observations")
    previous_by_period = {
        targets[index].target_period: targets[index - 1].value for index in range(1, len(targets))
    }
    grouped: dict[str, list[ForecastValue]] = defaultdict(list)
    seen: set[tuple[str, date]] = set()
    for forecast in forecasts:
        if forecast.target_series != target_series:
            raise BacktestError("Backtest forecast mixes target series")
        key = (forecast.model_version, forecast.target_period)
        if key in seen:
            raise BacktestError("Backtest has duplicate model-period forecasts")
        seen.add(key)
        grouped[forecast.model_version].append(forecast)
    expected_models = {*NAIVE_VERSIONS, BRIDGE_VERSION, STATE_SPACE_VERSION}
    if set(grouped) != expected_models:
        raise BacktestError("Backtest model set is incomplete or unexpected")

    metrics: list[MetricRow] = []
    for model_version, rows in grouped.items():
        rows.sort(key=lambda item: item.target_period)
        periods = tuple(item.target_period for item in rows)
        if periods != expected_periods:
            raise BacktestError(f"Backtest model has incomplete periods: {model_version}")
        errors: list[float] = []
        acceleration_matches: list[bool] = []
        coverage80: list[bool] = []
        coverage95: list[bool] = []
        has_intervals = all(
            item.low80 is not None
            and item.high80 is not None
            and item.low95 is not None
            and item.high95 is not None
            for item in rows
        )
        has_no_intervals = all(
            item.low80 is None
            and item.high80 is None
            and item.low95 is None
            and item.high95 is None
            for item in rows
        )
        if not has_intervals and not has_no_intervals:
            raise BacktestError(f"Backtest model has partial interval fields: {model_version}")
        for row in rows:
            actual = actual_by_period.get(row.target_period)
            previous = previous_by_period.get(row.target_period)
            if actual is None or previous is None:
                raise BacktestError("Backtest forecast cannot be matched to current/prior actuals")
            if not isfinite(row.point):
                raise BacktestError("Backtest forecast point is non-finite")
            errors.append(row.point - actual.value)
            acceleration_matches.append(
                _sign(row.point - previous) == _sign(actual.value - previous)
            )
            if has_intervals:
                low80 = _required(row.low80)
                high80 = _required(row.high80)
                low95 = _required(row.low95)
                high95 = _required(row.high95)
                if not low95 <= low80 <= row.point <= high80 <= high95:
                    raise BacktestError("Backtest interval ordering is invalid")
                coverage80.append(low80 <= actual.value <= high80)
                coverage95.append(low95 <= actual.value <= high95)
        count = len(rows)
        metrics.append(
            MetricRow(
                model_version=model_version,
                observations=count,
                period_start=rows[0].target_period,
                period_end=rows[-1].target_period,
                mae=sum(abs(item) for item in errors) / count,
                rmse=sqrt(sum(item * item for item in errors) / count),
                coverage80=(sum(coverage80) / count if has_intervals else None),
                coverage95=(sum(coverage95) / count if has_intervals else None),
                acceleration_sign_accuracy=sum(acceleration_matches) / count,
            )
        )
    metrics.sort(key=lambda item: item.model_version)
    return TargetResult(
        target_series=target_series,
        target_observations=len(targets),
        target_period_start=targets[0].target_period,
        target_period_end=targets[-1].target_period,
        first_vintage_url=targets[0].source_url,
        first_vintage_checksum=targets[0].checksum,
        last_vintage_url=targets[-1].source_url,
        last_vintage_checksum=targets[-1].checksum,
        metrics=tuple(metrics),
    )


def build_report(
    results: Sequence[TargetResult], *, as_of: datetime, start: date = BACKTEST_START
) -> dict[str, object]:
    """Build a canonical report and apply §6.2/§6.3 to the primary target."""

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise BacktestError("Backtest as-of timestamp must be timezone-aware")
    by_target = {item.target_series: item for item in results}
    if set(by_target) != set(TARGET_SERIES):
        raise BacktestError("Backtest report requires both prescribed targets")
    primary = by_target[PRIMARY_TARGET]
    primary_metrics = {item.model_version: item for item in primary.metrics}
    bridge = primary_metrics[BRIDGE_VERSION]
    state_space = primary_metrics[STATE_SPACE_VERSION]
    headline = state_space if state_space.mae < bridge.mae else bridge
    best_naive = min((primary_metrics[item] for item in NAIVE_VERSIONS), key=lambda item: item.mae)
    improvement = 1.0 - headline.mae / best_naive.mae
    bars = {
        "mae_at_least_10pct_better_than_best_naive": improvement >= 0.10,
        "coverage80_within_5pp": (
            headline.coverage80 is not None and 0.70 <= headline.coverage80 <= 0.90
        ),
        "coverage95_within_5pp": (
            headline.coverage95 is not None and 0.90 <= headline.coverage95 <= 1.00
        ),
        "acceleration_sign_accuracy_at_least_55pct": (headline.acceleration_sign_accuracy >= 0.55),
    }
    report: dict[str, object] = {
        "backtest_version": BACKTEST_VERSION,
        "as_of": as_of.astimezone(UTC).isoformat(),
        "window_start": start.isoformat(),
        "sources": {
            "g19_archive": "https://www.federalreserve.gov/releases/g19/",
            "h8_archive": "https://www.federalreserve.gov/releases/h8/",
            "census_marts_archive": "https://www.census.gov/retail/marts/historic_releases.html",
        },
        "targets": [_target_dict(by_target[item]) for item in TARGET_SERIES],
        "primary_headline": {
            "target_series": PRIMARY_TARGET,
            "model_version": headline.model_version,
            "state_space_eligible_under_section_6_2": state_space.mae < bridge.mae,
            "best_naive_model_version": best_naive.model_version,
            "mae_improvement_vs_best_naive": improvement,
            "bars": bars,
            "all_bars_pass": all(bars.values()),
        },
        "secondary_target_note": (
            "DELTA_DTCTLN.M is reported as a secondary diagnostic and is not used for the "
            "primary M2 acceptance-bar decision."
        ),
    }
    report["report_hash"] = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return report


def render_markdown(report: Mapping[str, object]) -> str:
    """Render the canonical report as a stable, reviewable Markdown artifact."""

    headline = _mapping(report["primary_headline"])
    bars = _mapping(headline["bars"])
    lines = [
        "# M2 Point-in-Time Backtest",
        "",
        f"Backtest version: `{report['backtest_version']}`  ",
        f"As of: `{report['as_of']}`  ",
        f"Window: `{report['window_start']}` through the latest gradeable first print  ",
        f"Report hash: `{report['report_hash']}`",
        "",
        "This report uses dated Federal Reserve Board G.19 and H.8 release archives plus dated",
        "Census MARTS releases. Every model input is selected through the Vintage Guard at the",
        "historical forecast timestamp; grades use release-coherent first-print G.19 flows.",
        "",
        "## Results",
        "",
    ]
    for target in _sequence(report["targets"]):
        target_map = _mapping(target)
        lines.extend(
            [
                f"### `{target_map['target_series']}`",
                "",
                "| Model | n | MAE | RMSE | 80% coverage | 95% coverage | Acceleration sign |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for metric in _sequence(target_map["metrics"]):
            row = _mapping(metric)
            lines.append(
                "| {model} | {n} | {mae:,.3f} | {rmse:,.3f} | {cov80} | {cov95} | {sign} |".format(
                    model=row["model_version"],
                    n=row["observations"],
                    mae=_number(row["mae"]),
                    rmse=_number(row["rmse"]),
                    cov80=_percentage(row["coverage80"]),
                    cov95=_percentage(row["coverage95"]),
                    sign=_percentage(row["acceleration_sign_accuracy"]),
                )
            )
        first_url = str(target_map["first_vintage_url"])
        last_url = str(target_map["last_vintage_url"])
        lines.extend(
            [
                "",
                f"First target vintage: [{first_url}]({first_url})  ",
                f"Last target vintage: [{last_url}]({last_url})",
                "",
            ]
        )
    state_space_eligible = str(headline["state_space_eligible_under_section_6_2"]).lower()
    improvement = _percentage(headline["mae_improvement_vs_best_naive"])
    lines.extend(
        [
            "## Primary headline decision",
            "",
            f"Selected model: `{headline['model_version']}` for `{headline['target_series']}`.",
            f"State-space eligible under §6.2: `{state_space_eligible}`.",
            f"Best naive comparator: `{headline['best_naive_model_version']}`.",
            f"MAE improvement versus best naive: `{improvement}`.",
            "",
            "| §6.3 bar | Result |",
            "|---|---|",
        ]
    )
    for name, passed in bars.items():
        lines.append(f"| `{name}` | {'PASS' if passed else 'FAIL'} |")
    lines.extend(
        [
            "",
            f"Overall primary bar decision: `{'PASS' if headline['all_bars_pass'] else 'FAIL'}`.",
            "",
            "The nonrevolving target is a secondary diagnostic and does not change the primary M2",
            "acceptance decision. No live-scoreboard or two-cycle claim is made by this report.",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(report: Mapping[str, object], output: Path, markdown: Path) -> None:
    """Atomically promote canonical JSON and Markdown only after both render successfully."""

    json_text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    markdown_text = render_markdown(report)
    _atomic_write(output, json_text)
    _atomic_write(markdown, markdown_text)


def _target_dict(result: TargetResult) -> dict[str, object]:
    return {
        "target_series": result.target_series,
        "target_observations": result.target_observations,
        "target_period_start": result.target_period_start.isoformat(),
        "target_period_end": result.target_period_end.isoformat(),
        "first_vintage_url": result.first_vintage_url,
        "first_vintage_checksum": result.first_vintage_checksum,
        "last_vintage_url": result.last_vintage_url,
        "last_vintage_checksum": result.last_vintage_checksum,
        "metrics": [
            {
                "model_version": item.model_version,
                "observations": item.observations,
                "period_start": item.period_start.isoformat(),
                "period_end": item.period_end.isoformat(),
                "mae": item.mae,
                "rmse": item.rmse,
                "coverage80": item.coverage80,
                "coverage95": item.coverage95,
                "acceleration_sign_accuracy": item.acceleration_sign_accuracy,
            }
            for item in result.metrics
        ],
    }


def _optional_float(forecast: object, field: str) -> float | None:
    value = getattr(forecast, field, None)
    return None if value is None else float(value)


def _required(value: float | None) -> float:
    if value is None or not isfinite(value):
        raise BacktestError("Backtest interval value is missing or non-finite")
    return value


def _sign(value: float) -> int:
    return int(value > 0) - int(value < 0)


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise BacktestError("Backtest render input is not a mapping")
    return value


def _sequence(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise BacktestError("Backtest render input is not a sequence")
    return value


def _number(value: object) -> float:
    if not isinstance(value, (int, float)):
        raise BacktestError("Backtest metric is not numeric")
    return float(value)


def _percentage(value: object) -> str:
    if value is None:
        return "—"
    if not isinstance(value, (int, float)):
        raise BacktestError("Backtest percentage is not numeric")
    return f"{float(value) * 100:.1f}%"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)
