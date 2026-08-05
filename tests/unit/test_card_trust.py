from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from dfri.ingest import card_trust
from dfri.ingest.card_trust import (
    CardTrust,
    CardTrustError,
    CardTrustIngestor,
    CardTrustIngestReceipt,
    CardTrustValidation,
    MetricContract,
    _metric_contract,
    _parse_number,
    discover_card_filings,
    load_card_trust_registry,
    parse_card_exhibit,
    select_exhibit,
    validate_card_trusts,
)
from dfri.ingest.edgar import EdgarJsonReceipt
from dfri.ingest.http import HttpReceipt
from dfri.lake.schemas import schema_for
from dfri.lake.store import AppendOnlyParquetStore

FIXTURES = Path(__file__).parents[1] / "fixtures" / "sec"
AMEX_FIXTURE = FIXTURES / "amex_card_2026_06_ex99_excerpt.html"
RETRIEVED = datetime(2026, 8, 4, 20, 0, tzinfo=UTC)


def _submissions(trust: CardTrust, periods: list[date]) -> EdgarJsonReceipt:
    return EdgarJsonReceipt(
        kind="submissions",
        source_url=f"https://data.sec.gov/submissions/CIK{trust.trust_cik}.json",
        checksum="a" * 64,
        retrieved_at=RETRIEVED.isoformat(),
        payload={
            "name": trust.expected_name,
            "filings": {
                "recent": {
                    "form": ["10-D" for _ in periods],
                    "accessionNumber": [
                        f"0001104659-26-{index:06d}" for index, _ in enumerate(periods, 1)
                    ],
                    "filingDate": ["2026-07-15" for _ in periods],
                    "reportDate": [period.isoformat() for period in periods],
                    "primaryDocument": ["form10d.htm" for _ in periods],
                }
            },
        },
    )


def _archive_index(trust: CardTrust, document: str) -> EdgarJsonReceipt:
    return EdgarJsonReceipt(
        kind="archive_index",
        source_url="https://www.sec.gov/Archives/edgar/data/index.json",
        checksum="b" * 64,
        retrieved_at=RETRIEVED.isoformat(),
        payload={"directory": {"item": [{"name": "form10d.htm"}, {"name": document}]}},
    )


def _source(content: bytes) -> HttpReceipt:
    return HttpReceipt(
        content=content,
        source_url="https://www.sec.gov/Archives/edgar/data/exhibit.htm",
        checksum=hashlib.sha256(content).hexdigest(),
        retrieved_at=RETRIEVED,
        status_code=200,
    )


def test_registry_pins_three_exact_trust_and_archive_identities() -> None:
    trusts = load_card_trust_registry()
    assert len(trusts) == 3
    assert {item.expected_name for item in trusts} == {
        "AMERICAN EXPRESS CREDIT ACCOUNT MASTER TRUST",
        "CITIBANK CREDIT CARD ISSUANCE TRUST",
        "BA Credit Card Trust",
    }
    assert all(item.minimum_months == 12 for item in trusts)
    assert any(item.trust_cik != item.archive_cik for item in trusts)
    assert all(item.identity_evidence_url.startswith("https://www.sec.gov/") for item in trusts)


def test_discovery_requires_exact_identity_and_contiguous_months() -> None:
    trust = load_card_trust_registry()[0]
    periods = [
        date(2025, 7, 31),
        date(2025, 8, 31),
        date(2025, 9, 30),
        date(2025, 10, 31),
        date(2025, 11, 30),
        date(2025, 12, 31),
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 31),
        date(2026, 4, 30),
        date(2026, 5, 31),
        date(2026, 6, 30),
    ]
    filings = discover_card_filings(trust, _submissions(trust, periods))
    assert len(filings) == 12
    assert filings[0].primary_document == "form10d.htm"
    assert filings[-1].period == trust.history_end

    with pytest.raises(CardTrustError, match="contiguous window"):
        discover_card_filings(trust, _submissions(trust, periods[:-1]))
    changed = replace(trust, expected_name="Changed")
    with pytest.raises(CardTrustError, match="identity changed"):
        discover_card_filings(changed, _submissions(trust, periods))


