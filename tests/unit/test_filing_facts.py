from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dfri.ingest.edgar import EdgarJsonReceipt
from dfri.ingest.filing_facts import (
    FilingFactsError,
    FilingFactsIngestor,
    companyfacts_rows,
    extract_filing_table,
    load_issuer_registry,
    validate_filing_facts,
    verify_latest_10k,
)
from dfri.ingest.http import HttpReceipt
from dfri.lake.store import AppendOnlyParquetStore

FIXTURES = Path(__file__).parents[1] / "fixtures" / "sec"
RETRIEVED = datetime(2026, 8, 4, 16, 39, 38, tzinfo=UTC)


def _json_receipt(kind: str, filename: str, checksum: str = "a" * 64) -> EdgarJsonReceipt:
    payload = json.loads((FIXTURES / filename).read_text(encoding="utf-8"))
    return EdgarJsonReceipt(
        kind=kind,
        source_url=(
            "https://data.sec.gov/submissions/CIK0001018724.json"
            if kind == "submissions"
            else "https://data.sec.gov/api/xbrl/companyfacts/CIK0001018724.json"
        ),
        checksum=checksum,
        retrieved_at=RETRIEVED.isoformat(),
        payload=payload,
    )


def _html_receipt(content: bytes) -> HttpReceipt:
    return HttpReceipt(
        content=content,
        source_url=(
            "https://www.sec.gov/Archives/edgar/data/1018724/000101872426000004/amzn-20251231.htm"
        ),
        checksum=hashlib.sha256(content).hexdigest(),
        retrieved_at=RETRIEVED,
        status_code=200,
    )


def test_registry_locks_ten_current_p0_and_seven_lender_issuers() -> None:
    definitions, fallbacks = load_issuer_registry()
    p0 = {item.ticker: item for item in definitions if item.role == "p0"}
    lenders = {item.ticker: item for item in definitions if item.role == "lender"}
    assert set(p0) == {"GM", "F", "AMZN", "WMT", "TGT", "LOW", "HD", "BBY", "ULTA", "TSCO"}
    assert set(lenders) == {"SYF", "BFH", "COF", "DFS_HIST", "AFRM", "FMCC", "GMF"}
    assert p0["AMZN"].latest_10k.accession == "0001018724-26-000004"
    assert p0["AMZN"].revenue_fact is not None
    assert p0["AMZN"].revenue_fact.value == "716924000000"
    assert "third-party account ownership" in p0["ULTA"].evidence_scope
    assert "Citi" in p0["TSCO"].evidence_scope
    assert fallbacks[0].source_checksum == (
        "beb1bfc28558135e76882db0cf36ba72a1ab44e2144fe37534f4da1e120ddb57"
    )


def test_actual_submissions_fixture_verifies_latest_10k_identity() -> None:
    definitions, _ = load_issuer_registry()
    amazon = next(item for item in definitions if item.ticker == "AMZN")
    receipt = _json_receipt("submissions", "amazon_submissions_2026_excerpt.json")
    verify_latest_10k(amazon, receipt)

    broken = dict(receipt.payload)
    broken["name"] = "Changed"
    with pytest.raises(FilingFactsError, match="entity name"):
        verify_latest_10k(amazon, replace(receipt, payload=broken))

    filings = json.loads(json.dumps(receipt.payload))
    filings["filings"]["recent"]["reportDate"] = ["2024-12-31"]
    with pytest.raises(FilingFactsError, match="identity changed"):
        verify_latest_10k(amazon, replace(receipt, payload=filings))


def test_actual_companyfacts_fixture_extracts_exact_latest_10k_rows() -> None:
    definitions, _ = load_issuer_registry()
    amazon = next(item for item in definitions if item.ticker == "AMZN")
    receipt = _json_receipt("companyfacts", "amazon_companyfacts_2025_excerpt.json")
    rows = companyfacts_rows(amazon, receipt)
    assert len(rows) == 3
    assert rows[-1]["tag"] == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert rows[-1]["period_end"].isoformat() == "2025-12-31"
    assert rows[-1]["value_json"] == "716924000000"
    assert rows[-1]["accession"] == "0001018724-26-000004"
    assert rows[-1]["source_checksum"] == "a" * 64

    changed = json.loads(json.dumps(receipt.payload))
    changed["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"][
        "USD"
    ][-1]["val"] = 1
    with pytest.raises(FilingFactsError, match="Pinned revenue fact changed"):
        companyfacts_rows(amazon, replace(receipt, payload=changed))


