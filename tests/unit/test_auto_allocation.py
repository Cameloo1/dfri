from __future__ import annotations

import json
from importlib import resources

import pytest

from dfri.attribution.auto_allocation import (
    AutoAllocationError,
    load_auto_allocation_reconciliation,
    reconcile_auto_allocation,
)


def test_frozen_auto_allocation_reconciles_all_three_source_lanes() -> None:
    result = load_auto_allocation_reconciliation()

    assert result.assumption_id == "A-T2-NONREV-AUTO-002"
    assert result.ffiec_auto_share == pytest.approx(0.63098018)
    assert result.ncua_auto_share == pytest.approx(0.76810623)
    assert result.combined_regulated_auto_share == pytest.approx(0.68902001)
    assert result.board_national_auto_share == pytest.approx(0.41024967)
    assert result.abs_covered_share == pytest.approx(0.40439662)
    assert result.prior_low <= result.leave_one_out_low
    assert result.prior_high >= result.leave_one_out_high
    assert len(result.evidence_urls) == 9


def test_auto_allocation_rejects_a_midpoint_that_no_longer_reconciles() -> None:
    payload = _evidence()
    payload["reconciliation"]["prior_mid"] = 0.18

    with pytest.raises(AutoAllocationError, match="midpoint"):
        reconcile_auto_allocation(payload)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload.update(schema_version=2), "schema changed"),
        (lambda payload: payload.update(assumption_id="wrong"), "identity changed"),
        (lambda payload: payload.pop("ffiec"), "object is missing"),
        (
            lambda payload: payload["ncua"].update(total_loans_and_leases=1),
            "residual consumer denominator",
        ),
        (lambda payload: payload["auto_abs"].update(trusts=[]), "sample is incomplete"),
        (
            lambda payload: payload["auto_abs"]["trusts"].append(
                dict(payload["auto_abs"]["trusts"][0])
            ),
            "Duplicate Auto ABS trust",
        ),
        (
            lambda payload: payload["auto_abs"].update(covered_sponsors=["unknown"]),
            "sponsor set is invalid",
        ),
        (
            lambda payload: payload["reconciliation"].update(
                prior_low=0.3, prior_mid=0.165903581813124
            ),
            "band is unordered",
        ),
        (
            lambda payload: payload["reconciliation"].update(prior_low=0.15),
            "does not enclose",
        ),
    ],
)
def test_auto_allocation_rejects_frozen_evidence_drift(
    mutation: object,
    message: str,
) -> None:
    payload = _evidence()
    mutation(payload)  # type: ignore[operator]

    with pytest.raises(AutoAllocationError, match=message):
        reconcile_auto_allocation(payload)


def _evidence() -> dict[str, object]:
    return json.loads(
        resources.files("dfri.attribution")
        .joinpath("auto_allocation_evidence_v1.json")
        .read_text("utf-8")
    )
