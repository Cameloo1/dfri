"""Verified SEC companyfacts and HTML footnote evidence for P0 and lender issuers."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path
from typing import Literal, cast

import polars as pl

from dfri.ingest.edgar import EdgarClient, EdgarJsonReceipt
from dfri.ingest.http import HttpReceipt, HttpTransport
from dfri.ingest.membership import load_pinned_membership
from dfri.lake.store import AppendOnlyParquetStore

IssuerRole = Literal["p0", "lender"]


class FilingFactsError(RuntimeError):
    """A filing identity, XBRL fact, or HTML evidence contract failed."""


@dataclass(frozen=True)
class TenKIdentity:
    filed_at: date
    period: date
    accession: str
    primary_document: str


@dataclass(frozen=True)
class RevenueFactContract:
    namespace: str
    tag: str
    unit: str
    period: date
    value: str


@dataclass(frozen=True)
class IssuerDefinition:
    role: IssuerRole
    ticker: str
    cik: str
    submissions_name: str
    companyfacts_name: str
    latest_10k: TenKIdentity
    revenue_fact: RevenueFactContract | None
    evidence_scope: str


@dataclass(frozen=True)
class HtmlFallbackDefinition:
    ticker: str
    cik: str
    evidence_type: str
    period: date
    filed_at: date
    accession: str
    primary_document: str
    source_checksum: str
    expected_snippet_hash: str
    context_anchor: str
    required_table_terms: tuple[str, ...]
    units: str


@dataclass(frozen=True)
class XbrlIngestReceipt:
    ticker: str
    role: IssuerRole
    source_url: str
    source_checksum: str
    accession: str
    row_count: int
    already_present: bool


@dataclass(frozen=True)
class FilingEvidence:
    context: str
    evidence_snippet: str
    snippet_hash: str
    extracted_table_json: str
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class FilingEvidenceReceipt:
    ticker: str
    evidence_type: str
    source_url: str
    source_checksum: str
    accession: str
    row_count: int
    snippet_hash: str
    already_present: bool


@dataclass(frozen=True)
class FilingFactsValidation:
    xbrl_rows: int
    xbrl_tickers: int
    p0_tickers: int
    lender_tickers: int
    html_evidence_rows: int


@dataclass(frozen=True)
class _CapturedTable:
    context: str
    rows: tuple[tuple[str, ...], ...]


class _FilingHtmlParser(HTMLParser):
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
        if tag in {"script", "style"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "table":
            if self._table_depth == 0:
                self._table_context = _normalized_text(" ".join(self._document_parts))[-4000:]
                self._rows = []
            self._table_depth += 1
        elif self._table_depth == 1 and tag == "tr":
            self._row = []
        elif self._table_depth == 1 and tag in {"th", "td"}:
            self._in_cell = True
            self._cell_parts = []
        elif tag == "br":
            self._document_parts.append(" ")
            if self._in_cell:
                self._cell_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if self._table_depth == 1 and tag in {"th", "td"} and self._in_cell:
            value = _normalized_text(" ".join(self._cell_parts))
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
        return _normalized_text(" ".join(self._document_parts))


class FilingFactsIngestor:
    """Fetch and append one immutable SEC response batch per registered issuer."""

    def __init__(
        self,
        store: AppendOnlyParquetStore,
        definitions: Sequence[IssuerDefinition] | None = None,
        fallbacks: Sequence[HtmlFallbackDefinition] | None = None,
    ) -> None:
        self._store = store
        loaded_definitions, loaded_fallbacks = load_issuer_registry()
        self._definitions = tuple(loaded_definitions if definitions is None else definitions)
        self._fallbacks = tuple(loaded_fallbacks if fallbacks is None else fallbacks)
        self._xbrl_existing = self._existing_by_identity("sec_xbrl_facts")
        self._html_existing = self._existing_by_identity("sec_filing_evidence")

    def fetch_all(
        self, client: EdgarClient, *, role: Literal["all", "p0", "lender"] = "all"
    ) -> tuple[XbrlIngestReceipt, ...]:
        selected = tuple(item for item in self._definitions if role == "all" or item.role == role)
        if not selected:
            raise FilingFactsError(f"No issuer definitions are registered for role {role}")
        receipts: list[XbrlIngestReceipt] = []
        for definition in selected:
            submissions = client.submissions(definition.cik)
            verify_latest_10k(definition, submissions)
            receipts.append(
                self.ingest_companyfacts(definition, client.companyfacts(definition.cik))
            )
        return tuple(receipts)

    def fetch_html_fallbacks(self, client: EdgarClient) -> tuple[FilingEvidenceReceipt, ...]:
        receipts: list[FilingEvidenceReceipt] = []
        definitions = {item.ticker: item for item in self._definitions}
        for fallback in self._fallbacks:
            issuer = definitions.get(fallback.ticker)
            if issuer is None or issuer.cik != fallback.cik:
                raise FilingFactsError(f"Unregistered HTML fallback issuer: {fallback.ticker}")
            source = client.archive_document_receipt(
                fallback.cik, fallback.accession, fallback.primary_document
            )
            receipts.append(self.ingest_html_evidence(issuer, fallback, source))
        return tuple(receipts)

    def ingest_companyfacts(
        self, definition: IssuerDefinition, receipt: EdgarJsonReceipt
    ) -> XbrlIngestReceipt:
        identity = (receipt.source_url, receipt.checksum)
        existing = self._xbrl_existing.get(identity)
        retrieved_at = _parse_datetime(receipt.retrieved_at)
        if existing is not None:
            stored_at = existing["ingested_at"].min()
            if not isinstance(stored_at, datetime):
                raise FilingFactsError("Stored SEC XBRL timestamp is invalid")
            rows = companyfacts_rows(definition, receipt, ingested_at=stored_at)
            if existing.height != len(rows):
                raise FilingFactsError("Stored SEC XBRL response row count changed")
            return XbrlIngestReceipt(
                definition.ticker,
                definition.role,
                receipt.source_url,
                receipt.checksum,
                definition.latest_10k.accession,
                len(rows),
                True,
            )

        rows = companyfacts_rows(definition, receipt, ingested_at=retrieved_at)
        write = self._store.append("sec_xbrl_facts", rows)
        stored = pl.read_parquet(write.path)
        self._xbrl_existing[identity] = stored
        return XbrlIngestReceipt(
            definition.ticker,
            definition.role,
            receipt.source_url,
            receipt.checksum,
            definition.latest_10k.accession,
            write.row_count,
            write.already_present,
        )

    def ingest_html_evidence(
        self,
        issuer: IssuerDefinition,
        fallback: HtmlFallbackDefinition,
        source: HttpReceipt,
    ) -> FilingEvidenceReceipt:
        if source.checksum != fallback.source_checksum:
            raise FilingFactsError(f"Pinned HTML checksum changed for {fallback.ticker}")
        identity = (source.source_url, source.checksum)
        existing = self._html_existing.get(identity)
        evidence = extract_filing_table(
            source.content,
            context_anchor=fallback.context_anchor,
            required_terms=fallback.required_table_terms,
        )
        if evidence.snippet_hash != fallback.expected_snippet_hash:
            raise FilingFactsError(f"Pinned HTML snippet changed for {fallback.ticker}")
        if existing is not None:
            if existing.height != 1 or existing["snippet_hash"][0] != evidence.snippet_hash:
                raise FilingFactsError("Stored SEC HTML evidence differs from source extraction")
            return FilingEvidenceReceipt(
                issuer.ticker,
                fallback.evidence_type,
                source.source_url,
                source.checksum,
                fallback.accession,
                len(evidence.rows),
                evidence.snippet_hash,
                True,
            )
        row = html_evidence_row(issuer, fallback, source, evidence)
        write = self._store.append("sec_filing_evidence", [row])
        self._html_existing[identity] = pl.read_parquet(write.path)
        return FilingEvidenceReceipt(
            issuer.ticker,
            fallback.evidence_type,
            source.source_url,
            source.checksum,
            fallback.accession,
            len(evidence.rows),
            evidence.snippet_hash,
            write.already_present,
        )

    def _existing_by_identity(self, table_name: str) -> dict[tuple[str, str], pl.DataFrame]:
        frame = self._store.read_table(table_name)
        existing: dict[tuple[str, str], pl.DataFrame] = {}
        if frame.is_empty():
            return existing
        for source_url, source_checksum in (
            frame.select(["source_url", "source_checksum"]).unique().iter_rows()
        ):
            identity = (str(source_url), str(source_checksum))
            existing[identity] = frame.filter(
                (pl.col("source_url") == source_url)
                & (pl.col("source_checksum") == source_checksum)
            )
        return existing


def companyfacts_rows(
    definition: IssuerDefinition,
    receipt: EdgarJsonReceipt,
    *,
    ingested_at: datetime | None = None,
) -> list[dict[str, object]]:
    payload = receipt.payload
    cik = payload.get("cik")
    entity_name = payload.get("entityName")
    facts = payload.get("facts")
    if str(cik).zfill(10) != definition.cik:
        raise FilingFactsError(f"SEC companyfacts CIK changed for {definition.ticker}")
    if entity_name != definition.companyfacts_name:
        raise FilingFactsError(f"SEC companyfacts entity name changed for {definition.ticker}")
    if not isinstance(facts, dict):
        raise FilingFactsError(f"SEC companyfacts are missing for {definition.ticker}")
    snapshot_at = ingested_at or _parse_datetime(receipt.retrieved_at)
    rows: list[dict[str, object]] = []
    for namespace, raw_group in sorted(facts.items()):
        if not isinstance(namespace, str) or not isinstance(raw_group, dict):
            raise FilingFactsError("SEC companyfacts namespace shape changed")
        for tag, raw_metadata in sorted(raw_group.items()):
            if not isinstance(tag, str) or not isinstance(raw_metadata, dict):
                raise FilingFactsError("SEC companyfacts tag shape changed")
            label = raw_metadata.get("label")
            description = raw_metadata.get("description")
            units = raw_metadata.get("units")
            if label is not None and not isinstance(label, str):
                raise FilingFactsError(f"SEC companyfacts label changed for {namespace}:{tag}")
            if description is not None and not isinstance(description, str):
                raise FilingFactsError(f"SEC companyfacts metadata changed for {namespace}:{tag}")
            if not isinstance(units, dict):
                raise FilingFactsError(f"SEC companyfacts units changed for {namespace}:{tag}")
            for unit, raw_items in sorted(units.items()):
                if not isinstance(unit, str) or not isinstance(raw_items, list):
                    raise FilingFactsError(f"SEC companyfacts unit shape changed for {tag}")
                for fact_index, raw_item in enumerate(raw_items):
                    if not isinstance(raw_item, dict):
                        raise FilingFactsError(f"SEC companyfacts observation changed for {tag}")
                    if raw_item.get("accn") != definition.latest_10k.accession:
                        continue
                    if raw_item.get("form") != "10-K":
                        continue
                    rows.append(
                        _xbrl_row(
                            definition,
                            entity_name,
                            namespace,
                            tag,
                            label,
                            description,
                            unit,
                            fact_index,
                            cast(dict[str, object], raw_item),
                            receipt,
                            snapshot_at,
                        )
                    )
    if not rows:
        raise FilingFactsError(f"No latest-10-K companyfacts for {definition.ticker}")
    _verify_revenue_fact(definition, rows)
    return rows


def verify_latest_10k(definition: IssuerDefinition, receipt: EdgarJsonReceipt) -> None:
    payload = receipt.payload
    if payload.get("name") != definition.submissions_name:
        raise FilingFactsError(f"SEC submissions entity name changed for {definition.ticker}")
    filings = payload.get("filings")
    if not isinstance(filings, dict) or not isinstance(filings.get("recent"), dict):
        raise FilingFactsError(f"SEC recent filings are missing for {definition.ticker}")
    recent = cast(dict[str, object], filings["recent"])
    fields = ("form", "filingDate", "reportDate", "accessionNumber", "primaryDocument")
    columns: list[list[object]] = []
    for field in fields:
        value = recent.get(field)
        if not isinstance(value, list):
            raise FilingFactsError(f"SEC submissions field {field} changed")
        columns.append(value)
    lengths = {len(column) for column in columns}
    if len(lengths) != 1:
        raise FilingFactsError("SEC submissions recent arrays have unequal lengths")
    latest: tuple[object, ...] | None = None
    for values in zip(*columns, strict=True):
        if values[0] == "10-K":
            latest = values
            break
    if latest is None:
        raise FilingFactsError(f"No 10-K filing found for {definition.ticker}")
    expected = definition.latest_10k
    actual = (
        latest[0],
        latest[1],
        latest[2],
        latest[3],
        latest[4],
    )
    wanted = (
        "10-K",
        expected.filed_at.isoformat(),
        expected.period.isoformat(),
        expected.accession,
        expected.primary_document,
    )
    if actual != wanted:
        raise FilingFactsError(f"Latest 10-K identity changed for {definition.ticker}: {actual!r}")


def extract_filing_table(
    content: bytes, *, context_anchor: str, required_terms: Sequence[str]
) -> FilingEvidence:
    try:
        html = content.decode("utf-8")
    except UnicodeDecodeError:
        html = content.decode("windows-1252", errors="replace")
    parser = _FilingHtmlParser()
    parser.feed(html)
    if context_anchor.casefold() not in parser.document_text.casefold():
        raise FilingFactsError("Configured filing context anchor is absent")
    matches: list[_CapturedTable] = []
    for table in parser.tables:
        table_text = " ".join(value for row in table.rows for value in row)
        if context_anchor.casefold() not in table.context.casefold():
            continue
        if all(term.casefold() in table_text.casefold() for term in required_terms):
            matches.append(table)
    if len(matches) != 1:
        raise FilingFactsError(f"Expected one filing table; found {len(matches)}")
    selected = matches[0]
    extracted_table_json = json.dumps(selected.rows, ensure_ascii=True, separators=(",", ":"))
    evidence_snippet = (
        selected.context[-1200:] + "\n" + "\n".join(" | ".join(row) for row in selected.rows)
    )
    snippet_hash = hashlib.sha256(evidence_snippet.encode("utf-8")).hexdigest()
    return FilingEvidence(
        context=selected.context,
        evidence_snippet=evidence_snippet,
        snippet_hash=snippet_hash,
        extracted_table_json=extracted_table_json,
        rows=selected.rows,
    )


def html_evidence_row(
    issuer: IssuerDefinition,
    fallback: HtmlFallbackDefinition,
    source: HttpReceipt,
    evidence: FilingEvidence,
) -> dict[str, object]:
    return {
        "issuer_role": issuer.role,
        "ticker": issuer.ticker,
        "cik": issuer.cik,
        "evidence_type": fallback.evidence_type,
        "period": fallback.period,
        "filed_at": fallback.filed_at,
        "accession": fallback.accession,
        "primary_document": fallback.primary_document,
        "source_url": source.source_url,
        "source_checksum": source.checksum,
        "context": evidence.context,
        "evidence_snippet": evidence.evidence_snippet,
        "snippet_hash": evidence.snippet_hash,
        "extracted_table_json": evidence.extracted_table_json,
        "ingested_at": source.retrieved_at,
    }


def validate_filing_facts(
    store: AppendOnlyParquetStore,
    definitions: Sequence[IssuerDefinition] | None = None,
    fallbacks: Sequence[HtmlFallbackDefinition] | None = None,
) -> FilingFactsValidation:
    loaded_definitions, loaded_fallbacks = load_issuer_registry()
    expected = tuple(loaded_definitions if definitions is None else definitions)
    expected_fallbacks = tuple(loaded_fallbacks if fallbacks is None else fallbacks)
    facts = store.read_table("sec_xbrl_facts")
    evidence = store.read_table("sec_filing_evidence")
    expected_tickers = {item.ticker for item in expected}
    observed_tickers = set(facts["ticker"].to_list())
    missing = sorted(expected_tickers - observed_tickers)
    if missing:
        raise FilingFactsError(f"SEC XBRL store is missing issuers: {missing}")
    for definition in expected:
        matched = facts.filter(
            (pl.col("ticker") == definition.ticker)
            & (pl.col("accession") == definition.latest_10k.accession)
        )
        if matched.is_empty():
            raise FilingFactsError(f"SEC XBRL store is missing accession for {definition.ticker}")
    for fallback in expected_fallbacks:
        matched = evidence.filter(
            (pl.col("ticker") == fallback.ticker)
            & (pl.col("accession") == fallback.accession)
            & (pl.col("source_checksum") == fallback.source_checksum)
        )
        if matched.height != 1:
            raise FilingFactsError(f"SEC HTML evidence is missing for {fallback.ticker}")
    return FilingFactsValidation(
        xbrl_rows=facts.height,
        xbrl_tickers=len(observed_tickers),
        p0_tickers=len({item.ticker for item in expected if item.role == "p0"}),
        lender_tickers=len({item.ticker for item in expected if item.role == "lender"}),
        html_evidence_rows=evidence.height,
    )


def load_issuer_registry() -> tuple[
    tuple[IssuerDefinition, ...], tuple[HtmlFallbackDefinition, ...]
]:
    payload = json.loads(
        resources.files("dfri.ingest").joinpath("issuer_registry.json").read_text(encoding="utf-8")
    )
    if not isinstance(payload, dict):
        raise FilingFactsError("Issuer registry must contain an object")
    definitions: list[IssuerDefinition] = []
    for role, key in (("p0", "p0"), ("lender", "lender_evidence")):
        raw_items = payload.get(key)
        if not isinstance(raw_items, list):
            raise FilingFactsError(f"Issuer registry is missing {key}")
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise FilingFactsError(f"Issuer registry {key} entry must be an object")
            definitions.append(_issuer_definition(cast(IssuerRole, role), raw))
    if len([item for item in definitions if item.role == "p0"]) != 10:
        raise FilingFactsError("Issuer registry must contain exactly ten P0 issuers")
    if len({item.ticker for item in definitions}) != len(definitions):
        raise FilingFactsError("Issuer registry tickers must be unique")
    _validate_p0_membership(definitions)

    raw_fallbacks = payload.get("html_fallbacks")
    if not isinstance(raw_fallbacks, list):
        raise FilingFactsError("Issuer registry is missing html_fallbacks")
    fallbacks = tuple(_fallback_definition(raw) for raw in raw_fallbacks if isinstance(raw, dict))
    if len(fallbacks) != len(raw_fallbacks) or not fallbacks:
        raise FilingFactsError("Issuer registry HTML fallbacks are invalid")
    return tuple(definitions), fallbacks


def _issuer_definition(role: IssuerRole, raw: Mapping[str, object]) -> IssuerDefinition:
    latest = _required_mapping(raw, "latest_10k")
    revenue_raw = raw.get("revenue_fact")
    revenue = None
    if revenue_raw is not None:
        if not isinstance(revenue_raw, dict):
            raise FilingFactsError("Issuer revenue_fact must be an object")
        revenue = RevenueFactContract(
            namespace=_required_str(revenue_raw, "namespace"),
            tag=_required_str(revenue_raw, "tag"),
            unit=_required_str(revenue_raw, "unit"),
            period=_parse_date(_required_str(revenue_raw, "period")),
            value=_required_str(revenue_raw, "value"),
        )
    scope = raw.get("selection_evidence", raw.get("evidence_scope"))
    if not isinstance(scope, str) or not scope:
        raise FilingFactsError("Issuer registry is missing evidence scope")
    return IssuerDefinition(
        role=role,
        ticker=_required_str(raw, "ticker"),
        cik=_required_str(raw, "cik"),
        submissions_name=_required_str(raw, "submissions_name"),
        companyfacts_name=_required_str(raw, "companyfacts_name"),
        latest_10k=TenKIdentity(
            filed_at=_parse_date(_required_str(latest, "filed_at")),
            period=_parse_date(_required_str(latest, "period")),
            accession=_required_str(latest, "accession"),
            primary_document=_required_str(latest, "primary_document"),
        ),
        revenue_fact=revenue,
        evidence_scope=scope,
    )


def _fallback_definition(raw: Mapping[str, object]) -> HtmlFallbackDefinition:
    terms = raw.get("required_table_terms")
    if not isinstance(terms, list) or not terms or not all(isinstance(item, str) for item in terms):
        raise FilingFactsError("HTML fallback terms must be non-empty strings")
    return HtmlFallbackDefinition(
        ticker=_required_str(raw, "ticker"),
        cik=_required_str(raw, "cik"),
        evidence_type=_required_str(raw, "evidence_type"),
        period=_parse_date(_required_str(raw, "period")),
        filed_at=_parse_date(_required_str(raw, "filed_at")),
        accession=_required_str(raw, "accession"),
        primary_document=_required_str(raw, "primary_document"),
        source_checksum=_required_str(raw, "source_checksum"),
        expected_snippet_hash=_required_str(raw, "expected_snippet_hash"),
        context_anchor=_required_str(raw, "context_anchor"),
        required_table_terms=tuple(cast(list[str], terms)),
        units=_required_str(raw, "units"),
    )


def _validate_p0_membership(definitions: Sequence[IssuerDefinition]) -> None:
    snapshot = load_pinned_membership()
    raw_entries = snapshot.get("entries")
    if not isinstance(raw_entries, list):
        raise FilingFactsError("Pinned membership snapshot is missing entries")
    membership = {
        item.get("symbol"): (item.get("cik"), item.get("security"))
        for item in raw_entries
        if isinstance(item, dict)
    }
    registry_payload = json.loads(
        resources.files("dfri.ingest").joinpath("issuer_registry.json").read_text(encoding="utf-8")
    )
    p0_raw = registry_payload.get("p0") if isinstance(registry_payload, dict) else None
    if not isinstance(p0_raw, list):
        raise FilingFactsError("Issuer registry P0 membership fields are missing")
    security_by_ticker = {
        item.get("ticker"): item.get("membership_security")
        for item in p0_raw
        if isinstance(item, dict)
    }
    for definition in definitions:
        if definition.role != "p0":
            continue
        if membership.get(definition.ticker) != (
            definition.cik,
            security_by_ticker.get(definition.ticker),
        ):
            raise FilingFactsError(f"P0 membership identity changed for {definition.ticker}")


def _xbrl_row(
    definition: IssuerDefinition,
    entity_name: object,
    namespace: str,
    tag: str,
    label: str | None,
    description: str | None,
    unit: str,
    fact_index: int,
    item: dict[str, object],
    receipt: EdgarJsonReceipt,
    ingested_at: datetime,
) -> dict[str, object]:
    end = _parse_date(_required_str(item, "end"))
    start_raw = item.get("start")
    start = _parse_date(start_raw) if isinstance(start_raw, str) else None
    fy_raw = item.get("fy")
    fiscal_year = fy_raw if isinstance(fy_raw, int) else None
    fp_raw = item.get("fp")
    fiscal_period = fp_raw if isinstance(fp_raw, str) else None
    frame_raw = item.get("frame")
    frame = frame_raw if isinstance(frame_raw, str) else None
    if "val" not in item:
        raise FilingFactsError(f"SEC companyfacts value is missing for {namespace}:{tag}")
    return {
        "issuer_role": definition.role,
        "ticker": definition.ticker,
        "cik": definition.cik,
        "entity_name": cast(str, entity_name),
        "namespace": namespace,
        "tag": tag,
        "label": label,
        "description": description,
        "unit": unit,
        "fact_index": fact_index,
        "period_start": start,
        "period_end": end,
        "fiscal_year": fiscal_year,
        "fiscal_period": fiscal_period,
        "form": _required_str(item, "form"),
        "filed_at": _parse_date(_required_str(item, "filed")),
        "accession": _required_str(item, "accn"),
        "frame": frame,
        "value_json": json.dumps(item["val"], ensure_ascii=True, separators=(",", ":")),
        "source_url": receipt.source_url,
        "source_checksum": receipt.checksum,
        "ingested_at": ingested_at,
    }


def _verify_revenue_fact(
    definition: IssuerDefinition, rows: Sequence[Mapping[str, object]]
) -> None:
    contract = definition.revenue_fact
    if contract is None:
        return
    matches = [
        row
        for row in rows
        if row["namespace"] == contract.namespace
        and row["tag"] == contract.tag
        and row["unit"] == contract.unit
        and row["period_end"] == contract.period
        and row["value_json"] == contract.value
    ]
    if len(matches) != 1:
        raise FilingFactsError(
            f"Pinned revenue fact changed for {definition.ticker}; matches={len(matches)}"
        )


def _required_mapping(parent: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise FilingFactsError(f"Issuer registry is missing {key}")
    return value


def _required_str(parent: Mapping[str, object], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value:
        raise FilingFactsError(f"Required filing field is missing: {key}")
    return value


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise FilingFactsError(f"Invalid SEC filing date: {value}") from exc


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FilingFactsError(f"Invalid SEC retrieval timestamp: {value}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\u00a0", " ")).strip()


def main() -> None:  # pragma: no cover - CLI and live-source boundary
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lake-root", type=Path, default=Path(".local/lake/raw"))
    parser.add_argument("--role", choices=("all", "p0", "lender"), default="all")
    parser.add_argument("--skip-html", action="store_true")
    args = parser.parse_args()
    store = AppendOnlyParquetStore(args.lake_root)
    definitions, fallbacks = load_issuer_registry()
    try:
        with HttpTransport(min_interval_seconds=0.11) as transport:
            client = EdgarClient(transport)
            xbrl = FilingFactsIngestor(store, definitions, fallbacks).fetch_all(
                client, role=args.role
            )
            html = (
                ()
                if args.skip_html or args.role == "lender"
                else FilingFactsIngestor(store, definitions, fallbacks).fetch_html_fallbacks(client)
            )
        expected = tuple(
            item for item in definitions if args.role == "all" or item.role == args.role
        )
        expected_fallbacks = () if args.skip_html or args.role == "lender" else fallbacks
        validation = validate_filing_facts(store, expected, expected_fallbacks)
    except Exception as exc:
        print(
            json.dumps(
                {"status": "BLOCKED", "error_type": type(exc).__name__, "error": str(exc)},
                sort_keys=True,
            )
        )
        raise SystemExit(1) from exc
    print(
        json.dumps(
            {
                "status": "PASS",
                "issuers": len(xbrl),
                "new_xbrl_batches": sum(not item.already_present for item in xbrl),
                "html_evidence": len(html),
                "new_html_batches": sum(not item.already_present for item in html),
                "validation": asdict(validation),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
