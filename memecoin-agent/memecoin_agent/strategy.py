"""Entry filtering/scoring and exit rules.

Entry thesis: buy tokens with real liquidity, sustained buy pressure and
rising price that are past the first-minutes sniper chaos but not yet
parabolic. Exits (let_it_run, the default): take the initial cost and half
the profit out once, let the rest run against its own peak, a hard stop with
dip reviews before it, a time stop, and an immediate exit if liquidity is
pulled. `exits.style: classic` keeps the older stop / take-profit / trailing stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import ExitConfig, FilterConfig, LaunchConfig, LowCapConfig
from .data.dexscreener import canonical_chain
from .models import Position, TokenSnapshot


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


@dataclass
class EntryEvaluation:
    snapshot: TokenSnapshot
    passed: bool
    score: float
    reasons: list[str]
    lane: str = "momentum"  # launch, lowcap and sniper are separate entry paths


@dataclass
class ExitDecision:
    fraction: float  # of the current position, (0, 1]
    reason: str
    tp_level: int | None = None
    make_dust: bool = False  # keep what's left as dust (LarpCheck-flag exits)
    larp: dict | None = None  # LarpCheck verdict + reasoning behind the exit, stored on the position
    secure: bool = False  # let_it_run: the initials are out, what's left is a runner


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
    if s.market_cap_usd < f.min_market_cap_usd:
        reasons.append(f"low cap ${s.market_cap_usd:,.0f} (handled by the low-cap or copy lane)")
    if lc.min_price_change_m5 is not None and s.price_change_m5 < lc.min_price_change_m5:
        reasons.append(f"crashing: {s.price_change_m5:+.0f}% in 5m")
    return reasons


def lowcap_reasons(s: TokenSnapshot, lc: LowCapConfig, f: FilterConfig, now: datetime) -> list[str]:
    """Why a post-launch small token does not qualify for the controlled low-cap lane."""
    reasons: list[str] = []
    enabled_chains = {canonical_chain(c) for c in lc.chains}
    if "*" not in enabled_chains and canonical_chain(s.chain) not in enabled_chains:
        reasons.append(f"chain {s.chain} not enabled for the low-cap lane")
    if s.mint in f.blacklist:
        reasons.append("blacklisted")
    if s.price_usd <= 0:
        reasons.append("no positive price")
    quote_symbols = lc.quote_symbols if canonical_chain(s.chain) != "solana" else f.quote_symbols
    if s.quote_symbol.upper() not in {q.upper() for q in quote_symbols}:
        reasons.append(f"quote {s.quote_symbol} not allowed")
    age = s.age_minutes(now)
    if age is None:
        reasons.append("unknown pair age")
    else:
        if age < lc.min_age_minutes:
            reasons.append(f"age {age:.0f}m < {lc.min_age_minutes:.0f}m")
        if age > lc.max_age_minutes:
            reasons.append(f"age {age:.0f}m > {lc.max_age_minutes:.0f}m")
    if not lc.min_market_cap_usd <= s.market_cap_usd < f.min_market_cap_usd:
        reasons.append(f"mcap ${s.market_cap_usd:,.0f} outside low-cap range")
    if s.market_cap_usd > lc.max_market_cap_usd:
        reasons.append(f"mcap ${s.market_cap_usd:,.0f} > ${lc.max_market_cap_usd:,.0f}")
    if s.liquidity_usd < lc.min_liquidity_usd:
        reasons.append(f"liquidity ${s.liquidity_usd:,.0f} < ${lc.min_liquidity_usd:,.0f}")
    if f.max_liquidity_usd is not None and s.liquidity_usd > f.max_liquidity_usd:
        reasons.append(f"liquidity ${s.liquidity_usd:,.0f} > max")
    if s.volume_h1 < lc.min_volume_h1_usd:
        reasons.append(f"vol1h ${s.volume_h1:,.0f} < ${lc.min_volume_h1_usd:,.0f}")
    if s.txns_h1 < lc.min_txns_h1:
        reasons.append(f"txns1h {s.txns_h1} < {lc.min_txns_h1}")
    if s.buy_sell_ratio_h1 < lc.min_buy_sell_ratio_h1:
        reasons.append(f"b/s1h {s.buy_sell_ratio_h1:.2f} < {lc.min_buy_sell_ratio_h1:.2f}")
    if s.buy_sell_ratio_m5 < lc.min_buy_sell_ratio_m5:
        reasons.append(f"b/s5m {s.buy_sell_ratio_m5:.2f} < {lc.min_buy_sell_ratio_m5:.2f}")
    if s.price_change_m5 < lc.min_price_change_m5:
        reasons.append(f"chg5m {s.price_change_m5:+.1f}% < {lc.min_price_change_m5:+.1f}%")
    if s.price_change_h1 < lc.min_price_change_h1:
        reasons.append(f"chg1h {s.price_change_h1:+.1f}% < {lc.min_price_change_h1:+.1f}%")
    if s.price_change_h1 > lc.max_price_change_h1:
        reasons.append(f"chg1h {s.price_change_h1:+.1f}% too extended")
    if lc.max_price_change_m5 is not None and s.price_change_m5 > lc.max_price_change_m5:
        reasons.append(f"chg5m {s.price_change_m5:+.1f}% spiking (top of a 5-minute candle)")
    if s.volume_to_liquidity_h1 < lc.min_volume_to_liquidity_h1:
        reasons.append(f"vol/liq {s.volume_to_liquidity_h1:.2f} < {lc.min_volume_to_liquidity_h1:.2f}")
    if lc.min_volume_m5_share is not None and s.volume_h1 > 0 and s.volume_m5 < lc.min_volume_m5_share * s.volume_h1:
        reasons.append(f"vol5m ${s.volume_m5:,.0f} is under {lc.min_volume_m5_share * 100:g}% of the hour's (fading)")
    score = momentum_score(s)
    if score < lc.min_score:
        reasons.append(f"score {score:.2f} < {lc.min_score:.2f}")
    return reasons


def secure_fraction(pos: Position, price: float, e: ExitConfig, fee_pct: float = 0.0) -> float:
    """Share of the position to sell so the proceeds cover the initial cost plus
    e.secure_profit_share of the profit at `price` (after the sell fee)."""
    value = pos.market_value_usd(price) * (1 - fee_pct / 100)
    if value <= 0:
        return 1.0
    profit = value - pos.initial_cost_usd
    if profit <= 0:
        return 1.0
    return min(1.0, (pos.initial_cost_usd + e.secure_profit_share * profit) / value)


RUNNER_TAKE = 99  # marker in Position.tp_levels_hit: the runner's one extra take profit is done


def runner_drawdown_limit(e: ExitConfig, peak_gain_pct: float) -> float:
    """How far below its peak a runner may fall before it is sold. With exits.runner_trail the limit depends
    on how far the runner's peak is above the entry price; otherwise it is exits.runner_drawdown_pct."""
    limit = e.runner_drawdown_pct
    for tier in sorted(e.runner_trail, key=lambda t: t.above_pct):
        if peak_gain_pct >= tier.above_pct:
            limit = tier.drawdown_pct
    return limit


