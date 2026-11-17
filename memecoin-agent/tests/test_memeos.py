"""MEME OS: X-Ray as the last check before a buy, bundle clusters on calls, credit caps and fallbacks."""

from __future__ import annotations

import httpx
import pytest

from memecoin_agent.caller import Caller
from memecoin_agent.config import CallerConfig, MemeosConfig
from memecoin_agent.data.memeos import MemeosClient, MemeosError, MemeosGate, load_api_key
from memecoin_agent.facts import FactGatherer

from .conftest import NOW, make_snap
from .test_caller import FakeDescriber, FakeWebhook, ely_snap, make_agent

# The response shapes on memeos.world's developer page.
HIGH_RISK = {"verdict": "HIGH RISK", "score": 22, "authorities": {"mint": "revoked", "freeze": "revoked"},
             "topHolders": [], "flags": ["bundled_supply", "fresh_wallets"]}
CAUTION = {"verdict": "CAUTION", "score": 48, "authorities": {"mint": "revoked", "freeze": "revoked"}, "flags": []}
BUNDLE = {"clusters": [{"rootFunder": "7xKX…9fQ2", "wallets": 4, "supplyPct": 82.3}], "linkedSupplyPct": 82.3}


class Api:
    def __init__(self, xray=None, status=200):
        self.xray = xray or {}
        self.status = status
        self.paths = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer sk_memeos_test"
        self.paths.append(req.url.path)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"status": self.status, "message": "nope"}})
        hdr = {"x-memeos-credits-remaining": "990"}
        if req.url.path == "/api/public/v1/account":
            return httpx.Response(200, json={"data": {"balance": 995}, "credits": {"cost": 0, "remaining": 995}}, headers=hdr)
        if req.url.path.startswith("/api/public/v1/bundle/"):
            return httpx.Response(200, json={"data": BUNDLE, "credits": {"cost": 35, "remaining": 960}}, headers=hdr)
        addr = req.url.path.rsplit("/", 1)[-1]
        if addr not in self.xray:
            return httpx.Response(400, json={"error": {"status": 400, "message": "unsupported address"}})
        return httpx.Response(200, json={"data": self.xray[addr], "credits": {"cost": 5, "remaining": 990}}, headers=hdr)


def gate(api, **cfg):
    client = MemeosClient("sk_memeos_test", http=httpx.Client(transport=httpx.MockTransport(api)))
    return MemeosGate(MemeosConfig(**cfg), client, clock=lambda: 0.0, today=lambda: "2026-11-17")


def test_high_risk_blocks_caution_passes_and_reports_are_cached():
    api = Api({"BAD": HIGH_RISK, "NEW": CAUTION})
    g = gate(api)
    why = g.block_reason("BAD")
    assert why.startswith("unsafe: MEME OS X-Ray HIGH RISK 22/100 (bundled supply, fresh wallets)")
    assert why.endswith("(https://memeos.world/xray/BAD)")
    assert g.block_reason("NEW") is None  # new coins are capped at CAUTION: never a reason to skip
    g.block_reason("BAD")
    assert api.paths.count("/api/public/v1/xray/BAD") == 1  # cached
    assert g.client.credits_left == 990
    assert gate(Api({"NEW": CAUTION}), min_score=50).block_reason("NEW").endswith("score under 50 (https://memeos.world/xray/NEW)")
    assert "bundled_supply" in gate(Api({"X": {**CAUTION, "flags": ["bundled_supply"]}}), reject_flags=["bundled_supply"]).block_reason("X")


def test_daily_caps_and_out_of_credits_let_the_other_checks_decide(caplog):
    api = Api({f"T{i}": CAUTION for i in range(5)})
    g = gate(api, max_xray_per_day=2)
    assert [g.xray(f"T{i}") is not None for i in range(4)] == [True, True, False, False]
    assert len(api.paths) == 2
    broke = gate(Api(status=402))
    assert broke.block_reason("A") is None and broke.block_reason("B") is None
    assert len(broke.client.http._transport.handler.paths) == 1  # off for the rest of the day after the 402
    assert "out of credits" in caplog.text
    no_bundle = gate(Api(status=403))
    assert no_bundle.bundle("A") is None and no_bundle.bundle("B") is None
    assert len(no_bundle.client.http._transport.handler.paths) == 1


