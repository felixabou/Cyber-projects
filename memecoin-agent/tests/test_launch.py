"""Launch lane: tokens with $400k+ volume in their first 30 minutes, cleared by a wallet scan first.

The scan is tested against a mock Solana RPC and RugCheck report shaped like the live ones
(checked against real pump.fun launches: a creator-only launch and a 15-wallet bundled one)."""

from __future__ import annotations

import json
import threading
import time
from datetime import timedelta

import httpx
import pytest

from memecoin_agent.agent import Agent
from memecoin_agent.config import Config, FilterConfig, LaunchConfig
from memecoin_agent.data.wallets import SCANNING, PacedRpc, WalletScan, WalletScanner
from memecoin_agent.execution.paper import PaperExecutor
from memecoin_agent.models import SafetyReport
from memecoin_agent.portfolio import Portfolio
from memecoin_agent.strategy import launch_reasons

from .conftest import NOW, make_snap
from .test_agent import Clock, FakeMarket, FakeSafety

MINT = "LAUNCHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxpump"
SUPPLY = 1_000_000_000.0


def launch_snap(**kw):
    """25 minutes old, $450k traded, $30k liquidity: fails the momentum lane (too young, too extended)."""
    base = dict(mint=MINT, symbol="LNCH", pair_created_at=NOW - timedelta(minutes=25), volume_h1=450_000,
                liquidity_usd=30_000, price_change_h1=900.0, price_change_m5=4.0)
    return make_snap(**{**base, **kw})


# -- the rule ----------------------------------------------------------------------------------
def test_launch_rule():
    lc, f = LaunchConfig(), FilterConfig()
    assert launch_reasons(launch_snap(), lc, f, NOW) == []
    assert launch_reasons(launch_snap(pair_created_at=NOW - timedelta(minutes=31)), lc, f, NOW) == ["not a fresh launch"]
    assert "launch volume $390,000 < $400,000" in launch_reasons(launch_snap(volume_h1=390_000), lc, f, NOW)
    assert "crashing: -35% in 5m" in launch_reasons(launch_snap(price_change_m5=-35), lc, f, NOW)
    assert any("liquidity" in r for r in launch_reasons(launch_snap(liquidity_usd=9_000), lc, f, NOW))
    assert any("quote" in r for r in launch_reasons(launch_snap(quote_symbol="QQQx"), lc, f, NOW))
    # Once qualified, age and volume aren't re-tested during the buy window; the rest is.
    old = launch_snap(pair_created_at=NOW - timedelta(minutes=40), volume_h1=100_000)
    assert launch_reasons(old, lc, f, NOW, qualified=True) == []
    assert launch_reasons(launch_snap(price_change_m5=-50), lc, f, NOW, qualified=True) == ["crashing: -50% in 5m"]


# -- the agent ---------------------------------------------------------------------------------
class FakeScanner:
    def __init__(self, verdicts):
        self.verdicts = list(verdicts)  # SafetyReports handed out in turn (the last one repeats)
        self.calls = []

    def check(self, mint, qualified_at=None):
        self.calls.append((mint, qualified_at))
        return self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]

    def result(self, mint):
        return WalletScan(mint, 0.0, bundle_wallets=1, bundle_bought_pct=6.6, bundle_held_pct=0.0, top10_pct=3.5)


class CountingSafety(FakeSafety):
    def __init__(self, unsafe=()):
        super().__init__(unsafe)
        self.checked = []

    def check(self, mint):
        self.checked.append(mint)
        return super().check(mint)


def launch_agent(tmp_path, scanner, safety=None, snaps=None):
    cfg = Config()
    cfg.agent.kill_switch_file = str(tmp_path / "STOP")
    clock = Clock()
    agent = Agent(cfg, FakeMarket(snaps or [launch_snap()]), safety or CountingSafety(), PaperExecutor(cfg.execution),
                  Portfolio(cash_usd=1000), clock=clock, persist=False, launch_scan=scanner)
    return agent, clock


