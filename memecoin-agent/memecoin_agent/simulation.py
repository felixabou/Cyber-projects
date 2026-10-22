"""Offline synthetic memecoin market for demos and tests.

Tokens launch continuously and follow one of several regimes (pump-then-dump,
slow bleed, chop, rug pull). It is a sanity check that the plumbing and risk
rules behave, not evidence the strategy makes money on real markets.
"""

from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .models import SafetyReport, TokenSnapshot


class SimClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.t = start or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


@dataclass
class _SimToken:
    mint: str
    symbol: str
    created: datetime
    price: float
    liquidity: float
    regime: str
    phase_until: datetime
    drift_per_min: float
    vol_per_min: float
    safe: bool
    rugged: bool = False
    history: deque = field(default_factory=lambda: deque(maxlen=2000))

    def change_since(self, now: datetime, minutes: float) -> float:
        cutoff = now - timedelta(minutes=minutes)
        past = next((p for t, p in self.history if t >= cutoff), self.price)
        return (self.price / past - 1) * 100 if past > 0 else 0.0


class SyntheticMarket:
    SUPPLY = 1_000_000_000

    def __init__(self, clock: SimClock, seed: int = 7, initial_tokens: int = 15, launch_every_min: float = 6.0) -> None:
        self.clock = clock
        self.rng = random.Random(seed)
        self.tokens: dict[str, _SimToken] = {}
        self.launch_every_min = launch_every_min
        self._next_launch = clock()
        self._n = 0
        for _ in range(initial_tokens):
            self._launch(age_min=self.rng.uniform(20, 600))

    def _launch(self, age_min: float = 0.0) -> None:
        self._n += 1
        rng = self.rng
        now = self.clock()
        regime = rng.choices(["pump", "bleed", "chop", "rug"], weights=[0.35, 0.25, 0.25, 0.15])[0]
        mcap = rng.uniform(80_000, 1_500_000)
        drift = {"pump": rng.uniform(0.4, 1.5), "bleed": -rng.uniform(0.1, 0.5),
                 "chop": 0.0, "rug": rng.uniform(0.3, 1.0)}[regime]
        tok = _SimToken(
            mint=f"SIM{self._n:05d}pump",
            symbol=f"SIM{self._n}",
            created=now - timedelta(minutes=age_min),
            price=mcap / self.SUPPLY,
            liquidity=mcap * rng.uniform(0.12, 0.3),
            regime=regime,
            phase_until=now + timedelta(minutes=rng.uniform(20, 120)),
            drift_per_min=drift,
            vol_per_min=rng.uniform(0.6, 2.0),
            # RugCheck catches most, but not all, rugs.
            safe=rng.random() > (0.7 if regime == "rug" else 0.15),
        )
        # Seed an hour of history so h1 stats are meaningful from the start.
        for i in range(60, 0, -1):
            tok.history.append((now - timedelta(minutes=i), tok.price * math.exp(-drift / 100 * i)))
        self.tokens[tok.mint] = tok

    def step(self, seconds: float) -> None:
        now = self.clock()
        while now >= self._next_launch:
            self._launch()
            self._next_launch += timedelta(minutes=self.launch_every_min)
        dt_min = seconds / 60
        for tok in self.tokens.values():
            if tok.rugged:
                continue
            if now >= tok.phase_until:
                if tok.regime == "rug":
                    tok.rugged = True
                    tok.liquidity *= 0.04
                    tok.price *= 0.08
                    tok.history.append((now, tok.price))
                    continue
                if tok.regime == "pump":  # the dump after the pump
                    tok.regime, tok.drift_per_min = "dump", -self.rng.uniform(0.5, 2.0)
                else:
                    tok.drift_per_min = self.rng.uniform(-0.6, 0.6)
                tok.phase_until = now + timedelta(minutes=self.rng.uniform(30, 150))
            shock = self.rng.gauss(0, tok.vol_per_min / 100 * math.sqrt(dt_min))
            ret = tok.drift_per_min / 100 * dt_min + shock
            tok.price *= math.exp(ret)
            tok.liquidity *= math.exp(ret / 2)  # pool deepens/shallows with price
            tok.history.append((now, tok.price))
        # Drop dead tokens to keep the universe bounded.
        cutoff = now - timedelta(hours=96)
        self.tokens = {m: t for m, t in self.tokens.items() if t.created > cutoff}

    def _snap(self, tok: _SimToken) -> TokenSnapshot:
        now = self.clock()
        ch5, ch1h, ch24 = tok.change_since(now, 5), tok.change_since(now, 60), tok.change_since(now, 1440)
        tx_h1 = 0 if tok.rugged else int(tok.liquidity / 120 * (1 + abs(ch1h) / 40))
        share_h1 = 0.5 + 0.25 * math.tanh(ch1h / 40)
        share_m5 = 0.5 + 0.25 * math.tanh(ch5 / 6)
        tx_m5 = tx_h1 // 12
        vol_h1 = 0.0 if tok.rugged else tok.liquidity * (0.3 + abs(ch1h) / 60)
        return TokenSnapshot(
            mint=tok.mint, symbol=tok.symbol, name=tok.symbol, pair_address=f"{tok.mint}-pair",
            dex_id="sim", quote_symbol="SOL", price_usd=tok.price, liquidity_usd=tok.liquidity,
            market_cap_usd=tok.price * self.SUPPLY, volume_m5=vol_h1 / 12, volume_h1=vol_h1, volume_h24=vol_h1 * 12,
            buys_m5=int(tx_m5 * share_m5), sells_m5=tx_m5 - int(tx_m5 * share_m5),
            buys_h1=int(tx_h1 * share_h1), sells_h1=tx_h1 - int(tx_h1 * share_h1),
            price_change_m5=ch5, price_change_h1=ch1h, price_change_h24=ch24,
            pair_created_at=tok.created, url=f"sim://{tok.mint}", fetched_at=now,
        )

    # -- MarketData interface ----------------------------------------------------------
    def discover(self) -> list[str]:
        return sorted(self.tokens, key=lambda m: self.tokens[m].created, reverse=True)

    def snapshots(self, mints: Iterable[str]) -> dict[str, TokenSnapshot]:
        return {m: self._snap(self.tokens[m]) for m in mints if m in self.tokens}

    def sol_price_usd(self) -> float:
        return 150.0


class SyntheticSafety:
    def __init__(self, market: SyntheticMarket) -> None:
        self.market = market

    def check(self, mint: str) -> SafetyReport:
        tok = self.market.tokens.get(mint)
        if tok is None or not tok.safe:
            return SafetyReport(ok=False, score=80.0, reasons=["rugcheck danger: synthetic risk flag"])
        return SafetyReport(ok=True, score=10.0, reasons=[])
