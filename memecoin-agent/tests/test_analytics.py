"""Entry details, after-sale tracking and the report."""

from __future__ import annotations

import csv
from datetime import timedelta

from memecoin_agent.agent import Agent
from memecoin_agent.analytics import Analytics, build_report, exit_kind, round_trips
from memecoin_agent.config import Config
from memecoin_agent.execution.paper import PaperExecutor
from memecoin_agent.portfolio import Portfolio

from .conftest import NOW, make_snap
from .test_agent import Clock, FakeMarket, FakeSafety


def test_exit_kinds():
    assert exit_kind("take profit 1 (+59.4%)") == "take profit"
    assert exit_kind("trailing stop (35% off peak, pnl +3.2%)") == "trailing stop"
    assert exit_kind("larpcheck LIKELY REAL 62/100: x") == "larpcheck flag"
    assert exit_kind("liquidity pulled (84% below entry)") == "liquidity pulled"
    assert exit_kind("initials out (+31.0%): sold 89% = the initial $50.00 + 50% of the profit, the rest runs") == "initials out"
    assert exit_kind("runner 72% below its peak (limit 70%, pnl +40.0%)") == "runner"
    assert exit_kind("dip review: -35% and still sliding (x)") == "dip review"


def test_buy_is_logged_and_sold_coin_is_watched_then_reported(tmp_path):
    cfg = Config()
    cfg.agent.kill_switch_file = str(tmp_path / "STOP")
    cfg.agent.trades_csv = str(tmp_path / "trades.csv")
    market, clock = FakeMarket([make_snap(mint="GOOD")]), Clock()
    an = Analytics(tmp_path, hours=1)
    agent = Agent(cfg, market, FakeSafety(), PaperExecutor(cfg.execution), Portfolio(cash_usd=1000),
                  clock=clock, persist=False, analytics=an)
    agent.scan_and_enter(NOW)
    entry = list(csv.DictReader(an.entries_csv.open()))[0]
    assert entry["mint"] == "GOOD" and entry["lane"] == "momentum" and float(entry["liquidity_usd"]) == 80_000
    price = market.snaps["GOOD"].price_usd
    market.snaps["GOOD"] = make_snap(mint="GOOD", price_usd=price * 0.45)  # past the 50% hard stop
    clock.t = NOW + timedelta(minutes=10)
    agent.manage_positions(clock.t)
    assert "GOOD" in an.watches and not agent.pf.positions
    agent.pf.flush_trades(cfg.agent.trades_csv)
    market.snaps["GOOD"] = make_snap(mint="GOOD", price_usd=price * 1.5)  # it runs after the sale
    an.update(clock.t + timedelta(minutes=5), agent._fetch)
    an.update(clock.t + timedelta(minutes=70), agent._fetch)  # window over
    after = list(csv.DictReader(an.after_csv.open()))[0]
    assert after["exit_reason"].startswith("stop loss") and float(after["high_pct"]) > 100
    assert not an.watches and not Analytics(tmp_path).watches  # persisted, nothing left to watch
    trips = round_trips(cfg.agent.trades_csv, an.entries_csv, an.after_csv)
    assert len(trips) == 1 and trips[0].entry["lane"] == "momentum" and trips[0].after
    text = build_report(cfg.agent.trades_csv, an.entries_csv, an.after_csv)
    assert "stop loss" in text and "momentum" in text and "After the sale" in text and "up >50%" in text


def test_watch_survives_a_restart(tmp_path):
    an = Analytics(tmp_path)
    an.watch("M", "SYM", 0.01, NOW, "time stop")
    again = Analytics(tmp_path)
    assert again.watches["M"].exit_price == 0.01 and again.watches["M"].reason == "time stop"