def test_agent_buys_a_launch_once_the_wallet_scan_clears_it(tmp_path):
    ok = SafetyReport(ok=True, score=0.0, reasons=[])
    scanner = FakeScanner([SafetyReport(False, None, [SCANNING]), ok])
    safety = CountingSafety()
    agent, clock = launch_agent(tmp_path, scanner, safety)
    agent.scan_and_enter(NOW)
    assert not agent.pf.positions and agent.last_scan[0]["outcome"] == f"waiting: {SCANNING}"
    assert agent.last_scan[0]["lane"] == "launch" and safety.checked == []  # no LarpCheck credit while scanning
    clock.t = NOW + timedelta(seconds=20)
    agent.scan_and_enter(clock.t)
    assert MINT in agent.pf.positions and safety.checked == [MINT]
    buy = agent.pf.trades[-1]
    assert buy.reason.startswith("launch lane: $450,000 volume; bundles hold 0.0% (6.6% bought in the launch block")
    assert scanner.calls[0] == (MINT, NOW)  # judged against when it qualified


def test_rejected_or_unscanned_launches_are_not_bought(tmp_path, caplog):
    bad = SafetyReport(ok=False, score=40.0, reasons=["bundled launch: 15 wallets bought 87.3% in the launch block"])
    safety = CountingSafety()
    agent, clock = launch_agent(tmp_path, FakeScanner([bad]), safety)
    with caplog.at_level("INFO"):
        for i in range(3):  # three scans of its buy window: one log line
            agent.scan_and_enter(NOW + timedelta(seconds=20 * i))
    assert not agent.pf.positions and safety.checked == []
    assert agent.last_scan[0]["outcome"].startswith("unsafe: bundled launch")
    assert sum("Skip LNCH (launch lane)" in r.getMessage() for r in caplog.records) == 1
    agent, _ = launch_agent(tmp_path, None)  # e.g. a backtest: no live wallet scan, no launch-lane buys
    agent.scan_and_enter(NOW)
    assert not agent.pf.positions and "need the live wallet scan" in agent.last_scan[0]["outcome"]


def test_buy_window_and_lanes_side_by_side(tmp_path):
    scanner = FakeScanner([SafetyReport(False, None, [SCANNING])])
    regular = make_snap(mint="REGULAR")  # passes the momentum lane; no wallet scan for it
    agent, clock = launch_agent(tmp_path, scanner, snaps=[launch_snap(), regular])
    agent.scan_and_enter(NOW)
    assert "REGULAR" in agent.pf.positions and MINT not in agent.pf.positions
    assert [c["lane"] for c in agent.last_scan] == ["launch", "momentum"]  # launches are tried first
    later = launch_snap(pair_created_at=NOW - timedelta(minutes=25), volume_h1=450_000)
    agent.market.snaps[MINT] = later
    for minutes, in_lane in ((10, True), (15, True), (16, False)):
        evs = {e.snapshot.mint: e for e in agent.evaluate_candidates(NOW + timedelta(minutes=minutes))}
        assert (evs[MINT].lane == "launch") is in_lane, minutes  # 41m old by then: only the window keeps it
    cfg_off = Config()
    cfg_off.launch.enabled = False
    assert not Agent(cfg_off, FakeMarket([launch_snap()]), FakeSafety(), PaperExecutor(cfg_off.execution),
                     Portfolio(cash_usd=1000), clock=Clock(), persist=False).evaluate_candidates(NOW)[0].passed


# -- the wallet scan ---------------------------------------------------------------------------
class FakeChain:
    """Mock Solana RPC + RugCheck for one token: `pages` signature pages back to the launch block."""

    def __init__(self, launch_buys, holdings, launched=NOW - timedelta(minutes=24), pages=2, rugcheck=None):
        self.slot0 = 400_000_000
        self.launched = launched
        self.launch_buys = launch_buys  # [[(signer, tokens bought), ...] per transaction] in the launch block
        self.holdings = holdings  # wallet -> tokens held now
        self.pages = pages
        self.rugcheck = rugcheck or {}
        self.calls: list[str] = []
        self.fail: dict[str, int] = {}  # method -> how many times to answer 429 first
        self.gate: threading.Event | None = None  # holds getTokenSupply until set (background-scan test)

    def sigs(self):
        older = [{"signature": f"L{i}", "slot": self.slot0, "blockTime": int(self.launched.timestamp()), "err": None}
                 for i in range(len(self.launch_buys))]
        older.insert(0, {"signature": "S0", "slot": self.slot0 + 1, "blockTime": int(self.launched.timestamp()) + 1,
                         "err": None})  # a sniper in the next block
        newer = [{"signature": f"N{i}", "slot": self.slot0 + 50 + i, "blockTime": 0, "err": None}
                 for i in range(1000 * (self.pages - 1))]
        return list(reversed(newer)) + older  # newest first, like the RPC

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.rugcheck.xyz":
            return httpx.Response(200, json={"token": {"supply": SUPPLY * 1e6, "decimals": 6}, **self.rugcheck})
        body = json.loads(req.content)
        method, params = body["method"], body["params"]
        self.calls.append(method)
        if self.fail.get(method):
            self.fail[method] -= 1
            return httpx.Response(429, json={"error": "slow down"})
        if method == "getSignaturesForAddress":
            all_sigs, before = self.sigs(), params[1].get("before")
            start = next(i for i, s in enumerate(all_sigs) if s["signature"] == before) + 1 if before else 0
            return ok(all_sigs[start:start + params[1]["limit"]])
        if method == "getTransaction":
            sig = params[0]
            if sig == "S0":
                return ok(tx([("SNIPER", 5_000_000.0)]))
            return ok(tx(self.launch_buys[int(sig[1:])]))
        if method == "getTokenSupply":
            if self.gate is not None:
                self.gate.wait(10)
            return ok({"value": {"uiAmount": SUPPLY}})
        if method == "getTokenAccountsByOwner":
            held = self.holdings.get(params[0], 0.0)
            return ok({"value": [{"account": {"data": {"parsed": {"info": {"tokenAmount": {"uiAmount": held}}}}}}]})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "nope"}})


