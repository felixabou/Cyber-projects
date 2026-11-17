"""Rejection telemetry: every blocked candidate by stage, reason and lane; persistence; reset; Discord/dashboard."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from memecoin_agent.agent import Agent
from memecoin_agent.caller import Caller
from memecoin_agent.config import CallerConfig, Config
from memecoin_agent.execution.paper import PaperExecutor
from memecoin_agent.models import SafetyReport
from memecoin_agent.portfolio import Portfolio
from memecoin_agent.telemetry import STAGES, Telemetry, build_report, classify, key, slug

from .conftest import NOW, make_snap
from .test_agent import Clock
from .test_caller import FakeDescriber, FakeWebhook
from .test_multichain_lowcap import ChainGate, MultiDex, MultiMarket, bsc_snap

T0 = NOW.timestamp()


class Tick:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


# -- the recorder ----------------------------------------------------------------------------------------------

def test_counts_per_scan_per_lane_and_totals(tmp_path):
    tel = Telemetry(tmp_path, clock=Tick())
    tel.begin_scan(candidates=3, passed=2)
    assert tel.block("regime", "market_falling", lane="lowcap", mint="A", regime_state="market falling: SOL -5%")
    assert tel.block("history", "too_many_rugs", lane="lowcap", mint="B", history_verdict="40 earlier coins: 60% rugged")
    assert tel.block("filters", "mcap_too_low", mint="C")
    tel.end_scan()
    ls = tel.last_scan
    assert ls["candidates"] == 3 and ls["by_stage"]["regime"] == 1 and ls["by_stage"]["history"] == 1
    assert ls["by_lane"]["lowcap"] == {"regime": 1, "history": 1} and ls["by_lane"]["momentum"] == {"filters": 1}
    assert tel.totals["by_reason"] == {"regime/market_falling": 1, "history/too_many_rugs": 1, "filters/mcap_too_low": 1}
    rows = tel.rows()
    assert {r["rejection_stage"] for r in rows} == {"regime", "history", "filters"}
    assert rows[0]["regime_state"] == "market falling: SOL -5%" and rows[1]["history_verdict"].startswith("40 earlier")
    assert set(rows[0]) >= {"lane", "chain", "mint", "rejection_stage", "rejection_reason", "regime_state",
                            "history_verdict", "score", "intended_position_size"}
    with pytest.raises(ValueError):
        tel.block("vibes", "x")


def test_repeats_within_the_window_count_per_scan_but_log_once(tmp_path):
    clock = Tick()
    tel = Telemetry(tmp_path, dedupe_minutes=60, clock=clock)
    for _ in range(3):
        tel.begin_scan(1, 1)
        tel.block("regime", "market_falling", mint="A")
    assert tel.last_scan["by_stage"]["regime"] == 1 and tel.totals["by_stage"]["regime"] == 1
    assert len(tel.rows()) == 1
    clock.t += 3601
    tel.block("regime", "market_falling", mint="A")
    assert len(tel.rows()) == 2 and tel.totals["by_stage"]["regime"] == 2


def test_totals_survive_a_restart_and_reset_zeroes_them(tmp_path):
    tel = Telemetry(tmp_path, clock=Tick())
    tel.begin_scan(0, 0)
    tel.begin_scan(5, 1)
    tel.block("risk", "entry_block", mint="A")
    tel.bought("lowcap")
    tel.end_scan()
    again = Telemetry(tmp_path, clock=Tick())
    assert again.totals["scans"] == 2 and again.totals["empty_scans"] == 1 and again.totals["bought"] == 1
    assert again.totals["by_stage"]["risk"] == 1 and again.totals["by_stage"]["discovery"] == 1
    again.reset()
    fresh = Telemetry(tmp_path, clock=Tick())
    assert fresh.totals["scans"] == 0 and fresh.totals["by_stage"] == {s: 0 for s in STAGES}
    assert len(fresh.rows()) == 1  # the block log is kept
    assert json.loads((tmp_path / "telemetry.json").read_text(encoding="utf-8"))["bought"] == 0


def test_reason_helpers():
    assert slug("liquidity $5,000 < $15,000") == "liquidity"
    assert slug("mcap too low") == "mcap_too_low"
    assert key("b/s1h too low") == "b_s1h_too_low"
    assert classify("unsafe: larpcheck no credits: x (strict policy)") == ("larpcheck", "larpcheck_no_credits")
    assert classify("unsafe: rugcheck danger: Mint Authority")[0] == "safety"
    assert classify("waiting: market falling: SOL -5%") == ("regime", "market_falling")
    assert classify("skipped: track record: 40 earlier coins") == ("history", "track_record")
    assert classify("skipped: live trading is Solana-only (bsc paper-only)") == ("execution", "paper_only_chain")
    assert classify("buy failed: timeout") == ("execution", "buy_failed")


# -- the agent ------------------------------------------------------------------------------------------------

class Judge:
    def __init__(self, why=None):
        self.why = why

    def judge(self, snap, lane, now):
        class V:
            def text(self_inner):
                return "40 earlier coins (passed the lowcap lane): 60% rugged, average 1h -30%, 2% doubled"
        return (self.why, 1.0, V()) if self.why else (None, 1.0, None)


def agent_with(tmp_path, snaps, gate=None, history=None, **patch):
    cfg = Config()
    cfg.lowcap.confirm_seconds = 0
    cfg.agent.kill_switch_file = ""
    for k, v in patch.items():
        section, key = k.split("__")
        setattr(getattr(cfg, section), key, v)
    tel = Telemetry(tmp_path, clock=Tick())
    agent = Agent(cfg, MultiMarket(snaps), safety=ChainGate(), executor=PaperExecutor(cfg.execution),
                  portfolio=Portfolio(cash_usd=1_000), clock=Clock(), persist=False, multichain=MultiDex(snaps),
                  chain_safety=gate or ChainGate(), history=history, telemetry=tel)
    return agent, tel


def test_every_stage_is_recorded_from_a_real_scan(tmp_path):
    good, bad, rejected = bsc_snap(), bsc_snap("0x" + "cd" * 20), bsc_snap("0x" + "ef" * 20, liquidity_usd=1_000)
    agent, tel = agent_with(tmp_path, [good, bad, rejected], gate=ChainGate(unsafe={bad.mint}))
    agent.scan_and_enter(NOW)
    assert good.mint in agent.pf.positions and len(agent.pf.positions) == 1
    ls = tel.last_scan
    assert ls["bought"] == 1 and ls["by_stage"]["safety"] == 1 and ls["by_stage"]["filters"] == 1
    by_mint = {r["mint"]: r for r in tel.rows()}
    assert by_mint[bad.mint]["rejection_stage"] == "safety" and by_mint[bad.mint]["lane"] == "lowcap"
    assert by_mint[bad.mint]["intended_position_size"] == pytest.approx(Config().lowcap.max_position_usd)
    assert by_mint[rejected.mint]["rejection_stage"] == "filters"
    assert good.mint not in by_mint  # bought, not blocked
    assert tel.totals["bought"] == 1 and agent.trades_made in (0, 1)


def test_regime_and_history_blocks_never_count_as_trades(tmp_path):
    snap = bsc_snap()
    agent, tel = agent_with(tmp_path, [snap])
    agent._regime_state = "market falling: only 10% of 40 scanned coins are up over the last hour"
    agent.regime = None  # keep the forced state for this scan
    agent.scan_and_enter(NOW)
    assert not agent.pf.positions and tel.totals["bought"] == 0
    row = tel.rows()[-1]
    assert row["rejection_stage"] == "regime" and row["regime_state"].startswith("market falling")

    agent2, tel2 = agent_with(tmp_path / "h", [snap], history=Judge("skipped: track record: 40 earlier coins (limit 50% rugged)"))
    agent2.scan_and_enter(NOW)
    assert not agent2.pf.positions and tel2.totals["bought"] == 0
    row = tel2.rows()[-1]
    assert row["rejection_stage"] == "history" and row["rejection_reason"] == "too_many_rugs"
    assert "60% rugged" in row["history_verdict"]


def test_larpcheck_category_is_its_own_stage(tmp_path):
    class StrictLarp:
        def check(self, mint):
            return SafetyReport(False, None, ["larpcheck no key: no LarpCheck API key configured (strict policy)"],
                                category="larpcheck_no_key")

    snap = make_snap()
    agent, tel = agent_with(tmp_path, [snap])
    agent.safety = StrictLarp()
    agent.scan_and_enter(NOW)
    row = tel.rows()[-1]
    assert row["rejection_stage"] == "larpcheck" and row["rejection_reason"] == "larpcheck_no_key"


def test_risk_execution_and_discovery(tmp_path):
    snap = bsc_snap()
    agent, tel = agent_with(tmp_path, [snap])
    agent.entries_paused = True
    agent.scan_when_blocked = True
    agent.scan_and_enter(NOW)
    assert tel.rows()[-1]["rejection_stage"] == "risk" and tel.totals["entry_blocked_scans"] == 1

    live, tel2 = agent_with(tmp_path / "live", [snap], execution__mode="live")
    live.scan_and_enter(NOW)
    assert tel2.rows()[-1]["rejection_stage"] == "execution"

    empty, tel3 = agent_with(tmp_path / "empty", [])
    empty.scan_and_enter(NOW)
    assert tel3.totals["empty_scans"] == 1 and tel3.last_scan["by_stage"]["discovery"] == 1


def test_report_from_synthetic_rows(tmp_path):
    clock = Tick()
    tel = Telemetry(tmp_path, clock=clock)
    tel.begin_scan(4, 3)
    tel.block("filters", "mcap_too_low", lane="momentum", mint="F1", symbol="LOW")
    tel.block("safety", "rugcheck", lane="lowcap", mint="S1", symbol="RUG")
    tel.block("regime", "market_falling", lane="lowcap", mint="R1", symbol="FALL", regime_state="market falling: SOL -4.5%")
    tel.block("history", "low_returns", lane="launch", mint="H1", symbol="HIST", history_verdict="120 coins: avg 1h -25%")
    tel.end_scan()
    text = build_report(tel, 24)
    assert "By stage:" in text and "regime 1" in text and "history 1" in text
    assert "Blocked by the market regime: 1" in text and "FALL (lowcap): market falling: SOL -4.5%" in text
    assert "Blocked by the track record: 1" in text and "120 coins: avg 1h -25%" in text
    assert "1 scans" in text


# -- Discord ---------------------------------------------------------------------------------------------------

def test_recap_and_regime_notices(tmp_path):
    clock = Tick()
    tel = Telemetry(tmp_path / "t", clock=clock)
    tel.begin_scan(2, 2)
    tel.block("regime", "market_falling", mint="A")
    tel.block("history", "too_many_rugs", mint="B")
    hook = FakeWebhook()
    caller = Caller(CallerConfig(enabled=True, recap_hour_utc=NOW.hour), hook, FakeDescriber(), tmp_path / "c.json")
    caller.telemetry = tel
    caller.tick(NOW, lambda m: {})
    assert "market regime **1** · track record **1**" in hook.posts[-1][0]["description"]
    caller.notice("Market regime: new entries on hold (market falling)", warn=True)
    assert hook.posts[-1][0]["description"].startswith("Market regime: new entries on hold")


def test_regime_transition_posts_once(tmp_path):
    class Regime:
        def __init__(self):
            self.state = None

        def observe(self, now, snaps):
            pass

        def risk_off(self):
            return self.state

    hook = FakeWebhook()
    agent, _ = agent_with(tmp_path, [bsc_snap()])
    agent.caller = Caller(CallerConfig(enabled=True, recap_hour_utc=None), hook, FakeDescriber(), tmp_path / "c.json")
    agent.regime = Regime()
    agent.regime.state = "market falling: SOL -5.0% over the last hour (limit -4%)"
    agent.evaluate_candidates(NOW)
    agent.evaluate_candidates(NOW + timedelta(seconds=20))
    agent.regime.state = None
    agent.evaluate_candidates(NOW + timedelta(seconds=40))
    texts = [p[0]["description"] for p in hook.posts]
    assert len(texts) == 2 and "on hold" in texts[0] and "recovered" in texts[1]
