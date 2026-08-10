"""Fail-closed static quality and performance checks for a built DFRI site."""

from __future__ import annotations

import argparse
import json
import math
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Final

from PIL import Image

MAX_PAGE_BYTES: Final = 500_000
FOUR_G_BYTES_PER_SECOND: Final = 200_000
FOUR_G_RTT_MS: Final = 150.0
MAX_ESTIMATED_4G_MS: Final = 1_000.0
UNIT_PATTERNS: Final = {
    "percent": re.compile(r"%|\bpercent\b", re.IGNORECASE),
    "millions-usd": re.compile(r"\$M|\bmillions? of U\.S\. dollars\b|\bmillion\b"),
    "records": re.compile(r"\brecords?\b", re.IGNORECASE),
    "ratio": re.compile(r"\bmultiple\b|\blift\b|\d+(?:\.\d+)?x\b", re.IGNORECASE),
}


@dataclass(frozen=True)
class _FigureRule:
    unit: str
    band: str = "not-applicable"
    tiered: bool = False
    provenance: bool = False
    estimated: bool = False


FIGURE_CONTRACTS: Final = {
    "dfr": _FigureRule(
        unit="percent", band="required", tiered=True, provenance=True, estimated=True
    ),
    "prediction": _FigureRule(unit="millions-usd", band="required", provenance=True),
    "count": _FigureRule(unit="records", provenance=True),
    "diagnostic-usd": _FigureRule(unit="millions-usd", provenance=True),
    "diagnostic-percent": _FigureRule(unit="percent", provenance=True),
    "tier-share": _FigureRule(unit="percent", tiered=True, provenance=True),
    "derived-ratio": _FigureRule(unit="ratio", provenance=True),
    "allocation": _FigureRule(unit="millions-usd", tiered=True, provenance=True),
    "calibration-count": _FigureRule(unit="records"),
    "calibration-percent": _FigureRule(unit="percent"),
    "calibration-usd": _FigureRule(unit="millions-usd"),
}
VOID_HTML_TAGS: Final = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


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


@dataclass
class _RenderedFigure:
    attributes: dict[str, str | None]
    text: list[str] = field(default_factory=list)
    classes: set[str] = field(default_factory=set)
    link_count: int = 0
    number_count: int = 0
    has_range_band: bool = False


class _FigureContractParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.figures: list[_RenderedFigure] = []
        self.active: list[_RenderedFigure] = []
        self.stack: list[tuple[str, bool, bool]] = []
        self.number_outside_figure = 0
        self.measured_outside_tier1 = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, push=tag not in VOID_HTML_TAGS)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, push=False)

    def _start(self, tag: str, attrs: list[tuple[str, str | None]], *, push: bool) -> None:
        attributes = dict(attrs)
        starts_figure = "data-figure" in attributes
        if starts_figure:
            new_figure = _RenderedFigure(attributes=attributes)
            self.figures.append(new_figure)
            self.active.append(new_figure)
        figure: _RenderedFigure | None = self.active[-1] if self.active else None
        classes = set(str(attributes.get("class") or "").split())
        if "number" in classes:
            if figure is None:
                self.number_outside_figure += 1
            else:
                figure.number_count += 1
        if figure is not None:
            figure.classes.update(classes)
            figure.has_range_band = figure.has_range_band or "range-band" in classes
            if tag == "a":
                figure.link_count += 1
        tier1_context = (self.stack[-1][2] if self.stack else False) or (
            attributes.get("data-evidence-tier") == "1"
        )
        if push:
            self.stack.append((tag, starts_figure, tier1_context))

    def handle_endtag(self, tag: str) -> None:
        if not self.stack:
            return
        while self.stack:
            opened_tag, started_figure, _tier1 = self.stack.pop()
            if started_figure and self.active:
                self.active.pop()
            if opened_tag == tag:
                return

    def handle_data(self, data: str) -> None:
        if self.active:
            self.active[-1].text.append(data)
        if re.search(r"\bmeasured\b", data, flags=re.IGNORECASE):
            tier1_context = self.stack[-1][2] if self.stack else False
            if not tier1_context:
                self.measured_outside_tier1 += 1


