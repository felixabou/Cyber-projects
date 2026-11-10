"""Exit lab: replay the agent's own past entries on real minute candles under different exit settings.

For every buy in a trades CSV it downloads the coin's minute candles from GeckoTerminal (free, no key; cached
in state/candles) from the entry until 30 hours later, then runs the production exit rules
(strategy.evaluate_exit) over them, once per set of exit settings, and compares the results on the same
entries. It answers "would a tighter stop / a different take profit / a shorter leash have done better on the
trades I actually took?" before the change is made.

What it cannot know: each minute is replayed as open, low, high, close (the bad price before the good one), a
stop fills at its trigger or at the candle's close if the candle fell through it, and every sale pays the paper
fee plus `slip_pct`. Liquidity is not in the candles, so it is assumed to follow the price the way a
constant-product pool does: liquidity-pull exits do not fire. Buy pressure is unknown and treated as even.
Treat the result as a comparison between settings, not as a forecast.
"""

from __future__ import annotations

import csv
import json
import logging
import random
import statistics
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import ExitConfig, lane_exits
from .models import Position, TokenSnapshot
from .strategy import evaluate_exit

log = logging.getLogger(__name__)

GECKO = "https://api.geckoterminal.com/api/v2"
HOURS = 30

@dataclass
class Entry:
    mint: str
    symbol: str
    time: datetime
    price: float
    cost: float
    lane: str


def lane_of(reason: str) -> str:
    r = (reason or "").strip()
    for key, lane in (("copy", "copy"), ("launch", "launch"), ("narrative", "narrative"), ("long-term", "longterm"),
                      ("manual", "manual"), ("low-cap", "lowcap"), ("Pump.fun", "sniper")):
        if r.startswith(key):
            return lane
    return "momentum"


def load_entries(paths: list[str | Path]) -> list[Entry]:
    out: list[Entry] = []
    seen: set[tuple[str, str]] = set()
    for path in paths:
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("side") != "buy" or (r["mint"], r["time"]) in seen:
                    continue
                seen.add((r["mint"], r["time"]))
                try:
                    t = datetime.fromisoformat(r["time"])
                    price, cost = float(r["price_usd"]), float(r["usd_value"])
                except (KeyError, ValueError):
                    continue
                if price > 0 and cost > 0:
                    out.append(Entry(r["mint"], r.get("symbol") or r["mint"][:6],
                                     t if t.tzinfo else t.replace(tzinfo=timezone.utc), price, cost,
                                     lane_of(r.get("reason", ""))))
    return sorted(out, key=lambda e: e.time)


class Candles:
    """Minute candles [ts, open, high, low, close, volume] after an entry, cached on disk."""

    def __init__(self, cache_dir: str | Path, http: httpx.Client | None = None, network: str = "solana",
                 pause_s: float = 2.2, sleep: Callable[[float], None] = time.sleep) -> None:
        self.dir = Path(cache_dir)
        self.http = http or httpx.Client(timeout=25.0, headers={"Accept": "application/json"})
        self.network, self.pause_s, self.sleep = network, pause_s, sleep
        self._pools: dict[str, str | None] = {}

    def _get(self, path: str, **params: Any) -> dict:
        for attempt in range(4):
            resp = self.http.get(f"{GECKO}{path}", params=params)
            self.sleep(self.pause_s)  # the free API allows about 30 calls a minute
            if resp.status_code == 429:
                self.sleep(8.0 * (attempt + 1))
                continue
            if resp.status_code == 404:
                return {}
            resp.raise_for_status()
            return resp.json() or {}
        return {}

    def _pool(self, mint: str) -> str | None:
        if mint not in self._pools:
            data = self._get(f"/networks/{self.network}/tokens/{mint}/pools", page=1).get("data") or []
            self._pools[mint] = data[0]["attributes"]["address"] if data else None
        return self._pools[mint]

    def after(self, e: Entry) -> list[list[float]]:
        t0 = int(e.time.timestamp())
        path = self.dir / f"{e.mint}_{t0}.json"
        if path.exists():
            try:
                return [c for c in json.loads(path.read_text(encoding="utf-8")).get("candles", []) if c[0] > t0]
            except (OSError, ValueError):
                pass
        candles: dict[int, list[float]] = {}
        pool = self._pool(e.mint)
        if pool:
            for before in (t0 + 14 * 3600, t0 + HOURS * 3600):
                data = self._get(f"/networks/{self.network}/pools/{pool}/ohlcv/minute", aggregate=1, limit=1000,
                                 before_timestamp=before, currency="usd")
                for c in ((data.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []:
                    candles[int(c[0])] = c
        rows = [candles[k] for k in sorted(candles)]
        self.dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"t0": t0, "symbol": e.symbol, "candles": rows}), encoding="utf-8")
        return [c for c in rows if c[0] > t0]


