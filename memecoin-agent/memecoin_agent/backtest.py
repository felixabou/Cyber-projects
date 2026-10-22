"""Replay recorded market data through the real agent and report performance.

The replay is strictly point-in-time: at simulated time T the agent sees only
records stamped at or before T, discovery returns what discovery returned at
the last recorded poll, and a token with no sample in the last few intervals
looks unavailable, as it would live. The agent, strategy, risk rules and paper
fill model are the exact production code paths.

What a backtest cannot tell you: real slippage and MEV on your fills, failed or
delayed transactions, and whether your own buys would have moved the market.
Treat results as an upper bound.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Iterator

from .agent import Agent
from .config import Config
from .data.rugcheck import UNAVAILABLE
from .execution.paper import PaperExecutor
from .models import ClosedTrade, SafetyReport, TokenSnapshot, TradeRecord
from .portfolio import Portfolio
from .recording import record_time
from .simulation import SimClock

log = logging.getLogger(__name__)


class ReplayMarket:
    def __init__(self, records: Iterable[dict[str, Any]], stale_after_s: float = 90.0) -> None:
        self._it: Iterator[dict[str, Any]] = iter(records)
        self._pending: dict[str, Any] | None = None
        self.stale_after = timedelta(seconds=stale_after_s)
        self.now: datetime | None = None
        self.latest: dict[str, TokenSnapshot] = {}
        self.discovered: list[str] = []
        self.safety: dict[str, SafetyReport] = {}
        self.records_read = 0
        self.exhausted = False
        self._peek()

    def _peek(self) -> None:
        if self._pending is None and not self.exhausted:
            self._pending = next(self._it, None)
            self.exhausted = self._pending is None

    def next_time(self) -> datetime | None:
        self._peek()
        return None if self._pending is None else record_time(self._pending)

    def advance_to(self, now: datetime) -> None:
        self.now = now
        while (t := self.next_time()) is not None and t <= now:
            rec, self._pending = self._pending, None
            self.records_read += 1
            kind = rec.get("type")
            if kind == "snap":
                snap = TokenSnapshot.from_dict(rec)
                self.latest[snap.mint] = snap
            elif kind == "discover":
                self.discovered = list(rec.get("mints") or [])
            elif kind == "safety":
                self.safety[rec["mint"]] = SafetyReport(bool(rec["ok"]), rec.get("score"), list(rec.get("reasons") or []))

    # -- MarketData interface ----------------------------------------------------------
    def discover(self) -> list[str]:
        return list(self.discovered)

    def snapshots(self, mints: Iterable[str]) -> dict[str, TokenSnapshot]:
        out = {}
        for m in mints:
            s = self.latest.get(m)
            if s is not None and self.now is not None and self.now - s.fetched_at <= self.stale_after:
                out[m] = s
        return out

    def sol_price_usd(self) -> float:
        return 150.0  # paper fills are priced in USD; SOL price is not used in backtests


class ReplaySafety:
    """Serves the recorded RugCheck verdicts. No verdict means rejected, as live (fail closed)."""

    def __init__(self, market: ReplayMarket, assume_safe: bool = False) -> None:
        self.market = market
        self.assume_safe = assume_safe

    def check(self, mint: str) -> SafetyReport:
        rep = self.market.safety.get(mint)
        if rep is not None:
            return rep
        return SafetyReport(ok=self.assume_safe, score=None, reasons=[] if self.assume_safe else [UNAVAILABLE])


@dataclass
class BacktestResult:
    start: datetime
    end: datetime
    starting_equity: float
    portfolio: Portfolio
    trades: list[TradeRecord]
    closed: list[ClosedTrade]
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    records_read: int = 0

    @property
    def final_equity(self) -> float:
        return self.portfolio.equity_usd()

    def max_drawdown_pct(self) -> float:
        peak, worst = self.starting_equity, 0.0
        for _, eq in self.equity_curve:
            peak = max(peak, eq)
            worst = max(worst, (1 - eq / peak) * 100 if peak > 0 else 0.0)
        return worst

    def stats(self) -> dict[str, Any]:
        wins = [c for c in self.closed if c.pnl_usd > 0]
        losses = [c for c in self.closed if c.pnl_usd <= 0]
        gross_win = sum(c.pnl_usd for c in wins)
        gross_loss = -sum(c.pnl_usd for c in losses)
        n = len(self.closed)
        return {
            "period_hours": (self.end - self.start).total_seconds() / 3600,
            "round_trips": n,
            "open_at_end": len(self.portfolio.positions),
            "win_rate_pct": len(wins) / n * 100 if n else 0.0,
            "avg_win_pct": sum(c.pnl_pct for c in wins) / len(wins) if wins else 0.0,
            "avg_loss_pct": sum(c.pnl_pct for c in losses) / len(losses) if losses else 0.0,
            "profit_factor": gross_win / gross_loss if gross_loss > 0 else float("inf") if gross_win else 0.0,
            "realized_pnl_usd": self.portfolio.total_realized_pnl(),
            "fees_usd": sum(t.fee_usd for t in self.trades),
            "return_pct": (self.final_equity / self.starting_equity - 1) * 100,
            "max_drawdown_pct": self.max_drawdown_pct(),
            "avg_hold_min": sum(c.hold_minutes for c in self.closed) / n if n else 0.0,
            "exit_reasons": dict(Counter(c.exit_reason.split(" (")[0] for c in self.closed).most_common()),
        }


def run_backtest(
    cfg: Config,
    records: Iterable[dict[str, Any]],
    assume_safe: bool = False,
    close_at_end: bool = True,
) -> BacktestResult:
    cfg.execution.mode = "paper"
    cfg.agent.kill_switch_file = ""  # a STOP file in the working directory must not affect a backtest
    market = ReplayMarket(records, stale_after_s=max(3 * cfg.recorder.interval_s, 2 * cfg.agent.tick_seconds))
    start = market.next_time()
    if start is None:
        raise ValueError("Recording contains no records")
    clock = SimClock(start)
    pf = Portfolio(cash_usd=cfg.risk.starting_balance_usd)
    agent = Agent(cfg, market, ReplaySafety(market, assume_safe), PaperExecutor(cfg.execution), pf,
                  clock=clock, persist=False)
    curve: list[tuple[datetime, float]] = []
    market.advance_to(start)
    while True:
        agent.tick()
        curve.append((clock(), pf.equity_usd()))
        if market.exhausted:
            break
        clock.advance(cfg.agent.tick_seconds)
        market.advance_to(clock())
    if close_at_end and pf.positions:
        agent.close_all("end of backtest")
        curve.append((clock(), pf.equity_usd()))
    return BacktestResult(
        start=start, end=clock(), starting_equity=cfg.risk.starting_balance_usd, portfolio=pf,
        trades=list(pf.trades), closed=list(pf.closed), equity_curve=curve, records_read=market.records_read,
    )


def format_report(res: BacktestResult) -> str:
    s = res.stats()
    lines = [
        f"Backtest {res.start:%Y-%m-%d %H:%M} -> {res.end:%Y-%m-%d %H:%M} UTC "
        f"({s['period_hours']:.1f}h, {res.records_read:,} records)",
        f"  Return          {s['return_pct']:+.2f}%   (${res.starting_equity:,.2f} -> ${res.final_equity:,.2f})",
        f"  Max drawdown    {s['max_drawdown_pct']:.2f}%",
        f"  Round trips     {s['round_trips']}  (open at end: {s['open_at_end']})",
        f"  Win rate        {s['win_rate_pct']:.1f}%   avg win {s['avg_win_pct']:+.1f}%   avg loss {s['avg_loss_pct']:+.1f}%",
        f"  Profit factor   {s['profit_factor']:.2f}",
        f"  Realized PnL    ${s['realized_pnl_usd']:+,.2f}   fees ${s['fees_usd']:,.2f}",
        f"  Avg hold        {s['avg_hold_min']:.0f} min",
    ]
    if s["exit_reasons"]:
        lines.append("  Exits           " + ", ".join(f"{k}: {v}" for k, v in s["exit_reasons"].items()))
    if res.closed:
        best = sorted(res.closed, key=lambda c: c.pnl_pct)
        lines.append("  Worst / best    " + ", ".join(f"{c.symbol} {c.pnl_pct:+.0f}%" for c in best[:3])
                     + "  |  " + ", ".join(f"{c.symbol} {c.pnl_pct:+.0f}%" for c in best[-3:][::-1]))
    return "\n".join(lines)