def check_site(root: Path) -> SiteQualityReceipt:
    required = (
        root / "index.html",
        root / "scoreboard" / "index.html",
        root / "companies" / "index.html",
        root / "methodology" / "index.html",
        root / "methodology" / "sensitivity" / "index.html",
        root / "methodology" / "coverage" / "index.html",
        root / "changelog" / "index.html",
        root / "v1" / "feeds" / "schema.json",
        root / "v2" / "feeds" / "schema.json",
        root / "v1" / "status.json",
        root / "v1" / "events.json",
        root / "events.xml",
        root / "status" / "banner.html",
    )
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    if missing:
        raise SiteQualityError(f"Missing required publication page: {missing[0]}")
    status_banner = root / "status" / "banner.html"
    html_files = sorted(path for path in root.rglob("*.html") if path != status_banner)
    company_files = sorted((root / "companies").glob("*/index.html"))
    company_feed = json.loads(
        (root / "v1" / "feeds" / "dfri_companies.json").read_text(encoding="utf-8")
    )
    expected_company_count = company_feed.get("meta", {}).get("row_count")
    if expected_company_count != 50 or len(company_files) != expected_company_count:
        raise SiteQualityError(
            f"Expected 50 company pages from the feed contract, found {len(company_files)}"
        )
    assets = sum(path.stat().st_size for path in (root / "assets").glob("*") if path.is_file())
    assets += status_banner.stat().st_size
    page_sizes: dict[str, int] = {}
    estimated: dict[str, float] = {}
    for path in html_files:
        relative = path.relative_to(root).as_posix()
        content = path.read_text(encoding="utf-8")
        _check_document(relative, content)
        _check_social_metadata(root, relative, content)
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
    if (
        "Revenue-weighted DFR%" not in home
        or "estimated share of U.S. consumer revenue" not in home
        or 'id="evidence-lift"' not in home
        or "No company-specific financing evidence found" not in home
        or home.count('data-lift-status="evidence-supported"') != 13
        or home.count('data-lift-status="baseline-only"') != 37
        or '<details class="baseline-disclosure">' not in home
        or 'href="companies/index.html"' not in home
    ):
        raise SiteQualityError("Homepage lacks the estimated revenue-weighted index contract")
    directory = (root / "companies" / "index.html").read_text(encoding="utf-8")
    if (
        directory.count("data-company-directory-entry") != expected_company_count
        or "covered companies, alphabetically by ticker" not in directory
    ):
        raise SiteQualityError(
            "Company directory lacks the complete alphabetical coverage contract"
        )
    methodology = (root / "methodology" / "index.html").read_text(encoding="utf-8")
    if (
        "Assumption Registry" not in methodology
        or "<h2>Tier 1</h2><p>Observed:" not in methodology
        or "Evidence Lift" not in methodology
        or 'id="credit-flow"' not in methodology
    ):
        raise SiteQualityError("Methodology lacks the versioned assumption/tier contract")
    _check_credit_flow("index.html", home, require_table=False)
    _check_credit_flow("methodology/index.html", methodology, require_table=True)
    comparison = (root / "methodology" / "sensitivity" / "index.html").read_text(encoding="utf-8")
    required_methodologies = ("Methodology 1.1.0", "Methodology 1.2.0", "Methodology 1.2.1")
    if any(item not in comparison for item in required_methodologies):
        raise SiteQualityError("Methodology sensitivity page lacks an immutable version")
    exclusions = (root / "methodology" / "coverage" / "index.html").read_text(encoding="utf-8")
    if "31 excluded" not in exclusions or "one-line reason" not in exclusions:
        raise SiteQualityError("Coverage page lacks the dated exclusion contract")
    javascript = (root / "assets" / "site.js").read_text(encoding="utf-8")
    forbidden = ("document.write", "google-analytics", "gtag(", "localStorage", "cookie")
    if any(item in javascript for item in forbidden):
        raise SiteQualityError("Site JavaScript contains a tracking, persistence, or content gate")
    css = (root / "assets" / "site.css").read_text()
    status_payload = json.loads((root / "v1" / "status.json").read_text(encoding="utf-8"))
    if status_payload.get("schema_version") != "v1" or len(status_payload.get("jobs", [])) != 5:
        raise SiteQualityError("Machine-readable job status lacks all five scheduled lanes")
    event_payload = json.loads((root / "v1" / "events.json").read_text(encoding="utf-8"))
    if event_payload.get("schema_version") != "v1" or not event_payload.get("data"):
        raise SiteQualityError("Versioned event feed is missing or empty")
    if "Automation " not in status_banner.read_text(encoding="utf-8"):
        raise SiteQualityError("Visible no-JavaScript automation status is missing")
    _check_editorial_contract(css)
    minimum_contrast = _check_contrast(css)
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
        '<a class="skip-link" href="#main-content">Skip to main content</a>',
        '<main id="main-content" tabindex="-1">',
        "<h1",
        'aria-label="Primary navigation"',
        "Research and educational content. Not investment advice.",
        "CC BY-NC 4.0",
        "commercial licensing reserved",
        'href="mailto:ops@camelon.app"',
        '<link rel="canonical" href="https://',
        '<meta property="og:image" content="https://',
        '<meta name="twitter:card" content="summary_large_image">',
        '<link rel="alternate" type="application/rss+xml"',
        'title="DFRI scheduled automation status"',
    )
    missing = [item for item in required if item not in content]
    if missing:
        raise SiteQualityError(f"{relative} lacks required accessible markup: {missing[0]}")
    main_before_heading = content.split('<main id="main-content" tabindex="-1">', 1)[1].split(
        "<h1", 1
    )[0]
    if 'class="eyebrow"' in main_before_heading or 'class="kicker"' in main_before_heading:
        raise SiteQualityError(f"{relative} places a tagline or kicker above its H1")
    if re.search(r'<script[^>]+src="https?://', content):
        raise SiteQualityError(f"{relative} loads remote JavaScript")
    for svg in re.findall(r"<svg\b.*?</svg>", content, flags=re.DOTALL):
        if 'role="img"' not in svg or "<title>" not in svg:
            raise SiteQualityError(f"{relative} has an unlabelled SVG chart")
        if ("range-chart" in svg or "history-chart" in svg) and (
            "<rect " not in svg or 'class="range-band"' not in svg
        ):
            raise SiteQualityError(f"{relative} renders a band without a visible range")
    visible = re.sub(r"<[^>]+>", " ", content)
    if len(" ".join(visible.split())) < 100:
        raise SiteQualityError(f"{relative} lacks server-rendered no-JavaScript content")
    _check_figure_contracts(relative, content)
    _check_navigation_semantics(relative, content)


