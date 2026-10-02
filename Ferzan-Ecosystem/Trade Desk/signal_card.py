"""Signal image (1200x630 PNG) for the per-chain Signals channels, in Ferzan colours.

Pure Pillow, no network, no emoji (fonts have none). Everything is optional: any missing number shows a dash,
and the caller falls back to the plain text card if rendering ever raises.
"""

from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from pnl_card import _crest, _font  # same crest crop and font fallback as the PnL card

W, H = 1200, 630
HERE = Path(__file__).resolve().parent

NAVY_TOP = (6, 12, 34)
NAVY_BOT = (10, 28, 74)
CYAN = (64, 214, 255)
WHITE = (236, 242, 255)
MUTED = (140, 158, 196)
PANEL = (16, 30, 66)
PANEL_EDGE = (36, 58, 112)
GREEN = (38, 222, 129)
RED = (255, 84, 104)
GOLD = (255, 190, 64)


@lru_cache(maxsize=1)
def _bg() -> Image.Image:
    col = Image.new("RGB", (1, H))
    for y in range(H):
        t = y / (H - 1)
        col.putpixel((0, y), tuple(int(NAVY_TOP[i] + (NAVY_BOT[i] - NAVY_TOP[i]) * t) for i in range(3)))
    return col.resize((W, H))


def _fit(draw: ImageDraw.ImageDraw, text: str, max_w: int, size: int, bold: bool = True, floor: int = 28):
    """Largest font (down to floor) in which text fits max_w."""
    while size > floor:
        f = _font(size, bold)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 4
    return _font(floor, bold)


def _money(v: float) -> str:
    v = float(v or 0)
    if v <= 0:
        return "—"
    for n, s in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= n:
            return f"${v / n:.2f}{s}"
    return f"${v:,.0f}"


def _price(v: float) -> str:
    v = float(v or 0)
    if v <= 0:
        return ""
    if v >= 1:
        return f"${v:,.4f}"
    if v >= 0.0001:
        return f"${v:.6f}"
    return f"${v:.10f}".rstrip("0")


def _pill(d: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, fill, ink, size: int = 26) -> int:
    f = _font(size, True)
    w = int(d.textlength(text, font=f)) + 36
    x, y = xy
    d.rounded_rectangle((x, y, x + w, y + size + 22), radius=(size + 22) // 2, fill=fill)
    d.text((x + 18, y + 9), text, font=f, fill=ink)
    return w


def render(
    *,
    symbol: str,
    chain: str,
    mc: float = 0.0,
    liq: float = 0.0,
    price: float = 0.0,
    chg_1h: float = 0.0,
    age: str = "",
    ca: str = "",
    tags: tuple[str, ...] = (),
    note: str = "",
) -> bytes:
    up = chg_1h >= 0
    accent = GREEN if up else RED
    img = _bg().copy().convert("RGBA")

    # soft glow so the card is not flat: cyan top-right, move colour bottom-left
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((780, -260, 1380, 340), fill=CYAN + (70,))
    gd.ellipse((-300, 380, 340, 900), fill=accent + (46,))
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(90)))
    d = ImageDraw.Draw(img)

    # header: crest + label, chain pill on the right
    crest = _crest(84)
    if crest is not None:
        img.paste(crest, (56, 44), crest)
    d.text((158, 56), "FERZAN SIGNALS", font=_font(30, True), fill=WHITE)
    d.text((158, 94), "new on-chain launches", font=_font(22), fill=MUTED)
    chain_txt = (chain or "?").upper()[:14]
    cf = _font(30, True)
    cw = int(d.textlength(chain_txt, font=cf)) + 56
    d.rounded_rectangle((W - 56 - cw, 52, W - 56, 106), radius=27, outline=CYAN, width=3)
    d.text((W - 56 - cw + 28, 61), chain_txt, font=cf, fill=CYAN)

    # symbol, big, with tags to its right
    sym = "$" + (symbol or "?").upper()[:20]
    tag_w = 0
    tag_specs = []
    for t in tags[:2]:
        tag_specs.append((t, GOLD if t == "HOT" else CYAN))
    tag_w = sum(int(d.textlength(t, font=_font(26, True))) + 36 + 14 for t, _ in tag_specs)
    sf = _fit(d, sym, W - 112 - tag_w, 132, True, 56)
    d.text((56, 150), sym, font=sf, fill=WHITE)
    sym_w = int(d.textlength(sym, font=sf))
    tx = 56 + sym_w + 22
    for t, col in tag_specs:
        tx += _pill(d, (tx, 190), t, col, NAVY_TOP) + 14
    px_txt = _price(price)
    if px_txt:
        d.text((60, 300), px_txt, font=_font(34), fill=MUTED)

    # three stat cards
    stats = [
        ("MARKET CAP", _money(mc), WHITE),
        ("LIQUIDITY", _money(liq), WHITE),
        ("1H CHANGE", (f"{'+' if up else ''}{chg_1h:.1f}%" if abs(chg_1h) >= 0.05 else "—"), accent if abs(chg_1h) >= 0.05 else WHITE),
    ]
    gap, x0, y0 = 24, 56, 360
    cw = (W - 112 - gap * 2) // 3
    for i, (label, value, col) in enumerate(stats):
        x = x0 + i * (cw + gap)
        d.rounded_rectangle((x, y0, x + cw, y0 + 150), radius=26, fill=PANEL, outline=PANEL_EDGE, width=2)
        d.text((x + 28, y0 + 24), label, font=_font(22, True), fill=MUTED)
        vf = _fit(d, value, cw - 56, 64, True, 34)
        d.text((x + 28, y0 + 66), value, font=vf, fill=col)

    # footer: short CA + age on the left, call to action on the right
    short = f"{ca[:6]}…{ca[-6:]}" if len(ca) > 16 else ca
    left = "  ·  ".join(p for p in (short, age) if p)
    if left:
        d.text((56, 556), left, font=_font(26), fill=MUTED)
    cta = note or "Buy in the Ferzan bot"
    cf2 = _font(28, True)
    d.text((W - 56 - int(d.textlength(cta, font=cf2)), 554), cta, font=cf2, fill=CYAN)
    d.rectangle((0, H - 8, W, H), fill=accent)

    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()
