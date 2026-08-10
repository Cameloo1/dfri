from __future__ import annotations

import math

import pytest

from dfri.attribution.engine import AttributionError, run_attribution
from dfri.attribution.registry import load_attribution_bundle


def test_attribution_is_deterministic_complete_and_finite() -> None:
    bundle = load_attribution_bundle()
    first = run_attribution(bundle)
    second = run_attribution(bundle)

    assert first == second
    assert first.draws == 20_000
    assert first.source_hash == bundle.source_hash
    assert len(first.companies) == 50
    assert first.aggregate.weighting == "revenue-weighted"
    assert "highest Evidence Lift" in first.evidence_lift_headline
    for estimate in (*first.companies, first.aggregate):
        assert math.isfinite(estimate.estimated_dfr_pct_mid)
        assert (
            estimate.estimated_dfr_pct_low
            <= estimate.estimated_dfr_pct_mid
            <= estimate.estimated_dfr_pct_high
        )
        assert estimate.estimated_debt_funded_revenue_mid_millions > 0
        assert estimate.estimated_us_consumer_revenue_mid_millions > 0
        assert estimate.tier1_share >= 0
        assert estimate.tier2_share >= 0
        assert estimate.tier3_share >= 0
        assert math.isclose(
            estimate.tier1_share + estimate.tier2_share + estimate.tier3_share,
            1,
            abs_tol=1e-12,
        )

    for company in first.companies:
        assert company.fungibility_baseline_dfr_pct_mid > 0
        assert company.evidence_lift >= 1
        assert company.evidence_lift_status in {"baseline-only", "evidence-supported"}


def test_carvana_correction_and_baseline_only_companies_are_explicit() -> None:
    result = run_attribution(load_attribution_bundle())
    by_ticker = {item.ticker: item for item in result.companies}

    cvna = by_ticker["CVNA"]
    assert cvna.tier1_source_url.endswith("/cvna-20251231.htm")
    assert cvna.tier1_share > 0.5
    assert cvna.evidence_lift_status == "evidence-supported"
    assert 15 < cvna.estimated_dfr_pct_mid < 22
    assert abs(cvna.estimated_dfr_pct_mid - by_ticker["GM"].estimated_dfr_pct_mid) < 2

    baseline_only = [
        item for item in result.companies if item.evidence_lift_status == "baseline-only"
    ]
    assert baseline_only
    assert all(math.isclose(item.evidence_lift, 1.0, abs_tol=1e-12) for item in baseline_only)
    evidence_supported = {
        item.ticker
        for item in result.companies
        if item.evidence_lift_status == "evidence-supported"
    }
    assert evidence_supported == {
        "AMZN",
        "BBY",
        "CVNA",
        "F",
        "GM",
        "HD",
        "LOW",
        "TGT",
        "TSCO",
        "TSLA",
        "TJX",
        "ULTA",
        "WMT",
    }


def test_each_company_exposes_traceability_and_ranked_sensitivity() -> None:
    result = run_attribution(load_attribution_bundle())

    for company in result.companies:
        assert company.revenue_source_url.startswith("https://www.sec.gov/")
        if company.tier1_source_url:
            assert company.tier1_source_url.startswith("https://www.sec.gov/")
        else:
            assert company.tier1_share == 0
        assert company.assumption_ids
        assert len(company.sensitivity_top5) <= 5
        correlations = [item.absolute_correlation for item in company.sensitivity_top5]
        assert correlations == sorted(correlations, reverse=True)
        assert all(
            item.direction in {"increases", "decreases"} for item in company.sensitivity_top5
        )


def test_aggregate_is_not_an_equal_weighted_company_average() -> None:
    result = run_attribution(load_attribution_bundle())
    equal_weighted = sum(item.estimated_dfr_pct_mid for item in result.companies) / len(
        result.companies
    )

    assert not math.isclose(result.aggregate.estimated_dfr_pct_mid, equal_weighted, abs_tol=1e-3)


@pytest.mark.parametrize("draws", [0, 9_999])
def test_attribution_rejects_too_few_draws(draws: int) -> None:
    with pytest.raises(AttributionError, match="at least 10,000"):
        run_attribution(load_attribution_bundle(), draws=draws)


def test_attribution_rejects_negative_seed() -> None:
    with pytest.raises(AttributionError, match="non-negative"):
        run_attribution(load_attribution_bundle(), seed=-1)
