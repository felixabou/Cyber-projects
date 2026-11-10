"""memecoin-agent: run `memecoin-agent` (or `memecoin-agent ui`) to trade with the dashboard.

Commands: ui, run, scan, status, report, close-all, record, backtest, demo.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

from .agent import Agent
from .config import Config, apply_override, load_config
from .data.dexscreener import DexScreenerClient
from .data.pumpportal import FeedAugmentedMarket, PumpPortalFeed
from .data.rugcheck import NoSafetyCheck, RugCheckClient
from .execution.paper import PaperExecutor
from .models import utcnow
from .portfolio import Portfolio
from .statelock import StateLock, StateLocked

LIVE_CONFIRM_ENV = "MEMECOIN_AGENT_CONFIRM_LIVE"

log = logging.getLogger("memecoin_agent")


def setup_logging(log_file: str | None, verbose: bool = False) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    # A coin name the console can't print (Windows code pages) must not raise inside a log call.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_gmgn(cfg: Config):
    """A GMGN client if GMGN is enabled and a key is configured, else None (with one log line)."""
    uses = cfg.gmgn.discovery or cfg.gmgn.safety or cfg.copy.enabled or cfg.longterm.discovery
    if not (cfg.gmgn.enabled and uses):
        return None
    from .data.gmgn import GmgnClient, load_api_key

    key = load_api_key(cfg.gmgn)
    if not key:
        log.info("GMGN not configured (set GMGN_API_KEY or gmgn.api_key_file); running without it"
                 "%s", " (the copy lane needs it)" if cfg.copy.enabled else "")
        return None
    return GmgnClient(key, cfg.gmgn.host, rate_per_s=cfg.gmgn.rate_per_s)


def build_market(cfg: Config, with_feed: bool = True, gmgn=None):
    """DexScreener market data. Discovery order: pump.fun graduations (launch feed), GMGN trending,
    then DexScreener's own lists across the configured chains. Prices always come from DexScreener."""
    base = DexScreenerClient(cfg.filters.quote_symbols, discovery_chains=cfg.lowcap.chains)
    if gmgn is not None and cfg.gmgn.discovery:
        from dataclasses import replace

        from .data.gmgn import ExtraDiscoveryMarket, GmgnDiscovery

        window = cfg.filters
        if cfg.launch.enabled:  # trending launches younger than the momentum lane's minimum age too
            window = replace(window, min_age_minutes=1,
                             min_liquidity_usd=min(window.min_liquidity_usd, cfg.launch.min_liquidity_usd))
        if cfg.lowcap.enabled:
            window = replace(window,
                             min_market_cap_usd=min(window.min_market_cap_usd, cfg.lowcap.min_market_cap_usd),
                             min_liquidity_usd=min(window.min_liquidity_usd, cfg.lowcap.min_liquidity_usd),
                             min_age_minutes=min(window.min_age_minutes, cfg.lowcap.min_age_minutes))
        gmgn_chains = [str(c).lower() for c in cfg.gmgn.discovery_chains]
        base = ExtraDiscoveryMarket(base, GmgnDiscovery(cfg.gmgn, window, gmgn, chains=gmgn_chains))
        log.info("GMGN discovery on: %s trending (%s), every %.0fs", ", ".join(gmgn_chains),
                 cfg.gmgn.trending_interval, cfg.gmgn.discover_every_s)
    if cfg.discovery.enabled:
        from .data.gmgn import ExtraDiscoveryMarket
        from .data.trending import TrendingDiscovery

        base = ExtraDiscoveryMarket(base, TrendingDiscovery(cfg.discovery, cfg.filters))
        log.info("Trending discovery on: Jupiter (%s) and GeckoTerminal trending pools, one list at a time, a round every %.0fs",
                 ", ".join(cfg.discovery.jupiter_lists), cfg.discovery.refresh_s)
    fc = cfg.feeds
    if not with_feed or not (fc.pumpportal_migrations or fc.pumpportal_new_tokens):
        return base, None
    feed = PumpPortalFeed(
        url=fc.pumpportal_url, migrations=fc.pumpportal_migrations, new_tokens=fc.pumpportal_new_tokens,
        max_age_s=cfg.filters.max_age_hours * 3600, max_mints=fc.max_buffered_mints, buffer_file=fc.buffer_file,
    )
    return FeedAugmentedMarket(base, feed, fc.evict_below_liquidity_usd,
                               fast_limit=cfg.agent.max_candidates_per_scan if cfg.sniper.enabled else 0,
                               launch_window_s=cfg.sniper.max_age_minutes * 60), feed