def _check_social_metadata(root: Path, relative: str, content: str) -> None:
    match = re.search(r'<meta property="og:image" content="([^"]+)">', content)
    if match is None:
        raise SiteQualityError(f"{relative} lacks an Open Graph preview image")
    marker = "/dfri/"
    if marker not in match.group(1):
        raise SiteQualityError(f"{relative} social image is outside the canonical site")
    image_path = root / Path(match.group(1).split(marker, 1)[1])
    if not image_path.is_file():
        raise SiteQualityError(f"{relative} social image is missing: {image_path.name}")
    with Image.open(image_path) as image:
        if image.size != (1200, 630) or image.format != "PNG":
            raise SiteQualityError(f"{relative} social image must be a 1200x630 PNG")


def _check_navigation_semantics(relative: str, content: str) -> None:
    navigation_match = re.search(
        r'<nav aria-label="Primary navigation">(.*?)</nav>', content, flags=re.DOTALL
    )
    if navigation_match is None:
        raise SiteQualityError(f"{relative} lacks primary navigation")
    navigation = navigation_match.group(1)
    if "companies/index.html" not in navigation:
        raise SiteQualityError(f"{relative} does not route Companies to the directory page")
    expected: str | None = None
    if relative == "scoreboard/index.html":
        expected = "Scoreboard"
    elif relative == "companies/index.html" or (
        relative.startswith("companies/") and relative.count("/") == 2
    ):
        expected = "Companies"
    elif relative.startswith("methodology/"):
        expected = "Methodology"
    elif relative == "changelog/index.html":
        expected = "Changelog"
    current = re.findall(r'<a [^>]*aria-current="page"[^>]*>([^<]+)</a>', navigation)
    if expected is None and current:
        raise SiteQualityError(f"{relative} exposes a false current-page navigation state")
    if expected is not None and current != [expected]:
        raise SiteQualityError(
            f"{relative} must expose exactly one current-page navigation state for {expected}"
        )


def _check_figure_contracts(relative: str, content: str) -> None:
    parser = _FigureContractParser()
    parser.feed(content)
    if parser.number_outside_figure:
        raise SiteQualityError(f"{relative} renders a headline number outside a figure contract")
    if parser.measured_outside_tier1:
        raise SiteQualityError(f"{relative} uses measured outside an explicit Tier 1 context")
    for index, figure in enumerate(parser.figures, start=1):
        kind = figure.attributes.get("data-figure") or ""
        rule = FIGURE_CONTRACTS.get(kind)
        label = f"{relative} figure {index}"
        if rule is None:
            raise SiteQualityError(f"{label} lacks a known figure contract")
        visible = " ".join(" ".join(figure.text).split())
        if not UNIT_PATTERNS[rule.unit].search(visible):
            raise SiteQualityError(f"{label} does not render its declared {rule.unit} unit")
        if rule.band == "required" and not (
            figure.has_range_band or re.search(r"\b(?:80|95)%? band\b", visible)
        ):
            raise SiteQualityError(f"{label} renders a modeled point without its band")
        if rule.tiered and "tier-badge" not in figure.classes:
            raise SiteQualityError(f"{label} carries tiers without a tier badge")
        if rule.provenance and figure.link_count == 0:
            raise SiteQualityError(f"{label} lacks a provenance link")
        if rule.estimated and not re.search(r"\bestimated\b", visible, flags=re.IGNORECASE):
            raise SiteQualityError(f"{label} renders DFR% without estimated")


