"""Call report extras: holder risk, momentum, known traders, LarpCheck, Bubblemaps; the KOL tracker; scan log."""

from __future__ import annotations

import csv
from datetime import timedelta

import httpx

from memecoin_agent.caller import Caller
from memecoin_agent.config import CallerConfig, KolConfig
from memecoin_agent.data.kols import KolTracker
from memecoin_agent.data.larpcheck import Verdict
from memecoin_agent.facts import FactGatherer, TokenFacts, bubblemaps_url, judge
from memecoin_agent.models import SafetyReport
from memecoin_agent.scanlog import COLS, ScanLog

from .conftest import NOW, make_snap
from .test_caller import FakeDescriber, FakeWebhook


class FakeGmgn:
    def __init__(self, feed=None):
        self.feed = feed or []

    def security(self, mint, chain):
        return {"top_10_holder_rate": "0.25", "dev_team_hold_rate": "0", "suspected_insider_hold_rate": "0.01",
                "bundler_trader_amount_rate": "0.02"}

    def info(self, mint, chain):
        return {"holder_count": 518, "stat": {"fresh_wallet_rate": "0.27"},
                "wallet_tags_stat": {"sniper_wallets": 3, "renowned_wallets": 4, "smart_wallets": 2},
                "dev": {"creator_open_count": 1}}

    def trades_feed(self, kind, chain, limit):
        assert kind == "kol"
        return self.feed if chain == "sol" else []


def free_apis(request):
    host, path = request.url.host, request.url.path
    if host == "lite-api.jup.ag":
        return httpx.Response(200, json=[{"id": "MINT1", "holderCount": 600, "organicScore": 66.0,
                                          "audit": {"topHoldersPercentage": 30.0}}])
    if host == "api.rugcheck.xyz":
        return httpx.Response(200, json={"lpLockedPct": 100.0})
    if host == "api.geckoterminal.com":
        # 5-min candles [ts, o, h, l, c, v]: the last 15 minutes +72%, volume 14x the 15 minutes before
        rows = [[1, 1, 1, 1, 1, 2000], [2, 1, 1, 1, 1, 2000], [3, 1, 1, 1, 1, 2214],
                [4, 1.0, 1.4, 1, 1.3, 30000], [5, 1.3, 1.6, 1.2, 1.5, 30000], [6, 1.5, 1.8, 1.6, 1.72, 27000]]
        return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": rows}}})
    raise AssertionError(f"unexpected {host}{path}")


class FakeLarp:
    def __init__(self, verdict=None):
        self.last_verdicts = {}
        self.verdict = verdict
        self.client = self

    def lookup_coin(self, mint):
        return {"latest": self.verdict} if self.verdict else {"latest": None}


REAL = {"status": "done", "verdict": "LIKELY REAL", "score": 74, "headline": "Real team, live product",
        "url": "https://larpcheck.app/c/abc", "stamps": [{"id": "coin", "word": "OK", "score": 70}]}


def kol_feed(t0):
    return [
        {"maker": "W1", "side": "buy", "base_address": "MINT1", "timestamp": t0 - 1300, "maker_info": {"twitter_name": "Yami"}},
        {"maker": "W2", "side": "buy", "base_address": "MINT1", "timestamp": t0 - 900, "maker_info": {"twitter_name": "Latuche"}},
        {"maker": "W3", "side": "buy", "base_address": "MINT1", "timestamp": t0 - 800},
        {"maker": "W1", "side": "sell", "base_address": "MINT1", "timestamp": t0 - 300},
        {"maker": "W2", "side": "sell", "base_address": "MINT1", "timestamp": t0 - 200},
        {"maker": "W9", "side": "sell", "base_address": "MINT1", "timestamp": t0 - 100},  # sold without a seen buy
    ]


def tracker(t0, **cfg):
    return KolTracker(KolConfig(**cfg), FakeGmgn(kol_feed(t0)), clock=lambda: t0)


def test_kol_tracker_counts_buyers_and_who_already_sold():
    t0 = NOW.timestamp()
    k = tracker(t0)
    k.poll()
    s = k.stats("solana", "MINT1")
    assert (s.bought, s.sold) == (3, 2)
    assert [n for n, _ in s.names] == ["W3", "Latuche", "Yami"] or s.names[0][0] in ("W3",)
    assert k.stats("solana", "OTHER") is None
    assert k.skip_reason("solana", "MINT1") is None  # rule off by default
    on = tracker(t0, skip_sold_share=0.6)
    on.poll()
    assert on.skip_reason("solana", "MINT1") == "skipped: 2 of 3 KOLs who bought already sold"
    assert tracker(t0, skip_sold_share=0.6, skip_min_kols=4).skip_reason("solana", "MINT1") is None


