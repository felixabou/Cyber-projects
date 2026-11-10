"""The tighter exits, the liquidity-removal detector, the market regime gate, the scan-log track record,
conviction sizing and the low-cap lane's confirmation step."""

from __future__ import annotations

import csv
from datetime import timedelta

import pytest

from memecoin_agent.agent import Agent
from memecoin_agent.config import Config, ExitConfig, LearningConfig, RegimeConfig, lane_exits, load_config
from memecoin_agent.edge import MarketRegime, ScanHistory, conviction
from memecoin_agent.execution.paper import PaperExecutor
from memecoin_agent.models import Fill
from memecoin_agent.portfolio import Portfolio
from memecoin_agent.scanlog import COLS, ScanLog
from memecoin_agent.strategy import RUNNER_TAKE, evaluate_exit, lowcap_reasons, lp_removed_pct, runner_drawdown_limit

from .conftest import NOW, make_snap
from .test_agent import Clock, FakeMarket, FakeSafety

E = ExitConfig()


def position(price=0.001, cost=50.0, liq=80_000):
    pf = Portfolio(cash_usd=1000)
    pos = pf.open(make_snap(price_usd=price, liquidity_usd=liq), Fill(cost / price, cost, price), NOW, 0.7)
    return pf, pos


def at(minutes):
    return NOW + timedelta(minutes=minutes)


def step(pf, pos, price, minutes, e=E, **kw):
    snap = make_snap(price_usd=price, **kw)
    pf.mark(snap)
    return evaluate_exit(pos, snap, e, at(minutes))


def test_default_stop_is_30_percent_without_dip_reviews():
    pf, pos = position()
    assert step(pf, pos, 0.00072, 5) is None and pos.dip_since is None
    assert step(pf, pos, 0.00069, 6).reason == "stop loss (-31.0%)"


def test_stall_exit_frees_the_slot_of_a_coin_that_never_moved():
    pf, pos = position()
    assert step(pf, pos, 0.00102, 29) is None
    d = step(pf, pos, 0.00099, 31)
    assert d.fraction == 1.0 and d.reason.startswith("no follow-through (31m held, best +2%")
    pf, pos = position()  # one that was up 8% at some point is given its time
    assert step(pf, pos, 0.00108, 10) is None and step(pf, pos, 0.00099, 31) is None


def test_protect_sells_a_faded_winner_near_break_even_when_enabled():
    e = ExitConfig(protect_after_pct=15.0)
    pf, pos = position()
    assert step(pf, pos, 0.00118, 5, e) is None
    assert step(pf, pos, 0.00105, 6, e) is None
    assert step(pf, pos, 0.00100, 7, e).reason.startswith("gave its gain back (was +18%")
    pf, pos = position()  # off by default
    step(pf, pos, 0.00118, 5)
    assert step(pf, pos, 0.00100, 7) is None


def secured(price=0.00125):
    pf, pos = position()
    d = step(pf, pos, price, 5)
    assert d.secure and 0.89 < d.fraction < 0.91  # the initial + half the profit
    pos.secured_at, pos.runner_peak_usd = at(5), price
    return pf, pos


def test_runner_trail_tightens_small_winners_and_gives_big_ones_room():
    assert [runner_drawdown_limit(E, g) for g in (30, 99, 100, 299, 300, 900)] == [30, 30, 40, 40, 50, 50]
    assert runner_drawdown_limit(ExitConfig(runner_trail=[]), 500) == 70
    pf, pos = secured()
    assert step(pf, pos, 0.00160, 10) is None  # peak +60%
    assert step(pf, pos, 0.00115, 11) is None  # 28% off the peak: held
    assert step(pf, pos, 0.00111, 12).reason.startswith("runner 31% below its peak (limit 30%")


def test_runner_sells_half_once_at_plus_100_then_trails_wider():
    pf, pos = secured()
    d = step(pf, pos, 0.00205, 10)
    assert d.fraction == 0.5 and d.tp_level == RUNNER_TAKE and d.reason.startswith("runner take profit (+105%)")
    pos.tp_levels_hit.append(d.tp_level)
    assert step(pf, pos, 0.00260, 11) is None  # no second take
    assert step(pf, pos, 0.00160, 12) is None  # 38% off a +160% peak: inside the 40% tier
    assert step(pf, pos, 0.00150, 13).fraction == 1.0


