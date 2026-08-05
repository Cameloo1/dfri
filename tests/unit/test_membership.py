from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from dfri.ingest.http import HttpReceipt
from dfri.ingest.membership import (
    MembershipContractError,
    MembershipEntry,
    NportHolding,
    NportPortfolio,
    ParsedMembership,
    load_membership_contracts,
    load_pinned_membership,
    parse_nport_portfolio,
    parse_wikimedia_membership,
    reconcile_membership,
    verify_pinned_membership,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "sec"
P0 = {
    "GM": "0001467858",
    "F": "0000037996",
    "AMZN": "0001018724",
    "WMT": "0000104169",
    "TGT": "0000027419",
    "LOW": "0000060667",
    "HD": "0000354950",
    "BBY": "0000764478",
    "TJX": "0000109198",
    "TSCO": "0000916365",
}


def _receipt(content: bytes) -> HttpReceipt:
    return HttpReceipt(
        content=content,
        source_url="https://en.wikipedia.org/w/api.php?action=parse",
        checksum=hashlib.sha256(content).hexdigest(),
        retrieved_at=datetime(2026, 8, 4, tzinfo=UTC),
        status_code=200,
    )


def _entry(symbol: str, security: str, cik: str) -> MembershipEntry:
    return MembershipEntry(
        symbol=symbol,
        security=security,
        gics_sector="Consumer Discretionary",
        gics_sub_industry="Test fixture",
        headquarters="Test fixture",
        date_added="2026-01-01",
        cik=cik,
        founded="1900",
    )


def _current(*entries: MembershipEntry) -> ParsedMembership:
    return ParsedMembership(
        revision_id=1367510220,
        source_url="https://en.wikipedia.org/w/api.php?action=parse",
        checksum="a" * 64,
        retrieved_at="2026-08-04T00:00:00+00:00",
        entries=entries,
    )


def test_real_wikimedia_and_nport_excerpts_parse_exact_source_values() -> None:
    wikimedia = parse_wikimedia_membership(
        _receipt((FIXTURES / "sp500_wikimedia_excerpt.json").read_bytes())
    )
    assert wikimedia.revision_id == 1367510220
    assert [(entry.symbol, entry.cik) for entry in wikimedia.entries] == [
        ("AMZN", "0001018724"),
        ("GM", "0001467858"),
    ]

    nport = parse_nport_portfolio((FIXTURES / "sp500_nport_excerpt.xml").read_bytes())
    assert nport.submission_type == "NPORT-P"
    assert nport.registrant_cik == "0000884394"
    assert nport.report_period == "2026-03-31"
    assert nport.holdings == (
        NportHolding(name="Aflac Inc", cusip="001055102", lei="549300N0B7DOGLXWPP39"),
    )


def test_fixture_provenance_checksums_are_current() -> None:
    provenance = json.loads((FIXTURES / "sp500_membership.provenance.json").read_text())
    for filename, expected in provenance["fixture_sha256"].items():
        assert hashlib.sha256((FIXTURES / filename).read_bytes()).hexdigest() == expected
    assert provenance["licenses"]["sp500_wikimedia_excerpt.json"].startswith("CC BY-SA")


def test_lagged_nport_names_reconcile_only_through_explicit_aliases() -> None:
    current = _current(
        _entry("AMZN", "Amazon", "0001018724"),
        _entry("GM", "General Motors", "0001467858"),
    )
    portfolio = NportPortfolio(
        submission_type="NPORT-P",
        registrant_cik="0000884394",
        registrant_name="SPDR S&P 500 ETF Trust",
        report_period="2026-03-31",
        holdings=(
            NportHolding("Amazon.com Inc", "023135106", "amazon-lei"),
            NportHolding("General Motors Co", "37045V100", "gm-lei"),
        ),
    )
    contracts: dict[str, object] = {
        "current_membership": {"expected_share_class_rows": 2, "expected_issuers": 2},
        "sec_nport_crosscheck": {
            "form": "NPORT-P",
            "cik": "0000884394",
            "period": "2026-03-31",
            "expected_holdings": 2,
            "expected_issuers": 2,
            "post_period_events": [],
            "name_aliases": [{"holding_name": "Amazon.com Inc", "membership_security": "Amazon"}],
        },
    }
    result = reconcile_membership(current, portfolio, contracts)
    assert result.status == "PASS"
    assert result.explicit_name_aliases == 1

    broken = cast(dict[str, object], contracts["sec_nport_crosscheck"])
    without_alias = {**contracts, "sec_nport_crosscheck": {**broken, "name_aliases": []}}
    with pytest.raises(MembershipContractError, match="reconciliation failed"):
        reconcile_membership(current, portfolio, without_alias)


def test_post_period_change_must_match_both_sides() -> None:
    current = _current(_entry("CASY", "Casey's", "0000726958"))
    portfolio = NportPortfolio(
        submission_type="NPORT-P",
        registrant_cik="0000884394",
        registrant_name="SPDR S&P 500 ETF Trust",
        report_period="2026-03-31",
        holdings=(NportHolding("Hologic Inc", "436440101", "hologic-lei"),),
    )
    event = {
        "addition_effective": "2026-04-09",
        "added_symbol": "CASY",
        "added_security": "Casey's",
        "removal_effective": "2026-04-09",
        "removed_symbol": "HOLX",
        "removed_holding_name": "Hologic Inc",
    }
    contracts: dict[str, object] = {
        "current_membership": {"expected_share_class_rows": 1, "expected_issuers": 1},
        "sec_nport_crosscheck": {
            "form": "NPORT-P",
            "cik": "0000884394",
            "period": "2026-03-31",
            "expected_holdings": 1,
            "expected_issuers": 1,
            "post_period_events": [event],
            "name_aliases": [],
        },
    }
    assert reconcile_membership(current, portfolio, contracts).post_period_events == 1
    with pytest.raises(MembershipContractError, match="does not match current"):
        reconcile_membership(
            current,
            portfolio,
            {
                **contracts,
                "sec_nport_crosscheck": {
                    **cast(dict[str, object], contracts["sec_nport_crosscheck"]),
                    "post_period_events": [{**event, "added_security": "Wrong"}],
                },
            },
        )


def test_nport_placeholder_cusips_need_unique_name_pairs() -> None:
    source = b"""<root><submissionType>NPORT-P</submissionType><regCik>0000884394</regCik>
    <regName>Fund</regName><repPdDate>2026-03-31</repPdDate><invstOrSec>
    <name>Foreign A</name><cusip>000000000</cusip><lei>A</lei></invstOrSec><invstOrSec>
    <name>Foreign B</name><cusip>000000000</cusip><lei>B</lei></invstOrSec></root>"""
    assert len(parse_nport_portfolio(source).holdings) == 2
    duplicate = source.replace(b"Foreign B", b"Foreign A")
    with pytest.raises(MembershipContractError, match="holding keys"):
        parse_nport_portfolio(duplicate)


def test_checked_in_snapshot_is_attributed_complete_and_p0_members_are_exact() -> None:
    snapshot = load_pinned_membership()
    contracts = load_membership_contracts()
    entries = snapshot["entries"]
    assert isinstance(entries, list)
    assert len(entries) == 503
    by_symbol = {item["symbol"]: item for item in entries if isinstance(item, dict)}
    assert {symbol: by_symbol[symbol]["cik"] for symbol in P0} == P0
    assert len({item["cik"] for item in entries if isinstance(item, dict)}) == 500
    assert snapshot["license"] == "CC BY-SA 4.0"
    assert "Wikipedia contributors" in str(snapshot["attribution"])

    nport = contracts["sec_nport_crosscheck"]
    assert isinstance(nport, dict)
    assert len(nport["name_aliases"]) == 52
    assert len(nport["post_period_events"]) == 6


def test_pinned_snapshot_comparison_is_field_exact() -> None:
    pinned = load_pinned_membership()
    raw_entries = pinned["entries"]
    assert isinstance(raw_entries, list)
    entries = tuple(MembershipEntry(**item) for item in raw_entries if isinstance(item, dict))
    current = _current(*entries)
    verify_pinned_membership(current, pinned)
    changed = replace(current, entries=(replace(entries[0], security="Changed"), *entries[1:]))
    with pytest.raises(MembershipContractError, match="differs"):
        verify_pinned_membership(changed, pinned)


def test_wikimedia_parser_rejects_shape_and_identity_drift() -> None:
    content = (FIXTURES / "sp500_wikimedia_excerpt.json").read_bytes()
    with pytest.raises(MembershipContractError, match="headers"):
        parse_wikimedia_membership(_receipt(content.replace(b"GICS Sector", b"Sector")))
    with pytest.raises(MembershipContractError, match="symbol"):
        parse_wikimedia_membership(_receipt(content.replace(b"AMZN", b"bad")))