def build_safety(cfg: Config, with_larpcheck: bool = True, gmgn=None):
    """The agent's pre-buy checks, cheapest first: RugCheck, Jupiter, GMGN, then LarpCheck (which can
    spend credits, so it only sees coins the others passed). The recorder passes with_larpcheck=False
    and no GMGN client: it vets hundreds of tokens, and the later checks are for the few the agent would
    buy."""
    from .data.larpcheck import CompositeSafety, LarpCheckClient, LarpCheckGate, RequireLarpCheck, load_api_key

    f = cfg.filters
    rug = (RugCheckClient(max_score=f.max_rugcheck_score, reject_danger=f.reject_danger_risks,
                          min_lp_locked_pct=f.min_lp_locked_pct) if f.require_rugcheck else NoSafetyCheck())
    checks: list = [rug]
    if with_larpcheck and cfg.jupiter.enabled:  # free, so before GMGN's rate budget
        from .data.jupiter_tokens import JupiterTokens, JupiterTokenSafety

        checks.append(JupiterTokenSafety(cfg.jupiter, JupiterTokens()))
        log.info("Jupiter token check on (coin factories, bot volume, shells, wash trading)")
    if gmgn is not None and cfg.gmgn.safety:
        from .data.gmgn import GmgnSafety

        checks.append(GmgnSafety(cfg.gmgn, gmgn))
        log.info("GMGN safety check on (rug ratio, wash trading, holder concentration, bundlers/insiders)")
    if not (with_larpcheck and cfg.larpcheck.enabled):
        return CompositeSafety(*checks) if len(checks) > 1 else rug
    key = load_api_key(cfg.larpcheck)
    if not key:
        if cfg.larpcheck.require_verdict:
            log.error("LarpCheck verdicts are required but no API key was found (set LARPCHECK_API_KEY or "
                      "larpcheck.api_key_file): the agent will open no positions; exits still run")
            return CompositeSafety(*checks, RequireLarpCheck("no LarpCheck API key configured"))
        log.warning("LarpCheck is enabled but no API key was found; continuing without it")
        return CompositeSafety(*checks) if len(checks) > 1 else rug
    gate = LarpCheckGate(cfg.larpcheck, LarpCheckClient(key, cfg.larpcheck.url))
    log.info("LarpCheck gate on: rejects %s and coin stamps %s; unchecked coins: %s%s",
             "/".join(cfg.larpcheck.reject_verdicts), "/".join(cfg.larpcheck.reject_coin_stamps),
             cfg.larpcheck.on_unchecked, "; no buy without a verdict" if cfg.larpcheck.require_verdict else "")
    return CompositeSafety(*checks, gate)


