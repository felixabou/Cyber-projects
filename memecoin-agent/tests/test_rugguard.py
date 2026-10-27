"""Rug and honeypot guards, from the charts the agent got caught on:
- "Robinhood": 46m old, 16,803 trades for $90k (a $5 average trade): bot spam, then a dev dump.
- "TREMP": one vertical candle, then a smooth climb on almost no volume (+1,856% in 6h).
plus token-settings honeypots and a Jupiter sell-back check."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx

from memecoin_agent.agent import Agent
from memecoin_agent.config import AuthenticityConfig, Config, ExecutionConfig, HoneypotConfig
from memecoin_agent.data.profile import TokenProfile, authenticity_reasons, mint_flags
from memecoin_agent.data.route import SellRouteCheck
from memecoin_agent.execution.paper import PaperExecutor
from memecoin_agent.portfolio import Portfolio

from .conftest import NOW, make_snap
from .test_agent import Clock, FakeMarket, FakeSafety

CLEAN = TokenProfile("M", kind="meme", holders=2_000)


def reasons(s, profile=CLEAN):
    return authenticity_reasons(s, profile, AuthenticityConfig(), s.age_minutes(NOW))


def test_robinhood_bot_spam():
    s = make_snap(volume_h1=90_000, buys_h1=11_644, sells_h1=5_159, volume_h24=90_000, liquidity_usd=48_000,
                  market_cap_usd=382_000, pair_created_at=NOW - timedelta(minutes=46), price_change_h6=770.0)
    got = reasons(s, TokenProfile("M", holders=2_038))
    assert any(r.startswith("bot spam: $5.36 average trade") for r in got), got
    assert any(r.startswith("already ran +770% in 6h") for r in got), got


def test_tremp_painted_staircase():
    s = make_snap(volume_h1=8_000, volume_h24=86_000, buys_h1=60, sells_h1=20, liquidity_usd=74_000,
                  market_cap_usd=795_000, price_change_h1=9.46, price_change_h6=1_856.0,
                  pair_created_at=NOW - timedelta(hours=2))
    got = reasons(s)
    assert any(r.startswith("already ran +1,856% in 6h") for r in got), got
    assert any(r.startswith("painted chart: up +9% in 1h on $8,000 of volume, 1.0% of its market cap") for r in got), got


def test_real_coins_pass():
    # LMAO from the live sample: 3h old, $427k last hour on a $1.24M cap, $20+ trades.
    s = make_snap(volume_h1=427_313, volume_h24=1_947_623, buys_h1=12_000, sells_h1=9_000, liquidity_usd=97_217,
                  market_cap_usd=1_235_923, price_change_h1=30.0, price_change_h6=180.0,
                  pair_created_at=NOW - timedelta(hours=3))
    assert reasons(s, TokenProfile("M", holders=5_131)) == []
    # An older coin trading quietly isn't "painted": that rule is for coins under a day old.
    old = make_snap(volume_h1=5_000, market_cap_usd=2_000_000, price_change_h1=8.0, pair_created_at=NOW - timedelta(days=5))
    assert not any("painted" in r for r in reasons(old))


def test_token_settings_honeypots():
    clean = {"mintAuthority": None, "freezeAuthority": None, "transferFee": {"pct": 0},
             "token_extensions": {"nonTransferable": False, "permanentDelegate": None, "defaultAccountState": None}}
    assert mint_flags(clean) == ([], 0.0)
    bad = {"mintAuthority": "Auth1", "freezeAuthority": "Auth2", "transferFee": {"pct": 10},
           "token_extensions": {"permanentDelegate": {"delegate": "X"}, "transferHook": {"programId": "H"},
                                "nonTransferable": True, "defaultAccountState": "frozen",
                                "pausableConfig": {"authority": "P", "paused": False}}}
    flags, fee = mint_flags(bad)
    assert fee == 10 and len(flags) == 7
    # pump.fun's own PUMP token: a hook authority but no hook program: nothing runs, not a honeypot.
    pump = {**clean, "token_extensions": {"transferHook": {"authority": "DMdB", "programId": None}, "pausableConfig": None}}
    assert mint_flags(pump) == ([], 0.0)
    prof = TokenProfile("M", holders=2_000, mint_flags=flags, transfer_fee_pct=fee)
    got = reasons(make_snap(), prof)
    assert "honeypot risk: freeze authority active (holders' tokens can be frozen)" in got
    assert "honeypot risk: 10% transfer fee (limit 5%)" in got


class FakeJupiter:
    def __init__(self, sell_back_ratio=0.97, no_sell=False, down=False):
        self.ratio, self.no_sell, self.down = sell_back_ratio, no_sell, down
        self.calls = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls += 1
        if getattr(self, "limit_once", False):
            self.limit_once = False
            return httpx.Response(429)
        if self.down:
            return httpx.Response(503)
        q = req.url.params
        amount = int(q["amount"])
        if q["inputMint"].startswith("So111"):
            return httpx.Response(200, json={"outAmount": str(amount * 1000)})
        if self.no_sell:
            return httpx.Response(400, json={"error": "Could not find any route", "errorCode": "COULD_NOT_FIND_ANY_ROUTE"})
        return httpx.Response(200, json={"outAmount": str(int(amount / 1000 * self.ratio))})


def route(fake, **kw):
    return SellRouteCheck(HoneypotConfig(**kw), ExecutionConfig(), lambda: 150.0,
                          client=httpx.Client(transport=httpx.MockTransport(fake)), clock=lambda: 0.0,
                          sleep=lambda s: None)


def test_sell_back_check():
    ok = FakeJupiter()
    rc = route(ok)
    assert rc.check("M", 50) is None
    assert rc.check("M", 50) is None and ok.calls == 2  # cached
    assert route(FakeJupiter(no_sell=True)).check("M", 50) == "honeypot check: Jupiter finds no route to sell it back"
    assert route(FakeJupiter(sell_back_ratio=0.7)).check("M", 50).startswith(
        "honeypot check: buying and selling straight back loses 30.0% (limit 12%)")
    assert route(FakeJupiter(down=True)).check("M", 50) == "honeypot check: Jupiter unreachable"
    assert route(FakeJupiter(down=True), fail_closed=False).check("M", 50) is None
    limited = FakeJupiter()
    limited.limit_once = True
    assert route(limited).check("M", 50) is None  # a 429 is retried once


class FakeRoute:
    def __init__(self, why):
        self.why = why

    def check(self, mint, usd):
        return self.why


def agent(tmp_path, market, route_check=None):
    cfg = Config()
    cfg.agent.kill_switch_file = str(tmp_path / "STOP")
    return Agent(cfg, market, FakeSafety(), PaperExecutor(cfg.execution), Portfolio(cash_usd=1000), clock=Clock(),
                 persist=False, route_check=route_check)


def test_no_buy_without_a_sell_route(tmp_path):
    a = agent(tmp_path, FakeMarket([make_snap(mint="HONEY")]), FakeRoute("honeypot check: Jupiter finds no route to sell it back"))
    a.scan_and_enter(NOW)
    assert not a.pf.positions and a.last_scan[0]["outcome"] == "unsafe: honeypot check: Jupiter finds no route to sell it back"
    b = agent(tmp_path, FakeMarket([make_snap(mint="FINE")]), FakeRoute(None))
    b.scan_and_enter(NOW)
    assert "FINE" in b.pf.positions


def test_no_buy_right_after_a_dump(tmp_path):
    market = FakeMarket([make_snap(mint="RUG", price_usd=0.004)])
    a = agent(tmp_path, market)
    a.entries_paused = True  # watching only
    a.scan_when_blocked = True
    a.scan_and_enter(NOW)  # sees it at its high
    a.entries_paused = False
    market.snaps["RUG"] = make_snap(mint="RUG", price_usd=0.001)  # the dev dump: -75%
    a.scan_and_enter(NOW + timedelta(minutes=20))
    assert not a.pf.positions
    assert a.last_scan[0]["outcome"] == "unsafe: just dumped: 75% below the high it hit 20m ago (limit 50%)"
    market.snaps["RUG"] = make_snap(mint="RUG", price_usd=0.001)
    a.scan_and_enter(NOW + timedelta(hours=3))  # the high is no longer within 2 hours: judged afresh
    assert "RUG" in a.pf.positions


def test_config_example_parses():
    from memecoin_agent.config import load_config

    cfg = load_config("config.example.yaml")
    assert cfg.honeypot.sell_route_check and cfg.authenticity.min_avg_trade_usd == 12
    assert json.dumps(cfg.authenticity.__dict__)
