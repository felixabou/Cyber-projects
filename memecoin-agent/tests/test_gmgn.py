"""GMGN discovery + safety, against a mock of its OpenAPI (envelope verified live; fields per GMGN's docs)."""

from __future__ import annotations

import httpx
import pytest

from memecoin_agent.config import Config, FilterConfig, GmgnConfig
from memecoin_agent.data.gmgn import (
    UNAVAILABLE,
    ExtraDiscoveryMarket,
    GmgnClient,
    GmgnDiscovery,
    GmgnError,
    GmgnSafety,
    load_api_key,
)

CLEAN_SEC = {"renounced_mint": True, "renounced_freeze_account": True, "buy_tax": "0", "sell_tax": "0",
             "top_10_holder_rate": "0.18", "dev_team_hold_rate": "0.0", "creator_balance_rate": "0",
             "suspected_insider_hold_rate": "0.02", "rug_ratio": "0.05", "is_wash_trading": False,
             "rat_trader_amount_rate": "0.04", "bundler_trader_amount_rate": "0.06", "sniper_count": 3,
             "burn_status": "burn"}
INFO = {"stat": {"fresh_wallet_rate": "0.53", "top_10_holder_rate": "0.18"},
        "wallet_tags_stat": {"smart_wallets": 2, "renowned_wallets": 1}, "dev": {"creator_open_count": 31}}


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeGmgn:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.security = {"GOOD": CLEAN_SEC}
        self.rank = [{"address": "T1", "symbol": "ONE"}, {"address": "T2"}, {"address": "T1"}, {"symbol": "no-address"}]
        self.rate_limited = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.headers.get("x-apikey") != "gm_test":
            return httpx.Response(401, json={"code": 401, "error": "AUTH_INVALID", "message": "missing api key or client_id"})
        assert req.url.params.get("timestamp") and req.url.params.get("client_id")
        if self.rate_limited:
            return httpx.Response(429, headers={"x-ratelimit-reset": str(1_800_000_090)},
                                  json={"code": 429, "error": "RATE_LIMIT_EXCEEDED", "message": "slow down"})
        path = req.url.path
        if path == "/v1/market/rank":
            return httpx.Response(200, json={"code": 0, "data": {"rank": self.rank}})
        if path == "/v1/token/security":
            mint = req.url.params["address"]
            return httpx.Response(200, json={"code": 0, "data": self.security.get(mint, {})})
        if path == "/v1/token/info":
            return httpx.Response(200, json={"code": 0, "data": INFO})
        return httpx.Response(404, json={"code": 404, "error": "NOT_FOUND", "message": path})


def make_client(fake, key="gm_test", clock=None):
    return GmgnClient(key, client=httpx.Client(transport=httpx.MockTransport(fake)), clock=clock or Clock())


def test_auth_and_envelope_errors():
    fake = FakeGmgn()
    with pytest.raises(GmgnError, match="AUTH_INVALID"):
        make_client(fake, key="wrong").security("GOOD")
    assert make_client(fake).security("GOOD")["rug_ratio"] == "0.05"


def test_discovery_sends_the_agent_window_dedupes_and_refreshes_on_schedule():
    fake = FakeGmgn()
    mono = Clock(0.0)
    d = GmgnDiscovery(GmgnConfig(discover_every_s=120), FilterConfig(), make_client(fake), clock=mono)
    assert d.discover() == ["T1", "T2"]
    q = fake.requests[0].url.params
    assert q["chain"] == "sol" and q["interval"] == "1h" and q["order_by"] == "volume"
    assert q.get_list("filters") == ["renounced", "frozen", "not_wash_trading"]
    assert q["min_liquidity"] == "25000" and q["max_marketcap"] == "5000000"
    assert q["min_created"] == "20m" and q["max_created"] == "72h"
    fake.rank = [{"address": "T3"}]
    mono.t += 60
    assert d.discover() == ["T1", "T2"] and len(fake.requests) == 1  # cached between refreshes
    mono.t += 61
    assert d.discover() == ["T3"]


def test_rate_limit_backs_off_until_reset_and_discovery_keeps_last_set():
    fake = FakeGmgn()
    clock, mono = Clock(), Clock(0.0)
    client = make_client(fake, clock=clock)
    d = GmgnDiscovery(GmgnConfig(), FilterConfig(), client, clock=mono)
    assert d.discover() == ["T1", "T2"]
    fake.rate_limited = True
    mono.t += 121
    assert d.discover() == ["T1", "T2"]  # failure: last trending set, not nothing
    n = len(fake.requests)
    with pytest.raises(GmgnError, match="rate limited"):
        client.security("GOOD")  # still inside the window: not even sent
    assert len(fake.requests) == n
    fake.rate_limited = False
    clock.t += 91
    assert client.security("GOOD")