def build_agent(cfg: Config, with_feed: bool = True) -> tuple[Agent, PumpPortalFeed | None]:
    gmgn = build_gmgn(cfg)
    market, feed = build_market(cfg, with_feed, gmgn=gmgn)
    safety = build_safety(cfg, gmgn=gmgn)
    if cfg.execution.mode == "live":
        if os.environ.get(LIVE_CONFIRM_ENV) != "yes":
            raise SystemExit(
                f"Refusing to trade real funds: execution.mode is 'live' but {LIVE_CONFIRM_ENV}=yes is not set.\n"
                "Paper trade first. Use a dedicated wallet holding only what you can afford to lose."
            )
        from .execution.jupiter import JupiterExecutor, load_keypair

        keypair = load_keypair()
        executor = JupiterExecutor(cfg.execution, keypair, market)
        if cfg.sniper.enabled:
            from .execution.pumpportal import PumpPortalExecutor

            executor = PumpPortalExecutor(executor, market, cfg.sniper)
            log.warning("PumpPortal pre-graduation execution enabled; entries remain gated by agent checks")
        log.warning("LIVE TRADING ENABLED for wallet %s", keypair.pubkey())
    else:
        executor = PaperExecutor(cfg.execution)
    pf = Portfolio.load(cfg.agent.state_file, cfg.risk.starting_balance_usd)
    from .data.larpcheck import CompositeSafety, LarpCheckGate

    # The LarpCheck gate also reviews held coins and exits when it flags them.
    guard = safety.find(LarpCheckGate) if isinstance(safety, CompositeSafety) else None
    launch_scan = None
    f = cfg.filters
    early_scans = f.wallet_scan_below_age_minutes > 0 or f.wallet_scan_below_market_cap_usd > 0
    if cfg.launch.enabled or early_scans or cfg.lowcap.enabled:
        from .data.wallets import WalletScanner

        launch_scan = WalletScanner(cfg.launch, cfg.execution.rpc_url)
        lc = cfg.launch
        scan = (f"wallet scan (bundles <= {lc.max_bundle_pct:.0f}%, linked wallets <= {lc.max_cluster_pct:.0f}%, "
                f"top 10 <= {lc.max_top10_pct:.0f}%, creator <= {lc.max_creator_pct:.0f}%)")
        if cfg.launch.enabled:
            log.info("Launch lane on: $%.0fK+ volume within %.0fm of launch, then a %s before the usual checks",
                     lc.min_volume_usd / 1000, lc.max_age_minutes, scan)
        if early_scans:
            log.info("Early or small buys (under %.0fm old or $%.0fK market cap) need the %s too",
                     f.wallet_scan_below_age_minutes, f.wallet_scan_below_market_cap_usd / 1000, scan)
        if cfg.lowcap.enabled:
            log.info("Low-cap lane on: $%.0fK-$%.0fK, age %.0f-%.0fm, max position $%.0f; wallet scan required",
                     cfg.lowcap.min_market_cap_usd / 1000, cfg.lowcap.max_market_cap_usd / 1000,
                     cfg.lowcap.min_age_minutes, cfg.lowcap.max_age_minutes, cfg.lowcap.max_position_usd)
    from .analytics import Analytics

    analytics = Analytics(Path(cfg.agent.state_file).parent)
    from .data.profile import TokenProfiler

    profiler = TokenProfiler(cfg.classify)
    if cfg.larpcheck.enabled and cfg.classify.enabled:
        log.info("Tech vs meme on: tech tokens get the full LarpCheck gate; memes get %s",
                 {"lookup": "its free lookup only (no credits)", "full": "the full gate too",
                  "off": "no LarpCheck"}[cfg.larpcheck.memes])
    if cfg.authenticity.enabled:
        a = cfg.authenticity
        log.info("Fake-chart check on: volume <= %gx liquidity/hour, <= $%.0f per holder, <= %g trades/holder/hour, "
                 ">= %d holders", a.max_turnover_per_hour, a.max_volume_per_holder_usd, a.max_trades_per_holder_h1,
                 a.min_holders)
    route_check = None
    if cfg.honeypot.sell_route_check and cfg.execution.mode == "paper":  # live: the executor's own preflight
        from .data.route import SellRouteCheck

        route_check = SellRouteCheck(cfg.honeypot, cfg.execution, market.sol_price_usd)
        log.info("Honeypot check on: every paper buy is quoted on Jupiter and sold straight back first "
                 "(no route, or a round trip losing over %g%%, means no buy)", cfg.execution.max_roundtrip_loss_pct)
    from .data.chain_safety import ChainSafety

    dex = DexScreenerClient(cfg.filters.quote_symbols, discovery_chains=cfg.lowcap.chains)
    chain_safety = ChainSafety(gmgn, fail_closed=cfg.copy.fail_closed)
    copy_tracker = None
    if cfg.copy.enabled and gmgn is not None:
        from .data.copytrade import CopyTracker

        cc = cfg.copy
        copy_tracker = CopyTracker(cc, gmgn, state_file=cc.state_file)
        log.info("Copy lane on: low caps (under $%.0fK) only when a tracked wallet buys them, on %s; wallets with a "
                 "%g-%g%% win rate, %g+ days old, real losses", cfg.filters.min_market_cap_usd / 1000,
                 ", ".join(cc.chains), cc.min_win_rate * 100, cc.max_win_rate * 100, cc.min_wallet_age_days)
    elif cfg.copy.enabled:
        log.info("Copy lane off: it needs a GMGN API key (low caps are not bought without it)")
    longterm = None
    if cfg.longterm.enabled:
        from .data.longterm import LongTermScanner

        longterm = LongTermScanner(cfg.longterm, dex, gmgn)
        lt = cfg.longterm
        log.info("Long-term lane on: %d watchlist token(s)%s; up to %d holds of $%.0f", len(lt.watchlist),
                 " + GMGN discovery" if gmgn is not None and lt.discovery else "", lt.max_positions, lt.position_usd)
    newscheck = None
    if cfg.narrative.enabled:
        from .data.newscheck import NewsCheckClient, load_api_key as newscheck_key

        nc_key = newscheck_key(cfg.narrative)
        if nc_key:
            newscheck = NewsCheckClient(cfg.narrative, nc_key)
            log.info("Narrative lane on (NewsCheck): young coins with a strong story are bought early; "
                     "LARP / RED FLAG coins are blocked")
        else:
            log.info("Narrative lane off: no NewsCheck key (free at newscheck.app/developers; set NEWSCHECK_API_KEY "
                     "or put it in %s)", cfg.narrative.api_key_file)
    scanlog = None
    if cfg.agent.scan_log and cfg.agent.scan_log_file:
        from .scanlog import ScanLog

        scanlog = ScanLog(cfg.agent.scan_log_file)
        log.info("Scan log on: every scanned coin is followed for 4 hours (%s); read it with `memecoin-agent scans`",
                 cfg.agent.scan_log_file)
    from .edge import MarketRegime, ScanHistory

    regime = MarketRegime(cfg.regime, market.sol_price_usd) if cfg.regime.enabled else None
    if regime is not None:
        log.info("Market regime gate on: no new automatic entries while under %.0f%% of scanned coins are up over "
                 "the hour or SOL is down %g%%+ in an hour", (cfg.regime.min_breadth or 0) * 100,
                 cfg.regime.max_sol_drop_h1_pct or 0)
    history = None
    if cfg.learning.enabled and cfg.agent.scan_log_file:
        history = ScanHistory(cfg.learning, cfg.agent.scan_log_file)
        log.info("Track record on: buys are checked against what earlier coins in the scan log did (needs %d+ "
                 "finished rows per group)", cfg.learning.min_lane_samples)
    return Agent(cfg, market, safety, executor, pf, newscheck=newscheck, scanlog=scanlog, regime=regime,
                 history=history, guard=guard, launch_scan=launch_scan, analytics=analytics,
                 profiler=profiler, route_check=route_check, multichain=dex, copy_tracker=copy_tracker,
                 longterm=longterm, chain_safety=chain_safety), feed


