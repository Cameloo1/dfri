"""Check every external M3 attribution provenance link against its live source."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import httpx

from dfri.attribution.registry import AttributionBundle, load_attribution_bundle

USER_AGENT: Final = "Camelon-DFRI/1.0 research provenance check ops@camelon.app"


@dataclass(frozen=True)
class LinkReceipt:
    url: str
    status: str
    http_status: int | None
    error: str | None


def attribution_links(bundle: AttributionBundle) -> tuple[str, ...]:
    links: set[str] = set()
    links.update(item.source_url for item in bundle.assumptions)
    for company_item in bundle.companies:
        links.add(company_item.revenue_source_url)
        links.add(company_item.tier1_source_url)
        if company_item.membership_snapshot_ref.startswith("https://"):
            links.add(company_item.membership_snapshot_ref)
    for flow_item in bundle.flows:
        links.update(_urls(flow_item.evidence_refs))
    for matrix_b_item in bundle.matrix_b:
        links.update(_urls(matrix_b_item.evidence_refs))
    return tuple(sorted(links))


def check_links(urls: Iterable[str], client: httpx.Client) -> tuple[LinkReceipt, ...]:
    receipts: list[LinkReceipt] = []
    for url in sorted(set(urls)):
        try:
            response = client.get(url, headers={"User-Agent": USER_AGENT})
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            receipts.append(
                LinkReceipt(
                    url=url,
                    status="FAIL",
                    http_status=exc.response.status_code,
                    error=f"HTTP {exc.response.status_code}",
                )
            )
        except httpx.HTTPError as exc:
            receipts.append(
                LinkReceipt(url=url, status="FAIL", http_status=None, error=type(exc).__name__)
            )
        else:
            receipts.append(
                LinkReceipt(url=url, status="PASS", http_status=response.status_code, error=None)
            )
    return tuple(receipts)


def write_receipt(path: Path, receipts: tuple[LinkReceipt, ...]) -> str:
    status = "PASS" if receipts and all(item.status == "PASS" for item in receipts) else "FAIL"
    payload = {
        "status": status,
        "checked_at": datetime.now(UTC).isoformat(),
        "link_count": len(receipts),
        "links": [asdict(item) for item in receipts],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return status


def _urls(values: Iterable[str]) -> set[str]:
    return {value for value in values if value.startswith("https://")}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path(".local/evidence/m3-provenance-links.json")
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with httpx.Client(follow_redirects=True, timeout=args.timeout) as client:
        receipts = check_links(attribution_links(load_attribution_bundle()), client)
    status = write_receipt(args.output, receipts)
    print(json.dumps({"status": status, "output": str(args.output), "links": len(receipts)}))
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
