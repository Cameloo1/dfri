"""Deterministic server-rendered social preview cards for static publication pages."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH = 1200
HEIGHT = 630
PAPER = "#f2eee4"
INK = "#171715"
MUTED = "#615d54"
VERIFIED = "#2f6b3c"


class SocialImageError(RuntimeError):
    """A social image is missing required context or cannot be rendered deterministically."""


@dataclass(frozen=True)
class SocialCard:
    label: str
    title: str
    figure: str
    units: str
    detail: str
    verified: bool = False


def render_social_image(path: Path, card: SocialCard) -> str:
    """Render one 1200x630 PNG and return its SHA-256 digest."""

    for label, value in (
        ("label", card.label),
        ("title", card.title),
        ("figure", card.figure),
        ("units", card.units),
        ("detail", card.detail),
    ):
        if not value.strip():
            raise SocialImageError(f"Social card {label} must not be empty")
    image = Image.new("RGB", (WIDTH, HEIGHT), PAPER)
    draw = ImageDraw.Draw(image)
    label_font = ImageFont.load_default(size=25)
    title_font = ImageFont.load_default(size=42)
    figure_font = ImageFont.load_default(size=100)
    units_font = ImageFont.load_default(size=27)
    detail_font = ImageFont.load_default(size=25)
    brand_font = ImageFont.load_default(size=25)

    draw.line((64, 52, WIDTH - 64, 52), fill=INK, width=2)
    draw.text((64, 76), card.label, font=label_font, fill=MUTED)
    _draw_wrapped(draw, card.title, (64, 124), title_font, INK, 43, 2)
    draw.text((64, 244), card.figure, font=figure_font, fill=VERIFIED if card.verified else INK)
    draw.text((68, 365), card.units, font=units_font, fill=MUTED)
    draw.line((64, 425, WIDTH - 64, 425), fill=MUTED, width=1)
    _draw_wrapped(draw, card.detail, (64, 456), detail_font, INK, 72, 3)
    draw.text((64, 570), "DFRI · Camelon Systems", font=brand_font, fill=INK)
    draw.text((WIDTH - 365, 570), "Public, append-only evidence", font=brand_font, fill=MUTED)

    path.parent.mkdir(parents=True, exist_ok=True)
    # The previews deliberately use the publication's paper/ink visual language.
    # A thresholded one-bit PNG keeps all per-page images small enough to remain
    # a metadata-only addition to the static site.
    bitonal = image.convert("L").point(lambda value: 255 if value >= 160 else 0, mode="1")
    bitonal.save(path, format="PNG", optimize=True)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _draw_wrapped(
    draw: ImageDraw.ImageDraw,
    text: str,
    position: tuple[int, int],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: str,
    columns: int,
    max_lines: int,
) -> None:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= columns:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".") + "…"
    x, y = position
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += int(font.size * 1.18) if isinstance(font, ImageFont.FreeTypeFont) else 36