def build_telegram(cfg: Config, runner):
    """The Telegram bot if enabled and a bot token is configured, else None."""
    if not cfg.telegram.enabled:
        return None
    from .telegram_bot import TelegramBot, TelegramClient, allowed_chats, load_token

    token = load_token(cfg.telegram)
    if not token:
        log.info("Telegram not configured (set TELEGRAM_BOT_TOKEN to get alerts and control from your phone)")
        return None
    return TelegramBot(cfg.telegram, TelegramClient(token), runner, allowed_chats(cfg.telegram))


class Engine:
    """Everything a trading session runs in one process: agent, launch feed, recorder, background
    lanes, Telegram bot, state lock."""

    def __init__(self, cfg: Config, args: argparse.Namespace) -> None:
        from .recording import Recorder, RecordingWriter
        from .ui.runner import AgentRunner, EquityHistory

        self.feed: PumpPortalFeed | None = None
        self.lock: StateLock | None = None
        self.bot = None
        record = bool(getattr(args, "record", False)) or cfg.recorder.record_while_trading
        if getattr(args, "synthetic", False):
            from .simulation import SimClock, SyntheticMarket, SyntheticSafety

            cfg.execution.mode = "paper"
            clock = SimClock(utcnow())
            market = SyntheticMarket(clock, seed=args.seed)

            def advance(seconds: float) -> None:
                clock.advance(seconds)
                market.step(seconds)

            agent = Agent(cfg, market, SyntheticSafety(market), PaperExecutor(cfg.execution),
                          Portfolio(cash_usd=cfg.risk.starting_balance_usd), clock=clock, persist=False)
            # Synthetic recordings go to their own folder so they never mix with real market data.
            rec_dir = str(Path(cfg.recorder.dir).with_name(Path(cfg.recorder.dir).name + "-synthetic"))
            cfg.recorder.dir = rec_dir
            self.runner = AgentRunner(
                agent, advance=advance, speed=args.speed, synthetic=True, record=record,
                max_ticks=getattr(args, "max_ticks", None),
                make_recorder=lambda: Recorder(cfg, market, SyntheticSafety(market), RecordingWriter(rec_dir),
                                               clock=clock),
            )
            return
        try:
            self.lock = StateLock(cfg.agent.state_file).acquire()
        except StateLocked as exc:
            raise SystemExit(str(exc)) from None
        try:
            agent, self.feed = build_agent(cfg)
        except BaseException:
            self.lock.release()
            raise
        self.runner = AgentRunner(
            agent, feed=self.feed, equity=EquityHistory(Path(cfg.agent.state_file).parent / "equity.csv"),
            record=record, max_ticks=getattr(args, "max_ticks", None),
            make_recorder=lambda: Recorder(cfg, agent.market, build_safety(cfg, with_larpcheck=False),
                                           RecordingWriter(cfg.recorder.dir), clock=agent.clock,
                                           wallet_scan=agent.launch_scan, chain_safety=agent.chain_safety),
        )
        self.bot = build_telegram(cfg, self.runner)
        if self.bot is not None:
            self.runner.listeners.append(self.bot)
            self.runner.telegram = self.bot

    def start(self) -> None:
        if self.feed is not None:
            self.feed.start()
        for worker in (self.runner.agent.copy_tracker, self.runner.agent.longterm):
            if worker is not None:
                worker.start()
        self.runner.start()
        if self.bot is not None:
            self.bot.start()

    def stop(self) -> None:
        self.runner.stop()
        for worker in (self.runner.agent.copy_tracker, self.runner.agent.longterm):
            if worker is not None:
                worker.stop()
        if self.bot is not None:
            self.bot.stop()
        if self.feed is not None:
            self.feed.stop()
        if self.lock is not None:
            self.lock.release()


