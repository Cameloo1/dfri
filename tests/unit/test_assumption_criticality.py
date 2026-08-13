from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from dfri.attribution.criticality import (
    CRITICALITY_POLICY_ID,
    CriticalityError,
    compute_assumption_criticality,
    criticality_payload,
    main,
    validate_criticality_metadata,
    write_criticality_report,
)
from dfri.attribution.registry import load_attribution_bundle


def test_computed_criticality_is_registered_and_all_critical_inputs_have_fallbacks() -> None:
    bundle = load_attribution_bundle()
    rows = compute_assumption_criticality(bundle)
    validate_criticality_metadata(bundle, rows)
    critical = [row for row in rows if row.rating == "CRITICAL"]

    assert len(critical) == 11
    assert all(row.dependency_share >= 0.05 for row in critical)
    assert all(row.independent_fallback_count >= 1 for row in critical)
    assert criticality_payload(bundle)["status"] == "PASS"


def test_criticality_report_flags_a_missing_independent_fallback_as_a_warning() -> None:
    bundle = load_attribution_bundle()
    assumption = bundle.assumptions_by_id["A-DEN-WMT-001"]
    broken = replace(assumption, fallback_source_ids=())
    assumptions = tuple(broken if item == assumption else item for item in bundle.assumptions)
    rows = compute_assumption_criticality(replace(bundle, assumptions=assumptions))

    row = next(item for item in rows if item.assumption_id == assumption.assumption_id)
    assert row.rating == "CRITICAL"
    assert row.warning


def test_criticality_rejects_nonpositive_dependency_totals() -> None:
    with pytest.raises(CriticalityError, match="totals must be positive"):
        compute_assumption_criticality(replace(load_attribution_bundle(), matrix_a=()))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"criticality_policy_id": "changed"}, "policy drift"),
        ({"criticality_rating": "CHANGED"}, "rating drift"),
        ({"criticality_dependency_share": 0.0}, "share drift"),
    ],
)
def test_criticality_metadata_rejects_registered_drift(
    change: dict[str, object],
    message: str,
) -> None:
    bundle = load_attribution_bundle()
    assumption = bundle.assumptions[0]
    changed = replace(assumption, **change)
    broken = replace(
        bundle,
        assumptions=tuple(changed if item == assumption else item for item in bundle.assumptions),
    )

    with pytest.raises(CriticalityError, match=message):
        validate_criticality_metadata(broken, compute_assumption_criticality(bundle))


def test_criticality_metadata_requires_the_complete_registry() -> None:
    bundle = load_attribution_bundle()
    rows = compute_assumption_criticality(bundle)
    with pytest.raises(CriticalityError, match="differs from the assumption registry"):
        validate_criticality_metadata(bundle, rows[:-1])


def test_criticality_report_write_and_check_are_atomic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "criticality.json"

    assert write_criticality_report(output) == output
    assert main(["--output", str(output), "--check"]) == 0
    receipt = capsys.readouterr().out
    assert '"status": "PASS"' in receipt
    assert CRITICALITY_POLICY_ID in output.read_text("utf-8")

    output.write_text("stale", encoding="utf-8")
    with pytest.raises(CriticalityError, match="report is stale"):
        main(["--output", str(output), "--check"])