def test_companyfacts_ingest_is_append_only_and_idempotent(tmp_path: Path) -> None:
    definitions, _ = load_issuer_registry()
    amazon = next(item for item in definitions if item.ticker == "AMZN")
    store = AppendOnlyParquetStore(tmp_path)
    ingestor = FilingFactsIngestor(store, (amazon,), ())
    receipt = _json_receipt("companyfacts", "amazon_companyfacts_2025_excerpt.json")
    first = ingestor.ingest_companyfacts(amazon, receipt)
    second = ingestor.ingest_companyfacts(amazon, receipt)
    assert first.row_count == 3
    assert first.already_present is False
    assert second.already_present is True
    assert store.read_table("sec_xbrl_facts").height == 3


def test_actual_10k_footnote_fixture_extracts_table_and_hash() -> None:
    content = (FIXTURES / "amazon_2025_segment_footnote_excerpt.html").read_bytes()
    evidence = extract_filing_table(
        content,
        context_anchor="Note 10 — SEGMENT INFORMATION",
        required_terms=("2023", "North America", "International", "AWS", "716,924"),
    )
    assert len(evidence.rows) == 22
    assert ("Net sales", "$", "574,785", "$", "637,959", "$", "716,924") in evidence.rows
    assert evidence.snippet_hash == (
        "efc5c12c99f4337265a4588898311119ad22b2c4fb459db55feecbe12d54f40a"
    )
    assert json.loads(evidence.extracted_table_json)[0] == ["Year Ended December 31,"]

    with pytest.raises(FilingFactsError, match="context anchor"):
        extract_filing_table(content, context_anchor="Absent note", required_terms=("AWS",))
    with pytest.raises(FilingFactsError, match="found 0"):
        extract_filing_table(
            content,
            context_anchor="Note 10 — SEGMENT INFORMATION",
            required_terms=("not in the table",),
        )


def test_html_evidence_persists_accession_table_and_snippet_hash(tmp_path: Path) -> None:
    definitions, fallbacks = load_issuer_registry()
    amazon = next(item for item in definitions if item.ticker == "AMZN")
    content = (FIXTURES / "amazon_2025_segment_footnote_excerpt.html").read_bytes()
    source = _html_receipt(content)
    fallback = replace(
        fallbacks[0],
        source_checksum=source.checksum,
        expected_snippet_hash=("efc5c12c99f4337265a4588898311119ad22b2c4fb459db55feecbe12d54f40a"),
    )
    store = AppendOnlyParquetStore(tmp_path)
    ingestor = FilingFactsIngestor(store, (amazon,), (fallback,))
    xbrl = _json_receipt("companyfacts", "amazon_companyfacts_2025_excerpt.json")
    ingestor.ingest_companyfacts(amazon, xbrl)
    first = ingestor.ingest_html_evidence(amazon, fallback, source)
    second = ingestor.ingest_html_evidence(amazon, fallback, source)
    assert first.already_present is False
    assert second.already_present is True
    stored = store.read_table("sec_filing_evidence")
    assert stored.height == 1
    assert stored["accession"][0] == "0001018724-26-000004"
    assert stored["snippet_hash"][0] == first.snippet_hash
    assert "716,924" in stored["extracted_table_json"][0]
    validation = validate_filing_facts(store, (amazon,), (fallback,))
    assert validation.p0_tickers == 1
    assert validation.html_evidence_rows == 1


def test_html_evidence_fails_on_source_or_extraction_drift(tmp_path: Path) -> None:
    definitions, fallbacks = load_issuer_registry()
    amazon = next(item for item in definitions if item.ticker == "AMZN")
    source = _html_receipt((FIXTURES / "amazon_2025_segment_footnote_excerpt.html").read_bytes())
    ingestor = FilingFactsIngestor(AppendOnlyParquetStore(tmp_path), (amazon,), fallbacks)
    with pytest.raises(FilingFactsError, match="checksum changed"):
        ingestor.ingest_html_evidence(amazon, fallbacks[0], source)
    checksum_ok = replace(fallbacks[0], source_checksum=source.checksum)
    with pytest.raises(FilingFactsError, match="snippet changed"):
        ingestor.ingest_html_evidence(amazon, checksum_ok, source)


def test_validator_reports_missing_issuer_evidence(tmp_path: Path) -> None:
    definitions, _ = load_issuer_registry()
    amazon = next(item for item in definitions if item.ticker == "AMZN")
    with pytest.raises(FilingFactsError, match="missing issuers"):
        validate_filing_facts(AppendOnlyParquetStore(tmp_path), (amazon,), ())


