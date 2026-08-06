from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from dfri.attribution.registry import (
    AttributionRegistryError,
    Prior,
    load_attribution_bundle,
    validate_attribution_bundle,
)


def test_public_attribution_bundle_is_complete_and_source_hashed() -> None:
    bundle = load_attribution_bundle()

    assert bundle.methodology_version == "1.1.1"
    assert bundle.data_vintage == "2026-05-07T19:00:00+00:00"
    assert bundle.first_published_at == "2026-08-06T05:40:24.524787+00:00"
    assert len(bundle.source_hash) == 64
    assert len(bundle.companies) == 50
    assert {
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
    } <= {item.ticker for item in bundle.companies}
    assert {item.quarter for item in bundle.flows} == {"2026-Q1"}
    assert all(len(item.tier1_excerpt.split()) <= 15 for item in bundle.companies)
    assert all(item.source_url.startswith("https://") for item in bundle.assumptions)

    digest = hashlib.sha256()
    root = Path(__file__).parents[2] / "src" / "dfri" / "attribution"
    for filename in (
        "assumption_registry_v1_1_1.json",
        "matrix_a_v1_1_1.json",
        "matrix_b_v1_1_1.json",
        "company_inputs_v1_1_1.json",
        "flow_inputs_v1_1_1.json",
    ):
        payload = json.loads((root / filename).read_text(encoding="utf-8"))
        digest.update(filename.encode())
        digest.update(b"\0")
        digest.update(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    assert bundle.source_hash == digest.hexdigest()


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


def test_historical_v1_bundle_remains_reproducible() -> None:
    bundle = load_attribution_bundle("1.0.0")

    assert bundle.methodology_version == "1.0.0"
    assert len(bundle.companies) == 10
    assert bundle.first_published_at == "2026-08-05T04:17:33.789348+00:00"


def test_historical_v1_1_0_bundle_remains_reproducible() -> None:
    bundle = load_attribution_bundle("1.1.0")

    assert bundle.methodology_version == "1.1.0"
    assert len(bundle.companies) == 50
    cvna = next(item for item in bundle.companies if item.ticker == "CVNA")
    assert cvna.tier1_source_url == ""


def test_registry_rejects_unknown_methodology_version() -> None:
    with pytest.raises(AttributionRegistryError, match="Unsupported"):
        load_attribution_bundle("9.9.9")


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


def test_registry_rejects_missing_p1_company() -> None:
    bundle = load_attribution_bundle()
    with pytest.raises(AttributionRegistryError, match="exactly 50"):
        validate_attribution_bundle(replace(bundle, companies=bundle.companies[:-1]))


def test_registry_rejects_incomplete_company_evidence() -> None:
    bundle = load_attribution_bundle()
    index = next(index for index, item in enumerate(bundle.companies) if item.tier1_source_url)
    company = replace(bundle.companies[index], tier1_excerpt="word " * 16)
    companies = list(bundle.companies)
    companies[index] = company
    with pytest.raises(AttributionRegistryError, match="exceeds 15 words"):
        validate_attribution_bundle(replace(bundle, companies=tuple(companies)))


def test_registry_allows_explicitly_absent_tier1_line_but_not_half_a_line() -> None:
    bundle = load_attribution_bundle()
    company = next(item for item in bundle.companies if not item.tier1_source_url)
    assert company.tier1_excerpt == ""
    broken = replace(company, tier1_excerpt="unsupported claim")
    companies = tuple(broken if item == company else item for item in bundle.companies)
    with pytest.raises(AttributionRegistryError, match="present together"):
        validate_attribution_bundle(replace(bundle, companies=companies))


def test_registry_rejects_revenue_that_does_not_match_pinned_xbrl_fact() -> None:
    bundle = load_attribution_bundle()
    company = replace(
        bundle.companies[0], revenue_total_millions=bundle.companies[0].revenue_total_millions + 1
    )
    with pytest.raises(AttributionRegistryError, match="verified XBRL fact"):
        validate_attribution_bundle(replace(bundle, companies=(company, *bundle.companies[1:])))