def test_actual_amex_fixture_extracts_exact_reported_metrics_and_evidence() -> None:
    trust = load_card_trust_registry()[0]
    content = AMEX_FIXTURE.read_bytes()
    provenance = json.loads(
        (FIXTURES / "amex_card_2026_06_ex99_excerpt.provenance.json").read_text(encoding="utf-8")
    )
    assert provenance["fixture_checksum_sha256"] == hashlib.sha256(content).hexdigest()
    assert provenance["source_checksum_sha256"] == (
        "fc4690a3a749e923de031838ad75056210a3a88f6f46d3a426d56611f17b4ce8"
    )
    metrics = parse_card_exhibit(trust, content, expected_period=date(2026, 6, 30))
    assert metrics.ending_principal_receivables == Decimal("25169022223.30")
    assert metrics.principal_payment_rate_pct == Decimal("52.292600")
    assert metrics.portfolio_yield_pct == Decimal("32.922600")
    assert metrics.chargeoff_amount == Decimal("36878335.84")
    assert metrics.chargeoff_amount_status == "REPORTED"
    assert metrics.chargeoff_rate_pct == Decimal("1.782700")
    assert (
        metrics.evidence_snippet_hash
        == hashlib.sha256(metrics.metric_evidence_json.encode()).hexdigest()
    )


def test_citi_preserves_rate_only_chargeoff_and_ba_applies_reported_thousands() -> None:
    _, citi, ba = load_card_trust_registry()
    citi_html = b"""
    <p>For the Due Period Ending June 25, 2026</p><table>
      <tr><td>1. Portfolio Yield for the Collateral Certificate</td><td>22.39</td><td>%</td></tr>
      <tr><td>Credit Loss Component</td><td>2.25</td><td>%</td></tr>
      <tr><td>4. Principal Payment Rate</td><td>41.93</td><td>%</td></tr>
      <tr><td>Principal Receivables End of Due Period</td><td>$</td><td>19,165,697,102</td></tr>
    </table>"""
    citi_metrics = parse_card_exhibit(citi, citi_html, expected_period=date(2026, 6, 25))
    assert citi_metrics.chargeoff_amount is None
    assert citi_metrics.chargeoff_amount_status == "NOT_REPORTED"
    assert citi_metrics.chargeoff_rate_pct == Decimal("2.250000")

    ba_html = (
        b"<p>June 30, 2026</p><table><tr><td>Investor Default Amount</td></tr>"
        b"<tr><td>Portfolio Yield</td></tr><tr><td>The Portfolio Yield for the related "
        b"Monthly Period</td><td>16.52%</td></tr></table><table><tr><td>Collections of "
        b"Trust Receivables and Payment Rates</td></tr><tr><td>(f)</td><td>Collections of "
        b"Principal Receivables as a percentage of prior month Principal Receivables</td>"
        b"<td>27.97%</td></tr></table><table><tr><td>Receivables in the Trust</td></tr>"
        b"<tr><td>BA Master Credit Card Trust II</td></tr><tr><td>(l)</td><td>The aggregate "
        b"amount of Receivables in the Trust as of the beginning of the related Monthly Period"
        b"</td><td>$</td><td>14,636,275,436.06</td></tr><tr><td>(m)</td><td>The aggregate "
        b"amount of Principal Receivables in the Trust as of the end of the day on the last "
        b"day of the related Monthly Period</td><td>$</td><td>14,333,396,313.73</td></tr>"
        b"</table><p>Principal Charge-Off Experience</p><table><tr><td>June 30, 2026</td>"
        b"<td>May 31, 2026</td></tr><tr><td>Total Charge-Offs</td><td>$</td><td>33,214"
        b"</td><td>$</td><td>32,459</td></tr><tr><td>Total Charge-Offs as a percentage "
        b"of Average Principal Receivables Outstanding</td><td>2.80</td><td>%</td><td>2.74"
        b"</td><td>%</td></tr></table>"
    )
    ba_metrics = parse_card_exhibit(ba, ba_html, expected_period=date(2026, 6, 30))
    assert ba_metrics.ending_principal_receivables == Decimal("14333396313.73")
    assert ba_metrics.principal_payment_rate_pct == Decimal("27.970000")
    assert ba_metrics.portfolio_yield_pct == Decimal("16.520000")
    assert ba_metrics.chargeoff_amount == Decimal("33214000.00")
    assert ba_metrics.chargeoff_rate_pct == Decimal("2.800000")


def test_parser_and_archive_selection_fail_closed_on_drift() -> None:
    trust = load_card_trust_registry()[0]
    assert select_exhibit(trust, _archive_index(trust, "filing_ex99-01.htm")) == (
        "filing_ex99-01.htm"
    )
    with pytest.raises(CardTrustError, match="2 matching"):
        select_exhibit(
            trust,
            EdgarJsonReceipt(
                kind="archive_index",
                source_url="index",
                checksum="a" * 64,
                retrieved_at=RETRIEVED.isoformat(),
                payload={
                    "directory": {
                        "item": [
                            {"name": "one_ex99-01.htm"},
                            {"name": "two_ex99-01.htm"},
                        ]
                    }
                },
            ),
        )


