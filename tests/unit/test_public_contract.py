from __future__ import annotations

from pathlib import Path


def test_repository_has_apache_license_and_self_contained_public_readme() -> None:
    root = Path(__file__).parents[2]
    license_text = (root / "LICENSE").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")

    assert "Apache License" in license_text
    assert "Version 2.0, January 2004" in license_text
    assert "END OF TERMS AND CONDITIONS" in license_text
    assert "DFRI_BUILD_SPEC.md" not in readme
    assert "Tier 1 — Observed" in readme
    assert "Tier 2 — Category-mapped" in readme
    assert "Tier 3 — Fungible" in readme
    assert "revenue-weighted" in readme
    assert "uses no market data" in readme
    assert "optional private mappings split is not used" in readme
    assert "Methodology 1.1.1" in readme
    assert "Evidence Lift" in readme
    assert "v2/feeds/dfri_companies" in readme
