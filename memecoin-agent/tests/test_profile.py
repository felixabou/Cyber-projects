"""Tech vs meme, the fake-chart check, memes on LarpCheck's free lookup, and early buys' wallet scans.

Cases are real tokens from a live sample (names, links, volumes, holder counts)."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from memecoin_agent.agent import Agent
from memecoin_agent.config import AuthenticityConfig, Config
from memecoin_agent.data.profile import TokenProfile, TokenProfiler, authenticity_reasons, classify
from memecoin_agent.data.wallets import SCANNING
from memecoin_agent.execution.paper import PaperExecutor
from memecoin_agent.models import SafetyReport
from memecoin_agent.portfolio import Portfolio

from .conftest import NOW, make_snap
from .test_agent import Clock, FakeMarket, FakeSafety
from .test_larpcheck import FakeWithReports, coin, held, review_gate


@pytest.mark.parametrize("name, symbol, desc, links, kind", [
    ("1EDGE", "1EDGE", "Official token of 1EDGE, the human-first launchpad.", ["https://1edge.app", "https://docs.1edge.app"], "tech"),
    ("RAYDIUM LAUNCHPAD", "RAYPAD", "", ["https://raypad.fun"], "tech"),
    ("HidraNet", "HNET", "", ["https://hidranet.pro", "https://github.com/hidranet"], "tech"),
    ("Fomo Labs", "FOMO", "", ["https://fomolabs.world"], "tech"),
    ("DeepSeek AI Agent", "DeepSeekAI", "", ["https://deepseek2025.club"], "tech"),
    ("SOLVET", "SOLVET", "", ["https://www.solvet.app"], "tech"),
    ("Large Milkers All Over", "LMAO", "Big energy, bold memes, and maximum vibes.", ["https://www.largemilkers.lol"], "meme"),
    ("Wifoween", "Wifoween", "", ["https://wifoween.fun", "https://x.com/wifoween"], "meme"),
    ("Trust in Trump", "TIT", "", ["https://trustintrump.site"], "meme"),
    ("Wizard Cat", "WizardCat", "", [], "meme"),
])
def test_classify_live_examples(name, symbol, desc, links, kind):
    got, why = classify(name, symbol, desc, links)
    assert got == kind, why


def test_extra_words():
    assert classify("Moon Rocket", "MOON", "", [])[0] == "meme"
    assert classify("Moon Rocket", "MOON", "", [], extra_words=["rocket"])[0] == "tech"


def snap(**kw):
    return make_snap(**kw)


@pytest.mark.parametrize("snap_kw, holders, risks, expect", [
    # ANT: $5.8M a day through $3.7k of liquidity, 758 holders, 85k trades.
    (dict(volume_h24=5_847_215, liquidity_usd=3_737, buys_h1=4_300, sells_h1=4_300, pair_created_at=NOW - timedelta(hours=10)),
     758, [], ["wash trading: volume is 156x", "wash trading: $7,714 of volume per holder", "bot churn: 11 trades"]),
    # MUSK2028: an hour old, $2.3M through $9.7k.
    (dict(volume_h24=2_318_437, liquidity_usd=9_690, pair_created_at=NOW - timedelta(minutes=70)),
     5_075, [], ["wash trading: volume is 205x"]),
    # LMAO, an active real token: 3h old, $1.9M through $97k, 5,131 holders.
    (dict(volume_h24=1_947_623, liquidity_usd=97_217, buys_h1=3_000, sells_h1=2_500, pair_created_at=NOW - timedelta(hours=3)),
     5_131, [], []),
    (dict(volume_h24=50_000, liquidity_usd=30_000), 60, [], ["only 60 holders (min 100)", "bot churn: 16 trades"]),
    (dict(volume_h24=50_000, liquidity_usd=30_000), 900, ["High holder correlation"], ["coordinated wallets"]),
])
def test_fake_chart_signals(snap_kw, holders, risks, expect):
    s = snap(**snap_kw)
    got = authenticity_reasons(s, TokenProfile(s.mint, holders=holders, risks=risks), AuthenticityConfig(), s.age_minutes(NOW))
    for e in expect:
        assert any(r.startswith(e) for r in got), (e, got)
    assert len(got) == len(expect), got


def test_profiler_reads_rugcheck_and_metadata_through_pinata():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.host == "api.rugcheck.xyz":
            return httpx.Response(200, json={"totalHolders": 812, "risks": [{"name": "Mutable metadata"}],
                                             "tokenMeta": {"uri": "https://ipfs.io/ipfs/bafkreiaadkokxi36itaunl2amcq5hxwqk3g6yrzlql7rvesavtlhotxosa"}})
        if req.url.host == "gateway.pinata.cloud":
            return httpx.Response(200, json={"description": "The fastest launchpad on Solana", "website": "https://x.example"})
        return httpx.Response(429, text="This IPFS gateway is switching to a service worker gateway only")

    clock = Clock()
    clock.t = 0.0
    prof = TokenProfiler(Config().classify, http=httpx.Client(transport=httpx.MockTransport(handler)), clock=lambda: 0.0)
    p = prof.profile(snap(mint="M1", name="Zap", symbol="ZAP"))
    assert p.holders == 812 and p.kind == "tech" and "description mentions launchpad" in p.kind_reasons
    assert p.description.startswith("The fastest") and "https://x.example" in p.links
    assert not any("ipfs.io" in u for u in seen)  # rewritten to the gateway that still serves JSON
    n = len(seen)
    prof.profile(snap(mint="M1", name="Zap", symbol="ZAP"))
    assert len(seen) == n  # cached


def test_profiler_survives_outages():
    prof = TokenProfiler(Config().classify, http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    p = prof.profile(snap(mint="M2", name="Doge Two", symbol="DOGE2"))
    assert p.holders is None and p.kind == "meme"


# -- LarpCheck: memes get the free lookup only --------------------------------------------------
def test_meme_lookup_only_tech_full_gate():
    fake = FakeWithReports({"BAD": coin("BAD", "LARP", "RIGGED")})
    gate = review_gate(fake, on_unchecked="check", require_verdict=True)
    gate.kinds.update({"MEME": "meme", "BAD": "meme", "TECH": "tech"})
    assert gate.check("MEME").ok  # unchecked meme: allowed, nothing started
    assert not gate.check("BAD").ok  # someone else's verdict still blocks it
    assert fake.started == []
    assert not gate.check("TECH").ok and fake.started == ["TECH"]  # tech: a check (a credit) as before
    off = review_gate(FakeWithReports({"BAD": coin("BAD", "LARP", "RIGGED")}), memes="off")
    off.kinds["BAD"] = "meme"
    assert off.check("BAD").ok


def test_held_meme_never_spends_a_credit():
    fake = FakeWithReports({"H2": coin("H2", "LIKELY LARP", "OK", "chkLOCKED2")}, open_reports=())
    gate = review_gate(fake, on_unchecked="check")
    pos = held("H1")
    pos.kind = "meme"
    assert gate.review_position(pos) is None and fake.started == []  # unchecked: no check started
    flagged = held("H2")
    flagged.kind = "meme"
    exit_ = gate.review_position(flagged)
    assert exit_ is not None and exit_.larp["reasoning"]["source"] == "summary"  # sold, but no report opened
    assert "open_report" not in [c[0] for c in fake.calls]


# -- the agent ---------------------------------------------------------------------------------
class FakeProfiler:
    def __init__(self, profile):
        self.p = profile

    def profile(self, s):
        return self.p


class FakeScanner:
    def __init__(self, report):
        self.report = report
        self.calls = []

    def check(self, mint, qualified_at=None):
        self.calls.append((mint, qualified_at))
        return self.report

    def result(self, mint):
        return None


class KindGate(FakeSafety):
    def __init__(self):
        super().__init__()
        self.kinds = {}
        self.checked = []

    def check(self, mint):
        self.checked.append(mint)
        return super().check(mint)


def agent_with(tmp_path, s, profile, scanner=None):
    cfg = Config()
    cfg.agent.kill_switch_file = str(tmp_path / "STOP")
    gate = KindGate()
    agent = Agent(cfg, FakeMarket([s]), gate, PaperExecutor(cfg.execution), Portfolio(cash_usd=1000), clock=Clock(),
                  persist=False, guard=gate, launch_scan=scanner, profiler=FakeProfiler(profile))
    return agent, gate


def test_fake_chart_is_skipped_before_any_paid_check(tmp_path):
    s = make_snap(mint="WASH", volume_h24=5_000_000, liquidity_usd=80_000)
    agent, gate = agent_with(tmp_path, s, TokenProfile("WASH", kind="meme", holders=758))
    agent.scan_and_enter(NOW)
    assert not agent.pf.positions and gate.checked == []
    assert agent.last_scan[0]["outcome"].startswith("unsafe: looks manipulated: wash trading")


def test_meme_kind_reaches_the_gate_position_and_scanner(tmp_path):
    s = make_snap(mint="OK1", volume_h24=500_000)
    agent, gate = agent_with(tmp_path, s, TokenProfile("OK1", kind="meme", holders=2_000))
    agent.scan_and_enter(NOW)
    assert agent.pf.positions["OK1"].kind == "meme" and gate.kinds["OK1"] == "meme"
    assert agent.last_scan[0]["kind"] == "meme"


@pytest.mark.parametrize("snap_kw, scanned", [
    (dict(pair_created_at=NOW - timedelta(minutes=8)), True),  # young
    (dict(market_cap_usd=30_000, liquidity_usd=15_000, volume_h1=30_000), True),  # small
    ({}, False),  # 3h old, $600k: no wallet scan
])
def test_early_and_small_buys_need_the_wallet_scan(tmp_path, snap_kw, scanned):
    s = make_snap(mint="E1", volume_h24=200_000, **snap_kw)
    scanner = FakeScanner(SafetyReport(False, None, [SCANNING]))
    agent, gate = agent_with(tmp_path, s, TokenProfile("E1", kind="meme", holders=2_000), scanner)
    agent.scan_and_enter(NOW)
    assert bool(scanner.calls) is scanned
    assert ("E1" in agent.pf.positions) is not scanned
    if scanned:
        assert scanner.calls[0] == ("E1", None)  # no launch-age check for early buys
        assert agent.last_scan[0]["outcome"] == f"waiting: {SCANNING}" and gate.checked == []