def cmd_run(args: argparse.Namespace, cfg: Config) -> None:
    engine = Engine(cfg, args)
    engine.start()
    try:
        engine.runner.wait()
    except KeyboardInterrupt:
        log.info("Interrupted; shutting down.")
    finally:
        engine.stop()


def cmd_scan(args: argparse.Namespace, cfg: Config) -> None:
    # The launch feed only sees events after it connects, so a one-shot scan uses its saved buffer.
    agent, feed = build_agent(cfg)
    if feed is not None:
        feed.load()
    evals = agent.evaluate_candidates(utcnow())
    if not evals:
        print("No candidates returned by discovery.")
        return
    print(f"{'symbol':<12} {'score':>5} {'liq$':>10} {'mcap$':>11} {'1h%':>7} {'b/s1h':>6}  verdict")
    for ev in evals[: args.limit]:
        s = ev.snapshot
        verdict = "PASS" if ev.passed else "; ".join(ev.reasons[:3])
        if ev.passed and args.safety:
            rep = agent.safety.check(s.mint)
            verdict = "PASS" if rep.ok else "UNSAFE: " + "; ".join(rep.reasons)
        print(f"{s.symbol[:12]:<12} {ev.score:>5.2f} {s.liquidity_usd:>10,.0f} {s.market_cap_usd:>11,.0f} "
              f"{s.price_change_h1:>+7.1f} {s.buy_sell_ratio_h1:>6.2f}  {verdict}")


def print_status(pf: Portfolio, starting: float) -> None:
    eq = pf.equity_usd()
    print(f"Equity ${eq:,.2f}  (cash ${pf.cash_usd:,.2f}, positions ${pf.positions_value_usd():,.2f})")
    print(f"Realized PnL ${pf.total_realized_pnl():+,.2f}   Return vs start {((eq / starting) - 1) * 100:+.1f}%")
    if not pf.positions:
        print("No open positions.")
        return
    print(f"\n{'symbol':<12} {'cost$':>9} {'value$':>9} {'pnl%':>7} {'peak%':>7}  opened")
    for p in pf.positions.values():
        peak = (p.peak_price_usd / p.entry_price_usd - 1) * 100
        print(f"{p.symbol[:12]:<12} {p.cost_basis_usd:>9.2f} {p.market_value_usd():>9.2f} "
              f"{p.pnl_pct():>+7.1f} {peak:>+7.1f}  {p.opened_at:%Y-%m-%d %H:%M}")


