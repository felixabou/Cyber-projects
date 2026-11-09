"""NewsCheck: rundown parsing, the story gate, and the narrative lane's early entries."""

from __future__ import annotations

from datetime import timedelta

import httpx

from memecoin_agent.config import Config, NarrativeConfig
from memecoin_agent.data.newscheck import NewsCheckClient, Rundown, judge

from .conftest import NOW, make_snap
from .test_agent import FakeMarket, FakeSafety, make_agent

CFG = NarrativeConfig()


def report(mint, catalyst="VIRAL POST", conf=75, verdict="LIKELY REAL", score=70):
    return {"ca": mint, "chain": "solana", "symbol": "X", "catalyst": catalyst, "confidence": conf,
            "summary": "story", "checked_at": "2026-03-01T11:50:00+00:00", "url": "https://newscheck.app/c/" + mint,
            "legit_or_larp": {"verdict": verdict, "score": score, "total": score}}


def client(rundowns, plan_checks=0, cfg=CFG):
    calls = []

    def api(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        assert req.headers["authorization"] == "Bearer nc_test"
        path = req.url.path
        if path.endswith("/checks/latest"):
            return httpx.Response(200, json={"checks": list(rundowns.values())})
        if path.endswith("/me"):
            return httpx.Response(200, json={"account": {"checks_left": plan_checks}})
        if req.method == "POST":
            return httpx.Response(202, json={"status": "running"})
        mint = path.rsplit(":", 1)[-1]
        if mint in rundowns:
            return httpx.Response(200, json={"report": rundowns[mint]})
        return httpx.Response(404, json={"error": {"code": "not_checked", "message": "Nobody checked this token yet."}})

    t = [0.0]
    nc = NewsCheckClient(cfg, "nc_test", httpx.Client(transport=httpx.MockTransport(api)), clock=lambda: t[0])
    return nc, calls, t


def test_judge():
    now = NOW
    assert judge(CFG, Rundown.parse(report("A")), now)[0] == "strong"
    assert judge(CFG, Rundown.parse(report("A", catalyst="RED FLAG")), now)[0] == "block"
    assert judge(CFG, Rundown.parse(report("A", verdict="LIKELY LARP", score=30)), now)[0] == "block"
    assert judge(CFG, Rundown.parse(report("A", catalyst="NO NEWS")), now)[0] == "neutral"
    assert judge(CFG, Rundown.parse(report("A", conf=40)), now)[0] == "neutral"  # a story nobody is sure of
    old = judge(CFG, Rundown.parse(report("A")), NOW + timedelta(days=2))
    assert old[0] == "neutral"  # a stale story


def test_client_caches_misses_and_discovers():
    nc, calls, t = client({"A": report("A"), "B": report("B", catalyst="NO NEWS")})
    assert nc.rundown("solana", "A").catalyst == "VIRAL POST"
    assert nc.rundown("solana", "NOPE") is None and nc.rundown("solana", "NOPE") is None
    nc.rundown("solana", "A")
    assert len(calls) == 2  # the hit and the miss are cached
    assert nc.discover(NOW) == ["A"]  # only strong stories are discovered
    t[0] += 700
    nc.rundown("solana", "A")
    assert len(calls) == 4  # feed read above + refresh after the ttl


def test_call_budget_and_checks():
    paid = NarrativeConfig(max_new_checks_per_day=5)
    nc, calls, _ = client({}, plan_checks=2, cfg=paid)
    assert nc.request_check("solana", "C") and nc.checks_left == 1
    assert not nc.request_check("solana", "C")  # once an hour per coin
    free, _, _ = client({}, plan_checks=0, cfg=paid)
    assert not free.request_check("solana", "D")  # the free plan can't run checks


class ScanOk:
    def check(self, mint, qualified=None):
        from memecoin_agent.models import SafetyReport

        return SafetyReport(ok=True, score=None, reasons=[])

    def result(self, mint):
        return None


def young(**kw):
    return make_snap(pair_created_at=NOW - timedelta(minutes=20), market_cap_usd=60_000, liquidity_usd=20_000,
                     volume_h1=15_000, buys_h1=90, sells_h1=60, price_change_h1=25.0, **kw)


def agent_with(snaps, rundowns, **cfg_kw):
    cfg = Config()
    cfg.launch.enabled = False
    cfg.lowcap.enabled = False  # keep this fixture focused on the narrative lane
    for k, v in cfg_kw.items():
        setattr(cfg.narrative, k, v)
    nc, _, _ = client(rundowns)
    agent, clock = make_agent(FakeMarket(snaps), FakeSafety(), cfg)
    agent.newscheck = nc
    agent.launch_scan = ScanOk()  # the wallet scan every young coin gets
    return agent


def test_narrative_lane_buys_young_coin_with_a_story_and_skips_the_rest():
    snaps = [young(mint="STORY"), young(mint="LARP"), young(mint="UNCHECKED"), young(mint="QUIET")]
    agent = agent_with(snaps, {"STORY": report("STORY"), "QUIET": report("QUIET", catalyst="NO NEWS"),
                               "LARP": report("LARP", verdict="LARP", score=20)})
    agent.scan_and_enter(NOW)
    assert set(agent.pf.positions) == {"STORY"}
    pos = agent.pf.positions["STORY"]
    assert pos.lane == "narrative"
    assert agent.pf.trades[0].reason.startswith("narrative: viral post")


def test_story_gate_blocks_a_coin_that_passes_momentum():
    good = make_snap(mint="GOOD")  # passes the regular momentum filters
    agent = agent_with([good], {"GOOD": report("GOOD", catalyst="DEV EXIT")})
    agent.scan_and_enter(NOW)
    assert not agent.pf.positions
    assert agent.last_scan[0]["outcome"].startswith("narrative: dev exit")


def test_require_rundown():
    agent = agent_with([make_snap(mint="GOOD")], {}, require_rundown=True)
    agent.scan_and_enter(NOW)
    assert not agent.pf.positions
    agent2 = agent_with([make_snap(mint="GOOD")], {})
    agent2.scan_and_enter(NOW)
    assert "GOOD" in agent2.pf.positions  # no rundown is not a reason to refuse