def test_facts_from_every_source_and_the_rendered_report(tmp_path):
    t0 = NOW.timestamp()
    k = tracker(t0)
    k.poll()
    g = FactGatherer(FakeGmgn(), k, FakeLarp(REAL), http=httpx.Client(transport=httpx.MockTransport(free_apis)))
    snap = make_snap(links=["https://x.com/coin"])
    f = g.gather(snap)
    assert f.top10_pct == 25 and f.holders == 518 and f.fresh_pct == 27 and f.snipers == 3 and f.kol_holders == 4
    assert f.lp_locked_pct == 100 and f.organic_score == 66
    assert round(f.m15_pct) == 72 and round(f.vol15 / f.vol15_prev, 1) == 14.0
    assert f.kols.bought == 3 and f.larp["verdict"] == "LIKELY REAL"

    hook = FakeWebhook()
    caller = Caller(CallerConfig(enabled=True), hook, FakeDescriber(), tmp_path / "calls.json", facts=g)
    caller.call(snap, NOW, "lowcap", "low-cap lane", 25, 0.001, SafetyReport(True, None, []), score=0.62)
    e = hook.posts[0][0]
    assert e["title"] == "CALL · LOW-CAP · $DOGE2 · 6/10"
    fields = {x["name"]: x["value"] for x in e["fields"]}
    assert fields["Momentum"].startswith("15m +72.0%") and "14.0x prev 15m" in fields["Momentum"]
    assert "Holders 518" in fields["Holders & risk"] and "LP locked 100%" in fields["Holders & risk"]
    assert fields["Known traders (KOLs)"].startswith("3 bought · 2 already sold") and "Latuche" in fields["Known traders (KOLs)"]
    assert "LIKELY REAL 74/100" in fields["LarpCheck"] and "larpcheck.app/c/abc" in fields["LarpCheck"]
    assert "app.bubblemaps.io/sol/token/MINT1" in fields["Links"] and "[LarpCheck](https://larpcheck.app/c/abc)" in fields["Links"]
    assert fields["Best"].startswith("• LarpCheck LIKELY REAL") and "LP locked/burned 100%" in fields["Best"]


def test_no_larpcheck_link_when_nobody_checked_it_and_sources_failing_leave_fields_empty(tmp_path):
    def down(request):
        return httpx.Response(503)

    g = FactGatherer(None, None, FakeLarp(None), http=httpx.Client(transport=httpx.MockTransport(down)))
    f = g.gather(make_snap())
    assert f == TokenFacts(sources=[])
    hook = FakeWebhook()
    Caller(CallerConfig(enabled=True), hook, FakeDescriber(), tmp_path / "c.json", facts=g).call(
        make_snap(), NOW, "momentum", "", 25, 0.001)
    fields = {x["name"]: x["value"] for x in hook.posts[0][0]["fields"]}
    assert "LarpCheck" not in fields and "Momentum" not in fields and "Holders & risk" not in fields
    assert "LarpCheck" not in fields["Links"] and "Bubblemaps" in fields["Links"]
    assert "no socials" in fields["Worst"]


def test_bubblemaps_links_by_chain():
    assert bubblemaps_url("solana", "M") == "https://app.bubblemaps.io/sol/token/M"
    assert bubblemaps_url("bsc", "0xab") == "https://app.bubblemaps.io/bsc/token/0xab"
    assert bubblemaps_url("elysium", "0xab") is None


def test_judge_flags_the_vyra_style_risks():
    from memecoin_agent.data.kols import KolStats

    f = TokenFacts(top10_pct=35, dev_pct=8, bundlers_pct=20, fresh_pct=30, kols=KolStats(5, 5))
    best, worst = judge(make_snap(), f, age_min=4)
    assert "top 10 hold 35%" in worst and "5 of 5 KOLs already sold" in worst and len(worst) == 5
    assert best == []


def test_scan_log_records_kols_and_upgrades_an_old_header(tmp_path):
    path = tmp_path / "scans.csv"
    old = COLS[:COLS.index("kol_bought")] + COLS[COLS.index("kol_sold") + 1:]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=old)
        w.writeheader()
        w.writerow({c: "1" for c in old} | {"mint": "OLD"})

    class Ev:
        def __init__(self, s):
            self.snapshot, self.passed, self.reasons, self.lane, self.score = s, True, [], "lowcap", 0.5

    log = ScanLog(str(path), clock=lambda: NOW.timestamp())
    log.observe(NOW.timestamp(), [Ev(make_snap())], {}, {}, {"MINT1": {"kol_bought": 4, "kol_sold": 3}})
    assert log.pending["MINT1"]["kol_bought"] == 4
    log._write(log.pending.pop("MINT1"))
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert list(rows[0].keys()) == COLS
    assert rows[0]["mint"] == "OLD" and rows[0]["kol_bought"] == ""
    assert rows[1]["mint"] == "MINT1" and rows[1]["kol_bought"] == "4" and rows[1]["kol_sold"] == "3"
