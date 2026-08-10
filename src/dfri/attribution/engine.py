"""Deterministic Monte Carlo attribution over validated public registries."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Final

import numpy as np
import numpy.typing as npt

from dfri.attribution.registry import AttributionBundle, MatrixBEntry, Prior
from dfri.attribution.resilience import SourceDegradation, resolve_assumption_sources

DEFAULT_DRAWS: Final = 20_000
DEFAULT_SEED: Final = 2_026_080_4
FUNGIBILITY_BASELINE_CATEGORIES: Final = frozenset(
    {
        "general_retail",
        "fungible_consumer",
        "fungible_consumer_nonrevolving",
    }
)


class AttributionError(RuntimeError):
    """Attribution cannot produce a finite, ordered, traceable result."""


@dataclass(frozen=True)
class Sensitivity:
    assumption_id: str
    absolute_correlation: float
    direction: str


@dataclass(frozen=True)
class CompanyEstimate:
    ticker: str
    company_name: str
    quarter: str
    estimated_dfr_pct_low: float
    estimated_dfr_pct_mid: float
    estimated_dfr_pct_high: float
    estimated_debt_funded_revenue_mid_millions: float
    estimated_us_consumer_revenue_mid_millions: float
    fungibility_baseline_dfr_pct_mid: float
    evidence_lift: float
    evidence_lift_status: str
    tier1_share: float
    tier2_share: float
    tier3_share: float
    revenue_source_url: str
    tier1_source_url: str
    tier1_excerpt: str
    assumption_ids: tuple[str, ...]
    sensitivity_top5: tuple[Sensitivity, ...]


@dataclass(frozen=True)
class AggregateEstimate:
    quarter: str
    weighting: str
    estimated_dfr_pct_low: float
    estimated_dfr_pct_mid: float
    estimated_dfr_pct_high: float
    estimated_debt_funded_revenue_mid_millions: float
    estimated_us_consumer_revenue_mid_millions: float
    tier1_share: float
    tier2_share: float
    tier3_share: float


@dataclass(frozen=True)
class AttributionResult:
    methodology_version: str
    data_vintage: str
    first_published_at: str
    source_hash: str
    quarter: str
    draws: int
    seed: int
    evidence_lift_headline: str
    source_degradations: tuple[SourceDegradation, ...]
    aggregate: AggregateEstimate
    companies: tuple[CompanyEstimate, ...]

    def payload(self) -> dict[str, object]:
        return asdict(self)


def run_attribution(
    bundle: AttributionBundle,
    *,
    draws: int = DEFAULT_DRAWS,
    seed: int = DEFAULT_SEED,
    unavailable_source_ids: frozenset[str] = frozenset(),
) -> AttributionResult:
    """Compute company and revenue-weighted aggregate bands from registered priors."""

    if draws < 10_000:
        raise AttributionError("Attribution requires at least 10,000 Monte Carlo draws")
    if seed < 0:
        raise AttributionError("Monte Carlo seed must be non-negative")
    bundle, source_degradations = resolve_assumption_sources(
        bundle,
        unavailable_source_ids=unavailable_source_ids,
    )
    rng = np.random.default_rng(seed)
    assumptions = bundle.assumptions_by_id
    assumption_draws = {
        assumption_id: _draw(rng, assumption.prior, draws)
        for assumption_id, assumption in sorted(assumptions.items())
    }
    flow_draws = {item.debt_product: _draw(rng, item.prior, draws) for item in bundle.flows}
    company_by_ticker = {item.ticker: item for item in bundle.companies}
    numerator_by_ticker = {
        ticker: np.zeros(draws, dtype=np.float64) for ticker in company_by_ticker
    }
    baseline_numerator_by_ticker = {
        ticker: np.zeros(draws, dtype=np.float64) for ticker in company_by_ticker
    }
    has_company_specific_evidence = dict.fromkeys(company_by_ticker, False)
    tier_by_ticker = {
        ticker: {tier: np.zeros(draws, dtype=np.float64) for tier in (1, 2, 3)}
        for ticker in company_by_ticker
    }
    used_by_ticker: dict[str, set[str]] = {ticker: set() for ticker in company_by_ticker}

    b_by_category: dict[str, list[MatrixBEntry]] = {}
    for b_row in bundle.matrix_b:
        b_by_category.setdefault(b_row.spend_category, []).append(b_row)
    for a_row in bundle.matrix_a:
        a_draw = _registered_draw(a_row.prior, a_row.assumption_ids, assumption_draws)
        for b_row in b_by_category.get(a_row.spend_category, []):
            b_assumption_ids = b_row.assumption_ids
            b_draw = (
                _registered_draw(b_row.prior, b_assumption_ids, assumption_draws)
                if b_assumption_ids
                else np.full(draws, b_row.prior.mid, dtype=np.float64)
            )
            contribution = flow_draws[a_row.debt_product] * a_draw * b_draw
            numerator_by_ticker[b_row.ticker] += contribution
            if a_row.spend_category in FUNGIBILITY_BASELINE_CATEGORIES:
                baseline_numerator_by_ticker[b_row.ticker] += contribution
            else:
                has_company_specific_evidence[b_row.ticker] = True
            tier_by_ticker[b_row.ticker][a_row.tier] += contribution
            used_by_ticker[b_row.ticker].update(a_row.assumption_ids)
            used_by_ticker[b_row.ticker].update(b_assumption_ids)

    estimates: list[CompanyEstimate] = []
    denominator_by_ticker: dict[str, npt.NDArray[np.float64]] = {}
    ratio_by_ticker: dict[str, npt.NDArray[np.float64]] = {}
    for ticker, company in sorted(company_by_ticker.items()):
        share_id = company.consumer_share_assumption_id
        denominator = company.revenue_total_millions / 4.0 * assumption_draws[share_id]
        if np.any(denominator <= 0):
            raise AttributionError(f"Non-positive denominator draw for {ticker}")
        ratio = numerator_by_ticker[ticker] / denominator * 100.0
        baseline_ratio = baseline_numerator_by_ticker[ticker] / denominator * 100.0
        _finite(ratio, f"company ratio {ticker}")
        _finite(baseline_ratio, f"fungibility baseline {ticker}")
        denominator_by_ticker[ticker] = denominator
        ratio_by_ticker[ticker] = ratio
        used_by_ticker[ticker].add(share_id)
        pct = _quantiles(ratio)
        baseline_mid = float(np.quantile(baseline_ratio, 0.5))
        if baseline_mid <= 0:
            raise AttributionError(f"Non-positive fungibility baseline for {ticker}")
        evidence_lift = pct[1] / baseline_mid
        if not math.isfinite(evidence_lift) or evidence_lift < 1:
            raise AttributionError(f"Invalid evidence lift for {ticker}")
        tier_shares = _tier_shares(tier_by_ticker[ticker])
        sensitivity = _sensitivities(ratio, used_by_ticker[ticker], assumption_draws)
        estimates.append(
            CompanyEstimate(
                ticker=ticker,
                company_name=company.company_name,
                quarter=next(iter({item.quarter for item in bundle.flows})),
                estimated_dfr_pct_low=pct[0],
                estimated_dfr_pct_mid=pct[1],
                estimated_dfr_pct_high=pct[2],
                estimated_debt_funded_revenue_mid_millions=float(
                    np.quantile(numerator_by_ticker[ticker], 0.5)
                ),
                estimated_us_consumer_revenue_mid_millions=float(np.quantile(denominator, 0.5)),
                fungibility_baseline_dfr_pct_mid=baseline_mid,
                evidence_lift=evidence_lift,
                evidence_lift_status=(
                    "evidence-supported"
                    if has_company_specific_evidence[ticker]
                    else "baseline-only"
                ),
                tier1_share=tier_shares[0],
                tier2_share=tier_shares[1],
                tier3_share=tier_shares[2],
                revenue_source_url=company.revenue_source_url,
                tier1_source_url=company.tier1_source_url,
                tier1_excerpt=company.tier1_excerpt,
                assumption_ids=tuple(sorted(used_by_ticker[ticker])),
                sensitivity_top5=sensitivity,
            )
        )

    aggregate_numerator = sum(numerator_by_ticker.values(), np.zeros(draws, dtype=np.float64))
    aggregate_denominator = sum(denominator_by_ticker.values(), np.zeros(draws, dtype=np.float64))
    aggregate_ratio = aggregate_numerator / aggregate_denominator * 100.0
    aggregate_pct = _quantiles(aggregate_ratio)
    aggregate_tiers = {
        tier: sum(
            (values[tier] for values in tier_by_ticker.values()),
            np.zeros(draws, dtype=np.float64),
        )
        for tier in (1, 2, 3)
    }
    aggregate_shares = _tier_shares(aggregate_tiers)
    quarter = next(iter({item.quarter for item in bundle.flows}))
    top_lift = max(estimates, key=lambda item: (item.evidence_lift, item.ticker))
    median_lift = float(np.median([item.evidence_lift for item in estimates]))
    evidence_lift_headline = (
        f"{top_lift.company_name} has the highest Evidence Lift at "
        f"{top_lift.evidence_lift:.2f}x versus {median_lift:.2f}x for the median covered company."
    )
    return AttributionResult(
        methodology_version=bundle.methodology_version,
        data_vintage=bundle.data_vintage,
        first_published_at=bundle.first_published_at,
        source_hash=bundle.source_hash,
        quarter=quarter,
        draws=draws,
        seed=seed,
        evidence_lift_headline=evidence_lift_headline,
        source_degradations=source_degradations,
        aggregate=AggregateEstimate(
            quarter=quarter,
            weighting="revenue-weighted",
            estimated_dfr_pct_low=aggregate_pct[0],
            estimated_dfr_pct_mid=aggregate_pct[1],
            estimated_dfr_pct_high=aggregate_pct[2],
            estimated_debt_funded_revenue_mid_millions=float(np.quantile(aggregate_numerator, 0.5)),
            estimated_us_consumer_revenue_mid_millions=float(
                np.quantile(aggregate_denominator, 0.5)
            ),
            tier1_share=aggregate_shares[0],
            tier2_share=aggregate_shares[1],
            tier3_share=aggregate_shares[2],
        ),
        companies=tuple(estimates),
    )


def _draw(rng: np.random.Generator, prior: Prior, draws: int) -> npt.NDArray[np.float64]:
    if prior.low == prior.high:
        return np.full(draws, prior.mid, dtype=np.float64)
    return rng.triangular(prior.low, prior.mid, prior.high, size=draws)


def _registered_draw(
    prior: Prior,
    assumption_ids: tuple[str, ...],
    assumption_draws: dict[str, npt.NDArray[np.float64]],
) -> npt.NDArray[np.float64]:
    if len(assumption_ids) != 1:
        raise AttributionError(f"Expected exactly one assumption for prior {prior}")
    return assumption_draws[assumption_ids[0]]


def _quantiles(values: npt.NDArray[np.float64]) -> tuple[float, float, float]:
    raw = np.quantile(values, (0.1, 0.5, 0.9))
    quantiles = (float(raw[0]), float(raw[1]), float(raw[2]))
    if not quantiles[0] <= quantiles[1] <= quantiles[2]:
        raise AttributionError("Monte Carlo quantiles are not ordered")
    return quantiles


def _tier_shares(
    tier_values: dict[int, npt.NDArray[np.float64]],
) -> tuple[float, float, float]:
    mids = [float(np.quantile(tier_values[tier], 0.5)) for tier in (1, 2, 3)]
    if any(value < 0 for value in mids):
        raise AttributionError("Tier contribution mids must be non-negative")
    total = sum(mids)
    if total <= 0:
        raise AttributionError("Tier contributions must have a positive midpoint")
    raw = [value / total for value in mids]
    shares = (raw[0], raw[1], 1.0 - raw[0] - raw[1])
    if not math.isclose(sum(shares), 1.0, abs_tol=1e-12):
        raise AttributionError("Tier shares do not sum to one")
    return shares


def _sensitivities(
    ratio: npt.NDArray[np.float64],
    used_ids: set[str],
    assumption_draws: dict[str, npt.NDArray[np.float64]],
) -> tuple[Sensitivity, ...]:
    ranked: list[Sensitivity] = []
    for assumption_id in sorted(used_ids):
        values = assumption_draws[assumption_id]
        if np.ptp(values) == 0:
            continue
        correlation = float(np.corrcoef(values, ratio)[0, 1])
        if not math.isfinite(correlation):
            continue
        ranked.append(
            Sensitivity(
                assumption_id=assumption_id,
                absolute_correlation=abs(correlation),
                direction="increases" if correlation >= 0 else "decreases",
            )
        )
    ranked.sort(key=lambda item: (-item.absolute_correlation, item.assumption_id))
    return tuple(ranked[:5])


def _finite(values: npt.NDArray[np.float64], label: str) -> None:
    if not np.all(np.isfinite(values)):
        raise AttributionError(f"Non-finite {label}")