def lp_removed_pct(pos: Position, s: TokenSnapshot) -> float:
    """% of the pool's liquidity that is missing beyond what the price move explains. In a constant-product
    pool liquidity moves with the square root of the price, so a fall in price alone is not a pull. 0 unless
    the pool is also at least 15% smaller than at entry (pools that don't track price, such as one-sided
    ones, must not look like a pull while the coin rises)."""
    if pos.entry_liquidity_usd <= 0 or pos.entry_price_usd <= 0 or s.price_usd <= 0:
        return 0.0
    if s.liquidity_usd > pos.entry_liquidity_usd * 0.85:
        return 0.0
    expected = pos.entry_liquidity_usd * (s.price_usd / pos.entry_price_usd) ** 0.5
    return (1 - s.liquidity_usd / expected) * 100 if expected > 0 else 0.0


def _review_due(pos: Position, now: datetime, minutes: float) -> bool:
    if pos.next_review_at is None:
        pos.next_review_at = now + timedelta(minutes=minutes)
        return False
    if now < pos.next_review_at:
        return False
    pos.next_review_at = now + timedelta(minutes=minutes)
    return True


def _let_it_run(pos: Position, s: TokenSnapshot, e: ExitConfig, now: datetime, fee_pct: float) -> ExitDecision | None:
    price = s.price_usd
    pnl = pos.pnl_pct(price)
    if pos.secured:
        # The runner: house money. Its peak is tracked every tick.
        pos.runner_peak_usd = max(pos.runner_peak_usd, price)
        peak = pos.runner_peak_usd
        drawdown = (1 - price / peak) * 100 if peak > 0 else 0.0
        peak_gain = (peak / pos.entry_price_usd - 1) * 100 if pos.entry_price_usd > 0 else 0.0
        limit = runner_drawdown_limit(e, peak_gain)
        if (e.runner_take_at_pct is not None and RUNNER_TAKE not in pos.tp_levels_hit and pnl >= e.runner_take_at_pct
                and 0 < e.runner_take_fraction < 1):
            return ExitDecision(e.runner_take_fraction, f"runner take profit ({pnl:+.0f}%): sold "
                                f"{e.runner_take_fraction * 100:.0f}% of the runner, the rest keeps running",
                                tp_level=RUNNER_TAKE)
        if e.runner_floor_pct is not None and pnl <= e.runner_floor_pct:
            return ExitDecision(1.0, f"runner back at its entry ({pnl:+.1f}%, floor {e.runner_floor_pct:+g}%)")
        if e.runner_review_minutes > 0 and not _review_due(pos, now, e.runner_review_minutes):
            return None
        since = (now - pos.secured_at).total_seconds() / 3600 if pos.secured_at else 0.0
        pos.review_note = (f"runner {pnl:+.0f}% from entry, {drawdown:.0f}% below its peak "
                           f"(sells at {limit:g}%), {since:.1f}h since the initials came out")
        if drawdown >= limit:
            return ExitDecision(1.0, f"runner {drawdown:.0f}% below its peak (limit {limit:g}%, "
                                     f"pnl {pnl:+.1f}%)")
        return None

    if pnl <= -e.hard_stop_pct:
        return ExitDecision(1.0, f"stop loss ({pnl:+.1f}%)")
    if pnl >= e.secure_at_pct:
        if e.secure_sell_fraction is not None:
            frac = e.secure_sell_fraction
            return ExitDecision(frac, f"first take profit ({pnl:+.1f}%): sold {frac * 100:.0f}%, the rest runs",
                                secure=frac < 0.999)
        frac = secure_fraction(pos, price, e, fee_pct)
        return ExitDecision(frac, f"initials out ({pnl:+.1f}%): sold {frac * 100:.0f}% = the initial "
                                  f"${pos.initial_cost_usd:,.2f} + {e.secure_profit_share * 100:g}% of the profit, "
                                  f"the rest runs", secure=frac < 0.999)
    peak_pnl = pos.pnl_pct(max(pos.peak_price_usd, price))
    if e.protect_after_pct is not None and peak_pnl >= e.protect_after_pct and pnl <= e.protect_floor_pct:
        return ExitDecision(1.0, f"gave its gain back (was {peak_pnl:+.0f}%, now {pnl:+.1f}%): out near break-even")
    held_min = (now - pos.opened_at).total_seconds() / 60
    if e.stall_minutes is not None and held_min >= e.stall_minutes and peak_pnl < e.stall_min_peak_pct:
        return ExitDecision(1.0, f"no follow-through ({held_min:.0f}m held, best {peak_pnl:+.0f}%, now {pnl:+.1f}%)")
    if e.dip_watch_pct is None:
        return None
    if pos.dip_since is None:
        if pnl <= -e.dip_watch_pct:
            pos.dip_since, pos.next_review_at = now, now + timedelta(minutes=e.dip_review_minutes)
            pos.dip_open_usd = pos.dip_high_usd = pos.dip_low_usd = price
            pos.review_note = f"dip watch: {pnl:+.0f}%, first review in {e.dip_review_minutes:g}m"
        return None
    pos.dip_high_usd, pos.dip_low_usd = max(pos.dip_high_usd, price), min(pos.dip_low_usd, price)
    if not _review_due(pos, now, e.dip_review_minutes):
        return None
    slide = (1 - price / pos.dip_open_usd) * 100 if pos.dip_open_usd > 0 else 0.0
    spread = (pos.dip_high_usd / pos.dip_low_usd - 1) * 100 if pos.dip_low_usd > 0 else 0.0
    window = f"over the last {e.dip_review_minutes:g}m: {-slide:+.0f}%, range {spread:.0f}%, b/s 1h {s.buy_sell_ratio_h1:.2f}"
    if pnl > -e.dip_watch_pct:
        pos.dip_since = pos.next_review_at = None
        pos.review_note = f"recovered from the dip ({pnl:+.0f}%)"
        return None
    if -pnl > e.dip_hold_max_pct:
        return ExitDecision(1.0, f"dip review: still {pnl:+.0f}%, deeper than the {e.dip_hold_max_pct:g}% hold band ({window})")
    consolidating = spread <= e.consolidation_max_range_pct
    accumulating = s.buys_h1 > s.sells_h1
    if slide > e.consolidation_max_slide_pct or not (consolidating or accumulating):
        return ExitDecision(1.0, f"dip review: {pnl:+.0f}% and still sliding ({window})")
    pos.dip_open_usd = pos.dip_high_usd = pos.dip_low_usd = price
    held = (now - pos.dip_since).total_seconds() / 60
    pos.review_note = (f"dip watch: {pnl:+.0f}%, {'consolidating' if consolidating else 'accumulating'} "
                       f"({window}); held {held:.0f}m in the dip, next review in {e.dip_review_minutes:g}m")
    return None


