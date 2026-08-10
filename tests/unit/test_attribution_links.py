from __future__ import annotations

import json
from pathlib import Path

import httpx

from dfri.attribution.registry import load_attribution_bundle
from dfri.publish.link_check import attribution_links, check_links, write_receipt


def test_attribution_link_inventory_is_https_complete_and_unique() -> None:
    bundle = load_attribution_bundle()
    links = attribution_links(bundle)

    assert len(links) == len(set(links))
    assert len(links) >= 15
    assert all(item.startswith("https://") for item in links)
    assert {item.revenue_source_url for item in bundle.companies} <= set(links)
    assert {item.tier1_source_url for item in bundle.companies if item.tier1_source_url} <= set(
        links
    )
    review = json.loads(
        (
            Path(__file__).parents[2]
            / "src"
            / "dfri"
            / "attribution"
            / "tier1_evidence_review_v1.json"
        ).read_text(encoding="utf-8")
    )
    review_links = {url for item in review["items"] for url in item["evidence_urls"]}
    assert review_links <= set(links)


def test_link_checker_records_green_http_sources(tmp_path: Path) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    receipts = check_links(
        ("https://example.test/b", "https://example.test/a"),
        client,
        min_interval_seconds=0,
    )
    output = tmp_path / "receipt.json"

    assert [item.url for item in receipts] == ["https://example.test/a", "https://example.test/b"]
    assert write_receipt(output, receipts) == "PASS"
    assert json.loads(output.read_text())["link_count"] == 2


def test_link_checker_fails_closed_on_http_error(tmp_path: Path) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    receipts = check_links(("https://example.test/missing",), client, min_interval_seconds=0)
    output = tmp_path / "receipt.json"

    assert receipts[0].status == "FAIL"
    assert receipts[0].http_status == 404
    assert write_receipt(output, receipts) == "FAIL"
