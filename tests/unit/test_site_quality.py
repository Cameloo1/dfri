from __future__ import annotations

from pathlib import Path

import pytest

from dfri.publish.quality import SiteQualityError, check_site, write_receipt

from .test_api_app import build_publication


def test_built_site_passes_static_quality_weight_contrast_and_no_js_gates(
    tmp_path: Path,
) -> None:
    root = build_publication(tmp_path)

    receipt = check_site(root)

    assert receipt.status == "PASS"
    assert receipt.company_page_count == 50
    assert receipt.max_page_bytes < 500_000
    assert receipt.max_estimated_4g_ms < 1_000
    assert receipt.minimum_contrast_ratio >= 4.5
    output = write_receipt(tmp_path / "quality.json", receipt)
    assert output.exists()


def test_quality_gate_rejects_unlabelled_svg(tmp_path: Path) -> None:
    root = build_publication(tmp_path)
    page = root / "companies" / "gm" / "index.html"
    content = page.read_text(encoding="utf-8").replace(
        "<title>Estimated DFR%", "<desc>Estimated DFR%", 1
    )
    page.write_text(content, encoding="utf-8")

    with pytest.raises(SiteQualityError, match="unlabelled SVG"):
        check_site(root)


def test_quality_gate_rejects_missing_required_page(tmp_path: Path) -> None:
    root = build_publication(tmp_path)
    (root / "changelog" / "index.html").unlink()

    with pytest.raises(SiteQualityError, match="Missing required"):
        check_site(root)


def test_quality_gate_rejects_chart_without_associated_text_equivalent(tmp_path: Path) -> None:
    root = build_publication(tmp_path)
    page = root / "companies" / "gm" / "index.html"
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            'data-chart-equivalent="company-dfr-band-data"',
            'data-chart-equivalent="missing-band-data"',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(SiteQualityError, match="does not describe its text-equivalent table"):
        check_site(root)


def test_quality_gate_rejects_flow_table_without_required_edge_field(tmp_path: Path) -> None:
    root = build_publication(tmp_path)
    page = root / "index.html"
    page.write_text(
        page.read_text(encoding="utf-8").replace("Destination", "Receiving node", 1),
        encoding="utf-8",
    )

    with pytest.raises(SiteQualityError, match="required text-equivalent data: Destination"):
        check_site(root)


def test_quality_gate_requires_complete_company_directory(tmp_path: Path) -> None:
    root = build_publication(tmp_path)
    page = root / "companies" / "index.html"
    page.write_text(
        page.read_text(encoding="utf-8").replace("data-company-directory-entry", "", 1),
        encoding="utf-8",
    )

    with pytest.raises(SiteQualityError, match="complete alphabetical coverage"):
        check_site(root)


def test_quality_gate_requires_skip_target_and_current_page_semantics(tmp_path: Path) -> None:
    root = build_publication(tmp_path)
    scoreboard = root / "scoreboard" / "index.html"
    scoreboard.write_text(
        scoreboard.read_text(encoding="utf-8").replace(' aria-current="page"', "", 1),
        encoding="utf-8",
    )

    with pytest.raises(SiteQualityError, match="current-page navigation state"):
        check_site(root)

    build_publication(tmp_path)
    home = root / "index.html"
    home.write_text(
        home.read_text(encoding="utf-8").replace('id="main-content"', 'id="missing-main"', 1),
        encoding="utf-8",
    )
    with pytest.raises(SiteQualityError, match="main-content"):
        check_site(root)


def test_quality_gate_requires_full_evidence_lift_partition(tmp_path: Path) -> None:
    root = build_publication(tmp_path)
    home = root / "index.html"
    home.write_text(
        home.read_text(encoding="utf-8").replace(
            'data-lift-status="baseline-only"', 'data-lift-status="missing"', 1
        ),
        encoding="utf-8",
    )

    with pytest.raises(SiteQualityError, match="revenue-weighted index contract"):
        check_site(root)