def test_lane_overrides_and_validation(tmp_path):
    e = ExitConfig(lanes={"lowcap": {"hard_stop_pct": 35, "runner_trail": [{"above_pct": 0, "drawdown_pct": 45}]}})
    low = lane_exits(e, "lowcap")
    assert low.hard_stop_pct == 35 and runner_drawdown_limit(low, 10) == 45 and lane_exits(e, "momentum") is e
    with pytest.raises(ValueError, match="exits.lanes.lowcap: nope"):
        lane_exits(ExitConfig(lanes={"lowcap": {"nope": 1}}), "lowcap")
    f = tmp_path / "c.yaml"
    f.write_text("exits:\n  runner_trail:\n    - {above_pct: 0, drawdown_pct: 20}\n  lanes:\n    lowcap: {hard_stop_pct: 40}\n")
    cfg = load_config(f)
    assert cfg.exits.runner_trail[0].drawdown_pct == 20 and lane_exits(cfg.exits, "lowcap").hard_stop_pct == 40
    f.write_text("exits:\n  lanes:\n    lowcap: {stop: 40}\n")
    with pytest.raises(ValueError):
        load_config(f)


def test_liquidity_removal_is_told_apart_from_a_price_fall():
    pf, pos = position(liq=80_000)
    # Price -19%: a constant-product pool is ~10% smaller. Nothing was removed.
    fall = make_snap(price_usd=0.00081, liquidity_usd=72_000)
    assert lp_removed_pct(pos, fall) == 0 and evaluate_exit(pos, fall, E, at(5)) is None
    # Same price, a third of the pool gone: under the old 40%-below-entry rule this was still held.
    pulled = make_snap(price_usd=0.00081, liquidity_usd=50_000)
    assert 29 < lp_removed_pct(pos, pulled) < 32
    assert evaluate_exit(pos, pulled, E, at(6)).reason.startswith("liquidity being removed (31% of the pool gone")
    # A coin that doubled while its (one-sided) pool stayed flat is not a pull.
    assert lp_removed_pct(pos, make_snap(price_usd=0.002, liquidity_usd=80_000)) == 0


# -- market regime ---------------------------------------------------------------------------------
def test_regime_blocks_on_weak_breadth_and_on_a_falling_sol():
    cfg = RegimeConfig(min_coins=10)
    r = MarketRegime(cfg)
    r.observe(NOW, [make_snap(mint=f"U{i}", price_change_h1=20) for i in range(6)]
              + [make_snap(mint=f"D{i}", price_change_h1=-20) for i in range(6)])
    assert r.risk_off() is None and r.breadth == 0.5
    r = MarketRegime(cfg)
    r.observe(NOW, [make_snap(mint=f"U{i}", price_change_h1=20) for i in range(2)]
              + [make_snap(mint=f"D{i}", price_change_h1=-20) for i in range(10)])
    assert "only 17% of 12 scanned coins are up" in r.risk_off()
    r = MarketRegime(cfg)  # too few coins to judge
    r.observe(NOW, [make_snap(mint="D", price_change_h1=-20)])
    assert r.risk_off() is None
    prices = iter([100.0, 99.0, 95.0])
    r = MarketRegime(cfg, lambda: next(prices))
    for m in (0, 15, 30):
        r.observe(at(m), [])
    assert round(r.sol_change_h1, 6) == -5.0 and "SOL -5.0% over the last hour" in r.risk_off()


def agent_with(tmp_path, snaps, cfg=None, **kw):
    cfg = cfg or Config()
    cfg.agent.kill_switch_file = str(tmp_path / "STOP")
    market, clock = FakeMarket(snaps), Clock()
    return Agent(cfg, market, FakeSafety(), PaperExecutor(cfg.execution), Portfolio(cash_usd=1000), clock=clock,
                 persist=False, **kw), market, clock


def test_agent_waits_while_the_market_falls_and_buys_when_it_recovers(tmp_path):
    cfg = Config()
    cfg.regime.min_coins = 5
    good = make_snap(mint="GOOD")
    falling = [make_snap(mint=f"D{i}", price_change_h1=-30, liquidity_usd=1000) for i in range(9)]
    a, market, clock = agent_with(tmp_path, [good, *falling], cfg, regime=MarketRegime(cfg.regime))
    a.scan_and_enter(NOW)
    assert not a.pf.positions
    assert next(r for r in a.last_scan if r["mint"] == "GOOD")["outcome"].startswith("waiting: market falling")
    for s in falling:
        s.price_change_h1 = 30
    clock.t = at(20)  # the weak scans age out of the 15-minute window
    a.scan_and_enter(clock.t)
    assert "GOOD" in a.pf.positions


# -- track record ----------------------------------------------------------------------------------
def write_scans(path, rows):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow({**{c: "" for c in COLS}, "chain": "solana", **r})


