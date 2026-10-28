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
        multichain: object | None = None,
        copy_tracker: object | None = None,
        longterm: object | None = None,
        chain_safety: object | None = None,
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
        # Tokens on other chains: data.dexscreener.DexScreenerClient (snapshots_on), plus which chain
        # each non-Solana token lives on (held positions carry their own).
        self.multichain = multichain
        self.chains: dict[str, str] = {}
        # Copy lane (data.copytrade.CopyTracker): low caps bought only when a tracked wallet buys them.
        self.copy_tracker = copy_tracker
        # Long-term lane (data.longterm.LongTermScanner): old, worn-down coins that still trade daily.
        self.longterm = longterm
        # data.evm_safety.ChainSafety: honeypot and tax checks for tokens on other chains.
        self.chain_safety = chain_safety
        self._longterm_exits: Any = None
        self.lane_outcomes: deque[dict[str, Any]] = deque(maxlen=100)  # copy / long-term decisions, newest last
        self._lane_last: dict[str, str] = {}  # mint -> last outcome noted (log each change once)
        self._next_longterm: datetime | None = None
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
            for lane in (self.enter_copy_signals, self.enter_longterm):  # every tick: copy signals go stale fast
                try:
                    lane(now)
                except Exception as exc:
                    log.warning("%s failed: %s", lane.__name__, exc)
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
    def chain_of(self, mint: str) -> str:
        pos = self.pf.positions.get(mint)
        return pos.chain if pos is not None else self.chains.get(mint, "solana")

    def _fetch(self, mints: list[str]) -> dict[str, TokenSnapshot]:
        """Snapshots for Solana mints from the market (with its discovery wrappers), and for tokens on
        other chains (copy trades, long-term holds, manual trades) from DexScreener directly."""
        if not mints:
            return {}
        by_chain: dict[str, list[str]] = {}
        for m in mints:
            by_chain.setdefault(self.chain_of(m), []).append(m)
        out: dict[str, TokenSnapshot] = {}
        for chain, group in by_chain.items():
            try:
                if chain == "solana":
                    out.update(self.market.snapshots(group))
                elif self.multichain is not None:
                    out.update(self.multichain.snapshots_on(chain, group))
            except Exception as exc:
                log.warning("Market data fetch failed (%s): %s", chain, exc)
        return out

    def exits_for(self, pos: Any) -> Any:
        """Exit settings for a position: long-term holds get their own stop and profit line, no time stop."""
        if pos.lane != "longterm":
            return self.cfg.exits
        if self._longterm_exits is None:
            from dataclasses import replace

            lt = self.cfg.longterm
            self._longterm_exits = replace(self.cfg.exits, style="let_it_run", hard_stop_pct=lt.hard_stop_pct,
                                           secure_at_pct=lt.secure_at_pct, dip_watch_pct=None, max_hold_minutes=None)
        return self._longterm_exits

    def manage_positions(self, now: datetime) -> None:
        snaps = self._fetch(list(self.pf.positions))
        for mint, pos in list(self.pf.positions.items()):
            snap = snaps.get(mint)
            if snap is not None:
                self.pf.mark(snap)
            else:
                log.warning("No market data for held token %s", pos.symbol)
            if not pos.managed:
                continue  # a manual trade: only you sell it (dashboard or Telegram)
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
            had_note = pos.review_note
            decision = evaluate_exit(pos, snap, self.exits_for(pos), now, self.cfg.execution.paper_fee_pct)
            if pos.review_note != had_note and not decision:
                log.info("Review %s: %s", pos.symbol, pos.review_note)
            if decision is None and self.guard is not None and pos.chain == "solana":
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
        if decision.secure:
            # What's left is house money: no stop loss or time stop, reviewed against its own peak.
            pos.secured_at, pos.runner_peak_usd = now, snap.price_usd if snap else pos.last_price_usd
            pos.next_review_at = now + timedelta(minutes=self.exits_for(pos).runner_review_minutes)
            pos.dip_since = None
            pos.review_note = "initials out; the rest runs"
        # A long-term hold that was sold isn't rebought while the scanner still likes it: a week off.
        cooldown = now + timedelta(days=7) if pos.lane == "longterm" else self.risk.cooldown_until(now)
        realized = self.pf.reduce(mint, decision.fraction, fill, now, decision.reason, cooldown_until=cooldown)
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

    def _entry_block_reason(self, now: datetime, lane: str = "momentum") -> str | None:
        if self.entries_paused:
            return "paused from the dashboard"
        if self.cfg.agent.kill_switch_file and Path(self.cfg.agent.kill_switch_file).exists():
            return f"kill switch file '{self.cfg.agent.kill_switch_file}' present"
        if lane == "longterm":  # its own slots, and no hourly entry limit
            if self.risk.daily_loss_halted(self.pf, now):
                return f"daily loss limit ({self.cfg.risk.max_daily_loss_pct:.0f}%) hit"
            return None
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

    # -- copy lane, long-term lane, manual trades ---------------------------------------------------
    def _lane_note(self, now: datetime, lane: str, snap_or_symbol: Any, chain: str, mint: str, outcome: str) -> None:
        symbol = getattr(snap_or_symbol, "symbol", snap_or_symbol) or mint[:8]
        if self._lane_last.get(mint) == outcome:
            for note in self.lane_outcomes:  # same verdict again: refresh it, don't repeat it
                if note["mint"] == mint and note["outcome"] == outcome:
                    note["at"] = now.isoformat()
            return
        self._lane_last[mint] = outcome
        if len(self._lane_last) > 5000:
            self._lane_last = {mint: outcome}
        self.lane_outcomes.append({"at": now.isoformat(), "lane": lane, "symbol": symbol, "chain": chain,
                                   "mint": mint, "outcome": outcome})
        if outcome == "bought" or outcome.startswith(("unsafe", "skipped")):
            log.info("%s lane: %s (%s): %s", lane.capitalize(), symbol, chain, outcome)

    def token_checks(self, snap: TokenSnapshot, size: float, now: datetime, lane: str) -> str | None:
        """The checks every non-momentum buy gets, by chain. None: buy it."""
        if snap.chain != "solana":
            if self.cfg.execution.mode == "live":
                return f"skipped: live trading is Solana-only ({snap.chain} is paper-only)"
            if self.chain_safety is None:
                return f"unsafe: no honeypot check available for {snap.chain}"
            rep = self.chain_safety.check(snap.chain, snap.mint)
            return None if rep.ok else "unsafe: " + "; ".join(rep.reasons)
        if self.profiler is not None:
            try:
                prof = self.profiler.profile(snap)
                flags = [f"honeypot risk: {f}" for f in prof.mint_flags]
                if prof.transfer_fee_pct > self.cfg.authenticity.max_transfer_fee_pct:
                    flags.append(f"honeypot risk: {prof.transfer_fee_pct:g}% transfer fee")
                if flags:
                    return "unsafe: " + "; ".join(flags)
                self.kinds[snap.mint] = prof.kind
                if self.guard is not None and hasattr(self.guard, "kinds"):
                    self.guard.kinds[snap.mint] = prof.kind
            except Exception as exc:
                log.warning("Token profile failed for %s: %s", snap.symbol, exc)
        if lane == "copy":
            age = snap.age_minutes(now)
            f = self.cfg.filters
            if (age is not None and age < f.wallet_scan_below_age_minutes) or snap.market_cap_usd < f.wallet_scan_below_market_cap_usd:
                ok, note = self._launch_check(snap, "copy")
                if not ok:
                    return note
        report = self.safety.check(snap.mint)
        if not report.ok:
            return "unsafe: " + "; ".join(report.reasons)
        if self.route_check is not None and (why := self.route_check.check(snap.mint, size)):
            return "unsafe: " + why
        if reason := self.executor.preflight(snap, size):
            return f"skipped: {reason}"
        return None

    def _open(self, snap: TokenSnapshot, size: float, now: datetime, lane: str, reason: str,
              managed: bool = True) -> Any:
        fill = self.executor.buy(snap, size)
        pos = self.pf.open(snap, fill, now, 0.0, reason=reason, lane=lane, managed=managed)
        pos.kind = self.kinds.get(snap.mint, "")
        if self.analytics is not None and snap.chain == "solana":
            self.analytics.log_entry(snap, lane, 0.0, size, fill.price_usd, now, None, kind=pos.kind)
        log.info("BUY %s (%s, %s lane) $%.2f @ %.10g | %s | liq $%.0f mcap $%.0f | %s", snap.symbol, snap.chain,
                 lane, size, fill.price_usd, reason, snap.liquidity_usd, snap.market_cap_usd, snap.url)
        return pos

    def enter_copy_signals(self, now: datetime) -> None:
        """Copy lane: low caps a tracked wallet just bought (data.copytrade)."""
        tracker = self.copy_tracker
        if tracker is None:
            return
        from .data.dexscreener import GMGN_TO_DEX, norm_address

        cc, f = self.cfg.copy, self.cfg.filters
        pending = []
        for sig in tracker.signals():
            chain = GMGN_TO_DEX.get(sig.chain, sig.chain)
            mint = norm_address(chain, sig.token)
            if self.risk.token_blocked(self.pf, mint, now) or mint in f.blacklist:
                tracker.consume(sig.chain, sig.token)
                continue
            self.chains[mint] = chain
            pending.append((sig, chain, mint))
        if not pending:
            return
        if block := self._entry_block_reason(now, "copy"):
            sig, chain, mint = pending[0]
            self._lane_note(now, "copy", sig.symbol, chain, mint, f"waiting: {block}")
            return
        snaps = self._fetch([mint for _, _, mint in pending])  # one lookup per chain for every signal
        for sig, chain, mint in pending:
            if block := self._entry_block_reason(now, "copy"):
                self._lane_note(now, "copy", sig.symbol, chain, mint, f"waiting: {block}")
                return
            snap = snaps.get(mint)
            if snap is None:
                self._lane_note(now, "copy", sig.symbol, chain, mint, "waiting: no DexScreener pair yet")
                continue
            why = None
            if snap.market_cap_usd >= f.min_market_cap_usd:
                why = f"skipped: not a low cap (${snap.market_cap_usd:,.0f}; the momentum lane judges it)"
            elif snap.market_cap_usd < cc.min_market_cap_usd:
                why = f"skipped: market cap ${snap.market_cap_usd:,.0f} < ${cc.min_market_cap_usd:,.0f}"
            elif snap.liquidity_usd < cc.min_liquidity_usd:
                why = f"skipped: liquidity ${snap.liquidity_usd:,.0f} < ${cc.min_liquidity_usd:,.0f}"
            size = min(cc.position_usd, self.executor.available_usd(self.pf)) if cc.position_usd \
                else self.risk.position_size_usd(self.pf, self.executor.available_usd(self.pf))
            if why is None and size <= 0:
                why = "skipped: not enough cash"
            why = why or self.token_checks(snap, size, now, "copy")
            if why:
                self._lane_note(now, "copy", snap, chain, mint, why)
                if not why.startswith("waiting"):
                    tracker.consume(sig.chain, sig.token)
                continue
            wallets = ", ".join(w[:6] + "…" for w in sig.wallets)
            try:
                self._open(snap, size, now, "copy", f"copy: {len(sig.wallets)} tracked wallet(s) bought "
                                                    f"${sig.usd:,.0f} ({wallets})")
                self._lane_note(now, "copy", snap, chain, mint, "bought")
            except Exception as exc:
                log.error("BUY %s (copy) failed: %s", snap.symbol, exc)
                self._lane_note(now, "copy", snap, chain, mint, f"buy failed: {exc}")
            tracker.consume(sig.chain, sig.token)

    def enter_longterm(self, now: datetime) -> None:
        """Long-term lane: buy qualified coins (data.longterm) while it has free slots."""
        scanner = self.longterm
        if scanner is None or (self._next_longterm is not None and now < self._next_longterm):
            return
        self._next_longterm = now + timedelta(minutes=1)  # its candidates change every few hours, not seconds
        lt = self.cfg.longterm
        held = sum(1 for p in self.pf.positions.values() if p.lane == "longterm")
        for v in scanner.qualified():
            if held >= lt.max_positions:
                return
            if self.risk.token_blocked(self.pf, v.address, now):
                continue
            if block := self._entry_block_reason(now, "longterm"):
                self._lane_note(now, "longterm", v.symbol, v.chain, v.address, f"waiting: {block}")
                return
            self.chains[v.address] = v.chain
            snap = self._fetch([v.address]).get(v.address)
            if snap is None:
                continue
            size = min(lt.position_usd, self.executor.available_usd(self.pf))
            why = "skipped: not enough cash" if size < self.cfg.risk.min_position_usd else \
                self.token_checks(snap, size, now, "longterm")
            if why:
                self._lane_note(now, "longterm", snap, v.chain, v.address, why)
                continue
            facts = v.facts
            try:
                self._open(snap, size, now, "longterm",
                           f"long-term: {facts.get('age_days', '?')} days old, {facts.get('below_high_pct', '?')}% below "
                           f"its high, {facts.get('holders', '?')} holders ({facts.get('large_holders', '?')} large), "
                           f"{snap.buys_h24} buys in 24h")
                self._lane_note(now, "longterm", snap, v.chain, v.address, "bought")
                held += 1
            except Exception as exc:
                log.error("BUY %s (long-term) failed: %s", snap.symbol, exc)

    def lookup(self, query: str) -> list[TokenSnapshot]:
        if self.multichain is None or not hasattr(self.multichain, "lookup"):
            raise RuntimeError("Token search isn't available in this mode")
        return self.multichain.lookup(query)

    def snapshot_for(self, chain: str, address: str) -> TokenSnapshot:
        from .data.dexscreener import norm_address

        chain = (chain or "solana").strip().lower()
        mint = norm_address(chain, address)
        if mint not in self.pf.positions:  # a held token keeps the chain it was bought on
            self.chains[mint] = chain
        snap = self._fetch([mint]).get(mint)
        if snap is None:
            raise ValueError(f"No market data for that token on {chain}")
        return snap

    def manual_check(self, snap: TokenSnapshot, usd: float) -> list[str]:
        """Warnings for a manual buy (shown, never blocking: it's your call)."""
        if snap.chain != "solana":
            if self.chain_safety is None:
                return [f"no honeypot check available for {snap.chain}"]
            return list(self.chain_safety.check(snap.chain, snap.mint).reasons)
        warnings: list[str] = []
        if self.profiler is not None:
            try:
                prof = self.profiler.profile(snap)
                warnings += [f"honeypot risk: {f}" for f in prof.mint_flags]
            except Exception:
                pass
        if self.route_check is not None and (why := self.route_check.check(snap.mint, usd)):
            warnings.append(why)
        return warnings

    def manual_buy(self, chain: str, address: str, usd: float, managed: bool, source: str = "dashboard") -> str:
        """Paper-buy any token on any chain DexScreener covers. managed: the agent's exits apply to it;
        otherwise only you sell it."""
        from .data.dexscreener import norm_address

        if self.cfg.execution.mode != "paper":
            raise ValueError("Manual trades are paper-only")
        chain = (chain or "solana").strip().lower()
        mint = norm_address(chain, address)
        if mint in self.pf.positions:
            raise ValueError(f"Already holding {self.pf.positions[mint].symbol}; sell it or switch who trades it")
        if not usd or usd <= 0:
            raise ValueError("Enter an amount in USD")
        if usd > self.executor.available_usd(self.pf):
            raise ValueError(f"Only ${self.executor.available_usd(self.pf):,.2f} of paper cash available")
        now = self.clock()
        snap = self.snapshot_for(chain, mint)
        warnings = self.manual_check(snap, usd)
        who = "the agent trades it" if managed else "you trade it"
        self._open(snap, usd, now, "manual", f"manual buy ({source}; {who})" +
                   (f"; warnings: {'; '.join(warnings)}" if warnings else ""), managed=managed)
        self.save()
        return f"Bought ${usd:,.2f} of {snap.symbol} on {chain}; {who}" + \
            (f". Warnings: {'; '.join(warnings)}" if warnings else "")

    def sell_part(self, mint: str, fraction: float, reason: str) -> bool:
        if mint not in self.pf.positions:
            raise ValueError("No open position for that token")
        if not 0 < fraction <= 1:
            raise ValueError("Sell between 1% and 100%")
        self._sell(mint, ExitDecision(1.0 if fraction >= 0.999 else fraction, reason),
                   self._fetch([mint]).get(mint), self.clock())
        self.save()
        return True

    def set_managed(self, mint: str, managed: bool) -> str:
        pos = self.pf.positions.get(mint)
        if pos is None:
            raise ValueError("No open position for that token")
        pos.managed = bool(managed)
        if managed:  # fresh review timers: the agent picks it up from where it is now
            pos.next_review_at = pos.dip_since = None
        log.info("%s: %s", pos.symbol, "the agent trades it" if managed else "you trade it (no automatic sells)")
        self.save()
        return f"{pos.symbol}: {'the agent trades it' if managed else 'you trade it'}"

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
