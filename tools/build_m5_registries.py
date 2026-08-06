"""Build the methodology 1.1.1 classification correction from pinned evidence.

This file intentionally uses only the standard library. The coverage registry is
the immutable 50-company selection boundary; the prior 1.1.0 bundle is the input.
The corrected Matrix B weights and Carvana evidence rows are deterministic
derivatives checked in CI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

METHODOLOGY_VERSION = "1.1.1"
FIRST_PUBLISHED_AT = "2026-08-06T05:40:24.524787+00:00"
CARVANA_10K_URL = (
    "https://www.sec.gov/Archives/edgar/data/1690820/000169082026000009/cvna-20251231.htm"
)
CARVANA_ABS_URL = (
    "https://www.sec.gov/Archives/edgar/data/1770373/000110465926026877/tm267822d6_424b5.htm"
)
AUTO_TICKERS = ("CVNA", "F", "GM", "TSLA")
GENERAL_CATEGORIES = {
    "general_retail",
    "auto_market",
    "fungible_consumer",
    "fungible_consumer_nonrevolving",
}
OUTPUTS = (
    "assumption_registry_v1_1_1.json",
    "matrix_a_v1_1_1.json",
    "matrix_b_v1_1_1.json",
    "company_inputs_v1_1_1.json",
    "flow_inputs_v1_1_1.json",
    "coverage_history_v1.json",
)


def build(root: Path) -> dict[str, dict[str, Any]]:
    coverage = _read(root / "coverage_registry_v1_1.json")
    prior_assumptions = _read(root / "assumption_registry_v1_1.json")
    prior_matrix_a = _read(root / "matrix_a_v1_1.json")
    prior_matrix_b = _read(root / "matrix_b_v1_1.json")
    prior_companies = _read(root / "company_inputs_v1_1.json")
    prior_flows = _read(root / "flow_inputs_v1_1.json")
    history = _read(root / "coverage_history_v1.json")

    companies = [dict(item) for item in _objects(prior_companies, "items")]
    all_tickers = [str(item["ticker"]) for item in companies]
    if len(all_tickers) != 50 or len(set(all_tickers)) != 50:
        raise ValueError("Methodology 1.1.1 must preserve exactly 50 unique companies")

    assumptions = [dict(item) for item in _objects(prior_assumptions, "items")]
    assumptions.append(
        {
            "assumption_id": "A-T1-CVNA-FINANCE-001",
            "statement": (
                "Carvana receives 1% to 3% of national nonrevolving flow through its "
                "originated used-auto finance channel."
            ),
            "low": 0.01,
            "mid": 0.02,
            "high": 0.03,
            "tier": 1,
            "source_url": CARVANA_ABS_URL,
            "evidence_snippet": (
                "Since February 2013, Carvana has also offered and originated loans to consumers."
            ),
            "sensitivity_note": (
                "The wide prior is scaled below GM and Ford using Carvana's smaller registered "
                "consumer-revenue denominator; the filing and ABS trust establish origination."
            ),
            "version": METHODOLOGY_VERSION,
            "active": True,
        }
    )
    cvna = next(item for item in companies if item["ticker"] == "CVNA")
    cvna["tier1_source_url"] = CARVANA_10K_URL
    cvna["tier1_excerpt"] = (
        "Finance receivables include installment contracts the Company originates to its "
        "customers to facilitate vehicle sales."
    )

    assumptions_by_id = {str(item["assumption_id"]): item for item in assumptions}
    denominator_midpoints = {
        str(company["ticker"]): Decimal(str(company["revenue_total_millions"]))
        * Decimal(str(assumptions_by_id[str(company["consumer_share_assumption_id"])]["mid"]))
        for company in companies
    }

    general_weights = _normalized_weights(denominator_midpoints)
    auto_weights = _normalized_weights(
        {ticker: denominator_midpoints[ticker] for ticker in AUTO_TICKERS}
    )
    matrix_b = [
        dict(item)
        for item in _objects(prior_matrix_b, "items")
        if str(item["spend_category"]) not in GENERAL_CATEGORIES
    ]
    matrix_b.append(
        {
            "version": METHODOLOGY_VERSION,
            "spend_category": "carvana_auto_finance",
            "ticker": "CVNA",
            "weight_low": 1.0,
            "weight_mid": 1.0,
            "weight_high": 1.0,
            "method": "direct originated-auto-finance link",
            "evidence_refs": [CARVANA_10K_URL, CARVANA_ABS_URL],
        }
    )
    for category in (
        "general_retail",
        "fungible_consumer",
        "fungible_consumer_nonrevolving",
    ):
        for ticker in sorted(general_weights):
            company = next(row for row in companies if row["ticker"] == ticker)
            weight = general_weights[ticker]
            matrix_b.append(
                {
                    "version": METHODOLOGY_VERSION,
                    "spend_category": category,
                    "ticker": ticker,
                    "weight_low": weight,
                    "weight_mid": weight,
                    "weight_high": weight,
                    "method": "normalized estimated U.S. consumer revenue midpoint",
                    "evidence_refs": [
                        company["revenue_source_url"],
                        f"denominator_assumption:{company['consumer_share_assumption_id']}",
                    ],
                }
            )
    for ticker in sorted(auto_weights):
        company = next(row for row in companies if row["ticker"] == ticker)
        weight = auto_weights[ticker]
        matrix_b.append(
            {
                "version": METHODOLOGY_VERSION,
                "spend_category": "auto_market",
                "ticker": ticker,
                "weight_low": weight,
                "weight_mid": weight,
                "weight_high": weight,
                "method": "normalized covered-auto U.S. consumer revenue midpoint",
                "evidence_refs": [
                    company["revenue_source_url"],
                    f"denominator_assumption:{company['consumer_share_assumption_id']}",
                ],
            }
        )

    matrix_a_rows = [dict(item) for item in _objects(prior_matrix_a, "items")]
    matrix_a_rows.append(
        {
            "version": METHODOLOGY_VERSION,
            "debt_product": "nonrevolving_credit",
            "spend_category": "carvana_auto_finance",
            "weight_low": 0.01,
            "weight_mid": 0.02,
            "weight_high": 0.03,
            "tier": 1,
            "assumption_ids": ["A-T1-CVNA-FINANCE-001"],
        }
    )
    flow_rows = [dict(item) for item in _objects(prior_flows, "items")]
    coverage_digest = hashlib.sha256(_canonical(coverage)).hexdigest()
    snapshots = [
        dict(item)
        for item in _objects(history, "snapshots")
        if item["methodology_version"] != METHODOLOGY_VERSION
    ]
    if [item["methodology_version"] for item in snapshots] != ["1.0.0", "1.1.0"]:
        raise ValueError("Coverage history does not end at immutable methodology 1.1.0")
    snapshots.append(
        {
            "methodology_version": METHODOLOGY_VERSION,
            "effective_at": FIRST_PUBLISHED_AT,
            "membership_snapshot_ref": coverage["membership_snapshot_ref"],
            "coverage_registry_sha256": coverage_digest,
            "included_tickers": sorted(all_tickers),
            "excluded_tickers": sorted(
                str(item["ticker"]) for item in _objects(coverage, "excluded")
            ),
            "change": "Correct CVNA auto-market and originated-finance classification.",
        }
    )
    return {
        "assumption_registry_v1_1_1.json": {
            "methodology_version": METHODOLOGY_VERSION,
            "items": assumptions,
        },
        "matrix_a_v1_1_1.json": {
            "methodology_version": METHODOLOGY_VERSION,
            "items": sorted(
                matrix_a_rows,
                key=lambda row: (str(row["debt_product"]), str(row["spend_category"])),
            ),
        },
        "matrix_b_v1_1_1.json": {
            "methodology_version": METHODOLOGY_VERSION,
            "weight_basis": (
                "Fixed weights are normalized estimated U.S. consumer-revenue midpoints. "
                "The underlying denominator uncertainty remains registered and sampled."
            ),
            "items": sorted(
                matrix_b, key=lambda row: (str(row["spend_category"]), str(row["ticker"]))
            ),
        },
        "company_inputs_v1_1_1.json": {
            "methodology_version": METHODOLOGY_VERSION,
            "first_published_at": FIRST_PUBLISHED_AT,
            "items": sorted(companies, key=lambda row: str(row["ticker"])),
        },
        "flow_inputs_v1_1_1.json": {
            "methodology_version": METHODOLOGY_VERSION,
            "data_vintage": prior_flows["data_vintage"],
            "items": flow_rows,
        },
        "coverage_history_v1.json": {
            "schema_version": "v1",
            "snapshots": snapshots,
        },
    }


def write_or_check(root: Path, *, check: bool) -> None:
    built = build(root)
    drift: list[str] = []
    for filename in OUTPUTS:
        expected = _pretty(built[filename])
        path = root / filename
        if check:
            if not path.is_file() or _read(path) != built[filename]:
                drift.append(filename)
        else:
            path.write_bytes(expected)
    if drift:
        raise SystemExit(f"Generated M5 registries are stale: {', '.join(drift)}")


def _normalized_weights(values: dict[str, Decimal]) -> dict[str, float]:
    if not values or any(value <= 0 for value in values.values()):
        raise ValueError("Normalized weights require positive denominators")
    total = sum(values.values(), Decimal("0"))
    quantum = Decimal("0.000000000001")
    tickers = sorted(values)
    raw: dict[str, Decimal] = {}
    for ticker in tickers[:-1]:
        raw[ticker] = (values[ticker] / total).quantize(quantum, rounding=ROUND_DOWN)
    raw[tickers[-1]] = Decimal("1") - sum(raw.values(), Decimal("0"))
    if sum(raw.values(), Decimal("0")) != Decimal("1"):
        raise AssertionError("Normalized weights do not sum to one")
    return {ticker: float(value) for ticker, value in raw.items()}


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _objects(value: dict[str, Any], key: str) -> list[dict[str, Any]]:
    rows = value.get(key)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"Expected object rows: {key}")
    return rows


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _pretty(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "src" / "dfri" / "attribution",
    )
    args = parser.parse_args()
    write_or_check(args.root, check=args.check)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