def test_track_record_blocks_a_lane_that_keeps_rugging_and_scales_size(tmp_path):
    cfg = LearningConfig(min_lane_samples=10)
    path = tmp_path / "scans.csv"
    snap = make_snap()
    h = ScanHistory(cfg, path)
    assert h.judge(snap, "lowcap", NOW) == (None, 1.0, None)  # no log yet: no opinion
    write_scans(path, [{"lane": "lowcap", "passed": "1", "rugged": "1" if i < 7 else "0", "ret_1h": "-90", "max_ret": "5"}
                       for i in range(10)])
    why, factor, v = ScanHistory(cfg, path).judge(snap, "lowcap", NOW)
    assert why.startswith("skipped: track record: 10 earlier coins (passed the lowcap lane): 70% rugged")
    write_scans(path, [{"lane": "lowcap", "passed": "1", "rugged": "0", "ret_1h": "30", "max_ret": "150"} for _ in range(10)])
    why, factor, v = ScanHistory(cfg, path).judge(snap, "lowcap", NOW)
    assert why is None and factor == 1.25 and v.runners == 1.0
    write_scans(path, [{"lane": "lowcap", "passed": "1", "rugged": "0", "ret_1h": "-10", "max_ret": "5"} for _ in range(10)])
    assert ScanHistory(cfg, path).judge(snap, "lowcap", NOW)[1] == 0.8
    assert ScanHistory(cfg, path).judge(snap, "momentum", NOW)[1] == 1.0  # another lane: not enough rows


def test_conviction_sizes_by_score(tmp_path):
    assert conviction(0.55, 0.55, 0.75, 0.6) == 0.6 and conviction(0.9, 0.55, 0.75, 0.6) == 1.0
    assert conviction(0.65, 0.55, 0.75, 0.6) == 0.8
    a, _, _ = agent_with(tmp_path, [make_snap(mint="GOOD")])
    a.scan_and_enter(NOW)
    pos = a.pf.positions["GOOD"]
    assert 30 <= pos.initial_cost_usd < 50  # 5% of $1,000 scaled by its score


# -- low-cap lane ----------------------------------------------------------------------------------
def lowcap_snap(**kw):
    base = dict(mint="LOW", market_cap_usd=60_000, liquidity_usd=20_000, volume_h1=40_000, volume_m5=4_000,
                price_change_h1=30, price_change_m5=5, pair_created_at=NOW - timedelta(minutes=45))
    return make_snap(**{**base, **kw})


def test_lowcap_skips_spikes_and_fading_volume():
    cfg = Config()
    assert lowcap_reasons(lowcap_snap(), cfg.lowcap, cfg.filters, NOW) == []
    assert any("spiking" in r for r in lowcap_reasons(lowcap_snap(price_change_m5=55), cfg.lowcap, cfg.filters, NOW))
    assert any("fading" in r for r in lowcap_reasons(lowcap_snap(volume_m5=500), cfg.lowcap, cfg.filters, NOW))


class Scanner:
    def check(self, mint, qualified=None):
        from memecoin_agent.models import SafetyReport
        return SafetyReport(True, None, [])

    def result(self, mint):
        return None


def test_lowcap_buys_only_after_it_still_qualifies_30_seconds_later(tmp_path):
    s = lowcap_snap()
    a, market, clock = agent_with(tmp_path, [s], launch_scan=Scanner())
    a.scan_and_enter(NOW)
    assert not a.pf.positions and "confirming" in a.last_scan[0]["reasons"][0]
    s.price_usd *= 0.85  # slipped 15% while confirming: the clock starts again
    clock.t = NOW + timedelta(seconds=40)
    a.scan_and_enter(clock.t)
    assert not a.pf.positions and "slipped" in a.last_scan[0]["reasons"][0]
    clock.t = NOW + timedelta(seconds=80)
    a.scan_and_enter(clock.t)
    assert a.pf.positions["LOW"].lane == "lowcap" and a.pf.positions["LOW"].initial_cost_usd <= 25


# -- scan log --------------------------------------------------------------------------------------
def test_tick_follows_scanned_coins_up_and_writes_the_scan_log(tmp_path):
    weak = make_snap(mint="WEAK", liquidity_usd=1000)
    log = ScanLog(str(tmp_path / "scans.csv"), horizons=(60, 120, 180))
    a, market, clock = agent_with(tmp_path, [weak], scanlog=log)
    a.tick()
    assert "WEAK" in log.pending
    for m in (2, 4, 6):
        clock.t = at(m)
        weak.price_usd *= 1.1
        a.tick()
    rows = list(csv.DictReader(open(tmp_path / "scans.csv")))
    assert len(rows) == 1 and rows[0]["mint"] == "WEAK" and float(rows[0]["ret_15m"]) > 0 and rows[0]["vol_m5"] == "8000.0"
    assert (tmp_path / "scans_pending.json").exists()
