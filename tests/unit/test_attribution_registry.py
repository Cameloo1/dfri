from __future__ import annotations

from dataclasses import replace

import pytest

from dfri.attribution.registry import (
    AttributionRegistryError,
    Prior,
    load_attribution_bundle,
    validate_attribution_bundle,
)


def test_public_attribution_bundle_is_complete_and_source_hashed() -> None:
    bundle = load_attribution_bundle()

    assert bundle.methodology_version == "1.0.0"
    assert bundle.data_vintage == "2026-05-07T19:00:00+00:00"
    assert len(bundle.source_hash) == 64
    assert {item.ticker for item in bundle.companies} == {
        "AMZN",
        "BBY",
        "F",
        "GM",
        "HD",
        "LOW",
        "TGT",
        "TSCO",
        "ULTA",
        "WMT",
    }
    assert {item.quarter for item in bundle.flows} == {"2026-Q1"}
    assert all(len(item.tier1_excerpt.split()) <= 15 for item in bundle.companies)
    assert all(item.source_url.startswith("https://") for item in bundle.assumptions)


def test_matrix_bounds_and_assumption_coverage_are_explicit() -> None:
    bundle = load_attribution_bundle()
    assumptions = bundle.assumptions_by_id

    for product in {item.debt_product for item in bundle.matrix_a}:
        rows = [item for item in bundle.matrix_a if item.debt_product == product]
        for bound in ("low", "mid", "high"):
            assert sum(getattr(item.prior, bound) for item in rows) <= 1
    for category in {item.spend_category for item in bundle.matrix_b}:
        rows = [item for item in bundle.matrix_b if item.spend_category == category]
        assert sum(item.prior.high for item in rows) <= 1 + 1e-9
    for item in bundle.matrix_a:
        assert len(item.assumption_ids) == 1
        assert assumptions[item.assumption_ids[0]].prior == item.prior


def test_registry_rejects_invalid_prior_order() -> None:
    with pytest.raises(AttributionRegistryError, match="low <= mid <= high"):
        Prior(0.5, 0.4, 0.6).validate("bad")


def test_registry_rejects_matrix_a_overallocation() -> None:
    bundle = load_attribution_bundle()
    first = bundle.matrix_a[0]
    broken = replace(first, prior=Prior(1.1, 1.1, 1.1), assumption_ids=("A-MISSING",))
    with pytest.raises(AttributionRegistryError):
        validate_attribution_bundle(replace(bundle, matrix_a=(broken, *bundle.matrix_a[1:])))


def test_registry_rejects_matrix_b_overallocation() -> None:
    bundle = load_attribution_bundle()
    row = bundle.matrix_b[0]
    broken = replace(row, prior=Prior(1.1, 1.1, 1.1), evidence_refs=("fixed",))
    with pytest.raises(AttributionRegistryError, match="weights exceed one"):
        validate_attribution_bundle(replace(bundle, matrix_b=(broken, *bundle.matrix_b[1:])))


def test_registry_rejects_unregistered_uncertainty() -> None:
    bundle = load_attribution_bundle()
    fixed = next(item for item in bundle.matrix_b if not item.assumption_ids)
    broken = replace(fixed, prior=Prior(0.1, 0.2, 0.3))
    rows = tuple(broken if item == fixed else item for item in bundle.matrix_b)
    with pytest.raises(AttributionRegistryError, match="lacks an assumption"):
        validate_attribution_bundle(replace(bundle, matrix_b=rows))


def test_registry_rejects_missing_p0_company() -> None:
    bundle = load_attribution_bundle()
    with pytest.raises(AttributionRegistryError, match="exactly ten"):
        validate_attribution_bundle(replace(bundle, companies=bundle.companies[:-1]))


def test_registry_rejects_incomplete_company_evidence() -> None:
    bundle = load_attribution_bundle()
    company = replace(bundle.companies[0], tier1_excerpt="word " * 16)
    with pytest.raises(AttributionRegistryError, match="exceeds 15 words"):
        validate_attribution_bundle(replace(bundle, companies=(company, *bundle.companies[1:])))


def test_registry_rejects_revenue_that_does_not_match_pinned_xbrl_fact() -> None:
    bundle = load_attribution_bundle()
    company = replace(
        bundle.companies[0], revenue_total_millions=bundle.companies[0].revenue_total_millions + 1
    )
    with pytest.raises(AttributionRegistryError, match="verified XBRL fact"):
        validate_attribution_bundle(replace(bundle, companies=(company, *bundle.companies[1:])))
