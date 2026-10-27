"""The trading loop: mark positions, run exits, scan for entries, persist."""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .data import MarketData, SafetyChecker
from .execution import Executor
from .models import TokenSnapshot, TradeRecord, utcnow
from .portfolio import Portfolio
from .risk import RiskManager
from .strategy import EntryEvaluation, ExitDecision, evaluate_entry, evaluate_exit, launch_reasons

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
        launch_scan: object | None = None,
        analytics: object | None = None,
        profiler: object | None = None,
        route_check: object | None = None,
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
        # Launch lane (strategy.launch_reasons): data.wallets.WalletScanner clears each candidate first.
        self.launch_scan = launch_scan
        # analytics.Analytics: per-buy details and after-sale tracking (state/entries.csv, after_exit.csv).
        self.analytics = analytics
        # data.profile.TokenProfiler: tech vs meme, and the fake-chart check, for each candidate.
        self.profiler = profiler
        self.kinds: dict[str, str] = {}  # mint -> "tech" / "meme", for the scanner view
        # data.route.SellRouteCheck: paper-mode honeypot check (live mode's executor does its own).
        self.route_check = route_check
        # Prices this agent has seen per candidate over the last 2 hours: catches buying right after a dump.
        self._seen_prices: dict[str, deque[tuple[datetime, float]]] = {}
        self.launch_qualified: dict[str, datetime] = {}  # mint -> when it first qualified
        self._launch_skips: dict[str, str] = {}  # mint -> last rejection logged
        self.ticks = 0
        # State surfaced by the dashboard (memecoin_agent/ui).
        self.entries_paused = False
        self.scan_when_blocked = False  # keep the scanner view fresh even while entries are blocked
        self.last_tick_at: datetime | None = None
        self.last_scan_at: datetime | None = None
        self.last_scan: list[dict] = []
        self.entry_block: str | None = None
        self.recent_trades: deque[TradeRecord] = deque(maxlen=500)
        self.trades_made = 0  # this session; lets listeners (the Telegram bot) find new trades
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
            if self.analytics is not None:
                try:
                    self.analytics.update(now, self._fetch)
                except Exception as exc:
                    log.warning("After-sale tracking failed: %s", exc)
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
        new = self.pf.trades[self._trades_mirrored:]
        self.recent_trades.extend(new)
        self.trades_made += len(new)
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
        if mint not in self.pf.positions and self.analytics is not None:
            self.analytics.watch(mint, pos.symbol, fill.price_usd, now, decision.reason)
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
        evals = []
        for s in self._fetch(mints).values():
            self._remember_price(s, now)
            ev = evaluate_entry(s, f, now)
            if not ev.passed and self.cfg.launch.enabled:
                ev = self._launch_lane(ev, now) or ev
            evals.append(ev)
        window = timedelta(minutes=self.cfg.launch.buy_window_minutes)
        self.launch_qualified = {m: t for m, t in self.launch_qualified.items() if now - t <= window}
        if len(self._launch_skips) > 2000:
            self._launch_skips.clear()
        if len(self.kinds) > 2000:
            self.kinds.clear()
        return sorted(evals, key=lambda e: (e.passed, e.lane == "launch", e.score), reverse=True)

    def _launch_lane(self, ev: EntryEvaluation, now: datetime) -> EntryEvaluation | None:
        s, lc = ev.snapshot, self.cfg.launch
        since = self.launch_qualified.get(s.mint)
        if since is not None and now - since > timedelta(minutes=lc.buy_window_minutes):
            since = None  # its buy window has passed; it would have to qualify afresh
        if launch_reasons(s, lc, self.cfg.filters, now, qualified=since is not None):
            return None
        if since is None:
            self.launch_qualified[s.mint] = now
            log.info("Launch lane: %s traded $%.0f in its first %.0fm (liq $%.0f) | %s", s.symbol, s.volume_h1,
                     s.age_minutes(now) or 0, s.liquidity_usd, s.url)
        return EntryEvaluation(snapshot=s, passed=True, score=ev.score, reasons=[], lane="launch")

    def _remember_price(self, s: TokenSnapshot, now: datetime) -> None:
        if s.price_usd <= 0:
            return
        seen = self._seen_prices.setdefault(s.mint, deque(maxlen=720))
        seen.append((now, s.price_usd))
        cutoff = now - timedelta(hours=2)
        while seen and seen[0][0] < cutoff:
            seen.popleft()
        if len(self._seen_prices) > 3000:  # forget coins not seen in the last 2 hours
            self._seen_prices = {m: q for m, q in self._seen_prices.items() if q and q[-1][0] >= cutoff}

    def _dumped(self, s: TokenSnapshot, now: datetime) -> str | None:
        limit = self.cfg.authenticity.max_drawdown_from_high_pct
        seen = self._seen_prices.get(s.mint)
        if limit is None or not self.cfg.authenticity.enabled or not seen or s.price_usd <= 0:
            return None
        high_at, high = max(seen, key=lambda x: x[1])
        drop = (1 - s.price_usd / high) * 100
        if drop > limit:
            return (f"unsafe: just dumped: {drop:.0f}% below the high it hit "
                    f"{(now - high_at).total_seconds() / 60:.0f}m ago (limit {limit:g}%)")
        return None

    def _needs_wallet_scan(self, ev: EntryEvaluation, now: datetime) -> bool:
        if ev.lane == "launch":
            return True
        f, s = self.cfg.filters, ev.snapshot
        age = s.age_minutes(now)
        return (age is not None and age < f.wallet_scan_below_age_minutes) or s.market_cap_usd < f.wallet_scan_below_market_cap_usd

    def _launch_check(self, snap: TokenSnapshot, lane: str = "launch") -> tuple[bool, str]:
        """(ok, note): the wallet scan for a launch-lane or early/small buy. The note is the trade
        journal reason, or why not to buy (yet)."""
        from .data.wallets import SCANNING

        if self.launch_scan is None:
            return False, "skipped: early and launch buys need the live wallet scan"
        qualified = self.launch_qualified.get(snap.mint) if lane == "launch" else None
        rep = self.launch_scan.check(snap.mint, qualified)
        if not rep.ok:
            if rep.reasons == [SCANNING]:
                return False, f"waiting: {SCANNING}"
            why = "; ".join(rep.reasons)
            if self._launch_skips.get(snap.mint) != why:  # once per verdict, not every scan of its window
                self._launch_skips[snap.mint] = why
                log.info("Skip %s (%s): %s", snap.symbol, "launch lane" if lane == "launch" else "wallet scan", why)
            return False, "unsafe: " + why
        res = self.launch_scan.result(snap.mint)
        summary = res.summary() if res else "wallet scan passed"
        return True, (f"launch lane: ${snap.volume_h1:,.0f} volume; {summary}" if lane == "launch"
                      else f"early buy: {summary}")

    def _profile_check(self, snap: TokenSnapshot, now: datetime) -> tuple[Any, str | None]:
        """(profile, why not): tech vs meme, then the fake-chart check. Free; before any paid check."""
        if self.profiler is None:
            return None, None
        from .data.profile import authenticity_reasons

        try:
            prof = self.profiler.profile(snap)
        except Exception as exc:  # the profile is extra evidence; never let it break a scan
            log.warning("Token profile failed for %s: %s", snap.symbol, exc)
            return None, None
        self.kinds[snap.mint] = prof.kind
        gate = self.guard
        if gate is not None and hasattr(gate, "kinds"):
            gate.kinds[snap.mint] = prof.kind  # memes get LarpCheck's free lookup only
        if self.cfg.authenticity.enabled:
            fake = authenticity_reasons(snap, prof, self.cfg.authenticity, snap.age_minutes(now))
            if fake:
                why = "; ".join(fake)
                kinds = "|".join(r.split(":")[0] for r in fake)  # numbers drift every scan; log each kind once
                if self._launch_skips.get(snap.mint) != kinds:
                    self._launch_skips[snap.mint] = kinds
                    log.info("Skip %s: looks manipulated: %s", snap.symbol, why)
                return prof, "unsafe: looks manipulated: " + why
        return prof, None

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
            # Free checks first (fake chart, then the wallet scan for launches and early or small buys):
            # they spare a LarpCheck credit on manipulated and bundled coins.
            dumped = self._dumped(snap, now)
            if dumped:
                outcomes[snap.mint] = dumped
                continue
            prof, fake = self._profile_check(snap, now)
            if fake:
                outcomes[snap.mint] = fake
                continue
            note = None
            if self._needs_wallet_scan(ev, now):
                ok, note = self._launch_check(snap, ev.lane)
                if not ok:
                    outcomes[snap.mint] = note
                    continue
            report = self.safety.check(snap.mint)
            if not report.ok:
                outcomes[snap.mint] = "unsafe: " + "; ".join(report.reasons)
                log.info("Skip %s: %s", snap.symbol, "; ".join(report.reasons))
                continue
            size = self.risk.position_size_usd(self.pf, self.executor.available_usd(self.pf))
            if size <= 0:
                log.info("Insufficient balance for a minimum-size position")
                break
            if self.route_check is not None and (why := self.route_check.check(snap.mint, size)):
                outcomes[snap.mint] = "unsafe: " + why
                log.info("Skip %s: %s", snap.symbol, why)
                continue
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
            pos = self.pf.open(snap, fill, now, ev.score, reason=note)
            pos.kind = prof.kind if prof else ""
            if self.analytics is not None:
                verdicts = getattr(self.guard, "last_verdicts", {}) or {}
                self.analytics.log_entry(snap, ev.lane, ev.score, size, fill.price_usd, now, verdicts.get(snap.mint),
                                         kind=pos.kind, holders=prof.holders if prof else None)
            outcomes[snap.mint] = "bought"
            log.info("BUY %s $%.2f @ %.10g | %s liq $%.0f mcap $%.0f 1h %+.0f%% | %s",
                     snap.symbol, size, fill.price_usd, note or f"score {ev.score:.2f}", snap.liquidity_usd,
                     snap.market_cap_usd, snap.price_change_h1, snap.url)
        self._record_scan(now, evals, outcomes)

    def _record_scan(self, now: datetime, evals: list[EntryEvaluation], outcomes: dict[str, str]) -> None:
        self.last_scan_at = now
        self.last_scan = [
            {
                "mint": e.snapshot.mint, "symbol": e.snapshot.symbol, "url": e.snapshot.url,
                "score": e.score, "passed": e.passed, "reasons": e.reasons, "lane": e.lane,
                "kind": self.kinds.get(e.snapshot.mint),
                "volume_h1": e.snapshot.volume_h1,
                "outcome": outcomes.get(e.snapshot.mint, ""),
                "liquidity_usd": e.snapshot.liquidity_usd, "market_cap_usd": e.snapshot.market_cap_usd,
                "price_change_m5": e.snapshot.price_change_m5, "price_change_h1": e.snapshot.price_change_h1,
                "buy_sell_ratio_h1": e.snapshot.buy_sell_ratio_h1, "age_minutes": e.snapshot.age_minutes(now),
            }
            for e in evals
        ]
