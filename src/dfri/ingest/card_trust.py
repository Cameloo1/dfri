"""Evidence-linked monthly credit-card trust Form 10-D aggregates."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path
from typing import Literal, cast

import polars as pl

from dfri.ingest.edgar import EdgarClient, EdgarJsonReceipt
from dfri.ingest.http import HttpReceipt, HttpTransport
from dfri.lake.store import AppendOnlyParquetStore

MetricUnit = Literal["usd", "percent"]
MONEY_QUANTUM = Decimal("0.01")
RATE_QUANTUM = Decimal("0.000001")


class CardTrustError(RuntimeError):
    """A card-trust identity, filing, exhibit, or metric contract failed."""


@dataclass(frozen=True)
class MetricContract:
    label: str
    table_anchors: tuple[str, ...]
    period_in_table: bool
    unit: MetricUnit
    scale: Decimal


@dataclass(frozen=True)
class CardTrust:
    trust_id: str
    trust_cik: str
    archive_cik: str
    expected_name: str
    sponsor: str
    history_start: date
    history_end: date
    minimum_months: int
    identity_evidence_url: str
    exhibit_pattern: str
    payment_rate_basis: str
    yield_basis: str
    chargeoff_basis: str
    metrics: Mapping[str, MetricContract | None]


@dataclass(frozen=True)
class CardTrustFiling:
    trust_id: str
    trust_cik: str
    archive_cik: str
    trust_name: str
    period: date
    filed_at: date
    accession: str
    primary_document: str


@dataclass(frozen=True)
class CardMetrics:
    ending_principal_receivables: Decimal
    principal_payment_rate_pct: Decimal
    portfolio_yield_pct: Decimal
    chargeoff_amount: Decimal | None
    chargeoff_amount_status: str
    chargeoff_rate_pct: Decimal
    metric_evidence_json: str
    evidence_snippet_hash: str


@dataclass(frozen=True)
class CardTrustIngestReceipt:
    trust_id: str
    period: date
    accession: str
    source_checksum: str
    already_present: bool


@dataclass(frozen=True)
class CardTrustValidation:
    trusts: int
    trust_months: int
    dollar_chargeoff_months: int
    rate_only_chargeoff_months: int


@dataclass(frozen=True)
class _CapturedTable:
    context: str
    rows: tuple[tuple[str, ...], ...]


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._table_depth = 0
        self._in_cell = False
        self._cell_parts: list[str] = []
        self._row: list[str] = []
        self._rows: list[tuple[str, ...]] = []
        self._document_parts: list[str] = []
        self._table_context = ""
        self.tables: list[_CapturedTable] = []

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in {"script", "style"}:
            self._skip_depth += 1
        elif not self._skip_depth and tag == "table":
            if self._table_depth == 0:
                self._table_context = _normalize(" ".join(self._document_parts))[-4000:]
                self._rows = []
            self._table_depth += 1
        elif not self._skip_depth and self._table_depth == 1 and tag == "tr":
            self._row = []
        elif not self._skip_depth and self._table_depth == 1 and tag in {"th", "td"}:
            self._in_cell = True
            self._cell_parts = []
        elif not self._skip_depth and tag == "br":
            self._document_parts.append(" ")
            if self._in_cell:
                self._cell_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"script", "style"}:
            if self._skip_depth:
                self._skip_depth -= 1
        elif self._skip_depth:
            return
        elif self._table_depth == 1 and tag in {"th", "td"} and self._in_cell:
            value = _normalize(" ".join(self._cell_parts))
            if value:
                self._row.append(value)
            self._in_cell = False
        elif self._table_depth == 1 and tag == "tr" and self._row:
            self._rows.append(tuple(self._row))
        elif tag == "table" and self._table_depth:
            self._table_depth -= 1
            if self._table_depth == 0 and self._rows:
                self.tables.append(_CapturedTable(self._table_context, tuple(self._rows)))

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self._document_parts.append(data)
        if self._in_cell:
            self._cell_parts.append(data)

    @property
    def document_text(self) -> str:
        return _normalize(" ".join(self._document_parts))


def _normalize(value: str) -> str:
    return " ".join(value.replace("\u2212", "-").split())


def _metric_contract(raw: object, metric: str, trust_id: str) -> MetricContract | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise CardTrustError(f"Card metric contract is malformed: {trust_id}.{metric}")
    anchors = raw.get("table_anchors")
    unit = raw.get("unit")
    period_in_table = raw.get("period_in_table", False)
    if (
        not isinstance(anchors, list)
        or not anchors
        or not all(isinstance(item, str) and item for item in anchors)
        or unit not in {"usd", "percent"}
        or not isinstance(period_in_table, bool)
    ):
        raise CardTrustError(f"Card metric contract is invalid: {trust_id}.{metric}")
    try:
        contract = MetricContract(
            label=str(raw["label"]),
            table_anchors=tuple(anchors),
            period_in_table=period_in_table,
            unit=cast(MetricUnit, unit),
            scale=Decimal(str(raw["scale"])),
        )
    except (KeyError, InvalidOperation) as exc:
        raise CardTrustError(f"Card metric contract is incomplete: {trust_id}.{metric}") from exc
    if not contract.label or contract.scale <= 0:
        raise CardTrustError(f"Card metric contract is invalid: {trust_id}.{metric}")
    return contract


def load_card_trust_registry() -> tuple[CardTrust, ...]:
    raw = json.loads(
        resources.files("dfri.ingest").joinpath("card_trust_registry.json").read_text("utf-8")
    )
    if not isinstance(raw, dict) or raw.get("schema_version") != 1 or raw.get("form") != "10-D":
        raise CardTrustError("Card-trust registry schema changed")
    items = raw.get("trusts")
    minimum = raw.get("minimum_trusts")
    if not isinstance(items, list) or not isinstance(minimum, int):
        raise CardTrustError("Card-trust registry is incomplete")
    required_metrics = {
        "ending_principal_receivables",
        "principal_payment_rate_pct",
        "portfolio_yield_pct",
        "chargeoff_amount",
        "chargeoff_rate_pct",
    }
    trusts: list[CardTrust] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("metrics"), dict):
            raise CardTrustError("Card-trust definition is malformed")
        metric_raw = cast(dict[str, object], item["metrics"])
        if set(metric_raw) != required_metrics:
            raise CardTrustError("Card-trust metric registry is incomplete")
        try:
            trust = CardTrust(
                trust_id=str(item["trust_id"]),
                trust_cik=str(item["trust_cik"]),
                archive_cik=str(item["archive_cik"]),
                expected_name=str(item["expected_name"]),
                sponsor=str(item["sponsor"]),
                history_start=date.fromisoformat(str(item["history_start"])),
                history_end=date.fromisoformat(str(item["history_end"])),
                minimum_months=int(item["minimum_months"]),
                identity_evidence_url=str(item["identity_evidence_url"]),
                exhibit_pattern=str(item["exhibit_pattern"]),
                payment_rate_basis=str(item["payment_rate_basis"]),
                yield_basis=str(item["yield_basis"]),
                chargeoff_basis=str(item["chargeoff_basis"]),
                metrics={
                    name: _metric_contract(value, name, str(item["trust_id"]))
                    for name, value in metric_raw.items()
                },
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CardTrustError("Card-trust definition is incomplete") from exc
        if (
            any(
                cik != cik.zfill(10) or not cik.isdigit()
                for cik in (trust.trust_cik, trust.archive_cik)
            )
            or trust.history_start > trust.history_end
            or trust.minimum_months < 12
            or not trust.identity_evidence_url.startswith("https://www.sec.gov/")
        ):
            raise CardTrustError(f"Card-trust identity contract is invalid: {trust.trust_id}")
        if any(trust.metrics[name] is None for name in required_metrics - {"chargeoff_amount"}):
            raise CardTrustError(f"Card-trust required metric is absent: {trust.trust_id}")
        try:
            re.compile(trust.exhibit_pattern)
        except re.error as exc:
            raise CardTrustError(
                f"Card-trust exhibit pattern is invalid: {trust.trust_id}"
            ) from exc
        trusts.append(trust)
    if len(trusts) < minimum or len({item.trust_id for item in trusts}) != len(trusts):
        raise CardTrustError("Card-trust registry does not meet the unique-trust minimum")
    return tuple(trusts)


def _month_keys(start: date, end: date) -> tuple[tuple[int, int], ...]:
    values: list[tuple[int, int]] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        values.append((year, month))
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return tuple(values)


def discover_card_filings(
    trust: CardTrust, receipt: EdgarJsonReceipt
) -> tuple[CardTrustFiling, ...]:
    if receipt.payload.get("name") != trust.expected_name:
        raise CardTrustError(f"SEC card-trust identity changed: {trust.trust_id}")
    try:
        filings = cast(Mapping[str, object], receipt.payload["filings"])
        recent = cast(Mapping[str, Sequence[object]], filings["recent"])
        columns = [
            recent["form"],
            recent["accessionNumber"],
            recent["filingDate"],
            recent["reportDate"],
            recent["primaryDocument"],
        ]
    except (KeyError, TypeError) as exc:
        raise CardTrustError(f"SEC card submissions shape changed: {trust.trust_id}") from exc
    if len({len(column) for column in columns}) != 1:
        raise CardTrustError(f"SEC card submissions columns changed length: {trust.trust_id}")
    selected: list[CardTrustFiling] = []
    for form, accession, filed_raw, period_raw, primary in zip(*columns, strict=True):
        if form != "10-D":
            continue
        try:
            period = date.fromisoformat(str(period_raw))
            filed_at = date.fromisoformat(str(filed_raw))
        except ValueError as exc:
            raise CardTrustError(f"SEC card filing date changed shape: {trust.trust_id}") from exc
        if trust.history_start <= period <= trust.history_end:
            selected.append(
                CardTrustFiling(
                    trust.trust_id,
                    trust.trust_cik,
                    trust.archive_cik,
                    trust.expected_name,
                    period,
                    filed_at,
                    str(accession),
                    str(primary),
                )
            )
    selected.sort(key=lambda item: item.period)
    actual_keys = tuple((item.period.year, item.period.month) for item in selected)
    if (
        not selected
        or selected[0].period != trust.history_start
        or selected[-1].period != trust.history_end
        or actual_keys != _month_keys(trust.history_start, trust.history_end)
        or len(selected) < trust.minimum_months
    ):
        raise CardTrustError(
            f"SEC card history is not the pinned contiguous window: {trust.trust_id}"
        )
    if len({item.accession for item in selected}) != len(selected):
        raise CardTrustError(f"SEC card accessions are duplicated: {trust.trust_id}")
    return tuple(selected)


def select_exhibit(trust: CardTrust, receipt: EdgarJsonReceipt) -> str:
    try:
        directory = cast(Mapping[str, object], receipt.payload["directory"])
        items = cast(Sequence[Mapping[str, object]], directory["item"])
    except (KeyError, TypeError) as exc:
        raise CardTrustError(f"SEC archive index shape changed: {trust.trust_id}") from exc
    matches = [
        Path(str(item.get("name", ""))).name
        for item in items
        if re.search(trust.exhibit_pattern, Path(str(item.get("name", ""))).name)
    ]
    if len(matches) != 1:
        raise CardTrustError(
            f"SEC archive contains {len(matches)} matching card exhibits: "
            f"{trust.trust_id} ({receipt.source_url})"
        )
    return matches[0]


def _parse_number(row: tuple[str, ...], label_index: int, contract: MetricContract) -> Decimal:
    for cell in row[label_index + 1 :]:
        token = cell.strip().replace(",", "").replace("$", "").replace("%", "")
        if not token or token in {"-", "—"}:
            continue
        negative = token.startswith("(") and token.endswith(")")
        if negative:
            token = token[1:-1].strip()
        try:
            value = Decimal(token)
        except InvalidOperation:
            continue
        value = (-value if negative else value) * contract.scale
        if value < 0:
            raise CardTrustError(f"Card metric is negative: {contract.label}")
        return value.quantize(MONEY_QUANTUM if contract.unit == "usd" else RATE_QUANTUM)
    raise CardTrustError(f"Card metric row has no numeric value: {contract.label}")


def _extract_metric(
    tables: Sequence[_CapturedTable], contract: MetricContract, period_label: str
) -> tuple[Decimal, tuple[str, ...]]:
    matches: list[tuple[Decimal, tuple[str, ...]]] = []
    expected_label = _normalize(contract.label).casefold()
    for table in tables:
        row_text = _normalize(" ".join(" ".join(row) for row in table.rows))
        haystack = _normalize(table.context + " " + row_text).casefold()
        if not all(_normalize(anchor).casefold() in haystack for anchor in contract.table_anchors):
            continue
        if (
            contract.period_in_table
            and _normalize(period_label).casefold() not in row_text.casefold()
        ):
            continue
        for row in table.rows:
            for index, cell in enumerate(row):
                if _normalize(cell).casefold() == expected_label:
                    matches.append((_parse_number(row, index, contract), row))
    if len(matches) != 1:
        raise CardTrustError(f"Card metric matched {len(matches)} rows: {contract.label}")
    return matches[0]


def parse_card_exhibit(trust: CardTrust, content: bytes, *, expected_period: date) -> CardMetrics:
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CardTrustError("Card 10-D exhibit is not UTF-8 HTML") from exc
    parser = _TableParser()
    parser.feed(text)
    period_label = f"{expected_period.strftime('%B')} {expected_period.day}, {expected_period.year}"
    if _normalize(period_label).casefold() not in parser.document_text.casefold():
        raise CardTrustError("Card 10-D exhibit does not contain the SEC report date")
    evidence: dict[str, object] = {}
    values: dict[str, Decimal | None] = {}
    for name, contract in trust.metrics.items():
        if contract is None:
            values[name] = None
            evidence[name] = {"status": "NOT_REPORTED"}
            continue
        value, row = _extract_metric(parser.tables, contract, period_label)
        values[name] = value
        evidence[name] = {
            "label": contract.label,
            "row": list(row),
            "scale": str(contract.scale),
            "unit": contract.unit,
        }
    evidence_json = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    required = [
        values["ending_principal_receivables"],
        values["principal_payment_rate_pct"],
        values["portfolio_yield_pct"],
        values["chargeoff_rate_pct"],
    ]
    if any(value is None for value in required):
        raise CardTrustError("Card 10-D required metric unexpectedly absent")
    return CardMetrics(
        ending_principal_receivables=cast(Decimal, required[0]),
        principal_payment_rate_pct=cast(Decimal, required[1]),
        portfolio_yield_pct=cast(Decimal, required[2]),
        chargeoff_amount=values["chargeoff_amount"],
        chargeoff_amount_status=(
            "REPORTED" if values["chargeoff_amount"] is not None else "NOT_REPORTED"
        ),
        chargeoff_rate_pct=cast(Decimal, required[3]),
        metric_evidence_json=evidence_json,
        evidence_snippet_hash=hashlib.sha256(evidence_json.encode("utf-8")).hexdigest(),
    )


def _aggregate_row(
    trust: CardTrust,
    filing: CardTrustFiling,
    exhibit_document: str,
    index: EdgarJsonReceipt,
    source: HttpReceipt,
    metrics: CardMetrics,
) -> dict[str, object]:
    return {
        "trust_id": trust.trust_id,
        "trust_name": trust.expected_name,
        "trust_cik": trust.trust_cik,
        "archive_cik": trust.archive_cik,
        "reporting_period_end": filing.period,
        "filed_at": filing.filed_at,
        "accession": filing.accession,
        "primary_document": filing.primary_document,
        "exhibit_document": exhibit_document,
        "ending_principal_receivables": metrics.ending_principal_receivables,
        "principal_payment_rate_pct": metrics.principal_payment_rate_pct,
        "payment_rate_basis": trust.payment_rate_basis,
        "portfolio_yield_pct": metrics.portfolio_yield_pct,
        "yield_basis": trust.yield_basis,
        "chargeoff_amount": metrics.chargeoff_amount,
        "chargeoff_amount_status": metrics.chargeoff_amount_status,
        "chargeoff_rate_pct": metrics.chargeoff_rate_pct,
        "chargeoff_basis": trust.chargeoff_basis,
        "metric_evidence_json": metrics.metric_evidence_json,
        "evidence_snippet_hash": metrics.evidence_snippet_hash,
        "source_url": source.source_url,
        "source_checksum": source.checksum,
        "source_bytes": len(source.content),
        "archive_index_url": index.source_url,
        "archive_index_checksum": index.checksum,
        "ingested_at": source.retrieved_at,
    }


class CardTrustIngestor:
    def __init__(
        self,
        store: AppendOnlyParquetStore,
        client: EdgarClient,
        trusts: Sequence[CardTrust],
    ) -> None:
        self._store = store
        self._client = client
        self._trusts = tuple(trusts)

    def ingest_all(
        self, *, max_filings_per_trust: int | None = None
    ) -> list[CardTrustIngestReceipt]:
        existing = self._store.read_table("card_trust_aggregates")
        prior_by_accession = {
            str(row["accession"]): cast(dict[str, object], row)
            for row in existing.iter_rows(named=True)
        }
        receipts: list[CardTrustIngestReceipt] = []
        for trust in self._trusts:
            filings = discover_card_filings(trust, self._client.submissions(trust.trust_cik))
            if max_filings_per_trust is not None:
                if max_filings_per_trust < 1:
                    raise CardTrustError("Card filing bound must be positive")
                filings = filings[-max_filings_per_trust:]
            for filing in filings:
                index = self._client.archive_index(trust.archive_cik, filing.accession)
                exhibit = select_exhibit(trust, index)
                prior = prior_by_accession.get(filing.accession)
                if prior is not None:
                    if (
                        prior["trust_id"] != trust.trust_id
                        or prior["exhibit_document"] != exhibit
                        or prior["archive_index_checksum"] != index.checksum
                    ):
                        raise CardTrustError(
                            f"Stored card-trust identity drift: {filing.accession}"
                        )
                    receipts.append(
                        CardTrustIngestReceipt(
                            trust.trust_id,
                            filing.period,
                            filing.accession,
                            str(prior["source_checksum"]),
                            True,
                        )
                    )
                    continue
                source = self._client.archive_document_receipt(
                    trust.archive_cik, filing.accession, exhibit
                )
                try:
                    metrics = parse_card_exhibit(
                        trust, source.content, expected_period=filing.period
                    )
                except CardTrustError as exc:
                    raise CardTrustError(f"{filing.accession}: {exc}") from exc
                row = _aggregate_row(trust, filing, exhibit, index, source, metrics)
                write = self._store.append("card_trust_aggregates", [row])
                already_present = write.already_present
                prior_by_accession[filing.accession] = row
                receipts.append(
                    CardTrustIngestReceipt(
                        trust.trust_id,
                        filing.period,
                        filing.accession,
                        source.checksum,
                        already_present,
                    )
                )
        return receipts


def validate_card_trusts(
    store: AppendOnlyParquetStore, trusts: Sequence[CardTrust]
) -> CardTrustValidation:
    frame = store.read_table("card_trust_aggregates")
    expected_ids = {item.trust_id for item in trusts}
    selected = frame.filter(pl.col("trust_id").is_in(expected_ids))
    if selected.is_empty() or selected["accession"].n_unique() != selected.height:
        raise CardTrustError("Card-trust aggregate lake is empty or has duplicate accessions")
    for trust in trusts:
        rows = selected.filter(pl.col("trust_id") == trust.trust_id).sort("reporting_period_end")
        periods = tuple(cast(date, value) for value in rows["reporting_period_end"])
        if (
            not periods
            or periods[0] != trust.history_start
            or periods[-1] != trust.history_end
            or tuple((item.year, item.month) for item in periods)
            != _month_keys(trust.history_start, trust.history_end)
            or len(periods) < trust.minimum_months
        ):
            raise CardTrustError(f"Card-trust aggregate coverage is incomplete: {trust.trust_id}")
        if rows.select(
            pl.any_horizontal(
                pl.col("ending_principal_receivables").is_null(),
                pl.col("principal_payment_rate_pct").is_null(),
                pl.col("portfolio_yield_pct").is_null(),
                pl.col("chargeoff_rate_pct").is_null(),
            ).any()
        ).item():
            raise CardTrustError(f"Card-trust required metric is null: {trust.trust_id}")
    reported = int((selected["chargeoff_amount_status"] == "REPORTED").sum())
    return CardTrustValidation(
        trusts=len(trusts),
        trust_months=selected.height,
        dollar_chargeoff_months=reported,
        rate_only_chargeoff_months=selected.height - reported,
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
        raise CardTrustError("A bounded card-trust run requires --allow-partial")
    trusts = load_card_trust_registry()
    if args.trust != "all":
        trusts = tuple(item for item in trusts if item.trust_id == args.trust)
        if not trusts:
            raise CardTrustError(f"Unknown card trust: {args.trust}")
    store = AppendOnlyParquetStore(args.lake_root)
    with HttpTransport(min_interval_seconds=0.11) as transport:
        receipts = CardTrustIngestor(store, EdgarClient(transport), trusts).ingest_all(
            max_filings_per_trust=args.max_filings_per_trust
        )
    validation = None if args.allow_partial else validate_card_trusts(store, trusts)
    print(
        json.dumps(
            {
                "status": "PARTIAL" if args.allow_partial else "PASS",
                "new_aggregate_batches": sum(not item.already_present for item in receipts),
                "validation": asdict(validation) if validation else None,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
