"""Operate the point-in-time SEC 10-Q refresh and append-only attribution ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, time
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Final, cast

import polars as pl

from dfri.attribution.engine import run_attribution
from dfri.attribution.registry import (
    AttributionBundle,
    CompanyInput,
    FlowInput,
    MatrixBEntry,
    Prior,
    load_attribution_bundle,
)
from dfri.ingest.board_targets import DERIVED_SOURCE
from dfri.ingest.edgar import EdgarClient, EdgarJsonReceipt
from dfri.ingest.http import HttpTransport
from dfri.lake.store import AppendOnlyParquetStore, WriteReceipt

TARGET_SERIES: Final = {
    "DELTA_DTCTLR.M": "revolving_credit",
    "DELTA_DTCTLN.M": "nonrevolving_credit",
}
REWEIGHTED_CATEGORIES: Final = (
    "general_retail",
    "fungible_consumer",
    "fungible_consumer_nonrevolving",
)
AUTO_TICKERS: Final = ("CVNA", "F", "GM", "TSLA")
REFRESH_ID_PREFIX: Final = "qrf_"


class QuarterlyRefreshError(RuntimeError):
    """A quarterly input, append boundary, or point-in-time contract failed."""


@dataclass(frozen=True)
class FilingIdentity:
    form: str
    filed_at: date
    period: date
    accession: str
    primary_document: str


@dataclass(frozen=True)
class SelectedFact:
    start: date
    end: date
    filed_at: date
    accession: str
    value: float


@dataclass(frozen=True)
class CompanyRefreshInput:
    ticker: str
    status: str
    period: date
    revenue_total_millions: float
    revenue_source_url: str
    annual_value_millions: float
    current_ytd_millions: float | None
    prior_ytd_millions: float | None
    annual_accession: str
    annual_filed_at: date
    quarterly_accession: str | None
    quarterly_filed_at: date | None
    revenue_tag: str
    selected_facts_hash: str

    def payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class QuarterlyFlows:
    quarter: str
    data_vintage: datetime
    inputs: tuple[FlowInput, ...]


@dataclass(frozen=True)
class QuarterlyRefreshRecord:
    refresh_id: str
    target_quarter: str
    effective_at: datetime
    data_vintage: datetime
    methodology_version: str
    source_hash: str
    company_count: int
    updated_company_count: int
    payload_json: str

    def row(self) -> dict[str, object]:
        return asdict(self)

    def payload(self) -> dict[str, object]:
        value = json.loads(self.payload_json)
        if not isinstance(value, dict):
            raise QuarterlyRefreshError("Stored quarterly refresh payload is not an object")
        return cast(dict[str, object], value)


@dataclass(frozen=True)
class QuarterlyRefreshAppend:
    refresh_id: str
    appended: bool
    storage: WriteReceipt | None


class QuarterlyRefreshLedger:
    """One atomic content-addressed row per complete 50-company refresh."""

    def __init__(self, store: AppendOnlyParquetStore) -> None:
        self._store = store

    def append(self, record: QuarterlyRefreshRecord) -> QuarterlyRefreshAppend:
        records = self.read_all()
        for semantic_match in records:
            if refresh_identity(semantic_match) != refresh_identity(record):
                continue
            _assert_compatible_refresh_metadata(semantic_match, record)
            return QuarterlyRefreshAppend(semantic_match.refresh_id, False, None)
        existing = {item.refresh_id: item for item in records}
        id_match = existing.get(record.refresh_id)
        if id_match is not None:
            if id_match != record:
                raise QuarterlyRefreshError(
                    f"Quarterly refresh {record.refresh_id} already exists with different content"
                )
            return QuarterlyRefreshAppend(record.refresh_id, False, None)
        receipt = self._store.append("attribution_refreshes", [record.row()])
        return QuarterlyRefreshAppend(record.refresh_id, True, receipt)

    def read_all(self) -> tuple[QuarterlyRefreshRecord, ...]:
        frame = self._store.read_table("attribution_refreshes")
        records = tuple(_record_from_row(row) for row in frame.iter_rows(named=True))
        by_id: dict[str, QuarterlyRefreshRecord] = {}
        for record in records:
            current = by_id.get(record.refresh_id)
            if current is not None and current != record:
                raise QuarterlyRefreshError(
                    f"Quarterly refresh {record.refresh_id} has conflicting stored rows"
                )
            by_id[record.refresh_id] = record
        return tuple(sorted(by_id.values(), key=lambda item: (item.effective_at, item.refresh_id)))


def latest_complete_quarter(store: AppendOnlyParquetStore, *, as_of: datetime) -> QuarterlyFlows:
    """Return the latest calendar quarter with all six first-print monthly flow rows."""

    _aware(as_of)
    frame = store.read_table("raw_observations").filter(
        (pl.col("source") == DERIVED_SOURCE) & (pl.col("release_date") <= as_of)
    )
    rows: dict[tuple[int, int], dict[str, list[dict[str, object]]]] = {}
    for row in frame.iter_rows(named=True):
        series_id = str(row["series_id"])
        if series_id not in TARGET_SERIES:
            continue
        period = cast(date, row["obs_period"])
        quarter = (period.year, (period.month - 1) // 3 + 1)
        rows.setdefault(quarter, {}).setdefault(series_id, []).append(row)
    complete: list[tuple[tuple[int, int], dict[str, list[dict[str, object]]]]] = []
    for quarter, by_series in rows.items():
        expected_months = set(range((quarter[1] - 1) * 3 + 1, quarter[1] * 3 + 1))
        if set(by_series) != set(TARGET_SERIES):
            continue
        if all(
            {cast(date, row["obs_period"]).month for row in by_series[series]} == expected_months
            and len(by_series[series]) == 3
            for series in TARGET_SERIES
        ):
            complete.append((quarter, by_series))
    if not complete:
        raise QuarterlyRefreshError("No complete three-month G.19 quarter is available")
    (year, quarter_number), selected = max(complete, key=lambda item: item[0])
    all_rows = [row for series_rows in selected.values() for row in series_rows]
    data_vintage = max(cast(datetime, row["release_date"]) for row in all_rows)
    inputs: list[FlowInput] = []
    for series_id, debt_product in TARGET_SERIES.items():
        series_rows = sorted(selected[series_id], key=lambda row: cast(date, row["obs_period"]))
        value = sum(float(cast(float, row["value"])) for row in series_rows)
        evidence = tuple(
            sorted(
                {str(row["source_url"]) for row in series_rows}
                | {f"sha256:{row['checksum']}" for row in series_rows}
            )
        )
        inputs.append(
            FlowInput(
                debt_product=debt_product,
                quarter=f"{year}-Q{quarter_number}",
                prior=Prior(value, value, value),
                unit="Millions of U.S. Dollars",
                evidence_refs=evidence,
            )
        )
    return QuarterlyFlows(f"{year}-Q{quarter_number}", data_vintage, tuple(inputs))


def select_company_refresh(
    company: CompanyInput,
    submissions: EdgarJsonReceipt,
    companyfacts: EdgarJsonReceipt,
    *,
    as_of: datetime,
    quarter_end: date,
) -> CompanyRefreshInput:
    """Select a point-in-time annual fact and apply a same-tag TTM 10-Q update when possible."""

    _aware(as_of)
    if _cik_value(submissions.payload.get("cik")) != int(company.cik):
        raise QuarterlyRefreshError(f"SEC submissions CIK differs for {company.ticker}")
    if _cik_value(companyfacts.payload.get("cik")) != int(company.cik):
        raise QuarterlyRefreshError(f"SEC companyfacts CIK differs for {company.ticker}")
    facts = _revenue_facts(company, companyfacts)
    annual = _select_annual(facts, as_of.date(), quarter_end)
    filings = _recent_filings(submissions)
    annual_filing = _filing_by_accession(filings, annual.accession, "10-K")
    annual_url = _filing_url(company.cik, annual_filing)
    latest_quarter = _select_quarter_filing(
        filings,
        as_of=as_of.date(),
        quarter_end=quarter_end,
        after_period=annual.end,
    )
    selected = [annual]
    status = "BASELINE_NO_NEW_10Q"
    revenue = annual.value
    period = annual.end
    current_ytd: SelectedFact | None = None
    prior_ytd: SelectedFact | None = None
    quarterly_url: str | None = None
    if latest_quarter is not None:
        current_ytd, prior_ytd = _select_ytd_pair(facts, latest_quarter)
        if current_ytd is not None and prior_ytd is not None:
            revenue = annual.value + current_ytd.value - prior_ytd.value
            if not math.isfinite(revenue) or revenue <= 0:
                raise QuarterlyRefreshError(f"Non-positive TTM revenue for {company.ticker}")
            if not 0.25 <= revenue / annual.value <= 2.5:
                raise QuarterlyRefreshError(f"Implausible TTM revenue change for {company.ticker}")
            selected.extend((current_ytd, prior_ytd))
            status = "UPDATED_TTM_FROM_10Q"
            period = current_ytd.end
            quarterly_url = _filing_url(company.cik, latest_quarter)
    selected_hash = hashlib.sha256(
        _canonical([_fact_payload(item) for item in selected])
    ).hexdigest()
    return CompanyRefreshInput(
        ticker=company.ticker,
        status=status,
        period=period,
        revenue_total_millions=revenue / 1_000_000,
        revenue_source_url=quarterly_url or annual_url,
        annual_value_millions=annual.value / 1_000_000,
        current_ytd_millions=current_ytd.value / 1_000_000 if current_ytd else None,
        prior_ytd_millions=prior_ytd.value / 1_000_000 if prior_ytd else None,
        annual_accession=annual.accession,
        annual_filed_at=annual_filing.filed_at,
        quarterly_accession=(
            latest_quarter.accession if latest_quarter is not None and quarterly_url else None
        ),
        quarterly_filed_at=(
            latest_quarter.filed_at if latest_quarter is not None and quarterly_url else None
        ),
        revenue_tag=company.revenue_tag,
        selected_facts_hash=selected_hash,
    )


def build_refresh_record(
    base: AttributionBundle,
    flows: QuarterlyFlows,
    company_inputs: Sequence[CompanyRefreshInput],
) -> QuarterlyRefreshRecord:
    """Reweight Matrix B, recompute all companies, and create an immutable record."""

    if len(company_inputs) != 50 or len({item.ticker for item in company_inputs}) != 50:
        raise QuarterlyRefreshError("Quarterly refresh requires exactly 50 unique companies")
    refresh_by_ticker = {item.ticker: item for item in company_inputs}
    if set(refresh_by_ticker) != {item.ticker for item in base.companies}:
        raise QuarterlyRefreshError("Quarterly refresh company coverage differs from methodology")
    companies = tuple(
        replace(
            company,
            period=refresh_by_ticker[company.ticker].period.isoformat(),
            revenue_total_millions=refresh_by_ticker[company.ticker].revenue_total_millions,
            revenue_source_url=refresh_by_ticker[company.ticker].revenue_source_url,
        )
        for company in base.companies
    )
    matrix_b = _reweighted_matrix_b(base, companies)
    source_material = {
        "methodology_version": base.methodology_version,
        "flows": [asdict(item) for item in flows.inputs],
        "companies": [
            item.payload() for item in sorted(company_inputs, key=lambda row: row.ticker)
        ],
    }
    source_hash = hashlib.sha256(_canonical(source_material)).hexdigest()
    filing_dates = [
        datetime.combine(item.quarterly_filed_at or item.annual_filed_at, time.max, tzinfo=UTC)
        for item in company_inputs
    ]
    effective_at = max([flows.data_vintage, *filing_dates])
    bundle = replace(
        base,
        data_vintage=flows.data_vintage.isoformat(),
        first_published_at=effective_at.isoformat(),
        matrix_b=matrix_b,
        companies=companies,
        flows=flows.inputs,
        source_hash=source_hash,
    )
    result = run_attribution(bundle)
    updated = sum(item.status == "UPDATED_TTM_FROM_10Q" for item in company_inputs)
    payload: dict[str, object] = {
        "schema_version": "v1",
        "target_quarter": flows.quarter,
        "effective_at": effective_at.isoformat(),
        "data_vintage": flows.data_vintage.isoformat(),
        "methodology_version": base.methodology_version,
        "source_hash": source_hash,
        "status": "PASS",
        "company_count": len(company_inputs),
        "updated_company_count": updated,
        "flow_inputs": [asdict(item) for item in flows.inputs],
        "company_inputs": [
            item.payload() for item in sorted(company_inputs, key=lambda row: row.ticker)
        ],
        "result": result.payload(),
    }
    # Derived sensitivity correlations can differ by machine epsilon across BLAS
    # implementations. Refresh identity belongs to the pinned inputs and method,
    # not to a platform-specific serialization of derived floating-point output.
    identity = {
        "methodology_version": base.methodology_version,
        "source_hash": source_hash,
        "target_quarter": flows.quarter,
    }
    refresh_id = REFRESH_ID_PREFIX + hashlib.sha256(_canonical(identity)).hexdigest()[:24]
    payload["refresh_id"] = refresh_id
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default)
    return QuarterlyRefreshRecord(
        refresh_id=refresh_id,
        target_quarter=flows.quarter,
        effective_at=effective_at,
        data_vintage=flows.data_vintage,
        methodology_version=base.methodology_version,
        source_hash=source_hash,
        company_count=len(company_inputs),
        updated_company_count=updated,
        payload_json=payload_json,
    )


def run_live_refresh(
    raw_store: AppendOnlyParquetStore,
    ledger: QuarterlyRefreshLedger,
    client: EdgarClient,
    *,
    as_of: datetime,
) -> tuple[QuarterlyRefreshRecord, QuarterlyRefreshAppend]:
    base = load_attribution_bundle()
    flows = latest_complete_quarter(raw_store, as_of=as_of)
    quarter_end = _quarter_end(flows.quarter)
    inputs = tuple(
        select_company_refresh(
            company,
            client.submissions(company.cik),
            client.companyfacts(company.cik),
            as_of=as_of,
            quarter_end=quarter_end,
        )
        for company in base.companies
    )
    record = build_refresh_record(base, flows, inputs)
    append = ledger.append(record)
    if not append.appended:
        matches = [item for item in ledger.read_all() if item.refresh_id == append.refresh_id]
        if len(matches) != 1:
            raise QuarterlyRefreshError(
                f"Existing quarterly refresh identity is ambiguous: {append.refresh_id}"
            )
        record = matches[0]
    return record, append


def refresh_identity(record: QuarterlyRefreshRecord) -> tuple[str, str, str]:
    """Return the source-semantic identity used across legacy and current IDs."""

    return (record.methodology_version, record.target_quarter, record.source_hash)


def _assert_compatible_refresh_metadata(
    current: QuarterlyRefreshRecord, candidate: QuarterlyRefreshRecord
) -> None:
    current_vintage = current.data_vintage.astimezone(UTC)
    candidate_vintage = candidate.data_vintage.astimezone(UTC)
    current_effective = current.effective_at.astimezone(UTC)
    candidate_effective = candidate.effective_at.astimezone(UTC)
    if (
        current_vintage != candidate_vintage
        or current_effective != candidate_effective
        or current.company_count != candidate.company_count
        or current.updated_company_count != candidate.updated_company_count
    ):
        raise QuarterlyRefreshError(
            "Quarterly refresh source identity already exists with conflicting metadata"
        )


def write_refresh_report(path: Path, record: QuarterlyRefreshRecord) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(record.payload(), indent=2, sort_keys=True, default=_json_default) + "\n"
    ).encode()
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)
    return path


def load_refresh_report(path: Path) -> QuarterlyRefreshRecord:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QuarterlyRefreshError(f"Cannot load quarterly refresh report: {path}") from exc
    if not isinstance(payload, dict):
        raise QuarterlyRefreshError("Quarterly refresh report must be an object")
    required = {
        "refresh_id",
        "target_quarter",
        "effective_at",
        "data_vintage",
        "methodology_version",
        "source_hash",
        "company_count",
        "updated_company_count",
        "result",
    }
    if not required.issubset(payload) or payload.get("schema_version") != "v1":
        raise QuarterlyRefreshError("Quarterly refresh report fields are incomplete")
    result = payload.get("result")
    companies = result.get("companies") if isinstance(result, dict) else None
    if not isinstance(companies, list) or len(companies) != 50:
        raise QuarterlyRefreshError("Quarterly refresh report must contain 50 company results")
    record = QuarterlyRefreshRecord(
        refresh_id=_text(payload, "refresh_id"),
        target_quarter=_text(payload, "target_quarter"),
        effective_at=_timestamp(payload, "effective_at"),
        data_vintage=_timestamp(payload, "data_vintage"),
        methodology_version=_text(payload, "methodology_version"),
        source_hash=_text(payload, "source_hash"),
        company_count=_integer(payload, "company_count"),
        updated_company_count=_integer(payload, "updated_company_count"),
        payload_json=json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=_json_default
        ),
    )
    if record.company_count != 50 or not record.refresh_id.startswith(REFRESH_ID_PREFIX):
        raise QuarterlyRefreshError("Quarterly refresh report identity is invalid")
    return record


def _revenue_facts(company: CompanyInput, receipt: EdgarJsonReceipt) -> list[dict[str, object]]:
    raw_facts = receipt.payload.get("facts")
    if not isinstance(raw_facts, dict):
        raise QuarterlyRefreshError(f"SEC facts are missing for {company.ticker}")
    namespace = raw_facts.get(company.revenue_namespace)
    if not isinstance(namespace, dict):
        raise QuarterlyRefreshError(f"SEC namespace changed for {company.ticker}")
    concept = namespace.get(company.revenue_tag)
    if not isinstance(concept, dict):
        raise QuarterlyRefreshError(f"Pinned revenue tag changed for {company.ticker}")
    units = concept.get("units")
    rows = units.get("USD") if isinstance(units, dict) else None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise QuarterlyRefreshError(f"USD revenue facts are missing for {company.ticker}")
    return cast(list[dict[str, object]], rows)


def _select_annual(
    facts: Sequence[dict[str, object]], as_of: date, quarter_end: date
) -> SelectedFact:
    selected = [
        fact
        for row in facts
        if row.get("form") == "10-K"
        and row.get("fp") == "FY"
        and (fact := _selected_fact(row)) is not None
        and fact.filed_at <= as_of
        and fact.end <= quarter_end
        and 300 <= (fact.end - fact.start).days <= 430
    ]
    if not selected:
        raise QuarterlyRefreshError("No point-in-time annual revenue fact is available")
    return max(selected, key=lambda item: (item.end, item.filed_at, item.start))


def _select_ytd_pair(
    facts: Sequence[dict[str, object]], filing: FilingIdentity
) -> tuple[SelectedFact | None, SelectedFact | None]:
    accession_facts = [
        fact
        for row in facts
        if row.get("form") == "10-Q"
        and row.get("accn") == filing.accession
        and (fact := _selected_fact(row)) is not None
    ]
    current = [item for item in accession_facts if item.end == filing.period]
    if not current:
        return None, None
    current_ytd = min(current, key=lambda item: item.start)
    duration = (current_ytd.end - current_ytd.start).days
    prior = [
        item
        for item in accession_facts
        if 330 <= (current_ytd.end - item.end).days <= 400
        and abs((item.end - item.start).days - duration) <= 14
    ]
    if not prior:
        return None, None
    prior_ytd = max(prior, key=lambda item: item.end)
    return current_ytd, prior_ytd


def _selected_fact(row: Mapping[str, object]) -> SelectedFact | None:
    try:
        start = date.fromisoformat(cast(str, row["start"]))
        end = date.fromisoformat(cast(str, row["end"]))
        filed = date.fromisoformat(cast(str, row["filed"]))
        accession = cast(str, row["accn"])
        raw_value = row["val"]
    except (KeyError, TypeError, ValueError):
        return None
    if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
        return None
    value = float(raw_value)
    if not accession or not math.isfinite(value):
        return None
    return SelectedFact(start, end, filed, accession, value)


def _recent_filings(receipt: EdgarJsonReceipt) -> tuple[FilingIdentity, ...]:
    filings = receipt.payload.get("filings")
    recent = filings.get("recent") if isinstance(filings, dict) else None
    if not isinstance(recent, dict):
        raise QuarterlyRefreshError("SEC submissions recent filings are missing")
    required = ("form", "filingDate", "reportDate", "accessionNumber", "primaryDocument")
    values = [recent.get(key) for key in required]
    if not all(isinstance(value, list) for value in values):
        raise QuarterlyRefreshError("SEC submissions filing columns are missing")
    columns = cast(list[list[object]], values)
    if len({len(value) for value in columns}) != 1:
        raise QuarterlyRefreshError("SEC submissions filing columns differ in length")
    result: list[FilingIdentity] = []
    for form, filed, period, accession, document in zip(*columns, strict=True):
        if form not in {"10-K", "10-Q"}:
            continue
        try:
            result.append(
                FilingIdentity(
                    form=cast(str, form),
                    filed_at=date.fromisoformat(cast(str, filed)),
                    period=date.fromisoformat(cast(str, period)),
                    accession=cast(str, accession),
                    primary_document=cast(str, document),
                )
            )
        except (TypeError, ValueError) as exc:
            raise QuarterlyRefreshError("SEC filing identity is invalid") from exc
    return tuple(result)


def _filing_by_accession(
    filings: Sequence[FilingIdentity], accession: str, form: str
) -> FilingIdentity:
    matches = [item for item in filings if item.accession == accession and item.form == form]
    if len(matches) != 1:
        raise QuarterlyRefreshError(f"SEC filing identity is missing for {accession}")
    return matches[0]


def _select_quarter_filing(
    filings: Sequence[FilingIdentity], *, as_of: date, quarter_end: date, after_period: date
) -> FilingIdentity | None:
    eligible = [
        item
        for item in filings
        if item.form == "10-Q"
        and item.filed_at <= as_of
        and after_period < item.period <= quarter_end
    ]
    return max(eligible, key=lambda item: (item.period, item.filed_at)) if eligible else None


def _reweighted_matrix_b(
    base: AttributionBundle, companies: Sequence[CompanyInput]
) -> tuple[MatrixBEntry, ...]:
    assumptions = base.assumptions_by_id
    denominators = {
        company.ticker: Decimal(str(company.revenue_total_millions))
        * Decimal(str(assumptions[company.consumer_share_assumption_id].prior.mid))
        for company in companies
    }
    general = _normalized_weights(denominators)
    auto = _normalized_weights({ticker: denominators[ticker] for ticker in AUTO_TICKERS})
    rows = [
        item
        for item in base.matrix_b
        if item.spend_category not in {*REWEIGHTED_CATEGORIES, "auto_market"}
    ]
    by_ticker = {item.ticker: item for item in companies}
    for category in REWEIGHTED_CATEGORIES:
        rows.extend(
            _fixed_b_row(
                category,
                ticker,
                weight,
                by_ticker[ticker],
                "consumer",
                base.methodology_version,
            )
            for ticker, weight in sorted(general.items())
        )
    rows.extend(
        _fixed_b_row(
            "auto_market",
            ticker,
            weight,
            by_ticker[ticker],
            "covered-auto",
            base.methodology_version,
        )
        for ticker, weight in sorted(auto.items())
    )
    return tuple(sorted(rows, key=lambda item: (item.spend_category, item.ticker)))


def _fixed_b_row(
    category: str,
    ticker: str,
    weight: float,
    company: CompanyInput,
    label: str,
    methodology_version: str,
) -> MatrixBEntry:
    return MatrixBEntry(
        version=methodology_version,
        spend_category=category,
        ticker=ticker,
        prior=Prior(weight, weight, weight),
        method=f"normalized estimated U.S. {label} revenue midpoint",
        evidence_refs=(
            company.revenue_source_url,
            f"denominator_assumption:{company.consumer_share_assumption_id}",
        ),
    )


def _normalized_weights(values: Mapping[str, Decimal]) -> dict[str, float]:
    total = sum(values.values(), Decimal("0"))
    if total <= 0:
        raise QuarterlyRefreshError("Cannot normalize non-positive company denominators")
    quantum = Decimal("0.000000000001")
    tickers = sorted(values)
    result: dict[str, Decimal] = {}
    for ticker in tickers[:-1]:
        result[ticker] = (values[ticker] / total).quantize(quantum, rounding=ROUND_DOWN)
    result[tickers[-1]] = Decimal("1") - sum(result.values(), Decimal("0"))
    return {ticker: float(value) for ticker, value in result.items()}


def _record_from_row(row: Mapping[str, object]) -> QuarterlyRefreshRecord:
    record = QuarterlyRefreshRecord(
        refresh_id=cast(str, row["refresh_id"]),
        target_quarter=cast(str, row["target_quarter"]),
        effective_at=cast(datetime, row["effective_at"]),
        data_vintage=cast(datetime, row["data_vintage"]),
        methodology_version=cast(str, row["methodology_version"]),
        source_hash=cast(str, row["source_hash"]),
        company_count=cast(int, row["company_count"]),
        updated_company_count=cast(int, row["updated_company_count"]),
        payload_json=cast(str, row["payload_json"]),
    )
    payload = record.payload()
    if payload.get("refresh_id") != record.refresh_id or record.company_count != 50:
        raise QuarterlyRefreshError("Stored quarterly refresh identity is invalid")
    return record


def _filing_url(cik: str, filing: FilingIdentity) -> str:
    return (
        "https://www.sec.gov/Archives/edgar/data/"
        f"{int(cik)}/{filing.accession.replace('-', '')}/{filing.primary_document}"
    )


def _fact_payload(item: SelectedFact) -> dict[str, object]:
    return asdict(item)


def _quarter_end(quarter: str) -> date:
    try:
        year_text, quarter_text = quarter.split("-Q", 1)
        year = int(year_text)
        quarter_number = int(quarter_text)
    except ValueError as exc:
        raise QuarterlyRefreshError(f"Invalid quarter: {quarter}") from exc
    if quarter_number not in {1, 2, 3, 4}:
        raise QuarterlyRefreshError(f"Invalid quarter: {quarter}")
    month = quarter_number * 3
    next_year = year + int(month == 12)
    next_month = 1 if month == 12 else month + 1
    return date(next_year, next_month, 1) - date.resolution


def _cik_value(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise QuarterlyRefreshError("SEC CIK value is invalid")
    try:
        return int(value)
    except ValueError as exc:
        raise QuarterlyRefreshError("SEC CIK value is invalid") from exc


def _text(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise QuarterlyRefreshError(f"Quarterly refresh field is invalid: {key}")
    return item


def _integer(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise QuarterlyRefreshError(f"Quarterly refresh field is invalid: {key}")
    return item


def _timestamp(value: Mapping[str, object], key: str) -> datetime:
    try:
        item = datetime.fromisoformat(_text(value, key))
    except ValueError as exc:
        raise QuarterlyRefreshError(f"Quarterly refresh field is invalid: {key}") from exc
    _aware(item)
    return item


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=_json_default,
    ).encode()


def _json_default(value: object) -> str:
    if isinstance(value, date | datetime):
        return value.isoformat()
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise QuarterlyRefreshError("Quarterly refresh as-of must be timezone-aware")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid ISO timestamp: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("Timestamp must include a timezone")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-lake-root", type=Path, default=Path(".local/lake/raw"))
    parser.add_argument("--ledger-root", type=Path, default=Path(".local/lake/curated"))
    parser.add_argument("--as-of", type=_parse_timestamp)
    parser.add_argument(
        "--output", type=Path, default=Path(".local/evidence/quarterly_refresh/latest.json")
    )
    args = parser.parse_args(argv)
    as_of = args.as_of or datetime.now(UTC)
    with HttpTransport(min_interval_seconds=0.12) as transport:
        record, append = run_live_refresh(
            AppendOnlyParquetStore(args.raw_lake_root),
            QuarterlyRefreshLedger(AppendOnlyParquetStore(args.ledger_root)),
            EdgarClient(transport),
            as_of=as_of,
        )
    write_refresh_report(args.output, record)
    print(
        json.dumps(
            {
                "status": "PASS",
                "refresh_id": record.refresh_id,
                "target_quarter": record.target_quarter,
                "effective_at": record.effective_at.isoformat(),
                "data_vintage": record.data_vintage.isoformat(),
                "company_count": record.company_count,
                "updated_company_count": record.updated_company_count,
                "appended": int(append.appended),
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
