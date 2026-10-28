"""Copy lane (tracked wallets), long-term lane, other chains, manual trades."""

from __future__ import annotations

from datetime import timedelta

import httpx

from memecoin_agent.agent import Agent
from memecoin_agent.config import Config, CopyConfig, LongTermConfig
from memecoin_agent.data.chain_safety import ChainSafety, goplus_reasons, gmgn_reasons
from memecoin_agent.data.copytrade import CopyTracker, vet_wallet
from memecoin_agent.data.dexscreener import DexScreenerClient, norm_address, parse_pair
from memecoin_agent.data.gmgn import GmgnClient
from memecoin_agent.data.longterm import DAY, Candle, LongTermVerdict, candle_reasons, large_from_distribution
from memecoin_agent.execution.paper import PaperExecutor
from memecoin_agent.models import SafetyReport
from memecoin_agent.portfolio import Portfolio

from .conftest import NOW, make_snap
from .test_agent import Clock, FakeMarket, FakeSafety

T0 = NOW.timestamp()
GEM = "0x" + "ab" * 20
BAD = "0x" + "cd" * 20


# -- wallets ---------------------------------------------------------------------------------------
def stats(win=0.2, buys=30, sells=25, pnl=0.4, age_days=200):
    return {"winrate": win, "buy_count": buys, "sell_count": sells, "pnl": pnl,
            "common": {"created_at": T0 - age_days * DAY}}


def test_wallet_vetting_follows_the_rules():
    c = CopyConfig()
    assert vet_wallet(c, stats(), T0)[0] == []
    assert vet_wallet(c, stats(win=0.10), T0)[0] == [] and vet_wallet(c, stats(win=0.30), T0)[0] == []  # inclusive
    assert vet_wallet(c, stats(win=0.62), T0)[0] == ["win rate 62% (wants 10-30%)"]
    assert vet_wallet(c, stats(win=0.05), T0)[0] == ["win rate 5% (wants 10-30%)"]
    assert vet_wallet(c, stats(age_days=40), T0)[0] == ["wallet 40 days old (min 60)"]
    assert "wallet age unknown" in vet_wallet(c, {**stats(), "common": {}}, T0)[0]
    assert vet_wallet(c, stats(buys=3, sells=2), T0)[0] == ["2 losing trades (min 3)", "5 trades in 30 days (min 10)"]
    assert vet_wallet(c, stats(pnl=-0.2), T0)[0] == ["30-day return -20%"]
    assert vet_wallet(CopyConfig(min_pnl=None), stats(pnl=-0.2), T0)[0] == []


class FakeGmgn:
    def __init__(self):
        self.activity: dict[str, list] = {}
        self.feed: list = []
        self.creators: dict[str, str] = {}
        self.stats: dict[str, dict] = {}

    def trades_feed(self, kind, chain, limit=100):
        return self.feed if kind == "smartmoney" else []

    def wallet_activity(self, chain, wallet, limit=20, types=("buy",)):
        return self.activity.get(wallet, [])

    def info(self, token, chain="sol"):
        return {"dev": {"creator_address": self.creators.get(token, "SOMEONE")}}

    def trending(self, interval, extra, chain="sol"):
        return [{"address": "HOT", "symbol": "HOT"}]

    def top_traders(self, chain, address, limit=50):
        return [{"address": w} for w in self.stats]

    def wallet_stats(self, chain, wallet, period="30d"):
        return self.stats[wallet]


def activity(token, usd=200.0, ts=T0 - 60, kind="buy", tx=None):
    return {"event_type": kind, "token": {"address": token, "symbol": token}, "cost_usd": usd, "timestamp": ts,
            "price_usd": 0.0001, "tx_hash": tx or f"{token}-{kind}-{ts}"}


def tracker(gm, **cfg):
    t = CopyTracker(CopyConfig(chains=["sol"], **cfg), gm, clock=lambda: T0)
    return t


