from __future__ import annotations

import math
from dataclasses import replace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from dfri.attribution.engine import run_attribution
from dfri.attribution.registry import (
    AttributionRegistryError,
    Prior,
    load_attribution_bundle,
    validate_attribution_bundle,
)


@given(
    low=st.floats(min_value=0, max_value=0.8, allow_nan=False, allow_infinity=False),
    width1=st.floats(min_value=0, max_value=0.1, allow_nan=False, allow_infinity=False),
    width2=st.floats(min_value=0, max_value=0.1, allow_nan=False, allow_infinity=False),
)
def test_ordered_finite_priors_validate(low: float, width1: float, width2: float) -> None:
    prior = Prior(low, low + width1, low + width1 + width2)

    prior.validate("generated")


@given(seed=st.integers(min_value=0, max_value=2**32 - 1))
@settings(max_examples=5, deadline=None)
def test_monte_carlo_outputs_preserve_bands_tiers_and_finite_flows(seed: int) -> None:
    result = run_attribution(load_attribution_bundle(), draws=10_000, seed=seed)

    for item in (*result.companies, result.aggregate):
        assert item.estimated_dfr_pct_low <= item.estimated_dfr_pct_mid
        assert item.estimated_dfr_pct_mid <= item.estimated_dfr_pct_high
        assert math.isfinite(item.estimated_debt_funded_revenue_mid_millions)
        assert math.isclose(item.tier1_share + item.tier2_share + item.tier3_share, 1, abs_tol=1e-9)


@given(weight=st.floats(min_value=1.000001, max_value=10, allow_nan=False))
def test_matrix_a_generated_overallocation_fails_closed(weight: float) -> None:
    bundle = load_attribution_bundle()
    row = bundle.matrix_a[0]
    prior = Prior(weight, weight, weight)
    broken_row = replace(row, prior=prior)
    assumption_id = row.assumption_ids[0]
    assumptions = tuple(
        replace(item, prior=prior) if item.assumption_id == assumption_id else item
        for item in bundle.assumptions
    )

    with pytest.raises(AttributionRegistryError, match="weights exceed one"):
        validate_attribution_bundle(
            replace(bundle, matrix_a=(broken_row, *bundle.matrix_a[1:]), assumptions=assumptions)
        )


@given(weight=st.floats(min_value=-10, max_value=-0.000001, allow_nan=False))
def test_matrix_b_generated_negative_weight_fails_closed(weight: float) -> None:
    bundle = load_attribution_bundle()
    row = next(item for item in bundle.matrix_b if not item.assumption_ids)
    broken = replace(row, prior=Prior(weight, weight, weight))
    rows = tuple(broken if item == row else item for item in bundle.matrix_b)

    with pytest.raises(AttributionRegistryError, match="Invalid Matrix B row"):
        validate_attribution_bundle(replace(bundle, matrix_b=rows))
