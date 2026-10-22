"""memecoin-agent: run `memecoin-agent` (or `memecoin-agent ui`) to trade with the dashboard.

Commands: ui, run, scan, status, close-all, record, backtest, demo.
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
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_gmgn(cfg: Config):
    """A GMGN client if GMGN is enabled and a key is configured, else None (with one log line)."""
    if not (cfg.gmgn.enabled and (cfg.gmgn.discovery or cfg.gmgn.safety)):
        return None
    from .data.gmgn import GmgnClient, load_api_key

    key = load_api_key(cfg.gmgn)
    if not key:
        log.info("GMGN not configured (set GMGN_API_KEY or gmgn.api_key_file); running without it")
        return None
    return GmgnClient(key, cfg.gmgn.host)


def build_market(cfg: Config, with_feed: bool = True, gmgn=None):
    """DexScreener market data. Discovery order: pump.fun graduations (launch feed), GMGN trending,
    then DexScreener's own lists. Prices always come from DexScreener."""
    base = DexScreenerClient(cfg.filters.quote_symbols)
    if gmgn is not None and cfg.gmgn.discovery:
        from .data.gmgn import ExtraDiscoveryMarket, GmgnDiscovery

        base = ExtraDiscoveryMarket(base, GmgnDiscovery(cfg.gmgn, cfg.filters, gmgn))
        log.info("GMGN discovery on: %s trending, every %.0fs", cfg.gmgn.trending_interval, cfg.gmgn.discover_every_s)
    fc = cfg.feeds
    if not with_feed or not (fc.pumpportal_migrations or fc.pumpportal_new_tokens):
        return base, None
    feed = PumpPortalFeed(
        url=fc.pumpportal_url, migrations=fc.pumpportal_migrations, new_tokens=fc.pumpportal_new_tokens,
        max_age_s=cfg.filters.max_age_hours * 3600, max_mints=fc.max_buffered_mints, buffer_file=fc.buffer_file,
    )
    return FeedAugmentedMarket(base, feed, fc.evict_below_liquidity_usd), feed


def build_safety(cfg: Config, with_larpcheck: bool = True, gmgn=None):
    """The agent's pre-buy checks, cheapest first: RugCheck, then GMGN, then LarpCheck (which can spend
    credits, so it only sees coins the others passed). The recorder passes with_larpcheck=False and no
    GMGN client: it vets hundreds of tokens, and these checks are for the few the agent would buy."""
    from .data.larpcheck import CompositeSafety, LarpCheckClient, LarpCheckGate, RequireLarpCheck, load_api_key

    f = cfg.filters
    rug = (RugCheckClient(max_score=f.max_rugcheck_score, reject_danger=f.reject_danger_risks,
                          min_lp_locked_pct=f.min_lp_locked_pct) if f.require_rugcheck else NoSafetyCheck())
    checks: list = [rug]
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
        log.warning("LIVE TRADING ENABLED for wallet %s", keypair.pubkey())
    else:
        executor = PaperExecutor(cfg.execution)
    pf = Portfolio.load(cfg.agent.state_file, cfg.risk.starting_balance_usd)
    from .data.larpcheck import CompositeSafety, LarpCheckGate

    # The LarpCheck gate also reviews held coins and exits when it flags them.
    guard = safety.find(LarpCheckGate) if isinstance(safety, CompositeSafety) else None
    return Agent(cfg, market, safety, executor, pf, guard=guard), feed


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
    """Everything a trading session runs in one process: agent, launch feed, recorder, hourly trade
    exports, Telegram bot, state lock."""

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
        exporter = None
        if cfg.export.enabled:
            from .export import TradeExporter

            exporter = TradeExporter(cfg.export, cfg.agent.trades_csv, agent.clock())
        self.runner = AgentRunner(
            agent, feed=self.feed, equity=EquityHistory(Path(cfg.agent.state_file).parent / "equity.csv"),
            record=record, max_ticks=getattr(args, "max_ticks", None), exporter=exporter,
            make_recorder=lambda: Recorder(cfg, agent.market, build_safety(cfg, with_larpcheck=False),
                                           RecordingWriter(cfg.recorder.dir), clock=agent.clock),
        )
        self.bot = build_telegram(cfg, self.runner)
        if self.bot is not None:
            self.runner.listeners.append(self.bot)
            self.runner.telegram = self.bot

    def start(self) -> None:
        if self.feed is not None:
            self.feed.start()
        self.runner.start()
        if self.bot is not None:
            self.bot.start()

    def stop(self) -> None:
        self.runner.stop()
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
    market, feed = build_market(cfg, gmgn=build_gmgn(cfg))
    if feed is not None:
        feed.start()
    try:
        Recorder(cfg, market, build_safety(cfg), writer).run(args.hours)
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
                                 password=os.environ.get("MEMECOIN_UI_PASSWORD"), config_dict=redacted_config(cfg))
    except BaseException:
        engine.stop()
        raise
    engine.start()
    mode = "synthetic market" if args.synthetic else f"{cfg.execution.mode} mode"
    print(f"Dashboard: {server.url}  ({mode}; Ctrl-C to stop)", flush=True)
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
    offline = args.command in ("status", "demo", "backtest") or getattr(args, "synthetic", False)
    if args.command == "record" and not args.synthetic and cfg.recorder.record_while_trading:
        log.warning("recorder.record_while_trading is on: a separate `record` process duplicates the "
                    "recording your trading session already does")
    setup_logging(None if offline else cfg.agent.log_file, args.verbose)
    if args.command in ("run", "ui"):
        # Docker and cloud hosts stop a container with SIGTERM: shut down as cleanly as on Ctrl-C.
        signal.signal(signal.SIGTERM, _raise_interrupt)
    quiet = args.command in ("status", "scan", "demo", "backtest") or (args.command == "record" and args.synthetic)
    if quiet and not args.verbose:
        # Backtests replay thousands of ticks; per-tick warnings (e.g. stale data) would drown the report.
        logging.getLogger().setLevel(logging.ERROR if args.command == "backtest" else logging.WARNING)
    args.func(args, cfg)


if __name__ == "__main__":
    main()
