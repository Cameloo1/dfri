"""Private raw EX-102 archival and public-safe Auto ABS aggregate ingestion."""

from __future__ import annotations

import argparse
import calendar
import gzip
import hashlib
import json
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path
from typing import Literal, cast

import polars as pl

from dfri.ingest.edgar import ARCHIVES_BASE, EdgarClient, EdgarJsonReceipt
from dfri.ingest.http import HttpFileReceipt, HttpReceipt, HttpTransport
from dfri.lake.store import AppendOnlyParquetStore, file_sha256

AUTO_LOAN_NAMESPACE = "http://www.sec.gov/edgar/document/absee/autoloan/assetdata"
CreditSegment = Literal["prime", "subprime", "not_labeled"]
FreshnessMode = Literal["active", "terminal_history"]
MONEY_QUANTUM = Decimal("0.00000001")
RATE_QUANTUM = Decimal("0.0000000001")
TERM_QUANTUM = Decimal("0.00000001")


class AutoAbsError(RuntimeError):
    """An Auto ABS identity, raw archive, XML, or aggregate contract failed."""


@dataclass(frozen=True)
class AutoAbsTrust:
    trust_id: str
    cik: str
    expected_name: str
    sponsor: str
    credit_segment: CreditSegment
    freshness_mode: FreshnessMode
    terminal_evidence_url: str | None
    classification_evidence_url: str
    history_start: date
    history_end: date
    minimum_months: int


@dataclass(frozen=True)
class AutoAbsFiling:
    trust_id: str
    cik: str
    trust_name: str
    credit_segment: CreditSegment
    period: date
    filed_at: date
    accession: str


@dataclass(frozen=True)
class Ex102Identity:
    document: str
    byte_count: int


@dataclass(frozen=True)
class Ex102Metrics:
    reporting_period_start: date
    reporting_period_end: date
    asset_count: int
    core_metric_asset_count: int
    recovery_only_asset_count: int
    asset_added_count: int
    asset_added_indicator_observed_count: int
    recovered_amount_sum: Decimal
    recovered_amount_observed_asset_count: int
    original_loan_amount_sum: Decimal
    asset_added_original_loan_amount_sum: Decimal
    beginning_balance_sum: Decimal
    ending_balance_sum: Decimal
    weighted_avg_original_interest_rate: Decimal
    weighted_avg_reporting_interest_rate: Decimal
    reporting_interest_rate_asset_count: int
    reporting_interest_rate_balance_sum: Decimal
    weighted_avg_original_loan_term: Decimal
    weighted_avg_remaining_term: Decimal
    remaining_term_asset_count: int
    remaining_term_balance_sum: Decimal


@dataclass(frozen=True)
class AutoAbsIngestReceipt:
    trust_id: str
    period: date
    accession: str
    source_checksum: str
    asset_count: int
    raw_path: str
    aggregate_already_present: bool
    raw_already_present: bool


@dataclass(frozen=True)
class AutoAbsValidation:
    trusts: int
    trust_months: int
    assets_across_snapshots: int
    prime_trusts: int
    subprime_trusts: int


@dataclass(frozen=True)
class _RawReceipt:
    source_url: str
    checksum: str
    compressed_checksum: str
    retrieved_at: datetime
    status_code: int
    byte_count: int
    compressed_byte_count: int