def test_tracker_finds_and_vets_wallets():
    gm = FakeGmgn()
    gm.feed = [{"maker": "GOOD"}, {"maker": "INSIDER"}]
    gm.stats = {"GOOD": stats(), "INSIDER": stats(win=0.95), "YOUNG": stats(age_days=10)}
    t = tracker(gm)
    t.refresh()
    assert list(t.wallets) == [("sol", "GOOD")]
    assert {"sol:INSIDER", "sol:YOUNG"} <= set(t.rejected)
    w = t.wallets[("sol", "GOOD")]
    assert w.win_rate == 0.2 and w.losses == 20 and w.source == "GMGN smartmoney feed"


def test_only_real_buys_become_signals():
    gm = FakeGmgn()
    t = tracker(gm, wallets=["W1"])
    gm.creators["OWN"] = "W1"
    gm.activity["W1"] = [
        activity("GEM"),
        activity("AIRDROP", kind="transferIn"),  # sent to them: not a buy
        activity("OWN"),  # their own launch
        activity("OLD", ts=T0 - 3600),  # stale
        activity("DUST", usd=5),  # too small to mean anything
    ]
    t._next_feed = T0 + 100  # skip the feed this poll
    t.poll_once()
    sigs = t.signals()
    assert [s.token for s in sigs] == ["GEM"] and sigs[0].wallets == {"W1": 200.0}
    t.poll_once()  # the same transaction again: no double count
    assert t.signals()[0].usd == 200.0
    t.consume("sol", "GEM")
    assert t.signals() == []


def test_two_wallet_rule():
    gm = FakeGmgn()
    t = tracker(gm, wallets=["W1", "W2"], min_wallets=2)
    t._next_feed = T0 + 100
    gm.activity = {"W1": [activity("GEM", tx="a")], "W2": [activity("GEM", tx="b")]}
    t.poll_once()
    assert t.signals() == []
    t.poll_once()
    assert t.signals()[0].wallets.keys() == {"W1", "W2"}


def test_feed_buys_by_tracked_wallets_count_too():
    gm = FakeGmgn()
    t = tracker(gm, wallets=["W1"])
    gm.feed = [{"maker": "W1", "side": "buy", "base_address": "GEM", "amount_usd": 300, "timestamp": T0 - 30,
                "transaction_hash": "x", "base_token": {"symbol": "GEM"}},
               {"maker": "W1", "side": "sell", "base_address": "OTHER", "amount_usd": 300, "timestamp": T0 - 30},
               {"maker": "STRANGER", "side": "buy", "base_address": "NOPE", "amount_usd": 300, "timestamp": T0 - 30}]
    t.poll_once()
    assert [s.token for s in t.signals()] == ["GEM"]


# -- other chains ----------------------------------------------------------------------------------
def test_honeypot_flags_other_chains():
    assert goplus_reasons({"is_honeypot": "1", "sell_tax": "0.25", "buy_tax": "0"}) == ["honeypot (it can't be sold)", "sell tax 25%"]
    assert goplus_reasons({"is_honeypot": "0", "sell_tax": "0.01", "owner_change_balance": "1"}) == ["the owner can change balances"]
    assert gmgn_reasons({"is_honeypot": "yes"}) == ["honeypot (it can't be sold)"]
    assert gmgn_reasons({"is_honeypot": "no", "buy_tax": "0.03", "sell_tax": "0.03"}) == []
    # A chain neither source covers: fail closed.
    assert ChainSafety(None, http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))).check(
        "robinhood", "0xabc").ok is False


