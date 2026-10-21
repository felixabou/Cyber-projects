"""Command-line entry point: run, scan, status, close-all, demo."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .agent import Agent
from .config import Config, load_config
from .data.dexscreener import DexScreenerClient
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


def build_agent(cfg: Config) -> Agent:
    market = DexScreenerClient(cfg.filters.quote_symbols)
    f = cfg.filters
    safety = (
        RugCheckClient(max_score=f.max_rugcheck_score, reject_danger=f.reject_danger_risks,
                       min_lp_locked_pct=f.min_lp_locked_pct)
        if f.require_rugcheck else NoSafetyCheck()
    )
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
    return Agent(cfg, market, safety, executor, pf)


def cmd_run(args: argparse.Namespace, cfg: Config) -> None:
    build_agent(cfg).run(max_ticks=args.max_ticks)


def cmd_scan(args: argparse.Namespace, cfg: Config) -> None:
    agent = build_agent(cfg)
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
    agent = build_agent(cfg)
    if not agent.pf.positions:
        print("No open positions.")
        return
    agent.close_all()
    print_status(agent.pf, cfg.risk.starting_balance_usd)


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

    p = sub.add_parser("scan", help="one-shot: discover and score tokens, no trading")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--safety", action="store_true", help="also run RugCheck on tokens that pass filters")
    p.set_defaults(func=cmd_scan)

    sub.add_parser("status", help="show portfolio from the state file").set_defaults(func=cmd_status)
    sub.add_parser("close-all", help="sell every open position now").set_defaults(func=cmd_close_all)

    p = sub.add_parser("demo", help="run against an offline synthetic market (no network)")
    p.add_argument("--ticks", type=int, default=2000)
    p.add_argument("--seed", type=int, default=7)
    p.set_defaults(func=cmd_demo)

    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    quiet = args.command in ("status", "scan", "demo") and not args.verbose
    setup_logging(None if args.command in ("status", "demo") else cfg.agent.log_file, args.verbose)
    if quiet:
        logging.getLogger().setLevel(logging.WARNING)
    args.func(args, cfg)


if __name__ == "__main__":
    main()
