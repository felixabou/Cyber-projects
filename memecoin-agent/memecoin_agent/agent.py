"""The trading loop: mark positions, run exits, scan for entries, persist."""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import Config
from .data import MarketData, SafetyChecker
from .execution import Executor
from .models import TokenSnapshot, TradeRecord, utcnow
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
        persist: bool = True,
        guard: object | None = None,
    ) -> None:
        self.cfg = cfg
        self.market = market
        self.safety = safety
        self.executor = executor
        self.pf = portfolio
        self.risk = RiskManager(cfg.risk)
        self.clock = clock
        self.persist = persist
        # Optional held-position reviewer (LarpCheckGate.review_position): exits on outside verdicts.
        self.guard = guard
        self.ticks = 0
        # State surfaced by the dashboard (memecoin_agent/ui).
        self.entries_paused = False
        self.scan_when_blocked = False  # keep the scanner view fresh even while entries are blocked
        self.last_tick_at: datetime | None = None
        self.last_scan_at: datetime | None = None
        self.last_scan: list[dict] = []
        self.entry_block: str | None = None
        self.recent_trades: deque[TradeRecord] = deque(maxlen=500)
        self.tick_errors = 0
        self._trades_mirrored = 0

    # -- loop (driven by ui.runner.AgentRunner for both `run` and `ui`) -------------
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
            self.last_tick_at = now
            self.save()

    def save(self) -> None:
        # Mirror new trades for the dashboard; pf.trades is cleared once flushed to CSV.
        if self._trades_mirrored > len(self.pf.trades):  # someone else cleared it
            self._trades_mirrored = 0
        self.recent_trades.extend(self.pf.trades[self._trades_mirrored:])
        self._trades_mirrored = len(self.pf.trades)
        if not self.persist:
            return
        self.pf.save(self.cfg.agent.state_file)
        self.pf.flush_trades(self.cfg.agent.trades_csv)
        self._trades_mirrored = len(self.pf.trades)

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
            if pos.dust:
                # Dust is left alone by the strategy; it only goes if it stops qualifying (or by hand).
                dust_exit = None
                if self.guard is not None:
                    try:
                        dust_exit = self.guard.review_dust(pos)
                    except Exception as exc:
                        log.warning("Dust review failed for %s: %s", pos.symbol, exc)
                if dust_exit:
                    self._sell(mint, dust_exit, snap, now)
                continue
            decision = evaluate_exit(pos, snap, self.cfg.exits, now)
            if decision is None and self.guard is not None:
                try:
                    decision = self.guard.review_position(pos)
                except Exception as exc:  # an outside service must never break exit handling
                    log.warning("Position review failed for %s: %s", pos.symbol, exc)
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
        if mint in self.pf.positions:
            if decision.larp is not None:
                self.pf.positions[mint].larp = decision.larp
            if decision.make_dust:
                self.pf.positions[mint].dust = True
                log.info("Keeping %.0f%% of %s as dust (left alone until closed manually)",
                         (1 - decision.fraction) * 100, pos.symbol)

    def close_position(self, mint: str, reason: str = "manual close") -> bool:
        if mint not in self.pf.positions:
            return False
        self._sell(mint, ExitDecision(1.0, reason), self._fetch([mint]).get(mint), self.clock())
        self.save()
        return mint not in self.pf.positions

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

    def _entry_block_reason(self, now: datetime) -> str | None:
        if self.entries_paused:
            return "paused from the dashboard"
        if self.cfg.agent.kill_switch_file and Path(self.cfg.agent.kill_switch_file).exists():
            return f"kill switch file '{self.cfg.agent.kill_switch_file}' present"
        return self.risk.entry_block_reason(self.pf, now)

    def _set_entry_block(self, block: str | None) -> None:
        # Log transitions only: a block can persist for hours and would otherwise repeat every scan.
        if block != self.entry_block:
            if block:
                log.info("New entries blocked: %s", block)
            else:
                log.info("New entries allowed again")
        self.entry_block = block

    def scan_and_enter(self, now: datetime) -> None:
        self._set_entry_block(self._entry_block_reason(now))
        if self.entry_block:
            if self.scan_when_blocked:
                self._record_scan(now, self.evaluate_candidates(now), {})
            return

        evals = self.evaluate_candidates(now)
        passed = [e for e in evals if e.passed]
        log.info("Scan: %d candidates, %d passed filters", len(evals), len(passed))
        outcomes: dict[str, str] = {}  # mint -> what happened after passing filters
        for ev in passed:
            snap = ev.snapshot
            if block := self._entry_block_reason(now):
                self._set_entry_block(block)
                break
            report = self.safety.check(snap.mint)
            if not report.ok:
                outcomes[snap.mint] = "unsafe: " + "; ".join(report.reasons)
                log.info("Skip %s: %s", snap.symbol, "; ".join(report.reasons))
                continue
            size = self.risk.position_size_usd(self.pf, self.executor.available_usd(self.pf))
            if size <= 0:
                log.info("Insufficient balance for a minimum-size position")
                break
            if reason := self.executor.preflight(snap, size):
                outcomes[snap.mint] = f"skipped: {reason}"
                log.info("Skip %s: %s", snap.symbol, reason)
                continue
            try:
                fill = self.executor.buy(snap, size)
            except Exception as exc:
                outcomes[snap.mint] = f"buy failed: {exc}"
                log.error("BUY %s failed: %s", snap.symbol, exc)
                continue
            self.pf.open(snap, fill, now, ev.score)
            outcomes[snap.mint] = "bought"
            log.info("BUY %s $%.2f @ %.10g | score %.2f liq $%.0f mcap $%.0f 1h %+.0f%% | %s",
                     snap.symbol, size, fill.price_usd, ev.score, snap.liquidity_usd, snap.market_cap_usd,
                     snap.price_change_h1, snap.url)
        self._record_scan(now, evals, outcomes)

    def _record_scan(self, now: datetime, evals: list[EntryEvaluation], outcomes: dict[str, str]) -> None:
        self.last_scan_at = now
        self.last_scan = [
            {
                "mint": e.snapshot.mint, "symbol": e.snapshot.symbol, "url": e.snapshot.url,
                "score": e.score, "passed": e.passed, "reasons": e.reasons,
                "outcome": outcomes.get(e.snapshot.mint, ""),
                "liquidity_usd": e.snapshot.liquidity_usd, "market_cap_usd": e.snapshot.market_cap_usd,
                "price_change_m5": e.snapshot.price_change_m5, "price_change_h1": e.snapshot.price_change_h1,
                "buy_sell_ratio_h1": e.snapshot.buy_sell_ratio_h1, "age_minutes": e.snapshot.age_minutes(now),
            }
            for e in evals
        ]
