"""Command-line entry point: run, ui, scan, status, close-all, record, backtest, demo."""

from __future__ import annotations

import argparse
import logging
import os
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


def build_market(cfg: Config, with_feed: bool = True):
    """DexScreener market data, with the real-time launch feed in front of discovery if enabled."""
    base = DexScreenerClient(cfg.filters.quote_symbols)
    fc = cfg.feeds
    if not with_feed or not (fc.pumpportal_migrations or fc.pumpportal_new_tokens):
        return base, None
    feed = PumpPortalFeed(
        url=fc.pumpportal_url, migrations=fc.pumpportal_migrations, new_tokens=fc.pumpportal_new_tokens,
        max_age_s=cfg.filters.max_age_hours * 3600, max_mints=fc.max_buffered_mints, buffer_file=fc.buffer_file,
    )
    return FeedAugmentedMarket(base, feed, fc.evict_below_liquidity_usd), feed


def build_safety(cfg: Config):
    f = cfg.filters
    if not f.require_rugcheck:
        return NoSafetyCheck()
    return RugCheckClient(max_score=f.max_rugcheck_score, reject_danger=f.reject_danger_risks,
                          min_lp_locked_pct=f.min_lp_locked_pct)


def build_agent(cfg: Config, with_feed: bool = True) -> tuple[Agent, PumpPortalFeed | None]:
    market, feed = build_market(cfg, with_feed)
    safety = build_safety(cfg)
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
    return Agent(cfg, market, safety, executor, pf), feed


def cmd_run(args: argparse.Namespace, cfg: Config) -> None:
    agent, feed = build_agent(cfg)
    if feed is not None:
        feed.start()
    try:
        agent.run(max_ticks=args.max_ticks)
    finally:
        if feed is not None:
            feed.stop()


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
    agent, _ = build_agent(cfg, with_feed=False)
    if not agent.pf.positions:
        print("No open positions.")
        return
    agent.close_all()
    print_status(agent.pf, cfg.risk.starting_balance_usd)


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
    market, feed = build_market(cfg)
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
    from .ui.runner import AgentRunner, EquityHistory, LogBuffer
    from .ui.server import DashboardServer

    logs = LogBuffer()
    root = logging.getLogger()
    root.addHandler(logs)
    for handler in root.handlers:
        # A dashboard-launched backtest replays thousands of simulated trades; keep them out of the live log.
        handler.addFilter(lambda record: record.threadName != "backtest")
    feed = None
    if args.synthetic:
        from .simulation import SimClock, SyntheticMarket, SyntheticSafety

        cfg.execution.mode = "paper"
        clock = SimClock(utcnow())
        market = SyntheticMarket(clock, seed=args.seed)

        def advance(seconds: float) -> None:
            clock.advance(seconds)
            market.step(seconds)

        agent = Agent(cfg, market, SyntheticSafety(market), PaperExecutor(cfg.execution),
                      Portfolio(cash_usd=cfg.risk.starting_balance_usd), clock=clock, persist=False)
        runner = AgentRunner(agent, advance=advance, speed=args.speed, synthetic=True)
    else:
        agent, feed = build_agent(cfg)
        equity = EquityHistory(Path(cfg.agent.state_file).parent / "equity.csv")
        runner = AgentRunner(agent, feed=feed, equity=equity)
    server = DashboardServer(runner, logs, host=args.host, port=args.port,
                             password=os.environ.get("MEMECOIN_UI_PASSWORD"), config_dict=redacted_config(cfg))
    if feed is not None:
        feed.start()
    runner.start()
    print(f"Dashboard: {server.url}  ({'synthetic market' if args.synthetic else cfg.execution.mode + ' mode'}; Ctrl-C to stop)",
          flush=True)
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
        runner.stop()
        if feed is not None:
            feed.stop()


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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="memecoin-agent", description=__doc__)
    parser.add_argument("-c", "--config", default=None, help="YAML config file (defaults are used if omitted)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="run the trading loop")
    p.add_argument("--max-ticks", type=int, default=None)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("ui", help="run the trading loop with a local web dashboard")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (non-local addresses require MEMECOIN_UI_PASSWORD)")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    p.add_argument("--synthetic", action="store_true", help="drive the dashboard with the offline synthetic market")
    p.add_argument("--speed", type=float, default=20.0, help="synthetic mode: simulated seconds per real second")
    p.add_argument("--seed", type=int, default=7)
    p.set_defaults(func=cmd_ui)

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

    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    offline = args.command in ("status", "demo", "backtest") or getattr(args, "synthetic", False)
    setup_logging(None if offline else cfg.agent.log_file, args.verbose)
    quiet = args.command in ("status", "scan", "demo", "backtest") or (args.command == "record" and args.synthetic)
    if quiet and not args.verbose:
        # Backtests replay thousands of ticks; per-tick warnings (e.g. stale data) would drown the report.
        logging.getLogger().setLevel(logging.ERROR if args.command == "backtest" else logging.WARNING)
    args.func(args, cfg)


if __name__ == "__main__":
    main()
