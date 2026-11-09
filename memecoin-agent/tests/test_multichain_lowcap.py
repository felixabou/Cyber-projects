"""Low-cap discovery and execution checks across EVM chains.

These tests deliberately use paper fills and mocked safety/data clients. They prove that a BSC/BNB
candidate reaches the new lane without bypassing honeypot checks, and that live mode refuses it until
an explicit chain executor exists.
"""

from __future__ import annotations

from datetime import timedelta

from memecoin_agent.agent import Agent
from memecoin_agent.config import Config
from memecoin_agent.data.dexscreener import canonical_chain
from memecoin_agent.execution.paper import PaperExecutor
from memecoin_agent.models import SafetyReport
from memecoin_agent.portfolio import Portfolio
from memecoin_agent.strategy import lowcap_reasons

from .conftest import NOW, make_snap
from .test_agent import Clock


class MultiMarket:
    def __init__(self, snapshots):
        self.snapshots_by_mint = {s.mint: s for s in snapshots}

    def discover(self):
        return list(self.snapshots_by_mint)

    def chain_of(self, mint):
        snap = self.snapshots_by_mint.get(mint)
        return snap.chain if snap else None

    def snapshots(self, mints):
        return {m: self.snapshots_by_mint[m] for m in mints
                if m in self.snapshots_by_mint and self.snapshots_by_mint[m].chain == "solana"}


class MultiDex:
    def __init__(self, snapshots):
        self.snapshots_by_mint = {s.mint: s for s in snapshots}
        self.lookups = []

    def snapshots_on(self, chain, addresses):
        chain = canonical_chain(chain)
        self.lookups.append((chain, list(addresses)))
        return {a: self.snapshots_by_mint[a] for a in addresses
                if a in self.snapshots_by_mint and canonical_chain(self.snapshots_by_mint[a].chain) == chain}


class ChainGate:
    def __init__(self, unsafe=()):
        self.unsafe = set(unsafe)
        self.calls = []

    def check(self, chain, address):
        self.calls.append((canonical_chain(chain), address))
        if address in self.unsafe:
            return SafetyReport(False, None, ["honeypot (it can't be sold)"])
        return SafetyReport(True, None, [])


def bsc_snap(mint="0x" + "ab" * 20, **overrides):
    values = dict(
        mint=mint,
        chain="bsc",
        quote_symbol="WBNB",
        market_cap_usd=80_000,
        liquidity_usd=20_000,
        volume_h1=30_000,
        pair_created_at=NOW - timedelta(minutes=45),
    )
    values.update(overrides)
    return make_snap(**values)


def make_agent(snaps, gate, mode="paper"):
    cfg = Config()
    cfg.execution.mode = mode
    cfg.agent.kill_switch_file = ""
    clock = Clock()
    market = MultiMarket(snaps)
    dex = MultiDex(snaps)
    agent = Agent(cfg, market, safety=ChainGate(), executor=PaperExecutor(cfg.execution),
                  portfolio=Portfolio(cash_usd=1_000), clock=clock, persist=False,
                  multichain=dex, chain_safety=gate)
    return agent, dex


def test_lowcap_rules_accept_bsc_but_keep_guardrails():
    snap = bsc_snap()
    assert lowcap_reasons(snap, Config().lowcap, Config().filters, NOW) == []
    assert any("liquidity" in r for r in lowcap_reasons(bsc_snap(liquidity_usd=5_000), Config().lowcap,
                                                         Config().filters, NOW))
    assert not any("chain" in r for r in lowcap_reasons(snap, Config().lowcap, Config().filters, NOW))
    assert any("quote" in r for r in lowcap_reasons(bsc_snap(quote_symbol="CAKE"), Config().lowcap,
                                                     Config().filters, NOW))


def test_lowcap_bsc_paper_buy_runs_chain_safety_and_caps_size():
    good, bad = bsc_snap(), bsc_snap("0x" + "cd" * 20)
    gate = ChainGate(unsafe={bad.mint})
    agent, dex = make_agent([good, bad], gate)
    agent.scan_and_enter(NOW)

    assert set(agent.pf.positions) == {good.mint}
    pos = agent.pf.positions[good.mint]
    assert pos.chain == "bsc" and pos.lane == "lowcap"
    assert pos.initial_cost_usd <= Config().lowcap.max_position_usd
    assert gate.calls == [("bsc", good.mint), ("bsc", bad.mint)]
    assert any("honeypot" in x["outcome"] for x in agent.last_scan if x["mint"] == bad.mint)
    assert dex.lookups and dex.lookups[0][0] == "bsc"


def test_lowcap_bsc_live_mode_fails_closed_without_chain_executor():
    snap = bsc_snap()
    gate = ChainGate()
    agent, _ = make_agent([snap], gate, mode="live")
    agent.scan_and_enter(NOW)

    assert not agent.pf.positions
    assert agent.last_scan[0]["outcome"] == "skipped: live trading is Solana-only (bsc needs a chain executor)"

