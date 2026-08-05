"""CLI boundary for the reproducible point-in-time M2 backtest."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

from dfri.lake.guard import VintageGuard
from dfri.lake.readers import CachingSeriesReader, LakeSeriesReader
from dfri.lake.store import AppendOnlyParquetStore
from dfri.nowcast.backtest import (
    BACKTEST_START,
    TARGET_SERIES,
    TargetResult,
    build_report,
    evaluate_target,
    normalize_forecast,
    write_report,
)
from dfri.nowcast.baselines import expanding_window_baselines
from dfri.nowcast.bridge import expanding_window_bridge
from dfri.nowcast.features import historical_bridge_features
from dfri.nowcast.state_space import expanding_window_state_space
from dfri.nowcast.targets import read_first_print_targets

TARGET_START = date(2015, 1, 1)


def run_backtest(
    lake_root: Path, *, as_of: datetime, start: date = BACKTEST_START
) -> dict[str, object]:
    """Reconstruct all prescribed models from the append-only local lake."""

    guard = VintageGuard(CachingSeriesReader(LakeSeriesReader(AppendOnlyParquetStore(lake_root))))
    results: list[TargetResult] = []
    for target_series in TARGET_SERIES:
        targets = read_first_print_targets(guard, target_series, as_of, start=TARGET_START)
        features = historical_bridge_features(guard, targets, start=TARGET_START)
        forecasts = [
            *[
                normalize_forecast(item)
                for item in expanding_window_baselines(targets, start=start)
            ],
            *[
                normalize_forecast(item)
                for item in expanding_window_bridge(targets, features, start=start)
            ],
            *[
                normalize_forecast(item)
                for item in expanding_window_state_space(targets, features, start=start)
            ],
        ]
        results.append(evaluate_target(targets, forecasts, start=start))
    return build_report(results, as_of=as_of, start=start)


def _parse_as_of(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid ISO as-of timestamp: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("Backtest as-of timestamp must include a timezone")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lake-root", type=Path, default=Path(".local/lake/raw"))
    parser.add_argument("--as-of", type=_parse_as_of, required=True)
    parser.add_argument("--start", type=date.fromisoformat, default=BACKTEST_START)
    parser.add_argument("--output", type=Path, default=Path("reports/m2_backtest.json"))
    parser.add_argument("--markdown", type=Path, default=Path("reports/M2_BACKTEST.md"))
    args = parser.parse_args()
    report = run_backtest(args.lake_root, as_of=args.as_of, start=args.start)
    write_report(report, args.output, args.markdown)
    headline = report["primary_headline"]
    if not isinstance(headline, dict):
        raise RuntimeError("Backtest headline result is malformed")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "markdown": str(args.markdown),
                "report_hash": report["report_hash"],
                "all_primary_bars_pass": headline["all_bars_pass"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
