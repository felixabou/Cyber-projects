"""Out-of-sample validation for exit settings (`memecoin-agent exitlab --validate`).

The plain exit lab (exitlab.compare) replays every past entry under several exit settings. Picking the best of
those on the same trades overfits: the winner is partly luck. This module:

- splits the entries chronologically into training, validation and an optional final holdout period, and drops
  ("purges") earlier-period entries whose replay window reaches into the next period, so no candle is seen twice;
- replays each strategy with realistic frictions: entry and exit latency, slippage and fees per side, exit orders
  that fail and are retried, fills capped at a share of each candle's volume (so a big sale takes several candles),
  partial exits, and a fixed horizon after which whatever is left is sold at the last price;
- compares the configured exits against the previous defaults, a fixed stop / take-profit, and buy-and-hold;
- reports net return, win rate, median and average return, maximum drawdown, profit factor, worst trade, number
  of trades, exposure time and a bootstrap interval for the average, each period labelled in- or out-of-sample;
- picks the best strategy on the training period only and shows how it ranks out of sample.

Nothing here changes how the agent trades. A strategy that only wins on the training period has shown nothing.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from .config import ExitConfig, lane_exits
from .exitlab import HOURS, Entry, wide_exits
from .models import Position, TokenSnapshot
from .strategy import ExitDecision, evaluate_exit

Decide = Callable[[Position, TokenSnapshot, datetime], "ExitDecision | None"]
MIN_TRADES = 30  # below this, every period's numbers come with a small-sample warning
TOO_FEW = 10


@dataclass
class SimParams:
    fee_pct: float = 0.5  # per side
    slip_pct: float = 1.0  # per side, on top of the candle price
    entry_latency_s: float = 0.0  # 0: the entry's recorded fill price is used as is
    exit_latency_s: float = 0.0
    fail_rate: float = 0.0  # chance an exit order fails; the exit rule then decides again on later prices
    max_volume_share: float | None = None  # at most this share of a candle's USD volume fills per candle
    horizon_hours: float = HOURS
    seed: int = 1

    def describe(self) -> str:
        vol = f"fills capped at {self.max_volume_share:.0%} of a candle's volume" if self.max_volume_share else "no volume cap"
        return (f"entry latency {self.entry_latency_s:g}s, exit latency {self.exit_latency_s:g}s, slippage "
                f"{self.slip_pct:g}%/side, fees {self.fee_pct:g}%/side, failed exits {self.fail_rate:.0%} (retried), "
                f"{vol}, horizon {self.horizon_hours:g}h")


@dataclass
class TradeResult:
    entry: Entry
    ret_pct: float | None  # None: the entry never filled (no price after the latency)
    hours_held: float = 0.0
    exits: int = 0
    failed_exits: int = 0


def _ticks(candles: list[list[float]], t_from: float, t_to: float) -> list[tuple[float, float, float, int]]:
    """(time, price, candle USD volume, candle index): each minute as open, low, high, close."""
    out = []
    for idx, c in enumerate(candles):
        ts = float(c[0])
        if ts + 60 <= t_from or ts > t_to:
            continue
        vol = float(c[5]) if len(c) > 5 and c[5] is not None else 0.0
        for i, price in enumerate((c[1], c[3], c[2], c[4])):
            t = ts + 15 * i
            if t_from <= t <= t_to and price and price > 0:
                out.append((t, float(price), vol, idx))
    return out


def simulate(e: Entry, candles: list[list[float]], decide: Decide, p: SimParams, rng: random.Random) -> TradeResult:
    t0 = e.time.timestamp()
    end = t0 + p.horizon_hours * 3600
    ticks = _ticks(candles, t0 + p.entry_latency_s, end)
    if not ticks:
        return TradeResult(e, None)
    keep = (1 - p.fee_pct / 100) * (1 - p.slip_pct / 100)
    if p.entry_latency_s > 0:
        entry_price = ticks[0][1] * (1 + p.slip_pct / 100)
        tokens = e.cost * (1 - p.fee_pct / 100) / entry_price
    else:
        entry_price, tokens = e.price, e.cost / e.price
    liq0 = 50_000.0
    pos = Position(mint=e.mint, symbol=e.symbol, pair_address="", opened_at=e.time, entry_price_usd=entry_price,
                   token_amount=tokens, cost_basis_usd=e.cost, initial_cost_usd=e.cost, peak_price_usd=entry_price,
                   entry_liquidity_usd=liq0, last_price_usd=entry_price, last_liquidity_usd=liq0, lane=e.lane)
    proceeds, left = 0.0, tokens
    order: dict | None = None  # {"tokens", "ready", "decision", "applied"}
    filled_in: dict[int, float] = {}  # candle index -> USD already sold in it
    res = TradeResult(e, 0.0)
    last_t = ticks[0][0]

    def snap(price: float, when: datetime) -> TokenSnapshot:
        return TokenSnapshot(mint=e.mint, symbol=e.symbol, name=e.symbol, pair_address="", dex_id="", quote_symbol="SOL",
                             price_usd=price, liquidity_usd=liq0 * (price / entry_price) ** 0.5, market_cap_usd=0.0,
                             volume_m5=0.0, volume_h1=0.0, volume_h24=0.0, buys_m5=1, sells_m5=1, buys_h1=1,
                             sells_h1=1, price_change_m5=0.0, price_change_h1=0.0, price_change_h24=0.0,
                             pair_created_at=None, fetched_at=when)

    for t, price, vol, idx in ticks:
        if left <= 1e-12:
            break
        last_t = t
        when = datetime.fromtimestamp(t, tz=timezone.utc)
        pos.last_price_usd, pos.peak_price_usd = price, max(pos.peak_price_usd, price)
        if order is None:
            d = decide(pos, snap(price, when), when)
            if d is None:
                continue
            order = {"tokens": left * d.fraction, "ready": t + p.exit_latency_s, "decision": d, "applied": False}
        if t < order["ready"]:
            continue
        if not order["applied"] and p.fail_rate > 0 and rng.random() < p.fail_rate:
            res.failed_exits += 1
            order = None  # the order failed; the exit rule decides again on the next prices
            continue
        sell = min(order["tokens"], left)
        if p.max_volume_share:
            room = max(0.0, p.max_volume_share * vol - filled_in.get(idx, 0.0)) / price
            sell = min(sell, room)
        if sell <= 1e-12:
            continue
        d = order["decision"]
        if not order["applied"]:  # the rule's own bookkeeping happens once, at the first fill
            order["applied"] = True
            res.exits += 1
            if d.tp_level is not None:
                pos.tp_levels_hit.append(d.tp_level)
            if d.secure:
                pos.secured_at, pos.runner_peak_usd = when, price
        filled_in[idx] = filled_in.get(idx, 0.0) + sell * price
        proceeds += sell * price * keep
        frac = sell / left
        left -= sell
        pos.token_amount = left
        pos.cost_basis_usd *= (1 - frac)
        order["tokens"] -= sell
        if order["tokens"] <= 1e-12:
            order = None
    if left > 1e-12:
        proceeds += left * ticks[-1][1] * keep  # held to the horizon (or the last candle): sold at the last price
        last_t = ticks[-1][0]
    res.ret_pct = (proceeds / e.cost - 1) * 100
    res.hours_held = max(0.0, (last_t - t0) / 3600)
    return res


# -- strategies ------------------------------------------------------------------------------------------------

def from_exits(exits: ExitConfig, fee_pct: float) -> Callable[[Entry], Decide]:
    """The production exit rules (strategy.evaluate_exit) for each entry's lane."""
    def factory(e: Entry) -> Decide:
        cfg = lane_exits(exits, e.lane)
        return lambda pos, s, now: evaluate_exit(pos, s, cfg, now, fee_pct)
    return factory


