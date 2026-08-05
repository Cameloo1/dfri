"""Current S&P 500 membership snapshot with an independent SEC holdings cross-check."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path
from typing import cast
from xml.etree import ElementTree

from dfri.ingest.edgar import EdgarClient
from dfri.ingest.http import DEFAULT_USER_AGENT, HttpReceipt, HttpTransport

WIKIMEDIA_API = "https://en.wikipedia.org/w/api.php"
WIKIMEDIA_PAGE = "List of S&P 500 companies"
EXPECTED_HEADERS = (
    "Symbol",
    "Security",
    "GICS Sector",
    "GICS Sub-Industry",
    "Headquarters Location",
    "Date added",
    "CIK",
    "Founded",
)
SYMBOL_PATTERN = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z])?$")
CIK_PATTERN = re.compile(r"^\d{10}$")


class MembershipContractError(ValueError):
    """A current-membership or cross-check source violated its pinned contract."""


@dataclass(frozen=True)
class MembershipEntry:
    symbol: str
    security: str
    gics_sector: str
    gics_sub_industry: str
    headquarters: str
    date_added: str
    cik: str
    founded: str


@dataclass(frozen=True)
class ParsedMembership:
    revision_id: int
    source_url: str
    checksum: str
    retrieved_at: str
    entries: tuple[MembershipEntry, ...]


@dataclass(frozen=True)
class NportHolding:
    name: str
    cusip: str
    lei: str


@dataclass(frozen=True)
class NportPortfolio:
    submission_type: str
    registrant_cik: str
    registrant_name: str
    report_period: str
    holdings: tuple[NportHolding, ...]


@dataclass(frozen=True)
class ReconciliationResult:
    status: str
    current_share_class_rows: int
    current_issuers: int
    nport_holdings: int
    nport_issuers: int
    post_period_events: int
    explicit_name_aliases: int


class _ConstituentTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._inside = False
        self._table_depth = 0
        self._in_cell = False
        self._cell_parts: list[str] = []
        self._row: list[str] = []
        self.rows: list[tuple[str, ...]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "table" and not self._inside and attributes.get("id") == "constituents":
            self._inside = True
            self._table_depth = 1
            return
        if not self._inside:
            return
        if tag == "table":
            self._table_depth += 1
        elif tag == "tr":
            self._row = []
        elif tag in {"th", "td"}:
            self._in_cell = True
            self._cell_parts = []

    def handle_endtag(self, tag: str) -> None:
        if not self._inside:
            return
        if tag in {"th", "td"} and self._in_cell:
            self._row.append(" ".join("".join(self._cell_parts).split()))
            self._in_cell = False
        elif tag == "tr" and self._row:
            self.rows.append(tuple(self._row))
        elif tag == "table":
            self._table_depth -= 1
            if self._table_depth == 0:
                self._inside = False

    def handle_data(self, data: str) -> None:
        if self._inside and self._in_cell:
            self._cell_parts.append(data)


def parse_wikimedia_membership(receipt: HttpReceipt) -> ParsedMembership:
    """Parse and validate the current constituent table returned by MediaWiki."""

    try:
        payload = json.loads(receipt.content)
    except json.JSONDecodeError as exc:
        raise MembershipContractError("Wikimedia membership response is not JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("parse"), dict):
        raise MembershipContractError("Wikimedia membership response is missing parse")
    parsed = cast(dict[str, object], payload["parse"])
    revision_id = parsed.get("revid")
    html = parsed.get("text")
    if not isinstance(revision_id, int) or not isinstance(html, str):
        raise MembershipContractError("Wikimedia response is missing revision or HTML")

    parser = _ConstituentTableParser()
    parser.feed(html)
    if not parser.rows or parser.rows[0] != EXPECTED_HEADERS:
        raise MembershipContractError("Wikimedia constituent headers changed")

    entries: list[MembershipEntry] = []
    for row_number, row in enumerate(parser.rows[1:], start=1):
        if len(row) != len(EXPECTED_HEADERS):
            raise MembershipContractError(
                f"Wikimedia constituent row {row_number} has {len(row)} cells"
            )
        symbol, security, sector, sub_industry, headquarters, added, cik, founded = row
        if not SYMBOL_PATTERN.fullmatch(symbol):
            raise MembershipContractError(f"Invalid membership symbol: {symbol}")
        if not CIK_PATTERN.fullmatch(cik):
            raise MembershipContractError(f"Invalid membership CIK for {symbol}")
        try:
            date.fromisoformat(added)
        except ValueError as exc:
            raise MembershipContractError(f"Invalid membership date for {symbol}") from exc
        if not all((security, sector, sub_industry, headquarters, founded)):
            raise MembershipContractError(f"Incomplete membership row for {symbol}")
        entries.append(
            MembershipEntry(
                symbol=symbol,
                security=security,
                gics_sector=sector,
                gics_sub_industry=sub_industry,
                headquarters=headquarters,
                date_added=added,
                cik=cik,
                founded=founded,
            )
        )

    if len({entry.symbol for entry in entries}) != len(entries):
        raise MembershipContractError("Wikimedia constituent symbols are not unique")
    return ParsedMembership(
        revision_id=revision_id,
        source_url=receipt.source_url,
        checksum=receipt.checksum,
        retrieved_at=receipt.retrieved_at.isoformat(),
        entries=tuple(entries),
    )


def parse_nport_portfolio(content: bytes) -> NportPortfolio:
    """Parse only the issuer identities required for the independent holdings check."""

    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise MembershipContractError("SEC N-PORT document is not XML") from exc

    submission_type = _required_local_text(root, "submissionType")
    registrant_cik = _required_local_text(root, "regCik")
    registrant_name = _required_local_text(root, "regName")
    report_period = _required_local_text(root, "repPdDate")
    holdings: list[NportHolding] = []
    for node in root.iter():
        if _local_name(node.tag) != "invstOrSec":
            continue
        holdings.append(
            NportHolding(
                name=_required_local_text(node, "name"),
                cusip=_required_local_text(node, "cusip"),
                lei=_required_local_text(node, "lei"),
            )
        )
    # Foreign issuers legitimately use the filing placeholder CUSIP 000000000.
    # Share classes can share an issuer name, so the filing's usable holding key is the pair.
    if len({(holding.name, holding.cusip) for holding in holdings}) != len(holdings):
        raise MembershipContractError("SEC N-PORT name/CUSIP holding keys are not unique")
    return NportPortfolio(
        submission_type=submission_type,
        registrant_cik=registrant_cik,
        registrant_name=registrant_name,
        report_period=report_period,
        holdings=tuple(holdings),
    )


def reconcile_membership(
    current: ParsedMembership,
    portfolio: NportPortfolio,
    contracts: dict[str, object],
) -> ReconciliationResult:
    """Reconcile a lagged public fund filing to current membership, failing on any gap."""

    current_contract = _required_object(contracts, "current_membership")
    nport_contract = _required_object(contracts, "sec_nport_crosscheck")
    expected_rows = _required_int(current_contract, "expected_share_class_rows")
    expected_issuers = _required_int(current_contract, "expected_issuers")
    expected_holdings = _required_int(nport_contract, "expected_holdings")
    expected_nport_issuers = _required_int(nport_contract, "expected_issuers")

    if len(current.entries) != expected_rows:
        raise MembershipContractError(
            f"Current membership has {len(current.entries)} rows; expected {expected_rows}"
        )
    if len({entry.cik for entry in current.entries}) != expected_issuers:
        raise MembershipContractError("Current membership issuer count changed")
    if portfolio.submission_type != _required_str(nport_contract, "form"):
        raise MembershipContractError("SEC cross-check form changed")
    if portfolio.registrant_cik != _required_str(nport_contract, "cik"):
        raise MembershipContractError("SEC cross-check registrant changed")
    if portfolio.report_period != _required_str(nport_contract, "period"):
        raise MembershipContractError("SEC cross-check report period changed")
    if len(portfolio.holdings) != expected_holdings:
        raise MembershipContractError("SEC cross-check holding count changed")

    raw_holdings = Counter(holding.name for holding in portfolio.holdings)
    if len({_normalize_issuer(name) for name in raw_holdings}) != expected_nport_issuers:
        raise MembershipContractError("SEC cross-check issuer count changed")

    current_by_symbol = {entry.symbol: entry for entry in current.entries}
    events = _required_object_list(nport_contract, "post_period_events")
    adjusted = raw_holdings.copy()
    for event in events:
        removed_name = _required_str(event, "removed_holding_name")
        removed_symbol = _required_str(event, "removed_symbol")
        added_symbol = _required_str(event, "added_symbol")
        added_security = _required_str(event, "added_security")
        _parse_iso_date(_required_str(event, "addition_effective"))
        _parse_iso_date(_required_str(event, "removal_effective"))
        if adjusted[removed_name] != 1:
            raise MembershipContractError(
                f"Reconciliation removal is absent or ambiguous: {removed_name}"
            )
        if removed_symbol in current_by_symbol:
            raise MembershipContractError(
                f"Reconciliation removed symbol remains current: {removed_symbol}"
            )
        current_added = current_by_symbol.get(added_symbol)
        if current_added is None or current_added.security != added_security:
            raise MembershipContractError(
                f"Reconciliation addition does not match current membership: {added_symbol}"
            )
        adjusted[removed_name] -= 1
        if adjusted[removed_name] == 0:
            del adjusted[removed_name]
        adjusted[added_security] += 1

    aliases_raw = _required_object_list(nport_contract, "name_aliases")
    aliases: dict[str, str] = {}
    current_security_names = {entry.security for entry in current.entries}
    for item in aliases_raw:
        holding_name = _required_str(item, "holding_name")
        security = _required_str(item, "membership_security")
        if holding_name in aliases:
            raise MembershipContractError(f"Duplicate SEC name alias: {holding_name}")
        if holding_name not in raw_holdings:
            raise MembershipContractError(f"Unused SEC name alias: {holding_name}")
        if security not in current_security_names:
            raise MembershipContractError(f"Alias target is not current: {security}")
        aliases[holding_name] = security

    adjusted_issuers: Counter[str] = Counter()
    for name, count in adjusted.items():
        adjusted_issuers[_normalize_issuer(aliases.get(name, name))] += count
    current_issuers = Counter(_normalize_issuer(entry.security) for entry in current.entries)
    if adjusted_issuers != current_issuers:
        missing = sorted((current_issuers - adjusted_issuers).elements())
        extra = sorted((adjusted_issuers - current_issuers).elements())
        raise MembershipContractError(
            f"SEC/current issuer reconciliation failed; missing={missing[:10]}, extra={extra[:10]}"
        )

    return ReconciliationResult(
        status="PASS",
        current_share_class_rows=len(current.entries),
        current_issuers=len(current_issuers),
        nport_holdings=len(portfolio.holdings),
        nport_issuers=len({_normalize_issuer(name) for name in raw_holdings}),
        post_period_events=len(events),
        explicit_name_aliases=len(aliases),
    )


def load_membership_contracts() -> dict[str, object]:
    return _load_resource_json("membership_contracts.json")


def load_pinned_membership() -> dict[str, object]:
    return _load_resource_json("membership_snapshot.json")


def verify_pinned_membership(current: ParsedMembership, pinned: dict[str, object]) -> None:
    raw_entries = pinned.get("entries")
    if not isinstance(raw_entries, list):
        raise MembershipContractError("Pinned membership snapshot is missing entries")
    expected: list[dict[str, object]] = []
    for item in raw_entries:
        if not isinstance(item, dict):
            raise MembershipContractError("Pinned membership entry is not an object")
        expected.append(cast(dict[str, object], item))
    actual = [asdict(entry) for entry in current.entries]
    if actual != expected:
        expected_symbols = {str(item.get("symbol")) for item in expected}
        actual_symbols = {entry.symbol for entry in current.entries}
        raise MembershipContractError(
            "Current membership differs from the pinned snapshot; "
            f"added={sorted(actual_symbols - expected_symbols)}, "
            f"removed={sorted(expected_symbols - actual_symbols)}"
        )


def build_snapshot(current: ParsedMembership, contracts: dict[str, object]) -> dict[str, object]:
    current_contract = _required_object(contracts, "current_membership")
    return {
        "schema_version": 1,
        "as_of": current.retrieved_at,
        "source": {
            "page_title": _required_str(current_contract, "page_title"),
            "page_url": _required_str(current_contract, "page_url"),
            "api_response_url": current.source_url,
            "revision_id": current.revision_id,
            "response_checksum": current.checksum,
        },
        "attribution": (
            "Source: Wikipedia contributors, List of S&P 500 companies, revision "
            f"{current.revision_id}."
        ),
        "license": _required_str(current_contract, "license"),
        "license_url": _required_str(current_contract, "license_url"),
        "entries": [asdict(entry) for entry in current.entries],
    }


def _fetch_current(transport: HttpTransport) -> ParsedMembership:
    receipt = transport.get(
        WIKIMEDIA_API,
        params={
            "action": "parse",
            "page": WIKIMEDIA_PAGE,
            "prop": "text|revid",
            "format": "json",
            "formatversion": 2,
        },
        headers={"Accept": "application/json"},
    )
    return parse_wikimedia_membership(receipt)


def _live_verification(*, refresh_snapshot: bool) -> tuple[dict[str, object], ParsedMembership]:
    contracts = load_membership_contracts()
    nport_contract = _required_object(contracts, "sec_nport_crosscheck")
    with HttpTransport(user_agent=DEFAULT_USER_AGENT, min_interval_seconds=0.11) as transport:
        current = _fetch_current(transport)
        document = EdgarClient(transport).archive_document(
            _required_str(nport_contract, "cik"),
            _required_str(nport_contract, "accession"),
            _required_str(nport_contract, "document"),
        )
    checksum = hashlib.sha256(document).hexdigest()
    if checksum != _required_str(nport_contract, "checksum"):
        raise MembershipContractError("Immutable SEC N-PORT checksum changed")
    portfolio = parse_nport_portfolio(document)
    reconciliation = reconcile_membership(current, portfolio, contracts)
    if not refresh_snapshot:
        verify_pinned_membership(current, load_pinned_membership())

    return (
        {
            "schema_version": 1,
            "status": "PASS",
            "verified_at": current.retrieved_at,
            "current_membership": {
                "source_url": current.source_url,
                "revision_id": current.revision_id,
                "checksum": current.checksum,
                "share_class_rows": len(current.entries),
                "issuers": len({entry.cik for entry in current.entries}),
                "pinned_snapshot_match": not refresh_snapshot,
            },
            "sec_nport_crosscheck": {
                "source_url": _required_str(nport_contract, "source_url"),
                "filing_index_url": _required_str(nport_contract, "filing_index_url"),
                "accession": _required_str(nport_contract, "accession"),
                "period": portfolio.report_period,
                "checksum": checksum,
                "reconciliation": asdict(reconciliation),
            },
        },
        current,
    )


def _normalize_issuer(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    normalized = normalized.casefold().replace("&", " and ")
    normalized = re.sub(
        r"\b(the|plc|nv|sa|ag|ltd|limited|holdings?|group|corporation|corp|company|co|"
        r"incorporated|inc|class [abc])\b",
        " ",
        normalized,
    )
    return re.sub(r"[^a-z0-9]+", "", normalized)


def _required_local_text(root: ElementTree.Element, local_name: str) -> str:
    for node in root.iter():
        if _local_name(node.tag) == local_name and node.text and node.text.strip():
            return node.text.strip()
    raise MembershipContractError(f"SEC N-PORT is missing {local_name}")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _load_resource_json(filename: str) -> dict[str, object]:
    try:
        payload = json.loads(
            resources.files("dfri.ingest").joinpath(filename).read_text(encoding="utf-8")
        )
    except FileNotFoundError as exc:
        raise MembershipContractError(f"Missing checked-in {filename}") from exc
    if not isinstance(payload, dict):
        raise MembershipContractError(f"{filename} must contain an object")
    return cast(dict[str, object], payload)


def _required_object(parent: dict[str, object], key: str) -> dict[str, object]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise MembershipContractError(f"Membership contract is missing {key}")
    return cast(dict[str, object], value)


def _required_object_list(parent: dict[str, object], key: str) -> list[dict[str, object]]:
    value = parent.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise MembershipContractError(f"Membership contract {key} must be an object list")
    return cast(list[dict[str, object]], value)


def _required_str(parent: dict[str, object], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value:
        raise MembershipContractError(f"Membership contract is missing {key}")
    return value


def _required_int(parent: dict[str, object], key: str) -> int:
    value = parent.get(key)
    if not isinstance(value, int):
        raise MembershipContractError(f"Membership contract is missing {key}")
    return value


def _parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise MembershipContractError(f"Invalid reconciliation date: {value}") from exc


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary_name).replace(path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def main() -> None:  # pragma: no cover - CLI and live-source boundary
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path(".local/evidence/membership-verification.json")
    )
    parser.add_argument("--refresh-snapshot", action="store_true")
    parser.add_argument("--snapshot-output", type=Path)
    args = parser.parse_args()
    if args.snapshot_output is not None and not args.refresh_snapshot:
        parser.error("--snapshot-output requires --refresh-snapshot")
    try:
        receipt, current = _live_verification(refresh_snapshot=args.refresh_snapshot)
        if args.refresh_snapshot:
            if args.snapshot_output is None:
                parser.error("--refresh-snapshot requires --snapshot-output")
            _write_json(args.snapshot_output, build_snapshot(current, load_membership_contracts()))
    except Exception as exc:
        blocked: dict[str, object] = {
            "schema_version": 1,
            "status": "BLOCKED",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        _write_json(args.output, blocked)
        raise SystemExit(1) from exc
    _write_json(args.output, receipt)
    print(
        json.dumps(
            {
                "status": "PASS",
                "share_class_rows": len(current.entries),
                "issuers": len({entry.cik for entry in current.entries}),
                "revision_id": current.revision_id,
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
