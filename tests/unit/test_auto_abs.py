from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from dfri.ingest import auto_abs
from dfri.ingest.auto_abs import (
    AutoAbsError,
    AutoAbsIngestor,
    AutoAbsIngestReceipt,
    AutoAbsTrust,
    discover_trust_filings,
    load_auto_abs_registry,
    parse_ex102,
    parse_filing_index,
    validate_auto_abs,
)
from dfri.ingest.edgar import EdgarJsonReceipt
from dfri.ingest.http import HttpFileReceipt, HttpReceipt
from dfri.lake.schemas import schema_for
from dfri.lake.store import AppendOnlyParquetStore, file_sha256

FIXTURES = Path(__file__).parents[1] / "fixtures" / "sec"
EX102 = FIXTURES / "amcar_2023_1_ex102_excerpt.xml"
RETRIEVED = datetime(2026, 8, 4, 18, 0, tzinfo=UTC)


def _gzip_fixture(destination: Path) -> None:
    with destination.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            compressed.write(EX102.read_bytes())


def _submissions(trust: AutoAbsTrust, periods: list[date]) -> EdgarJsonReceipt:
    return EdgarJsonReceipt(
        kind="submissions",
        source_url=f"https://data.sec.gov/submissions/CIK{trust.cik}.json",
        checksum="a" * 64,
        retrieved_at=RETRIEVED.isoformat(),
        payload={
            "cik": trust.cik,
            "name": trust.expected_name,
            "filings": {
                "recent": {
                    "form": ["ABS-EE" for _ in periods],
                    "accessionNumber": [
                        f"0001963240-26-{index:06d}" for index, _ in enumerate(periods, 1)
                    ],
                    "filingDate": ["2026-07-24" for _ in periods],
                    "reportDate": [period.isoformat() for period in periods],
                }
            },
        },
    )


def _index_receipt(byte_count: int) -> HttpReceipt:
    content = f"""
    <html><table summary="Document Format Files">
      <tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
      <tr><td>1</td><td>ABS-EE</td><td><a href="form.htm">form.htm</a></td>
          <td>ABS-EE</td><td>100</td></tr>
      <tr><td>2</td><td>EX-102</td><td><a href="source.xml">source.xml</a></td>
          <td>EX-102</td><td>{byte_count}</td></tr>
      <tr><td>3</td><td>EX-103</td><td><a href="related.xml">related.xml</a></td>
          <td>EX-103</td><td>50</td></tr>
    </table></html>
    """.encode()
    return HttpReceipt(
        content=content,
        source_url="https://www.sec.gov/Archives/edgar/data/index.htm",
        checksum=hashlib.sha256(content).hexdigest(),
        retrieved_at=RETRIEVED,
        status_code=200,
    )


class _FakeEdgar:
    def __init__(self, trust: AutoAbsTrust) -> None:
        self.trust = trust
        self.downloads = 0

    def submissions(self, _cik: str) -> EdgarJsonReceipt:
        return _submissions(self.trust, [date(2026, 6, 30)])

    def archive_document_receipt(self, _cik: str, _accession: str, _document: str) -> HttpReceipt:
        return _index_receipt(EX102.stat().st_size)

    def archive_document_to_gzip(
        self, _cik: str, accession: str, document: str, destination: Path
    ) -> HttpFileReceipt:
        self.downloads += 1
        _gzip_fixture(destination)
        return HttpFileReceipt(
            path=destination,
            source_url=(
                f"https://www.sec.gov/Archives/edgar/data/1963240/"
                f"{accession.replace('-', '')}/{document}"
            ),
            checksum=hashlib.sha256(EX102.read_bytes()).hexdigest(),
            compressed_checksum=file_sha256(destination),
            retrieved_at=RETRIEVED,
            status_code=200,
            byte_count=EX102.stat().st_size,
            compressed_byte_count=destination.stat().st_size,
        )


def test_registry_pins_six_trusts_and_explicit_credit_spectrum() -> None:
    trusts = load_auto_abs_registry()
    assert len(trusts) == 6
    assert {item.credit_segment for item in trusts} >= {"prime", "subprime"}
    assert all(item.minimum_months == 12 for item in trusts)
    assert sum(item.freshness_mode == "active" for item in trusts) == 5
    terminal = [item for item in trusts if item.freshness_mode == "terminal_history"]
    assert len(terminal) == 1
    assert terminal[0].terminal_evidence_url and terminal[0].terminal_evidence_url.startswith(
        "https://www.sec.gov/"
    )
    assert all(len(item.cik) == 10 for item in trusts)
    assert all(
        item.classification_evidence_url.startswith("https://www.sec.gov/") for item in trusts
    )