def fixed_stop_take(stop_pct: float, take_pct: float) -> Callable[[Entry], Decide]:
    def factory(e: Entry) -> Decide:
        def decide(pos: Position, s: TokenSnapshot, now: datetime) -> ExitDecision | None:
            change = (s.price_usd / pos.entry_price_usd - 1) * 100
            if change <= -stop_pct:
                return ExitDecision(1.0, f"fixed stop -{stop_pct:g}%")
            if change >= take_pct:
                return ExitDecision(1.0, f"fixed take +{take_pct:g}%")
            return None
        return decide
    return factory


def hold() -> Callable[[Entry], Decide]:
    return lambda e: (lambda pos, s, now: None)


def default_strategies(exits: ExitConfig, fee_pct: float, stop_pct: float = 30, take_pct: float = 100) -> dict:
    return {"current exits": from_exits(exits, fee_pct),
            "previous exits (50% stop, 70% leash)": from_exits(wide_exits(exits), fee_pct),
            f"fixed stop {stop_pct:g}% / take {take_pct:g}%": fixed_stop_take(stop_pct, take_pct),
            "buy and hold to the horizon": hold()}


# -- periods ---------------------------------------------------------------------------------------------------

@dataclass
class Period:
    name: str
    in_sample: bool
    entries: list[Entry] = field(default_factory=list)
    purged: int = 0