def cmd_report(args: argparse.Namespace, cfg: Config) -> None:
    from .analytics import build_report

    state = Path(cfg.agent.state_file).parent
    print(build_report(args.trades or cfg.agent.trades_csv, state / "entries.csv", state / "after_exit.csv"))


def cmd_scans(args: argparse.Namespace, cfg: Config) -> None:
    from .scanlog import build_scan_report

    print(build_scan_report(args.file or cfg.agent.scan_log_file or "state/scans.csv"))


def cmd_exitlab(args: argparse.Namespace, cfg: Config) -> None:
    import copy

    from .exitlab import Candles, compare, load_entries, wide_exits

    trades = args.trades or [cfg.agent.trades_csv]
    entries = load_entries([t for t in trades if Path(t).exists()])
    if args.last:
        entries = entries[-args.last:]
    if not entries:
        print(f"Exit lab: no buys found in {', '.join(trades)}")
        return
    variants = {"your settings": cfg.exits, "wide (50% stop, 70% leash)": wide_exits(cfg.exits)}
    for assignment in args.set or []:
        trial = copy.deepcopy(cfg)
        for one in assignment.split(","):
            apply_override(trial, one.strip())
        variants[assignment.replace("exits.", "")] = trial.exits
    source = Candles(args.cache or str(Path(cfg.agent.state_file).parent / "candles"))
    print(f"Exit lab: {len(entries)} entries; downloading minute candles for the ones not cached yet "
          f"(about 5 seconds each)...", flush=True)
    print(compare(entries, source.after, variants, cfg.execution.paper_fee_pct))


def cmd_status(args: argparse.Namespace, cfg: Config) -> None:
    print_status(Portfolio.load(cfg.agent.state_file, cfg.risk.starting_balance_usd), cfg.risk.starting_balance_usd)


def cmd_close_all(args: argparse.Namespace, cfg: Config) -> None:
    try:
        lock = StateLock(cfg.agent.state_file).acquire()
    except StateLocked as exc:
        raise SystemExit(f"{exc}\nIf the dashboard is running, use its Close all button instead.") from None
    try:
        agent, _ = build_agent(cfg, with_feed=False)
        if not agent.pf.positions:
            print("No open positions.")
            return
        agent.close_all()
        print_status(agent.pf, cfg.risk.starting_balance_usd)
    finally:
        lock.release()


def cmd_record(args: argparse.Namespace, cfg: Config) -> None:
    from .recording import Recorder, RecordingWriter

    writer = RecordingWriter(args.out or cfg.recorder.dir)
    if args.synthetic:
        # Offline: record the synthetic market as fast as the CPU allows (for trying out backtests).
        from .simulation import SimClock, SyntheticMarket, SyntheticSafety

        clock = SimClock()
        market = SyntheticMarket(clock, seed=args.seed)

        def advance(seconds: float) -> None:
            clock.advance(cfg.recorder.interval_s)
            market.step(cfg.recorder.interval_s)

        Recorder(cfg, market, SyntheticSafety(market), writer, clock=clock, sleep=advance).run(args.hours or 12)
        print(f"Wrote {writer.records_written:,} synthetic records to {writer.dir}")
        return
    gmgn = build_gmgn(cfg)
    market, feed = build_market(cfg, gmgn=gmgn)
    if feed is not None:
        feed.start()
    try:
        wallet_scan = None
        if cfg.sniper.enabled or cfg.lowcap.enabled:
            from .data.wallets import WalletScanner

            wallet_scan = WalletScanner(cfg.launch, cfg.execution.rpc_url)
        from .data.chain_safety import ChainSafety

        Recorder(cfg, market, build_safety(cfg, gmgn=gmgn), writer, wallet_scan=wallet_scan,
                 chain_safety=ChainSafety(gmgn, fail_closed=cfg.copy.fail_closed)).run(args.hours)
    finally:
        if feed is not None:
            feed.stop()