def evaluate_exit(pos: Position, s: TokenSnapshot | None, e: ExitConfig, now: datetime,
                  fee_pct: float = 0.0) -> ExitDecision | None:
    """Decide whether to sell some or all of a position. Full exits take priority over partials.
    let_it_run keeps its review timers and dip range on the position, so call it every tick."""
    held_min = (now - pos.opened_at).total_seconds() / 60
    if e.style == "let_it_run":
        if s is None:
            if not pos.secured and e.max_hold_minutes is not None and held_min >= e.max_hold_minutes:
                return ExitDecision(1.0, f"time stop ({held_min:.0f}m, no market data)")
            return None
        if pos.entry_liquidity_usd > 0:
            liq_drop = (1 - s.liquidity_usd / pos.entry_liquidity_usd) * 100
            if liq_drop >= e.liquidity_drop_exit_pct:
                return ExitDecision(1.0, f"liquidity pulled ({liq_drop:.0f}% below entry)")
            removed = lp_removed_pct(pos, s)
            if e.lp_removed_exit_pct is not None and removed >= e.lp_removed_exit_pct:
                return ExitDecision(1.0, f"liquidity being removed ({removed:.0f}% of the pool gone beyond "
                                         f"what the price move explains)")
        decision = _let_it_run(pos, s, e, now, fee_pct)
        if decision is None and not pos.secured and e.max_hold_minutes is not None and held_min >= e.max_hold_minutes:
            return ExitDecision(1.0, f"time stop ({held_min:.0f}m, pnl {pos.pnl_pct(s.price_usd):+.1f}%)")
        return decision
    if s is None:
        # No fresh market data: we can't price stops, so only the time stop applies.
        if e.max_hold_minutes is not None and held_min >= e.max_hold_minutes:
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
    if e.max_hold_minutes is not None and held_min >= e.max_hold_minutes:
        return ExitDecision(1.0, f"time stop ({held_min:.0f}m, pnl {pnl:+.1f}%)")

    for idx, lvl in enumerate(e.take_profit_levels):
        if idx in pos.tp_levels_hit:
            continue
        if pnl >= lvl.at_pct:
            return ExitDecision(lvl.sell_fraction, f"take profit {idx + 1} ({pnl:+.1f}%)", tp_level=idx)
    return None


