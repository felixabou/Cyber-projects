"""Entry filtering/scoring and exit rules.

Entry thesis: buy tokens with real liquidity, sustained buy pressure and
rising price that are past the first-minutes sniper chaos but not yet
parabolic. Exits are mechanical: hard stop, staged take-profit, trailing
stop, time stop, and an immediate exit if liquidity is pulled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .config import ExitConfig, FilterConfig, LaunchConfig
from .models import Position, TokenSnapshot


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


@dataclass
class EntryEvaluation:
    snapshot: TokenSnapshot
    passed: bool
    score: float
    reasons: list[str]
    lane: str = "momentum"  # or "launch": passed on launch volume (see launch_reasons)


@dataclass
class ExitDecision:
    fraction: float  # of the current position, (0, 1]
    reason: str
    tp_level: int | None = None
    make_dust: bool = False  # keep what's left as dust (LarpCheck-flag exits)
    larp: dict | None = None  # LarpCheck verdict + reasoning behind the exit, stored on the position


def momentum_score(s: TokenSnapshot) -> float:
    """0..1 composite of buy pressure, turnover, momentum and activity."""
    buy_pressure_h1 = _clamp01((s.buy_sell_ratio_h1 - 1.0) / 1.0)
    buy_pressure_m5 = _clamp01((s.buy_sell_ratio_m5 - 1.0) / 1.5)
    turnover = _clamp01(s.volume_to_liquidity_h1 / 2.0)
    # Reward h1 gains up to ~60%, then taper: late entries on huge candles get smaller scores.
    h1 = s.price_change_h1
    trend_h1 = _clamp01(h1 / 60.0) if h1 <= 60 else _clamp01(1.0 - (h1 - 60) / 300.0)
    trend_m5 = _clamp01(s.price_change_m5 / 8.0)
    activity = _clamp01(s.txns_h1 / 1000.0)
    return round(
        0.25 * buy_pressure_h1
        + 0.15 * buy_pressure_m5
        + 0.20 * turnover
        + 0.20 * trend_h1
        + 0.10 * trend_m5
        + 0.10 * activity,
        4,
    )


def evaluate_entry(s: TokenSnapshot, f: FilterConfig, now: datetime) -> EntryEvaluation:
    reasons: list[str] = []

    def need(cond: bool, msg: str) -> None:
        if not cond:
            reasons.append(msg)

    need(s.mint not in f.blacklist, "blacklisted")
    need(s.quote_symbol.upper() in {q.upper() for q in f.quote_symbols}, f"quote {s.quote_symbol} not allowed")
    need(s.liquidity_usd >= f.min_liquidity_usd, f"liquidity ${s.liquidity_usd:,.0f} < ${f.min_liquidity_usd:,.0f}")
    if f.max_liquidity_usd is not None:
        need(s.liquidity_usd <= f.max_liquidity_usd, f"liquidity ${s.liquidity_usd:,.0f} > max")
    need(s.market_cap_usd >= f.min_market_cap_usd, f"mcap ${s.market_cap_usd:,.0f} < min")
    need(s.market_cap_usd <= f.max_market_cap_usd, f"mcap ${s.market_cap_usd:,.0f} > max")
    age = s.age_minutes(now)
    if age is None:
        reasons.append("unknown pair age")
    else:
        need(age >= f.min_age_minutes, f"age {age:.0f}m < {f.min_age_minutes:.0f}m")
        need(age <= f.max_age_hours * 60, f"age {age / 60:.1f}h > {f.max_age_hours:.0f}h")
    need(s.volume_h1 >= f.min_volume_h1_usd, f"vol1h ${s.volume_h1:,.0f} < min")
    need(s.txns_h1 >= f.min_txns_h1, f"txns1h {s.txns_h1} < {f.min_txns_h1}")
    need(s.buy_sell_ratio_h1 >= f.min_buy_sell_ratio_h1, f"b/s1h {s.buy_sell_ratio_h1:.2f} < min")
    need(s.buy_sell_ratio_m5 >= f.min_buy_sell_ratio_m5, f"b/s5m {s.buy_sell_ratio_m5:.2f} < min")
    need(s.price_change_m5 >= f.min_price_change_m5, f"chg5m {s.price_change_m5:+.1f}% < min")
    need(s.price_change_h1 >= f.min_price_change_h1, f"chg1h {s.price_change_h1:+.1f}% < min")
    need(s.price_change_h1 <= f.max_price_change_h1, f"chg1h {s.price_change_h1:+.1f}% too extended")
    need(s.volume_to_liquidity_h1 >= f.min_volume_to_liquidity_h1, f"vol/liq {s.volume_to_liquidity_h1:.2f} < min")

    score = momentum_score(s)
    need(score >= f.min_score, f"score {score:.2f} < {f.min_score:.2f}")
    return EntryEvaluation(snapshot=s, passed=not reasons, score=score, reasons=reasons)


def launch_reasons(s: TokenSnapshot, lc: LaunchConfig, f: FilterConfig, now: datetime,
                   qualified: bool = False) -> list[str]:
    """Why a token doesn't make the launch lane (empty: it does). A launch qualifies by trading
    lc.min_volume_usd while its pool is at most lc.max_age_minutes old. A token that already
    qualified (`qualified`) stays in the lane during its buy window while it isn't crashing."""
    if not qualified:
        age = s.age_minutes(now)
        if age is None or age > lc.max_age_minutes:
            return ["not a fresh launch"]
        if s.volume_h1 < lc.min_volume_usd:  # a pool under an hour old: h1 volume is all of it
            return [f"launch volume ${s.volume_h1:,.0f} < ${lc.min_volume_usd:,.0f}"]
    reasons: list[str] = []
    if s.mint in f.blacklist:
        reasons.append("blacklisted")
    if s.quote_symbol.upper() not in {q.upper() for q in f.quote_symbols}:
        reasons.append(f"quote {s.quote_symbol} not allowed")
    if s.liquidity_usd < lc.min_liquidity_usd:
        reasons.append(f"liquidity ${s.liquidity_usd:,.0f} < ${lc.min_liquidity_usd:,.0f}")
    if lc.min_price_change_m5 is not None and s.price_change_m5 < lc.min_price_change_m5:
        reasons.append(f"crashing: {s.price_change_m5:+.0f}% in 5m")
    return reasons