def test_evm_addresses_are_keyed_lowercase():
    assert norm_address("robinhood", "0xDED852De9fe9bA9b6f27f39e8e81CF851A5C79cc") == "0xded852de9fe9ba9b6f27f39e8e81cf851a5c79cc"
    assert norm_address("solana", "BBEost5HN6rwjLkUrvPwBWDZ1skW5FScxvhvWmEfpump").endswith("Efpump")
    snap = parse_pair({"chainId": "robinhood", "baseToken": {"address": "0xDED852De9fe9bA9b6f27f39e8e81CF851A5C79cc"},
                       "priceUsd": "0.001", "txns": {"h24": {"buys": 168, "sells": 174}}})
    assert snap.chain == "robinhood" and snap.mint.startswith("0xded8") and snap.buys_h24 == 168


def test_name_search_ranks_by_volume_not_fake_liquidity():
    pairs = [
        {"chainId": "ethereum", "baseToken": {"address": "0x" + "1" * 40, "symbol": "Bonk"}, "priceUsd": "0.006",
         "liquidity": {"usd": 335_000_000}, "volume": {"h24": 0}},
        {"chainId": "solana", "baseToken": {"address": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "symbol": "Bonk"},
         "priceUsd": "0.00002", "liquidity": {"usd": 300_000}, "volume": {"h24": 150_000}},
    ]
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"pairs": pairs})))
    found = DexScreenerClient(client=client).lookup("BONK")
    assert [s.chain for s in found] == ["solana", "ethereum"]


def test_gmgn_rate_budget_spaces_out_bursts():
    t = [1000.0]
    waits = []
    gm = GmgnClient("k", client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"code": 0, "data": {"list": []}}))),
        clock=lambda: t[0], sleep=lambda s: (waits.append(s), t.__setitem__(0, t[0] + s)), rate_per_s=5)
    for _ in range(4):
        gm.wallet_activity("sol", "W")  # weight 3 each, 5 a second, bucket of 5
    assert waits and abs(sum(waits) - (3 * 4 - 5) / 5) < 1e-6


# -- long-term -------------------------------------------------------------------------------------
def days(closes, start=T0 - 120 * DAY, volume=1000.0):
    return [Candle(start + i * DAY, c, c * 1.05, volume) for i, c in enumerate(closes)]


def test_long_term_chart_rules():
    c = LongTermConfig()
    now = T0
    # A slow bleed from 1.0 to 0.2 over 120 days, flat lately: qualifies.
    bleed = [1.0 - 0.8 * i / 110 for i in range(110)] + [0.2] * 10
    assert candle_reasons(c, days(bleed), now)[0] == []
    # Still near its high.
    assert candle_reasons(c, days([1.0] * 120), now)[0][0].startswith("only 5% below its high")
    # A rug candle this month.
    rug = bleed[:-5] + [0.08] * 5
    assert any("in one day this month" in r for r in candle_reasons(c, days(rug), now)[0])
    # Dead most days.
    quiet = days(bleed)
    for x in quiet[-30:-10]:
        x.volume_usd = 0
    assert any(r.startswith("traded on 10 of the last 30 days") for r in candle_reasons(c, quiet, now)[0])
    assert candle_reasons(c, days([1.0] * 10), now)[0] == ["only 10 days of price history"]


def test_large_holders_from_geckoterminal_buckets():
    # ROBINCAT: $988k cap, ranks 11-30 hold 14.5% (~$7k each), 31-50 hold 8.8% (~$4.4k each).
    dist = {"top_10": "24.0", "11_30": "14.46", "31_50": "8.85", "rest": "52.7"}
    assert large_from_distribution(dist, 987_568, 1_000) == 30
    assert large_from_distribution(dist, 987_568, 6_000) == 10
    assert large_from_distribution(dist, 50_000, 1_000) == 1


# -- the agent's lanes -----------------------------------------------------------------------------
class FakeTracker:
    def __init__(self, sigs):
        self.sigs = sigs
        self.consumed = []

    def signals(self):
        return [s for s in self.sigs if (s.chain, s.token) not in self.consumed]

    def consume(self, chain, token):
        self.consumed.append((chain, token))