def test_submissions_discovery_requires_exact_identity_and_contiguous_window() -> None:
    trust = load_auto_abs_registry()[0]
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
    filings = discover_trust_filings(trust, _submissions(trust, periods))
    assert len(filings) == 12
    assert filings[0].period == trust.history_start
    assert filings[-1].period == trust.history_end

    with pytest.raises(AutoAbsError, match="contiguous window"):
        discover_trust_filings(trust, _submissions(trust, periods[:-1]))
    changed = replace(trust, expected_name="Changed")
    with pytest.raises(AutoAbsError, match="identity changed"):
        discover_trust_filings(changed, _submissions(trust, periods))


def test_actual_ex102_fixture_aggregates_exact_values_without_identifiers(tmp_path: Path) -> None:
    provenance = json.loads(
        (FIXTURES / "amcar_2023_1_ex102_excerpt.provenance.json").read_text(encoding="utf-8")
    )
    assert provenance["fixture_checksum_sha256"] == hashlib.sha256(EX102.read_bytes()).hexdigest()
    assert provenance["source_checksum_sha256"] == (
        "0ef8755a6d8c76c79be9ad61b9edba47b16212a5797cea4d783d0cdf32567205"
    )
    archive = tmp_path / "fixture.xml.gz"
    _gzip_fixture(archive)
    metrics = parse_ex102(archive, expected_period=date(2026, 6, 30))
    assert metrics.asset_count == 1
    assert metrics.core_metric_asset_count == 1
    assert metrics.recovery_only_asset_count == 0
    assert metrics.asset_added_count == 0
    assert metrics.asset_added_indicator_observed_count == 1
    assert metrics.recovered_amount_sum == Decimal("0E-8")
    assert metrics.recovered_amount_observed_asset_count == 1
    assert metrics.original_loan_amount_sum == Decimal("17148.76000000")
    assert metrics.beginning_balance_sum == Decimal("4057.23000000")
    assert metrics.ending_balance_sum == Decimal("3717.93000000")
    assert metrics.weighted_avg_original_interest_rate == Decimal("0.1400000000")
    assert metrics.weighted_avg_reporting_interest_rate == Decimal("0E-10")
    assert metrics.reporting_interest_rate_asset_count == 1
    assert metrics.reporting_interest_rate_balance_sum == Decimal("3717.93000000")
    assert metrics.weighted_avg_original_loan_term == Decimal("86.00000000")
    assert metrics.weighted_avg_remaining_term == Decimal("11.00000000")
    assert metrics.remaining_term_asset_count == 1
    assert metrics.remaining_term_balance_sum == Decimal("3717.93000000")
    assert "assetNumber" not in schema_for("auto_abs_aggregates").names
    assert "obligorCreditScore" not in schema_for("auto_abs_aggregates").names

    with pytest.raises(AutoAbsError, match="reporting period"):
        parse_ex102(archive, expected_period=date(2026, 5, 31))


def test_filing_index_selects_exactly_one_ex102() -> None:
    identity = parse_filing_index(_index_receipt(12345))
    assert identity.document == "source.xml"
    assert identity.byte_count == 12345
    broken = _index_receipt(12345)
    broken = HttpReceipt(
        content=broken.content.replace(b"EX-102", b"EX-999"),
        source_url=broken.source_url,
        checksum=broken.checksum,
        retrieved_at=broken.retrieved_at,
        status_code=broken.status_code,
    )
    with pytest.raises(AutoAbsError, match="contains 0"):
        parse_filing_index(broken)


def test_ingestor_archives_raw_privately_and_appends_only_aggregate(tmp_path: Path) -> None:
    base = load_auto_abs_registry()[1]
    trust = replace(
        base,
        history_start=date(2026, 6, 30),
        history_end=date(2026, 6, 30),
        minimum_months=1,
    )
    lake_root = tmp_path / "lake" / "raw"
    store = AppendOnlyParquetStore(lake_root)
    client = _FakeEdgar(trust)
    ingestor = AutoAbsIngestor(
        store,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        lake_root / "_private" / "sec_auto_abs_ee",
        (trust,),
    )
    first = ingestor.ingest_all()
    second = ingestor.ingest_all()
    assert client.downloads == 1
    assert first[0].aggregate_already_present is False
    assert first[0].raw_already_present is False
    assert second[0].aggregate_already_present is True
    assert second[0].raw_already_present is True
    assert "_private" in first[0].raw_path
    assert not list((tmp_path / "published").rglob("*.xml*"))

    stored = store.read_table("auto_abs_aggregates")
    assert stored.height == 1
    assert stored["asset_count"][0] == 1
    assert stored["credit_segment"][0] == "subprime"
    validation = validate_auto_abs(store, (trust,))
    assert validation.trusts == 1
    assert validation.trust_months == 1
    assert validation.assets_across_snapshots == 1


