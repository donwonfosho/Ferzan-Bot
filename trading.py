"""Paper execution + risk vault.

Live DEX/CEX execution is intentionally not wired. Holding user keys and
broadcasting swaps is how Banana Gun-class bots work — and how they get
drained. This file marks fills against public prices instead.
"""

from __future__ import annotations

from dataclasses import dataclass

import db
import fees
from price_fetcher import PriceFetchError, quote_price


@dataclass
class RiskDecision:
    ok: bool
    reason: str
    usd_size: float
    qty: float
    stop: float | None
    take: float | None


def evaluate_risk(
    user_id: int,
    price: float,
    stop_pct: float,
    take_pct: float,
    override_usd: float | None = None,
) -> RiskDecision:
    user = db.get_user(user_id)
    if not user:
        return RiskDecision(False, "No account. Tap /start first.", 0, 0, None, None)
    if price <= 0:
        return RiskDecision(False, "No mark price.", 0, 0, None, None)

    cash = float(user["paper_cash"])
    starting = float(user["starting_equity"])
    size_pct = float(user["size_pct"])
    max_dd = float(user["max_daily_loss_pct"])

    day_pnl = db.realized_today(user_id)
    if day_pnl <= -(starting * max_dd / 100.0):
        return RiskDecision(
            False,
            f"Daily loss breaker is open ({day_pnl:+.2f} USD). Trading resumes after UTC midnight.",
            0,
            0,
            None,
            None,
        )

    if len(db.open_positions(user_id)) >= 6:
        return RiskDecision(False, "Max 6 open paper positions. Close one first.", 0, 0, None, None)

    usd = override_usd if override_usd is not None else cash * (size_pct / 100.0)
    usd = min(usd, cash * 0.25, cash)
    cut = fees.quote(usd)
    if usd < 10:
        return RiskDecision(False, "Not enough cash for a $10 ticket.", 0, 0, None, None)
    if cash < usd:
        return RiskDecision(False, "Not enough cash after the platform cut.", 0, 0, None, None)

    qty = cut.working_usd / price
    stop = price * (1 - stop_pct / 100.0)
    take = price * (1 + take_pct / 100.0)
    return RiskDecision(True, "Risk accepted.", usd, qty, stop, take)


def paper_buy(
    user_id: int,
    card,
    force: bool = False,
    override_usd: float | None = None,
) -> tuple[bool, str]:
    user = db.get_user(user_id)
    if not user:
        return False, "Tap /start first."

    min_c = int(user["min_confluence"])
    if card.score < min_c and not force:
        return (
            False,
            f"Refused. Score {card.score} is below your floor of {min_c}. "
            f"Use the Override button if you still want the ticket.",
        )
    if card.bias == "AVOID" and not force:
        return False, "Refused. Bias is AVOID. Override only if you accept the veto."

    price = card.snapshot.price_usd
    decision = evaluate_risk(
        user_id, price, card.stop_pct, card.take_pct, override_usd=override_usd
    )
    if not decision.ok:
        return False, decision.reason

    cut = fees.quote(decision.usd_size)
    db.update_user(user_id, paper_cash=float(user["paper_cash"]) - decision.usd_size)
    fees.record(user_id, "paper_buy", cut, note=card.snapshot.symbol)
    pos_id = db.open_position(
        user_id=user_id,
        symbol=card.snapshot.symbol,
        query=card.snapshot.query,
        side="LONG",
        qty=decision.qty,
        entry=price,
        stop=decision.stop,
        take=decision.take,
        reason=card.thesis,
        signal=card.to_dict(),
    )
    db.add_journal(
        user_id,
        "BUY",
        f"#{pos_id} LONG {card.snapshot.symbol} qty={decision.qty:.6g} @ {price:.6g} "
        f"score={card.score} force={force}",
    )
    return (
        True,
        f"Filled paper #{pos_id}: LONG {card.snapshot.symbol}\n"
        f"Size ${decision.usd_size:,.2f} @ ${price:,.6g}\n"
        f"{cut.disclose()}\n"
        f"Stop ${decision.stop:,.6g}  |  Target ${decision.take:,.6g}\n"
        f"Score {card.score} · {card.bias}",
    )


def paper_close(user_id: int, pos_id: int, reason: str = "manual") -> tuple[bool, str]:
    pos = db.get_position(pos_id, user_id)
    if not pos or pos.get("closed_at"):
        return False, "No open position with that id."
    try:
        px = quote_price(pos["query"] or pos["symbol"])
    except PriceFetchError as exc:
        return False, f"Could not mark the book: {exc}"

    gross = float(pos["qty"]) * px
    cut = fees.quote(gross)
    proceeds = cut.working_usd
    pnl = proceeds - (float(pos["entry"]) * float(pos["qty"]))
    user = db.get_user(user_id)
    db.update_user(user_id, paper_cash=float(user["paper_cash"]) + proceeds)
    fees.record(user_id, "paper_sell", cut, note=str(pos["symbol"]))
    db.close_position(pos_id, px, pnl)
    db.add_journal(
        user_id,
        "SELL",
        f"#{pos_id} closed {pos['symbol']} @ {px:.6g} pnl={pnl:+.2f} ({reason}) fee={cut.fee_usd:.4f}",
    )
    return (
        True,
        f"Closed #{pos_id} {pos['symbol']} @ ${px:,.6g}\n"
        f"PnL {pnl:+,.2f} USD · {reason}\n"
        f"{cut.disclose()}",
    )


def mark_open_positions() -> list[tuple[int, int, str]]:
    """Check stops/targets. Returns (user_id, pos_id, message) for fills."""
    notices: list[tuple[int, int, str]] = []
    for pos in db.all_open_positions():
        try:
            px = quote_price(pos["query"] or pos["symbol"])
        except PriceFetchError:
            continue
        stop = pos["stop"]
        take = pos["take"]
        hit = None
        if stop is not None and px <= float(stop):
            hit = "stop"
        elif take is not None and px >= float(take):
            hit = "target"
        if not hit:
            continue
        ok, msg = paper_close(int(pos["user_id"]), int(pos["id"]), reason=hit)
        if ok:
            notices.append((int(pos["user_id"]), int(pos["id"]), msg))
    return notices


def paper_equity(user_id: int) -> float:
    user = db.get_user(user_id)
    if not user:
        return 0.0
    equity = float(user["paper_cash"])
    for pos in db.open_positions(user_id):
        try:
            px = quote_price(pos["query"] or pos["symbol"])
        except PriceFetchError:
            px = float(pos["entry"])
        equity += float(pos["qty"]) * px
    return equity


def update_peak_and_drawdown(user_id: int) -> tuple[float, float, float]:
    """Returns equity, peak, drawdown_pct. Updates stored peak."""
    user = db.get_user(user_id)
    equity = paper_equity(user_id)
    peak = float(user.get("peak_equity") or user["starting_equity"] or equity)
    if equity > peak:
        peak = equity
        db.update_user(user_id, peak_equity=peak)
    dd = 0.0 if peak <= 0 else max(0.0, (peak - equity) / peak * 100.0)
    db.record_equity_mark(user_id, equity, peak)
    return equity, peak, dd