class FakeMulti:
    def __init__(self, snaps):
        self.snaps = {s.mint: s for s in snaps}

    def snapshots_on(self, chain, addresses):
        return {a: self.snaps[a] for a in addresses if a in self.snaps and self.snaps[a].chain == chain}

    def lookup(self, q):
        return [s for s in self.snaps.values() if s.mint == q]


class FakeChainSafety:
    def __init__(self, bad=()):
        self.bad = set(bad)

    def check(self, chain, address):
        return SafetyReport(address not in self.bad, None, ["gmgn: honeypot (it can't be sold)"] if address in self.bad else [])


def lane_agent(tmp_path, sol=(), other=(), tracker=None, longterm=None, mode="paper"):
    cfg = Config()
    cfg.agent.kill_switch_file = str(tmp_path / "STOP")
    cfg.execution.mode = mode
    clock = Clock()
    a = Agent(cfg, FakeMarket(list(sol)), FakeSafety(), PaperExecutor(cfg.execution), Portfolio(cash_usd=1000),
              clock=clock, persist=False, multichain=FakeMulti(list(other)), copy_tracker=tracker,
              longterm=longterm, chain_safety=FakeChainSafety(bad={BAD}))
    return a


def sig(chain, token, wallets=None):
    from memecoin_agent.data.copytrade import CopySignal

    return CopySignal(chain, token, token, wallets or {"W1abcdef": 250.0}, T0)


def test_copy_lane_buys_low_caps_on_any_chain(tmp_path):
    low_sol = make_snap(mint="LOWSOL", market_cap_usd=60_000, liquidity_usd=20_000)
    big_sol = make_snap(mint="BIGSOL", market_cap_usd=900_000)
    base = make_snap(mint=GEM, chain="base", market_cap_usd=40_000, liquidity_usd=15_000)
    bad = make_snap(mint=BAD, chain="base", market_cap_usd=40_000, liquidity_usd=15_000)
    tr = FakeTracker([sig("sol", "LOWSOL"), sig("sol", "BIGSOL"), sig("base", GEM.upper().replace("0X", "0x")),
                      sig("base", BAD)])
    a = lane_agent(tmp_path, sol=[low_sol, big_sol], other=[base, bad], tracker=tr)
    a.enter_copy_signals(NOW)
    assert set(a.pf.positions) == {"LOWSOL", GEM}
    assert a.pf.positions[GEM].chain == "base" and a.pf.positions[GEM].lane == "copy"
    assert a.pf.trades[0].reason.startswith("copy: 1 tracked wallet(s) bought $250")
    outcomes = {o["mint"]: o["outcome"] for o in a.lane_outcomes}
    assert outcomes["BIGSOL"].startswith("skipped: not a low cap")
    assert outcomes[BAD] == "unsafe: gmgn: honeypot (it can't be sold)"
    assert len(tr.consumed) == 4
    # Held copy positions on other chains are priced from DexScreener on their chain.
    assert set(a._fetch([GEM, "LOWSOL"])) == {GEM, "LOWSOL"}


def test_other_chains_are_paper_only(tmp_path):
    base = make_snap(mint=GEM, chain="base", market_cap_usd=40_000, liquidity_usd=15_000)
    a = lane_agent(tmp_path, other=[base], tracker=FakeTracker([sig("base", GEM)]), mode="live")
    a.enter_copy_signals(NOW)
    assert not a.pf.positions
    assert a.lane_outcomes[-1]["outcome"] == "skipped: live trading is Solana-only (base is paper-only)"


class FakeLongTerm:
    def __init__(self, verdicts):
        self.verdicts = verdicts

    def qualified(self):
        return self.verdicts