def test_unsupported_addresses_are_quiet_and_errors_never_carry_the_key(caplog):
    g = gate(Api({}))
    assert g.block_reason("0xelysium") is None
    assert "failed" not in caplog.text

    def boom(req):
        raise httpx.ConnectError("could not reach memeos with Bearer sk_memeos_test")

    client = MemeosClient("sk_memeos_test", http=httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(MemeosError) as exc:
        client.account()
    assert "sk_memeos" not in str(exc.value)


def test_key_from_environment_or_file(monkeypatch, tmp_path):
    cfg = MemeosConfig(api_key_file=str(tmp_path / "key"))
    monkeypatch.delenv("MEMEOS_API_KEY", raising=False)
    assert load_api_key(cfg) is None
    (tmp_path / "key").write_text("sk_memeos_file\n")
    assert load_api_key(cfg) == "sk_memeos_file"
    monkeypatch.setenv("MEMEOS_API_KEY", "sk_memeos_env")
    assert load_api_key(cfg) == "sk_memeos_env"


def test_call_shows_xray_bundle_and_link(tmp_path):
    g = gate(Api({"MINT1": HIGH_RISK}))
    g.xray("MINT1")
    facts = FactGatherer(memeos=g, http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    hook = FakeWebhook()
    Caller(CallerConfig(enabled=True), hook, FakeDescriber(), tmp_path / "c.json", facts=facts).call(
        make_snap(), NOW, "lowcap", "", 25, 0.001)
    fields = {x["name"]: x["value"] for x in hook.posts[0][0]["fields"]}
    assert fields["MEME OS"].startswith("[X-Ray HIGH RISK 22/100](https://memeos.world/xray/MINT1)")
    assert "linked wallets hold 82% (1 funder cluster, biggest 4 wallets)" in fields["MEME OS"]
    assert "[MEME OS X-Ray](https://memeos.world/xray/MINT1)" in fields["Links"]
    assert "linked wallets hold 82%" in fields["Worst"] and "MEME OS X-Ray HIGH RISK 22" in fields["Worst"]


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def test_agent_runs_xray_first_on_other_chains_and_skips_high_risk(isolated):
    agent, hook, _, _ = make_agent([make_snap(chain="bsc", mint="0x" + "ab" * 20, quote_symbol="WBNB",
                                              market_cap_usd=80_000, liquidity_usd=20_000, volume_h1=30_000,
                                              pair_created_at=NOW.replace(hour=11, minute=15))])
    agent.memeos = gate(Api({"0x" + "ab" * 20: HIGH_RISK}))
    agent.scan_and_enter(NOW)
    assert not agent.pf.positions
    assert agent.last_scan[0]["outcome"].startswith("unsafe: MEME OS X-Ray HIGH RISK")


def test_agent_buys_on_caution_and_when_memeos_is_out_of_credits(isolated):
    agent, _, _, _ = make_agent([ely_snap()])
    agent.memeos = gate(Api({ely_snap().mint: CAUTION}))
    agent.scan_and_enter(NOW)
    assert ely_snap().mint in agent.pf.positions
    agent2, _, _, _ = make_agent([ely_snap()])
    agent2.memeos = gate(Api(status=402))
    agent2.scan_and_enter(NOW)
    assert ely_snap().mint in agent2.pf.positions


def test_low_balance_switches_xray_off_and_keeps_credits_from_bundles():
    class Low(Api):
        def __call__(self, req):
            resp = super().__call__(req)
            if req.url.path == "/api/public/v1/account":
                return httpx.Response(200, json={"data": {"balance": 125}})  # the 25 free checks
            return resp

    api = Low({"A": CAUTION})
    g = gate(api)
    assert g.start() == 125
    assert g.bundle("A") is None  # 125 < bundle_min_credits: credits stay with the buy checks
    assert g.xray("A") is not None
    assert not any("bundle" in p for p in api.paths)

    class Empty(Api):
        def __call__(self, req):
            return httpx.Response(200, json={"data": {"balance": 3}})

    empty = gate(Empty())
    assert empty.start() == 3
    assert empty.check("B") == empty.check("B") and empty.check("B").ok  # under one X-Ray's cost: off, others decide
    assert empty.client.http._transport.handler is not None


def test_larpcheck_then_memeos_then_the_original_checks(monkeypatch):
    from memecoin_agent import cli
    from memecoin_agent.config import Config
    from memecoin_agent.data.larpcheck import LarpCheckGate

    cfg = Config()
    cfg.jupiter.enabled = False
    monkeypatch.setenv("LARPCHECK_API_KEY", "lc_test")
    g = gate(Api({"BAD": HIGH_RISK}))
    safety = cli.build_safety(cfg, memeos=g)
    kinds = [type(c).__name__ for c in safety.checkers]
    assert isinstance(safety.checkers[0], LarpCheckGate) and kinds[1] == "MemeosGate" and kinds[2] == "RugCheckClient"
    rep = g.check("BAD")
    assert not rep.ok and rep.reasons[0].startswith("MEME OS X-Ray HIGH RISK")
