"""Deterministic reconciliation for the FFIEC/NCUA/Board/ABS auto allocation."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from decimal import Decimal
from importlib import resources
from typing import Any, Final

AUTO_ALLOCATION_ASSUMPTION_ID: Final = "A-T2-NONREV-AUTO-002"


class AutoAllocationError(RuntimeError):
    """The frozen replacement evidence no longer reproduces its registered prior."""


@dataclass(frozen=True)
class AutoAllocationReconciliation:
    assumption_id: str
    period: str
    ffiec_auto_share: float
    ncua_auto_share: float
    combined_regulated_auto_share: float
    board_national_auto_share: float
    abs_covered_share: float
    leave_one_out_low: float
    leave_one_out_high: float
    prior_low: float
    prior_mid: float
    prior_high: float
    evidence_urls: tuple[str, ...]


def load_auto_allocation_reconciliation() -> AutoAllocationReconciliation:
    payload = json.loads(
        resources.files("dfri.attribution")
        .joinpath("auto_allocation_evidence_v1.json")
        .read_text("utf-8")
    )
    return reconcile_auto_allocation(payload)


def reconcile_auto_allocation(payload: dict[str, Any]) -> AutoAllocationReconciliation:
    if payload.get("schema_version") != 1:
        raise AutoAllocationError("Auto-allocation evidence schema changed")
    if payload.get("assumption_id") != AUTO_ALLOCATION_ASSUMPTION_ID:
        raise AutoAllocationError("Auto-allocation assumption identity changed")
    ffiec = _object(payload, "ffiec")
    ncua = _object(payload, "ncua")
    board = _object(payload, "board_g19")
    abs_payload = _object(payload, "auto_abs")
    registered = _object(payload, "reconciliation")

    ffiec_auto = _decimal(ffiec, "automobile")
    ffiec_other = _decimal(ffiec, "other_consumer")
    ffiec_share = ffiec_auto / (ffiec_auto + ffiec_other)
    ffiec_unit_multiplier = _decimal(ffiec, "unit_multiplier")

    ncua_auto = _decimal(ncua, "used_vehicle") + _decimal(ncua, "new_vehicle")
    ncua_residual = (
        _decimal(ncua, "total_loans_and_leases")
        - _decimal(ncua, "consumer_real_estate")
        - _decimal(ncua, "commercial_member")
        - _decimal(ncua, "commercial_nonmember")
        - _decimal(ncua, "credit_cards")
        - _decimal(ncua, "leases")
    )
    if ncua_residual <= 0 or ncua_auto > ncua_residual:
        raise AutoAllocationError("NCUA residual consumer denominator is invalid")
    ncua_share = ncua_auto / ncua_residual
    combined_regulated_share = ((ffiec_auto * ffiec_unit_multiplier) + ncua_auto) / (
        ((ffiec_auto + ffiec_other) * ffiec_unit_multiplier) + ncua_residual
    )

    board_share = _decimal(board, "motor_vehicle") / _decimal(board, "nonrevolving")
    trusts = abs_payload.get("trusts")
    covered_ids = abs_payload.get("covered_sponsors")
    if (
        not isinstance(trusts, list)
        or len(trusts) < 6
        or not isinstance(covered_ids, list)
        or not all(isinstance(item, str) for item in covered_ids)
    ):
        raise AutoAllocationError("Auto ABS reconciliation sample is incomplete")
    amounts: dict[str, Decimal] = {}
    urls: list[str] = [
        _string(ffiec, "source_url"),
        _string(ncua, "source_url"),
        _string(board, "source_url"),
    ]
    for item in trusts:
        if not isinstance(item, dict):
            raise AutoAllocationError("Auto ABS trust evidence is malformed")
        trust_id = _string(item, "trust_id")
        if trust_id in amounts:
            raise AutoAllocationError(f"Duplicate Auto ABS trust: {trust_id}")
        amounts[trust_id] = _decimal(item, "amount")
        urls.append(_string(item, "source_url"))
    covered = set(covered_ids)
    if not covered < set(amounts):
        raise AutoAllocationError("Covered Auto ABS sponsor set is invalid")
    total = sum(amounts.values(), Decimal(0))
    covered_total = sum((amounts[item] for item in covered), Decimal(0))
    abs_share = covered_total / total
    leave_one_out = []
    for trust_id, amount in amounts.items():
        reduced_total = total - amount
        reduced_covered = covered_total - (amount if trust_id in covered else Decimal(0))
        leave_one_out.append(reduced_covered / reduced_total)

    low = _decimal(registered, "prior_low")
    mid = _decimal(registered, "prior_mid")
    high = _decimal(registered, "prior_high")
    derived_mid = board_share * abs_share
    if abs(mid - derived_mid) > Decimal("0.0000000000005"):
        raise AutoAllocationError("Registered auto-allocation midpoint no longer reconciles")
    if not low <= mid <= high:
        raise AutoAllocationError("Registered auto-allocation band is unordered")
    derived_low = board_share * min(leave_one_out)
    derived_high = board_share * max(leave_one_out)
    if low > derived_low or high < derived_high:
        raise AutoAllocationError("Registered band does not enclose source disagreement")

    result = AutoAllocationReconciliation(
        assumption_id=AUTO_ALLOCATION_ASSUMPTION_ID,
        period=_string(payload, "period"),
        ffiec_auto_share=float(ffiec_share),
        ncua_auto_share=float(ncua_share),
        combined_regulated_auto_share=float(combined_regulated_share),
        board_national_auto_share=float(board_share),
        abs_covered_share=float(abs_share),
        leave_one_out_low=float(derived_low),
        leave_one_out_high=float(derived_high),
        prior_low=float(low),
        prior_mid=float(mid),
        prior_high=float(high),
        evidence_urls=tuple(urls),
    )
    if not all(
        math.isfinite(value)
        for value in (
            result.ffiec_auto_share,
            result.ncua_auto_share,
            result.combined_regulated_auto_share,
            result.board_national_auto_share,
            result.abs_covered_share,
            result.prior_low,
            result.prior_mid,
            result.prior_high,
        )
    ):
        raise AutoAllocationError("Auto-allocation reconciliation is non-finite")
    return result


def _object(item: dict[str, Any], key: str) -> dict[str, Any]:
    value = item.get(key)
    if not isinstance(value, dict):
        raise AutoAllocationError(f"Auto-allocation object is missing: {key}")
    return value


def _string(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise AutoAllocationError(f"Auto-allocation string is missing: {key}")
    return value


def _decimal(item: dict[str, Any], key: str) -> Decimal:
    value = item.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise AutoAllocationError(f"Auto-allocation number is missing: {key}")
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed < 0:
        raise AutoAllocationError(f"Auto-allocation number is invalid: {key}")
    return parsed
