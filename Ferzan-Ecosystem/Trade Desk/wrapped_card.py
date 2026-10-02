"""Weekly Wrapped card (1200x675 PNG): a user's last 7 days on the desk, in Ferzan colours. Pure Pillow."""
from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFilter

from pnl_card import CYAN, GREEN, H, MUTED, RED, W, WHITE, _crest, _font, _fit, _fmt_usd, _gradient


def render(*, name: str, week: dict, rank: dict, footer: str = "") -> bytes:
    pnl = float(week.get("pnl_usd") or 0)
    accent = GREEN if pnl >= 0 else RED
    img = _gradient().convert("RGBA")
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((-140, 120, 640, 600), fill=(*CYAN, 55))
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(90)))
    crest = _crest(360)
    if crest is not None:
        img.alpha_composite(crest.copy(), (790, 150))
    d = ImageDraw.Draw(img)
    left = 70
    d.text((left, 50), "FERZAN WRAPPED", font=_font(28, True), fill=CYAN)
    d.line((left, 92, left + 270, 92), fill=CYAN, width=3)
    d.text((left, 112), "YOUR WEEK", font=_font(22, True), fill=MUTED)
    who = (name or "Trader")[:22]
    d.text((left, 142), who, font=_fit(d, who, 640, 54), fill=WHITE)
    d.text((left, 212), str(rank.get("title", "Rookie")).upper(), font=_font(34, True), fill=CYAN)
    d.text((left, 280), "WEEK PNL", font=_font(20, True), fill=MUTED)
    txt = _fmt_usd(pnl)
    d.text((left, 306), txt, font=_fit(d, txt, 600, 120), fill=accent)
    wr = week.get("win_rate")
    cells = [("VOLUME", f"${float(week.get('volume_usd') or 0):,.0f}"), ("TRADES", f"{int(week.get('trades') or 0)}"),
             ("WIN RATE", "-" if wr is None else f"{wr:.0f}%"), ("BEST TRADE", _fmt_usd(float(week.get("best_usd") or 0)))]
    for i, (k, v) in enumerate(cells):
        x = left + (i % 2) * 300
        y = 452 + (i // 2) * 78
        d.text((x, y), k, font=_font(18, True), fill=MUTED)
        d.text((x, y + 24), v, font=_font(34, True), fill=WHITE)
    d.rectangle((0, H - 58, W, H), fill=(4, 8, 24))
    d.rectangle((0, H - 58, W, H - 55), fill=CYAN)
    if footer:
        d.text((left, H - 44), footer, font=_font(24, True), fill=WHITE)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
