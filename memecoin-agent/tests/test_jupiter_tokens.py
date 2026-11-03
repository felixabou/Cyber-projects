"""Jupiter's token data as a pre-buy check (fields as served live by lite-api.jup.ag/tokens/v2)."""

from __future__ import annotations

import httpx

from memecoin_agent.config import JupiterConfig
from memecoin_agent.data.jupiter_tokens import JupiterTokens, JupiterTokenSafety, reasons_for, wash_turnover_limit

NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z
TWO_DAYS_AGO = "2027-01-13T08:00:00Z"
TEN_MIN_AGO = "2027-01-15T07:50:00Z"


def token(mint="GOOD", organic=72.0, dev_mints=2, holders=3000, mcap=600_000, liq=60_000, vol_1h=40_000,
          vol_24h=300_000, created=TWO_DAYS_AGO):
    return {"id": mint, "symbol": mint, "organicScore": organic, "organicScoreLabel": "medium", "holderCount": holders,
            "mcap": mcap, "liquidity": liq, "createdAt": created,
            "audit": {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True, "devMints": dev_mints},
            "stats1h": {"buyVolume": vol_1h / 2, "sellVolume": vol_1h / 2},
            "stats24h": {"buyVolume": vol_24h / 2, "sellVolume": vol_24h / 2}}


def test_rules():
    c = JupiterConfig()
    assert reasons_for(c, token(), NOW) == []
    # The live 🖖 coin's creator: 179,773 coins, at ~1,460 holders. A factory coin that found a real crowd
    # passes (live: DJT, 71,231 holders, from a 1,146-coin creator).
    assert reasons_for(c, token(dev_mints=179_773, holders=1462), NOW) == ["creator has minted 179,773 coins (coin factory)"]
    assert reasons_for(c, token(dev_mints=1146, holders=71_231), NOW) == []
    assert reasons_for(c, token(organic=0, vol_1h=167_000), NOW) == [
        "organic score 0/100 on $167,000 of 1h volume: mostly bot trading"]
    # Jupiter scores fresh coins 0 until it has looked at them, and quiet coins can't be judged.
    assert reasons_for(c, token(organic=0, vol_1h=167_000, created=TEN_MIN_AGO), NOW) == []
    assert reasons_for(c, token(organic=0, vol_1h=5_000), NOW) == []
    # Live "Wanted": $1.4M market cap, 38 holders.
    assert reasons_for(c, token(mcap=1_400_000, liq=1_322_000, holders=38, vol_24h=100_000), NOW) == [
        "$1,400,000 market cap held by 38 wallets (shell)"]
    # Turnover far past what the pool explains, among few holders, is wash; among many, a real rush.
    assert reasons_for(c, token(holders=120, liq=50_000, vol_24h=1_000_000), NOW) == [
        "24h volume 20x its pool among 120 holders: wash trading"]
    assert reasons_for(c, token(holders=1600, liq=61_800, vol_24h=1_300_000), NOW) == []
    assert reasons_for(c, token(holders=12, liq=0.0001, vol_24h=5_000_000), NOW) == [  # a near-empty pool
        "24h volume over 1,000x its pool among 12 holders: wash trading"]
    assert [wash_turnover_limit(x) for x in (50_000, 500_000, 5_000_000)] == [15.0, 12.0, 30.0]
    off = JupiterConfig(max_dev_mints=None, min_organic_score=None, shell_max_holders=0, wash_max_holders=0)
    assert reasons_for(off, token(dev_mints=10**6, organic=0, vol_1h=10**6, holders=1, mcap=10**7), NOW) == []


def test_batched_cached_lookups_and_outages():
    calls: list[list[str]] = []
    down = {"on": False}

    def api(req: httpx.Request) -> httpx.Response:
        if down["on"]:
            return httpx.Response(503)
        mints = req.url.params["query"].split(",")
        calls.append(mints)
        return httpx.Response(200, json=[token(m, dev_mints=5000 if m.startswith("BAD") else 1, holders=400) for m in mints
                                         if m != "UNKNOWN"])

    t = [0.0]
    jup = JupiterTokens(httpx.Client(transport=httpx.MockTransport(api)), ttl_s=120, gap_s=0,
                        clock=lambda: t[0], sleep=lambda s: None)
    mints = [f"M{i}" for i in range(150)]
    got = jup.fetch(mints + ["UNKNOWN"])
    assert len(got) == 150 and [len(c) for c in calls] == [100, 51]  # 100 a call
    jup.fetch(mints[:10] + ["UNKNOWN"])
    assert len(calls) == 2  # cached, the unknown mint too
    t[0] += 121
    jup.fetch(["M1"])
    assert len(calls) == 3

    safety = JupiterTokenSafety(JupiterConfig(), jup, clock=lambda: NOW)
    rep = safety.check("BADDEV")
    assert not rep.ok and rep.reasons == ["jupiter: creator has minted 5,000 coins (coin factory)"]
    assert safety.check("UNKNOWN").ok  # Jupiter doesn't know it: the other checks decide
    down["on"] = True
    t[0] += 500
    assert safety.check("M5").ok  # unreachable: same


def test_a_short_rate_limit_is_waited_out_once():
    answers = iter([httpx.Response(429, headers={"retry-after": "2"}), httpx.Response(200, json=[token("A")]),
                    httpx.Response(429, headers={"retry-after": "60"})])
    slept: list[float] = []
    jup = JupiterTokens(httpx.Client(transport=httpx.MockTransport(lambda req: next(answers))), gap_s=0,
                        sleep=slept.append)
    assert "A" in jup.fetch(["A"]) and slept == [2.0]
    try:
        jup.fetch(["B"])  # a long window isn't waited out: the caller treats Jupiter as unavailable
        raise AssertionError("should have raised")
    except httpx.HTTPStatusError:
        pass
    assert slept == [2.0]


def test_wired_before_gmgn_and_into_manual_buys():
    from memecoin_agent import cli
    from memecoin_agent.agent import Agent
    from memecoin_agent.config import Config
    from memecoin_agent.data.larpcheck import CompositeSafety
    from memecoin_agent.execution.paper import PaperExecutor
    from memecoin_agent.portfolio import Portfolio

    from .conftest import make_snap
    from .test_agent import FakeMarket

    cfg = Config()
    cfg.larpcheck.enabled = False
    safety = cli.build_safety(cfg)
    assert isinstance(safety, CompositeSafety) and isinstance(safety.checkers[1], JupiterTokenSafety)
    assert not any(isinstance(c, JupiterTokenSafety) for c in getattr(cli.build_safety(cfg, with_larpcheck=False),
                                                                     "checkers", []))  # the recorder skips it

    jup = JupiterTokens(httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=[token("MINT1", dev_mints=9999, holders=400)]))), gap_s=0)
    agent = Agent(cfg, FakeMarket([make_snap()]), CompositeSafety(JupiterTokenSafety(cfg.jupiter, jup, clock=lambda: NOW)),
                  PaperExecutor(cfg.execution), Portfolio(cash_usd=1000), persist=False)
    assert "jupiter: creator has minted 9,999 coins (coin factory)" in agent.manual_check(make_snap(), 20)
