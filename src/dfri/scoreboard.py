"""Idempotent prediction and first-print grading jobs for the M2 scoreboard clock."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final

from dfri.lake.guard import VintageGuard
from dfri.lake.readers import CachingSeriesReader, LakeSeriesReader
from dfri.lake.store import AppendOnlyParquetStore
from dfri.nowcast.bridge import fit_bridge
from dfri.nowcast.features import (
    build_bridge_feature,
    historical_bridge_features,
    latest_h8_data_vintage,
)
from dfri.nowcast.targets import FirstPrintTarget, read_first_print_targets
from dfri.publish.ledger import (
    GradeLedger,
    GradingResult,
    PredictionLedger,
    grade_matured_predictions,
    verify_regrade_integrity,
)

TARGET_START: Final = date(2015, 1, 1)
TARGET_SERIES: Final = ("DELTA_DTCTLR.M", "DELTA_DTCTLN.M")


class ScoreboardJobError(RuntimeError):
    """A scoreboard job cannot proceed without violating its release or data boundary."""


@dataclass(frozen=True)
class PredictionSeriesResult:
    target_series: str
    h8_release_at: datetime
    latest_h8_observation: date
    target_periods: tuple[date, ...]
    appended: int
    already_present: int


@dataclass(frozen=True)
class PredictionJobResult:
    as_of: datetime
    series: tuple[PredictionSeriesResult, ...]

    @property
    def appended(self) -> int:
        return sum(item.appended for item in self.series)

    @property
    def already_present(self) -> int:
        return sum(item.already_present for item in self.series)


@dataclass(frozen=True)
class GradingJobResult:
    as_of: datetime
    attempted: int
    appended: int
    already_present: int
    not_matured: int
    integrity_verified: bool
    latest_graded_at: datetime | None


def run_prediction_job(
    raw_store: AppendOnlyParquetStore,
    ledger_store: AppendOnlyParquetStore,
    *,
    as_of: datetime,
) -> PredictionJobResult:
    """Append one bridge prediction per unreleased target month at the latest H.8 origin."""

    _validate_as_of(as_of)
    guard = VintageGuard(CachingSeriesReader(LakeSeriesReader(raw_store)))
    ledger = PredictionLedger(ledger_store)
    results: list[PredictionSeriesResult] = []
    for target_series in TARGET_SERIES:
        targets = read_first_print_targets(guard, target_series, as_of, start=TARGET_START)
        if not targets:
            raise ScoreboardJobError(f"No first-print training targets exist for {target_series}")
        training_features = historical_bridge_features(guard, targets, start=TARGET_START)
        next_period = _next_month_end(targets[-1].target_period)
        probe = build_bridge_feature(guard, target_series, next_period, as_of)
        origin = probe.latest_h8_release_at
        latest_observation = probe.latest_h8_observation_period
        if origin is None or latest_observation is None:
            raise ScoreboardJobError(f"No dated H.8 release is available for {target_series}")
        _validate_h8_origin(origin, latest_observation, as_of)
        target_periods = _month_ends(next_period, _month_end(latest_observation))
        appended = already_present = 0
        for target_period in target_periods:
            feature = build_bridge_feature(guard, target_series, target_period, origin)
            forecast = replace(fit_bridge(targets, training_features, feature), made_at=as_of)
            receipt = ledger.append(forecast)
            if receipt.appended:
                appended += 1
            else:
                already_present += 1
        results.append(
            PredictionSeriesResult(
                target_series=target_series,
                h8_release_at=origin,
                latest_h8_observation=latest_observation,
                target_periods=target_periods,
                appended=appended,
                already_present=already_present,
            )
        )
    return PredictionJobResult(as_of=as_of, series=tuple(results))


def run_grading_job(
    raw_store: AppendOnlyParquetStore,
    ledger_store: AppendOnlyParquetStore,
    *,
    as_of: datetime,
) -> GradingJobResult:
    """Grade every matured prediction and verify all grades against raw first prints."""

    _validate_as_of(as_of)
    guard = VintageGuard(CachingSeriesReader(LakeSeriesReader(raw_store)))
    targets: list[FirstPrintTarget] = []
    for target_series in TARGET_SERIES:
        targets.extend(read_first_print_targets(guard, target_series, as_of, start=TARGET_START))
    predictions = PredictionLedger(ledger_store)
    grades = GradeLedger(ledger_store)
    existing_grade_ids = {item.prediction_id for item in grades.read_all()}
    result = grade_matured_predictions(predictions, grades, tuple(targets), as_of=as_of)
    verify_regrade_integrity(predictions, grades, tuple(targets))
    new_grades = [
        item for item in grades.read_all() if item.prediction_id not in existing_grade_ids
    ]
    return _grading_job_result(
        as_of,
        result,
        latest_graded_at=max((item.graded_at for item in new_grades), default=None),
    )


def write_job_receipt(
    directory: Path,
    kind: str,
    result: PredictionJobResult | GradingJobResult,
    *,
    executed_at: datetime,
    duration_ms: int,
) -> Path:
    """Append a content-addressed operational receipt without overwriting prior attempts."""

    _validate_as_of(executed_at)
    if duration_ms < 0:
        raise ScoreboardJobError("Job duration cannot be negative")
    payload = {
        "kind": kind,
        "executed_at": executed_at.astimezone(UTC).isoformat(),
        "duration_ms": duration_ms,
        "result": _json_value(asdict(result)),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    content_hash = hashlib.sha256(encoded).hexdigest()
    path = directory / f"{kind}-{content_hash}.json"
    if path.exists():
        if path.read_bytes() != encoded + b"\n":
            raise ScoreboardJobError(f"Job receipt hash collision: {path}")
        return path
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".{path.name}.tmp"
    temporary.write_bytes(encoded + b"\n")
    temporary.replace(path)
    return path


def _grading_job_result(
    as_of: datetime, result: GradingResult, *, latest_graded_at: datetime | None
) -> GradingJobResult:
    return GradingJobResult(
        as_of=as_of,
        attempted=result.attempted,
        appended=result.appended,
        already_present=result.already_present,
        not_matured=result.not_matured,
        integrity_verified=True,
        latest_graded_at=latest_graded_at,
    )


def _validate_h8_origin(origin: datetime, observation: date, as_of: datetime) -> None:
    if origin > as_of:
        raise ScoreboardJobError("Latest H.8 release is after the job as-of boundary")
    lag = origin.date() - observation
    if lag < timedelta(days=1) or lag > timedelta(days=10):
        raise ScoreboardJobError("Latest H.8 observation/release lag is outside 1-10 days")
    if origin.weekday() not in (3, 4):
        raise ScoreboardJobError("Latest H.8 origin is not a Thursday/Friday Board release")


def _validate_as_of(as_of: datetime) -> None:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ScoreboardJobError("Scoreboard timestamp must be timezone-aware")


def _month_ends(start: date, end: date) -> tuple[date, ...]:
    if start > end:
        return ()
    periods: list[date] = []
    current = start
    while current <= end:
        periods.append(current)
        current = _next_month_end(current)
    return tuple(periods)


def _next_month_end(period: date) -> date:
    year = period.year + int(period.month == 12)
    month = 1 if period.month == 12 else period.month + 1
    following_year = year + int(month == 12)
    following_month = 1 if month == 12 else month + 1
    return date(following_year, following_month, 1) - date.resolution


def _month_end(period: date) -> date:
    year = period.year + int(period.month == 12)
    month = 1 if period.month == 12 else period.month + 1
    return date(year, month, 1) - date.resolution


def _json_value(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def serialize_job_result(result: PredictionJobResult | GradingJobResult) -> dict[str, object]:
    """Return the stable CLI payload consumed by the scoreboard workflow."""

    payload = _json_value(asdict(result))
    if not isinstance(payload, dict):
        raise ScoreboardJobError("Scoreboard result did not serialize as an object")
    if isinstance(result, PredictionJobResult):
        payload["appended"] = result.appended
        payload["already_present"] = result.already_present
    return payload


def _parse_as_of(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid ISO timestamp: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("Scoreboard timestamp must include a timezone")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("predict", "grade", "data-vintage"))
    parser.add_argument("--as-of", type=_parse_as_of)
    parser.add_argument("--raw-root", type=Path, default=Path(".local/lake/raw"))
    parser.add_argument("--ledger-root", type=Path, default=Path(".local/lake/curated"))
    parser.add_argument("--receipt-dir", type=Path, default=Path(".local/evidence/scoreboard_jobs"))
    args = parser.parse_args()
    started = time.perf_counter()
    as_of = args.as_of or datetime.now(UTC)
    raw_store = AppendOnlyParquetStore(args.raw_root)
    ledger_store = AppendOnlyParquetStore(args.ledger_root)
    if args.command == "data-vintage":
        guard = VintageGuard(CachingSeriesReader(LakeSeriesReader(raw_store)))
        data_vintage = latest_h8_data_vintage(guard, as_of)
        print(json.dumps({"as_of": as_of.isoformat(), "data_vintage": data_vintage.isoformat()}))
        return
    result: PredictionJobResult | GradingJobResult
    if args.command == "predict":
        result = run_prediction_job(raw_store, ledger_store, as_of=as_of)
    else:
        result = run_grading_job(raw_store, ledger_store, as_of=as_of)
    duration_ms = round((time.perf_counter() - started) * 1000)
    receipt = write_job_receipt(
        args.receipt_dir,
        args.command,
        result,
        executed_at=datetime.now(UTC),
        duration_ms=duration_ms,
    )
    payload = serialize_job_result(result)
    payload["receipt"] = str(receipt)
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