def chrono_split(entries: list[Entry], fractions: tuple[float, ...] = (0.6, 0.2, 0.2),
                 horizon_hours: float = HOURS) -> list[Period]:
    """Training, validation and (with three fractions) holdout, in time order. An entry whose replay window reaches
    past the start of the next period is dropped, so the periods share no candles."""
    if not 2 <= len(fractions) <= 3 or any(f <= 0 for f in fractions):
        raise ValueError("split needs two or three positive fractions, e.g. 0.6,0.2,0.2")
    names = ["train", "validation", "holdout"][:len(fractions)]
    ordered = sorted(entries, key=lambda e: e.time)
    total, n = sum(fractions), len(ordered)
    bounds, acc = [], 0.0
    for f in fractions[:-1]:
        acc += f
        bounds.append(round(n * acc / total))
    cuts = [0, *bounds, n]
    periods = [Period(name, name == "train", ordered[a:b]) for name, a, b in zip(names, cuts, cuts[1:])]
    for cur, nxt in zip(periods, periods[1:]):
        if not nxt.entries:
            continue
        start = nxt.entries[0].time
        keep = [e for e in cur.entries if e.time + timedelta(hours=horizon_hours) <= start]
        cur.purged, cur.entries = len(cur.entries) - len(keep), keep
    return periods


# -- metrics ---------------------------------------------------------------------------------------------------

