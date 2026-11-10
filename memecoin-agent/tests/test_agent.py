from __future__ import annotations

from datetime import timedelta
import threading
import time

from memecoin_agent.agent import Agent
from memecoin_agent.config import Config
from memecoin_agent.execution.paper import PaperExecutor
from memecoin_agent.models import SafetyReport
from memecoin_agent.portfolio import Portfolio
from memecoin_agent.simulation import SimClock, SyntheticMarket, SyntheticSafety

from .conftest import NOW, make_snap


class FakeMarket:
    def __init__(self, snaps):
        self.snaps = {s.mint: s for s in snaps}

    def discover(self):
        return list(self.snaps)

    def snapshots(self, mints):
        return {m: self.snaps[m] for m in mints if m in self.snaps}

    def sol_price_usd(self):
        return 150.0


class FakeSafety:
    def __init__(self, unsafe=()):
        self.unsafe = set(unsafe)

    def check(self, mint):
        bad = mint in self.unsafe
        return SafetyReport(ok=not bad, score=None, reasons=["danger"] if bad else [])


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


def make_agent(market, safety=None, cfg=None, tmp_path=None):
    cfg = cfg or Config()
    if tmp_path is not None:
        cfg.agent.kill_switch_file = str(tmp_path / "STOP")
    clock = Clock()
    agent = Agent(cfg, market, safety or FakeSafety(), PaperExecutor(cfg.execution),
                  Portfolio(cash_usd=1000), clock=clock, persist=False)
    return agent, clock


def test_agent_enters_safe_tokens_and_exits_on_rug(tmp_path):
    good, unsafe, weak = make_snap(mint="GOOD"), make_snap(mint="BAD"), make_snap(mint="WEAK", liquidity_usd=1000)
    market = FakeMarket([good, unsafe, weak])
    cfg = Config()
    cfg.risk.conviction_sizing = False
    agent, clock = make_agent(market, FakeSafety(unsafe={"BAD"}), cfg=cfg, tmp_path=tmp_path)

    agent.tick()
    assert set(agent.pf.positions) == {"GOOD"}
    assert agent.pf.cash_usd == 950

    # Liquidity pulled -> full exit, then cooldown prevents re-entry.
    clock.t += timedelta(minutes=1)
    market.snaps["GOOD"] = make_snap(mint="GOOD", price_usd=0.0002, liquidity_usd=5_000)
    agent.tick()
    assert not agent.pf.positions
    sells = [t for t in agent.pf.trades if t.side == "sell"]
    assert len(sells) == 1 and "liquidity pulled" in sells[0].reason and sells[0].realized_pnl_usd < 0

    market.snaps["GOOD"] = make_snap(mint="GOOD")
    for _ in range(4):
        clock.t += timedelta(minutes=1)
        agent.tick()
    assert not agent.pf.positions


def test_kill_switch_blocks_entries(tmp_path):
    agent, _ = make_agent(FakeMarket([make_snap()]), tmp_path=tmp_path)
    (tmp_path / "STOP").touch()
    agent.tick()
    assert not agent.pf.positions


def test_max_open_positions_respected(tmp_path):
    cfg = Config()
    cfg.risk.max_open_positions = 2
    market = FakeMarket([make_snap(mint=f"M{i}") for i in range(5)])
    agent, _ = make_agent(market, cfg=cfg, tmp_path=tmp_path)
    agent.tick()
    assert len(agent.pf.positions) == 2


def test_market_outage_does_not_crash(tmp_path):
    class Broken(FakeMarket):
        def snapshots(self, mints):
            raise ConnectionError("down")

    agent, _ = make_agent(Broken([make_snap()]), tmp_path=tmp_path)
    agent.tick()
    assert agent.ticks == 1 and not agent.pf.positions


def test_sniper_discovery_does_not_wait_for_story_feed(tmp_path):
    class Story:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()
            self.calls = 0

        def discover(self, now):
            self.calls += 1
            self.started.set()
            assert self.release.wait(2)
            return ["STORY"]

    cfg, story = Config(), Story()
    cfg.sniper.enabled = True
    cfg.feeds.pumpportal_new_tokens = True
    agent, clock = make_agent(FakeMarket([]), cfg=cfg, tmp_path=tmp_path)
    agent.newscheck = story
    try:
        before = time.monotonic()
        assert agent.evaluate_candidates(clock()) == []
        assert time.monotonic() - before < .5
        assert story.started.wait(1)
    finally:
        story.release.set()
    for _ in range(100):
        if agent._cached_story_discovery(clock()) == ["STORY"]:
            break
        time.sleep(.01)
    assert story.calls == 1