def test_quality_gate_rejects_band_reduced_to_non_range_markup(tmp_path: Path) -> None:
    root = build_publication(tmp_path)
    page = root / "companies" / "gm" / "index.html"
    content = page.read_text(encoding="utf-8").replace(
        'class="range-band"', 'class="range-only-point"', 1
    )
    page.write_text(content, encoding="utf-8")

    with pytest.raises(SiteQualityError, match="without a visible range"):
        check_site(root)


def test_quality_gate_reserves_accent_for_graded_state(tmp_path: Path) -> None:
    root = build_publication(tmp_path)
    css = root / "assets" / "site.css"
    css.write_text(css.read_text(encoding="utf-8") + "\na { color: var(--verified); }\n")

    with pytest.raises(SiteQualityError, match="exclusive to the graded state"):
        check_site(root)


def test_quality_gate_rejects_flow_over_twelve_nodes(tmp_path: Path) -> None:
    root = build_publication(tmp_path)
    page = root / "index.html"
    page.write_text(
        page.read_text(encoding="utf-8").replace('data-node-count="9"', 'data-node-count="10"'),
        encoding="utf-8",
    )

    with pytest.raises(SiteQualityError, match="9-node flow readability cap"):
        check_site(root)


def test_quality_gate_rejects_missing_flow_tier_style(tmp_path: Path) -> None:
    root = build_publication(tmp_path)
    page = root / "methodology" / "index.html"
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            'class="flow-ribbon tier-3"', 'class="flow-ribbon tier-missing"'
        ),
        encoding="utf-8",
    )

    with pytest.raises(SiteQualityError, match="tier-encoded flow contract"):
        check_site(root)


def test_quality_gate_rejects_page_tagline_above_h1(tmp_path: Path) -> None:
    root = build_publication(tmp_path)
    page = root / "scoreboard" / "index.html"
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            "<h1>Predictions</h1>", '<div class="eyebrow">Tagline</div><h1>Predictions</h1>'
        ),
        encoding="utf-8",
    )

    with pytest.raises(SiteQualityError, match="tagline or kicker"):
        check_site(root)


def test_quality_gate_rejects_headline_number_without_figure_contract(tmp_path: Path) -> None:
    root = build_publication(tmp_path)
    page = root / "index.html"
    page.write_text(
        page.read_text(encoding="utf-8").replace('data-figure="dfr"', 'data-not-a-figure="dfr"', 1),
        encoding="utf-8",
    )

    with pytest.raises(SiteQualityError, match="headline number outside a figure contract"):
        check_site(root)


def test_quality_gate_rejects_figure_without_declared_or_visible_unit(tmp_path: Path) -> None:
    root = build_publication(tmp_path)
    page = root / "companies" / "gm" / "index.html"
    content = page.read_text(encoding="utf-8").replace("DFR%", "DFR share")
    content = content.replace("%", " pct").replace("percent", "share")
    page.write_text(content, encoding="utf-8")

    with pytest.raises(SiteQualityError, match="does not render its declared percent unit"):
        check_site(root)


def test_quality_gate_rejects_modeled_point_without_band_copy(tmp_path: Path) -> None:
    root = build_publication(tmp_path)
    page = root / "methodology" / "sensitivity" / "index.html"
    page.write_text(
        page.read_text(encoding="utf-8").replace("80% band", "point estimate"),
        encoding="utf-8",
    )

    with pytest.raises(SiteQualityError, match="modeled point without its band"):
        check_site(root)


def test_quality_gate_rejects_tiered_figure_without_badge(tmp_path: Path) -> None:
    root = build_publication(tmp_path)
    page = root / "companies" / "gm" / "index.html"
    page.write_text(
        page.read_text(encoding="utf-8").replace("tier-badge", "tier-label"),
        encoding="utf-8",
    )

    with pytest.raises(SiteQualityError, match="carries tiers without a tier badge"):
        check_site(root)