def replay(e: Entry, candles: list[list[float]], exits: ExitConfig, fee_pct: float = 0.5, slip_pct: float = 1.0) -> float:
    """Profit or loss, in % of the cost, of entry `e` held under `exits` over `candles`."""
    liq0 = 50_000.0
    pos = Position(mint=e.mint, symbol=e.symbol, pair_address="", opened_at=e.time, entry_price_usd=e.price,
                   token_amount=e.cost / e.price, cost_basis_usd=e.cost, initial_cost_usd=e.cost,
                   peak_price_usd=e.price, entry_liquidity_usd=liq0, last_price_usd=e.price,
                   last_liquidity_usd=liq0, lane=e.lane)
    keep = (1 - fee_pct / 100) * (1 - slip_pct / 100)
    proceeds, left = 0.0, pos.token_amount

    def snap(price: float, when: datetime) -> TokenSnapshot:
        return TokenSnapshot(mint=e.mint, symbol=e.symbol, name=e.symbol, pair_address="", dex_id="", quote_symbol="SOL",
                             price_usd=price, liquidity_usd=liq0 * (price / e.price) ** 0.5, market_cap_usd=0.0,
                             volume_m5=0.0, volume_h1=0.0, volume_h24=0.0, buys_m5=1, sells_m5=1, buys_h1=1,
                             sells_h1=1, price_change_m5=0.0, price_change_h1=0.0, price_change_h24=0.0,
                             pair_created_at=None, fetched_at=when)

    for c in candles:
        ts, o, h, low, close = c[0], c[1], c[2], c[3], c[4]
        for i, price in enumerate((o, low, h, close)):
            if left <= 1e-12 or price <= 0:
                continue
            when = datetime.fromtimestamp(ts + 15 * i, tz=timezone.utc)
            pos.last_price_usd, pos.peak_price_usd = price, max(pos.peak_price_usd, price)
            d = evaluate_exit(pos, snap(price, when), exits, when, fee_pct)
            if d is None:
                continue
            fill = price
            if price == low and d.reason.startswith("stop loss"):  # the trigger, or the close of a candle that fell through it
                trigger = e.price * (1 - exits.hard_stop_pct / 100)
                fill = trigger if close >= trigger * 0.9 else max(low, min(trigger, close))
            sold = left * d.fraction
            proceeds += sold * fill * keep
            left -= sold
            pos.token_amount = left
            pos.cost_basis_usd *= (1 - d.fraction)
            if d.tp_level is not None:
                pos.tp_levels_hit.append(d.tp_level)
            if d.secure:
                pos.secured_at, pos.runner_peak_usd = when, price
                pos.next_review_at = when + timedelta(minutes=exits.runner_review_minutes)
        if left <= 1e-12:
            break
    if left > 1e-12 and candles:
        proceeds += left * candles[-1][4] * keep  # still held when the candles end: marked at the last price
    return (proceeds / e.cost - 1) * 100


def wide_exits(base: ExitConfig) -> ExitConfig:
    """The exit settings the agent shipped with before the replay: 50% stop, 70% runner leash."""
    return replace(base, style="let_it_run", hard_stop_pct=50.0, dip_watch_pct=30.0, dip_hold_max_pct=40.0,
                   secure_profit_share=0.5, secure_sell_fraction=None, runner_review_minutes=30.0, runner_trail=[],
                   runner_drawdown_pct=70.0, runner_take_at_pct=None, runner_floor_pct=None, protect_after_pct=None,
                   stall_minutes=None, lanes={})


def _row(name: str, r: list[float], base: list[float] | None, width: int = 34) -> str:
    s = sorted(r)
    trimmed = statistics.fmean(s[:-3]) if len(s) > 6 else statistics.fmean(s)
    line = (f"  {name:<{width}} avg {statistics.fmean(r):+6.1f}%  without best 3 {trimmed:+6.1f}%  "
            f"wins {sum(x > 0 for x in r) / len(r) * 100:3.0f}%  best {s[-1]:+5.0f}%  worst {s[0]:+5.0f}%")
    if base is not None:
        diff = [a - b for a, b in zip(r, base)]
        rng = random.Random(1)
        boot = sorted(statistics.fmean(rng.choices(diff, k=len(diff))) for _ in range(2000))
        line += f"  | vs first {statistics.fmean(diff):+5.1f} (90% of resamples {boot[100]:+.1f} to {boot[1900]:+.1f})"
    return line


def compare(entries: list[Entry], candles: Callable[[Entry], list[list[float]]], variants: dict[str, ExitConfig],
            fee_pct: float = 0.5, min_candles: int = 5) -> str:
    usable = [(e, c) for e in entries if len(c := candles(e)) >= min_candles]
    if not usable:
        return "Exit lab: no entries with candle data (GeckoTerminal has no pool for these coins, or the file has no buys)."
    lanes = sorted({e.lane for e, _ in usable})
    out = [f"Exit lab: {len(usable)} of {len(entries)} entries have minute candles ({', '.join(lanes)}). "
           f"Result per entry in % of its cost, after fees and 1% slippage per sale.", ""]
    base: list[float] | None = None
    width = max(len(n) for n in variants)
    for name, exits in variants.items():
        r = [replay(e, c, lane_exits(exits, e.lane), fee_pct) for e, c in usable]
        out.append(_row(name, r, base, width))
        base = base if base is not None else r
    out += ["", "Each line replays the same entries. 'vs first' is the average difference per entry against the first "
                "line; when its range includes 0 the difference is not established by this sample."]
    return "\n".join(out)