def test_story_feed_failure_changes_client_only_on_agent_thread(tmp_path):
    class Unauthorized(Exception):
        status = 401

    class Story:
        def __init__(self):
            self.started = threading.Event()

        def discover(self, now):
            self.started.set()
            raise Unauthorized("expired key")

    cfg, story = Config(), Story()
    cfg.sniper.enabled = True
    cfg.feeds.pumpportal_new_tokens = True
    agent, clock = make_agent(FakeMarket([]), cfg=cfg, tmp_path=tmp_path)
    agent.newscheck = story
    agent.evaluate_candidates(clock())
    assert story.started.wait(1)
    for _ in range(100):
        with agent._story_discovery_lock:
            pending = agent._story_discovery_error
        if pending is not None:
            break
        time.sleep(.01)
    assert agent.newscheck is story  # the worker did not disable a live check mid-scan
    assert agent._cached_story_discovery(clock()) == []
    assert agent.newscheck is None  # the agent thread applies the failure on its next scan


def test_sniper_lane_is_evaluated_before_older_momentum_candidates(tmp_path):
    class Market(FakeMarket):
        def is_pregraduation(self, mint):
            return mint == "EARLY"

    cfg = Config()
    cfg.sniper.enabled = True
    cfg.feeds.pumpportal_new_tokens = True
    early = make_snap(mint="EARLY", pair_created_at=NOW - timedelta(minutes=1), liquidity_usd=8_000,
                      market_cap_usd=50_000, volume_h1=3_000, buys_h1=20, sells_h1=10,
                      price_change_h1=1)
    agent, _ = make_agent(Market([make_snap(mint="LATE"), early]), cfg=cfg, tmp_path=tmp_path)
    evals = agent.evaluate_candidates(NOW)
    assert [ev.snapshot.mint for ev in evals[:2]] == ["EARLY", "LATE"]
    assert evals[0].lane == "sniper" and evals[0].passed


def test_sniper_keeps_story_discovery_in_a_full_feed_scan(tmp_path):
    class Market(FakeMarket):
        def discover(self):
            return [f"FEED{i}" for i in range(30)]

    cfg = Config()
    cfg.sniper.enabled = True
    cfg.feeds.pumpportal_new_tokens = True
    cfg.agent.max_candidates_per_scan = 30
    market = Market([make_snap(mint=f"FEED{i}") for i in range(30)] + [make_snap(mint="STORY")])
    agent, _ = make_agent(market, cfg=cfg, tmp_path=tmp_path)
    agent.newscheck = object()
    agent._story_discovery_mints = ["STORY"]
    agent._story_discovery_next = time.monotonic() + 60
    scanned = {ev.snapshot.mint for ev in agent.evaluate_candidates(NOW)}
    assert "FEED0" in scanned and "STORY" in scanned
    assert len(scanned) == 30
    cfg.agent.max_candidates_per_scan = 1
    assert [ev.snapshot.mint for ev in agent.evaluate_candidates(NOW)] == ["FEED0"]


def test_synthetic_market_run_keeps_books_consistent(tmp_path):
    cfg = Config()
    cfg.agent.kill_switch_file = str(tmp_path / "STOP")
    clock = SimClock()
    market = SyntheticMarket(clock, seed=11)
    agent = Agent(cfg, market, SyntheticSafety(market), PaperExecutor(cfg.execution),
                  Portfolio(cash_usd=1000), clock=clock, persist=False)
    for _ in range(1500):
        clock.advance(cfg.agent.tick_seconds)
        market.step(cfg.agent.tick_seconds)
        agent.tick()
        assert len(agent.pf.positions) <= cfg.risk.max_open_positions
        assert agent.pf.cash_usd >= -1e-6
    buys = [t for t in agent.pf.trades if t.side == "buy"]
    sells = [t for t in agent.pf.trades if t.side == "sell"]
    assert buys and sells
    # Cash identity: start - spent + proceeds == cash.
    assert abs(1000 - sum(t.usd_value for t in buys) + sum(t.usd_value for t in sells) - agent.pf.cash_usd) < 1e-6
    # Realized PnL from the journal matches the ledger.
    assert abs(sum(t.realized_pnl_usd for t in sells) - agent.pf.total_realized_pnl()) < 1e-6