class _FilingIndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_row = False
        self._in_cell = False
        self._cell_text: list[str] = []
        self._cell_href: str | None = None
        self._row: list[tuple[str, str | None]] = []
        self.rows: list[tuple[tuple[str, str | None], ...]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._in_row = True
            self._row = []
        elif self._in_row and tag in {"td", "th"}:
            self._in_cell = True
            self._cell_text = []
            self._cell_href = None
        elif self._in_cell and tag == "a":
            self._cell_href = dict(attrs).get("href")

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._in_cell and tag in {"td", "th"}:
            self._row.append((" ".join("".join(self._cell_text).split()), self._cell_href))
            self._in_cell = False
        elif self._in_row and tag == "tr":
            if self._row:
                self.rows.append(tuple(self._row))
            self._in_row = False


def load_auto_abs_registry() -> tuple[AutoAbsTrust, ...]:
    raw = json.loads(
        resources.files("dfri.ingest").joinpath("auto_abs_registry.json").read_text("utf-8")
    )
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise AutoAbsError("Auto ABS registry schema changed")
    if raw.get("form") != "ABS-EE" or raw.get("exhibit_type") != "EX-102":
        raise AutoAbsError("Auto ABS form/exhibit contract changed")
    items = raw.get("trusts")
    if not isinstance(items, list):
        raise AutoAbsError("Auto ABS registry trusts are missing")
    trusts: list[AutoAbsTrust] = []
    for item in items:
        if not isinstance(item, dict):
            raise AutoAbsError("Auto ABS trust definition is not an object")
        segment = item.get("credit_segment")
        freshness_mode = item.get("freshness_mode")
        if segment not in {"prime", "subprime", "not_labeled"}:
            raise AutoAbsError("Auto ABS credit segment is invalid")
        if freshness_mode not in {"active", "terminal_history"}:
            raise AutoAbsError("Auto ABS freshness mode is invalid")
        try:
            trust = AutoAbsTrust(
                trust_id=str(item["trust_id"]),
                cik=str(item["cik"]),
                expected_name=str(item["expected_name"]),
                sponsor=str(item["sponsor"]),
                credit_segment=cast(CreditSegment, segment),
                freshness_mode=cast(FreshnessMode, freshness_mode),
                terminal_evidence_url=(
                    str(item["terminal_evidence_url"])
                    if item["terminal_evidence_url"] is not None
                    else None
                ),
                classification_evidence_url=str(item["classification_evidence_url"]),
                history_start=date.fromisoformat(str(item["history_start"])),
                history_end=date.fromisoformat(str(item["history_end"])),
                minimum_months=int(item["minimum_months"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AutoAbsError("Auto ABS trust definition is malformed") from exc
        if trust.cik != trust.cik.zfill(10) or not trust.cik.isdigit():
            raise AutoAbsError(f"Auto ABS CIK is not normalized: {trust.trust_id}")
        if trust.history_start > trust.history_end or trust.minimum_months < 12:
            raise AutoAbsError(f"Auto ABS history contract is invalid: {trust.trust_id}")
        if trust.freshness_mode == "terminal_history" and (
            trust.terminal_evidence_url is None
            or not trust.terminal_evidence_url.startswith("https://www.sec.gov/")
        ):
            raise AutoAbsError(f"Auto ABS terminal evidence is invalid: {trust.trust_id}")
        if trust.freshness_mode == "active" and trust.terminal_evidence_url is not None:
            raise AutoAbsError(f"Active Auto ABS trust has terminal evidence: {trust.trust_id}")
        trusts.append(trust)
    if len(trusts) < 6 or len({item.trust_id for item in trusts}) != len(trusts):
        raise AutoAbsError("Auto ABS registry requires at least six unique trusts")
    segments = {item.credit_segment for item in trusts}
    if not {"prime", "subprime"}.issubset(segments):
        raise AutoAbsError("Auto ABS registry must include prospectus-defined prime and subprime")
    return tuple(trusts)


def _month_ends(start: date, end: date) -> tuple[date, ...]:
    values: list[date] = []
    current = start
    while current <= end:
        expected_day = calendar.monthrange(current.year, current.month)[1]
        if current.day != expected_day:
            raise AutoAbsError(f"History boundary is not month-end: {current.isoformat()}")
        values.append(current)
        if current.month == 12:
            current = date(current.year + 1, 1, 31)
        else:
            month = current.month + 1
            current = date(current.year, month, calendar.monthrange(current.year, month)[1])
    return tuple(values)


def discover_trust_filings(
    trust: AutoAbsTrust, receipt: EdgarJsonReceipt
) -> tuple[AutoAbsFiling, ...]:
    payload = receipt.payload
    if payload.get("name") != trust.expected_name:
        raise AutoAbsError(f"SEC trust identity changed: {trust.trust_id}")
    try:
        filings = cast(Mapping[str, object], payload["filings"])
        recent = cast(Mapping[str, Sequence[object]], filings["recent"])
        forms = recent["form"]
        accessions = recent["accessionNumber"]
        filed_dates = recent["filingDate"]
        report_dates = recent["reportDate"]
    except (KeyError, TypeError) as exc:
        raise AutoAbsError(f"SEC submissions shape changed: {trust.trust_id}") from exc
    if not (len(forms) == len(accessions) == len(filed_dates) == len(report_dates)):
        raise AutoAbsError(f"SEC submissions columns changed length: {trust.trust_id}")
    selected: list[AutoAbsFiling] = []
    for form, accession, filed_raw, period_raw in zip(
        forms, accessions, filed_dates, report_dates, strict=True
    ):
        if form != "ABS-EE":
            continue
        try:
            period = date.fromisoformat(str(period_raw))
            filed_at = date.fromisoformat(str(filed_raw))
        except ValueError as exc:
            raise AutoAbsError(f"SEC ABS-EE date changed shape: {trust.trust_id}") from exc
        if trust.history_start <= period <= trust.history_end:
            selected.append(
                AutoAbsFiling(
                    trust_id=trust.trust_id,
                    cik=trust.cik,
                    trust_name=trust.expected_name,
                    credit_segment=trust.credit_segment,
                    period=period,
                    filed_at=filed_at,
                    accession=str(accession),
                )
            )
    selected.sort(key=lambda item: item.period)
    expected = _month_ends(trust.history_start, trust.history_end)
    actual = tuple(item.period for item in selected)
    if actual != expected or len(actual) < trust.minimum_months:
        raise AutoAbsError(
            f"SEC ABS-EE history is not the pinned contiguous window: {trust.trust_id}"
        )
    if len({item.accession for item in selected}) != len(selected):
        raise AutoAbsError(f"SEC ABS-EE accessions are duplicated: {trust.trust_id}")
    return tuple(selected)


def parse_filing_index(receipt: HttpReceipt) -> Ex102Identity:
    parser = _FilingIndexParser()
    parser.feed(receipt.content.decode("utf-8", errors="strict"))
    matches: list[Ex102Identity] = []
    for row in parser.rows:
        if len(row) < 5 or row[3][0] != "EX-102":
            continue
        document = Path(row[2][1] or "").name
        try:
            byte_count = int(row[4][0])
        except ValueError as exc:
            raise AutoAbsError("SEC filing index EX-102 size changed shape") from exc
        if not document.endswith(".xml") or byte_count < 1:
            raise AutoAbsError("SEC filing index EX-102 identity is invalid")
        matches.append(Ex102Identity(document=document, byte_count=byte_count))
    if len(matches) != 1:
        raise AutoAbsError(f"SEC filing index contains {len(matches)} EX-102 documents")
    return matches[0]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _date_value(value: str, field: str) -> date:
    try:
        if len(value) == 10 and value[4] == "-":
            return date.fromisoformat(value)
        separator = "-" if "-" in value else "/"
        month, day, year = (int(part) for part in value.split(separator))
        return date(year, month, day)
    except (TypeError, ValueError) as exc:
        raise AutoAbsError(f"EX-102 {field} is not a supported exact date") from exc


def _decimal_value(values: Mapping[str, str], field: str) -> Decimal:
    raw = values.get(field)
    if raw is None or not raw.strip():
        raise AutoAbsError(f"EX-102 asset is missing {field}")
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise AutoAbsError(f"EX-102 {field} is not numeric") from exc


def _optional_decimal_value(values: Mapping[str, str], field: str) -> Decimal | None:
    raw = values.get(field)
    if raw is None:
        return None
    if not raw.strip():
        raise AutoAbsError(f"EX-102 {field} is present but empty")
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise AutoAbsError(f"EX-102 {field} is not numeric") from exc


def _integer_value(values: Mapping[str, str], field: str) -> int:
    value = _decimal_value(values, field)
    if value != value.to_integral_value():
        raise AutoAbsError(f"EX-102 {field} is not an integer")
    return int(value)


def _weighted(numerator: Decimal, denominator: Decimal, quantum: Decimal) -> Decimal:
    if denominator <= 0:
        raise AutoAbsError("EX-102 weighted-average denominator is not positive")
    return (numerator / denominator).quantize(quantum, rounding=ROUND_HALF_EVEN)


def parse_ex102(path: Path, *, expected_period: date | None = None) -> Ex102Metrics:
    """Stream an EX-102 gzip and retain no loan-level identifiers after aggregation."""

    asset_count = 0
    core_metric_asset_count = 0
    recovery_only_asset_count = 0
    asset_added_count = 0
    asset_added_indicator_observed_count = 0
    recovered_amount_sum = Decimal(0)
    recovered_amount_observed_asset_count = 0
    original_sum = Decimal(0)
    added_original_sum = Decimal(0)
    beginning_sum = Decimal(0)
    ending_sum = Decimal(0)
    original_rate_numerator = Decimal(0)
    reporting_rate_numerator = Decimal(0)
    reporting_rate_balance_sum = Decimal(0)
    reporting_rate_asset_count = 0
    original_term_numerator = Decimal(0)
    remaining_term_numerator = Decimal(0)
    remaining_term_balance_sum = Decimal(0)
    remaining_term_asset_count = 0
    period_starts: set[date] = set()
    period_ends: set[date] = set()
    asset_hashes: set[bytes] = set()
    root_checked = False

    try:
        with gzip.open(path, "rb") as source:
            for event, element in ET.iterparse(source, events=("start", "end")):
                if not root_checked and event == "start":
                    if element.tag != f"{{{AUTO_LOAN_NAMESPACE}}}assetData":
                        raise AutoAbsError("EX-102 root namespace changed")
                    root_checked = True
                if event != "end" or _local_name(element.tag) != "assets":
                    continue
                values = {
                    _local_name(child.tag): (child.text or "").strip() for child in list(element)
                }
                asset_number = values.get("assetNumber", "")
                if not asset_number:
                    raise AutoAbsError("EX-102 asset is missing assetNumber")
                asset_hash = hashlib.sha256(asset_number.encode("utf-8")).digest()
                if asset_hash in asset_hashes:
                    raise AutoAbsError("EX-102 contains a duplicate assetNumber")
                asset_hashes.add(asset_hash)

                period_start = _date_value(
                    values.get("reportingPeriodBeginningDate", ""),
                    "reportingPeriodBeginningDate",
                )
                period_end = _date_value(
                    values.get("reportingPeriodEndingDate", ""), "reportingPeriodEndingDate"
                )
                asset_count += 1
                period_starts.add(period_start)
                period_ends.add(period_end)
                recovered = _optional_decimal_value(values, "recoveredAmount")
                if recovered is not None:
                    recovered_amount_sum += recovered
                    recovered_amount_observed_asset_count += 1
                if "originalLoanAmount" not in values:
                    recovery_fields = {
                        "assetTypeNumber",
                        "assetNumber",
                        "reportingPeriodBeginningDate",
                        "reportingPeriodEndingDate",
                        "recoveredAmount",
                    }
                    if set(values) != recovery_fields or recovered is None:
                        raise AutoAbsError("EX-102 asset has an unknown partial field set")
                    recovery_only_asset_count += 1
                    element.clear()
                    continue
                original = _decimal_value(values, "originalLoanAmount")
                beginning = _decimal_value(values, "reportingPeriodBeginningLoanBalanceAmount")
                ending = _decimal_value(values, "reportingPeriodActualEndBalanceAmount")
                original_rate = _decimal_value(values, "originalInterestRatePercentage")
                reporting_rate = _optional_decimal_value(
                    values, "reportingPeriodInterestRatePercentage"
                )
                original_term = _integer_value(values, "originalLoanTerm")
                remaining_term_raw = _optional_decimal_value(
                    values, "remainingTermToMaturityNumber"
                )
                if (
                    remaining_term_raw is not None
                    and remaining_term_raw != remaining_term_raw.to_integral_value()
                ):
                    raise AutoAbsError("EX-102 remainingTermToMaturityNumber is not an integer")
                remaining_term = int(remaining_term_raw) if remaining_term_raw is not None else None
                added_raw = values.get("assetAddedIndicator")
                added = added_raw.casefold() if added_raw is not None else None
                if added not in {"true", "false", None}:
                    raise AutoAbsError("EX-102 assetAddedIndicator is not boolean")
                if min(original, beginning, ending) < 0:
                    raise AutoAbsError("EX-102 contains a negative money balance")

                core_metric_asset_count += 1
                original_sum += original
                beginning_sum += beginning
                ending_sum += ending
                original_rate_numerator += original_rate * original
                if reporting_rate is not None:
                    reporting_rate_numerator += reporting_rate * ending
                    reporting_rate_balance_sum += ending
                    reporting_rate_asset_count += 1
                original_term_numerator += Decimal(original_term) * original
                if remaining_term is not None:
                    remaining_term_numerator += Decimal(remaining_term) * ending
                    remaining_term_balance_sum += ending
                    remaining_term_asset_count += 1
                if added is not None:
                    asset_added_indicator_observed_count += 1
                if added == "true":
                    asset_added_count += 1
                    added_original_sum += original
                element.clear()
    except (ET.ParseError, OSError) as exc:
        raise AutoAbsError("EX-102 XML or gzip is invalid") from exc

    if asset_count < 1 or len(period_starts) != 1 or len(period_ends) != 1:
        raise AutoAbsError("EX-102 does not contain one non-empty reporting period")
    period_start = next(iter(period_starts))
    period_end = next(iter(period_ends))
    if expected_period is not None and period_end != expected_period:
        raise AutoAbsError("EX-102 reporting period does not match SEC submissions")
    return Ex102Metrics(
        reporting_period_start=period_start,
        reporting_period_end=period_end,
        asset_count=asset_count,
        core_metric_asset_count=core_metric_asset_count,
        recovery_only_asset_count=recovery_only_asset_count,
        asset_added_count=asset_added_count,
        asset_added_indicator_observed_count=asset_added_indicator_observed_count,
        recovered_amount_sum=recovered_amount_sum.quantize(MONEY_QUANTUM),
        recovered_amount_observed_asset_count=recovered_amount_observed_asset_count,
        original_loan_amount_sum=original_sum.quantize(MONEY_QUANTUM),
        asset_added_original_loan_amount_sum=added_original_sum.quantize(MONEY_QUANTUM),
        beginning_balance_sum=beginning_sum.quantize(MONEY_QUANTUM),
        ending_balance_sum=ending_sum.quantize(MONEY_QUANTUM),
        weighted_avg_original_interest_rate=_weighted(
            original_rate_numerator, original_sum, RATE_QUANTUM
        ),
        weighted_avg_reporting_interest_rate=_weighted(
            reporting_rate_numerator, reporting_rate_balance_sum, RATE_QUANTUM
        ),
        reporting_interest_rate_asset_count=reporting_rate_asset_count,
        reporting_interest_rate_balance_sum=reporting_rate_balance_sum.quantize(MONEY_QUANTUM),
        weighted_avg_original_loan_term=_weighted(
            original_term_numerator, original_sum, TERM_QUANTUM
        ),
        weighted_avg_remaining_term=_weighted(
            remaining_term_numerator, remaining_term_balance_sum, TERM_QUANTUM
        ),
        remaining_term_asset_count=remaining_term_asset_count,
        remaining_term_balance_sum=remaining_term_balance_sum.quantize(MONEY_QUANTUM),
    )


def _raw_receipt_json(receipt: HttpFileReceipt) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_url": receipt.source_url,
        "checksum": receipt.checksum,
        "compressed_checksum": receipt.compressed_checksum,
        "retrieved_at": receipt.retrieved_at.isoformat(),
        "status_code": receipt.status_code,
        "byte_count": receipt.byte_count,
        "compressed_byte_count": receipt.compressed_byte_count,
    }


def _load_raw_receipt(
    directory: Path, document: str, expected_bytes: int, expected_url: str
) -> _RawReceipt:
    source_path = directory / f"{document}.gz"
    receipt_path = directory / "receipt.json"
    if not source_path.is_file() or not receipt_path.is_file():
        raise AutoAbsError(f"Private raw archive is incomplete: {directory.name}")
    try:
        raw = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt = _RawReceipt(
            source_url=str(raw["source_url"]),
            checksum=str(raw["checksum"]),
            compressed_checksum=str(raw["compressed_checksum"]),
            retrieved_at=datetime.fromisoformat(str(raw["retrieved_at"])),
            status_code=int(raw["status_code"]),
            byte_count=int(raw["byte_count"]),
            compressed_byte_count=int(raw["compressed_byte_count"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AutoAbsError(f"Private raw receipt is invalid: {directory.name}") from exc
    if (
        receipt.source_url != expected_url
        or receipt.byte_count != expected_bytes
        or receipt.status_code != 200
        or receipt.retrieved_at.tzinfo is None
        or len(receipt.checksum) != 64
        or len(receipt.compressed_checksum) != 64
    ):
        raise AutoAbsError(f"Private raw receipt identity changed: {directory.name}")
    if source_path.stat().st_size != receipt.compressed_byte_count:
        raise AutoAbsError(f"Private raw compressed size changed: {directory.name}")
    if file_sha256(source_path) != receipt.compressed_checksum:
        raise AutoAbsError(f"Private raw checksum changed: {directory.name}")
    return receipt


def _archive_raw(
    client: EdgarClient,
    private_root: Path,
    filing: AutoAbsFiling,
    identity: Ex102Identity,
) -> tuple[Path, _RawReceipt, bool]:
    parent = private_root / filing.trust_id
    destination_dir = parent / filing.accession
    source_path = destination_dir / f"{identity.document}.gz"
    expected_url = (
        f"{ARCHIVES_BASE}/{int(filing.cik)}/{filing.accession.replace('-', '')}/{identity.document}"
    )
    if destination_dir.exists():
        receipt = _load_raw_receipt(
            destination_dir, identity.document, identity.byte_count, expected_url
        )
        return source_path, receipt, True
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{filing.accession}.", dir=parent) as temporary:
        temporary_dir = Path(temporary)
        temporary_source = temporary_dir / f"{identity.document}.gz"
        streamed = client.archive_document_to_gzip(
            filing.cik, filing.accession, identity.document, temporary_source
        )
        if streamed.byte_count != identity.byte_count:
            raise AutoAbsError(f"SEC EX-102 byte count changed: {filing.accession}")
        (temporary_dir / "receipt.json").write_text(
            json.dumps(_raw_receipt_json(streamed), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            temporary_dir.replace(destination_dir)
        except FileExistsError as exc:
            raise AutoAbsError(f"Concurrent raw archive write: {filing.accession}") from exc
    receipt = _load_raw_receipt(
        destination_dir, identity.document, identity.byte_count, expected_url
    )
    return source_path, receipt, False


def _aggregate_row(
    filing: AutoAbsFiling,
    identity: Ex102Identity,
    index_receipt: HttpReceipt,
    raw_receipt: _RawReceipt,
    metrics: Ex102Metrics,
) -> dict[str, object]:
    return {
        "trust_id": filing.trust_id,
        "trust_name": filing.trust_name,
        "credit_segment": filing.credit_segment,
        "cik": filing.cik,
        "reporting_period_start": metrics.reporting_period_start,
        "reporting_period_end": metrics.reporting_period_end,
        "filed_at": filing.filed_at,
        "accession": filing.accession,
        "exhibit_document": identity.document,
        "asset_count": metrics.asset_count,
        "core_metric_asset_count": metrics.core_metric_asset_count,
        "recovery_only_asset_count": metrics.recovery_only_asset_count,
        "asset_added_count": metrics.asset_added_count,
        "asset_added_indicator_observed_count": metrics.asset_added_indicator_observed_count,
        "recovered_amount_sum": metrics.recovered_amount_sum,
        "recovered_amount_observed_asset_count": metrics.recovered_amount_observed_asset_count,
        "original_loan_amount_sum": metrics.original_loan_amount_sum,
        "asset_added_original_loan_amount_sum": metrics.asset_added_original_loan_amount_sum,
        "beginning_balance_sum": metrics.beginning_balance_sum,
        "ending_balance_sum": metrics.ending_balance_sum,
        "weighted_avg_original_interest_rate": metrics.weighted_avg_original_interest_rate,
        "weighted_avg_reporting_interest_rate": metrics.weighted_avg_reporting_interest_rate,
        "reporting_interest_rate_asset_count": metrics.reporting_interest_rate_asset_count,
        "reporting_interest_rate_balance_sum": metrics.reporting_interest_rate_balance_sum,
        "weighted_avg_original_loan_term": metrics.weighted_avg_original_loan_term,
        "weighted_avg_remaining_term": metrics.weighted_avg_remaining_term,
        "remaining_term_asset_count": metrics.remaining_term_asset_count,
        "remaining_term_balance_sum": metrics.remaining_term_balance_sum,
        "source_url": raw_receipt.source_url,
        "source_checksum": raw_receipt.checksum,
        "source_bytes": raw_receipt.byte_count,
        "filing_index_url": index_receipt.source_url,
        "filing_index_checksum": index_receipt.checksum,
        "ingested_at": raw_receipt.retrieved_at,
    }


class AutoAbsIngestor:
    def __init__(
        self,
        store: AppendOnlyParquetStore,
        client: EdgarClient,
        private_root: Path,
        trusts: Sequence[AutoAbsTrust],
    ) -> None:
        self._store = store
        self._client = client
        self._private_root = private_root
        self._trusts = tuple(trusts)

    def ingest_all(self, *, max_filings_per_trust: int | None = None) -> list[AutoAbsIngestReceipt]:
        receipts: list[AutoAbsIngestReceipt] = []
        existing = self._store.read_table("auto_abs_aggregates")
        existing_rows = {
            str(row["accession"]): cast(dict[str, object], row)
            for row in existing.iter_rows(named=True)
        }
        for trust in self._trusts:
            submissions = self._client.submissions(trust.cik)
            filings = discover_trust_filings(trust, submissions)
            if max_filings_per_trust is not None:
                if max_filings_per_trust < 1:
                    raise AutoAbsError("Auto ABS filing bound must be positive")
                filings = filings[-max_filings_per_trust:]
            for filing in filings:
                index_document = f"{filing.accession}-index.htm"
                index_receipt = self._client.archive_document_receipt(
                    filing.cik, filing.accession, index_document
                )
                identity = parse_filing_index(index_receipt)
                raw_path, raw_receipt, raw_present = _archive_raw(
                    self._client, self._private_root, filing, identity
                )
                prior = existing_rows.get(filing.accession)
                if prior is not None:
                    if (
                        prior["source_checksum"] != raw_receipt.checksum
                        or prior["source_bytes"] != raw_receipt.byte_count
                        or prior["filing_index_checksum"] != index_receipt.checksum
                    ):
                        raise AutoAbsError(f"Stored Auto ABS identity drift: {filing.accession}")
                    receipts.append(
                        AutoAbsIngestReceipt(
                            trust_id=filing.trust_id,
                            period=filing.period,
                            accession=filing.accession,
                            source_checksum=raw_receipt.checksum,
                            asset_count=int(cast(int, prior["asset_count"])),
                            raw_path=str(raw_path),
                            aggregate_already_present=True,
                            raw_already_present=raw_present,
                        )
                    )
                    continue
                metrics = parse_ex102(raw_path, expected_period=filing.period)
                row = _aggregate_row(filing, identity, index_receipt, raw_receipt, metrics)
                write = self._store.append("auto_abs_aggregates", [row])
                receipts.append(
                    AutoAbsIngestReceipt(
                        trust_id=filing.trust_id,
                        period=filing.period,
                        accession=filing.accession,
                        source_checksum=raw_receipt.checksum,
                        asset_count=metrics.asset_count,
                        raw_path=str(raw_path),
                        aggregate_already_present=write.already_present,
                        raw_already_present=raw_present,
                    )
                )
                existing_rows[filing.accession] = row
        return receipts


def validate_auto_abs(
    store: AppendOnlyParquetStore, trusts: Sequence[AutoAbsTrust]
) -> AutoAbsValidation:
    frame = store.read_table("auto_abs_aggregates")
    if frame.is_empty():
        raise AutoAbsError("Auto ABS aggregate lake is empty")
    expected_ids = {item.trust_id for item in trusts}
    selected = frame.filter(pl.col("trust_id").is_in(expected_ids))
    if selected["accession"].n_unique() != selected.height:
        raise AutoAbsError("Auto ABS aggregate lake contains duplicate accessions")
    for trust in trusts:
        trust_rows = selected.filter(pl.col("trust_id") == trust.trust_id).sort(
            "reporting_period_end"
        )
        actual = tuple(cast(date, value) for value in trust_rows["reporting_period_end"])
        expected = _month_ends(trust.history_start, trust.history_end)
        if actual != expected or len(actual) < trust.minimum_months:
            raise AutoAbsError(f"Auto ABS aggregate coverage is incomplete: {trust.trust_id}")
        if set(trust_rows["credit_segment"].to_list()) != {trust.credit_segment}:
            raise AutoAbsError(f"Auto ABS credit segment drift: {trust.trust_id}")
    return AutoAbsValidation(
        trusts=len(trusts),
        trust_months=selected.height,
        assets_across_snapshots=int(selected["asset_count"].sum()),
        prime_trusts=sum(item.credit_segment == "prime" for item in trusts),
        subprime_trusts=sum(item.credit_segment == "subprime" for item in trusts),
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lake-root", type=Path, default=Path(".local/lake/raw"))
    parser.add_argument("--trust", default="all")
    parser.add_argument("--max-filings-per-trust", type=int)
    parser.add_argument("--allow-partial", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.max_filings_per_trust is not None and not args.allow_partial:
        raise AutoAbsError("A bounded Auto ABS run requires --allow-partial")
    trusts = load_auto_abs_registry()
    if args.trust != "all":
        trusts = tuple(item for item in trusts if item.trust_id == args.trust)
        if not trusts:
            raise AutoAbsError(f"Unknown Auto ABS trust: {args.trust}")
    store = AppendOnlyParquetStore(args.lake_root)
    with HttpTransport(min_interval_seconds=0.11) as transport:
        receipts = AutoAbsIngestor(
            store,
            EdgarClient(transport),
            args.lake_root / "_private" / "sec_auto_abs_ee",
            trusts,
        ).ingest_all(max_filings_per_trust=args.max_filings_per_trust)
    validation = None if args.allow_partial else validate_auto_abs(store, trusts)
    print(
        json.dumps(
            {
                "status": "PARTIAL" if args.allow_partial else "PASS",
                "new_aggregate_batches": sum(
                    not item.aggregate_already_present for item in receipts
                ),
                "new_raw_archives": sum(not item.raw_already_present for item in receipts),
                "validation": asdict(validation) if validation else None,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