def test_quality_gate_rejects_color_only_tier_distinction(tmp_path: Path) -> None:
    root = build_publication(tmp_path)
    css = root / "assets" / "site.css"
    css.write_text(
        css.read_text(encoding="utf-8").replace(
            ".tier-2 { background-color: var(--raised-paper); background-image:",
            ".tier-2 { background-color: var(--raised-paper); color:",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(SiteQualityError, match="depends on color or lacks texture"):
        check_site(root)


def test_quality_gate_requires_estimated_for_dfr_figures(tmp_path: Path) -> None:
    root = build_publication(tmp_path)
    page = root / "companies" / "gm" / "index.html"
    content = page.read_text(encoding="utf-8").replace("Estimated", "Modeled")
    page.write_text(content.replace("estimated", "modeled"), encoding="utf-8")

    with pytest.raises(SiteQualityError, match="renders DFR% without estimated"):
        check_site(root)


def test_quality_gate_reserves_measured_for_explicit_tier_one_context(tmp_path: Path) -> None:
    root = build_publication(tmp_path)
    page = root / "scoreboard" / "index.html"
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            "Each row is an immutable monthly forecast",
            "Each measured row is an immutable monthly forecast",
        ),
        encoding="utf-8",
    )

    with pytest.raises(SiteQualityError, match="uses measured outside an explicit Tier 1"):
        check_site(root)


def test_quality_gate_requires_sitewide_disclaimer_and_license(tmp_path: Path) -> None:
    root = build_publication(tmp_path)
    page = root / "changelog" / "index.html"
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            "commercial licensing reserved", "commercial terms available"
        ),
        encoding="utf-8",
    )

    with pytest.raises(SiteQualityError, match="lacks required accessible markup"):
        check_site(root)


def test_browser_accessibility_gate_includes_m5_methodology_pages() -> None:
    script = (Path(__file__).parents[2] / "tools" / "axe-check.mjs").read_text(encoding="utf-8")

    assert '"/methodology/coverage/"' in script
    assert '"/methodology/sensitivity/"' in script
    assert '"/roadmap/"' in script
    assert '"/corrections/"' in script
    assert '"/companies/"' in script
    assert '"scoreboard", "predictions"' in script
    assert "`/scoreboard/predictions/${entry.name}/`" in script
    assert "keyboardAudit" in script
    assert "textarea,iframe,[tabindex]" in script
    assert "semanticAudit" in script
    assert "mobileLayoutAudit" in script
    assert "viewport: { width: 390, height: 844 }" in script
    assert "document.documentElement.scrollWidth" in script
    assert "mobileLayoutFailures.length === 0" in script
    assert "details.baseline-disclosure" in script


def test_ux_inventory_crawler_captures_every_route_family_without_javascript() -> None:
    script = (Path(__file__).parents[2] / "tools" / "ux-inventory.mjs").read_text(encoding="utf-8")

    assert "javaScriptEnabled: false" in script
    assert '"companies/"' in script
    assert '"roadmap/"' in script
    assert '"corrections/"' in script
    assert "companies/${String(row.ticker).toLowerCase()}/" in script
    assert "scoreboard/predictions/${String(row.prediction_id)}/" in script
    assert "response?.status() !== 200 || page.url() !== expected" in script
    assert "document.body?.innerText" in script
    assert 'document.querySelectorAll("details")' in script
    assert "disclosure.open = true" in script
    assert 'document.querySelectorAll("a[href]")' in script

    diff_script = (Path(__file__).parents[2] / "tools" / "ux-inventory-diff.mjs").read_text(
        encoding="utf-8"
    )
    assert "Missing routes" in diff_script
    assert "Missing distinct lines" in diff_script
    assert "Missing normalized internal targets" in diff_script
    assert "Missing outbound targets" in diff_script