def test_extra_discovery_order_and_fault_isolation():
    class Src:
        def __init__(self, out):
            self.out = out

        def discover(self):
            if isinstance(self.out, Exception):
                raise self.out
            return self.out

    class Base(Src):
        def snapshots(self, mints):
            return {"x": 1}

        def sol_price_usd(self):
            return 150.0

    m = ExtraDiscoveryMarket(Base(["D1", "G1"]), Src(["G1", "G2"]), Src(RuntimeError("down")))
    assert m.discover() == ["G1", "G2", "D1"]
    assert m.snapshots(["a"]) == {"x": 1} and m.sol_price_usd() == 150.0


@pytest.mark.parametrize("field, value, reason", [
    ("rug_ratio", "0.62", "rug ratio 62% > 30%"),
    ("is_wash_trading", True, "wash trading"),
    ("is_wash_trading", "yes", "wash trading"),
    ("top_10_holder_rate", "0.55", "top-10 holders 55%"),
    ("dev_team_hold_rate", 0.2, "dev team holds 20%"),
    ("suspected_insider_hold_rate", "0.3", "insiders hold 30%"),
    ("bundler_trader_amount_rate", "0.45", "bundler volume 45%"),
    ("rat_trader_amount_rate", "0.5", "insider volume 50%"),
    ("sell_tax", "0.25", "sell tax 25%"),
])
def test_safety_rules(field, value, reason):
    fake = FakeGmgn()
    fake.security["BAD"] = {**CLEAN_SEC, field: value}
    safety = GmgnSafety(GmgnConfig(), make_client(fake))
    assert safety.check("GOOD").ok
    rep = safety.check("BAD")
    assert not rep.ok and any(reason in r for r in rep.reasons), rep.reasons


def test_info_rules_only_fetch_info_when_needed():
    fake = FakeGmgn()
    assert GmgnSafety(GmgnConfig(), make_client(fake)).check("GOOD").ok
    assert not any(r.url.path == "/v1/token/info" for r in fake.requests)  # no info-based rule: one call
    strict = GmgnSafety(GmgnConfig(max_fresh_wallet_rate=0.5, max_creator_launches=20, min_smart_wallets=3),
                        make_client(fake))
    reasons = strict.check("GOOD").reasons
    assert any("fresh wallets 53%" in r for r in reasons)
    assert any("creator's past launches 31 > 20" in r for r in reasons)
    assert any("smart-money holders 2 < 3" in r for r in reasons)


def test_missing_fields_dont_reject_and_outages_follow_fail_mode():
    fake = FakeGmgn()
    assert GmgnSafety(GmgnConfig(), make_client(fake)).check("UNKNOWN").ok  # GMGN has no data: other checks decide
    fake.rate_limited = True
    assert GmgnSafety(GmgnConfig(), make_client(fake)).check("GOOD").ok
    assert GmgnSafety(GmgnConfig(fail_closed=True), make_client(fake)).check("GOOD").reasons == [UNAVAILABLE]


def test_key_loading_and_wiring(monkeypatch, tmp_path):
    from memecoin_agent import cli
    from memecoin_agent.data.larpcheck import CompositeSafety

    cfg = Config()
    cfg.gmgn.api_key_file = str(tmp_path / ".env")
    monkeypatch.delenv("GMGN_API_KEY", raising=False)
    assert load_api_key(cfg.gmgn) is None and cli.build_gmgn(cfg) is None
    (tmp_path / ".env").write_text("GMGN_PRIVATE_KEY=-----BEGIN PRIVATE KEY-----\\nabc\\n\nGMGN_API_KEY=gm_file\n")
    assert load_api_key(cfg.gmgn) == "gm_file"  # the file gmgn-cli writes; the private key is ignored
    monkeypatch.setenv("GMGN_API_KEY", "gm_env")
    assert load_api_key(cfg.gmgn) == "gm_env"

    cfg.filters.require_rugcheck = False
    cfg.larpcheck.enabled = False
    gm = cli.build_gmgn(cfg)
    safety = cli.build_safety(cfg, gmgn=gm)
    assert isinstance(safety, CompositeSafety) and safety.find(GmgnSafety) is not None
    market, _ = cli.build_market(cfg, with_feed=False, gmgn=gm)
    assert isinstance(market, ExtraDiscoveryMarket)
    cfg.gmgn.enabled = False
    assert cli.build_gmgn(cfg) is None