def test_metric_and_source_shape_errors_are_explicit() -> None:
    valid = {
        "label": "Metric",
        "table_anchors": ["Anchor"],
        "unit": "percent",
        "scale": "1",
    }
    with pytest.raises(CardTrustError, match="malformed"):
        _metric_contract("bad", "metric", "trust")
    with pytest.raises(CardTrustError, match="invalid"):
        _metric_contract({**valid, "table_anchors": []}, "metric", "trust")
    with pytest.raises(CardTrustError, match="invalid"):
        _metric_contract({**valid, "period_in_table": "yes"}, "metric", "trust")
    with pytest.raises(CardTrustError, match="incomplete"):
        _metric_contract({key: value for key, value in valid.items() if key != "label"}, "m", "t")
    with pytest.raises(CardTrustError, match="invalid"):
        _metric_contract({**valid, "scale": "-1"}, "metric", "trust")

    contract = MetricContract("Metric", ("Anchor",), False, "percent", Decimal("1"))
    with pytest.raises(CardTrustError, match="negative"):
        _parse_number(("Metric", "(1.0)"), 0, contract)
    with pytest.raises(CardTrustError, match="no numeric"):
        _parse_number(("Metric", "$", "—", "unknown"), 0, contract)

    trust = load_card_trust_registry()[0]
    malformed = EdgarJsonReceipt(
        kind="archive_index",
        source_url="index",
        checksum="a" * 64,
        retrieved_at=RETRIEVED.isoformat(),
        payload={},
    )
    with pytest.raises(CardTrustError, match="index shape"):
        select_exhibit(trust, malformed)
    with pytest.raises(CardTrustError, match="UTF-8"):
        parse_card_exhibit(trust, b"\xff", expected_period=trust.history_end)


def test_discovery_rejects_shape_date_and_duplicate_accession_drift() -> None:
    base = load_card_trust_registry()[0]
    trust = replace(
        base,
        history_start=date(2026, 5, 31),
        history_end=date(2026, 6, 30),
        minimum_months=2,
    )
    periods = [trust.history_start, trust.history_end]
    missing = _submissions(trust, periods)
    missing.payload["filings"] = {}
    with pytest.raises(CardTrustError, match="submissions shape"):
        discover_card_filings(trust, missing)

    uneven = _submissions(trust, periods)
    uneven.payload["filings"]["recent"]["primaryDocument"].pop()  # type: ignore[index]
    with pytest.raises(CardTrustError, match="changed length"):
        discover_card_filings(trust, uneven)

    invalid_date = _submissions(trust, periods)
    invalid_date.payload["filings"]["recent"]["reportDate"][0] = "bad"  # type: ignore[index]
    with pytest.raises(CardTrustError, match="date changed shape"):
        discover_card_filings(trust, invalid_date)

    duplicate = _submissions(trust, periods)
    recent = duplicate.payload["filings"]["recent"]  # type: ignore[index]
    recent["accessionNumber"][1] = recent["accessionNumber"][0]  # type: ignore[index]
    with pytest.raises(CardTrustError, match="duplicated"):
        discover_card_filings(trust, duplicate)
    with pytest.raises(CardTrustError, match="report date"):
        parse_card_exhibit(trust, AMEX_FIXTURE.read_bytes(), expected_period=date(2026, 5, 31))
    with pytest.raises(CardTrustError, match="matched 0"):
        parse_card_exhibit(
            trust,
            AMEX_FIXTURE.read_bytes().replace(b"Monthly Payment Rate", b"Missing Rate"),
            expected_period=date(2026, 6, 30),
        )


class _FakeEdgar:
    def __init__(
        self, trust: CardTrust, *, index_checksum: str = "b" * 64, content: bytes | None = None
    ) -> None:
        self.trust = trust
        self.index_checksum = index_checksum
        self.content = content or AMEX_FIXTURE.read_bytes()

    def submissions(self, _cik: str) -> EdgarJsonReceipt:
        return _submissions(self.trust, [self.trust.history_end])

    def archive_index(self, _cik: str, _accession: str) -> EdgarJsonReceipt:
        return replace(
            _archive_index(self.trust, "filing_ex99-01.htm"), checksum=self.index_checksum
        )

    def archive_document_receipt(self, _cik: str, _accession: str, _document: str) -> HttpReceipt:
        return _source(self.content)


