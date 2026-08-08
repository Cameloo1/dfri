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


def test_browser_accessibility_gate_includes_m5_methodology_pages() -> None:
    script = (Path(__file__).parents[2] / "tools" / "axe-check.mjs").read_text(encoding="utf-8")

    assert '"/methodology/coverage/"' in script
    assert '"/methodology/sensitivity/"' in script
    assert '"scoreboard", "predictions"' in script
    assert "`/scoreboard/predictions/${entry.name}/`" in script
