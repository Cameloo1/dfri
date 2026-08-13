from __future__ import annotations

from pathlib import Path

from PIL import Image

from dfri.publish.social import SocialCard, render_social_image


def test_social_image_is_deterministic_static_png(tmp_path: Path) -> None:
    card = SocialCard(
        label="2026-Q1 · estimated DFR%",
        title="Debt-Funded Revenue Index",
        figure="3.36%",
        units="80% band 3.11%-3.62%",
        detail="Revenue-weighted estimate across 50 covered companies.",
    )
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"

    assert render_social_image(first, card) == render_social_image(second, card)
    assert first.read_bytes() == second.read_bytes()
    with Image.open(first) as image:
        assert image.size == (1200, 630)
        assert image.mode == "1"