def test_ingestor_is_append_only_idempotent_and_validator_checks_coverage(tmp_path: Path) -> None:
    base = load_card_trust_registry()[0]
    trust = replace(base, history_start=base.history_end, minimum_months=1)
    store = AppendOnlyParquetStore(tmp_path / "lake")
    ingestor = CardTrustIngestor(store, _FakeEdgar(trust), (trust,))  # type: ignore[arg-type]
    first = ingestor.ingest_all()
    second = ingestor.ingest_all()
    assert first[0].already_present is False
    assert second[0].already_present is True
    assert store.read_table("card_trust_aggregates").height == 1
    validation = validate_card_trusts(store, (trust,))
    assert validation.trusts == 1
    assert validation.trust_months == 1
    assert validation.dollar_chargeoff_months == 1
    assert "chargeoff_amount" in schema_for("card_trust_aggregates").names

    with pytest.raises(CardTrustError, match="bound must be positive"):
        ingestor.ingest_all(max_filings_per_trust=0)
    with pytest.raises(CardTrustError, match="identity drift"):
        CardTrustIngestor(
            store,
            _FakeEdgar(trust, index_checksum="c" * 64),  # type: ignore[arg-type]
            (trust,),
        ).ingest_all()


def test_ingestor_wraps_parser_failure_and_validator_rejects_missing_coverage(
    tmp_path: Path,
) -> None:
    base = load_card_trust_registry()[0]
    one_month = replace(base, history_start=base.history_end, minimum_months=1)
    broken = AMEX_FIXTURE.read_bytes().replace(b"Monthly Payment Rate", b"Missing Rate")
    with pytest.raises(CardTrustError, match="0001104659-26-000001"):
        CardTrustIngestor(
            AppendOnlyParquetStore(tmp_path / "broken"),
            _FakeEdgar(one_month, content=broken),  # type: ignore[arg-type]
            (one_month,),
        ).ingest_all()

    empty = AppendOnlyParquetStore(tmp_path / "empty")
    with pytest.raises(CardTrustError, match="empty"):
        validate_card_trusts(empty, (base,))

    partial_store = AppendOnlyParquetStore(tmp_path / "partial")
    CardTrustIngestor(
        partial_store,
        _FakeEdgar(one_month),
        (one_month,),  # type: ignore[arg-type]
    ).ingest_all()
    with pytest.raises(CardTrustError, match="coverage is incomplete"):
        validate_card_trusts(partial_store, (base,))


def test_cli_requires_partial_gate_and_reports_bounded_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(CardTrustError, match="requires --allow-partial"):
        card_trust.main(["--max-filings-per-trust", "1"])

    trust = load_card_trust_registry()[0]

    class FakeContext:
        def __enter__(self) -> FakeContext:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class FakeRunner:
        def ingest_all(
            self, *, max_filings_per_trust: int | None = None
        ) -> list[CardTrustIngestReceipt]:
            assert max_filings_per_trust == 1
            return [
                CardTrustIngestReceipt(
                    trust.trust_id,
                    trust.history_end,
                    "0001104659-26-083820",
                    "a" * 64,
                    False,
                )
            ]

    monkeypatch.setattr(card_trust, "HttpTransport", lambda **_kwargs: FakeContext())
    monkeypatch.setattr(card_trust, "EdgarClient", lambda _transport: object())
    monkeypatch.setattr(card_trust, "CardTrustIngestor", lambda *_args: FakeRunner())
    assert (
        card_trust.main(
            [
                "--lake-root",
                str(tmp_path / "lake"),
                "--max-filings-per-trust",
                "1",
                "--allow-partial",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "new_aggregate_batches": 1,
        "status": "PARTIAL",
        "validation": None,
    }

    with pytest.raises(CardTrustError, match="Unknown card trust"):
        card_trust.main(["--trust", "missing"])

    monkeypatch.setattr(
        card_trust,
        "validate_card_trusts",
        lambda *_args: CardTrustValidation(3, 36, 24, 12),
    )

    class FullRunner:
        def ingest_all(
            self, *, max_filings_per_trust: int | None = None
        ) -> list[CardTrustIngestReceipt]:
            assert max_filings_per_trust is None
            return []

    monkeypatch.setattr(card_trust, "CardTrustIngestor", lambda *_args: FullRunner())
    assert card_trust.main(["--lake-root", str(tmp_path / "full")]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "new_aggregate_batches": 0,
        "status": "PASS",
        "validation": {
            "dollar_chargeoff_months": 24,
            "rate_only_chargeoff_months": 12,
            "trust_months": 36,
            "trusts": 3,
        },
    }
