"""Unchecked coins (chains no safety check covers) are paper-only, capped and labelled, with several independent guards:
the agent refuses them before any executor, every live executor refuses them on its own, and config can't get around
either."""

from __future__ import annotations

from datetime import timedelta

import pytest

from memecoin_agent.config import Config
from memecoin_agent.execution import ExecutionError, UncheckedLiveError, refuse_unchecked_live
from memecoin_agent.execution.jupiter import JupiterExecutor
from memecoin_agent.execution.paper import PaperExecutor
from memecoin_agent.execution.pumpportal import PumpPortalExecutor
from memecoin_agent.models import ALWAYS_UNCHECKED_CHAINS, UNCHECKED_MAX_USD, Position, is_unchecked

from .conftest import NOW, make_snap
from .test_caller import FakeDescriber, FakeWebhook, ely_snap, make_agent
from .test_multichain_lowcap import ChainGate, MultiDex, MultiMarket

ELY = ely_snap().mint


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def live_executor(cls):
    ex = object.__new__(cls)  # no wallet or RPC: the guard must refuse before touching either
    return ex


# -- the marker -----------------------------------------------------------------------------------------------

def test_marker_and_hard_coded_chains():
    assert "elysium" in ALWAYS_UNCHECKED_CHAINS and UNCHECKED_MAX_USD == 25.0
    assert is_unchecked(ely_snap()) and is_unchecked(make_snap(chain="elysium", unchecked=False))
    assert is_unchecked(make_snap(unchecked=True)) and not is_unchecked(make_snap())


# -- paper: capped and labelled ------------------------------------------------------------------------------

def test_unchecked_paper_buy_succeeds_under_the_cap_and_is_labelled():
    agent, hook, _, _ = make_agent([ely_snap()])
    agent.scan_and_enter(NOW)
    pos = agent.pf.positions[ELY]
    assert pos.lane == "unchecked" and pos.initial_cost_usd == pytest.approx(10.0)
    assert agent.pf.trades[-1].reason.startswith("UNCHECKED")
    assert hook.posts[0][0]["title"].startswith("UNCHECKED CALL")


def test_paper_executor_refuses_an_unchecked_buy_over_the_cap():
    ex = PaperExecutor(Config().execution, unchecked_cap_usd=10)
    assert ex.buy(ely_snap(), 10).usd_value == 10
    with pytest.raises(ExecutionError, match="over the \\$10.00 paper cap"):
        ex.buy(ely_snap(), 10.01)
    assert ex.buy(make_snap(), 500).usd_value == 500  # checked coins are not affected
    assert PaperExecutor(Config().execution, unchecked_cap_usd=1_000).unchecked_cap_usd == UNCHECKED_MAX_USD


# -- live: always refused -------------------------------------------------------------------------------------

@pytest.mark.parametrize("snap", [ely_snap(), make_snap(unchecked=True), make_snap(chain="elysium"),
                                  make_snap(chain="bsc", mint="0x" + "ab" * 20)])
def test_every_live_executor_refuses_unchecked_and_off_solana_buys(snap):
    with pytest.raises(UncheckedLiveError):
        refuse_unchecked_live(snap)
    for cls in (JupiterExecutor, PumpPortalExecutor):
        ex = live_executor(cls)
        assert ex.live is True
        with pytest.raises(UncheckedLiveError):
            ex.buy(snap, 10)
    with pytest.raises(UncheckedLiveError):
        live_executor(PumpPortalExecutor).buy_pregraduation(snap, 10)
    assert "refused" in live_executor(JupiterExecutor).preflight(snap, 10)
    assert "refused" in live_executor(PumpPortalExecutor).preflight_pregraduation(snap, 10)


def test_agent_refuses_unchecked_in_live_mode_before_any_executor():
    class LivePaper(PaperExecutor):
        live = True  # what a live executor reports

        def buy(self, snap, usd):  # pragma: no cover - must never be reached
            raise AssertionError("an unchecked coin reached a live executor")

    agent, hook, _, _ = make_agent([ely_snap()])
    agent.executor = LivePaper(Config().execution)  # mode still says paper: the executor's own flag is enough
    agent.scan_and_enter(NOW)
    assert not agent.pf.positions and not hook.posts
    assert agent.last_scan[0]["outcome"].startswith("refused:")

    live, _, _, _ = make_agent([ely_snap()], mode="live")
    live.scan_and_enter(NOW)
    assert not live.pf.positions


# -- config can't get around it --------------------------------------------------------------------------------

def test_config_cannot_raise_the_cap():
    cfg = Config()
    cfg.unchecked.position_usd = 1_000
    with pytest.raises(ValueError, match="hard limit"):
        cfg.validate()