def metrics(results: list[TradeResult], seed: int = 1) -> dict:
    filled = [r for r in results if r.ret_pct is not None]
    rets = [r.ret_pct for r in filled]
    out = {"n": len(filled), "missed": len(results) - len(filled)}
    if not rets:
        return out
    cost = sum(r.entry.cost for r in filled)
    gains, losses = sum(x for x in rets if x > 0), -sum(x for x in rets if x < 0)
    curve, peak, dd = 0.0, 0.0, 0.0
    for r in sorted(filled, key=lambda r: r.entry.time):
        curve += r.ret_pct
        peak = max(peak, curve)
        dd = min(dd, curve - peak)
    rng = random.Random(seed)
    boot = sorted(statistics.fmean(rng.choices(rets, k=len(rets))) for _ in range(2000)) if len(rets) > 1 else [rets[0]] * 2
    out.update(net_pct=sum(r.ret_pct * r.entry.cost for r in filled) / cost, win_rate=sum(x > 0 for x in rets) / len(rets),
               median=statistics.median(rets), average=statistics.fmean(rets), max_drawdown=dd,
               profit_factor=(gains / losses) if losses else float("inf"), worst=min(rets),
               exposure_hours=statistics.fmean(r.hours_held for r in filled),
               ci90=(boot[len(boot) // 20], boot[len(boot) * 19 // 20]),
               exits=sum(r.exits for r in filled), failed_exits=sum(r.failed_exits for r in filled))
    return out


def _row(name: str, m: dict, width: int) -> str:
    if not m.get("n"):
        return f"  {name:<{width}}  no filled trades"
    pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
    return (f"  {name:<{width}}  {m['n']:>4}  {m['net_pct']:+7.1f}%  {m['win_rate']:4.0%}  {m['median']:+7.1f}%  "
            f"{m['average']:+7.1f}% [{m['ci90'][0]:+.1f}, {m['ci90'][1]:+.1f}]  {m['max_drawdown']:+8.0f}%  {pf:>5}  "
            f"{m['worst']:+6.0f}%  {m['exposure_hours']:5.1f}h")


def validate(entries: list[Entry], candles: Callable[[Entry], list[list[float]]], strategies: dict,
             params: SimParams | None = None, fractions: tuple[float, ...] = (0.6, 0.2, 0.2),
             purge_hours: float | None = None) -> tuple[str, dict]:
    """The report text, and {period: {strategy: metrics}} for callers and tests. purge_hours (default: the horizon)
    is how far an entry's window reaches when deciding whether it overlaps the next period."""
    p = params or SimParams()
    usable = [e for e in entries if len(candles(e)) >= 5]
    purge = p.horizon_hours if purge_hours is None else purge_hours
    periods = chrono_split(usable, fractions, purge)
    table: dict[str, dict[str, dict]] = {}
    width = max(len(n) for n in strategies)
    lines = [f"ExitLab validation: {len(usable)} of {len(entries)} entries have minute candles; split by date, "
             "earlier periods purged where their replay window reaches the next.", ""]
    for per in periods:
        span = (f"{per.entries[0].time:%Y-%m-%d} to {per.entries[-1].time:%Y-%m-%d}" if per.entries else "empty")
        label = "IN-SAMPLE" if per.in_sample else "OUT-OF-SAMPLE"
        lines.append(f"  {per.name:<10} {len(per.entries):>4} entries  {span}  ({label})"
                     + (f", {per.purged} purged" if per.purged else ""))
    if purge < p.horizon_hours:
        lines.append(f"  ! purge window {purge:g}h is shorter than the {p.horizon_hours:g}h horizon: trades held longer "
                     "can share candles with the next period")
    lines += ["", f"Assumptions: {p.describe()}.", "Columns: trades, net return (cost-weighted), win rate, median, "
              "average [90% bootstrap interval], max drawdown (cumulative, in % of one stake), profit factor, worst "
              "trade, average time held.", ""]
    for per in periods:
        label = "IN-SAMPLE: tuning ground, not evidence" if per.in_sample else "OUT-OF-SAMPLE"
        lines.append(f"[{per.name}: {label}]")
        table[per.name] = {}
        for name, factory in strategies.items():
            rng = random.Random(p.seed)
            res = [simulate(e, candles(e), factory(e), p, rng) for e in per.entries]
            m = metrics(res, p.seed)
            table[per.name][name] = m
            lines.append(_row(name, m, width))
        n = len(per.entries)
        if n < TOO_FEW:
            lines.append(f"  ! only {n} trades: too few to judge anything")
        elif n < MIN_TRADES:
            lines.append(f"  ! small sample ({n} trades): differences inside the bootstrap intervals are noise")
        lines.append("")
    train = table.get("train", {})
    ranked = sorted((k for k in train if train[k].get("n")), key=lambda k: train[k]["net_pct"], reverse=True)
    if ranked:
        best = ranked[0]
        lines.append(f"Picked on the training period only: '{best}' (net {train[best]['net_pct']:+.1f}%).")
        for per in periods[1:]:
            got = table[per.name]
            order = sorted((k for k in got if got[k].get("n")), key=lambda k: got[k]["net_pct"], reverse=True)
            if best in order:
                lines.append(f"  {per.name}: ranked {order.index(best) + 1} of {len(order)}, net {got[best]['net_pct']:+.1f}%")
        lines.append("Only the out-of-sample rows say anything about future trades. A strategy that leads on the "
                     "training period alone has shown nothing; one that also leads out of sample, outside the noise, "
                     "is worth a cautious, small live trial, not a conclusion.")
    return "\n".join(lines), table
