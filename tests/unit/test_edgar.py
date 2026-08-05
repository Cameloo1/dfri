from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from dfri.ingest.edgar import EdgarClient, EdgarContractError, normalize_cik
from dfri.ingest.http import HttpFileReceipt, HttpReceipt


class FakeTransport:
    def get(self, url: str, **_kwargs: object) -> HttpReceipt:
        fixture_name = (
            "apple_companyfacts_sample.json"
            if "companyfacts" in url
            else "apple_submissions_sample.json"
        )
        content = (Path(__file__).parents[1] / "fixtures" / "sec" / fixture_name).read_bytes()
        if "search-index" in url:
            content = b'{"hits":{"total":{"value":0},"hits":[]}}'
        if url.endswith("index.json"):
            content = b'{"directory":{"name":"edgar/data/320193/000032019325000079","item":[]}}'
        if "/Archives/" in url and not url.endswith("index.json"):
            content = b"<html>filing</html>"
        return HttpReceipt(
            content=content,
            source_url=url,
            checksum="d" * 64,
            retrieved_at=datetime(2026, 8, 4, tzinfo=UTC),
            status_code=200,
        )

    def get_to_gzip(self, url: str, destination: Path) -> HttpFileReceipt:
        destination.write_bytes(b"gzip")
        return HttpFileReceipt(
            path=destination,
            source_url=url,
            checksum="e" * 64,
            compressed_checksum="f" * 64,
            retrieved_at=datetime(2026, 8, 4, tzinfo=UTC),
            status_code=200,
            byte_count=10,
            compressed_byte_count=4,
        )


def test_edgar_clients_and_urls_are_pinned(tmp_path: Path) -> None:
    client = EdgarClient(FakeTransport())  # type: ignore[arg-type]
    assert client.submissions("320193").payload["name"] == "Apple Inc."
    assert client.companyfacts("0000320193").payload["entityName"] == "Apple Inc."
    assert "directory" in client.archive_index("320193", "0000320193-25-000079").payload
    assert (
        client.efts_search(
            query="auto loan", forms="ABS-EE", start_date="2026-01-01", end_date="2026-08-04"
        ).kind
        == "efts"
    )
    document = client.archive_document("320193", "0000320193-25-000079", "aapl-20250927.htm")
    assert document.startswith(b"<html>")
    receipt = client.archive_document_receipt("320193", "0000320193-25-000079", "aapl-20250927.htm")
    assert receipt.checksum == "d" * 64
    streamed = client.archive_document_to_gzip(
        "320193", "0000320193-25-000079", "asset.xml", tmp_path / "asset.xml.gz"
    )
    assert streamed.checksum == "e" * 64


def test_edgar_identifier_boundaries_fail_closed() -> None:
    assert normalize_cik("320193") == "0000320193"
    with pytest.raises(EdgarContractError, match="CIK"):
        normalize_cik("bad")
    client = EdgarClient(FakeTransport())  # type: ignore[arg-type]
    with pytest.raises(EdgarContractError, match="accession"):
        client.archive_document("320193", "bad", "document.htm")
    with pytest.raises(EdgarContractError, match="accession"):
        client.archive_index("320193", "bad")
    with pytest.raises(EdgarContractError, match="document"):
        client.archive_document("320193", "0000320193-25-000079", "../bad")
    with pytest.raises(EdgarContractError, match="result size"):
        client.efts_search(query="", forms="10-K", start_date="2026-01-01", end_date="2026-01-02")