def evaluate_exit(pos: Position, s: TokenSnapshot | None, e: ExitConfig, now: datetime) -> ExitDecision | None:
    """Decide whether to sell some or all of a position. Full exits take priority over partials."""
    held_min = (now - pos.opened_at).total_seconds() / 60
    if s is None:
        # No fresh market data: we can't price stops, so only the time stop applies.
        if held_min >= e.max_hold_minutes:
            return ExitDecision(1.0, f"time stop ({held_min:.0f}m, no market data)")
        return None

    price = s.price_usd
    pnl = pos.pnl_pct(price)
    peak = max(pos.peak_price_usd, price)

    if pos.entry_liquidity_usd > 0:
        liq_drop = (1 - s.liquidity_usd / pos.entry_liquidity_usd) * 100
        if liq_drop >= e.liquidity_drop_exit_pct:
            return ExitDecision(1.0, f"liquidity pulled ({liq_drop:.0f}% below entry)")
    if pnl <= -e.stop_loss_pct:
        return ExitDecision(1.0, f"stop loss ({pnl:+.1f}%)")
    peak_gain = (peak / pos.entry_price_usd - 1) * 100
    if peak_gain >= e.trailing_activation_pct:
        drawdown = (1 - price / peak) * 100
        if drawdown >= e.trailing_stop_pct:
            return ExitDecision(1.0, f"trailing stop ({drawdown:.0f}% off peak, pnl {pnl:+.1f}%)")
    if held_min >= e.max_hold_minutes:
        return ExitDecision(1.0, f"time stop ({held_min:.0f}m, pnl {pnl:+.1f}%)")

    for idx, lvl in enumerate(e.take_profit_levels):
        if idx in pos.tp_levels_hit:
            continue
        if pnl >= lvl.at_pct:
            return ExitDecision(lvl.sell_fraction, f"take profit {idx + 1} ({pnl:+.1f}%)", tp_level=idx)
    return None