def ok(result):
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


def tx(buys):
    """A transaction in which each (signer, amount) bought from a curve that doesn't sign."""
    keys = [{"pubkey": "FEEPAYER" if not buys else buys[0][0], "signer": True}]
    keys += [{"pubkey": w, "signer": True} for w, _ in buys[1:]] + [{"pubkey": "CURVE", "signer": False}]
    pre = [{"mint": MINT, "owner": "CURVE", "uiTokenAmount": {"uiAmount": SUPPLY}}]
    post = [{"mint": MINT, "owner": "CURVE", "uiTokenAmount": {"uiAmount": SUPPLY - sum(a for _, a in buys)}}]
    post += [{"mint": MINT, "owner": w, "uiTokenAmount": {"uiAmount": a}} for w, a in buys]
    return {"meta": {"preTokenBalances": pre, "postTokenBalances": post}, "transaction": {"message": {"accountKeys": keys}}}


def scanner_for(chain, cfg=None):
    client = httpx.Client(transport=httpx.MockTransport(chain))
    rpc = PacedRpc("https://rpc.example/?api-key=SECRET", client=client, gap_s=0, sleep=lambda s: None)
    return WalletScanner(cfg or LaunchConfig(), "unused", rpc=rpc, http=client, background=False)


def test_creator_only_launch_passes_and_reports_the_numbers():
    # Like the live LAUNCH token: the creator bought 6.6% in the launch block and sold since.
    chain = FakeChain([[("CREATOR", 66_000_000.0)]], holdings={"CREATOR": 0.0}, rugcheck={
        "topHolders": [{"owner": "POOL", "pct": 13.0}, *[{"owner": f"H{i}", "pct": 0.4} for i in range(12)]],
        "knownAccounts": {"POOL": {"type": "AMM"}}, "insiderNetworks": [], "creatorBalance": 0, "risks": []})
    sc = scanner_for(chain)
    rep = sc.check(MINT, qualified_at=NOW)
    assert rep.ok, rep.reasons
    res = sc.result(MINT)
    assert res.launched_at == (NOW - timedelta(minutes=24)).replace(microsecond=0)
    assert (res.bundle_wallets, round(res.bundle_bought_pct, 1), res.bundle_held_pct) == (1, 6.6, 0.0)
    assert round(res.sniper_bought_pct, 1) == 0.5 and round(res.top10_pct, 1) == 4.0  # the pool is left out
    assert chain.calls.count("getSignaturesForAddress") == 2  # walked back past a full page


def test_bundled_launch_is_rejected_even_after_the_bundle_sold():
    # Like the live CAKE token: 3 transactions, multi-wallet bundles, 72% of supply in the launch block.
    buys = [[("CREATOR", 60_000_000.0)],
            [("B1", 100_000_000.0), ("B2", 110_000_000.0), ("B3", 90_000_000.0)],
            [("B4", 200_000_000.0), ("B5", 160_000_000.0)]]
    chain = FakeChain(buys, holdings={"B4": 5_000_000.0}, rugcheck={
        "insiderNetworks": [{"size": 5, "currentHolding": 0.193 * SUPPLY * 1e6}], "topHolders": []})
    rep = scanner_for(chain).check(MINT, qualified_at=NOW)
    assert not rep.ok
    assert "bundled launch: 6 wallets bought 72.0% in the launch block and still hold 0.5% (limit 30%)" in rep.reasons
    assert "linked wallets hold 19.3%" not in " ".join(rep.reasons)  # under its own 30% limit


