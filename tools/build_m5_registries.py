"""Build the committed methodology 1.1 attribution registries from pinned evidence.

This file intentionally uses only the standard library. The coverage registry is
the hand-reviewed selection/evidence boundary; all repeated Matrix B weights and
per-company denominator rows are deterministic derivatives checked in CI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

METHODOLOGY_VERSION = "1.1.0"
GENERAL_CATEGORIES = {
    "general_retail",
    "auto_market",
    "fungible_consumer",
    "fungible_consumer_nonrevolving",
}
OUTPUTS = (
    "assumption_registry_v1_1.json",
    "matrix_a_v1_1.json",
    "matrix_b_v1_1.json",
    "company_inputs_v1_1.json",
    "flow_inputs_v1_1.json",
    "coverage_history_v1.json",
)


def build(root: Path) -> dict[str, dict[str, Any]]:
    coverage = _read(root / "coverage_registry_v1_1.json")
    base_assumptions = _read(root / "assumption_registry_v1.json")
    base_matrix_a = _read(root / "matrix_a_v1.json")
    base_matrix_b = _read(root / "matrix_b_v1.json")
    base_companies = _read(root / "company_inputs_v1.json")
    base_flows = _read(root / "flow_inputs_v1.json")

    expansion = _objects(coverage, "expansion")
    base_company_rows = _objects(base_companies, "items")
    all_tickers = [str(item["ticker"]) for item in base_company_rows + expansion]
    if len(all_tickers) != 50 or len(set(all_tickers)) != 50:
        raise ValueError("Methodology 1.1 must derive exactly 50 unique companies")

    assumptions = list(_objects(base_assumptions, "items"))
    companies = list(base_company_rows)
    denominator_midpoints: dict[str, Decimal] = {}
    assumptions_by_id = {str(item["assumption_id"]): item for item in assumptions}
    for company in base_company_rows:
        assumption = assumptions_by_id[str(company["consumer_share_assumption_id"])]
        denominator_midpoints[str(company["ticker"])] = Decimal(
            str(company["revenue_total_millions"])
        ) * Decimal(str(assumption["mid"]))

    for item in expansion:
        filing = _object(item, "latest_10k")
        revenue = _object(item, "revenue_fact")
        prior = _object(item, "consumer_share_prior")
        ticker = str(item["ticker"])
        source_url = _filing_url(str(item["cik"]), filing)
        assumption_id = f"A-DEN-{ticker}-001"
        assumptions.append(
            {
                "assumption_id": assumption_id,
                "statement": (
                    f"{item['company_name']} U.S. consumer revenue is "
                    f"{float(prior['low']) * 100:g}% to {float(prior['high']) * 100:g}% "
                    "of consolidated annual revenue."
                ),
                "low": prior["low"],
                "mid": prior["mid"],
                "high": prior["high"],
                "tier": 2,
                "source_url": source_url,
                "evidence_snippet": item["denominator_evidence"],
                "sensitivity_note": (
                    "This modeled geographic and customer-mix split moves the company denominator "
                    "and its revenue weight in the aggregate index."
                ),
                "version": METHODOLOGY_VERSION,
                "active": True,
            }
        )
        revenue_millions = Decimal(str(revenue["value"])) / Decimal("1000000")
        denominator_midpoints[ticker] = revenue_millions * Decimal(str(prior["mid"]))
        companies.append(
            {
                "ticker": ticker,
                "company_name": item["company_name"],
                "cik": item["cik"],
                "period": filing["period"],
                "revenue_total_millions": float(revenue_millions),
                "revenue_namespace": revenue["namespace"],
                "revenue_tag": revenue["tag"],
                "revenue_source_url": source_url,
                "consumer_share_assumption_id": assumption_id,
                "tier1_source_url": "",
                "tier1_excerpt": "",
                "membership_snapshot_ref": coverage["membership_snapshot_ref"],
            }
        )

    general_weights = _normalized_weights(denominator_midpoints)
    auto_weights = _normalized_weights(
        {ticker: denominator_midpoints[ticker] for ticker in ("F", "GM", "TSLA")}
    )
    matrix_b = [
        dict(item)
        for item in _objects(base_matrix_b, "items")
        if str(item["spend_category"]) not in GENERAL_CATEGORIES
    ]
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

    matrix_a_rows = [dict(item) for item in _objects(base_matrix_a, "items")]
    flow_rows = [dict(item) for item in _objects(base_flows, "items")]
    coverage_digest = hashlib.sha256(_canonical(coverage)).hexdigest()
    base_tickers = sorted(str(item["ticker"]) for item in base_company_rows)
    expansion_tickers = sorted(str(item["ticker"]) for item in expansion)
    exclusion_tickers = sorted(str(item["ticker"]) for item in _objects(coverage, "excluded"))
    return {
        "assumption_registry_v1_1.json": {
            "methodology_version": METHODOLOGY_VERSION,
            "items": assumptions,
        },
        "matrix_a_v1_1.json": {
            "methodology_version": METHODOLOGY_VERSION,
            "items": matrix_a_rows,
        },
        "matrix_b_v1_1.json": {
            "methodology_version": METHODOLOGY_VERSION,
            "weight_basis": (
                "Fixed weights are normalized estimated U.S. consumer-revenue midpoints. "
                "The underlying denominator uncertainty remains registered and sampled."
            ),
            "items": sorted(
                matrix_b, key=lambda row: (str(row["spend_category"]), str(row["ticker"]))
            ),
        },
        "company_inputs_v1_1.json": {
            "methodology_version": METHODOLOGY_VERSION,
            "first_published_at": coverage["first_published_at"],
            "items": sorted(companies, key=lambda row: str(row["ticker"])),
        },
        "flow_inputs_v1_1.json": {
            "methodology_version": METHODOLOGY_VERSION,
            "data_vintage": base_flows["data_vintage"],
            "items": flow_rows,
        },
        "coverage_history_v1.json": {
            "schema_version": "v1",
            "snapshots": [
                {
                    "methodology_version": "1.0.0",
                    "effective_at": base_companies["first_published_at"],
                    "membership_snapshot_ref": coverage["membership_snapshot_ref"],
                    "included_tickers": base_tickers,
                    "excluded_tickers": [],
                },
                {
                    "methodology_version": METHODOLOGY_VERSION,
                    "effective_at": coverage["first_published_at"],
                    "membership_snapshot_ref": coverage["membership_snapshot_ref"],
                    "coverage_registry_sha256": coverage_digest,
                    "included_tickers": sorted(base_tickers + expansion_tickers),
                    "excluded_tickers": exclusion_tickers,
                },
            ],
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


def _filing_url(cik: str, filing: dict[str, Any]) -> str:
    return (
        "https://www.sec.gov/Archives/edgar/data/"
        f"{int(cik)}/{str(filing['accession']).replace('-', '')}/{filing['primary_document']}"
    )


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


def _object(value: dict[str, Any], key: str) -> dict[str, Any]:
    row = value.get(key)
    if not isinstance(row, dict):
        raise ValueError(f"Expected object: {key}")
    return row


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
