"""Builds the Ferzan countdown and promo graphics in promo_img/ (1600x900, same look as the originals).
  python scripts/make_promo_images.py            # writes every image
  python scripts/make_promo_images.py countdown  # only the countdown set
Re-run after changing LAUNCH_TEXT or any copy below. Needs Pillow and the DejaVu Sans fonts."""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent.parent
IMG = HERE / "promo_img"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
CYAN, ORANGE, WHITE = (0, 224, 255), (255, 107, 30), (255, 255, 255)
LAUNCH_TEXT = "Friday, November 13  •  4 PM ET"
X0, MAXW = 646, 900  # text column: starts right of the coin, this wide


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def clean_template(src: str) -> Image.Image:
    """An original graphic with its text wiped. Each row of the text area is refilled with a smooth blend from the
    background just left of it to the clean right edge, and the patch is feathered in so no seam shows."""
    im = Image.open(IMG / src).convert("RGB")
    px = im.load()
    W, H = im.size
    x0, x1, y0, y1 = 600, 1592, 215, 790
    patch = Image.new("RGB", (W, H))
    pp = patch.load()
    for y in range(y0, y1):
        lft = sorted(px[x, y] for x in range(560, 596))[18]
        rgt = sorted(px[x, y] for x in range(1530, 1592))[31]
        for x in range(x0, x1):
            t = min(1.0, max(0.0, (x - x0) / 420.0))
            t = t * t * (3 - 2 * t)
            pp[x, y] = tuple(int(lft[i] + (rgt[i] - lft[i]) * t) for i in range(3))
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rectangle([x0 + 30, y0 + 30, x1, y1 - 30], fill=255)
    im = Image.composite(patch, im, mask.filter(ImageFilter.GaussianBlur(28)))
    d = ImageDraw.Draw(im, "RGBA")  # the soft horizontal light line behind the text
    for x in range(640, 1500):
        a = int(150 * (1 - (x - 640) / 860) ** 1.6)
        d.line([(x, 479), (x, 481)], fill=(40, 120, 150, a))
    return im


def glow_text(im: Image.Image, xy, text, fnt, color, spacing=0) -> None:
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(xy, text, font=fnt, fill=color + (255,), anchor="ls")
    blur = layer.filter(ImageFilter.GaussianBlur(9))
    im.paste(Image.alpha_composite(Image.new("RGBA", im.size, (0, 0, 0, 0)), blur).convert("RGB"), mask=blur.split()[3].point(lambda v: int(v * 0.6)))
    ImageDraw.Draw(im).text(xy, text, font=fnt, fill=color, anchor="ls")


def fit(text: str, path: str, size: int, width: int = MAXW) -> ImageFont.FreeTypeFont:
    while size > 26 and font(path, size).getlength(text) > width:
        size -= 2
    return font(path, size)


def badge(im: Image.Image, y: int, text: str, color) -> None:
    f = font(BOLD, 22)
    w = int(f.getlength(text)) + 34
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([X0, y, X0 + w, y + 34], radius=8, outline=color, width=2)
    d.text((X0 + 17, y + 3), text, font=f, fill=color)


def underline(im: Image.Image, y: int, color) -> None:
    ImageDraw.Draw(im).rectangle([X0, y, X0 + 220, y + 3], fill=color)


def info_card(name: str, tag: str, title: list[str], lines: list[str], color=CYAN, base="promo_14.jpg") -> None:
    im = clean_template(base)
    lh = 34 * 1.45
    top = 480 - (len(title) * 92 + 70 + len(lines) * lh) / 2 - 10
    top = max(top, 215)
    badge(im, int(top), tag, color)
    y = int(top) + 56
    for t in title:
        f = fit(t, BOLD, 96)
        y += int(f.size * 0.9)
        glow_text(im, (X0, y), t, f, color)
        y += int(f.size * 0.16)
    underline(im, y + 10, color)
    y += 52
    d = ImageDraw.Draw(im)
    for ln in lines:
        d.text((X0, y), ln, font=fit(ln, REG, 34), fill=WHITE, anchor="ls")
        y += int(lh)
    im.save(IMG / name, quality=92)


def countdown(name: str, number: str, unit: str, tag: str, base: str) -> None:
    im = clean_template(base)
    badge(im, 262, tag, CYAN)
    glow_text(im, (X0 + 4, 472), number, fit(number, BOLD, 190, 760), CYAN)
    glow_text(im, (X0, 560), unit, fit(unit, BOLD, 82, MAXW), CYAN)
    underline(im, 578, CYAN)
    ImageDraw.Draw(im).text((X0, 626), LAUNCH_TEXT, font=font(REG, 30), fill=WHITE, anchor="ls")
    im.save(IMG / name, quality=92)


