"""Fail-closed static quality and performance checks for a built DFRI site."""

from __future__ import annotations

import argparse
import json
import math
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

MAX_PAGE_BYTES: Final = 500_000
FOUR_G_BYTES_PER_SECOND: Final = 200_000
FOUR_G_RTT_MS: Final = 150.0
MAX_ESTIMATED_4G_MS: Final = 1_000.0


class SiteQualityError(RuntimeError):
    """A published page violates a blocking M4 quality contract."""


@dataclass(frozen=True)
class SiteQualityReceipt:
    status: str
    page_count: int
    company_page_count: int
    max_page_path: str
    max_page_bytes: int
    max_estimated_4g_ms: float
    minimum_contrast_ratio: float


def check_site(root: Path) -> SiteQualityReceipt:
    required = (
        root / "index.html",
        root / "scoreboard" / "index.html",
        root / "methodology" / "index.html",
        root / "changelog" / "index.html",
        root / "v1" / "feeds" / "schema.json",
    )
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    if missing:
        raise SiteQualityError(f"Missing required publication page: {missing[0]}")
    html_files = sorted(root.rglob("*.html"))
    company_files = sorted((root / "companies").glob("*/index.html"))
    if len(company_files) != 10:
        raise SiteQualityError(f"Expected ten company pages, found {len(company_files)}")
    assets = sum(path.stat().st_size for path in (root / "assets").glob("*") if path.is_file())
    page_sizes: dict[str, int] = {}
    estimated: dict[str, float] = {}
    for path in html_files:
        relative = path.relative_to(root).as_posix()
        content = path.read_text(encoding="utf-8")
        _check_document(relative, content)
        page_bytes = path.stat().st_size + assets
        page_sizes[relative] = page_bytes
        estimated[relative] = FOUR_G_RTT_MS + page_bytes / FOUR_G_BYTES_PER_SECOND * 1_000
        if page_bytes > MAX_PAGE_BYTES:
            raise SiteQualityError(f"Page exceeds 500 KB: {relative} ({page_bytes})")
        if estimated[relative] >= MAX_ESTIMATED_4G_MS:
            raise SiteQualityError(
                "Estimated 4G load is not below one second: "
                f"{relative} ({estimated[relative]:.1f}ms)"
            )
    for path in company_files:
        _check_company(path.relative_to(root).as_posix(), path.read_text(encoding="utf-8"))
    home = (root / "index.html").read_text(encoding="utf-8")
    if "revenue-weighted company index" not in home or "Estimated DFR%" not in home:
        raise SiteQualityError("Homepage lacks the estimated revenue-weighted index contract")
    methodology = (root / "methodology" / "index.html").read_text(encoding="utf-8")
    if "Assumption Registry" not in methodology or "Tier 1 — Observed" not in methodology:
        raise SiteQualityError("Methodology lacks the versioned assumption/tier contract")
    javascript = (root / "assets" / "site.js").read_text(encoding="utf-8")
    forbidden = ("document.write", "google-analytics", "gtag(", "localStorage", "cookie")
    if any(item in javascript for item in forbidden):
        raise SiteQualityError("Site JavaScript contains a tracking, persistence, or content gate")
    minimum_contrast = _check_contrast((root / "assets" / "site.css").read_text())
    max_path = max(page_sizes, key=page_sizes.__getitem__)
    return SiteQualityReceipt(
        status="PASS",
        page_count=len(html_files),
        company_page_count=len(company_files),
        max_page_path=max_path,
        max_page_bytes=page_sizes[max_path],
        max_estimated_4g_ms=max(estimated.values()),
        minimum_contrast_ratio=minimum_contrast,
    )


def write_receipt(path: Path, receipt: SiteQualityReceipt) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(receipt), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _check_document(relative: str, content: str) -> None:
    required = (
        '<html lang="en">',
        '<meta name="viewport"',
        "<main>",
        "<h1",
        'aria-label="Primary navigation"',
        "Research and educational content. Not investment advice.",
    )
    missing = [item for item in required if item not in content]
    if missing:
        raise SiteQualityError(f"{relative} lacks required accessible markup: {missing[0]}")
    if re.search(r'<script[^>]+src="https?://', content):
        raise SiteQualityError(f"{relative} loads remote JavaScript")
    for svg in re.findall(r"<svg\b.*?</svg>", content, flags=re.DOTALL):
        if 'role="img"' not in svg or "<title>" not in svg:
            raise SiteQualityError(f"{relative} has an unlabelled SVG chart")
    visible = re.sub(r"<[^>]+>", " ", content)
    if len(" ".join(visible.split())) < 100:
        raise SiteQualityError(f"{relative} lacks server-rendered no-JavaScript content")


def _check_company(relative: str, content: str) -> None:
    required = (
        "Estimated DFR% band",
        "Tier 1",
        "Tier 2",
        "Tier 3",
        "Assumption IDs",
        "Assumption sensitivity top 5",
        "https://www.sec.gov/Archives/",
        "<svg",
    )
    missing = [item for item in required if item not in content]
    if missing:
        raise SiteQualityError(f"{relative} lacks company evidence contract: {missing[0]}")


def _check_contrast(css: str) -> float:
    variables = dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6})", css))
    pairs = (
        ("ink", "paper"),
        ("muted", "paper"),
        ("blue", "paper"),
        ("ink", "surface"),
        ("muted", "surface"),
    )
    try:
        ratios = [
            _contrast(variables[foreground], variables[background])
            for foreground, background in pairs
        ]
    except KeyError as exc:
        raise SiteQualityError(f"Missing checked color token: {exc.args[0]}") from exc
    minimum = min(ratios)
    if minimum < 4.5:
        raise SiteQualityError(f"Text contrast below WCAG AA: {minimum:.2f}:1")
    return minimum


def _contrast(first: str, second: str) -> float:
    bright = max(_luminance(first), _luminance(second))
    dark = min(_luminance(first), _luminance(second))
    return (bright + 0.05) / (dark + 0.05)


def _luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        value / 12.92 if value <= 0.04045 else math.pow((value + 0.055) / 1.055, 2.4)
        for value in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("published/public"))
    parser.add_argument("--output", type=Path, default=Path(".local/evidence/m4-site-quality.json"))
    args = parser.parse_args(argv)
    receipt = check_site(args.root)
    write_receipt(args.output, receipt)
    print(json.dumps(asdict(receipt), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
