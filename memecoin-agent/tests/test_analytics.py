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


def _trips(pcts_by_day, cost=20.0):
    from datetime import datetime, timezone

    from memecoin_agent.analytics import RoundTrip

    out, start = [], datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
    for d, pcts in enumerate(pcts_by_day):
        for i, p in enumerate(pcts):
            t = start + timedelta(days=d, minutes=10 * i)
            out.append(RoundTrip(f"M{d}_{i}", "X", t, t + timedelta(minutes=5), cost, cost * p / 100))
    return out


def test_edge_check_verdicts():
    from memecoin_agent.analytics import edge_check, edge_lines

    assert edge_check([]).level == "early"
    assert edge_check(_trips([[10.0] * 10])).verdict == "too early: 10 trades, wait for 30+"
    assert edge_check(_trips([[10.0] * 40])).level == "early"  # one day: same-day trades share a market
    assert edge_check(_trips([[-20.0, -15.0, -30.0, 5.0]] * 8)).level == "losing"
    steady = edge_check(_trips([[8.0, 12.0, -5.0, 6.0]] * 8))
    assert steady.level == "promising" and steady.without_best3 > 0 and steady.days == 8 and steady.days_up == 8
    assert edge_check(_trips([[8.0, 12.0, -5.0, 6.0]] * 10 + [[9.0, 7.0, -4.0, 5.0, 10.0, 3.0] * 2] * 6)).level == "real"
    # One runner carrying a pile of small losers: positive, but only because of the best trades.
    lucky = edge_check(_trips([[-6.0, -4.0, -5.0, -3.0]] * 8 + [[900.0, 400.0, 300.0]]))
    assert lucky.level in ("fragile", "unclear") and lucky.without_best3 < 0
    assert any("3 best trades made" in n for n in lucky.notes)
    text = "\n".join(edge_lines(_trips([[8.0, 12.0, -5.0, 6.0]] * 8)))
    assert text.startswith("Edge check") and "promising" in text


def test_paper_fills_drift_against_fast_movers():
    from memecoin_agent.config import ExecutionConfig

    calm, fast = make_snap(price_change_m5=0.0), make_snap(price_change_m5=60.0)
    ex = PaperExecutor(ExecutionConfig(paper_price_impact=False, paper_fee_pct=0.0))
    assert abs(ex.buy(calm, 100).price_usd - calm.price_usd) < 1e-12
    # 60% in 5 minutes over a 6 s fill delay: 1.2% worse.
    assert abs(ex.buy(fast, 100).price_usd / fast.price_usd - 1.012) < 1e-9
    off = PaperExecutor(ExecutionConfig(paper_price_impact=False, paper_fee_pct=0.0, paper_latency_s=0))
    assert abs(off.buy(fast, 100).price_usd - fast.price_usd) < 1e-12