def test_long_term_lane_buys_with_its_own_budget_and_slots(tmp_path):
    snaps = [make_snap(mint=f"0xlt{i}", chain="robinhood", market_cap_usd=900_000) for i in range(7)]
    verdicts = [LongTermVerdict("robinhood", s.mint, s.symbol, ok=True, facts={"age_days": 400}) for s in snaps]
    a = lane_agent(tmp_path, other=snaps, longterm=FakeLongTerm(verdicts))
    a.enter_longterm(NOW)
    held = [p for p in a.pf.positions.values() if p.lane == "longterm"]
    assert len(held) == 5 and all(abs(p.initial_cost_usd - 50) < 1e-9 for p in held)
    assert a.pf.active_positions() == {}  # doesn't use the 20 trading slots
    pos = held[0]
    e = a.exits_for(pos)
    assert e.secure_at_pct == 100 and e.max_hold_minutes is None and e.dip_watch_pct is None
    # Sold: not rebought for a week even though the scanner still likes it.
    a.sell_part(pos.mint, 1.0, "test")
    assert a.risk.token_blocked(a.pf, pos.mint, NOW + timedelta(days=6))


def test_manual_trades_any_chain(tmp_path):
    rc = make_snap(mint="0xded852de9fe9ba9b6f27f39e8e81cf851a5c79cc", chain="robinhood", symbol="ROBINCAT")
    a = lane_agent(tmp_path, other=[rc])
    msg = a.manual_buy("robinhood", "0xDED852De9fe9bA9b6f27f39e8e81CF851A5C79cc", 40, managed=False)
    assert msg.startswith("Bought $40.00 of ROBINCAT on robinhood; you trade it")
    pos = a.pf.positions[rc.mint]
    assert pos.lane == "manual" and not pos.managed and a.pf.cash_usd == 960
    a.sell_part(rc.mint, 0.5, "manual sell 50% (dashboard)")
    assert abs(pos.token_amount * pos.entry_price_usd - 20) < 0.5
    a.set_managed(rc.mint, True)
    assert pos.managed
    for bad, why in ((lambda: a.manual_buy("robinhood", rc.mint, 10, True), "Already holding"),
                     (lambda: a.manual_buy("base", "0x" + "2" * 40, 10, True), "No market data"),
                     (lambda: a.manual_buy("robinhood", "0x" + "3" * 40, 5000, True), "paper cash")):
        try:
            bad()
            raise AssertionError("should have refused")
        except ValueError as exc:
            assert why in str(exc)
    live = lane_agent(tmp_path, other=[rc], mode="live")
    try:
        live.manual_buy("robinhood", rc.mint, 10, True)
        raise AssertionError("should have refused")
    except ValueError as exc:
        assert "paper-only" in str(exc)


def test_telegram_buy_sell_agent(tmp_path):
    from memecoin_agent.config import TelegramConfig
    from memecoin_agent.telegram_bot import TelegramBot

    calls = []

    class Runner:
        view = {"positions": [{"mint": "0xabc", "symbol": "ROBINCAT"}]}

        def submit(self, action, **kw):
            calls.append((action, kw))
            return {"message": "ok"}

    bot = TelegramBot(TelegramConfig(), client=None, runner=Runner(), chats={1})
    assert bot.respond(1, "/buy 0xded8 50 robinhood") == "ok"
    assert calls[-1] == ("manual_buy", {"source": "Telegram", "chain": "robinhood", "address": "0xded8", "usd": 50.0,
                                        "managed": True})
    assert "Which chain" in bot.respond(1, "/buy 0xded8 50")
    bot.respond(1, "/buy BBEost5HN6rwjLkUrvPwBWDZ1skW5FScxvhvWmEfpump $25 manual")
    assert calls[-1][1]["chain"] == "solana" and calls[-1][1]["managed"] is False
    bot.respond(1, "/sell robincat 50%")
    assert calls[-1] == ("sell", {"source": "Telegram", "mint": "0xabc", "fraction": 0.5})
    bot.respond(1, "/agent ROBINCAT off")
    assert calls[-1] == ("set_managed", {"source": "Telegram", "mint": "0xabc", "managed": False})
