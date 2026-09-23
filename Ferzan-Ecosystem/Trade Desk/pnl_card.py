"""Shareable PnL card (1200x675 PNG) in Ferzan colours.

Pure Pillow, no network. Fonts: DejaVu if the droplet has it, otherwise
Pillow's built-in scalable font (Pillow >= 10.1), so it never hard-fails on
a minimal server image.
"""

from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1200, 675
HERE = Path(__file__).resolve().parent
LOGO = HERE / "logo.jpg"

NAVY_TOP = (6, 12, 34)
NAVY_BOT = (10, 28, 74)
CYAN = (64, 214, 255)
WHITE = (236, 242, 255)
MUTED = (140, 158, 196)
GREEN = (38, 222, 129)
RED = (255, 84, 104)

_FONT_DIRS = (
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/dejavu",
    "/usr/share/fonts/TTF",
)


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for d in _FONT_DIRS:
        p = Path(d) / name
        if p.exists():
            return ImageFont.truetype(str(p), size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def _gradient() -> Image.Image:
    col = Image.new("RGB", (1, H))
    for y in range(H):
        t = y / (H - 1)
        col.putpixel((0, y), tuple(int(NAVY_TOP[i] + (NAVY_BOT[i] - NAVY_TOP[i]) * t) for i in range(3)))
    return col.resize((W, H))


@lru_cache(maxsize=2)
def _crest(size: int) -> Image.Image | None:
    """Circular crop of the Ferzan badge from logo.jpg."""
    if not LOGO.exists():
        return None
    src = Image.open(LOGO).convert("RGB")
    sw, sh = src.size
    # Badge is centred horizontally, slightly below vertical centre; its
    # diameter is ~84% of the image width.
    d = int(sw * 0.84)
    cx, cy = sw // 2, int(sh * 0.50)
    box = (cx - d // 2, cy - d // 2, cx + d // 2, cy + d // 2)
    crest = src.crop(box).resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(crest, (0, 0), mask)
    return out


def _fmt_usd(v: float) -> str:
    sign = "+" if v >= 0 else "-"
    v = abs(v)
    if v >= 1_000_000:
        return f"{sign}${v / 1_000_000:.2f}M"
    if v >= 10_000:
        return f"{sign}${v / 1_000:.1f}K"
    return f"{sign}${v:,.2f}"


def _fit(draw: ImageDraw.ImageDraw, text: str, max_w: int, size: int, bold: bool = True):
    """Largest font <= size that keeps text within max_w."""
    while size > 18:
        f = _font(size, bold)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 4
    return _font(size, bold)


def render(
    *,
    symbol: str,
    chain: str,
    cost_usd: float,
    worth_usd: float,
    footer: str = "",
) -> bytes:
    pnl = worth_usd - cost_usd
    pct = (pnl / cost_usd * 100) if cost_usd > 0 else 0.0
    up = pnl >= 0
    accent = GREEN if up else RED

    img = _gradient().convert("RGBA")

    # soft accent glow behind the big number
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((-120, 150, 620, 560), fill=(*accent, 70))
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(90)))

    # crest, right side, with a cyan halo
    crest = _crest(430)
    if crest is not None:
        halo = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(halo).ellipse((700, 110, 1170, 580), fill=(*CYAN, 60))
        img = Image.alpha_composite(img, halo.filter(ImageFilter.GaussianBlur(40)))
        img.alpha_composite(crest.copy(), (720, 122))

    d = ImageDraw.Draw(img)
    left = 70
    max_w = 620

    # header
    d.text((left, 52), "FERZAN TRADE DESK", font=_font(26, True), fill=CYAN)
    d.line((left, 94, left + 300, 94), fill=CYAN, width=3)

    # token + chain
    title = f"${symbol.upper()[:14]}" if symbol else "POSITION"
    d.text((left, 120), title, font=_fit(d, title, max_w, 64), fill=WHITE)
    d.text((left, 200), f"{chain.upper()} · LIVE", font=_font(26), fill=MUTED)

    # big % number
    pct_txt = f"{pct:+.1f}%"
    d.text((left, 250), pct_txt, font=_fit(d, pct_txt, max_w, 150), fill=accent)

    # $ pnl
    d.text((left, 425), _fmt_usd(pnl), font=_font(52, True), fill=accent)

    # cost / worth
    d.text((left, 508), "INVESTED", font=_font(20, True), fill=MUTED)
    d.text((left, 534), f"${cost_usd:,.2f}", font=_font(34, True), fill=WHITE)
    d.text((left + 260, 508), "WORTH", font=_font(20, True), fill=MUTED)
    d.text((left + 260, 534), f"${worth_usd:,.2f}", font=_font(34, True), fill=WHITE)

    # footer bar
    d.rectangle((0, H - 58, W, H), fill=(4, 8, 24))
    d.rectangle((0, H - 58, W, H - 55), fill=CYAN)
    if footer:
        d.text((left, H - 44), footer, font=_font(24, True), fill=WHITE)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