def cmd_backtest(args: argparse.Namespace, cfg: Config) -> None:
    from .backtest import format_report, run_backtest
    from .recording import read_recordings

    for assignment in args.set or []:
        apply_override(cfg, assignment)
    res = run_backtest(cfg, read_recordings(args.dir or cfg.recorder.dir),
                       assume_safe=args.assume_safe, close_at_end=not args.keep_open)
    print(format_report(res))
    if args.trades_out:
        res.portfolio.trades = list(res.trades)
        Path(args.trades_out).unlink(missing_ok=True)
        res.portfolio.flush_trades(args.trades_out)
        print(f"Trades written to {args.trades_out}")


def cmd_ui(args: argparse.Namespace, cfg: Config) -> None:
    from .ui import redacted_config
    from .ui.runner import LogBuffer
    from .ui.server import DashboardServer

    logs = LogBuffer()
    root = logging.getLogger()
    root.addHandler(logs)
    for handler in root.handlers:
        # A dashboard-launched backtest replays thousands of simulated trades; keep them out of the live log.
        handler.addFilter(lambda record: record.threadName != "backtest")
    engine = Engine(cfg, args)
    try:
        server = DashboardServer(engine.runner, logs, host=args.host, port=args.port,
                                 password=os.environ.get("MEMECOIN_UI_PASSWORD"),
                                 share_password=os.environ.get("MEMECOIN_SHARE_PASSWORD"),
                                 config_dict=redacted_config(cfg))
    except BaseException:
        engine.stop()
        raise
    engine.start()
    mode = "synthetic market" if args.synthetic else f"{cfg.execution.mode} mode"
    print(f"Dashboard: {server.url}  ({mode}; Ctrl-C to stop)", flush=True)
    if server.share_password:
        print(f"Friend view: {server.url.rstrip('/')}/share  (separate password)", flush=True)
    if not args.no_browser:
        try:
            import webbrowser

            webbrowser.open(server.url)
        except Exception:
            pass
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        engine.stop()


def cmd_demo(args: argparse.Namespace, cfg: Config) -> None:
    from .simulation import SimClock, SyntheticMarket, SyntheticSafety

    cfg.execution.mode = "paper"
    clock = SimClock()
    market = SyntheticMarket(clock, seed=args.seed)
    agent = Agent(cfg, market, SyntheticSafety(market), PaperExecutor(cfg.execution),
                  Portfolio(cash_usd=cfg.risk.starting_balance_usd), clock=clock, persist=False)
    sells = 0
    for _ in range(args.ticks):
        clock.advance(cfg.agent.tick_seconds)
        market.step(cfg.agent.tick_seconds)
        agent.tick()
        sells += sum(1 for t in agent.pf.trades if t.side == "sell")
        agent.pf.trades.clear()
    hours = args.ticks * cfg.agent.tick_seconds / 3600
    print(f"\n--- Synthetic demo: {hours:.1f} simulated hours, {sells} sells ---")
    print_status(agent.pf, cfg.risk.starting_balance_usd)