COUNTDOWNS = [  # file, number, unit, badge, background
    ("countdown_14d.jpg", "14", "DAYS", "COUNTDOWN", "promo_14.jpg"),
    ("countdown_10d.jpg", "10", "DAYS", "COUNTDOWN", "promo_14.jpg"),
    ("countdown_7d.jpg", "7", "DAYS", "LAUNCH WEEK", "promo_14.jpg"),
    ("countdown_5d.jpg", "5", "DAYS", "LAUNCH WEEK", "promo_14.jpg"),
    ("countdown_3d.jpg", "3", "DAYS", "LAUNCH WEEK", "promo_14.jpg"),
    ("countdown_2d.jpg", "2", "DAYS", "LAUNCH WEEK", "promo_14.jpg"),
    ("countdown_24h.jpg", "24", "HOURS", "LAUNCH DAY", "promo_14.jpg"),
    ("countdown_12h.jpg", "12", "HOURS", "LAUNCH DAY", "promo_14.jpg"),
    ("countdown_6h.jpg", "6", "HOURS", "LAUNCH DAY", "promo_14.jpg"),
    ("countdown_3h.jpg", "3", "HOURS", "LAUNCH DAY", "promo_14.jpg"),
    ("countdown_1h.jpg", "1", "HOUR", "LAUNCH DAY", "countdown_10m.jpg"),
    ("countdown_30m.jpg", "30", "MINUTES", "LAUNCH DAY", "countdown_10m.jpg"),
    ("countdown_10m.jpg", "10", "MINUTES", "GO TIME", "countdown_10m.jpg"),
    ("countdown_5m.jpg", "5", "MINUTES", "GO TIME", "countdown_10m.jpg"),
]

PROMOS = [  # file, badge, title lines, info lines, colour
    ("promo_15.jpg", "SUPPLY", ["1B FERZAN"], ["28%  public curve", "7%   graduation liquidity, locked forever", "5%   at graduation: 2% rewards, 3% team", "60%  locked, 25M a month for 24 months"], CYAN),
    ("promo_16.jpg", "TEAM LOCK", ["3% TEAM,", "12-MONTH CLIFF"], ["10M each to three team wallets", "Nothing moves for 12 months, then monthly releases", "Locked on-chain, link posted for anyone to check"], CYAN),
    ("promo_17.jpg", "ANTI-SNIPER", ["99% TO 1%"], ["Fee at open: 99%. One minute in: 85%", "5 min: 46%   10 min: 21%   20 min: 5%", "30 minutes in: the normal 1%", "Sniping the open costs almost everything"], ORANGE),
    ("promo_18.jpg", "BURN", ["WATCH THE", "BURN LIVE"], ["30% of Ferzan's Solana fees buy FERZAN daily and burn it", "Every buy and burn posted with its transaction link", "ferzan-factory.com/transparency"], ORANGE),
    ("promo_19.jpg", "TRADE BOT", ["DEGEN MODE"], ["Paste a CA and it buys. Bigger sizes, looser limits", "TP +300%, SL -25%, 20% trailing stop on every buy", "Rug Guard stays on. Honeypots stay blocked", "Off in one tap, your old settings come back"], ORANGE),
    ("promo_20.jpg", "TRADE BOT", ["19 CHAINS,", "ONE DESK"], ["Solana, Base, BNB, Ethereum, Tron, TON and more", "One wallet screen, one card, one tap to buy", "t.me/Ferzan_Trade_Bot"], CYAN),
    ("promo_21.jpg", "BEFORE YOU BUY", ["SEE IT FIRST"], ["Paste any CA and the card shows:", "Safety verdict, liquidity and holder concentration", "Price impact, and what you already hold", "One tap to Protect with TP and SL"], CYAN),
    ("promo_22.jpg", "COMPETE", ["WEEKLY", "LEADERBOARD"], ["Top traders and top callers on every Ferzan coin", "Every chain, ranked each week", "ferzan-factory.com/compete"], CYAN),
    ("promo_23.jpg", "BRIDGE", ["BRIDGE", "AND BUY"], ["Move value across chains and buy in one flow", "Inside Ferzan Trade Bot or on the website", "ferzan-factory.com/bridge"], CYAN),
]

if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("all", "countdown"):
        for c in COUNTDOWNS:
            countdown(*c)
    if what in ("all", "promos"):
        for f, tag, title, lines, col in PROMOS:
            info_card(f, tag, title, lines, col)
    print("done")