def test_fetch_lanes_use_verified_submissions_companyfacts_and_html(tmp_path: Path) -> None:
    definitions, fallbacks = load_issuer_registry()
    amazon = next(item for item in definitions if item.ticker == "AMZN")
    content = (FIXTURES / "amazon_2025_segment_footnote_excerpt.html").read_bytes()
    source = _html_receipt(content)
    fallback = replace(
        fallbacks[0],
        source_checksum=source.checksum,
        expected_snippet_hash=("efc5c12c99f4337265a4588898311119ad22b2c4fb459db55feecbe12d54f40a"),
    )

    class FakeEdgarClient:
        def submissions(self, cik: str) -> EdgarJsonReceipt:
            assert cik == amazon.cik
            return _json_receipt("submissions", "amazon_submissions_2026_excerpt.json")

        def companyfacts(self, cik: str) -> EdgarJsonReceipt:
            assert cik == amazon.cik
            return _json_receipt("companyfacts", "amazon_companyfacts_2025_excerpt.json")

        def archive_document_receipt(
            self, cik: str, accession: str, primary_document: str
        ) -> HttpReceipt:
            assert (cik, accession, primary_document) == (
                amazon.cik,
                amazon.latest_10k.accession,
                amazon.latest_10k.primary_document,
            )
            return source

    store = AppendOnlyParquetStore(tmp_path)
    ingestor = FilingFactsIngestor(store, (amazon,), (fallback,))
    xbrl = ingestor.fetch_all(FakeEdgarClient(), role="p0")  # type: ignore[arg-type]
    html = ingestor.fetch_html_fallbacks(FakeEdgarClient())  # type: ignore[arg-type]
    assert xbrl[0].row_count == 3
    assert html[0].row_count == 22
    with pytest.raises(FilingFactsError, match="No issuer definitions"):
        ingestor.fetch_all(FakeEdgarClient(), role="lender")  # type: ignore[arg-type]


def test_companyfacts_shape_drift_fails_at_explicit_boundaries() -> None:
    definitions, _ = load_issuer_registry()
    amazon = next(item for item in definitions if item.ticker == "AMZN")
    receipt = _json_receipt("companyfacts", "amazon_companyfacts_2025_excerpt.json")

    wrong_cik = {**receipt.payload, "cik": 1}
    with pytest.raises(FilingFactsError, match="CIK changed"):
        companyfacts_rows(amazon, replace(receipt, payload=wrong_cik))
    wrong_name = {**receipt.payload, "entityName": "Wrong"}
    with pytest.raises(FilingFactsError, match="entity name changed"):
        companyfacts_rows(amazon, replace(receipt, payload=wrong_name))
    no_facts = {**receipt.payload, "facts": []}
    with pytest.raises(FilingFactsError, match="facts are missing"):
        companyfacts_rows(amazon, replace(receipt, payload=no_facts))

    malformed = json.loads(json.dumps(receipt.payload))
    metadata = malformed["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]
    metadata["label"] = 7
    with pytest.raises(FilingFactsError, match="label changed"):
        companyfacts_rows(amazon, replace(receipt, payload=malformed))
    metadata["label"] = None
    metadata["description"] = None
    rows = companyfacts_rows(amazon, replace(receipt, payload=malformed))
    assert rows[0]["label"] is None and rows[0]["description"] is None
    metadata["units"] = []
    with pytest.raises(FilingFactsError, match="units changed"):
        companyfacts_rows(amazon, replace(receipt, payload=malformed))


def test_submissions_array_boundaries_fail_closed() -> None:
    definitions, _ = load_issuer_registry()
    amazon = next(item for item in definitions if item.ticker == "AMZN")
    receipt = _json_receipt("submissions", "amazon_submissions_2026_excerpt.json")

    missing = json.loads(json.dumps(receipt.payload))
    del missing["filings"]["recent"]["filingDate"]
    with pytest.raises(FilingFactsError, match="field filingDate"):
        verify_latest_10k(amazon, replace(receipt, payload=missing))
    unequal = json.loads(json.dumps(receipt.payload))
    unequal["filings"]["recent"]["filingDate"].append("2026-01-01")
    with pytest.raises(FilingFactsError, match="unequal lengths"):
        verify_latest_10k(amazon, replace(receipt, payload=unequal))
    no_10k = json.loads(json.dumps(receipt.payload))
    no_10k["filings"]["recent"]["form"] = ["10-Q"]
    with pytest.raises(FilingFactsError, match="No 10-K"):
        verify_latest_10k(amazon, replace(receipt, payload=no_10k))


@pytest.mark.parametrize(
    "filename",
    [
        "amazon_companyfacts_2025.provenance.json",
        "amazon_submissions_2026.provenance.json",
        "amazon_2025_segment_footnote.provenance.json",
    ],
)
def test_filing_fixture_provenance_hashes_are_current(filename: str) -> None:
    provenance = json.loads((FIXTURES / filename).read_text(encoding="utf-8"))
    fixture_name = {
        "amazon_companyfacts_2025.provenance.json": "amazon_companyfacts_2025_excerpt.json",
        "amazon_submissions_2026.provenance.json": "amazon_submissions_2026_excerpt.json",
        "amazon_2025_segment_footnote.provenance.json": (
            "amazon_2025_segment_footnote_excerpt.html"
        ),
    }[filename]
    assert (
        hashlib.sha256((FIXTURES / fixture_name).read_bytes()).hexdigest()
        == provenance["fixture_sha256"]
    )