@pytest.mark.parametrize("launched, rugcheck, reason", [
    (NOW - timedelta(minutes=45), {}, "launched 45m before it reached the volume (limit 30m)"),
    (None, {"insiderNetworks": [{"currentHolding": 0.42 * SUPPLY * 1e6}]}, "linked wallets hold 42.0% > 30%"),
    (None, {"topHolders": [{"owner": "W", "pct": 30.0, "insider": True}, {"owner": "X", "pct": 9.0}]},
     "linked wallets hold 30.0% > 30%"),
    (None, {"topHolders": [{"owner": "W", "pct": 30.0}, {"owner": "X", "pct": 9.0}]}, "top 10 holders 39.0% > 35%"),
    (None, {"creatorBalance": 0.12 * SUPPLY * 1e6}, "creator holds 12.0% > 10%"),
    (None, {"rugged": True}, "RugCheck marks this token rugged"),
    (None, {"risks": [{"name": "Creator history of rugged tokens", "level": "danger"}]},
     "RugCheck: Creator history of rugged tokens"),
])
def test_scam_signals(launched, rugcheck, reason):
    rugcheck = {"topHolders": [], **rugcheck}
    if reason == "linked wallets hold 30.0% > 30%":  # exactly at the limit passes; one more insider tips it
        rugcheck["topHolders"].append({"owner": "Y", "pct": 0.5, "insider": True})
        reason = "linked wallets hold 30.5% > 30%"
    chain = FakeChain([[("CREATOR", 10_000_000.0)]], holdings={}, rugcheck=rugcheck,
                      **({"launched": launched} if launched else {}))
    rep = scanner_for(chain).check(MINT, qualified_at=NOW)
    assert not rep.ok and reason in rep.reasons, rep.reasons


def test_rpc_trouble_fails_closed_and_never_leaks_the_rpc_key():
    chain = FakeChain([[("CREATOR", 10_000_000.0)]], holdings={})
    chain.fail["getSignaturesForAddress"] = 2  # rate limited twice, then fine
    assert scanner_for(chain).check(MINT, qualified_at=NOW).ok
    deep = FakeChain([[("CREATOR", 10_000_000.0)]], holdings={}, pages=4)
    rep = scanner_for(deep, LaunchConfig(max_signature_pages=2)).check(MINT, qualified_at=NOW)
    assert not rep.ok and "couldn't reach the launch block" in rep.reasons[0]
    down = FakeChain([[("CREATOR", 10_000_000.0)]], holdings={})
    down.fail["getTokenSupply"] = 99
    rep = scanner_for(down).check(MINT, qualified_at=NOW)
    assert rep.reasons == ["wallet scan failed: getTokenSupply: rate limited by the RPC"]
    assert "SECRET" not in rep.reasons[0]


def test_background_scan_reports_scanning_then_the_verdict():
    chain = FakeChain([[("CREATOR", 10_000_000.0)]], holdings={})
    chain.gate = threading.Event()
    sc = scanner_for(chain)
    sc.background = True
    assert sc.check(MINT, NOW).reasons == [SCANNING]
    assert sc.check(MINT, NOW).reasons == [SCANNING]  # still running: not queued twice
    chain.gate.set()
    deadline = time.monotonic() + 10
    while sc.result(MINT) is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert sc.check(MINT, NOW).ok and chain.calls.count("getTokenSupply") == 1


def test_wiring(tmp_path):
    from memecoin_agent import cli

    cfg = Config()
    cfg.filters.require_rugcheck = False
    cfg.larpcheck.enabled = False
    cfg.gmgn.enabled = False
    agent, _ = cli.build_agent(cfg, with_feed=False)
    assert isinstance(agent.launch_scan, WalletScanner)
    cfg.launch.enabled = False
    assert isinstance(cli.build_agent(cfg, with_feed=False)[0].launch_scan, WalletScanner)  # early buys use it
    cfg.filters.wallet_scan_below_age_minutes = cfg.filters.wallet_scan_below_market_cap_usd = 0
    assert cli.build_agent(cfg, with_feed=False)[0].launch_scan is None
