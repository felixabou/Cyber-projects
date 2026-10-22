"""The trading loop: mark positions, run exits, scan for entries, persist."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import Config
from .data import MarketData, SafetyChecker
from .execution import Executor
from .models import TokenSnapshot, utcnow
from .portfolio import Portfolio
from .risk import RiskManager
from .strategy import EntryEvaluation, ExitDecision, evaluate_entry, evaluate_exit

log = logging.getLogger(__name__)


class Agent:
    def __init__(
        self,
        cfg: Config,
        market: MarketData,
        safety: SafetyChecker,
        executor: Executor,
        portfolio: Portfolio,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
        persist: bool = True,
    ) -> None:
        self.cfg = cfg
        self.market = market
        self.safety = safety
        self.executor = executor
        self.pf = portfolio
        self.risk = RiskManager(cfg.risk)
        self.clock = clock
        self.sleep = sleep
        self.persist = persist
        self.ticks = 0

    # -- loop -----------------------------------------------------------------------
    def run(self, max_ticks: int | None = None) -> None:
        log.info("Agent started in %s mode. Equity $%.2f, %d open position(s).",
                 self.cfg.execution.mode.upper(), self.pf.equity_usd(), len(self.pf.positions))
        try:
            while max_ticks is None or self.ticks < max_ticks:
                started = time.monotonic()
                try:
                    self.tick()
                except Exception:  # one bad tick (API hiccup, bad data) must not kill the loop
                    log.exception("Tick failed")
                self.sleep(max(0.0, self.cfg.agent.tick_seconds - (time.monotonic() - started)))
        except KeyboardInterrupt:
            log.info("Interrupted; saving state.")
        finally:
            self.save()

    def tick(self) -> None:
        now = self.clock()
        try:
            self.executor.sync_cash(self.pf)
            self.risk.start_day(self.pf, now)
            self.risk.prune(self.pf, now)
            self.manage_positions(now)
            if self.ticks % self.cfg.agent.scan_every_ticks == 0:
                self.scan_and_enter(now)
        finally:
            self.ticks += 1
            self.save()

    def save(self) -> None:
        if not self.persist:
            return
        self.pf.save(self.cfg.agent.state_file)
        self.pf.flush_trades(self.cfg.agent.trades_csv)

    # -- exits ----------------------------------------------------------------------
    def _fetch(self, mints: list[str]) -> dict[str, TokenSnapshot]:
        if not mints:
            return {}
        try:
            return self.market.snapshots(mints)
        except Exception as exc:
            log.warning("Market data fetch failed: %s", exc)
            return {}

    def manage_positions(self, now: datetime) -> None:
        snaps = self._fetch(list(self.pf.positions))
        for mint, pos in list(self.pf.positions.items()):
            snap = snaps.get(mint)
            if snap is not None:
                self.pf.mark(snap)
            else:
                log.warning("No market data for held token %s", pos.symbol)
            decision = evaluate_exit(pos, snap, self.cfg.exits, now)
            if decision:
                self._sell(mint, decision, snap, now)

    def _sell(self, mint: str, decision: ExitDecision, snap: TokenSnapshot | None, now: datetime) -> None:
        pos = self.pf.positions[mint]
        try:
            fill = self.executor.sell(pos, decision.fraction, snap)
        except Exception as exc:
            log.error("SELL %s failed (%s): %s", pos.symbol, decision.reason, exc)
            return
        if decision.tp_level is not None:
            pos.tp_levels_hit.append(decision.tp_level)
        realized = self.pf.reduce(mint, decision.fraction, fill, now, decision.reason,
                                  cooldown_until=self.risk.cooldown_until(now))
        log.info("SELL %s %.0f%% -> $%.2f | %s | realized %+.2f | equity $%.2f",
                 pos.symbol, decision.fraction * 100, fill.usd_value, decision.reason, realized, self.pf.equity_usd())

    def close_all(self, reason: str = "manual close-all") -> None:
        now = self.clock()
        snaps = self._fetch(list(self.pf.positions))
        for mint in list(self.pf.positions):
            self._sell(mint, ExitDecision(1.0, reason), snaps.get(mint), now)
        self.save()

    # -- entries --------------------------------------------------------------------
    def evaluate_candidates(self, now: datetime) -> list[EntryEvaluation]:
        try:
            discovered = self.market.discover()
        except Exception as exc:
            log.warning("Discovery failed: %s", exc)
            discovered = []
        f = self.cfg.filters
        mints = [m for m in dict.fromkeys([*f.watchlist, *discovered])
                 if m not in f.blacklist and not self.risk.token_blocked(self.pf, m, now)]
        mints = mints[: self.cfg.agent.max_candidates_per_scan]
        evals = [evaluate_entry(s, f, now) for s in self._fetch(mints).values()]
        return sorted(evals, key=lambda e: (e.passed, e.score), reverse=True)

    def scan_and_enter(self, now: datetime) -> None:
        if self.cfg.agent.kill_switch_file and Path(self.cfg.agent.kill_switch_file).exists():
            log.info("Kill switch file '%s' present: not opening positions.", self.cfg.agent.kill_switch_file)
            return
        if block := self.risk.entry_block_reason(self.pf, now):
            log.info("Entries paused: %s", block)
            return

        evals = self.evaluate_candidates(now)
        passed = [e for e in evals if e.passed]
        log.info("Scan: %d candidates, %d passed filters", len(evals), len(passed))
        for ev in passed:
            snap = ev.snapshot
            if block := self.risk.entry_block_reason(self.pf, now):
                log.info("Entries paused: %s", block)
                break
            report = self.safety.check(snap.mint)
            if not report.ok:
                log.info("Skip %s: %s", snap.symbol, "; ".join(report.reasons))
                continue
            size = self.risk.position_size_usd(self.pf, self.executor.available_usd(self.pf))
            if size <= 0:
                log.info("Insufficient balance for a minimum-size position")
                break
            if reason := self.executor.preflight(snap, size):
                log.info("Skip %s: %s", snap.symbol, reason)
                continue
            try:
                fill = self.executor.buy(snap, size)
            except Exception as exc:
                log.error("BUY %s failed: %s", snap.symbol, exc)
                continue
            self.pf.open(snap, fill, now, ev.score)
            log.info("BUY %s $%.2f @ %.10g | score %.2f liq $%.0f mcap $%.0f 1h %+.0f%% | %s",
                     snap.symbol, size, fill.price_usd, ev.score, snap.liquidity_usd, snap.market_cap_usd,
                     snap.price_change_h1, snap.url)