def test_removing_elysium_from_the_lane_does_not_make_it_buyable_as_checked():
    cfg = Config()
    cfg.unchecked.enabled = False
    cfg.unchecked.chains = []
    cfg.lowcap.confirm_seconds = 0
    cfg.lowcap.quote_symbols = [*cfg.lowcap.quote_symbols, "HYPE"]
    cfg.agent.kill_switch_file = ""
    snap = ely_snap(market_cap_usd=60_000, liquidity_usd=20_000, volume_h1=30_000, buys_h1=400, sells_h1=200,
                    pair_created_at=NOW - timedelta(minutes=45))
    from memecoin_agent.agent import Agent
    from memecoin_agent.portfolio import Portfolio
    from .test_agent import Clock

    agent = Agent(cfg, MultiMarket([snap]), safety=ChainGate(), executor=PaperExecutor(cfg.execution),
                  portfolio=Portfolio(cash_usd=1_000), clock=Clock(), persist=False, multichain=MultiDex([snap]),
                  chain_safety=ChainGate())  # a chain check that (wrongly) passes everything
    agent.scan_and_enter(NOW)
    assert not agent.pf.positions
    row = next(x for x in agent.last_scan if x["mint"] == snap.mint)
    # It passed the low-cap filters and the (wrongly permissive) chain check, and the guard still refused it.
    assert row["passed"] and row["lane"] == "lowcap"
    assert row["outcome"] == "unsafe: elysium is UNCHECKED (no safety check); only the unchecked lane may paper-buy it"


def test_copy_and_long_term_lanes_refuse_unchecked_chains():
    agent, _, _, _ = make_agent([])
    assert "UNCHECKED" in agent.token_checks(ely_snap(), 10, NOW, "copy")
    assert "UNCHECKED" in agent.token_checks(ely_snap(), 10, NOW, "longterm")


def test_open_refuses_unchecked_on_a_live_executor_and_labels_paper_ones():
    agent, _, _, _ = make_agent([])
    pos = agent._open(ely_snap(), 10, NOW, "manual", "manual buy (dashboard)")
    assert pos.lane == "manual" and agent.pf.trades[-1].reason.startswith("UNCHECKED")
    with pytest.raises(ExecutionError):
        agent._open(ely_snap(mint="0x" + "e3" * 20), 50, NOW, "manual", "manual buy")  # over the paper cap
    agent.cfg.execution.mode = "live"
    with pytest.raises(UncheckedLiveError):
        agent._open(ely_snap(mint="0x" + "e4" * 20), 10, NOW, "manual", "manual buy")


# -- exits still work ---------------------------------------------------------------------------------------------

def test_unchecked_positions_can_still_be_sold():
    agent, _, _, _ = make_agent([ely_snap()])
    agent.scan_and_enter(NOW)
    assert agent.close_position(ELY, "manual close")
    assert ELY not in agent.pf.active_positions()


def test_live_executors_never_block_a_sell():
    class Rpc:
        def get_token_balance(self, owner, mint):
            return 0, 6

    ex = live_executor(JupiterExecutor)
    ex.rpc, ex.owner = Rpc(), "OWNER"
    pos = Position(mint=ELY, symbol="ELYCAT", pair_address="", opened_at=NOW, entry_price_usd=1.0, token_amount=10,
                   cost_basis_usd=10, initial_cost_usd=10, peak_price_usd=1.0, entry_liquidity_usd=1.0,
                   last_price_usd=1.0, last_liquidity_usd=1.0, lane="unchecked", chain="elysium")
    with pytest.raises(ExecutionError) as exc:
        ex.sell(pos, 1.0, None)
    assert not isinstance(exc.value, UncheckedLiveError) and "No ELYCAT balance" in str(exc.value)


# -- Discord ---------------------------------------------------------------------------------------------------------

def test_discord_never_shows_an_unchecked_coin_as_safety_approved(tmp_path):
    from memecoin_agent.caller import Caller
    from memecoin_agent.config import CallerConfig
    from memecoin_agent.models import SafetyReport

    hook = FakeWebhook()
    caller = Caller(CallerConfig(enabled=True), hook, FakeDescriber(), tmp_path / "c.json")
    caller.call(ely_snap(), NOW, "lowcap", "", 10, 0.001, SafetyReport(True, None, []), unchecked=False)
    embed = hook.posts[0][0]
    assert embed["title"].startswith("UNCHECKED CALL")
    safety = next(f["value"] for f in embed["fields"] if f["name"] == "Safety")
    assert "UNCHECKED" in safety and "Passed" not in safety