def _check_company(relative: str, content: str) -> None:
    required = (
        "estimated DFR% of U.S. consumer revenue",
        "Evidence Lift",
        "Tier 1",
        "Tier 2",
        "Tier 3",
        "<h2>Assumptions</h2>",
        "<h2>Sensitivity</h2>",
        "<h2>History</h2>",
        'class="figure-evidence"',
        "history-chart",
        "https://www.sec.gov/Archives/",
        "<svg",
    )
    missing = [item for item in required if item not in content]
    if missing:
        raise SiteQualityError(f"{relative} lacks company evidence contract: {missing[0]}")


def _check_credit_flow(relative: str, content: str, *, require_table: bool) -> None:
    required = (
        'class="credit-flow"',
        "Width = estimated dollars",
        "Style = how much is actually known",
        'class="flow-ribbon tier-1"',
        'class="flow-ribbon tier-2"',
        'class="flow-ribbon tier-3"',
        "Tier 3 flows are proportional allocations, not observed transfers.",
    )
    missing = [item for item in required if item not in content]
    if missing:
        raise SiteQualityError(f"{relative} lacks the tier-encoded flow contract: {missing[0]}")
    match = re.search(r'data-node-count="(\d+)"', content)
    if match is None or int(match.group(1)) > 9:
        raise SiteQualityError(f"{relative} exceeds the 9-node flow readability cap")
    if require_table and "Exact static values behind the diagram." not in content:
        raise SiteQualityError(f"{relative} lacks the static flow-value ledger")


def _check_contrast(css: str) -> float:
    variables = dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6})", css))
    pairs = (
        ("ink", "paper"),
        ("muted", "paper"),
        ("verified", "paper"),
        ("ink", "raised-paper"),
        ("muted", "raised-paper"),
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


def _check_editorial_contract(css: str) -> None:
    required = (
        "--display:",
        "--mono:",
        "--verified:",
        "font-variant-numeric: tabular-nums lining-nums",
        ".section-block::before",
        ".range-mid-rule",
        ".status.graded",
        "--figure-large:",
        ".figure-label",
        ".flow-ribbon.tier-1",
        ".flow-ribbon.tier-2",
        ".flow-ribbon.tier-3",
    )
    missing = [item for item in required if item not in css]
    if missing:
        raise SiteQualityError(f"Editorial ledger CSS lacks required contract: {missing[0]}")
    forbidden = ("@font-face", "box-shadow:", "filter: drop-shadow", "url(http")
    used = [item for item in forbidden if item in css]
    if used:
        raise SiteQualityError(f"Editorial ledger CSS contains forbidden treatment: {used[0]}")
    figure_label = re.search(r"\.figure-label\s*\{([^}]*)\}", css, flags=re.DOTALL)
    if figure_label is None or "text-transform" in figure_label.group(1):
        raise SiteQualityError("Figure labels must exist and remain uppercase-free")
    verified_selectors = [
        selector.strip()
        for selector, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        if "var(--verified)" in declarations
    ]
    if verified_selectors != [".status.graded"]:
        raise SiteQualityError("Verified accent must be exclusive to the graded state")
    tier_contracts = {
        ".tier-1": ("border:",),
        ".tier-2": ("background-image:",),
        ".tier-3": ("background-image:", "background-size:"),
        ".flow-ribbon.tier-2": ("stroke-dasharray:",),
        ".flow-ribbon.tier-3": ("stroke-dasharray:", "stroke-linecap: round"),
        ".flow-key.tier-2": ("border-top-style: dashed",),
        ".flow-key.tier-3": ("border-top-style: dotted",),
    }
    for selector, declarations in tier_contracts.items():
        block = re.search(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", css, flags=re.DOTALL)
        if block is None or any(item not in block.group(1) for item in declarations):
            raise SiteQualityError(
                f"Tier distinction depends on color or lacks texture: {selector}"
            )


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