def _raise_interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def _default_to_ui(argv: list[str], commands: set[str]) -> list[str]:
    """Bare `memecoin-agent [-c cfg] [ui options]` means `memecoin-agent [-c cfg] ui [ui options]`."""
    head: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("-c", "--config") and i + 1 < len(argv):
            head += argv[i:i + 2]
            i += 2
        elif tok.startswith("--config=") or tok in ("-v", "--verbose"):
            head.append(tok)
            i += 1
        elif tok in commands or tok in ("-h", "--help"):
            return argv  # an explicit command (or top-level help): leave untouched
        else:
            break
    return [*head, "ui", *argv[i:]]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="memecoin-agent", description=__doc__)
    parser.add_argument("-c", "--config", default=None, help="YAML config file (defaults are used if omitted)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    def session_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--record", action="store_true",
                       help="also record market data for backtests (same as recorder.record_while_trading)")
        p.add_argument("--synthetic", action="store_true", help="use the offline synthetic market (no network)")
        p.add_argument("--speed", type=float, default=20.0, help="synthetic mode: simulated seconds per real second")
        p.add_argument("--seed", type=int, default=7)

    p = sub.add_parser("ui", help="trade with the local web dashboard (the default command)")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (non-local addresses require MEMECOIN_UI_PASSWORD)")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    session_args(p)
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("run", help="trade headless (no dashboard)")
    p.add_argument("--max-ticks", type=int, default=None)
    session_args(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("scan", help="one-shot: discover and score tokens, no trading")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--safety", action="store_true", help="also run RugCheck on tokens that pass filters")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("report", help="win rate and profit by exit reason, lane, liquidity, age... from the trade files")
    p.add_argument("--trades", nargs="+", default=None, help="one or more trades CSVs, e.g. a downloaded slice (default: agent.trades_csv)")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("scans", help="what the scan-everything log says: which filters block winners, which let rugs through")
    p.add_argument("--file", default=None, help="a scans.csv (default: agent.scan_log_file)")
    p.set_defaults(func=cmd_scans)

    p = sub.add_parser("exitlab", help="replay your past entries on real minute candles under different exit settings")
    p.add_argument("--trades", nargs="+", default=None, help="one or more trades CSVs (default: agent.trades_csv)")
    p.add_argument("--set", action="append", metavar="EXITS.KEY=VALUE[,...]",
                   help="another set of exit settings to compare, e.g. --set exits.hard_stop_pct=35 (repeatable)")
    p.add_argument("--last", type=int, default=None, help="only the newest N entries")
    p.add_argument("--cache", default=None, help="candle cache directory (default: next to the state file)")
    p.set_defaults(func=cmd_exitlab)

    sub.add_parser("status", help="show portfolio from the state file").set_defaults(func=cmd_status)
    sub.add_parser("close-all", help="sell every open position now").set_defaults(func=cmd_close_all)

    p = sub.add_parser("record", help="record market data for backtesting")
    p.add_argument("--hours", type=float, default=None, help="stop after this long (default: until Ctrl-C)")
    p.add_argument("--out", default=None, help="output directory (default: recorder.dir)")
    p.add_argument("--synthetic", action="store_true", help="record the offline synthetic market instead")
    p.add_argument("--seed", type=int, default=7)
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("backtest", help="replay recorded data through the agent")
    p.add_argument("dir", nargs="?", default=None, help="recording directory (default: recorder.dir)")
    p.add_argument("--set", action="append", metavar="SECTION.KEY=VALUE",
                   help="override a config value, e.g. --set filters.min_score=0.6 (repeatable)")
    p.add_argument("--assume-safe", action="store_true",
                   help="treat tokens with no recorded RugCheck verdict as safe (default: reject)")
    p.add_argument("--keep-open", action="store_true", help="don't liquidate open positions at the end")
    p.add_argument("--trades-out", default=None, help="write the trade journal CSV here")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("demo", help="run against an offline synthetic market (no network)")
    p.add_argument("--ticks", type=int, default=2000)
    p.add_argument("--seed", type=int, default=7)
    p.set_defaults(func=cmd_demo)

    argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(_default_to_ui(argv, set(sub.choices)))
    cfg = load_config(args.config)
    offline = args.command in ("status", "demo", "backtest", "report", "scans", "exitlab") or getattr(args, "synthetic", False)
    if args.command == "record" and not args.synthetic and cfg.recorder.record_while_trading:
        log.warning("recorder.record_while_trading is on: a separate `record` process duplicates the "
                    "recording your trading session already does")
    setup_logging(None if offline else cfg.agent.log_file, args.verbose)
    if args.command in ("run", "ui"):
        # Docker and cloud hosts stop a container with SIGTERM: shut down as cleanly as on Ctrl-C.
        signal.signal(signal.SIGTERM, _raise_interrupt)
    quiet = args.command in ("status", "scan", "demo", "backtest", "report", "scans", "exitlab") or (args.command == "record" and args.synthetic)
    if quiet and not args.verbose:
        # Backtests replay thousands of ticks; per-tick warnings (e.g. stale data) would drown the report.
        logging.getLogger().setLevel(logging.ERROR if args.command == "backtest" else logging.WARNING)
    args.func(args, cfg)


if __name__ == "__main__":
    main()


