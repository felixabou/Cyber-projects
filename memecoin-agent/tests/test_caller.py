"""Discord caller, the unchecked-chain (Elysium) lane, and its discovery source."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest

from memecoin_agent.agent import Agent
from memecoin_agent.caller import Caller, Describer, DiscordWebhook, describe_from_gmgn
from memecoin_agent.config import CallerConfig, Config, UncheckedConfig
from memecoin_agent.data.unchecked import ChainSearchDiscovery
from memecoin_agent.execution.paper import PaperExecutor
from memecoin_agent.models import SafetyReport
from memecoin_agent.portfolio import Portfolio

from .conftest import NOW, make_snap
from .test_agent import Clock
from .test_multichain_lowcap import ChainGate, MultiDex, MultiMarket, bsc_snap

ELY = "0x" + "e1" * 20


class FakeWebhook:
    def __init__(self):
        self.posts = []

    def post(self, content="", embeds=None):
        self.posts.append(embeds or [])
        return True


class FakeDescriber:
    def __init__(self, text=None):
        self.text = text

    def describe(self, snap):
        return (self.text, "GMGN") if self.text else (None, "")


def ely_snap(mint=ELY, **kw):
    values = dict(mint=mint, chain="elysium", symbol="ELYCAT", name="Elysium Cat", quote_symbol="HYPE",
                  dex_id="uniswap", market_cap_usd=20_000, liquidity_usd=10_000, volume_h1=5_000,
                  buys_h1=80, sells_h1=40, price_change_h1=12.0, pair_created_at=NOW - timedelta(hours=2),
                  url="https://dexscreener.com/elysium/pair")
    values.update(kw)
    return make_snap(**values)


def make_caller(tmp_path, describer=None, **cfg):
    hook = FakeWebhook()
    return Caller(CallerConfig(enabled=True, **cfg), hook, describer or FakeDescriber(), tmp_path / "calls.json"), hook


# -- the report ---------------------------------------------------------------------------------------------

def test_checked_call_report_has_numbers_checks_and_description(tmp_path):
    caller, hook = make_caller(tmp_path, FakeDescriber("Elon posted about his China trip."))
    snap = make_snap()
    assert caller.call(snap, NOW, "lowcap", "low-cap lane", 25.0, 0.001, SafetyReport(True, 0.9, []))
    embed = hook.posts[0][0]
    assert embed["title"].startswith("CALL · LOW-CAP · $DOGE2") and embed["color"] == 0x2ECC71
    assert "Elon posted" in embed["description"]
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert snap.mint in fields["Contract"]
    assert fields["Market cap"] == "$600.00K" and fields["Liquidity"] == "$80.00K"
    assert "Passed every safety check" in fields["Safety"] and "0.90" in fields["Safety"]
    assert fields["Paper buy"].startswith("$25.00")
    assert "gmgn.ai/sol/token/" in fields["Links"]


def test_unchecked_call_is_marked_with_a_warning(tmp_path):
    caller, hook = make_caller(tmp_path)
    caller.call(ely_snap(), NOW, "unchecked", "", 10.0, 0.001, None, unchecked=True)
    embed = hook.posts[0][0]
    assert embed["title"].startswith("UNCHECKED CALL") and embed["color"] == 0xE67E22
    assert "WARNING: UNCHECKED CHAIN (ELYSIUM)" in embed["description"]
    safety = next(f["value"] for f in embed["fields"] if f["name"] == "Safety")
    assert "No honeypot or tax check exists for elysium" in safety and "$10" in safety
    assert "No description found" in embed["description"]


def test_same_coin_is_not_called_twice(tmp_path):
    caller, hook = make_caller(tmp_path)
    assert caller.call(make_snap(), NOW, "lowcap", "", 25, 0.001)
    assert not caller.call(make_snap(), NOW, "lowcap", "", 25, 0.001)
    assert len(hook.posts) == 1


# -- follow-ups and recap -----------------------------------------------------------------------------------

def test_follow_ups_post_once_each_and_survive_a_restart(tmp_path):
    caller, hook = make_caller(tmp_path, recap_hour_utc=None)
    caller.call(make_snap(), NOW, "lowcap", "", 25, 0.001)
    up = make_snap(price_usd=0.0015, market_cap_usd=900_000)
    fetch = lambda mints: {up.mint: up}

    caller.tick(NOW + timedelta(minutes=10), fetch)
    assert len(hook.posts) == 1
    caller.tick(NOW + timedelta(minutes=16), fetch)
    assert len(hook.posts) == 2 and "+50.0%" in hook.posts[1][0]["title"] and "15m" in hook.posts[1][0]["title"]
    caller.tick(NOW + timedelta(minutes=17), fetch)
    assert len(hook.posts) == 2

    again, hook2 = make_caller(tmp_path, recap_hour_utc=None)
    again.tick(NOW + timedelta(minutes=61), fetch)
    assert len(hook2.posts) == 1 and "1h" in hook2.posts[0][0]["title"]


def test_follow_up_with_no_price_says_so(tmp_path):
    caller, hook = make_caller(tmp_path, recap_hour_utc=None)
    caller.call(make_snap(), NOW, "lowcap", "", 25, 0.001)
    caller.tick(NOW + timedelta(minutes=16), lambda mints: {})
    assert "No price found" in hook.posts[1][0]["description"]


def test_daily_recap_runs_once_at_its_hour(tmp_path):
    caller, hook = make_caller(tmp_path, recap_hour_utc=13, follow_up_minutes=[60])
    caller.call(make_snap(), NOW, "lowcap", "", 25, 0.001)
    up = make_snap(price_usd=0.002)
    caller.tick(NOW.replace(hour=13, minute=5), lambda m: {up.mint: up})
    recap = [p for p in hook.posts if p and p[0]["title"].startswith("Daily recap")]
    assert len(recap) == 1 and "Calls: **1**" in recap[0][0]["description"] and "doubled: **1**" in recap[0][0]["description"]
    caller.tick(NOW.replace(hour=13, minute=30), lambda m: {up.mint: up})
    assert len([p for p in hook.posts if p and p[0]["title"].startswith("Daily recap")]) == 1


# -- webhook and descriptions -------------------------------------------------------------------------------

def test_webhook_retries_once_on_rate_limit_and_hides_the_url(caplog):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(429, json={"retry_after": 0.1}) if len(seen) == 1 else httpx.Response(204)

    hook = DiscordWebhook("https://discord.com/api/webhooks/1/SECRET", http=httpx.Client(transport=httpx.MockTransport(handler)),
                          sleep=lambda s: None)
    assert hook.post(embeds=[{"title": "x"}])
    assert len(seen) == 2 and seen[0]["allowed_mentions"] == {"parse": []}

    def boom(request):
        raise httpx.ConnectError("cannot reach https://discord.com/api/webhooks/1/SECRET")

    bad = DiscordWebhook("https://discord.com/api/webhooks/1/SECRET", http=httpx.Client(transport=httpx.MockTransport(boom)))
    assert not bad.post(embeds=[{"title": "x"}])
    assert "SECRET" not in caplog.text


def test_description_from_gmgn_top_level_or_nested():
    assert describe_from_gmgn({"description": " hi "}) == "hi"
    assert describe_from_gmgn({"link": {"intro": "nested"}}) == "nested"
    assert describe_from_gmgn({"symbol": "X"}) is None
    assert describe_from_gmgn(None) is None


def test_describer_falls_back_to_dexscreener_profiles():
    def handler(request):
        if request.url.path == "/token-profiles/latest/v1":
            return httpx.Response(200, json=[{"chainId": "solana", "tokenAddress": "MINT1", "description": "From Dex"}])
        return httpx.Response(200, json=[])

    class NoDesc:
        def info(self, mint, chain):
            return {"symbol": "X"}

    d = Describer(NoDesc(), http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert d.describe(make_snap()) == ("From Dex", "DexScreener")


# -- the unchecked (Elysium) lane ---------------------------------------------------------------------------

def make_agent(snaps, mode="paper"):
    cfg = Config()
    cfg.lowcap.confirm_seconds = 0
    cfg.execution.mode = mode
    cfg.agent.kill_switch_file = ""
    hook = FakeWebhook()
    caller = Caller(CallerConfig(enabled=True, recap_hour_utc=None), hook, FakeDescriber(), state_path=None)
    caller.state_path = caller.state_path.parent / f"calls_test_{id(caller)}.json"
    gate = ChainGate()
    agent = Agent(cfg, MultiMarket(snaps), safety=ChainGate(), executor=PaperExecutor(cfg.execution),
                  portfolio=Portfolio(cash_usd=1_000), clock=Clock(), persist=False,
                  multichain=MultiDex(snaps), chain_safety=gate, caller=caller)
    return agent, hook, gate, caller


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def test_elysium_coin_is_paper_bought_for_10_and_called_unchecked(isolated):
    agent, hook, gate, _ = make_agent([ely_snap()])
    agent.scan_and_enter(NOW)
    pos = agent.pf.positions[ELY]
    assert pos.lane == "unchecked" and pos.chain == "elysium"
    assert pos.initial_cost_usd == pytest.approx(10.0)
    assert "UNCHECKED" in agent.pf.trades[-1].reason
    assert gate.calls == []  # no safety check is pretended for it
    assert hook.posts and hook.posts[0][0]["title"].startswith("UNCHECKED CALL")


def test_elysium_coin_is_never_bought_in_live_mode(isolated):
    agent, hook, _, _ = make_agent([ely_snap()], mode="live")
    agent.scan_and_enter(NOW)
    assert not agent.pf.positions and not hook.posts
    assert agent.last_scan[0]["outcome"] == "skipped: elysium has no safety check; paper only"


def test_elysium_lane_keeps_its_size_and_activity_limits(isolated):
    agent, hook, _, _ = make_agent([ely_snap(liquidity_usd=500), ely_snap("0x" + "e2" * 20, quote_symbol="PEPE")])
    agent.scan_and_enter(NOW)
    assert not agent.pf.positions
    reasons = {x["mint"]: "; ".join(x["reasons"]) for x in agent.last_scan}
    assert "liquidity" in reasons[ELY] and "quoted in PEPE" in reasons["0x" + "e2" * 20]


def test_checked_chains_still_run_their_safety_check_and_get_called(isolated):
    agent, hook, gate, _ = make_agent([bsc_snap()])
    agent.scan_and_enter(NOW)
    assert gate.calls and agent.pf.positions
    assert hook.posts[0][0]["title"].startswith("CALL · LOW-CAP")


def test_a_failed_discord_post_does_not_undo_the_buy(isolated):
    agent, hook, _, caller = make_agent([ely_snap()])
    caller.webhook.post = lambda **kw: (_ for _ in ()).throw(RuntimeError("down"))
    agent.scan_and_enter(NOW)
    assert ELY in agent.pf.positions


# -- discovery ----------------------------------------------------------------------------------------------

def test_chain_search_keeps_pairs_on_the_chain_quoted_in_hype():
    pairs = [
        {"chainId": "elysium", "baseToken": {"address": "0xAAA" + "0" * 37}, "quoteToken": {"symbol": "HYPE"}},
        {"chainId": "elysium", "baseToken": {"address": "0xBBB" + "0" * 37}, "quoteToken": {"symbol": "PEPE"}},
        {"chainId": "base", "baseToken": {"address": "0xCCC" + "0" * 37}, "quoteToken": {"symbol": "WETH"}},
    ]
    calls = []

    def handler(request):
        calls.append(request.url.params["q"])
        return httpx.Response(200, json={"pairs": pairs})

    t = [0.0]
    d = ChainSearchDiscovery(UncheckedConfig(), http=httpx.Client(transport=httpx.MockTransport(handler)), clock=lambda: t[0])
    found = d.discover()
    assert found == ["0xaaa" + "0" * 37] and d.chain_of(found[0]) == "elysium"
    d.discover()
    assert calls == ["elysium"]  # not again before discover_every_s
    t[0] = 1_000
    d.discover()
    assert calls == ["elysium", "elysium"]