def test_parser_fails_closed_on_namespace_and_duplicate_asset(tmp_path: Path) -> None:
    changed = EX102.read_text(encoding="utf-8").replace(AUTO_NAMESPACE, "urn:changed")
    namespace_path = tmp_path / "namespace.xml.gz"
    with gzip.open(namespace_path, "wt", encoding="utf-8") as handle:
        handle.write(changed)
    with pytest.raises(AutoAbsError, match="namespace changed"):
        parse_ex102(namespace_path)

    body = EX102.read_text(encoding="utf-8")
    asset = body[body.index("  <assets>") : body.index("</assetData>")]
    duplicate = body.replace("</assetData>", asset + "</assetData>")
    duplicate_path = tmp_path / "duplicate.xml.gz"
    with gzip.open(duplicate_path, "wt", encoding="utf-8") as handle:
        handle.write(duplicate)
    with pytest.raises(AutoAbsError, match="duplicate assetNumber"):
        parse_ex102(duplicate_path)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("<originalLoanAmount>17148.76000000", "<originalLoanAmount>bad", "not numeric"),
        ("<originalLoanTerm>86", "<originalLoanTerm>86.5", "not an integer"),
        ("<assetAddedIndicator>false", "<assetAddedIndicator>unknown", "not boolean"),
        (
            "<reportingPeriodBeginningLoanBalanceAmount>4057.23000000",
            "<reportingPeriodBeginningLoanBalanceAmount>-1.00000000",
            "negative money",
        ),
    ],
)
def test_parser_rejects_invalid_core_asset_values(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    content = EX102.read_text(encoding="utf-8").replace(old, new)
    path = tmp_path / f"{message.replace(' ', '-')}.xml.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(content)
    with pytest.raises(AutoAbsError, match=message):
        parse_ex102(path)


def test_parser_labels_recovery_only_records_without_fabricating_core_metrics(
    tmp_path: Path,
) -> None:
    recovery = """
  <assets>
    <assetTypeNumber>CIK number-Sequential asset number</assetTypeNumber>
    <assetNumber>0001963240 - recovery-only</assetNumber>
    <reportingPeriodBeginningDate>06-01-2026</reportingPeriodBeginningDate>
    <reportingPeriodEndingDate>06-30-2026</reportingPeriodEndingDate>
    <recoveredAmount>1.23000000</recoveredAmount>
  </assets>
"""
    content = EX102.read_text(encoding="utf-8").replace("</assetData>", recovery + "</assetData>")
    path = tmp_path / "recovery.xml.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(content)
    metrics = parse_ex102(path, expected_period=date(2026, 6, 30))
    assert metrics.asset_count == 2
    assert metrics.core_metric_asset_count == 1
    assert metrics.recovery_only_asset_count == 1
    assert metrics.recovered_amount_observed_asset_count == 2
    assert metrics.recovered_amount_sum == Decimal("1.23000000")
    assert metrics.original_loan_amount_sum == Decimal("17148.76000000")

    invalid = content.replace("<recoveredAmount>1.23000000</recoveredAmount>", "")
    invalid_path = tmp_path / "invalid-recovery.xml.gz"
    with gzip.open(invalid_path, "wt", encoding="utf-8") as handle:
        handle.write(invalid)
    with pytest.raises(AutoAbsError, match="unknown partial field set"):
        parse_ex102(invalid_path)


AUTO_NAMESPACE = "http://www.sec.gov/edgar/document/absee/autoloan/assetdata"


def test_cli_requires_explicit_partial_gate_and_reports_bounded_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(AutoAbsError, match="requires --allow-partial"):
        auto_abs.main(["--max-filings-per-trust", "1"])

    base = load_auto_abs_registry()[1]
    trust = replace(
        base,
        history_start=date(2026, 6, 30),
        history_end=date(2026, 6, 30),
        minimum_months=1,
    )

    class FakeContext:
        def __enter__(self) -> FakeContext:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class FakeRunner:
        def ingest_all(
            self, *, max_filings_per_trust: int | None = None
        ) -> list[AutoAbsIngestReceipt]:
            assert max_filings_per_trust == 1
            return [
                AutoAbsIngestReceipt(
                    trust_id=trust.trust_id,
                    period=trust.history_end,
                    accession="0001963240-26-000026",
                    source_checksum="a" * 64,
                    asset_count=1,
                    raw_path="private/source.xml.gz",
                    aggregate_already_present=False,
                    raw_already_present=False,
                )
            ]

    monkeypatch.setattr(auto_abs, "load_auto_abs_registry", lambda: (trust,))
    monkeypatch.setattr(auto_abs, "HttpTransport", lambda **_kwargs: FakeContext())
    monkeypatch.setattr(auto_abs, "EdgarClient", lambda _transport: object())
    monkeypatch.setattr(auto_abs, "AutoAbsIngestor", lambda *_args: FakeRunner())
    assert (
        auto_abs.main(
            [
                "--lake-root",
                str(tmp_path / "lake"),
                "--trust",
                trust.trust_id,
                "--max-filings-per-trust",
                "1",
                "--allow-partial",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "new_aggregate_batches": 1,
        "new_raw_archives": 1,
        "status": "PARTIAL",
        "validation": None,
    }
