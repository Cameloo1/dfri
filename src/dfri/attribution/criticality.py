"""Computed assumption dependency criticality and fallback-risk reporting."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

from dfri.attribution.registry import AttributionBundle, MatrixBEntry, load_attribution_bundle
from dfri.attribution.resilience import load_source_registry

CRITICAL_DEPENDENCY_THRESHOLD: Final = 0.05
CRITICALITY_POLICY_ID: Final = "midpoint-dependency-v1"


class CriticalityError(RuntimeError):
    """Criticality metadata or its deterministic report is inconsistent."""


@dataclass(frozen=True)
class CriticalityRow:
    assumption_id: str
    rating: str
    dependency_share: float
    numerator_dependency_share: float
    denominator_dependency_share: float
    primary_source_id: str
    fallback_source_ids: tuple[str, ...]
    independent_fallback_count: int
    warning: str


def compute_assumption_criticality(bundle: AttributionBundle) -> tuple[CriticalityRow, ...]:
    assumptions = bundle.assumptions_by_id
    flows = {item.debt_product: item.prior.mid for item in bundle.flows}
    numerator: dict[str, float] = dict.fromkeys(assumptions, 0.0)
    denominator: dict[str, float] = dict.fromkeys(assumptions, 0.0)
    total_numerator = 0.0
    matrix_b_by_category: dict[str, list[MatrixBEntry]] = {}
    for row in bundle.matrix_b:
        matrix_b_by_category.setdefault(row.spend_category, []).append(row)
    for a_row in bundle.matrix_a:
        for b_row in matrix_b_by_category.get(a_row.spend_category, []):
            contribution = flows[a_row.debt_product] * a_row.prior.mid * b_row.prior.mid
            total_numerator += contribution
            for assumption_id in (*a_row.assumption_ids, *b_row.assumption_ids):
                numerator[assumption_id] += contribution
    total_denominator = 0.0
    for company in bundle.companies:
        assumption_id = company.consumer_share_assumption_id
        value = company.revenue_total_millions / 4.0 * assumptions[assumption_id].prior.mid
        denominator[assumption_id] += value
        total_denominator += value
    if total_numerator <= 0 or total_denominator <= 0:
        raise CriticalityError("Attribution dependency totals must be positive")

    sources = load_source_registry()
    rows: list[CriticalityRow] = []
    for assumption_id, assumption in sorted(assumptions.items()):
        numerator_share = numerator[assumption_id] / total_numerator
        denominator_share = denominator[assumption_id] / total_denominator
        dependency_share = max(numerator_share, denominator_share)
        rating = "CRITICAL" if dependency_share >= CRITICAL_DEPENDENCY_THRESHOLD else "NONCRITICAL"
        primary = sources.get(assumption.primary_source_id)
        independent = tuple(
            source_id
            for source_id in assumption.fallback_source_ids
            if source_id in sources
            and primary is not None
            and sources[source_id].independent_group != primary.independent_group
        )
        warning = ""
        if rating == "CRITICAL" and not independent:
            warning = "critical assumption has no registered independent fallback"
        rows.append(
            CriticalityRow(
                assumption_id=assumption_id,
                rating=rating,
                dependency_share=dependency_share,
                numerator_dependency_share=numerator_share,
                denominator_dependency_share=denominator_share,
                primary_source_id=assumption.primary_source_id,
                fallback_source_ids=assumption.fallback_source_ids,
                independent_fallback_count=len(independent),
                warning=warning,
            )
        )
    return tuple(rows)


def validate_criticality_metadata(
    bundle: AttributionBundle, rows: tuple[CriticalityRow, ...]
) -> None:
    by_id = {row.assumption_id: row for row in rows}
    if set(by_id) != set(bundle.assumptions_by_id):
        raise CriticalityError("Criticality report differs from the assumption registry")
    for assumption in bundle.assumptions:
        computed = by_id[assumption.assumption_id]
        if assumption.criticality_policy_id != CRITICALITY_POLICY_ID:
            raise CriticalityError(f"Criticality policy drift: {assumption.assumption_id}")
        if assumption.criticality_rating != computed.rating:
            raise CriticalityError(f"Criticality rating drift: {assumption.assumption_id}")
        if abs(assumption.criticality_dependency_share - computed.dependency_share) > 1e-12:
            raise CriticalityError(f"Criticality share drift: {assumption.assumption_id}")


def criticality_payload(bundle: AttributionBundle) -> dict[str, object]:
    rows = compute_assumption_criticality(bundle)
    validate_criticality_metadata(bundle, rows)
    warnings = [row.assumption_id for row in rows if row.warning]
    return {
        "schema_version": 1,
        "methodology_version": bundle.methodology_version,
        "policy_id": CRITICALITY_POLICY_ID,
        "critical_threshold": CRITICAL_DEPENDENCY_THRESHOLD,
        "rating_basis": (
            "Maximum of each assumption's share of midpoint attributed numerator and "
            "midpoint covered-company denominator."
        ),
        "status": "WARN" if warnings else "PASS",
        "critical_count": sum(row.rating == "CRITICAL" for row in rows),
        "warning_count": len(warnings),
        "warnings": warnings,
        "items": [asdict(row) for row in rows],
    }


def write_criticality_report(output: Path) -> Path:
    payload = criticality_payload(load_attribution_bundle())
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp-{uuid.uuid4().hex}")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    temporary.replace(output)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("reports/ASSUMPTION_CRITICALITY.json"))
    parser.add_argument("--check", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = criticality_payload(load_attribution_bundle())
    expected = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.check:
        if not args.output.exists() or args.output.read_text("utf-8") != expected:
            raise CriticalityError("Committed assumption criticality report is stale")
    else:
        write_criticality_report(args.output)
    warnings = payload["warnings"]
    if not isinstance(warnings, list):
        raise CriticalityError("Criticality warnings payload is malformed")
    for warning in warnings:
        print(f"WARNING assumption fallback risk: {warning}", file=sys.stderr)
    print(
        json.dumps(
            {
                "status": payload["status"],
                "critical_count": payload["critical_count"],
                "warning_count": payload["warning_count"],
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
