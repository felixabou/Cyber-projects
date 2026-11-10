"""Trending discovery (Jupiter and GeckoTerminal lists) and the holders-leaving rule."""

from __future__ import annotations

import time

import httpx

from memecoin_agent.config import Config, JupiterConfig
from memecoin_agent.data.gmgn import ExtraDiscoveryMarket
from memecoin_agent.data.jupiter_tokens import reasons_for
from memecoin_agent.data.trending import TrendingDiscovery

NOW = 1_800_000_000.0


def created(hours_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - hours_ago * 3600))


def row(mint, mcap=60_000, liq=20_000, hours=2, **audit):
    return {"id": mint, "mcap": mcap, "liquidity": liq, "createdAt": created(hours),
            "audit": {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True, **audit}}


def client(calls, fail=()):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if any(f in str(req.url) for f in fail):
            return httpx.Response(429)
        if "geckoterminal" in req.url.host:
            return httpx.Response(200, json={"data": [
                {"attributes": {"fdv_usd": "80000", "reserve_in_usd": "25000"},
                 "relationships": {"base_token": {"data": {"id": "solana_GECKO"}}}},
                {"attributes": {"fdv_usd": "900000000", "reserve_in_usd": "5000000"},
                 "relationships": {"base_token": {"data": {"id": "solana_HUGE"}}}}]})
        return httpx.Response(200, json=[row("GOOD"), row("TINY", mcap=3_000), row("BIG", mcap=50_000_000),
                                         row("THIN", liq=900), row("OLD", hours=200),
                                         row("MINTABLE", mintAuthorityDisabled=False), row("BANNED")])
    return httpx.Client(transport=httpx.MockTransport(handler))


def discovery(calls, fail=()):
    cfg = Config()
    cfg.filters.blacklist = ["BANNED"]
    now = [0.0]
    t = TrendingDiscovery(cfg.discovery, cfg.filters, client(calls, fail), clock=lambda: now[0], wall=lambda: NOW)
    return t, now


def full_round(t, now):
    for _ in range(7):  # five Jupiter lists and two GeckoTerminal ones, one per call
        now[0] += 120 / 7 + 0.01
        found = t.discover()
    return found


def test_one_list_is_refreshed_per_call_and_coins_outside_the_window_are_dropped():
    calls: list[str] = []
    t, now = discovery(calls)
    assert t.discover() == ["GOOD"] and len(calls) == 1
    assert t.discover() == ["GOOD"] and len(calls) == 1  # not due yet: no call
    assert full_round(t, now) == ["GOOD", "GECKO"] and len(calls) == 8
    assert t.counts["jupiter toptrending/5m"] == 1 and t.counts["geckoterminal 5m"] == 1
    assert t.chain_of("GOOD") == "solana" and t.chain_of("NOPE") is None


def test_a_list_that_is_down_keeps_its_last_result():
    calls: list[str] = []
    t, now = discovery(calls)
    now[0] = -1.0
    full_round(t, now)
    t.http = client(calls, fail=("jup.ag", "geckoterminal"))
    assert full_round(t, now) == ["GOOD", "GECKO"]


def test_it_feeds_the_market_ahead_of_the_base_lists():
    class Base:
        def discover(self):
            return ["BASE", "GOOD"]

    t, now = discovery([])
    now[0] = -1.0
    full_round(t, now)
    assert ExtraDiscoveryMarket(Base(), t).discover() == ["GOOD", "GECKO", "BASE"]


def test_holders_leaving_blocks_a_buy():
    cfg = JupiterConfig()
    assert reasons_for(cfg, {"stats1h": {"holderChange": -1.5}}, NOW) == []
    assert reasons_for(cfg, {"stats1h": {"holderChange": -8.2}}, NOW) == ["holders down 8% in the last hour: they are leaving"]
