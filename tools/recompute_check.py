"""Independently recompute three published DFRI company midpoints.

This script intentionally imports no DFRI code. It reads the committed curated
inputs directly and performs the formula in DFRI_BUILD_SPEC.md section 7.2.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

DEFAULT_TICKERS = ("AMZN", "GM", "WMT")
TOLERANCE_PP = 0.5
CURRENT_INPUT_SUFFIX = "v1_2"


def _assumption_mid(row: dict[str, Any]) -> float:
    value = row.get("mid", row.get("weight_mid"))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Assumption has no numeric midpoint: {row.get('assumption_id')}")
    return float(value)


def _read(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return payload


def _items(payload: dict[str, Any], label: str) -> list[dict[str, Any]]:
    rows = payload.get("items")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"Expected item rows: {label}")
    return rows


def recompute(
    inputs_root: Path,
    published_report: Path,
    tickers: Sequence[str] = DEFAULT_TICKERS,
) -> dict[str, Any]:
    assumptions = {
        row["assumption_id"]: _assumption_mid(row)
        for row in _items(
            _read(inputs_root / f"assumption_registry_{CURRENT_INPUT_SUFFIX}.json"),
            "assumptions",
        )
    }
    flows = {
        row["debt_product"]: float(row["mid"])
        for row in _items(_read(inputs_root / f"flow_inputs_{CURRENT_INPUT_SUFFIX}.json"), "flows")
    }
    matrix_a = _items(_read(inputs_root / f"matrix_a_{CURRENT_INPUT_SUFFIX}.json"), "Matrix A")
    matrix_b = _items(_read(inputs_root / f"matrix_b_{CURRENT_INPUT_SUFFIX}.json"), "Matrix B")
    companies = {
        row["ticker"]: row
        for row in _items(
            _read(inputs_root / f"company_inputs_{CURRENT_INPUT_SUFFIX}.json"), "companies"
        )
    }
    published_payload = _read(published_report)
    published_rows = published_payload.get("companies")
    if not isinstance(published_rows, list):
        raise ValueError("Published report has no company rows")
    published = {row["ticker"]: row for row in published_rows}

    b_lookup: dict[tuple[str, str], float] = {}
    for row in matrix_b:
        b_lookup[(str(row["spend_category"]), str(row["ticker"]))] = float(row["weight_mid"])

    checks: list[dict[str, Any]] = []
    for ticker in tickers:
        company = companies[ticker]
        numerator = 0.0
        for row in matrix_a:
            category = str(row["spend_category"])
            b_weight = b_lookup.get((category, ticker), 0.0)
            numerator += flows[str(row["debt_product"])] * float(row["weight_mid"]) * b_weight
        consumer_share = assumptions[str(company["consumer_share_assumption_id"])]
        denominator = float(company["revenue_total_millions"]) / 4.0 * consumer_share
        independently_computed = numerator / denominator * 100.0
        published_mid = float(published[ticker]["estimated_dfr_pct_mid"])
        difference_pp = abs(independently_computed - published_mid)
        checks.append(
            {
                "ticker": ticker,
                "independent_mid_pct": independently_computed,
                "published_monte_carlo_mid_pct": published_mid,
                "absolute_difference_pp": difference_pp,
                "status": "PASS" if difference_pp <= TOLERANCE_PP else "FAIL",
            }
        )
    return {
        "status": "PASS" if all(row["status"] == "PASS" for row in checks) else "FAIL",
        "tolerance_percentage_points": TOLERANCE_PP,
        "method": "independent deterministic midpoint formula",
        "checks": checks,
    }


def build_parser() -> argparse.ArgumentParser:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inputs-root", default=project_root / "src" / "dfri" / "attribution", type=Path
    )
    parser.add_argument(
        "--published", default=project_root / "reports" / "dfri_companies.json", type=Path
    )
    parser.add_argument("--tickers", nargs=3, default=DEFAULT_TICKERS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    receipt = recompute(args.inputs_root, args.published, args.tickers)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
