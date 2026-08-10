"""CLI for the point-in-time Monthly Treasury Statement benchmark report."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import date, datetime
from pathlib import Path

from dfri.lake.guard import VintageGuard
from dfri.lake.readers import CachingSeriesReader, LakeSeriesReader
from dfri.lake.store import AppendOnlyParquetStore
from dfri.nowcast.mts import MTS_BACKTEST_START, read_mts_first_print_targets, run_mts_backtest

MTS_TARGET_SERIES = ("MTS:DEFICIT.M", "MTS:OUTLAYS.M")


def build_mts_backtest(lake_root: Path, *, as_of: datetime) -> dict[str, object]:
    guard = VintageGuard(CachingSeriesReader(LakeSeriesReader(AppendOnlyParquetStore(lake_root))))
    histories = {
        series_id: read_mts_first_print_targets(guard, series_id, as_of, start=date(2017, 12, 31))
        for series_id in MTS_TARGET_SERIES
    }
    return run_mts_backtest(histories, as_of=as_of, start=MTS_BACKTEST_START)


def write_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _as_of(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("MTS backtest as-of must include a timezone")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lake-root", type=Path, default=Path(".local/lake/raw"))
    parser.add_argument("--as-of", type=_as_of, required=True)
    parser.add_argument("--output", type=Path, default=Path("reports/mts_backtest.json"))
    args = parser.parse_args()
    report = build_mts_backtest(args.lake_root, as_of=args.as_of)
    write_report(args.output, report)
    print(json.dumps({"output": str(args.output), "report_hash": report["report_hash"]}))


if __name__ == "__main__":
    main()
