"""Versioned M3 assumption and matrix registries with fail-closed validation."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from importlib import resources
from typing import Any, Final


class AttributionRegistryError(RuntimeError):
    """A source registry cannot support a reproducible attribution."""


@dataclass(frozen=True)
class Prior:
    low: float
    mid: float
    high: float

    def validate(self, label: str) -> None:
        if not all(math.isfinite(value) for value in (self.low, self.mid, self.high)):
            raise AttributionRegistryError(f"{label} prior must be finite")
        if not self.low <= self.mid <= self.high:
            raise AttributionRegistryError(f"{label} prior must satisfy low <= mid <= high")


@dataclass(frozen=True)
class Assumption:
    assumption_id: str
    statement: str
    prior: Prior
    tier: int
    source_url: str
    evidence_snippet: str
    sensitivity_note: str
    version: str
    active: bool


@dataclass(frozen=True)
class MatrixAEntry:
    version: str
    debt_product: str
    spend_category: str
    prior: Prior
    tier: int
    assumption_ids: tuple[str, ...]


@dataclass(frozen=True)
class MatrixBEntry:
    version: str
    spend_category: str
    ticker: str
    prior: Prior
    method: str
    evidence_refs: tuple[str, ...]

    @property
    def assumption_ids(self) -> tuple[str, ...]:
        return tuple(item for item in self.evidence_refs if item.startswith("A-"))


@dataclass(frozen=True)
class CompanyInput:
    ticker: str
    company_name: str
    cik: str
    period: str
    revenue_total_millions: float
    revenue_namespace: str
    revenue_tag: str
    revenue_source_url: str
    consumer_share_assumption_id: str
    tier1_source_url: str
    tier1_excerpt: str
    membership_snapshot_ref: str


@dataclass(frozen=True)
class FlowInput:
    debt_product: str
    quarter: str
    prior: Prior
    unit: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class AttributionBundle:
    methodology_version: str
    data_vintage: str
    assumptions: tuple[Assumption, ...]
    matrix_a: tuple[MatrixAEntry, ...]
    matrix_b: tuple[MatrixBEntry, ...]
    companies: tuple[CompanyInput, ...]
    flows: tuple[FlowInput, ...]
    source_hash: str

    @property
    def assumptions_by_id(self) -> dict[str, Assumption]:
        return {item.assumption_id: item for item in self.assumptions}


_RESOURCE_FILES: Final = (
    "assumption_registry_v1.json",
    "matrix_a_v1.json",
    "matrix_b_v1.json",
    "company_inputs_v1.json",
    "flow_inputs_v1.json",
)


def load_attribution_bundle() -> AttributionBundle:
    """Load all public M3 inputs and prove their cross-file contracts."""

    payloads: dict[str, dict[str, Any]] = {}
    digest = hashlib.sha256()
    for filename in _RESOURCE_FILES:
        raw = resources.files("dfri.attribution").joinpath(filename).read_bytes()
        digest.update(filename.encode())
        digest.update(b"\0")
        digest.update(raw)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise AttributionRegistryError(f"{filename} must contain a JSON object")
        payloads[filename] = value

    assumptions_payload = payloads["assumption_registry_v1.json"]
    matrix_a_payload = payloads["matrix_a_v1.json"]
    matrix_b_payload = payloads["matrix_b_v1.json"]
    companies_payload = payloads["company_inputs_v1.json"]
    flows_payload = payloads["flow_inputs_v1.json"]
    versions = {_required_str(payload, "methodology_version") for payload in payloads.values()}
    if len(versions) != 1:
        raise AttributionRegistryError("Attribution registry methodology versions differ")

    assumptions = tuple(_assumption(item) for item in _items(assumptions_payload))
    matrix_a = tuple(_matrix_a(item) for item in _items(matrix_a_payload))
    matrix_b = tuple(_matrix_b(item) for item in _items(matrix_b_payload))
    companies = tuple(_company(item) for item in _items(companies_payload))
    flows = tuple(_flow(item) for item in _items(flows_payload))
    bundle = AttributionBundle(
        methodology_version=versions.pop(),
        data_vintage=_required_str(flows_payload, "data_vintage"),
        assumptions=assumptions,
        matrix_a=matrix_a,
        matrix_b=matrix_b,
        companies=companies,
        flows=flows,
        source_hash=digest.hexdigest(),
    )
    validate_attribution_bundle(bundle)
    return bundle


def validate_attribution_bundle(bundle: AttributionBundle) -> None:
    """Enforce evidence coverage, matrix bounds, and the exact P0 issuer contract."""

    if not bundle.methodology_version:
        raise AttributionRegistryError("Methodology version is required")
    assumptions = bundle.assumptions_by_id
    if len(assumptions) != len(bundle.assumptions):
        raise AttributionRegistryError("Assumption IDs must be unique")
    for assumption_item in bundle.assumptions:
        assumption_item.prior.validate(assumption_item.assumption_id)
        if assumption_item.tier not in {1, 2, 3}:
            raise AttributionRegistryError(f"Invalid tier for {assumption_item.assumption_id}")
        if not assumption_item.active:
            raise AttributionRegistryError(
                f"Referenced registry contains inactive input: {assumption_item.assumption_id}"
            )
        if not all(
            (
                assumption_item.statement,
                assumption_item.source_url,
                assumption_item.evidence_snippet,
                assumption_item.sensitivity_note,
                assumption_item.version,
            )
        ):
            raise AttributionRegistryError(
                f"Assumption evidence is incomplete: {assumption_item.assumption_id}"
            )

    products = {item.debt_product for item in bundle.flows}
    if len(products) != len(bundle.flows):
        raise AttributionRegistryError("Flow inputs must contain one row per debt product")
    quarters = {item.quarter for item in bundle.flows}
    if len(quarters) != 1:
        raise AttributionRegistryError("Flow inputs must describe one quarter")
    for flow_item in bundle.flows:
        flow_item.prior.validate(f"flow {flow_item.debt_product}")
        if not flow_item.unit or not flow_item.evidence_refs:
            raise AttributionRegistryError(f"Flow evidence is incomplete: {flow_item.debt_product}")

    a_identity = [(item.debt_product, item.spend_category) for item in bundle.matrix_a]
    if len(a_identity) != len(set(a_identity)):
        raise AttributionRegistryError("Matrix A product/category identities must be unique")
    for matrix_a_item in bundle.matrix_a:
        matrix_a_item.prior.validate(
            f"Matrix A {matrix_a_item.debt_product}/{matrix_a_item.spend_category}"
        )
        if matrix_a_item.debt_product not in products:
            raise AttributionRegistryError(f"Matrix A has no flow: {matrix_a_item.debt_product}")
        _validate_assumption_refs(matrix_a_item.assumption_ids, assumptions, matrix_a_item.prior)
    for product in sorted(products):
        rows = [item for item in bundle.matrix_a if item.debt_product == product]
        for bound in ("low", "mid", "high"):
            total = sum(getattr(item.prior, bound) for item in rows)
            if total > 1.0 + 1e-12:
                raise AttributionRegistryError(
                    f"Matrix A {product} {bound} weights exceed one: {total}"
                )

    tickers = {item.ticker for item in bundle.companies}
    if len(tickers) != 10 or len(tickers) != len(bundle.companies):
        raise AttributionRegistryError("M3 requires exactly ten unique P0 companies")
    category_names = {item.spend_category for item in bundle.matrix_a}
    b_identity = [(item.spend_category, item.ticker) for item in bundle.matrix_b]
    if len(b_identity) != len(set(b_identity)):
        raise AttributionRegistryError("Matrix B category/ticker identities must be unique")
    for matrix_b_item in bundle.matrix_b:
        matrix_b_item.prior.validate(
            f"Matrix B {matrix_b_item.spend_category}/{matrix_b_item.ticker}"
        )
        if matrix_b_item.prior.low < 0 or matrix_b_item.ticker not in tickers:
            raise AttributionRegistryError(
                f"Invalid Matrix B row: {matrix_b_item.spend_category}/{matrix_b_item.ticker}"
            )
        if matrix_b_item.spend_category not in category_names:
            raise AttributionRegistryError(
                f"Matrix B category has no Matrix A row: {matrix_b_item.spend_category}"
            )
        if not matrix_b_item.method or not matrix_b_item.evidence_refs:
            raise AttributionRegistryError(
                f"Matrix B evidence is missing: {matrix_b_item.spend_category}/"
                f"{matrix_b_item.ticker}"
            )
        if matrix_b_item.assumption_ids:
            _validate_assumption_refs(
                matrix_b_item.assumption_ids, assumptions, matrix_b_item.prior
            )
        elif matrix_b_item.prior.low != matrix_b_item.prior.high:
            raise AttributionRegistryError(
                "Uncertain Matrix B row lacks an assumption: "
                f"{matrix_b_item.spend_category}/{matrix_b_item.ticker}"
            )
    for category in sorted(category_names):
        total_high = sum(
            item.prior.high for item in bundle.matrix_b if item.spend_category == category
        )
        if total_high > 1.0 + 1e-9:
            raise AttributionRegistryError(
                f"Matrix B {category} high weights exceed one: {total_high}"
            )

    issuer_payload = json.loads(
        resources.files("dfri.ingest").joinpath("issuer_registry.json").read_text("utf-8")
    )
    issuer_by_ticker = {item["ticker"]: item for item in issuer_payload["p0"]}
    if tickers != set(issuer_by_ticker):
        raise AttributionRegistryError("Company inputs differ from the verified P0 issuer registry")
    for company_item in bundle.companies:
        issuer = issuer_by_ticker[company_item.ticker]
        expected_revenue = float(issuer["revenue_fact"]["value"]) / 1_000_000
        if not math.isclose(company_item.revenue_total_millions, expected_revenue, abs_tol=1e-9):
            raise AttributionRegistryError(
                f"Revenue differs from verified XBRL fact: {company_item.ticker}"
            )
        if (
            company_item.cik != issuer["cik"]
            or company_item.revenue_tag != issuer["revenue_fact"]["tag"]
        ):
            raise AttributionRegistryError(
                f"Issuer identity differs from verified registry: {company_item.ticker}"
            )
        if company_item.consumer_share_assumption_id not in assumptions:
            raise AttributionRegistryError(
                f"Company denominator assumption is missing: {company_item.ticker}"
            )
        if _word_count(company_item.tier1_excerpt) > 15:
            raise AttributionRegistryError(
                f"Tier 1 excerpt exceeds 15 words: {company_item.ticker}"
            )
        if not all(
            (
                company_item.company_name,
                company_item.period,
                company_item.revenue_namespace,
                company_item.revenue_source_url,
                company_item.tier1_source_url,
                company_item.tier1_excerpt,
                company_item.membership_snapshot_ref,
            )
        ):
            raise AttributionRegistryError(f"Company evidence is incomplete: {company_item.ticker}")

    covered = {item.ticker for item in bundle.matrix_b}
    if covered != tickers:
        raise AttributionRegistryError(
            f"Matrix B coverage differs from P0: {sorted(tickers - covered)}"
        )


def _validate_assumption_refs(
    refs: tuple[str, ...], assumptions: dict[str, Assumption], prior: Prior
) -> None:
    if len(refs) != 1 or refs[0] not in assumptions:
        raise AttributionRegistryError(f"Expected one registered assumption, got {refs}")
    registered = assumptions[refs[0]].prior
    if registered != prior:
        raise AttributionRegistryError(f"Matrix prior differs from {refs[0]}")


def _assumption(item: dict[str, Any]) -> Assumption:
    return Assumption(
        assumption_id=_required_str(item, "assumption_id"),
        statement=_required_str(item, "statement"),
        prior=_prior(item),
        tier=_required_int(item, "tier"),
        source_url=_required_str(item, "source_url"),
        evidence_snippet=_required_str(item, "evidence_snippet"),
        sensitivity_note=_required_str(item, "sensitivity_note"),
        version=_required_str(item, "version"),
        active=_required_bool(item, "active"),
    )


def _matrix_a(item: dict[str, Any]) -> MatrixAEntry:
    return MatrixAEntry(
        version=_required_str(item, "version"),
        debt_product=_required_str(item, "debt_product"),
        spend_category=_required_str(item, "spend_category"),
        prior=_prior(item),
        tier=_required_int(item, "tier"),
        assumption_ids=_string_tuple(item, "assumption_ids"),
    )


def _matrix_b(item: dict[str, Any]) -> MatrixBEntry:
    return MatrixBEntry(
        version=_required_str(item, "version"),
        spend_category=_required_str(item, "spend_category"),
        ticker=_required_str(item, "ticker"),
        prior=_prior(item),
        method=_required_str(item, "method"),
        evidence_refs=_string_tuple(item, "evidence_refs"),
    )


def _company(item: dict[str, Any]) -> CompanyInput:
    return CompanyInput(
        ticker=_required_str(item, "ticker"),
        company_name=_required_str(item, "company_name"),
        cik=_required_str(item, "cik"),
        period=_required_str(item, "period"),
        revenue_total_millions=_required_float(item, "revenue_total_millions"),
        revenue_namespace=_required_str(item, "revenue_namespace"),
        revenue_tag=_required_str(item, "revenue_tag"),
        revenue_source_url=_required_str(item, "revenue_source_url"),
        consumer_share_assumption_id=_required_str(item, "consumer_share_assumption_id"),
        tier1_source_url=_required_str(item, "tier1_source_url"),
        tier1_excerpt=_required_str(item, "tier1_excerpt"),
        membership_snapshot_ref=_required_str(item, "membership_snapshot_ref"),
    )


def _flow(item: dict[str, Any]) -> FlowInput:
    return FlowInput(
        debt_product=_required_str(item, "debt_product"),
        quarter=_required_str(item, "quarter"),
        prior=_prior(item),
        unit=_required_str(item, "unit"),
        evidence_refs=_string_tuple(item, "evidence_refs"),
    )


def _prior(item: dict[str, Any]) -> Prior:
    return Prior(
        low=_required_float(item, "weight_low" if "weight_low" in item else "low"),
        mid=_required_float(item, "weight_mid" if "weight_mid" in item else "mid"),
        high=_required_float(item, "weight_high" if "weight_high" in item else "high"),
    )


def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise AttributionRegistryError("Registry items must be a list of objects")
    return items


def _required_str(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise AttributionRegistryError(f"Required string is missing: {key}")
    return value


def _required_float(item: dict[str, Any], key: str) -> float:
    value = item.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AttributionRegistryError(f"Required number is missing: {key}")
    return float(value)


def _required_int(item: dict[str, Any], key: str) -> int:
    value = item.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AttributionRegistryError(f"Required integer is missing: {key}")
    return value


def _required_bool(item: dict[str, Any], key: str) -> bool:
    value = item.get(key)
    if not isinstance(value, bool):
        raise AttributionRegistryError(f"Required boolean is missing: {key}")
    return value


def _string_tuple(item: dict[str, Any], key: str) -> tuple[str, ...]:
    value = item.get(key)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(entry, str) for entry in value)
    ):
        raise AttributionRegistryError(f"Required string list is missing: {key}")
    return tuple(value)


def _word_count(value: str) -> int:
    return len(value.replace("…", " ").split())
