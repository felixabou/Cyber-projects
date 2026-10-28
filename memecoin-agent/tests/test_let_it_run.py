"""Let-it-run exits: initials and half the profit out once, the rest runs; a 50% hard stop with
30-minute dip reviews in between."""

from __future__ import annotations

from datetime import timedelta

from memecoin_agent.agent import Agent
from memecoin_agent.config import Config, ExitConfig
from memecoin_agent.execution.paper import PaperExecutor
from memecoin_agent.models import Fill
from memecoin_agent.portfolio import Portfolio
from memecoin_agent.strategy import evaluate_exit, secure_fraction

from .conftest import NOW, make_snap
from .test_agent import Clock, FakeMarket, FakeSafety

E = ExitConfig()


def position(price=0.001, cost=50.0):
    pf = Portfolio(cash_usd=1000)
    pos = pf.open(make_snap(price_usd=price), Fill(cost / price, cost, price), NOW, 0.7)
    return pf, pos


def at(minutes):
    return NOW + timedelta(minutes=minutes)


def test_initials_and_half_the_profit_come_out_at_plus_25():
    _, pos = position()
    assert evaluate_exit(pos, make_snap(price_usd=0.00120), E, at(5)) is None  # +20%: hold
    d = evaluate_exit(pos, make_snap(price_usd=0.00125), E, at(6))
    assert d.secure and d.reason.startswith("initials out (+25.0%)")
    value = pos.market_value_usd(0.00125)
    assert abs(d.fraction * value - (50 + 0.5 * (value - 50))) < 1e-9  # $50 back + half of the $12.50 profit
    assert abs(d.fraction - secure_fraction(pos, 0.00125, E)) < 1e-12 and 0.89 < d.fraction < 0.91


def test_hard_stop_at_minus_50_not_30():
    _, pos = position()
    assert evaluate_exit(pos, make_snap(price_usd=0.00069), E, at(5)) is None  # -31%: dip watch, no sale
    assert pos.dip_since == at(5)
    assert evaluate_exit(pos, make_snap(price_usd=0.00049), E, at(6)).reason == "stop loss (-51.0%)"


def test_dip_reviews_hold_consolidation_and_sell_a_slide():
    _, pos = position()
    evaluate_exit(pos, make_snap(price_usd=0.00068), E, at(1))  # -32%: watch starts, review at 31m
    for m, p in ((10, 0.00066), (20, 0.00070)):
        assert evaluate_exit(pos, make_snap(price_usd=p), E, at(m)) is None
    # Review 1: range 6%, flat: consolidating -> hold for another 30 minutes.
    assert evaluate_exit(pos, make_snap(price_usd=0.00067), E, at(31)) is None
    assert "consolidating" in pos.review_note
    # Between reviews even -45% doesn't sell (only the 50% hard stop does)...
    assert evaluate_exit(pos, make_snap(price_usd=0.00055), E, at(45)) is None
    # ...but at review 2 it is still -40%+ deeper than the hold band: sell.
    d = evaluate_exit(pos, make_snap(price_usd=0.00058), E, at(61))
    assert d.fraction == 1.0 and d.reason.startswith("dip review: still -42%, deeper than the 40% hold band")


def test_dip_review_sells_a_steady_slide_and_ends_on_recovery():
    _, pos = position()
    evaluate_exit(pos, make_snap(price_usd=0.00069), E, at(1))
    evaluate_exit(pos, make_snap(price_usd=0.00075), E, at(10))  # bounce, then fades
    d = evaluate_exit(pos, make_snap(price_usd=0.00062, buys_h1=100, sells_h1=200), E, at(31))
    assert d.reason.startswith("dip review: -38% and still sliding")  # -10%+ over the window, sellers ahead
    _, pos = position()
    evaluate_exit(pos, make_snap(price_usd=0.00069), E, at(1))
    assert evaluate_exit(pos, make_snap(price_usd=0.00080), E, at(31)) is None
    assert pos.dip_since is None and pos.review_note.startswith("recovered")


def test_accumulation_also_holds_a_dip():
    _, pos = position()
    evaluate_exit(pos, make_snap(price_usd=0.00068), E, at(1))
    evaluate_exit(pos, make_snap(price_usd=0.00090), E, at(10))  # wide range (32%)
    assert evaluate_exit(pos, make_snap(price_usd=0.00066, buys_h1=300, sells_h1=200), E, at(31)) is None
    assert "accumulating" in pos.review_note


def agent_with(tmp_path, snaps):
    cfg = Config()
    cfg.agent.kill_switch_file = str(tmp_path / "STOP")
    market, clock = FakeMarket(snaps), Clock()
    return Agent(cfg, market, FakeSafety(), PaperExecutor(cfg.execution), Portfolio(cash_usd=1000), clock=clock,
                 persist=False), market, clock


def test_runner_is_reviewed_every_30_minutes_and_sold_only_70_percent_off_its_peak(tmp_path):
    a, market, clock = agent_with(tmp_path, [make_snap(mint="RUN")])
    a.scan_and_enter(NOW)
    pos = a.pf.positions["RUN"]
    entry = pos.entry_price_usd

    def tick(minutes, mult):
        clock.t = at(minutes)
        market.snaps["RUN"] = make_snap(mint="RUN", price_usd=entry * mult)
        a.manage_positions(clock.t)

    tick(5, 1.30)  # initials out
    assert pos.secured and 0.08 < pos.token_amount / (50 / entry) < 0.13
    assert "RUN" not in a.pf.active_positions()  # a runner doesn't take a slot
    sells = [t for t in a.pf.trades if t.side == "sell"]
    assert sells[0].reason.startswith("initials out") and sells[0].usd_value > pos.initial_cost_usd
    tick(20, 6.0)  # it runs: peak 6x
    tick(40, 2.0)  # -67% from the peak at the first review (due 35m): hold; next due 70m
    tick(400, 1.9)  # no time stop for runners, still above the 70% line; next review due 430m
    assert "RUN" in a.pf.positions and "68% below its peak" in pos.review_note
    tick(410, 1.7)  # 72% below the peak, but the next review isn't due yet
    assert "RUN" in a.pf.positions
    tick(431, 1.7)
    assert "RUN" not in a.pf.positions
    assert a.pf.trades[-1].reason.startswith("runner 72% below its peak (limit 70%")


def test_unmanaged_manual_positions_are_never_sold_by_the_agent(tmp_path):
    a, market, clock = agent_with(tmp_path, [make_snap(mint="MINE")])
    a.scan_and_enter(NOW)
    a.set_managed("MINE", False)
    market.snaps["MINE"] = make_snap(mint="MINE", price_usd=0.0001)  # -90%
    a.manage_positions(at(5))
    assert "MINE" in a.pf.positions
    a.set_managed("MINE", True)
    a.manage_positions(at(6))
    assert "MINE" not in a.pf.positions


def test_classic_style_still_available():
    _, pos = position()
    e = ExitConfig(style="classic")
    assert evaluate_exit(pos, make_snap(price_usd=0.00074), e, at(5)).reason.startswith("stop loss")
