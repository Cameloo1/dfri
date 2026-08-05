from __future__ import annotations

import gzip
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from dfri.ingest.http import HttpTransport, SourceRequestError, safe_source_url


def test_safe_source_url_removes_credentials() -> None:
    safe = safe_source_url(
        "https://example.test/data",
        {"series": "A", "api_key": "secret", "UserID": "also-secret"},
    )
    assert safe == "https://example.test/data?series=A"
    assert "secret" not in safe


def test_transport_retries_and_returns_checksum() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(200, content=b"board-data", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = HttpTransport(
        client=client,
        sleep=sleeps.append,
        now=lambda: datetime(2026, 8, 4, tzinfo=UTC),
    )
    receipt = transport.get("https://example.test/data")

    assert calls == 2
    assert sleeps == [1.0]
    assert receipt.status_code == 200
    assert len(receipt.checksum) == 64
    assert receipt.source_url == "https://example.test/data"


def test_transport_failure_does_not_expose_secret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=request)

    transport = HttpTransport(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(SourceRequestError) as error:
        transport.get("https://example.test/data", params={"api_key": "never-print"})
    assert "never-print" not in str(error.value)


def test_transport_rejects_zero_attempts() -> None:
    with pytest.raises(ValueError, match="at least one"):
        HttpTransport(max_attempts=0)


def test_transport_paces_requests_and_closes_owned_client() -> None:
    ticks = iter([1.0, 1.2, 2.0])
    sleeps: list[float] = []
    responses = httpx.MockTransport(lambda request: httpx.Response(200, request=request))
    transport = HttpTransport(
        client=httpx.Client(transport=responses),
        min_interval_seconds=1.0,
        monotonic=lambda: next(ticks),
        sleep=sleeps.append,
    )
    transport.get("https://example.test/one")
    transport.get("https://example.test/two")
    assert sleeps == [pytest.approx(0.8)]


def test_transport_wraps_network_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    sleeps: list[float] = []
    transport = HttpTransport(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_attempts=2,
        sleep=sleeps.append,
    )
    with pytest.raises(SourceRequestError, match="after retries"):
        transport.get("https://example.test/data")
    assert sleeps == [1.0]


def test_transport_streams_deterministic_gzip_without_overwrite(tmp_path: Path) -> None:
    body = b"large-source-body" * 1000
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=body, request=request)
        )
    )
    transport = HttpTransport(
        client=client,
        now=lambda: datetime(2026, 8, 4, tzinfo=UTC),
    )
    destination = tmp_path / "source.xml.gz"
    receipt = transport.get_to_gzip("https://example.test/source.xml", destination)
    assert gzip.decompress(destination.read_bytes()) == body
    assert receipt.checksum == hashlib.sha256(body).hexdigest()
    assert receipt.byte_count == len(body)
    assert receipt.compressed_byte_count == destination.stat().st_size
    assert receipt.compressed_checksum == hashlib.sha256(destination.read_bytes()).hexdigest()
    with pytest.raises(SourceRequestError, match="overwrite"):
        transport.get_to_gzip("https://example.test/source.xml", destination)
